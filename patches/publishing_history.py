#!/usr/bin/env python3
"""Shared publication-history helpers for SparkGrid standalone Web Upload."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from publication_slots import ensure_slot_schema, slot_progress


def utc_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))).strftime("%Y-%m-%d %H:%M:%S")


def ensure_history_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ig_publishing_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL DEFAULT 0,
            run_id TEXT NOT NULL DEFAULT '',
            account_name TEXT NOT NULL DEFAULT '',
            asset_id INTEGER NOT NULL DEFAULT 0,
            video_name TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL DEFAULT '',
            caption TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL DEFAULT 'api',
            provider TEXT NOT NULL DEFAULT '',
            background_web INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'processing',
            media_id TEXT NOT NULL DEFAULT '',
            shortcode TEXT NOT NULL DEFAULT '',
            permalink TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 1,
            verification_attempts INTEGER NOT NULL DEFAULT 0,
            published_at TEXT NOT NULL DEFAULT '',
            verified_at TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT NOT NULL DEFAULT '',
            next_verify_at TEXT NOT NULL DEFAULT '',
            publish_intent_at TEXT NOT NULL DEFAULT '',
            share_clicked_at TEXT NOT NULL DEFAULT '',
            publish_request_started_at TEXT NOT NULL DEFAULT '',
            publish_request_finished_at TEXT NOT NULL DEFAULT '',
            publish_request_state TEXT NOT NULL DEFAULT '',
            publish_request_path TEXT NOT NULL DEFAULT '',
            publish_http_status INTEGER NOT NULL DEFAULT 0,
            publication_slot_id INTEGER NOT NULL DEFAULT 0,
            asset_usage_recorded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(ig_publishing_history)")}
    required = {
        "job_id": "INTEGER NOT NULL DEFAULT 0",
        "run_id": "TEXT NOT NULL DEFAULT ''",
        "account_name": "TEXT NOT NULL DEFAULT ''",
        "asset_id": "INTEGER NOT NULL DEFAULT 0",
        "video_name": "TEXT NOT NULL DEFAULT ''",
        "file_path": "TEXT NOT NULL DEFAULT ''",
        "caption": "TEXT NOT NULL DEFAULT ''",
        "engine": "TEXT NOT NULL DEFAULT 'api'",
        "provider": "TEXT NOT NULL DEFAULT ''",
        "background_web": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'processing'",
        "media_id": "TEXT NOT NULL DEFAULT ''",
        "shortcode": "TEXT NOT NULL DEFAULT ''",
        "permalink": "TEXT NOT NULL DEFAULT ''",
        "error": "TEXT NOT NULL DEFAULT ''",
        "attempts": "INTEGER NOT NULL DEFAULT 1",
        "verification_attempts": "INTEGER NOT NULL DEFAULT 0",
        "published_at": "TEXT NOT NULL DEFAULT ''",
        "verified_at": "TEXT NOT NULL DEFAULT ''",
        "last_checked_at": "TEXT NOT NULL DEFAULT ''",
        "next_verify_at": "TEXT NOT NULL DEFAULT ''",
        "publish_intent_at": "TEXT NOT NULL DEFAULT ''",
        "share_clicked_at": "TEXT NOT NULL DEFAULT ''",
        "publish_request_started_at": "TEXT NOT NULL DEFAULT ''",
        "publish_request_finished_at": "TEXT NOT NULL DEFAULT ''",
        "publish_request_state": "TEXT NOT NULL DEFAULT ''",
        "publish_request_path": "TEXT NOT NULL DEFAULT ''",
        "publish_http_status": "INTEGER NOT NULL DEFAULT 0",
        "publication_slot_id": "INTEGER NOT NULL DEFAULT 0",
        "asset_usage_recorded": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in required.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE ig_publishing_history ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ig_publish_history_status_due ON ig_publishing_history(status,next_verify_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ig_publish_history_account ON ig_publishing_history(account_name,id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ig_publish_history_slot ON ig_publishing_history(publication_slot_id,id DESC)")
    ensure_slot_schema(conn)
    conn.commit()


def create_history(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    run_id: str,
    account_name: str,
    asset: Dict[str, Any],
    engine: str,
    provider: str = "",
    background_web: bool = False,
    caption: str = "",
    history_id: int = 0,
    publication_slot_id: int = 0,
) -> int:
    ensure_history_schema(conn)
    asset_id = int(asset.get("id") or 0)
    file_path = str(asset.get("file_path") or "")
    video_name = str(asset.get("original_name") or Path(file_path).name or "")
    if history_id:
        existing = conn.execute(
            "SELECT status,COALESCE(publish_intent_at,'') AS publish_intent_at FROM ig_publishing_history WHERE id=?",
            (int(history_id),),
        ).fetchone()
        # An explicit/manual retry route must not silently convert an occupied
        # ambiguous slot back to processing.  ISSUE-023 intentionally offers
        # no automatic replay after the durable boundary.
        if existing and (str(existing["publish_intent_at"] or "") or str(existing["status"] or "") in {"submitted_unverified", "uploaded_unverified", "uploaded", "confirmed", "verified"}):
            return int(history_id)
        conn.execute(
            """
            UPDATE ig_publishing_history
            SET job_id=?, run_id=?, account_name=?, asset_id=?, video_name=?, file_path=?, caption=?,
                engine=?, provider=?, background_web=?, status='processing', error='',
                media_id='', shortcode='', permalink='', published_at='', verified_at='',
                verification_attempts=0, last_checked_at='', next_verify_at='',
                publication_slot_id=CASE WHEN ?>0 THEN ? ELSE publication_slot_id END,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (
                int(job_id), str(run_id), str(account_name), asset_id, video_name, file_path,
                str(caption or ""), str(engine), str(provider), 1 if background_web else 0,
                int(publication_slot_id or 0), int(publication_slot_id or 0),
                int(history_id),
            ),
        )
        conn.commit()
        return int(history_id)
    cur = conn.execute(
        """
        INSERT INTO ig_publishing_history(
            job_id,run_id,account_name,asset_id,video_name,file_path,caption,engine,provider,
            background_web,status,attempts,publication_slot_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,'processing',1,?)
        """,
        (
            int(job_id), str(run_id), str(account_name), asset_id, video_name, file_path,
            str(caption or ""), str(engine), str(provider), 1 if background_web else 0,
            int(publication_slot_id or 0),
        ),
    )
    history_id = int(cur.lastrowid)
    if int(publication_slot_id or 0):
        conn.execute(
            "UPDATE ig_publication_slots SET history_id=?,updated_at=datetime('now') "
            "WHERE id=? AND status NOT IN ('publishing','processing','uploaded_unverified','verified','completed')",
            (history_id, int(publication_slot_id)),
        )
    conn.commit()
    return history_id


def update_history(conn: sqlite3.Connection, history_id: int, **values: Any) -> None:
    if not history_id or not values:
        return
    ensure_history_schema(conn)
    allowed = {
        "job_id", "run_id", "account_name", "asset_id", "video_name", "file_path", "caption",
        "engine", "provider", "background_web", "status", "media_id", "shortcode", "permalink",
        "error", "attempts", "verification_attempts", "published_at", "verified_at",
        "last_checked_at", "next_verify_at", "publish_intent_at", "share_clicked_at",
        "publish_request_started_at", "publish_request_finished_at", "publish_request_state",
        "publish_request_path", "publish_http_status", "publication_slot_id", "asset_usage_recorded",
    }
    parts = []
    params = []
    for key, value in values.items():
        if key not in allowed:
            continue
        parts.append(f"{key}=?")
        params.append(value)
    if not parts:
        return
    parts.append("updated_at=datetime('now')")
    params.append(int(history_id))
    conn.execute(f"UPDATE ig_publishing_history SET {', '.join(parts)} WHERE id=?", params)
    conn.commit()


def mark_uploaded(
    conn: sqlite3.Connection,
    history_id: int,
    *,
    media_id: str = "",
    shortcode: str = "",
    permalink: str = "",
    verifiable: bool = True,
) -> None:
    values: Dict[str, Any] = {
        "status": "uploaded",
        "media_id": str(media_id or ""),
        "shortcode": str(shortcode or ""),
        "permalink": str(permalink or ""),
        "error": "",
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "last_checked_at": "",
        "verification_attempts": 0,
        "next_verify_at": utc_after(60) if verifiable and (media_id or shortcode) else "",
    }
    update_history(conn, history_id, **values)


def persist_reel_publish_intent(conn: sqlite3.Connection, history_id: int, job_id: int) -> Dict[str, Any]:
    """Atomically reserve the sole Reel Share action for one history slot.

    The committed history marker is the durable guard.  Updating the job's
    stage in the same transaction makes both existing startup reconcilers
    conservative even if the process dies before the browser click.
    """
    if not int(history_id or 0) or not int(job_id or 0):
        return {"ok": False, "status": "PUBLISH_INTENT_PERSIST_FAILED", "error": "missing publication identity"}
    try:
        ensure_history_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,COALESCE(publish_intent_at,'') AS publish_intent_at,job_id,publication_slot_id "
            "FROM ig_publishing_history WHERE id=?",
            (int(history_id),),
        ).fetchone()
        if not row or int(row["job_id"] or 0) != int(job_id):
            conn.rollback()
            return {"ok": False, "status": "PUBLISH_INTENT_PERSIST_FAILED", "error": "publication identity mismatch"}
        if str(row["status"] or "") in {"uploaded", "confirmed", "verified", "uploaded_unverified", "submitted_unverified"} or str(row["publish_intent_at"] or ""):
            conn.rollback()
            return {"ok": False, "status": "PUBLISH_ALREADY_ATTEMPTED", "error": "publication action already reserved"}
        slot_id = int(row["publication_slot_id"] or 0)
        if slot_id:
            slot = conn.execute(
                "SELECT status,COALESCE(share_clicked_at,'') AS share_clicked_at "
                "FROM ig_publication_slots WHERE id=?",
                (slot_id,),
            ).fetchone()
            if not slot or str(slot["share_clicked_at"] or "") or str(slot["status"] or "") in {
                "publishing", "processing", "uploaded_unverified", "verified", "completed"
            }:
                conn.rollback()
                return {"ok": False, "status": "PUBLISH_ALREADY_ATTEMPTED", "error": "publication slot already occupied"}
        cur = conn.execute(
            "UPDATE ig_publishing_history SET status='publish_intent',publish_intent_at=datetime('now'),updated_at=datetime('now') "
            "WHERE id=? AND status='processing' AND COALESCE(publish_intent_at,'')=''",
            (int(history_id),),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return {"ok": False, "status": "PUBLISH_ALREADY_ATTEMPTED", "error": "publication action already reserved"}
        if slot_id:
            conn.execute(
                "UPDATE ig_publication_slots SET status='intent',history_id=?,updated_at=datetime('now') WHERE id=?",
                (int(history_id), slot_id),
            )
        conn.execute(
            "UPDATE ig_web_upload_jobs SET current_step='reel_publish_intent',updated_at=datetime('now') WHERE id=?",
            (int(job_id),),
        )
        conn.commit()
        return {"ok": True, "status": "REEL_PUBLISH_INTENT"}
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "status": "PUBLISH_INTENT_PERSIST_FAILED", "error": "database_unavailable"}


def record_reel_share_click(
    conn: sqlite3.Connection,
    history_id: int,
    job_id: int,
    *,
    observation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist the one delivered Share click and its secret-free evidence."""
    if not int(history_id or 0) or not int(job_id or 0):
        return {"ok": False, "status": "SHARE_CLICK_PERSIST_FAILED", "error": "missing publication identity"}
    observation = dict(observation or {})
    try:
        ensure_history_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT job_id,COALESCE(publish_intent_at,'') AS publish_intent_at,"
            "COALESCE(share_clicked_at,'') AS share_clicked_at,asset_id,publication_slot_id "
            "FROM ig_publishing_history WHERE id=?",
            (int(history_id),),
        ).fetchone()
        if not row or int(row["job_id"] or 0) != int(job_id) or not str(row["publish_intent_at"] or ""):
            conn.rollback()
            return {"ok": False, "status": "SHARE_CLICK_PERSIST_FAILED", "error": "publish intent is missing"}
        if str(row["share_clicked_at"] or ""):
            conn.rollback()
            return {"ok": True, "status": "SHARE_ALREADY_RECORDED", "already_recorded": True}
        slot_id = int(row["publication_slot_id"] or 0)
        if slot_id:
            slot = conn.execute(
                "SELECT history_id,COALESCE(share_clicked_at,'') AS share_clicked_at,status "
                "FROM ig_publication_slots WHERE id=?",
                (slot_id,),
            ).fetchone()
            if not slot or (
                str(slot["share_clicked_at"] or "")
                and int(slot["history_id"] or 0) != int(history_id)
            ) or str(slot["status"] or "") in {"uploaded_unverified", "verified", "completed"}:
                conn.rollback()
                return {"ok": True, "status": "SHARE_ALREADY_RECORDED", "already_recorded": True}
        conn.execute(
            """
            UPDATE ig_publishing_history
            SET status='publishing',share_clicked_at=datetime('now'),
                publish_request_started_at=?,publish_request_finished_at=?,
                publish_request_state=?,publish_request_path=?,publish_http_status=?,
                updated_at=datetime('now')
            WHERE id=? AND COALESCE(share_clicked_at,'')=''
            """,
            (
                str(observation.get("request_started_at") or ""),
                str(observation.get("request_finished_at") or ""),
                str(observation.get("request_state") or ""),
                str(observation.get("safe_path") or ""),
                int(observation.get("http_status") or 0),
                int(history_id),
            ),
        )
        if slot_id:
            conn.execute(
                """
                UPDATE ig_publication_slots
                SET status='publishing',history_id=?,share_clicked_at=datetime('now'),updated_at=datetime('now')
                WHERE id=?
                """,
                (int(history_id), slot_id),
            )
        asset_id = int(row["asset_id"] or 0)
        if asset_id:
            asset_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(api_content_assets)")}
            asset = conn.execute(
                ("SELECT COALESCE(content_kind,'') AS content_kind FROM api_content_assets WHERE id=?"
                 if "content_kind" in asset_columns else
                 "SELECT '' AS content_kind FROM api_content_assets WHERE id=?"),
                (asset_id,),
            ).fetchone()
            # Scale is deliberately reusable; its global availability is not
            # the duplicate-safety boundary.
            if asset and str(asset["content_kind"] or "").lower() != "scale":
                conn.execute(
                    "UPDATE api_content_assets SET status='publishing',updated_at=datetime('now') WHERE id=?",
                    (asset_id,),
                )
        conn.execute(
            "UPDATE ig_web_upload_jobs SET status='sharing',current_step='share_clicked',updated_at=datetime('now') WHERE id=?",
            (int(job_id),),
        )
        conn.commit()
        return {"ok": True, "status": "SHARE_CLICK_RECORDED"}
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": False, "status": "SHARE_CLICK_PERSIST_FAILED", "error": "database_unavailable"}


def job_has_reel_publish_intent(conn: sqlite3.Connection, job_id: int) -> bool:
    """Return durable, per-slot intent evidence without interpreting UI text."""
    row = conn.execute(
        "SELECT 1 FROM ig_publishing_history WHERE job_id=? AND COALESCE(publish_intent_at,'')!='' LIMIT 1",
        (int(job_id),),
    ).fetchone()
    return bool(row)


def job_has_reel_share_click(conn: sqlite3.Connection, job_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ig_publishing_history WHERE job_id=? AND COALESCE(share_clicked_at,'')!='' LIMIT 1",
        (int(job_id),),
    ).fetchone()
    return bool(row)


def preserve_verified_publication_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    stop_reason: str = "user_stop_after_verified_publication",
) -> bool:
    """Keep verified publication evidence authoritative during Stop/restart.

    A job can still have pending Scale slots when the user stops it.  In that
    case the job is a partial success, not a failed or uploaded-unverified
    publication.  The history and slot rows remain untouched.
    """
    if not int(job_id or 0):
        return False
    ensure_history_schema(conn)
    verified = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ig_publishing_history
        WHERE job_id=? AND status IN ('confirmed','verified','uploaded')
        """,
        (int(job_id),),
    ).fetchone()
    if not verified or int(verified["count"] or 0) <= 0:
        return False
    job = conn.execute(
        """
        SELECT COALESCE(posted_count,0) AS posted_count,
               COALESCE(target_uploads,1) AS target_uploads
        FROM ig_web_upload_jobs WHERE id=?
        """,
        (int(job_id),),
    ).fetchone()
    if not job:
        return False
    posted = max(int(job["posted_count"] or 0), int(verified["count"] or 0))
    target = max(1, int(job["target_uploads"] or 1))
    status = "success" if posted >= target else "partial_success"
    step = (
        f"verified publication preserved; {posted}/{target}"
        if posted >= target
        else f"Scale {posted}/{target}; user stopped after verified publication"
    )
    conn.execute(
        """
        UPDATE ig_web_upload_jobs
        SET status=?,current_step=?,posted_count=?,last_error='',
            finished_at=CASE WHEN finished_at='' THEN datetime('now') ELSE finished_at END,
            updated_at=datetime('now')
        WHERE id=?
        """,
        (status, step, posted, int(job_id)),
    )
    return True


def finalize_publication_attempt(
    conn: sqlite3.Connection,
    *,
    history_id: int,
    job_id: int,
    outcome: str,
    error: str = "",
    asset_id: int = 0,
    account_name: str = "",
    posted_count: Optional[int] = None,
    job_status: str = "",
    job_step: str = "",
    plan_item_id: int = 0,
    observation: Optional[Dict[str, Any]] = None,
    proven_rejection: bool = False,
    on_write: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Commit one publication outcome and every local consequence together.

    The browser Share call deliberately happens before this function.  SQLite's
    ``BEGIN IMMEDIATE`` serializes competing worker/watchdog/verifier/recovery
    finalizers, while the history row is the per-attempt idempotency key.
    ``on_write`` is an intentionally tiny test-only fault-injection seam.
    """
    if outcome not in {"confirmed", "uploaded_unverified", "processing", "submitted_unverified", "failed"}:
        return {"committed": False, "result": "conflict", "error": "invalid outcome"}
    if not int(history_id or 0) or not int(job_id or 0):
        return {"committed": False, "result": "conflict", "error": "missing publication identity"}
    ensure_history_schema(conn)
    def checkpoint(name: str) -> None:
        if on_write:
            on_write(name)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id,job_id,status,COALESCE(publish_intent_at,'') AS publish_intent_at,"
            "COALESCE(share_clicked_at,'') AS share_clicked_at,asset_id,account_name,"
            "publication_slot_id,asset_usage_recorded "
            "FROM ig_publishing_history WHERE id=?", (int(history_id),)
        ).fetchone()
        if not row or int(row["job_id"] or 0) != int(job_id):
            conn.rollback()
            return {"committed": False, "result": "conflict", "error": "publication identity mismatch"}
        current = str(row["status"] or "")
        if current in {"confirmed", "verified"}:
            conn.rollback()
            return {"committed": False, "already_finalized": True, "result": "already_finalized"}
        # An intent is durable audit evidence; a stale non-ambiguous caller
        # cannot erase it or turn it into a retryable failure.
        if (
            current in {"submitted_unverified", "uploaded_unverified", "uploaded"}
            or (current == "processing" and str(row["share_clicked_at"] or ""))
        ) and outcome not in {"confirmed", "processing"} and not (
            current == "uploaded_unverified"
            and outcome == "uploaded_unverified"
            and not int(row["asset_usage_recorded"] or 0)
        ):
            conn.rollback()
            return {"committed": False, "already_finalized": True, "result": "already_finalized"}
        if outcome == "failed" and str(row["share_clicked_at"] or "") and not proven_rejection:
            outcome = "uploaded_unverified"
        elif outcome == "failed" and str(row["publish_intent_at"] or "") and not proven_rejection:
            outcome = "submitted_unverified"
        final_status = "verified" if outcome == "confirmed" else outcome
        observation = dict(observation or {})
        conn.execute(
            """
            UPDATE ig_publishing_history
            SET status=?,error=?,
                published_at=CASE WHEN ? IN ('verified','uploaded_unverified','processing') THEN
                    COALESCE(NULLIF(published_at,''),NULLIF(share_clicked_at,''),datetime('now')) ELSE published_at END,
                verified_at=CASE WHEN ?='verified' THEN datetime('now') ELSE verified_at END,
                media_id=COALESCE(NULLIF(?,''),media_id),
                shortcode=COALESCE(NULLIF(?,''),shortcode),
                permalink=COALESCE(NULLIF(?,''),permalink),
                publish_request_started_at=COALESCE(NULLIF(?,''),publish_request_started_at),
                publish_request_finished_at=COALESCE(NULLIF(?,''),publish_request_finished_at),
                publish_request_state=COALESCE(NULLIF(?,''),publish_request_state),
                publish_request_path=COALESCE(NULLIF(?,''),publish_request_path),
                publish_http_status=CASE WHEN ?>0 THEN ? ELSE publish_http_status END,
                next_verify_at=CASE WHEN ? IN ('uploaded_unverified','processing') THEN ? ELSE '' END,
                last_checked_at='',updated_at=datetime('now')
            WHERE id=?
            """,
            (
                final_status, "" if outcome == "confirmed" else str(error or "")[:4000],
                final_status, final_status,
                str(observation.get("media_id") or ""), str(observation.get("shortcode") or ""),
                str(observation.get("permalink") or ""),
                str(observation.get("request_started_at") or ""),
                str(observation.get("request_finished_at") or ""),
                str(observation.get("request_state") or ""),
                str(observation.get("safe_path") or ""),
                int(observation.get("http_status") or 0), int(observation.get("http_status") or 0),
                final_status, utc_after(60), int(history_id),
            ),
        )
        checkpoint("history")
        effective_asset = int(asset_id or row["asset_id"] or 0)
        effective_account = str(account_name or row["account_name"] or "")
        slot_id = int(row["publication_slot_id"] or 0)
        accepted_outcome = outcome in {"confirmed", "uploaded_unverified", "processing"}
        usage_added = False
        if accepted_outcome and effective_asset:
            columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(api_content_assets)")}
            if columns:
                content_kind = ""
                if "content_kind" in columns:
                    asset_row = conn.execute(
                        "SELECT COALESCE(content_kind,'') AS content_kind FROM api_content_assets WHERE id=?",
                        (effective_asset,),
                    ).fetchone()
                    content_kind = str(asset_row["content_kind"] or "").lower() if asset_row else ""
                parts = ["status='ready'" if content_kind == "scale" else "status='uploaded'"]
                if "uploaded_at" in columns:
                    parts.append("uploaded_at=datetime('now')")
                if "account_name" in columns and effective_account:
                    parts.append("account_name=?")
                    conn.execute(f"UPDATE api_content_assets SET {', '.join(parts)} WHERE id=?", (effective_account, effective_asset))
                else:
                    conn.execute(f"UPDATE api_content_assets SET {', '.join(parts)} WHERE id=?", (effective_asset,))
                if not int(row["asset_usage_recorded"] or 0):
                    if "publication_use_count" in columns:
                        conn.execute(
                            "UPDATE api_content_assets SET publication_use_count=publication_use_count+1,"
                            "updated_at=datetime('now') WHERE id=?",
                            (effective_asset,),
                        )
                    conn.execute(
                        "UPDATE ig_publishing_history SET asset_usage_recorded=1 WHERE id=?",
                        (int(history_id),),
                    )
                    usage_added = True
                checkpoint("asset")
        elif outcome == "failed" and proven_rejection and effective_asset:
            conn.execute(
                "UPDATE api_content_assets SET status='ready',last_error=?,updated_at=datetime('now') WHERE id=?",
                (str(error or "publish rejected")[:4000], effective_asset),
            )
            checkpoint("asset")
        if slot_id:
            if accepted_outcome:
                slot_status = "verified" if outcome == "confirmed" else final_status
                conn.execute(
                    """
                    UPDATE ig_publication_slots
                    SET status=?,history_id=?,completed_at=COALESCE(NULLIF(completed_at,''),datetime('now')),
                        usage_recorded=CASE WHEN ? THEN 1 ELSE usage_recorded END,
                        updated_at=datetime('now')
                    WHERE id=?
                    """,
                    (slot_status, int(history_id), 1 if usage_added else 0, slot_id),
                )
            elif outcome == "failed" and proven_rejection:
                conn.execute(
                    """
                    UPDATE ig_publication_slots
                    SET status='failed',history_id=0,share_clicked_at='',completed_at='',
                        updated_at=datetime('now')
                    WHERE id=?
                    """,
                    (slot_id,),
                )
            checkpoint("slot")
        effective_plan_item_id = int(plan_item_id or 0)
        if not effective_plan_item_id and slot_id:
            slot_plan = conn.execute(
                "SELECT plan_item_id FROM ig_publication_slots WHERE id=?", (slot_id,)
            ).fetchone()
            effective_plan_item_id = int(slot_plan["plan_item_id"] or 0) if slot_plan else 0
        if accepted_outcome and effective_plan_item_id:
            # This helper retains the existing set-boundary semantics; with
            # commit=False it participates in this transaction.
            from content_plans import complete_plan_item
            complete_plan_item(conn, effective_account, effective_plan_item_id, commit=False)
            checkpoint("plan")
        job_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(ig_web_upload_jobs)")}
        campaign_sql = (
            ",COALESCE(campaign_run_identity,'') AS campaign_run_identity"
            if "campaign_run_identity" in job_columns else ",'' AS campaign_run_identity"
        )
        job = conn.execute(
            "SELECT posted_count,status"
            + (",target_uploads" if "target_uploads" in job_columns else ",1 AS target_uploads")
            + campaign_sql
            + " FROM ig_web_upload_jobs WHERE id=?",
            (int(job_id),),
        ).fetchone()
        if not job:
            raise sqlite3.IntegrityError("publication job disappeared")
        count = int(posted_count) if posted_count is not None else int(job["posted_count"] or 0) + (1 if usage_added else 0)
        status = str(job_status or ("uploaded_unverified" if outcome in {"uploaded_unverified", "processing"} else "manual_required" if outcome == "submitted_unverified" else job["status"] or "running"))
        step = str(job_step or outcome)
        target = int(job["target_uploads"] or 1)
        campaign_run_identity = str(job["campaign_run_identity"] or "")
        if campaign_run_identity and slot_id:
            progress = slot_progress(conn, effective_account, campaign_run_identity)
            count = int(progress["completed"])
            target = int(progress["total"] or target)
            if status in {"success", "uploaded_unverified", "processing"} and count < target:
                status = "partial_success"
                step = f"Scale {count}/{target}; {target - count} slot(s) remaining"
        if "target_uploads" in job_columns:
            conn.execute(
                "UPDATE ig_web_upload_jobs SET posted_count=?,target_uploads=?,status=?,current_step=?,last_error=?,"
                "finished_at=CASE WHEN ? IN ('success','failed','manual_required','submitted_unverified','uploaded_unverified','processing','partial_success') THEN datetime('now') ELSE finished_at END,updated_at=datetime('now') WHERE id=?",
                (count, target, status, step, "" if outcome == "confirmed" else str(error or "")[:4000], status, int(job_id)),
            )
        else:
            conn.execute(
                "UPDATE ig_web_upload_jobs SET posted_count=?,status=?,current_step=?,last_error=?,"
                "finished_at=CASE WHEN ? IN ('success','failed','manual_required','submitted_unverified','uploaded_unverified','processing','partial_success') THEN datetime('now') ELSE finished_at END,updated_at=datetime('now') WHERE id=?",
                (count, status, step, "" if outcome == "confirmed" else str(error or "")[:4000], status, int(job_id)),
            )
        checkpoint("job")
        account_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(accounts)")}
        if effective_account and account_columns:
            parts, params = [], []
            if "web_upload_last_error" in account_columns:
                parts.append("web_upload_last_error=?"); params.append("" if outcome == "confirmed" else str(error or "")[:4000])
            if outcome in {"confirmed", "uploaded_unverified", "processing"} and "web_upload_last_upload_at" in account_columns:
                parts.append("web_upload_last_upload_at=datetime('now')")
            if parts:
                if "updated_at" in account_columns:
                    parts.append("updated_at=datetime('now')")
                params.append(effective_account)
                conn.execute(f"UPDATE accounts SET {', '.join(parts)} WHERE name=?", params)
                checkpoint("account")
        conn.commit()
        return {"committed": True, "result": "committed", "outcome": outcome, "posted_count": count}
    except Exception as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"committed": False, "result": "rolled_back", "error": f"{type(exc).__name__}: {exc}"}


def mark_failed(conn: sqlite3.Connection, history_id: int, error: str) -> None:
    ensure_history_schema(conn)
    row = conn.execute(
        "SELECT publication_slot_id,COALESCE(publish_intent_at,'') AS publish_intent_at,"
        "COALESCE(share_clicked_at,'') AS share_clicked_at "
        "FROM ig_publishing_history WHERE id=?",
        (int(history_id),),
    ).fetchone()
    # Once intent is committed, even a local exception cannot re-open a slot.
    # It is ambiguous external work, not a retry-safe failed upload.
    conn.execute(
        "UPDATE ig_publishing_history SET status=CASE WHEN COALESCE(share_clicked_at,'')!='' THEN 'uploaded_unverified' WHEN COALESCE(publish_intent_at,'')!='' THEN 'submitted_unverified' ELSE 'failed' END, "
        "error=?,next_verify_at='',last_checked_at='',updated_at=datetime('now') "
        "WHERE id=? AND status NOT IN ('uploaded','confirmed','verified')",
        (str(error or "upload failed")[:4000], int(history_id)),
    )
    if row and int(row["publication_slot_id"] or 0):
        slot_id = int(row["publication_slot_id"])
        if str(row["share_clicked_at"] or ""):
            conn.execute(
                "UPDATE ig_publication_slots SET status='uploaded_unverified',updated_at=datetime('now') "
                "WHERE id=?",
                (slot_id,),
            )
        elif str(row["publish_intent_at"] or ""):
            # Ambiguous intent remains occupied until a human or verifier
            # resolves it; automatic continuation must not replay it.
            conn.execute(
                "UPDATE ig_publication_slots SET status='intent',updated_at=datetime('now') WHERE id=?",
                (slot_id,),
            )
        else:
            conn.execute(
                "UPDATE ig_publication_slots SET status='pending',history_id=0,updated_at=datetime('now') "
                "WHERE id=?",
                (slot_id,),
            )
    conn.commit()


def reconcile_terminal_upload_history(
    conn: sqlite3.Connection,
    job_id: int,
    job_status: str,
    error: str,
) -> bool:
    """Close an interrupted upload row without inferring publication success.

    The caller owns the transaction so a watchdog or startup recovery can
    persist the job and its active history together.  The status predicate is
    intentional: confirmed rows (``uploaded``) are immutable here, and a
    second reconciliation has no effect.
    """
    if not int(job_id or 0) or str(job_status or "") not in {"failed", "stopped", "submitted_unverified", "uploaded_unverified"}:
        return False
    cur = conn.execute(
        """
        UPDATE ig_publishing_history
        SET status=CASE WHEN COALESCE(share_clicked_at,'')!='' OR ?='uploaded_unverified' THEN 'uploaded_unverified'
                        WHEN COALESCE(publish_intent_at,'')!='' OR ?='submitted_unverified' THEN 'submitted_unverified'
                        WHEN ?='stopped' THEN 'cancelled'
                        ELSE 'failed' END,
            error=?, next_verify_at='', last_checked_at='',
            updated_at=datetime('now')
        WHERE job_id=? AND status IN ('processing','publishing','publish_intent')
        """,
        (
            str(job_status),
            str(job_status),
            str(job_status),
            str(error or "upload worker stopped before terminal result")[:4000],
            int(job_id),
        ),
    )
    return bool(cur.rowcount)


def history_row(conn: sqlite3.Connection, history_id: int) -> Optional[Dict[str, Any]]:
    ensure_history_schema(conn)
    row = conn.execute("SELECT * FROM ig_publishing_history WHERE id=?", (int(history_id),)).fetchone()
    return dict(row) if row else None
