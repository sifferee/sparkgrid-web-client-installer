from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Iterable


DEFAULT_WINDOWS = [
    {"start": "09:00", "end": "12:00"},
    {"start": "18:00", "end": "21:00"},
]
DEFAULT_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]
# Automation randomizes only the beginning of a publication session.  The
# existing warmup/content workflow remains the sole owner of pauses between
# individual publications inside that session.
SESSION_START_JITTER_MINUTES = 5
SESSION_START_JITTER_MAX_MINUTES = 20


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _json(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except Exception:
        return fallback


def ensure_automation_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 0,
            timezone_name TEXT NOT NULL DEFAULT 'local',
            engine TEXT NOT NULL DEFAULT 'clean_web',
            provider TEXT NOT NULL DEFAULT 'camoufox',
            login_delay_min_minutes INTEGER NOT NULL DEFAULT 360,
            login_delay_max_minutes INTEGER NOT NULL DEFAULT 480,
            sessions_per_day INTEGER NOT NULL DEFAULT 2,
            weekdays_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
            windows_json TEXT NOT NULL DEFAULT '[{"start":"09:00","end":"12:00"},{"start":"18:00","end":"21:00"}]',
            niche_mode TEXT NOT NULL DEFAULT '',
            niche_name TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_plan_accounts (
            plan_id INTEGER NOT NULL,
            account_name TEXT NOT NULL COLLATE NOCASE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(plan_id, account_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_plan_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            account_name TEXT NOT NULL COLLATE NOCASE,
            local_date TEXT NOT NULL,
            session_index INTEGER NOT NULL DEFAULT 0,
            scheduled_for TEXT NOT NULL,
            window_end TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(plan_id, account_name, local_date, session_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_connection_leases (
            connection_id INTEGER PRIMARY KEY,
            owner_type TEXT NOT NULL DEFAULT '',
            owner_id TEXT NOT NULL DEFAULT '',
            acquired_at TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    account_cols = _columns(conn, "accounts")
    if account_cols and "web_upload_last_login_at" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN web_upload_last_login_at TEXT NOT NULL DEFAULT ''")
    # Added 2026-08-14: /session showed "last: <date>" taken from
    # web_upload_last_login_at, i.e. the last time credentials were actually
    # entered — often days old, since a session check only verifies an
    # existing session and never re-logs in. Reading that next to a session
    # check result implied the check itself was stale. This column records
    # when the check ran, so the bot can show what it actually means.
    if account_cols and "web_upload_session_checked_at" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN web_upload_session_checked_at TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_automation_slots_due "
        "ON automation_plan_slots(status, scheduled_for, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_automation_plan_accounts_name "
        "ON automation_plan_accounts(account_name, plan_id)"
    )
    conn.commit()


def _clock(raw: Any) -> str:
    value = str(raw or "").strip()
    try:
        hh, mm = value.split(":", 1)
        hour, minute = int(hh), int(mm)
    except Exception as exc:
        raise ValueError(f"Invalid time: {value or '(empty)'}") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid time: {value}")
    return f"{hour:02d}:{minute:02d}"


def normalize_windows(raw: Any, sessions_per_day: int) -> list[dict[str, str]]:
    values = raw if isinstance(raw, list) else _json(raw, [])
    result: list[dict[str, str]] = []
    for item in values or []:
        if not isinstance(item, dict):
            continue
        start, end = _clock(item.get("start")), _clock(item.get("end"))
        if end <= start:
            raise ValueError("Automation windows must end after they start on the same day")
        result.append({"start": start, "end": end})
    if not result:
        result = [dict(item) for item in DEFAULT_WINDOWS]
    sessions = max(1, min(int(sessions_per_day or len(result) or 1), 8))
    if len(result) != sessions:
        raise ValueError("The number of time windows must match sessions per day")
    return result


def normalize_weekdays(raw: Any) -> list[int]:
    values = raw if isinstance(raw, list) else _json(raw, DEFAULT_WEEKDAYS)
    result = sorted({int(value) for value in values if str(value).lstrip("-").isdigit() and 0 <= int(value) <= 6})
    return result or list(DEFAULT_WEEKDAYS)


def _clean_accounts(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values or []:
        name = str(value or "").strip().lstrip("@")
        if name and name not in result:
            result.append(name)
    return result


def save_automation_plan(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    ensure_automation_schema(conn)
    plan_id = int(body.get("id") or 0)
    name = " ".join(str(body.get("name") or "").strip().split())[:80]
    if not name:
        raise ValueError("Plan name is required")
    engine = str(body.get("engine") or "clean_web").strip().lower()
    if engine not in {"clean_web", "api"}:
        raise ValueError("Automation engine must be Clean Web or API")
    provider = str(body.get("provider") or "camoufox").strip().lower()
    if provider not in {"camoufox", "playwright"}:
        provider = "camoufox"
    sessions = max(1, min(int(body.get("sessions_per_day") or 2), 8))
    windows = normalize_windows(body.get("windows"), sessions)
    weekdays = normalize_weekdays(body.get("weekdays"))
    delay_min = max(0, min(int(body.get("login_delay_min_minutes") or 360), 7 * 24 * 60))
    delay_max = max(delay_min, min(int(body.get("login_delay_max_minutes") or 480), 7 * 24 * 60))
    enabled = 1 if bool(body.get("enabled")) else 0
    timezone_name = str(body.get("timezone_name") or "local").strip()[:80] or "local"
    niche_mode = str(body.get("niche_mode") or "").strip().lower()
    if niche_mode not in {"", "scale", "quality"}:
        niche_mode = ""
    niche_name = str(body.get("niche_name") or "").strip()[:100]
    accounts = _clean_accounts(body.get("accounts") or [])

    if plan_id:
        exists = conn.execute("SELECT 1 FROM automation_plans WHERE id=?", (plan_id,)).fetchone()
        if not exists:
            raise ValueError("Automation plan not found")
        conn.execute(
            """
            UPDATE automation_plans
            SET name=?,enabled=?,timezone_name=?,engine=?,provider=?,
                login_delay_min_minutes=?,login_delay_max_minutes=?,sessions_per_day=?,
                weekdays_json=?,windows_json=?,
                niche_mode=?,niche_name=?,last_error='',updated_at=datetime('now')
            WHERE id=?
            """,
            (
                name, enabled, timezone_name, engine, provider, delay_min, delay_max, sessions,
                json.dumps(weekdays), json.dumps(windows),
                niche_mode, niche_name, plan_id,
            ),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO automation_plans(
                name,enabled,timezone_name,engine,provider,login_delay_min_minutes,
                login_delay_max_minutes,sessions_per_day,weekdays_json,windows_json,niche_mode,niche_name
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                name, enabled, timezone_name, engine, provider, delay_min, delay_max, sessions,
                json.dumps(weekdays), json.dumps(windows),
                niche_mode, niche_name,
            ),
        )
        plan_id = int(cur.lastrowid)

    if niche_mode and niche_name:
        column = "web_upload_scale_niche" if niche_mode == "scale" else "web_upload_quality_niche"
        rows = conn.execute(
            f"SELECT name FROM accounts WHERE TRIM(COALESCE({column},''))=? COLLATE NOCASE ORDER BY name",
            (niche_name,),
        ).fetchall()
        accounts = _clean_accounts([row[0] for row in rows])
    if not accounts:
        raise ValueError("Select at least one account or niche")
    placeholders = ",".join("?" for _ in accounts)
    found = {str(row[0]) for row in conn.execute(f"SELECT name FROM accounts WHERE name IN ({placeholders})", accounts)}
    missing = [item for item in accounts if item not in found]
    if missing:
        raise ValueError("Accounts not found: " + ", ".join(missing[:5]))
    conn.execute("DELETE FROM automation_plan_accounts WHERE plan_id=?", (plan_id,))
    conn.executemany(
        "INSERT INTO automation_plan_accounts(plan_id,account_name) VALUES(?,?)",
        [(plan_id, account) for account in accounts],
    )
    # Pending slots are regenerated from the edited plan; historical rows stay.
    conn.execute(
        "DELETE FROM automation_plan_slots WHERE plan_id=? AND status IN ('pending','waiting_login_delay')",
        (plan_id,),
    )
    conn.commit()
    return get_automation_plan(conn, plan_id)


def _plan_payload(conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    plan_id = int(item.get("id") or 0)
    accounts = [
        str(value[0]) for value in conn.execute(
            "SELECT account_name FROM automation_plan_accounts WHERE plan_id=? ORDER BY account_name",
            (plan_id,),
        ).fetchall()
    ]
    item["enabled"] = bool(int(item.get("enabled") or 0))
    item["weekdays"] = normalize_weekdays(item.pop("weekdays_json", ""))
    item["windows"] = normalize_windows(item.pop("windows_json", ""), int(item.get("sessions_per_day") or 2))
    item["accounts"] = accounts
    item["account_count"] = len(accounts)
    item["pending_count"] = int(conn.execute(
        "SELECT COUNT(*) FROM automation_plan_slots WHERE plan_id=? AND status IN ('pending','waiting_login_delay')",
        (plan_id,),
    ).fetchone()[0])
    item["next_run"] = str(conn.execute(
        "SELECT COALESCE(MIN(scheduled_for),'') FROM automation_plan_slots "
        "WHERE plan_id=? AND status IN ('pending','waiting_login_delay')",
        (plan_id,),
    ).fetchone()[0] or "")
    return item


def get_automation_plan(conn: sqlite3.Connection, plan_id: int) -> dict[str, Any]:
    ensure_automation_schema(conn)
    row = conn.execute("SELECT * FROM automation_plans WHERE id=?", (int(plan_id),)).fetchone()
    if not row:
        raise ValueError("Automation plan not found")
    return _plan_payload(conn, row)


def list_automation_plans(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_automation_schema(conn)
    return [_plan_payload(conn, row) for row in conn.execute("SELECT * FROM automation_plans ORDER BY name,id").fetchall()]


def delete_automation_plan(conn: sqlite3.Connection, plan_id: int) -> bool:
    ensure_automation_schema(conn)
    plan_id = int(plan_id)
    exists = conn.execute("SELECT 1 FROM automation_plans WHERE id=?", (plan_id,)).fetchone()
    if not exists:
        return False
    conn.execute("DELETE FROM automation_plan_accounts WHERE plan_id=?", (plan_id,))
    conn.execute("DELETE FROM automation_plan_slots WHERE plan_id=?", (plan_id,))
    conn.execute("DELETE FROM automation_plans WHERE id=?", (plan_id,))
    conn.commit()
    return True


def _stable_fraction(*parts: Any) -> float:
    raw = "|".join(str(part) for part in parts).encode("utf-8", errors="ignore")
    value = int(hashlib.sha256(raw).hexdigest()[:12], 16)
    return value / float(16**12 - 1)


def stable_login_delay_minutes(plan: dict[str, Any], account: str, login_at: str) -> int:
    low = int(plan.get("login_delay_min_minutes") or 0)
    high = max(low, int(plan.get("login_delay_max_minutes") or low))
    if high == low:
        return low
    # A worker receives a joined slot row where ``id`` is the slot id and
    # ``plan_id`` is the stable plan id.  Key the delay to the plan so editing
    # or recreating today's persisted slot cannot unexpectedly change it.
    plan_id = plan.get("plan_id") or plan.get("id")
    return low + int(round((high - low) * _stable_fraction(plan_id, account, login_at, "login-delay")))


def _local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _plan_tz(name: str):
    if not name or name == "local":
        return _local_tz()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return _local_tz()


def _parse_local(day: date, value: str, tzinfo) -> datetime:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return datetime.combine(day, dt_time(hour=hour, minute=minute), tzinfo=tzinfo)


def materialize_plan_slots(conn: sqlite3.Connection, plan: dict[str, Any], day: date) -> int:
    ensure_automation_schema(conn)
    if not plan.get("enabled") or day.weekday() not in set(plan.get("weekdays") or DEFAULT_WEEKDAYS):
        return 0
    accounts = list(plan.get("accounts") or [])
    if not accounts:
        return 0
    tzinfo = _plan_tz(str(plan.get("timezone_name") or "local"))
    created = 0
    for session_index, window in enumerate(plan.get("windows") or []):
        local_start = _parse_local(day, str(window["start"]), tzinfo)
        local_end = _parse_local(day, str(window["end"]), tzinfo)
        window_minutes = max(1, int((local_end - local_start).total_seconds() // 60))
        jitter_low = min(SESSION_START_JITTER_MINUTES, max(0, window_minutes - 1))
        jitter_high = min(SESSION_START_JITTER_MAX_MINUTES, max(jitter_low, window_minutes - 1))
        # Keep a small, deterministic start jitter.  We deliberately do not
        # spread accounts over the whole 2–3 hour window: shared proxy lanes
        # and the normal queue already serialize them safely.  Persisting the
        # slot means an application restart cannot reshuffle today's run.
        for account in accounts:
            fraction = _stable_fraction(plan.get("id"), day.isoformat(), session_index, account, "session-start")
            jitter_minutes = jitter_low + int(round((jitter_high - jitter_low) * fraction))
            scheduled_local = local_start + timedelta(minutes=jitter_minutes)
            scheduled_utc = scheduled_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_utc = local_end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO automation_plan_slots(
                    plan_id,account_name,local_date,session_index,scheduled_for,window_end
                ) VALUES(?,?,?,?,?,?)
                """,
                (int(plan["id"]), account, day.isoformat(), session_index, scheduled_utc, end_utc),
            )
            created += int(cur.rowcount or 0)
    conn.commit()
    return created


def materialize_enabled_slots(conn: sqlite3.Connection, days: int = 2) -> int:
    ensure_automation_schema(conn)
    today = datetime.now(_local_tz()).date()
    total = 0
    for plan in list_automation_plans(conn):
        if not plan.get("enabled"):
            continue
        for offset in range(max(1, int(days))):
            total += materialize_plan_slots(conn, plan, today + timedelta(days=offset))
    return total


def due_slot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    ensure_automation_schema(conn)
    row = conn.execute(
        """
        SELECT s.*,p.name AS plan_name,p.engine,p.provider,p.enabled,
               p.login_delay_min_minutes,p.login_delay_max_minutes,
               COALESCE(a.status,'') AS account_status,
               COALESCE(a.web_upload_login_status,'') AS login_status,
               COALESCE(a.web_upload_last_login_at,'') AS last_login_at,
               COALESCE(a.web_upload_last_error,'') AS account_error
        FROM automation_plan_slots s
        JOIN automation_plans p ON p.id=s.plan_id
        JOIN accounts a ON a.name=s.account_name
        WHERE p.enabled=1
          AND s.status IN ('pending','waiting_login_delay')
          AND datetime(s.scheduled_for)<=datetime('now')
        ORDER BY datetime(s.scheduled_for),s.id
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def mark_slot(conn: sqlite3.Connection, slot_id: int, status: str, error: str = "") -> None:
    fields = ["status=?", "last_error=?", "updated_at=datetime('now')"]
    params: list[Any] = [str(status), str(error or "")[:2000]]
    if status == "running":
        fields += ["attempts=attempts+1", "started_at=datetime('now')", "finished_at='' "]
    if status in {"success", "failed", "skipped", "missed", "no_content"}:
        fields += ["finished_at=datetime('now')"]
    params.append(int(slot_id))
    conn.execute(f"UPDATE automation_plan_slots SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()


def slot_history(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    ensure_automation_schema(conn)
    rows = conn.execute(
        """
        SELECT s.*,p.name AS plan_name
        FROM automation_plan_slots s JOIN automation_plans p ON p.id=s.plan_id
        ORDER BY s.id DESC LIMIT ?
        """,
        (max(1, min(int(limit), 2000)),),
    ).fetchall()
    return [dict(row) for row in rows]
