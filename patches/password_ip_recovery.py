"""Durable, account-scoped password/IP recovery state.

This module owns credential-attempt accounting only.  Proxy health and mobile
connection leases remain owned by the existing connection scheduler.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"

ACTIVE_STATES = {
    "INITIAL",
    "SUBMISSION_RESERVED",
    "FIRST_PASSWORD_REJECTED",
    "ROTATION_REQUESTED",
    "ROTATION_COMMAND_ACCEPTED",
    "ROTATION_COOLDOWN",
    "ROTATION_STABILIZING",
    "PROXY_READINESS_CONFIRMED",
    "EXIT_IP_CHANGED",
    "READY_FOR_SECOND_SUBMISSION",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS password_ip_recovery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL COLLATE NOCASE,
            workflow_id TEXT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '',
            workflow_task TEXT NOT NULL DEFAULT 'auto_login',
            status TEXT NOT NULL DEFAULT 'active',
            recovery_stage TEXT NOT NULL DEFAULT 'INITIAL',
            password_submission_count INTEGER NOT NULL DEFAULT 0,
            recovery_ip_change_count INTEGER NOT NULL DEFAULT 0,
            submission_reserved INTEGER NOT NULL DEFAULT 0,
            initial_connection_type TEXT NOT NULL DEFAULT '',
            initial_connection_id INTEGER NOT NULL DEFAULT 0,
            initial_static_proxy_id INTEGER NOT NULL DEFAULT 0,
            initial_exit_ip TEXT NOT NULL DEFAULT '',
            replacement_static_proxy_id INTEGER NOT NULL DEFAULT 0,
            replacement_exit_ip TEXT NOT NULL DEFAULT '',
            mobile_connection_id INTEGER NOT NULL DEFAULT 0,
            mobile_lease_id TEXT NOT NULL DEFAULT '',
            mobile_lease_owner TEXT NOT NULL DEFAULT '',
            initial_mobile_generation TEXT NOT NULL DEFAULT '',
            recovery_mobile_generation TEXT NOT NULL DEFAULT '',
            ip_checker TEXT NOT NULL DEFAULT '',
            rotation_requested_at TEXT NOT NULL DEFAULT '',
            rotation_accepted_at TEXT NOT NULL DEFAULT '',
            readiness_confirmed_at TEXT NOT NULL DEFAULT '',
            last_submission_at TEXT NOT NULL DEFAULT '',
            terminal_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(account_name, workflow_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_ip_recovery_active "
        "ON password_ip_recovery(account_name,status,updated_at)"
    )


def _row(conn: sqlite3.Connection, recovery_id: int) -> dict[str, Any]:
    value = conn.execute(
        "SELECT * FROM password_ip_recovery WHERE id=?", (int(recovery_id),)
    ).fetchone()
    return dict(value) if value else {}


def begin_or_resume(
    account_name: str,
    run_id: str,
    workflow_task: str,
    connection_type: str,
    connection_id: int,
    exit_ip: str = "",
    *,
    workflow_id: str = "",
    initial_generation: str = "",
    ip_checker: str = "",
) -> dict[str, Any]:
    """Resume only this accepted run's attempt, or create a fresh one.

    A workflow id is not sufficient authority to cross an API acceptance
    boundary.  Worker restarts inside one durable run may resume; a later run
    must receive fresh submission and IP-change budgets.
    """
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        if workflow_id:
            exact = conn.execute(
                """
                SELECT * FROM password_ip_recovery
                WHERE account_name=? AND workflow_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (str(account_name), str(workflow_id)),
            ).fetchone()
            if exact and str(exact["run_id"] or "") == str(run_id):
                if int(exact["submission_reserved"] or 0):
                    conn.execute(
                        """
                        UPDATE password_ip_recovery
                        SET submission_reserved=0,
                            recovery_stage=CASE
                              WHEN password_submission_count>=2
                              THEN 'TERMINAL' ELSE recovery_stage END,
                            status=CASE
                              WHEN password_submission_count>=2
                              THEN 'terminal' ELSE status END,
                            terminal_reason=CASE
                              WHEN password_submission_count>=2
                              THEN 'invalid_credentials_after_ip_retry'
                              ELSE terminal_reason END,
                            updated_at=datetime('now')
                        WHERE id=?
                        """,
                        (int(exact["id"]),),
                    )
                conn.commit()
                return _row(conn, int(exact["id"]))
            if exact:
                # The caller supplied a receipt owned by another accepted run.
                # Never reactivate or clone its counters.
                workflow_id = ""
        existing = conn.execute(
            """
            SELECT * FROM password_ip_recovery
            WHERE account_name=? AND run_id=? AND status='active'
            ORDER BY id DESC LIMIT 1
            """,
            (str(account_name), str(run_id)),
        ).fetchone()
        if existing:
            # A process may have stopped after reserving a physical submission.
            # Conservatively consume that boundary on restart: replaying it
            # could produce a forbidden third submission.
            if int(existing["submission_reserved"] or 0):
                conn.execute(
                    """
                    UPDATE password_ip_recovery
                    SET submission_reserved=0,
                        recovery_stage=CASE
                          WHEN password_submission_count>=2 THEN 'TERMINAL'
                          ELSE recovery_stage END,
                        status=CASE
                          WHEN password_submission_count>=2 THEN 'terminal'
                          ELSE status END,
                        terminal_reason=CASE
                          WHEN password_submission_count>=2
                          THEN 'invalid_credentials_after_ip_retry'
                          ELSE terminal_reason END,
                        updated_at=datetime('now')
                    WHERE id=?
                    """,
                    (int(existing["id"]),),
                )
            conn.commit()
            return _row(conn, int(existing["id"]))

        stopped = conn.execute(
            """
            SELECT * FROM password_ip_recovery
            WHERE account_name=? AND run_id=? AND status='stopped'
              AND (
                password_submission_count>0
                OR recovery_ip_change_count>0
              )
            ORDER BY id DESC LIMIT 1
            """,
            (str(account_name), str(run_id)),
        ).fetchone()
        if stopped:
            conn.execute(
                """
                UPDATE password_ip_recovery
                SET status='active',updated_at=datetime('now')
                WHERE id=?
                """,
                (int(stopped["id"]),),
            )
            conn.commit()
            return _row(conn, int(stopped["id"]))

        value = str(workflow_id or uuid.uuid4().hex)
        is_mobile = str(connection_type) in {"mobile", "phone"}
        cursor = conn.execute(
            """
            INSERT INTO password_ip_recovery(
                account_name,workflow_id,run_id,workflow_task,
                initial_connection_type,initial_connection_id,
                initial_static_proxy_id,initial_exit_ip,mobile_connection_id,
                initial_mobile_generation,ip_checker
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(account_name),
                value,
                str(run_id),
                str(workflow_task),
                str(connection_type),
                int(connection_id or 0),
                int(connection_id or 0) if str(connection_type) == "static" else 0,
                str(exit_ip or ""),
                int(connection_id or 0) if is_mobile else 0,
                str(initial_generation or ""),
                str(ip_checker or ""),
            ),
        )
        conn.commit()
        return _row(conn, int(cursor.lastrowid))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_active(
    account_name: str,
    *,
    run_id: str = "",
    workflow_id: str = "",
) -> dict[str, Any]:
    conn = _connect()
    try:
        ensure_schema(conn)
        filters = ["account_name=?", "status='active'"]
        params: list[Any] = [str(account_name)]
        if run_id:
            filters.append("run_id=?")
            params.append(str(run_id))
        if workflow_id:
            filters.append("workflow_id=?")
            params.append(str(workflow_id))
        value = conn.execute(
            "SELECT * FROM password_ip_recovery WHERE "
            + " AND ".join(filters)
            + " ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(value) if value else {}
    finally:
        conn.close()


def update_initial_context(
    account_name: str,
    workflow_id: str,
    *,
    exit_ip: str = "",
    initial_generation: str = "",
    ip_checker: str = "",
) -> None:
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute(
            """
            UPDATE password_ip_recovery
            SET initial_exit_ip=CASE WHEN initial_exit_ip='' THEN ? ELSE initial_exit_ip END,
                initial_mobile_generation=CASE
                  WHEN initial_mobile_generation='' THEN ?
                  ELSE initial_mobile_generation END,
                ip_checker=CASE WHEN ip_checker='' THEN ? ELSE ip_checker END,
                updated_at=datetime('now')
            WHERE account_name=? AND workflow_id=? AND status='active'
            """,
            (
                str(exit_ip or ""),
                str(initial_generation or ""),
                str(ip_checker or ""),
                str(account_name),
                str(workflow_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def reserve_submission(account_name: str, workflow_id: str) -> dict[str, Any]:
    """Reserve one physical submit boundary, never allowing a third."""
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM password_ip_recovery
            WHERE account_name=? AND workflow_id=? AND status='active'
            """,
            (str(account_name), str(workflow_id)),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"ok": False, "reason": "recovery_workflow_not_active"}
        count = int(row["password_submission_count"] or 0)
        stage = str(row["recovery_stage"] or "")
        if int(row["submission_reserved"] or 0):
            conn.rollback()
            return {"ok": False, "reason": "password_submission_already_reserved"}
        if count >= 2:
            conn.rollback()
            return {"ok": False, "reason": "password_submission_limit_reached"}
        if count == 1 and stage != "READY_FOR_SECOND_SUBMISSION":
            conn.rollback()
            return {"ok": False, "reason": "second_submission_not_ready"}
        conn.execute(
            """
            UPDATE password_ip_recovery
            SET submission_reserved=1,password_submission_count=password_submission_count+1,
                recovery_stage='SUBMISSION_RESERVED',
                last_submission_at=datetime('now'),updated_at=datetime('now')
            WHERE id=?
            """,
            (int(row["id"]),),
        )
        conn.commit()
        return {"ok": True, "submission": count + 1}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_submission(
    account_name: str,
    workflow_id: str,
    *,
    physically_dispatched: bool,
) -> None:
    """Commit or roll back a reservation when dispatch was proven false."""
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        if physically_dispatched:
            conn.execute(
                """
                UPDATE password_ip_recovery
                SET submission_reserved=0,recovery_stage='SUBMITTED',
                    updated_at=datetime('now')
                WHERE account_name=? AND workflow_id=? AND status='active'
                """,
                (str(account_name), str(workflow_id)),
            )
        else:
            conn.execute(
                """
                UPDATE password_ip_recovery
                SET submission_reserved=0,
                    password_submission_count=MAX(0,password_submission_count-1),
                    recovery_stage=CASE
                      WHEN password_submission_count<=1 THEN 'INITIAL'
                      ELSE 'READY_FOR_SECOND_SUBMISSION' END,
                    updated_at=datetime('now')
                WHERE account_name=? AND workflow_id=? AND status='active'
                  AND submission_reserved=1
                """,
                (str(account_name), str(workflow_id)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_first_rejection(account_name: str, workflow_id: str) -> dict[str, Any]:
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM password_ip_recovery WHERE account_name=? AND workflow_id=?",
            (str(account_name), str(workflow_id)),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"ok": False, "reason": "recovery_workflow_missing"}
        count = int(row["password_submission_count"] or 0)
        if (
            count == 1
            and str(row["recovery_stage"] or "") == "READY_FOR_SECOND_SUBMISSION"
        ):
            conn.execute(
                """
                UPDATE password_ip_recovery
                SET password_submission_count=2,
                    last_submission_at=datetime('now')
                WHERE id=?
                """,
                (int(row["id"]),),
            )
            count = 2
        if count >= 2:
            conn.execute(
                """
                UPDATE password_ip_recovery
                SET status='terminal',recovery_stage='TERMINAL',
                    terminal_reason='invalid_credentials_after_ip_retry',
                    submission_reserved=0,updated_at=datetime('now')
                WHERE id=?
                """,
                (int(row["id"]),),
            )
            conn.commit()
            return {"ok": False, "terminal": True, "reason": "invalid_credentials_after_ip_retry"}
        if count == 0:
            # An explicit contextual rejection is itself durable proof that
            # the worker crossed the physical submit boundary, even if it
            # exited before finalizing its local reservation.
            conn.execute(
                """
                UPDATE password_ip_recovery
                SET password_submission_count=1,
                    last_submission_at=datetime('now')
                WHERE id=?
                """,
                (int(row["id"]),),
            )
            count = 1
        if count != 1:
            conn.rollback()
            return {"ok": False, "reason": "password_rejection_without_submission"}
        conn.execute(
            """
            UPDATE password_ip_recovery
            SET recovery_stage='FIRST_PASSWORD_REJECTED',
                submission_reserved=0,updated_at=datetime('now')
            WHERE id=?
            """,
            (int(row["id"]),),
        )
        conn.commit()
        return {"ok": True, "terminal": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_rotation_requested(
    account_name: str,
    workflow_id: str,
    *,
    generation: str = "",
    lease_id: str = "",
    lease_owner: str = "",
) -> dict[str, Any]:
    """Consume the sole recovery IP-change budget before external mutation."""
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM password_ip_recovery
            WHERE account_name=? AND workflow_id=? AND status='active'
            """,
            (str(account_name), str(workflow_id)),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"ok": False, "reason": "recovery_workflow_not_active"}
        if int(row["recovery_ip_change_count"] or 0) >= 1:
            conn.rollback()
            return {"ok": False, "reason": "recovery_ip_change_limit_reached"}
        if str(row["recovery_stage"] or "") != "FIRST_PASSWORD_REJECTED":
            conn.rollback()
            return {"ok": False, "reason": "recovery_rotation_not_allowed"}
        conn.execute(
            """
            UPDATE password_ip_recovery
            SET recovery_ip_change_count=1,recovery_stage='ROTATION_REQUESTED',
                recovery_mobile_generation=?,mobile_lease_id=?,mobile_lease_owner=?,
                rotation_requested_at=datetime('now'),updated_at=datetime('now')
            WHERE id=?
            """,
            (str(generation), str(lease_id), str(lease_owner), int(row["id"])),
        )
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_stage(
    account_name: str,
    workflow_id: str,
    stage: str,
    *,
    replacement_connection_id: int = 0,
    replacement_exit_ip: str = "",
) -> None:
    allowed = {
        "ROTATION_COMMAND_ACCEPTED",
        "ROTATION_COOLDOWN",
        "ROTATION_STABILIZING",
        "PROXY_READINESS_CONFIRMED",
        "EXIT_IP_CHANGED",
        "READY_FOR_SECOND_SUBMISSION",
        "ROTATION_NOT_STABILIZED",
        "ROTATION_FAILED",
    }
    if stage not in allowed:
        raise ValueError(f"unsupported recovery stage: {stage}")
    conn = _connect()
    try:
        ensure_schema(conn)
        accepted = ",rotation_accepted_at=datetime('now')" if stage == "ROTATION_COMMAND_ACCEPTED" else ""
        readiness = ",readiness_confirmed_at=datetime('now')" if stage in {
            "PROXY_READINESS_CONFIRMED", "EXIT_IP_CHANGED", "READY_FOR_SECOND_SUBMISSION"
        } else ""
        conn.execute(
            f"""
            UPDATE password_ip_recovery
            SET recovery_stage=?,
                replacement_static_proxy_id=CASE WHEN ?>0 THEN ? ELSE replacement_static_proxy_id END,
                replacement_exit_ip=CASE WHEN ?!='' THEN ? ELSE replacement_exit_ip END,
                updated_at=datetime('now'){accepted}{readiness}
            WHERE account_name=? AND workflow_id=? AND status='active'
            """,
            (
                str(stage),
                int(replacement_connection_id or 0),
                int(replacement_connection_id or 0),
                str(replacement_exit_ip or ""),
                str(replacement_exit_ip or ""),
                str(account_name),
                str(workflow_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def mark_terminal(account_name: str, workflow_id: str, reason: str) -> None:
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute(
            """
            UPDATE password_ip_recovery
            SET status='terminal',recovery_stage='TERMINAL',
                submission_reserved=0,terminal_reason=?,updated_at=datetime('now')
            WHERE account_name=? AND workflow_id=?
            """,
            (str(reason), str(account_name), str(workflow_id)),
        )
        conn.commit()
    finally:
        conn.close()


def mark_success(account_name: str, workflow_id: str) -> None:
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute(
            """
            UPDATE password_ip_recovery
            SET status='succeeded',recovery_stage='SUCCESS',
                submission_reserved=0,terminal_reason='',updated_at=datetime('now')
            WHERE account_name=? AND workflow_id=?
            """,
            (str(account_name), str(workflow_id)),
        )
        conn.commit()
    finally:
        conn.close()


def mark_stopped(account_name: str, workflow_id: str) -> None:
    """User Stop is audit-only and never changes credential counters."""
    conn = _connect()
    try:
        ensure_schema(conn)
        conn.execute(
            """
            UPDATE password_ip_recovery
            SET status='stopped',
                submission_reserved=0,terminal_reason='',updated_at=datetime('now')
            WHERE account_name=? AND workflow_id=? AND status='active'
            """,
            (str(account_name), str(workflow_id)),
        )
        conn.commit()
    finally:
        conn.close()
