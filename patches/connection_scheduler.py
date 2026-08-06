#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from connections import (
    account_connections,
    ensure_connection_schema,
    acquire_mobile_rotation_lease,
    mobile_rotation_lease_status,
    release_mobile_rotation_lease,
    renew_mobile_rotation_lease,
    quarantine_static_connection,
    rotate_connection,
)
from platform_runtime import process_group_kwargs, stop_process_tree
from publishing_history import job_has_reel_publish_intent, reconcile_terminal_upload_history
from proxy_telemetry import emit_proxy_telemetry
from disk_safety import DEFAULT_RESERVE_BYTES, preflight, retention
from lifecycle_recovery import LIFECYCLE_RESULTS, irreversible_stage, retry_safe
from password_ip_recovery import (
    begin_or_resume as begin_or_resume_password_recovery,
    get_active as get_active_password_recovery,
    mark_rotation_requested as mark_password_rotation_requested,
    mark_stage as mark_password_recovery_stage,
    mark_success as mark_password_recovery_success,
    mark_terminal as mark_password_recovery_terminal,
    record_first_rejection as record_password_rejection,
    update_initial_context as update_password_recovery_context,
)
from task_receipts import (
    current_run_id,
    mark_child as mark_task_child,
    opaque_account_ref,
    record_outcome as record_task_outcome,
    update_receipt as update_task_receipt,
)
from run_diagnostics import append_event as append_run_event

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"
_PRINT_LOCK = threading.Lock()
_EXIT_IP_LOCK = threading.Lock()
_USED_EXIT_IPS: dict[str, str] = {}
# ISSUE-017 only consumes stable worker/job classifications.  Arbitrary
# exception text, a page close, or a browser close is never enough to rotate a
# shared mobile endpoint.
MOBILE_RECOVERY_CODES = {
    "proxy_connection_failed", "proxy_transport_failed", "proxy_failed",
    "network_timeout", "main_frame_network_failed", "connection_reset",
    "tunnel_failed", "instagram_transport_unreachable_after_launch",
    "blank_document", "browser_proxy_application_failed",
}
MOBILE_RECOVERY_BLOCKED_STEPS = {
    "submitted_unverified", "confirmed", "share_clicked", "reel_publish_intent", "publish_intent", "publish_clicked",
    "story_share_clicked", "story_publish_clicked", "credentials_submitted",
    "otp_submitted", "two_factor_code_submitted",
}
_BROWSER_LAUNCH_LOCK = threading.Lock()
_LAST_BROWSER_LAUNCH = 0.0
MOBILE_ROTATION_LEASE_WAIT_SECONDS = 240.0
MOBILE_ROTATION_LEASE_POLL_SECONDS = 0.25
MOBILE_READINESS_PROBE_ATTEMPTS = 5
MOBILE_READINESS_BACKOFF_SECONDS = (0.0, 2.0, 4.0, 5.0, 5.0)
MOBILE_STALE_IP_CONFIRMATIONS = 2
MOBILE_MAX_ROTATION_REQUESTS = 2


class ProxyReadinessResult:
    """Two-value-compatible readiness result that also retains the observed IP."""

    def __init__(self, ok: bool, detail: str, exit_ip: str = "") -> None:
        self.ok = bool(ok)
        self.detail = str(detail or "")
        self.exit_ip = str(exit_ip or "")

    def __iter__(self):
        yield self.ok
        yield self.detail

def browser_storage_targets() -> list[Path]:
    """Every runtime location which can grow during a browser job."""
    return [
        DATA_DIR / "browser_profiles" / "ig_web_upload",
        DATA_DIR / "ai_content_data" / "debug" / "ig_web_upload",
        DATA_DIR / "browser_warmup_data" / "debug" / "web_warmup",
        DATA_DIR / "tmp", DATA_DIR / "bot.db",
    ]

def browser_disk_preflight() -> dict[str, Any]:
    # One non-blocking retention pass per scheduler process; retention itself
    # owns a process-local lock and never touches profiles/content/SQLite.
    for root in (DATA_DIR / "ai_content_data" / "debug" / "ig_web_upload", DATA_DIR / "browser_warmup_data" / "debug" / "web_warmup"):
        retention(root)
    return preflight(browser_storage_targets(), DEFAULT_RESERVE_BYTES)


def log(message: str, level: str = "INFO") -> None:
    from log_config import log_to_file_and_print
    log_to_file_and_print("automation", message, level)


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_exit_ip_history_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_exit_ip_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER NOT NULL DEFAULT 0,
            account_name TEXT NOT NULL COLLATE NOCASE,
            exit_ip TEXT NOT NULL,
            used_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proxy_exit_ip_recent "
        "ON proxy_exit_ip_history(exit_ip,used_at,id)"
    )
    # This is a recent-reuse guard, not a permanent blacklist.  Mobile
    # providers can legitimately return the same address again in the future.
    conn.execute("DELETE FROM proxy_exit_ip_history WHERE datetime(used_at)<datetime('now','-7 days')")
    conn.commit()


def persisted_exit_ip_owner(exit_ip: str, account: str, mobile: bool) -> str:
    conn = db_conn()
    try:
        ensure_exit_ip_history_schema(conn)
        if mobile:
            # A mobile endpoint must not give the next browser any IP used in
            # the recent automation/login/upload window, even for the same
            # account in a later scheduler process.
            row = conn.execute(
                """
                SELECT account_name FROM proxy_exit_ip_history
                WHERE exit_ip=? AND datetime(used_at)>=datetime('now','-24 hours')
                ORDER BY id DESC LIMIT 1
                """,
                (str(exit_ip or ""),),
            ).fetchone()
        else:
            # A static IP may be reused by its dedicated account, but never by
            # another account in the recent queue.
            row = conn.execute(
                """
                SELECT account_name FROM proxy_exit_ip_history
                WHERE exit_ip=? AND account_name<>? COLLATE NOCASE
                  AND datetime(used_at)>=datetime('now','-24 hours')
                ORDER BY id DESC LIMIT 1
                """,
                (str(exit_ip or ""), str(account or "")),
            ).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def persist_exit_ip(connection_id: int, account: str, exit_ip: str) -> None:
    conn = db_conn()
    try:
        ensure_exit_ip_history_schema(conn)
        conn.execute(
            "INSERT INTO proxy_exit_ip_history(connection_id,account_name,exit_ip) VALUES(?,?,?)",
            (int(connection_id or 0), str(account or ""), str(exit_ip or "")),
        )
        if int(connection_id or 0):
            conn.execute(
                "UPDATE web_connections SET last_status='healthy',last_error='',last_ip=?,last_checked_at=datetime('now'),updated_at=datetime('now') WHERE id=?",
                (str(exit_ip or ""), int(connection_id)),
            )
        conn.commit()
    finally:
        conn.close()


def reserve_persisted_exit_ip(
    connection_id: int,
    account: str,
    exit_ip: str,
    mobile: bool,
) -> tuple[bool, str]:
    """Atomically check and reserve an exit IP across scheduler processes."""
    conn = db_conn()
    try:
        ensure_exit_ip_history_schema(conn)
        # BEGIN IMMEDIATE serializes the read + insert pair. Without it, two
        # independent top-level schedulers could both observe an unused IP
        # before either inserted its reservation.
        conn.execute("BEGIN IMMEDIATE")
        if mobile:
            row = conn.execute(
                """
                SELECT account_name FROM proxy_exit_ip_history
                WHERE exit_ip=? AND datetime(used_at)>=datetime('now','-24 hours')
                ORDER BY id DESC LIMIT 1
                """,
                (str(exit_ip or ""),),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT account_name FROM proxy_exit_ip_history
                WHERE exit_ip=? AND account_name<>? COLLATE NOCASE
                  AND datetime(used_at)>=datetime('now','-24 hours')
                ORDER BY id DESC LIMIT 1
                """,
                (str(exit_ip or ""), str(account or "")),
            ).fetchone()
        if row:
            owner = str(row[0] or "")
            conn.rollback()
            return False, owner
        conn.execute(
            "INSERT INTO proxy_exit_ip_history(connection_id,account_name,exit_ip) VALUES(?,?,?)",
            (int(connection_id or 0), str(account or ""), str(exit_ip or "")),
        )
        if int(connection_id or 0):
            conn.execute(
                "UPDATE web_connections SET last_status='healthy',last_error='',last_ip=?,"
                "last_checked_at=datetime('now'),updated_at=datetime('now') WHERE id=?",
                (str(exit_ip or ""), int(connection_id)),
            )
        conn.commit()
        return True, ""
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_names(raw: str) -> list[str]:
    result: list[str] = []
    for token in str(raw or "").replace("\n", ",").split(","):
        name = token.strip().lstrip("@")
        if name and name not in result:
            result.append(name)
    return result


def story_snapshot_names(
    requested_names: list[str],
    selection: dict[str, Any],
) -> list[str]:
    """Intersect requested names with the immutable Story job snapshot."""
    saved_names = parse_names(",".join(selection.get("final_account_names") or []))
    saved = {name.lower() for name in saved_names}
    return [name for name in requested_names if name.lower() in saved]


def story_ready_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Story login readiness is subtractive and never queries extra accounts."""
    return [
        account
        for account in accounts
        if str(account.get("web_upload_login_status") or "").strip().lower() == "logged_in"
    ]


def lane_key(account: dict[str, Any], no_proxy: bool) -> str:
    if no_proxy:
        return "direct:forced"
    cid = int(account.get("web_connection_id") or 0)
    ctype = str(account.get("connection_type") or "direct")
    return f"{ctype}:{cid}"


def base_command(args: argparse.Namespace, account_data: dict[str, Any]) -> list[str]:
    account = str(account_data["name"])
    if args.operation == "api":
        command = [
            sys.executable, "-u", str(ROOT / "instagram_private_web_api_upload.py"),
            "--accounts", account,
            "--parallel", "1",
            "--provider", args.provider,
            "--caption", args.caption,
            "--session-headless",
        ]
        if args.ignore_cooldown:
            command.append("--ignore-cooldown")
        if args.asset_id:
            command += ["--asset-id", str(args.asset_id)]
        if args.history_id:
            command += ["--history-id", str(args.history_id)]
    elif args.operation == "clean_web":
        command = [
            sys.executable, "-u", str(ROOT / "instagram_web_upload.py"),
            "--accounts", account,
            "--target", str(args.target),
            "--mode", "desktop",
            "--provider", args.provider,
            "--pre-warmup-min", str(args.pre_warmup_min),
            "--pre-warmup-max", str(args.pre_warmup_max),
            "--post-warmup-min", str(args.post_warmup_min),
            "--post-warmup-max", str(args.post_warmup_max),
            "--cooldown-hours", str(args.cooldown_hours),
            "--caption", args.caption,
            "--max-workers", "1",
        ]
        if args.headless:
            command.append("--headless")
        if args.ignore_cooldown:
            command.append("--ignore-cooldown")
        if args.asset_id:
            command += ["--asset-id", str(args.asset_id)]
        if args.history_id:
            command += ["--history-id", str(args.history_id)]
    elif args.operation == "workflow":
        command = [
            sys.executable, "-u", str(ROOT / "instagram_web_profile_workflow.py"),
            "--task", args.task,
            "--accounts", account,
            "--minutes", str(args.minutes),
            "--provider", args.provider,
            "--max-workers", "1",
        ]
        if args.task in {"open_profile", "check_login", "auto_login", "auto_login_setup"}:
            command += ["--arrive", args.arrive]
        if args.task in {"convert_professional", "auto_login_setup"}:
            command += ["--professional-type", args.professional_type,
                        "--professional-category", args.professional_category]
            if args.ensure_public:
                command.append("--ensure-public")
            if args.convert_professional:
                command.append("--convert-professional")
            if args.show_category:
                command.append("--show-category")
        if args.headless:
            command.append("--headless")
        if args.skip_proxy_check:
            command.append("--skip-proxy-check")
        if args.task == "open_profile":
            command.append("--keep-open")
    elif args.operation == "web_warmup":
        command = [
            sys.executable, "-u", str(ROOT / "web_warmup.py"),
            "--minutes", str(args.minutes),
            "--profile", account,
            "--persona", args.persona,
            "--mode", "desktop",
            "--profile-root", str(DATA_DIR / "browser_profiles" / "ig_web_upload"),
            "--mark-db",
        ]
        proxy = str(account_data.get("proxy_url") or "").strip()
        if proxy and not args.no_proxy:
            command += ["--proxy", proxy]
        if args.headless:
            command.append("--headless")
    elif args.operation == "analytics_session":
        command = [
            sys.executable, "-u", str(ROOT / "view_analytics.py"),
            "--session-account", account,
        ]
        if args.target_ids:
            command += ["--target-ids", str(args.target_ids)]
    elif args.operation == "story":
        story = dict(account_data.get("story") or {})
        command = [
            sys.executable, "-u", str(ROOT / "instagram_web_profile_workflow.py"),
            "--task", "post_story",
            "--accounts", account,
            "--provider", args.provider,
            "--max-workers", "1",
            "--image", str(story.get("image") or args.image),
            "--link", str(story.get("link") or args.link),
            "--sticker-text", str(story.get("sticker_text") or args.sticker_text),
            "--sticker-x", str(story.get("sticker_x") if story.get("sticker_x") is not None else args.sticker_x),
            "--sticker-y", str(story.get("sticker_y") if story.get("sticker_y") is not None else args.sticker_y),
            "--highlight-name", str(story.get("highlight_name") or args.highlight_name),
        ]
        if args.headless:
            command.append("--headless")
    else:
        raise ValueError(f"Unsupported operation: {args.operation}")
    if args.no_proxy:
        command.append("--no-proxy")
    return command


def probe_proxy_exit_ip(
    proxy_url: str,
    timeout: float = 15.0,
    *,
    checker: str = "",
) -> tuple[bool, str, str]:
    """Verify the real public exit through a neutral service, never Instagram."""
    value = str(proxy_url or "").strip()
    if not value:
        return False, "proxy endpoint is missing", ""
    try:
        scheme = str(urllib.parse.urlparse(value).scheme or "http").lower()
    except Exception:
        scheme = "http"
    if scheme in {"socks4", "socks5", "socks5h"}:
        try:
            import requests
            errors = []
            endpoints = (
                ("https://api.ipify.org?format=json", True),
                ("https://icanhazip.com/", False),
            )
            if checker:
                endpoints = tuple(item for item in endpoints if item[0] == checker)
            for endpoint, is_json in endpoints:
                try:
                    response = requests.get(
                        endpoint,
                        proxies={"http": value, "https": value},
                        headers={"User-Agent": "SparkGrid-Proxy-Gate/2.0"},
                        timeout=max(2.0, float(timeout)),
                    )
                    if int(response.status_code) != 200:
                        errors.append(f"{endpoint}: HTTP {response.status_code}")
                        continue
                    payload = response.text.strip()
                    candidate = str(response.json().get("ip") or "").strip() if is_json else payload.splitlines()[0].strip()
                    ipaddress.ip_address(candidate)
                    return True, f"proxy connected; exit IP {candidate}", candidate
                except Exception as exc:
                    errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
            return False, "SOCKS proxy exit-IP check failed: " + " | ".join(errors[-2:]), ""
        except Exception as exc:
            return False, f"SOCKS proxy verifier unavailable: {exc}", ""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": value, "https": value})
    )
    errors = []
    endpoints = (
        ("https://api.ipify.org?format=json", True),
        ("https://icanhazip.com/", False),
    )
    if checker:
        endpoints = tuple(item for item in endpoints if item[0] == checker)
    for endpoint, is_json in endpoints:
        try:
            request = urllib.request.Request(
                endpoint,
                headers={"User-Agent": "SparkGrid-Proxy-Gate/2.0", "Accept": "application/json,text/plain"},
            )
            with opener.open(request, timeout=max(2.0, float(timeout))) as response:
                status = int(getattr(response, "status", 0) or response.getcode() or 0)
                payload = response.read(512).decode("utf-8", errors="replace").strip()
                if status != 200:
                    errors.append(f"{endpoint}: HTTP {status}")
                    continue
                candidate = str(json.loads(payload).get("ip") or "").strip() if is_json else payload.splitlines()[0].strip()
                ipaddress.ip_address(candidate)
                return True, f"proxy connected; exit IP {candidate}", candidate
        except Exception as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
    return False, "proxy exit-IP check failed: " + " | ".join(errors[-2:]), ""


def _recovery_exit_ip_probe(
    proxy_url: str, checker: str
) -> tuple[bool, str, str]:
    """Use one checker throughout a recovery sequence.

    The TypeError fallback keeps offline monkeypatches compatible while
    production always uses the explicitly selected endpoint.
    """
    try:
        return probe_proxy_exit_ip(proxy_url, checker=checker)
    except TypeError:
        return probe_proxy_exit_ip(proxy_url)


def verify_proxy_after_rotation(proxy_url: str, timeout: float = 8.0) -> ProxyReadinessResult:
    ok, detail, exit_ip = probe_proxy_exit_ip(proxy_url, timeout=timeout)
    return ProxyReadinessResult(ok, detail, exit_ip)


def _proxy_probe_classification(ok: bool, detail: str, exit_ip: str) -> str:
    """Classify a secret-free proxy probe without changing its tuple API."""
    text = str(detail or "").lower()
    if any(token in text for token in (
        "407", "proxy authentication", "proxy auth", "authentication required",
        "invalid proxy credentials", "socks5 authentication",
    )):
        return "proxy_auth_failed"
    if exit_ip:
        return "ip_observed"
    if any(token in text for token in (
        "http 500", "http 502", "http 503", "http 504", "empty",
        "jsondecodeerror", "invalid ip", "does not appear to be an ipv",
    )):
        return "connectivity_confirmed_ip_unavailable"
    return "transient_connection_failure"


def _static_proxy_failure_outcome(detail: str) -> str:
    """Map private probe detail to the existing public static-proxy taxonomy."""
    text = str(detail or "").lower()
    if any(token in text for token in (
        "407", "proxy authentication", "proxy auth", "authentication required",
        "invalid proxy credentials", "socks5 authentication",
    )):
        return "proxy_auth_failed"
    if any(token in text for token in (
        "timed out", "timeout", "timeouterror", "read operation timed out",
    )):
        return "proxy_connection_timeout"
    if any(token in text for token in (
        "name or service not known", "temporary failure in name resolution",
        "nodename nor servname", "getaddrinfo", "gaierror", "dns",
        "network is unreachable", "no route to host", "connection refused",
        "actively refused", "proxy unreachable",
    )):
        return "proxy_unreachable"
    if any(token in text for token in (
        "http 500", "http 502", "http 503", "http 504", "empty",
        "jsondecodeerror", "invalid ip", "does not appear to be an ipv",
    )):
        return "proxy_readiness_timeout"
    return "proxy_connection_failed"


def _connection_ref(run_id: str, connection_id: int) -> str:
    return "connection-" + hashlib.sha256(
        (str(run_id) + "\0" + str(int(connection_id or 0))).encode("utf-8")
    ).hexdigest()[:16]


def _record_static_proxy_gate_outcome(
    args: argparse.Namespace,
    account: dict[str, Any],
    outcome: str,
) -> int:
    """Close the real account attempt when its worker was blocked by the gate."""
    name = str(account.get("name") or "")
    run_id = str(
        getattr(args, "workflow_run_id", "") or current_run_id() or uuid.uuid4().hex
    )
    setattr(args, "workflow_run_id", run_id)
    code = str(outcome or "proxy_connection_failed")
    conn = db_conn()
    try:
        row = conn.execute(
            """
            SELECT id FROM ig_web_upload_jobs
            WHERE run_id=? AND account_name=? ORDER BY id DESC LIMIT 1
            """,
            (run_id, name),
        ).fetchone()
        if row:
            job_id = int(row[0])
            worker_not_started = False
            conn.execute(
                """
                UPDATE ig_web_upload_jobs SET
                    status='failed',current_step=?,last_error=?,
                    domain_outcome=?,
                    closure_owner='connection_scheduler',
                    closure_reason='proxy_gate_failed',
                    finished_at=datetime('now'),updated_at=datetime('now')
                WHERE id=?
                """,
                (code, code, code, job_id),
            )
        else:
            worker_not_started = True
            cursor = conn.execute(
                """
                INSERT INTO ig_web_upload_jobs(
                    run_id,account_name,mode,provider,status,target_uploads,
                    current_step,last_error,domain_outcome,infrastructure_outcome,
                    closure_owner,closure_reason,started_at,finished_at,updated_at
                ) VALUES(?,?,?,?,'failed',0,?,?,?,'worker_not_started',
                         'connection_scheduler','proxy_gate_failed',
                         datetime('now'),datetime('now'),datetime('now'))
                """,
                (
                    run_id, name, str(getattr(args, "operation", "workflow")),
                    str(getattr(args, "provider", "camoufox")), code, code, code,
                ),
            )
            job_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()
    record_task_outcome(
        run_id,
        domain_outcome=code,
        infrastructure_outcome=("worker_not_started" if worker_not_started else ""),
        connection_state="unavailable",
        scheduler_state="prelaunch_failed",
        closure_owner="connection_scheduler",
        closure_reason="proxy_gate_failed",
    )
    event_fields = {
        "account_ref": opaque_account_ref(run_id, name),
        "connection_ref": _connection_ref(
            run_id, int(account.get("web_connection_id") or 0)
        ),
    }
    try:
        append_run_event(
            run_id, "domain_outcome", stream="outcomes",
            domain_outcome=code, **event_fields,
        )
        if worker_not_started:
            append_run_event(
                run_id, "infrastructure_outcome", stream="outcomes",
                infrastructure_outcome="worker_not_started", **event_fields,
            )
        append_run_event(
            run_id, "closure", stream="outcomes",
            closure_owner="connection_scheduler",
            closure_reason="proxy_gate_failed", **event_fields,
        )
    except Exception:
        pass
    return job_id


def probe_instagram_transport(proxy_url: str, timeout: float = 12.0) -> tuple[bool, str]:
    """Best-effort proxy -> Instagram transport hint.

    The neutral exit-IP probe is the authoritative launch gate. Mobile proxy
    providers can successfully carry a full browser while Python's standalone
    urllib/OpenSSL request times out during Instagram TLS negotiation. Treat
    that mismatch as inconclusive so a verified proxy is not rejected before
    the browser has a chance to use it. An explicit proxy-authentication
    response remains a hard failure.
    """
    value = str(proxy_url or "").strip()
    endpoint = "https://www.instagram.com/robots.txt"
    try:
        scheme = str(urllib.parse.urlparse(value).scheme or "http").lower()
    except Exception:
        scheme = "http"
    try:
        if scheme in {"socks4", "socks5", "socks5h"}:
            import requests
            response = requests.get(
                endpoint,
                proxies={"http": value, "https": value},
                headers={"User-Agent": "SparkGrid-Proxy-Gate/2.1", "Accept": "*/*"},
                timeout=max(2.0, float(timeout)),
                allow_redirects=False,
                stream=True,
            )
            status = int(response.status_code)
            response.close()
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": value, "https": value}))
            request = urllib.request.Request(endpoint, headers={"User-Agent": "SparkGrid-Proxy-Gate/2.1", "Accept": "*/*"})
            try:
                with opener.open(request, timeout=max(2.0, float(timeout))) as response:
                    status = int(getattr(response, "status", 0) or response.getcode() or 0)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
        # Login walls, challenges, rate limits and access denials still prove
        # that TLS reached Instagram. Authentication is checked later inside
        # the account's own browser session.
        if status == 407:
            return False, f"Instagram transport returned HTTP {status}"
        if status <= 0:
            return True, f"Instagram transport check inconclusive (HTTP {status}); continuing after verified exit IP"
        return True, f"Instagram transport reached HTTP {status}"
    except Exception as exc:
        return True, (
            "Instagram transport check inconclusive; continuing after verified exit IP: "
            f"{type(exc).__name__}: {exc}"
        )


def used_exit_ip_owner(exit_ip: str) -> str:
    with _EXIT_IP_LOCK:
        return str(_USED_EXIT_IPS.get(str(exit_ip or "")) or "")


def reserve_exit_ip(exit_ip: str, account: str) -> tuple[bool, str]:
    with _EXIT_IP_LOCK:
        owner = str(_USED_EXIT_IPS.get(exit_ip) or "")
        if owner and owner != account:
            return False, owner
        _USED_EXIT_IPS[exit_ip] = account
        return True, owner


def durable_mobile_rotation(
    account: dict[str, Any],
    readiness: Any,
    *,
    sleep_after: bool = True,
    lease_seconds: float = 180.0,
    wait_seconds: float = MOBILE_ROTATION_LEASE_WAIT_SECONDS,
    generation: str = "",
    stage_callback: Any = None,
    lease_owner: str = "",
) -> dict[str, Any]:
    """Run one mobile rotation cycle under a SQLite lease.

    The lease is held from provider request through its cooldown/wait and the
    readiness callback. A follower only consumes the owner terminal outcome;
    it never sends its own duplicate request while it was waiting.
    """
    connection_id = int(account.get("web_connection_id") or 0)
    proxy_url = str(account.get("proxy_url") or "")
    ctype = str(account.get("connection_type") or "")
    if connection_id <= 0 or ctype not in {"mobile", "phone"}:
        return {"ok": False, "error": "mobile connection id is missing", "outcome": "rotation_lock_timeout"}
    owner_id = str(lease_owner or uuid.uuid4().hex)
    generation = str(generation or uuid.uuid4().hex)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    waited = False
    while True:
        # Once this invocation has observed an active owner, consume that
        # owner's terminal outcome before attempting a fresh acquisition.
        # This closes the release/acquire race that would otherwise produce a
        # duplicate rotation immediately after a successful owner release.
        if waited:
            conn = db_conn()
            try:
                status = mobile_rotation_lease_status(conn, connection_id)
            finally:
                conn.close()
            if status.get("state") == "terminal" and str(status.get("generation") or "") == generation:
                outcome = str(status.get("outcome") or "rotation_failed")
                reused = outcome == "rotation_verified"
                emit_proxy_telemetry(proxy_url, proxy_type=ctype, phase="rotation_lock", normalized_result="rotation_lock_reused_result",
                                     provider_state="not_applicable", final_classification="rotation_lock_reused_result")
                return {"ok": reused, "error": "" if reused else outcome, "outcome": outcome, "lock_outcome": "rotation_lock_reused_result", "reused": True}
        conn = db_conn()
        try:
            # Only followers in this logical rotation generation consume its
            # terminal result. A new explicit workflow generation must replace
            # an older terminal row and send a fresh provider request.
            status = mobile_rotation_lease_status(conn, connection_id)
            if status.get("state") == "terminal" and str(status.get("generation") or "") == generation:
                outcome = str(status.get("outcome") or "rotation_failed")
                reused = outcome == "rotation_verified"
                return {"ok": reused, "error": "" if reused else outcome, "outcome": outcome, "lock_outcome": "rotation_lock_reused_result", "reused": True}
            acquired = acquire_mobile_rotation_lease(
                conn, connection_id, owner_id, lease_seconds, generation=generation,
            )
        finally:
            conn.close()
        if acquired["acquired"]:
            lock_outcome = "rotation_lock_stale_recovered" if acquired["stale_recovered"] else "rotation_lock_acquired"
            emit_proxy_telemetry(proxy_url, proxy_type=ctype, phase="rotation_lock", normalized_result=lock_outcome,
                                 provider_state="not_applicable", final_classification=lock_outcome)
            break
        waited = True
        emit_proxy_telemetry(proxy_url, proxy_type=ctype, phase="rotation_lock", normalized_result="rotation_lock_waiting",
                             provider_state="not_applicable", final_classification="rotation_lock_waiting")
        conn = db_conn()
        try:
            status = mobile_rotation_lease_status(conn, connection_id)
        finally:
            conn.close()
        if status.get("state") == "terminal" and str(status.get("generation") or "") == generation:
            outcome = str(status.get("outcome") or "rotation_failed")
            reused = outcome == "rotation_verified"
            emit_proxy_telemetry(proxy_url, proxy_type=ctype, phase="rotation_lock", normalized_result="rotation_lock_reused_result",
                                 provider_state="not_applicable", final_classification="rotation_lock_reused_result")
            return {"ok": reused, "error": "" if reused else outcome, "outcome": outcome, "lock_outcome": "rotation_lock_reused_result", "reused": True}
        if time.monotonic() >= deadline:
            emit_proxy_telemetry(proxy_url, proxy_type=ctype, phase="rotation_lock", normalized_result="rotation_lock_timeout",
                                 provider_state="not_applicable", final_classification="rotation_lock_timeout")
            return {"ok": False, "error": "mobile rotation lease is still active", "outcome": "rotation_lock_timeout"}
        time.sleep(MOBILE_ROTATION_LEASE_POLL_SECONDS)

    stopped = threading.Event()

    def heartbeat() -> None:
        interval = max(0.1, float(lease_seconds) / 3.0)
        while not stopped.wait(interval):
            heartbeat_conn = db_conn()
            try:
                if not renew_mobile_rotation_lease(heartbeat_conn, connection_id, owner_id, lease_seconds):
                    return
            finally:
                heartbeat_conn.close()

    thread = threading.Thread(target=heartbeat, name="mobile-rotation-lease", daemon=True)
    thread.start()
    terminal = "rotation_request_failed"
    try:
        pre_ok, pre_detail, pre_rotation_ip = probe_proxy_exit_ip(
            proxy_url, timeout=2.0
        )
        if pre_ok and pre_rotation_ip:
            pre_conn = db_conn()
            try:
                pre_conn.execute(
                    "UPDATE web_connections SET last_ip=?,last_checked_at=datetime('now'),updated_at=datetime('now') WHERE id=?",
                    (pre_rotation_ip, connection_id),
                )
                pre_conn.commit()
            except sqlite3.Error:
                pre_conn.rollback()
            finally:
                pre_conn.close()
        if _proxy_probe_classification(pre_ok, pre_detail, pre_rotation_ip) == "proxy_auth_failed":
            terminal = "proxy_auth_failed"
            return {
                "ok": False, "error": terminal, "outcome": terminal,
                "rotation_requests": 0, "pre_rotation_ip": "",
            }

        rotation_requests = 0
        last_detail = ""
        connectivity_confirmed = False
        for series in range(MOBILE_MAX_ROTATION_REQUESTS):
            rotation_conn = db_conn()
            try:
                # Grace is owned here so readiness is never skipped when an
                # older call site passes sleep_after=False.
                result = rotate_connection(
                    rotation_conn, connection_id, sleep_after=False
                )
            finally:
                rotation_conn.close()
            rotation_requests += int(result.get("rotation_requests") or 1)
            if not result.get("ok"):
                terminal = str(result.get("outcome") or "rotation_request_failed")
                wait = min(30.0, max(0.0, float(result.get("provider_wait_seconds") or 0)))
                if wait:
                    time.sleep(wait)
                return {
                    "ok": False, "error": terminal, "outcome": terminal,
                    "rotation_requests": rotation_requests,
                    "retryable": terminal in {
                        "rotation_endpoint_timeout", "rotation_endpoint_connection_failure",
                        "rotation_endpoint_rate_limited", "rotation_endpoint_busy",
                    },
                }

            provider_state = str(result.get("provider_state") or "unknown")
            terminal = "rotation_request_accepted"
            if stage_callback is not None:
                stage_callback("ROTATION_COMMAND_ACCEPTED")
                stage_callback("ROTATION_COOLDOWN")
                stage_callback("ROTATION_STABILIZING")
            grace = min(120.0, max(0.0, float(result.get("provider_wait_seconds") or 0)))
            if grace:
                time.sleep(grace)

            stale_confirmations = 0
            stale_confirmed = False
            for probe_index in range(MOBILE_READINESS_PROBE_ATTEMPTS):
                delay = MOBILE_READINESS_BACKOFF_SECONDS[
                    min(probe_index, len(MOBILE_READINESS_BACKOFF_SECONDS) - 1)
                ]
                if delay:
                    time.sleep(delay)
                callback_result = readiness()
                try:
                    ready_hint, readiness_detail = callback_result
                except ValueError:
                    ready_hint, readiness_detail, callback_ip = callback_result
                else:
                    callback_ip = str(
                        getattr(callback_result, "exit_ip", "") or ""
                    )
                ok = bool(ready_hint)
                detail = str(readiness_detail or "")
                candidate_ip = callback_ip
                last_detail = str(detail or "")
                classification = _proxy_probe_classification(ok, detail, candidate_ip)
                if classification == "proxy_auth_failed":
                    terminal = "proxy_auth_failed"
                    return {
                        "ok": False, "error": terminal, "outcome": terminal,
                        "rotation_requests": rotation_requests, "retryable": False,
                    }
                if classification == "connectivity_confirmed_ip_unavailable":
                    connectivity_confirmed = True
                    stale_confirmations = 0
                    continue
                if classification != "ip_observed":
                    if ok and not pre_rotation_ip and not candidate_ip:
                        terminal = "rotation_verified"
                        if stage_callback is not None:
                            stage_callback("PROXY_READINESS_CONFIRMED")
                        return {
                            "ok": True, "error": "", "detail": last_detail,
                            "outcome": terminal, "lock_outcome": lock_outcome,
                            "reused": False, "provider_state": provider_state,
                            "generation": generation,
                            "rotation_requests": rotation_requests,
                            "pre_rotation_ip": "", "exit_ip": "",
                        }
                    stale_confirmations = 0
                    continue

                connectivity_confirmed = True
                if pre_rotation_ip and candidate_ip == pre_rotation_ip:
                    stale_confirmations += 1
                    if stale_confirmations < MOBILE_STALE_IP_CONFIRMATIONS:
                        continue
                    stale_confirmed = True
                    terminal = (
                        "rotation_stale_ip_confirmed"
                        if series == 0
                        else "rotation_stale_ip_after_retry"
                    )
                    break

                stale_confirmations = 0
                ready = bool(ready_hint)
                last_detail = str(readiness_detail or detail or "")
                callback_classification = _proxy_probe_classification(
                    bool(ready), last_detail, candidate_ip if ready else ""
                )
                if callback_classification == "proxy_auth_failed":
                    terminal = "proxy_auth_failed"
                    return {
                        "ok": False, "error": terminal, "outcome": terminal,
                        "rotation_requests": rotation_requests, "retryable": False,
                    }
                if not ready:
                    continue
                terminal = "rotation_verified"
                if stage_callback is not None:
                    stage_callback("PROXY_READINESS_CONFIRMED")
                emit_proxy_telemetry(
                    proxy_url, proxy_type=ctype, phase="readiness",
                    normalized_result=terminal, provider_state=provider_state,
                    connectivity=True, ip_changed=(
                        True if pre_rotation_ip else "unknown"
                    ), instagram_reachable="unknown", browser_launched=False,
                    retry_attempt=probe_index + 1,
                    final_classification=terminal,
                )
                return {
                    "ok": True, "error": "", "detail": last_detail,
                    "outcome": terminal, "lock_outcome": lock_outcome,
                    "reused": False, "provider_state": provider_state,
                    "generation": generation, "rotation_requests": rotation_requests,
                    "pre_rotation_ip": pre_rotation_ip, "exit_ip": candidate_ip,
                }

            if stale_confirmed and series == 0:
                emit_proxy_telemetry(
                    proxy_url, proxy_type=ctype, phase="readiness",
                    normalized_result="rotation_stale_ip_confirmed",
                    provider_state=provider_state, connectivity=True,
                    ip_changed=False, instagram_reachable="unknown",
                    browser_launched=False,
                    retry_attempt=MOBILE_READINESS_PROBE_ATTEMPTS,
                    final_classification="rotation_stale_ip_confirmed",
                )
                continue
            if stale_confirmed:
                return {
                    "ok": False, "error": terminal, "outcome": terminal,
                    "rotation_requests": rotation_requests, "retryable": True,
                }
            terminal = (
                "proxy_readiness_timeout"
                if connectivity_confirmed else "proxy_connection_failed"
            )
            return {
                "ok": False, "error": terminal, "outcome": terminal,
                "accepted_outcome": "rotation_accepted_but_not_ready",
                "detail": last_detail, "rotation_requests": rotation_requests,
                "retryable": True,
            }
        terminal = "proxy_readiness_timeout"
        return {
            "ok": False, "error": terminal, "outcome": terminal,
            "rotation_requests": rotation_requests, "retryable": True,
        }
    finally:
        stopped.set()
        thread.join(timeout=1.0)
        release_conn = db_conn()
        try:
            release_mobile_rotation_lease(release_conn, connection_id, owner_id, terminal)
        finally:
            release_conn.close()


def mark_proxy_failed(
    args: argparse.Namespace,
    account: dict[str, Any],
    detail: str,
    allow_static_replacement: bool = True,
    outcome: str = "",
) -> dict[str, Any] | None:
    name = str(account.get("name") or "")
    connection_id = int(account.get("web_connection_id") or 0)
    is_static = str(account.get("connection_type") or "") == "static"
    code = str(outcome or "proxy_connection_failed")
    error = (
        "low_quality_proxy: " if is_static else code + ": "
    ) + str(detail or "proxy validation failed")
    conn = db_conn()
    try:
        try:
            replacement = None
            if is_static:
                replacement = quarantine_static_connection(
                    conn,
                    name,
                    connection_id,
                    detail,
                    allow_replacement=allow_static_replacement,
                )
                if replacement:
                    refreshed = account_connections(conn, [name])
                    if refreshed:
                        account.clear()
                        account.update(refreshed[0])
            else:
                conn.execute(
                    "UPDATE accounts SET web_upload_last_error=?,updated_at=datetime('now') WHERE name=?",
                    (error, name),
                )
            if connection_id and not is_static:
                conn.execute(
                    "UPDATE web_connections SET enabled=CASE WHEN connection_type='static' THEN 0 ELSE enabled END,last_status=?,last_error=?,last_checked_at=datetime('now'),updated_at=datetime('now') WHERE id=?",
                    (code, str(detail or "proxy validation failed"), connection_id),
                )
            conn.commit()
            if is_static:
                # A replacement candidate keeps the same account attempt open.
                # Only a terminal gate failure owns the real job closure.
                if not replacement:
                    _record_static_proxy_gate_outcome(args, account, code)
            else:
                run_id = str(
                    getattr(args, "workflow_run_id", "")
                    or current_run_id()
                    or f"proxy-gate-{int(time.time())}"
                )
                conn.execute(
                    """
                    INSERT INTO ig_web_upload_jobs(
                        run_id,account_name,mode,provider,status,target_uploads,current_step,last_error,
                        domain_outcome,infrastructure_outcome,closure_owner,
                        closure_reason,started_at,finished_at,updated_at
                    ) VALUES(?,?,?,?,'failed',0,?,?,?,'worker_not_started',
                             'connection_scheduler','proxy_gate_failed','',
                             datetime('now'),datetime('now'))
                    """,
                    (
                        run_id, name,
                        str(getattr(args, "operation", "workflow")),
                        str(getattr(args, "provider", "camoufox")),
                        code, error, code,
                    ),
                )
                conn.commit()
                record_task_outcome(
                    run_id,
                    domain_outcome=code,
                    infrastructure_outcome="worker_not_started",
                    connection_state="unavailable",
                    scheduler_state="prelaunch_failed",
                    closure_owner="connection_scheduler",
                    closure_reason="proxy_gate_failed",
                )
            return dict(account) if replacement else None
        except Exception as exc:
            conn.rollback()
            log(f"{name}: could not persist proxy_failed state: {exc}", "WARNING")
    finally:
        conn.close()
    return None


def strict_proxy_gate(
    args: argparse.Namespace,
    lane: dict[str, Any],
    account: dict[str, Any],
    ctype: str,
    connection_name: str,
    has_rotation: bool,
    allow_static_replacement: bool = True,
) -> tuple[bool, str]:
    name = str(account.get("name") or "")
    proxy_url = str(account.get("proxy_url") or "").strip()
    if ctype not in {"static", "mobile", "phone"}:
        return True, ""
    attempts = 3
    last_detail = ""
    mobile_stale_confirmations = 0
    mobile_rotation_used = False
    terminal_outcome = "proxy_connection_failed"
    for attempt in range(attempts):
        try:
            ok, detail, exit_ip = probe_proxy_exit_ip(proxy_url)
        except Exception as exc:
            if ctype != "static":
                raise
            ok, detail, exit_ip = False, type(exc).__name__, ""
            terminal_outcome = "proxy_gate_internal_error"
            last_detail = detail
            break
        last_detail = detail
        probe_classification = _proxy_probe_classification(ok, detail, exit_ip)
        if probe_classification == "proxy_auth_failed":
            terminal_outcome = "proxy_auth_failed"
            break
        if ctype == "static" and not ok:
            terminal_outcome = _static_proxy_failure_outcome(detail)
        if ok:
            instagram_ok, instagram_detail = probe_instagram_transport(proxy_url)
            if not instagram_ok:
                last_detail = instagram_detail
                ok = False
                if ctype == "static":
                    terminal_outcome = _static_proxy_failure_outcome(
                        instagram_detail
                    )
            elif "inconclusive" in instagram_detail.lower():
                log(f"{connection_name}: {name} {instagram_detail}", "WARNING")
            else:
                log(f"{connection_name}: {name} {instagram_detail}", "OK")
        if ok:
            recovery = dict(lane.get("credential_recovery_series", {}).get(name) or {})
            recovery_ips = set(recovery.get("exit_ips") or set())
            if ctype == "static" and exit_ip in recovery_ips:
                last_detail = "candidate returned an exit IP already used in this credential recovery series"
                ok = False
        if ok:
            owner = used_exit_ip_owner(exit_ip)
            memory_allows = not owner or (not has_rotation and owner == name)
            if memory_allows:
                db_reserved, db_owner = reserve_persisted_exit_ip(
                    int(account.get("web_connection_id") or 0), name, exit_ip, has_rotation
                )
                if db_reserved:
                    reserved, memory_owner = reserve_exit_ip(exit_ip, name)
                    if reserved:
                        lane["last_exit_ip"] = exit_ip
                        if ctype == "static" and name in lane.get("credential_recovery_series", {}):
                            lane["credential_recovery_series"][name]["exit_ips"].add(exit_ip)
                        emit_proxy_telemetry(
                            proxy_url, proxy_type=ctype, phase="readiness",
                            normalized_result="ready", provider_state="not_applicable",
                            connectivity=True, ip_changed="unknown",
                            instagram_reachable=("inconclusive" not in instagram_detail.lower()),
                            browser_launched=False, retry_attempt=attempt + 1,
                            final_classification="proxy_ready",
                        )
                        log(f"{connection_name}: {name} proxy gate passed; exit IP {exit_ip}", "OK")
                        return True, exit_ip
                    owner = memory_owner
                else:
                    owner = db_owner
            last_detail = f"exit IP {exit_ip} was recently used by {owner}; a fresh IP is required"
        if has_rotation and attempt + 1 < attempts:
            if exit_ip and "used by" in last_detail:
                mobile_stale_confirmations += 1
            else:
                mobile_stale_confirmations = 0
            if (
                mobile_stale_confirmations >= MOBILE_STALE_IP_CONFIRMATIONS
                and not mobile_rotation_used
            ):
                log(
                    f"{connection_name}: {name} stale exit IP confirmed; rotating before browser launch",
                    "ACT",
                )
                rotation = durable_mobile_rotation(
                    account,
                    lambda: verify_proxy_after_rotation(
                        str(account.get("proxy_url") or "")
                    ),
                )
                mobile_rotation_used = True
                if not rotation.get("ok"):
                    terminal_outcome = str(
                        rotation.get("outcome") or "proxy_readiness_timeout"
                    )
                    last_detail = str(rotation.get("error") or terminal_outcome)
                    break
            else:
                # A single timeout or stale observation is evidence for another
                # probe, never permission to send a rotation request.
                time.sleep(MOBILE_READINESS_BACKOFF_SECONDS[min(
                    attempt + 1, len(MOBILE_READINESS_BACKOFF_SECONDS) - 1
                )])
        elif not has_rotation and attempt + 1 < attempts:
            # A duplicate static IP cannot become fresh by retrying the same
            # endpoint. Network timeouts, resets and refused connections can.
            if "used by" in last_detail:
                break
            log(f"{connection_name}: {name} {last_detail}; retrying the same static proxy", "ACT")
            time.sleep(3.0)
    if has_rotation and terminal_outcome == "proxy_connection_failed":
        terminal_outcome = "proxy_readiness_timeout"
    replacement = (
        mark_proxy_failed(
            args, account, last_detail, allow_static_replacement,
            outcome=terminal_outcome,
        )
        if ctype == "static"
        else mark_proxy_failed(
            args, account, last_detail, allow_static_replacement,
            outcome=terminal_outcome,
        )
    )
    emit_proxy_telemetry(
        proxy_url, proxy_type=ctype, phase="readiness", normalized_result="not_ready",
        provider_state="unknown", connectivity=False, ip_changed="unknown",
        instagram_reachable="unknown", browser_launched=False, retry_attempt=attempts,
        final_classification=(
            "proxy_readiness_failed" if ctype == "static" else terminal_outcome
        ),
    )
    if is_static := (ctype == "static"):
        if replacement and allow_static_replacement:
            replacement_name = str(account.get("connection_name") or "replacement static proxy")
            log(f"{connection_name}: quarantined for {name}; testing automatic replacement {replacement_name}", "ACT")
            return strict_proxy_gate(
                args,
                lane,
                account,
                "static",
                replacement_name,
                False,
                allow_static_replacement=False,
            )
    log(f"{connection_name}: skipped {name}; proxy_failed: {last_detail}", "WARNING")
    return False, ""

def _heartbeat_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_heartbeat(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return dict(value) if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _classify_worker_liveness(
    proc: Any,
    payload: dict[str, Any],
    *,
    transport_error: bool,
) -> str:
    if proc.poll() is not None:
        return "worker_process_dead"
    if transport_error:
        return "heartbeat_transport_failed"
    if payload.get("browser_process_tree_present") is False:
        return "browser_process_tree_missing"
    return "worker_alive"


def _persist_watchdog_closure(path: Path, evidence: dict[str, Any]) -> None:
    """Atomically preserve closure ownership before terminating a process tree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _record_infrastructure_outcome(
    account_name: str,
    outcome: str,
    *,
    closure_owner: str = "",
    closure_reason: str = "",
) -> None:
    """Persist process/cleanup evidence without changing the domain status."""
    conn = db_conn()
    try:
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(ig_web_upload_jobs)"
            ).fetchall()
        }
        for column in (
            "domain_outcome",
            "infrastructure_outcome",
            "closure_owner",
            "closure_reason",
        ):
            if column not in columns:
                conn.execute(
                    "ALTER TABLE ig_web_upload_jobs "
                    f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        row = conn.execute(
            """
            SELECT id,COALESCE(status,''),COALESCE(domain_outcome,''),
                   COALESCE(infrastructure_outcome,'')
            FROM ig_web_upload_jobs
            WHERE account_name=?
              AND (?='' OR run_id=?)
            ORDER BY id DESC LIMIT 1
            """,
            (
                str(account_name or ""),
                str(current_run_id() or ""),
                str(current_run_id() or ""),
            ),
        ).fetchone()
        if not row:
            conn.commit()
            return
        prior = str(row[3] or "")
        value = str(outcome or "")
        infrastructure = prior
        if value and value not in prior.split(";"):
            infrastructure = ";".join(
                item for item in (prior, value) if item
            )
        domain = str(row[2] or row[1] or "")
        conn.execute(
            """
            UPDATE ig_web_upload_jobs
            SET domain_outcome=?,infrastructure_outcome=?,
                closure_owner=CASE WHEN ?<>'' THEN ? ELSE closure_owner END,
                closure_reason=CASE WHEN ?<>'' THEN ? ELSE closure_reason END,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (
                domain,
                infrastructure,
                closure_owner,
                closure_owner,
                closure_reason,
                closure_reason,
                int(row[0]),
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log(
            f"{account_name}: could not persist infrastructure outcome: "
            f"{type(exc).__name__}",
            "WARNING",
        )
    finally:
        conn.close()


def _mark_workflow_stalled(account: dict[str, Any], seconds: int, reason: str = "workflow_stalled") -> None:
    name = str(account.get("name") or "")
    safe_reason = (
        reason
        if reason in {
            "browser_start_stalled",
            "worker_liveness_lost",
            "browser_process_tree_missing",
        }
        else "workflow_stalled"
    )
    phase = "browser startup" if safe_reason == "browser_start_stalled" else "browser workflow"
    error = f"{safe_reason}: no heartbeat during {phase} for {int(seconds)} seconds; process tree closed safely"
    conn = db_conn()
    try:
        conn.execute(
            "UPDATE accounts SET web_upload_last_error=?,updated_at=datetime('now') WHERE name=?",
            (error, name),
        )
        job = conn.execute(
            "SELECT id FROM ig_web_upload_jobs WHERE account_name=? AND status='running' ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        if job:
            terminal_status = "submitted_unverified" if job_has_reel_publish_intent(conn, int(job["id"])) else "failed"
            terminal_step = "submitted_unverified" if terminal_status == "submitted_unverified" else safe_reason
            conn.execute(
                """
                UPDATE ig_web_upload_jobs
                SET status=?,current_step=?,last_error=?,finished_at=datetime('now'),updated_at=datetime('now')
                WHERE id=?
                """,
                (terminal_status, terminal_step, error, int(job["id"])),
            )
            reconcile_terminal_upload_history(conn, int(job["id"]), terminal_status, error)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log(f"{name}: could not persist workflow_stalled: {exc}", "WARNING")
    finally:
        conn.close()


def _lifecycle_retry_safe(args: argparse.Namespace, typed: str, status: str, step: str, recovery_attempt: int) -> bool:
    """ISSUE-018's one clean retry, explicitly separate from proxy recovery."""
    if typed not in LIFECYCLE_RESULTS or typed in {"startup_reconciliation_required", "recovery_exhausted"}:
        return False
    if recovery_attempt >= 1 or status in {"success", "submitted_unverified", "confirmed"}:
        return False
    if not retry_safe(step, str(getattr(args, "operation", ""))):
        return False
    # An unknown Auto Login/2FA transition must never replay secret submission.
    lowered = str(step or "").lower()
    return not any(marker in lowered for marker in ("credentials_submitted", "otp_submitted", "two_factor"))


def _set_lifecycle_terminal(name: str, typed: str, initial: str = "") -> None:
    code = "recovery_exhausted" if typed in LIFECYCLE_RESULTS else "lifecycle_state_unknown"
    detail = f"{code}: initial={initial}" if initial else code
    conn = db_conn()
    try:
        row = conn.execute("SELECT id FROM ig_web_upload_jobs WHERE account_name=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        conn.execute("UPDATE accounts SET web_upload_last_error=?,updated_at=datetime('now') WHERE name=?", (detail, name))
        if row:
            conn.execute("UPDATE ig_web_upload_jobs SET status='failed',current_step=?,last_error=?,finished_at=datetime('now'),updated_at=datetime('now') WHERE id=?", (code, detail, int(row[0])))
        conn.commit()
    finally:
        conn.close()


def _run_worker_with_watchdog(command: list[str], env: dict[str, str], account: dict[str, Any], operation: str) -> int:
    global _LAST_BROWSER_LAUNCH
    name = str(account.get("name") or "")
    started = time.time()
    heartbeat_root = DATA_DIR / "runtime" / "heartbeats"
    heartbeat_root.mkdir(parents=True, exist_ok=True)
    heartbeat_path = heartbeat_root / f"{int(time.time() * 1000)}-{threading.get_ident()}-{name}.heartbeat"
    heartbeat_error_path = heartbeat_path.with_suffix(".transport_error.json")
    closure_path = heartbeat_path.with_suffix(".closure.json")
    heartbeat_path.write_text("scheduler_start\n", encoding="utf-8")
    env["SPARKGRID_HEARTBEAT_PATH"] = str(heartbeat_path)
    env["SPARKGRID_HEARTBEAT_ERROR_PATH"] = str(heartbeat_error_path)
    initial_heartbeat = _heartbeat_mtime(heartbeat_path)
    last_heartbeat = 0.0
    heartbeat_count = 0
    last_progress_at = started
    last_payload: dict[str, Any] = {}
    transport_error_logged = False
    # Starting several full browser engines in the same millisecond causes a
    # CPU/disk spike and false 240-second startup stalls. Stagger only process
    # creation; already-open independent lanes continue concurrently.
    if str(operation or "") not in {"api", "analytics_session"}:
        with _BROWSER_LAUNCH_LOCK:
            delay = max(0.0, 1.25 - (time.time() - _LAST_BROWSER_LAUNCH))
            if delay:
                time.sleep(delay)
            proc = subprocess.Popen(command, cwd=str(ROOT), env=env, **process_group_kwargs())
            _LAST_BROWSER_LAUNCH = time.time()
    else:
        proc = subprocess.Popen(command, cwd=str(ROOT), env=env, **process_group_kwargs())
    receipt_run_id = str(env.get("SPARKGRID_RUN_ID") or current_run_id())
    account_ref = (
        opaque_account_ref(receipt_run_id, name)
        if receipt_run_id and name
        else ""
    )
    if receipt_run_id:
        mark_task_child(receipt_run_id, "running", int(proc.pid or 0))
        try:
            append_run_event(
                receipt_run_id, "child_start", stream="process_events",
                child_process_state="running", pid=int(proc.pid or 0),
                account_ref=account_ref, worker_started=True,
            )
        except Exception:
            pass
    if str(operation or "") in {"api", "analytics_session"}:
        # These are request workers without a browser dump heartbeat. Their
        # own HTTP timeouts remain authoritative; a browser-heartbeat watchdog
        # would incorrectly kill a large own-API scan.
        code = int(proc.wait() or 0)
        if receipt_run_id:
            mark_task_child(receipt_run_id, "exited", int(proc.pid or 0))
            try:
                append_run_event(
                    receipt_run_id, "child_exit", stream="process_events",
                    child_process_state="exited", pid=int(proc.pid or 0),
                    return_code=code, account_ref=account_ref,
                    worker_started=True,
                )
            except Exception:
                pass
        heartbeat_path.unlink(missing_ok=True)
        return code
    try:
        while proc.poll() is None:
            time.sleep(2.0)
            heartbeat = _heartbeat_mtime(heartbeat_path)
            if heartbeat > max(initial_heartbeat, last_heartbeat):
                last_heartbeat = heartbeat
                heartbeat_count += 1
                last_progress_at = time.time()
                last_payload = _read_heartbeat(heartbeat_path)
            now = time.time()
            transport_error = heartbeat_error_path.is_file()
            liveness = _classify_worker_liveness(
                proc,
                last_payload,
                transport_error=transport_error,
            )
            if transport_error:
                if not transport_error_logged:
                    _record_infrastructure_outcome(
                        name, "heartbeat_transport_error"
                    )
                    log(
                        f"{name}: heartbeat_transport_error; worker remains "
                        "owned by its semantic workflow result",
                        "ERROR",
                    )
                    transport_error_logged = True
                # A broken file transport cannot prove a workflow stall.
                continue
            startup_stalled = heartbeat_count < 2 and now - started > 300
            active_stalled = heartbeat_count >= 2 and now - last_progress_at > 180
            if startup_stalled or active_stalled:
                threshold = 300 if startup_stalled else 180
                reason = (
                    "browser_start_stalled"
                    if startup_stalled
                    else (
                        "browser_process_tree_missing"
                        if liveness == "browser_process_tree_missing"
                        else "worker_liveness_lost"
                    )
                )
                evidence = {
                    "closure_owner": "connection_scheduler_watchdog",
                    "closure_reason": reason,
                    "worker_pid": int(getattr(proc, "pid", 0) or 0),
                    "last_liveness_sequence": int(
                        last_payload.get("heartbeat_sequence") or heartbeat_count
                    ),
                    "last_semantic_phase": str(
                        last_payload.get("current_operation") or ""
                    ),
                    "last_fresh_evidence_timestamp": int(
                        last_payload.get("monotonic_timestamp_ms") or 0
                    ),
                    "liveness_state": liveness,
                    "threshold_seconds": threshold,
                }
                _persist_watchdog_closure(closure_path, evidence)
                _mark_workflow_stalled(account, threshold, reason)
                _record_infrastructure_outcome(
                    name,
                    reason,
                    closure_owner="connection_scheduler_watchdog",
                    closure_reason=reason,
                )
                log(
                    f"{name}: {reason}; independent worker liveness stopped "
                    f"for {threshold}s; closing process tree",
                    "ERROR",
                )
                stop_process_tree(proc)
                return 124
        returncode = int(proc.returncode or 0)
        if receipt_run_id:
            mark_task_child(receipt_run_id, "exited", int(proc.pid or 0))
        _record_infrastructure_outcome(
            name,
            "worker_exit_0" if returncode == 0 else "worker_exit_nonzero",
            closure_owner="worker_process",
            closure_reason=(
                "normal_exit"
                if returncode == 0
                else "process_exit_nonzero"
            ),
        )
        return returncode
    finally:
        if receipt_run_id and proc.poll() is not None:
            mark_task_child(receipt_run_id, "exited", int(proc.pid or 0))
            try:
                append_run_event(
                    receipt_run_id, "child_exit", stream="process_events",
                    child_process_state="exited", pid=int(proc.pid or 0),
                    return_code=int(proc.returncode or 0),
                    account_ref=account_ref, worker_started=True,
                )
            except Exception:
                pass
        heartbeat_path.unlink(missing_ok=True)


def _auto_login_outcome(account: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str]:
    """Return the persisted login outcome separately from the child exit code."""
    if str(getattr(args, "operation", "") or "") != "workflow":
        return True, ""
    if str(getattr(args, "task", "") or "") not in {"auto_login", "auto_login_setup"}:
        return True, ""
    name = str(account.get("name") or "")
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(web_upload_login_status,''),COALESCE(web_upload_last_error,'') "
            "FROM accounts WHERE name=?",
            (name,),
        ).fetchone()
    finally:
        conn.close()
    status = str(row[0] if row else "") or "unknown"
    detail = str(row[1] if row else "")
    if status == "logged_in":
        return True, "logged_in"
    return False, status + (f": {detail}" if detail else "")


def _set_auto_login_terminal(name: str, code: str, detail: str) -> None:
    """Persist a local, secret-free Auto Login terminal outcome."""
    conn = db_conn()
    try:
        conn.execute(
            "UPDATE accounts SET web_upload_login_status=?,web_upload_last_error=?,updated_at=datetime('now') WHERE name=?",
            (code, detail, name),
        )
        job = conn.execute(
            "SELECT id FROM ig_web_upload_jobs WHERE account_name=? ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        if job:
            conn.execute(
                "UPDATE ig_web_upload_jobs SET status='failed',current_step=?,last_error=?,finished_at=datetime('now'),updated_at=datetime('now') WHERE id=?",
                (code, detail, int(job[0])),
            )
        conn.commit()
    finally:
        conn.close()


def _record_static_worker_domain(
    args: argparse.Namespace,
    account: dict[str, Any],
    outcome: str,
) -> None:
    """Attach a post-worker static recovery failure to that worker's real job."""
    run_id = str(getattr(args, "workflow_run_id", "") or current_run_id())
    name = str(account.get("name") or "")
    code = str(outcome or "static_proxy_pool_exhausted")
    if not run_id:
        return
    conn = db_conn()
    try:
        conn.execute(
            """
            UPDATE ig_web_upload_jobs SET
                status='failed',current_step=?,last_error=?,domain_outcome=?,
                closure_owner='connection_scheduler',
                closure_reason='proxy_gate_failed',
                finished_at=datetime('now'),updated_at=datetime('now')
            WHERE id=(
                SELECT id FROM ig_web_upload_jobs
                WHERE run_id=? AND account_name=? ORDER BY id DESC LIMIT 1
            )
            """,
            (code, code, code, run_id, name),
        )
        conn.commit()
    finally:
        conn.close()
    record_task_outcome(
        run_id,
        domain_outcome=code,
        connection_state="unavailable",
        scheduler_state="account_failed",
        closure_owner="connection_scheduler",
        closure_reason="proxy_gate_failed",
    )
    try:
        append_run_event(
            run_id, "domain_outcome", stream="outcomes",
            account_ref=opaque_account_ref(run_id, name),
            connection_ref=_connection_ref(
                run_id, int(account.get("web_connection_id") or 0)
            ),
            domain_outcome=code,
        )
    except Exception:
        pass


def _record_auto_login_prelaunch_failure(
    args: argparse.Namespace,
    account: dict[str, Any],
    code: str,
    detail: str,
) -> None:
    """Create the account-scoped terminal result missing before worker launch."""
    if (
        str(getattr(args, "operation", "") or "") != "workflow"
        or str(getattr(args, "task", "") or "") not in {"auto_login", "auto_login_setup"}
    ):
        return
    name = str(account.get("name") or "")
    run_id = str(
        getattr(args, "workflow_run_id", "")
        or current_run_id()
        or uuid.uuid4().hex
    )
    safe_detail = str(detail or code)[:160]
    conn = db_conn()
    try:
        conn.execute(
            "UPDATE accounts SET web_upload_login_status=?,web_upload_last_error=?,updated_at=datetime('now') WHERE name=?",
            (code, safe_detail, name),
        )
        conn.execute(
            """
            INSERT INTO ig_web_upload_jobs(
                run_id,account_name,mode,provider,status,target_uploads,current_step,last_error,
                domain_outcome,infrastructure_outcome,closure_owner,
                closure_reason,started_at,finished_at,updated_at
            ) VALUES(?,?,?,?,'failed',0,?,?,?,'worker_not_started',
                     'connection_scheduler',?,'',datetime('now'),datetime('now'))
            """,
            (
                run_id,
                name,
                str(account.get("web_upload_mode") or "desktop"),
                str(getattr(args, "provider", "camoufox") or "camoufox"),
                code,
                safe_detail,
                code,
                code,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    record_task_outcome(
        run_id,
        domain_outcome=code,
        infrastructure_outcome="worker_not_started",
        connection_state=safe_detail,
        scheduler_state="prelaunch_failed",
        closure_owner="connection_scheduler",
        closure_reason=code,
    )


def _normalized_rotation_failure(value: Any) -> str:
    """Collapse provider/network detail to a strict secret-free subtype."""
    text = str(value or "").lower()
    known = (
        "rotation_request_failed", "rotation_request_accepted",
        "rotation_endpoint_timeout", "rotation_endpoint_connection_failure",
        "rotation_endpoint_auth_failure", "rotation_endpoint_rate_limited",
        "rotation_endpoint_busy", "proxy_auth_failed", "proxy_connection_failed",
        "proxy_readiness_timeout", "rotation_stale_ip_confirmed",
        "rotation_stale_ip_after_retry", "rotation_accepted_but_not_ready",
        "rotation_verified",
    )
    for outcome in known:
        if outcome in text:
            return outcome
    if any(token in text for token in ("timed out", "timeout", "timeouterror")):
        return "rotation_endpoint_timeout"
    if any(token in text for token in ("ip unchanged", "has not changed", "not changed")):
        return "rotation_stale_ip_after_retry"
    if any(token in text for token in ("lease is still active", "rotation_lock_timeout")):
        return "rotation_lease_conflict"
    if any(token in text for token in ("malformed", "missing rotation", "invalid rotation")):
        return "malformed_rotation_configuration"
    if any(token in text for token in ("provider_busy", "cooldown", "rate_limited")):
        return "rotation_endpoint_busy"
    if any(token in text for token in ("validation", "proxy not ready", "not stabilized")):
        return "proxy_readiness_timeout"
    return "rotation_request_failed"


def _rotation_generation(args: argparse.Namespace, account: dict[str, Any], phase: str) -> str:
    workflow_run_id = str(getattr(args, "workflow_run_id", "") or "")
    if not workflow_run_id:
        workflow_run_id = uuid.uuid4().hex
        setattr(args, "workflow_run_id", workflow_run_id)
    return f"{workflow_run_id}:{int(account.get('web_connection_id') or 0)}:{phase}"


def _credential_recovery_state(lane: dict[str, Any], account: dict[str, Any]) -> dict[str, set[Any]]:
    series = lane.setdefault("credential_recovery_series", {})
    name = str(account.get("name") or "")
    state = series.setdefault(name, {"connection_ids": set(), "exit_ips": set()})
    state["connection_ids"].add(int(account.get("web_connection_id") or 0))
    prior_ip = str(lane.get("last_exit_ip") or "")
    if prior_ip:
        state["exit_ips"].add(prior_ip)
    return state


def _replace_static_after_credential_rejection(
    args: argparse.Namespace,
    lane: dict[str, Any],
    account: dict[str, Any],
    connection_name: str,
) -> tuple[bool, str]:
    """Quarantine the failed static record and atomically attach one fresh candidate.

    The caller performs the strict gate before launching its new browser.  A
    failed candidate is deliberately left quarantined, then this bounded caller
    asks for another free record; no browser/session is held during selection.
    """
    space = browser_disk_preflight()
    if not space.get("ok"):
        return False, "disk_space_low"
    name = str(account.get("name") or "")
    previous_id = int(account.get("web_connection_id") or 0)
    conn = db_conn()
    try:
        replacement = quarantine_static_connection(
            conn,
            name,
            previous_id,
            "credential rejection; static exit IP requires review",
            allow_replacement=True,
        )
        if not replacement:
            _set_auto_login_terminal(name, "static_proxy_pool_exhausted", "no eligible static proxy with a fresh exit IP")
            return False, "static_proxy_pool_exhausted"
        refreshed = account_connections(conn, [name])
        if not refreshed:
            _set_auto_login_terminal(name, "static_proxy_pool_exhausted", "replacement static proxy could not be assigned")
            return False, "static_proxy_pool_exhausted"
        account.clear()
        account.update(refreshed[0])
        state = lane.setdefault("credential_recovery_series", {}).setdefault(
            name, {"connection_ids": set(), "exit_ips": set()}
        )
        candidate_id = int(account.get("web_connection_id") or 0)
        if candidate_id in state["connection_ids"]:
            _set_auto_login_terminal(name, "static_proxy_pool_exhausted", "no unused static proxy record is available")
            return False, "static_proxy_pool_exhausted"
        state["connection_ids"].add(candidate_id)
        log(f"{connection_name}: {name} selected a fresh static proxy candidate", "ACT")
        return True, ""
    finally:
        conn.close()


def _latest_worker_result(name: str) -> tuple[str, str, str]:
    """Read the worker's typed terminal result without interpreting raw errors."""
    conn = db_conn()
    try:
        job = conn.execute(
            "SELECT COALESCE(status,''),COALESCE(current_step,''),COALESCE(last_error,'') "
            "FROM ig_web_upload_jobs WHERE account_name=? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        account = conn.execute(
            "SELECT COALESCE(web_upload_login_status,''),COALESCE(web_upload_last_error,'') "
            "FROM accounts WHERE name=?", (name,)
        ).fetchone()
    except Exception:
        return "", "", ""
    finally:
        conn.close()
    step = str(job[1] if job else "")
    status = str(job[0] if job else "")
    # Login workflow has an account-level typed result, while upload/story
    # workers persist it in current_step.
    typed = step
    # A successful newer worker job must not inherit a stale account-level
    # proxy classification from the failed attempt.  Account status is only a
    # fallback for failed login jobs that did not write a canonical step.
    if typed not in MOBILE_RECOVERY_CODES and status != "success":
        account_typed = str(account[0] if account else "")
        if account_typed in MOBILE_RECOVERY_CODES:
            typed = account_typed
    return typed, status, step


def _mobile_retry_safe(args: argparse.Namespace, account: dict[str, Any], typed: str, status: str, step: str) -> bool:
    if typed not in MOBILE_RECOVERY_CODES or status in {"submitted_unverified", "success"}:
        return False
    text = str(step or "").lower()
    if text in MOBILE_RECOVERY_BLOCKED_STEPS:
        return False
    # ISSUE-016 owns every OTP outcome; ISSUE-015 owns submitted-password
    # credential recovery.  Neither can enter an IP retry through this path.
    if any(marker in text for marker in ("two_factor", "incorrect_credentials", "login_required", "challenge", "checkpoint")):
        return False
    return True


def _mobile_recovery_rotation(
    args: argparse.Namespace, lane: dict[str, Any], account: dict[str, Any], connection_name: str,
    failed_ip: str, *, sleep_after: bool, recovery: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Rotate once under the existing lease and prove a different ready IP."""
    recovery = dict(recovery or {})
    account_name = str(account.get("name") or "")
    workflow_id = str(recovery.get("workflow_id") or "")
    if not workflow_id:
        rotation = durable_mobile_rotation(
            account,
            lambda: verify_proxy_after_rotation(
                str(account.get("proxy_url") or "")
            ),
            sleep_after=sleep_after,
        )
        if not rotation.get("ok"):
            return False, str(rotation.get("error") or "rotation_failed")
        ok, fresh_ip = strict_proxy_gate(
            args, lane, account, "mobile", connection_name, True
        )
        if not ok:
            return False, "mobile_proxy_not_ready"
        if not fresh_ip or fresh_ip == failed_ip:
            return False, "mobile_proxy_exit_ip_unchanged"
        return True, fresh_ip
    checker = str(
        recovery.get("ip_checker")
        or "https://api.ipify.org?format=json"
    )
    fresh_ip = ""
    readiness_detail = ""

    def readiness() -> ProxyReadinessResult:
        nonlocal fresh_ip, readiness_detail
        ok, detail, candidate = _recovery_exit_ip_probe(
            str(account.get("proxy_url") or ""), checker
        )
        readiness_detail = detail
        if not ok:
            return ProxyReadinessResult(False, detail, candidate)
        if not candidate or candidate == failed_ip:
            return ProxyReadinessResult(
                False, "mobile recovery exit IP has not changed yet", candidate
            )
        instagram_ok, instagram_detail = probe_instagram_transport(
            str(account.get("proxy_url") or "")
        )
        if not instagram_ok:
            return ProxyReadinessResult(False, instagram_detail, candidate)
        db_reserved, db_owner = reserve_persisted_exit_ip(
            int(account.get("web_connection_id") or 0),
            account_name,
            candidate,
            True,
        )
        if not db_reserved:
            return ProxyReadinessResult(
                False,
                f"mobile recovery exit IP was recently used by {db_owner}",
                candidate,
            )
        reserved, memory_owner = reserve_exit_ip(candidate, account_name)
        if not reserved:
            return ProxyReadinessResult(
                False,
                f"mobile recovery exit IP is active for {memory_owner}",
                candidate,
            )
        fresh_ip = candidate
        return ProxyReadinessResult(True, detail, candidate)

    def stage(stage_name: str) -> None:
        if workflow_id:
            mark_password_recovery_stage(
                account_name, workflow_id, stage_name
            )

    generation = str(
        recovery.get("recovery_mobile_generation")
        or _rotation_generation(args, account, "password-recovery")
    )
    rotation = durable_mobile_rotation(
        account,
        readiness,
        sleep_after=sleep_after,
        generation=generation,
        stage_callback=stage if workflow_id else None,
        lease_owner=workflow_id,
    )
    if not rotation.get("ok"):
        if workflow_id:
            mark_password_recovery_stage(
                account_name, workflow_id, "ROTATION_NOT_STABILIZED"
            )
        return False, str(
            rotation.get("error")
            or readiness_detail
            or "mobile_rotation_not_stabilized"
        )
    if not fresh_ip or fresh_ip == failed_ip:
        return False, "mobile_proxy_exit_ip_unchanged"
    if workflow_id:
        mark_password_recovery_stage(
            account_name,
            workflow_id,
            "EXIT_IP_CHANGED",
            replacement_exit_ip=fresh_ip,
        )
    return True, fresh_ip


def _set_mobile_recovery_terminal(name: str, code: str, initial: str) -> None:
    """Keep the failure local and typed; do not alter account/login identity."""
    detail = f"{code}: initial={initial}" if initial else code
    conn = db_conn()
    try:
        conn.execute("UPDATE accounts SET web_upload_last_error=?,updated_at=datetime('now') WHERE name=?", (detail, name))
        row = conn.execute(
            "SELECT id FROM ig_web_upload_jobs WHERE account_name=? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE ig_web_upload_jobs SET status='failed',current_step=?,last_error=?,finished_at=datetime('now'),updated_at=datetime('now') WHERE id=?",
                (code, detail, int(row[0])),
            )
        conn.commit()
    finally:
        conn.close()


def run_account(args: argparse.Namespace, lane: dict[str, Any], account: dict[str, Any], position: int) -> int:
    name = str(account["name"])
    ctype = "direct" if args.no_proxy else str(account.get("connection_type") or "direct")
    connection_name = "Direct" if args.no_proxy else str(account.get("connection_name") or "Direct")
    is_mobile = not args.no_proxy and ctype in {"mobile", "phone"}
    has_rotation = is_mobile and bool(str(account.get("rotation_url") or "").strip())
    is_auto_login = (
        args.operation == "workflow"
        and str(getattr(args, "task", "")) in {"auto_login", "auto_login_setup"}
    )
    recovery: dict[str, Any] = {}
    initial_generation = ""
    if is_auto_login:
        recovery = begin_or_resume_password_recovery(
            name,
            str(getattr(args, "workflow_run_id", "") or ""),
            str(getattr(args, "task", "")),
            ctype,
            int(account.get("web_connection_id") or 0),
            workflow_id=str(getattr(args, "password_recovery_workflow_id", "") or ""),
        )
        setattr(args, "password_recovery_workflow_id", str(recovery.get("workflow_id") or ""))
        if not getattr(args, "workflow_run_id", ""):
            setattr(args, "workflow_run_id", str(recovery.get("workflow_id") or ""))
    resuming_second_submission = bool(
        recovery
        and int(recovery.get("password_submission_count") or 0) == 1
        and str(recovery.get("recovery_stage") or "") == "READY_FOR_SECOND_SUBMISSION"
    )
    recovery_ip_change_already_consumed = bool(
        recovery
        and int(recovery.get("password_submission_count") or 0) == 1
        and int(recovery.get("recovery_ip_change_count") or 0) == 1
    )
    should_rotate_before_first = (
        has_rotation
        and not recovery_ip_change_already_consumed
        and (
            args.operation == "story"
            or (position == 0 and (
                args.operation == "analytics_session"
                or bool(int(account.get("rotate_before_first") or 0))
            ))
        )
    )
    if should_rotate_before_first:
        reason = (
            "before Story" if args.operation == "story"
            else "before own API scan" if args.operation == "analytics_session"
            else "before first account"
        )
        log(f"{connection_name}: rotating {reason} {name}", "ACT")
        initial_generation = _rotation_generation(args, account, "before-first")
        result = durable_mobile_rotation(
            account,
            lambda: verify_proxy_after_rotation(str(account.get("proxy_url") or "")),
            generation=initial_generation,
        )
        if not result.get("ok"):
            rotation_error = str(result.get("error") or "rotation_failed")
            detail = _normalized_rotation_failure(
                result.get("outcome") or rotation_error
            )
            try:
                append_run_event(
                    args.workflow_run_id,
                    "connection_rotation",
                    rotation_state=detail,
                    infrastructure_outcome=(
                        "connection_rotation_failed_before_browser_launch"
                    ),
                )
            except Exception:
                pass
            _record_auto_login_prelaunch_failure(
                args, account, detail, detail,
            )
            if not is_auto_login:
                mark_proxy_failed(
                    args, account, detail, outcome=detail,
                )
            log(
                f"{connection_name}: rotation failed before {name}: {detail}",
                "ERROR",
            )
            return 3
        detail = str(result.get("detail") or "shared rotation result")
        log(f"{connection_name}: initial rotation complete; {detail}; starting {name}", "OK")
    elif is_mobile and not has_rotation:
        log(f"{connection_name}: no rotation link; post-process rotation is unavailable for {name}", "WARNING")

    if is_mobile and recovery_ip_change_already_consumed:
        gate_ok, gate_detail, used_exit_ip = _recovery_exit_ip_probe(
            str(account.get("proxy_url") or ""),
            str(
                recovery.get("ip_checker")
                or "https://api.ipify.org?format=json"
            ),
        )
        initial_ip = str(recovery.get("initial_exit_ip") or "")
        if (
            not gate_ok
            or not used_exit_ip
            or (initial_ip and used_exit_ip == initial_ip)
        ):
            reason = "mobile_rotation_not_stabilized"
            mark_password_recovery_terminal(
                name, str(recovery.get("workflow_id") or ""), reason
            )
            _set_auto_login_terminal(name, reason, str(gate_detail or reason))
            return 3
        if not resuming_second_submission:
            mark_password_recovery_stage(
                name,
                str(recovery.get("workflow_id") or ""),
                "EXIT_IP_CHANGED",
                replacement_exit_ip=used_exit_ip,
            )
            mark_password_recovery_stage(
                name,
                str(recovery.get("workflow_id") or ""),
                "READY_FOR_SECOND_SUBMISSION",
            )
            resuming_second_submission = True
    else:
        gate_ok, used_exit_ip = strict_proxy_gate(
            args, lane, account, ctype, connection_name, has_rotation
        )
    if not gate_ok:
        # A bad/duplicate proxy belongs to this account, not to the whole
        # queue. Keep the browser closed and let run_lane continue.
        return 4

    if recovery:
        checker = "https://api.ipify.org?format=json"
        baseline_ok, _baseline_detail, baseline_ip = _recovery_exit_ip_probe(
            str(account.get("proxy_url") or ""), checker
        )
        if baseline_ok and baseline_ip:
            used_exit_ip = baseline_ip
        update_password_recovery_context(
            name,
            str(recovery.get("workflow_id") or ""),
            exit_ip=used_exit_ip,
            initial_generation=initial_generation,
            ip_checker=checker,
        )
        recovery = get_active_password_recovery(
            name, workflow_id=str(recovery.get("workflow_id") or "")
        ) or recovery

    if ctype == "static" and args.operation == "workflow" and str(getattr(args, "task", "")) in {"auto_login", "auto_login_setup"}:
        _credential_recovery_state(lane, account)

    command = base_command(args, account)
    log(f"{connection_name}: {name} started ({args.operation})", "OK")
    env = os.environ.copy()
    env["SPARKGRID_DATA_DIR"] = str(DATA_DIR)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["SPARKGRID_RECOVERY_ATTEMPT"] = "0"
    env["SPARKGRID_PROXY_GATE_PASSED"] = "1"
    if recovery:
        env["SPARKGRID_PASSWORD_RECOVERY_WORKFLOW_ID"] = str(
            recovery.get("workflow_id") or ""
        )
    if args.operation == "workflow" and not args.no_proxy:
        selected_proxy = str(account.get("proxy_url") or "").strip()
        if selected_proxy:
            # The scheduler's joined web_connections row is authoritative.
            # Keep credentials out of the child command line and override a
            # potentially stale legacy accounts.proxy value via the environment.
            env["SPARKGRID_ACCOUNT_PROXY"] = selected_proxy
    outcome = ""
    outcome_ok = False
    returncode = _run_worker_with_watchdog(command, env, account, str(args.operation))
    logical_returncode = int(returncode)
    if returncode == 0:
        outcome_ok, outcome = _auto_login_outcome(account, args)
        if outcome_ok:
            if recovery:
                mark_password_recovery_success(
                    name, str(recovery.get("workflow_id") or "")
                )
            suffix = f"; {outcome}" if outcome else ""
            log(f"{connection_name}: {name} finished with code 0{suffix}", "OK")
            if resuming_second_submission:
                return 0
        else:
            logical_returncode = 5
            log(
                f"{connection_name}: {name} worker exited with code 0 but login did not complete; {outcome}",
                "WARNING",
            )
    else:
        log(f"{connection_name}: {name} finished with code {returncode}", "ERROR")

    # ISSUE-017: a post-gate, typed transport/blank-document failure is the
    # only mobile failure eligible for one same-task retry.  The worker has
    # exited before this point, so its browser/process resources are already
    # released; no terminal account result is written before the retry.
    typed, worker_status, worker_step = _latest_worker_result(name)
    explicit_password_rejection = bool(
        is_auto_login
        and returncode == 0
        and not outcome_ok
        and str(outcome).startswith("incorrect_credentials")
    )
    if explicit_password_rejection and recovery:
        workflow_id = str(recovery.get("workflow_id") or "")
        rejection = record_password_rejection(name, workflow_id)
        if rejection.get("terminal"):
            reason = "invalid_credentials_after_ip_retry"
            mark_password_recovery_terminal(name, workflow_id, reason)
            _set_auto_login_terminal(
                name,
                reason,
                "password rejected after one fresh-IP retry",
            )
            log(
                f"{connection_name}: {name} password recovery ended after two submissions",
                "WARNING",
            )
            return 5
        if not rejection.get("ok"):
            reason = str(
                rejection.get("reason")
                or "password_recovery_state_invalid"
            )
            mark_password_recovery_terminal(name, workflow_id, reason)
            _set_auto_login_terminal(name, reason, reason)
            return 5

        recovery = get_active_password_recovery(
            name, workflow_id=workflow_id
        ) or recovery
        if int(recovery.get("recovery_ip_change_count") or 0) >= 1:
            reason = "invalid_credentials_after_ip_retry"
            mark_password_recovery_terminal(name, workflow_id, reason)
            _set_auto_login_terminal(name, reason, reason)
            return 5

        if ctype == "static":
            changed = mark_password_rotation_requested(
                name, workflow_id
            )
            if not changed.get("ok"):
                reason = str(
                    changed.get("reason")
                    or "static_recovery_ip_change_not_allowed"
                )
                mark_password_recovery_terminal(name, workflow_id, reason)
                _set_auto_login_terminal(name, reason, reason)
                return 5
            selected, recovery_code = _replace_static_after_credential_rejection(
                args, lane, account, connection_name
            )
            if not selected:
                reason = str(recovery_code or "static_proxy_pool_exhausted")
                mark_password_recovery_terminal(name, workflow_id, reason)
                if reason == "static_proxy_pool_exhausted":
                    _record_static_worker_domain(args, account, reason)
                return 6 if reason == "disk_space_low" else 5
            gate_ok, fresh_ip = strict_proxy_gate(
                args,
                lane,
                account,
                "static",
                str(account.get("connection_name") or connection_name),
                False,
                allow_static_replacement=False,
            )
            if gate_ok:
                consistent_ok, _consistent_detail, consistent_ip = (
                    _recovery_exit_ip_probe(
                        str(account.get("proxy_url") or ""),
                        str(
                            recovery.get("ip_checker")
                            or "https://api.ipify.org?format=json"
                        ),
                    )
                )
                gate_ok = bool(consistent_ok)
                fresh_ip = consistent_ip if consistent_ok else ""
            if not gate_ok or not fresh_ip or fresh_ip == used_exit_ip:
                reason = (
                    "static_replacement_exit_ip_unchanged"
                    if fresh_ip == used_exit_ip
                    else "static_replacement_not_ready"
                )
                mark_password_recovery_terminal(name, workflow_id, reason)
                _set_auto_login_terminal(name, reason, reason)
                return 5
            mark_password_recovery_stage(
                name,
                workflow_id,
                "EXIT_IP_CHANGED",
                replacement_connection_id=int(
                    account.get("web_connection_id") or 0
                ),
                replacement_exit_ip=fresh_ip,
            )
        elif is_mobile and has_rotation:
            generation = _rotation_generation(
                args, account, "password-recovery"
            )
            changed = mark_password_rotation_requested(
                name,
                workflow_id,
                generation=generation,
                lease_id=(
                    f"mobile:{int(account.get('web_connection_id') or 0)}:"
                    f"{generation}"
                ),
                lease_owner=workflow_id,
            )
            if not changed.get("ok"):
                reason = str(
                    changed.get("reason")
                    or "mobile_recovery_rotation_not_allowed"
                )
                mark_password_recovery_terminal(name, workflow_id, reason)
                _set_auto_login_terminal(name, reason, reason)
                return 5
            recovery = get_active_password_recovery(
                name, workflow_id=workflow_id
            ) or recovery
            fresh, fresh_ip = _mobile_recovery_rotation(
                args,
                lane,
                account,
                connection_name,
                used_exit_ip,
                sleep_after=True,
                recovery=recovery,
            )
            if not fresh:
                reason = "mobile_rotation_not_stabilized"
                mark_password_recovery_terminal(name, workflow_id, reason)
                _set_auto_login_terminal(name, reason, str(fresh_ip or reason))
                log(
                    f"{connection_name}: {name} recovery rotation did not stabilize; password retry blocked",
                    "ERROR",
                )
                return 3
        else:
            reason = "password_ip_recovery_connection_unsupported"
            mark_password_recovery_terminal(name, workflow_id, reason)
            _set_auto_login_terminal(name, reason, reason)
            return 5

        mark_password_recovery_stage(
            name, workflow_id, "READY_FOR_SECOND_SUBMISSION"
        )
        env["SPARKGRID_RECOVERY_ATTEMPT"] = "1"
        if not args.no_proxy:
            env["SPARKGRID_ACCOUNT_PROXY"] = str(
                account.get("proxy_url") or ""
            )
        log(
            f"{connection_name}: {name} Auto Login submission 2/2 starting in a fresh browser",
            "ACT",
        )
        retry_returncode = _run_worker_with_watchdog(
            base_command(args, account), env, account, str(args.operation)
        )
        retry_ok, retry_outcome = (
            _auto_login_outcome(account, args)
            if retry_returncode == 0
            else (False, "")
        )
        if retry_ok:
            mark_password_recovery_success(name, workflow_id)
            log(
                f"{connection_name}: {name} logged in after one fresh-IP retry",
                "OK",
            )
            # Recovery success must not trigger another mobile rotation.
            return 0
        if (
            retry_returncode == 0
            and str(retry_outcome).startswith("incorrect_credentials")
        ):
            record_password_rejection(name, workflow_id)
            reason = "invalid_credentials_after_ip_retry"
            mark_password_recovery_terminal(name, workflow_id, reason)
            _set_auto_login_terminal(
                name,
                reason,
                "password rejected after one fresh-IP retry",
            )
            return 5
        reason = (
            str(retry_outcome).split(":", 1)[0]
            if retry_outcome
            else "password_recovery_retry_failed"
        )
        mark_password_recovery_terminal(name, workflow_id, reason)
        return int(retry_returncode or 5)

    if is_mobile and has_rotation and _mobile_retry_safe(args, account, typed, worker_status, worker_step):
        failed_ip = used_exit_ip or str(lane.get("last_exit_ip") or "")
        space = browser_disk_preflight()
        if not space.get("ok"):
            log(f"{connection_name}: {name} mobile recovery paused for disk safety after {typed}", "WARNING")
            return 6
        log(f"{connection_name}: {name} retrying once after mobile IP rotation ({typed})", "ACT")
        fresh, recovery_detail = _mobile_recovery_rotation(
            args, lane, account, connection_name, failed_ip, sleep_after=True,
        )
        if not fresh:
            _set_mobile_recovery_terminal(name, "mobile_proxy_recovery_exhausted", typed)
            log(f"{connection_name}: {name} mobile recovery could not prove a fresh ready IP: {recovery_detail}", "ERROR")
            return 3
        command = base_command(args, account)
        retry_returncode = _run_worker_with_watchdog(command, env, account, str(args.operation))
        retry_logical = int(retry_returncode)
        retry_outcome = ""
        if retry_returncode == 0:
            retry_ok, retry_outcome = _auto_login_outcome(account, args)
            if not retry_ok:
                retry_logical = 5
        retry_typed, retry_status, retry_step = _latest_worker_result(name)
        if _mobile_retry_safe(args, account, retry_typed, retry_status, retry_step):
            terminal = "mobile_proxy_blank_document_after_retry" if retry_typed == "blank_document" else "mobile_proxy_network_failed_after_retry"
            _set_mobile_recovery_terminal(name, terminal, typed)
            log(f"{connection_name}: {name} recovery retry exhausted ({retry_typed}); preparing lane hygiene rotation", "WARNING")
            hygiene_ok, hygiene_detail = _mobile_recovery_rotation(
                args, lane, account, connection_name, fresh, sleep_after=True,
            )
            if not hygiene_ok:
                log(f"{connection_name}: hygiene rotation failed; mobile lane is not ready: {hygiene_detail}", "ERROR")
                return 3
            log(f"{connection_name}: hygiene rotation ready; continuing lane after {name}", "OK")
            return 5
        logical_returncode = retry_logical
        if retry_logical == 0:
            suffix = f"; {retry_outcome}" if retry_outcome else ""
            log(f"{connection_name}: {name} mobile recovery retry succeeded{suffix}", "OK")

    # Mobile transport/blank failures already have exactly one owner in
    # ISSUE-017.  Static/direct browser lifecycle failures get one clean
    # session on the same assigned connection, only before an irreversible
    # stage.  No proxy is quarantined by this generic path.
    if not is_mobile and _lifecycle_retry_safe(args, typed, worker_status, worker_step, 0):
        space = browser_disk_preflight()
        if not space.get("ok"):
            return 6
        log(f"{connection_name}: {name} retrying once in a clean browser session ({typed})", "ACT")
        env["SPARKGRID_RECOVERY_ATTEMPT"] = "1"
        retry_returncode = _run_worker_with_watchdog(base_command(args, account), env, account, str(args.operation))
        retry_typed, retry_status, retry_step = _latest_worker_result(name)
        if _lifecycle_retry_safe(args, retry_typed, retry_status, retry_step, 1):
            _set_lifecycle_terminal(name, retry_typed, typed)
            log(f"{connection_name}: {name} lifecycle recovery exhausted ({retry_typed})", "WARNING")
            return 5
        logical_returncode = int(retry_returncode)

    # Every mobile process leaves the endpoint on a fresh IP. Between queued
    # accounts the configured wait happens here, so the next account starts
    # only after the provider has applied the rotation. The last rotation does
    # not sleep because no account follows it.
    if has_rotation and args.operation != "story":
        log(f"{connection_name}: rotating after {name}", "ACT")
        has_next = position + 1 < int(lane.get("accounts") or 0)
        # The lease owns the entire rotation/cooldown/readiness boundary.  The
        # existing final probe below remains for the lane's exit-IP bookkeeping.
        rotation = durable_mobile_rotation(
            account,
            lambda: verify_proxy_after_rotation(str(account.get("proxy_url") or "")),
            sleep_after=has_next,
            generation=_rotation_generation(args, account, f"after:{position}:{name}"),
        )
        if not rotation.get("ok"):
            detail = str(rotation.get("error") or "rotation_failed")
            _record_infrastructure_outcome(
                name,
                "post_workflow_rotation_failed",
            )
            log(f"{connection_name}: rotation failed after {name}: {detail}", "ERROR")
            return 3
        verified, detail, prepared_ip = probe_proxy_exit_ip(str(account.get("proxy_url") or ""))
        if not verified:
            _record_infrastructure_outcome(
                name,
                "post_workflow_rotation_verification_failed",
            )
            log(f"{connection_name}: rotation completed but {detail}", "ERROR")
            return 3
        previous_owner = used_exit_ip_owner(prepared_ip)
        if has_next and previous_owner:
            log(f"{connection_name}: rotation returned previously used exit IP {prepared_ip}; next account gate will rotate again", "WARNING")
        else:
            log(f"{connection_name}: post-process rotation complete for {name}; {detail}", "OK")
    return int(logical_returncode)


def run_lane(args: argparse.Namespace, lane_name: str, accounts: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"lane": lane_name, "accounts": len(accounts), "failed": 0}
    log(f"Connection lane {lane_name}: {len(accounts)} account(s) queued", "INFO")
    for index, account in enumerate(accounts):
        code = run_account(args, result, account, index)
        if code != 0:
            result["failed"] += 1
            # Code 4 means the account was deliberately skipped by the
            # strict proxy gate. Its single proxy_failed record is enough;
            # continue without a second cascade-style lane error.
            if code == 4:
                continue
            if code == 3:
                log(
                    f"{lane_name}: account-local rotation failure; continuing queue",
                    "WARNING",
                )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="SparkGrid connection-aware account scheduler")
    parser.add_argument("--operation", choices=["api", "clean_web", "workflow", "web_warmup", "story", "analytics_session"], required=True)
    parser.add_argument("--accounts", required=True)
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--provider", choices=["camoufox", "playwright"], default="camoufox")
    parser.add_argument("--worker-script", default="", help=argparse.SUPPRESS)
    parser.add_argument("--max-workers", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--caption", default="")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--ignore-cooldown", action="store_true")
    parser.add_argument("--asset-id", type=int, default=0)
    parser.add_argument("--history-id", type=int, default=0)
    parser.add_argument("--target-ids", default="")
    parser.add_argument("--target", type=int, default=1)
    parser.add_argument("--pre-warmup-min", type=float, default=1)
    parser.add_argument("--pre-warmup-max", type=float, default=2)
    parser.add_argument("--post-warmup-min", type=float, default=1)
    parser.add_argument("--post-warmup-max", type=float, default=3)
    parser.add_argument("--cooldown-hours", type=float, default=4)
    parser.add_argument("--task", default="check_login")
    parser.add_argument("--professional-type", choices=["creator", "business"], default="creator")
    parser.add_argument("--professional-category", default="Personal blog")
    parser.add_argument("--show-category", action="store_true")
    parser.add_argument("--ensure-public", action="store_true")
    parser.add_argument("--convert-professional", action="store_true")
    parser.add_argument("--minutes", type=float, default=8)
    parser.add_argument("--persona", choices=["generalist", "shopper", "foodie", "techie", "random"], default="random")
    parser.add_argument("--arrive", choices=["direct", "search"], default="direct")
    parser.add_argument("--skip-proxy-check", action="store_true")
    parser.add_argument("--include-parser-accounts", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--story-manifest", default="")
    parser.add_argument("--story-job-id", type=int, default=0)
    parser.add_argument("--image", default="")
    parser.add_argument("--link", default="")
    parser.add_argument("--sticker-text", default="Chat with me👇🏻")
    parser.add_argument("--sticker-x", type=float, default=0.5)
    parser.add_argument("--sticker-y", type=float, default=0.82)
    parser.add_argument("--highlight-name", default="")
    parser.add_argument("--stop-lane-on-rotation-error", action="store_true", default=False, help=argparse.SUPPRESS)
    parser.add_argument("--run-id", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.workflow_run_id = str(
        args.run_id or current_run_id() or uuid.uuid4().hex
    )
    if args.workflow_run_id:
        update_task_receipt(
            args.workflow_run_id,
            scheduler_state="running",
            parent_process_state="running",
        )
        try:
            append_run_event(
                args.workflow_run_id, "scheduler_start",
                stream="process_events", scheduler_state="running",
            )
        except Exception:
            pass
    args.parallel = max(1, min(int(args.parallel or 1), 100))

    names = parse_names(args.accounts)
    if not names:
        log("No accounts selected", "ERROR")
        return 2
    story_manifest: dict[str, Any] = {}
    if args.operation == "story" and args.story_manifest:
        try:
            story_manifest = json.loads(Path(args.story_manifest).read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"Story manifest could not be read: {exc}", "ERROR")
            return 2
        selection = dict(story_manifest.get("selection") or {})
        if selection:
            names = story_snapshot_names(names, selection)
            if not names:
                log("Saved Story selection is empty; no workers will start", "WARNING")
                return 0
    conn = db_conn()
    try:
        ensure_connection_schema(conn)
        accounts = account_connections(
            conn,
            names,
            include_parser_accounts=bool(args.include_parser_accounts),
        )
    finally:
        conn.close()
    found = {str(item["name"]) for item in accounts}
    for account in accounts:
        try:
            connection_id = int(account.get("web_connection_id") or 0)
            connection_ref = _connection_ref(
                args.workflow_run_id, connection_id
            )
            append_run_event(
                args.workflow_run_id,
                "connection_assignment",
                connection_ref=connection_ref,
                connection_type=str(
                    account.get("connection_type") or "direct"
                ),
                category=(
                    "available" if connection_id > 0 else "not_applicable"
                ),
            )
        except Exception:
            pass
    missing = [name for name in names if name not in found]
    if missing:
        log("Accounts not found: " + ", ".join(missing), "WARNING")
    if not accounts:
        return 2
    if args.operation in {"clean_web", "workflow", "web_warmup", "story", "analytics_session"}:
        space = browser_disk_preflight()
        try:
            append_run_event(
                args.workflow_run_id, "disk_preflight",
                category="passed" if space.get("ok") else "blocked",
                infrastructure_outcome=(
                    "" if space.get("ok") else "insufficient_disk_space"
                ),
            )
        except Exception:
            pass
        if not space.get("ok"):
            code = str(space.get("code") or "disk_space_low")
            detail = f"{code}; free_bytes={int(space.get('free_bytes') or 0)}; required_reserve_bytes={int(space.get('required_reserve_bytes') or DEFAULT_RESERVE_BYTES)}"
            log(f"Browser jobs paused: {detail}", "WARNING")
            # This is system state, not an account/proxy/login fault. No worker,
            # reservation, retry counter, account state, or proxy is changed.
            record_task_outcome(
                args.workflow_run_id,
                infrastructure_outcome="insufficient_disk_space",
                scheduler_state="preflight_rejected",
                closure_owner="connection_scheduler",
                closure_reason="insufficient_disk_space",
            )
            return 6
    active_accounts = []
    for account in accounts:
        state = " ".join(str(account.get(key) or "") for key in ("status", "web_upload_login_status", "web_upload_last_error")).lower()
        if "suspend" in state or "banned" in state or "account disabled" in state:
            log(f"{account['name']}: suspended account skipped", "WARNING")
            continue
        if "low_quality_proxy" in state:
            log(f"{account['name']}: skipped until a replacement proxy is assigned", "WARNING")
            continue
        if "proxy_required" in state:
            log(f"{account['name']}: skipped because no proxy is assigned", "WARNING")
            continue
        active_accounts.append(account)
    accounts = active_accounts
    if not accounts:
        log("All selected accounts are suspended; nothing will be opened", "WARNING")
        return 0
    if args.operation == "story":
        ready_accounts = story_ready_accounts(accounts)
        skipped = [str(account["name"]) for account in accounts if account not in ready_accounts]
        for name in skipped:
            log(f"{name}: Story skipped because the saved account is not logged in", "WARNING")
        accounts = ready_accounts
        if not accounts:
            log("No saved Story accounts are currently ready; no workers will start", "WARNING")
            return 0
    if args.story_manifest:
        manifest_accounts = (
            dict(story_manifest.get("accounts") or {})
            if isinstance(story_manifest.get("accounts"), dict)
            else story_manifest
        )
        for account in accounts:
            account["story"] = dict(manifest_accounts.get(str(account["name"])) or {})

    with _EXIT_IP_LOCK:
        _USED_EXIT_IPS.clear()
    lanes: dict[str, list[dict[str, Any]]] = {}
    for account in accounts:
        lanes.setdefault(lane_key(account, args.no_proxy), []).append(account)

    workers = min(args.parallel, len(lanes))
    lane_summary = ", ".join(f"{key}={len(value)}" for key, value in lanes.items())
    log(f"Connection scheduler: accounts={len(accounts)}, lanes={len(lanes)}, parallel_lanes={workers}", "OK")
    log(f"Lanes: {lane_summary}", "INFO")

    failures = 0
    if workers <= 1:
        for key, lane_accounts in lanes.items():
            failures += int(run_lane(args, key, lane_accounts).get("failed") or 0)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="connection-lane") as pool:
            futures = [pool.submit(run_lane, args, key, lane_accounts) for key, lane_accounts in lanes.items()]
            for future in as_completed(futures):
                try:
                    failures += int(future.result().get("failed") or 0)
                except Exception as exc:
                    failures += 1
                    log(f"Connection lane crashed: {type(exc).__name__}: {exc}", "ERROR")
    log(f"Connection scheduler finished: failures={failures}", "OK" if failures == 0 else "WARNING")
    update_task_receipt(
        args.workflow_run_id,
        scheduler_state="exiting",
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
