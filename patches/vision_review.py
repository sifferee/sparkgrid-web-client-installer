"""Periodic reviewer for vision_fallback successes.

Reads unreviewed entries from logs/vision_fallback/successes.jsonl, asks a
stronger model (Sonnet) to propose a specific, minimal fast-path fix (a
regex/text pattern addition) that would let the structural search handle
this case without vision next time, and sends the proposal to Telegram
admins with approve/deny buttons.

Run this PERIODICALLY (Task Scheduler, once a day/week) — it is a separate
process from the running automation and never touches production code
directly. It only proposes; a human approves, and approval only produces
a ready-to-relay Hermes task (see telegram_bot.py's vfapprove/vfdeny
callback handlers) — nothing here auto-applies code changes.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ".")
VISION_LOG_DIR = DATA_DIR / "logs" / "vision_fallback"
SUCCESSES_FILE = VISION_LOG_DIR / "successes.jsonl"
USERS_FILE = DATA_DIR / "telegram_users.json"

# Matches the claude-sonnet-5-thinking token config — a stronger model for
# reasoning about a code fix than the Haiku used for live detection.
REVIEW_MODEL = "claude-sonnet-5-thinking"
REVIEW_MAX_TOKENS = 600


def _load_records() -> list[dict[str, Any]]:
    if not SUCCESSES_FILE.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in SUCCESSES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip a corrupt line rather than crash the whole reviewer
    return records


def _replace_with_retry(tmp, path, attempts: int = 5, delay_seconds: float = 0.15) -> None:
    """Same WinError 5 fix as browser_launcher.py, 2026-08-16."""
    last_exc = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    raise last_exc


def _save_records(records: list[dict[str, Any]]) -> None:
    tmp = SUCCESSES_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    _replace_with_retry(tmp, SUCCESSES_FILE)  # atomic — same lesson as save_users()


def _admin_chat_ids() -> list[int]:
    if not USERS_FILE.exists():
        return []
    try:
        users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [
        int(cid) for cid, info in users.items()
        if isinstance(info, dict) and info.get("role") == "admin"
    ]


def _propose_fix(record: dict[str, Any], *, client_factory: Any = None) -> str | None:
    """Ask Sonnet to propose a specific fast-path fix. Returns None on any
    failure — this reviewer is best-effort and must never raise."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or None
    if not api_key:
        return None
    screenshot_path = record.get("screenshot")
    if not screenshot_path or not Path(screenshot_path).exists():
        return None
    try:
        import anthropic
    except ImportError:
        return None

    try:
        image_b64 = base64.b64encode(Path(screenshot_path).read_bytes()).decode("ascii")
    except OSError:
        return None

    prompt = (
        "A browser automation's fast/structural button-finder could not "
        f"locate this element and had to fall back to a vision model: "
        f"{record.get('intent')}\n\n"
        "Look at the screenshot. Propose a SPECIFIC, minimal addition to a "
        "regex/text-matching pattern (e.g. an exact button label variant, "
        "an aria-label pattern) that would let a fast text-based search "
        "find this element without needing vision. Be concrete — quote "
        "the exact visible text of the button/element. If you cannot "
        "identify anything specific and reliable, say so plainly instead "
        "of guessing."
    )

    make_client = client_factory or anthropic.Anthropic
    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = make_client(**client_kwargs)

    try:
        response = client.messages.create(
            model=REVIEW_MODEL,
            max_tokens=REVIEW_MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception:
        return None

    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    text = "\n".join(parts).strip()
    return text or None


def _send_telegram_proposal(
    chat_id: int, record: dict[str, Any], proposal: str, *, sender: Any = None,
) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False
    text = (
        f"🔍 Зрение сработало для: {record.get('intent')}\n\n"
        f"Предложение Sonnet:\n{proposal[:1200]}"
    )
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Оформить как задачу Гермесу", "callback_data": f"vfapprove:{record['id']}"},
                {"text": "❌ Не нужно", "callback_data": f"vfdeny:{record['id']}"},
            ]],
        },
    }
    send = sender or _default_sender
    return send(token, payload)


def _default_sender(token: str, payload: dict[str, Any]) -> bool:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def run(*, client_factory: Any = None, sender: Any = None) -> dict[str, int]:
    records = _load_records()
    admins = _admin_chat_ids()
    stats = {
        "total": len(records),
        "unreviewed": 0,
        "proposed": 0,
        "sent": 0,
        "skipped_no_admin": 0,
    }

    if not admins:
        stats["skipped_no_admin"] = sum(1 for r in records if not r.get("reviewed"))
        return stats

    changed = False
    for record in records:
        if record.get("reviewed"):
            continue
        stats["unreviewed"] += 1
        proposal = _propose_fix(record, client_factory=client_factory)
        # Mark reviewed regardless of outcome — a bad/missing screenshot
        # shouldn't get retried forever on every run.
        record["reviewed"] = True
        changed = True
        if not proposal:
            continue
        stats["proposed"] += 1
        record["proposal"] = proposal
        sent_to_any = False
        for chat_id in admins:
            if _send_telegram_proposal(chat_id, record, proposal, sender=sender):
                sent_to_any = True
        if sent_to_any:
            stats["sent"] += 1

    if changed:
        _save_records(records)
    return stats


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
