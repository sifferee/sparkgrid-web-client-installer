#!/usr/bin/env python3
"""Verify recently published Reels using the saved Instagram Web session."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from publishing_history import ensure_history_schema, finalize_publication_attempt, update_history, utc_after
from instagram_private_web_api_upload import (
    _account_proxy,
    _extract_page_tokens,
    _has_login_cookies,
    _headers,
    _load_storage_state,
    _request_context,
    _tokens_from_storage_state,
    _user_agent,
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"
LOCK_PATH = DATA_DIR / "jobs" / "publication_verifier.lock"
LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

RETRY_DELAYS = [60, 120, 300, 600, 900]


def log(message: str, level: str = "INFO") -> None:
    from log_config import log_to_file_and_print
    log_to_file_and_print("verifier", message, level)


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def due_rows(conn: sqlite3.Connection, limit: int) -> List[Dict[str, Any]]:
    ensure_history_schema(conn)
    rows = conn.execute(
        """
        SELECT h.*, COALESCE(a.proxy,'') AS proxy
        FROM ig_publishing_history h
        LEFT JOIN accounts a ON a.name=h.account_name
        WHERE h.status IN ('uploaded','uploaded_unverified','submitted_unverified','processing')
          AND h.next_verify_at!=''
          AND datetime(h.next_verify_at) <= datetime('now')
        ORDER BY datetime(h.next_verify_at), h.id
        LIMIT ?
        """,
        (max(1, min(int(limit), 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def response_json(response: Any) -> Dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {"value": value}
    except Exception:
        try:
            return {"raw": response.text()[:2000]}
        except Exception:
            return {}


def classify(payload: Dict[str, Any], status_code: int, row: Dict[str, Any]) -> tuple[str, str]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    item = items[0] if items and isinstance(items[0], dict) else None
    if item:
        pk = str(item.get("pk") or item.get("id") or "")
        code = str(item.get("code") or "")
        wanted = str(row.get("media_id") or "")
        if not wanted or pk == wanted or pk.startswith(wanted) or wanted.startswith(pk):
            return "verified", code
    message = str(payload.get("message") or payload.get("error_title") or payload.get("error") or payload.get("raw") or "")
    lowered = message.lower()
    if status_code == 200 and str(payload.get("status") or "").lower() == "ok" and items:
        return "verified", str((items[0] or {}).get("code") or "")
    if status_code in {401, 403} or any(x in lowered for x in ("login_required", "challenge_required", "checkpoint_required")):
        return "session", message or f"HTTP {status_code}"
    if status_code in {202, 429, 500, 502, 503, 504} or re.search(r"processing|transcod|not ready|try again|tempor", lowered):
        return "processing", message or f"HTTP {status_code}"
    if status_code == 404 or re.search(r"not found|media unavailable|does not exist", lowered):
        return "missing", message or "media not found"
    return "processing", message or f"unexpected verification response HTTP {status_code}"


def _profile_candidates(payload: Any) -> List[Dict[str, Any]]:
    """Extract public media identity/timestamps from evolving profile payloads."""
    found: Dict[str, Dict[str, Any]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            code = str(value.get("code") or value.get("shortcode") or "")
            media_id = str(value.get("pk") or value.get("id") or "")
            taken = value.get("taken_at") or value.get("taken_at_timestamp")
            typename = str(value.get("__typename") or value.get("media_type") or "").lower()
            is_video = bool(value.get("is_video")) or "video" in typename or str(value.get("product_type") or "").lower() in {"clips", "reels"}
            if code and taken and is_video:
                found[code] = {"shortcode": code, "media_id": media_id, "taken_at": int(taken)}
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return list(found.values())


def match_profile_candidate(payload: Any, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    boundary = str(row.get("share_clicked_at") or row.get("publish_intent_at") or row.get("created_at") or "")
    try:
        target = datetime.fromisoformat(boundary.replace(" ", "T") + "+00:00").timestamp()
    except Exception:
        return None
    candidates = [
        item for item in _profile_candidates(payload)
        if target - 300 <= float(item["taken_at"]) <= target + 1800
    ]
    return candidates[0] if len(candidates) == 1 else None


def _verify_profile_candidate(api: Any, tokens: Dict[str, str], account: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={account}"
    response = api.get(
        url, headers=_headers(api, tokens, referer=f"https://www.instagram.com/{account}/"),
        timeout=90_000, fail_on_status_code=False,
    )
    if int(response.status) != 200:
        return None
    return match_profile_candidate(response_json(response), row)


def verify_account(playwright: Any, account: str, proxy: str, rows: List[Dict[str, Any]]) -> None:
    state = _load_storage_state(account, proxy)
    if not _has_login_cookies(state):
        for row in rows:
            note_retry(row, "saved Web session is not available for verification", session_problem=True)
        return
    api = _request_context(playwright, state or {}, _user_agent(account, proxy), proxy)
    try:
        boot = api.get("https://www.instagram.com/?hl=en", timeout=90_000, fail_on_status_code=False)
        if boot.status >= 400:
            for row in rows:
                note_retry(row, f"verification bootstrap HTTP {boot.status}", session_problem=True)
            return
        tokens = _extract_page_tokens(boot.text())
        current_state = api.storage_state()
        for key, value in _tokens_from_storage_state(current_state).items():
            if value:
                tokens[key] = value
        for row in rows:
            media_id = str(row.get("media_id") or "")
            shortcode = str(row.get("shortcode") or "")
            if not media_id and not shortcode:
                try:
                    candidate = _verify_profile_candidate(api, tokens, account, row)
                except Exception:
                    candidate = None
                if candidate:
                    media_id = str(candidate.get("media_id") or "")
                    shortcode = str(candidate.get("shortcode") or "")
                    conn = db_conn()
                    try:
                        final = finalize_publication_attempt(
                            conn, history_id=int(row["id"]), job_id=int(row["job_id"]),
                            outcome="confirmed", asset_id=int(row.get("asset_id") or 0),
                            account_name=str(row.get("account_name") or ""),
                            job_status="success", job_step="delayed profile verification",
                            observation={
                                "media_id": media_id, "shortcode": shortcode,
                                "permalink": f"https://www.instagram.com/reel/{shortcode}/" if shortcode else "",
                            },
                        )
                        if not (final.get("committed") or final.get("already_finalized")):
                            raise RuntimeError(final.get("error") or "profile verification finalization failed")
                    finally:
                        conn.close()
                    log(f"{account}: history #{row['id']} verified from profile", "OK")
                    continue
                note_retry(row, "published Reel not uniquely identifiable in profile yet")
                continue
            if media_id:
                url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
            else:
                url = f"https://www.instagram.com/api/v1/media/shortcode/{shortcode}/info/"
            referer = str(row.get("permalink") or f"https://www.instagram.com/{account}/")
            try:
                response = api.get(url, headers=_headers(api, tokens, referer=referer), timeout=90_000, fail_on_status_code=False)
                payload = response_json(response)
                kind, detail = classify(payload, int(response.status), row)
                if kind == "verified":
                    conn = db_conn()
                    try:
                        code = detail or shortcode
                        permalink = str(row.get("permalink") or (f"https://www.instagram.com/reel/{code}/" if code else ""))
                        if str(row.get("status") or "") == "submitted_unverified" and int(row.get("job_id") or 0):
                            final = finalize_publication_attempt(
                                conn, history_id=int(row["id"]), job_id=int(row["job_id"]),
                                outcome="confirmed", asset_id=int(row.get("asset_id") or 0),
                                account_name=str(row.get("account_name") or ""),
                                job_status="success", job_step="manually verified confirmed",
                            )
                            if not (final.get("committed") or final.get("already_finalized")):
                                raise RuntimeError(final.get("error") or "manual verification finalization failed")
                        else:
                            update_history(
                                conn, int(row["id"]), status="verified", shortcode=code, permalink=permalink,
                                error="", verified_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                last_checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), next_verify_at="",
                                verification_attempts=int(row.get("verification_attempts") or 0) + 1,
                            )
                    finally:
                        conn.close()
                    log(f"{account}: history #{row['id']} verified", "OK")
                else:
                    note_retry(row, detail, session_problem=(kind == "session"), missing=(kind == "missing"))
            except Exception as exc:
                note_retry(row, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            api.dispose()
        except Exception:
            pass


def note_retry(row: Dict[str, Any], error: str, *, session_problem: bool = False, missing: bool = False) -> None:
    attempts = int(row.get("verification_attempts") or 0) + 1
    published = str(row.get("published_at") or row.get("created_at") or "")
    age_seconds = 0.0
    try:
        age_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(published.replace(" ", "T") + "+00:00")).total_seconds()
    except Exception:
        pass
    final = attempts >= 5 and age_seconds >= 15 * 60 and not session_problem
    status = "unavailable" if final else "processing"
    delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
    conn = db_conn()
    try:
        update_history(
            conn,
            int(row["id"]),
            status=status,
            error=str(error or ("media unavailable" if missing else "verification pending"))[:4000],
            verification_attempts=attempts,
            last_checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            next_verify_at="" if final else utc_after(delay),
        )
        # When a publication is definitively unavailable (5/5 attempts, 15+ min
        # old, not a session problem), the asset was marked "uploaded" by the
        # worker's optimistic accounting but Instagram never actually published
        # it.  Roll the asset back to "ready" so it re-enters the queue and the
        # UI stops counting it as published.  Decrement the job's posted_count
        # and relabel the job partial_success so the overview reflects reality.
        if final:
            asset_id = int(row.get("asset_id") or 0)
            job_id = int(row.get("job_id") or 0)
            if asset_id:
                conn.execute(
                    "UPDATE api_content_assets SET status='ready', last_error=?, updated_at=datetime('now') WHERE id=?",
                    (str(error or "publication unavailable after verification")[:4000], asset_id),
                )
            if job_id:
                job_row = conn.execute(
                    "SELECT posted_count, target_uploads, status FROM ig_web_upload_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if job_row:
                    new_posted = max(0, int(job_row["posted_count"] or 0) - 1)
                    target = int(job_row["target_uploads"] or 0)
                    job_status = str(job_row["status"] or "")
                    # Only demote a job that was considered successful; failed
                    # or already-partial jobs keep their status.
                    if job_status in {"success", "partial_success"}:
                        if new_posted < target:
                            conn.execute(
                                "UPDATE ig_web_upload_jobs SET status='partial_success',"
                                " posted_count=?, current_step=?, last_error=?, updated_at=datetime('now') WHERE id=?",
                                (new_posted, f"verified {new_posted}/{target}; unavailable asset reverted", str(error or "publication unavailable")[:4000], job_id),
                            )
                        else:
                            conn.execute(
                                "UPDATE ig_web_upload_jobs SET posted_count=?, updated_at=datetime('now') WHERE id=?",
                                (new_posted, job_id),
                            )
            conn.commit()
    finally:
        conn.close()
    log(f"{row.get('account_name')}: history #{row['id']} -> {status} ({attempts}/5): {error}", "WARNING")


def run_once(limit: int) -> int:
    conn = db_conn()
    try:
        rows = due_rows(conn, limit)
    finally:
        conn.close()
    if not rows:
        return 0
    by_account: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_account.setdefault(str(row.get("account_name") or ""), []).append(row)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log(f"Playwright unavailable: {exc}", "ERROR")
        return 2
    with sync_playwright() as p:
        for account, account_rows in by_account.items():
            verify_account(p, account, str(account_rows[0].get("proxy") or ""), account_rows)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return run_once(args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
