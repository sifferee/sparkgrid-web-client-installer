#!/usr/bin/env python3
"""Instagram Web profile/cookie workflow helper.

Tasks:
- create_profiles: create browser profile folders + proxy/fingerprint config per account
- check_login: open persistent profile, visit Instagram, detect login/checkpoint/ready
- warmup: run browser cookie warmup only, no upload
- auto_login: open profile, fill saved password/TOTP, save browser session
- open_profile: open one profile for manual login

Keeps compact live dumps under ai_content_data/debug/ig_web_upload/.
"""
from __future__ import annotations

from instagram_consent_flow import (
    consent_present,
    request_failed as consent_request_failed,
    resolve_instagram_consent,
)
from instagram_dialog_gate import (
    HANDLED_REEVALUATE,
    NO_BLOCKER,
    TRANSITIONING_RETRY,
    continue_after_dialog,
    inspect_dialog,
)
from instagram_auth_goal import (
    confirm_authenticated_state,
    continue_authentication_goal,
)
from browser_workflow_goal import (
    AUTHENTICATED_CONFIRMED as SESSION_AUTHENTICATED_CONFIRMED,
    LOGIN_REQUIRED as SESSION_LOGIN_REQUIRED,
    NO_PROGRESS_TIMEOUT as SESSION_NO_PROGRESS_TIMEOUT,
    STABLE_BLOCKER as SESSION_STABLE_BLOCKER,
)
from instagram_session_goal import run_check_session_goal
from initial_browser_load import recover_initial_browser_load
from blocking_popup_transaction import (
    AUTOMATED_POPUP_CATEGORIES,
    attempt_vision_click,
    inspect_topmost_blocker,
)

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import uuid

try:
    from browser_preferences import preferred_search_engine, save_browser_preferences
except Exception as _exc:
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    preferred_search_engine = None
    save_browser_preferences = None
from typing import Any, Dict, List
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request
from disk_safety import DiagnosticWriter
from lifecycle_recovery import IndependentHeartbeat
from password_ip_recovery import (
    finish_submission as finish_password_submission,
    reserve_submission as reserve_password_submission,
)
from task_receipts import opaque_account_ref, record_outcome as record_task_outcome
from run_diagnostics import (
    append_event as append_run_event,
    append_event_once as append_run_event_once,
    capture_visual as capture_run_visual,
    ensure_run as ensure_run_diagnostics,
    update_latest_state as update_run_latest_state,
)
from log_config import get_logger

logger = get_logger("automation")

try:
    import pyotp
except Exception as _exc:
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    pyotp = None

try:
    from ig_human import make_human
except Exception as _exc:
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    make_human = None

try:
    from ig_network_capture import start_instagram_network_capture
except Exception as _exc:
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    start_instagram_network_capture = None

try:
    from browser_launcher import (
        open_spark_browser,
        save_browser_state,
        storage_state_path as sparkbrowser_state_path,
        active_profile_dir as sparkbrowser_profile_dir,
        ensure_profile_metadata as sparkbrowser_metadata,
        get_profile_runtime as sparkbrowser_runtime,
        proxy_signature as sparkbrowser_proxy_signature,
        create_spark_profile,
        ProxyConfigurationError,
        BrowserProxyApplicationError,
    )
except Exception as _exc:
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    open_spark_browser = None
    save_browser_state = None
    sparkbrowser_state_path = None
    sparkbrowser_profile_dir = None
    sparkbrowser_metadata = None
    sparkbrowser_runtime = None
    sparkbrowser_proxy_signature = None
    create_spark_profile = None
    ProxyConfigurationError = ()
    BrowserProxyApplicationError = ()

ROOT = Path(__file__).resolve().parent
# Writable data root: on the client/frozen app this script lives inside a
# read-only .app bundle, so persistent data (bot.db, profiles, debug dumps)
# must go to the client data dir (SPARKGRID_DATA_DIR, set by the agent/worker).
# In dev it falls back to the repo dir.
_DATA_ROOT = Path(os.environ["SPARKGRID_DATA_DIR"]) if os.environ.get("SPARKGRID_DATA_DIR") else ROOT
DB_PATH = _DATA_ROOT / "bot.db"
try:
    import geoip2  # present only when camoufox[geoip] extra is installed
    _GEOIP_OK = True
except Exception as _exc:
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    _GEOIP_OK = False
DEBUG_ROOT = _DATA_ROOT / "ai_content_data" / "debug" / "ig_web_upload"
PROFILE_ROOT = _DATA_ROOT / "browser_profiles" / "ig_web_upload"

RESET = "\033[0m"
COLORS = {"OK": "\033[92m", "ERROR": "\033[91m", "WARNING": "\033[93m", "INFO": "\033[94m"}

DESKTOP_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
TIMEZONES = ["America/New_York", "America/Chicago", "America/Los_Angeles", "Europe/Berlin", "Europe/London"]
LOCALES = ["en-US", "en-GB", "de-DE"]


def log(msg: str, level: str = "INFO") -> None:
    from log_config import log_to_file_and_print
    log_to_file_and_print("browser", msg, level)


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip().lstrip("@"))[:90] or "account"


def db_conn():
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    return c


def cols(conn, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return set()


def ensure_schema():
    c = db_conn()
    try:
        account_cols = cols(c, "accounts")
        for col, ddl in [
            ("web_upload_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("web_upload_mode", "TEXT NOT NULL DEFAULT 'desktop'"),
            ("web_upload_profile_status", "TEXT NOT NULL DEFAULT ''"),
            ("web_upload_login_status", "TEXT NOT NULL DEFAULT ''"),
            ("web_upload_cookie_status", "TEXT NOT NULL DEFAULT ''"),
            ("web_upload_last_error", "TEXT NOT NULL DEFAULT ''"),
            ("web_upload_last_upload_at", "TEXT NOT NULL DEFAULT ''"),
            ("web_upload_cooldown_until", "TEXT NOT NULL DEFAULT ''"),
            ("web_privacy_status", "TEXT NOT NULL DEFAULT 'unchecked'"),
            ("web_privacy_checked_at", "TEXT NOT NULL DEFAULT ''"),
            ("web_privacy_last_error", "TEXT NOT NULL DEFAULT ''"),
            ("web_professional_status", "TEXT NOT NULL DEFAULT 'unchecked'"),
            ("web_professional_checked_at", "TEXT NOT NULL DEFAULT ''"),
            ("web_professional_category", "TEXT NOT NULL DEFAULT ''"),
            ("web_professional_last_error", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if account_cols and col not in account_cols:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
        c.execute("""
            CREATE TABLE IF NOT EXISTS ig_web_upload_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT 'desktop',
                provider TEXT NOT NULL DEFAULT 'playwright',
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
        """)
        job_cols = cols(c, "ig_web_upload_jobs")
        for column in (
            "domain_outcome",
            "infrastructure_outcome",
            "closure_owner",
            "closure_reason",
        ):
            if column not in job_cols:
                c.execute(
                    "ALTER TABLE ig_web_upload_jobs "
                    f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        c.commit()
    finally:
        c.close()


def update_account(account: str, **kw):
    c = db_conn()
    try:
        cs = cols(c, "accounts")
        sets, vals = [], []
        for k, v in kw.items():
            if k in cs:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        if "updated_at" in cs:
            sets.append("updated_at=datetime('now')")
        vals.append(account)
        c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE name=?", vals)
        c.commit()
    finally:
        c.close()


def create_job(run_id: str, account: str, task: str, debug_dir: str, provider: str = "playwright") -> int:
    c = db_conn()
    try:
        cur = c.execute("""
            INSERT INTO ig_web_upload_jobs(run_id, account_name, mode, provider, status, target_uploads, current_step, debug_dir, started_at, updated_at)
            VALUES (?, ?, 'desktop', ?, 'running', 0, ?, ?, datetime('now'), datetime('now'))
        """, (run_id, account, provider, task, debug_dir))
        c.commit()
        return int(cur.lastrowid)
    finally:
        c.close()


def update_job(job_id: int, **kw):
    c = db_conn()
    try:
        if "status" in kw and "domain_outcome" not in kw:
            kw["domain_outcome"] = str(kw["status"] or "")
        not_null = set()
        try:
            for row in c.execute("PRAGMA table_info(ig_web_upload_jobs)").fetchall():
                name = row["name"] if hasattr(row, "keys") and "name" in row.keys() else row[1]
                is_not_null = row["notnull"] if hasattr(row, "keys") and "notnull" in row.keys() else row[3]
                if is_not_null:
                    not_null.add(str(name))
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            not_null = {"finished_at", "started_at", "updated_at", "created_at"}
        sets, vals = [], []
        for k, v in kw.items():
            if v is None and k in not_null:
                continue
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=datetime('now')")
        vals.append(job_id)
        c.execute(f"UPDATE ig_web_upload_jobs SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
    finally:
        c.close()


def normalise_accounts(raw: str) -> List[str]:
    out = []
    for line in str(raw or "").replace(",", "\n").splitlines():
        name = line.strip().split("|")[0].split(":")[0].strip().lstrip("@")
        if name and name not in out:
            out.append(name)
    return out


def get_accounts(names: List[str]) -> List[dict]:
    ensure_schema()
    c = db_conn()
    try:
        where = "WHERE COALESCE(enabled,1)=1 AND COALESCE(warm_only,0)=0"
        params = []
        if names:
            ph = ",".join(["?"] * len(names))
            where += f" AND name IN ({ph})"
            params = names
        rows = c.execute(f"SELECT * FROM accounts {where} ORDER BY name", params).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def _apply_scheduler_proxy_override(accounts: List[dict]) -> None:
    """Use the scheduler-selected connection without exposing it in argv."""
    proxy = str(os.environ.get("SPARKGRID_ACCOUNT_PROXY") or "").strip()
    if proxy and len(accounts) == 1:
        accounts[0]["proxy"] = proxy
        accounts[0]["proxy_url"] = proxy


def profile_dir(account: str, mode: str = "desktop") -> Path:
    return PROFILE_ROOT / safe_name(account) / mode


def account_proxy(account: dict) -> str:
    return str(account.get("proxy") or account.get("proxy_url") or "").strip()


def fingerprint_for(account: str, proxy: str = "") -> dict:
    """Compatibility wrapper for the unified SparkBrowser profile metadata.

    The old implementation generated a second random identity from
    account+proxy. Runtime v2 deliberately keeps proxy out of the identity.
    """
    if sparkbrowser_metadata:
        return sparkbrowser_metadata(account, proxy, "desktop")
    return {
        "account": account,
        "schema_version": 2,
        "viewport": {"width": 1280, "height": 720},
        "device_scale_factor": 1,
        "locale": "en-US",
        "timezone_id": "",
        "proxy_present": bool(proxy),
    }


def ensure_profile(account: dict) -> dict:
    """Create/migrate the one real browser profile for this account.

    Legacy ``fingerprint.json`` and ``proxy.json`` are no longer sources of
    truth. They are renamed once so older exports remain recoverable without
    leaking the current proxy into a plaintext compatibility file.
    """
    name = account["name"]
    proxy = account_proxy(account)
    if create_spark_profile:
        metadata = create_spark_profile(name, proxy, "desktop")
        active = sparkbrowser_profile_dir(name, proxy, "desktop") if sparkbrowser_profile_dir else profile_dir(name, "desktop")
    elif sparkbrowser_metadata:
        metadata = sparkbrowser_metadata(name, proxy, "desktop", locale="en-US")
        active = sparkbrowser_profile_dir(name, proxy, "desktop") if sparkbrowser_profile_dir else profile_dir(name, "desktop")
    else:
        metadata = fingerprint_for(name, proxy)
        active = profile_dir(name, "desktop")
        active.mkdir(parents=True, exist_ok=True)

    legacy_root = profile_dir(name, "desktop")
    legacy_root.mkdir(parents=True, exist_ok=True)
    for legacy_name in ("fingerprint.json", "proxy.json"):
        legacy = legacy_root / legacy_name
        archived = legacy_root / (legacy_name + ".legacy_v1")
        try:
            if legacy.exists() and not archived.exists():
                payload = json.loads(legacy.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
                payload.pop("proxy", None)
                payload["proxy_signature"] = (
                    sparkbrowser_proxy_signature(proxy)
                    if sparkbrowser_proxy_signature else ("direct" if not proxy else "configured")
                )
                archived.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                legacy.unlink()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass

    signature = sparkbrowser_proxy_signature(proxy) if sparkbrowser_proxy_signature else ("direct" if not proxy else "configured")
    state = {
        "schema_version": 2,
        "account": name,
        "profile_dir": str(active),
        "updated_at": now_iso(),
        "status": "profile_ready",
        "proxy_signature": signature,
        "geometry_preset": metadata.get("geometry_preset", "stable_desktop_1440x900_v2"),
    }
    (legacy_root / "profile_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    update_account(name, web_upload_profile_status="profile_ready", web_upload_last_error="")
    return metadata


AUTO_LOGIN_DIAGNOSTIC_SCHEMA_VERSION = 1
AUTO_LOGIN_DIAGNOSTIC_FILE = "auto_login_transaction.jsonl"
_AUTO_LOGIN_DIAGNOSTIC_STATES = {
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
_AUTO_LOGIN_DIAGNOSTIC_URL_CATEGORIES = {
    "challenge",
    "consent",
    "instagram",
    "login_family",
    "two_factor",
    "unknown",
}
_AUTO_LOGIN_DIAGNOSTIC_EVENTS = {
    "interaction",
    "observation",
    "terminal",
}
_AUTO_LOGIN_DIAGNOSTIC_INTERACTIONS = {
    "click_fill",
    "native_setter",
    "none",
    "reacquire",
}
_AUTO_LOGIN_DIAGNOSTIC_TERMINALS = {
    "blocker_detected",
    "challenge_detected",
    "login_form_transition_timeout",
    "login_submit_control_not_found",
    "login_submit_no_transition",
    "password_field_not_found",
    "password_input_not_retained",
    "unrecognized_surface",
    "unsupported_login_state",
    "username_field_not_found",
    "username_field_not_ready",
}
_ACTIVE_AUTO_LOGIN_DIAGNOSTIC_DUMP: ContextVar[Any | None] = ContextVar(
    "active_auto_login_diagnostic_dump",
    default=None,
)
_ACTIVE_BROWSER_PRE_CLEANUP_FINALIZER: ContextVar[Any | None] = ContextVar(
    "active_browser_pre_cleanup_finalizer",
    default=None,
)


def _auto_login_diagnostic_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _auto_login_diagnostic_token(
    value: Any, allowed: set[str], fallback: str
) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def _auto_login_exception_class(value: Any) -> str:
    name = (
        type(value).__name__
        if isinstance(value, BaseException)
        else str(value or "")
    )
    if not name:
        return ""
    return (
        name
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", name)
        else "UnknownError"
    )


def _write_auto_login_diagnostic(payload: dict[str, Any]) -> None:
    dump = _ACTIVE_AUTO_LOGIN_DIAGNOSTIC_DUMP.get()
    recorder = getattr(dump, "record_auto_login_diagnostic", None)
    if recorder is None:
        return
    try:
        recorder(payload)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


class LiveDump:
    def __init__(self, run_id: str, account: str, max_snapshots: int = 30):
        self.run_id = run_id
        self.account = safe_name(account)
        self.root = DEBUG_ROOT / run_id / self.account
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            ensure_run_diagnostics(
                run_id,
                task_category="workflow",
                account_refs=[opaque_account_ref(run_id, account)],
            )
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        DEBUG_ROOT.mkdir(parents=True, exist_ok=True)
        (DEBUG_ROOT / "latest_run.txt").write_text(run_id, encoding="utf-8")
        (DEBUG_ROOT / "latest_account.txt").write_text(self.account, encoding="utf-8")
        self.last_state = ""
        self.max_snapshots = int(max_snapshots or 30)
        self.actions = self.root / "actions.jsonl"
        self.writer = DiagnosticWriter(self.root)
        self._auto_login_diagnostic_nonce = uuid.uuid4().hex[:12]
        self._auto_login_diagnostic_sequence = 0
        self._auto_login_diagnostic_refs: dict[str, dict[str, str]] = {
            "frame": {},
            "container": {},
        }
        self._auto_login_document_epochs: dict[str, int] = {}
        heartbeat_target = str(
            os.environ.get("SPARKGRID_HEARTBEAT_PATH") or ""
        ).strip()
        scheduler_error = str(
            os.environ.get("SPARKGRID_HEARTBEAT_ERROR_PATH") or ""
        ).strip()
        self.liveness = IndependentHeartbeat(
            heartbeat_target,
            run_ref=self.run_id,
            account_ref=self.account,
            task_ref=str(os.environ.get("SPARKGRID_TASK_ID") or ""),
            workflow="profile",
            role="profile_worker",
            recovery_attempt=int(
                os.environ.get("SPARKGRID_RECOVERY_ATTEMPT", "0") or 0
            ),
            error_paths=tuple(
                item
                for item in (
                    scheduler_error,
                    str(self.root / "heartbeat_transport_error.json"),
                )
                if item
            ),
        ).start()

    def _heartbeat(self, state: str) -> None:
        self.liveness.update_phase(state)

    def _auto_login_opaque_ref(self, kind: str, raw: Any) -> str:
        if kind not in {"frame", "container"} or raw in {None, ""}:
            return ""
        if not hasattr(self, "_auto_login_diagnostic_nonce"):
            self._auto_login_diagnostic_nonce = uuid.uuid4().hex[:12]
            self._auto_login_diagnostic_refs = {
                "frame": {},
                "container": {},
            }
        values = self._auto_login_diagnostic_refs.setdefault(kind, {})
        key = str(raw)
        if key not in values:
            values[key] = (
                f"{kind[:1]}_{self._auto_login_diagnostic_nonce}_"
                f"{len(values) + 1:03d}"
            )
        return values[key]

    def _auto_login_document_epoch(self, raw: Any) -> int:
        if raw in {None, ""}:
            return 0
        if not hasattr(self, "_auto_login_document_epochs"):
            self._auto_login_document_epochs = {}
        key = str(raw)
        if key not in self._auto_login_document_epochs:
            self._auto_login_document_epochs[key] = (
                len(self._auto_login_document_epochs) + 1
            )
        return self._auto_login_document_epochs[key]

    def record_auto_login_diagnostic(
        self, payload: dict[str, Any]
    ) -> None:
        """Persist only the versioned Auto Login diagnostic allowlist."""
        if self.writer.disabled:
            return
        if not hasattr(self, "_auto_login_diagnostic_sequence"):
            self._auto_login_diagnostic_sequence = 0
        self._auto_login_diagnostic_sequence += 1
        selected_raw = (
            payload.get("selected_candidate")
            if isinstance(payload.get("selected_candidate"), dict)
            else None
        )
        selected = None
        if selected_raw is not None:
            selected = {
                "intent": _auto_login_diagnostic_token(
                    selected_raw.get("intent"),
                    {"otp", "password", "username", "unknown"},
                    "unknown",
                ),
                "type_category": _auto_login_diagnostic_token(
                    selected_raw.get("type_category"),
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
                "autocomplete_category": (
                    _auto_login_diagnostic_token(
                        selected_raw.get("autocomplete_category"),
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
                    )
                ),
                "form_owned": bool(selected_raw.get("form_owned")),
                "attached": bool(selected_raw.get("attached")),
                "visible_probe": _auto_login_diagnostic_bool(
                    selected_raw.get("visible_probe")
                ),
                "enabled_probe": _auto_login_diagnostic_bool(
                    selected_raw.get("enabled_probe")
                ),
                "editable_probe": _auto_login_diagnostic_bool(
                    selected_raw.get("editable_probe")
                ),
                "readonly": bool(selected_raw.get("readonly")),
                "bounding_box_present": bool(
                    selected_raw.get("bounding_box_present")
                ),
                "viewport_intersection": bool(
                    selected_raw.get("viewport_intersection")
                ),
                "node_replacement": bool(
                    selected_raw.get("node_replacement")
                ),
            }
        counts_raw = (
            payload.get("candidate_counts")
            if isinstance(payload.get("candidate_counts"), dict)
            else {}
        )
        interaction_raw = (
            payload.get("interaction")
            if isinstance(payload.get("interaction"), dict)
            else {}
        )
        postcondition_raw = (
            payload.get("postcondition")
            if isinstance(payload.get("postcondition"), dict)
            else {}
        )
        terminal_raw = (
            payload.get("terminal")
            if isinstance(payload.get("terminal"), dict)
            else {}
        )
        terminal_code = str(terminal_raw.get("code") or "")
        if terminal_code not in _AUTO_LOGIN_DIAGNOSTIC_TERMINALS:
            terminal_code = ""
        record = {
            "schema_version": AUTO_LOGIN_DIAGNOSTIC_SCHEMA_VERSION,
            "timestamp_utc": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "sequence": self._auto_login_diagnostic_sequence,
            "event": _auto_login_diagnostic_token(
                payload.get("event"),
                _AUTO_LOGIN_DIAGNOSTIC_EVENTS,
                "observation",
            ),
            "attempt_number": max(
                0, min(999, int(payload.get("attempt_number") or 0))
            ),
            "document_epoch": self._auto_login_document_epoch(
                payload.get("document_key")
            ),
            "mutation_epoch": max(
                0, int(payload.get("mutation_epoch") or 0)
            ),
            "state": _auto_login_diagnostic_token(
                payload.get("state"),
                _AUTO_LOGIN_DIAGNOSTIC_STATES,
                "unknown",
            ),
            "url_category": _auto_login_diagnostic_token(
                payload.get("url_category"),
                _AUTO_LOGIN_DIAGNOSTIC_URL_CATEGORIES,
                "unknown",
            ),
            "frame_ref": self._auto_login_opaque_ref(
                "frame", payload.get("frame_key")
            ),
            "container_ref": self._auto_login_opaque_ref(
                "container", payload.get("container_key")
            ),
            "candidate_counts": {
                intent: max(
                    0, min(999, int(counts_raw.get(intent) or 0))
                )
                for intent in ("username", "password", "otp", "other")
            },
            "selected_candidate": selected,
            "interaction": {
                "attempted": bool(interaction_raw.get("attempted")),
                "kind": _auto_login_diagnostic_token(
                    interaction_raw.get("kind"),
                    _AUTO_LOGIN_DIAGNOSTIC_INTERACTIONS,
                    "none",
                ),
                "exception_class": _auto_login_exception_class(
                    interaction_raw.get("exception_class")
                ),
            },
            "postcondition": {
                "value_match": _auto_login_diagnostic_bool(
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
                "reason_category": _auto_login_diagnostic_token(
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
        self.writer.append_text(
            self.root / AUTO_LOGIN_DIAGNOSTIC_FILE,
            json.dumps(
                record,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        try:
            state = str(record.get("state") or "unknown")
            event_type = (
                "username_interaction"
                if (selected or {}).get("intent") == "username"
                else "password_interaction"
                if (selected or {}).get("intent") == "password"
                else "submission_readiness"
                if record.get("event") in {
                    "submission_readiness", "submission_blocked",
                    "submission_attempt",
                }
                else "login_surface_classified"
            )
            append_run_event(
                self.run_id,
                event_type,
                stream="actions",
                login_surface_category=state,
                interaction_category=str(
                    (record.get("interaction") or {}).get("kind") or "none"
                ),
                interaction_attempted=bool(
                    (record.get("interaction") or {}).get("attempted")
                ),
                value_verified=bool(
                    (record.get("postcondition") or {}).get("value_match")
                ),
                document_epoch=int(record.get("document_epoch") or 0),
                mutation_epoch=int(record.get("mutation_epoch") or 0),
            )
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass

    def visible_text(self, page) -> str:
        try:
            return (page.locator("body").inner_text(timeout=1500) or "")[:12000]
        except Exception as exc:
            logger.debug("%s: %s", type(exc).__name__, exc)
            return f"<visible text unavailable: {exc}>"

    def capture(
        self,
        page,
        state: str,
        action: str = "",
        error: str = "",
        force_snapshot: bool = False,
        take_screenshot: bool = True,
        take_visible_text: bool = True,
    ):
        self._heartbeat(state)
        if self.writer.disabled:
            return
        payload = {"run_id": self.run_id, "account": self.account, "state": state, "action": action, "error": error, "url": "", "ts": now_iso()}
        try:
            payload["url"] = str(page.url or "").split("?", 1)[0].split("#", 1)[0]
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        meaningful_visual = bool(
            take_screenshot
            and (
                force_snapshot
                or error
                or state != self.last_state
            )
        )
        sensitive_input = False
        if meaningful_visual:
            try:
                sensitive_input = bool(page.evaluate(
                    """() => Array.from(document.querySelectorAll('input')).some(el => {
                      const t=(el.type||'').toLowerCase();
                      const a=(el.autocomplete||'').toLowerCase();
                      return !!el.value && (t==='password' || a==='one-time-code');
                    })"""
                ))
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                sensitive_input = True
        if meaningful_visual and not sensitive_input:
            try:
                page.screenshot(path=str(self.root / "latest.png"), full_page=False)
            except Exception as exc:
                logger.debug("%s: %s", type(exc).__name__, exc)
                payload["screenshot_error"] = str(exc)
        if take_visible_text:
            try:
                self.writer.write_text(self.root / "latest_text.txt", self.visible_text(page))
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
        if not self.writer.write_text(self.root / "latest_state.json", json.dumps(payload, ensure_ascii=False, indent=2)): return
        if not self.writer.append_text(self.actions, json.dumps(payload, ensure_ascii=False) + "\n"): return
        if force_snapshot or error or state != self.last_state:
            self.last_state = state
            snap = self.root / "snapshots"
            snap.mkdir(exist_ok=True)
            base = datetime.now().strftime("%H%M%S") + "_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", state)[:40]
            if meaningful_visual and not sensitive_input:
                try:
                    shutil.copy2(self.root / "latest.png", snap / f"{base}.png")
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
            if not self.writer.write_text(snap / f"{base}.json", json.dumps(payload, ensure_ascii=False, indent=2)): return
            files = sorted([x for x in snap.iterdir() if x.is_file()], key=lambda x: x.stat().st_mtime)
            overflow = max(0, len(files) - self.max_snapshots * 2)
            for x in files[:overflow]:
                try:
                    x.unlink()
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
        try:
            normalized_state = (
                "authenticated"
                if "logged_in" in state or "authenticated" in state
                else "challenge"
                if "challenge" in state
                else "two_factor"
                if "two_factor" in state or "2fa" in state.lower()
                else "login_username_first"
                if "username" in state
                else "login_password_only"
                if "password" in state
                else "unknown"
            )
            update_run_latest_state(
                self.run_id, normalized_state,
                browser_live=True, page_live=True,
            )
            reason = (
                "password_submission_blocker"
                if "second_submission" in (state + " " + error)
                else "browser_load_failure"
                if "browser_load" in (state + " " + error)
                else "regional_ads_transition"
                if "regional_ads" in state
                else "recognized_blocker"
                if "consent" in state or "dialog" in state
                else "terminal_failure"
                if error
                else "final_success"
                if normalized_state == "authenticated"
                else "first_meaningful_surface"
            )
            if meaningful_visual:
                capture_run_visual(page, self.run_id, reason)
            if "regional_ads" in state:
                append_run_event(
                    self.run_id, "regional_ads_consent_step",
                    popup_category="regional_ads_consent",
                    category="transitioning",
                )
            elif "consent" in state or "dialog" in state:
                append_run_event(
                    self.run_id, "popup_classified",
                    popup_category=(
                        "cookie_consent"
                        if "cookie" in state
                        else "unknown_blocker"
                    ),
                )
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass

    def capture_safe_dom(self, page, label: str = "post_action_stable") -> str:
        """Persist a credential-free DOM copy for a terminal browser state."""
        if self.writer.disabled:
            return ""
        try:
            html = page.evaluate("""() => {
                const root = document.documentElement.cloneNode(true);
                const allowed = new Set([
                    'role', 'type', 'name', 'placeholder', 'autocomplete',
                    'maxlength', 'disabled', 'aria-label', 'aria-live',
                    'aria-busy', 'aria-disabled', 'aria-hidden', 'aria-modal'
                ]);
                root.querySelectorAll('*').forEach(el => {
                    [...el.attributes].forEach(attr => {
                        if (!allowed.has(attr.name.toLowerCase())) el.removeAttribute(attr.name);
                    });
                });
                root.querySelectorAll('input,textarea').forEach(el => {
                    el.removeAttribute('value');
                    el.setAttribute('value', '[redacted]');
                    el.textContent = '';
                });
                root.querySelectorAll('script,style,link,img,video,audio,source').forEach(el => el.remove());
                return '<!doctype html>\\n' + root.outerHTML;
            }""")
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return ""
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "post_action_stable"))[:50]
        latest = self.root / "latest_safe_dom.html"
        if not self.writer.write_text(latest, str(html or "")):
            return ""
        snap = self.root / "snapshots"
        snap.mkdir(exist_ok=True)
        target = snap / f"{datetime.now().strftime('%H%M%S')}_{safe_label}.html"
        if not self.writer.write_text(target, str(html or "")):
            return str(latest)
        return str(target)

    def record_consent_recovery(self, payload: dict) -> None:
        """Write the recovery allowlist without account, URL, DOM, or secrets."""
        fields = (
            "recovery_strategy",
            "attempt_number",
            "state_before_navigation",
            "state_after_navigation",
            "authenticated_after_navigation",
            "consent_detected",
            "request_processing_detected",
            "recovery_succeeded",
            "recovery_exhausted",
            "final_outcome",
        )
        safe = {field: payload.get(field) for field in fields}
        try:
            with (self.root / "consent_recovery.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(safe, ensure_ascii=False) + "\n")
        except OSError:
            pass
        try:
            append_run_event(
                self.run_id,
                "popup_classified",
                popup_category=(
                    "cookie_consent"
                    if payload.get("consent_detected")
                    else "none"
                ),
                transition_count=int(payload.get("attempt_number") or 0),
            )
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass

    def record_initial_browser_load(self, payload: dict) -> None:
        """Persist only the initial-load allowlist."""
        fields = (
            "target_category",
            "navigation_timeout",
            "main_frame_failure_category",
            "browser_live",
            "context_live",
            "page_live",
            "document_category",
            "retry_count",
            "outcome",
            "ok",
        )
        safe = {field: payload.get(field) for field in fields}
        try:
            with (self.root / "initial_browser_load.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
        except OSError:
            pass
        try:
            append_run_event(
                self.run_id,
                "document_classified",
                document_category=str(
                    payload.get("document_category") or "unknown_document"
                ),
                failure_category=str(
                    payload.get("main_frame_failure_category") or ""
                ),
                navigation_timeout=bool(payload.get("navigation_timeout")),
                browser_live=bool(payload.get("browser_live")),
                context_live=bool(payload.get("context_live")),
                page_live=bool(payload.get("page_live")),
                retry_count=int(payload.get("retry_count") or 0),
                target_category=str(
                    payload.get("target_category") or "unknown"
                ),
            )
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass


def _human_event_sink(dump: LiveDump):
    existing = getattr(dump, "_human_event_sink", None)
    if existing is not None:
        return existing
    path = dump.root / "human_actions.jsonl"

    def sink(event):
        payload = {
            "run_id": dump.run_id,
            "account": dump.account,
            "ts": now_iso(),
        }
        payload.update(dict(event or {}))
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass

    dump._human_event_sink = sink
    return sink


def _human_for(page, account: str = "", dump: LiveDump | None = None):
    if make_human is None:
        return None
    try:
        name = account or (dump.account if dump is not None else "instagram_web")
        sink = _human_event_sink(dump) if dump is not None else None
        return make_human(page, name, event_sink=sink)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return None


def _record_direct_fallback(dump: LiveDump | None, action: str, error: str = "") -> None:
    if dump is None:
        return
    _human_event_sink(dump)({
        "at": time.time(),
        "kind": "direct_fallback",
        "action": str(action or "click"),
        "error": str(error or ""),
    })


def require_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        raise RuntimeError("Playwright is required. Run install_windows.bat on Windows or ./install.command on macOS/Linux. " + str(exc))



def _parse_proxy_for_browser(proxy: str):
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    if "://" in proxy:
        try:
            from urllib.parse import urlparse
            u = urlparse(proxy)
            server = f"{u.scheme}://{u.hostname}:{u.port}" if u.hostname and u.port else proxy
            out = {"server": server}
            if u.username:
                out["username"] = u.username
            if u.password:
                out["password"] = u.password
            return out
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return {"server": proxy}
    parts = proxy.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return {"server": f"http://{host}:{port}", "username": user, "password": password}
    if len(parts) == 2:
        host, port = parts
        return {"server": f"http://{host}:{port}"}
    return {"server": proxy}


def _proxy_url_for_urllib(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    parts = proxy.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    return proxy


def _check_proxy_reachable(proxy: str, timeout: float = 15.0) -> tuple[bool, str]:
    if os.environ.get("SPARKGRID_PROXY_GATE_PASSED") == "1":
        return True, "scheduler strict proxy gate already passed"
    proxy_url = _proxy_url_for_urllib(proxy)
    if not proxy_url:
        return True, "no proxy configured"
    try:
        scheme = str(urlparse(proxy_url).scheme or "").lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        scheme = ""
    # urllib does not provide native SOCKS transport. SparkBrowser/Playwright does,
    # so SOCKS endpoints are validated by the real browser connection instead of
    # being rejected by this HTTP-only preflight.
    if scheme in {"socks4", "socks5", "socks5h"}:
        return True, "SOCKS proxy will be verified by SparkBrowser"
    try:
        opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
        req = Request(
            "https://www.instagram.com/robots.txt",
            headers={"User-Agent": random.choice(DESKTOP_UA_POOL), "Accept": "*/*"},
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                code = int(getattr(resp, "status", 0) or resp.getcode() or 0)
        except HTTPError as exc:
            code = int(exc.code)
        # Do not inspect a page or require logged-in content here. Any HTTP
        # response except proxy-auth failure proves that transport reached the
        # Instagram host; the browser classifies login/challenge separately.
        if code <= 0 or code == 407:
            return False, f"proxy transport returned HTTP {code}"
        return True, f"proxy transport reached Instagram (HTTP {code})"
    except Exception as exc:
        # Mobile proxies can intermittently take longer than the lightweight
        # urllib preflight while still working in Camoufox. A preflight timeout
        # must not abort the account task; the real browser connection remains
        # the authoritative proxy check and will report a genuine launch or
        # navigation failure normally.
        logger.debug("%s: %s", type(exc).__name__, exc)
        error = str(exc) or type(exc).__name__
        if "timed out" in error.lower() or "timeout" in error.lower():
            return True, f"proxy precheck timed out; SparkBrowser will verify the proxy ({error})"
        return False, f"proxy precheck failed: {error}"


_PROXY_FAILURE_DIRECT_MARKERS = (
    "failed to connect to proxy",
    "unable to connect to proxy",
    "connection to proxy",
    "proxy connection",
    "proxyerror",
    "proxy precheck failed",
    "proxy exit-ip check failed",
    "proxy authentication required",
    "browser does not support socks5 proxy authentication",
)

_PROXY_LAUNCH_NETWORK_MARKERS = (
    "connecttimeouterror",
    "connection timed out",
    "connect timeout",
    "timed out",
    "connection reset",
    "connection aborted",
    "connection refused",
)


def _proxy_failure_classification(error: BaseException | str) -> str:
    current = error
    seen = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        classification = str(getattr(current, "classification", "") or "")
        if classification in {"proxy_parse_error", "browser_proxy_application_failed"}:
            return classification
        current = current.__cause__ or current.__context__
    return ""


def _is_proxy_failure(error: BaseException | str) -> bool:
    if _proxy_failure_classification(error):
        return True
    lowered = str(error or "").lower()
    if any(marker in lowered for marker in _PROXY_FAILURE_DIRECT_MARKERS):
        return True
    # Do not misclassify a normal Playwright page/locator timeout as a proxy
    # problem. Generic network markers count only while SparkBrowser itself is
    # being launched through the proxy.
    return "sparkbrowser failed to launch" in lowered and any(
        marker in lowered for marker in _PROXY_LAUNCH_NETWORK_MARKERS
    )


def _finish_proxy_failure(name: str, job: int, error: BaseException | str) -> bool:
    if not _is_proxy_failure(error):
        return False
    classification = _proxy_failure_classification(error) or "proxy_failed"
    # Typed launcher errors intentionally expose only their stable category.
    detail = classification if classification != "proxy_failed" else str(error)
    update_account(
        name,
        web_upload_login_status=classification,
        web_upload_last_error=detail,
    )
    update_job(
        job,
        status="failed",
        current_step=classification,
        last_error=detail,
        finished_at=now_iso(),
    )
    log(f"{name}: {classification}; browser closed", "ERROR")
    return True


def _proxy_for_account(account: dict) -> str:
    if bool(account.get("_no_proxy")):
        return ""
    return account_proxy(account)


def _profile_storage_state_path(account: str, mode: str = "desktop") -> Path:
    if sparkbrowser_state_path:
        return sparkbrowser_state_path(account, _account_proxy_from_db(account), mode)
    p = profile_dir(account, mode)
    p.mkdir(parents=True, exist_ok=True)
    return p / "camoufox_storage_state.json"


def _account_proxy_from_db(account: str) -> str:
    try:
        c = db_conn()
        try:
            table_cols = cols(c, "accounts")
            if "proxy" in table_cols:
                row = c.execute("SELECT COALESCE(proxy,'') AS proxy FROM accounts WHERE name=?", (account,)).fetchone()
                return str(row["proxy"] or "") if row else ""
            if "proxy_url" in table_cols:
                row = c.execute("SELECT COALESCE(proxy_url,'') AS proxy FROM accounts WHERE name=?", (account,)).fetchone()
                return str(row["proxy"] or "") if row else ""
        finally:
            c.close()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return ""


def _open_camoufox_context(account: dict, headless: bool = False, manual: bool = False):
    name = account["name"]
    ensure_profile(account)
    proxy_raw = "" if bool(account.get("_no_proxy")) else account_proxy(account)
    if not open_spark_browser:
        raise RuntimeError("SparkBrowser launcher is not available")
    cm, context, page = open_spark_browser(name, proxy_raw, mode="desktop", headless=headless, humanize=False)
    try:
        setattr(context, "_sparkgrid_proxy", proxy_raw)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return cm, context, page


def _save_camoufox_state(context, account: str, proxy: str | None = None):
    try:
        proxy = proxy if proxy is not None else getattr(context, "_sparkgrid_proxy", _account_proxy_from_db(account))
        if save_browser_state:
            return save_browser_state(context, account, proxy, "desktop")
        storage_path = _profile_storage_state_path(account, "desktop")
        context.storage_state(path=str(storage_path))
        return str(storage_path)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return ""

def launch_context(p, account: dict, provider: str = "playwright", headless: bool = False, manual: bool = False):
    name = account["name"]
    fp = ensure_profile(account)

    if provider == "camoufox":
        try:
            cm, context, page = _open_camoufox_context(account, headless=headless, manual=manual)
            log(f"{name}: SparkBrowser opened; profile={sparkbrowser_profile_dir(name, account_proxy(account), 'desktop') if sparkbrowser_profile_dir else profile_dir(name)}", "OK")
            return cm, context, page
        except Exception as exc:
            # Never change browser engine/fingerprint silently. A Camoufox
            # profile must either open correctly or fail with an actionable error.
            logger.debug("%s: %s", type(exc).__name__, exc)
            if isinstance(exc, (ProxyConfigurationError, BrowserProxyApplicationError)):
                raise
            raise RuntimeError(f"SparkBrowser launch failed: {exc}") from exc

    pdir = profile_dir(name, "desktop")
    proxy = "" if bool(account.get("_no_proxy")) else account_proxy(account)
    runtime = sparkbrowser_runtime(name, proxy, "desktop") if sparkbrowser_runtime else fp
    kwargs = dict(
        headless=headless,
        accept_downloads=True,
        locale=runtime.get("locale") or "en-US",
        viewport=runtime.get("viewport") or {"width": 1280, "height": 720},
        device_scale_factor=runtime.get("device_scale_factor") or 1,
        user_agent=fp.get("user_agent") or DESKTOP_UA_POOL[0],
        color_scheme=fp.get("color_scheme") or "dark",
        args=["--disable-notifications", "--use-fake-ui-for-media-stream"],
    )
    if runtime.get("timezone_id"):
        kwargs["timezone_id"] = runtime.get("timezone_id")
    proxy_cfg = _parse_proxy_for_browser(proxy)
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    ctx = p.chromium.launch_persistent_context(str(pdir), **kwargs)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return None, ctx, page


def _authenticated_session_present(page) -> bool:
    """Use the shared strong-or-corroborated authentication predicate."""
    return bool(confirm_authenticated_state(page).get("confirmed"))


def get_state(page) -> tuple[str, str]:
    try:
        txt = (page.locator("body").inner_text(timeout=2000) or "").lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        txt = ""
    url = ""
    try:
        url = (page.url or "").lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    if _is_consent_loop(page):
        return "consent_required", "Instagram cookie consent could not be saved"
    # A visible dialog is stronger evidence than a successful current-user
    # endpoint.  A content-removal notice is a temporary UI blocker, never a
    # durable account review/restriction state.
    gate = continue_after_dialog(page, allow_safe_close=True, wait_seconds=0.8)
    if gate.get("outcome") == TRANSITIONING_RETRY:
        return "unknown", "Instagram DOM/navigation is still transitioning"
    gate_state = str(gate.get("state") or "")
    if gate_state:
        dialog_states = {
            "checkpoint": ("checkpoint", "checkpoint dialog requires review"),
            "restricted": ("restricted", "restriction dialog requires review"),
            "suspended": ("suspended", "suspension dialog requires review"),
            "unknown_dialog": ("unknown_popup", "stable unknown dialog requires review"),
            "blocking_dialog_not_dismissed": (
                "blocking_dialog_not_dismissed",
                "Instagram dialog was not dismissed",
            ),
        }
        if gate_state in dialog_states:
            return dialog_states[gate_state]
    auth_goal = continue_authentication_goal(
        page, timeout_seconds=0.0, optional_cleanup=True
    )
    if auth_goal.get("ok"):
        return "logged_in", str(
            auth_goal.get("reason")
            or "authenticated goal confirmed the session"
        )
    strong_auth_states = {
        "login_required": ("login_required", "visible login form"),
        "two_factor_required": ("two_factor_required", "2FA URL/form requires action"),
        "checkpoint": ("checkpoint", "scoped challenge evidence requires review"),
        "restricted": ("restricted", "restriction dialog requires review"),
        "suspended": ("suspended", "suspension dialog requires review"),
        "unknown_popup": ("unknown_popup", "stable unknown dialog requires review"),
    }
    auth_state = str(auth_goal.get("state") or "")
    if auth_state in strong_auth_states:
        return strong_auth_states[auth_state]
    # Instagram sometimes uses /accounts/suspended for a reversible human
    # confirmation flow. Do not misclassify that screen as a permanent ban.
    if (
        ("/accounts/suspended" in url or "/suspended" in url)
        and any(s in txt for s in [
            "confirm you're human",
            "confirm you’re human",
            "takes about 30 seconds",
            "prove you're human",
            "prove you’re human",
        ])
    ):
        return "checkpoint", "human confirmation required"
    if "/accounts/suspended" in url or "/suspended" in url or any(
        s in txt for s in ["we suspended your account", "account has been suspended",
                           "dein konto wurde gesperrt", "konto gesperrt"]):
        return "suspended", "account suspended"
    if (
        "two_step_verification" in url
        or "two_factor" in url
        or any(s in txt for s in [
            "authentication app",
            "two-factor",
            "2fa",
            "6-digit code",
            "6 digit code",
            "security code",
            "verification code",
            "enter code",
            "код",
        ])
    ):
        return "two_factor_required", "2FA code requested"
    # Language-agnostic: a visible password field means this is the LOGIN screen,
    # not a logged-in page (the 2FA page, checked above, has no password field).
    # Without this, non-English login pages (e.g. German "Passwort/Anmelden") fall
    # through to the loose "instagram in text" check and get mislabeled logged_in.
    try:
        has_password_field = page.locator(
            "input[type='password'], input[name='password'], input[autocomplete='current-password']"
        ).count() > 0
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        has_password_field = False
    if has_password_field:
        return "login_required", "password field present"
    # A footer may contain login/signup links on a fully usable Reels page.
    # Prefer the authenticated endpoint over bare page-wide login copy.
    if "instagram.com" in url and _authenticated_session_present(page):
        return "logged_in", "authenticated current-user endpoint confirmed the session"
    if any(s in txt for s in ["log in", "sign up", "enter your password", "войдите",
                              "anmelden", "passwort", "neues konto", "registrieren"]):
        return "login_required", "login text detected"
    if any(s in txt for s in ["suspicious", "try again later", "we restrict", "огранич", "подозр",
                              "verdächtig", "später erneut", "eingeschränkt"]):
        return "restricted", "restriction text detected"
    # Positive logged-in signal: the app nav/home surface, not merely the word
    # "instagram" (which appears on every IG page including login).
    try:
        logged_in_ui = page.locator(
            "svg[aria-label='Home' i], a[href='/'], [aria-label='New post' i], "
            "svg[aria-label='Startseite' i], nav a[href*='/direct/']"
        ).count() > 0
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        logged_in_ui = False
    if logged_in_ui:
        return "logged_in", "logged-in app UI present"
    if "instagram.com" in url and _authenticated_session_present(page):
        return "logged_in", "authenticated current-user endpoint confirmed the session"
    try:
        cookies = page.context.cookies("https://www.instagram.com/")
        has_session = any(
            str(item.get("name") or "").lower() == "sessionid" and str(item.get("value") or "").strip()
            for item in cookies
        )
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        has_session = False
    if has_session and "instagram.com" in url:
        return "unknown", "session cookie exists but Instagram did not confirm authentication"
    # Raw dialog nodes are not evidence of a blocker. Instagram commonly keeps
    # dismissed consent markup mounted; inspect_dialog above is the single
    # visibility-aware source of truth.
    return "unknown", "unknown page state"


# TEST TOGGLE: force Instagram UI to English so text detection is reliable while
# debugging. Set to False later to let IG render in the proxy/fingerprint language.
FORCE_IG_ENGLISH = True


def _force_english(page, dump=None) -> None:
    """Normalize IG UI to English (hl=en) for the rest of the session."""
    if not FORCE_IG_ENGLISH:
        return
    try:
        page.goto("https://www.instagram.com/?hl=en", wait_until="domcontentloaded", timeout=45000)
        time.sleep(random.uniform(1.0, 2.0))
        if dump:
            dump.capture(page, "arrive_force_english", "normalized IG UI to hl=en")
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _arrive_instagram(
    page,
    dump,
    via_search: bool,
    account: str = "",
    mode: str = "desktop",
) -> dict[str, Any]:
    """Land on instagram.com. With via_search=True arrive organically through a
    search result (looks like a real referral) and fall back to a direct visit."""
    if via_search:
        routes = {
            "google": "https://www.google.com/search?q=instagram+login&hl=en",
            "bing": "https://www.bing.com/search?q=instagram+login",
            "duckduckgo": "https://lite.duckduckgo.com/lite/?q=instagram+login",
        }
        preferred = preferred_search_engine(account, mode) if (account and preferred_search_engine) else "google"
        order = [preferred] + [e for e in ("google", "bing", "duckduckgo") if e != preferred]
        search_routes = [(engine, routes[engine]) for engine in order]
        for engine, url in search_routes:
            try:
                dump.capture(page, f"arrive_search_{engine}", f"searching for instagram via {engine}")
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(random.uniform(1.5, 3.0))
                link = page.locator("a[href*='instagram.com']").first
                if link and link.count() > 0:
                    dump.capture(page, f"arrive_click_result_{engine}", f"clicking instagram.com result via {engine}")
                    actor = _human_for(page, account, dump)
                    clicked = bool(actor and actor.click(link, timeout=8000))
                    if not clicked:
                        link.click(timeout=8000)
                        _record_direct_fallback(dump, f"search_result_{engine}")
                    if actor is not None:
                        actor.dwell(1.2, 2.4, micro_moves=True)
                    else:
                        time.sleep(random.uniform(2.0, 4.0))
                    if "instagram.com" in (page.url or ""):
                        log(f"arrived at instagram.com via {engine} search result", "OK")
                        if account and save_browser_preferences:
                            try:
                                save_browser_preferences(account, mode, last_working_search_engine=engine)
                            except Exception as _exc:
                                logger.debug("%s: %s", type(_exc).__name__, _exc)
                                pass
                        _force_english(page, dump)
                        result = recover_initial_browser_load(page)
                        if hasattr(dump, "record_initial_browser_load"):
                            dump.record_initial_browser_load(result)
                        return result
            except Exception as exc:
                logger.debug("%s: %s", type(exc).__name__, exc)
                log(f"{engine} search arrival failed ({exc}); trying next route", "WARNING")
    initial_error = None
    try:
        url = "https://www.instagram.com/?hl=en" if FORCE_IG_ENGLISH else "https://www.instagram.com/"
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        initial_error = exc
    result = recover_initial_browser_load(
        page,
        initial_error=initial_error,
    )
    if hasattr(dump, "record_initial_browser_load"):
        dump.record_initial_browser_load(result)
    if not result.get("ok"):
        dump.capture(
            page,
            "initial_browser_load_failed",
            "browser_load_failed_after_retry",
            force_snapshot=True,
        )
    return result


def _click_login_if_present(page, dump, account: str = "") -> bool:
    """Open a login form from a non-form surface; never submit credentials."""
    # Identical visible text is used by Instagram's landing CTA and by the
    # submit control.  A rendered form is authoritative: do not touch any
    # ``Log in`` text once its credential fields are present.
    if _login_fields_available(page):
        return False
    actor = _human_for(page, account, dump)
    for getter in (
        lambda: page.get_by_role("button", name=re.compile(r"log in", re.I)),
        lambda: page.get_by_role("link", name=re.compile(r"log in", re.I)),
        lambda: page.get_by_text(re.compile(r"^\s*log in\s*$", re.I)),
    ):
        try:
            loc = getter().first
            if loc and loc.count() > 0:
                clicked = bool(actor and actor.click(loc, timeout=4000))
                if not clicked:
                    loc.click(timeout=4000)
                    _record_direct_fallback(dump, "open_login_form")
                dump.capture(page, "auto_login_navigation_cta", "opened login form")
                if actor is not None:
                    actor.dwell(1.0, 2.2, micro_moves=True)
                else:
                    time.sleep(random.uniform(1.5, 3.0))
                return True
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return False


def _consent_capture(dump: LiveDump | None):
    def capture(page, step: str, detail: str) -> None:
        if dump is not None:
            dump.capture(page, step, detail, force_snapshot=step in {"consent_unresolved", "consent_request_error_closed"})
    return capture


def _is_consent_loop(page) -> bool:
    # Kept for callers that need to know whether the wizard is still present.
    # A normal cookie/ads wizard is resolved before it is classified as a
    # broken loop.
    return consent_present(page)


def _instagram_url_category(page) -> str:
    """Return only a sanitized route family; never capture query values."""
    try:
        parsed = urlparse(str(getattr(page, "url", "") or ""))
        path = (parsed.path or "").lower()
        query = (parsed.query or "").lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return "unknown"
    if "/challenge" in path or "challenge" in query:
        return "challenge"
    if "/consent" in path:
        return "cookies" if "cookie" in query else "consent"
    if "/accounts" in path:
        return "accounts"
    return "unknown"


def _wait_for_current_dom(page) -> None:
    """Wait on the current document without retaining any pre-navigation node."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _record_consent_recovery(
    dump: LiveDump | None,
    *,
    attempt_number: int,
    state_before_navigation: str,
    state_after_navigation: str,
    authenticated_after_navigation: bool,
    consent_detected: bool,
    request_processing_detected: bool,
    recovery_succeeded: bool,
    recovery_exhausted: bool,
    final_outcome: str,
) -> None:
    if dump is None or not hasattr(dump, "record_consent_recovery"):
        return
    dump.record_consent_recovery({
        "recovery_strategy": "homepage_navigation",
        "attempt_number": int(attempt_number),
        "state_before_navigation": str(state_before_navigation),
        "state_after_navigation": str(state_after_navigation),
        "authenticated_after_navigation": bool(authenticated_after_navigation),
        "consent_detected": bool(consent_detected),
        "request_processing_detected": bool(request_processing_detected),
        "recovery_succeeded": bool(recovery_succeeded),
        "recovery_exhausted": bool(recovery_exhausted),
        "final_outcome": str(final_outcome),
    })


def _current_consent_recovery_state(page) -> tuple[str, str]:
    """Classify only the current document after navigation/DOM readiness."""
    if consent_request_failed(page):
        return "request_processing", "Instagram request-processing page detected"
    if consent_present(page):
        return "consent_required", "Instagram consent state detected"
    return get_state(page)


def _consent_recovery_result(
    page,
    dump: LiveDump | None = None,
    *,
    authenticated_confirmed: bool = False,
    max_navigation_attempts: int = 3,
) -> dict:
    """Resolve consent and re-enter Instagram through its homepage when needed.

    Authentication evidence already observed by the caller is retained while
    readiness is false. Recovery never clears the profile, changes its proxy,
    repeats a post-login action, or re-enters login. Every homepage navigation
    uses the current context and is followed by fresh DOM/state detection.
    """
    capture = _consent_capture(dump)
    human = _human_for(
        page,
        str(getattr(dump, "account", "") or ""),
        dump,
    )
    authenticated = bool(authenticated_confirmed or _authenticated_session_present(page))
    attempts = 0
    limit = max(0, int(max_navigation_attempts))
    retry_navigation = False

    while True:
        category = _instagram_url_category(page)
        request_processing_before = consent_request_failed(page)
        if request_processing_before or retry_navigation:
            if attempts >= limit:
                return {
                    "ok": False,
                    "handled": attempts > 0,
                    "state": "consent_required",
                    "reason": (
                        "Instagram request-processing page persisted after "
                        f"{attempts} homepage navigation attempts"
                    ),
                    "authenticated": authenticated,
                    "operationally_ready": False,
                    "consent_state": "consent_pending",
                    "manual_required": True,
                    "request_failed": True,
                    "navigation_attempts": attempts,
                    "reload_attempts": 0,
                    "recovery_strategy": "homepage_navigation",
                    "url_category": category,
                }
            state_before_navigation = (
                "request_processing" if request_processing_before else "unknown"
            )
            retry_navigation = False
            attempts += 1
            navigation_failed = False
            try:
                page.goto(
                    "https://www.instagram.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as _exc:
                # A timeout may still leave a usable committed document.
                # It consumes this attempt and is classified below.
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                navigation_failed = True
            _wait_for_current_dom(page)
            state_after_navigation, _ = _current_consent_recovery_state(page)
            request_processing_after = state_after_navigation == "request_processing"
            consent_after = state_after_navigation == "consent_required"
            authenticated_after_navigation = _authenticated_session_present(page)
            authenticated = bool(authenticated or authenticated_after_navigation)

            if request_processing_after:
                exhausted = attempts >= limit
                _record_consent_recovery(
                    dump,
                    attempt_number=attempts,
                    state_before_navigation=state_before_navigation,
                    state_after_navigation=state_after_navigation,
                    authenticated_after_navigation=authenticated_after_navigation,
                    consent_detected=False,
                    request_processing_detected=True,
                    recovery_succeeded=False,
                    recovery_exhausted=exhausted,
                    final_outcome="manual_required" if exhausted else "retry",
                )
                if exhausted:
                    continue
                # Reuse the existing bounded DOM wait between attempts. No
                # locator or element handle survives this boundary.
                _wait_for_current_dom(page)
                continue

            if consent_after:
                result = resolve_instagram_consent(
                    page, capture, max_seconds=70, human=human
                )
                if not result.get("ok"):
                    request_processing_after = bool(
                        result.get("request_failed") or consent_request_failed(page)
                    )
                    exhausted = request_processing_after and attempts >= limit
                    _record_consent_recovery(
                        dump,
                        attempt_number=attempts,
                        state_before_navigation=state_before_navigation,
                        state_after_navigation=state_after_navigation,
                        authenticated_after_navigation=authenticated_after_navigation,
                        consent_detected=True,
                        request_processing_detected=request_processing_after,
                        recovery_succeeded=False,
                        recovery_exhausted=exhausted,
                        final_outcome=(
                            "manual_required"
                            if exhausted or not request_processing_after
                            else "retry"
                        ),
                    )
                    if request_processing_after:
                        if not exhausted:
                            _wait_for_current_dom(page)
                        continue
                    return {
                        **result,
                        "state": "consent_required",
                        "reason": "Instagram consent flow remains unresolved",
                        "authenticated": authenticated,
                        "operationally_ready": False,
                        "consent_state": "consent_pending",
                        "manual_required": True,
                        "navigation_attempts": attempts,
                        "reload_attempts": 0,
                        "recovery_strategy": "homepage_navigation",
                        "url_category": _instagram_url_category(page),
                    }

            # The homepage or consent resolver may have replaced the document.
            _wait_for_current_dom(page)
            state, reason = _current_consent_recovery_state(page)
            current_authenticated = _authenticated_session_present(page)
            authenticated = bool(authenticated or current_authenticated)
            ready = bool(
                state == "logged_in"
                and current_authenticated
                and not consent_present(page)
                and not consent_request_failed(page)
            )
            if navigation_failed and state == "unknown":
                exhausted = attempts >= limit
                _record_consent_recovery(
                    dump,
                    attempt_number=attempts,
                    state_before_navigation=state_before_navigation,
                    state_after_navigation=state_after_navigation,
                    authenticated_after_navigation=authenticated_after_navigation,
                    consent_detected=False,
                    request_processing_detected=False,
                    recovery_succeeded=False,
                    recovery_exhausted=exhausted,
                    final_outcome="manual_required" if exhausted else "retry",
                )
                if not exhausted:
                    retry_navigation = True
                    _wait_for_current_dom(page)
                    continue
                return {
                    "ok": False,
                    "handled": True,
                    "state": "consent_required",
                    "reason": (
                        "Instagram homepage navigation remained unavailable "
                        f"after {attempts} attempts"
                    ),
                    "authenticated": authenticated,
                    "operationally_ready": False,
                    "consent_state": "consent_pending",
                    "manual_required": True,
                    "request_failed": False,
                    "navigation_attempts": attempts,
                    "reload_attempts": 0,
                    "recovery_strategy": "homepage_navigation",
                    "url_category": _instagram_url_category(page),
                }
            if ready:
                _record_consent_recovery(
                    dump,
                    attempt_number=attempts,
                    state_before_navigation=state_before_navigation,
                    state_after_navigation=state_after_navigation,
                    authenticated_after_navigation=authenticated_after_navigation,
                    consent_detected=consent_after,
                    request_processing_detected=False,
                    recovery_succeeded=True,
                    recovery_exhausted=False,
                    final_outcome="ready",
                )
                return {
                    "ok": True,
                    "handled": True,
                    "state": "logged_in",
                    "reason": reason,
                    "authenticated": True,
                    "operationally_ready": True,
                    "consent_state": "resolved",
                    "manual_required": False,
                    "request_failed": False,
                    "navigation_attempts": attempts,
                    "reload_attempts": 0,
                    "recovery_strategy": "homepage_navigation",
                    "url_category": _instagram_url_category(page),
                }
            final_outcome = (
                "login_challenge_contract"
                if state in {
                    "login_required", "two_factor_required", "checkpoint",
                    "restricted", "suspended",
                }
                else "manual_required"
            )
            _record_consent_recovery(
                dump,
                attempt_number=attempts,
                state_before_navigation=state_before_navigation,
                state_after_navigation=state_after_navigation,
                authenticated_after_navigation=authenticated_after_navigation,
                consent_detected=consent_after,
                request_processing_detected=False,
                recovery_succeeded=False,
                recovery_exhausted=False,
                final_outcome=final_outcome,
            )
            return {
                "ok": False,
                "handled": True,
                "state": state,
                "reason": reason,
                "authenticated": bool(current_authenticated),
                "operationally_ready": False,
                "consent_state": "resolved" if not consent_present(page) else "consent_pending",
                "manual_required": True,
                "request_failed": False,
                "navigation_attempts": attempts,
                "reload_attempts": 0,
                "recovery_strategy": "homepage_navigation",
                "url_category": _instagram_url_category(page),
            }

        if consent_present(page):
            result = resolve_instagram_consent(
                page, capture, max_seconds=70, human=human
            )
            if not result.get("ok"):
                if result.get("request_failed"):
                    continue
                return {
                    **result,
                    "state": "consent_required",
                    "reason": "Instagram consent flow remains unresolved",
                    "authenticated": authenticated,
                    "operationally_ready": False,
                    "navigation_attempts": attempts,
                    "reload_attempts": attempts,
                    "url_category": _instagram_url_category(page),
                }

        # The resolver may have navigated to a completely new document. Probe
        # the current page and endpoint again; never continue an old branch.
        _wait_for_current_dom(page)
        state, reason = get_state(page)
        current_authenticated = _authenticated_session_present(page)
        authenticated = bool(authenticated or current_authenticated)
        ready = bool(
            state == "logged_in"
            and current_authenticated
            and not consent_present(page)
            and not consent_request_failed(page)
        )
        if ready:
            return {
                "ok": True,
                "handled": True,
                "state": "logged_in",
                "reason": reason,
                "authenticated": True,
                "operationally_ready": True,
                "consent_state": "resolved",
                "manual_required": False,
                "request_failed": False,
                "navigation_attempts": attempts,
                "reload_attempts": attempts,
                "url_category": _instagram_url_category(page),
            }
        if state in {
            "login_required", "two_factor_required", "checkpoint",
            "restricted", "suspended",
        }:
            return {
                "ok": not consent_present(page),
                "handled": True,
                "state": state,
                "reason": reason,
                "authenticated": bool(current_authenticated),
                "operationally_ready": False,
                "consent_state": "resolved",
                "manual_required": True,
                "request_failed": False,
                "navigation_attempts": attempts,
                "reload_attempts": attempts,
                "url_category": _instagram_url_category(page),
            }
        return {
            "ok": not consent_present(page),
            "handled": True,
            "state": state,
            "reason": reason,
            "authenticated": authenticated,
            "operationally_ready": False,
            "consent_state": "resolved" if not consent_present(page) else "consent_pending",
            "manual_required": True,
            "request_failed": False,
            "navigation_attempts": attempts,
            "reload_attempts": attempts,
            "url_category": _instagram_url_category(page),
        }


def _recover_instagram_consent(page, dump: LiveDump | None = None, account: str = "") -> bool:
    """Compatibility adapter: clear consent without destroying browser state."""
    result = _consent_recovery_result(page, dump, max_navigation_attempts=3)
    return bool(result.get("ok"))


def _dismiss_instagram_consent(page, dump: LiveDump | None = None, account: str = "") -> bool:
    result = resolve_instagram_consent(
        page,
        _consent_capture(dump),
        max_seconds=70,
        human=_human_for(page, account, dump),
    )
    return bool(result.get("handled"))


def _login_blocker_first(
    page,
    dump: LiveDump | None,
    account: str,
    action: str,
    *,
    wait_seconds: float = 1.0,
    structural_login_surface: bool = False,
) -> tuple[bool, str, str]:
    """Resolve the topmost blocker, then classify a newly-read DOM.

    Every successful dismissal restarts inspection from the page.  No locator
    obtained before a dialog mutation crosses this boundary.
    """
    handled = False
    for _attempt in range(5):
        observed = inspect_topmost_blocker(page)
        if observed.get("authenticated_surface"):
            return True, "logged_in", "authenticated state confirmed"
        if observed.get("two_factor_surface"):
            return True, "two_factor_required", "2FA surface confirmed"
        observation = (
            _observe_login_surface(page)
            if structural_login_surface
            else None
        )
        if observation is not None and observation.state in {
            "authenticated",
            "challenge",
            "two_factor",
        }:
            state = {
                "authenticated": "logged_in",
                "challenge": "checkpoint",
                "two_factor": "two_factor_required",
            }[observation.state]
            return True, state, observation.reason
        category = str(observed.get("category") or "")
        if category in {"checkpoint", "restriction", "suspended"}:
            return (
                False,
                category,
                "existing challenge or account-state handling is required",
            )
        if (
            not observed.get("present")
            or category not in AUTOMATED_POPUP_CATEGORIES
        ):
            if structural_login_surface:
                observation = observation or _observe_login_surface(page)
                state = {
                    "authenticated": "logged_in",
                    "two_factor": "two_factor_required",
                    "challenge": "checkpoint",
                    "consent_blocker": "consent_required",
                    "transitioning": "unknown",
                    "unsupported_stable": "unknown",
                    "login_combined": "login_required",
                    "login_username_first": "login_required",
                    "login_password_only": "login_required",
                }.get(observation.state, "unknown")
                reason = observation.reason
            else:
                state, reason = get_state(page)
            if handled and state == "unknown" and _attempt < 4:
                _wait_for_current_dom(page)
                continue
            return True, state, reason

        gate = continue_after_dialog(
            page,
            allow_safe_close=True,
            wait_seconds=wait_seconds,
        )
        outcome = str(gate.get("outcome") or "")
        if outcome == NO_BLOCKER or (not outcome and not gate.get("present")):
            _wait_for_current_dom(page)
            handled = True
            continue
        if outcome in {HANDLED_REEVALUATE, TRANSITIONING_RETRY} or gate.get("dismissed"):
            handled = True
            if dump is not None:
                dump.capture(
                    page,
                    "auto_login_blocker_resolved",
                    f"action={action}; DOM will be re-evaluated",
                    take_visible_text=False,
                )
            _wait_for_current_dom(page)
            continue

        state = str(gate.get("state") or "unknown_popup")
        if state in {"unknown_dialog", "blocking_dialog_not_dismissed"}:
            state = "unknown_popup"
        reason = "blocking Instagram dialog requires manual review"
        if dump is not None:
            dump.capture(
                page,
                "auto_login_" + state,
                f"blocked before {action}",
                force_snapshot=True,
            )
        return False, state, reason

    if dump is not None:
        dump.capture(
            page,
            "auto_login_unknown_popup",
            f"blocker chain did not settle before {action}",
            force_snapshot=True,
        )
    return False, "unknown_popup", "blocking Instagram dialogs did not settle"


def _login_credentials_action_ready(
    page,
    dump: LiveDump,
    account: str,
    action: str,
) -> bool:
    ok, state, _reason = _login_blocker_first(
        page,
        dump,
        account,
        action,
        structural_login_surface=True,
    )
    return bool(ok and state == "login_required")



def _first_visible(page, selectors: List[str], timeout_ms: int = 1200):
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc and int(loc.count() or 0) > 0 and loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return None


def _first_existing(page, selectors: List[str]):
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc and int(loc.count() or 0) > 0:
                return loc
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return None


def _click_first(
    page, candidates, timeout_ms: int = 3000, *, account: str = "",
    dump: LiveDump | None = None, action: str = "click_first",
) -> bool:
    actor = _human_for(page, account, dump)
    for getter in candidates:
        try:
            loc = getter().first
            if loc and int(loc.count() or 0) > 0:
                try:
                    if not loc.is_visible(timeout=timeout_ms):
                        continue
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
                if actor is not None and actor.click(loc, timeout=timeout_ms):
                    return True
                loc.click(timeout=timeout_ms, force=True)
                _record_direct_fallback(dump, action)
                return True
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return False


def _generate_totp(secret: str) -> str:
    secret = str(secret or "").strip().replace(" ", "")
    if not secret:
        return ""
    if re.fullmatch(r"\d{6,8}", secret):
        return secret
    if pyotp is None:
        raise RuntimeError("pyotp is required for web 2FA auto-login")
    remaining = 30 - (int(time.time()) % 30)
    if remaining <= 6:
        log(f"TOTP window almost expired ({remaining}s left); waiting for a fresh code", "INFO")
        time.sleep(remaining + 1)
    return pyotp.TOTP(secret).now()


def _wait_for_next_totp_window(account: dict, dump: LiveDump | None = None) -> bool:
    secret = str(account.get("api_totp_secret") or "").strip().replace(" ", "")
    if not secret or re.fullmatch(r"\d{6,8}", secret):
        return False
    remaining = 30 - (int(time.time()) % 30)
    wait_for = max(2, remaining + 1)
    log(f"{account.get('name', '')}: waiting {wait_for}s for a fresh 2FA code", "INFO")
    time.sleep(wait_for)
    return True


def _fresh_totp_code(secret: str, submitted_codes: set[str]) -> tuple[str, str]:
    """Return a safe, not-yet-submitted TOTP without exposing it in logs.

    A code from a nearly-finished window is deliberately deferred.  The caller
    can wait for the next window without consuming an OTP submission budget.
    """
    normalized = str(secret or "").strip().replace(" ", "")
    if not normalized:
        return "", "missing_secret"
    if re.fullmatch(r"\d{6,8}", normalized):
        return "", "manual_code_not_reusable"
    if pyotp is None:
        return "", "totp_generator_unavailable"
    remaining = 30 - (int(time.time()) % 30)
    if remaining <= 6:
        return "", "next_window_required"
    try:
        code = str(pyotp.TOTP(normalized).now() or "")
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return "", "invalid_secret"
    if not re.fullmatch(r"\d{6,8}", code):
        return "", "invalid_generated_code"
    if code in submitted_codes:
        return "", "next_window_required"
    return code, "ok"


def _is_2fa_rejection(reason: str) -> bool:
    value = str(reason or "").lower()
    return "2fa code" in value or "security code" in value or "verification code" in value


def _two_factor_feedback(page) -> tuple[str, str]:
    """Classify only explicit post-submit OTP feedback, never a timeout."""
    try:
        text = (page.locator("body").inner_text(timeout=1200) or "").lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return "", ""
    expired = (
        "code has expired", "security code has expired", "expired code",
        "request a new code", "try a new code",
    )
    rejected = (
        "code is incorrect", "security code is incorrect", "incorrect security code",
        "invalid code", "please check the code", "code you entered is incorrect",
    )
    if any(marker in text for marker in expired):
        return "two_factor_code_expired", "Instagram reported that the 2FA code expired"
    if any(marker in text for marker in rejected):
        return "two_factor_code_rejected", "Instagram rejected the 2FA code"
    return "", ""


def _two_factor_liveness(page) -> tuple[str, str]:
    """Return a typed lifecycle result without treating it as OTP feedback."""
    try:
        if bool(page.is_closed()):
            return "page_closed", "2FA page closed while waiting for Instagram"
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    try:
        browser = getattr(getattr(page, "context", None), "browser", None)
        if browser is not None and hasattr(browser, "is_connected") and not browser.is_connected():
            return "browser_unavailable", "browser became unavailable while waiting for Instagram"
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return "", ""


LOGIN_INPUT_LIKE_SELECTOR = (
    "input,textarea,[contenteditable='true'],"
    "[contenteditable='plaintext-only']"
)
LOGIN_CONTROL_SELECTOR = (
    "button,input[type='submit'],input[type='button'],[role='button']"
)
LOGIN_USERNAME_STATES = {"login_combined", "login_username_first"}
LOGIN_CREDENTIAL_STATES = {
    "login_combined", "login_username_first", "login_password_only"
}


@dataclass(frozen=True)
class LoginCandidateRecipe:
    frame_index: int
    node_index: int
    node_ref: str
    group_ref: str


@dataclass(frozen=True)
class LoginCandidateDescriptor:
    recipe: LoginCandidateRecipe
    intent: str
    semantic: str
    type_category: str
    autocomplete_category: str
    form_owned: bool
    attached: bool
    visible: bool
    viewport_intersecting: bool
    bounding_box_present: bool
    disabled: bool
    readonly: bool
    covered: bool
    group_has_submit: bool
    group_challenge: bool
    score: int

    @property
    def structurally_viable(self) -> bool:
        return bool(
            self.attached
            and self.bounding_box_present
            and self.viewport_intersecting
        )


@dataclass
class LoginSurfaceObservation:
    state: str
    reason: str
    url_category: str
    loading: bool
    semantic_fingerprint: str
    frame_epochs: tuple[tuple[int, str, int], ...]
    candidates: list[LoginCandidateDescriptor] = field(default_factory=list)
    selected_group_ref: str = ""

    def candidates_for(
        self, intent: str, preferred_group_ref: str = ""
    ) -> list[LoginCandidateDescriptor]:
        values = [
            candidate
            for candidate in self.candidates
            if candidate.intent == intent
        ]
        preferred = [
            candidate
            for candidate in values
            if preferred_group_ref
            and candidate.recipe.group_ref == preferred_group_ref
        ]
        return sorted(preferred or values, key=lambda item: item.score, reverse=True)


def _strong_combined_login_surface(
    observation: LoginSurfaceObservation,
) -> bool:
    def ready(candidate: LoginCandidateDescriptor) -> bool:
        return bool(
            candidate.attached
            and candidate.visible
            and candidate.viewport_intersecting
            and candidate.bounding_box_present
            and not candidate.disabled
            and not candidate.readonly
        )

    groups: dict[str, set[str]] = {}
    for candidate in observation.candidates:
        if ready(candidate) and candidate.intent in {"username", "password"}:
            groups.setdefault(candidate.recipe.group_ref, set()).add(
                candidate.intent
            )
    return any(
        {"username", "password"}.issubset(intents)
        for intents in groups.values()
    )


def _credential_surface_readiness(
    observation: LoginSurfaceObservation,
) -> tuple[bool, bool]:
    ready_by_group: dict[str, set[str]] = {}
    for candidate in observation.candidates:
        ready = bool(
            candidate.attached
            and candidate.visible
            and candidate.viewport_intersecting
            and candidate.bounding_box_present
            and not candidate.disabled
            and not candidate.readonly
        )
        if ready and candidate.intent in {"username", "password"}:
            ready_by_group.setdefault(
                candidate.recipe.group_ref, set()
            ).add(candidate.intent)
    combined = next(
        (
            intents
            for intents in ready_by_group.values()
            if {"username", "password"}.issubset(intents)
        ),
        set(),
    )
    return "username" in combined, "password" in combined


def _diagnostic_document_epoch(dump: Any, raw: Any) -> int:
    try:
        return int(dump._auto_login_document_epoch(raw))
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return 0


def _surface_diagnostic_evidence(
    dump: Any,
    observation: LoginSurfaceObservation | None,
) -> dict[str, Any]:
    username_ready = False
    password_ready = False
    document_epoch = 0
    mutation_epoch = 0
    if observation is not None:
        username_ready, password_ready = (
            _credential_surface_readiness(observation)
        )
        if observation.frame_epochs:
            _frame, document_ref, mutation_epoch = (
                observation.frame_epochs[0]
            )
            document_epoch = _diagnostic_document_epoch(
                dump, document_ref
            )
    return {
        "document_epoch": document_epoch,
        "mutation_epoch": int(mutation_epoch or 0),
        "username_ready": username_ready,
        "password_ready": password_ready,
    }


def _blocker_diagnostic_evidence(
    dump: Any,
    observed: dict[str, Any] | None,
) -> dict[str, Any]:
    value = dict(observed or {})
    return {
        "document_epoch": _diagnostic_document_epoch(
            dump, value.get("document_epoch")
        ),
        "mutation_epoch": int(value.get("mutation_epoch") or 0),
        "username_ready": False,
        "password_ready": False,
    }


def _record_arrival_route(
    dump: Any,
    route: str,
    *,
    popup_category: str = "",
    surface: LoginSurfaceObservation | None = None,
    observed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = (
        _surface_diagnostic_evidence(dump, surface)
        if surface is not None
        else _blocker_diagnostic_evidence(dump, observed)
    )
    if route == "credential_surface" and not (
        evidence["username_ready"] and evidence["password_ready"]
    ):
        return evidence
    try:
        append_run_event(
            str(dump.run_id),
            "arrival_route_selected",
            route=route,
            popup_category=(
                popup_category if route == "known_popup" else ""
            ),
            source_live_debug=(
                os.environ.get("SPARKGRID_SOURCE_LIVE_DEBUG") == "1"
            ),
            **evidence,
        )
        dump._stage1_arrival_route = route
        dump._stage1_route_evidence = dict(evidence)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return evidence


def _record_credential_workflow_started(
    page: Any,
    dump: Any,
) -> None:
    try:
        evidence = dict(
            getattr(dump, "_stage1_route_evidence", {}) or {}
        )
        if (
            getattr(dump, "_stage1_arrival_route", "")
            != "credential_surface"
            or not evidence.get("username_ready")
            or not evidence.get("password_ready")
        ):
            surface = _observe_login_surface(page)
            if not _strong_combined_login_surface(surface):
                return
            evidence = _record_arrival_route(
                dump, "credential_surface", surface=surface
            )
        append_run_event(
            str(dump.run_id),
            "credential_workflow_started",
            reason="credential_surface",
            username_ready=True,
            password_ready=True,
            document_epoch=int(evidence.get("document_epoch") or 0),
            mutation_epoch=int(evidence.get("mutation_epoch") or 0),
        )
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


@dataclass
class LoginInteractionResult:
    verified: bool
    intent: str
    state: str
    group_ref: str = ""
    candidate_seen: bool = False
    structurally_viable_seen: bool = False
    normal_attempts: int = 0
    fresh_observations: int = 0
    missing_observations: int = 0
    fallback_available: bool = False
    fallback_attempted: bool = False
    postcondition_checks: int = 0
    exception_classes: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class LoginSurfaceWaitResult:
    observation: LoginSurfaceObservation | None
    code: str = ""
    reason: str = ""


def _auto_login_selected_candidate(
    observation: LoginSurfaceObservation,
) -> LoginCandidateDescriptor | None:
    selected = [
        candidate
        for candidate in observation.candidates
        if observation.selected_group_ref
        and candidate.recipe.group_ref == observation.selected_group_ref
    ]
    values = selected or observation.candidates
    return max(values, key=lambda item: item.score, default=None)


def _auto_login_candidate_diagnostic(
    candidate: LoginCandidateDescriptor | None,
    *,
    probes: dict[str, bool | None] | None = None,
    node_replacement: bool = False,
) -> dict[str, Any] | None:
    if candidate is None:
        return None
    values = probes or {}
    return {
        "intent": candidate.intent or "unknown",
        "type_category": candidate.type_category,
        "autocomplete_category": candidate.autocomplete_category,
        "form_owned": candidate.form_owned,
        "attached": candidate.attached,
        "visible_probe": (
            values.get("visible")
            if "visible" in values
            else candidate.visible
        ),
        "enabled_probe": values.get("enabled"),
        "editable_probe": values.get("editable"),
        "readonly": candidate.readonly,
        "bounding_box_present": candidate.bounding_box_present,
        "viewport_intersection": candidate.viewport_intersecting,
        "node_replacement": bool(node_replacement),
    }


def _auto_login_diagnostic_base(
    observation: LoginSurfaceObservation,
    candidate: LoginCandidateDescriptor | None = None,
) -> dict[str, Any]:
    selected = candidate or _auto_login_selected_candidate(observation)
    frame_key: Any = (
        selected.recipe.frame_index if selected is not None else None
    )
    container_key: Any = (
        selected.recipe.group_ref if selected is not None else ""
    )
    document_key = ""
    mutation_epoch = 0
    for frame_index, raw_document, raw_mutation in observation.frame_epochs:
        if frame_key is None or frame_index == frame_key:
            frame_key = frame_index
            document_key = raw_document
            mutation_epoch = raw_mutation
            break
    counts = {"username": 0, "password": 0, "otp": 0, "other": 0}
    for value in observation.candidates:
        intent = (
            value.intent
            if value.intent in {"username", "password", "otp"}
            else "other"
        )
        counts[intent] += 1
    return {
        "state": observation.state,
        "url_category": observation.url_category,
        "frame_key": frame_key,
        "container_key": container_key,
        "document_key": document_key,
        "mutation_epoch": mutation_epoch,
        "candidate_counts": counts,
    }


def _record_auto_login_observation(
    observation: LoginSurfaceObservation,
) -> None:
    dump = _ACTIVE_AUTO_LOGIN_DIAGNOSTIC_DUMP.get()
    if dump is not None:
        try:
            dump._auto_login_last_observation = observation
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
    selected = _auto_login_selected_candidate(observation)
    payload = _auto_login_diagnostic_base(observation, selected)
    payload.update(
        {
            "event": "observation",
            "attempt_number": 0,
            "selected_candidate": _auto_login_candidate_diagnostic(
                selected
            ),
            "interaction": {
                "attempted": False,
                "kind": "none",
                "exception_class": "",
            },
            "postcondition": {"value_match": None},
            "terminal": {},
        }
    )
    _write_auto_login_diagnostic(payload)


def _record_auto_login_interaction(
    observation: LoginSurfaceObservation,
    candidate: LoginCandidateDescriptor | None,
    *,
    attempt_number: int,
    attempted: bool,
    kind: str,
    probes: dict[str, bool | None] | None = None,
    exception_class: str = "",
    value_match: bool | None = None,
    node_replacement: bool = False,
) -> None:
    payload = _auto_login_diagnostic_base(observation, candidate)
    payload.update(
        {
            "event": "interaction",
            "attempt_number": attempt_number,
            "selected_candidate": _auto_login_candidate_diagnostic(
                candidate,
                probes=probes,
                node_replacement=node_replacement,
            ),
            "interaction": {
                "attempted": attempted,
                "kind": kind,
                "exception_class": exception_class,
            },
            "postcondition": {"value_match": value_match},
            "terminal": {},
        }
    )
    _write_auto_login_diagnostic(payload)


def _auto_login_terminal_reason_category(code: str) -> str:
    return {
        "blocker_detected": "blocker",
        "challenge_detected": "challenge",
        "login_form_transition_timeout": "transition_timeout",
        "login_submit_control_not_found": "submit_not_dispatched",
        "login_submit_no_transition": "submit_no_transition",
        "password_field_not_found": "candidate_absent",
        "password_input_not_retained": "postcondition_negative",
        "unrecognized_surface": "unsupported_state",
        "unsupported_login_state": "unsupported_state",
        "username_field_not_found": "candidate_absent",
        "username_field_not_ready": "interaction_not_verified",
    }.get(str(code or ""), "none")


def _record_auto_login_terminal(
    observation: LoginSurfaceObservation,
    code: str,
) -> None:
    selected = _auto_login_selected_candidate(observation)
    payload = _auto_login_diagnostic_base(observation, selected)
    payload.update(
        {
            "event": "terminal",
            "attempt_number": 0,
            "selected_candidate": _auto_login_candidate_diagnostic(
                selected
            ),
            "interaction": {
                "attempted": False,
                "kind": "none",
                "exception_class": "",
            },
            "postcondition": {"value_match": None},
            "terminal": {
                "owner": "auto_login_transaction_coordinator",
                "code": code,
                "reason_category": (
                    _auto_login_terminal_reason_category(code)
                ),
            },
        }
    )
    _write_auto_login_diagnostic(payload)


_LOGIN_SURFACE_SCRIPT = r"""() => {
  const INPUTS = "input,textarea,[contenteditable='true'],[contenteditable='plaintext-only']";
  const CONTROLS = "button,input[type='submit'],input[type='button'],[role='button']";
  const key = Symbol.for('sparkgrid.login.surface.v1');
  let state = globalThis[key];
  if (!state) {
    state = {
      documentRef: (
        (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function')
          ? globalThis.crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`
      ),
      mutation: 0,
      sequence: 0,
      nodeRefs: new WeakMap(),
      groupRefs: new WeakMap(),
    };
    const root = document.documentElement;
    if (root && typeof MutationObserver === 'function') {
      state.observer = new MutationObserver(() => { state.mutation += 1; });
      state.observer.observe(root, {
        subtree: true,
        childList: true,
        attributes: true,
      });
    }
    globalThis[key] = state;
  }
  const token = (map, node, prefix) => {
    if (!node) return '';
    let value = map.get(node);
    if (!value) {
      state.sequence += 1;
      value = `${prefix}${state.sequence}`;
      map.set(node, value);
    }
    return value;
  };
  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return (
      rect.width > 1 && rect.height > 1 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      Number.parseFloat(style.opacity || '1') > 0.01
    );
  };
  const viewport = (rect) => (
    rect.width > 1 && rect.height > 1 &&
    rect.bottom > 0 && rect.right > 0 &&
    rect.top < innerHeight && rect.left < innerWidth
  );
  const groupFor = (el) => {
    if (el.form) return el.form;
    let current = el.parentElement;
    while (current && current !== document.body) {
      const inputCount = current.querySelectorAll(INPUTS).length;
      if (
        inputCount > 0 &&
        (inputCount > 1 || current.querySelector(CONTROLS))
      ) return current;
      current = current.parentElement;
    }
    return el.parentElement || document.body || document.documentElement;
  };
  const category = (value, allowed, fallback = 'other') => {
    const normalized = String(value || '').trim().toLowerCase();
    return allowed.includes(normalized) ? normalized : fallback;
  };
  const nodes = [...new Set(document.querySelectorAll(INPUTS))];
  const candidates = nodes.map((el, index) => {
    const rect = el.getBoundingClientRect();
    const group = groupFor(el);
    const groupVisibleControls = [...group.querySelectorAll(CONTROLS)]
      .some(control => visible(control) && viewport(control.getBoundingClientRect()));
    const action = String(
      (el.form && el.form.getAttribute('action')) ||
      (group && group.getAttribute && group.getAttribute('action')) ||
      ''
    ).toLowerCase();
    const groupChallenge = (
      action.includes('/challenge') ||
      action.includes('/checkpoint') ||
      !!group.querySelector(
        "form[action*='/challenge' i],form[action*='/checkpoint' i]," +
        "[data-testid*='challenge' i],[data-testid*='checkpoint' i]"
      )
    );
    const rawType = String(
      el.getAttribute('type') ||
      (el.tagName.toLowerCase() === 'textarea' ? 'textarea' : 'text')
    ).toLowerCase();
    const rawAutocomplete = String(el.getAttribute('autocomplete') || '')
      .trim().toLowerCase();
    const rawName = String(el.getAttribute('name') || '').trim().toLowerCase();
    const inputMode = String(el.getAttribute('inputmode') || '').trim().toLowerCase();
    const maxLength = Number(el.getAttribute('maxlength') || 0);
    let semantic = 'generic';
    if (rawType === 'password' || rawAutocomplete === 'current-password') {
      semantic = 'password';
    } else if (
      rawAutocomplete === 'one-time-code' ||
      (
        ['numeric', 'decimal'].includes(inputMode) &&
        maxLength >= 4 && maxLength <= 8
      )
    ) {
      semantic = 'otp';
    } else if (
      ['username', 'email', 'tel'].includes(rawAutocomplete) ||
      ['email', 'tel'].includes(rawType) ||
      /(^|[_-])(user(name)?|login|identifier|email|phone|mobile)([_-]|$)/.test(rawName)
    ) {
      semantic = 'username';
    }
    return {
      index,
      node_ref: token(state.nodeRefs, el, 'n'),
      group_ref: token(state.groupRefs, group, 'g'),
      semantic,
      type_category: category(
        rawType,
        ['text', 'email', 'tel', 'password', 'number', 'search', 'textarea'],
      ),
      autocomplete_category: category(
        rawAutocomplete,
        ['username', 'email', 'tel', 'current-password', 'one-time-code', 'off', 'new-password'],
        rawAutocomplete ? 'other' : 'none',
      ),
      numeric_mode: ['numeric', 'decimal'].includes(inputMode),
      max_length: maxLength,
      form_owned: !!el.form,
      attached: !!el.isConnected,
      visible: visible(el),
      viewport_intersecting: viewport(rect),
      bounding_box_present: rect.width > 1 && rect.height > 1,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      readonly: !!el.readOnly || el.getAttribute('contenteditable') === 'false',
      covered: false,
      group_has_submit: groupVisibleControls,
      group_challenge: groupChallenge,
    };
  });
  const busy = [...document.querySelectorAll(
    "[aria-busy='true'],[role='progressbar']"
  )].some(visible);
  return {
    document_ref: state.documentRef,
    mutation_epoch: state.mutation,
    ready_state: String(document.readyState || ''),
    loading: document.readyState !== 'complete' || busy || !document.body,
    candidates,
  };
}"""


def _trusted_login_frames(page) -> list[tuple[int, Any]]:
    try:
        frames = list(getattr(page, "frames", []) or [])
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        frames = []
    try:
        main_frame = getattr(page, "main_frame", None)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        main_frame = None
    if not frames and main_frame is not None:
        frames = [main_frame]
    trusted: list[tuple[int, Any]] = []
    for index, frame in enumerate(frames):
        if frame is main_frame:
            trusted.append((index, frame))
            continue
        try:
            parsed = urlparse(str(getattr(frame, "url", "") or ""))
            host = str(parsed.hostname or "").lower()
            scheme = str(parsed.scheme or "").lower()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
        if scheme in {"about", "data"} or host == "instagram.com" or host.endswith(
            ".instagram.com"
        ):
            trusted.append((index, frame))
    return trusted


def _login_route_category(page) -> str:
    try:
        parsed = urlparse(str(getattr(page, "url", "") or ""))
        path = str(parsed.path or "").lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return "unknown"
    if "/challenge" in path or "/checkpoint" in path:
        return "challenge"
    if "two_factor" in path or "two_step_verification" in path:
        return "two_factor"
    if "/consent" in path:
        return "consent"
    if "/accounts" in path or "/login" in path:
        return "login_family"
    return "instagram" if str(parsed.hostname or "").lower().endswith(
        "instagram.com"
    ) else "unknown"


def _scoped_consent_surface(page) -> bool:
    if _login_route_category(page) == "consent":
        return True
    try:
        dialog = inspect_dialog(page)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return False
    return bool(
        dialog.get("present")
        and str(dialog.get("category") or "") == "cookie_consent"
    )


def _candidate_score(raw: dict[str, Any], intent: str) -> int:
    score = 0
    if raw.get("visible"):
        score += 60
    else:
        score -= 120
    if raw.get("viewport_intersecting"):
        score += 35
    else:
        score -= 60
    if raw.get("bounding_box_present"):
        score += 20
    else:
        score -= 50
    if raw.get("attached"):
        score += 15
    else:
        score -= 100
    if raw.get("form_owned"):
        score += 25
    if raw.get("group_has_submit"):
        score += 20
    if raw.get("covered"):
        score -= 15
    if raw.get("disabled"):
        score -= 30
    if raw.get("readonly"):
        score -= 30
    semantic = str(raw.get("semantic") or "")
    if semantic == intent:
        score += 80
    elif intent == "username" and semantic == "generic":
        score += 10
    return score


def _classify_login_surface(
    raw_candidates: list[dict[str, Any]],
    *,
    loading: bool,
    url_category: str,
    authenticated: bool = False,
    consent: bool = False,
    frame_epochs: tuple[tuple[int, str, int], ...] = (),
) -> LoginSurfaceObservation:
    candidates: list[LoginCandidateDescriptor] = []
    groups: dict[str, list[LoginCandidateDescriptor]] = {}
    group_has_submit: dict[str, bool] = {}
    group_challenge: dict[str, bool] = {}
    grouped_raw: dict[str, list[dict[str, Any]]] = {}
    for raw in raw_candidates:
        frame_index = int(raw.get("frame_index") or 0)
        group_key = f"{frame_index}:{str(raw.get('group_ref') or '')}"
        grouped_raw.setdefault(group_key, []).append(raw)
    segmented_otp_groups = {
        group_key
        for group_key, values in grouped_raw.items()
        if 4 <= len(values) <= 8
        and all(
            bool(item.get("numeric_mode"))
            and int(item.get("max_length") or 0) == 1
            for item in values
        )
    }

    for raw in raw_candidates:
        frame_index = int(raw.get("frame_index") or 0)
        raw_group_ref = str(raw.get("group_ref") or "")
        group_ref = f"{frame_index}:{raw_group_ref}"
        semantic = str(raw.get("semantic") or "generic")
        if group_ref in segmented_otp_groups:
            semantic = "otp"
        type_category = str(raw.get("type_category") or "other")
        intent = semantic if semantic in {"password", "otp", "username"} else ""
        if not intent and type_category not in {"password", "number", "search"}:
            intent = "username"
        recipe = LoginCandidateRecipe(
            frame_index=frame_index,
            node_index=int(raw.get("index") or 0),
            node_ref=str(raw.get("node_ref") or ""),
            group_ref=group_ref,
        )
        candidate = LoginCandidateDescriptor(
            recipe=recipe,
            intent=intent,
            semantic=semantic,
            type_category=type_category,
            autocomplete_category=str(
                raw.get("autocomplete_category") or "none"
            ),
            form_owned=bool(raw.get("form_owned")),
            attached=bool(raw.get("attached")),
            visible=bool(raw.get("visible")),
            viewport_intersecting=bool(raw.get("viewport_intersecting")),
            bounding_box_present=bool(raw.get("bounding_box_present")),
            disabled=bool(raw.get("disabled")),
            readonly=bool(raw.get("readonly")),
            covered=bool(raw.get("covered")),
            group_has_submit=bool(raw.get("group_has_submit")),
            group_challenge=bool(raw.get("group_challenge")),
            score=_candidate_score(raw, intent),
        )
        candidates.append(candidate)
        groups.setdefault(recipe.group_ref, []).append(candidate)
        group_has_submit[recipe.group_ref] = bool(raw.get("group_has_submit"))
        group_challenge[recipe.group_ref] = bool(raw.get("group_challenge"))

    fingerprint_parts: list[str] = [url_category, "loading" if loading else "stable"]
    classified_groups: list[tuple[int, str, str]] = []
    for group_ref, values in groups.items():
        passwords = [item for item in values if item.intent == "password"]
        otps = [item for item in values if item.intent == "otp"]
        explicit_usernames = [
            item
            for item in values
            if item.intent == "username" and item.semantic == "username"
        ]
        usernames = [
            item
            for item in values
            if item.intent == "username"
            and (
                item.semantic == "username"
                or bool(passwords)
            )
        ]
        visible_score = max((item.score for item in values), default=-500)
        if group_challenge.get(group_ref):
            group_state, priority = "challenge", 1000
        elif otps and not passwords:
            group_state, priority = "two_factor", 900
        elif usernames and passwords:
            group_state, priority = "login_combined", 700
        elif passwords:
            group_state, priority = "login_password_only", 600
        elif explicit_usernames and group_has_submit.get(group_ref):
            group_state, priority = "login_username_first", 500
        else:
            group_state, priority = "unrelated", 0
        classified_groups.append(
            (priority + visible_score, group_state, group_ref)
        )
        fingerprint_parts.append(
            f"{group_state}:{len(usernames)}:{len(passwords)}:{len(otps)}:"
            f"{int(group_has_submit.get(group_ref, False))}"
        )

    classified_groups.sort(reverse=True)
    selected_state = classified_groups[0][1] if classified_groups else ""
    selected_group = classified_groups[0][2] if classified_groups else ""
    if consent:
        state, reason, selected_group = (
            "consent_blocker",
            "scoped consent surface is active",
            "",
        )
    elif selected_state == "challenge" or url_category == "challenge":
        state, reason = "challenge", "scoped challenge structure is active"
    elif selected_state == "two_factor" or url_category == "two_factor":
        state, reason = "two_factor", "OTP verification structure is active"
    elif selected_state in LOGIN_CREDENTIAL_STATES:
        state, reason = selected_state, "coherent credential group is active"
    elif authenticated:
        state, reason, selected_group = (
            "authenticated",
            "authenticated state confirmed without an active credential form",
            "",
        )
    elif loading:
        state, reason, selected_group = (
            "transitioning",
            "document or login surface is transitioning",
            "",
        )
    else:
        state, reason, selected_group = (
            "unsupported_stable",
            "stable document has no coherent login surface",
            "",
        )

    return LoginSurfaceObservation(
        state=state,
        reason=reason,
        url_category=url_category,
        loading=bool(loading),
        semantic_fingerprint="|".join(sorted(fingerprint_parts)),
        frame_epochs=frame_epochs,
        candidates=candidates,
        selected_group_ref=selected_group,
    )


def _observe_login_surface(page) -> LoginSurfaceObservation:
    raw_candidates: list[dict[str, Any]] = []
    frame_epochs: list[tuple[int, str, int]] = []
    loading = False
    for frame_index, frame in _trusted_login_frames(page):
        try:
            payload = frame.evaluate(_LOGIN_SURFACE_SCRIPT)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
        if (
            not isinstance(payload, dict)
            or "document_ref" not in payload
            or not isinstance(payload.get("candidates"), list)
        ):
            continue
        document_ref = str(payload.get("document_ref") or "")
        mutation_epoch = int(payload.get("mutation_epoch") or 0)
        frame_epochs.append((frame_index, document_ref, mutation_epoch))
        loading = loading or bool(payload.get("loading"))
        for raw in list(payload.get("candidates") or []):
            if isinstance(raw, dict):
                raw_candidates.append({**raw, "frame_index": frame_index})
    url_category = _login_route_category(page)
    has_credential_structure = any(
        str(item.get("semantic") or "") in {"password", "otp"}
        for item in raw_candidates
    )
    authenticated = False
    if not has_credential_structure and url_category not in {
        "challenge", "two_factor", "consent"
    }:
        try:
            authenticated = bool(_authenticated_session_present(page))
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            authenticated = False
    observation = _classify_login_surface(
        raw_candidates,
        loading=loading or not frame_epochs,
        url_category=url_category,
        authenticated=authenticated,
        consent=_scoped_consent_surface(page),
        frame_epochs=tuple(frame_epochs),
    )
    try:
        _record_auto_login_observation(observation)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return observation


def _resolve_login_candidate(page, candidate: LoginCandidateDescriptor):
    frames = dict(_trusted_login_frames(page))
    frame = frames.get(candidate.recipe.frame_index)
    if frame is None:
        return None
    try:
        locator = frame.locator(LOGIN_INPUT_LIKE_SELECTOR).nth(
            candidate.recipe.node_index
        )
        if int(locator.count() or 0) != 1:
            return None
        node_ref = str(
            locator.evaluate(
                """el => {
                  const state=globalThis[Symbol.for('sparkgrid.login.surface.v1')];
                  return state && state.nodeRefs.get(el) || '';
                }"""
            )
            or ""
        )
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return None
    return locator if node_ref == candidate.recipe.node_ref else None


def _fresh_login_candidate(
    page, intent: str, preferred_group_ref: str = ""
) -> tuple[
    LoginSurfaceObservation,
    LoginCandidateDescriptor | None,
    Any | None,
]:
    observation = _observe_login_surface(page)
    for candidate in observation.candidates_for(intent, preferred_group_ref):
        locator = _resolve_login_candidate(page, candidate)
        if locator is not None:
            return observation, candidate, locator
    return observation, None, None


def _wait_for_login_surface_change(
    page,
    observation: LoginSurfaceObservation,
    *,
    timeout_ms: int = 900,
) -> None:
    if not observation.frame_epochs:
        return
    frame_index, document_ref, mutation_epoch = observation.frame_epochs[0]
    frame = dict(_trusted_login_frames(page)).get(frame_index)
    if frame is None:
        return
    try:
        frame.wait_for_function(
            """previous => {
              const state=globalThis[Symbol.for('sparkgrid.login.surface.v1')];
              return !state ||
                state.documentRef !== previous.documentRef ||
                state.mutation !== previous.mutation ||
                document.readyState !== 'complete';
            }""",
            arg={
                "documentRef": document_ref,
                "mutation": mutation_epoch,
            },
            timeout=max(50, int(timeout_ms)),
        )
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _login_input_count(page) -> int:
    """Compatibility metric: count unique input-like nodes, never form authority."""
    return len(_observe_login_surface(page).candidates)


def _login_fields(page, visible_timeout_ms: int = 600):
    """Compatibility wrapper returning freshly resolved structural candidates."""
    del visible_timeout_ms
    observation = _observe_login_surface(page)
    user = next(
        iter(observation.candidates_for("username", observation.selected_group_ref)),
        None,
    )
    password = next(
        iter(observation.candidates_for("password", observation.selected_group_ref)),
        None,
    )
    return (
        _resolve_login_candidate(page, user) if user else None,
        _resolve_login_candidate(page, password) if password else None,
    )


def _login_fields_available(page) -> bool:
    return _observe_login_surface(page).state == "login_combined"


def _body_text_len(page) -> int:
    try:
        return len((page.locator("body").inner_text(timeout=1200) or "").strip())
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return 0


def _input_value(locator) -> str:
    try:
        return str(locator.input_value(timeout=1500) or "")
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return ""


def _login_values_confirmed(user_field, pass_field, username: str, password: str) -> bool:
    return _input_value(user_field).strip() == username.strip() and _input_value(pass_field) == password


def _login_field_ready(field) -> bool:
    """Sample weak actionability probes for diagnostics only."""
    if not field:
        return False
    for method in ("is_visible", "is_enabled", "is_editable"):
        check = getattr(field, method, None)
        if check is None:
            continue
        try:
            if not bool(check(timeout=900)):
                return False
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return False
    return True


def _set_login_form_failure(dump, code: str) -> None:
    # LiveDump is per-account/per-worker.  This carries only a normalized code
    # back to do_auto_login, never field content or credentials.
    try:
        dump.login_form_failure_code = code
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _login_form_failure_code(dump) -> str:
    return str(getattr(dump, "login_form_failure_code", "") or "login_form_not_ready")


def _login_page_frame_identity(page) -> tuple[int, int, str]:
    """Compatibility identity enriched with the structural document epoch."""
    observation = _observe_login_surface(page)
    epoch = observation.frame_epochs[0] if observation.frame_epochs else (-1, "", -1)
    return id(page), hash(epoch), observation.url_category


def _wait_for_username_field_ready(
    page,
    dump: LiveDump,
    account: str,
    *,
    total_seconds: float = 12.0,
    required_missing_reads: int = 3,
    clock=None,
    wait=None,
):
    """Compatibility wait returning a fresh structural locator.

    This helper has no terminal authority. Auto Login uses the interaction
    transaction below, which attempts real input before any not-ready outcome.
    """
    now = clock or time.monotonic
    deadline = now() + max(0.0, float(total_seconds))
    del dump, account, required_missing_reads, wait
    last_observation = _observe_login_surface(page)
    while now() <= deadline:
        observation, candidate, locator = _fresh_login_candidate(
            page, "username"
        )
        if candidate is not None and locator is not None:
            return locator
        remaining = deadline - now()
        if remaining <= 0:
            break
        _wait_for_login_surface_change(
            page,
            last_observation,
            timeout_ms=int(min(remaining, 0.9) * 1000),
        )
        last_observation = observation
    return None


def _safe_clear_and_type(field, value: str, hum=None) -> bool:
    """Attempt normal Playwright input; the caller owns fresh verification."""
    try:
        if hum is not None:
            try:
                hum.move_to_locator(field, timeout=2500, allow_overshoot=False)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
        field.click(timeout=4000)
        field.fill("", timeout=5000)
        field.fill(value, timeout=8000)
        return True
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return False


def _verified_field_input(field, expected: str, hum=None, *, username: bool) -> tuple[bool, bool]:
    """Legacy single-locator helper retained for non-transaction test callers."""
    _safe_clear_and_type(field, expected, hum)
    value = _input_value(field)
    return (
        value.strip() == expected.strip() if username else value == expected,
        False,
    )


def _react_fill(locator, value: str) -> None:
    """Set an input through its native setter and notify React without logging it."""
    locator.evaluate("""(el, value) => {
        if (el.isContentEditable) {
            el.textContent = value;
        } else {
            const proto = Object.getPrototypeOf(el);
            const descriptor = Object.getOwnPropertyDescriptor(proto, 'value') ||
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            const tracker = el._valueTracker;
            if (tracker && typeof tracker.setValue === 'function') tracker.setValue('');
            if (descriptor && descriptor.set) descriptor.set.call(el, value);
            else el.value = value;
        }
        try { el.dispatchEvent(new InputEvent('input', {bubbles:true, data:value, inputType:'insertText'})); }
        catch (_) { el.dispatchEvent(new Event('input', {bubbles:true})); }
        el.dispatchEvent(new Event('change', {bubbles:true}));
    }""", value)


def _value_postcondition(intent: str, actual: str, expected: str) -> bool:
    return (
        str(actual or "").strip() == str(expected or "").strip()
        if intent == "username"
        else str(actual or "") == str(expected or "")
    )


def _fresh_login_value_postcondition(
    page,
    intent: str,
    expected: str,
    preferred_group_ref: str,
) -> tuple[
    bool,
    LoginSurfaceObservation,
    LoginCandidateDescriptor | None,
]:
    observation, candidate, locator = _fresh_login_candidate(
        page, intent, preferred_group_ref
    )
    if candidate is None or locator is None:
        return False, observation, candidate
    try:
        value = str(
            locator.evaluate(
                """el => el.isContentEditable
                  ? String(el.textContent || '')
                  : String(el.value || '')"""
            )
            or ""
        )
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        value = ""
    return (
        _value_postcondition(intent, value, expected),
        observation,
        candidate,
    )


def _interaction_state_still_requires(
    observation: LoginSurfaceObservation, intent: str
) -> bool:
    if intent == "username":
        return observation.state in LOGIN_USERNAME_STATES
    return observation.state in {"login_combined", "login_password_only"}


def _interact_and_verify_login_candidate(
    page,
    *,
    intent: str,
    expected: str,
    preferred_group_ref: str,
    hum=None,
    max_normal_attempts: int = 2,
    total_seconds: float = 10.0,
    clock=None,
) -> LoginInteractionResult:
    """Use real input plus a fresh value read as the credential authority."""
    now = clock or time.monotonic
    deadline = now() + max(0.2, float(total_seconds))
    normal_attempts = 0
    fresh_observations = 0
    missing_observations = 0
    candidate_seen = False
    viable_seen = False
    postcondition_checks = 0
    fallback_attempted = False
    fallback_available = False
    human_action_failures = 0
    exceptions: list[str] = []
    last_state = "transitioning"
    last_group = preferred_group_ref
    last_observation = _observe_login_surface(page)

    while normal_attempts < max(2, int(max_normal_attempts)) and now() < deadline:
        observation, candidate, locator = _fresh_login_candidate(
            page, intent, last_group
        )
        fresh_observations += 1
        last_observation = observation
        last_state = observation.state
        if not _interaction_state_still_requires(observation, intent):
            break
        if candidate is None or locator is None:
            missing_observations += 1
            _wait_for_login_surface_change(
                page,
                observation,
                timeout_ms=int(min(0.9, max(0.05, deadline - now())) * 1000),
            )
            continue

        candidate_seen = True
        viable_seen = viable_seen or candidate.structurally_viable
        last_group = candidate.recipe.group_ref
        # Weak probes are deliberately sampled but never gate the real action.
        probes: dict[str, bool | None] = {
            "visible": None,
            "enabled": None,
            "editable": None,
        }
        for method in ("is_visible", "is_enabled", "is_editable"):
            try:
                probe = getattr(locator, method, None)
                if probe is not None:
                    probes[method.removeprefix("is_")] = bool(
                        probe(timeout=500)
                    )
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass

        # Probes are observations, so resolve the recipe again before acting.
        # A React replacement during a probe invalidates the prior locator.
        locator = _resolve_login_candidate(page, candidate)
        if locator is None:
            try:
                _record_auto_login_interaction(
                    observation,
                    candidate,
                    attempt_number=normal_attempts + 1,
                    attempted=False,
                    kind="reacquire",
                    probes=probes,
                    exception_class="NodeReplaced",
                    value_match=None,
                    node_replacement=True,
                )
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            _wait_for_login_surface_change(
                page,
                observation,
                timeout_ms=int(
                    min(0.9, max(0.05, deadline - now())) * 1000
                ),
            )
            continue

        normal_attempts += 1
        attempt_exception = ""
        action_accepted = True
        try:
            if hum is not None:
                action_accepted = bool(hum.type_text(
                    expected,
                    locator=locator,
                    clear=True,
                    sensitive=True,
                    allow_typos=False,
                ))
                if not action_accepted:
                    human_action_failures += 1
                    attempt_exception = "HumanActionNotExecuted"
                    exceptions.append(attempt_exception)
            else:
                locator.click(timeout=1800)
                locator.fill("", timeout=2200)
                locator.fill(expected, timeout=3500)
        except Exception as exc:
            logger.debug("%s: %s", type(exc).__name__, exc)
            action_accepted = False
            if hum is not None:
                human_action_failures += 1
            attempt_exception = type(exc).__name__
            exceptions.append(attempt_exception)

        matched, post_observation, post_candidate = (
            _fresh_login_value_postcondition(
                page, intent, expected, last_group
            )
        )
        matched = bool(matched and action_accepted)
        fresh_observations += 1
        postcondition_checks += 1
        last_observation = post_observation
        last_state = post_observation.state
        if post_candidate is not None:
            candidate_seen = True
            viable_seen = viable_seen or post_candidate.structurally_viable
            last_group = post_candidate.recipe.group_ref
        node_replacement = bool(
            post_candidate is None
            or post_candidate.recipe.node_ref
            != candidate.recipe.node_ref
        )
        try:
            _record_auto_login_interaction(
                post_observation,
                post_candidate or candidate,
                attempt_number=normal_attempts,
                attempted=True,
                kind="click_fill",
                probes=probes,
                exception_class=attempt_exception,
                value_match=matched,
                node_replacement=node_replacement,
            )
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        if matched:
            return LoginInteractionResult(
                verified=True,
                intent=intent,
                state=last_state,
                group_ref=last_group,
                candidate_seen=candidate_seen,
                structurally_viable_seen=viable_seen,
                normal_attempts=normal_attempts,
                fresh_observations=fresh_observations,
                missing_observations=missing_observations,
                postcondition_checks=postcondition_checks,
                exception_classes=tuple(exceptions),
                reason="fresh value postcondition matched",
            )
        _wait_for_login_surface_change(
            page,
            post_observation,
            timeout_ms=int(min(0.9, max(0.05, deadline - now())) * 1000),
        )

    if (
        normal_attempts >= 2
        and now() < deadline
        and not (
            hum is not None
            and human_action_failures >= normal_attempts
        )
    ):
        observation, candidate, locator = _fresh_login_candidate(
            page, intent, last_group
        )
        fresh_observations += 1
        last_observation = observation
        last_state = observation.state
        if candidate is not None and locator is not None:
            candidate_seen = True
            viable_seen = viable_seen or candidate.structurally_viable
            last_group = candidate.recipe.group_ref
            if not candidate.readonly and not candidate.disabled:
                fallback_available = True
                fallback_attempted = True
                fallback_exception = ""
                try:
                    _react_fill(locator, expected)
                except Exception as exc:
                    logger.debug("%s: %s", type(exc).__name__, exc)
                    fallback_exception = type(exc).__name__
                    exceptions.append(fallback_exception)
                matched, post_observation, post_candidate = (
                    _fresh_login_value_postcondition(
                        page, intent, expected, last_group
                    )
                )
                fresh_observations += 1
                postcondition_checks += 1
                last_observation = post_observation
                last_state = post_observation.state
                if post_candidate is not None:
                    last_group = post_candidate.recipe.group_ref
                try:
                    _record_auto_login_interaction(
                        post_observation,
                        post_candidate or candidate,
                        attempt_number=normal_attempts + 1,
                        attempted=True,
                        kind="native_setter",
                        exception_class=fallback_exception,
                        value_match=matched,
                        node_replacement=bool(
                            post_candidate is None
                            or post_candidate.recipe.node_ref
                            != candidate.recipe.node_ref
                        ),
                    )
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
                if matched:
                    return LoginInteractionResult(
                        verified=True,
                        intent=intent,
                        state=last_state,
                        group_ref=last_group,
                        candidate_seen=True,
                        structurally_viable_seen=viable_seen,
                        normal_attempts=normal_attempts,
                        fresh_observations=fresh_observations,
                        missing_observations=missing_observations,
                        fallback_available=True,
                        fallback_attempted=True,
                        postcondition_checks=postcondition_checks,
                        exception_classes=tuple(exceptions),
                        reason="fresh value postcondition matched after native setter",
                    )

    reason = (
        "candidate absent after fresh structural observations"
        if not candidate_seen
        else "fresh value postconditions remained negative"
    )
    if not _interaction_state_still_requires(last_observation, intent):
        reason = "login surface changed before value verification"
    return LoginInteractionResult(
        verified=False,
        intent=intent,
        state=last_state,
        group_ref=last_group,
        candidate_seen=candidate_seen,
        structurally_viable_seen=viable_seen,
        normal_attempts=normal_attempts,
        fresh_observations=fresh_observations,
        missing_observations=missing_observations,
        fallback_available=fallback_available,
        fallback_attempted=fallback_attempted,
        postcondition_checks=postcondition_checks,
        exception_classes=tuple(exceptions),
        reason=reason,
    )


def _looks_like_cookie_or_session(value: str) -> bool:
    v = str(value or "").strip().lower()
    if not v:
        return False
    signals = [
        "ds_user_id",
        "csrftoken",
        "sessionid",
        "mid=",
        "ig_did",
        "rur=",
        "authorization",
        "android-",
    ]
    if any(s in v for s in signals):
        return True
    return len(v) > 180 and (";" in v or "|" in v)


def _detect_login_rejection(page) -> str:
    """Classify only fresh, contextual login rejection evidence."""
    try:
        txt = (page.locator("body").inner_text(timeout=1200) or "").strip().lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        txt = ""
    try:
        password_present = page.locator(
            "input[type='password'], input[name='password'], "
            "input[autocomplete='current-password']"
        ).count() > 0
        login_form_present = page.locator("form").count() > 0 and password_present
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        login_form_present = False

    # Explicit password evidence outranks generic blockers, but only on the
    # live Instagram login form.  The bare word "incorrect" is never enough.
    if login_form_present and any(
        marker in txt
        for marker in (
            "login information you entered is incorrect",
            "password was incorrect",
            "incorrect password",
            "wrong password",
            "the password you entered is incorrect",
        )
    ):
        return "explicit_password_rejection: instagram rejected the password"
    if login_form_present and any(
        marker in txt
        for marker in (
            "username doesn't belong to an account",
            "username does not belong to an account",
            "user not found",
            "no account found",
        )
    ):
        return "username_rejection: instagram rejected the username"
    if any(
        marker in txt
        for marker in (
            "code is incorrect",
            "security code is incorrect",
            "please check the code",
            "invalid code",
            "code you entered",
        )
    ):
        return "invalid_2fa: instagram rejected the 2FA code"
    return ""


def _wait_for_instagram_login_form(page, dump: LiveDump, total_seconds: int = 24, account: str = "") -> bool:
    """Wait for a coherent login group; observations are never locators."""
    deadline = time.monotonic() + max(0.2, float(total_seconds))
    observation = _observe_login_surface(page)
    while time.monotonic() < deadline:
        _dismiss_instagram_consent(page, dump, account)
        ready, blocker_state, _reason = _login_blocker_first(
            page,
            dump,
            account,
            "login surface",
            wait_seconds=0.2,
            structural_login_surface=True,
        )
        if not ready or blocker_state in {
            "checkpoint", "restricted", "suspended", "unknown_popup"
        }:
            return False
        observation = _observe_login_surface(page)
        if observation.state in LOGIN_CREDENTIAL_STATES:
            return True
        if observation.state in {
            "authenticated", "two_factor", "challenge", "unsupported_stable"
        }:
            return False
        _wait_for_login_surface_change(
            page,
            observation,
            timeout_ms=int(
                min(0.9, max(0.05, deadline - time.monotonic())) * 1000
            ),
        )
    return _observe_login_surface(page).state in LOGIN_CREDENTIAL_STATES


def _classify_login_form_failure(page) -> str:
    observation = _observe_login_surface(page)
    return (
        f"state={observation.state}; url_category={observation.url_category}; "
        f"reason={observation.reason}"
    )


ACTION_NOT_EXECUTED = "ACTION_NOT_EXECUTED"
ACTION_ACCEPTED_TRANSITIONING = "ACTION_ACCEPTED_TRANSITIONING"
KNOWN_NEXT_STATE = "KNOWN_NEXT_STATE"
STABLE_SAME_STATE_WITH_ERROR = "STABLE_SAME_STATE_WITH_ERROR"
UNKNOWN_STABLE_STATE = "UNKNOWN_STABLE_STATE"
TERMINAL_FAILURE = "TERMINAL_FAILURE"


def _safe_request_path(value: str) -> str:
    try:
        path = str(urlparse(str(value or "")).path or "/")
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    return path[:300]


def _safe_network_failure(value) -> str:
    if isinstance(value, dict):
        raw = str(value.get("errorText") or value.get("error") or "")
    else:
        raw = str(value or "")
    lowered = raw.lower()
    for needle, label in (
        ("timed_out", "timeout"),
        ("timeout", "timeout"),
        ("connection_reset", "connection_reset"),
        ("connection_closed", "connection_closed"),
        ("name_not_resolved", "name_not_resolved"),
        ("tunnel_connection_failed", "proxy_tunnel_failed"),
        ("proxy_connection_failed", "proxy_connection_failed"),
        ("internet_disconnected", "internet_disconnected"),
        ("aborted", "aborted"),
        ("cancel", "cancelled"),
        ("blocked", "blocked"),
    ):
        if needle in lowered:
            return label
    return "network_failure" if raw else "unknown_failure"


class LoginPostActionTelemetry:
    """Safe, temporary network/UI evidence scoped to one login submit."""

    def __init__(self, page, dump: LiveDump | None = None):
        self.page = page
        self.dump = dump
        self.started = time.monotonic()
        self.requests: dict[int, dict] = {}
        self.active = False
        self.attached_events: set[str] = set()
        self.main_frame_navigations = 0
        self.iteration = 0
        self._handlers = {
            "request": self._request_started,
            "response": self._response,
            "requestfinished": self._request_finished,
            "requestfailed": self._request_failed,
            "framenavigated": self._frame_navigated,
        }

    def _emit(self, event: str, **fields) -> None:
        payload = {
            "event": str(event),
            "elapsed_ms": round((time.monotonic() - self.started) * 1000, 1),
        }
        payload.update(fields)
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        print("[LOGIN_POST_ACTION] " + serialized, flush=True)
        if self.dump is not None:
            try:
                self.dump.writer.append_text(
                    self.dump.root / "login_post_action.jsonl",
                    serialized + "\n",
                )
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass

    @staticmethod
    def _is_safe_request(request) -> bool:
        try:
            parsed = urlparse(str(request.url or ""))
            host = str(parsed.hostname or "").lower()
            resource_type = str(request.resource_type or "").lower()
            return (
                (host == "instagram.com" or host.endswith(".instagram.com"))
                and resource_type in {"document", "xhr", "fetch"}
            )
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return False

    def start(self) -> None:
        if self.active:
            return
        self.active = True
        for event, handler in self._handlers.items():
            try:
                self.page.on(event, handler)
                self.attached_events.add(event)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
        self._emit(
            "telemetry_started",
            url_path=_safe_request_path(getattr(self.page, "url", "")),
            worker_pid=os.getpid(),
            runtime_commit=str(os.environ.get("SPARKGRID_LIVE_COMMIT") or "")[:40],
            source_path=str(Path(__file__).resolve()),
        )

    def stop(self, outcome: str = "") -> None:
        if not self.active:
            return
        for event, handler in self._handlers.items():
            try:
                self.page.remove_listener(event, handler)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                try:
                    self.page.off(event, handler)
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
        for data in list(self.requests.values()):
            if not data.get("terminal"):
                self._emit(
                    "request_pending",
                    request_id=data["request_id"],
                    method=data["method"],
                    url_path=data["url_path"],
                    request_elapsed_ms=round((time.monotonic() - data["started"]) * 1000, 1),
                )
        self._emit("telemetry_stopped", outcome=str(outcome or ""))
        self.active = False

    def _request_started(self, request) -> None:
        if not self.active or not self._is_safe_request(request):
            return
        key = id(request)
        request_id = f"r{len(self.requests) + 1}"
        redirected_from = getattr(request, "redirected_from", None)
        data = {
            "request_id": request_id,
            "method": str(getattr(request, "method", "") or "").upper()[:12],
            "url_path": _safe_request_path(getattr(request, "url", "")),
            "resource_type": str(getattr(request, "resource_type", "") or "")[:20],
            "started": time.monotonic(),
            "terminal": False,
            "outcome": "pending",
            "status": 0,
            "failure_reason": "",
        }
        self.requests[key] = data
        self._emit(
            "request_started",
            request_id=request_id,
            method=data["method"],
            url_path=data["url_path"],
            resource_type=data["resource_type"],
            redirected=bool(redirected_from),
            redirected_from_path=(
                _safe_request_path(getattr(redirected_from, "url", ""))
                if redirected_from else ""
            ),
        )

    def _response(self, response) -> None:
        request = getattr(response, "request", None)
        data = self.requests.get(id(request))
        if not data:
            return
        data["status"] = int(getattr(response, "status", 0) or 0)
        self._emit(
            "response_received",
            request_id=data["request_id"],
            method=data["method"],
            url_path=data["url_path"],
            status=data["status"],
            request_elapsed_ms=round((time.monotonic() - data["started"]) * 1000, 1),
        )

    def _request_finished(self, request) -> None:
        data = self.requests.get(id(request))
        if not data:
            return
        data["terminal"] = True
        data["outcome"] = "finished"
        self._emit(
            "request_finished",
            request_id=data["request_id"],
            method=data["method"],
            url_path=data["url_path"],
            request_elapsed_ms=round((time.monotonic() - data["started"]) * 1000, 1),
        )

    def _request_failed(self, request) -> None:
        data = self.requests.get(id(request))
        if not data:
            return
        data["terminal"] = True
        try:
            failure = request.failure
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            failure = None
        data["outcome"] = "failed"
        data["failure_reason"] = _safe_network_failure(failure)
        self._emit(
            "request_failed",
            request_id=data["request_id"],
            method=data["method"],
            url_path=data["url_path"],
            failure_reason=data["failure_reason"],
            request_elapsed_ms=round((time.monotonic() - data["started"]) * 1000, 1),
        )

    def _frame_navigated(self, frame) -> None:
        try:
            if frame != self.page.main_frame:
                return
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        self.main_frame_navigations += 1
        self._emit("framenavigated", url_path=_safe_request_path(getattr(frame, "url", "")))

    @property
    def pending_navigation_requests(self) -> list[dict]:
        return [
            data
            for data in self.requests.values()
            if not data.get("terminal")
            and str(data.get("resource_type") or "").lower() == "document"
        ]

    @property
    def can_observe_transitions(self) -> bool:
        return bool({"request", "framenavigated"} & self.attached_events)

    @staticmethod
    def _is_login_request(data: dict) -> bool:
        path = str(data.get("url_path") or "").lower()
        return (
            str(data.get("method") or "").upper() == "POST"
            and (
                "/accounts/login/ajax" in path
                or "/web/accounts/login" in path
            )
        )

    @property
    def login_requests(self) -> list[dict]:
        return [data for data in self.requests.values() if self._is_login_request(data)]

    @property
    def login_request_started(self) -> bool:
        return bool(self.login_requests)

    def login_request_outcome(self) -> tuple[str, str, int]:
        requests = self.login_requests
        if not requests:
            return "not_started", "", 0
        failed = next((data for data in reversed(requests) if data.get("outcome") == "failed"), None)
        if failed is not None:
            return "failed", str(failed.get("failure_reason") or "network_failure"), 0
        pending = next((data for data in reversed(requests) if not data.get("terminal")), None)
        if pending is not None:
            return "pending", "", int(pending.get("status") or 0)
        finished = requests[-1]
        return "finished", "", int(finished.get("status") or 0)

    def observe(self, page, state: str, rejection: bool = False, ui: dict | None = None) -> None:
        self.iteration += 1
        ui = dict(ui or _login_post_action_ui(page))
        self._emit(
            "observation",
            iteration=self.iteration,
            url_path=_safe_request_path(getattr(page, "url", "")),
            classified_state=str(state or "unknown")[:60],
            rejection=bool(rejection),
            **ui,
        )


def _login_post_action_ui(page) -> dict:
    defaults = {
        "submit_visible": False,
        "submit_enabled": False,
        "submit_disabled": False,
        "submit_loading": False,
        "submit_aria_busy": False,
        "login_form_visible": False,
        "login_form_disabled": False,
        "username_present": False,
        "username_visible": False,
        "username_disabled": False,
        "password_present": False,
        "password_visible": False,
        "password_disabled": False,
        "password_has_value": False,
    }
    try:
        result = page.evaluate("""() => {
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const password = document.querySelector(
                "input[type='password'],input[name='password'],input[name='pass'],input[autocomplete='current-password']"
            );
            const username = document.querySelector(
                "input[name='username'],input[name='email'],input[autocomplete='username']"
            );
            const form = password ? password.closest('form') : document.querySelector('form');
            const controls = form ? [...form.querySelectorAll("button,[role='button'],input[type='submit']")] : [];
            const label = el => [
                el.getAttribute('aria-label') || '',
                el.getAttribute('value') || '',
                el.innerText || el.textContent || ''
            ].join(' ').trim();
            const submit = controls.find(el => /log\\s*in|login/i.test(label(el)))
                || controls.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit')
                || null;
            const loading = !!(form && form.querySelector(
                "[role='status'],svg[aria-label*='loading' i],[aria-busy='true']"
            ));
            const disabled = !!(submit && (
                submit.hasAttribute('disabled')
                || (submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
            ));
            return {
                submit_visible: visible(submit),
                submit_enabled: !!submit && visible(submit) && !disabled,
                submit_disabled: disabled,
                submit_loading: loading,
                submit_aria_busy: !!submit && (submit.getAttribute('aria-busy') || '').toLowerCase() === 'true',
                login_form_visible: visible(form),
                login_form_disabled: !!form && form.hasAttribute('disabled'),
                username_present: !!username,
                username_visible: visible(username),
                username_disabled: !!username && username.disabled,
                password_present: !!password,
                password_visible: visible(password),
                password_disabled: !!password && password.disabled,
                password_has_value: !!password && !!password.value,
            };
        }""")
        if isinstance(result, dict):
            defaults.update({key: bool(result.get(key)) for key in defaults})
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return defaults


def _login_submit_control(
    page, timeout_ms: int = 700, *, group_ref: str = ""
):
    """Return a fresh, form-scoped semantic submit control when one exists."""
    # Enumerate every generic control in trusted frames. A hidden first match
    # cannot suppress a later visible control in the selected credential group.
    for frame_index, frame in _trusted_login_frames(page):
        expected_group_ref = str(group_ref or "")
        if ":" in expected_group_ref:
            expected_frame, expected_group_ref = expected_group_ref.split(
                ":", 1
            )
            if expected_frame.isdigit() and int(expected_frame) != frame_index:
                continue
        try:
            controls = frame.locator(LOGIN_CONTROL_SELECTOR)
            count = min(int(controls.count() or 0), 30)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
        for index in range(count):
            try:
                control = controls.nth(index)
                candidate_group = str(
                    control.evaluate(
                        """el => {
                          const INPUTS = "input,textarea,[contenteditable='true'],[contenteditable='plaintext-only']";
                          const CONTROLS = "button,input[type='submit'],input[type='button'],[role='button']";
                          const state=globalThis[Symbol.for('sparkgrid.login.surface.v1')];
                          if (!state) return '';
                          const groupFor = node => {
                            if (node.form) return node.form;
                            let current=node.parentElement;
                            while (current && current !== document.body) {
                              const count=current.querySelectorAll(INPUTS).length;
                              if (count > 0 && (count > 1 || current.querySelector(CONTROLS))) {
                                return current;
                              }
                              current=current.parentElement;
                            }
                            return node.parentElement || document.body || document.documentElement;
                          };
                          return state.groupRefs.get(groupFor(el)) || '';
                        }"""
                    )
                    or ""
                )
                if group_ref and candidate_group != expected_group_ref:
                    continue
                # Playwright 1.60 removed the Locator.is_visible timeout
                # argument; visibility remains an immediate fresh-state probe.
                if not control.is_visible():
                    continue
                return control
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                continue
    return None


def _control_state(control) -> dict:
    if control is None:
        return {"present": False, "visible": False, "enabled": False, "busy": False}
    state = {"present": True, "visible": False, "enabled": False, "busy": False}
    try:
        state["visible"] = bool(control.is_visible(timeout=400))
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    try:
        state["enabled"] = bool(control.is_enabled(timeout=400))
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        state["enabled"] = True
    try:
        aria_busy = str(control.get_attribute("aria-busy") or "").lower() == "true"
        aria_disabled = str(control.get_attribute("aria-disabled") or "").lower() == "true"
        disabled = control.get_attribute("disabled") is not None
        text = str(control.inner_text(timeout=400) or "").strip()
        state.update({
            "busy": aria_busy,
            "aria_disabled": aria_disabled,
            "disabled": disabled,
            "text_present": bool(text),
        })
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return state


def _execute_login_submit(
    page,
    pass_field,
    account: str,
    dump: LiveDump,
    hum=None,
    *,
    group_ref: str = "",
) -> tuple[str, str, object, dict]:
    """Dispatch exactly one submit, retrying only a human action proven false."""
    control = _login_submit_control(
        page, timeout_ms=1200, group_ref=group_ref
    )
    before = _control_state(control)
    if control is not None and before.get("visible") and before.get("enabled"):
        if hum is not None:
            try:
                if hum.click(control):
                    return ACTION_ACCEPTED_TRANSITIONING, "button_human_pointer", control, before
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
        try:
            control.click(timeout=5000)
            _record_direct_fallback(dump, "submit_login_form_locator")
            return ACTION_ACCEPTED_TRANSITIONING, "button_locator_click", control, before
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return ACTION_NOT_EXECUTED, "button_click_failed", control, before
    try:
        if hum is not None:
            if group_ref:
                _surface, _candidate, pass_field = _fresh_login_candidate(
                    page, "password", group_ref
                )
                if pass_field is None:
                    return (
                        ACTION_NOT_EXECUTED,
                        "password_reacquire_failed",
                        control,
                        before,
                    )
            hum.press(pass_field, "Enter")
        else:
            if group_ref:
                _surface, _candidate, pass_field = _fresh_login_candidate(
                    page, "password", group_ref
                )
                if pass_field is None:
                    return (
                        ACTION_NOT_EXECUTED,
                        "password_reacquire_failed",
                        control,
                        before,
                    )
            pass_field.press("Enter", timeout=3000)
        _record_direct_fallback(dump, "submit_login_form_enter")
        return ACTION_ACCEPTED_TRANSITIONING, "password_enter", control, before
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return ACTION_NOT_EXECUTED, "enter_failed", control, before


class _AutoLoginTransactionCoordinator:
    """Own structural credential actions and the single terminal boundary."""

    def __init__(self, page, account: dict, dump: LiveDump):
        self.page = page
        self.account = account
        self.dump = dump
        self.name = str(account.get("name") or "")
        self.password = str(account.get("api_password") or "").strip()
        self.hum = _human_for(page, self.name, dump)

    def _terminal(self, code: str, reason: str) -> bool:
        observation = getattr(
            self.dump, "_auto_login_last_observation", None
        )
        if not isinstance(observation, LoginSurfaceObservation):
            observation = LoginSurfaceObservation(
                state={
                    "blocker_detected": "blocker_detected",
                    "challenge_detected": "challenge",
                    "unrecognized_surface": "unsupported_stable",
                    "unsupported_login_state": "unsupported_stable",
                }.get(code, "unknown"),
                reason="",
                url_category="unknown",
                loading=False,
                semantic_fingerprint="",
                frame_epochs=(),
                candidates=[],
                selected_group_ref="",
            )
        try:
            _record_auto_login_terminal(observation, code)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        _set_login_form_failure(self.dump, code)
        self.dump.capture(
            self.page,
            "auto_login_" + code,
            reason,
            force_snapshot=True,
            take_screenshot=False,
            take_visible_text=False,
        )
        return False

    def _wait_for_surface(
        self, *, total_seconds: float = 20.0
    ) -> LoginSurfaceWaitResult:
        deadline = time.monotonic() + max(0.5, float(total_seconds))
        stable_fingerprint = ""
        stable_reads = 0
        last_observation: LoginSurfaceObservation | None = None
        while time.monotonic() < deadline:
            _dismiss_instagram_consent(
                self.page, self.dump, self.name
            )
            ready, blocker_state, blocker_reason = _login_blocker_first(
                self.page,
                self.dump,
                self.name,
                "credential transaction",
                wait_seconds=0.2,
                structural_login_surface=True,
            )
            if not ready:
                code = (
                    "challenge_detected"
                    if blocker_state in {
                        "checkpoint", "restricted", "suspended"
                    }
                    else "blocker_detected"
                )
                return LoginSurfaceWaitResult(
                    None, code, blocker_reason or blocker_state
                )

            observation = _observe_login_surface(self.page)
            last_observation = observation
            if observation.state in LOGIN_CREDENTIAL_STATES:
                return LoginSurfaceWaitResult(observation)
            if observation.state == "authenticated":
                self.dump._login_transaction_authenticated = True
                return LoginSurfaceWaitResult(observation)
            if observation.state == "two_factor":
                self.dump._login_transaction_two_factor = True
                return LoginSurfaceWaitResult(observation)
            if observation.state == "challenge":
                return LoginSurfaceWaitResult(
                    None,
                    "challenge_detected",
                    observation.reason,
                )

            if observation.semantic_fingerprint == stable_fingerprint:
                stable_reads += 1
            else:
                stable_fingerprint = observation.semantic_fingerprint
                stable_reads = 1
            if observation.state == "consent_blocker" and stable_reads >= 3:
                return LoginSurfaceWaitResult(
                    None,
                    "blocker_detected",
                    "scoped consent blocker remained after bounded resolution",
                )
            if observation.state == "unsupported_stable" and stable_reads >= 3:
                return LoginSurfaceWaitResult(
                    None,
                    "unrecognized_surface",
                    "stable observations contained no coherent login surface",
                )
            _wait_for_login_surface_change(
                self.page,
                observation,
                timeout_ms=int(
                    min(
                        0.9,
                        max(0.05, deadline - time.monotonic()),
                    )
                    * 1000
                ),
            )
        return LoginSurfaceWaitResult(
            None,
            "login_form_transition_timeout",
            (
                "login surface did not settle before the bounded deadline"
                if last_observation is None
                else f"last_state={last_observation.state}"
            ),
        )

    def _interaction_terminal(
        self,
        result: LoginInteractionResult,
        *,
        initial_state: str,
    ) -> bool:
        if result.intent == "username":
            if (
                result.candidate_seen
                and result.structurally_viable_seen
                and result.normal_attempts >= 2
                and result.postcondition_checks >= 2
                and (
                    not result.fallback_available
                    or result.fallback_attempted
                )
                and result.state in LOGIN_USERNAME_STATES
            ):
                return self._terminal(
                    "username_field_not_ready",
                    (
                        "two normal attempts against freshly reacquired "
                        "username candidates had negative fresh postconditions"
                    ),
                )
            if (
                not result.candidate_seen
                and result.missing_observations >= 3
                and initial_state in LOGIN_USERNAME_STATES
            ):
                return self._terminal(
                    "username_field_not_found",
                    "fresh observations could not reacquire a username candidate",
                )
            return self._terminal(
                "login_form_transition_timeout",
                result.reason or "username transaction changed state",
            )
        if not result.candidate_seen:
            return self._terminal(
                "password_field_not_found",
                "fresh observations could not reacquire a password candidate",
            )
        return self._terminal(
            "password_input_not_retained",
            result.reason or "fresh password postconditions remained negative",
        )

    def _continue_username_first(
        self, observation: LoginSurfaceObservation
    ) -> LoginSurfaceWaitResult:
        group_ref = observation.selected_group_ref
        deadline = time.monotonic() + 8.0
        attempts = 0
        current = observation
        while time.monotonic() < deadline and attempts < 2:
            current = _observe_login_surface(self.page)
            if current.state != "login_username_first":
                return LoginSurfaceWaitResult(current)
            control = _login_submit_control(
                self.page,
                timeout_ms=900,
                group_ref=group_ref or current.selected_group_ref,
            )
            if control is None:
                _wait_for_login_surface_change(
                    self.page, current, timeout_ms=700
                )
                continue
            attempts += 1
            try:
                control.click(timeout=5000)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            # The clicked control and all pre-click observations are invalid.
            post = _observe_login_surface(self.page)
            if post.state != "login_username_first":
                return LoginSurfaceWaitResult(post)
            _wait_for_login_surface_change(
                self.page, post, timeout_ms=900
            )
        return LoginSurfaceWaitResult(
            None,
            "login_form_transition_timeout",
            "verified username continuation did not reach a new structural state",
        )

    def run(self) -> bool:
        token = _ACTIVE_AUTO_LOGIN_DIAGNOSTIC_DUMP.set(
            getattr(self, "dump", None)
        )
        try:
            return self._run_transaction()
        finally:
            _ACTIVE_AUTO_LOGIN_DIAGNOSTIC_DUMP.reset(token)

    def _run_transaction(self) -> bool:
        surface = self._wait_for_surface()
        if surface.code:
            return self._terminal(surface.code, surface.reason)
        observation = surface.observation
        if observation is None:
            return self._terminal(
                "login_form_transition_timeout",
                "credential transaction produced no observation",
            )
        if observation.state in {"authenticated", "two_factor"}:
            return True
        if not self.password:
            raise RuntimeError("missing saved api_password for web auto-login")
        if _looks_like_cookie_or_session(self.password):
            raise RuntimeError(
                "saved api_password looks like a cookie/session dump, not a real password"
            )

        username_result: LoginInteractionResult | None = None
        if observation.state in LOGIN_USERNAME_STATES:
            username_result = _interact_and_verify_login_candidate(
                self.page,
                intent="username",
                expected=self.name,
                preferred_group_ref=observation.selected_group_ref,
                hum=self.hum,
            )
            if not username_result.verified:
                return self._interaction_terminal(
                    username_result, initial_state=observation.state
                )
            self.dump.capture(
                self.page,
                "auto_login_username_verified",
                (
                    "username_verified=true; "
                    f"normal_attempts={username_result.normal_attempts}; "
                    f"fallback_used={str(username_result.fallback_attempted).lower()}"
                ),
                take_screenshot=False,
                take_visible_text=False,
            )
            observation = _observe_login_surface(self.page)

        if observation.state == "login_username_first":
            continuation = self._continue_username_first(observation)
            if continuation.code:
                return self._terminal(
                    continuation.code, continuation.reason
                )
            observation = continuation.observation
            if observation is None:
                return self._terminal(
                    "login_form_transition_timeout",
                    "username continuation produced no fresh observation",
                )
            if observation.state == "two_factor":
                self.dump._login_transaction_two_factor = True
                return True
            if observation.state == "authenticated":
                self.dump._login_transaction_authenticated = True
                return True
            if observation.state == "challenge":
                return self._terminal(
                    "challenge_detected", observation.reason
                )

        if observation.state not in {
            "login_combined", "login_password_only"
        }:
            refreshed = self._wait_for_surface(total_seconds=8.0)
            if refreshed.code:
                return self._terminal(
                    refreshed.code, refreshed.reason
                )
            observation = refreshed.observation
            if observation is None or observation.state not in {
                "login_combined", "login_password_only"
            }:
                return self._terminal(
                    "unrecognized_surface",
                    "verified username did not lead to a password surface",
                )

        password_result = _interact_and_verify_login_candidate(
            self.page,
            intent="password",
            expected=self.password,
            preferred_group_ref=observation.selected_group_ref,
            hum=self.hum,
        )
        if not password_result.verified:
            return self._interaction_terminal(
                password_result, initial_state=observation.state
            )

        # Authorize submit only after a bounded, mutation-stable round in which
        # all credentials required by the current state have fresh values.
        fresh = _observe_login_surface(self.page)
        password_candidate: LoginCandidateDescriptor | None = None
        credentials_verified = False
        for _verification_round in range(3):
            fresh = _observe_login_surface(self.page)
            if fresh.state == "authenticated":
                self.dump._login_transaction_authenticated = True
                return True
            if fresh.state == "two_factor":
                self.dump._login_transaction_two_factor = True
                return True
            if fresh.state == "challenge":
                return self._terminal(
                    "challenge_detected", fresh.reason
                )
            if fresh.state not in {
                "login_combined", "login_password_only"
            }:
                return self._terminal(
                    "login_form_transition_timeout",
                    "credential surface changed during fresh verification",
                )

            group_ref = fresh.selected_group_ref
            username_epoch = fresh.frame_epochs
            if fresh.state == "login_combined":
                username_matches, username_observation, username_candidate = (
                    _fresh_login_value_postcondition(
                        self.page,
                        "username",
                        self.name,
                        group_ref,
                    )
                )
                fresh = username_observation
                if not username_matches or username_candidate is None:
                    restored_username = _interact_and_verify_login_candidate(
                        self.page,
                        intent="username",
                        expected=self.name,
                        preferred_group_ref=group_ref,
                        hum=self.hum,
                    )
                    if not restored_username.verified:
                        return self._interaction_terminal(
                            restored_username,
                            initial_state="login_combined",
                        )
                    continue
                group_ref = username_candidate.recipe.group_ref
                username_epoch = username_observation.frame_epochs

            password_matches, password_observation, password_candidate = (
                _fresh_login_value_postcondition(
                    self.page,
                    "password",
                    self.password,
                    group_ref,
                )
            )
            fresh = password_observation
            if not password_matches or password_candidate is None:
                restored_password = _interact_and_verify_login_candidate(
                    self.page,
                    intent="password",
                    expected=self.password,
                    preferred_group_ref=group_ref,
                    hum=self.hum,
                )
                if not restored_password.verified:
                    return self._interaction_terminal(
                        restored_password, initial_state=fresh.state
                    )
                continue

            if fresh.state == "login_combined":
                final_username, final_observation, _ = (
                    _fresh_login_value_postcondition(
                        self.page,
                        "username",
                        self.name,
                        group_ref,
                    )
                )
                fresh = final_observation
                if (
                    not final_username
                    or final_observation.frame_epochs
                    != password_observation.frame_epochs
                    or username_epoch
                    != password_observation.frame_epochs
                ):
                    continue
            credentials_verified = True
            break

        if not credentials_verified or password_candidate is None:
            return self._terminal(
                "login_form_transition_timeout",
                "credential postconditions did not stabilize before submit",
            )

        self.dump.capture(
            self.page,
            "auto_login_values_confirmed",
            "username_requirement_verified=true; password_verified=true",
            take_screenshot=False,
            take_visible_text=False,
        )
        before_url = str(getattr(self.page, "url", "") or "")
        telemetry = LoginPostActionTelemetry(self.page, self.dump)
        telemetry.start()
        self.dump._login_post_action_telemetry = telemetry
        recovery_workflow_id = str(
            os.environ.get("SPARKGRID_PASSWORD_RECOVERY_WORKFLOW_ID") or ""
        )
        submission_reserved = False
        if recovery_workflow_id:
            reservation = reserve_password_submission(
                self.name, recovery_workflow_id
            )
            if not reservation.get("ok"):
                raise RuntimeError(
                    "password_submission_blocked: "
                    + str(
                        reservation.get("reason")
                        or "durable recovery gate rejected submit"
                    )
                )
            submission_reserved = True

        action_state, method, _submit_control, control_before = (
            _execute_login_submit(
                self.page,
                None,
                self.name,
                self.dump,
                self.hum,
                group_ref=fresh.selected_group_ref,
            )
        )
        if action_state == ACTION_NOT_EXECUTED:
            if submission_reserved:
                finish_password_submission(
                    self.name,
                    recovery_workflow_id,
                    physically_dispatched=False,
                )
            telemetry.stop(ACTION_NOT_EXECUTED)
            self.dump._login_post_action_telemetry = None
            return self._terminal(
                "login_submit_control_not_found",
                "fresh group-scoped submit action was not dispatched",
            )
        self.dump.capture(
            self.page,
            "auto_login_submit_dispatched",
            (
                f"method={method}; "
                f"url_unchanged={str(str(getattr(self.page, 'url', '') or '') == before_url).lower()}; "
                f"control_present={str(bool(control_before.get('present'))).lower()}; "
                f"control_enabled={str(bool(control_before.get('enabled'))).lower()}"
            ),
            take_screenshot=False,
            take_visible_text=False,
        )
        transition, evidence = _wait_for_password_submit_activation(
            self.page,
            None,
            before_url,
            initial_control_state=control_before,
            group_ref=fresh.selected_group_ref,
            timeout_seconds=8.0,
            telemetry=telemetry,
        )
        if (
            transition == UNKNOWN_STABLE_STATE
            and method == "password_enter"
            and not telemetry.login_request_started
        ):
            fresh_ui = _login_post_action_ui(self.page)
            fallback_control = _login_submit_control(
                self.page,
                timeout_ms=1200,
                group_ref=fresh.selected_group_ref,
            )
            fallback_state = _control_state(fallback_control)
            if (
                not fresh_ui.get("submit_loading")
                and not fresh_ui.get("login_form_disabled")
                and not fresh_ui.get("password_disabled")
                and fallback_control is not None
                and fallback_state.get("visible")
                and fallback_state.get("enabled")
            ):
                try:
                    fallback_control.click(timeout=5000)
                    _record_direct_fallback(
                        self.dump,
                        "submit_login_form_locator_after_enter_no_request",
                    )
                    transition, evidence = (
                        _wait_for_password_submit_activation(
                            self.page,
                            None,
                            before_url,
                            initial_control_state=fallback_state,
                            group_ref=fresh.selected_group_ref,
                            timeout_seconds=8.0,
                            telemetry=telemetry,
                        )
                    )
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
        if transition in {
            UNKNOWN_STABLE_STATE, ACTION_NOT_EXECUTED, TERMINAL_FAILURE
        }:
            if submission_reserved:
                finish_password_submission(
                    self.name,
                    recovery_workflow_id,
                    physically_dispatched=False,
                )
            telemetry.stop(transition)
            self.dump._login_post_action_telemetry = None
            self.dump.capture_safe_dom(
                self.page, "login_submit_no_transition"
            )
            return self._terminal(
                "login_submit_no_transition",
                "login submit reached a stable unchanged form",
            )
        if submission_reserved:
            finish_password_submission(
                self.name,
                recovery_workflow_id,
                physically_dispatched=True,
            )
        self.dump.capture(
            self.page,
            "auto_login_submitted_password",
            f"method={method}; contract={transition}; {evidence}",
            take_screenshot=False,
            take_visible_text=False,
        )
        return True


def _fill_instagram_login_form(page, account: dict, dump: LiveDump) -> bool:
    return _AutoLoginTransactionCoordinator(page, account, dump).run()


def _wait_for_password_submit_activation(
    page, pass_field, before_url: str, *, submit_control=None,
    initial_control_state: dict | None = None,
    group_ref: str = "",
    timeout_seconds: float = 8.0,
    telemetry: LoginPostActionTelemetry | None = None,
) -> tuple[str, str]:
    """Classify fresh post-submit observations without mistaking SPA loading for failure."""
    legacy_pass_field = pass_field
    deadline = time.time() + max(0.5, float(timeout_seconds))
    stable_reads = 0
    initial_control = dict(
        initial_control_state
        if initial_control_state is not None
        else _control_state(submit_control)
    )
    if (
        initial_control.get("busy")
        or initial_control.get("aria_disabled")
        or initial_control.get("disabled")
        or (
            initial_control.get("present")
            and initial_control.get("visible")
            and not initial_control.get("enabled")
        )
    ):
        return (
            ACTION_ACCEPTED_TRANSITIONING,
            "login submit control was already loading/disabled",
        )
    while True:
        try:
            current_url = str(page.url or "")
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            current_url = ""
        if current_url and current_url != str(before_url or ""):
            safe_url = current_url.split("?", 1)[0].split("#", 1)[0]
            return KNOWN_NEXT_STATE, f"URL changed to {safe_url}"
        rejection = _detect_login_rejection(page)
        state, reason = get_state(page)
        ui = _login_post_action_ui(page)
        if telemetry is not None:
            telemetry.observe(page, state, rejection=bool(rejection), ui=ui)
        if rejection:
            return STABLE_SAME_STATE_WITH_ERROR, "Instagram returned a login rejection"
        if state in {"logged_in", "two_factor_required", "checkpoint", "restricted"}:
            return KNOWN_NEXT_STATE, f"state={state}: {reason}"
        if telemetry is not None and telemetry.login_request_started:
            return ACTION_ACCEPTED_TRANSITIONING, "Instagram request started after login submit"
        if (
            ui.get("submit_loading")
            or ui.get("login_form_disabled")
            or ui.get("username_disabled")
            or ui.get("password_disabled")
            or (
                ui.get("submit_visible")
                and ui.get("submit_disabled")
            )
        ):
            return ACTION_ACCEPTED_TRANSITIONING, "login form entered confirmed loading/disabled state"
        fresh_control = _login_submit_control(
            page, timeout_ms=250, group_ref=group_ref
        )
        control_state = _control_state(fresh_control)
        if (
            control_state.get("busy")
            or control_state.get("aria_disabled")
            or control_state.get("disabled")
            or (
                initial_control.get("enabled")
                and control_state.get("present")
                and not control_state.get("enabled")
            )
            or (
                initial_control.get("text_present")
                and control_state.get("present")
                and not control_state.get("text_present")
            )
        ):
            return ACTION_ACCEPTED_TRANSITIONING, "login submit control entered loading/disabled state"
        surface, _password_candidate, fresh_password = _fresh_login_candidate(
            page, "password", group_ref
        )
        if fresh_password is None and legacy_pass_field is not None:
            # Script-level post-action characterization fakes do not implement
            # structural frames. Production passes no pre-submit locator here.
            fresh_password = legacy_pass_field
        if fresh_password is None:
            return ACTION_ACCEPTED_TRANSITIONING, "login form detached during navigation"
        try:
            if not fresh_password.is_visible(timeout=350):
                return ACTION_ACCEPTED_TRANSITIONING, "visible login form disappeared"
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return ACTION_ACCEPTED_TRANSITIONING, "login form detached during navigation"
        if state == "login_required":
            stable_reads += 1
        else:
            stable_reads = 0
        if time.time() >= deadline:
            break
        _wait_for_login_surface_change(
            page,
            surface,
            timeout_ms=int(
                min(0.4, max(0.05, deadline - time.time())) * 1000
            ),
        )
    return UNKNOWN_STABLE_STATE, (
        f"login form remained visible across {stable_reads} fresh reads "
        "with no navigation, loading state, known next state, or inline error"
    )


def _find_2fa_code_field(page):
    """Find the 2FA verification-code input, robust across languages/markup.
    Checks role=textbox, then the FIRST VISIBLE input among ALL inputs (IG often
    renders a hidden input before the visible code field, which defeats
    `.first`-only selectors), then broad attribute fallbacks."""
    getters = [
        lambda: page.locator(
            "input[name='verificationCode'], input[autocomplete='one-time-code'], "
            "input[aria-label*='code' i], input[inputmode='numeric'], "
            "input[maxlength='6'], input[maxlength='8'], input[maxlength='1'], "
            "input[type='tel'], input[type='number']"
        ),
        lambda: page.locator("form input:visible"),
        lambda: page.get_by_role("textbox"),
        lambda: page.locator("input:visible, textarea:visible"),
    ]
    for getter in getters:
        try:
            loc = getter()
            n = min(int(loc.count() or 0), 10)
            for i in range(n):
                el = loc.nth(i)
                try:
                    if el.is_visible(timeout=500):
                        return el
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    continue
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    # Last resort: IG's 2FA code input is often CSS-hidden (custom OTP widget:
    # a real <input type=text autocomplete=off> behind a visual layer), so
    # is_visible() is False. Return the first EXISTING text-like input (not the
    # submit/checkbox); we fill it with force=True.
    for sel in ("input[type='text']", "input[inputmode]", "input:not([type])",
                "input[autocomplete='off']"):
        try:
            loc = page.locator(sel).first
            if int(loc.count() or 0) > 0:
                return loc
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return None


def _digits_only(value: object) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _read_confirmed_otp(page, code_field) -> str:
    """Read the OTP that is actually present in the live Instagram DOM.

    Instagram uses both one controlled input and six one-character inputs. A
    successful Playwright keyboard call is not proof that React retained the
    value, so submission is allowed only after this function reads six digits.
    """
    direct = ""
    try:
        direct = _digits_only(code_field.input_value(timeout=1200))
        if len(direct) >= 6:
            return direct
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    selectors = (
        "input[maxlength='1']",
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
        "input[name='verificationCode']",
    )
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = min(int(loc.count() or 0), 10)
            values = []
            for index in range(count):
                item = loc.nth(index)
                try:
                    if not item.is_visible(timeout=250):
                        continue
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    continue
                try:
                    value = _digits_only(item.input_value(timeout=400))
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    value = ""
                if value:
                    values.append(value)
            combined = "".join(values)
            if len(combined) >= 6:
                return combined
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return direct


def _otp_is_confirmed(page, code_field, expected_code: str) -> tuple[bool, int]:
    actual = _read_confirmed_otp(page, code_field)
    expected = _digits_only(expected_code)
    return bool(expected and actual == expected), len(actual)


def _type_and_confirm_otp(page, code_field, code: str, account: dict, dump: LiveDump) -> tuple[bool, object, object]:
    """Type OTP with real keys, re-resolving once after a React re-render."""
    hum = _human_for(page, account.get("name") or "instagram_2fa", dump)
    current = code_field
    for attempt in range(1, 3):
        if attempt > 1:
            current = _find_2fa_code_field(page) or current
            dump.capture(page, "auto_login_2fa_input_retry", "OTP was not retained; retrying input")
        try:
            current.scroll_into_view_if_needed(timeout=1500)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        focused = bool(hum and hum.click(current, timeout=3000))
        if not focused:
            for focus in (
                lambda: current.click(timeout=3000),
                lambda: current.click(timeout=3000, force=True),
                lambda: current.focus(timeout=2000),
            ):
                try:
                    focus()
                    focused = True
                    break
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    continue
        try:
            current.press("ControlOrMeta+A")
            current.press("Delete")
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        typed = False
        if hum is not None:
            try:
                typed = bool(hum.type_text(code, locator=current, clear=True, sensitive=True, allow_typos=False))
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                typed = False
        if not typed:
            for typer in (
                lambda: current.press_sequentially(code, delay=130),
                lambda: current.type(code, delay=130),
                lambda: page.keyboard.type(code, delay=130),
            ):
                try:
                    typer()
                    break
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    continue
        time.sleep(0.8)
        confirmed, actual_length = _otp_is_confirmed(page, current, code)
        if confirmed:
            return True, current, hum
        log(
            f"{account.get('name') or 'instagram_2fa'}: 2FA input was not retained "
            f"(actual length {actual_length}); submit is blocked",
            "WARNING",
        )
        time.sleep(0.5)
    return False, current, hum


def _try_submit_totp(
    page, account: dict, dump: LiveDump, wait_seconds: int = 35,
    submitted_codes: set[str] | None = None,
) -> tuple[bool, str]:
    secret = str(account.get("api_totp_secret") or "").strip()
    deadline = time.time() + wait_seconds
    code_field = None
    selectors = [
        "input[name='verificationCode']",
        "input[autocomplete='one-time-code']",
        "input[aria-label*='Security Code' i]",
        "input[aria-label*='code' i]",
        "input[inputmode='numeric']",
        "input[type='tel']",
        "input[type='number']",
        "input[type='text']",
        # IG's 2FA input often has NO type attribute (so input[type=text] misses it),
        # a maxlength, and autocomplete off. Broad fallbacks catch the single field.
        "input:not([type])",
        "input[maxlength='6']",
        "input[maxlength='8']",
        "input[autocomplete='off']",
        "form input:visible",
        "input",
    ]
    while time.time() < deadline:
        ready, blocker_state, _blocker_reason = _login_blocker_first(
            page,
            dump,
            str(account.get("name") or ""),
            "2FA field discovery",
            wait_seconds=0,
        )
        if not ready:
            return False, blocker_state
        rejection = _detect_login_rejection(page)
        if rejection:
            dump.capture(page, "auto_login_rejected", rejection, force_snapshot=True)
            raise RuntimeError(rejection)
        txt = ""
        try:
            txt = (page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        # We are only called once get_state() confirmed the 2FA page (by URL), so
        # do NOT gate on English text — the code input is found by attributes,
        # which is language-agnostic. (German pages say "Authentifizierungs-App".)
        code_field = _find_2fa_code_field(page) or _first_visible(page, selectors, timeout_ms=800)
        if code_field:
            break
        time.sleep(1.0)
    if not code_field:
        dump.capture(page, "auto_login_2fa_field_not_found",
                     "2FA text detected but code input not found", force_snapshot=True)
        try:
            (dump.root / "2fa_page.html").write_text(page.content(), encoding="utf-8")
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        return False, "two_factor_code_required"
    if not secret:
        dump.capture(page, "auto_login_2fa_missing_secret", "2FA requested but no saved TOTP secret", force_snapshot=True)
        return False, "two_factor_code_required"

    sent = submitted_codes if submitted_codes is not None else set()
    code, code_state = _fresh_totp_code(secret, sent)
    if not code:
        if code_state == "next_window_required":
            dump.capture(page, "auto_login_2fa_wait_next_window", "waiting for a distinct fresh TOTP window")
            return False, "two_factor_wait_next_window"
        else:
            dump.capture(page, "auto_login_2fa_invalid_secret", "saved TOTP secret cannot produce a safe automatic code", force_snapshot=True)
        return False, "two_factor_code_required"
    ready, blocker_state, _blocker_reason = _login_blocker_first(
        page,
        dump,
        str(account.get("name") or ""),
        "2FA input",
        wait_seconds=0,
    )
    if not ready:
        return False, blocker_state
    code_field = _find_2fa_code_field(page) or _first_visible(page, selectors, timeout_ms=800)
    if not code_field:
        return False, "two_factor_code_required"
    # A keyboard API returning success does not prove React retained the OTP.
    # Verify the live DOM value and retry once; never click Continue otherwise.
    confirmed, code_field, hum = _type_and_confirm_otp(page, code_field, code, account, dump)
    if not confirmed:
        dump.capture(
            page,
            "auto_login_2fa_value_not_confirmed",
            "2FA field did not retain the complete six-digit code; confirmation was not clicked",
            force_snapshot=True,
        )
        return False, "two_factor_code_required"
    dump.capture(page, "auto_login_2fa_typed", "six-digit 2FA value confirmed in the live field", force_snapshot=True)
    # Prefer a trusted browser session when IG offers it; this reduces repeat 2FA
    # prompts. IG PRE-CHECKS "Trust this device", so only toggle it when it is
    # actually OFF — clicking a checked box would turn trust OFF (the old bug).
    _ensure_trust_device_checked(page, account.get("name") or "instagram_2fa", dump)
    time.sleep(random.uniform(0.5, 1.0))
    account_name = account.get("name") or "instagram_2fa"
    ready, blocker_state, _blocker_reason = _login_blocker_first(
        page, dump, account_name, "2FA submit", wait_seconds=0
    )
    if not ready:
        return False, blocker_state
    code_field = _find_2fa_code_field(page) or _first_visible(page, selectors, timeout_ms=800)
    confirmed, _actual_length = _otp_is_confirmed(page, code_field, code) if code_field else (False, 0)
    if not confirmed:
        dump.capture(
            page,
            "auto_login_2fa_submit_blocked",
            "fresh 2FA field did not retain the complete code after blocker check",
            force_snapshot=True,
        )
        return False, "two_factor_code_required"
    log(f"{account_name}: 2FA code entered; submitting confirmation", "INFO")
    submitted = _submit_2fa_confirmation(page, code_field, account_name, dump, hum=hum, expected_code=code)
    if not submitted:
        dump.capture(page, "auto_login_2fa_submit_not_found",
                     "2FA code was entered, but no usable confirmation control was found",
                     force_snapshot=True)
        log(f"{account_name}: 2FA code entered, but confirmation could not be submitted", "ERROR")
        return False, "two_factor_code_required"
    sent.add(code)
    dump.capture(page, "auto_login_submitted_2fa", "submitted 2FA code")
    log(f"{account_name}: 2FA confirmation submitted; waiting for Instagram", "OK")
    return True, "submitted"



def _2fa_submit_transition(page, code_field, before_url: str, clicked_control=None, timeout_seconds: float = 5.0) -> tuple[bool, str]:
    """Return True only when the page shows evidence that the OTP was submitted.

    A Playwright click returning successfully is not enough: on the Windows
    Camoufox build a locator can be clicked while Instagram's React form does
    not consume the action.  We therefore require a URL/state/form transition,
    a validation message, the OTP field disappearing, or the clicked control
    entering a disabled/loading state.
    """
    deadline = time.time() + max(1.0, float(timeout_seconds))
    before_url = str(before_url or "")
    validating_terms = (
        "being validated", "validating", "please wait", "checking code",
        "wird überprüft", "wird ueberprueft", "bitte warten",
        "проверяем", "проверка кода", "подождите",
        "перевіря", "зачекайте", "validando", "vérification", "verifying",
    )
    while time.time() < deadline:
        try:
            current_url = str(page.url or "")
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            current_url = ""
        if before_url and current_url and current_url != before_url:
            return True, "URL changed"
        try:
            state, reason = get_state(page)
            if state in {"logged_in", "checkpoint", "restricted", "login_required", "failed"}:
                return True, f"state changed to {state}: {reason}"
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        try:
            if int(code_field.count() or 0) == 0 or not code_field.is_visible(timeout=350):
                return True, "2FA field disappeared"
        except Exception as _exc:
            # A detached locator is also evidence that the form transitioned.
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return True, "2FA field detached"
        try:
            body = (page.locator("body").inner_text(timeout=600) or "").lower()
            if any(term in body for term in validating_terms):
                return True, "Instagram is validating the code"
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        if clicked_control is not None:
            try:
                if not clicked_control.is_enabled(timeout=300):
                    return True, "confirmation control became disabled"
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            try:
                if (clicked_control.get_attribute("aria-busy") or "").lower() == "true":
                    return True, "confirmation control is busy"
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
        time.sleep(0.35)
    return False, "no form transition observed"


def _sync_react_otp_value(code_field) -> None:
    """Make React observe the value after physical keyboard input.

    Real keystrokes normally suffice, but Windows/Camoufox can visually update
    the input without enabling Instagram's Continue button.  Resetting React's
    value tracker and dispatching input/change keeps the visible value while
    synchronizing component state.
    """
    try:
        code_field.evaluate("""el => {
            const value = el.value;
            const tracker = el._valueTracker;
            if (tracker && typeof tracker.setValue === 'function') tracker.setValue('');
            const proto = Object.getPrototypeOf(el);
            const descriptor = Object.getOwnPropertyDescriptor(proto, 'value') ||
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            if (descriptor && descriptor.set) descriptor.set.call(el, value);
            try { el.dispatchEvent(new InputEvent('input', {bubbles: true, data: value, inputType: 'insertText'})); }
            catch (_) { el.dispatchEvent(new Event('input', {bubbles: true})); }
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur', {bubbles: true}));
            el.focus();
        }""")
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _submit_2fa_confirmation(page, code_field, account: str, dump: LiveDump, hum=None, timeout_seconds: int = 12, expected_code: str = "") -> bool:
    """Submit Instagram's 2FA form and verify that submission actually began.

    The button markup differs between Instagram builds and locales.  Candidate
    controls are ranked by label/type and by physical proximity below the OTP
    input.  Every click is followed by transition verification; a successful
    Playwright call without a page/form change is treated as a failed attempt.
    """
    positive = re.compile(
        r"^(continue|confirm|submit|next|log in|login|verify|done|"
        r"weiter|bestätigen|bestaetigen|anmelden|fertig|"
        r"продолжить|подтвердить|войти|готово|"
        r"продовжити|підтвердити|увійти|готово|"
        r"continuar|confirmar|entrar|verificar|"
        r"suivant|continuer|confirmer|se connecter|vérifier|verifier|"
        r"avanti|continua|conferma|accedi|verifica|"
        r"dalej|kontynuuj|potwierdź|potwierdz|zaloguj|zweryfikuj|"
        r"devam|onayla|giriş yap|giris yap|doğrula|dogrula)\s*$",
        re.I,
    )
    negative = re.compile(
        r"back|cancel|resend|send again|another way|backup|trust|forgot|"
        r"zurück|zurueck|abbrechen|erneut|andere|vertrauen|"
        r"назад|отмена|отправить снова|другой способ|резерв|довер|"
        r"назад|скасувати|надіслати знову|інший спосіб|резерв|довір|"
        r"atrás|atras|cancelar|reenviar|otro método|otro metodo|"
        r"retour|annuler|renvoyer|autre méthode|autre methode|"
        r"indietro|annulla|invia di nuovo|altro metodo|"
        r"wstecz|anuluj|wyślij ponownie|wyslij ponownie|inna metoda|"
        r"geri|iptal|yeniden gönder|yeniden gonder|başka yöntem|baska yontem",
        re.I,
    )

    _sync_react_otp_value(code_field)
    time.sleep(0.7)
    confirmed, actual_length = _otp_is_confirmed(page, code_field, expected_code)
    if not confirmed:
        log(f"{account}: 2FA confirmation blocked; live OTP length is {actual_length}", "ERROR")
        dump.capture(
            page,
            "auto_login_2fa_submit_blocked",
            "confirmation was blocked because the complete OTP was not present",
            force_snapshot=True,
        )
        return False

    try:
        code_box = code_field.bounding_box(timeout=1200)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        code_box = None

    def text_of(loc) -> str:
        for getter in (
            lambda: loc.inner_text(timeout=500),
            lambda: loc.get_attribute("aria-label"),
            lambda: loc.get_attribute("value"),
            lambda: loc.get_attribute("title"),
        ):
            try:
                value = getter()
                if value:
                    return str(value).strip()
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                continue
        return ""

    def usable(loc) -> bool:
        try:
            if not loc.is_visible(timeout=500):
                return False
            try:
                if not loc.is_enabled(timeout=500):
                    return False
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            try:
                if (loc.get_attribute("aria-disabled") or "").lower() == "true":
                    return False
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            return True
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return False

    candidates = []
    seen = set()
    controls = page.locator("button, [role='button'], input[type='submit']")
    try:
        control_count = min(int(controls.count() or 0), 40)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        control_count = 0
    for i in range(control_count):
        loc = controls.nth(i)
        if not usable(loc):
            continue
        label = text_of(loc)
        if label and negative.search(label):
            continue
        try:
            box = loc.bounding_box(timeout=500)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            box = None
        try:
            tag = (loc.evaluate("el => el.tagName.toLowerCase()") or "").lower()
            typ = (loc.get_attribute("type") or "").lower()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            tag, typ = "", ""
        score = 0
        if label and positive.search(label):
            score += 1000
        if typ == "submit":
            score += 700
        if code_box and box:
            code_cx = code_box["x"] + code_box["width"] / 2
            box_cx = box["x"] + box["width"] / 2
            dy = box["y"] - (code_box["y"] + code_box["height"])
            dx = abs(box_cx - code_cx)
            if -20 <= dy <= 520:
                score += max(0, 500 - int(max(0, dy)))
                score += max(0, 250 - int(dx))
        # Prefer controls with actual text over icon-only buttons.
        if label:
            score += 80
        key = (label.lower(), round(box["x"] if box else -1), round(box["y"] if box else -1), tag, typ)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((score, loc, label, box))

    candidates.sort(key=lambda item: item[0], reverse=True)

    def click_and_verify(loc, label: str, box, method: str) -> bool:
        before_url = str(page.url or "")
        try:
            loc.scroll_into_view_if_needed(timeout=1200)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        clicked = False
        try:
            if method == "locator":
                loc.click(timeout=3000)
            elif method == "mouse" and box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            elif method == "dom":
                loc.evaluate("""el => {
                    const opts = {bubbles: true, cancelable: true, view: window};
                    for (const name of ['pointerdown','mousedown','pointerup','mouseup','click']) {
                        try { el.dispatchEvent(new MouseEvent(name, opts)); } catch (_) {}
                    }
                    if (typeof el.click === 'function') el.click();
                }""")
            else:
                return False
            clicked = True
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return False
        if clicked:
            ok, evidence = _2fa_submit_transition(page, code_field, before_url, clicked_control=loc, timeout_seconds=5.5)
            if ok:
                log(f"{account}: 2FA confirmation activated via {method} ({label or 'unlabelled control'}; {evidence})", "OK")
                _record_direct_fallback(dump, f"submit_2fa_{method}")
                return True
            log(f"{account}: 2FA {method} click produced no form transition ({label or 'unlabelled control'})", "WARNING")
        return False

    # Try the most likely controls, using a real locator click first and then a
    # coordinate click.  Limit attempts to avoid pressing unrelated actions.
    for score, loc, label, box in candidates[:6]:
        if score < 120 and code_box is not None:
            continue
        if click_and_verify(loc, label, box, "locator"):
            return True
        try:
            box = loc.bounding_box(timeout=500) or box
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        if click_and_verify(loc, label, box, "mouse"):
            return True

    # Keyboard submit can be the only supported action in some OTP widgets.
    for source in ("field", "page"):
        before_url = str(page.url or "")
        try:
            if source == "field":
                code_field.press("Enter", timeout=2500)
            else:
                page.keyboard.press("Enter")
            ok, evidence = _2fa_submit_transition(page, code_field, before_url, timeout_seconds=5.5)
            if ok:
                log(f"{account}: 2FA confirmation activated with Enter ({evidence})", "OK")
                _record_direct_fallback(dump, f"submit_2fa_enter_{source}")
                return True
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass

    # Final targeted DOM click on the highest-ranked candidate.  Still require
    # evidence afterwards; do not report a false submit.
    if candidates:
        _, loc, label, box = candidates[0]
        if click_and_verify(loc, label, box, "dom"):
            return True

    # requestSubmit is safe only when the input really belongs to a form.
    try:
        form = code_field.locator("xpath=ancestor::form[1]")
        if int(form.count() or 0) > 0:
            before_url = str(page.url or "")
            ok = form.first.evaluate("""form => {
                if (typeof form.requestSubmit === 'function') { form.requestSubmit(); return true; }
                return false;
            }""")
            if ok:
                observed, evidence = _2fa_submit_transition(page, code_field, before_url, timeout_seconds=5.5)
                if observed:
                    log(f"{account}: 2FA form submitted with requestSubmit ({evidence})", "OK")
                    _record_direct_fallback(dump, "submit_2fa_request_submit")
                    return True
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return False

def _ensure_trust_device_checked(page, account: str = "", dump: LiveDump | None = None) -> None:
    """Make sure "Trust this device" is ON without ever toggling it OFF.

    IG renders it pre-checked. Blindly clicking the label (old behaviour) turned
    it off. We only click when we can confirm the box is currently unchecked.
    """
    getters = [
        lambda: page.get_by_role("checkbox", name=re.compile(r"trust this device", re.I)),
        lambda: page.locator("input[type='checkbox'][name*='trust' i]"),
        lambda: page.locator("input[type='checkbox']").first,
    ]
    for get in getters:
        try:
            el = get()
            if int(el.count() or 0) == 0:
                continue
            el = el.first
            try:
                checked = el.is_checked(timeout=1000)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                checked = None
            if checked is False:
                # definitively off → turn it on
                actor = _human_for(page, account, dump)
                if not (actor and actor.click(el, timeout=1500)):
                    try:
                        el.check(timeout=1500)
                        _record_direct_fallback(dump, "trust_device_check")
                    except Exception as _exc:
                        logger.debug("%s: %s", type(_exc).__name__, _exc)
                        try:
                            el.click(timeout=1500)
                            _record_direct_fallback(dump, "trust_device_click")
                        except Exception as _exc:
                            logger.debug("%s: %s", type(_exc).__name__, _exc)
                            pass
            return  # found the control (checked or unknown) → never toggle it off
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue


def _wait_after_2fa_submit(page, dump: LiveDump, max_seconds: int = 60) -> tuple[str, str]:
    """Observe the submitted OTP until an explicit, bounded outcome exists.

    The continued presence of the OTP form is deliberately not a rejection:
    Instagram can retain it while processing.  This keeps browser cleanup out
    of the interval between click and the final observable result.
    """
    deadline = time.time() + max_seconds
    last_state = "two_factor_required"
    last_reason = "2FA code submitted; waiting for validation"
    saw_validating = False
    while time.time() < deadline:
        lifecycle_state, lifecycle_reason = _two_factor_liveness(page)
        if lifecycle_state:
            dump.capture(page, "auto_login_2fa_" + lifecycle_state, lifecycle_reason, force_snapshot=True)
            return lifecycle_state, lifecycle_reason
        feedback_state, feedback_reason = _two_factor_feedback(page)
        if feedback_state:
            dump.capture(page, "auto_login_2fa_" + feedback_state, feedback_reason, force_snapshot=True)
            return feedback_state, feedback_reason
        rejection = _detect_login_rejection(page)
        if rejection:
            # Generic login wording must not turn a submitted OTP into an
            # incorrect-credentials/static-proxy recovery path.
            dump.capture(page, "auto_login_2fa_unclassified_feedback", "2FA page showed unclassified feedback", force_snapshot=True)
            return "two_factor_transition_timeout", "2FA feedback was not an explicit OTP result"
        try:
            txt = (page.locator("body").inner_text(timeout=1200) or "").lower()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            txt = ""
        if "being validated" in txt or "validating" in txt:
            saw_validating = True
            last_reason = "2FA code is being validated"
            time.sleep(2.0)
            continue
        try:
            current_url_lower = str(page.url or "").lower()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            current_url_lower = ""
        if "/challenge" in current_url_lower:
            return "challenge", "Instagram opened a challenge after 2FA submission"
        # Instagram redirects to /accounts/onetap/ (Save Login Info) or
        # /consent/ after a successful 2FA.  get_state() gives
        # continue_after_dialog only 0.8 s to find and click the save_login
        # dialog, which races with React lazy-mounting of [role='dialog'].
        # When the dialog is not found in time, get_state returns "unknown"
        # (session cookie exists but IG did not confirm authentication), and
        # the loop spins until the 60 s timeout.  Dispatching the full
        # _dismiss_post_login_prompts pipeline (settle_seconds=8, text-based
        # _login_info_prompt_present, dialog gate with 4 s budget) resolves
        # the intermediate page deterministically instead of relying on the
        # 0.8 s get_state budget.
        if (
            "/accounts/onetap/" in current_url_lower
            or "/consent/" in current_url_lower
            or _login_info_prompt_present(page)
        ):
            dump.capture(page, "auto_login_2fa_post_login_page", f"post-2FA intermediate page: {current_url_lower}")
            post_login = _dismiss_post_login_prompts(
                page,
                dump,
                "instagram_2fa",
                authenticated_confirmed=False,
                settle_seconds=8.0,
            )
            post_state = str(post_login.get("state") or "")
            if post_state == "logged_in" or post_login.get("operationally_ready"):
                return "logged_in", str(post_login.get("reason") or "post-2FA login info prompt resolved")
            if post_login.get("manual_required"):
                return str(post_login.get("state") or "unknown_popup"), str(post_login.get("reason") or "post-2FA dialog requires manual review")
            # The intermediate page was not resolved; fall through to get_state
            # which may still detect the session on the next iteration.
        state, reason = get_state(page)
        last_state, last_reason = state, reason
        if state in {"logged_in", "human_verification", "checkpoint", "challenge", "restricted", "suspended", "login_required"}:
            return state, reason
        if state == "two_factor_required":
            # The 2FA URL can remain visible for a while after submit while IG is
            # still validating. Give it time instead of closing the browser early.
            time.sleep(2.0 if saw_validating else 1.0)
            continue
        time.sleep(1.0)
    dump.capture(page, "auto_login_2fa_validation_timeout", last_reason, force_snapshot=True)
    return "two_factor_transition_timeout", "2FA post-submit transition timed out without an explicit result"


def _post_login_body_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1500) or "").lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return ""


def _login_info_prompt_present(page) -> bool:
    text = _post_login_body_text(page)
    return any(
        marker in text
        for marker in (
            "save your login info",
            "save login information",
            "save your login information",
        )
    )


def _dismiss_post_login_prompts(
    page,
    dump: LiveDump,
    account: str = "",
    *,
    login_info_action: str = "not_now",
    authenticated_confirmed: bool = False,
    settle_seconds: float = 8.0,
) -> dict:
    """Handle only identified post-login screens, then re-detect live state.

    The persistent browser profile owns session persistence. Automated login
    therefore declines Instagram's separate credential-saving offer. The
    explicit ``save`` option exists for characterization/recovery tests and
    controlled callers; it is never retried after navigation.
    """
    authenticated_before = bool(authenticated_confirmed or _authenticated_session_present(page))
    action_taken = "absent"
    action_telemetry = LoginPostActionTelemetry(page, dump)
    action_telemetry.start()

    try:
        # Diagnosed 2026-08-11: gating this click on authenticated_before
        # raced with _authenticated_session_present(page) not yet
        # confirming the session right after 2FA (DIAG log showed
        # authenticated_before=False, login_info_present=True — the click
        # was simply never attempted). The gate was also redundant:
        # Instagram only shows "Save your login info?" after a successful
        # login in the first place, so the prompt's presence is already
        # sufficient evidence. Not touching authenticated_before itself —
        # it's still used correctly below for other decisions — only
        # dropping it from this one click-gate.
        if _login_info_prompt_present(page):
            requested = str(login_info_action or "not_now").strip().lower()
            if requested == "save":
                patterns = (r"^\s*save info\s*$", r"^\s*save information\s*$", r"^\s*save\s*$")
                action_taken = "save"
            else:
                patterns = (r"^\s*not now\s*$", r"^\s*skip\s*$", r"^\s*later\s*$")
                action_taken = "not_now"
            clicked = _click_first(
                page,
                [
                    lambda pattern=re.compile(pattern, re.I): page.get_by_role("button", name=pattern)
                    for pattern in patterns
                ] + [
                    lambda pattern=re.compile(pattern, re.I): page.get_by_text(pattern, exact=True)
                    for pattern in patterns
                ],
                timeout_ms=2500,
                account=account,
                dump=dump,
                action=f"post_login_info_{action_taken}",
            )
            if not clicked:
                action_taken = "unresolved"
                dump.capture(
                    page,
                    "auto_login_info_unresolved",
                    f"identified popup but {requested} action was unavailable",
                    force_snapshot=True,
                )
            else:
                dump.capture(
                    page,
                    f"auto_login_info_{action_taken}",
                    f"clicked once; url_category={_instagram_url_category(page)}",
                    force_snapshot=True,
                )
                action_telemetry._emit(
                    "post_action_handled",
                    action=action_taken,
                    contract=HANDLED_REEVALUATE,
                    url_path=_safe_request_path(getattr(page, "url", "")),
                )
                _wait_for_current_dom(page)

        # Dialogs are order-independent.  A Save-login click does not suppress
        # discovery of the next Notifications/cookies/policy prompt.
        dialog_before = inspect_dialog(page)
        if dialog_before.get("present"):
            dialog_category = str(dialog_before.get("category") or "dialog")
            gate = continue_after_dialog(
                page,
                allow_safe_close=True,
                wait_seconds=4.0,
            )
            gate_outcome = str(gate.get("outcome") or "")
            if gate_outcome in {HANDLED_REEVALUATE, TRANSITIONING_RETRY} or gate.get("dismissed"):
                prior_action = action_taken
                action_taken = (
                    "dialog_" + dialog_category
                    if prior_action == "absent"
                    else prior_action + "+dialog_" + dialog_category
                )
                dump.capture(
                    page,
                    "auto_login_dialog_handled",
                    f"category={dialog_category}; DOM will be re-evaluated",
                    take_visible_text=False,
                )
                action_telemetry._emit(
                    "post_action_handled",
                    action=action_taken,
                    contract=HANDLED_REEVALUATE,
                    url_path=_safe_request_path(getattr(page, "url", "")),
                )
                _wait_for_current_dom(page)
            elif gate.get("present") or gate.get("state"):
                # Authentication is the terminal goal. Optional cleanup may be
                # incomplete, but cannot downgrade a confirmed session.
                auth_goal = continue_authentication_goal(
                    page,
                    timeout_seconds=0.5,
                    authenticated_hint=authenticated_before,
                )
                if auth_goal.get("ok"):
                    return {
                        **auth_goal,
                        "handled": action_taken != "absent",
                        "consent_state": "resolved",
                        "request_failed": False,
                        "reload_attempts": 0,
                        "url_category": _instagram_url_category(page),
                        "login_info_action": action_taken,
                        "post_action_outcome": HANDLED_REEVALUATE,
                    }
                return {
                    "ok": False,
                    "handled": False,
                    "state": str(gate.get("state") or "unknown_popup"),
                    "reason": "blocking Instagram dialog requires manual review",
                    "authenticated": bool(authenticated_before),
                    "operationally_ready": False,
                    "consent_state": "resolved",
                    "manual_required": True,
                    "request_failed": False,
                    "reload_attempts": 0,
                    "url_category": _instagram_url_category(page),
                    "login_info_action": "unresolved",
                    "post_action_outcome": gate_outcome or "TERMINAL_MANUAL",
                }

        handled = action_taken not in {"absent", "unresolved"}
        deadline = time.time() + max(0.0, float(settle_seconds))
        quiet_until = time.time() + min(3.0, max(0.0, float(settle_seconds)))
        stable_reads = 0
        while True:
            # A successful click invalidates the old DOM classification. Consent
            # may be routed a few seconds later, so every pass reads URL + DOM
            # again before authenticated success can become terminal.
            if consent_present(page) or consent_request_failed(page):
                result = _consent_recovery_result(
                    page,
                    dump,
                    authenticated_confirmed=authenticated_before,
                    max_navigation_attempts=3,
                )
                result["login_info_action"] = action_taken
                result["post_action_outcome"] = HANDLED_REEVALUATE if handled else NO_BLOCKER
                return result

            state, reason = get_state(page)
            login_info_blocking = _login_info_prompt_present(page)
            if login_info_blocking:
                state, reason = "unknown_popup", "Save-login popup remains unresolved"
            authenticated_now = bool(
                authenticated_before or _authenticated_session_present(page)
            )
            pending_navigation = bool(action_telemetry.pending_navigation_requests)
            rendered = _body_text_len(page) > 0
            ready_read = bool(
                state == "logged_in"
                and authenticated_now
                and rendered
                and not pending_navigation
                and not login_info_blocking
                and not consent_present(page)
                and not consent_request_failed(page)
            )
            stable_reads = stable_reads + 1 if ready_read else 0

            # With no handled action there is no stale pre-click classification
            # to drain. Test doubles without browser event hooks also retain the
            # legacy synchronous behavior.
            must_settle = handled and action_telemetry.can_observe_transitions
            if ready_read and (
                not must_settle
                or (stable_reads >= 2 and time.time() >= quiet_until)
            ):
                return {
                    "ok": True,
                    "handled": handled,
                    "state": state,
                    "reason": reason,
                    "authenticated": True,
                    "operationally_ready": True,
                    "consent_state": "resolved",
                    "manual_required": False,
                    "request_failed": False,
                    "reload_attempts": 0,
                    "url_category": _instagram_url_category(page),
                    "login_info_action": action_taken,
                    "post_action_outcome": HANDLED_REEVALUATE if handled else NO_BLOCKER,
                }
            if not must_settle or time.time() >= deadline:
                auth_goal = continue_authentication_goal(
                    page,
                    timeout_seconds=0.5,
                    authenticated_hint=authenticated_before,
                )
                if auth_goal.get("ok"):
                    return {
                        **auth_goal,
                        "handled": handled,
                        "consent_state": "resolved",
                        "request_failed": False,
                        "reload_attempts": 0,
                        "url_category": _instagram_url_category(page),
                        "login_info_action": action_taken,
                        "post_action_outcome": HANDLED_REEVALUATE if handled else NO_BLOCKER,
                    }
                return {
                    "ok": False,
                    "handled": handled,
                    "state": state,
                    "reason": (
                        "post-login navigation did not reach a rendered stable authenticated state"
                        if handled and (not rendered or pending_navigation or state == "unknown")
                        else reason
                    ),
                    "authenticated": bool(authenticated_now),
                    "operationally_ready": False,
                    "consent_state": "resolved",
                    "manual_required": True,
                    "request_failed": False,
                    "reload_attempts": 0,
                    "url_category": _instagram_url_category(page),
                    "login_info_action": action_taken,
                    "post_action_outcome": HANDLED_REEVALUATE if handled else NO_BLOCKER,
                    "pending_navigation": pending_navigation,
                }
            time.sleep(0.5)
    finally:
        action_telemetry.stop(action_taken)


def _wait_after_password_submit(page, dump: LiveDump, max_seconds: int = 60) -> str:
    deadline = time.time() + max_seconds
    last_state = ""
    while time.time() < deadline:
        ready, blocker_state, _blocker_reason = _login_blocker_first(
            page, dump, "", "login result processing", wait_seconds=0
        )
        if not ready:
            return blocker_state
        rejection = _detect_login_rejection(page)
        if rejection:
            dump.capture(page, "auto_login_rejected", rejection, force_snapshot=True)
            raise RuntimeError(rejection)
        state, reason = get_state(page)
        telemetry = getattr(dump, "_login_post_action_telemetry", None)
        if telemetry is not None:
            telemetry.observe(page, state, rejection=bool(rejection))
            request_outcome, failure_reason, _response_status = telemetry.login_request_outcome()
            if request_outcome == "failed":
                return "login_network_" + (failure_reason or "network_failure")
        last_state = state
        if state == "two_factor_required":
            return "2fa"
        if state in {"logged_in", "checkpoint", "restricted"}:
            return state
        try:
            txt = (page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            txt = ""
        if any(s in txt for s in ["authentication code", "security code", "verification code", "two-factor", "2fa"]):
            return "2fa"
        time.sleep(1.0)
    # One final fresh read is mandatory after the deadline. A SPA navigation
    # that commits on the boundary must never be overwritten by a stale state.
    ready, blocker_state, _blocker_reason = _login_blocker_first(
        page, dump, "", "final login result processing", wait_seconds=0
    )
    if not ready:
        return blocker_state
    rejection = _detect_login_rejection(page)
    if rejection:
        dump.capture(page, "auto_login_rejected", rejection, force_snapshot=True)
        raise RuntimeError(rejection)
    state, reason = get_state(page)
    telemetry = getattr(dump, "_login_post_action_telemetry", None)
    if telemetry is not None:
        telemetry.observe(page, state, rejection=False)
    if state == "two_factor_required":
        return "2fa"
    if state in {"logged_in", "checkpoint", "restricted"}:
        return state
    try:
        final_text = (page.locator("body").inner_text(timeout=1000) or "").lower()
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        final_text = ""
    if any(s in final_text for s in [
        "authentication code", "security code", "verification code", "two-factor", "2fa"
    ]):
        return "2fa"

    final_state = state or last_state or "unknown"
    if telemetry is not None:
        request_outcome, failure_reason, response_status = telemetry.login_request_outcome()
        if request_outcome == "failed":
            return "login_network_" + (failure_reason or "network_failure")
        if request_outcome == "pending":
            return "login_request_pending_timeout"
        if request_outcome == "finished" and final_state == "login_required":
            return (
                f"login_response_{response_status}_form_unchanged"
                if response_status else "login_response_form_unchanged"
            )
        final_ui = _login_post_action_ui(page)
        if request_outcome == "not_started" and (
            final_ui.get("submit_loading") or final_ui.get("login_form_disabled")
        ):
            return "login_loading_without_request"
    if final_state == "login_required":
        # A slow mobile route can show only Instagram's splash while a stale or
        # hidden password input remains in the DOM. Only a visible rendered
        # login form is authoritative after the transition timeout.
        if _body_text_len(page) <= 0 or not _login_fields_available(page):
            final_state = "unknown"
    dump.capture(page, "auto_login_wait_after_password", f"state={final_state}")
    return final_state


def _hold_manual_post_login(
    page,
    ctx,
    dump: LiveDump,
    name: str,
    job: int,
    reason: str,
) -> bool:
    """Keep an unresolved headed post-login flow open until user or auth wins."""
    update_account(
        name,
        web_upload_login_status="manual_required",
        web_upload_last_error=reason,
    )
    update_job(
        job,
        status="manual_required",
        current_step="manual_required",
        last_error=reason,
        finished_at=None,
    )
    dump.capture(
        page,
        "auto_login_manual_required",
        reason,
        force_snapshot=True,
    )
    log(
        f"{name}: post-login action requires manual review; browser remains open",
        "WARNING",
    )
    stable_reads = 0
    while True:
        try:
            if page.is_closed():
                return False
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        try:
            if not [item for item in (getattr(ctx, "pages", []) or []) if not item.is_closed()]:
                return False
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        state, _state_reason = get_state(page)
        ready = bool(
            state == "logged_in"
            and _authenticated_session_present(page)
            and _body_text_len(page) > 0
            and not consent_present(page)
            and not consent_request_failed(page)
            and not inspect_dialog(page).get("present")
        )
        stable_reads = stable_reads + 1 if ready else 0
        if stable_reads >= 2:
            return True
        time.sleep(1.0)


def _password_submission_blocker_reason(error: Any) -> str:
    value = str(error or "").strip().lower()
    if not value.startswith("password_submission_blocked:"):
        return ""
    reason = value.split(":", 1)[1].strip()
    return (
        reason
        if reason in {
            "password_submission_already_reserved",
            "password_submission_limit_reached",
            "recovery_workflow_not_active",
            "second_submission_not_ready",
        }
        else "durable_recovery_gate_rejected_submit"
    )


_TYPED_POPUP_FAILURES = {
    "browser_internal_error",
    "cookie_consent_action_unavailable",
    "cookie_consent_transition_timeout",
    "ads_consent_action_unavailable",
    "ads_consent_loop_detected",
    "ads_consent_transition_timeout",
    "save_login_info_action_unavailable",
    "notifications_prompt_action_unavailable",
    "promo_or_ad_action_unavailable",
    "open_in_app_action_unavailable",
    "popup_action_unavailable",
    "popup_transition_timeout",
    "unrecognized_surface",
    "unknown_blocker",
}


def _typed_popup_failure(result: Any) -> str:
    value = dict(result or {}) if isinstance(result, dict) else {}
    step = str(value.get("step") or "").strip().lower()
    return step if not value.get("ok") and step in _TYPED_POPUP_FAILURES else ""


def _persist_browser_domain_failure(
    *,
    run_id: str,
    name: str,
    job: int,
    code: str,
    detail: str,
    dump: LiveDump | None = None,
    page: Any = None,
) -> None:
    """Flush diagnostics and both durable outcomes before browser cleanup."""
    safe_code = str(code or "browser_domain_failure")[:80]
    safe_detail = str(detail or safe_code)[:240]
    if dump is not None and page is not None:
        try:
            dump.capture(
                page,
                safe_code,
                safe_detail,
                force_snapshot=True,
            )
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
    update_account(
        name,
        web_upload_login_status=safe_code,
        web_upload_last_error=safe_detail,
    )
    update_job(
        job,
        status="failed",
        current_step=safe_code,
        last_error=safe_detail,
        domain_outcome=safe_code,
        infrastructure_outcome="",
        closure_owner="browser_workflow",
        closure_reason=safe_code,
        finished_at=now_iso(),
    )
    record_task_outcome(
        run_id,
        domain_outcome=safe_code,
        closure_owner="browser_workflow",
        closure_reason=safe_code,
    )


def _source_live_debug_enabled(args: Any) -> bool:
    return bool(
        os.environ.get("SPARKGRID_SOURCE_LIVE_DEBUG") == "1"
        and not bool(getattr(args, "headless", False))
    )


def _record_source_routing_fingerprint(run_id: str) -> None:
    try:
        files = {
            "blocking_popup_transaction_sha256": (
                ROOT / "blocking_popup_transaction.py"
            ),
            "instagram_web_profile_workflow_sha256": Path(__file__).resolve(),
        }
        hashes = {
            key: hashlib.sha256(path.read_bytes()).hexdigest()
            for key, path in files.items()
        }
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        append_run_event_once(
            run_id,
            "source_routing_fingerprint",
            routing_schema="stage1-minimal-v1",
            git_head=str(head.stdout or "").strip().lower(),
            dirty_worktree=bool(str(status.stdout or "").strip()),
            **hashes,
        )
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _popup_handler_result(value: Any) -> str:
    result = dict(value or {}) if isinstance(value, dict) else {}
    outcome = str(result.get("outcome") or "")
    if outcome == HANDLED_REEVALUATE:
        return "handled_reevaluate"
    if outcome == NO_BLOCKER:
        return "no_blocker"
    if outcome == TRANSITIONING_RETRY:
        return "transitioning_retry"
    if result.get("ok"):
        return "completed"
    return "action_unavailable"


def _record_known_popup_completion(
    dump: Any,
    popup_category: str,
    before: dict[str, Any],
    after: dict[str, Any],
    handler_result: Any,
) -> None:
    try:
        append_run_event(
            str(dump.run_id),
            "known_popup_handler_completed",
            popup_category=popup_category,
            handler_result=_popup_handler_result(handler_result),
            fresh_reclassification_started=True,
            document_epoch_before=_diagnostic_document_epoch(
                dump, before.get("document_epoch")
            ),
            document_epoch_after=_diagnostic_document_epoch(
                dump, after.get("document_epoch")
            ),
            mutation_epoch_before=int(
                before.get("mutation_epoch") or 0
            ),
            mutation_epoch_after=int(after.get("mutation_epoch") or 0),
        )
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _source_live_stop_requested(page: Any, context: Any) -> bool:
    stop_path = str(
        os.environ.get("SPARKGRID_SOURCE_LIVE_STOP_FILE") or ""
    ).strip()
    if stop_path and Path(stop_path).is_file():
        return True
    try:
        if page.is_closed():
            return True
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return True
    try:
        pages = list(getattr(context, "pages", []) or [])
        if not any(not item.is_closed() for item in pages):
            return True
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return True
    return False


def _hold_unrecognized_surface_for_source_live(
    page: Any,
    context: Any,
    args: Any,
    *,
    stop_requested: Any = None,
    sleep_fn: Any = time.sleep,
) -> bool:
    """Hold only a persisted source-live unknown surface until operator Stop."""
    if not _source_live_debug_enabled(args):
        return False
    should_stop = stop_requested or _source_live_stop_requested
    while not should_stop(page, context):
        sleep_fn(0.5)
    return True


@contextlib.contextmanager
def _browser_pre_cleanup_finalizer(callback):
    token = _ACTIVE_BROWSER_PRE_CLEANUP_FINALIZER.set(callback)
    try:
        yield
    finally:
        _ACTIVE_BROWSER_PRE_CLEANUP_FINALIZER.reset(token)


def _resolve_arrival_popup(page: Any, dump: LiveDump | None) -> dict[str, Any]:
    """Route the current topmost blocker through one bounded transaction."""
    observed = inspect_topmost_blocker(page)
    category = str(observed.get("category") or "")
    if observed.get("document_category") == "browser_internal_error":
        return {"handled": False, "ok": False, "step": "browser_internal_error"}
    if observed.get("authenticated_surface"):
        _record_arrival_route(dump, "authenticated", observed=observed)
        return {"handled": False, "ok": True, "step": "not_present"}
    if observed.get("two_factor_surface"):
        _record_arrival_route(dump, "two_factor", observed=observed)
        return {"handled": False, "ok": True, "step": "not_present"}
    surface = _observe_login_surface(page)
    if surface.state in {"authenticated", "challenge", "two_factor"}:
        _record_arrival_route(dump, surface.state, surface=surface)
        return {"handled": False, "ok": True, "step": "not_present"}
    if category in {"checkpoint", "restriction", "suspended"}:
        _record_arrival_route(dump, "challenge", observed=observed)
        return {"handled": False, "ok": True, "step": "not_present"}
    if observed.get("present") and category in {
        "cookie_consent",
        "regional_ads_consent",
        "request_processing",
    }:
        _record_arrival_route(
            dump,
            "known_popup",
            popup_category=category,
            observed=observed,
        )
        result = resolve_instagram_consent(
            page,
            _consent_capture(dump),
            max_seconds=70,
            human=_human_for(
                page,
                str(getattr(dump, "account", "") or ""),
                dump,
            ),
        )
        after = inspect_topmost_blocker(page)
        _record_known_popup_completion(
            dump, category, observed, after, result
        )
        return result
    if not observed.get("present") or category not in AUTOMATED_POPUP_CATEGORIES:
        if _strong_combined_login_surface(surface):
            _record_arrival_route(
                dump, "credential_surface", surface=surface
            )
            return {
                "handled": False,
                "ok": True,
                "step": "credential_surface",
            }
        if surface.state in {
            "authenticated",
            "challenge",
            "two_factor",
            "transitioning",
        }:
            _record_arrival_route(
                dump,
                (
                    "transitional"
                    if surface.state == "transitioning"
                    else surface.state
                ),
                surface=surface,
            )
            return {
                "handled": False,
                "ok": True,
                "step": "not_present",
            }
        _record_arrival_route(
            dump, "unrecognized_surface", surface=surface
        )
        return {
            "handled": False,
            "ok": False,
            "step": "unrecognized_surface",
        }

    _record_arrival_route(
        dump,
        "known_popup",
        popup_category=category,
        observed=observed,
    )
    gate = continue_after_dialog(
        page,
        allow_safe_close=True,
        wait_seconds=4.0,
    )
    after = inspect_topmost_blocker(page)
    _record_known_popup_completion(
        dump, category, observed, after, gate
    )
    outcome = str(gate.get("outcome") or "")
    if outcome in {NO_BLOCKER, HANDLED_REEVALUATE}:
        return {
            "handled": bool(gate.get("dismissed")),
            "ok": True,
            "step": "completed",
        }
    if outcome == TRANSITIONING_RETRY:
        return {
            "handled": bool(gate.get("dismissed")),
            "ok": False,
            "step": "popup_transition_timeout",
        }
    if outcome == "UNKNOWN_BLOCKER":
        return {
            "handled": False,
            "ok": False,
            "step": "unrecognized_surface",
        }
    return {
        "handled": bool(gate.get("dismissed")),
        "ok": False,
        "step": (
            category + "_action_unavailable"
            if category + "_action_unavailable" in _TYPED_POPUP_FAILURES
            else "popup_action_unavailable"
        ),
    }


def do_auto_login(account: dict, args, run_id: str):
    name = account["name"]
    dump = LiveDump(run_id, name)
    _record_source_routing_fingerprint(run_id)
    job = create_job(run_id, name, "auto_login", str(dump.root), provider=getattr(args, "provider", "camoufox"))
    update_account(name, web_upload_last_error="")
    log(f"{name}: Auto login attempt started", "INFO")
    try:
        proxy = _proxy_for_account(account)
        if proxy and not bool(getattr(args, "skip_proxy_check", False)):
            ok, reason = _check_proxy_reachable(proxy)
            if not ok:
                raise RuntimeError(reason)
            log(f"{name}: {reason}", "OK")
        def persist_before_cleanup(exc: BaseException, page: Any) -> None:
            reason = _password_submission_blocker_reason(exc)
            if reason:
                _persist_browser_domain_failure(
                    run_id=run_id,
                    name=name,
                    job=job,
                    code="password_submission_blocked",
                    detail="password_submission_blocked:" + reason,
                    dump=dump,
                    page=page,
                )

        with _browser_pre_cleanup_finalizer(
            persist_before_cleanup
        ), _browser_session(account, args) as (ctx_obj, ctx, page):
            session_ready_for_persistence = False
            post_login_transition = None
            arrival = _arrive_instagram(
                page,
                dump,
                via_search=(getattr(args, "arrive", "direct") == "search"),
                account=name,
                mode=getattr(args, "mode", "desktop"),
            )
            # Historical offline callers replace the arrival helper with a
            # side-effect-only stub. Production returns the structured result.
            if arrival is None:
                arrival = {"ok": True}
            if not arrival.get("ok"):
                code = "browser_load_failed_after_retry"
                detail = (
                    code
                    + ":"
                    + str(
                        arrival.get("main_frame_failure_category")
                        or "unknown_failure"
                    )
                )
                update_account(
                    name,
                    web_upload_login_status=code,
                    web_upload_last_error=detail,
                )
                update_job(
                    job,
                    status="failed",
                    current_step=code,
                    last_error=detail,
                    domain_outcome="failed",
                    infrastructure_outcome=code,
                    finished_at=now_iso(),
                )
                return
            actor = _human_for(page, name, dump)
            if actor is not None:
                actor.dwell(1.4, 2.8, micro_moves=True)
                actor.wander(1)
            else:
                time.sleep(random.uniform(2.0, 4.0))
            consent_result = _resolve_arrival_popup(page, dump)
            popup_failure = _typed_popup_failure(consent_result)
            if popup_failure:
                _persist_browser_domain_failure(
                    run_id=run_id,
                    name=name,
                    job=job,
                    code=popup_failure,
                    detail=popup_failure,
                    dump=dump,
                    page=page,
                )
                if popup_failure == "unrecognized_surface":
                    update_job(
                        job,
                        status="manual_required",
                        current_step="unrecognized_surface",
                        last_error="unrecognized_surface",
                        domain_outcome="unrecognized_surface",
                        finished_at=now_iso(),
                    )
                    _hold_unrecognized_surface_for_source_live(
                        page, ctx, args
                    )
                return
            if _is_consent_loop(page) and not _recover_instagram_consent(page, dump, name):
                reason = "Instagram consent remained unresolved after bounded recovery"
                dump.capture(page, "consent_failed", reason, force_snapshot=True)
                update_account(name, web_upload_login_status="consent_failed", web_upload_last_error=reason)
                update_job(job, status="manual_required", current_step="consent_failed", last_error=reason, finished_at=now_iso())
                return
            state, reason = get_state(page)
            if state == "logged_in":
                dump.capture(page, "auto_login_already_logged_in", reason, force_snapshot=True)
                update_account(name, web_upload_login_status="logged_in", web_upload_last_error="", web_upload_last_login_at=now_iso())
                update_job(job, status="success", current_step="logged_in", finished_at=now_iso())
                session_ready_for_persistence = True
            else:
                if state in ("login_required", "unknown"):
                    _click_login_if_present(page, dump, name)
                    time.sleep(random.uniform(1.0, 2.0))
                    _dismiss_instagram_consent(page, dump, name)
                _record_credential_workflow_started(page, dump)
                if not _fill_instagram_login_form(page, account, dump):
                    code = _login_form_failure_code(dump)
                    update_account(name, web_upload_login_status=code, web_upload_last_error=code)
                    if code in {
                        "unknown_popup",
                        "blocking_dialog_not_dismissed",
                        "blocker_detected",
                        "unrecognized_surface",
                    }:
                        typed_code = (
                            "unrecognized_surface"
                            if code in {
                                "unknown_popup",
                                "blocker_detected",
                                "unrecognized_surface",
                            }
                            else "popup_action_unavailable"
                        )
                        _persist_browser_domain_failure(
                            run_id=run_id,
                            name=name,
                            job=job,
                            code=typed_code,
                            detail=typed_code,
                            dump=dump,
                            page=page,
                        )
                        if typed_code == "unrecognized_surface":
                            update_job(
                                job,
                                status="manual_required",
                                current_step="unrecognized_surface",
                                last_error="unrecognized_surface",
                                domain_outcome="unrecognized_surface",
                                finished_at=now_iso(),
                            )
                            _hold_unrecognized_surface_for_source_live(
                                page, ctx, args
                            )
                        return
                    manual = code in {
                        "unknown_popup", "checkpoint", "restricted", "suspended",
                        "blocking_dialog_not_dismissed",
                        "challenge_detected", "blocker_detected",
                    }
                    update_job(job, status="manual_required" if manual else "failed", current_step=code, last_error=code, finished_at=now_iso())
                    return
                if bool(
                    getattr(
                        dump, "_login_transaction_authenticated", False
                    )
                ):
                    post_password_state = "authenticated"
                elif bool(
                    getattr(dump, "_login_transaction_two_factor", False)
                ):
                    post_password_state = "2fa"
                else:
                    post_password_state = _wait_after_password_submit(
                        page, dump, max_seconds=60
                    )
                telemetry = getattr(dump, "_login_post_action_telemetry", None)
                if telemetry is not None:
                    telemetry.stop(post_password_state)
                    dump._login_post_action_telemetry = None
                if post_password_state == "2fa":
                    state, reason = "two_factor_required", "2FA code requested"
                    log(f"{name}: Instagram requested 2FA", "INFO")
                    max_2fa_attempts = 3
                    submitted_totp_codes: set[str] = set()
                    window_waits = 0
                    while len(submitted_totp_codes) < max_2fa_attempts:
                        if submitted_totp_codes:
                            dump.capture(page, f"auto_login_2fa_retry_{len(submitted_totp_codes) + 1}", reason, force_snapshot=True)
                            _wait_for_next_totp_window(account, dump)
                        submitted, submit_state = _try_submit_totp(
                            page, account, dump, submitted_codes=submitted_totp_codes,
                        )
                        if not submitted:
                            if submit_state in {"unknown_popup", "checkpoint", "restricted", "suspended"}:
                                state = submit_state
                                reason = "blocking Instagram dialog interrupted the 2FA transition"
                                break
                            if submit_state == "two_factor_wait_next_window" and window_waits < 2:
                                window_waits += 1
                                _wait_for_next_totp_window(account, dump)
                                continue
                            state, reason = get_state(page)
                            if submit_state == "two_factor_code_required":
                                state = submit_state
                                reason = "a saved valid TOTP secret and a fresh automatic code are required"
                            if not bool(getattr(args, "headless", False)) and state == "two_factor_required":
                                log(f"{name}: Automatic 2FA confirmation was not activated. Browser will stay open for 120 seconds for manual confirmation.", "WARNING")
                                dump.capture(page, "auto_login_2fa_manual_fallback", "automatic confirmation click failed; waiting for manual confirmation", force_snapshot=True)
                                state, reason = _wait_after_2fa_submit(page, dump, max_seconds=120)
                            break
                        state, reason = _wait_after_2fa_submit(page, dump, max_seconds=60)
                        if state not in {"two_factor_code_rejected", "two_factor_code_expired"}:
                            break
                    if state in {"two_factor_code_rejected", "two_factor_code_expired"} and len(submitted_totp_codes) >= max_2fa_attempts:
                        state = "two_factor_failed_after_retries"
                        reason = "Instagram explicitly rejected or expired all allowed 2FA submissions"
                elif post_password_state in {"unknown_popup", "checkpoint", "restricted", "suspended"}:
                    state = post_password_state
                    reason = "blocking Instagram dialog interrupted the login transition"
                elif post_password_state.startswith("login_network_"):
                    state = post_password_state
                    reason = "Instagram login request failed: " + post_password_state.removeprefix("login_network_")
                elif post_password_state == "login_request_pending_timeout":
                    state = post_password_state
                    reason = "Instagram login request remained pending for the bounded post-submit timeout"
                elif post_password_state.startswith("login_response_"):
                    state = post_password_state
                    reason = "Instagram login response completed but the login form remained actionable"
                elif post_password_state == "login_loading_without_request":
                    state = post_password_state
                    reason = "Instagram kept the login form loading without starting a login request"
                elif post_password_state == "authenticated":
                    state = "logged_in"
                    reason = "structural Auto Login observation confirmed authentication"
                else:
                    time.sleep(random.uniform(5.0, 8.0))
                    state, reason = get_state(page)
                if state == "logged_in":
                    post_login_transition = _dismiss_post_login_prompts(
                        page,
                        dump,
                        name,
                        authenticated_confirmed=True,
                    )
                    state = str(post_login_transition.get("state") or "unknown")
                    reason = str(post_login_transition.get("reason") or "post-login state unresolved")
                    if (
                        post_login_transition.get("manual_required")
                        and not bool(getattr(args, "headless", False))
                    ):
                        manually_resolved = _hold_manual_post_login(
                            page,
                            ctx,
                            dump,
                            name,
                            job,
                            reason,
                        )
                        if not manually_resolved:
                            update_job(
                                job,
                                status="manual_required",
                                current_step="manual_closed",
                                last_error=reason,
                                finished_at=now_iso(),
                            )
                            return
                        post_login_transition = {
                            **post_login_transition,
                            "ok": True,
                            "state": "logged_in",
                            "reason": "authenticated state confirmed after manual post-login action",
                            "authenticated": True,
                            "operationally_ready": True,
                            "manual_required": False,
                        }
                        state = "logged_in"
                        reason = str(post_login_transition["reason"])
                dump.capture(page, "auto_login_" + state, reason, force_snapshot=True)
                ok = bool(
                    state == "logged_in"
                    and (
                        post_login_transition is None
                        or post_login_transition.get("operationally_ready")
                    )
                )
                session_ready_for_persistence = ok
                failed = (
                    state.startswith("login_network_")
                    or state in {
                        "login_request_pending_timeout",
                        "login_loading_without_request",
                    }
                )
                if not ok:
                    log(f"{name}: Auto login did not complete: {state} · {reason}", "WARNING")
                update_account(name, web_upload_login_status=state, web_upload_last_error="" if ok else reason, **({"web_upload_last_login_at": now_iso()} if ok else {}))
                update_job(
                    job,
                    status="success" if ok else ("failed" if failed else "manual_required"),
                    current_step=state,
                    last_error="" if ok else reason,
                    finished_at=now_iso(),
                )
            try:
                if (
                    session_ready_for_persistence
                    and getattr(args, "provider", "playwright") == "camoufox"
                ):
                    _save_camoufox_state(ctx, name)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            ctx.close()
            try:
                if 'ctx_obj' in locals() and ctx_obj and hasattr(ctx_obj, "__exit__"):
                    ctx_obj.__exit__(None, None, None)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        error = str(exc) or type(exc).__name__
        lowered = error.lower()
        reason = _password_submission_blocker_reason(error)
        if reason:
            code = "password_submission_blocked"
            detail = code + ":" + reason
            update_account(
                name,
                web_upload_login_status=code,
                web_upload_last_error=detail,
            )
            update_job(
                job,
                status="failed",
                current_step=code,
                last_error=detail,
                domain_outcome=code,
                infrastructure_outcome="",
                finished_at=now_iso(),
            )
            log(
                f"{name}: duplicate-safe password submission gate blocked the action",
                "ERROR",
            )
            return
        incorrect_credentials = "explicit_password_rejection:" in lowered
        if incorrect_credentials:
            update_account(
                name,
                web_upload_login_status="incorrect_credentials",
                web_upload_last_error="Instagram rejected the submitted credentials",
            )
            update_job(
                job,
                status="failed",
                current_step="incorrect_credentials",
                last_error="Instagram rejected the submitted credentials",
                finished_at=now_iso(),
            )
            log(f"{name}: Instagram rejected the login credentials; browser closed", "ERROR")
            return
        if _finish_proxy_failure(name, job, exc):
            return
        update_account(name, web_upload_login_status="failed", web_upload_last_error=error)
        update_job(job, status="failed", current_step="crashed", last_error=error, finished_at=now_iso())
        raise


@contextlib.contextmanager
def _browser_session(account: dict, args):
    """Yield (ctx_obj, ctx, page).

    Camoufox manages its OWN Playwright, so it must NOT run inside a
    sync_playwright() block (that raises 'Sync API inside the asyncio loop' and
    silently falls back to Chromium). Playwright Chromium, by contrast, needs the
    sync_playwright context to stay open while the page is used.
    """
    provider = getattr(args, "provider", "camoufox")
    headless = bool(getattr(args, "headless", False))
    manual = bool(getattr(args, "keep_open", False))
    if provider == "camoufox":
        manager = None
        context = None
        try:
            manager, context, page = launch_context(
                None, account, provider="camoufox", headless=headless, manual=manual
            )
            try:
                yield manager, context, page
            except BaseException as exc:
                finalizer = _ACTIVE_BROWSER_PRE_CLEANUP_FINALIZER.get()
                if finalizer is not None:
                    finalizer(exc, page)
                raise
        finally:
            # A rejected password, proxy failure, or page exception must never
            # leave SparkBrowser/profile locks alive after the workflow ends.
            if context is not None:
                try:
                    context.close()
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
            if manager is not None and hasattr(manager, "__exit__"):
                try:
                    manager.__exit__(None, None, None)
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
            # Do not let the worker exit (and the scheduler start the next
            # profile) while the native Camoufox window is still tearing down.
            if context is not None or manager is not None:
                time.sleep(1.0)
    else:
        sync_playwright = require_playwright()
        with sync_playwright() as p:
            _, context, page = launch_context(
                p, account, provider="playwright", headless=headless
            )
            try:
                try:
                    yield None, context, page
                except BaseException as exc:
                    finalizer = _ACTIVE_BROWSER_PRE_CLEANUP_FINALIZER.get()
                    if finalizer is not None:
                        finalizer(exc, page)
                    raise
            finally:
                try:
                    context.close()
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass


def _keep_manual_profile_open(name: str, account: dict, dump: LiveDump, job: int, ctx, page):
    """Open the profile for human control without focus stealing.

    Runtime v2.2 keeps manual recording passive. The old implementation took a
    Playwright screenshot and full HTML snapshot every five seconds; on macOS
    that can make the browser flash or jump above other windows. We now record
    DOM/network events continuously, save cookies periodically, and take screen
    snapshots only once at open and once at close unless explicitly enabled.
    """
    pages = []
    try:
        pages = [p for p in (getattr(ctx, "pages", []) or []) if not p.is_closed()]
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pages = []
    if pages:
        page = pages[0]
    else:
        try:
            page = ctx.new_page()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass

    try:
        for wait_until in ("domcontentloaded", "commit"):
            try:
                page.goto("https://www.instagram.com/?hl=en", wait_until=wait_until, timeout=60000)
                if "instagram.com" in (page.url or ""):
                    break
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                continue
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass

    _dismiss_instagram_consent(page, dump, name)
    if _is_consent_loop(page) and not _recover_instagram_consent(page, dump, name):
        reason = "Instagram consent remained unresolved after bounded recovery"
        dump.capture(page, "consent_failed", reason, force_snapshot=True)
        update_account(name, web_upload_login_status="consent_failed", web_upload_last_error=reason)
        update_job(job, status="manual_required", current_step="consent_failed", last_error=reason, finished_at=now_iso())
        try:
            ctx.close()
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        return

    # Browser launch already makes the window visible. Manual mode must never
    # issue foreground/focus commands after startup.
    try:
        dump.capture(page, "manual_open_ready", "SparkBrowser is ready for manual control", force_snapshot=True)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    try:
        import manual_recorder
        recpath = manual_recorder.attach(page, name, dump.root)
        if recpath:
            log(f"{name}: recording manual session -> {recpath}", "INFO")
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        log(f"{name}: manual recorder not attached ({exc})", "WARNING")

    update_job(job, status="manual_required", current_step="manual_open", last_error="", finished_at=None)
    log(f"{name}: SparkBrowser opened for manual control. Automation is paused until you close the browser.", "OK")

    periodic_screenshots = os.environ.get("SPARKGRID_MANUAL_LIVE_SCREENSHOTS") == "1"
    next_state_save = 0.0
    next_debug_capture = 0.0
    last_url = ""
    while True:
        try:
            if hasattr(page, "is_closed") and page.is_closed():
                break
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            break
        try:
            live_pages = [p for p in (getattr(ctx, "pages", []) or []) if not p.is_closed()]
            if not live_pages:
                break
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass

        now = time.time()
        if now >= next_state_save:
            next_state_save = now + 15.0
            try:
                _save_camoufox_state(ctx, name)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            try:
                current_url = str(page.url or "")
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                current_url = ""
            if current_url != last_url:
                last_url = current_url
                try:
                    dump.capture(
                        page,
                        "manual_navigation",
                        current_url,
                        take_screenshot=False,
                        take_visible_text=False,
                    )
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass

        # Optional deep debugging. Disabled by default because screenshots can
        # visibly flash a headed Firefox window on macOS.
        if periodic_screenshots and now >= next_debug_capture:
            next_debug_capture = now + 30.0
            try:
                dump.capture(page, "manual_live", "optional manual trace checkpoint")
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
        time.sleep(1.0)

    try:
        _save_camoufox_state(ctx, name)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    try:
        if not page.is_closed():
            dump.capture(page, "manual_closed", "final manual trace checkpoint", force_snapshot=True)
            (dump.root / "latest.html").write_text(page.content(), encoding="utf-8")
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    update_job(job, status="success", current_step="manual_closed", last_error="", finished_at=now_iso())


def do_check_login(account: dict, args, run_id: str):
    name = account["name"]
    dump = LiveDump(run_id, name)
    job = create_job(run_id, name, "check_login", str(dump.root))
    network_capture = None
    try:
        proxy = _proxy_for_account(account)
        if proxy and not bool(getattr(args, "skip_proxy_check", False)):
            ok, reason = _check_proxy_reachable(proxy)
            if not ok:
                raise RuntimeError(reason)
            log(f"{name}: {reason}", "OK")
        with _browser_session(account, args) as (ctx_obj, ctx, page):
            if args.keep_open:
                try:
                    if start_instagram_network_capture is not None:
                        network_capture = start_instagram_network_capture(
                            ctx,
                            dump.root,
                            account=name,
                            run_id=run_id,
                            phase="manual_profile_capture",
                        )
                        if network_capture is not None:
                            log(
                                f"{name}: manual Instagram network capture active -> "
                                f"{dump.root / 'network'}",
                                "OK",
                            )
                except Exception as exc:
                    logger.debug("%s: %s", type(exc).__name__, exc)
                    log(
                        f"{name}: manual network capture did not start: "
                        f"{type(exc).__name__}",
                        "WARNING",
                    )
                try:
                    _keep_manual_profile_open(name, account, dump, job, ctx, page)
                finally:
                    try:
                        if network_capture is not None:
                            network_capture.stop()
                            log(
                                f"{name}: manual network capture saved -> "
                                f"{dump.root / 'network'}",
                                "OK",
                            )
                    except Exception as exc:
                        logger.debug("%s: %s", type(exc).__name__, exc)
                        log(
                            f"{name}: manual capture finalize failed: "
                            f"{type(exc).__name__}",
                            "WARNING",
                        )
                return "manual_open", "manual browser closed"
            arrival = _arrive_instagram(
                page,
                dump,
                via_search=(getattr(args, "arrive", "direct") == "search"),
                account=name,
                mode=getattr(args, "mode", "desktop"),
            )
            if not arrival.get("ok"):
                code = "browser_load_failed_after_retry"
                detail = (
                    code
                    + ":"
                    + str(
                        arrival.get("main_frame_failure_category")
                        or "unknown_failure"
                    )
                )
                update_account(
                    name,
                    web_upload_login_status=code,
                    web_upload_last_error=detail,
                )
                update_job(
                    job,
                    status="failed",
                    current_step=code,
                    last_error=detail,
                    domain_outcome="failed",
                    infrastructure_outcome=code,
                    finished_at=now_iso(),
                )
                return code, detail
            result, _callbacks = run_check_session_goal(
                page,
                workflow_run_id=run_id,
                timeout_seconds=8.0,
                poll_interval=0.2,
            )
            if result.code is SESSION_AUTHENTICATED_CONFIRMED:
                state, reason = (
                    "logged_in",
                    "authenticated browser session confirmed",
                )
            elif result.code is SESSION_LOGIN_REQUIRED:
                state, reason = "login_required", "visible login screen confirmed"
            elif result.code is SESSION_STABLE_BLOCKER:
                state = result.operation_state or "unknown"
                reason = "stable browser blocker confirmed"
                # When the session check hits a stable blocker that it cannot
                # classify (checkpoint, face verification, phone/email challenge,
                # restriction, or an unknown block page), treat the account as
                # suspended so it shows up in "Delete banned" and is excluded
                # from future workflows.  The blocker text is preserved in
                # web_upload_last_error for manual inspection.
                if state in ("unknown", "unknown_dialog", "") or state not in (
                    "cookie_consent", "save_login", "notification", "policy_notice"
                ):
                    state = "suspended"
            elif result.code is SESSION_NO_PROGRESS_TIMEOUT:
                state, reason = "suspended", "session check made no progress; account may be blocked"
            else:
                state = result.operation_state or "failed"
                reason = result.error_category or "session check failed"
            dump.capture(page, "check_login_" + state, reason, force_snapshot=True)
            previous_status = str(account.get("web_upload_login_status") or "")
            # A dialog that could not be closed is scoped to this operation;
            # retain the prior account state so it is neither quarantined nor
            # highlighted as a lasting account problem.
            if state != "blocking_dialog_not_dismissed":
                update_account(name, web_upload_login_status=state, web_upload_last_error="" if state == "logged_in" else reason)
            update_job(job, status="success" if state == "logged_in" else "manual_required", current_step=state, last_error="" if state == "logged_in" else reason, finished_at=now_iso())
            try:
                if (
                    state == "logged_in"
                    and getattr(args, "provider", "playwright") == "camoufox"
                ):
                    _save_camoufox_state(ctx, name)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            ctx.close()
            try:
                if 'ctx_obj' in locals() and ctx_obj and hasattr(ctx_obj, "__exit__"):
                    ctx_obj.__exit__(None, None, None)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        error = str(exc) or type(exc).__name__
        if _finish_proxy_failure(name, job, error):
            return
        update_account(name, web_upload_last_error=error)
        update_job(job, status="failed", current_step="crashed", last_error=error, finished_at=now_iso())
        raise


# ── Instagram ACCOUNT warmup (web) — mirrors the phone watch_reels behaviour ──
# Scrolls the Reels feed as the logged-in account and, per reel, probabilistically
# likes / saves / rewatches / reads comments / views the author / browses explore,
# with human timings. Action weights match the phone warmer (watch_reels.py).
_IG_WARMUP_ACTIONS = [
    ("none", 0.72), ("like", 0.07), ("rewatch", 0.06), ("explore_to_reels", 0.05),
    ("read_comments", 0.04), ("save", 0.03), ("browse_explore", 0.02), ("view_profile", 0.01),
]


def _wm_dwell(a, b):
    time.sleep(random.uniform(a, b))


def _wm_mouse_wander(page, moves=None, hum=None):
    """Human-like idle cursor movement across the reel area."""
    if hum is not None:
        try:
            hum.wander(moves)
            return
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        w, h = int(vp.get("width", 1280)), int(vp.get("height", 800))
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        w, h = 1280, 800
    for _ in range(moves or random.randint(1, 3)):
        try:
            x = random.randint(int(w * 0.22), int(w * 0.78))
            y = random.randint(int(h * 0.20), int(h * 0.80))
            page.mouse.move(x, y, steps=random.randint(8, 24))
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        _wm_dwell(0.4, 1.4)


def _wm_click_svg(page, labels, timeout_ms=1500, hum=None):
    for lab in labels:
        try:
            el = page.locator(f"svg[aria-label='{lab}' i]").first
            if int(el.count() or 0) > 0 and el.is_visible(timeout=timeout_ms):
                if hum is not None and hum.click(el):
                    return True
                el.click(timeout=2500)
                return True
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return False


def _wm_like(page, hum=None):
    try:
        vid = page.locator("video").first
        if int(vid.count() or 0) > 0 and vid.is_visible(timeout=1000):
            if hum is not None:
                try:
                    hum.move_to_element(vid)
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
            vid.dblclick(timeout=2500)
            return True
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return _wm_click_svg(page, ["Like"], hum=hum)


def _wm_next_reel(page, hum=None):
    # Move the cursor into the reel (human path) first, then either wheel-scroll
    # or press ArrowDown, like a real viewer. The cursor move always happens so
    # the mousemove stream stays alive between reels.
    try:
        if hum is not None:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            hum.move_to(random.uniform(vp["width"] * 0.4, vp["width"] * 0.62),
                        random.uniform(vp["height"] * 0.35, vp["height"] * 0.72),
                        overshoot=False)
        else:
            page.mouse.move(random.randint(480, 820), random.randint(300, 620), steps=random.randint(6, 16))
        _wm_dwell(0.2, 0.6)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    if random.random() < 0.55:
        try:
            page.mouse.wheel(0, random.randint(700, 1150))
            return
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
    try:
        page.keyboard.press("ArrowDown")
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        try:
            page.mouse.wheel(0, random.randint(700, 1000))
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass


def _wm_read_comments(page, hum=None):
    if _wm_click_svg(page, ["Comment"], hum=hum):
        for _ in range(random.randint(1, 3)):
            try:
                page.mouse.wheel(0, random.randint(250, 600))
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            _wm_dwell(1.2, 3.0)
        try:
            page.keyboard.press("Escape")
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass
        _wm_dwell(0.6, 1.4)


def _wm_view_profile(page, hum=None):
    try:
        link = page.locator("a[href^='/'][role='link']").first
        if int(link.count() or 0) > 0 and link.is_visible(timeout=1000):
            if not (hum is not None and hum.click(link)):
                link.click(timeout=2500)
            _wm_dwell(1.5, 3.0)
            for _ in range(random.randint(2, 4)):
                try:
                    page.mouse.wheel(0, random.randint(300, 700))
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
                _wm_dwell(1.0, 2.5)
            try:
                page.go_back(timeout=8000)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            _wm_dwell(1.0, 2.0)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _wm_browse_explore(page, back_to_reels):
    try:
        page.goto("https://www.instagram.com/explore/?hl=en", wait_until="domcontentloaded", timeout=45000)
        _wm_dwell(2.0, 4.0)
        for _ in range(random.randint(2, 5)):
            try:
                page.mouse.wheel(0, random.randint(400, 900))
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            _wm_dwell(1.5, 3.5)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    if back_to_reels:
        try:
            page.goto("https://www.instagram.com/reels/?hl=en", wait_until="domcontentloaded", timeout=45000)
            _wm_dwell(2.0, 4.0)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            pass


def warmup_actions(page, dump: LiveDump, minutes: float, account_name: str = ""):
    """Use the same Reels-first, verified HumanInteractor warmup as uploads.

    Keeping one implementation prevents the standalone Warm up account button
    and pre/post-upload warmups from drifting into different behaviour.
    """
    try:
        from instagram_web_upload import warmup_web as reels_first_warmup
        return reels_first_warmup(
            page, dump, float(minutes), mode="desktop", account=account_name
        )
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        dump.capture(page, "ig_warmup_error", error=str(exc), force_snapshot=True)
        return {"ok": False, "state": "failed", "error": str(exc)}


def do_warmup(account: dict, args, run_id: str):
    name = account["name"]
    dump = LiveDump(run_id, name)
    job = create_job(run_id, name, "cookie_warmup", str(dump.root))
    network_capture = None
    try:
        proxy = _proxy_for_account(account)
        if proxy and not bool(getattr(args, "skip_proxy_check", False)):
            ok, reason = _check_proxy_reachable(proxy)
            if not ok:
                update_account(name, web_upload_last_error=reason)
                update_job(job, status="manual_required", current_step="proxy_dead", last_error=reason, finished_at=now_iso())
                log(f"{name}: {reason}", "ERROR")
                return
            log(f"{name}: {reason}", "OK")
        with _browser_session(account, args) as (ctx_obj, ctx, page):
            try:
                if start_instagram_network_capture is not None:
                    network_capture = start_instagram_network_capture(
                        ctx, dump.root, account=name, run_id=run_id, phase="account_warmup"
                    )
                    if network_capture is not None:
                        log(f"{name}: Instagram GraphQL/private API capture active -> {dump.root / 'network'}", "OK")
                        dump.capture(page, "network_capture_active", "HAR + request/response/payload/JSON")
                res = warmup_actions(page, dump, float(args.minutes), account_name=name)
                if res.get("ok"):
                    update_account(name, web_upload_cookie_status="cookies_warm", web_upload_login_status="logged_in", web_upload_last_error="")
                    update_job(job, status="success", current_step="cookies_warm", finished_at=now_iso())
                else:
                    update_account(name, web_upload_cookie_status=res.get("state") or "failed", web_upload_last_error=res.get("error") or "")
                    update_job(job, status="manual_required", current_step=res.get("state") or "failed", last_error=res.get("error") or "", finished_at=now_iso())
            finally:
                try:
                    if network_capture is not None:
                        network_capture.stop()
                        log(f"{name}: network capture saved -> {dump.root / 'network'}", "OK")
                except Exception as exc:
                    logger.debug("%s: %s", type(exc).__name__, exc)
                    log(f"{name}: network capture finalize failed: {type(exc).__name__}", "WARNING")
            try:
                if getattr(args, "provider", "playwright") == "camoufox":
                    _save_camoufox_state(ctx, name)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            ctx.close()
            try:
                if 'ctx_obj' in locals() and ctx_obj and hasattr(ctx_obj, "__exit__"):
                    ctx_obj.__exit__(None, None, None)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        error = str(exc) or type(exc).__name__
        if _finish_proxy_failure(name, job, error):
            return
        update_account(name, web_upload_last_error=error)
        update_job(job, status="failed", current_step="crashed", last_error=error, finished_at=now_iso())
        raise


def do_post_story(account: dict, args, run_id: str):
    """Publish one Story through Instagram Web private API.

    The actual upload is executed inside the authenticated instagram.com page
    context by web_story_link.py:

      rupload_igphoto -> configure_to_story

    No Create Post composer and no mouse interaction are used for publishing.
    """
    name = account["name"]
    image_path = Path(str(getattr(args, "image", "") or "")).expanduser()

    dump = LiveDump(run_id, name)
    job = create_job(
        run_id,
        name,
        "post_story_private_api",
        str(dump.root),
        provider=getattr(args, "provider", "camoufox"),
    )
    network_capture = None

    # The current Story implementation supports only image publication through
    # the authenticated private-web API. Fail before browser launch and keep
    # local paths out of public job/account results.
    if not image_path.exists() or not image_path.is_file():
        update_job(job, status="failed", current_step="story_media_missing", last_error="Story media is unavailable", finished_at=now_iso())
        return
    if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        update_job(job, status="failed", current_step="story_media_unsupported", last_error="Story media type is unsupported", finished_at=now_iso())
        return

    try:
        from web_story_link import post_story_with_link

        proxy = _proxy_for_account(account)
        if proxy and not bool(getattr(args, "skip_proxy_check", False)):
            ok, reason = _check_proxy_reachable(proxy)
            if not ok:
                update_account(name, web_upload_last_error=reason)
                update_job(
                    job,
                    status="manual_required",
                    current_step="proxy_dead",
                    last_error=reason,
                    finished_at=now_iso(),
                )
                log(f"{name}: {reason}", "ERROR")
                return
            log(f"{name}: {reason}", "OK")

        with _browser_session(account, args) as (ctx_obj, ctx, page):
            try:
                if start_instagram_network_capture is not None:
                    network_capture = start_instagram_network_capture(
                        ctx,
                        dump.root,
                        account=name,
                        run_id=run_id,
                        phase="story_private_api",
                    )
                    if network_capture is not None:
                        log(
                            f"{name}: Story private API capture active -> "
                            f"{dump.root / 'network'}",
                            "OK",
                        )
            except Exception as exc:
                logger.debug("%s: %s", type(exc).__name__, exc)
                log(
                    f"{name}: Story network capture did not start: "
                    f"{type(exc).__name__}",
                    "WARNING",
                )

            try:
                page.goto(
                    "https://www.instagram.com/?hl=en",
                    wait_until="domcontentloaded",
                    timeout=90000,
                )
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass

            update_job(job, current_step="story_entry_opened")
            time.sleep(random.uniform(2.0, 4.0))
            dialog = continue_after_dialog(page, allow_safe_close=True)
            if str(dialog.get("state") or ""):
                update_job(job, status="manual_required", current_step="blocking_dialog_not_dismissed", last_error="blocking_dialog_not_dismissed", finished_at=now_iso())
                return
            _dismiss_instagram_consent(page, dump)
            state, reason = get_state(page)

            if state != "logged_in":
                dump.capture(
                    page,
                    "private_story_not_logged_in_" + state,
                    reason,
                    force_snapshot=True,
                )
                update_account(name, web_upload_last_error=reason)
                update_job(
                    job,
                    status="manual_required",
                    current_step=state,
                    last_error=reason,
                    finished_at=now_iso(),
                )
                return

            link_url = str(getattr(args, "link", "") or "").strip()
            sticker_text = str(
                getattr(args, "sticker_text", "") or "Chat with me👇🏻"
            ).strip()[:80]
            sticker_x = float(getattr(args, "sticker_x", 0.5) or 0.5)
            sticker_y = float(getattr(args, "sticker_y", 0.82) or 0.82)
            highlight_name = str(
                getattr(args, "highlight_name", "") or ""
            ).strip()

            log(
                f"{name}: publishing Story through private API "
                "(rupload_igphoto -> configure_to_story); "
                "no Create Post UI or mouse actions expected.",
                "OK",
            )
            dump.capture(
                page,
                "private_story_start",
                f"image={image_path.name}; link={bool(link_url)}",
                force_snapshot=True,
            )

            def persist_story_stage(stage: str) -> None:
                update_job(job, current_step=stage)
                dump.capture(page, stage, take_screenshot=False, take_visible_text=False)

            result = post_story_with_link(
                page=page,
                image_path=str(image_path),
                link_url=link_url,
                x=sticker_x,
                y=sticker_y,
                dump=dump,
                sticker_text=sticker_text,
                stage_callback=persist_story_stage,
            )

            # Retry on transport failure with backoff — rupload_transport
            # failures are often transient (network hiccup, proxy rotation).
            # Retry up to 2 times with 15-30s pause, human-like.
            retry_count = 0
            while not result.get("ok") and retry_count < 2:
                failed_step = str(result.get("step") or "")
                if "rupload" not in failed_step and "transport" not in failed_step:
                    break  # Not a transport error, don't retry
                retry_count += 1
                backoff = random.uniform(15, 30)
                log(f"{name}: Story transport failed ({failed_step}), retry {retry_count}/2 in {backoff:.0f}s", "WARNING")
                dump.capture(page, f"story_retry_{retry_count}", f"transport failure: {failed_step}; retrying in {backoff:.0f}s", force_snapshot=True)
                time.sleep(backoff)
                result = post_story_with_link(
                    page=page,
                    image_path=str(image_path),
                    link_url=link_url,
                    x=sticker_x,
                    y=sticker_y,
                    dump=dump,
                    sticker_text=sticker_text,
                    stage_callback=persist_story_stage,
                )

            if result.get("ok"):
                if highlight_name:
                    pending = (
                        f'Story posted through private API. Highlight '
                        f'"{highlight_name}" is not implemented by the supplied '
                        "web_story_link.py module yet."
                    )
                    update_account(name, web_upload_last_error=pending)
                    update_job(
                        job,
                        status="manual_required",
                        current_step="story_posted_highlight_pending",
                        last_error=pending,
                        finished_at=now_iso(),
                    )
                    dump.capture(
                        page,
                        "private_story_posted_highlight_pending",
                        json.dumps(result, ensure_ascii=False)[:900],
                        force_snapshot=True,
                    )
                else:
                    update_account(name, web_upload_last_error="")
                    update_job(
                        job,
                        status="success",
                        current_step="story_confirmed",
                        last_error="",
                        finished_at=now_iso(),
                    )
                    dump.capture(
                        page,
                        "private_story_posted",
                        json.dumps(result, ensure_ascii=False)[:900],
                        force_snapshot=True,
                    )
            else:
                step = str(result.get("step") or "failed")
                submitted = step == "configure"
                error = "Story API stage: " + step
                update_account(name, web_upload_last_error=error)
                update_job(
                    job,
                    status="submitted_unverified" if submitted else "failed",
                    current_step="story_submitted_unverified" if submitted else "story_private_api_" + step,
                    last_error=error,
                    finished_at=now_iso(),
                )
                dump.capture(
                    page,
                    "private_story_failed",
                    error,
                    force_snapshot=True,
                )

            try:
                if getattr(args, "provider", "playwright") == "camoufox":
                    _save_camoufox_state(ctx, name)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
            finally:
                try:
                    if network_capture is not None:
                        network_capture.stop()
                        log(
                            f"{name}: Story private API capture saved -> "
                            f"{dump.root / 'network'}",
                            "OK",
                        )
                except Exception as exc:
                    logger.debug("%s: %s", type(exc).__name__, exc)
                    log(
                        f"{name}: Story capture finalize failed: "
                        f"{type(exc).__name__}",
                        "WARNING",
                    )

            try:
                ctx.close()
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass

            try:
                if (
                    "ctx_obj" in locals()
                    and ctx_obj
                    and hasattr(ctx_obj, "__exit__")
                ):
                    ctx_obj.__exit__(None, None, None)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass

    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        error = str(exc) or type(exc).__name__
        if _finish_proxy_failure(name, job, error):
            return
        update_account(name, web_upload_last_error=error)
        update_job(
            job,
            status="failed",
            current_step="crashed",
            last_error=error,
            finished_at=now_iso(),
        )
        raise



def _settings_body(page) -> str:
    try:
        return (page.locator("body").inner_text(timeout=2500) or "")[:30000]
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return ""


def _settings_click(page, names, timeout_ms: int = 8000) -> str:
    """Click one exact semantic control and return the matched label."""
    for name in names:
        pattern = re.compile(r"^\s*" + re.escape(str(name)) + r"\s*$", re.I)
        candidates = [
            page.get_by_role("button", name=pattern),
            page.get_by_role("radio", name=pattern),
            page.get_by_role("link", name=pattern),
            page.get_by_text(pattern),
            page.locator("button").filter(has_text=pattern),
            page.locator("[role='button']").filter(has_text=pattern),
            page.locator("[role='link']").filter(has_text=pattern),
            page.locator("a").filter(has_text=pattern),
            page.locator("div").filter(has_text=pattern),
        ]
        for candidate in candidates:
            try:
                for index in range(min(int(candidate.count() or 0), 20)):
                    loc = candidate.nth(index)
                    if loc.is_visible(timeout=500):
                        loc.click(timeout=timeout_ms)
                        return str(name)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                continue
    return ""


def _settings_wait_text(page, values, timeout_seconds: float = 20.0) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        body = _settings_body(page).lower()
        for value in values:
            if str(value).lower() in body:
                return str(value)
        time.sleep(0.5)
    return ""


def _settings_guard(page) -> None:
    state, reason = get_state(page)
    if state in {"login_required", "two_factor_required", "checkpoint", "restricted", "suspended", "consent_required"}:
        raise RuntimeError(f"Instagram settings unavailable: {state} - {reason}")


def _sync_login_status_from_settings_error(name: str, error: str) -> None:
    lowered = str(error or "").lower()
    for state in ("login_required", "consent_required", "two_factor_required", "checkpoint", "restricted", "suspended"):
        if state in lowered:
            update_account(name, web_upload_login_status=state, web_upload_last_error=str(error))
            return


def _browser_account_state(page, username: str) -> dict:
    """Read authoritative account fields through the current browser session."""
    payload = page.evaluate(
        """
        async ({username}) => {
          const headers = {'X-IG-App-ID':'936619743392459','X-Requested-With':'XMLHttpRequest'};
          const current = await fetch('/api/v1/accounts/current_user/?edit=true', {credentials:'include',headers});
          let currentData = {}; try { currentData = await current.json(); } catch (_) {}
          const profile = await fetch('/api/v1/users/web_profile_info/?username='+encodeURIComponent(username), {credentials:'include',headers});
          let profileData = {}; try { profileData = await profile.json(); } catch (_) {}
          return {currentStatus: current.status, profileStatus: profile.status, currentData, profileData};
        }
        """,
        {"username": str(username or "")},
    )
    current = payload.get("currentData") if isinstance(payload, dict) else {}
    user = current.get("user", current) if isinstance(current, dict) else {}
    profile_data = payload.get("profileData") if isinstance(payload, dict) else {}
    profile_user = ((profile_data.get("data") or {}).get("user") if isinstance(profile_data, dict) else None)
    current_private = user.get("is_private") if isinstance(user, dict) else None
    profile_private = profile_user.get("is_private") if isinstance(profile_user, dict) else None
    if not isinstance(current_private, bool) or not isinstance(profile_private, bool) or current_private != profile_private:
        raise RuntimeError("authenticated privacy endpoints did not agree")
    professional = bool(user.get("is_professional_account"))
    account_type = int(user.get("account_type") or 0) if str(user.get("account_type") or "0").isdigit() else 0
    professional_type = "business" if professional and (bool(user.get("is_business")) or account_type == 2) else "creator" if professional else "personal"
    return {
        "privacy": "private" if current_private else "public",
        "professional": professional_type,
        "category": str(user.get("category") or user.get("category_name") or ""),
    }


def _private_switch(page):
    candidates = [
        page.get_by_role("switch", name=re.compile(r"private account", re.I)),
        page.locator("input[role='switch'][aria-label*='Private account' i]"),
        page.locator("[role='switch'][aria-label*='Private account' i]"),
        page.locator("input[type='checkbox'][aria-label*='Private account' i]"),
        page.locator("input[type='checkbox']"),
    ]
    for candidate in candidates:
        try:
            count = int(candidate.count() or 0)
            for index in range(min(count, 20)):
                loc = candidate.nth(index)
                try:
                    visible = loc.is_visible(timeout=2000)
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    visible = False
                if visible:
                    return loc
                # Element exists but may be visually hidden (CSS opacity/size).
                # Instagram hides the real <input> under a styled <div> overlay.
                # If attached and has role=switch or type=checkbox, return it anyway.
                try:
                    tag = loc.evaluate("el => el.tagName")
                    if tag and tag.lower() in ("input", "div", "button"):
                        return loc
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    pass
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return None


def _control_checked(control) -> bool:
    try:
        return bool(control.is_checked())
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        try:
            return str(control.get_attribute("aria-checked") or "").lower() == "true"
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            return False


def do_make_public(account: dict, args, run_id: str):
    name = account["name"]
    dump = LiveDump(run_id, name)
    job = create_job(run_id, name, "make_public", str(dump.root), getattr(args, "provider", "camoufox"))
    previous_privacy = str(account.get("web_privacy_status") or "unknown")
    conn = db_conn()
    try:
        row = conn.execute("SELECT COALESCE(web_privacy_status,'unknown') FROM accounts WHERE name=?", (name,)).fetchone()
        if row:
            previous_privacy = str(row[0] or "unknown")
    finally:
        conn.close()
    try:
        with _browser_session(account, args) as (_manager, context, page):
            update_job(job, current_step="open_account_privacy")
            page.goto("https://www.instagram.com/accounts/settings/v2/account_privacy/?hl=en",
                      wait_until="domcontentloaded", timeout=90000)
            _settings_wait_text(page, ["Account privacy", "Private account"], 25)
            dump.capture(page, "privacy_settings", "opened Account privacy", force_snapshot=True)
            _settings_guard(page)
            switch = _private_switch(page)
            if switch is None:
                raise RuntimeError("Account privacy switch was not found")
            if not _control_checked(switch):
                update_account(name, web_privacy_status="public", web_privacy_checked_at=now_iso(),
                               web_privacy_last_error="")
                update_job(job, status="success", current_step="already_public", finished_at=now_iso())
                return
            try:
                switch.click(timeout=8000)
            except Exception as click_exc:
                # Diagnosed 2026-08-11: Playwright's click() can report a
                # timeout even when the click's side effect (React state
                # change -> confirmation dialog) already happened. Instagram
                # replaces the toggle's DOM node right after the click, and
                # Playwright's own post-click stability check fails against
                # the now-detached old node even though the user-visible
                # action succeeded (confirmed via dump snapshots: the dialog
                # was visibly open right after a reported click timeout).
                # Retry once with force=True (skips the stability wait) —
                # matches the same pattern already used below for the
                # confirmation button — before giving up on this click.
                logger.debug(
                    "switch.click timed out (%s: %s), retrying with force=True",
                    type(click_exc).__name__, click_exc,
                )
                try:
                    switch.click(timeout=3000, force=True)
                except Exception as force_exc:
                    # Still couldn't force it through — don't abort yet.
                    # The dialog/toggle-state check right below already
                    # tells us definitively whether the click actually took
                    # effect, so let that be the real verdict instead of
                    # trusting Playwright's click() report alone.
                    logger.debug(
                        "force click also failed (%s: %s); falling through "
                        "to state check instead of aborting",
                        type(force_exc).__name__, force_exc,
                    )
            # After clicking the toggle, Instagram shows a confirmation
            # dialog "Switch to public account?" with a blue "Switch to
            # public" text button.  Playwright may refuse to click because
            # Instagram overlays the dialog with a semi-transparent layer.
            # Use force=True to bypass the visibility/intercept check.
            time.sleep(2.0)  # Let the dialog render.
            dump.capture(page, "privacy_public_confirmation", "after toggle click", force_snapshot=True)
            clicked = False
            for sel in (
                page.get_by_text(re.compile(r"^Switch to public$", re.I)),
                page.locator("button", has_text=re.compile(r"Switch to public", re.I)),
                page.locator("[role='button']", has_text=re.compile(r"Switch to public", re.I)),
                page.locator("a", has_text=re.compile(r"Switch to public", re.I)),
                page.locator("[role='link']", has_text=re.compile(r"Switch to public", re.I)),
                page.locator("div", has_text=re.compile(r"^Switch to public$", re.I)),
            ):
                try:
                    cnt = int(sel.count() or 0)
                    if cnt > 0:
                        # Try normal click first, then force click.
                        try:
                            sel.first.click(timeout=3000)
                        except Exception as _exc:
                            logger.debug("%s: %s", type(_exc).__name__, _exc)
                            sel.first.click(timeout=3000, force=True)
                        clicked = True
                        break
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    continue
            if not clicked:
                # Safe to try vision here specifically: by this point we've
                # already confirmed (via _control_checked before the toggle
                # click) that the account WAS private and we clicked to
                # switch it public — so clicking whatever vision identifies
                # as "Switch to public" is directionally unambiguous. This
                # is NOT extended to the earlier "switch itself not found"
                # case (see _private_switch above) — there we don't yet
                # know the current state, and a wrong vision click could
                # flip an already-public account back to private, which is
                # worse than just failing cleanly.
                vision_result = attempt_vision_click(
                    page, "the blue 'Switch to public' confirmation button in the dialog",
                )
                if vision_result.get("ok"):
                    clicked = True
            if not clicked:
                # Check if toggle already switched (no dialog needed).
                if not _control_checked(switch):
                    dump.capture(page, "privacy_public_no_dialog", "toggle switched without dialog", force_snapshot=True)
                else:
                    raise RuntimeError("Switch to public confirmation button was not found")
            time.sleep(2.0)
            page.reload(wait_until="domcontentloaded", timeout=90000)
            _settings_wait_text(page, ["Account privacy", "Private account"], 20)
            switch = _private_switch(page)
            if switch is None or _control_checked(switch):
                raise RuntimeError("Instagram did not confirm that the account is public")
            dump.capture(page, "privacy_public_verified", "private switch is off", force_snapshot=True)
            update_account(name, web_privacy_status="public", web_privacy_checked_at=now_iso(),
                           web_privacy_last_error="")
            update_job(job, status="success", current_step="public_verified", finished_at=now_iso())
            try:
                _save_camoufox_state(context, name)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        error = f"{type(exc).__name__}: {exc}"
        _sync_login_status_from_settings_error(name, error)
        update_account(name, web_privacy_status="public" if previous_privacy == "public" else "unknown", web_privacy_checked_at=now_iso(),
                       web_privacy_last_error=error)
        update_job(job, status="manual_required", current_step="make_public_failed",
                   last_error=error, finished_at=now_iso())
        log(f"{name}: make public stopped safely: {error}", "WARNING")


def do_auto_login_setup(account: dict, args, run_id: str):
    """Login one account, then immediately verify/publicize and professionalize it."""
    name = account["name"]
    do_auto_login(account, args, run_id)
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(web_upload_login_status,'') AS login_status FROM accounts WHERE name=?",
            (name,),
        ).fetchone()
        login_status = str(row["login_status"] if row else "")
    finally:
        conn.close()
    if login_status != "logged_in":
        log(f"{name}: post-login setup skipped because login status is {login_status or 'unknown'}", "WARNING")
        return
    if bool(getattr(args, "ensure_public", False)):
        # This screen is authoritative: it reads the real switch and changes it
        # only when it is currently on. It also updates privacy status to PUBLIC.
        do_make_public(account, args, run_id)
        conn = db_conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(web_privacy_status,'') AS privacy FROM accounts WHERE name=?",
                (name,),
            ).fetchone()
            privacy = str(row["privacy"] if row else "")
        finally:
            conn.close()
        if privacy != "public":
            log(f"{name}: professional conversion skipped because public status was not verified", "WARNING")
            return
    if bool(getattr(args, "convert_professional", False)):
        do_convert_professional(account, args, run_id)


def _category_checkbox(page):
    for candidate in [
        page.get_by_role("checkbox", name=re.compile(r"show category on profile", re.I)),
        page.locator("input[type='checkbox']"),
    ]:
        try:
            for index in range(min(int(candidate.count() or 0), 20)):
                loc = candidate.nth(index)
                if loc.is_visible(timeout=500):
                    return loc
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
    return None


def do_convert_professional(account: dict, args, run_id: str):
    name = account["name"]
    target_type = str(getattr(args, "professional_type", "creator") or "creator").lower()
    category = str(getattr(args, "professional_category", "Personal blog") or "Personal blog").strip()[:80]
    show_category = bool(getattr(args, "show_category", False))
    dump = LiveDump(run_id, name)
    # Professional profiles must be public. Verify through the saved API
    # session first; if that cannot prove PUBLIC (or proves PRIVATE), use the
    # authoritative settings switch. This rule applies to both onboarding and
    # a later manual Convert to Professional action.
    privacy = "unknown"
    try:
        from account_privacy import verify_account_state
        privacy = str(verify_account_state(name, rotate_mobile=False).get("privacy") or "unknown")
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        log(f"{name}: API privacy precheck unavailable; checking the settings switch: {exc}", "WARNING")
    if privacy != "public":
        do_make_public(account, args, run_id)
        conn = db_conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(web_privacy_status,'unknown') FROM accounts WHERE name=?",
                (name,),
            ).fetchone()
            privacy = str(row[0] if row else "unknown")
        finally:
            conn.close()
    job = create_job(run_id, name, "convert_professional", str(dump.root), getattr(args, "provider", "camoufox"))
    if privacy != "public":
        error = "professional conversion blocked: public account status was not verified"
        update_account(name, web_professional_status="unknown",
                       web_professional_checked_at=now_iso(), web_professional_last_error=error)
        update_job(job, status="manual_required", current_step="public_required",
                   last_error=error, finished_at=now_iso())
        log(f"{name}: {error}", "WARNING")
        return
    try:
        with _browser_session(account, args) as (_manager, context, page):
            update_job(job, current_step="open_professional_conversion")
            page.goto("https://www.instagram.com/accounts/convert_to_professional_account/?hl=en",
                      wait_until="domcontentloaded", timeout=90000)
            _settings_wait_text(page, ["Which best describes you?", "Select a category"], 25)
            dump.capture(page, "professional_start", "opened professional conversion", force_snapshot=True)
            _settings_guard(page)
            body = _settings_body(page).lower()
            if "professional account is ready" in body:
                update_account(name, web_professional_status=target_type,
                               web_professional_category=category,
                               web_professional_checked_at=now_iso(),
                               web_professional_last_error="", web_privacy_status="public",
                               web_privacy_checked_at=now_iso(), web_privacy_last_error="")
                update_job(job, status="success", current_step="already_professional", finished_at=now_iso())
                return
            choice = "Business" if target_type == "business" else "Creator"
            if "which best describes you" in body:
                if not _settings_click(page, [choice], 8000):
                    raise RuntimeError(f"{choice} account type option was not found")
                if not _settings_click(page, ["Next"], 8000):
                    raise RuntimeError("First Next button was not found")
                time.sleep(1.0)
                dump.capture(page, "professional_type_details", f"selected {choice}", force_snapshot=True)
                # Instagram shows one informational details screen after type selection.
                if _settings_wait_text(page, ["Next", "Select a category"], 15).lower() == "next":
                    if not _settings_click(page, ["Next"], 8000):
                        raise RuntimeError("Professional details Next button was not found")
            if not _settings_wait_text(page, ["Select a category"], 20):
                raise RuntimeError("Professional category screen did not appear")
            dump.capture(page, "professional_category", "category screen", force_snapshot=True)
            search = page.get_by_role("textbox").first
            try:
                search.fill(category, timeout=8000)
                time.sleep(1.0)
            except Exception as exc:
                logger.debug("%s: %s", type(exc).__name__, exc)
                dump.capture(page, "professional_category_search_failed", error=str(exc), force_snapshot=True)
            category_selected = _settings_click(page, [category], 8000)
            if not category_selected:
                # Some Instagram variants render an empty/virtualized list but
                # still support keyboard selection after a search.
                try:
                    search.press("ArrowDown", timeout=3000)
                    search.press("Enter", timeout=3000)
                    category_selected = True
                except Exception as _exc:
                    logger.debug("%s: %s", type(_exc).__name__, _exc)
                    category_selected = False
            if not category_selected:
                dump.capture(page, "professional_category_missing", f"requested category={category}", force_snapshot=True)
                raise RuntimeError(f"Professional category was not found: {category}")
            checkbox = _category_checkbox(page)
            if checkbox is not None and _control_checked(checkbox) != show_category:
                checkbox.click(timeout=6000)
            if not _settings_click(page, ["Done"], 8000):
                raise RuntimeError("Category Done button was not found")
            if not _settings_wait_text(page, ["Switch to a professional account?", "Continue"], 15):
                raise RuntimeError("Professional conversion confirmation did not appear")
            dump.capture(page, "professional_confirmation", "conversion confirmation", force_snapshot=True)
            if not _settings_click(page, ["Continue"], 10000):
                raise RuntimeError("Professional conversion Continue button was not found")
            ready = _settings_wait_text(page, [
                "Your Instagram creator account is ready",
                "Your Instagram business account is ready",
                "professional account is ready",
            ], 35)
            if not ready:
                dump.capture(page, "professional_success_not_visible", "waiting for API verification", force_snapshot=True)
                raise RuntimeError("Instagram did not show professional-account success")
            dump.capture(page, "professional_verified", ready, force_snapshot=True)
            _settings_click(page, ["Done"], 5000)
            verified_state = None
            verify_error = ""
            for attempt in range(3):
                try:
                    verified_state = _browser_account_state(page, name)
                    if str(verified_state.get("professional") or "") in {"creator", "business"}:
                        break
                except Exception as exc:
                    logger.debug("%s: %s", type(exc).__name__, exc)
                    verify_error = f"{type(exc).__name__}: {exc}"
                time.sleep(2.0 + attempt)
            actual_type = str((verified_state or {}).get("professional") or "")
            if actual_type not in {"creator", "business"}:
                raise RuntimeError(
                    "Instagram UI completed but authenticated API did not confirm a professional account"
                    + (f": {verify_error}" if verify_error else "")
                )
            update_account(name, web_professional_status=actual_type,
                           web_professional_category=str((verified_state or {}).get("category") or category),
                           web_professional_checked_at=now_iso(), web_professional_last_error="",
                           web_privacy_status="public", web_privacy_checked_at=now_iso(), web_privacy_last_error="")
            update_job(job, status="success", current_step="professional_verified", finished_at=now_iso())
            try:
                _save_camoufox_state(context, name)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
    except Exception as exc:
        logger.debug("%s: %s", type(exc).__name__, exc)
        error = f"{type(exc).__name__}: {exc}"
        _sync_login_status_from_settings_error(name, error)
        update_account(name, web_professional_status="unknown",
                       web_professional_checked_at=now_iso(), web_professional_last_error=error)
        update_job(job, status="manual_required", current_step="convert_professional_failed",
                   last_error=error, finished_at=now_iso())
        log(f"{name}: professional conversion stopped safely: {error}", "WARNING")


def do_create_profiles(accounts: List[dict], run_id: str):
    for acc in accounts:
        name = acc["name"]
        p = profile_dir(name, "desktop")
        p.mkdir(parents=True, exist_ok=True)
        dump = LiveDump(run_id, name)
        job = create_job(run_id, name, "create_profile", str(dump.root))
        try:
            fp = ensure_profile(acc)
            active = sparkbrowser_profile_dir(name, account_proxy(acc), "desktop") if sparkbrowser_profile_dir else p
            state = {
                "run_id": run_id,
                "account": safe_name(name),
                "state": "profile_created",
                "action": str(active),
                "error": "",
                "url": "",
                "host_os": fp.get("created_on_host_os"),
                "identity_os": fp.get("identity_os") or fp.get("identity_os_preference"),
                "location_policy": fp.get("location_policy"),
                "locale": fp.get("locale") or "en-US",
                "ts": now_iso(),
            }
            (dump.root / "latest_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            (dump.root / "latest_text.txt").write_text(json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8")
            update_job(job, status="success", current_step="profile_created", finished_at=now_iso())
            log(
                f"{name}: profile created at {active}; os={state['identity_os']}; "
                f"location={state['location_policy']}; locale=en-US",
                "OK",
            )
        except Exception as exc:
            logger.debug("%s: %s", type(exc).__name__, exc)
            update_account(name, web_upload_profile_status="profile_error", web_upload_last_error=str(exc))
            update_job(job, status="failed", current_step="profile_create_failed", last_error=str(exc), finished_at=now_iso())
            log(f"{name}: profile creation failed: {exc}", "ERROR")


def _run_accounts(fn, accounts: List[dict], args, run_id: str) -> None:
    workers = max(1, min(int(getattr(args, "max_workers", 1) or 1), len(accounts), 8))
    if workers <= 1:
        for acc in accounts:
            try:
                fn(acc, args, run_id)
            except Exception as exc:
                logger.debug("%s: %s", type(exc).__name__, exc)
                log(f"{acc.get('name', 'account')}: worker failed; continuing queue: {exc}", "ERROR")
            time.sleep(random.uniform(0.8, 2.2))
        return
    log(f"running {len(accounts)} accounts with max_workers={workers}", "OK")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for acc in accounts:
            futures.append(pool.submit(fn, acc, args, run_id))
            time.sleep(random.uniform(0.6, 1.8))
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                logger.debug("%s: %s", type(exc).__name__, exc)
                log(f"account worker crashed: {exc}", "ERROR")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["create_profiles", "check_login", "warmup", "open_profile", "auto_login", "auto_login_setup", "post_story", "make_public", "convert_professional"], required=True)
    ap.add_argument("--image", default="", help="Image path for post_story")
    ap.add_argument("--link", default="", help="Tappable link-sticker URL for post_story")
    ap.add_argument("--sticker-text", default="Chat with me👇🏻", help="Visible custom CTA text baked into the Story")
    ap.add_argument("--sticker-x", type=float, default=0.5)
    ap.add_argument("--sticker-y", type=float, default=0.82)
    ap.add_argument("--highlight-name", default="", help="Existing or new Instagram Web Highlight name")
    ap.add_argument("--accounts", default="")
    ap.add_argument("--minutes", type=float, default=8.0)
    ap.add_argument("--provider", choices=["playwright", "camoufox"], default="camoufox")
    ap.add_argument("--arrive", choices=["direct", "search"], default="direct",
                    help="How to reach instagram.com: 'search' arrives organically via a search result")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--no-proxy", action="store_true", help="Ignore saved account proxies for this local run")
    ap.add_argument("--skip-proxy-check", action="store_true", help="Do not run the quick proxy reachability check before login/check")
    ap.add_argument("--max-workers", type=int, default=1, help="Run this many accounts in parallel (each own browser). 1 = sequential.")
    ap.add_argument("--professional-type", choices=["creator", "business"], default="creator")
    ap.add_argument("--professional-category", default="Personal blog")
    ap.add_argument("--show-category", action="store_true")
    ap.add_argument("--ensure-public", action="store_true")
    ap.add_argument("--convert-professional", action="store_true")
    args = ap.parse_args()

    ensure_schema()
    accounts = get_accounts(normalise_accounts(args.accounts))
    _apply_scheduler_proxy_override(accounts)
    if args.no_proxy:
        for acc in accounts:
            acc["_no_proxy"] = True
    if not accounts:
        log("No accounts selected", "WARNING")
        return 2
    # The API acceptance boundary owns the run identity. Direct/offline
    # invocations retain a local fallback for backwards compatibility.
    run_id = str(
        os.environ.get("SPARKGRID_RUN_ID")
        or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    )
    if args.task == "create_profiles":
        do_create_profiles(accounts, run_id)
    elif args.task == "check_login":
        _run_accounts(do_check_login, accounts, args, run_id)
    elif args.task == "open_profile":
        args.keep_open = True
        for acc in accounts[:1]:
            do_check_login(acc, args, run_id)
    elif args.task == "auto_login":
        _run_accounts(do_auto_login, accounts, args, run_id)
    elif args.task == "auto_login_setup":
        args.max_workers = 1
        _run_accounts(do_auto_login_setup, accounts, args, run_id)
    elif args.task == "warmup":
        _run_accounts(do_warmup, accounts, args, run_id)
    elif args.task == "post_story":
        _run_accounts(do_post_story, accounts, args, run_id)
    elif args.task == "make_public":
        args.max_workers = 1
        _run_accounts(do_make_public, accounts, args, run_id)
    elif args.task == "convert_professional":
        args.max_workers = 1
        _run_accounts(do_convert_professional, accounts, args, run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
