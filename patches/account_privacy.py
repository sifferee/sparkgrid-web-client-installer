from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from connections import ensure_connection_schema, rotate_connection


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(accounts)")}
    required = {
        "web_privacy_status": "TEXT NOT NULL DEFAULT 'unchecked'",
        "web_privacy_checked_at": "TEXT NOT NULL DEFAULT ''",
        "web_privacy_last_error": "TEXT NOT NULL DEFAULT ''",
        "web_professional_status": "TEXT NOT NULL DEFAULT 'unchecked'",
        "web_professional_checked_at": "TEXT NOT NULL DEFAULT ''",
        "web_professional_category": "TEXT NOT NULL DEFAULT ''",
        "web_professional_last_error": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in required.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}")
    conn.commit()


def _material(conn: sqlite3.Connection, account: str) -> dict[str, Any] | None:
    ensure_connection_schema(conn)
    row = conn.execute(
        """
        SELECT a.name,COALESCE(a.account_role,'managed') AS account_role,
               COALESCE(a.web_upload_login_status,'') AS login_status,
               COALESCE(a.web_connection_id,0) AS connection_id,
               COALESCE(c.connection_type,'direct') AS connection_type,
               COALESCE(c.proxy_url,'') AS proxy_url,
               COALESCE(c.rotation_url,'') AS rotation_url,
               COALESCE(c.rotate_before_first,0) AS rotate_before_first
        FROM accounts a LEFT JOIN web_connections c ON c.id=a.web_connection_id
        WHERE a.name=?
        """,
        (account,),
    ).fetchone()
    return dict(row) if row else None


def _set_result(
    conn: sqlite3.Connection,
    account: str,
    status: str,
    error: str = "",
    professional: str = "",
    category: str = "",
) -> None:
    professional_sql = ""
    values: list[Any] = [status, str(error or "")[:1000]]
    if professional:
        professional_sql = ",web_professional_status=?,web_professional_checked_at=datetime('now'),web_professional_category=?,web_professional_last_error=''"
        values.extend([professional, category])
    values.append(account)
    conn.execute(
        f"""
        UPDATE accounts SET web_privacy_status=?,web_privacy_last_error=?,
            web_privacy_checked_at=datetime('now'){professional_sql},updated_at=datetime('now') WHERE name=?
        """,
        values,
    )
    conn.commit()


def _rotate_if_needed(conn: sqlite3.Connection, material: dict[str, Any]) -> None:
    if str(material.get("connection_type") or "") not in {"mobile", "phone"}:
        return
    connection_id = int(material.get("connection_id") or 0)
    if not connection_id or not str(material.get("rotation_url") or "").strip():
        raise RuntimeError("mobile connection has no rotation link")
    result = rotate_connection(conn, connection_id, sleep_after=True)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "mobile proxy rotation failed"))
    from connection_scheduler import probe_proxy_exit_ip

    ok, detail, _exit_ip = probe_proxy_exit_ip(str(material.get("proxy_url") or ""), timeout=15.0)
    if not ok:
        raise RuntimeError(f"proxy check after rotation failed: {detail}")


def _check_one(playwright: Any, conn: sqlite3.Connection, account: str, rotate_mobile: bool) -> dict[str, str]:
    from instagram_private_web_api_upload import (
        _extract_page_tokens,
        _has_login_cookies,
        _headers,
        _load_storage_state,
        _request_context,
        _tokens_from_storage_state,
        _user_agent,
    )

    material = _material(conn, account)
    if not material or str(material.get("account_role") or "managed") != "managed":
        raise RuntimeError("working account not found")
    proxy = str(material.get("proxy_url") or "")
    if rotate_mobile:
        _rotate_if_needed(conn, material)
    state = _load_storage_state(account, proxy)
    if not _has_login_cookies(state):
        raise RuntimeError("saved Instagram API session is unavailable; run Auto login first")
    api = _request_context(playwright, state or {}, _user_agent(account, proxy), proxy)
    try:
        boot = api.get("https://www.instagram.com/?hl=en", timeout=90_000, fail_on_status_code=False)
        if int(boot.status) in {401, 403, 429} or int(boot.status) >= 500:
            raise RuntimeError(f"Instagram API bootstrap HTTP {int(boot.status)}")
        tokens = _extract_page_tokens(str(boot.text() or ""))
        for key, value in _tokens_from_storage_state(api.storage_state()).items():
            if value:
                tokens[key] = value
        response = api.get(
            "https://www.instagram.com/api/v1/accounts/current_user/",
            params={"edit": "true"},
            headers=_headers(api, tokens, referer="https://www.instagram.com/accounts/edit/"),
            timeout=90_000,
            fail_on_status_code=False,
        )
        status = int(response.status)
        try:
            payload = response.json()
        except Exception:
            payload = json.loads(str(response.text() or "{}"))
        lowered = json.dumps(payload, ensure_ascii=False)[:100000].lower()
        if status in {401, 403, 429} or any(
            marker in lowered for marker in ("login_required", "checkpoint_required", "challenge_required", "consent_required")
        ):
            raise RuntimeError(f"Instagram API session blocked (HTTP {status})")
        user = payload.get("user", payload) if isinstance(payload, dict) else {}
        value = user.get("is_private") if isinstance(user, dict) else None
        if not isinstance(value, bool):
            raise RuntimeError(f"privacy field missing in Instagram response (HTTP {status})")

        # Do not label an account private from one occasionally stale response.
        # The public-profile endpoint describes the profile as other Instagram
        # clients see it and must agree with current_user before the UI receives
        # a destructive PRIVATE label.
        profile_response = api.get(
            "https://www.instagram.com/api/v1/users/web_profile_info/",
            params={"username": account},
            headers=_headers(api, tokens, referer=f"https://www.instagram.com/{account}/"),
            timeout=90_000,
            fail_on_status_code=False,
        )
        profile_status = int(profile_response.status)
        try:
            profile_payload = profile_response.json()
        except Exception:
            profile_payload = json.loads(str(profile_response.text() or "{}"))
        profile_user = (
            (profile_payload.get("data") or {}).get("user")
            if isinstance(profile_payload, dict)
            else None
        )
        profile_value = profile_user.get("is_private") if isinstance(profile_user, dict) else None
        if not isinstance(profile_value, bool):
            raise RuntimeError(
                f"privacy confirmation missing in profile response (HTTP {profile_status})"
            )
        if profile_value is not value:
            raise RuntimeError(
                "privacy endpoints disagree; status left unknown for safety"
            )
        professional = bool(user.get("is_professional_account"))
        account_type = int(user.get("account_type") or 0) if str(user.get("account_type") or "0").isdigit() else 0
        if professional:
            professional_status = "business" if bool(user.get("is_business")) or account_type == 2 else "creator"
        else:
            professional_status = "personal"
        return {
            "privacy": "private" if value else "public",
            "professional": professional_status,
            "category": str(user.get("category") or user.get("category_name") or ""),
            "privacy_confirmed_by": "current_user+web_profile_info",
        }
    finally:
        api.dispose()


def verify_account_state(account: str, rotate_mobile: bool = False) -> dict[str, str]:
    """Verify privacy/professional state through the saved authenticated API session."""
    from playwright.sync_api import sync_playwright

    clean = str(account or "").strip().lstrip("@")
    if not clean:
        raise ValueError("account is required")
    conn = db_conn()
    try:
        ensure_schema(conn)
        with sync_playwright() as playwright:
            state = _check_one(playwright, conn, clean, bool(rotate_mobile))
        _set_result(
            conn,
            clean,
            state["privacy"],
            professional=state["professional"],
            category=state.get("category", ""),
        )
        return state
    finally:
        conn.close()


def check_accounts(accounts: list[str]) -> int:
    from playwright.sync_api import sync_playwright

    clean = list(dict.fromkeys(str(name).strip().lstrip("@") for name in accounts if str(name).strip()))
    if not clean:
        return 0
    failures = 0
    previous_mobile_connection = 0
    with sync_playwright() as playwright:
        for account in clean:
            conn = db_conn()
            try:
                ensure_schema(conn)
                material = _material(conn, account) or {}
                connection_id = int(material.get("connection_id") or 0)
                is_mobile = str(material.get("connection_type") or "") in {"mobile", "phone"}
                # Shared mobile identities are rotated before the first check
                # and between accounts. Static accounts retain their dedicated
                # IP and are still processed sequentially.
                rotate_mobile = bool(is_mobile and (connection_id != previous_mobile_connection or previous_mobile_connection != 0))
                if is_mobile and previous_mobile_connection == 0:
                    rotate_mobile = True
                try:
                    state = _check_one(playwright, conn, account, rotate_mobile)
                    _set_result(
                        conn,
                        account,
                        state["privacy"],
                        professional=state["professional"],
                        category=state.get("category", ""),
                    )
                    print(f"[OK] {account}: {state['privacy']}, {state['professional']}", flush=True)
                except Exception as exc:
                    failures += 1
                    _set_result(conn, account, "unknown", f"{type(exc).__name__}: {exc}")
                    print(f"[WARNING] {account}: {type(exc).__name__}: {exc}", flush=True)
                previous_mobile_connection = connection_id if is_mobile else 0
            finally:
                conn.close()
    return 1 if failures == len(clean) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SparkGrid account privacy checker")
    parser.add_argument("--accounts", required=True)
    args = parser.parse_args()
    return check_accounts(str(args.accounts or "").split(","))


if __name__ == "__main__":
    raise SystemExit(main())
