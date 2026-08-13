#!/usr/bin/env python3
"""Standalone local server for SparkGrid Instagram Web Upload.

The standalone workspace supports three explicit Web work modes:
Manual account access, Clean Web browser publishing, and optimized API publishing.
It intentionally contains no phone automation, dashboard, mobile API/instagrapi,
landing-page builder, uniqueizer, tenant/agent manager, or unrelated SparkGrid UI.
"""
from __future__ import annotations

import json
import mimetypes
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from license_client import APP_VERSION
from platform_runtime import process_group_kwargs, stop_process_tree, create_process_job, terminate_process_job, close_process_job

from publishing_history import (
    create_history,
    ensure_history_schema,
    history_row,
    job_has_reel_publish_intent,
    job_has_reel_share_click,
    preserve_verified_publication_job,
    reconcile_terminal_upload_history,
    update_history,
)
from publication_slots import latest_scale_progress
from disk_safety import DEFAULT_RESERVE_BYTES, preflight, retention, system_status
from lifecycle_recovery import irreversible_stage
from password_ip_recovery import (
    ensure_schema as ensure_password_recovery_schema,
    get_active as get_active_password_recovery,
    mark_stopped as mark_password_recovery_stopped,
)
from task_receipts import (
    create_receipt,
    ensure_schema as ensure_task_receipt_schema,
    finalize_process_exit,
    mark_accepted as mark_task_accepted,
    new_run_id as new_task_run_id,
    recent_receipts,
    record_outcome as record_task_outcome,
    reject_receipt,
    opaque_account_ref,
)
from run_diagnostics import (
    account_worker_lifecycle,
    append_event as append_run_event,
    build_run_archive,
    cleanup_diagnostics as cleanup_run_diagnostics,
    ensure_run as ensure_run_diagnostics,
    finalize_run as finalize_run_diagnostics,
    normalize_task_category,
)
from connections import (
    ensure_connection_schema, list_connections, get_connection, upsert_connection,
    assign_connection, remove_connection, rotate_connection, import_static_connections,
    available_static_connections, connection_payload, direct_connection_id,
    list_proxy_groups, assign_static_group, restore_quarantined_connection,
    delete_proxy_group, create_proxy_group, rename_proxy_group,
    add_proxies_to_group, delete_proxy_from_group,
)
from content_plans import (
    ensure_plan_schema, get_plan, save_plan, reset_plan_position, plan_summaries, save_scale_settings,
    preview_scale_pattern, apply_scale_pattern, scale_library,
)
from automation_plans import (
    ensure_automation_schema, list_automation_plans, save_automation_plan,
    delete_automation_plan, materialize_enabled_slots, due_slot, mark_slot, slot_history,
)
from view_analytics import (
    ensure_view_schema, settings as view_settings, save_settings as save_view_settings,
    analytics_overview, retry_public_targets, session_accounts_for_targets, due_targets,
    register_parser_accounts, parser_accounts as list_parser_accounts,
    set_parser_accounts_enabled, remove_parser_account, mark_parser_logging_in,
)

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError as exc:
    raise SystemExit(
        ("Missing dependencies. Run install_windows.bat on Windows or ./install.command on macOS/Linux.\n" + str(exc))
    )

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"
UI_PATH = ROOT / "ui" / "index.html"
CONTENT_DIR = DATA_DIR / "content"
DEBUG_UPLOAD = DATA_DIR / "debug" / "ig_web_upload"
DEBUG_WARMUP = DATA_DIR / "debug" / "web_warmup"
WARMUP_JOBS = DATA_DIR / "jobs" / "web_warmup"
PROFILE_ROOT = DATA_DIR / "browser_profiles" / "ig_web_upload"
STORY_DIR = DATA_DIR / "story_uploads"
LOG_DIR = DATA_DIR / "logs"

for directory in (DATA_DIR, CONTENT_DIR, DEBUG_UPLOAD, DEBUG_WARMUP, WARMUP_JOBS, PROFILE_ROOT, STORY_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# Startup maintenance is deliberately limited to diagnostics roots.
for _diagnostic_root in (DEBUG_UPLOAD, DEBUG_WARMUP, DATA_DIR / "ai_content_data" / "debug" / "ig_web_upload", DATA_DIR / "browser_warmup_data" / "debug" / "web_warmup"):
    retention(_diagnostic_root)

os.environ.setdefault("SPARKGRID_DATA_DIR", str(DATA_DIR))
os.environ["PYTHONPATH"] = str(ROOT) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")

# Redirect server stdout/stderr to logs/server/server.log
try:
    from log_config import redirect_server_stdout
    redirect_server_stdout()
except Exception:
    pass


def profile_dir_for(account_name: str) -> Path:
    """Return the active profile directory for the current operating system."""
    try:
        from browser_launcher import active_profile_dir
        return Path(active_profile_dir(account_name, "", "desktop", create=False))
    except Exception:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(account_name or "").strip().lstrip("@"))[:90] or "account"
        return PROFILE_ROOT / safe / "desktop" / "profiles" / "default"


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def ensure_schema() -> None:
    conn = db_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                name TEXT PRIMARY KEY,
                password TEXT NOT NULL DEFAULT '',
                api_password TEXT NOT NULL DEFAULT '',
                api_totp_secret TEXT NOT NULL DEFAULT '',
                proxy TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                warm_only INTEGER NOT NULL DEFAULT 0,
                package TEXT NOT NULL DEFAULT '',
                device_serial TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ready',
                web_upload_enabled INTEGER NOT NULL DEFAULT 1,
                web_upload_mode TEXT NOT NULL DEFAULT 'desktop',
                web_upload_profile_status TEXT NOT NULL DEFAULT '',
                web_upload_login_status TEXT NOT NULL DEFAULT '',
                web_upload_cookie_status TEXT NOT NULL DEFAULT '',
                web_upload_last_error TEXT NOT NULL DEFAULT '',
                web_upload_last_upload_at TEXT NOT NULL DEFAULT '',
                web_upload_cooldown_until TEXT NOT NULL DEFAULT '',
                web_upload_content_mode TEXT NOT NULL DEFAULT 'scale',
                web_upload_quality_niche TEXT NOT NULL DEFAULT '',
                web_upload_scale_niche TEXT NOT NULL DEFAULT '',
                web_upload_cycle_count INTEGER NOT NULL DEFAULT 0,
                web_upload_next_cycle_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        required = {
            "password": "TEXT NOT NULL DEFAULT ''",
            "api_password": "TEXT NOT NULL DEFAULT ''",
            "api_totp_secret": "TEXT NOT NULL DEFAULT ''",
            "proxy": "TEXT NOT NULL DEFAULT ''",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "warm_only": "INTEGER NOT NULL DEFAULT 0",
            "package": "TEXT NOT NULL DEFAULT ''",
            "device_serial": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'ready'",
            "web_upload_enabled": "INTEGER NOT NULL DEFAULT 1",
            "web_upload_mode": "TEXT NOT NULL DEFAULT 'desktop'",
            "web_upload_profile_status": "TEXT NOT NULL DEFAULT ''",
            "web_upload_login_status": "TEXT NOT NULL DEFAULT ''",
            "web_upload_cookie_status": "TEXT NOT NULL DEFAULT ''",
            "web_upload_last_error": "TEXT NOT NULL DEFAULT ''",
            "web_upload_last_upload_at": "TEXT NOT NULL DEFAULT ''",
            "web_upload_cooldown_until": "TEXT NOT NULL DEFAULT ''",
            "web_upload_content_mode": "TEXT NOT NULL DEFAULT 'scale'",
            "web_upload_quality_niche": "TEXT NOT NULL DEFAULT ''",
            "web_upload_scale_niche": "TEXT NOT NULL DEFAULT ''",
            "web_upload_cycle_count": "INTEGER NOT NULL DEFAULT 0",
            "web_upload_next_cycle_at": "TEXT NOT NULL DEFAULT ''",
            "web_privacy_status": "TEXT NOT NULL DEFAULT 'unchecked'",
            "web_privacy_checked_at": "TEXT NOT NULL DEFAULT ''",
            "web_privacy_last_error": "TEXT NOT NULL DEFAULT ''",
            "web_professional_status": "TEXT NOT NULL DEFAULT 'unchecked'",
            "web_professional_checked_at": "TEXT NOT NULL DEFAULT ''",
            "web_professional_category": "TEXT NOT NULL DEFAULT ''",
            "web_professional_last_error": "TEXT NOT NULL DEFAULT ''",
            "web_upload_traffic_total": "INTEGER NOT NULL DEFAULT 0",
            "web_upload_traffic_last": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        existing = columns(conn, "accounts")
        for name, ddl in required.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {ddl}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ig_web_upload_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'desktop',
                provider TEXT NOT NULL DEFAULT 'camoufox',
                status TEXT NOT NULL DEFAULT 'queued',
                current_step TEXT NOT NULL DEFAULT '',
                posted_count INTEGER NOT NULL DEFAULT 0,
                target_uploads INTEGER NOT NULL DEFAULT 1,
                last_error TEXT NOT NULL DEFAULT '',
                debug_dir TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        job_cols = columns(conn, "ig_web_upload_jobs")
        for name, ddl in {
            "upload_engine": "TEXT NOT NULL DEFAULT 'clean_web'",
            "post_id": "TEXT NOT NULL DEFAULT ''",
            "permalink": "TEXT NOT NULL DEFAULT ''",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "campaign_run_identity": "TEXT NOT NULL DEFAULT ''",
            "domain_outcome": "TEXT NOT NULL DEFAULT ''",
            "infrastructure_outcome": "TEXT NOT NULL DEFAULT ''",
            "closure_owner": "TEXT NOT NULL DEFAULT ''",
            "closure_reason": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in job_cols:
                conn.execute(f"ALTER TABLE ig_web_upload_jobs ADD COLUMN {name} {ddl}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ig_web_upload_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO ig_web_upload_settings(key,value) VALUES ('upload_engine','clean_web')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO ig_web_upload_settings(key,value) VALUES ('api_parallel','3')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO ig_web_upload_settings(key,value) VALUES ('browser_parallel','3')"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quality_niches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO quality_niches(name)
            SELECT DISTINCT TRIM(COALESCE(web_upload_quality_niche,''))
            FROM accounts
            WHERE TRIM(COALESCE(web_upload_quality_niche,''))!=''
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scale_niches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO scale_niches(name)
            SELECT DISTINCT TRIM(COALESCE(web_upload_scale_niche,''))
            FROM accounts
            WHERE TRIM(COALESCE(web_upload_scale_niche,''))!=''
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_content_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL,
                original_name TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ready',
                content_kind TEXT NOT NULL DEFAULT 'scale',
                uploaded_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        content_cols = columns(conn, "api_content_assets")
        for name, ddl in {
            "account_name": "TEXT NOT NULL DEFAULT ''",
            "file_path": "TEXT NOT NULL DEFAULT ''",
            "original_name": "TEXT NOT NULL DEFAULT ''",
            "caption": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'ready'",
            "content_kind": "TEXT NOT NULL DEFAULT 'scale'",
            "uploaded_at": "TEXT NOT NULL DEFAULT ''",
            "last_error": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "quality_position": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in content_cols:
                conn.execute(f"ALTER TABLE api_content_assets ADD COLUMN {name} {ddl}")
        # Absolute content paths from another computer/OS are rebased to the
        # local content directory when the copied file is present here.
        try:
            rows = conn.execute("SELECT id,file_path FROM api_content_assets").fetchall()
            for asset_row in rows:
                old_path = Path(str(asset_row["file_path"] or ""))
                if old_path.is_file():
                    continue
                local_candidate = CONTENT_DIR / old_path.name
                if local_candidate.is_file():
                    conn.execute("UPDATE api_content_assets SET file_path=?,updated_at=datetime('now') WHERE id=?", (str(local_candidate), int(asset_row["id"])))
        except Exception:
            pass

        ensure_history_schema(conn)
        ensure_plan_schema(conn)
        ensure_connection_schema(conn)
        ensure_password_recovery_schema(conn)
        ensure_task_receipt_schema(conn)
        ensure_automation_schema(conn)
        ensure_view_schema(conn)
        conn.commit()
    finally:
        conn.close()


def ensure_task_detail_rows(
    run_id: str,
    account_names: list[str],
    receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    """Finalize selected accounts without copying one worker's outcome."""
    names = list(
        dict.fromkeys(
            str(name).strip().lstrip("@")
            for name in account_names
            if str(name).strip()
        )
    )
    if not names:
        return []
    run_ref = str(run_id or "")
    closure_reason = str(receipt.get("closure_reason") or "")
    cancelled = bool(
        str(receipt.get("domain_outcome") or "") == "cancelled"
        or str(receipt.get("infrastructure_outcome") or "") == "cancelled"
        or closure_reason in {"stop_all", "targeted_stop", "user_stop"}
    )
    outcomes: list[dict[str, Any]] = []
    scheduler_lifecycle = account_worker_lifecycle(run_ref)
    conn = db_conn()
    try:
        for name in names:
            try:
                existing = conn.execute(
                    """
                    SELECT id,status,current_step,domain_outcome,
                           infrastructure_outcome,closure_owner,closure_reason
                    FROM ig_web_upload_jobs
                    WHERE run_id=? AND account_name=? ORDER BY id DESC LIMIT 1
                    """,
                    (run_ref, name),
                ).fetchone()
            except sqlite3.OperationalError:
                return outcomes
            if existing:
                infrastructure = str(existing[4] or "")
                worker_started = "worker_not_started" not in infrastructure.split(";")
                outcome = {
                    "run_id": run_ref,
                    "account_ref": opaque_account_ref(run_ref, name),
                    "worker_started": worker_started,
                    "real_job_id": int(existing[0]) if worker_started else None,
                    "domain_outcome": str(existing[3] or existing[1] or ""),
                    "infrastructure_outcome": infrastructure,
                    "closure_reason": str(existing[6] or ""),
                    "source": "worker" if worker_started else "scheduler",
                }
            elif scheduler_lifecycle.get(opaque_account_ref(run_ref, name), {}).get(
                "worker_started"
            ):
                lifecycle = scheduler_lifecycle[opaque_account_ref(run_ref, name)]
                worker_exited = bool(lifecycle.get("worker_exited"))
                return_code = lifecycle.get("return_code")
                if cancelled:
                    status = "stopped"
                    step = "stopped"
                    domain = "cancelled"
                    infrastructure = "cancelled"
                    owner = str(receipt.get("closure_owner") or "user_stop")
                    reason = closure_reason or "user_stop"
                elif worker_exited and int(return_code or 0) == 0:
                    status = "success"
                    step = "completed"
                    domain = "success"
                    infrastructure = "worker_exit_0"
                    owner = "connection_scheduler"
                    reason = "normal_exit"
                else:
                    status = "failed"
                    step = "failed"
                    domain = "failed"
                    infrastructure = "worker_exit_nonzero"
                    owner = "connection_scheduler"
                    reason = "process_exit_nonzero"
                conn.execute(
                    """
                    INSERT INTO ig_web_upload_jobs(
                        run_id,account_name,status,current_step,last_error,
                        target_uploads,domain_outcome,infrastructure_outcome,
                        closure_owner,closure_reason,started_at,finished_at,updated_at
                    ) VALUES(?,?,?,?,?,0,?,?,?,?,?,?,datetime('now'))
                    """,
                    (
                        run_ref, name, status, step,
                        "" if status == "success" else step,
                        domain, infrastructure, owner, reason,
                        str(receipt.get("started_at") or ""),
                        str(receipt.get("finished_at") or ""),
                    ),
                )
                outcome = {
                    "run_id": run_ref,
                    "account_ref": opaque_account_ref(run_ref, name),
                    "worker_started": True,
                    "real_job_id": None,
                    "domain_outcome": domain,
                    "infrastructure_outcome": infrastructure,
                    "closure_reason": reason,
                    "source": "scheduler",
                }
            else:
                step = "stopped_before_start" if cancelled else "not_started"
                domain = "cancelled" if cancelled else "not_started"
                infrastructure = "worker_not_started"
                owner = (
                    str(receipt.get("closure_owner") or "user_stop")
                    if cancelled
                    else "scheduler"
                )
                reason = (
                    (closure_reason or "user_stop")
                    if cancelled
                    else "scheduler_completed_before_account_start"
                )
                conn.execute(
                    """
                    INSERT INTO ig_web_upload_jobs(
                        run_id,account_name,status,current_step,last_error,
                        target_uploads,domain_outcome,infrastructure_outcome,
                        closure_owner,closure_reason,started_at,finished_at,updated_at
                    ) VALUES(?,?,?,?,?,0,?,?,?,?,?,?,datetime('now'))
                    """,
                    (
                        run_ref, name, "stopped", step, step, domain,
                        infrastructure, owner, reason, "",
                        str(receipt.get("finished_at") or ""),
                    ),
                )
                outcome = {
                    "run_id": run_ref,
                    "account_ref": opaque_account_ref(run_ref, name),
                    "worker_started": False,
                    "real_job_id": None,
                    "domain_outcome": domain,
                    "infrastructure_outcome": infrastructure,
                    "closure_reason": reason,
                    "source": "scheduler" if cancelled else "synthetic",
                }
            outcomes.append(outcome)
            try:
                append_run_event(
                    run_ref,
                    "account_detail_finalized",
                    account_ref=outcome["account_ref"],
                    real_job_id=outcome["real_job_id"],
                    worker_started=outcome["worker_started"],
                    source=outcome["source"],
                    domain_outcome=outcome["domain_outcome"],
                    infrastructure_outcome=outcome["infrastructure_outcome"],
                    closure_reason=outcome["closure_reason"],
                )
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    return outcomes


class ProcessManager:
    """Run independent account/connection lanes concurrently."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self._log_path = LOG_DIR / "current.log"

    @staticmethod
    def _conflict(left: set[str], right: set[str]) -> bool:
        return "global:*" in left or "global:*" in right or bool(left.intersection(right))

    def _finish_job(
        self,
        job_id: int,
        terminate: bool = False,
        *,
        closure_owner: str = "process_manager",
        closure_reason: str = "",
    ) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        proc = job.get("proc")
        handle = job.get("job_handle")
        if terminate and proc is not None:
            if handle is not None:
                terminate_process_job(handle)
                job["job_handle"] = None
            stop_process_tree(proc, graceful_timeout=5.0)
        elif handle is not None:
            close_process_job(handle)
            job["job_handle"] = None
        returncode = proc.poll() if proc is not None else None
        receipt = finalize_process_exit(
            str(job.get("run_id") or ""),
            returncode,
            cancelled=bool(terminate),
            closure_owner=(
                str(closure_owner or "user_stop")
                if terminate
                else str(closure_owner or "process_manager")
            ),
            closure_reason=(
                str(closure_reason or "user_stop")
                if terminate
                else str(closure_reason or "process_exit_observed")
            ),
        )
        ensure_task_detail_rows(
            str(job.get("run_id") or ""),
            list(job.get("accounts") or []),
            receipt,
        )
        try:
            append_run_event(
                str(job.get("run_id") or ""),
                "child_exit",
                stream="process_events",
                child_process_state="exited",
                return_code=int(returncode or 0),
            )
            append_run_event(
                str(job.get("run_id") or ""),
                "scheduler_exit",
                stream="process_events",
                scheduler_state="finished",
                return_code=int(returncode or 0),
            )
            finalize_run_diagnostics(
                str(job.get("run_id") or ""),
                domain_outcome=str(receipt.get("domain_outcome") or ""),
                infrastructure_outcome=str(
                    receipt.get("infrastructure_outcome") or ""
                ),
                closure_owner=str(receipt.get("closure_owner") or "process_manager"),
                closure_reason=str(
                    receipt.get("closure_reason") or "process_exit_observed"
                ),
            )
        except Exception:
            pass
        log = job.get("log")
        if log is not None:
            try:
                log.close()
            except Exception:
                pass
        self._jobs.pop(job_id, None)

    def _refresh(self) -> None:
        for job_id, job in list(self._jobs.items()):
            proc = job.get("proc")
            if proc is None or proc.poll() is not None:
                self._finish_job(
                    job_id,
                    terminate=False,
                    closure_owner="process_manager",
                    closure_reason="process_exit_observed",
                )

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            jobs = sorted(self._jobs.values(), key=lambda item: float(item.get("started_at") or 0))
            now = time.time()
            public_jobs = [
                {
                    "id": int(job["id"]),
                    "label": str(job.get("label") or "task"),
                    "run_id": str(job.get("run_id") or ""),
                    "pid": job["proc"].pid,
                    "started_at": float(job.get("started_at") or 0),
                    "elapsed_seconds": max(0, int(now - float(job.get("started_at") or now))),
                    "accounts": list(job.get("accounts") or []),
                }
                for job in jobs
            ]
            first = public_jobs[0] if public_jobs else {}
            count = len(public_jobs)
            label = str(first.get("label") or "")
            if count > 1:
                label = f"{count} tasks active · {label}"
            return {
                "key": "ig-web", "running": bool(public_jobs), "queued": False,
                "active_count": count, "jobs": public_jobs, "label": label,
                "pid": first.get("pid"), "started_at": first.get("started_at", 0.0),
                "elapsed_seconds": first.get("elapsed_seconds", 0),
            }

    def start(
        self,
        command: list[str],
        label: str,
        resources: set[str] | None = None,
        accounts: list[str] | None = None,
        *,
        run_id: str = "",
    ) -> tuple[bool, str]:
        owned = set(resources or {"global:*"})
        account_names = list(accounts or [])
        run_id = str(run_id or new_task_run_id())
        if not run_id.startswith("run-"):
            # Caller-supplied identities remain valid; this branch only keeps
            # the variable visibly normalized as text.
            run_id = str(run_id)
        try:
            create_receipt(run_id, label, account_names)
        except sqlite3.IntegrityError:
            pass
        with self._lock:
            self._refresh()
            for running in self._jobs.values():
                if self._conflict(owned, set(running.get("resources") or set())):
                    reject_receipt(run_id, "active_resource_conflict")
                    try:
                        append_run_event(
                            run_id, "task_rejected", stream="task_index",
                            request_state="rejected",
                            infrastructure_outcome="scheduler_rejected",
                            rejection_owner="process_manager",
                            closure_reason="active_resource_conflict",
                        )
                        finalize_run_diagnostics(
                            run_id,
                            infrastructure_outcome="scheduler_rejected",
                            closure_owner="process_manager",
                            closure_reason="active_resource_conflict",
                        )
                    except Exception:
                        pass
                    return False, "Task conflicts with an active account or proxy lane: " + str(running.get("label") or "task")
            env = os.environ.copy()
            env["SPARKGRID_DATA_DIR"] = str(DATA_DIR)
            env["SPARKGRID_RUN_ID"] = str(run_id)
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env.setdefault("SPARKGRID_SHOW_CURSOR", "1")
            env.setdefault("SPARKGRID_HUMAN_PERSONA", "careful")
            env.setdefault("SPARKGRID_HUMAN_SPEED_MULTIPLIER", "1.30")
            env.setdefault("SPARKGRID_NETWORK_CAPTURE", "0")
            env.setdefault("SPARKGRID_NETWORK_MAX_REQUEST_MB", "2")
            env.setdefault("SPARKGRID_NETWORK_MAX_RESPONSE_MB", "12")
            env.setdefault("SPARKGRID_WARMUP_REELS_FIRST", "1")
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            job_id = self._next_id
            self._next_id += 1
            log_path = LOG_DIR / f"task-{job_id}-{int(time.time())}.log"
            log = log_path.open("w", encoding="utf-8")
            try:
                proc = subprocess.Popen(command, cwd=str(ROOT), env=env, stdout=log,
                                        stderr=subprocess.STDOUT, text=True, **process_group_kwargs())
                job_handle = create_process_job(proc)
            except Exception:
                log.close()
                record_task_outcome(
                    run_id,
                    infrastructure_outcome="browser_start_failed",
                    scheduler_state="start_failed",
                    closure_owner="process_manager",
                    closure_reason="browser_start_failed",
                )
                finalize_process_exit(
                    run_id,
                    1,
                    closure_owner="process_manager",
                    closure_reason="browser_start_failed",
                )
                raise
            mark_task_accepted(run_id, int(proc.pid or 0))
            try:
                append_run_event(
                    run_id, "task_accepted", stream="task_index",
                    request_state="accepted",
                    parent_process_state="running",
                    pid=int(proc.pid or 0),
                )
                append_run_event(
                    run_id, "scheduler_start", stream="process_events",
                    scheduler_state="starting",
                )
                append_run_event(
                    run_id, "child_spawn", stream="process_events",
                    child_process_state="running",
                    pid=int(proc.pid or 0),
                )
            except Exception:
                pass
            self._jobs[job_id] = {
                "id": job_id, "proc": proc, "job_handle": job_handle, "label": label,
                "started_at": time.time(), "resources": owned, "accounts": account_names,
                "log": log, "log_path": log_path, "run_id": str(run_id),
            }
            self._log_path = log_path
            return True, "started"

    def stop(self) -> bool:
        with self._lock:
            self._refresh()
            job_ids = list(self._jobs)
            for job_id in job_ids:
                self._finish_job(
                    job_id,
                    terminate=True,
                    closure_owner="user_stop",
                    closure_reason="stop_all",
                )
            return bool(job_ids)

    def stop_job(self, job_id: int) -> dict[str, Any] | None:
        """Stop exactly one owned worker/browser tree."""
        with self._lock:
            self._refresh()
            job = self._jobs.get(int(job_id))
            if not job:
                return None
            stopped = {
                "id": int(job["id"]),
                "label": str(job.get("label") or "task"),
                "run_id": str(job.get("run_id") or ""),
                "pid": int(job["proc"].pid),
                "accounts": list(job.get("accounts") or []),
                "resources": set(job.get("resources") or set()),
            }
            self._finish_job(
                int(job_id),
                terminate=True,
                closure_owner="user_stop",
                closure_reason="targeted_stop",
            )
            return stopped


procman = ProcessManager()
app = FastAPI(title="SparkGrid Web Upload", docs_url=None, redoc_url=None)


@app.exception_handler(Exception)
async def unhandled_api_error(request: Request, exc: Exception) -> JSONResponse:
    """Return a traceable local error without leaking URLs, credentials or bodies."""
    incident = uuid.uuid4().hex[:10]
    try:
        with (LOG_DIR / "server-errors.log").open("a", encoding="utf-8") as stream:
            stream.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] incident={incident} "
                f"method={request.method} path={request.url.path} "
                f"error={type(exc).__name__}: {exc}\n{traceback.format_exc()}\n"
            )
    except Exception:
        pass
    return JSONResponse(
        {"ok": False, "error": f"SparkGrid API error ({type(exc).__name__}); incident {incident}"},
        status_code=500,
        headers={"X-SparkGrid-Incident": incident},
    )

_VERIFIER_STOP = threading.Event()
_VERIFIER_THREAD: threading.Thread | None = None
_BACKGROUND_STOP = threading.Event()
_BACKGROUND_THREAD: threading.Thread | None = None
_METRICS_THREAD: threading.Thread | None = None


def _recover_background_state() -> None:
    conn = db_conn()
    try:
        ensure_automation_schema(conn)
        ensure_view_schema(conn)
        conn.execute(
            """
            UPDATE automation_plan_slots
            SET status='failed',last_error='SparkGrid restarted while this scheduled session was running',
                finished_at=datetime('now'),updated_at=datetime('now')
            WHERE status='running'
            """
        )
        conn.execute(
            """
            UPDATE view_analytics_runs
            SET status='failed',last_error='SparkGrid restarted during analytics',finished_at=datetime('now')
            WHERE status='running'
            """
        )
        orphan_jobs = conn.execute(
            "SELECT id,COALESCE(current_step,'') AS current_step,COALESCE(status,'') AS status FROM ig_web_upload_jobs "
            "WHERE status IN ('running','starting','browser_launching','uploading','sharing','processing',"
            "'submitted_unverified','uploaded_unverified')"
        ).fetchall()
        for job in orphan_jobs:
            step = str(job["current_step"] or "")
            boundary = irreversible_stage(step)
            if preserve_verified_publication_job(
                conn, int(job["id"]), stop_reason="startup_after_verified_publication"
            ):
                # Verified history/slot evidence is stronger than stale worker
                # state, Share markers, or an interrupted cleanup.
                continue
            if job_has_reel_share_click(conn, int(job["id"])) or boundary in {"share_clicked", "publish_clicked"}:
                conn.execute("UPDATE ig_web_upload_jobs SET status='uploaded_unverified',current_step='uploaded_unverified',last_error='startup_verification_required',finished_at=datetime('now'),updated_at=datetime('now') WHERE id=?", (int(job["id"]),))
                reconcile_terminal_upload_history(conn, int(job["id"]), "uploaded_unverified", "startup_verification_required")
                conn.execute(
                    "UPDATE ig_publishing_history SET next_verify_at='1970-01-01 00:00:00' "
                    "WHERE job_id=? AND status='uploaded_unverified' AND next_verify_at=''",
                    (int(job["id"]),),
                )
            elif job_has_reel_publish_intent(conn, int(job["id"])) or boundary in {"reel_publish_intent", "publish_intent", "submitted_unverified", "story_share_clicked", "story_publish_intent", "story_publish_clicked"}:
                # Never replay an ambiguous publication after restart.
                conn.execute("UPDATE ig_web_upload_jobs SET status='submitted_unverified',current_step='submitted_unverified',last_error='startup_reconciliation_required',finished_at=datetime('now'),updated_at=datetime('now') WHERE id=?", (int(job["id"]),))
                reconcile_terminal_upload_history(conn, int(job["id"]), "submitted_unverified", "startup_reconciliation_required")
            elif boundary in {"confirmed", "uploaded"}:
                # Durable confirmation remains authoritative.
                conn.execute("UPDATE ig_web_upload_jobs SET status='success',updated_at=datetime('now') WHERE id=?", (int(job["id"]),))
            else:
                # Existing UI recognizes stopped as a safely released slot. The
                # typed step/attempt count makes this idempotent and lets the
                # next explicit scheduler pass requeue at most once.
                conn.execute("UPDATE ig_web_upload_jobs SET status='stopped',current_step='startup_reconciliation_required',last_error='worker_process_missing',attempts=MIN(COALESCE(attempts,0)+1,1),finished_at=datetime('now'),updated_at=datetime('now') WHERE id=?", (int(job["id"]),))
                reconcile_terminal_upload_history(conn, int(job["id"]), "stopped", "worker_process_missing")
        conn.commit()
    finally:
        conn.close()


def _background_dispatcher_loop() -> None:
    # Automation and analytics share the same ProcessManager as manual login,
    # upload, warmup and Story jobs.  Therefore only one top-level workflow can
    # own a browser/proxy lane at a time.
    while not _BACKGROUND_STOP.wait(10):
        try:
            if procman.status()["running"]:
                continue
            command: list[str] = []
            label = ""
            conn = db_conn()
            try:
                ensure_automation_schema(conn)
                ensure_view_schema(conn)
                materialize_enabled_slots(conn, days=2)
                slot = due_slot(conn)
                if slot:
                    command = [
                        sys.executable, "-u", str(ROOT / "automation_worker.py"),
                        "--slot-id", str(int(slot["id"])),
                    ]
                    label = f"Automation: {slot.get('plan_name') or 'plan'} · {slot.get('account_name') or ''}"
                else:
                    cfg = view_settings(conn)
                    analytics_due = bool(conn.execute(
                        """
                        SELECT 1 FROM view_analytics_settings
                        WHERE id=1 AND enabled=1
                          AND (next_run_at='' OR datetime(next_run_at)<=datetime('now'))
                        """
                    ).fetchone())
                    if cfg.get("enabled") and analytics_due and due_targets(conn, 1):
                        command = [sys.executable, "-u", str(ROOT / "view_analytics.py"), "--parser-pool"]
                        label = "View analytics · Parser Pool API"
            finally:
                conn.close()
            if command and not procman.status()["running"]:
                background_run_id = new_task_run_id()
                create_receipt(
                    background_run_id,
                    "background_dispatch",
                    _command_accounts(command),
                )
                procman.start(
                    command,
                    label,
                    run_id=background_run_id,
                )
        except Exception as exc:
            try:
                with (LOG_DIR / "background_dispatcher.log").open("a", encoding="utf-8") as handle:
                    handle.write(f"[ERROR] {type(exc).__name__}: {exc}\n")
            except Exception:
                pass


def _run_verifier_once_async() -> None:
    def worker() -> None:
        log_path = LOG_DIR / "publication_verifier.log"
        env = os.environ.copy()
        env["SPARKGRID_DATA_DIR"] = str(DATA_DIR)
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                subprocess.run(
                    [sys.executable, "-u", str(ROOT / "instagram_publication_verifier.py"), "--once", "--limit", "30"],
                    cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT,
                    text=True, timeout=240, check=False,
                )
        except Exception as exc:
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"[ERROR] verifier launcher: {type(exc).__name__}: {exc}\n")
            except Exception:
                pass
    threading.Thread(target=worker, name="publication-verifier-once", daemon=True).start()

def _verifier_loop() -> None:
    # The verifier is intentionally separate from the upload ProcessManager so it
    # can check finished posts while a later upload is running. A file lock in the
    # verifier prevents duplicate workers after rapid refreshes/restarts.
    while not _VERIFIER_STOP.wait(15):
        try:
            # Saved-session verification must not compete with login/upload or
            # analytics for the same account or proxy connection.
            if procman.status()["running"]:
                continue
            conn = db_conn()
            try:
                ensure_history_schema(conn)
                due = conn.execute(
                    "SELECT 1 FROM ig_publishing_history WHERE status IN ('uploaded','uploaded_unverified','submitted_unverified','processing') "
                    "AND next_verify_at!='' "
                    "AND datetime(next_verify_at)<=datetime('now') LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if due:
                _run_verifier_once_async()
        except Exception:
            pass

@app.on_event("startup")
def _start_publication_verifier() -> None:
    global _VERIFIER_THREAD, _BACKGROUND_THREAD, _METRICS_THREAD
    ensure_schema()
    cleanup_run_diagnostics(trigger="startup")
    _recover_background_state()
    if _VERIFIER_THREAD is None or not _VERIFIER_THREAD.is_alive():
        _VERIFIER_STOP.clear()
        _VERIFIER_THREAD = threading.Thread(target=_verifier_loop, name="publication-verifier-loop", daemon=True)
        _VERIFIER_THREAD.start()
    if _BACKGROUND_THREAD is None or not _BACKGROUND_THREAD.is_alive():
        _BACKGROUND_STOP.clear()
        _BACKGROUND_THREAD = threading.Thread(target=_background_dispatcher_loop, name="automation-analytics-loop", daemon=True)
        _BACKGROUND_THREAD.start()
    # Start Ads Power metrics checker
    try:
        import ads_power_checker
        _METRICS_THREAD = ads_power_checker.start_checker_thread()
    except Exception as exc:
        print(f"[startup] metrics checker failed to start: {exc}")
    # Start Story auto-trigger
    try:
        import story_trigger
        story_trigger.start_trigger_thread()
        story_trigger.start_retry_thread()
    except Exception as exc:
        print(f"[startup] story trigger failed to start: {exc}")

@app.on_event("shutdown")
def _stop_publication_verifier() -> None:
    _VERIFIER_STOP.set()
    _BACKGROUND_STOP.set()

app.mount("/ig-web-upload-debug", StaticFiles(directory=str(DEBUG_UPLOAD)), name="ig_web_upload_debug")
app.mount("/web-warmup-debug", StaticFiles(directory=str(DEBUG_WARMUP)), name="web_warmup_debug")


DIAGNOSTICS_DIR = DATA_DIR / "diagnostics"
WORKER_DEBUG_UPLOAD = (
    DATA_DIR / "ai_content_data" / "debug" / "ig_web_upload"
)
WORKER_DEBUG_WARMUP = (
    DATA_DIR / "browser_warmup_data" / "debug" / "web_warmup"
)
DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)


def _diagnostic_redactions(account_name: str) -> list[str]:
    account_values = {
        str(account_name or ""),
        re.sub(r"[^A-Za-z0-9_.-]+", "_", account_name)[:80],
    }
    values: list[str] = [
        str(ROOT),
        str(DATA_DIR),
        str(Path.home()),
        *account_values,
    ]
    conn = db_conn()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(a.password,''),COALESCE(a.api_password,''),COALESCE(a.api_totp_secret,''),
                   COALESCE(a.proxy,''),COALESCE(c.proxy_url,''),COALESCE(c.rotation_url,'')
            FROM accounts a LEFT JOIN web_connections c ON c.id=a.web_connection_id WHERE a.name=?
            """,
            (account_name,),
        ).fetchone()
        if row:
            values.extend(str(value or "") for value in row)
        for table, fields in (
            (
                "api_content_assets",
                ("caption", "file_path", "original_name"),
            ),
            (
                "ig_publishing_history",
                ("caption", "file_path", "video_name"),
            ),
        ):
            columns = {
                str(item[1])
                for item in conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            selected = [field for field in fields if field in columns]
            if "account_name" not in columns or not selected:
                continue
            rows = conn.execute(
                f"SELECT {','.join(selected)} FROM {table} "
                "WHERE account_name=?",
                (account_name,),
            ).fetchall()
            for item in rows:
                values.extend(str(value or "") for value in item)
    except Exception:
        pass
    finally:
        conn.close()
    expanded = {value for value in values if value}
    for value in tuple(expanded):
        if "://" not in value:
            continue
        try:
            parsed = urlparse(value)
        except Exception:
            continue
        expanded.update(
            item
            for item in (
                parsed.hostname,
                parsed.username,
                parsed.password,
                parsed.path if parsed.path not in {"", "/"} else "",
                parsed.query,
                parsed.fragment,
            )
            if item
        )
    return sorted(
        {
            value
            for value in expanded
            if len(value) >= 4 or value in account_values
        },
        key=len,
        reverse=True,
    )


def _redact_diagnostic_text(text: str, secrets: list[str]) -> str:
    result = str(text or "")
    for secret in secrets:
        result = result.replace(secret, "[REDACTED]")
    result = re.sub(
        r'(?i)(sessionid|csrftoken|cookie|authorization|bearer|token|'
        r'password|api[_-]?key|totp|2fa)([^A-Za-z0-9]+)'
        r'[^\s",;]+',
        r"\1\2[REDACTED]",
        result,
    )
    result = re.sub(r"(?i)https?://[^\s/@:]+:[^\s/@]+@", "http://[REDACTED]@", result)
    return result


AUTO_LOGIN_EXPORT_FILE = "auto_login_transaction.jsonl"
AUTO_LOGIN_EXPORT_SCHEMA_VERSION = 1
_AUTO_LOGIN_EXPORT_STATES = {
    "authenticated",
    "blocker_detected",
    "challenge",
    "consent_blocker",
    "login_combined",
    "login_password_only",
    "login_username_first",
    "transitioning",
    "two_factor",
    "unsupported_stable",
    "unknown",
}
_AUTO_LOGIN_EXPORT_TERMINALS = {
    "blocker_detected",
    "challenge_detected",
    "login_form_transition_timeout",
    "login_submit_control_not_found",
    "login_submit_no_transition",
    "password_field_not_found",
    "password_input_not_retained",
    "unsupported_login_state",
    "username_field_not_found",
    "username_field_not_ready",
}
_DIAGNOSTIC_WORKER_FILE_ALLOWLIST = {
    "actions.jsonl",
    AUTO_LOGIN_EXPORT_FILE,
    "consent_recovery.jsonl",
    "heartbeat_transport_error.json",
    "human_actions.jsonl",
    "human_status.json",
    "latest_state.json",
    "login_post_action.jsonl",
    "warmup_stats.json",
}
_DIAGNOSTIC_ARCHIVE_STATIC_ALLOWLIST = {
    "diagnostic.json",
    "runtime/evidence.json",
    "runtime/task.log",
}


def _diagnostic_archive_path_allowed(name: str) -> bool:
    value = str(name or "")
    if value in _DIAGNOSTIC_ARCHIVE_STATIC_ALLOWLIST:
        return True
    if value.startswith("worker/"):
        worker_name = value.removeprefix("worker/")
        if worker_name in _DIAGNOSTIC_WORKER_FILE_ALLOWLIST:
            return True
    return bool(
        re.fullmatch(r"worker/snapshots/\d{2}\.json", value)
        or re.fullmatch(r"worker/sanitized_dom/\d{2}\.html", value)
    )


def _diagnostic_int(value: Any, *, maximum: int = 999_999_999) -> int:
    try:
        return max(0, min(maximum, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _diagnostic_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _diagnostic_enum(value: Any, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def _diagnostic_identifier(value: Any, fallback: str = "") -> str:
    normalized = str(value or "")
    return (
        normalized
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,79}", normalized)
        else fallback
    )


def _sanitize_auto_login_diagnostic_jsonl(text: str) -> str:
    records: list[str] = []
    for line in str(text or "").splitlines():
        try:
            raw = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            continue
        counts = (
            raw.get("candidate_counts")
            if isinstance(raw.get("candidate_counts"), dict)
            else {}
        )
        candidate_raw = (
            raw.get("selected_candidate")
            if isinstance(raw.get("selected_candidate"), dict)
            else None
        )
        candidate = None
        if candidate_raw is not None:
            candidate = {
                "intent": _diagnostic_enum(
                    candidate_raw.get("intent"),
                    {"otp", "password", "username", "unknown"},
                    "unknown",
                ),
                "type_category": _diagnostic_enum(
                    candidate_raw.get("type_category"),
                    {
                        "email",
                        "number",
                        "other",
                        "password",
                        "search",
                        "tel",
                        "text",
                        "textarea",
                    },
                    "other",
                ),
                "autocomplete_category": _diagnostic_enum(
                    candidate_raw.get("autocomplete_category"),
                    {
                        "current-password",
                        "email",
                        "new-password",
                        "none",
                        "off",
                        "one-time-code",
                        "other",
                        "tel",
                        "username",
                    },
                    "other",
                ),
                "form_owned": bool(candidate_raw.get("form_owned")),
                "attached": bool(candidate_raw.get("attached")),
                "visible_probe": _diagnostic_bool(
                    candidate_raw.get("visible_probe")
                ),
                "enabled_probe": _diagnostic_bool(
                    candidate_raw.get("enabled_probe")
                ),
                "editable_probe": _diagnostic_bool(
                    candidate_raw.get("editable_probe")
                ),
                "readonly": bool(candidate_raw.get("readonly")),
                "bounding_box_present": bool(
                    candidate_raw.get("bounding_box_present")
                ),
                "viewport_intersection": bool(
                    candidate_raw.get("viewport_intersection")
                ),
                "node_replacement": bool(
                    candidate_raw.get("node_replacement")
                ),
            }
        interaction_raw = (
            raw.get("interaction")
            if isinstance(raw.get("interaction"), dict)
            else {}
        )
        postcondition_raw = (
            raw.get("postcondition")
            if isinstance(raw.get("postcondition"), dict)
            else {}
        )
        terminal_raw = (
            raw.get("terminal")
            if isinstance(raw.get("terminal"), dict)
            else {}
        )
        terminal_code = str(terminal_raw.get("code") or "")
        if terminal_code not in _AUTO_LOGIN_EXPORT_TERMINALS:
            terminal_code = ""
        timestamp = str(raw.get("timestamp_utc") or "")
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
            r"(?:\.\d{1,6})?Z",
            timestamp,
        ):
            timestamp = ""
        frame_ref = str(raw.get("frame_ref") or "")
        if not re.fullmatch(r"f_[a-f0-9]{12}_\d{3}", frame_ref):
            frame_ref = ""
        container_ref = str(raw.get("container_ref") or "")
        if not re.fullmatch(r"c_[a-f0-9]{12}_\d{3}", container_ref):
            container_ref = ""
        record = {
            "schema_version": AUTO_LOGIN_EXPORT_SCHEMA_VERSION,
            "timestamp_utc": timestamp,
            "sequence": _diagnostic_int(raw.get("sequence")),
            "event": _diagnostic_enum(
                raw.get("event"),
                {"interaction", "observation", "terminal"},
                "observation",
            ),
            "attempt_number": _diagnostic_int(
                raw.get("attempt_number"), maximum=999
            ),
            "document_epoch": _diagnostic_int(
                raw.get("document_epoch"), maximum=99_999
            ),
            "mutation_epoch": _diagnostic_int(
                raw.get("mutation_epoch")
            ),
            "state": _diagnostic_enum(
                raw.get("state"),
                _AUTO_LOGIN_EXPORT_STATES,
                "unknown",
            ),
            "url_category": _diagnostic_enum(
                raw.get("url_category"),
                {
                    "challenge",
                    "consent",
                    "instagram",
                    "login_family",
                    "two_factor",
                    "unknown",
                },
                "unknown",
            ),
            "frame_ref": frame_ref,
            "container_ref": container_ref,
            "candidate_counts": {
                intent: _diagnostic_int(
                    counts.get(intent), maximum=999
                )
                for intent in ("username", "password", "otp", "other")
            },
            "selected_candidate": candidate,
            "interaction": {
                "attempted": bool(interaction_raw.get("attempted")),
                "kind": _diagnostic_enum(
                    interaction_raw.get("kind"),
                    {"click_fill", "native_setter", "none", "reacquire"},
                    "none",
                ),
                "exception_class": _diagnostic_identifier(
                    interaction_raw.get("exception_class"),
                    "UnknownError"
                    if interaction_raw.get("exception_class")
                    else "",
                ),
            },
            "postcondition": {
                "value_match": _diagnostic_bool(
                    postcondition_raw.get("value_match")
                )
            },
            "terminal": {
                "owner": (
                    "auto_login_transaction_coordinator"
                    if terminal_code
                    and terminal_raw.get("owner")
                    == "auto_login_transaction_coordinator"
                    else ""
                ),
                "code": terminal_code,
                "reason_category": _diagnostic_enum(
                    terminal_raw.get("reason_category"),
                    {
                        "blocker",
                        "candidate_absent",
                        "challenge",
                        "interaction_not_verified",
                        "none",
                        "postcondition_negative",
                        "submit_no_transition",
                        "submit_not_dispatched",
                        "transition_timeout",
                        "unsupported_state",
                    },
                    "none",
                ),
            },
        }
        records.append(
            json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        )
    return "\n".join(records) + ("\n" if records else "")


def _sanitize_human_actions_jsonl(text: str) -> str:
    allowed_kinds = {
        "click",
        "click_error",
        "cursor_overlay",
        "cursor_overlay_restore",
        "cursor_overlay_restore_error",
        "direct_fallback",
        "hover",
        "idle",
        "move",
        "move_error",
        "press",
        "scroll",
        "scroll_error",
        "target",
        "type",
        "type_error",
        "wander",
    }
    records: list[str] = []
    for line in str(text or "").splitlines():
        try:
            raw = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        error_value = raw.get("error")
        records.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": _diagnostic_enum(
                        raw.get("kind") or raw.get("event"),
                        allowed_kinds,
                        "unknown",
                    ),
                    "method": _diagnostic_identifier(
                        raw.get("method"), ""
                    ),
                    "exception_class": _diagnostic_identifier(
                        error_value,
                        "UnknownError" if error_value else "",
                    ),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    return "\n".join(records) + ("\n" if records else "")


def _sanitize_worker_state_records(text: str, *, jsonl: bool) -> str:
    raw_values: list[Any] = []
    if jsonl:
        for line in str(text or "").splitlines():
            try:
                raw_values.append(json.loads(line))
            except (TypeError, ValueError):
                continue
    else:
        try:
            raw_values.append(json.loads(str(text or "")))
        except (TypeError, ValueError):
            pass
    records = []
    for raw in raw_values:
        if not isinstance(raw, dict):
            continue
        records.append(
            {
                "schema_version": 1,
                "state": _diagnostic_enum(
                    raw.get("state")
                    or raw.get("classified_state")
                    or raw.get("event"),
                    {
                        "authenticated",
                        "failed",
                        "login_required",
                        "login_transition",
                        "logged_in",
                        "request_failed",
                        "request_finished",
                        "request_started",
                        "response_received",
                        "telemetry_started",
                        "telemetry_stopped",
                        "transition_timeout",
                        "transitioning",
                        "two_factor_required",
                        "unknown",
                    },
                    "unknown",
                ),
                "iteration": _diagnostic_int(
                    raw.get("iteration"), maximum=99_999
                ),
            }
        )
    if jsonl:
        return "".join(
            json.dumps(item, ensure_ascii=True, separators=(",", ":"))
            + "\n"
            for item in records
        )
    return json.dumps(
        records[0] if records else {},
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _sanitize_worker_artifact(name: str, text: str) -> str:
    if name == AUTO_LOGIN_EXPORT_FILE:
        return _sanitize_auto_login_diagnostic_jsonl(text)
    if name == "human_actions.jsonl":
        return _sanitize_human_actions_jsonl(text)
    return _sanitize_worker_state_records(
        text,
        jsonl=name.endswith(".jsonl"),
    )


def _sanitize_task_log_excerpt(
    text: str, account_name: str
) -> str:
    categories = {
        "authenticated": ("authenticated", "logged_in"),
        "blocker": ("blocker", "popup"),
        "challenge": ("challenge", "checkpoint", "restricted"),
        "consent": ("consent", "cookie"),
        "login": ("login", "password", "username"),
        "network": ("network", "connection", "proxy"),
        "timeout": ("timeout", "timed out"),
        "two_factor": ("two_factor", "2fa", "otp"),
        "worker_exit": ("worker_exit", "process_exit", "heartbeat"),
    }
    records = []
    for line_number, line in enumerate(str(text or "").splitlines(), 1):
        if account_name not in line:
            continue
        lowered = line.lower()
        matched = sorted(
            name
            for name, markers in categories.items()
            if any(marker in lowered for marker in markers)
        )
        records.append(
            {
                "schema_version": 1,
                "line_number": line_number,
                "categories": matched or ["other"],
            }
        )
    return "".join(
        json.dumps(item, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for item in records
    )


def _diagnostic_error_category(value: Any) -> str:
    lowered = str(value or "").lower()
    for category, markers in (
        ("challenge", ("challenge", "checkpoint", "restricted")),
        ("consent", ("consent", "cookie")),
        ("credential_rejected", ("incorrect", "rejected", "invalid")),
        ("network", ("network", "proxy", "connection", "tunnel")),
        ("timeout", ("timeout", "timed out")),
        ("two_factor", ("two_factor", "2fa", "otp")),
    ):
        if any(marker in lowered for marker in markers):
            return category
    return "none" if not lowered else "other"


def _sanitized_dom_for_export(text: str, secrets: list[str]) -> str:
    value = _redact_diagnostic_text(text, secrets)
    value = re.sub(
        r"(?is)<(script|style|textarea)\b[^>]*>.*?</\1>",
        "",
        value,
    )
    value = re.sub(
        r'(?i)\s(value|src|href|name|placeholder|aria-label|'
        r'class|id|style|action|'
        r'data-[\w-]+)="[^"]*"',
        r' \1="[REDACTED]"',
        value,
    )
    # Keep structural tags and accessibility attributes, but not arbitrary
    # page text which can contain captions, DMs, or other private content.
    value = re.sub(
        r">([^<]+)<",
        lambda match: (
            "><"
            if not match.group(1).strip()
            else ">[TEXT_REDACTED]<"
        ),
        value,
    )
    return value


def _diagnostic_build_identity() -> dict[str, str]:
    result = {
        "app_version": APP_VERSION,
        "release_commit": "source-tree",
    }
    roots = [ROOT]
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        roots.insert(0, Path(str(frozen_root)))
    for root in roots:
        manifest_path = root / "build_manifest.json"
        try:
            payload = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError):
            continue
        result["app_version"] = str(
            payload.get("app_version") or APP_VERSION
        )[:80]
        result["release_commit"] = str(
            payload.get("release_commit") or "unknown"
        )[:80]
        break
    return result


def build_account_diagnostic(account_name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", account_name)[:80] or "account"
    candidates: list[Path] = []
    for root in (WORKER_DEBUG_UPLOAD, WORKER_DEBUG_WARMUP):
        try:
            candidates.extend(path for path in root.glob(f"*/{safe}") if path.is_dir())
            candidates.extend(path for path in root.glob(f"*/{account_name}") if path.is_dir())
        except Exception:
            pass
    candidates = sorted(set(candidates), key=lambda path: path.stat().st_mtime, reverse=True)
    source = candidates[0] if candidates else None
    output = DIAGNOSTICS_DIR / (
        f"SparkGrid-diagnostic-{int(time.time())}-"
        f"{uuid.uuid4().hex[:12]}.zip"
    )
    secrets = _diagnostic_redactions(account_name)
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT status,web_upload_login_status,web_upload_last_error FROM accounts WHERE name=?",
            (account_name,),
        ).fetchone()
        manifest = {
            "schema_version": 2,
            "status": _diagnostic_identifier(
                str(row[0] or "") if row else "missing",
                "unknown",
            ),
            "login_status": _diagnostic_identifier(
                str(row[1] or "") if row else "",
                "unknown",
            ),
            "error_category": _diagnostic_error_category(
                str(row[2] or "") if row else ""
            ),
            "build": _diagnostic_build_identity(),
            "note": (
                "Cookies, passwords, tokens, proxy credentials, captions, "
                "private page text, screenshots, HAR, network captures and "
                "bot.db are excluded."
            ),
        }
        job = conn.execute(
            """
            SELECT id,run_id,status,current_step,domain_outcome,
                   infrastructure_outcome,closure_owner,closure_reason,
                   debug_dir,started_at,finished_at,updated_at
            FROM ig_web_upload_jobs
            WHERE account_name=? ORDER BY id DESC LIMIT 1
            """,
            (account_name,),
        ).fetchone()
        raw_job = dict(job) if job else {}
        job_evidence = {
            "id": _diagnostic_int(raw_job.get("id")),
            "status": _diagnostic_identifier(
                raw_job.get("status"), "unknown"
            ),
            "current_step": _diagnostic_identifier(
                raw_job.get("current_step"), "unknown"
            ),
            "domain_outcome": _diagnostic_identifier(
                raw_job.get("domain_outcome"), ""
            ),
            "infrastructure_outcome": _diagnostic_identifier(
                raw_job.get("infrastructure_outcome"), ""
            ),
            "closure_owner": _diagnostic_identifier(
                raw_job.get("closure_owner"), ""
            ),
            "closure_reason": _diagnostic_identifier(
                raw_job.get("closure_reason"), ""
            ),
            "started": bool(raw_job.get("started_at")),
            "finished": bool(raw_job.get("finished_at")),
            "updated": bool(raw_job.get("updated_at")),
        }
    finally:
        conn.close()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostic.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if source:
            for name in sorted(_DIAGNOSTIC_WORKER_FILE_ALLOWLIST):
                file = source / name
                if file.is_file() and file.stat().st_size <= 5_000_000:
                    sanitized = _sanitize_worker_artifact(
                        name,
                        file.read_text(
                            encoding="utf-8", errors="replace"
                        ),
                    )
                    archive.writestr(
                        f"worker/{name}",
                        sanitized,
                    )
            snapshots = source / "snapshots"
            if snapshots.is_dir():
                structured = sorted(
                    snapshots.glob("*.json"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )[:20]
                dom_files = sorted(
                    snapshots.glob("*.html"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )[:5]
                latest_dom = source / "latest_safe_dom.html"
                if latest_dom.is_file():
                    dom_files.insert(0, latest_dom)
                for index, file in enumerate(structured):
                    if file.stat().st_size <= 2_000_000:
                        archive.writestr(
                            f"worker/snapshots/{index:02d}.json",
                            _sanitize_worker_state_records(
                                file.read_text(
                                    encoding="utf-8", errors="replace"
                                ),
                                jsonl=False,
                            ),
                        )
                for index, file in enumerate(dom_files):
                    if file.stat().st_size <= 2_000_000:
                        archive.writestr(
                            f"worker/sanitized_dom/{index:02d}.html",
                            _sanitized_dom_for_export(
                                file.read_text(
                                    encoding="utf-8", errors="replace"
                                ),
                                secrets,
                            ),
                        )
        task_logs = sorted(
            LOG_DIR.glob("task-*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for log_file in task_logs:
            if log_file.stat().st_size > 5_000_000:
                continue
            text = log_file.read_text(
                encoding="utf-8", errors="replace"
            )
            if account_name not in text:
                continue
            archive.writestr(
                "runtime/task.log",
                _sanitize_task_log_excerpt(text, account_name),
            )
            break
        runtime_files: list[dict[str, Any]] = []
        heartbeat_root = DATA_DIR / "runtime" / "heartbeats"
        for pattern in (
            f"*-{safe}.closure.json",
            f"*-{safe}.transport_error.json",
            f"*-{safe}.heartbeat",
        ):
            for file in sorted(
                heartbeat_root.glob(pattern),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:5]:
                try:
                    raw_evidence = json.loads(
                        file.read_text(encoding="utf-8")
                    )
                    if not isinstance(raw_evidence, dict):
                        continue
                    suffix = (
                        "closure"
                        if file.name.endswith(".closure.json")
                        else "transport_error"
                        if file.name.endswith(".transport_error.json")
                        else "heartbeat"
                    )
                    runtime_files.append(
                        {
                            "kind": suffix,
                            "evidence": {
                                "closure_owner": _diagnostic_identifier(
                                    raw_evidence.get("closure_owner"), ""
                                ),
                                "closure_reason": _diagnostic_identifier(
                                    raw_evidence.get("closure_reason"), ""
                                ),
                                "worker_pid": _diagnostic_int(
                                    raw_evidence.get("worker_pid")
                                ),
                                "last_liveness_sequence": (
                                    _diagnostic_int(
                                        raw_evidence.get(
                                            "last_liveness_sequence"
                                        )
                                    )
                                ),
                                "last_semantic_phase": (
                                    _diagnostic_identifier(
                                        raw_evidence.get(
                                            "last_semantic_phase"
                                        ),
                                        "",
                                    )
                                ),
                            },
                        }
                    )
                except (OSError, ValueError, TypeError):
                    continue
        runtime = {
            "job": job_evidence,
            "heartbeat_and_closure": runtime_files,
            "build": _diagnostic_build_identity(),
        }
        archive.writestr(
            "runtime/evidence.json",
            _redact_diagnostic_text(
                json.dumps(runtime, ensure_ascii=False, indent=2),
                secrets,
            ),
        )
    with zipfile.ZipFile(output, "r") as archive:
        unexpected = [
            name
            for name in archive.namelist()
            if not _diagnostic_archive_path_allowed(name)
        ]
    if unexpected:
        raise RuntimeError(
            "diagnostic archive allowlist rejected generated artifacts"
        )
    return output


def response_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def ensure_story_library_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS story_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL UNIQUE,
            original_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS story_settings (
            id INTEGER PRIMARY KEY CHECK (id=1),
            descriptions TEXT NOT NULL DEFAULT '',
            link_url TEXT NOT NULL DEFAULT '',
            sticker_text TEXT NOT NULL DEFAULT 'Open link',
            sticker_x REAL NOT NULL DEFAULT 0.5,
            sticker_y REAL NOT NULL DEFAULT 0.82,
            highlight_name TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    story_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(story_settings)")}
    for column, ddl in {
        "sticker_text": "TEXT NOT NULL DEFAULT 'Open link'",
        "sticker_x": "REAL NOT NULL DEFAULT 0.5",
        "sticker_y": "REAL NOT NULL DEFAULT 0.82",
        "highlight_name": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in story_columns:
            conn.execute(f"ALTER TABLE story_settings ADD COLUMN {column} {ddl}")
    conn.execute("INSERT OR IGNORE INTO story_settings(id) VALUES (1)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS story_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            selection_json TEXT NOT NULL DEFAULT '{}',
            manifest_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def suspended_account(name: str, status: str, login_status: str, error: str) -> bool:
    haystack = " ".join((str(status or ""), str(login_status or ""), str(error or ""))).lower()
    return any(word in haystack for word in ("suspend", "banned", "account disabled", "restrict", "checkpoint", "challenge", "human_verification"))


# Product-facing Banned is deliberately narrower than generic workflow trouble.
# The current normalized login state wins over an older account status/error.
BANNED_ACCOUNT_STATES = frozenset({
    "human_verification", "checkpoint", "challenge", "suspended", "disabled",
    "restricted", "account_restricted",
})


def current_normalized_account_state(status: str, login_status: str) -> str:
    """Return the current normalized account state without inspecting errors/jobs."""
    return str(login_status or status or "").strip().lower()


def current_banned_account_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name,status,web_upload_login_status FROM accounts ORDER BY name"
    ).fetchall()
    return [
        str(row["name"])
        for row in rows
        if current_normalized_account_state(row["status"], row["web_upload_login_status"])
        in BANNED_ACCOUNT_STATES
    ]


def without_suspended_accounts(names: list[str]) -> tuple[list[str], list[str]]:
    if not names:
        return [], []
    conn = db_conn()
    try:
        placeholders = ",".join("?" for _ in names)
        rows = conn.execute(
            f"SELECT name,status,web_upload_login_status,web_upload_last_error FROM accounts WHERE name IN ({placeholders})",
            names,
        ).fetchall()
        dead = {
            str(row["name"])
            for row in rows
            if suspended_account(row["name"], row["status"], row["web_upload_login_status"], row["web_upload_last_error"])
        }
    finally:
        conn.close()
    return [name for name in names if name not in dead], [name for name in names if name in dead]


def upload_settings(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    if own:
        conn = db_conn()
    try:
        rows = conn.execute("SELECT key,value FROM ig_web_upload_settings").fetchall()
        values = {str(row["key"]): str(row["value"] or "") for row in rows}
        engine = values.get("work_mode") or values.get("upload_engine", "clean_web")
        if engine not in {"manual", "clean_web", "api"}:
            engine = "clean_web"
        try:
            parallel = max(1, min(int(values.get("api_parallel", "3") or 3), 100))
        except Exception:
            parallel = 3
        try:
            browser_parallel = max(1, min(int(values.get("browser_parallel", "3") or 3), 50))
        except Exception:
            browser_parallel = 3
        return {"upload_engine": engine, "api_parallel": parallel, "browser_parallel": browser_parallel,
                "traffic_saver": str(values.get("traffic_saver") or "off"),
                "warmup_enabled": str(values.get("warmup_enabled") or "on")}
    finally:
        if own and conn is not None:
            conn.close()


def current_browser_parallel() -> int:
    return int(upload_settings().get("browser_parallel") or 3)


def save_upload_settings(engine: str, api_parallel: int, browser_parallel: int = 3, traffic_saver: str = "", warmup_enabled: str = "") -> dict[str, Any]:
    engine = str(engine or "clean_web").strip().lower()
    if engine not in {"manual", "clean_web", "api"}:
        engine = "clean_web"
    parallel = max(1, min(int(api_parallel or 3), 100))
    browser_parallel = max(1, min(int(browser_parallel or 3), 50))
    ts_value = "on" if str(traffic_saver or "").lower() in {"on", "1", "true", "yes"} else "off"
    # Default "on" (matches historical always-warmup behavior) if unset —
    # only "off"/"0"/"false"/"no" turns it off, everything else is "on".
    we_value = "off" if str(warmup_enabled or "").lower() in {"off", "0", "false", "no"} else "on"
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO ig_web_upload_settings(key,value,updated_at) VALUES ('upload_engine',?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (engine,),
        )
        conn.execute(
            "INSERT INTO ig_web_upload_settings(key,value,updated_at) VALUES ('work_mode',?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (engine,),
        )
        conn.execute(
            "INSERT INTO ig_web_upload_settings(key,value,updated_at) VALUES ('api_parallel',?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (str(parallel),),
        )
        conn.execute(
            "INSERT INTO ig_web_upload_settings(key,value,updated_at) VALUES ('browser_parallel',?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (str(browser_parallel),),
        )
        conn.execute(
            "INSERT INTO ig_web_upload_settings(key,value,updated_at) VALUES ('traffic_saver',?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (ts_value,),
        )
        conn.execute(
            "INSERT INTO ig_web_upload_settings(key,value,updated_at) VALUES ('warmup_enabled',?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (we_value,),
        )
        conn.commit()
    finally:
        conn.close()
    return {"upload_engine": engine, "api_parallel": parallel, "browser_parallel": browser_parallel,
            "traffic_saver": ts_value, "warmup_enabled": we_value}


def assign_legacy_proxy_connection(conn: sqlite3.Connection, account_name: str, proxy: str) -> dict[str, Any]:
    """Keep old proxy import/edit paths compatible with the Connections model."""
    ensure_connection_schema(conn)
    proxy = str(proxy or "").strip()
    if not proxy:
        connection_id = direct_connection_id(conn)
    else:
        created = import_static_connections(conn, proxy, "Static")
        if not created:
            raise ValueError("Could not create a static connection")
        connection_id = int(created[0].get("id") or 0)
    assign_connection(conn, [account_name], connection_id)
    return get_connection(conn, connection_id) or {}


def clean_names(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        source = raw
    else:
        source = re.split(r"[,\n]+", str(raw or ""))
    result: list[str] = []
    for item in source:
        name = str(item or "").strip().split("|")[0].split(":")[0].lstrip("@")
        if name and name not in result:
            result.append(name)
    return result


def clean_niche_name(raw: Any, allow_empty: bool = True) -> str:
    value = re.sub(r"\s+", " ", str(raw or "").strip())
    value = re.sub(r"[<>\x00-\x1f]", "", value)[:48].strip()
    if not value and not allow_empty:
        raise ValueError("Niche name is required")
    return value


def parse_story_scope(raw: Any) -> Any:
    if raw is None or isinstance(raw, (list, dict)):
        return raw
    value = str(raw).strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid Story selection scope") from exc


def resolve_story_account_scope(
    conn: sqlite3.Connection,
    niche_scope: Any,
    account_scope: Any,
    legacy_accounts: Any = None,
) -> dict[str, Any]:
    """Resolve one fail-closed Story scope; later stages may only subtract."""
    niche_scope = parse_story_scope(niche_scope)
    account_scope = parse_story_scope(account_scope)
    legacy_names = clean_names(legacy_accounts)

    if account_scope is None:
        explicit_names: list[str] | None = legacy_names
        account_contract: Any = None
    elif account_scope == "all":
        explicit_names = None
        account_contract = "all"
    elif isinstance(account_scope, list):
        explicit_names = clean_names(account_scope)
        account_contract = explicit_names
    else:
        raise ValueError("Invalid Story account scope")

    field = ""
    params: list[Any] = []
    where = ["COALESCE(web_upload_enabled,1)=1"]
    normalized_niche: Any = niche_scope
    if niche_scope is None:
        # Legacy callers have no niche contract and therefore must provide an
        # explicit account list. Missing scope never means every account.
        if explicit_names is None:
            raise ValueError("Story account scope is required")
    elif niche_scope == "all":
        normalized_niche = "all"
    elif isinstance(niche_scope, dict):
        workspace = str(niche_scope.get("workspace") or "").strip().lower()
        mode = str(niche_scope.get("mode") or "").strip().lower()
        if workspace not in {"quality", "scale"}:
            raise ValueError("Invalid Story niche workspace")
        field = "web_upload_quality_niche" if workspace == "quality" else "web_upload_scale_niche"
        where.append("COALESCE(web_upload_content_mode,'scale')=?")
        params.append(workspace)
        if mode == "named":
            name = clean_niche_name(niche_scope.get("name"), allow_empty=False)
            where.append(f"LOWER(TRIM(COALESCE({field},'')))=LOWER(?)")
            params.append(name)
            normalized_niche = {"workspace": workspace, "mode": "named", "name": name}
        elif mode == "unassigned":
            where.append(f"TRIM(COALESCE({field},''))=''")
            normalized_niche = {"workspace": workspace, "mode": "unassigned", "name": ""}
        elif mode == "all":
            normalized_niche = {"workspace": workspace, "mode": "all", "name": ""}
        else:
            raise ValueError("Invalid Story niche scope")
    else:
        raise ValueError("Invalid Story niche scope")

    rows = conn.execute(
        f"SELECT name FROM accounts WHERE {' AND '.join(where)} ORDER BY name",
        params,
    ).fetchall()
    candidates = [str(row["name"]) for row in rows]
    candidate_keys = {name.lower(): name for name in candidates}
    if explicit_names is None:
        final_names = candidates
    else:
        final_names = [
            candidate_keys[name.lower()]
            for name in explicit_names
            if name.lower() in candidate_keys
        ]
    return {
        "version": 1,
        "niche_scope": normalized_niche,
        "account_scope": account_contract,
        "final_account_names": final_names,
    }


def load_story_job_snapshot(manifest_path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("selection"), dict):
        return dict(payload["selection"])
    # Old manifests were account-name keyed. Treat those keys as their frozen
    # scope so compatibility cannot turn into a database-wide selection.
    return {
        "version": 0,
        "niche_scope": None,
        "account_scope": list(payload) if isinstance(payload, dict) else [],
        "final_account_names": list(payload) if isinstance(payload, dict) else [],
    }


def latest_dump() -> dict[str, Any]:
    candidates: list[tuple[Path, str]] = [(DEBUG_UPLOAD, "ig_upload"), (DEBUG_WARMUP, "web_warmup")]
    latest: dict[str, Any] = {}
    latest_mtime = 0.0
    for root, source in candidates:
        try:
            run_file = root / "latest_run.txt"
            account_file = root / ("latest_account.txt" if source == "ig_upload" else "latest_profile.txt")
            if not run_file.exists() or not account_file.exists():
                continue
            run_id = run_file.read_text(encoding="utf-8").strip()
            account = account_file.read_text(encoding="utf-8").strip()
            folder = root / run_id / account
            state_file = folder / "latest_state.json"
            text_file = folder / "latest_text.txt"
            image_file = folder / "latest.png"
            stamp = max([p.stat().st_mtime for p in (state_file, text_file, image_file) if p.exists()] or [0])
            if stamp <= latest_mtime:
                continue
            payload: dict[str, Any] = {"source": source, "account": account}
            if state_file.exists():
                try:
                    payload.update(json.loads(state_file.read_text(encoding="utf-8")))
                except Exception:
                    pass
            if text_file.exists():
                payload["visible_text"] = text_file.read_text(encoding="utf-8", errors="ignore")[-4500:]
            if image_file.exists():
                prefix = "/ig-web-upload-debug" if source == "ig_upload" else "/web-warmup-debug"
                payload["screenshot_url"] = f"{prefix}/{run_id}/{account}/latest.png?ts={int(time.time())}"
            latest = payload
            latest_mtime = stamp
        except Exception:
            continue
    return latest


def mark_orphan_jobs_stopped(
    account_names: list[str] | None = None,
    *,
    closure_owner: str = "scheduler_cleanup",
    closure_reason: str = "worker_process_missing",
) -> None:
    names = list(dict.fromkeys(str(name).strip().lstrip("@") for name in (account_names or []) if str(name).strip()))
    if not names and procman.status()["running"]:
        return
    where = (
        " AND account_name IN (" + ",".join("?" for _ in names) + ")"
        if names else ""
    )
    conn = db_conn()
    try:
        orphan_jobs = conn.execute(
            "SELECT id FROM ig_web_upload_jobs "
            "WHERE status IN ('running','starting','browser_launching','uploading','sharing','processing',"
            "'submitted_unverified','uploaded_unverified')" + where,
            names,
        ).fetchall()
        intent_job_ids = {int(job["id"]) for job in orphan_jobs if job_has_reel_publish_intent(conn, int(job["id"]))}
        clicked_job_ids = {int(job["id"]) for job in orphan_jobs if job_has_reel_share_click(conn, int(job["id"]))}
        conn.execute(
            """
            UPDATE ig_web_upload_jobs
            SET status='stopped',
                domain_outcome='stopped',
                infrastructure_outcome=?,
                closure_owner=?,
                closure_reason=?,
                current_step=CASE WHEN current_step='' THEN 'stopped' ELSE current_step END,
                last_error=CASE WHEN last_error='' THEN 'worker_process_missing' ELSE last_error END,
                finished_at=CASE WHEN finished_at='' THEN datetime('now') ELSE finished_at END,
                updated_at=datetime('now')
            WHERE status IN (
                'running','starting','browser_launching','uploading','sharing','processing',
                'submitted_unverified','uploaded_unverified'
            )
            """ + where,
            [
                str(closure_reason or "worker_process_missing"),
                str(closure_owner or "scheduler_cleanup"),
                str(closure_reason or "worker_process_missing"),
                *names,
            ],
        )
        for job in orphan_jobs:
            job_id = int(job["id"])
            if preserve_verified_publication_job(
                conn, job_id, stop_reason="user_stop_after_verified_publication"
            ):
                conn.execute(
                    "UPDATE ig_web_upload_jobs SET domain_outcome=status "
                    "WHERE id=?",
                    (job_id,),
                )
                continue
            if job_id in clicked_job_ids:
                conn.execute("UPDATE ig_web_upload_jobs SET status='uploaded_unverified',current_step='uploaded_unverified',last_error='startup_verification_required',finished_at=datetime('now'),updated_at=datetime('now') WHERE id=?", (job_id,))
                reconcile_terminal_upload_history(conn, job_id, "uploaded_unverified", "startup_verification_required")
                conn.execute(
                    "UPDATE ig_publishing_history SET next_verify_at='1970-01-01 00:00:00' "
                    "WHERE job_id=? AND status='uploaded_unverified' AND next_verify_at=''",
                    (job_id,),
                )
            elif job_id in intent_job_ids:
                conn.execute("UPDATE ig_web_upload_jobs SET status='submitted_unverified',current_step='submitted_unverified',last_error='startup_reconciliation_required',finished_at=datetime('now'),updated_at=datetime('now') WHERE id=?", (job_id,))
                reconcile_terminal_upload_history(conn, job_id, "submitted_unverified", "startup_reconciliation_required")
            else:
                reconcile_terminal_upload_history(conn, job_id, "stopped", "worker_process_missing")
            conn.execute(
                "UPDATE ig_web_upload_jobs SET domain_outcome=status "
                "WHERE id=?",
                (job_id,),
            )
        conn.commit()
    finally:
        conn.close()


def looks_totp(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z2-7]{16,}", str(value or "").strip().replace(" ", "").upper()))


def looks_session_dump(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    signals = ("ds_user_id", "csrftoken", "sessionid", "ig_did", "rur=", "mid=", "android-")
    return any(signal in lowered for signal in signals) or (len(lowered) > 180 and (";" in lowered or "|" in lowered))


def parse_bulk(raw: str) -> list[dict[str, str]]:
    parsed: dict[str, dict[str, str]] = {}
    for raw_line in str(raw or "").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in re.split(r"[|,;]", line)]
        name = parts[0].split(":")[0].strip().lstrip("@") if parts else ""
        password = ""
        proxy = ""
        totp = ""
        candidates: list[str] = []
        if any(sep in line for sep in ("|", ";", ",")):
            if len(parts) > 1 and "://" not in parts[1] and parts[1].count(":") < 2 and not looks_session_dump(parts[1]):
                password = parts[1]
                candidates = parts[2:]
            else:
                candidates = parts[1:]
        elif ":" in line:
            sub = line.split(":")
            name = sub[0].strip().lstrip("@")
            if len(sub) > 1 and not looks_session_dump(sub[1]):
                password = sub[1].strip()
            rest = ":".join(sub[2:]).strip() if len(sub) > 2 else ""
            candidates = [rest] if rest else []
        for token in candidates:
            token = token.strip()
            if not token or looks_session_dump(token):
                continue
            normalized_totp = token.replace(" ", "").upper()
            if looks_totp(normalized_totp) and not totp:
                totp = normalized_totp
            elif not proxy:
                proxy = token
        if re.fullmatch(r"[A-Za-z0-9._]{1,80}", name):
            parsed[name] = {"name": name, "password": password, "proxy": proxy, "totp": totp}
    return list(parsed.values())


@app.get("/")
@app.get("/ig-web-upload")
def index() -> FileResponse:
    return FileResponse(
        str(UI_PATH),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "sparkgrid-web-upload",
        "version": APP_VERSION,
        "process": procman.status(),
    }


@app.get("/api/system-storage")
def system_storage() -> dict[str, Any]:
    """Safe status for the UI: numeric capacity only, never local paths."""
    targets = (DATA_DIR, DEBUG_UPLOAD, DEBUG_WARMUP, PROFILE_ROOT, DATA_DIR / "tmp")
    result = preflight(targets, DEFAULT_RESERVE_BYTES)
    return {"ok": bool(result.get("ok")), "storage": result, "paused": system_status()}


@app.get("/api/process-status")
def process_status() -> dict[str, Any]:
    return {"ok": True, **procman.status()}


@app.get("/api/ig-web-upload/overview")
def overview() -> dict[str, Any]:
    ensure_schema()
    mark_orphan_jobs_stopped()
    conn = db_conn()
    try:
        rows = conn.execute(
            """
            SELECT a.name, a.status, a.proxy, a.web_connection_id,
                   COALESCE(c.name,'Direct') AS web_connection_name,
                   COALESCE(c.connection_type,'direct') AS web_connection_type,
                   COALESCE(c.last_status,'ready') AS web_connection_status,
                   COALESCE(c.last_error,'') AS web_connection_error,
                   a.web_upload_profile_status, a.web_upload_login_status,
                   web_upload_cookie_status, web_upload_last_error,
                   web_upload_last_upload_at, web_upload_cooldown_until,
                   COALESCE(a.created_at,'') AS created_at,
                   web_upload_content_mode, COALESCE(web_upload_quality_niche,'') AS web_upload_quality_niche,
                   COALESCE(web_upload_scale_niche,'') AS web_upload_scale_niche,
                   web_upload_cycle_count, web_upload_next_cycle_at,
                   COALESCE(web_privacy_status,'unchecked') AS web_privacy_status,
                   COALESCE(web_privacy_checked_at,'') AS web_privacy_checked_at,
                   COALESCE(web_privacy_last_error,'') AS web_privacy_last_error,
                   COALESCE(web_professional_status,'unchecked') AS web_professional_status,
                   COALESCE(web_professional_checked_at,'') AS web_professional_checked_at,
                   COALESCE(web_professional_category,'') AS web_professional_category,
                   COALESCE(web_professional_last_error,'') AS web_professional_last_error,
                   COALESCE(web_upload_traffic_total,0) AS web_upload_traffic_total,
                   COALESCE(web_upload_traffic_last,0) AS web_upload_traffic_last
            FROM accounts a
            LEFT JOIN web_connections c ON c.id=a.web_connection_id
            WHERE COALESCE(a.enabled,1)=1
              AND COALESCE(a.warm_only,0)=0
              AND COALESCE(a.web_upload_enabled,1)=1
            ORDER BY a.name LIMIT 2000
            """
        ).fetchall()
        accounts = [dict(row) for row in rows]
        shared_rows = conn.execute(
            "SELECT COALESCE(content_kind,'scale') AS kind, COUNT(*) AS c FROM api_content_assets WHERE status='ready' AND account_name='' GROUP BY COALESCE(content_kind,'scale')"
        ).fetchall()
        shared_by_kind = {str(row["kind"] or "scale"): int(row["c"] or 0) for row in shared_rows}
        count_rows = conn.execute(
            "SELECT account_name, COALESCE(content_kind,'scale') AS kind, COUNT(*) AS c FROM api_content_assets WHERE status='ready' AND account_name!='' GROUP BY account_name, COALESCE(content_kind,'scale')"
        ).fetchall()
        by_account_kind = {(row["account_name"], str(row["kind"] or "scale")): int(row["c"] or 0) for row in count_rows}
        summaries = plan_summaries(conn, [account["name"] for account in accounts], preview_limit=4)
        for account in accounts:
            name = account["name"]
            own_scale = by_account_kind.get((name, "scale"), 0)
            own_quality = by_account_kind.get((name, "quality"), 0)
            shared_scale = shared_by_kind.get("scale", 0)
            shared_quality = shared_by_kind.get("quality", 0)
            account["web_ready_scale_content"] = shared_scale + own_scale
            account["web_ready_quality_content"] = shared_quality + own_quality
            account["web_account_scale_content"] = own_scale
            account["web_account_quality_content"] = own_quality
            mode = str(account.get("web_upload_content_mode") or "scale")
            account["web_ready_content"] = account["web_ready_quality_content"] if mode == "quality" else account["web_ready_scale_content"]
            account["web_account_content"] = own_quality if mode == "quality" else own_scale
            account["web_shared_content"] = shared_quality if mode == "quality" else shared_scale
            account["web_profile_exists"] = profile_dir_for(account["name"]).exists()
            plan = summaries.get(account["name"], {})
            for key, value in plan.items():
                account["plan_" + key] = value
            scale_progress = latest_scale_progress(conn, name)
            for key, value in scale_progress.items():
                account["scale_run_" + key] = value
            for preview in account.get("plan_next_assets", []):
                preview["url"] = f"/api/ig-web-upload/content-file/{int(preview.get('asset_id') or 0)}"
        jobs = [dict(row) for row in conn.execute("SELECT * FROM ig_web_upload_jobs ORDER BY id DESC LIMIT 100").fetchall()]
        ready_content = conn.execute("SELECT COUNT(*) AS c FROM api_content_assets WHERE status='ready'").fetchone()["c"]
        return {
            "ok": True,
            "accounts": accounts,
            "jobs": jobs,
            "task_receipts": recent_receipts(100),
            "ready_content": int(ready_content or 0),
            "latest": latest_dump(),
            "process": procman.status(),
            "settings": upload_settings(conn),
            "connections": list_connections(conn),
            "connection_groups": list_proxy_groups(conn),
        }
    finally:
        conn.close()


@app.get("/api/ig-web-upload/settings")
def get_upload_settings() -> dict[str, Any]:
    ensure_schema()
    return {"ok": True, **upload_settings()}


@app.post("/api/ig-web-upload/settings")
async def set_upload_settings(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    current = upload_settings()
    engine = str(body.get("upload_engine") or body.get("engine") or current["upload_engine"])
    parallel = body.get("api_parallel") if body.get("api_parallel") not in (None, "") else current["api_parallel"]
    browser_parallel = body.get("browser_parallel") if body.get("browser_parallel") not in (None, "") else current["browser_parallel"]
    traffic_saver = str(body.get("traffic_saver") or current.get("traffic_saver") or "off")
    warmup_enabled = str(body.get("warmup_enabled") or current.get("warmup_enabled") or "on")
    saved = save_upload_settings(engine, int(parallel), int(browser_parallel), traffic_saver, warmup_enabled)
    return JSONResponse({"ok": True, **saved})


@app.post("/api/ig-web-upload/import-accounts")
async def import_accounts(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    parsed = parse_bulk(body.get("accounts") or "")
    if not parsed:
        return response_error("No valid accounts found")
    try:
        quality_niche = clean_niche_name(body.get("quality_niche") or "")
        scale_niche = clean_niche_name(body.get("scale_niche") or "")
    except ValueError as exc:
        return response_error(str(exc))
    content_mode = str(body.get("content_mode") or "").strip().lower()
    if content_mode not in {"scale", "quality"}:
        content_mode = ""

    connection_mode = str(body.get("connection_mode") or "legacy").strip().lower()
    aliases = {"static_available": "static", "mobile_connection": "mobile", "set_later": "later"}
    connection_mode = aliases.get(connection_mode, connection_mode)
    if connection_mode not in {"legacy", "inline", "direct", "later", "static", "mobile"}:
        return response_error("Unknown connection option")
    try:
        requested_connection_id = int(body.get("connection_id") or 0)
    except Exception:
        requested_connection_id = 0

    conn = db_conn()
    created = updated = 0
    existing_names: set[str] = set()
    names = [item["name"] for item in parsed]
    connection_result: dict[str, Any] = {
        "mode": connection_mode,
        "assigned": 0,
        "unassigned": 0,
        "static_imported": 0,
        "connection_name": "",
    }
    try:
        for item in parsed:
            # Inline proxies remain supported for old import files. The new Bulk Add
            # connection step takes precedence whenever a mode is selected.
            item_proxy = item["proxy"] if connection_mode in {"legacy", "inline"} else ""
            exists = conn.execute("SELECT 1 FROM accounts WHERE name=?", (item["name"],)).fetchone()
            if exists:
                existing_names.add(item["name"])
                conn.execute(
                    """
                    UPDATE accounts SET
                        password=CASE WHEN ?!='' THEN ? ELSE password END,
                        api_password=CASE WHEN ?!='' THEN ? ELSE api_password END,
                        api_totp_secret=CASE WHEN ?!='' THEN ? ELSE api_totp_secret END,
                        proxy=CASE WHEN ?!='' THEN ? ELSE proxy END,
                        enabled=1, warm_only=0, status='ready',
                        web_upload_enabled=1, web_upload_mode='desktop',
                        web_upload_content_mode=CASE WHEN ?!='' THEN ? ELSE web_upload_content_mode END,
                        web_upload_quality_niche=CASE WHEN ?!='' THEN ? ELSE web_upload_quality_niche END,
                        web_upload_scale_niche=CASE WHEN ?!='' THEN ? ELSE web_upload_scale_niche END,
                        updated_at=datetime('now')
                    WHERE name=?
                    """,
                    (
                        item["password"], item["password"], item["password"], item["password"],
                        item["totp"], item["totp"], item_proxy, item_proxy,
                        content_mode, content_mode, quality_niche, quality_niche,
                        scale_niche, scale_niche, item["name"],
                    ),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO accounts(name,password,api_password,api_totp_secret,proxy,status,web_upload_enabled,web_upload_content_mode,web_upload_quality_niche,web_upload_scale_niche,created_at)
                    VALUES (?,?,?,?,?,'ready',1,?,?,?,datetime('now'))
                    """,
                    (item["name"], item["password"], item["password"], item["totp"], item_proxy, content_mode or "scale", quality_niche, scale_niche),
                )
                created += 1
            if item_proxy:
                assign_legacy_proxy_connection(conn, item["name"], item_proxy)

        if quality_niche:
            conn.execute(
                "INSERT OR IGNORE INTO quality_niches(name,updated_at) VALUES (?,datetime('now'))",
                (quality_niche,),
            )
        if scale_niche:
            conn.execute(
                "INSERT OR IGNORE INTO scale_niches(name,updated_at) VALUES (?,datetime('now'))",
                (scale_niche,),
            )
        conn.commit()

        if connection_mode == "direct":
            connection_id = direct_connection_id(conn)
            connection_result["assigned"] = assign_connection(conn, names, connection_id)
            connection_result["connection_name"] = "Direct"
        elif connection_mode == "later":
            # "Set later" must never erase a saved proxy from an existing
            # account when the same account is imported again. Only newly
            # created accounts receive the neutral Direct assignment.
            new_names = [name for name in names if name not in existing_names]
            if new_names:
                assign_connection(conn, new_names, direct_connection_id(conn))
            connection_result["assigned"] = 0
            connection_result["unassigned"] = len(new_names)
            connection_result["connection_name"] = "Keep existing"
        elif connection_mode == "mobile":
            connection = get_connection(conn, requested_connection_id) if requested_connection_id else None
            if not connection or str(connection.get("connection_type") or "") not in {"mobile", "phone"}:
                return response_error("Choose a saved mobile connection")
            connection_result["assigned"] = assign_connection(conn, names, int(connection["id"]))
            connection_result["connection_name"] = str(connection.get("name") or "Mobile")
        elif connection_mode == "static":
            imported = import_static_connections(
                conn,
                str(body.get("static_proxies") or ""),
                str(body.get("static_prefix") or "Static"),
                str(body.get("static_group_name") or body.get("static_prefix") or "Static proxies"),
            ) if str(body.get("static_proxies") or "").strip() else []
            connection_result["static_imported"] = len(imported)
            # Explicitly pasted proxies belong to this import and are assigned in
            # line order. Only use the saved free pool when no new proxies were
            # supplied, so Bulk Add never unexpectedly consumes an older slot.
            static_group_id = int(body.get("static_group_id") or 0)
            assignment_connections = imported if imported else available_static_connections(conn, static_group_id)
            assigned_names: list[str] = []
            for account_name, connection in zip(names, assignment_connections):
                assign_connection(conn, [account_name], int(connection["id"]))
                assigned_names.append(account_name)
            remaining = [name for name in names if name not in set(assigned_names)]
            if remaining:
                placeholders = ",".join("?" for _ in remaining)
                conn.execute(
                    f"UPDATE accounts SET status='proxy_required',web_upload_last_error='proxy_required: no free static proxy in selected group',updated_at=datetime('now') WHERE name IN ({placeholders})",
                    remaining,
                )
                conn.commit()
            connection_result["assigned"] = len(assigned_names)
            connection_result["unassigned"] = len(remaining)
            connection_result["connection_name"] = "Static proxies"

        return JSONResponse({
            "ok": True,
            "created": created,
            "updated": updated,
            "total": len(parsed),
            "accounts": names,
            "quality_niche": quality_niche,
            "scale_niche": scale_niche,
            "content_mode": content_mode,
            "connection": connection_result,
        })
    except (ValueError, sqlite3.Error) as exc:
        return response_error(f"Static proxy import failed: {exc}")
    finally:
        conn.close()


@app.post("/api/ig-web-upload/add-account")
async def add_account(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    name = str(body.get("username") or body.get("name") or "").strip().lstrip("@")
    password = str(body.get("password") or "")
    totp = str(body.get("totp") or body.get("totp_secret") or "").replace(" ", "").upper()
    proxy = str(body.get("proxy") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._]{1,80}", name):
        return response_error("Enter a valid Instagram username")
    if not password:
        return response_error("Password is required for Auto Login")
    if totp and not looks_totp(totp):
        return response_error("2FA must be the permanent Base32 secret, not a 6-digit code")
    conn = db_conn()
    try:
        exists = conn.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone()
        conn.execute(
            """
            INSERT INTO accounts(name,password,api_password,api_totp_secret,proxy,status,web_upload_enabled,created_at)
            VALUES (?,?,?,?,?,'ready',1,datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
                password=excluded.password,
                api_password=excluded.api_password,
                api_totp_secret=excluded.api_totp_secret,
                proxy=excluded.proxy,
                enabled=1,
                warm_only=0,
                status='ready',
                web_upload_enabled=1,
                web_upload_last_error='',
                updated_at=datetime('now')
            """,
            (name, password, password, totp, proxy),
        )
        conn.commit()
        connection = assign_legacy_proxy_connection(conn, name, proxy)
        return JSONResponse({"ok": True, "created": not bool(exists), "username": name, "has_2fa": bool(totp), "has_proxy": bool(proxy), "connection_id": int(connection.get("id") or 0)})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/remove-selected")
async def remove_selected(request: Request) -> JSONResponse:
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    conn = db_conn()
    try:
        placeholders = ",".join("?" for _ in names)
        cur = conn.execute(
            f"UPDATE accounts SET web_upload_enabled=0, web_upload_last_error='', updated_at=datetime('now') WHERE name IN ({placeholders})",
            names,
        )
        conn.commit()
        return JSONResponse({"ok": True, "removed": int(cur.rowcount or 0), "accounts": names})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/set-proxy")
async def set_proxy(request: Request) -> JSONResponse:
    body = await request.json()
    name = str(body.get("account") or body.get("name") or "").strip().lstrip("@")
    proxy = str(body.get("proxy") or "").strip()
    if not name:
        return response_error("account is required")
    conn = db_conn()
    try:
        exists = conn.execute("SELECT 1 FROM accounts WHERE name=? AND web_upload_enabled=1", (name,)).fetchone()
        if not exists:
            return response_error("account not found", 404)
        try:
            connection = assign_legacy_proxy_connection(conn, name, proxy)
        except ValueError as exc:
            return response_error(str(exc))
        return JSONResponse({"ok": True, "account": name, "proxy": proxy, "connection_id": int(connection.get("id") or 0), "connection_name": str(connection.get("name") or "Direct")})
    finally:
        conn.close()




@app.get("/api/ig-web-upload/connections")
def get_connections() -> dict[str, Any]:
    ensure_schema()
    conn = db_conn()
    try:
        return {"ok": True, "connections": list_connections(conn), "groups": list_proxy_groups(conn)}
    finally:
        conn.close()


@app.get("/api/ig-web-upload/connections/{connection_id}")
def get_connection_details(connection_id: int):
    """Return full saved values only when the local editor explicitly opens."""
    ensure_schema()
    conn = db_conn()
    try:
        saved = get_connection(conn, connection_id)
        if not saved:
            return response_error("connection not found", 404)
        detail = connection_payload(saved)
        detail["proxy_url"] = str(saved.get("proxy_url") or "")
        detail["rotation_url"] = str(saved.get("rotation_url") or "")
        return {"ok": True, "connection": detail}
    finally:
        conn.close()


@app.post("/api/ig-web-upload/connections")
async def save_connection(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    conn = db_conn()
    try:
        try:
            saved = upsert_connection(conn, dict(body))
            assign_names = clean_names(body.get("assign_accounts"))
            assigned = assign_connection(conn, assign_names, int(saved.get("id") or 0)) if assign_names else 0
        except (ValueError, sqlite3.IntegrityError) as exc:
            return response_error(str(exc))
        return JSONResponse({
            "ok": True,
            "connection": connection_payload(saved),
            "reused": bool(saved.get("_reused")),
            "assigned": assigned,
            "accounts": assign_names,
        })
    finally:
        conn.close()


@app.post("/api/ig-web-upload/connections/import-static")
async def import_static_proxy_connections(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    conn = db_conn()
    try:
        created = import_static_connections(
            conn,
            str(body.get("proxies") or ""),
            str(body.get("prefix") or "Static"),
            str(body.get("group_name") or body.get("prefix") or "Static proxies"),
        )
        if not created:
            return response_error("Add at least one proxy")
        account_names = clean_names(body.get("accounts"))
        assigned = 0
        if account_names:
            for index, (account_name, connection) in enumerate(zip(account_names, created), start=1):
                connection_id = int(connection.get("id") or 0)
                occupied = conn.execute(
                    "SELECT name FROM accounts WHERE web_connection_id=? AND name<>? AND COALESCE(web_upload_enabled,1)=1 LIMIT 1",
                    (connection_id, account_name),
                ).fetchone()
                if occupied:
                    raise ValueError(
                        f"Static proxy #{index} is already assigned to {occupied['name']}. "
                        "Use another proxy or delete its old assignment first."
                    )
                assigned += assign_connection(conn, [account_name], connection_id)
        return JSONResponse({"ok": True, "created": len(created), "assigned": assigned, "connections": [connection_payload(item) for item in created]})
    except Exception as exc:
        # Never turn an individual proxy/database compatibility problem into
        # an opaque HTTP 500. The UI receives the exact safe exception type
        # and message so the failing line can be corrected immediately.
        return response_error(f"Static proxy import failed ({type(exc).__name__}): {exc}")
    finally:
        conn.close()


@app.get("/api/ig-web-upload/connection-groups")
def get_static_proxy_groups() -> dict[str, Any]:
    ensure_schema()
    conn = db_conn()
    try:
        return {"ok": True, "groups": list_proxy_groups(conn)}
    finally:
        conn.close()


@app.post("/api/ig-web-upload/connection-groups/{group_id}/assign")
async def assign_accounts_from_static_group(group_id: int, request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    conn = db_conn()
    try:
        group = conn.execute(
            "SELECT id,name FROM proxy_groups WHERE id=? AND enabled=1",
            (int(group_id),),
        ).fetchone()
        if not group:
            return response_error("Static proxy group not found", 404)
        result = assign_static_group(conn, names, int(group_id))
        return JSONResponse({"ok": True, "group_id": int(group_id), "group_name": str(group["name"]), **result})
    finally:
        conn.close()


@app.delete("/api/ig-web-upload/connection-groups/{group_id}")
def delete_static_proxy_group(group_id: int) -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        result = delete_proxy_group(conn, int(group_id))
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        return response_error(str(exc), 400)
    finally:
        conn.close()


@app.post("/api/ig-web-upload/connection-groups")
async def create_proxy_group_endpoint(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    conn = db_conn()
    try:
        result = create_proxy_group(conn, str(body.get("name") or ""))
        return JSONResponse({"ok": True, "group": result})
    except ValueError as exc:
        return response_error(str(exc), 400)
    finally:
        conn.close()


@app.patch("/api/ig-web-upload/connection-groups/{group_id}")
async def rename_proxy_group_endpoint(group_id: int, request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    conn = db_conn()
    try:
        result = rename_proxy_group(conn, int(group_id), str(body.get("name") or ""))
        return JSONResponse({"ok": True, "group": result})
    except ValueError as exc:
        return response_error(str(exc), 400)
    finally:
        conn.close()


@app.post("/api/ig-web-upload/connection-groups/{group_id}/proxies")
async def add_proxies_endpoint(group_id: int, request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    conn = db_conn()
    try:
        created = add_proxies_to_group(
            conn,
            int(group_id),
            str(body.get("proxies") or ""),
            str(body.get("prefix") or "Static"),
        )
        return JSONResponse({"ok": True, "created": len(created), "connections": [connection_payload(item) for item in created]})
    except ValueError as exc:
        return response_error(str(exc), 400)
    finally:
        conn.close()


@app.delete("/api/ig-web-upload/connections/{connection_id}")
def delete_proxy_connection(connection_id: int) -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        result = delete_proxy_from_group(conn, int(connection_id))
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        return response_error(str(exc), 400)
    finally:
        conn.close()


@app.post("/api/ig-web-upload/connections/assign")
async def assign_account_connection(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    try:
        connection_id = int(body.get("connection_id") or 0)
    except Exception:
        return response_error("Choose a connection")
    conn = db_conn()
    try:
        try:
            updated = assign_connection(conn, names, connection_id)
        except ValueError as exc:
            return response_error(str(exc))
        return JSONResponse({"ok": True, "updated": updated, "accounts": names, "connection_id": connection_id})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/connections/{connection_id}/rotate")
def rotate_saved_connection(connection_id: int) -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        try:
            result = rotate_connection(conn, int(connection_id), sleep_after=False)
        except ValueError as exc:
            return response_error(str(exc))
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)
    finally:
        conn.close()


@app.delete("/api/ig-web-upload/connections/{connection_id}")
def delete_saved_connection(connection_id: int) -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        try:
            moved = remove_connection(conn, int(connection_id))
        except ValueError as exc:
            return response_error(str(exc))
        return JSONResponse({"ok": True, "moved_to_direct": moved})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/connections/{connection_id}/restore")
def restore_saved_static_connection(connection_id: int) -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        try:
            restored = restore_quarantined_connection(conn, int(connection_id))
        except ValueError as exc:
            return response_error(str(exc), 404)
        return JSONResponse({"ok": True, "connection": connection_payload(restored)})
    finally:
        conn.close()


@app.get("/api/ig-web-upload/quality-niches")
def quality_niches() -> dict[str, Any]:
    ensure_schema()
    conn = db_conn()
    try:
        rows = conn.execute(
            """
            SELECT n.id,n.name,n.created_at,n.updated_at,
                   COUNT(CASE WHEN a.web_upload_content_mode='quality' THEN 1 END) AS account_count,
                   COUNT(CASE WHEN a.web_upload_content_mode='quality' AND LOWER(COALESCE(a.web_upload_login_status,'')) LIKE '%logged_in%' THEN 1 END) AS ready_count,
                   COALESCE(SUM(CASE WHEN a.web_upload_content_mode='quality' THEN (
                       SELECT COUNT(*) FROM api_content_assets c
                       WHERE c.account_name=a.name AND c.content_kind='quality' AND c.status='ready'
                   ) ELSE 0 END),0) AS upcoming_count
            FROM quality_niches n
            LEFT JOIN accounts a ON LOWER(TRIM(COALESCE(a.web_upload_quality_niche,'')))=LOWER(n.name)
                               AND COALESCE(a.enabled,1)=1 AND COALESCE(a.web_upload_enabled,1)=1
            GROUP BY n.id,n.name,n.created_at,n.updated_at
            ORDER BY LOWER(n.name)
            """
        ).fetchall()
        unassigned = conn.execute(
            """
            SELECT COUNT(*) AS account_count,
                   COUNT(CASE WHEN LOWER(COALESCE(web_upload_login_status,'')) LIKE '%logged_in%' THEN 1 END) AS ready_count
            FROM accounts
            WHERE COALESCE(enabled,1)=1 AND COALESCE(web_upload_enabled,1)=1
              AND web_upload_content_mode='quality'
              AND TRIM(COALESCE(web_upload_quality_niche,''))=''
            """
        ).fetchone()
        return {
            "ok": True,
            "niches": [dict(row) for row in rows],
            "unassigned": dict(unassigned or {}),
        }
    finally:
        conn.close()


@app.post("/api/ig-web-upload/quality-niches")
async def mutate_quality_niches(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    action = str(body.get("action") or "create").strip().lower()
    conn = db_conn()
    try:
        if action == "create":
            try:
                name = clean_niche_name(body.get("name"), allow_empty=False)
            except ValueError as exc:
                return response_error(str(exc))
            try:
                conn.execute("INSERT INTO quality_niches(name) VALUES (?)", (name,))
            except sqlite3.IntegrityError:
                return response_error("A niche with this name already exists", 409)
            conn.commit()
            return JSONResponse({"ok": True, "action": action, "name": name})
        if action == "rename":
            try:
                old_name = clean_niche_name(body.get("old_name"), allow_empty=False)
                new_name = clean_niche_name(body.get("new_name"), allow_empty=False)
            except ValueError as exc:
                return response_error(str(exc))
            if old_name.casefold() == new_name.casefold():
                return JSONResponse({"ok": True, "action": action, "name": new_name})
            try:
                cur = conn.execute(
                    "UPDATE quality_niches SET name=?,updated_at=datetime('now') WHERE LOWER(name)=LOWER(?)",
                    (new_name, old_name),
                )
            except sqlite3.IntegrityError:
                return response_error("A niche with this name already exists", 409)
            if not cur.rowcount:
                return response_error("Niche not found", 404)
            conn.execute(
                "UPDATE accounts SET web_upload_quality_niche=?,updated_at=datetime('now') WHERE LOWER(TRIM(COALESCE(web_upload_quality_niche,'')))=LOWER(?)",
                (new_name, old_name),
            )
            conn.commit()
            return JSONResponse({"ok": True, "action": action, "old_name": old_name, "name": new_name})
        if action == "delete":
            try:
                name = clean_niche_name(body.get("name"), allow_empty=False)
            except ValueError as exc:
                return response_error(str(exc))
            cur = conn.execute("DELETE FROM quality_niches WHERE LOWER(name)=LOWER(?)", (name,))
            if not cur.rowcount:
                return response_error("Niche not found", 404)
            conn.execute(
                "UPDATE accounts SET web_upload_quality_niche='',updated_at=datetime('now') WHERE LOWER(TRIM(COALESCE(web_upload_quality_niche,'')))=LOWER(?)",
                (name,),
            )
            conn.commit()
            return JSONResponse({"ok": True, "action": action, "name": name})
        return response_error("Invalid niche action")
    finally:
        conn.close()


@app.post("/api/ig-web-upload/quality-niche/assign")
async def assign_quality_niche(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    try:
        niche = clean_niche_name(body.get("niche") or "")
    except ValueError as exc:
        return response_error(str(exc))
    conn = db_conn()
    try:
        if niche:
            conn.execute("INSERT OR IGNORE INTO quality_niches(name) VALUES (?)", (niche,))
        placeholders = ",".join("?" for _ in names)
        cur = conn.execute(
            f"UPDATE accounts SET web_upload_quality_niche=?,updated_at=datetime('now') WHERE name IN ({placeholders}) AND COALESCE(web_upload_enabled,1)=1",
            [niche, *names],
        )
        conn.commit()
        return JSONResponse({"ok": True, "updated": int(cur.rowcount or 0), "niche": niche, "accounts": names})
    finally:
        conn.close()



@app.get("/api/ig-web-upload/scale-niches")
def scale_niches() -> dict[str, Any]:
    ensure_schema()
    conn = db_conn()
    try:
        rows = conn.execute(
            """
            SELECT n.id,n.name,n.created_at,n.updated_at,
                   COUNT(CASE WHEN a.web_upload_content_mode='scale' THEN 1 END) AS account_count,
                   COUNT(CASE WHEN a.web_upload_content_mode='scale' AND LOWER(COALESCE(a.web_upload_login_status,'')) LIKE '%logged_in%' THEN 1 END) AS ready_count
            FROM scale_niches n
            LEFT JOIN accounts a ON LOWER(TRIM(COALESCE(a.web_upload_scale_niche,'')))=LOWER(n.name)
                               AND COALESCE(a.enabled,1)=1 AND COALESCE(a.web_upload_enabled,1)=1
            GROUP BY n.id,n.name,n.created_at,n.updated_at
            ORDER BY LOWER(n.name)
            """
        ).fetchall()
        unassigned = conn.execute(
            """
            SELECT COUNT(*) AS account_count,
                   COUNT(CASE WHEN LOWER(COALESCE(web_upload_login_status,'')) LIKE '%logged_in%' THEN 1 END) AS ready_count
            FROM accounts
            WHERE COALESCE(enabled,1)=1 AND COALESCE(web_upload_enabled,1)=1
              AND web_upload_content_mode='scale'
              AND TRIM(COALESCE(web_upload_scale_niche,''))=''
            """
        ).fetchone()
        return {"ok": True, "niches": [dict(row) for row in rows], "unassigned": dict(unassigned or {})}
    finally:
        conn.close()


@app.post("/api/ig-web-upload/scale-niches")
async def mutate_scale_niches(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    action = str(body.get("action") or "create").strip().lower()
    conn = db_conn()
    try:
        if action == "create":
            try:
                name = clean_niche_name(body.get("name"), allow_empty=False)
            except ValueError as exc:
                return response_error(str(exc))
            try:
                conn.execute("INSERT INTO scale_niches(name) VALUES (?)", (name,))
            except sqlite3.IntegrityError:
                return response_error("A niche with this name already exists", 409)
            conn.commit()
            return JSONResponse({"ok": True, "action": action, "name": name})
        if action == "rename":
            try:
                old_name = clean_niche_name(body.get("old_name"), allow_empty=False)
                new_name = clean_niche_name(body.get("new_name"), allow_empty=False)
            except ValueError as exc:
                return response_error(str(exc))
            if old_name.casefold() == new_name.casefold():
                return JSONResponse({"ok": True, "action": action, "name": new_name})
            try:
                cur = conn.execute(
                    "UPDATE scale_niches SET name=?,updated_at=datetime('now') WHERE LOWER(name)=LOWER(?)",
                    (new_name, old_name),
                )
            except sqlite3.IntegrityError:
                return response_error("A niche with this name already exists", 409)
            if not cur.rowcount:
                return response_error("Niche not found", 404)
            conn.execute(
                "UPDATE accounts SET web_upload_scale_niche=?,updated_at=datetime('now') WHERE LOWER(TRIM(COALESCE(web_upload_scale_niche,'')))=LOWER(?)",
                (new_name, old_name),
            )
            conn.commit()
            return JSONResponse({"ok": True, "action": action, "old_name": old_name, "name": new_name})
        if action == "delete":
            try:
                name = clean_niche_name(body.get("name"), allow_empty=False)
            except ValueError as exc:
                return response_error(str(exc))
            cur = conn.execute("DELETE FROM scale_niches WHERE LOWER(name)=LOWER(?)", (name,))
            if not cur.rowcount:
                return response_error("Niche not found", 404)
            conn.execute(
                "UPDATE accounts SET web_upload_scale_niche='',updated_at=datetime('now') WHERE LOWER(TRIM(COALESCE(web_upload_scale_niche,'')))=LOWER(?)",
                (name,),
            )
            conn.commit()
            return JSONResponse({"ok": True, "action": action, "name": name})
        return response_error("Invalid niche action")
    finally:
        conn.close()


@app.post("/api/ig-web-upload/scale-niche/assign")
async def assign_scale_niche(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    try:
        niche = clean_niche_name(body.get("niche") or "")
    except ValueError as exc:
        return response_error(str(exc))
    conn = db_conn()
    try:
        if niche:
            conn.execute("INSERT OR IGNORE INTO scale_niches(name) VALUES (?)", (niche,))
        placeholders = ",".join("?" for _ in names)
        cur = conn.execute(
            f"UPDATE accounts SET web_upload_scale_niche=?,updated_at=datetime('now') WHERE name IN ({placeholders}) AND web_upload_content_mode='scale' AND COALESCE(web_upload_enabled,1)=1",
            [niche, *names],
        )
        conn.commit()
        return JSONResponse({"ok": True, "updated": int(cur.rowcount or 0), "niche": niche, "accounts": names})
    finally:
        conn.close()


@app.get("/api/ig-web-upload/accounts/banned")
def banned_accounts() -> JSONResponse:
    """Preview only the current, explicitly removable Banned account states."""
    ensure_schema()
    conn = db_conn()
    try:
        names = current_banned_account_names(conn)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "count": len(names), "accounts": names})


@app.post("/api/ig-web-upload/accounts/delete-banned")
def delete_banned_accounts() -> JSONResponse:
    """Delete the backend-selected current Banned set, with no client fallback."""
    ensure_schema()
    if procman.status()["running"]:
        return response_error("Stop the current task before deleting accounts", 409)
    conn = db_conn()
    try:
        names = current_banned_account_names(conn)
    finally:
        conn.close()
    failed = []
    proxies_deleted = 0
    for name in names:
        response = delete_account(name)
        if response.status_code == 200:
            try:
                body = response.body
                import json as _json
                data = _json.loads(body) if isinstance(body, (bytes, bytearray)) else body
                if isinstance(data, dict) and data.get("proxy_deleted"):
                    proxies_deleted += 1
            except Exception:
                pass
        else:
            failed.append(name)
    return JSONResponse({"ok": True, "selected": len(names), "deleted": len(names) - len(failed), "failed": failed, "proxies_deleted": proxies_deleted})


@app.delete("/api/ig-web-upload/accounts/{account_name}")
def delete_account(account_name: str) -> JSONResponse:
    ensure_schema()
    name = str(account_name or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,80}", name):
        return response_error("Invalid account name")
    if procman.status()["running"]:
        return response_error("Stop the current task before deleting an account", 409)

    conn = db_conn()
    try:
        if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
            return response_error("Account not found", 404)

        # --- Proxy cleanup: when a banned/blocked account is deleted, also
        # delete its assigned proxy from the pool so it can never be
        # accidentally assigned to a fresh account.  Only delete the proxy
        # if this was the sole account using it; shared proxies (multiple
        # accounts on one mobile/static connection) are left intact. ---
        from connections import ensure_connection_schema, direct_connection_id
        ensure_connection_schema(conn)
        direct_id = direct_connection_id(conn)
        account_row = conn.execute(
            "SELECT web_connection_id FROM accounts WHERE name=?", (name,)
        ).fetchone()
        connection_id = int(account_row["web_connection_id"] or 0) if account_row else 0
        proxy_deleted = False
        if connection_id and connection_id != direct_id:
            other_users = int(conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE web_connection_id=? AND name!=?",
                (connection_id, name),
            ).fetchone()[0])
            if other_users == 0:
                # No other account uses this proxy — delete it entirely.
                conn.execute("DELETE FROM web_connections WHERE id=?", (connection_id,))
                proxy_deleted = True

        set_ids = [int(row[0]) for row in conn.execute(
            "SELECT id FROM ig_account_content_plan_sets WHERE account_name=?", (name,)
        ).fetchall()]
        if set_ids:
            placeholders = ",".join("?" for _ in set_ids)
            conn.execute(f"DELETE FROM ig_account_content_plan_items WHERE set_id IN ({placeholders})", set_ids)
        deleted = {}
        for table in (
            "ig_account_content_plan_sets",
            "ig_account_content_plan_state",
            "ig_web_upload_jobs",
            "ig_publishing_history",
            "api_content_assets",
        ):
            cur = conn.execute(f"DELETE FROM {table} WHERE account_name=?", (name,))
            deleted[table] = int(cur.rowcount or 0)
        conn.execute("DELETE FROM accounts WHERE name=?", (name,))
        conn.commit()
    finally:
        conn.close()

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:90] or "account"
    removed_paths = []
    cleanup_targets = [PROFILE_ROOT / safe, CONTENT_DIR / safe]
    for run_dir in (DEBUG_UPLOAD.glob("run_*")):
        cleanup_targets.append(run_dir / safe)
    for run_dir in DEBUG_WARMUP.glob("run_*"):
        cleanup_targets.append(run_dir / safe)
    for target in cleanup_targets:
        try:
            resolved = target.resolve()
            allowed_roots = (
                PROFILE_ROOT.resolve(),
                CONTENT_DIR.resolve(),
                DEBUG_UPLOAD.resolve(),
                DEBUG_WARMUP.resolve(),
            )
            if target.exists() and any(root == resolved or root in resolved.parents for root in allowed_roots):
                shutil.rmtree(target)
                removed_paths.append(str(target))
        except Exception:
            pass
    return JSONResponse({"ok": True, "account": name, "deleted": deleted, "removed_paths": len(removed_paths), "proxy_deleted": proxy_deleted})


@app.post("/api/ig-web-upload/onboard-accounts")
async def onboard_accounts(request: Request) -> JSONResponse:
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    tasks = [task for task in (body.get("tasks") or []) if task in {"create_profiles", "auto_login", "check_login"}]
    ensure_public = bool(body.get("ensure_public"))
    convert_professional = bool(body.get("convert_professional"))
    if (ensure_public or convert_professional) and "auto_login" not in tasks:
        return response_error("Auto login is required for automatic account setup")
    if not tasks:
        return response_error("Choose at least one onboarding step")
    provider = str(body.get("provider") or "camoufox")
    if provider not in {"camoufox", "playwright"}:
        provider = "camoufox"
    parallel = max(1, min(int(body.get("parallel") or 3), 50))
    command = [
        sys.executable, "-u", str(ROOT / "quality_account_onboarding.py"),
        "--accounts", ",".join(names),
        "--tasks", ",".join(tasks),
        "--provider", provider,
        "--parallel", str(parallel),
    ]
    if ensure_public:
        command.append("--ensure-public")
    if convert_professional:
        command += ["--convert-professional", "--professional-type", str(body.get("professional_type") or "creator"),
                    "--professional-category", str(body.get("professional_category") or "Personal blog")[:80]]
        if bool(body.get("show_category")):
            command.append("--show-category")
    if bool(body.get("no_proxy")):
        command.append("--no-proxy")
    if bool(body.get("headless")):
        command.append("--headless")
    return start_process(command, f"account onboarding: {len(names)} account(s)")


@app.post("/api/ig-web-upload/set-content-mode")
async def set_content_mode(request: Request) -> JSONResponse:
    body = await request.json()
    names = clean_names(body.get("accounts"))
    mode = str(body.get("mode") or "").lower()
    if not names:
        return response_error("no accounts selected")
    if mode not in {"scale", "quality"}:
        return response_error("mode must be scale or quality")
    conn = db_conn()
    try:
        placeholders = ",".join("?" for _ in names)
        conn.execute(
            f"UPDATE accounts SET web_upload_content_mode=?, web_upload_cycle_count=0, web_upload_next_cycle_at='', updated_at=datetime('now') WHERE name IN ({placeholders})",
            [mode, *names],
        )
        conn.commit()
        return JSONResponse({"ok": True, "updated": len(names), "mode": mode})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/reset-cycle")
async def reset_cycle(request: Request) -> JSONResponse:
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("no accounts selected")
    conn = db_conn()
    try:
        placeholders = ",".join("?" for _ in names)
        conn.execute(
            f"UPDATE accounts SET web_upload_cycle_count=0, web_upload_next_cycle_at='', updated_at=datetime('now') WHERE name IN ({placeholders})",
            names,
        )
        conn.commit()
        return JSONResponse({"ok": True, "updated": len(names)})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/set-content-kind")
async def set_content_kind(request: Request) -> JSONResponse:
    body = await request.json()
    ids = [int(value) for value in body.get("ids") or [] if str(value).isdigit()]
    kind = str(body.get("kind") or "").lower()
    if not ids:
        return response_error("no content selected")
    if kind not in {"scale", "quality"}:
        return response_error("kind must be scale or quality")
    conn = db_conn()
    try:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE api_content_assets SET content_kind=?, updated_at=datetime('now') WHERE id IN ({placeholders})", [kind, *ids])
        if kind == "quality":
            rows = conn.execute(
                f"SELECT id, account_name, COALESCE(quality_position,0) AS quality_position FROM api_content_assets WHERE id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()
            next_by_account: dict[str, int] = {}
            for row in rows:
                account_name = str(row["account_name"] or "")
                if int(row["quality_position"] or 0) > 0:
                    continue
                if account_name not in next_by_account:
                    max_row = conn.execute(
                        "SELECT COALESCE(MAX(quality_position),0) AS p FROM api_content_assets WHERE account_name=? AND content_kind='quality'",
                        (account_name,),
                    ).fetchone()
                    next_by_account[account_name] = int(max_row["p"] or 0) + 10
                conn.execute("UPDATE api_content_assets SET quality_position=? WHERE id=?", (next_by_account[account_name], int(row["id"])))
                next_by_account[account_name] += 10
        conn.commit()
        return JSONResponse({"ok": True, "updated": len(ids), "kind": kind})
    finally:
        conn.close()


def _command_accounts(command: list[str]) -> list[str]:
    names: list[str] = []
    for flag in ("--accounts", "--profile"):
        try:
            value = str(command[command.index(flag) + 1])
        except (ValueError, IndexError):
            continue
        for raw in value.split(","):
            name = raw.strip().lstrip("@")
            if name and name not in names:
                names.append(name)
    return names


def _process_resources(command: list[str]) -> tuple[set[str], list[str]]:
    names = _command_accounts(command)
    if not names:
        return {"global:*"}, []
    resources = {f"account:{name.lower()}" for name in names}
    force_direct = "--no-proxy" in command
    placeholders = ",".join("?" for _ in names)
    conn = db_conn()
    try:
        rows = conn.execute(
            f"SELECT a.name,COALESCE(a.web_connection_id,0) AS connection_id "
            f"FROM accounts a WHERE a.name IN ({placeholders})", names,
        ).fetchall()
    finally:
        conn.close()
    found = {str(row["name"]).lower() for row in rows}
    if len(found) != len({name.lower() for name in names}):
        resources.add("global:*")
        return resources, names
    for row in rows:
        connection_id = int(row["connection_id"] or 0)
        if force_direct:
            resources.add("network:direct")
        else:
            resources.add(f"connection:{connection_id}" if connection_id > 0 else "network:direct")
    return resources, names


def start_process(command: list[str], label: str) -> JSONResponse:
    run_id = new_task_run_id()
    try:
        resources, accounts = _process_resources(command)
        account_refs = [
            opaque_account_ref(run_id, account) for account in accounts
        ]
        diagnostics_dir = ensure_run_diagnostics(
            run_id,
            task_category=normalize_task_category(label),
            account_refs=account_refs,
        )
        create_receipt(
            run_id,
            label,
            accounts,
            diagnostics_dir=str(diagnostics_dir),
        )
        ok, message = procman.start(
            command,
            label,
            resources=resources,
            accounts=accounts,
            run_id=run_id,
        )
        return JSONResponse({"ok": ok, "message": message, "run_id": run_id, "accounts": accounts},
                            status_code=200 if ok else 409)
    except Exception as exc:
        try:
            if not recent_receipts(1) or not any(
                item.get("run_id") == run_id for item in recent_receipts(20)
            ):
                create_receipt(run_id, label, [])
            receipt = finalize_process_exit(
                run_id,
                1,
                closure_owner="process_manager",
                closure_reason="browser_start_failed",
            )
            ensure_task_detail_rows(run_id, list(locals().get("accounts") or []), receipt)
            finalize_run_diagnostics(
                run_id,
                domain_outcome=str(receipt.get("domain_outcome") or ""),
                infrastructure_outcome=str(
                    receipt.get("infrastructure_outcome") or "browser_start_failed"
                ),
                closure_owner="process_manager",
                closure_reason="browser_start_failed",
            )
        except Exception:
            pass
        return response_error(str(exc), 500)


@app.get("/api/diagnostics/runs/{run_id}/export")
async def run_diagnostic_api(
    run_id: str, include_images: bool = False
):
    """Export a finalized run; visual evidence is opt-in."""
    try:
        output = build_run_archive(
            run_id, include_images=bool(include_images)
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return response_error(
            "Could not build run diagnostic: " + type(exc).__name__, 400
        )
    return FileResponse(
        str(output),
        media_type="application/zip",
        filename=f"SparkGrid-run-diagnostic-{int(time.time())}.zip",
    )


@app.post("/api/ig-web-upload/workflow")
async def workflow(request: Request) -> JSONResponse:
    body = await request.json()
    task = str(body.get("task") or "")
    allowed = {"create_profiles", "check_login", "warmup", "open_profile", "auto_login", "post_story", "make_public", "convert_professional", "auto_login_setup"}
    if task not in allowed:
        return response_error("Invalid workflow task")
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    names, skipped_suspended = without_suspended_accounts(names)
    if not names:
        return response_error("All selected accounts are suspended and were skipped")
    provider = str(body.get("provider") or "camoufox")
    if provider not in {"camoufox", "playwright"}:
        provider = "camoufox"
    max_workers = max(1, min(int(body.get("max_workers") or body.get("parallel") or current_browser_parallel()), 50))
    if task in {"auto_login", "check_login", "warmup", "make_public", "convert_professional", "auto_login_setup"}:
        command = [
            sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
            "--operation", "workflow",
            "--task", task,
            "--accounts", ",".join(names),
            "--minutes", str(body.get("minutes") or 8),
            "--provider", provider,
            "--parallel", str(max_workers),
            "--arrive", str(body.get("arrive") or "direct") if str(body.get("arrive") or "direct") in {"direct", "search"} else "direct",
        ]
        if task in {"convert_professional", "auto_login_setup"}:
            if task == "auto_login_setup":
                if bool(body.get("ensure_public")):
                    command.append("--ensure-public")
                if bool(body.get("convert_professional")):
                    command.append("--convert-professional")
            professional_type = str(body.get("professional_type") or "creator").lower()
            if professional_type not in {"creator", "business"}:
                return response_error("Invalid professional account type")
            category = str(body.get("professional_category") or "Personal blog").strip()[:80] or "Personal blog"
            command += ["--professional-type", professional_type, "--professional-category", category]
            if bool(body.get("show_category")):
                command.append("--show-category")
        if bool(body.get("headless")):
            command.append("--headless")
        if bool(body.get("no_proxy")):
            command.append("--no-proxy")
        if bool(body.get("skip_proxy_check")):
            command.append("--skip-proxy-check")
        return start_process(command, f"connection-aware workflow: {task} · {len(names)} account(s)")

    command = [
        sys.executable, "-u", str(ROOT / "instagram_web_profile_workflow.py"),
        "--task", task,
        "--accounts", ",".join(names),
        "--minutes", str(body.get("minutes") or 8),
        "--provider", provider,
        "--max-workers", "1",
    ]
    if task == "open_profile":
        command += ["--arrive", str(body.get("arrive") or "direct"), "--keep-open"]
    if bool(body.get("headless")):
        command.append("--headless")
    if bool(body.get("no_proxy")):
        command.append("--no-proxy")
    if bool(body.get("skip_proxy_check")):
        command.append("--skip-proxy-check")
    return start_process(command, f"workflow: {task}")


@app.post("/api/ig-web-upload/account-privacy/check")
async def account_privacy_check_api(request: Request) -> JSONResponse:
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    names, skipped_suspended = without_suspended_accounts(names)
    if not names:
        return response_error("All selected accounts are suspended and were skipped")
    command = [
        sys.executable, "-u", str(ROOT / "account_privacy.py"),
        "--accounts", ",".join(names),
    ]
    return start_process(command, f"Check account privacy · {len(names)} account(s)")


@app.post("/api/ig-web-upload/web-warmup")
async def web_warmup(request: Request) -> JSONResponse:
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    minutes = max(1.0, float(body.get("minutes") or 8))
    parallel = max(1, min(int(body.get("parallel") or current_browser_parallel()), 50))
    persona = str(body.get("persona") or "random")
    if persona not in {"generalist", "shopper", "foodie", "techie", "random"}:
        persona = "random"
    conn = db_conn()
    try:
        placeholders = ",".join("?" for _ in names)
        rows = conn.execute(f"SELECT name, proxy FROM accounts WHERE name IN ({placeholders}) AND web_upload_enabled=1 ORDER BY name", names).fetchall()
    finally:
        conn.close()
    if not rows:
        return response_error("No selected Web accounts found")
    command = [
        sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
        "--operation", "web_warmup",
        "--accounts", ",".join(str(row["name"]) for row in rows),
        "--parallel", str(parallel),
        "--minutes", str(minutes),
        "--persona", persona,
    ]
    if bool(body.get("headless")):
        command.append("--headless")
    return start_process(command, f"web warmup: {len(rows)} account(s)")


@app.post("/api/ig-web-upload/start")
async def start_upload(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    names, skipped_suspended = without_suspended_accounts(names)
    if not names:
        return response_error("All selected accounts are suspended and were skipped")
    provider = str(body.get("provider") or "camoufox")
    if provider not in {"camoufox", "playwright"}:
        provider = "camoufox"

    current = upload_settings()
    engine = str(body.get("engine") or body.get("upload_engine") or current["upload_engine"]).strip().lower()
    if engine not in {"manual", "clean_web", "api"}:
        return response_error("Work mode must be manual, clean_web or api")
    if engine == "manual":
        return response_error("Manual mode opens an account profile; it does not start automatic publishing")
    api_parallel = max(1, min(int(body.get("api_parallel") or body.get("parallel") or current["api_parallel"]), 100))
    browser_parallel = max(1, min(int(body.get("browser_parallel") or body.get("max_workers") or current["browser_parallel"]), 50))
    save_upload_settings(engine, api_parallel, browser_parallel)

    operation = "api" if engine == "api" else "clean_web"
    # Compatibility marker for the original explicit API command: "--parallel", str(api_parallel)
    # Every saved connection is one lane. Accounts sharing the same mobile
    # proxy remain sequential with rotation; distinct mobile connections and
    # static connections with unique exit IPs may run in parallel.
    lane_parallel = api_parallel if engine == "api" else browser_parallel
    current_upload_settings = upload_settings()
    warmup_is_enabled = str(current_upload_settings.get("warmup_enabled") or "on") == "on"
    # Defaults follow the persisted Settings toggle (Александр's request
    # 12.08 — a UI switch, not a bot-side hardcode). An explicit value in
    # the request body still overrides this per-call if ever needed.
    default_pre_min, default_pre_max = (1, 2) if warmup_is_enabled else (0, 0)
    default_post_min, default_post_max = (1, 3) if warmup_is_enabled else (0, 0)
    command = [
        sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
        "--operation", operation,
        "--accounts", ",".join(names),
        "--parallel", str(lane_parallel),
        "--provider", provider,
        "--caption", str(body.get("caption") or ""),
        "--worker-script", str(ROOT / ("instagram_private_web_api_upload.py" if engine == "api" else "instagram_web_upload.py")),
        "--max-workers", "1",
        "--target", str(int(body.get("target") or 1)),
        "--pre-warmup-min", str(body.get("pre_warmup_min") if body.get("pre_warmup_min") not in (None, "") else default_pre_min),
        "--pre-warmup-max", str(body.get("pre_warmup_max") if body.get("pre_warmup_max") not in (None, "") else default_pre_max),
        "--post-warmup-min", str(body.get("post_warmup_min") if body.get("post_warmup_min") not in (None, "") else default_post_min),
        "--post-warmup-max", str(body.get("post_warmup_max") if body.get("post_warmup_max") not in (None, "") else default_post_max),
        "--cooldown-hours", str(body.get("cooldown_hours") if body.get("cooldown_hours") not in (None, "") else 4),
    ]
    if bool(body.get("headless")):
        command.append("--headless")
    if bool(body.get("no_proxy")):
        command.append("--no-proxy")
    label = (f"API upload: {len(names)} account(s) · parallel {min(api_parallel, len(names))} · connection-aware" if engine == "api" else f"Clean Web upload: {len(names)} account(s) · up to {min(browser_parallel, len(names))} independent connection lane(s)")
    return start_process(command, label)




@app.get("/api/ig-web-upload/story-library")
def get_story_library() -> dict[str, Any]:
    conn = db_conn()
    try:
        ensure_story_library_schema(conn)
        rows = [dict(row) for row in conn.execute("SELECT * FROM story_library ORDER BY id DESC").fetchall()]
        settings = dict(conn.execute("SELECT * FROM story_settings WHERE id=1").fetchone())
        return {"ok": True, "images": rows, "settings": settings}
    finally:
        conn.close()


@app.post("/api/ig-web-upload/story-library")
async def add_story_library_images(request: Request) -> JSONResponse:
    form = await request.form()
    files = list(form.getlist("files"))
    if not files:
        return response_error("Choose one or more Story images")
    library_dir = STORY_DIR / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    conn = db_conn()
    created = []
    try:
        ensure_story_library_schema(conn)
        for upload in files:
            filename = str(getattr(upload, "filename", "") or "")
            extension = Path(filename).suffix.lower()
            if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                return response_error(f"{filename}: Story image must be JPG/PNG/WebP")
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)[-120:] or f"story{extension}"
            destination = library_dir / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{safe_name}"
            destination.write_bytes(await upload.read())
            cur = conn.execute(
                "INSERT INTO story_library(file_path,original_name) VALUES (?,?)",
                (str(destination), filename),
            )
            created.append({"id": int(cur.lastrowid), "file_path": str(destination), "original_name": filename})
        conn.commit()
        return JSONResponse({"ok": True, "created": created, "count": len(created)})
    finally:
        conn.close()


@app.delete("/api/ig-web-upload/story-library/{asset_id}")
def delete_story_library_image(asset_id: int) -> JSONResponse:
    conn = db_conn()
    try:
        ensure_story_library_schema(conn)
        row = conn.execute("SELECT file_path FROM story_library WHERE id=?", (int(asset_id),)).fetchone()
        if not row:
            return response_error("Story image not found", 404)
        path = Path(str(row["file_path"] or ""))
        conn.execute("DELETE FROM story_library WHERE id=?", (int(asset_id),))
        conn.commit()
        try:
            if path.is_file() and STORY_DIR in path.resolve().parents:
                path.unlink()
        except Exception:
            pass
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@app.get("/api/ig-web-upload/story-library/{asset_id}/image")
def story_library_image(asset_id: int):
    conn = db_conn()
    try:
        ensure_story_library_schema(conn)
        row = conn.execute("SELECT file_path FROM story_library WHERE id=?", (int(asset_id),)).fetchone()
    finally:
        conn.close()
    if not row or not Path(str(row["file_path"] or "")).is_file():
        return response_error("Story image not found", 404)
    return FileResponse(str(row["file_path"]))


@app.post("/api/ig-web-upload/story-settings")
async def save_story_settings(request: Request) -> JSONResponse:
    body = await request.json()
    descriptions = "\n".join(line.strip() for line in str(body.get("descriptions") or "").splitlines() if line.strip())
    link_url = str(body.get("link_url") or "").strip()
    if link_url and not re.match(r"^https?://", link_url, re.I):
        link_url = "https://" + link_url
    sticker_text = str(body.get("sticker_text") or "Open link").strip()[:80] or "Open link"
    sticker_x = max(0.05, min(float(body.get("sticker_x") or 0.5), 0.95))
    sticker_y = max(0.05, min(float(body.get("sticker_y") or 0.82), 0.95))
    highlight_name = str(body.get("highlight_name") or "").strip()[:80]
    conn = db_conn()
    try:
        ensure_story_library_schema(conn)
        conn.execute(
            "UPDATE story_settings SET descriptions=?,link_url=?,sticker_text=?,sticker_x=?,sticker_y=?,highlight_name=?,updated_at=datetime('now') WHERE id=1",
            (descriptions, link_url, sticker_text, sticker_x, sticker_y, highlight_name),
        )
        conn.commit()
        return JSONResponse({"ok": True, "descriptions": descriptions, "link_url": link_url, "sticker_text": sticker_text, "sticker_x": sticker_x, "sticker_y": sticker_y, "highlight_name": highlight_name})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/post-story")
async def post_story(request: Request) -> JSONResponse:
    form = await request.form()
    conn = db_conn()
    try:
        ensure_story_library_schema(conn)
        try:
            selection = resolve_story_account_scope(
                conn,
                form.get("niche_scope"),
                form.get("account_scope"),
                form.get("accounts"),
            )
        except ValueError as exc:
            return response_error(str(exc))
    finally:
        conn.close()
    names = list(selection["final_account_names"])
    names, skipped_suspended = without_suspended_accounts(names)
    selection["final_account_names"] = names
    if not names:
        conn = db_conn()
        try:
            ensure_story_library_schema(conn)
            cur = conn.execute(
                "INSERT INTO story_jobs(selection_json,status,last_error) VALUES(?,'empty_selection','No accounts matched the saved Story selection')",
                (json.dumps(selection, ensure_ascii=False),),
            )
            conn.commit()
            job_id = int(cur.lastrowid)
        finally:
            conn.close()
        return JSONResponse(
            {
                "ok": True,
                "started": False,
                "reason": "empty_selection",
                "message": "No accounts matched the Story selection; no workers were started",
                "story_job_id": job_id,
                "accounts": [],
            }
        )
    image = form.get("image")

    conn = db_conn()
    try:
        ensure_story_library_schema(conn)
        settings = dict(conn.execute("SELECT * FROM story_settings WHERE id=1").fetchone())
        library = [dict(row) for row in conn.execute("SELECT * FROM story_library ORDER BY id").fetchall()]
    finally:
        conn.close()
    descriptions = [line.strip() for line in str(settings.get("descriptions") or "").splitlines() if line.strip()]
    saved_link = str(settings.get("link_url") or "").strip()

    uploaded_path = ""
    if image and getattr(image, "filename", ""):
        extension = Path(image.filename).suffix.lower()
        if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
            return response_error("Story image must be JPG/PNG/WebP")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", image.filename)[-120:]
        destination = STORY_DIR / f"{int(time.time() * 1000)}_{safe_name}"
        destination.write_bytes(await image.read())
        uploaded_path = str(destination)
    valid_library = [item for item in library if Path(str(item.get("file_path") or "")).is_file()]
    if not uploaded_path and not valid_library:
        return response_error("Add one or more images to the Story library first")

    fallback_text = str(form.get("sticker_text") or settings.get("sticker_text") or "Open link").strip()[:80] or "Open link"
    link_url = str(form.get("link") or saved_link).strip()
    sticker_x = max(0.05, min(float(form.get("sticker_x") or settings.get("sticker_x") or 0.5), 0.95))
    sticker_y = max(0.05, min(float(form.get("sticker_y") or settings.get("sticker_y") or 0.82), 0.95))
    highlight_name = str(form.get("highlight_name") or settings.get("highlight_name") or "").strip()[:80]
    account_manifest = {}
    for name in names:
        chosen_path = uploaded_path or str(random.choice(valid_library)["file_path"])
        chosen_text = (random.choice(descriptions) if descriptions else fallback_text)[:80]
        account_manifest[name] = {"image": chosen_path, "link": link_url, "sticker_text": chosen_text, "sticker_x": sticker_x, "sticker_y": sticker_y, "highlight_name": highlight_name}
    manifest_dir = STORY_DIR / "jobs"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"story_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
    provider = str(form.get("provider") or "camoufox")
    if provider not in {"camoufox", "playwright"}:
        provider = "camoufox"
    launch = {
        "provider": provider,
        "headless": str(form.get("headless") or "").lower() in {"1", "true", "yes", "on"},
        "no_proxy": str(form.get("no_proxy") or "").lower() in {"1", "true", "yes", "on"},
    }
    manifest = {
        "version": 1,
        "selection": selection,
        "accounts": account_manifest,
        "launch": launch,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    conn = db_conn()
    try:
        ensure_story_library_schema(conn)
        cur = conn.execute(
            "INSERT INTO story_jobs(selection_json,manifest_path,status) VALUES(?,?,'queued')",
            (json.dumps(selection, ensure_ascii=False), str(manifest_path)),
        )
        conn.commit()
        story_job_id = int(cur.lastrowid)
    finally:
        conn.close()
    command = [
        sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
        "--operation", "story",
        "--accounts", ",".join(names),
        "--parallel", "1",
        "--provider", provider,
        "--story-manifest", str(manifest_path),
        "--image", uploaded_path,
        "--link", link_url,
        "--sticker-text", fallback_text,
        "--sticker-x", str(sticker_x),
        "--sticker-y", str(sticker_y),
        "--highlight-name", highlight_name,
        "--story-job-id", str(story_job_id),
    ]
    if launch["headless"]:
        command.append("--headless")
    if launch["no_proxy"]:
        command.append("--no-proxy")
    suffix = f"; {len(skipped_suspended)} suspended skipped" if skipped_suspended else ""
    started = start_process(command, f"story: {len(names)} account(s){suffix}")
    payload = json.loads(bytes(started.body).decode("utf-8"))
    payload.update({"started": bool(payload.get("ok")), "story_job_id": story_job_id})
    return JSONResponse(payload, status_code=started.status_code)


@app.post("/api/ig-web-upload/story-jobs/{story_job_id}/retry")
def retry_story_job(story_job_id: int) -> JSONResponse:
    conn = db_conn()
    try:
        ensure_story_library_schema(conn)
        row = conn.execute(
            "SELECT * FROM story_jobs WHERE id=?",
            (int(story_job_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return response_error("Story job not found", 404)
    manifest_path = Path(str(row["manifest_path"] or ""))
    if not manifest_path.is_file():
        return response_error("Saved Story job manifest is missing", 409)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        selection = dict(manifest.get("selection") or {})
        launch = dict(manifest.get("launch") or {})
    except Exception as exc:
        return response_error(f"Saved Story job manifest is invalid: {exc}", 409)
    names = clean_names(selection.get("final_account_names"))
    if not names:
        return JSONResponse(
            {
                "ok": True,
                "started": False,
                "reason": "empty_selection",
                "message": "Saved Story selection is empty; no workers were started",
                "story_job_id": int(story_job_id),
                "accounts": [],
            }
        )
    command = [
        sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
        "--operation", "story",
        "--accounts", ",".join(names),
        "--parallel", "1",
        "--provider", str(launch.get("provider") or "camoufox"),
        "--story-manifest", str(manifest_path),
        "--story-job-id", str(story_job_id),
    ]
    if bool(launch.get("headless")):
        command.append("--headless")
    if bool(launch.get("no_proxy")):
        command.append("--no-proxy")
    started = start_process(command, f"story retry #{story_job_id}: {len(names)} account(s)")
    payload = json.loads(bytes(started.body).decode("utf-8"))
    payload.update({"started": bool(payload.get("ok")), "story_job_id": int(story_job_id)})
    return JSONResponse(payload, status_code=started.status_code)


@app.get("/api/ig-web-upload/publishing-history")
def publishing_history(limit: int = 300, status: str = "", account: str = "") -> dict[str, Any]:
    ensure_schema()
    limit = max(1, min(int(limit or 300), 2000))
    where = []
    params: list[Any] = []
    if status.strip():
        where.append("status=?")
        params.append(status.strip().lower())
    if account.strip():
        where.append("account_name=?")
        params.append(account.strip().lstrip("@"))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = db_conn()
    try:
        ensure_history_schema(conn)
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM ig_publishing_history" + clause + " ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()]
        counts = {str(row["status"]): int(row["c"] or 0) for row in conn.execute(
            "SELECT status,COUNT(*) AS c FROM ig_publishing_history GROUP BY status"
        ).fetchall()}
        return {"ok": True, "history": rows, "counts": counts}
    finally:
        conn.close()


@app.post("/api/ig-web-upload/publishing-history/verify-due")
def verify_due_publications() -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        ensure_history_schema(conn)
        rows = conn.execute(
            "SELECT id FROM ig_publishing_history WHERE status IN ('uploaded','uploaded_unverified','submitted_unverified','processing') "
            "ORDER BY id DESC LIMIT 100"
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE ig_publishing_history SET status='processing', error='', next_verify_at='1970-01-01 00:00:00', updated_at=datetime('now') WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()
    finally:
        conn.close()
    if ids:
        _run_verifier_once_async()
    return JSONResponse({"ok": True, "queued": len(ids)})


@app.post("/api/ig-web-upload/publishing-history/{history_id}/verify")
def verify_publication(history_id: int) -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        row = history_row(conn, history_id)
        if not row:
            return response_error("Publishing-history row not found", 404)
        if not (row.get("media_id") or row.get("shortcode")):
            return response_error("This publication has no media identifier to verify")
        update_history(conn, history_id, status="processing", error="", next_verify_at="1970-01-01 00:00:00")
    finally:
        conn.close()
    _run_verifier_once_async()
    return JSONResponse({"ok": True, "history_id": history_id, "status": "processing"})


@app.post("/api/ig-web-upload/publishing-history/{history_id}/requeue")
def requeue_publication_content(history_id: int) -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        row = history_row(conn, history_id)
        if not row:
            return response_error("Publishing-history row not found", 404)
        asset_id = int(row.get("asset_id") or 0)
        file_path = str(row.get("file_path") or "")
        if not asset_id:
            return response_error("This history row is not linked to a content asset")
        if not file_path or not Path(file_path).is_file():
            return response_error("The original video file is missing")
        asset = conn.execute("SELECT id FROM api_content_assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            return response_error("The original content record no longer exists", 404)
        conn.execute(
            "UPDATE api_content_assets SET status='ready', last_error='', updated_at=datetime('now') WHERE id=?",
            (asset_id,),
        )
        conn.commit()
        return JSONResponse({"ok": True, "history_id": history_id, "asset_id": asset_id, "status": "ready"})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/publishing-history/{history_id}/retry")
def retry_publication(history_id: int) -> JSONResponse:
    ensure_schema()
    if procman.status()["running"]:
        return response_error("Stop or wait for the current upload before retrying")
    conn = db_conn()
    try:
        row = history_row(conn, history_id)
        if not row:
            return response_error("Publishing-history row not found", 404)
        if str(row.get("status") or "") not in {"failed"}:
            return response_error("Retry is available only for failed publications; use Verify again for uploaded/processing rows")
        account = str(row.get("account_name") or "")
        asset_id = int(row.get("asset_id") or 0)
        file_path = str(row.get("file_path") or "")
        if not account or not asset_id or not file_path or not Path(file_path).is_file():
            return response_error("The original account or video file is unavailable")
        engine = str(row.get("engine") or "api")
        provider = str(row.get("provider") or "camoufox")
        # A proven rejection is retryable, but it remains an immutable audit
        # attempt. Create a fresh slot so a prior Share timestamp can never be
        # erased or accidentally reused.
        retry_history_id = create_history(
            conn, job_id=0, run_id="", account_name=account,
            asset={
                "id": asset_id, "file_path": file_path,
                "original_name": str(row.get("video_name") or Path(file_path).name),
            },
            engine=engine, provider=provider,
            background_web=bool(row.get("background_web")),
            caption=str(row.get("caption") or ""),
        )
    finally:
        conn.close()

    if engine == "api":
        command = [
            sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
            "--operation", "api", "--accounts", account, "--parallel", "1",
            "--provider", provider, "--asset-id", str(asset_id),
            "--history-id", str(retry_history_id), "--ignore-cooldown",
        ]
        return start_process(command, f"Retry API publication #{retry_history_id} · {account}")

    command = [
        sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
        "--operation", "clean_web", "--accounts", account, "--parallel", "1",
        "--provider", provider, "--asset-id", str(asset_id),
        "--history-id", str(retry_history_id), "--ignore-cooldown",
        "--target", "1", "--pre-warmup-min", "0", "--pre-warmup-max", "0",
        "--post-warmup-min", "0", "--post-warmup-max", "0",
    ]
    if int(row.get("background_web") or 0):
        command.append("--headless")
    return start_process(command, f"Retry Clean Web publication #{retry_history_id} · {account}")




@app.get("/api/ig-web-upload/diagnostics/{account_name}")
async def account_diagnostic_api(account_name: str):
    name = str(account_name or "").strip().lstrip("@")
    conn = db_conn()
    try:
        exists = conn.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if not exists:
        return response_error("Account not found", 404)
    try:
        output = build_account_diagnostic(name)
        return FileResponse(str(output), media_type="application/zip", filename=output.name)
    except Exception as exc:
        return response_error(f"Could not build diagnostic: {exc}", 500)


@app.get("/api/ig-web-upload/automation-plans")
async def automation_plans_api() -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        materialize_enabled_slots(conn, days=2)
        return JSONResponse({
            "ok": True,
            "plans": list_automation_plans(conn),
            "history": slot_history(conn, 100),
        })
    finally:
        conn.close()


@app.post("/api/ig-web-upload/automation-plans")
async def save_automation_plan_api(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    conn = db_conn()
    try:
        try:
            plan = save_automation_plan(conn, dict(body or {}))
            materialize_enabled_slots(conn, days=2)
            plan = next(item for item in list_automation_plans(conn) if int(item["id"]) == int(plan["id"]))
            return JSONResponse({"ok": True, "plan": plan})
        except ValueError as exc:
            return response_error(str(exc))
    finally:
        conn.close()


@app.delete("/api/ig-web-upload/automation-plans/{plan_id}")
async def delete_automation_plan_api(plan_id: int) -> JSONResponse:
    conn = db_conn()
    try:
        return JSONResponse({"ok": True, "deleted": delete_automation_plan(conn, int(plan_id))})
    finally:
        conn.close()


@app.get("/api/ig-web-upload/automation-history")
async def automation_history_api(limit: int = 200) -> JSONResponse:
    conn = db_conn()
    try:
        return JSONResponse({"ok": True, "history": slot_history(conn, limit)})
    finally:
        conn.close()


@app.get("/api/ig-web-upload/view-analytics")
async def view_analytics_api(limit: int = 500) -> JSONResponse:
    ensure_schema()
    conn = db_conn()
    try:
        result = analytics_overview(conn, limit)
        result["mobile_connections"] = [
            {
                "id": int(item.get("id") or 0),
                "name": str(item.get("name") or "Mobile proxy"),
                "last_status": str(item.get("last_status") or ""),
                "has_rotation": bool(item.get("has_rotation")),
            }
            for item in list_connections(conn)
            if str(item.get("connection_type") or item.get("type") or "") in {"mobile", "phone"}
        ]
        result["connections"] = [
            {
                "id": int(item.get("id") or 0),
                "name": str(item.get("name") or "Connection"),
                "type": str(item.get("connection_type") or item.get("type") or "direct"),
                "has_proxy": bool(item.get("proxy_url")),
                "has_rotation": bool(item.get("has_rotation")),
                "last_status": str(item.get("last_status") or ""),
            }
            for item in list_connections(conn)
            if bool(item.get("enabled", True))
        ]
        result["ok"] = True
        result["process"] = procman.status()
        return JSONResponse(result)
    finally:
        conn.close()


@app.post("/api/ig-web-upload/view-analytics/settings")
async def view_analytics_settings_api(request: Request) -> JSONResponse:
    body = await request.json()
    conn = db_conn()
    try:
        try:
            return JSONResponse({"ok": True, "settings": save_view_settings(conn, dict(body or {}))})
        except (TypeError, ValueError) as exc:
            return response_error(str(exc))
    finally:
        conn.close()


@app.post("/api/ig-web-upload/view-analytics/run-parser-pool")
async def run_parser_pool_analytics_api() -> JSONResponse:
    command = [sys.executable, "-u", str(ROOT / "view_analytics.py"), "--parser-pool", "--force"]
    return start_process(command, "View analytics · Parser Pool API")


@app.post("/api/ig-web-upload/view-analytics/retry-parser-pool")
async def retry_parser_pool_analytics_api(request: Request) -> JSONResponse:
    body = await request.json()
    ids = [int(value) for value in (body.get("target_ids") or []) if str(value).isdigit() and int(value) > 0]
    conn = db_conn()
    try:
        updated = retry_public_targets(conn, ids)
    finally:
        conn.close()
    if not updated:
        return response_error("No Unparsed publications selected")
    command = [sys.executable, "-u", str(ROOT / "view_analytics.py"), "--parser-pool", "--force"]
    return start_process(command, f"View analytics · Parser Pool retry · {updated} publication(s)")


@app.post("/api/ig-web-upload/view-analytics/check-own-api")
async def own_api_view_analytics_api(request: Request) -> JSONResponse:
    body = await request.json()
    ids = [int(value) for value in (body.get("target_ids") or []) if str(value).isdigit() and int(value) > 0]
    conn = db_conn()
    try:
        accounts = session_accounts_for_targets(conn, ids)
    finally:
        conn.close()
    if not accounts:
        return response_error("No Unparsed working accounts are available")
    command = [
        sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
        "--operation", "analytics_session",
        "--accounts", ",".join(accounts),
        "--parallel", "1",
        "--target-ids", ",".join(str(value) for value in ids),
    ]
    return start_process(command, f"View analytics · own API · {len(accounts)} account(s)")


@app.post("/api/ig-web-upload/view-analytics/parser-accounts")
async def import_view_parser_accounts_api(request: Request) -> JSONResponse:
    body = await request.json()
    parsed = parse_bulk(str(body.get("accounts") or ""))
    if not parsed:
        return response_error("No valid parser accounts found")
    try:
        connection_id = int(body.get("connection_id") or 0)
    except Exception:
        connection_id = 0
    conn = db_conn()
    try:
        connection = get_connection(conn, connection_id) if connection_id else None
        if not connection:
            return response_error("Choose a saved connection for Parser Pool")
        result = register_parser_accounts(
            conn,
            parsed,
            int(connection.get("id") or 0),
            str(connection.get("proxy_url") or ""),
        )
        if not result.get("accounts") and result.get("conflicts"):
            return response_error("These usernames already belong to publishing accounts: " + ", ".join(result["conflicts"][:10]))
        return JSONResponse({"ok": True, **result})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/view-analytics/parser-accounts/auto-login")
async def auto_login_view_parser_accounts_api(request: Request) -> JSONResponse:
    body = await request.json()
    requested = clean_names(body.get("accounts"))
    conn = db_conn()
    try:
        available = [item for item in list_parser_accounts(conn, include_disabled=False)]
        allowed = {str(item.get("account_name") or "") for item in available}
        names = [name for name in requested if name in allowed] if requested else sorted(allowed)
        if not names:
            return response_error("No enabled parser accounts selected")
        mark_parser_logging_in(conn, names)
    finally:
        conn.close()
    command = [
        sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
        "--operation", "workflow", "--task", "auto_login",
        "--accounts", ",".join(names), "--provider", "camoufox", "--parallel", "1",
        "--include-parser-accounts",
    ]
    # Auto Login only prepares all sessions. It never starts view parsing.
    return start_process(command, f"Parser Pool · Auto login · {len(names)} account(s)")


@app.post("/api/ig-web-upload/view-analytics/parser-accounts/enabled")
async def enable_view_parser_accounts_api(request: Request) -> JSONResponse:
    body = await request.json()
    names = clean_names(body.get("accounts"))
    conn = db_conn()
    try:
        changed = set_parser_accounts_enabled(conn, names, bool(body.get("enabled")))
        return JSONResponse({"ok": True, "updated": changed})
    finally:
        conn.close()


@app.delete("/api/ig-web-upload/view-analytics/parser-accounts/{account_name}")
async def delete_view_parser_account_api(account_name: str) -> JSONResponse:
    name = str(account_name or "").strip().lstrip("@")
    conn = db_conn()
    try:
        deleted = remove_parser_account(conn, name)
    finally:
        conn.close()
    if deleted:
        try:
            target = profile_dir_for(name)
            resolved = target.resolve()
            root = DATA_DIR.resolve()
            if target.exists() and (resolved == root or root in resolved.parents):
                shutil.rmtree(target)
        except Exception:
            pass
    return JSONResponse({"ok": True, "deleted": bool(deleted), "account": name})


@app.post("/api/ig-web-upload/stop")
def stop() -> dict[str, Any]:
    before = procman.status()
    stopped = procman.stop()
    for job in list(before.get("jobs") or []):
        for account_name in list(job.get("accounts") or []):
            recovery = get_active_password_recovery(
                str(account_name), run_id=str(job.get("run_id") or "")
            )
            if recovery:
                mark_password_recovery_stopped(
                    str(account_name),
                    str(recovery.get("workflow_id") or ""),
                )
    mark_orphan_jobs_stopped(
        closure_owner="user_stop",
        closure_reason="stop_all",
    )
    return {
        "ok": True,
        "stopped": stopped,
        "was_running": bool(before.get("running")),
        "label": str(before.get("label") or ""),
        "message": "All task processes stopped" if stopped else "No active task",
    }


@app.post("/api/ig-web-upload/stop/{job_id}")
def stop_job(job_id: int) -> JSONResponse:
    stopped = procman.stop_job(job_id)
    if not stopped:
        return response_error("Task is not active", 404)
    accounts = list(stopped.get("accounts") or [])
    for account_name in accounts:
        recovery = get_active_password_recovery(
            str(account_name), run_id=str(stopped.get("run_id") or "")
        )
        if recovery:
            mark_password_recovery_stopped(
                str(account_name),
                str(recovery.get("workflow_id") or ""),
            )
    mark_orphan_jobs_stopped(
        accounts,
        closure_owner="targeted_stop",
        closure_reason="targeted_stop",
    )
    return JSONResponse({
        "ok": True,
        "stopped": True,
        "job_id": int(stopped["id"]),
        "label": str(stopped.get("label") or ""),
        "accounts": accounts,
        "message": "Task process stopped",
    })




@app.post("/api/ig-web-upload/scale-settings")
async def set_scale_settings_endpoint(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    strategy = str(body.get("strategy") or "standard").strip().lower()
    posts_per_run = max(1, min(int(body.get("posts_per_run") or 3), 100))
    asset_value = body.get("standard_asset_id")
    standard_asset_id = None if asset_value in (None, "") else int(asset_value or 0)
    preserve_asset = bool(body.get("preserve_asset", standard_asset_id is None))
    conn = db_conn()
    try:
        try:
            result = save_scale_settings(
                conn, names,
                strategy=strategy,
                posts_per_run=posts_per_run,
                standard_asset_id=standard_asset_id,
                preserve_asset=preserve_asset,
            )
        except ValueError as exc:
            return response_error(str(exc))
        return JSONResponse({"ok": True, **result})
    finally:
        conn.close()

@app.post("/api/ig-web-upload/scale-pattern/preview")
async def preview_scale_pattern_endpoint(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    conn = db_conn()
    try:
        try:
            result = preview_scale_pattern(conn, names, body.get("launches") or [])
        except ValueError as exc:
            return response_error(str(exc))
        return JSONResponse({"ok": True, **result})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/scale-pattern/apply")
async def apply_scale_pattern_endpoint(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return response_error("Select accounts first")
    conn = db_conn()
    try:
        try:
            result = apply_scale_pattern(
                conn, names, body.get("launches") or [],
                after_final=str(body.get("after_final") or "repeat"),
            )
        except ValueError as exc:
            return response_error(str(exc))
        return JSONResponse({"ok": True, **result})
    finally:
        conn.close()


@app.get("/api/ig-web-upload/content-plan")
def get_content_plan(account: str = "") -> dict[str, Any]:
    ensure_schema()
    account = account.strip().lstrip("@")
    if not account:
        return {"ok": False, "error": "account is required"}
    conn = db_conn()
    try:
        plan = get_plan(conn, account)
        assets = scale_library(conn, account)
        for item in assets:
            item["scope"] = "account" if item.get("account_name") else "shared"
            item["url"] = f"/api/ig-web-upload/content-file/{int(item['id'])}"
        for plan_set in plan["sets"]:
            for item in plan_set["items"]:
                item["url"] = f"/api/ig-web-upload/content-file/{int(item.get('asset_id') or 0)}"
        return {"ok": True, "plan": plan, "assets": assets}
    finally:
        conn.close()


@app.post("/api/ig-web-upload/content-plan")
async def set_content_plan(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    account = str(body.get("account_name") or body.get("account") or "").strip().lstrip("@")
    if not account:
        return response_error("account_name is required")
    conn = db_conn()
    try:
        try:
            plan = save_plan(
                conn, account, body.get("sets") or [],
                current_set_order=int(body.get("current_set_order") or 0),
                after_final=str(body.get("after_final") or "repeat"),
            )
        except ValueError as exc:
            return response_error(str(exc))
        for plan_set in plan["sets"]:
            for item in plan_set["items"]:
                item["url"] = f"/api/ig-web-upload/content-file/{int(item.get('asset_id') or 0)}"
        return JSONResponse({"ok": True, "plan": plan})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/content-plan/reset")
async def reset_content_plan(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    account = str(body.get("account_name") or body.get("account") or "").strip().lstrip("@")
    if not account:
        return response_error("account_name is required")
    conn = db_conn()
    try:
        plan = reset_plan_position(conn, account, int(body.get("set_order") or 0))
        return JSONResponse({"ok": True, "plan": plan})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/content-caption")
async def update_content_caption(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    try:
        asset_id = int(body.get("asset_id") or 0)
    except Exception:
        asset_id = 0
    if not asset_id:
        return response_error("asset_id is required")
    caption = str(body.get("caption") or "")
    conn = db_conn()
    try:
        row = conn.execute("SELECT id FROM api_content_assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            return response_error("content asset not found", 404)
        conn.execute(
            "UPDATE api_content_assets SET caption=?,updated_at=datetime('now') WHERE id=?",
            (caption, asset_id),
        )
        conn.commit()
        return JSONResponse({"ok": True, "asset_id": asset_id, "caption": caption})
    finally:
        conn.close()


@app.get("/api/ig-web-upload/content")
def list_content(account: str = "", all: str = "") -> dict[str, Any]:
    ensure_schema()
    account = account.strip().lstrip("@")
    show_all = all.lower() in {"1", "true", "yes", "on"}
    conn = db_conn()
    try:
        base = """
            SELECT id, account_name, file_path, original_name, caption, status,
                   content_kind, COALESCE(quality_position,0) AS quality_position,
                   created_at, updated_at, uploaded_at, last_error
            FROM api_content_assets
        """
        if show_all:
            rows = conn.execute(base + " ORDER BY CASE WHEN content_kind='quality' AND status='ready' THEN 0 ELSE 1 END, CASE WHEN content_kind='quality' AND quality_position>0 THEN quality_position ELSE id END, id LIMIT 3000").fetchall()
        else:
            rows = conn.execute(
                base + " WHERE account_name='' OR account_name=? ORDER BY CASE WHEN account_name=? THEN 0 ELSE 1 END, CASE WHEN content_kind='quality' AND status='ready' THEN 0 ELSE 1 END, CASE WHEN content_kind='quality' AND quality_position>0 THEN quality_position ELSE id END, id LIMIT 500",
                (account, account),
            ).fetchall()
        assets = []
        ready = account_ready = shared_ready = 0
        for row in rows:
            item = dict(row)
            item["exists"] = bool(item["file_path"] and Path(item["file_path"]).is_file())
            item["scope"] = "account" if item["account_name"] else "shared"
            item["url"] = f"/api/ig-web-upload/content-file/{item['id']}"
            if item["status"] == "ready":
                ready += 1
                if item["account_name"]:
                    account_ready += 1
                else:
                    shared_ready += 1
            assets.append(item)
        return {"ok": True, "account": account, "assets": assets, "ready": ready, "account_ready": account_ready, "shared_ready": shared_ready}
    finally:
        conn.close()



@app.post("/api/ig-web-upload/quality-workspace")
async def quality_workspace(request: Request) -> JSONResponse:
    """Return the visual QUALITY queues for a set of accounts in one request."""
    ensure_schema()
    body = await request.json()
    names = clean_names(body.get("accounts"))
    if not names:
        return JSONResponse({"ok": True, "accounts": {}, "shared": []})
    conn = db_conn()
    try:
        placeholders = ",".join("?" for _ in names)
        rows = conn.execute(
            f"""
            SELECT id, account_name, original_name, caption, status,
                   COALESCE(quality_position,0) AS quality_position,
                   uploaded_at, last_error, created_at, updated_at
            FROM api_content_assets
            WHERE content_kind='quality'
              AND status!='archived'
              AND account_name IN ({placeholders})
            ORDER BY account_name,
                     CASE status WHEN 'ready' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                     CASE WHEN quality_position>0 THEN quality_position ELSE id END,
                     id
            """,
            names,
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
        for row in rows:
            item = dict(row)
            item["url"] = f"/api/ig-web-upload/content-file/{int(item['id'])}"
            grouped.setdefault(str(item["account_name"] or ""), []).append(item)
        shared_rows = conn.execute(
            """
            SELECT id, account_name, original_name, caption, status,
                   COALESCE(quality_position,0) AS quality_position,
                   uploaded_at, last_error, created_at, updated_at
            FROM api_content_assets
            WHERE content_kind='quality' AND status='ready' AND account_name=''
            ORDER BY CASE WHEN quality_position>0 THEN quality_position ELSE id END, id
            LIMIT 100
            """
        ).fetchall()
        shared = []
        for row in shared_rows:
            item = dict(row)
            item["url"] = f"/api/ig-web-upload/content-file/{int(item['id'])}"
            shared.append(item)
        return JSONResponse({"ok": True, "accounts": grouped, "shared": shared})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/quality-order")
async def quality_order(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    account = str(body.get("account_name") or "").strip().lstrip("@")
    ids = [int(value) for value in body.get("asset_ids") or [] if str(value).isdigit()]
    if not account:
        return response_error("account_name is required")
    if not ids:
        return response_error("asset_ids are required")
    conn = db_conn()
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id FROM api_content_assets WHERE account_name=? AND content_kind='quality' AND status='ready' AND id IN ({placeholders})",
            [account, *ids],
        ).fetchall()
        valid = {int(row["id"]) for row in rows}
        if valid != set(ids):
            return response_error("one or more quality assets do not belong to this account or are not ready", 409)
        for index, asset_id in enumerate(ids, start=1):
            conn.execute(
                "UPDATE api_content_assets SET quality_position=?,updated_at=datetime('now') WHERE id=?",
                (index * 10, asset_id),
            )
        conn.commit()
        return JSONResponse({"ok": True, "account": account, "asset_ids": ids})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/content-status")
async def content_status(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    try:
        asset_id = int(body.get("asset_id") or 0)
    except Exception:
        asset_id = 0
    status = str(body.get("status") or "").strip().lower()
    if not asset_id:
        return response_error("asset_id is required")
    if status not in {"ready", "archived"}:
        return response_error("status must be ready or archived")
    conn = db_conn()
    try:
        row = conn.execute("SELECT id,account_name,content_kind FROM api_content_assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            return response_error("content asset not found", 404)
        quality_position = 0
        if status == "ready" and str(row["content_kind"] or "") == "quality":
            max_row = conn.execute(
                "SELECT COALESCE(MAX(quality_position),0) AS p FROM api_content_assets WHERE account_name=? AND content_kind='quality' AND status='ready'",
                (str(row["account_name"] or ""),),
            ).fetchone()
            quality_position = int(max_row["p"] or 0) + 10
        conn.execute(
            "UPDATE api_content_assets SET status=?,quality_position=CASE WHEN ?='ready' THEN ? ELSE quality_position END,updated_at=datetime('now') WHERE id=?",
            (status, status, quality_position, asset_id),
        )
        conn.commit()
        return JSONResponse({"ok": True, "asset_id": asset_id, "status": status})
    finally:
        conn.close()

@app.get("/api/ig-web-upload/content-file/{asset_id}")
def content_file(asset_id: int):
    conn = db_conn()
    try:
        row = conn.execute("SELECT file_path, original_name FROM api_content_assets WHERE id=?", (asset_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return response_error("content asset not found", 404)
    path = Path(row["file_path"])
    if not path.is_file():
        return response_error("content file missing", 404)
    return FileResponse(str(path), media_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream", filename=row["original_name"] or path.name)




@app.post("/api/ig-web-upload/content-archive")
async def distribute_content_archive(request: Request) -> JSONResponse:
    """Safely unpack a ZIP and distribute paired videos/captions to accounts."""
    ensure_schema()
    form = await request.form()
    upload = form.get("archive")
    if not upload or not getattr(upload, "filename", ""):
        return response_error("Choose a ZIP archive")
    names = clean_names(form.get("accounts"))
    names, skipped_suspended = without_suspended_accounts(names)
    if not names:
        return response_error("No active target accounts; suspended accounts were skipped")
    kind = str(form.get("content_kind") or "quality").lower()
    if kind not in {"scale", "quality"}:
        kind = "quality"
    mode = str(form.get("distribution_mode") or "even").lower()
    if mode not in {"even", "fixed", "duplicate"}:
        mode = "even"
    per = max(1, min(int(form.get("per_account") or 1), 1000))
    order = str(form.get("order") or "keep").lower()
    seed = str(form.get("order_seed") or "0")
    explicit_captions = [line.strip() for line in str(form.get("captions") or "").splitlines()]

    temp_dir = DATA_DIR / "tmp" / ("content_zip_" + uuid.uuid4().hex)
    temp_dir.mkdir(parents=True, exist_ok=True)
    archive_path = temp_dir / "upload.zip"
    written_files: list[Path] = []
    try:
        with archive_path.open("wb") as target:
            while True:
                chunk = await upload.read(8 * 1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        try:
            zf = zipfile.ZipFile(archive_path)
        except zipfile.BadZipFile:
            return response_error("The selected file is not a valid ZIP archive")
        with zf:
            members = [item for item in zf.infolist() if not item.is_dir()]
            if len(members) > 5000:
                return response_error("ZIP contains too many files (maximum 5000)")
            total_size = sum(max(0, int(item.file_size)) for item in members)
            if total_size > 50 * 1024 * 1024 * 1024:
                return response_error("ZIP expands beyond the 50 GB safety limit")
            video_exts = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
            video_members = sorted(
                [item for item in members if Path(item.filename).suffix.lower() in video_exts],
                key=lambda item: Path(item.filename).name.casefold(),
            )
            if not video_members:
                return response_error("ZIP contains no supported videos (MP4/MOV/MKV/WEBM/M4V)")
            text_members = {
                Path(item.filename).stem.casefold(): item
                for item in members
                if Path(item.filename).suffix.lower() in {".txt", ".caption"}
            }
            global_caption_member = next(
                (item for item in members if Path(item.filename).name.casefold() in {"captions.txt", "captions.csv"}),
                None,
            )
            archive_caption_lines: list[str] = []
            if global_caption_member is not None:
                archive_caption_lines = zf.read(global_caption_member).decode("utf-8-sig", errors="replace").splitlines()
            entries = []
            staging = temp_dir / "videos"
            staging.mkdir()
            for index, member in enumerate(video_members):
                original_name = Path(member.filename).name
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", original_name)[-120:] or f"video_{index}.mp4"
                source_path = staging / f"{index:05d}_{safe_name}"
                with zf.open(member) as source, source_path.open("wb") as target:
                    shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
                caption = explicit_captions[index] if index < len(explicit_captions) else ""
                if not caption:
                    sidecar = text_members.get(Path(original_name).stem.casefold())
                    if sidecar is not None:
                        caption = zf.read(sidecar).decode("utf-8-sig", errors="replace").strip()
                    elif index < len(archive_caption_lines):
                        caption = archive_caption_lines[index].strip()
                entries.append({"source": source_path, "name": original_name, "caption": caption})

        if order == "shuffle":
            random.Random(seed).shuffle(entries)
        plan = {name: [] for name in names}
        if mode == "duplicate":
            for name in names:
                plan[name] = list(entries)
        elif mode == "fixed":
            cursor = 0
            for name in names:
                for _ in range(per):
                    if not entries:
                        break
                    if kind == "scale":
                        plan[name].append(entries[cursor % len(entries)])
                        cursor += 1
                    elif cursor < len(entries):
                        plan[name].append(entries[cursor])
                        cursor += 1
        else:
            for index, entry in enumerate(entries):
                plan[names[index % len(names)]].append(entry)

        conn = db_conn()
        created = []
        try:
            next_quality = {}
            for account_name, assigned in plan.items():
                if kind == "quality":
                    row = conn.execute(
                        "SELECT COALESCE(MAX(quality_position),0) AS p FROM api_content_assets WHERE account_name=? AND content_kind='quality'",
                        (account_name,),
                    ).fetchone()
                    next_quality[account_name] = int(row["p"] or 0) + 10
                for entry in assigned:
                    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(entry["name"]))[-120:] or "video.mp4"
                    destination = CONTENT_DIR / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{safe}"
                    shutil.copy2(entry["source"], destination)
                    written_files.append(destination)
                    position = next_quality.get(account_name, 0) if kind == "quality" else 0
                    cur = conn.execute(
                        "INSERT INTO api_content_assets(account_name,file_path,original_name,caption,status,content_kind,quality_position) VALUES (?,?,?,?,'ready',?,?)",
                        (account_name, str(destination), entry["name"], entry["caption"], kind, position),
                    )
                    created.append({"id": int(cur.lastrowid), "account": account_name, "filename": entry["name"], "caption": entry["caption"]})
                    if kind == "quality":
                        next_quality[account_name] += 10
            conn.commit()
        except Exception:
            conn.rollback()
            for path in written_files:
                try:
                    path.unlink()
                except Exception:
                    pass
            raise
        finally:
            conn.close()
        summary = [{"account": name, "count": len(items)} for name, items in plan.items()]
        return JSONResponse({
            "ok": True,
            "videos": len(entries),
            "assignments": len(created),
            "accounts": len([row for row in summary if row["count"]]),
            "plan": summary,
            "skipped_suspended": skipped_suspended,
        })
    except (ValueError, OSError, zipfile.BadZipFile, sqlite3.Error) as exc:
        return response_error(f"ZIP content import failed ({type(exc).__name__}): {exc}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/api/ig-web-upload/content-upload")
async def content_upload(request: Request) -> JSONResponse:
    ensure_schema()
    form = await request.form()
    files = form.getlist("files") if hasattr(form, "getlist") else []
    if not files:
        return response_error("No files uploaded")
    account = str(form.get("account_name") or "").strip().lstrip("@")
    caption = str(form.get("caption") or "")
    kind = str(form.get("content_kind") or "scale").lower()
    if kind not in {"scale", "quality"}:
        kind = "scale"
    created = []
    conn = db_conn()
    try:
        next_quality_position = 0
        if kind == "quality":
            pos_row = conn.execute(
                "SELECT COALESCE(MAX(quality_position),0) AS p FROM api_content_assets WHERE account_name=? AND content_kind='quality'",
                (account,),
            ).fetchone()
            next_quality_position = int(pos_row["p"] or 0) + 10
        for file in files:
            filename = getattr(file, "filename", "") or "upload.bin"
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)[-120:] or "upload.bin"
            destination = CONTENT_DIR / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{safe}"
            destination.write_bytes(await file.read())
            cursor = conn.execute(
                "INSERT INTO api_content_assets(account_name,file_path,original_name,caption,status,content_kind,quality_position) VALUES (?,?,?,?,'ready',?,?)",
                (account, str(destination), filename, caption, kind, next_quality_position if kind == "quality" else 0),
            )
            created.append({"id": int(cursor.lastrowid), "file_path": str(destination), "filename": filename, "content_kind": kind})
            if kind == "quality":
                next_quality_position += 10
        conn.commit()
        return JSONResponse({"ok": True, "created": created, "count": len(created)})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/content-local")
async def content_local(request: Request) -> JSONResponse:
    ensure_schema()
    body = await request.json()
    account = str(body.get("account_name") or "").strip().lstrip("@")
    file_path = str(body.get("file_path") or "").strip()
    if not account or not file_path:
        return response_error("account_name and file_path are required")
    source = Path(file_path).expanduser()
    if not source.is_file():
        return response_error("The selected video is no longer available; choose it again")
    kind = str(body.get("content_kind") or "scale").lower()
    if kind not in {"scale", "quality"}:
        kind = "scale"
    original_name = str(body.get("original_name") or source.name or "video.mp4")
    try:
        source_resolved = source.resolve()
        content_root = CONTENT_DIR.resolve()
        if source_resolved == content_root or content_root in source_resolved.parents:
            stored_path = source_resolved
        else:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", original_name)[-120:] or "video.mp4"
            stored_path = CONTENT_DIR / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{safe}"
            shutil.copy2(source_resolved, stored_path)
    except OSError as exc:
        return response_error(f"Could not copy video into SparkGrid data ({type(exc).__name__}): {exc}")
    conn = db_conn()
    try:
        quality_position = 0
        if kind == "quality":
            pos_row = conn.execute(
                "SELECT COALESCE(MAX(quality_position),0) AS p FROM api_content_assets WHERE account_name=? AND content_kind='quality'",
                (account,),
            ).fetchone()
            quality_position = int(pos_row["p"] or 0) + 10
        cursor = conn.execute(
            """
            INSERT INTO api_content_assets(account_name,file_path,original_name,caption,status,content_kind,quality_position)
            VALUES (?,?,?,?,'ready',?,?)
            """,
            (
                account,
                str(stored_path),
                original_name,
                str(body.get("caption") or ""),
                kind,
                quality_position,
            ),
        )
        conn.commit()
        return JSONResponse({"ok": True, "id": int(cursor.lastrowid), "count": 1, "local": True, "file_path": str(stored_path)})
    finally:
        conn.close()


# ── Log viewing API ─────────────────────────────────────────────────
@app.get("/api/logs")
async def list_logs() -> dict[str, Any]:
    """List all log files with size, modification time, and error count."""
    result: list[dict[str, Any]] = []
    if not LOG_DIR.is_dir():
        return {"ok": True, "logs": []}
    for entry in sorted(LOG_DIR.rglob("*.log"), key=lambda p: str(p)):
        try:
            stat = entry.stat()
            rel = entry.relative_to(LOG_DIR).as_posix()
            error_count = 0
            try:
                with entry.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "ERROR" in line or "CRITICAL" in line:
                            error_count += 1
            except Exception:
                pass
            result.append({
                "file": rel,
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "errors": error_count,
            })
        except Exception:
            pass
    return {"ok": True, "logs": result, "total": len(result)}


@app.get("/api/logs/{category}")
async def read_log(category: str, lines: int = 100) -> dict[str, Any]:
    """Read the last N lines from a log file by category."""
    lines = max(1, min(int(lines), 500))
    category_map = {
        "errors": LOG_DIR / "errors-combined.log",
        "server": LOG_DIR / "server" / "server.log",
        "automation": LOG_DIR / "automation" / "automation.log",
        "browser": LOG_DIR / "browser" / "browser.log",
        "warmup": LOG_DIR / "warmup" / "warmup.log",
        "verifier": LOG_DIR / "verifier" / "verifier.log",
        "background": LOG_DIR / "background" / "background.log",
        "analytics": LOG_DIR / "analytics" / "analytics.log",
        "proxy": LOG_DIR / "proxy" / "proxy.log",
        "onboarding": LOG_DIR / "onboarding" / "onboarding.log",
        "playwright_guard": LOG_DIR / "playwright_guard" / "playwright_guard.log",
        "api-errors": LOG_DIR / "server-errors.log",
    }
    file_path = category_map.get(category)
    if file_path is None:
        candidate = LOG_DIR / category
        if candidate.suffix != ".log":
            candidate = candidate.with_suffix(".log")
        file_path = candidate
    if not file_path.exists():
        return {"ok": False, "error": f"Log file not found: {category}", "path": str(file_path)}
    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return {
                "ok": True,
                "file": str(file_path.relative_to(LOG_DIR)).replace("\\", "/"),
                "total_lines": len(all_lines),
                "showing": len(tail),
                "content": "".join(tail),
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ─── Ads Power Metrics Dashboard ─────────────────────────────────────────────

@app.get("/api/ig-web-upload/metrics/overview")
def metrics_overview(hours: int = 24) -> dict[str, Any]:
    """Overview metrics for all accounts with delta."""
    try:
        import ads_power_checker
        conn = db_conn()
        try:
            data = ads_power_checker.get_overview(conn, hours=hours)
            return {"ok": True, **data}
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/ig-web-upload/metrics/{account_name}")
def metrics_account(account_name: str, hours: int = 168) -> dict[str, Any]:
    """Time-series history for one account."""
    try:
        import ads_power_checker
        conn = db_conn()
        try:
            history = ads_power_checker.get_account_history(conn, account_name, hours=hours)
            return {"ok": True, "account": account_name, "history": history}
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/ig-web-upload/metrics/config")
async def metrics_config(request: Request) -> JSONResponse:
    """Set Ads Power config (parser profiles, api url, enabled)."""
    body = await request.json()
    conn = db_conn()
    try:
        import ads_power_checker
        ads_power_checker.ensure_metrics_schema(conn)
        if "parser_profiles" in body:
            ads_power_checker.set_config(conn, "parser_profiles", str(body["parser_profiles"]))
        if "ads_power_api_url" in body:
            ads_power_checker.set_config(conn, "ads_power_api_url", str(body["ads_power_api_url"]))
        if "ads_power_api_key" in body:
            ads_power_checker.set_config(conn, "ads_power_api_key", str(body["ads_power_api_key"]))
        if "enabled" in body:
            ads_power_checker.set_config(conn, "enabled", str(body["enabled"]))
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    finally:
        conn.close()


@app.post("/api/ig-web-upload/metrics/run")
def metrics_run_now() -> dict[str, Any]:
    """Trigger immediate metrics check cycle."""
    try:
        import threading
        import ads_power_checker
        t = threading.Thread(target=ads_power_checker.run_once, daemon=True, name="metrics-run-now")
        t.start()
        return {"ok": True, "message": "metrics check started"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/dashboard")
def dashboard() -> FileResponse:
    """Metrics dashboard page."""
    dashboard_path = ROOT / "ui" / "dashboard.html"
    if dashboard_path.exists():
        return FileResponse(str(dashboard_path), headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        })
    return JSONResponse({"ok": False, "error": "dashboard.html not found"}, status_code=404)


@app.post("/api/ig-web-upload/story-trigger/run")
def story_trigger_run_now() -> dict[str, Any]:
    """Trigger immediate story trigger check."""
    try:
        import threading
        import story_trigger
        t = threading.Thread(target=story_trigger.run_trigger_check, daemon=True, name="story-trigger-run")
        t.start()
        return {"ok": True, "message": "story trigger check started"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/ig-web-upload/story-trigger/status")
def story_trigger_status() -> dict[str, Any]:
    """Get story trigger history."""
    conn = db_conn()
    try:
        import story_trigger
        story_trigger.ensure_story_trigger_schema(conn)
        rows = conn.execute("""
            SELECT * FROM story_triggers
            ORDER BY created_at DESC LIMIT 50
        """).fetchall()
        return {"ok": True, "triggers": [dict(r) for r in rows]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


if __name__ == "__main__":
    ensure_schema()
    host = os.environ.get("WEB_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_UI_PORT", "8770"))
    print("=" * 58)
    print(" SparkGrid Instagram Web Upload — standalone")
    print(f" http://{host}:{port}")
    print(f" data: {DATA_DIR}")
    print("=" * 58, flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
