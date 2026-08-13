#!/usr/bin/env python3
"""Durable publication slots for reusable Scale assets."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List


ACCEPTED_SLOT_STATES = {"processing", "uploaded_unverified", "verified", "completed"}
OCCUPIED_SLOT_STATES = ACCEPTED_SLOT_STATES | {"intent", "publishing"}


def ensure_slot_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ig_publication_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL,
            campaign_run_identity TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            asset_id INTEGER NOT NULL DEFAULT 0,
            plan_item_id INTEGER NOT NULL DEFAULT 0,
            slot_order INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            history_id INTEGER NOT NULL DEFAULT 0,
            share_clicked_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            usage_recorded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_name,campaign_run_identity,slot_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ig_publication_slots_pending "
        "ON ig_publication_slots(account_name,campaign_run_identity,status,slot_order,id)"
    )
    asset_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(api_content_assets)")}
    if asset_columns and "publication_use_count" not in asset_columns:
        conn.execute(
            "ALTER TABLE api_content_assets ADD COLUMN publication_use_count INTEGER NOT NULL DEFAULT 0"
        )


def prepare_publication_slots(
    conn: sqlite3.Connection,
    *,
    account_name: str,
    campaign_run_identity: str,
    items: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Create one durable row per planned publication and return pending items."""
    ensure_slot_schema(conn)
    planned = list(items)
    by_key: Dict[str, Dict[str, Any]] = {}
    for order, raw in enumerate(planned, start=1):
        item = dict(raw)
        slot_key = str(item.get("slot_key") or f"slot:{order}")
        asset_id = int(item.get("asset_id") or item.get("id") or 0)
        plan_item_id = int(item.get("plan_item_id") or 0)
        conn.execute(
            """
            INSERT INTO ig_publication_slots(
                account_name,campaign_run_identity,slot_key,asset_id,plan_item_id,slot_order
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(account_name,campaign_run_identity,slot_key) DO UPDATE SET
                asset_id=excluded.asset_id,plan_item_id=excluded.plan_item_id,
                slot_order=excluded.slot_order,updated_at=datetime('now')
            """,
            (str(account_name), str(campaign_run_identity), slot_key, asset_id, plan_item_id, order),
        )
        by_key[slot_key] = item
    placeholders = ",".join("?" for _ in OCCUPIED_SLOT_STATES)
    rows = conn.execute(
        f"""
        SELECT id,slot_key,status FROM ig_publication_slots
        WHERE account_name=? AND campaign_run_identity=?
          AND status NOT IN ({placeholders})
          AND slot_order < COALESCE((
              SELECT MIN(blocked.slot_order)
              FROM ig_publication_slots AS blocked
              WHERE blocked.account_name=ig_publication_slots.account_name
                AND blocked.campaign_run_identity=ig_publication_slots.campaign_run_identity
                AND blocked.status IN ('intent','publishing')
          ),2147483647)
        ORDER BY slot_order,id
        """,
        (str(account_name), str(campaign_run_identity), *sorted(OCCUPIED_SLOT_STATES)),
    ).fetchall()
    prepared: List[Dict[str, Any]] = []
    for row in rows:
        item = by_key.get(str(row["slot_key"]))
        if item is None:
            continue
        item = dict(item)
        item["publication_slot_id"] = int(row["id"])
        item["campaign_run_identity"] = str(campaign_run_identity)
        item["slot_key"] = str(row["slot_key"])
        item["slot_status"] = str(row["status"] or "pending")
        prepared.append(item)
    conn.commit()
    return prepared


def slot_rows(
    conn: sqlite3.Connection, account_name: str, campaign_run_identity: str
) -> List[Dict[str, Any]]:
    ensure_slot_schema(conn)
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM ig_publication_slots
            WHERE account_name=? AND campaign_run_identity=?
            ORDER BY slot_order,id
            """,
            (str(account_name), str(campaign_run_identity)),
        )
    ]


def slot_progress(
    conn: sqlite3.Connection, account_name: str, campaign_run_identity: str
) -> Dict[str, Any]:
    """Return authoritative whole-run progress and the next executable slot."""
    rows = slot_rows(conn, account_name, campaign_run_identity)
    accepted = [row for row in rows if str(row["status"] or "") in ACCEPTED_SLOT_STATES]
    occupied = [row for row in rows if str(row["status"] or "") in OCCUPIED_SLOT_STATES]
    blocking_orders = [
        int(row["slot_order"])
        for row in rows
        if str(row["status"] or "") in {"intent", "publishing"}
    ]
    first_blocking_order = min(blocking_orders) if blocking_orders else 2147483647
    executable = [
        row for row in rows
        if str(row["status"] or "") not in OCCUPIED_SLOT_STATES
        and int(row["slot_order"]) < first_blocking_order
    ]
    total = len(rows)
    completed = len(accepted)
    if total and completed == total:
        status = "success"
    elif any(str(row["status"] or "") in {"intent", "publishing"} for row in occupied):
        status = "processing"
    elif completed:
        status = "partial_success"
    else:
        status = "pending"
    return {
        "account_name": str(account_name),
        "campaign_run_identity": str(campaign_run_identity),
        "status": status,
        "completed": completed,
        "total": total,
        "remaining": max(0, total - completed),
        "executable": len(executable),
        "next_slot_id": int(executable[0]["id"]) if executable else 0,
        "next_slot_key": str(executable[0]["slot_key"]) if executable else "",
    }


def latest_scale_progress(conn: sqlite3.Connection, account_name: str) -> Dict[str, Any]:
    """Return the most recently materialized Scale run for UI/API reporting."""
    ensure_slot_schema(conn)
    row = conn.execute(
        """
        SELECT campaign_run_identity,MAX(id) AS last_slot_id
        FROM ig_publication_slots
        WHERE account_name=? AND campaign_run_identity LIKE 'scale-%'
        GROUP BY campaign_run_identity
        ORDER BY last_slot_id DESC
        LIMIT 1
        """,
        (str(account_name),),
    ).fetchone()
    if not row:
        return {
            "campaign_run_identity": "", "status": "not_started",
            "completed": 0, "total": 0, "remaining": 0,
            "executable": 0, "next_slot_id": 0, "next_slot_key": "",
        }
    return slot_progress(conn, account_name, str(row["campaign_run_identity"]))
