from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from proxy_telemetry import emit_proxy_telemetry
from rotation_provider_states import (
    RotationResponse,
    classify_rotation_response,
    describe_rotation_response,
)

CONNECTION_TYPES = {"direct", "static", "mobile", "phone"}
PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}
UI_PROXY_SCHEMES = {"http", "https", "socks5"}
MOBILE_ROTATION_LEASE_SECONDS = 180.0


def ensure_mobile_rotation_lease_schema(conn: sqlite3.Connection) -> None:
    """Create the durable, secret-free lease used by mobile rotations."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mobile_rotation_leases (
            connection_id INTEGER PRIMARY KEY,
            owner_id TEXT NOT NULL,
            generation TEXT NOT NULL DEFAULT '',
            acquired_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            state TEXT NOT NULL,
            outcome TEXT NOT NULL DEFAULT ''
        )
        """
    )
    lease_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(mobile_rotation_leases)")}
    if "generation" not in lease_columns:
        conn.execute("ALTER TABLE mobile_rotation_leases ADD COLUMN generation TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mobile_rotation_leases_active "
        "ON mobile_rotation_leases(state,expires_at)"
    )


def acquire_mobile_rotation_lease(
    conn: sqlite3.Connection, connection_id: int, owner_id: str, lease_seconds: float = MOBILE_ROTATION_LEASE_SECONDS,
    generation: str = "",
) -> dict[str, Any]:
    """Atomically acquire a per-connection lease, or report its active owner.

    `connection_id` is the sole lock identity.  The row deliberately contains
    no proxy material, account name, or provider URL.
    """
    ensure_mobile_rotation_lease_schema(conn)
    now = time.time()
    expires_at = now + max(1.0, float(lease_seconds))
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT owner_id,generation,expires_at,state,outcome FROM mobile_rotation_leases WHERE connection_id=?",
            (int(connection_id),),
        ).fetchone()
        if row and str(row["state"]) == "active" and float(row["expires_at"]) > now:
            conn.rollback()
            return {"acquired": False, "stale_recovered": False, "outcome": ""}
        stale_recovered = bool(row and str(row["state"]) == "active" and float(row["expires_at"]) <= now)
        conn.execute(
            """
            INSERT INTO mobile_rotation_leases(connection_id,owner_id,generation,acquired_at,expires_at,updated_at,state,outcome)
            VALUES(?,?,?,?,?,?,'active','')
            ON CONFLICT(connection_id) DO UPDATE SET
                owner_id=excluded.owner_id,generation=excluded.generation,acquired_at=excluded.acquired_at,
                expires_at=excluded.expires_at,updated_at=excluded.updated_at,
                state='active',outcome=''
            """,
            (int(connection_id), str(owner_id), str(generation), now, expires_at, now),
        )
        conn.commit()
        return {"acquired": True, "stale_recovered": stale_recovered, "outcome": ""}
    except Exception:
        conn.rollback()
        raise


def renew_mobile_rotation_lease(
    conn: sqlite3.Connection, connection_id: int, owner_id: str, lease_seconds: float = MOBILE_ROTATION_LEASE_SECONDS,
) -> bool:
    ensure_mobile_rotation_lease_schema(conn)
    now = time.time()
    cur = conn.execute(
        "UPDATE mobile_rotation_leases SET expires_at=?,updated_at=? "
        "WHERE connection_id=? AND owner_id=? AND state='active' AND expires_at>?",
        (now + max(1.0, float(lease_seconds)), now, int(connection_id), str(owner_id), now),
    )
    conn.commit()
    return bool(cur.rowcount)


def release_mobile_rotation_lease(conn: sqlite3.Connection, connection_id: int, owner_id: str, outcome: str) -> bool:
    """Publish a terminal outcome. Repeated own release is harmless."""
    ensure_mobile_rotation_lease_schema(conn)
    now = time.time()
    cur = conn.execute(
        "UPDATE mobile_rotation_leases SET state='terminal',outcome=?,expires_at=?,updated_at=? "
        "WHERE connection_id=? AND owner_id=? AND (state='active' OR state='terminal')",
        (str(outcome), now, now, int(connection_id), str(owner_id)),
    )
    conn.commit()
    return bool(cur.rowcount)


def mobile_rotation_lease_status(conn: sqlite3.Connection, connection_id: int) -> dict[str, Any]:
    ensure_mobile_rotation_lease_schema(conn)
    row = conn.execute(
        "SELECT owner_id,generation,acquired_at,expires_at,updated_at,state,outcome "
        "FROM mobile_rotation_leases WHERE connection_id=?",
        (int(connection_id),),
    ).fetchone()
    return dict(row) if row else {}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_connection_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            connection_type TEXT NOT NULL DEFAULT 'static',
            proxy_url TEXT NOT NULL DEFAULT '',
            rotation_url TEXT NOT NULL DEFAULT '',
            rotation_method TEXT NOT NULL DEFAULT 'GET',
            rotation_wait_seconds INTEGER NOT NULL DEFAULT 12,
            rotate_before_first INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_status TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            last_ip TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '',
            last_rotated_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    cols = _cols(conn, "web_connections")
    additions = {
        "group_id": "INTEGER NOT NULL DEFAULT 0",
        "quarantined": "INTEGER NOT NULL DEFAULT 0",
        "failure_count": "INTEGER NOT NULL DEFAULT 0",
        "quarantined_at": "TEXT NOT NULL DEFAULT ''",
        "connection_type": "TEXT NOT NULL DEFAULT 'static'",
        "proxy_url": "TEXT NOT NULL DEFAULT ''",
        "rotation_url": "TEXT NOT NULL DEFAULT ''",
        "rotation_method": "TEXT NOT NULL DEFAULT 'GET'",
        "rotation_wait_seconds": "INTEGER NOT NULL DEFAULT 12",
        "rotate_before_first": "INTEGER NOT NULL DEFAULT 1",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "last_status": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "last_ip": "TEXT NOT NULL DEFAULT ''",
        "last_checked_at": "TEXT NOT NULL DEFAULT ''",
        "last_rotated_at": "TEXT NOT NULL DEFAULT ''",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE web_connections ADD COLUMN {name} {ddl}")

    # Existing installations predate explicit folders. Preserve every saved
    # static endpoint by placing it in one visible legacy group.
    orphan_static = conn.execute(
        "SELECT 1 FROM web_connections WHERE connection_type='static' AND COALESCE(group_id,0)=0 LIMIT 1"
    ).fetchone()
    if orphan_static:
        conn.execute(
            "INSERT OR IGNORE INTO proxy_groups(name,updated_at) VALUES('Legacy static proxies',datetime('now'))"
        )
        legacy_group = conn.execute(
            "SELECT id FROM proxy_groups WHERE name='Legacy static proxies' COLLATE NOCASE"
        ).fetchone()
        conn.execute(
            "UPDATE web_connections SET group_id=? WHERE connection_type='static' AND COALESCE(group_id,0)=0",
            (int(legacy_group[0]),),
        )

    account_cols = _cols(conn, "accounts")
    if "web_connection_id" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN web_connection_id INTEGER")

    conn.execute(
        """
        INSERT OR IGNORE INTO web_connections(
            name,connection_type,proxy_url,rotation_url,rotation_method,
            rotation_wait_seconds,rotate_before_first,enabled,last_status,updated_at
        ) VALUES ('Direct','direct','','','GET',0,0,1,'ready',datetime('now'))
        """
    )
    direct_id = int(conn.execute("SELECT id FROM web_connections WHERE lower(name)='direct' LIMIT 1").fetchone()[0])

    # Convert legacy account.proxy values once. Accounts with the same exact proxy
    # intentionally share one connection lane.
    rows = conn.execute(
        "SELECT name,COALESCE(proxy,'') AS proxy,web_connection_id FROM accounts"
    ).fetchall()
    for row in rows:
        if row["web_connection_id"] not in (None, 0, ""):
            continue
        proxy = normalize_proxy(str(row["proxy"] or "").strip())
        if not proxy:
            conn.execute("UPDATE accounts SET web_connection_id=? WHERE name=?", (direct_id, row["name"]))
            continue
        existing = conn.execute(
            "SELECT id FROM web_connections WHERE proxy_url=? ORDER BY id LIMIT 1", (proxy,)
        ).fetchone()
        if existing:
            connection_id = int(existing[0])
        else:
            digest = hashlib.sha1(proxy.encode("utf-8", errors="ignore")).hexdigest()[:6].upper()
            base = f"Imported {digest}"
            name = base
            suffix = 2
            while conn.execute("SELECT 1 FROM web_connections WHERE name=? COLLATE NOCASE", (name,)).fetchone():
                name = f"{base} {suffix}"
                suffix += 1
            cur = conn.execute(
                "INSERT INTO web_connections(name,connection_type,proxy_url,last_status,updated_at) VALUES (?,?,?,'saved',datetime('now'))",
                (name, "static", proxy),
            )
            connection_id = int(cur.lastrowid)
        conn.execute("UPDATE accounts SET web_connection_id=?,proxy=? WHERE name=?", (connection_id, proxy, row["name"]))
    ensure_mobile_rotation_lease_schema(conn)
    conn.commit()


def direct_connection_id(conn: sqlite3.Connection) -> int:
    ensure_connection_schema(conn)
    row = conn.execute("SELECT id FROM web_connections WHERE connection_type='direct' ORDER BY id LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("Direct connection is missing")
    return int(row[0])


def normalize_proxy(raw: str, default_scheme: str = "http") -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    scheme = str(default_scheme or "http").strip().lower()
    if scheme not in PROXY_SCHEMES:
        scheme = "http"
    if "://" in value:
        parsed_scheme = value.split("://", 1)[0].strip().lower()
        if parsed_scheme not in PROXY_SCHEMES:
            raise ValueError(f"Unsupported proxy protocol: {parsed_scheme}")
        return parsed_scheme + "://" + value.split("://", 1)[1]
    # Most imported providers use host:port:user:password. Split at
    # most three times so a colon inside the password remains valid.
    parts = value.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit():
        host, port, user, password = parts
        return f"{scheme}://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}@{host}:{port}"
    if len(parts) == 2 and parts[1].isdigit():
        return f"{scheme}://{value}"
    # Also accept the common login:password:host:port provider export.
    reverse = value.rsplit(":", 3)
    if len(reverse) == 4 and reverse[3].isdigit() and ("." in reverse[2] or reverse[2].lower() == "localhost"):
        user, password, host, port = reverse
        return f"{scheme}://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}@{host}:{port}"
    return f"{scheme}://{value}"


def proxy_scheme(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return "http"
    try:
        parsed = urllib.parse.urlparse(value if "://" in value else "http://" + value)
        scheme = str(parsed.scheme or "http").lower()
        return scheme if scheme in PROXY_SCHEMES else "http"
    except Exception:
        return "http"


def mask_proxy(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlparse(value if "://" in value else "http://" + value)
        host = parsed.hostname or "proxy"
        port = f":{parsed.port}" if parsed.port else ""
        scheme = parsed.scheme or "http"
        return f"{scheme}://{host}{port}"
    except Exception:
        return "configured"


def clean_connection_name(raw: Any) -> str:
    value = re.sub(r"\s+", " ", str(raw or "").strip())
    value = re.sub(r"[<>\x00-\x1f]", "", value)[:48].strip()
    if not value:
        raise ValueError("Connection name is required")
    return value


def connection_payload(row: sqlite3.Row | dict[str, Any], assigned_count: int = 0) -> dict[str, Any]:
    d = dict(row)
    ctype = str(d.get("connection_type") or "static")
    return {
        "id": int(d.get("id") or 0),
        "name": str(d.get("name") or "Connection"),
        "type": ctype,
        "proxy_masked": mask_proxy(str(d.get("proxy_url") or "")),
        "proxy_scheme": proxy_scheme(str(d.get("proxy_url") or "")),
        "has_proxy": bool(str(d.get("proxy_url") or "").strip()),
        "has_rotation": bool(str(d.get("rotation_url") or "").strip()),
        "rotation_method": str(d.get("rotation_method") or "GET").upper(),
        "rotation_wait_seconds": int(d.get("rotation_wait_seconds") or 0),
        "rotate_before_first": bool(int(d.get("rotate_before_first") or 0)),
        "enabled": bool(int(d.get("enabled") if d.get("enabled") is not None else 1)),
        "last_status": str(d.get("last_status") or ""),
        "last_error": str(d.get("last_error") or ""),
        "last_ip": str(d.get("last_ip") or ""),
        "last_checked_at": str(d.get("last_checked_at") or ""),
        "last_rotated_at": str(d.get("last_rotated_at") or ""),
        "assigned_accounts": int(assigned_count or 0),
        "group_id": int(d.get("group_id") or 0),
        "group_name": str(d.get("group_name") or ""),
        "quarantined": bool(int(d.get("quarantined") or 0)),
        "failure_count": int(d.get("failure_count") or 0),
        "quarantined_at": str(d.get("quarantined_at") or ""),
    }


def list_connections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_connection_schema(conn)
    counts = {
        int(row["web_connection_id"] or 0): int(row["c"] or 0)
        for row in conn.execute(
            "SELECT web_connection_id,COUNT(*) AS c FROM accounts WHERE COALESCE(web_upload_enabled,1)=1 GROUP BY web_connection_id"
        ).fetchall()
    }
    rows = conn.execute(
        """
        SELECT c.*,COALESCE(g.name,'') AS group_name
        FROM web_connections c
        LEFT JOIN proxy_groups g ON g.id=c.group_id
        WHERE c.enabled=1
        ORDER BY CASE c.connection_type WHEN 'direct' THEN 0 WHEN 'mobile' THEN 1 WHEN 'phone' THEN 2 ELSE 3 END,
                 COALESCE(g.name,''),c.name
        """
    ).fetchall()
    return [connection_payload(row, counts.get(int(row["id"]), 0)) for row in rows]


def get_connection(conn: sqlite3.Connection, connection_id: int) -> dict[str, Any] | None:
    ensure_connection_schema(conn)
    row = conn.execute("SELECT * FROM web_connections WHERE id=? AND enabled=1", (int(connection_id),)).fetchone()
    return dict(row) if row else None


def upsert_connection(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    ensure_connection_schema(conn)
    connection_id = int(body.get("id") or 0)
    old = None
    if connection_id:
        old = conn.execute("SELECT * FROM web_connections WHERE id=?", (connection_id,)).fetchone()
        if not old:
            raise ValueError("Connection not found")

    ctype = str(
        body.get("type")
        or body.get("connection_type")
        or (old["connection_type"] if old else "static")
    ).strip().lower()
    if ctype not in CONNECTION_TYPES:
        raise ValueError("Connection type must be direct, static, mobile or phone")
    name = clean_connection_name(body.get("name") or (old["name"] if old else ("Direct" if ctype == "direct" else "Connection")))

    proxy_supplied = "proxy_url" in body or "proxy" in body
    rotation_supplied = "rotation_url" in body
    proxy_raw = body.get("proxy_url") if "proxy_url" in body else body.get("proxy")
    requested_scheme = str(body.get("proxy_scheme") or "").strip().lower()
    if requested_scheme and requested_scheme not in UI_PROXY_SCHEMES:
        raise ValueError("Proxy protocol must be HTTP, HTTPS or SOCKS5")
    fallback_scheme = requested_scheme or (proxy_scheme(str(old["proxy_url"] or "")) if old else "http")
    proxy_url = normalize_proxy(str(proxy_raw or ""), fallback_scheme) if proxy_supplied else str(old["proxy_url"] or "") if old else ""
    rotation_url = str(body.get("rotation_url") or "").strip() if rotation_supplied else str(old["rotation_url"] or "") if old else ""
    method = str(body.get("rotation_method") or (old["rotation_method"] if old else "GET")).strip().upper()
    if method not in {"GET", "POST"}:
        method = "GET"
    wait_raw = body.get("rotation_wait_seconds")
    if wait_raw in (None, ""):
        wait_raw = old["rotation_wait_seconds"] if old else 12
    wait = max(0, min(int(wait_raw), 180))
    rotate_first = 1 if bool(body.get("rotate_before_first", bool(old["rotate_before_first"]) if old else True)) else 0

    if ctype == "direct":
        proxy_url = ""
        rotation_url = ""
        wait = 0
        rotate_first = 0
    elif not proxy_url:
        raise ValueError("Proxy endpoint is required")

    if old and str(old["connection_type"]) == "direct" and ctype != "direct":
        raise ValueError("The Direct connection cannot be changed")

    # A mobile endpoint is one shared connection, even when it is added again
    # from another account's Details window. Reuse it instead of creating
    # duplicate mobile lanes that would rotate independently.
    if not old and ctype in {"mobile", "phone"} and proxy_url:
        existing = conn.execute(
            "SELECT * FROM web_connections WHERE enabled=1 AND connection_type=? AND proxy_url=? ORDER BY id LIMIT 1",
            (ctype, proxy_url),
        ).fetchone()
        if existing:
            connection_id = int(existing["id"])
            updates = []
            params: list[Any] = []
            if rotation_url and not str(existing["rotation_url"] or "").strip():
                updates.append("rotation_url=?")
                params.append(rotation_url)
            if rotation_url:
                updates.extend(["rotation_method=?", "rotation_wait_seconds=?", "rotate_before_first=?"])
                params.extend([method, wait, rotate_first])
            if updates:
                params.append(connection_id)
                conn.execute(
                    f"UPDATE web_connections SET {','.join(updates)},updated_at=datetime('now') WHERE id=?",
                    params,
                )
                conn.commit()
            reused = get_connection(conn, connection_id) or dict(existing)
            reused["_reused"] = True
            return reused

    if old:
        conn.execute(
            """
            UPDATE web_connections SET name=?,connection_type=?,proxy_url=?,rotation_url=?,rotation_method=?,
                rotation_wait_seconds=?,rotate_before_first=?,updated_at=datetime('now') WHERE id=?
            """,
            (name, ctype, proxy_url, rotation_url, method, wait, rotate_first, connection_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO web_connections(name,connection_type,proxy_url,rotation_url,rotation_method,
                rotation_wait_seconds,rotate_before_first,last_status,updated_at)
            VALUES (?,?,?,?,?,?,?,'saved',datetime('now'))
            """,
            (name, ctype, proxy_url, rotation_url, method, wait, rotate_first),
        )
        connection_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE accounts SET proxy=?,updated_at=datetime('now') WHERE web_connection_id=?",
        (proxy_url, connection_id),
    )
    conn.commit()
    return get_connection(conn, connection_id) or {}


def assign_connection(
    conn: sqlite3.Connection,
    account_names: Iterable[str],
    connection_id: int,
    *,
    commit: bool = True,
) -> int:
    if commit:
        ensure_connection_schema(conn)
    names = list(dict.fromkeys(str(x).strip().lstrip("@") for x in account_names if str(x).strip()))
    if not names:
        return 0
    if commit:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM web_connections WHERE id=? AND enabled=1",
            (int(connection_id),),
        ).fetchone()
        connection = dict(row) if row else None
        if not connection:
            raise ValueError("Connection not found")

        # The ownership check and assignment share one SQLite write
        # transaction, so concurrent servers cannot claim the same static slot.
        if str(connection.get("connection_type") or "") == "static":
            if len(names) > 1:
                raise ValueError("A static proxy can be assigned to one account only")
            occupied = conn.execute(
                "SELECT name FROM accounts WHERE web_connection_id=? AND name<>? AND COALESCE(web_upload_enabled,1)=1 LIMIT 1",
                (int(connection_id), names[0]),
            ).fetchone()
            if occupied:
                raise ValueError(f"This static proxy is already assigned to {occupied['name']}")

        placeholders = ",".join("?" for _ in names)
        cur = conn.execute(
            f"UPDATE accounts SET web_connection_id=?,proxy=?,status=CASE WHEN status='low_quality_proxy' THEN 'ready' ELSE status END,web_upload_last_error=CASE WHEN status='low_quality_proxy' THEN '' ELSE web_upload_last_error END,updated_at=datetime('now') WHERE name IN ({placeholders}) AND COALESCE(web_upload_enabled,1)=1",
            [int(connection_id), str(connection.get("proxy_url") or ""), *names],
        )
        if commit:
            conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        if commit:
            conn.rollback()
        raise


def available_static_connections(conn: sqlite3.Connection, group_id: int = 0) -> list[dict[str, Any]]:
    ensure_connection_schema(conn)
    rows = conn.execute(
        """
        SELECT c.*
        FROM web_connections c
        LEFT JOIN accounts a ON a.web_connection_id=c.id
        WHERE c.enabled=1 AND c.connection_type='static' AND COALESCE(c.quarantined,0)=0
          AND (?=0 OR c.group_id=?)
        GROUP BY c.id
        HAVING COUNT(a.name)=0
        ORDER BY c.id
        """
        ,
        (int(group_id or 0), int(group_id or 0)),
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_proxy_group(conn: sqlite3.Connection, raw_name: str) -> dict[str, Any]:
    ensure_connection_schema(conn)
    name = clean_connection_name(raw_name or "Static proxies")
    conn.execute(
        "INSERT OR IGNORE INTO proxy_groups(name,updated_at) VALUES(?,datetime('now'))",
        (name,),
    )
    conn.execute(
        "UPDATE proxy_groups SET enabled=1,updated_at=datetime('now') WHERE name=? COLLATE NOCASE",
        (name,),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM proxy_groups WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    return dict(row) if row else {}


def list_proxy_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_connection_schema(conn)
    rows = conn.execute(
        """
        SELECT g.id,g.name,
               COUNT(c.id) AS total,
               SUM(CASE WHEN c.enabled=1 AND COALESCE(c.quarantined,0)=0 THEN 1 ELSE 0 END) AS usable,
               SUM(CASE WHEN c.enabled=1 AND COALESCE(c.quarantined,0)=1 THEN 1 ELSE 0 END) AS quarantined,
               SUM(CASE WHEN c.enabled=1 AND COALESCE(c.quarantined,0)=0 AND a.name IS NULL THEN 1 ELSE 0 END) AS free,
               SUM(CASE WHEN c.enabled=1 AND COALESCE(c.quarantined,0)=0 AND a.name IS NOT NULL THEN 1 ELSE 0 END) AS assigned
        FROM proxy_groups g
        LEFT JOIN web_connections c ON c.group_id=g.id AND c.connection_type='static'
        LEFT JOIN accounts a ON a.web_connection_id=c.id
        WHERE g.enabled=1
        GROUP BY g.id,g.name
        ORDER BY g.name
        """
    ).fetchall()
    return [dict(row) for row in rows]


def assign_static_group(conn: sqlite3.Connection, account_names: Iterable[str], group_id: int) -> dict[str, Any]:
    names = list(dict.fromkeys(str(x).strip().lstrip("@") for x in account_names if str(x).strip()))
    ensure_connection_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        free = [
            dict(row)
            for row in conn.execute(
                """
                SELECT c.*
                FROM web_connections c
                LEFT JOIN accounts a ON a.web_connection_id=c.id
                WHERE c.enabled=1 AND c.connection_type='static' AND COALESCE(c.quarantined,0)=0
                  AND (?=0 OR c.group_id=?)
                GROUP BY c.id
                HAVING COUNT(a.name)=0
                ORDER BY c.id
                """,
                (int(group_id or 0), int(group_id or 0)),
            ).fetchall()
        ]
        assigned: list[str] = []
        for account_name, connection in zip(names, free):
            assign_connection(
                conn, [account_name], int(connection["id"]), commit=False
            )
            assigned.append(account_name)
        conn.commit()
        return {
            "assigned": len(assigned),
            "unassigned": [name for name in names if name not in set(assigned)],
        }
    except Exception:
        conn.rollback()
        raise


def quarantine_static_connection(
    conn: sqlite3.Connection,
    account_name: str,
    connection_id: int,
    detail: str,
    allow_replacement: bool = True,
) -> dict[str, Any] | None:
    """Permanently delete a confirmed bad static slot and assign a free peer.

    The old endpoint is physically deleted from web_connections so it can
    never be re-used. A replacement is drawn only from the same saved group
    so geography/provider intent is preserved.
    """
    ensure_connection_schema(conn)
    old = conn.execute(
        "SELECT id,connection_type,group_id FROM web_connections WHERE id=?",
        (int(connection_id),),
    ).fetchone()
    if not old or str(old["connection_type"] or "") != "static":
        return None
    group_id = int(old["group_id"] or 0)
    conn.execute("BEGIN IMMEDIATE")
    try:
        replacement = conn.execute(
            """
            SELECT c.* FROM web_connections c
            LEFT JOIN accounts a ON a.web_connection_id=c.id AND COALESCE(a.web_upload_enabled,1)=1
            WHERE c.connection_type='static' AND c.enabled=1 AND COALESCE(c.quarantined,0)=0
              AND c.id<>? AND c.group_id=?
            GROUP BY c.id
            HAVING COUNT(a.name)=0
            ORDER BY CASE WHEN c.last_status='healthy' THEN 0 ELSE 1 END,c.id
            LIMIT 1
            """,
            (int(connection_id), group_id),
        ).fetchone() if allow_replacement else None
        conn.execute(
            "DELETE FROM web_connections WHERE id=?",
            (int(connection_id),),
        )
        if replacement:
            conn.execute(
                """
                UPDATE accounts
                SET web_connection_id=?,proxy=?,status='ready',
                    web_upload_last_error=?,updated_at=datetime('now')
                WHERE name=?
                """,
                (
                    int(replacement["id"]),
                    str(replacement["proxy_url"] or ""),
                    f"proxy_deleted: removed connection {int(connection_id)} ({detail}); replacement {replacement['name']}",
                    str(account_name),
                ),
            )
            conn.commit()
            return dict(replacement)
        conn.execute(
            """
            UPDATE accounts
            SET status='low_quality_proxy',
                web_upload_last_error=?,updated_at=datetime('now')
            WHERE name=?
            """,
            (
                "low_quality_proxy: proxy deleted; no free replacement in the same proxy group",
                str(account_name),
            ),
        )
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise


def restore_quarantined_connection(conn: sqlite3.Connection, connection_id: int) -> dict[str, Any]:
    ensure_connection_schema(conn)
    row = conn.execute(
        "SELECT * FROM web_connections WHERE id=? AND connection_type='static'",
        (int(connection_id),),
    ).fetchone()
    if not row:
        raise ValueError("Static proxy not found")
    conn.execute(
        """
        UPDATE web_connections
        SET quarantined=0,failure_count=0,last_status='saved',last_error='',
            quarantined_at='',updated_at=datetime('now')
        WHERE id=?
        """,
        (int(connection_id),),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM web_connections WHERE id=?", (int(connection_id),)).fetchone())


def remove_connection(conn: sqlite3.Connection, connection_id: int) -> int:
    ensure_connection_schema(conn)
    row = conn.execute("SELECT connection_type FROM web_connections WHERE id=?", (int(connection_id),)).fetchone()
    if not row:
        raise ValueError("Connection not found")
    if str(row[0]) == "direct":
        raise ValueError("Direct cannot be deleted")
    direct_id = direct_connection_id(conn)
    cur = conn.execute(
        "UPDATE accounts SET web_connection_id=?,proxy='',updated_at=datetime('now') WHERE web_connection_id=?",
        (direct_id, int(connection_id)),
    )
    conn.execute("UPDATE web_connections SET enabled=0,updated_at=datetime('now') WHERE id=?", (int(connection_id),))
    conn.commit()
    return int(cur.rowcount or 0)


def account_connections(
    conn: sqlite3.Connection,
    names: list[str],
    *,
    include_parser_accounts: bool = False,
) -> list[dict[str, Any]]:
    ensure_connection_schema(conn)
    if not names:
        return []
    placeholders = ",".join("?" for _ in names)
    enabled_filter = "" if include_parser_accounts else "AND COALESCE(a.web_upload_enabled,1)=1"
    rows = conn.execute(
        f"""
        SELECT a.name,a.status,a.web_upload_login_status,a.web_upload_last_error,
               a.web_connection_id,c.name AS connection_name,c.connection_type,c.proxy_url,
               c.rotation_url,c.rotation_method,c.rotation_wait_seconds,c.rotate_before_first,c.enabled
        FROM accounts a
        LEFT JOIN web_connections c ON c.id=a.web_connection_id
        WHERE a.name IN ({placeholders}) {enabled_filter}
        ORDER BY a.name
        """,
        names,
    ).fetchall()
    direct_id = direct_connection_id(conn)
    result = []
    for row in rows:
        d = dict(row)
        if not d.get("web_connection_id") or not d.get("connection_type"):
            d.update({
                "web_connection_id": direct_id,
                "connection_name": "Direct",
                "connection_type": "direct",
                "proxy_url": "",
                "rotation_url": "",
                "rotation_method": "GET",
                "rotation_wait_seconds": 0,
                "rotate_before_first": 0,
                "enabled": 1,
            })
        result.append(d)
    return result


def delete_proxy_group(conn: sqlite3.Connection, group_id: int) -> dict[str, Any]:
    """Delete one explicit static group without ever sending its accounts Direct."""
    ensure_connection_schema(conn)
    group = conn.execute(
        "SELECT id,name FROM proxy_groups WHERE id=? AND enabled=1",
        (int(group_id),),
    ).fetchone()
    if not group:
        raise ValueError("Static proxy group not found")
    connection_ids = [
        int(row[0]) for row in conn.execute(
            "SELECT id FROM web_connections WHERE group_id=? AND connection_type='static'",
            (int(group_id),),
        ).fetchall()
    ]
    affected = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        if connection_ids:
            placeholders = ",".join("?" for _ in connection_ids)
            affected = int(conn.execute(
                f"SELECT COUNT(*) FROM accounts WHERE web_connection_id IN ({placeholders})",
                connection_ids,
            ).fetchone()[0])
            # Keep affected accounts blocked for proxy work. Moving them to the
            # Direct lane would silently expose the computer IP on the next run.
            conn.execute(
                f"""
                UPDATE accounts
                SET web_connection_id=NULL,proxy='',status='proxy_required',
                    web_upload_last_error='proxy_required: assigned proxy group was deleted',
                    updated_at=datetime('now')
                WHERE web_connection_id IN ({placeholders})
                """,
                connection_ids,
            )
            conn.execute(
                f"DELETE FROM web_connections WHERE id IN ({placeholders})",
                connection_ids,
            )
        conn.execute("DELETE FROM proxy_groups WHERE id=?", (int(group_id),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"deleted": True, "connections": len(connection_ids), "affected_accounts": affected}


def create_proxy_group(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """Create a new empty proxy group."""
    ensure_connection_schema(conn)
    clean = clean_connection_name(name or "New group")
    existing = conn.execute(
        "SELECT id FROM proxy_groups WHERE name=? COLLATE NOCASE AND enabled=1",
        (clean,),
    ).fetchone()
    if existing:
        raise ValueError(f"A proxy group named '{clean}' already exists")
    conn.execute(
        "INSERT INTO proxy_groups(name,enabled,updated_at) VALUES(?,1,datetime('now'))",
        (clean,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM proxy_groups WHERE name=? COLLATE NOCASE", (clean,)
    ).fetchone()
    return dict(row) if row else {}


def rename_proxy_group(conn: sqlite3.Connection, group_id: int, new_name: str) -> dict[str, Any]:
    """Rename an existing proxy group."""
    ensure_connection_schema(conn)
    clean = clean_connection_name(new_name or "")
    if not clean:
        raise ValueError("Group name cannot be empty")
    group = conn.execute(
        "SELECT id,name FROM proxy_groups WHERE id=? AND enabled=1",
        (int(group_id),),
    ).fetchone()
    if not group:
        raise ValueError("Proxy group not found")
    dup = conn.execute(
        "SELECT id FROM proxy_groups WHERE name=? COLLATE NOCASE AND id<>? AND enabled=1",
        (clean, int(group_id)),
    ).fetchone()
    if dup:
        raise ValueError(f"A proxy group named '{clean}' already exists")
    conn.execute(
        "UPDATE proxy_groups SET name=?,updated_at=datetime('now') WHERE id=?",
        (clean, int(group_id)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM proxy_groups WHERE id=?", (int(group_id),)
    ).fetchone()
    return dict(row) if row else {}


def add_proxies_to_group(
    conn: sqlite3.Connection, group_id: int, raw: str, prefix: str = "Static"
) -> list[dict[str, Any]]:
    """Add static proxies to an existing group."""
    ensure_connection_schema(conn)
    group = conn.execute(
        "SELECT id,name FROM proxy_groups WHERE id=? AND enabled=1",
        (int(group_id),),
    ).fetchone()
    if not group:
        raise ValueError("Proxy group not found")
    group_name = str(group["name"])
    lines = [line.strip() for line in re.split(r"[\s,]+", str(raw or "")) if line.strip()]
    if not lines:
        raise ValueError("No proxies provided")
    created: list[dict[str, Any]] = []
    # Determine next index based on existing proxies in the group
    existing_count = int(conn.execute(
        "SELECT COUNT(*) FROM web_connections WHERE group_id=? AND connection_type='static'",
        (int(group_id),),
    ).fetchone()[0])
    for index, line in enumerate(lines, start=existing_count + 1):
        try:
            proxy = normalize_proxy(line)
        except ValueError as exc:
            raise ValueError(f"Static proxy #{index}: {exc}") from exc
        name = f"{prefix or 'Static'} {index:02d}"
        suffix = 2
        original = name
        while conn.execute(
            "SELECT 1 FROM web_connections WHERE name=? COLLATE NOCASE", (name,)
        ).fetchone():
            name = f"{original} ({suffix})"
            suffix += 1
        cur = conn.execute(
            """
            INSERT INTO web_connections(
                name,connection_type,proxy_url,group_id,quarantined,failure_count,last_status,updated_at
            ) VALUES (?,?,?, ?,0,0,'saved',datetime('now'))
            """,
            (name, "static", proxy, int(group_id)),
        )
        created.append(get_connection(conn, int(cur.lastrowid)) or {})
    conn.commit()
    return created


def delete_proxy_from_group(conn: sqlite3.Connection, connection_id: int) -> dict[str, Any]:
    """Delete a single proxy from a group. Accounts using it become proxy_required."""
    ensure_connection_schema(conn)
    row = conn.execute(
        "SELECT id,name,group_id,connection_type FROM web_connections WHERE id=?",
        (int(connection_id),),
    ).fetchone()
    if not row:
        raise ValueError("Proxy not found")
    affected = int(conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE web_connection_id=?",
        (int(connection_id),),
    ).fetchone()[0])
    conn.execute("BEGIN IMMEDIATE")
    try:
        if affected:
            conn.execute(
                """
                UPDATE accounts
                SET web_connection_id=NULL,proxy='',status='proxy_required',
                    web_upload_last_error='proxy_required: assigned proxy was deleted',
                    updated_at=datetime('now')
                WHERE web_connection_id=?
                """,
                (int(connection_id),),
            )
        conn.execute("DELETE FROM web_connections WHERE id=?", (int(connection_id),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"deleted": True, "affected_accounts": affected}


def _rotation_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _emit_rotation_http_exchange(
    *, started_at: str, finished_at: str, elapsed_ms: float, status: int,
    content_type: str, payload: bytes, retry_after: str = "",
    network_error: str = "",
) -> None:
    """Write one URL-free, credential-safe record to the scheduler task log."""
    try:
        detail = describe_rotation_response(status, content_type, payload, retry_after)
        detail.update({
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_ms": round(float(elapsed_ms), 2),
            "content_type": str(content_type or "").split(";", 1)[0].strip().lower(),
            "response_body_bytes": len(payload),
            "response_body_sha256": hashlib.sha256(payload).hexdigest(),
            "network_error": str(network_error or ""),
        })
        print(
            "[ROTATION_HTTP] "
            + json.dumps(
                {"rotation_http_exchange": detail},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
    except Exception:
        # Observability must never change provider or workflow behavior.
        pass


def _rotation_request(connection: dict[str, Any], timeout: float = 30.0) -> RotationResponse:
    url = str(connection.get("rotation_url") or "").strip()
    if not url:
        return RotationResponse("permanent_error", 0, "none", detail="rotation link missing")
    method = str(connection.get("rotation_method") or "GET").upper()
    request = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    request.add_header("User-Agent", "SparkGrid-Connections/1.0")
    started_at = _rotation_timestamp()
    started_clock = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = getattr(response, "headers", {}) or {}
            content_type = str(headers.get("Content-Type") or "")
            retry_after = str(headers.get("Retry-After") or "")
            payload = response.read(-1)
            status = int(response.status)
            result = classify_rotation_response(status, content_type, payload, retry_after)
            _emit_rotation_http_exchange(
                started_at=started_at, finished_at=_rotation_timestamp(),
                elapsed_ms=(time.perf_counter() - started_clock) * 1000,
                status=status, content_type=content_type, payload=payload,
                retry_after=retry_after,
            )
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(-1)
        except Exception:
            payload = b""
        content_type = str(exc.headers.get("Content-Type") or "") if exc.headers else ""
        retry_after = str(exc.headers.get("Retry-After") or "") if exc.headers else ""
        status = int(exc.code)
        result = classify_rotation_response(status, content_type, payload, retry_after)
        _emit_rotation_http_exchange(
            started_at=started_at, finished_at=_rotation_timestamp(),
            elapsed_ms=(time.perf_counter() - started_clock) * 1000,
            status=status, content_type=content_type, payload=payload,
            retry_after=retry_after,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _emit_rotation_http_exchange(
            started_at=started_at, finished_at=_rotation_timestamp(),
            elapsed_ms=(time.perf_counter() - started_clock) * 1000,
            status=0, content_type="", payload=b"",
            network_error=type(exc).__name__,
        )
        result = {"state": "transient_error", "http_status": 0, "response_type": "network", "cooldown_seconds": 0}
    return RotationResponse(**result, detail=f"provider_state={result['state']}")


def _provider_cooldown_seconds(detail: str) -> int:
    text = str(detail or "").lower()
    patterns = (
        r"wait\s+(\d+)\s*(?:s|sec|second)",
        r"retry[- ]?after[=: ]+(\d+)",
        r"try again in\s+(\d+)\s*(?:s|sec|second)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return max(1, min(int(match.group(1)), 120))
    if re.search(r"http(?:\s+error)?\s+429\b", text) or "too many requests" in text:
        return 10
    if re.search(r"http(?:\s+error)?\s+(?:500|502|503|504)\b", text):
        return 5
    if "bad_switch" in text or (re.search(r"http(?:\s+error)?\s+400\b", text) and "switch" in text):
        return 12
    return 0


def rotate_connection(conn: sqlite3.Connection, connection_id: int, sleep_after: bool = True) -> dict[str, Any]:
    ensure_connection_schema(conn)
    connection = get_connection(conn, int(connection_id))
    if not connection:
        raise ValueError("Connection not found")
    ctype = str(connection.get("connection_type") or "")
    if ctype not in {"mobile", "phone"}:
        raise ValueError("Only mobile or phone connections can rotate")
    try:
        info = ""
        provider_waited = 0
        def as_response(value: Any) -> RotationResponse:
            if isinstance(value, RotationResponse):
                return value
            ok, detail = value  # legacy test/call-site compatibility
            result = classify_rotation_response(200 if ok else 400, "text", str(detail or "").encode("utf-8"))
            return RotationResponse(**result, detail=str(detail or ""))
        response = as_response(_rotation_request(connection))
        for attempt in range(1, 3):
            if response.state != "transient_error":
                break
            # Providers commonly reject a second switch while the modem is
            # still changing IP. Honour their real cooldown instead of
            # treating it as a dead rotation link and stopping the lane.
            time.sleep(5)
            provider_waited += 5
            response = as_response(_rotation_request(connection))
        pending = response.state in {"accepted", "in_progress", "cooldown", "provider_busy", "rate_limited"}
        if not response.ready and not pending:
            emit_proxy_telemetry(
                str(connection.get("proxy_url") or ""), proxy_type=ctype,
                phase="rotation_request", normalized_result="failed",
                provider_state="unknown", connectivity="unknown", ip_changed="unknown",
                instagram_reachable="unknown", browser_launched=False,
                retry_attempt=attempt, final_classification="rotation_request_failed",
            )
            raise RuntimeError(f"rotation provider state: {response.state}")
        conn.execute(
            "UPDATE web_connections SET last_status=?,last_error='',updated_at=datetime('now') WHERE id=?",
            ("rotation_ready_pending_gate" if response.ready else "rotation_pending", int(connection_id)),
        )
        conn.commit()
        wait = max(0, min(int(connection.get("rotation_wait_seconds") or 0), 120))
        stabilization_wait = max(
            int(response.cooldown_seconds or 0),
            wait if pending else 0,
        )
        if sleep_after and stabilization_wait:
            time.sleep(stabilization_wait)
        emit_proxy_telemetry(
            str(connection.get("proxy_url") or ""), proxy_type=ctype,
            phase="rotation_response", normalized_result=response.state,
            provider_state=response.state, http_status=response.http_status, cooldown_seconds=response.cooldown_seconds, connectivity="unknown", ip_changed="unknown",
            instagram_reachable="unknown", browser_launched=False,
            retry_attempt=attempt, final_classification="rotation_pending" if pending else "rotation_ready_pending_gate",
        )
        return {
            "ok": True,
            "message": response.detail,
            "waited": provider_waited + (stabilization_wait if sleep_after else 0),
            "provider_waited": provider_waited,
            "provider_state": response.state,
            "pending": pending,
            "provider_wait_seconds": stabilization_wait,
            "rotation_stage": (
                "ROTATION_COMMAND_ACCEPTED"
                if response.state == "accepted"
                else "ROTATION_REQUESTED"
                if pending
                else "ROTATION_COMMAND_ACCEPTED"
            ),
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        conn.execute(
            "UPDATE web_connections SET last_status='rotation_failed',last_error=?,updated_at=datetime('now') WHERE id=?",
            (error, int(connection_id)),
        )
        conn.commit()
        return {"ok": False, "error": error, "waited": 0}


def import_static_connections(
    conn: sqlite3.Connection,
    raw: str,
    prefix: str = "Static",
    group_name: str = "",
) -> list[dict[str, Any]]:
    ensure_connection_schema(conn)
    group = ensure_proxy_group(conn, group_name or prefix or "Static proxies")
    group_id = int(group.get("id") or 0)
    # Provider exports are commonly newline, tab, or space separated.
    # Treat every whitespace-delimited token as one proxy so pasting three
    # proxies on one row cannot silently create only one connection.
    lines = [line.strip() for line in re.split(r"[\s,]+", str(raw or "")) if line.strip()]
    created: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        try:
            proxy = normalize_proxy(line)
        except ValueError as exc:
            raise ValueError(f"Static proxy #{index}: {exc}") from exc
        # A pasted line represents a dedicated static slot. Providers often
        # sell several sessions through the same gateway, so equal endpoint strings
        # must remain separate rows and be assignable one-to-one to accounts.
        name = f"{prefix} {index:02d}"
        suffix = 2
        original = name
        # Disabled/deleted connections still occupy the UNIQUE name
        # in SQLite. Include them when choosing the suffix; otherwise adding a
        # new static proxy after deleting Static 01 raises HTTP 500.
        while conn.execute("SELECT 1 FROM web_connections WHERE name=? COLLATE NOCASE", (name,)).fetchone():
            name = f"{original} ({suffix})"
            suffix += 1
        cur = conn.execute(
            """
            INSERT INTO web_connections(
                name,connection_type,proxy_url,group_id,quarantined,failure_count,last_status,updated_at
            ) VALUES (?,?,?, ?,0,0,'saved',datetime('now'))
            """,
            (name, "static", proxy, group_id),
        )
        created.append(get_connection(conn, int(cur.lastrowid)) or {})
    conn.commit()
    return created
