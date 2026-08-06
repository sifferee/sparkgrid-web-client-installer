from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from connections import ensure_connection_schema, rotate_connection


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"

VIEW_FIELDS = (
    "video_play_count",
    "play_count",
    "video_view_count",
    "videoViewCount",
    "view_count",
    "views",
)
PARSER_TARGET_ATTEMPTS = 2
REELS_QUERY_NAME = "PolarisProfileReelsTabContentQuery"
REELS_PAGE_SIZE = 12
REELS_MAX_PAGES = 25
_REELS_DOC_ID_CACHE = ""


class ParserBlocked(RuntimeError):
    """The authenticated parser identity cannot safely continue."""


def log(message: str, level: str = "INFO") -> None:
    from log_config import log_to_file_and_print
    log_to_file_and_print("analytics", message, level)


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def ensure_view_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS view_analytics_settings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            enabled INTEGER NOT NULL DEFAULT 0,
            interval_minutes INTEGER NOT NULL DEFAULT 60,
            mobile_connection_id INTEGER NOT NULL DEFAULT 0,
            last_status TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_run_at TEXT NOT NULL DEFAULT '',
            next_run_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO view_analytics_settings(id) VALUES(1)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS view_analytics_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER NOT NULL DEFAULT 0 UNIQUE,
            account_name TEXT NOT NULL COLLATE NOCASE,
            media_id TEXT NOT NULL DEFAULT '',
            shortcode TEXT NOT NULL DEFAULT '',
            permalink TEXT NOT NULL DEFAULT '',
            views INTEGER,
            views_field TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            public_attempts INTEGER NOT NULL DEFAULT 0,
            session_attempts INTEGER NOT NULL DEFAULT 0,
            parser_attempts INTEGER NOT NULL DEFAULT 0,
            last_parser TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '',
            next_check_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    _ensure_column(conn, "view_analytics_targets", "parser_attempts", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "view_analytics_targets", "last_parser", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS view_analytics_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL,
            views INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'parser_pool',
            views_field TEXT NOT NULL DEFAULT '',
            exit_ip TEXT NOT NULL DEFAULT '',
            captured_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS view_analytics_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL DEFAULT 'parser_pool',
            status TEXT NOT NULL DEFAULT 'running',
            connection_id INTEGER NOT NULL DEFAULT 0,
            checked INTEGER NOT NULL DEFAULT 0,
            parsed INTEGER NOT NULL DEFAULT 0,
            unparsed INTEGER NOT NULL DEFAULT 0,
            rotations INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS view_parser_accounts (
            account_name TEXT PRIMARY KEY COLLATE NOCASE,
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'login_required',
            cooldown_until TEXT NOT NULL DEFAULT '',
            request_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            last_success_at TEXT NOT NULL DEFAULT '',
            last_used_at TEXT NOT NULL DEFAULT '',
            last_exit_ip TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if "accounts" in {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        _ensure_column(conn, "accounts", "account_role", "TEXT NOT NULL DEFAULT 'managed'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_view_targets_due ON view_analytics_targets(status,next_check_at,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_view_targets_account ON view_analytics_targets(account_name,status,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_view_samples_target ON view_analytics_samples(target_id,id)")
    # One-time compatibility migration from the removed anonymous public mode.
    conn.execute(
        """
        UPDATE view_analytics_targets
        SET status=CASE
            WHEN status='session_success' THEN 'own_api_success'
            WHEN status IN ('public_unparsed','session_required') THEN 'unparsed'
            WHEN status IN ('public_success','public_retry') THEN 'pending'
            ELSE status END,
            updated_at=datetime('now')
        WHERE status IN ('session_success','public_unparsed','session_required','public_success','public_retry')
        """
    )
    conn.commit()


def sync_targets(conn: sqlite3.Connection) -> int:
    ensure_view_schema(conn)
    try:
        rows = conn.execute(
            """
            SELECT id,account_name,COALESCE(media_id,'') AS media_id,
                   COALESCE(shortcode,'') AS shortcode,COALESCE(permalink,'') AS permalink
            FROM ig_publishing_history
            WHERE status IN ('uploaded','verified','processing')
              AND (
                    COALESCE(media_id,'')!=''
                 OR COALESCE(shortcode,'')!=''
                 OR COALESCE(permalink,'')!=''
              )
            ORDER BY id
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    created = 0
    for row in rows:
        permalink = str(row["permalink"] or "").strip()
        shortcode = str(row["shortcode"] or "").strip()
        if not permalink and shortcode:
            permalink = f"https://www.instagram.com/reel/{shortcode}/"
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO view_analytics_targets(
                history_id,account_name,media_id,shortcode,permalink,status,next_check_at
            ) VALUES(?,?,?,?,?,'pending',datetime('now'))
            """,
            (int(row["id"]), str(row["account_name"]), str(row["media_id"]), shortcode, permalink),
        )
        created += int(cur.rowcount or 0)
        conn.execute(
            """
            UPDATE view_analytics_targets
            SET account_name=?,media_id=?,shortcode=?,permalink=?,updated_at=datetime('now')
            WHERE history_id=?
            """,
            (str(row["account_name"]), str(row["media_id"]), shortcode, permalink, int(row["id"])),
        )
    conn.commit()
    return created


def settings(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_view_schema(conn)
    value = dict(conn.execute("SELECT * FROM view_analytics_settings WHERE id=1").fetchone())
    value["enabled"] = bool(int(value.get("enabled") or 0))
    return value


def save_settings(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    ensure_view_schema(conn)
    enabled = 1 if bool(body.get("enabled")) else 0
    interval = max(15, min(int(body.get("interval_minutes") or 60), 7 * 24 * 60))
    current = settings(conn)
    if enabled:
        next_run = current.get("next_run_at") if current.get("enabled") else ""
        if not next_run:
            conn.execute(
                """
                UPDATE view_analytics_settings SET enabled=1,interval_minutes=?,
                    next_run_at=datetime('now',?),last_error='',updated_at=datetime('now') WHERE id=1
                """,
                (interval, f"+{interval} minutes"),
            )
        else:
            conn.execute(
                "UPDATE view_analytics_settings SET enabled=1,interval_minutes=?,last_error='',updated_at=datetime('now') WHERE id=1",
                (interval,),
            )
    else:
        conn.execute(
            "UPDATE view_analytics_settings SET enabled=0,interval_minutes=?,next_run_at='',last_error='',updated_at=datetime('now') WHERE id=1",
            (interval,),
        )
    conn.commit()
    return settings(conn)


def due_targets(conn: sqlite3.Connection, limit: int = 0, force: bool = False) -> list[dict[str, Any]]:
    ensure_view_schema(conn)
    sync_targets(conn)
    due_clause = "" if force else "AND (t.next_check_at='' OR datetime(t.next_check_at)<=datetime('now'))"
    sql = f"""
        SELECT t.*,COALESCE(h.video_name,'') AS video_name,COALESCE(h.published_at,'') AS published_at
        FROM view_analytics_targets t
        LEFT JOIN ig_publishing_history h ON h.id=t.history_id
        WHERE t.status IN ('pending','parser_success','parser_retry')
          {due_clause}
        ORDER BY CASE t.status
            WHEN 'pending' THEN 0 WHEN 'parser_retry' THEN 1 WHEN 'unparsed' THEN 2 ELSE 3 END,t.id
    """
    params: tuple[Any, ...] = ()
    if int(limit or 0) > 0:
        sql += " LIMIT ?"
        params = (max(1, int(limit)),)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def parser_accounts(conn: sqlite3.Connection, include_disabled: bool = True) -> list[dict[str, Any]]:
    ensure_view_schema(conn)
    where = "" if include_disabled else "WHERE p.enabled=1"
    rows = conn.execute(
        f"""
        SELECT p.*,COALESCE(a.web_upload_login_status,'') AS login_status,
               COALESCE(a.web_upload_profile_status,'') AS profile_status,
               COALESCE(a.web_upload_last_error,'') AS login_error,
               COALESCE(a.web_connection_id,0) AS connection_id,
               COALESCE(c.name,'Direct') AS connection_name,
               COALESCE(c.connection_type,'direct') AS connection_type,
               CASE WHEN COALESCE(c.proxy_url,'')!='' THEN 1 ELSE 0 END AS has_proxy,
               CASE WHEN COALESCE(c.rotation_url,'')!='' THEN 1 ELSE 0 END AS has_rotation,
               COALESCE(c.last_status,'') AS connection_status,
               COALESCE(c.last_ip,'') AS connection_last_ip
        FROM view_parser_accounts p
        LEFT JOIN accounts a ON a.name=p.account_name
        LEFT JOIN web_connections c ON c.id=a.web_connection_id
        {where}
        ORDER BY p.enabled DESC,p.created_at,p.account_name
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for raw in rows:
        item = dict(raw)
        cooldown = _parse_db_time(str(item.get("cooldown_until") or ""))
        login_status = str(item.get("login_status") or "").lower()
        if not bool(int(item.get("enabled") or 0)):
            display = "disabled"
        elif cooldown and cooldown > now:
            display = "cooldown"
        elif login_status == "logged_in":
            display = "working" if str(item.get("status")) == "working" else "ready"
        elif "incorrect" in login_status:
            display = "invalid_credentials"
        elif any(marker in login_status for marker in ("challenge", "checkpoint", "restrict")):
            display = "challenge"
        elif str(item.get("status") or "") == "logging_in":
            display = "logging_in"
        else:
            display = "login_required"
        item["display_status"] = display
        result.append(item)
    return result


def analytics_overview(conn: sqlite3.Connection, limit: int = 500) -> dict[str, Any]:
    ensure_view_schema(conn)
    sync_targets(conn)
    rows = conn.execute(
        """
        SELECT t.*,COALESCE(h.video_name,'') AS video_name,COALESCE(h.published_at,'') AS published_at
        FROM view_analytics_targets t
        LEFT JOIN ig_publishing_history h ON h.id=t.history_id
        ORDER BY CASE t.status WHEN 'unparsed' THEN 0 WHEN 'own_session_required' THEN 1
            WHEN 'pending' THEN 2 ELSE 3 END,
            datetime(COALESCE(NULLIF(t.last_checked_at,''),t.created_at)) DESC,t.id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit or 500), 5000)),),
    ).fetchall()
    summary_rows = conn.execute("SELECT status,COUNT(*) AS count FROM view_analytics_targets GROUP BY status").fetchall()
    latest_runs = conn.execute("SELECT * FROM view_analytics_runs ORDER BY id DESC LIMIT 30").fetchall()
    return {
        "settings": settings(conn),
        "summary": {str(row["status"]): int(row["count"] or 0) for row in summary_rows},
        "targets": [dict(row) for row in rows],
        "runs": [dict(row) for row in latest_runs],
        "parser_accounts": parser_accounts(conn),
    }


def register_parser_accounts(
    conn: sqlite3.Connection,
    parsed: list[dict[str, str]],
    connection_id: int,
    proxy_url: str,
) -> dict[str, Any]:
    ensure_view_schema(conn)
    created = updated = 0
    names: list[str] = []
    conflicts: list[str] = []
    for item in parsed:
        name = str(item.get("name") or "").strip().lstrip("@")
        if not name:
            continue
        existing = conn.execute(
            "SELECT COALESCE(account_role,'managed') AS role,COALESCE(web_upload_enabled,1) AS upload FROM accounts WHERE name=?",
            (name,),
        ).fetchone()
        if existing and str(existing["role"] or "managed") != "parser":
            conflicts.append(name)
            continue
        if existing:
            conn.execute(
                """
                UPDATE accounts SET password=CASE WHEN ?!='' THEN ? ELSE password END,
                    api_password=CASE WHEN ?!='' THEN ? ELSE api_password END,
                    api_totp_secret=CASE WHEN ?!='' THEN ? ELSE api_totp_secret END,
                    proxy=?,web_connection_id=?,enabled=1,warm_only=0,web_upload_enabled=0,
                    account_role='parser',web_upload_last_error='',updated_at=datetime('now') WHERE name=?
                """,
                (
                    item.get("password", ""), item.get("password", ""),
                    item.get("password", ""), item.get("password", ""),
                    item.get("totp", ""), item.get("totp", ""),
                    proxy_url, int(connection_id), name,
                ),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO accounts(
                    name,password,api_password,api_totp_secret,proxy,enabled,warm_only,status,
                    web_upload_enabled,web_connection_id,account_role
                ) VALUES(?,?,?,?,?,1,0,'ready',0,?,'parser')
                """,
                (name, item.get("password", ""), item.get("password", ""), item.get("totp", ""), proxy_url, int(connection_id)),
            )
            created += 1
        conn.execute(
            """
            INSERT INTO view_parser_accounts(account_name,enabled,status)
            VALUES(?,1,'login_required')
            ON CONFLICT(account_name) DO UPDATE SET enabled=1,status='login_required',
                cooldown_until='',last_error='',updated_at=datetime('now')
            """,
            (name,),
        )
        names.append(name)
    conn.commit()
    return {"created": created, "updated": updated, "accounts": names, "conflicts": conflicts}


def set_parser_accounts_enabled(conn: sqlite3.Connection, names: list[str], enabled: bool) -> int:
    ensure_view_schema(conn)
    clean = sorted({str(name).strip().lstrip("@") for name in names if str(name).strip()})
    if not clean:
        return 0
    placeholders = ",".join("?" for _ in clean)
    cur = conn.execute(
        f"UPDATE view_parser_accounts SET enabled=?,status=?,updated_at=datetime('now') WHERE account_name IN ({placeholders})",
        [1 if enabled else 0, "login_required" if enabled else "disabled", *clean],
    )
    conn.commit()
    return int(cur.rowcount or 0)


def remove_parser_account(conn: sqlite3.Connection, name: str) -> bool:
    ensure_view_schema(conn)
    name = str(name or "").strip().lstrip("@")
    cur = conn.execute("DELETE FROM view_parser_accounts WHERE account_name=?", (name,))
    conn.execute("DELETE FROM accounts WHERE name=? AND account_role='parser'", (name,))
    conn.commit()
    return bool(cur.rowcount)


def mark_parser_logging_in(conn: sqlite3.Connection, names: list[str]) -> int:
    ensure_view_schema(conn)
    clean = sorted({str(name).strip().lstrip("@") for name in names if str(name).strip()})
    if not clean:
        return 0
    placeholders = ",".join("?" for _ in clean)
    cur = conn.execute(
        f"UPDATE view_parser_accounts SET status='logging_in',cooldown_until='',last_error='',updated_at=datetime('now') WHERE enabled=1 AND account_name IN ({placeholders})",
        clean,
    )
    conn.commit()
    return int(cur.rowcount or 0)


def retry_public_targets(conn: sqlite3.Connection, target_ids: list[int] | None = None) -> int:
    """Compatibility alias: retries now go through Parser Pool, never public HTTP."""
    ensure_view_schema(conn)
    ids = sorted({int(value) for value in (target_ids or []) if int(value) > 0})
    where = "status IN ('unparsed','own_session_required','own_api_failed')"
    params: list[Any] = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        where += f" AND id IN ({placeholders})"
        params.extend(ids)
    cur = conn.execute(
        f"UPDATE view_analytics_targets SET status='parser_retry',parser_attempts=0,next_check_at=datetime('now'),last_error='',updated_at=datetime('now') WHERE {where}",
        params,
    )
    conn.commit()
    return int(cur.rowcount or 0)


def session_accounts_for_targets(conn: sqlite3.Connection, target_ids: list[int] | None = None) -> list[str]:
    ensure_view_schema(conn)
    ids = sorted({int(value) for value in (target_ids or []) if int(value) > 0})
    where = "t.status IN ('unparsed','own_session_required','own_api_failed')"
    params: list[Any] = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        where += f" AND id IN ({placeholders})"
        params.extend(ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT t.account_name
        FROM view_analytics_targets t
        JOIN accounts a ON a.name=t.account_name
        WHERE {where} AND COALESCE(a.account_role,'managed')='managed'
        ORDER BY t.account_name
        """,
        params,
    ).fetchall()
    return [str(row[0]) for row in rows]


def _extract_views_from_value(value: Any) -> tuple[int | None, str]:
    if isinstance(value, dict):
        for field in VIEW_FIELDS:
            candidate = value.get(field)
            if isinstance(candidate, (int, float)) and candidate >= 0:
                return int(candidate), field
            if isinstance(candidate, str) and candidate.isdigit():
                return int(candidate), field
        for nested in value.values():
            found, field = _extract_views_from_value(nested)
            if found is not None:
                return found, field
    elif isinstance(value, list):
        for nested in value:
            found, field = _extract_views_from_value(nested)
            if found is not None:
                return found, field
    return None, ""


def _response_payload(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        try:
            value = json.loads(response.text())
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _cookie_value(api: Any, name: str) -> str:
    try:
        for cookie in (api.storage_state() or {}).get("cookies", []):
            if str(cookie.get("name") or "") == name:
                return str(cookie.get("value") or "")
    except Exception:
        pass
    return ""


def _extract_lsd(page_html: str) -> str:
    text = str(page_html or "")
    patterns = (
        r'\["LSD",\[\],\{"token":"([^"]+)"\}',
        r'"lsd"\s*:\s*"([^"]+)"',
        r'name=["\']lsd["\'][^>]+value=["\']([^"\']+)',
    )
    for pattern in patterns:
        found = re.search(pattern, text, re.I)
        if found:
            return html.unescape(found.group(1))
    return ""


def _query_doc_id_from_text(text: str) -> str:
    """Read the current Relay artifact id without pinning an Instagram build."""
    source = str(text or "")
    name = re.escape(REELS_QUERY_NAME)
    patterns = (
        rf'id:"(\d{{10,}})".{{0,700}}?name:"{name}"',
        rf'name:"{name}".{{0,700}}?id:"(\d{{10,}})"',
        rf'doc_id["\']?\s*[:=]\s*["\'](\d{{10,}})["\'].{{0,700}}?{name}',
        rf'{name}.{{0,700}}?doc_id["\']?\s*[:=]\s*["\'](\d{{10,}})["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.S)
        if match:
            return match.group(1)
    return ""


def _discover_reels_doc_id(api: Any, profile_html: str, profile_url: str) -> str:
    """Discover the live Reels Relay query id from the current Web build."""
    global _REELS_DOC_ID_CACHE
    if _REELS_DOC_ID_CACHE:
        return _REELS_DOC_ID_CACHE
    direct = _query_doc_id_from_text(profile_html)
    if direct:
        _REELS_DOC_ID_CACHE = direct
        return direct
    sources = []
    for raw in re.findall(r'<script[^>]+src=["\']([^"\']+)', str(profile_html or ""), re.I):
        url = urljoin(profile_url, html.unescape(raw).replace("\\/", "/"))
        if url not in sources:
            sources.append(url)
    # The route artifact is normally in the first few Instagram bundles. Keep
    # discovery bounded so a changed Web build falls back instead of stalling.
    for url in sources[:40]:
        try:
            response = api.get(url, timeout=30_000, fail_on_status_code=False)
            if int(response.status) != 200:
                continue
            body = str(response.text() or "")
            if REELS_QUERY_NAME not in body:
                continue
            found = _query_doc_id_from_text(body)
            if found:
                _REELS_DOC_ID_CACHE = found
                return found
        except Exception:
            continue
    return ""


def _profile_info(api: Any, headers_fn: Any, tokens: dict[str, str], username: str) -> tuple[str, bool | None]:
    url = "https://www.instagram.com/api/v1/users/web_profile_info/"
    response = api.get(
        url,
        params={"username": username},
        headers=headers_fn(api, tokens, referer=f"https://www.instagram.com/{username}/"),
        timeout=60_000,
        fail_on_status_code=False,
    )
    status = int(response.status)
    payload = _response_payload(response)
    lowered = json.dumps(payload, ensure_ascii=False)[:100000].lower()
    if status in {401, 403, 429} or any(
        marker in lowered for marker in ("login_required", "checkpoint_required", "challenge_required", "consent_required")
    ):
        raise ParserBlocked(f"Instagram profile API blocked (HTTP {status})")
    user = payload.get("data", {}).get("user", {}) if isinstance(payload, dict) else {}
    if not isinstance(user, dict):
        user = {}
    user_id = str(user.get("id") or user.get("pk") or "").strip()
    privacy = user.get("is_private") if isinstance(user.get("is_private"), bool) else None
    return user_id, privacy


def _reel_media_nodes(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        connection = payload["data"]["xdt_api__v1__clips__user__connection_v2"]
    except (KeyError, TypeError):
        return [], {}
    nodes: list[dict[str, Any]] = []
    for edge in connection.get("edges", []) if isinstance(connection, dict) else []:
        media = edge.get("node", {}).get("media", {}) if isinstance(edge, dict) else {}
        if isinstance(media, dict):
            nodes.append(media)
    page_info = connection.get("page_info", {}) if isinstance(connection, dict) else {}
    return nodes, page_info if isinstance(page_info, dict) else {}


def _request_account_batch(
    api: Any,
    headers_fn: Any,
    tokens: dict[str, str],
    username: str,
    targets: list[dict[str, Any]],
) -> tuple[dict[int, tuple[int, str]], bool | None] | None:
    """Fetch a target account's Reels in pages, matching all saved targets.

    ``None`` means dynamic query discovery was unavailable and callers should
    use the existing per-Reel API fallback. A real block still raises
    ``ParserBlocked`` so parser rotation/cooldown semantics remain unchanged.
    """
    profile_url = f"https://www.instagram.com/{username}/reels/"
    try:
        profile = api.get(profile_url, timeout=60_000, fail_on_status_code=False)
    except Exception as exc:
        raise ParserBlocked(f"Reels bootstrap failed: {type(exc).__name__}: {exc}") from exc
    if int(profile.status) in {401, 403, 429}:
        raise ParserBlocked(f"Reels bootstrap HTTP {int(profile.status)}")
    profile_html = str(profile.text() or "")
    user_id, privacy = _profile_info(api, headers_fn, tokens, username)
    if not user_id:
        return None
    doc_id = _discover_reels_doc_id(api, profile_html, profile_url)
    if not doc_id:
        return None

    wanted_media = {str(t.get("media_id") or ""): int(t["id"]) for t in targets if str(t.get("media_id") or "")}
    wanted_codes = {str(t.get("shortcode") or ""): int(t["id"]) for t in targets if str(t.get("shortcode") or "")}
    found: dict[int, tuple[int, str]] = {}
    cursor = ""
    lsd = _extract_lsd(profile_html)
    for _page in range(REELS_MAX_PAGES):
        data: dict[str, Any] = {
            "include_feed_video": True,
            "page_size": REELS_PAGE_SIZE,
            "target_user_id": user_id,
        }
        if cursor:
            data["after"] = cursor
        form = {
            "av": _cookie_value(api, "ds_user_id"),
            "__d": "www",
            "__user": "0",
            "__a": "1",
            "__comet_req": "7",
            "fb_dtsg": str(tokens.get("fb_dtsg") or ""),
            "jazoest": str(tokens.get("jazoest") or ""),
            "lsd": lsd,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": REELS_QUERY_NAME,
            "server_timestamps": "true",
            "variables": json.dumps({"data": data}, separators=(",", ":")),
            "doc_id": doc_id,
        }
        response = api.post(
            "https://www.instagram.com/graphql/query",
            form={key: value for key, value in form.items() if value != ""},
            headers=headers_fn(api, tokens, referer=profile_url),
            timeout=90_000,
            fail_on_status_code=False,
        )
        status = int(response.status)
        payload = _response_payload(response)
        lowered = json.dumps(payload, ensure_ascii=False)[:100000].lower()
        if status in {401, 403, 429} or any(
            marker in lowered for marker in ("login_required", "checkpoint_required", "challenge_required", "consent_required")
        ):
            raise ParserBlocked(f"Reels batch API blocked (HTTP {status})")
        if status >= 500:
            raise ParserBlocked(f"Reels batch API HTTP {status}")
        nodes, page_info = _reel_media_nodes(payload)
        if not nodes and not page_info:
            # Query metadata can change between boot and request. Fallback is
            # safer than misclassifying every Reel as missing.
            return None
        for media in nodes:
            media_id = str(media.get("pk") or media.get("id") or "")
            code = str(media.get("code") or media.get("shortcode") or "")
            target_id = wanted_media.get(media_id) or wanted_codes.get(code)
            if not target_id:
                continue
            views, field = _extract_views_from_value(media)
            if views is not None:
                found[target_id] = (int(views), field)
        if len(found) >= len(targets):
            break
        cursor = str(page_info.get("end_cursor") or "")
        if not bool(page_info.get("has_next_page")) or not cursor:
            break
    return found, privacy


def _parse_db_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _account_api_material(conn: sqlite3.Connection, account: str) -> dict[str, Any] | None:
    ensure_connection_schema(conn)
    row = conn.execute(
        """
        SELECT a.name,COALESCE(a.web_upload_login_status,'') AS login_status,
               COALESCE(a.web_connection_id,0) AS connection_id,
               COALESCE(c.name,'Direct') AS connection_name,
               COALESCE(c.connection_type,'direct') AS connection_type,
               COALESCE(c.proxy_url,'') AS proxy_url,
               COALESCE(c.rotation_url,'') AS rotation_url
        FROM accounts a LEFT JOIN web_connections c ON c.id=a.web_connection_id
        WHERE a.name=?
        """,
        (account,),
    ).fetchone()
    return dict(row) if row else None


def _open_authenticated_api(playwright: Any, account: str, proxy: str):
    from instagram_private_web_api_upload import (
        _extract_page_tokens,
        _has_login_cookies,
        _headers,
        _load_storage_state,
        _request_context,
        _tokens_from_storage_state,
        _user_agent,
    )

    state = _load_storage_state(account, proxy)
    if not _has_login_cookies(state):
        raise ParserBlocked("saved Instagram API session is unavailable")
    api = _request_context(playwright, state or {}, _user_agent(account, proxy), proxy)
    try:
        boot = api.get("https://www.instagram.com/?hl=en", timeout=90_000, fail_on_status_code=False)
        status = int(boot.status)
        if status in {401, 403, 429}:
            raise ParserBlocked(f"API bootstrap HTTP {status}")
        if status >= 400:
            raise ParserBlocked(f"API bootstrap HTTP {status}")
        body = str(boot.text() or "")
        lowered = body[:100000].lower()
        if "/accounts/login" in str(boot.url).lower() or "login_required" in lowered or "checkpoint_required" in lowered:
            raise ParserBlocked("Instagram API session requires login or checkpoint")
        tokens = _extract_page_tokens(body)
        for key, value in _tokens_from_storage_state(api.storage_state()).items():
            if value:
                tokens[key] = value
        return api, tokens, _headers
    except Exception:
        api.dispose()
        raise


def _request_target(api: Any, headers_fn: Any, tokens: dict[str, str], target: dict[str, Any], referer_account: str) -> tuple[int | None, str, str]:
    media_id = str(target.get("media_id") or "").strip()
    shortcode = str(target.get("shortcode") or "").strip()
    if media_id:
        url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
    elif shortcode:
        url = f"https://www.instagram.com/api/v1/media/shortcode/{shortcode}/info/"
    else:
        return None, "", "Reel media id and shortcode are missing"
    try:
        response = api.get(
            url,
            headers=headers_fn(api, tokens, referer=str(target.get("permalink") or f"https://www.instagram.com/{referer_account}/")),
            timeout=90_000,
            fail_on_status_code=False,
        )
    except Exception as exc:
        raise ParserBlocked(f"API request failed: {type(exc).__name__}: {exc}") from exc
    status = int(response.status)
    payload = _response_payload(response)
    lowered = json.dumps(payload, ensure_ascii=False)[:100000].lower()
    if status in {401, 403, 429}:
        raise ParserBlocked(f"Instagram API HTTP {status}")
    if any(marker in lowered for marker in ("login_required", "checkpoint_required", "challenge_required", "consent_required")):
        raise ParserBlocked("Instagram API returned a login/challenge wall")
    views, field = _extract_views_from_value(payload)
    if views is not None:
        return int(views), field, ""
    if status == 404:
        return None, "", "Reel not found (HTTP 404)"
    if status >= 500:
        raise ParserBlocked(f"Instagram API HTTP {status}")
    return None, "", f"API response has no views field (HTTP {status})"


def _record_success(
    conn: sqlite3.Connection,
    target: dict[str, Any],
    views: int,
    field: str,
    source: str,
    exit_ip: str,
    interval: int,
    parser_name: str = "",
) -> None:
    target_id = int(target["id"])
    status = "own_api_success" if source == "own_api" else "parser_success"
    conn.execute(
        """
        UPDATE view_analytics_targets SET views=?,views_field=?,status=?,
            session_attempts=session_attempts+?,parser_attempts=0,last_parser=?,last_error='',
            last_checked_at=datetime('now'),next_check_at=datetime('now',?),updated_at=datetime('now')
        WHERE id=?
        """,
        (
            int(views), field, status, 1 if source == "own_api" else 0, parser_name,
            f"+{max(15, int(interval))} minutes", target_id,
        ),
    )
    previous = conn.execute(
        "SELECT views FROM view_analytics_samples WHERE target_id=? ORDER BY id DESC LIMIT 1",
        (target_id,),
    ).fetchone()
    if not previous or int(previous[0]) != int(views):
        conn.execute(
            "INSERT INTO view_analytics_samples(target_id,views,source,views_field,exit_ip) VALUES(?,?,?,?,?)",
            (target_id, int(views), source, field, str(exit_ip or "")),
        )
    conn.commit()


def _mark_parser_target_error(conn: sqlite3.Connection, target: dict[str, Any], parser_name: str, error: str) -> bool:
    attempts = int(target.get("parser_attempts") or 0) + 1
    exhausted = attempts >= PARSER_TARGET_ATTEMPTS
    conn.execute(
        """
        UPDATE view_analytics_targets SET status=?,parser_attempts=?,last_parser=?,last_error=?,
            last_checked_at=datetime('now'),next_check_at='',updated_at=datetime('now') WHERE id=?
        """,
        ("unparsed" if exhausted else "parser_retry", attempts, parser_name, str(error or "views unavailable")[:1000], int(target["id"])),
    )
    conn.commit()
    target["parser_attempts"] = attempts
    target["status"] = "unparsed" if exhausted else "parser_retry"
    target["last_error"] = error
    return exhausted


def _set_parser_state(conn: sqlite3.Connection, account: str, status: str, error: str = "", cooldown_minutes: int = 0) -> None:
    cooldown = f"+{max(1, int(cooldown_minutes))} minutes" if cooldown_minutes else ""
    if cooldown:
        conn.execute(
            """
            UPDATE view_parser_accounts SET status=?,error_count=error_count+1,last_error=?,
                cooldown_until=datetime('now',?),last_used_at=datetime('now'),updated_at=datetime('now')
            WHERE account_name=?
            """,
            (status, str(error)[:1000], cooldown, account),
        )
    else:
        conn.execute(
            "UPDATE view_parser_accounts SET status=?,last_error=?,last_used_at=datetime('now'),updated_at=datetime('now') WHERE account_name=?",
            (status, str(error)[:1000], account),
        )
    conn.commit()


def _rotate_parser_proxy(conn: sqlite3.Connection, material: dict[str, Any], account: str) -> tuple[bool, str, str]:
    ctype = str(material.get("connection_type") or "direct")
    connection_id = int(material.get("connection_id") or 0)
    proxy = str(material.get("proxy_url") or "")
    if ctype not in {"mobile", "phone"}:
        return True, "No mobile rotation required", ""
    if not connection_id or not str(material.get("rotation_url") or "").strip():
        return False, "Mobile parser connection has no rotation link", ""
    result = rotate_connection(conn, connection_id, sleep_after=True)
    if not result.get("ok"):
        return False, str(result.get("error") or "rotation failed"), ""
    from connection_scheduler import probe_proxy_exit_ip

    ok, detail, exit_ip = probe_proxy_exit_ip(proxy, timeout=15.0)
    if ok:
        conn.execute(
            "UPDATE view_parser_accounts SET last_exit_ip=?,updated_at=datetime('now') WHERE account_name=?",
            (exit_ip, account),
        )
        conn.commit()
    return ok, detail, exit_ip


def _ready_parser_material(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in parser_accounts(conn, include_disabled=False):
        if item.get("display_status") != "ready":
            continue
        material = _account_api_material(conn, str(item["account_name"]))
        if material:
            material.update(item)
            result.append(material)
    return result


def run_parser_pool(limit: int = 0, force: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    conn = db_conn()
    run_id = 0
    try:
        ensure_view_schema(conn)
        cfg = settings(conn)
        if not cfg.get("enabled") and not force:
            return 0
        queue = due_targets(conn, limit, force=force)
        if not queue:
            interval = int(cfg.get("interval_minutes") or 60)
            conn.execute(
                "UPDATE view_analytics_settings SET last_status='idle',last_error='',next_run_at=datetime('now',?),updated_at=datetime('now') WHERE id=1",
                (f"+{interval} minutes",),
            )
            conn.commit()
            return 0
        parsers = _ready_parser_material(conn)
        if not parsers:
            conn.execute(
                "UPDATE view_analytics_settings SET last_status='no_parser_ready',last_error='No logged-in parser account is ready',next_run_at=datetime('now','+15 minutes'),updated_at=datetime('now') WHERE id=1"
            )
            conn.commit()
            return 2
        cur = conn.execute("INSERT INTO view_analytics_runs(mode,status) VALUES('parser_pool','running')")
        run_id = int(cur.lastrowid)
        conn.commit()
        interval = int(cfg.get("interval_minutes") or 60)
        checked = parsed = rotations = 0
        attempted_any = False
        with sync_playwright() as playwright:
            for parser_data in parsers:
                if not queue:
                    break
                account = str(parser_data["account_name"])
                proxy = str(parser_data.get("proxy_url") or "")
                _set_parser_state(conn, account, "working")
                api = None
                parser_error = ""
                soft_target_error = False
                try:
                    api, tokens, headers_fn = _open_authenticated_api(playwright, account, proxy)
                    batch_attempted_accounts: set[str] = set()
                    while queue:
                        target = queue[0]
                        target_account = str(target.get("account_name") or "").strip().lstrip("@")
                        account_targets = [
                            item for item in queue
                            if str(item.get("account_name") or "").strip().lstrip("@").lower() == target_account.lower()
                        ]
                        attempted_any = True
                        account_key = target_account.lower()
                        batch = None
                        if account_key not in batch_attempted_accounts:
                            batch_attempted_accounts.add(account_key)
                            batch = _request_account_batch(api, headers_fn, tokens, target_account, account_targets)
                        if batch is not None:
                            batch_views, _privacy = batch
                            checked += 1
                            conn.execute(
                                "UPDATE view_parser_accounts SET request_count=request_count+1,last_used_at=datetime('now'),updated_at=datetime('now') WHERE account_name=?",
                                (account,),
                            )
                            conn.commit()
                            matched = 0
                            for item in list(account_targets):
                                result = batch_views.get(int(item["id"]))
                                if result is None:
                                    continue
                                views, field = result
                                _record_success(
                                    conn, item, int(views), field, "parser_pool",
                                    str(parser_data.get("last_exit_ip") or ""), interval, account,
                                )
                                queue.remove(item)
                                parsed += 1
                                matched += 1
                            if matched:
                                conn.execute(
                                    """
                                    UPDATE view_parser_accounts SET success_count=success_count+?,last_success_at=datetime('now'),
                                        last_error='',updated_at=datetime('now') WHERE account_name=?
                                    """,
                                    (matched, account),
                                )
                                conn.commit()
                            # A successfully paginated response may still omit
                            # an old/deleted Reel. Verify only those unmatched
                            # targets through the established media-info route.
                            if not any(item in queue for item in account_targets):
                                continue

                        # Dynamic Relay discovery is intentionally allowed to
                        # fail closed. The existing authenticated per-Reel API
                        # remains the compatibility fallback.
                        target = next((item for item in account_targets if item in queue), queue[0])
                        views, field, error = _request_target(api, headers_fn, tokens, target, account)
                        checked += 1
                        conn.execute(
                            "UPDATE view_parser_accounts SET request_count=request_count+1,last_used_at=datetime('now'),updated_at=datetime('now') WHERE account_name=?",
                            (account,),
                        )
                        conn.commit()
                        if views is None:
                            exhausted = _mark_parser_target_error(conn, target, account, error)
                            parser_error = error
                            soft_target_error = True
                            if exhausted:
                                queue.pop(0)
                            # Retry this exact target with the next parser identity.
                            break
                        _record_success(conn, target, int(views), field, "parser_pool", str(parser_data.get("last_exit_ip") or ""), interval, account)
                        conn.execute(
                            """
                            UPDATE view_parser_accounts SET success_count=success_count+1,last_success_at=datetime('now'),
                                last_error='',updated_at=datetime('now') WHERE account_name=?
                            """,
                            (account,),
                        )
                        conn.commit()
                        parsed += 1
                        queue.pop(0)
                except ParserBlocked as exc:
                    parser_error = str(exc)
                    cooldown = 60 if any(word in parser_error.lower() for word in ("429", "challenge", "checkpoint")) else 20
                    _set_parser_state(conn, account, "cooldown", parser_error, cooldown)
                except Exception as exc:
                    parser_error = f"{type(exc).__name__}: {exc}"
                    _set_parser_state(conn, account, "cooldown", parser_error, 20)
                finally:
                    if api is not None:
                        try:
                            api.dispose()
                        except Exception:
                            pass
                if not parser_error:
                    _set_parser_state(conn, account, "ready")
                    continue
                if soft_target_error:
                    _set_parser_state(conn, account, "ready", parser_error)
                ok, detail, _ = _rotate_parser_proxy(conn, parser_data, account)
                if str(parser_data.get("connection_type") or "") in {"mobile", "phone"}:
                    rotations += 1
                    if not ok:
                        _set_parser_state(conn, account, "cooldown", f"{parser_error}; rotation failed: {detail}", 20)
                log(f"Parser {account} stopped: {parser_error}; next parser will continue", "WARNING")
        # Do not classify untouched queue entries as Unparsed merely because no
        # unused parser identity remains in this run.  Only the exact Reel that
        # failed with PARSER_TARGET_ATTEMPTS distinct parser identities reaches
        # Unparsed in _mark_parser_target_error().  Pending/parser_retry entries
        # stay eligible for a later scheduled Parser Pool pass.
        unparsed = int(conn.execute("SELECT COUNT(*) FROM view_analytics_targets WHERE status='unparsed'").fetchone()[0])
        status = "success" if not queue else "warning"
        last_error = "" if not queue else "Parser Pool exhausted; remaining targets require own API fallback"
        conn.execute(
            """
            UPDATE view_analytics_runs SET status=?,checked=?,parsed=?,unparsed=?,rotations=?,last_error=?,finished_at=datetime('now') WHERE id=?
            """,
            (status, checked, parsed, unparsed, rotations, last_error, run_id),
        )
        conn.execute(
            """
            UPDATE view_analytics_settings SET last_status=?,last_error=?,last_run_at=datetime('now'),
                next_run_at=datetime('now',?),updated_at=datetime('now') WHERE id=1
            """,
            (status, last_error, f"+{interval} minutes"),
        )
        conn.commit()
        log(f"Parser Pool finished: checked={checked}, parsed={parsed}, unparsed={unparsed}, rotations={rotations}", "OK")
        return 0 if status == "success" else 1
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if run_id:
            conn.execute(
                "UPDATE view_analytics_runs SET status='failed',last_error=?,finished_at=datetime('now') WHERE id=?",
                (error, run_id),
            )
        conn.execute(
            "UPDATE view_analytics_settings SET last_status='failed',last_error=?,last_run_at=datetime('now'),next_run_at=datetime('now','+15 minutes'),updated_at=datetime('now') WHERE id=1",
            (error,),
        )
        conn.commit()
        log(error, "ERROR")
        return 1
    finally:
        conn.close()


def run_public_once(limit: int = 0, force: bool = False) -> int:
    """Removed public mode compatibility entrypoint; always uses Parser Pool."""
    return run_parser_pool(limit, force)


def run_session_account(account: str, target_ids: list[int] | None = None) -> int:
    """Manual own-API fallback: one managed account may query only its own Reels."""
    from playwright.sync_api import sync_playwright

    account = str(account or "").strip().lstrip("@")
    conn = db_conn()
    try:
        ensure_view_schema(conn)
        params: list[Any] = [account]
        where = "t.account_name=? AND t.status IN ('unparsed','own_session_required','own_api_failed')"
        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            where += f" AND t.id IN ({placeholders})"
            params.extend(int(value) for value in target_ids)
        targets = [dict(row) for row in conn.execute(f"SELECT t.* FROM view_analytics_targets t WHERE {where} ORDER BY t.id", params).fetchall()]
        role = conn.execute("SELECT COALESCE(account_role,'managed') FROM accounts WHERE name=?", (account,)).fetchone()
        if not role or str(role[0]) != "managed":
            log(f"Own API isolation rejected account {account}", "ERROR")
            return 2
        material = _account_api_material(conn, account)
        if not material:
            return 2
    finally:
        conn.close()
    if not targets:
        return 0

    proxy = str(material.get("proxy_url") or "")
    with sync_playwright() as playwright:
        api = None
        try:
            api, tokens, headers_fn = _open_authenticated_api(playwright, account, proxy)
            for target in targets:
                try:
                    views, field, error = _request_target(api, headers_fn, tokens, target, account)
                except ParserBlocked as exc:
                    error = str(exc)
                    views = None
                    field = ""
                conn = db_conn()
                try:
                    if views is None:
                        conn.execute(
                            """
                            UPDATE view_analytics_targets SET status='own_api_failed',session_attempts=session_attempts+1,
                                last_error=?,last_checked_at=datetime('now'),updated_at=datetime('now') WHERE id=?
                            """,
                            (str(error or "own API response has no views")[:1000], int(target["id"])),
                        )
                        conn.commit()
                    else:
                        cfg = settings(conn)
                        _record_success(conn, target, int(views), field, "own_api", "", int(cfg.get("interval_minutes") or 60), account)
                finally:
                    conn.close()
        except ParserBlocked as exc:
            conn = db_conn()
            try:
                conn.execute(
                    """
                    UPDATE view_analytics_targets SET status='own_session_required',last_error=?,updated_at=datetime('now')
                    WHERE account_name=? AND status IN ('unparsed','own_api_failed')
                    """,
                    (str(exc)[:1000], account),
                )
                conn.commit()
            finally:
                conn.close()
        finally:
            if api is not None:
                api.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SparkGrid authenticated Reel view collector")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--parser-pool", action="store_true")
    mode.add_argument("--public-once", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--session-account", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--target-ids", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.parser_pool or args.public_once:
        return run_parser_pool(int(args.limit), bool(args.force))
    ids = [int(value) for value in str(args.target_ids or "").split(",") if value.strip().isdigit()]
    return run_session_account(str(args.session_account), ids)


if __name__ == "__main__":
    raise SystemExit(main())
