"""Vision fallback: last-resort element location via a vision model.

WHEN TO CALL THIS — critical distinction, read before wiring in:

    Call click_via_vision() ONLY after the existing structural/text-based
    search has completed WITHOUT raising an exception and found nothing
    (e.g. reason == 'action_unavailable', 'container_missing', or similar
    clean "not found" outcomes already used across this codebase).

    NEVER call this from inside an `except Exception` block to paper over
    a real code error. A Python exception means the code itself is broken
    (syntax error, bad reference, whatever) — that needs a real fix, not a
    vision workaround. This project already lost two weeks to a duplicate
    `const` declaration that got silently swallowed by a broad except
    block; routing exceptions through vision would recreate exactly that
    failure mode, just less visibly. Exceptions still go through the
    normal logger.debug(...) path already in place everywhere else in
    this codebase.

    In short: vision is for "the button's wording/position changed and we
    honestly couldn't find it," not for "something crashed."

RETURN SHAPE matches the {ok, reason, ...} convention already used
throughout blocking_popup_transaction.py / initial_browser_load.py, so
call sites can handle it the same way they handle every other internal
result dict.

GRACEFUL DEGRADATION: if the API key is missing, invalid, or out of
credit, this returns {"ok": False, "reason": "vision_unavailable", ...}
instead of raising — callers should treat that exactly like the old
"nothing found" outcome (fail this one action, keep going) rather than
crashing. After an auth/billing failure, vision disables itself for
VISION_COOLDOWN_SECONDS so a dead key doesn't get hit on every account
in the meantime.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from typing import Any

VISION_MODEL = "claude-haiku-4-5-20251001"  # matches the exact model name enabled on the token — no -thinking suffix, this task doesn't need it
VISION_COOLDOWN_SECONDS = 30 * 60  # after an auth/billing failure, stop trying for 30 min
VISION_MAX_TOKENS = 300

_disabled_until = 0.0
_disabled_reason = ""
_lock = threading.Lock()


def _is_disabled() -> tuple[bool, str]:
    with _lock:
        if time.monotonic() < _disabled_until:
            return True, _disabled_reason
        return False, ""


def _disable_for_cooldown(reason: str) -> None:
    global _disabled_until, _disabled_reason
    with _lock:
        _disabled_until = time.monotonic() + VISION_COOLDOWN_SECONDS
        _disabled_reason = reason


def _extract_json(text: str) -> dict[str, Any] | None:
    """Model responses can include stray text around the JSON despite
    instructions — pull out the first {...} block rather than trusting
    the whole response to be clean JSON."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None


def click_via_vision(
    page: Any,
    *,
    intent: str,
    api_key: str | None = None,
    base_url: str | None = None,
    client_factory: Any = None,
) -> dict[str, Any]:
    """Locate an element by visual intent and click it.

    Args:
        page: Playwright page (must support .screenshot() and .mouse.click()).
        intent: plain-language description of what to find, e.g.
            "the button that accepts all cookies" — not a selector, not a
            phrase to match verbatim, an actual description.
        api_key: overrides ANTHROPIC_API_KEY env var (mainly for tests).
        base_url: overrides ANTHROPIC_BASE_URL env var. Lets this point at
            a gateway/reseller (e.g. apiyi.com) instead of api.anthropic.com
            directly, without changing any calling code. None/unset means
            "use the SDK's own default (official Anthropic endpoint)."
        client_factory: overrides the Anthropic client constructor (tests only).

    Returns:
        {"ok": True, "reason": "clicked", "x": int, "y": int, "confidence": str}
        or
        {"ok": False, "reason": "vision_unavailable" | "not_found" | "parse_failed", "detail": str}
    """
    disabled, disabled_reason = _is_disabled()
    if disabled:
        return {"ok": False, "reason": "vision_unavailable", "detail": disabled_reason}

    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"ok": False, "reason": "vision_unavailable", "detail": "no_api_key"}

    resolved_base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL") or None

    try:
        import anthropic
    except ImportError:
        return {"ok": False, "reason": "vision_unavailable", "detail": "sdk_not_installed"}

    try:
        screenshot_bytes = page.screenshot()
    except Exception as exc:
        return {"ok": False, "reason": "screenshot_failed", "detail": f"{type(exc).__name__}: {exc}"}

    image_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
    prompt = (
        f"Look at this screenshot of a web page. Find: {intent}\n\n"
        "Respond with ONLY a JSON object, no other text:\n"
        '{"found": true/false, "x": <int center-x in pixels>, '
        '"y": <int center-y in pixels>, "confidence": "high"|"medium"|"low"}\n'
        'If you cannot find it, respond {"found": false}.'
    )

    make_client = client_factory or anthropic.Anthropic
    client_kwargs: dict[str, Any] = {"api_key": key}
    if resolved_base_url:
        client_kwargs["base_url"] = resolved_base_url
    client = make_client(**client_kwargs)

    try:
        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=VISION_MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except anthropic.AuthenticationError as exc:
        _disable_for_cooldown("auth_error")
        return {"ok": False, "reason": "vision_unavailable", "detail": f"auth_error: {exc}"}
    except (anthropic.PermissionDeniedError, anthropic.RateLimitError) as exc:
        _disable_for_cooldown("billing_or_rate_limit")
        return {"ok": False, "reason": "vision_unavailable", "detail": f"{type(exc).__name__}: {exc}"}
    except anthropic.APIError as exc:
        # Transient (network, 5xx, timeout) — do NOT disable, just fail this one call.
        return {"ok": False, "reason": "vision_unavailable", "detail": f"{type(exc).__name__}: {exc}"}

    text_parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    raw_text = "\n".join(text_parts)
    parsed = _extract_json(raw_text)
    if parsed is None:
        return {"ok": False, "reason": "parse_failed", "detail": raw_text[:200]}

    if not parsed.get("found"):
        return {"ok": False, "reason": "not_found", "detail": intent}

    try:
        x = int(parsed["x"])
        y = int(parsed["y"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "reason": "parse_failed", "detail": f"bad coordinates: {parsed}"}

    try:
        page.mouse.click(x, y)
    except Exception as exc:
        return {"ok": False, "reason": "click_failed", "detail": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "reason": "clicked",
        "x": x,
        "y": y,
        "confidence": str(parsed.get("confidence", "unknown")),
    }
