"""Durable, privacy-safe receipts for accepted top-level tasks.

The receipt is the authority for one API acceptance boundary.  It deliberately
stores only run-local opaque account references; account names remain in the
existing account/job tables where the product already requires them.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"

TERMINAL_DOMAIN_OUTCOMES = {
    "cancelled",
    "suspended",
    "challenge_required",
    "login_failed",
    "logged_in",
    "missing_persisted_workflow_outcome",
    "password_submission_blocked",
    "rotation_request_failed",
    "rotation_endpoint_timeout",
    "rotation_endpoint_connection_failure",
    "rotation_endpoint_auth_failure",
    "rotation_endpoint_rate_limited",
    "rotation_endpoint_busy",
    "proxy_auth_failed",
    "proxy_connection_failed",
    "proxy_connection_timeout",
    "proxy_unreachable",
    "proxy_readiness_timeout",
    "proxy_gate_internal_error",
    "static_proxy_pool_exhausted",
    "rotation_stale_ip_after_retry",
    "rotation_accepted_but_not_ready",
}
TERMINAL_INFRASTRUCTURE_OUTCOMES = {
    "insufficient_disk_space",
    "scheduler_rejected",
    "connection_rotation_failed_before_browser_launch",
    "browser_start_failed",
    "browser_load_failed_after_retry",
    "worker_exit_nonzero",
    "worker_not_started",
    "cancelled",
}
PRELAUNCH_INFRASTRUCTURE_OUTCOMES = {
    "insufficient_disk_space",
    "scheduler_rejected",
    "connection_rotation_failed_before_browser_launch",
    "browser_start_failed",
    "worker_not_started",
}
STRONG_DOMAIN_OUTCOMES = {
    "logged_in",
    "suspended",
    "challenge_required",
    "login_failed",
    "password_submission_blocked",
}


def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex


def current_run_id() -> str:
    return str(os.environ.get("SPARKGRID_RUN_ID") or "").strip()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_run_receipts (
            run_id TEXT PRIMARY KEY,
            request_state TEXT NOT NULL DEFAULT 'pending',
            task_category TEXT NOT NULL DEFAULT '',
            scheduler_state TEXT NOT NULL DEFAULT 'pending',
            selected_account_refs TEXT NOT NULL DEFAULT '[]',
            parent_process_state TEXT NOT NULL DEFAULT 'not_started',
            parent_pid INTEGER NOT NULL DEFAULT 0,
            child_process_state TEXT NOT NULL DEFAULT 'not_started',
            child_pid INTEGER NOT NULL DEFAULT 0,
            connection_state TEXT NOT NULL DEFAULT 'not_started',
            normalized_exit_category TEXT NOT NULL DEFAULT '',
            domain_outcome TEXT NOT NULL DEFAULT '',
            infrastructure_outcome TEXT NOT NULL DEFAULT '',
            cancellation_owner TEXT NOT NULL DEFAULT '',
            rejection_owner TEXT NOT NULL DEFAULT '',
            closure_owner TEXT NOT NULL DEFAULT '',
            closure_reason TEXT NOT NULL DEFAULT '',
            diagnostics_dir TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_run_receipts_finished "
        "ON task_run_receipts(finished_at,created_at)"
    )


def opaque_account_ref(run_id: str, account_name: str) -> str:
    digest = hashlib.sha256(
        (str(run_id) + "\0" + str(account_name).strip().lower()).encode("utf-8")
    ).hexdigest()[:16]
    return "account-" + digest


def create_receipt(
    run_id: str,
    task_category: str,
    account_names: Iterable[str],
    *,
    diagnostics_dir: str = "",
) -> dict[str, Any]:
    refs = [
        opaque_account_ref(run_id, name)
        for name in dict.fromkeys(str(value) for value in account_names if str(value))
    ]
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO task_run_receipts(
                run_id,task_category,selected_account_refs,diagnostics_dir
            ) VALUES(?,?,?,?)
            """,
            (
                str(run_id),
                str(task_category or "task")[:120],
                json.dumps(refs, separators=(",", ":")),
                str(diagnostics_dir or "")[:500],
            ),
        )
        conn.commit()
        return get_receipt(run_id, conn=conn)
    finally:
        conn.close()


_FIELDS = {
    "request_state",
    "task_category",
    "scheduler_state",
    "parent_process_state",
    "parent_pid",
    "child_process_state",
    "child_pid",
    "connection_state",
    "normalized_exit_category",
    "domain_outcome",
    "infrastructure_outcome",
    "cancellation_owner",
    "rejection_owner",
    "closure_owner",
    "closure_reason",
    "diagnostics_dir",
    "started_at",
    "finished_at",
}


def update_receipt(run_id: str, **values: Any) -> dict[str, Any]:
    updates = {key: value for key, value in values.items() if key in _FIELDS}
    if not updates:
        return get_receipt(run_id)
    conn = _connect()
    try:
        ensure_schema(conn)
        assignments = [f"{key}=?" for key in updates]
        params = [
            int(value or 0) if key in {"parent_pid", "child_pid"} else str(value or "")
            for key, value in updates.items()
        ]
        conn.execute(
            "UPDATE task_run_receipts SET "
            + ",".join(assignments)
            + ",updated_at=datetime('now') WHERE run_id=?",
            [*params, str(run_id)],
        )
        conn.commit()
        return get_receipt(run_id, conn=conn)
    finally:
        conn.close()


def get_receipt(
    run_id: str, *, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    owned = conn is None
    db = conn or _connect()
    try:
        ensure_schema(db)
        row = db.execute(
            "SELECT * FROM task_run_receipts WHERE run_id=?", (str(run_id),)
        ).fetchone()
        value = dict(row) if row else {}
        if value:
            try:
                value["selected_account_refs"] = json.loads(
                    value.get("selected_account_refs") or "[]"
                )
            except (TypeError, ValueError):
                value["selected_account_refs"] = []
        return value
    finally:
        if owned:
            db.close()


def reject_receipt(run_id: str, reason: str) -> dict[str, Any]:
    return update_receipt(
        run_id,
        request_state="rejected",
        scheduler_state="rejected",
        parent_process_state="not_started",
        infrastructure_outcome="scheduler_rejected",
        rejection_owner="process_manager",
        closure_owner="process_manager",
        closure_reason=str(reason or "scheduler_rejected")[:120],
        finished_at=_sqlite_now(),
    )


def mark_accepted(run_id: str, parent_pid: int) -> dict[str, Any]:
    return update_receipt(
        run_id,
        request_state="accepted",
        scheduler_state="starting",
        parent_process_state="running",
        parent_pid=int(parent_pid or 0),
        started_at=_sqlite_now(),
    )


def mark_child(run_id: str, state: str, pid: int = 0) -> dict[str, Any]:
    return update_receipt(
        run_id,
        child_process_state=str(state or "unknown")[:80],
        child_pid=int(pid or 0),
    )


def record_outcome(
    run_id: str,
    *,
    domain_outcome: str = "",
    infrastructure_outcome: str = "",
    connection_state: str = "",
    scheduler_state: str = "",
    closure_owner: str = "",
    closure_reason: str = "",
) -> dict[str, Any]:
    current = get_receipt(run_id)
    if not current:
        return {}
    values: dict[str, Any] = {}
    existing_domain = str(current.get("domain_outcome") or "")
    requested_domain = str(domain_outcome or "")
    if requested_domain and (
        not existing_domain
        or existing_domain == "missing_persisted_workflow_outcome"
        or requested_domain in STRONG_DOMAIN_OUTCOMES
    ):
        values["domain_outcome"] = requested_domain
    for key, value in (
        ("infrastructure_outcome", infrastructure_outcome),
        ("connection_state", connection_state),
        ("scheduler_state", scheduler_state),
        ("closure_owner", closure_owner),
        ("closure_reason", closure_reason),
    ):
        if value:
            values[key] = str(value)
    return update_receipt(run_id, **values) if values else current


def _job_domain_outcome(conn: sqlite3.Connection, run_id: str) -> str:
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(status,''),COALESCE(current_step,''),
                   COALESCE(domain_outcome,'')
            FROM ig_web_upload_jobs WHERE run_id=? ORDER BY id DESC
            """,
            (str(run_id),),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    for row in rows:
        status, step, domain = (str(row[index] or "").lower() for index in range(3))
        if domain == "password_submission_blocked":
            return domain
        text = " ".join((status, step, domain))
        if "logged_in" in text or (status == "success" and step == "logged_in"):
            return "logged_in"
        if "suspend" in text:
            return "suspended"
        if "challenge" in text or "two_factor" in text:
            return "challenge_required"
        if any(token in text for token in ("incorrect_credentials", "login_failed")):
            return "login_failed"
        if "cancel" in text or status in {"stopped", "cancelled"}:
            return "cancelled"
        # Non-Auto Login workers already persist their domain result in this
        # shared column. Preserve it verbatim instead of applying Auto Login
        # state inference or synthesizing a missing outcome at process exit.
        if domain and domain not in {"queued", "running", "pending"}:
            return domain
        if status in {"success", "failed", "manual_required"}:
            return status
    return ""


def _job_infrastructure_outcome(
    conn: sqlite3.Connection, run_id: str
) -> str:
    try:
        row = conn.execute(
            """
            SELECT COALESCE(infrastructure_outcome,'')
            FROM ig_web_upload_jobs
            WHERE run_id=? AND COALESCE(infrastructure_outcome,'')<>''
            ORDER BY id DESC LIMIT 1
            """,
            (str(run_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row[0] or "") if row else ""


def finalize_process_exit(
    run_id: str,
    returncode: int | None,
    *,
    cancelled: bool = False,
    closure_owner: str = "process_manager",
    closure_reason: str = "",
) -> dict[str, Any]:
    conn = _connect()
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM task_run_receipts WHERE run_id=?", (str(run_id),)
        ).fetchone()
        if not row:
            return {}
        receipt = dict(row)
        existing_domain = str(receipt.get("domain_outcome") or "")
        persisted_domain = _job_domain_outcome(conn, run_id)
        persisted_infrastructure = _job_infrastructure_outcome(conn, run_id)
        domain = existing_domain
        if persisted_domain and (
            not domain
            or domain == "missing_persisted_workflow_outcome"
            or persisted_domain in STRONG_DOMAIN_OUTCOMES
        ):
            domain = persisted_domain
        if cancelled:
            domain = domain or "cancelled"
            infrastructure = "cancelled"
            exit_category = "cancelled"
            cancellation_owner = closure_owner or "user_stop"
        else:
            code = int(returncode or 0)
            infrastructure = (
                str(receipt.get("infrastructure_outcome") or "")
                or persisted_infrastructure
            )
            infrastructure_tokens = {
                token for token in infrastructure.split(";") if token
            }
            child_state = str(receipt.get("child_process_state") or "not_started")
            scheduler_state = str(receipt.get("scheduler_state") or "pending")
            explicit_worker_exit = bool(
                infrastructure_tokens
                & {"worker_exit_0", "worker_exit_nonzero"}
            )
            worker_not_started = bool(
                child_state == "not_started"
                and not explicit_worker_exit
                and (
                    bool(
                        infrastructure_tokens
                        & PRELAUNCH_INFRASTRUCTURE_OUTCOMES
                    )
                    or scheduler_state in {
                        "running",
                        "prelaunch_failed",
                        "preflight_rejected",
                        "exiting",
                    }
                )
            )
            if worker_not_started:
                infrastructure = infrastructure or "worker_not_started"
                exit_category = "worker_not_started"
            elif "worker_exit_nonzero" in infrastructure_tokens:
                exit_category = "worker_exit_nonzero"
            elif "worker_exit_0" in infrastructure_tokens:
                exit_category = "worker_exit_0"
            else:
                infrastructure = (
                    infrastructure
                    or ("worker_exit_0" if code == 0 else "worker_exit_nonzero")
                )
                exit_category = (
                    "worker_exit_0" if code == 0 else "worker_exit_nonzero"
                )
            cancellation_owner = str(receipt.get("cancellation_owner") or "")
            if not domain and infrastructure not in TERMINAL_INFRASTRUCTURE_OUTCOMES:
                domain = "missing_persisted_workflow_outcome"
        conn.execute(
            """
            UPDATE task_run_receipts SET
                scheduler_state='finished',
                parent_process_state=?,
                child_process_state=CASE
                  WHEN child_process_state='running' THEN 'exited'
                  ELSE child_process_state END,
                normalized_exit_category=?,
                domain_outcome=?,
                infrastructure_outcome=?,
                cancellation_owner=?,
                closure_owner=?,
                closure_reason=?,
                finished_at=datetime('now'),
                updated_at=datetime('now')
            WHERE run_id=?
            """,
            (
                "cancelled" if cancelled else "exited",
                exit_category,
                domain,
                infrastructure,
                cancellation_owner,
                str(
                    closure_owner
                    if cancelled
                    else receipt.get("closure_owner") or closure_owner
                    or "process_manager"
                )[:80],
                str(
                    (
                        closure_reason
                        if cancelled
                        else receipt.get("closure_reason") or closure_reason
                    )
                    or ("cancelled" if cancelled else "process_exit_observed")
                )[:120],
                str(run_id),
            ),
        )
        conn.commit()
        return get_receipt(run_id, conn=conn)
    finally:
        conn.close()


def recent_receipts(limit: int = 100) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM task_run_receipts ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            try:
                value["selected_account_refs"] = json.loads(
                    value.get("selected_account_refs") or "[]"
                )
            except (TypeError, ValueError):
                value["selected_account_refs"] = []
            result.append(value)
        return result
    finally:
        conn.close()


def _sqlite_now() -> str:
    conn = sqlite3.connect(":memory:")
    try:
        return str(conn.execute("SELECT datetime('now')").fetchone()[0])
    finally:
        conn.close()
