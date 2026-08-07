#!/usr/bin/env python3
"""Instagram Web Upload engine for BBBOOOT.

Desktop-first browser uploader with compact live dumps and warmup/post-warmup
logic derived from the real-device workflow philosophy:

- persistent browser profile per account
- pre-upload warmup
- upload through Instagram web UI
- post-upload warmup
- stop on login/checkpoint/challenge/suspicious screens
- one live dump overwritten on every step, bounded important snapshots only

This engine is intentionally separate from ADB/real-phone upload and instagrapi.
"""
from __future__ import annotations

from instagram_consent_flow import resolve_instagram_consent
from instagram_dialog_gate import continue_after_dialog
from instagram_auth_goal import (
    AUTHENTICATED_CONFIRMED,
    TRANSITIONING,
    continue_authentication_goal,
)
from browser_page_router import attach_page_router, router_for_page

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from publishing_history import (
    create_history, finalize_publication_attempt, mark_failed, mark_uploaded,
    persist_reel_publish_intent, preserve_verified_publication_job,
    record_reel_share_click, update_history,
)
from content_plans import next_plan_set, advance_plan, complete_plan_item, ensure_plan_schema
from disk_safety import DiagnosticWriter
from lifecycle_recovery import IndependentHeartbeat, classify_blank_document
from instagram_publish_observer import PublishObserver
from instagram_publish_goal import (
    PublishActionType,
    PublishGoal,
    PublishGoalController,
    PublishObservedState,
)
from instagram_publish_success import PublishSuccessObserver, cleanup_success_dialog
from publication_slots import prepare_publication_slots, slot_progress
from browser_workflow_goal import (
    ACTION_PERFORMED,
    RECONCILIATION_REQUIRED,
    STABLE_BLOCKER,
    BrowserWorkflowResult,
)

try:
    import ig_signals
except Exception:
    ig_signals = None

try:
    from ig_human import make_human
except Exception:
    make_human = None

try:
    from ig_network_capture import start_instagram_network_capture
except Exception:
    start_instagram_network_capture = None

try:
    from browser_launcher import (
        open_spark_browser,
        save_browser_state,
        storage_state_path as sparkbrowser_state_path,
        active_profile_dir as sparkbrowser_profile_dir,
        get_profile_runtime as sparkbrowser_runtime,
    )
except Exception:
    open_spark_browser = None
    save_browser_state = None
    sparkbrowser_state_path = None
    sparkbrowser_profile_dir = None
    sparkbrowser_runtime = None

ROOT = Path(__file__).resolve().parent
# Writable data root on the client/frozen app (the .app bundle is read-only);
# set by the agent/worker via SPARKGRID_DATA_DIR. Falls back to repo dir in dev.
_DATA_ROOT = Path(os.environ["SPARKGRID_DATA_DIR"]) if os.environ.get("SPARKGRID_DATA_DIR") else ROOT
DB_PATH = _DATA_ROOT / "bot.db"
try:
    import geoip2  # present only when camoufox[geoip] extra is installed
    _GEOIP_OK = True
except Exception:
    _GEOIP_OK = False
CONTENT_TABLE = "api_content_assets"
DEBUG_ROOT = _DATA_ROOT / "ai_content_data" / "debug" / "ig_web_upload"
PROFILE_ROOT = _DATA_ROOT / "browser_profiles" / "ig_web_upload"

# SCALE-mode schedule (per-account, same video reused):
#   cycle 1 -> post 1, rest 6h, then 3 per cycle with 6h between each cycle.
SCALE_FIRST_CYCLE_POSTS = 3
SCALE_STEADY_POSTS = 3
SCALE_COOLDOWN_HOURS = 6.0

RESET = "\033[0m"
COLORS = {"OK": "\033[92m", "ERROR": "\033[91m", "WARNING": "\033[93m", "INFO": "\033[94m"}

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
MOBILE_LIKE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
FORCE_IG_ENGLISH = True


_PRE_WARMUP_LOGIN_STATES = {"login", "login_required"}
_PRE_WARMUP_MANUAL_STATES = {
    "blocked", "checkpoint", "challenge", "rate_limited", "restricted",
    "restriction", "suspended", "disabled", "consent_required",
    "human_verification", "two_factor_required",
}
_PRE_WARMUP_INFRA_STATES = {
    "blank_document", "browser_unavailable", "page_closed", "proxy_dead",
    "proxy_failed", "proxy_unavailable",
}
_PRE_WARMUP_SOFT_STATES = {
    "", "dialog_failure", "reels_unavailable", "transitioning", "unknown",
    "unknown_popup",
}


def pre_warmup_policy(warm: Dict[str, Any]) -> Dict[str, Any]:
    """Classify the optional Publish warmup without crossing into composer work."""
    if warm.get("ok"):
        return {"continue": True, "skipped": False, "warning": ""}

    state = str(
        warm.get("state") or warm.get("error") or warm.get("failure_kind") or ""
    ).strip().lower()
    authenticated = bool(
        warm.get("authenticated")
        or warm.get("auth_confirmed")
        or warm.get("operationally_ready")
    )

    if state in _PRE_WARMUP_LOGIN_STATES:
        return {
            "continue": False,
            "skipped": False,
            "status": "manual_required",
            "step": "pre-warmup login_required",
            "error": "login_required",
        }
    if state in _PRE_WARMUP_MANUAL_STATES:
        return {
            "continue": False,
            "skipped": False,
            "status": "manual_required",
            "step": f"pre-warmup {state}",
            "error": state,
        }
    if state in _PRE_WARMUP_INFRA_STATES or warm.get("hard_failure"):
        typed = state if state in _PRE_WARMUP_INFRA_STATES else "browser_unavailable"
        return {
            "continue": False,
            "skipped": False,
            "status": "failed",
            "step": f"pre-warmup {typed}",
            "error": typed,
        }
    if authenticated:
        return {
            "continue": True,
            "skipped": True,
            "warning": "pre_warmup_skipped",
            "reason": state or "warmup_action_unavailable",
        }

    # Generic unknown/transition/dialog outcomes are not manual-action proof.
    # Without authenticated evidence they also cannot authorize Create/Share.
    return {
        "continue": False,
        "skipped": False,
        "status": "failed",
        "step": "pre-warmup auth_unconfirmed",
        "error": state if state in _PRE_WARMUP_SOFT_STATES else (
            state or "auth_unconfirmed"
        ),
    }


def continue_warmup_auth_transition(
    page,
    dump,
    reel_snapshot,
    *,
    attempts: int = 3,
    timeout_seconds: float = 2.0,
    sleep=time.sleep,
) -> Dict[str, Any]:
    """Bound auth transitions without turning them into manual blockers.

    Warmup has its own operational readiness signal: an exposed Reel video.
    A generic authentication observation may still be transitioning while that
    surface finishes rendering. Only an explicit terminal auth result can
    leave this adapter as a blocker; otherwise every retry is a fresh read.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "state": "transitioning",
        "goal": TRANSITIONING,
        "manual_required": False,
    }
    attempt_count = max(1, int(attempts))
    for epoch in range(1, attempt_count + 1):
        result = continue_authentication_goal(
            page,
            timeout_seconds=timeout_seconds,
            optional_cleanup=True,
        )
        snapshot = reel_snapshot()
        if result.get("ok"):
            return result
        state = str(result.get("state") or "").lower()
        if snapshot.get("video") and state in {"", "transitioning", "unknown"}:
            dump.capture(
                page,
                "warmup_auth_transition",
                action=(
                    f"fresh observation epoch={epoch}; "
                    f"goal={result.get('goal') or TRANSITIONING}; "
                    "visible_reel=authenticated"
                ),
            )
            return {
                **result,
                "ok": True,
                "state": "warmup_ready",
                "manual_required": False,
                "operationally_ready": True,
                "warmup_surface": "visible_reel",
                "fresh_reobserve_epoch": epoch,
            }
        if result.get("goal") != TRANSITIONING:
            return result
        dump.capture(
            page,
            "warmup_auth_transition",
            action=f"fresh observation epoch={epoch}; goal={TRANSITIONING}",
        )
        if epoch < attempt_count:
            sleep(0.5)
    return result


def log(msg: str, level: str = "INFO") -> None:
    from log_config import log_to_file_and_print
    log_to_file_and_print("browser", msg, level)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip().lstrip("@"))[:90] or "account"


def db_conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def ensure_schema() -> None:
    c = db_conn()
    try:
        account_cols = _cols(c, "accounts")
        for col, ddl in [
            ("web_upload_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("web_upload_mode", "TEXT NOT NULL DEFAULT 'desktop'"),
            ("web_upload_last_error", "TEXT NOT NULL DEFAULT ''"),
            ("web_upload_last_upload_at", "TEXT NOT NULL DEFAULT ''"),
            ("web_upload_cooldown_until", "TEXT NOT NULL DEFAULT ''"),
            # Per-account content strategy (SCALE = reuse one video with a 1->3
            # ramp + 6h cooldown between cycles; QUALITY = 1 unique video per run).
            ("web_upload_content_mode", "TEXT NOT NULL DEFAULT 'scale'"),
            ("web_upload_cycle_count", "INTEGER NOT NULL DEFAULT 0"),
            ("web_upload_next_cycle_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if account_cols and col not in account_cols:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
        # Content filter: which assets are for scale vs quality accounts.
        content_cols = _cols(c, CONTENT_TABLE)
        if content_cols and "content_kind" not in content_cols:
            c.execute(f"ALTER TABLE {CONTENT_TABLE} ADD COLUMN content_kind TEXT NOT NULL DEFAULT 'scale'")
            content_cols.add("content_kind")
        if content_cols and "quality_position" not in content_cols:
            c.execute(f"ALTER TABLE {CONTENT_TABLE} ADD COLUMN quality_position INTEGER NOT NULL DEFAULT 0")
        ensure_plan_schema(c)
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
        job_cols = _cols(c, "ig_web_upload_jobs")
        for column in (
            "campaign_run_identity",
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


def normalise_accounts(raw: str) -> List[str]:
    out: List[str] = []
    for line in str(raw or "").replace(",", "\n").splitlines():
        name = line.strip().split("|")[0].split(":")[0].strip().lstrip("@")
        if name and name not in out:
            out.append(name)
    return out


class LiveDump:
    def __init__(self, run_id: str, account: str, max_snapshots: int = 40):
        self.run_id = run_id
        self.account = safe_name(account)
        self.root = DEBUG_ROOT / run_id / self.account
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_snapshots = int(max_snapshots or 40)
        self.last_state = ""
        self.actions_file = self.root / "actions.jsonl"
        self.writer = DiagnosticWriter(self.root)
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
            workflow="upload",
            role="upload_worker",
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
        DEBUG_ROOT.mkdir(parents=True, exist_ok=True)
        (DEBUG_ROOT / "latest_run.txt").write_text(run_id, encoding="utf-8")
        (DEBUG_ROOT / "latest_account.txt").write_text(self.account, encoding="utf-8")
        self._heartbeat("worker_ready")

    def _heartbeat(self, state: str) -> None:
        self.liveness.update_phase(state)

    def heartbeat(self, state: str) -> None:
        """Refresh watchdog liveness without taking a screenshot."""
        self._heartbeat(state)

    def _visible_text(self, page) -> str:
        try:
            return (page.locator("body").inner_text(timeout=1600) or "")[:12000]
        except Exception as exc:
            return f"<visible text unavailable: {exc}>"

    def capture(self, page, state: str, action: str = "", error: str = "", force_snapshot: bool = False) -> None:
        self._heartbeat(state)
        if self.writer.disabled:
            return
        payload = {"run_id": self.run_id, "account": self.account, "state": state, "action": action, "error": error, "url": "", "ts": now_iso()}
        try:
            payload["url"] = str(page.url or "").split("?", 1)[0].split("#", 1)[0]
        except Exception:
            pass
        try:
            page.screenshot(path=str(self.root / "latest.png"), full_page=False)
        except Exception as exc:
            payload["screenshot_error"] = str(exc)
        text = self._visible_text(page)
        if not self.writer.write_text(self.root / "latest_text.txt", text): return
        if not self.writer.write_text(self.root / "latest_state.json", json.dumps(payload, ensure_ascii=False, indent=2)): return
        if not self.writer.append_text(self.actions_file, json.dumps(payload, ensure_ascii=False) + "\n"): return
        if force_snapshot or error or state != self.last_state:
            self.last_state = state
            stamp = datetime.now().strftime("%H%M%S")
            snap_dir = self.root / "snapshots"
            snap_dir.mkdir(exist_ok=True)
            base = f"{stamp}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', state)[:45]}"
            try:
                shutil.copy2(self.root / "latest.png", snap_dir / f"{base}.png")
            except Exception:
                pass
            if not self.writer.write_text(snap_dir / f"{base}.json", json.dumps(payload, ensure_ascii=False, indent=2)): return
            snaps = sorted([p for p in snap_dir.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime)
            overflow = max(0, len(snaps) - self.max_snapshots * 2)
            for p in snaps[:overflow]:
                try:
                    p.unlink()
                except Exception:
                    pass

    def capture_safe_dom(self, page, label: str) -> str:
        """Persist a credential-free DOM snapshot for terminal page evidence."""
        if self.writer.disabled:
            return ""
        try:
            html = page.evaluate("""() => {
              const root=document.documentElement.cloneNode(true);
              const allowed=new Set(['role','type','name','placeholder','autocomplete',
                'maxlength','disabled','aria-label','aria-live','aria-busy',
                'aria-disabled','aria-hidden','aria-modal']);
              root.querySelectorAll('*').forEach(el => {
                [...el.attributes].forEach(a => { if(!allowed.has(a.name.toLowerCase())) el.removeAttribute(a.name); });
              });
              root.querySelectorAll('input,textarea').forEach(el => {
                el.removeAttribute('value'); el.setAttribute('value','[redacted]'); el.textContent='';
              });
              root.querySelectorAll('script,style,link,img,video,audio,source').forEach(el=>el.remove());
              return '<!doctype html>\\n'+root.outerHTML;
            }""")
        except Exception:
            return ""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "page"))[:60]
        target = self.root / "snapshots" / f"{datetime.now().strftime('%H%M%S')}_{safe}.html"
        target.parent.mkdir(exist_ok=True)
        return str(target) if self.writer.write_text(target, str(html or "")) else ""


def selected_accounts(names: List[str]) -> List[dict]:
    ensure_schema()
    c = db_conn()
    try:
        where = "WHERE COALESCE(enabled,1)=1 AND COALESCE(warm_only,0)=0"
        cols = _cols(c, "accounts")
        if "web_upload_enabled" in cols:
            where += " AND COALESCE(web_upload_enabled,1)=1"
        params: List[str] = []
        if names:
            ph = ",".join(["?"] * len(names))
            where += f" AND name IN ({ph})"
            params.extend(names)
        rows = c.execute(f"SELECT * FROM accounts {where} ORDER BY name", params).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def reserve_asset(account: str, kind: Optional[str] = None) -> Optional[dict]:
    """Pick a 'ready' asset for the account (or the shared pool).

    `kind` filters by content_kind ('scale'/'quality') when that column exists,
    so scale accounts only draw scale videos and quality accounts only draw
    quality videos. Selection is deterministic (lowest id) so a scale account
    keeps reusing the SAME video across cycles.
    """
    c = db_conn()
    try:
        if CONTENT_TABLE not in {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            return None
        cols = _cols(c, CONTENT_TABLE)
        account_col = "account_name" if "account_name" in cols else ""
        file_col = "file_path" if "file_path" in cols else ("path" if "path" in cols else "")
        if not file_col:
            return None
        caption_col = "caption" if "caption" in cols else "''"
        kind_clause = ""
        params: List[object] = []
        if kind and "content_kind" in cols:
            kind_clause = " AND COALESCE(content_kind,'scale')=?"
        if account_col:
            sql = f"""
                SELECT id, {file_col} AS file_path, {caption_col} AS caption
                FROM {CONTENT_TABLE}
                WHERE (status='ready' OR (?='scale' AND status='uploaded'))
                  AND (COALESCE({account_col},'')='' OR {account_col}=?){kind_clause}
                ORDER BY CASE WHEN status='ready' THEN 0 ELSE 1 END,
                         CASE WHEN {account_col}=? THEN 0 ELSE 1 END,
                         CASE WHEN ?='quality' AND COALESCE(quality_position,0)>0 THEN quality_position ELSE id END,
                         id
                LIMIT 1
            """
            params = [kind or "", account]
            if kind_clause:
                params.append(kind)
            params.append(account)
            params.append(kind or "")
        else:
            sql = f"""
                SELECT id, {file_col} AS file_path, {caption_col} AS caption
                FROM {CONTENT_TABLE}
                WHERE (status='ready' OR (?='scale' AND status='uploaded')){kind_clause}
                ORDER BY CASE WHEN status='ready' THEN 0 ELSE 1 END,
                         CASE WHEN ?='quality' AND COALESCE(quality_position,0)>0 THEN quality_position ELSE id END, id LIMIT 1
            """
            if kind_clause:
                params = [kind, kind, kind]
            else:
                params = [kind or "", kind or ""]
        row = c.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def mark_asset(asset_id: int, status: str, account: str = "") -> None:
    c = db_conn()
    try:
        cols = _cols(c, CONTENT_TABLE)
        if not cols:
            return
        set_parts = ["status=?"]
        vals: List[object] = [status]
        if "updated_at" in cols:
            set_parts.append("updated_at=datetime('now')")
        if status == "uploaded":
            if "uploaded_at" in cols:
                set_parts.append("uploaded_at=datetime('now')")
            if "account_name" in cols and account:
                set_parts.append("account_name=?")
                vals.append(account)
        vals.append(int(asset_id))
        c.execute(f"UPDATE {CONTENT_TABLE} SET {', '.join(set_parts)} WHERE id=?", vals)
        c.commit()
    finally:
        c.close()


def create_job(
    run_id: str,
    account: str,
    mode: str,
    provider: str,
    target: int,
    debug_dir: str,
    *,
    campaign_run_identity: str = "",
    posted_count: int = 0,
) -> int:
    c = db_conn()
    try:
        cur = c.execute("""
            INSERT INTO ig_web_upload_jobs(
                run_id,account_name,mode,provider,status,target_uploads,posted_count,
                current_step,debug_dir,campaign_run_identity,started_at,updated_at
            )
            VALUES (?, ?, ?, ?, 'running', ?, ?, 'starting', ?, ?, datetime('now'), datetime('now'))
        """, (
            run_id, account, mode, provider, int(target), int(posted_count),
            debug_dir, str(campaign_run_identity or ""),
        ))
        c.commit()
        return int(cur.lastrowid)
    finally:
        c.close()


def update_job(job_id: int, **kw) -> None:
    if not kw:
        return
    c = db_conn()
    try:
        if "status" in kw and "domain_outcome" not in kw:
            kw["domain_outcome"] = str(kw["status"] or "")
        cols, vals = [], []
        for k, v in kw.items():
            cols.append(f"{k}=?")
            vals.append(v)
        cols.append("updated_at=datetime('now')")
        vals.append(int(job_id))
        c.execute(f"UPDATE ig_web_upload_jobs SET {', '.join(cols)} WHERE id=?", vals)
        c.commit()
    finally:
        c.close()


def update_account(account: str, **kw) -> None:
    if not kw:
        return
    c = db_conn()
    try:
        cols_avail = _cols(c, "accounts")
        cols, vals = [], []
        for k, v in kw.items():
            if k in cols_avail:
                cols.append(f"{k}=?")
                vals.append(v)
        if not cols:
            return
        if "updated_at" in cols_avail:
            cols.append("updated_at=datetime('now')")
        vals.append(account)
        c.execute(f"UPDATE accounts SET {', '.join(cols)} WHERE name=?", vals)
        c.commit()
    finally:
        c.close()


def require_playwright():
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError  # type: ignore
        return sync_playwright, PlaywrightTimeoutError
    except Exception as exc:
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
        except Exception:
            return {"server": proxy}
    parts = proxy.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return {"server": f"http://{host}:{port}", "username": user, "password": password}
    if len(parts) == 2:
        host, port = parts
        return {"server": f"http://{host}:{port}"}
    return {"server": proxy}


def _profile_storage_state_path(account: str, mode: str = "desktop") -> Path:
    if sparkbrowser_state_path:
        return sparkbrowser_state_path(account, _account_proxy_from_db(account), mode)
    p = PROFILE_ROOT / safe_name(account) / mode
    p.mkdir(parents=True, exist_ok=True)
    return p / "camoufox_storage_state.json"


def _account_proxy_from_db(account: str) -> str:
    try:
        c = db_conn()
        try:
            cols = _cols(c, "accounts")
            if "proxy" in cols:
                row = c.execute("SELECT COALESCE(proxy,'') AS proxy FROM accounts WHERE name=?", (account,)).fetchone()
                return str(row["proxy"] or "") if row else ""
            if "proxy_url" in cols:
                row = c.execute("SELECT COALESCE(proxy_url,'') AS proxy FROM accounts WHERE name=?", (account,)).fetchone()
                return str(row["proxy"] or "") if row else ""
        finally:
            c.close()
    except Exception:
        pass
    return ""


def _open_camoufox_context(account: str, mode: str, headless: bool, no_proxy: bool = False):
    proxy_raw = "" if no_proxy else _account_proxy_from_db(account)
    if not open_spark_browser:
        raise RuntimeError("SparkBrowser launcher is not available")
    cm, context, page = open_spark_browser(account, proxy_raw, mode=mode, headless=headless, humanize=False)
    try:
        setattr(context, "_sparkgrid_proxy", proxy_raw)
    except Exception:
        pass
    return cm, context, page


def _save_camoufox_state(context, account: str, mode: str = "desktop"):
    try:
        proxy_raw = getattr(context, "_sparkgrid_proxy", _account_proxy_from_db(account))
        if save_browser_state:
            return save_browser_state(context, account, proxy_raw, mode)
        storage_path = _profile_storage_state_path(account, mode)
        context.storage_state(path=str(storage_path))
        return str(storage_path)
    except Exception:
        return ""

def open_context(p, account: str, mode: str, provider: str, headless: bool, no_proxy: bool = False):
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    profile_dir = str(PROFILE_ROOT / safe_name(account) / mode)
    is_mobile_like = mode == "mobile_like"

    if provider == "camoufox":
        try:
            cm, context, page = _open_camoufox_context(account, mode, headless, no_proxy=no_proxy)
            proxy_raw = "" if no_proxy else _account_proxy_from_db(account)
            log(f"{account}: SparkBrowser opened; profile={sparkbrowser_profile_dir(account, proxy_raw, mode) if sparkbrowser_profile_dir else profile_dir}", "OK")
            return cm, context, page
        except Exception as exc:
            raise RuntimeError(f"SparkBrowser launch failed: {exc}") from exc

    args = ["--disable-notifications", "--use-fake-ui-for-media-stream"]
    proxy_raw = "" if no_proxy else _account_proxy_from_db(account)
    runtime = sparkbrowser_runtime(account, proxy_raw, mode) if sparkbrowser_runtime else {}
    kwargs = dict(
        headless=headless,
        accept_downloads=True,
        locale=runtime.get("locale") or "en-US",
        args=args,
    )
    if runtime.get("timezone_id"):
        kwargs["timezone_id"] = runtime.get("timezone_id")
    proxy_cfg = _parse_proxy_for_browser(proxy_raw)
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    if is_mobile_like:
        kwargs.update(dict(
            viewport=runtime.get("viewport") or {"width": 390, "height": 844},
            device_scale_factor=runtime.get("device_scale_factor") or 3,
            is_mobile=True, has_touch=True, user_agent=MOBILE_LIKE_UA,
        ))
    else:
        kwargs.update(dict(
            viewport=runtime.get("viewport") or {"width": 1280, "height": 720},
            device_scale_factor=runtime.get("device_scale_factor") or 1,
            user_agent=DESKTOP_UA,
        ))
    ctx = p.chromium.launch_persistent_context(profile_dir, **kwargs)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return None, ctx, page


def visible_text(page) -> str:
    try:
        return (page.locator("body").inner_text(timeout=2000) or "")[:10000]
    except Exception:
        return ""


def manual_signal(page) -> str:
    """Compatibility view of the typed pre-Create classifier."""
    state = classify_pre_create_state(page).get("state", "")
    if state == "login_required":
        return "login"
    return state if state not in {"ready", "blank_document", "page_closed", "browser_unavailable"} else ""


def classify_pre_create_state(page, dump: LiveDump | None = None) -> Dict[str, str]:
    """Classify the visible page before *any* Create discovery or click.

    This deliberately uses visible state as the permission boundary.  An
    authenticated endpoint/cookie may corroborate readiness elsewhere, but it
    must not authorize Create while the rendered page is login, 2FA, challenge
    or a blocking dialog.
    """
    def result(state: str) -> Dict[str, str]:
        return {"state": state, "status": state.upper(), "error": state}

    try:
        if page is None or bool(page.is_closed()):
            return result("page_closed")
    except AttributeError:
        if page is None:
            return result("browser_unavailable")
    except Exception:
        return result("page_closed")
    try:
        # Playwright raises when the context/browser process has gone away.
        _ = page.context.pages
    except AttributeError:
        pass
    except Exception:
        return result("browser_unavailable")

    auth_goal = continue_authentication_goal(
        page, timeout_seconds=0.0, optional_cleanup=True
    )
    if auth_goal.get("ok"):
        return result("ready")
    if auth_goal.get("state") in {
        "login_required", "two_factor_required", "checkpoint",
        "restricted", "suspended", "unknown_popup",
    }:
        return result(str(auth_goal["state"]))

    url = str(getattr(page, "url", "") or "").lower()
    if "/accounts/login" in url or "/accounts/emailsignup" in url:
        return result("login_required")
    if "two_step_verification" in url or "two_factor" in url:
        return result("two_factor_required")
    if "/checkpoint" in url:
        return result("checkpoint")
    if "/challenge" in url:
        return result("challenge")
    if "/accounts/suspended" in url or "/disabled" in url:
        return result("suspended" if "suspend" in url else "disabled")
    dialog = continue_after_dialog(page, allow_safe_close=True)
    if dialog.get("state"):
        return result("blocking_dialog_not_dismissed")
    text = visible_text(page)
    if not text.strip():
        # Keep the legacy fake-page compatibility path, but require the full
        # evidence gate whenever Playwright can provide it.
        detail = classify_blank_document(page, navigation_started=True)
        state = str(detail.get("state") or "lifecycle_state_unknown")
        if state == "renderer_unavailable" and not hasattr(page, "evaluate"):
            state = "blank_document"
        return result(state)
    try:
        password = page.locator("input[type='password'], input[name='password'], input[autocomplete='current-password']")
        count = getattr(password, "count", None)
        present = int(count() or 0) > 0 if callable(count) else bool(password.is_visible(timeout=500))
        if present:
            return result("login_required")
    except Exception:
        pass
    lowered = text.lower()
    if any(value in lowered for value in ("we suspended your account", "account has been suspended")):
        return result("suspended")
    if any(value in lowered for value in ("account disabled", "your account was disabled")):
        return result("disabled")
    # Footer navigation often contains Log in / Sign up on an authenticated
    # Reels page.  URL and credential-form evidence above are authoritative;
    # this remaining phrase is an explicit unauthenticated surface.
    if "enter your password" in lowered:
        return result("login_required")
    return result("ready")


def submitted_unverified_result(observation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    error = "share clicked but Instagram did not confirm publication; verify the profile before retry"
    return {"ok": False, "status": "UPLOADED_UNVERIFIED", "error": error, "observation": dict(observation or {})}


def partial_success_after_warmup(posted: int, target: int, error: str) -> Dict[str, Any]:
    return {
        "status": "partial_success",
        "current_step": f"posted {int(posted)}/{int(target)}; post-warmup stopped",
        "posted_count": int(posted),
        "last_error": str(error or "post-warmup failed"),
        "finished_at": now_iso(),
    }


def jitter(min_s=0.7, max_s=2.2):
    time.sleep(random.uniform(float(min_s), float(max_s)))



def _human_event_sink(dump: LiveDump | None):
    if dump is None:
        return None
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
        except Exception:
            pass

    dump._human_event_sink = sink
    return sink


def _human(page, account: str = "", dump: LiveDump | None = None):
    if make_human is None:
        return None
    try:
        actor = make_human(
            page,
            account or (dump.account if dump is not None else "instagram_web"),
            event_sink=_human_event_sink(dump),
        )
        if dump is not None and not bool(getattr(dump, "_human_announced", False)):
            dump._human_announced = True
            status = {
                "active": True,
                "profile": getattr(getattr(actor, "profile", None), "name", ""),
                "visible_cursor": bool(getattr(actor, "_cursor_enabled", False)),
                "speed_multiplier": float(getattr(actor, "_speed_multiplier", 1.0)),
                "note": "browser-level Playwright pointer; macOS system cursor does not move",
            }
            try:
                (dump.root / "human_status.json").write_text(
                    json.dumps(status, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass
            log(
                f"{dump.account}: HumanInteractor active; "
                f"profile={status['profile']}; visible_cursor={status['visible_cursor']}; "
                f"speed={status['speed_multiplier']:.2f}",
                "OK",
            )
        return actor
    except Exception as exc:
        if dump is not None:
            try:
                (dump.root / "human_status.json").write_text(
                    json.dumps({"active": False, "error": type(exc).__name__}, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass
            log(f"{dump.account}: HumanInteractor unavailable: {type(exc).__name__}", "WARNING")
        return None

def human_mouse(page, moves: int = 1, account: str = ""):
    actor = _human(page, account)
    if actor is not None:
        try:
            actor.wander(max(1, int(moves or 1)))
            return
        except Exception:
            pass
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        page.mouse.move(
            random.randint(80, max(81, int(vp["width"]) - 80)),
            random.randint(120, max(121, int(vp["height"]) - 80)),
        )
    except Exception:
        pass


def goto_fast(page, url: str, timeout: int = 18000) -> None:
    """Navigate without waiting for Instagram's long-running app shell to settle."""
    try:
        page.goto(url, wait_until="commit", timeout=timeout)
        jitter(2.0, 3.5)
        return
    except Exception:
        pass
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=min(timeout, 12000))
        jitter(2.0, 3.5)
    except Exception:
        # If the browser visibly navigated but Playwright kept waiting, keep going.
        jitter(1.0, 2.0)


def force_english(page, dump: LiveDump | None = None) -> None:
    if not FORCE_IG_ENGLISH:
        return
    try:
        page.evaluate("""() => {
            try { localStorage.setItem('IG_HAS_SEEN_LANGUAGE_TOOLTIP', 'true'); } catch (e) {}
            try { document.cookie = 'ig_lang=en; path=/; domain=.instagram.com; max-age=31536000'; } catch (e) {}
        }""")
    except Exception:
        pass
    try:
        current = str(getattr(page, "url", "") or "")
        if "instagram.com" in current and "hl=en" not in current:
            sep = "&" if "?" in current else "?"
            goto_fast(page, current + sep + "hl=en", timeout=12000)
            if dump:
                dump.capture(page, "upload_force_english", "normalized IG UI to hl=en")
    except Exception:
        pass


def click_by_candidates(page, labels: List[str], timeout: int = 4000) -> bool:
    for label in labels:
        candidates = [
            lambda lab=label: page.get_by_role("button", name=re.compile(lab, re.I)),
            lambda lab=label: page.get_by_role("link", name=re.compile(lab, re.I)),
            lambda lab=label: page.get_by_text(re.compile(lab, re.I)),
            lambda lab=label: page.locator(f"[aria-label*='{lab}' i]").first,
        ]
        for getter in candidates:
            try:
                loc = getter().first
                actor = _human(page)
                clicked = actor.click(loc, timeout=timeout) if actor is not None else False
                if not clicked:
                    human_mouse(page)
                    loc.click(timeout=timeout)
                jitter()
                return True
            except Exception:
                continue
    return False



def resolve_consent_for_workflow(page, dump: LiveDump | None, label: str) -> dict:
    def capture(current_page, step: str, detail: str) -> None:
        if dump is not None:
            dump.capture(current_page, step, f"{label}: {detail}", force_snapshot=step == "consent_unresolved")
    return resolve_instagram_consent(page, capture, max_seconds=35)


def _legacy_dismiss_instagram_prompts(page, dump: LiveDump | None = None, label: str = "prompt") -> bool:
    """Compatibility-only legacy prompt helper; ISSUE-010 does not call it."""
    body_text = visible_text(page).lower()
    if any(s in body_text for s in ["create new post", "drag photos and videos here", "select from computer"]):
        return False

    skip_re = (
        r"^(not now|skip|cancel|plus tard|pas maintenant|ahora no|más tarde|mas tarde|"
        r"nicht jetzt|später|spaeter|non ora|più tardi|piu tardi|agora não|agora nao|"
        r"не сейчас|позже)$"
    )
    clicked_any = False
    for i in range(4):
        body_text = visible_text(page).lower()
        clicked = click_visible_text_js(page, skip_re, f"{label}-skip")

        if not clicked:
            break
        clicked_any = True
        if dump:
            dump.capture(page, f"{label}_{i+1}", "dismissed instagram prompt with pointer")
        jitter(0.8, 1.6)
    return clicked_any


def dismiss_instagram_prompts(page, dump: LiveDump | None = None, label: str = "prompt") -> bool:
    """Apply the shared semantic dialog contract; no coordinate fallback."""
    result = continue_after_dialog(page, allow_safe_close=True)
    if dump and result.get("present"):
        dump.capture(page, label, "semantic Instagram dialog gate")
    return not bool(result.get("state"))


_CREATE_NAVIGATION_TERMS = (
    "create", "new post", "new publication", "create post",
    "crear", "nueva publicacion", "crear publicacion",
    "creer", "nouvelle publication", "creer une publication",
    "erstellen", "beitrag erstellen", "neuer beitrag",
    "crea", "nuovo post", "crea post",
    "criar", "nova publicacao", "criar publicacao",
    "создать", "новая публикация", "создать публикацию",
)
_KNOWN_NAVIGATION_TERMS = (
    "home", "inicio", "accueil", "startseite", "главная",
    "search", "buscar", "recherche", "suche", "поиск",
    "explore", "explorar", "entdecken", "интересное",
    "reels", "messages", "mensajes", "nachrichten", "сообщения",
    "notifications", "notificaciones", "benachrichtigungen", "уведомления",
    "profile", "perfil", "profil", "профиль",
)
_NAVIGATION_TOGGLE_TERMS = (
    "open navigation", "navigation menu", "open menu", "menu",
    "abrir menu", "menu de navigation", "navigationsmenu", "открыть меню",
)


def _fold_accessible_name(value: Any) -> str:
    return " ".join(
        "".join(
            char
            for char in unicodedata.normalize("NFKD", str(value or ""))
            if not unicodedata.combining(char)
        ).casefold().split()
    )


def _semantic_match(value: Any, terms: tuple[str, ...]) -> bool:
    folded = _fold_accessible_name(value)
    return bool(folded and any(
        folded == _fold_accessible_name(term) for term in terms
    ))


def classify_instagram_navigation(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate Create/toggle controls inside Instagram navigation.

    The browser probe supplies only visible clickable nodes and geometry.
    Classification stays deterministic and testable offline.  A generic plus
    in the feed is rejected unless it belongs to an explicit navigation
    container or the left rail is corroborated by several known nav actions.
    """
    candidates = [
        dict(item)
        for item in raw.get("candidates") or ()
        if isinstance(item, dict)
        and bool(item.get("visible"))
        and bool(item.get("enabled", True))
    ]
    viewport = dict(raw.get("viewport") or {})
    viewport_width = max(1.0, float(viewport.get("width") or 1.0))
    rail_limit = max(120.0, min(340.0, viewport_width * 0.20))

    def label(item: Dict[str, Any]) -> str:
        return " ".join(
            str(item.get(key) or "")
            for key in ("accessible_name", "title", "svg_title", "text")
        ).strip()

    def item_matches(item: Dict[str, Any], terms: tuple[str, ...]) -> bool:
        return any(
            _semantic_match(item.get(key), terms)
            for key in ("accessible_name", "title", "svg_title", "text")
        )

    def in_left_rail(item: Dict[str, Any]) -> bool:
        return (
            float(item.get("left") or 0.0) < rail_limit
            and float(item.get("width") or 0.0) <= 340.0
            and float(item.get("height") or 0.0) <= 150.0
        )

    known_rail = {
        _fold_accessible_name(label(item))
        for item in candidates
        if in_left_rail(item)
        and item_matches(item, _KNOWN_NAVIGATION_TERMS)
    }

    def belongs_to_navigation(item: Dict[str, Any]) -> bool:
        return bool(item.get("explicit_navigation")) or (
            in_left_rail(item) and len(known_rail) >= 3
        )

    create = None
    for item in candidates:
        href = str(item.get("href") or "").casefold()
        semantic = item_matches(item, _CREATE_NAVIGATION_TERMS)
        if (
            ("/create/" in href or semantic)
            and belongs_to_navigation(item)
        ):
            create = item
            break

    toggle = None
    for item in candidates:
        if not belongs_to_navigation(item):
            continue
        if item_matches(item, _NAVIGATION_TOGGLE_TERMS) and (
            item.get("aria_controls")
            or item.get("aria_expanded") in {"true", "false", True, False}
            or _fold_accessible_name(label(item)) != "more"
        ):
            toggle = item
            break

    create_text = _fold_accessible_name((create or {}).get("text"))
    if create is not None:
        navigation_mode = "full" if _semantic_match(
            create_text, _CREATE_NAVIGATION_TERMS
        ) else "icon_only"
    elif toggle is not None:
        navigation_mode = "hidden"
    elif candidates:
        navigation_mode = "present_without_create"
    else:
        navigation_mode = "absent"

    structural = {
        "ready_state": str(raw.get("ready_state") or ""),
        "busy": bool(raw.get("busy")),
        "login_form": bool(raw.get("login_form")),
        "app_shell": bool(raw.get("app_shell")),
        "account_menu": bool(raw.get("account_menu")),
        "create_menu_open": bool(raw.get("create_menu_open")),
        "known_rail": sorted(known_rail),
        "navigation_mode": navigation_mode,
        "create_label": _fold_accessible_name(label(create or {})),
        "toggle_label": _fold_accessible_name(label(toggle or {})),
        "candidate_count": len(candidates),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            structural, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        **structural,
        "viewport": {
            "width": int(float(viewport.get("width") or 0)),
            "height": int(float(viewport.get("height") or 0)),
        },
        "create_available": create is not None,
        "create_control": create,
        "navigation_toggle_available": toggle is not None,
        "navigation_toggle": toggle,
        "navigation_collapsed": create is None and toggle is not None,
        "create_menu_open": bool(raw.get("create_menu_open")),
        "authenticated_home": bool(
            not raw.get("login_form")
            and raw.get("app_shell")
            and (raw.get("account_menu") or len(known_rail) >= 3)
        ),
        "fingerprint": fingerprint,
    }


def inspect_instagram_navigation(page) -> Dict[str, Any]:
    """Return a bounded structural probe of the current Instagram shell."""
    try:
        raw = page.evaluate(
            """() => { // IG_PUBLISH_NAVIGATION_OBSERVE
              const visible = (el) => {
                if (!el) return false;
                const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                return r.width>8 && r.height>8 && s.display!=='none' &&
                  s.visibility!=='hidden' && Number.parseFloat(s.opacity||'1')>0.01 &&
                  r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
              };
              const nodes=[...document.querySelectorAll(
                "a,button,[role='button'],[role='link'],[tabindex]"
              )];
              const candidates=[];
              const seen=new Set();
              for(const node of nodes){
                const target=node.closest(
                  "a,button,[role='button'],[role='link'],[tabindex]"
                )||node;
                if(seen.has(target)||!visible(target)) continue;
                seen.add(target);
                const r=target.getBoundingClientRect();
                const nested=[...target.querySelectorAll(
                  "svg[aria-label],svg title,[aria-label],[title]"
                )];
                const svgTitle=nested.map(x =>
                  x.getAttribute('aria-label')||x.getAttribute('title')||
                  x.textContent||''
                ).join(' ');
                const accessible=[
                  target.getAttribute('aria-label')||'',
                  target.getAttribute('title')||'',
                  svgTitle
                ].join(' ').replace(/\\s+/g,' ').trim();
                const text=String(target.innerText||'')
                  .replace(/\\s+/g,' ').trim().slice(0,160);
                const disabled=!!target.disabled ||
                  target.getAttribute('aria-disabled')==='true';
                candidates.push({
                  accessible_name:accessible.slice(0,240),
                  title:String(target.getAttribute('title')||'').slice(0,120),
                  svg_title:svgTitle.slice(0,240), text,
                  href:String(target.getAttribute('href')||'').slice(0,200),
                  role:String(target.getAttribute('role')||target.tagName||''),
                  aria_controls:String(target.getAttribute('aria-controls')||''),
                  aria_expanded:target.getAttribute('aria-expanded'),
                  explicit_navigation:!!target.closest(
                    "nav,[role='navigation'],aside,header"
                  ),
                  visible:true,enabled:!disabled,
                  left:r.left,top:r.top,width:r.width,height:r.height
                });
              }
              const anyVisible = (selector) =>
                [...document.querySelectorAll(selector)].some(visible);
              const actionName=(el)=>[
                el.getAttribute('aria-label')||'',
                el.getAttribute('title')||'',
                el.innerText||''
              ].join(' ').replace(/\\s+/g,' ').trim().toLocaleLowerCase();
              let createMenuOpen=false;
              const actionNodes=nodes.filter(visible);
              for(const node of actionNodes){
                if(!/^(post|new post|reel|create reel)$/.test(actionName(node))) continue;
                if(node.closest("nav,[role='navigation'],aside,header")) continue;
                let parent=node.closest("[role='menu'],[role='dialog'],[aria-modal='true']");
                for(let depth=0;!parent&&node.parentElement&&depth<4;depth++){
                  let current=node.parentElement;
                  for(let step=0;step<depth;step++) current=current&&current.parentElement;
                  if(!current) break;
                  const names=[...current.querySelectorAll(
                    "button,a,[role='button'],[role='menuitem']"
                  )].filter(visible).map(actionName);
                  if(names.some(x=>/^(post|new post)$/.test(x)) &&
                     names.some(x=>/^(reel|create reel)$/.test(x))){
                    parent=current;
                  }
                }
                if(parent){createMenuOpen=true;break;}
              }
              return {
                ready_state:String(document.readyState||''),
                busy:anyVisible(
                  "[aria-busy='true'],[role='progressbar'],"+
                  "svg[aria-label*='loading' i]"
                ),
                login_form:anyVisible(
                  "input[type='password'],input[name='password'],"+
                  "input[autocomplete='current-password']"
                ),
                app_shell:!!document.querySelector('main') &&
                  (anyVisible("nav,[role='navigation'],a[href='/']") ||
                   candidates.filter(x => x.left < Math.max(120,innerWidth*.20)).length>=3),
                account_menu:anyVisible(
                  "nav img[alt*='profile picture' i],"+
                  "a[href*='/accounts/edit'],"+
                  "[aria-label='Profile' i],[aria-label*='profile picture' i]"
                ),
                create_menu_open:createMenuOpen,
                viewport:{width:innerWidth,height:innerHeight},
                candidates:candidates.slice(0,180)
              };
            }"""
        )
        return classify_instagram_navigation(
            dict(raw) if isinstance(raw, dict) else {}
        )
    except Exception as exc:
        return {
            "create_available": False,
            "navigation_collapsed": False,
            "authenticated_home": False,
            "navigation_mode": "probe_failed",
            "fingerprint": "",
            "error": type(exc).__name__,
        }


def _click_structural_control(
    page, control: Dict[str, Any] | None, *, dump: LiveDump | None, label: str
) -> bool:
    """Click a freshly validated DOM control using its current visible box."""
    if not control or not control.get("visible") or not control.get("enabled", True):
        return False
    try:
        left = float(control.get("left"))
        top = float(control.get("top"))
        width = float(control.get("width"))
        height = float(control.get("height"))
        if width <= 8 or height <= 8:
            return False
        x = left + width * 0.5
        y = top + height * 0.5
        actor = _human(page)
        clicked = actor.click_point(x, y) if actor is not None else False
        if not clicked:
            page.mouse.click(x, y)
            clicked = True
        if clicked and dump:
            dump.capture(page, label, "fresh structural navigation control")
        return bool(clicked)
    except Exception:
        return False


def open_instagram_navigation(
    page, dump: LiveDump | None = None
) -> Dict[str, str]:
    probe = inspect_instagram_navigation(page)
    if probe.get("create_available"):
        return {"state": "ready"}
    if not probe.get("navigation_collapsed"):
        return {"state": "navigation_toggle_not_found"}
    if not _click_structural_control(
        page, probe.get("navigation_toggle"), dump=dump,
        label="upload_navigation_opened",
    ):
        return {"state": "navigation_toggle_click_failed"}
    return {"state": "ready"}


def select_instagram_reel_from_create_menu(
    page, dump: LiveDump | None = None
) -> Dict[str, str]:
    labels = (
        "reel", "create reel", "new reel", "video",
        "рилс", "создать reel", "creer un reel", "crear reel",
    )
    try:
        control = page.evaluate(
            """(labels) => { // IG_PUBLISH_CREATE_MENU_REEL
              const wanted=labels.map(x=>String(x).toLocaleLowerCase());
              const visible=(el)=>{
                const r=el.getBoundingClientRect(),s=getComputedStyle(el);
                return r.width>8&&r.height>8&&s.display!=='none'&&
                  s.visibility!=='hidden'&&r.bottom>0&&r.right>0&&
                  r.top<innerHeight&&r.left<innerWidth;
              };
              const name=(el)=>[
                el.getAttribute('aria-label')||'',
                el.getAttribute('title')||'',
                el.innerText||'',
                [...el.querySelectorAll('svg[aria-label],svg title')]
                  .map(x=>x.getAttribute('aria-label')||x.textContent||'').join(' ')
              ].join(' ').replace(/\\s+/g,' ').trim().toLocaleLowerCase();
              const nodes=[...document.querySelectorAll(
                "button,a,[role='button'],[role='menuitem']"
              )].filter(visible);
              for(const node of nodes){
                if(!wanted.includes(name(node))) continue;
                if(node.closest("nav,[role='navigation'],aside,header")) continue;
                let container=node.closest(
                  "[role='dialog'],[role='menu'],[aria-modal='true']"
                );
                if(!container){
                  let parent=node.parentElement;
                  for(let depth=0;parent&&depth<4;depth++,parent=parent.parentElement){
                    const names=[...parent.querySelectorAll(
                      "button,a,[role='button'],[role='menuitem']"
                    )].filter(visible).map(name);
                    if(names.some(x=>/^(post|new post|reel|create reel)$/.test(x)) &&
                       names.length>=2){
                      container=parent;break;
                    }
                  }
                }
                if(!container) continue;
                const r=node.getBoundingClientRect();
                return {
                  accessible_name:name(node),text:String(node.innerText||''),
                  visible:true,enabled:!node.disabled&&
                    node.getAttribute('aria-disabled')!=='true',
                  left:r.left,top:r.top,width:r.width,height:r.height
                };
              }
              return null;
            }""",
            list(labels),
        )
    except Exception:
        control = None
    if _click_structural_control(
        page, control, dump=dump, label="upload_create_reel_selected"
    ):
        if dump:
            dump.capture(page, "upload_create_reel_selected", "semantic menu action")
        return {"state": "ready"}
    return {"state": "create_menu_reel_not_found"}


def open_instagram_create(
    page,
    dump: LiveDump | None = None,
) -> Dict[str, str]:
    """Open one composer intent across direct and existing cluster branches."""
    def create_modal_ready() -> bool:
        txt = visible_text(page).lower()
        return any(s in txt for s in ["create new post", "drag photos and videos here", "select from computer"])

    def wait_create_ready(seconds: float = 4.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if dump:
                dump.heartbeat("upload_wait_create")
            if create_modal_ready():
                return True
            time.sleep(0.25)
        return create_modal_ready()

    def post_attempt() -> bool:
        deadline = time.time() + 4.0
        menu_attempted = False
        while time.time() < deadline:
            if create_modal_ready():
                return True
            navigation = inspect_instagram_navigation(page)
            if navigation.get("create_menu_open") and not menu_attempted:
                menu_attempted = True
                selected = select_instagram_reel_from_create_menu(page, dump)
                if selected.get("state") != "ready":
                    return False
                continue
            if dump:
                dump.heartbeat("upload_wait_create")
            time.sleep(0.25)
        return create_modal_ready()

    if create_modal_ready():
        return {"state": "ready"}
    initial_navigation = inspect_instagram_navigation(page)
    if initial_navigation.get("create_menu_open"):
        selected = select_instagram_reel_from_create_menu(page, dump)
        if selected.get("state") == "ready" and wait_create_ready():
            return {"state": "ready"}

    actor = _human(page)
    create_re = re.compile(
        r"^(create|new post|new publication|créer|creer|crear|создать|erstellen|crea|criar)$",
        re.I,
    )
    direct_candidates = [
        lambda: page.locator("a[href*='/create/select']"),
        lambda: page.get_by_role("link", name=create_re),
        lambda: page.get_by_role("button", name=create_re),
        lambda: page.locator("[aria-label*='Create' i], [title*='Create' i]"),
        lambda: page.locator("svg[aria-label='New post']"),
        lambda: page.locator("a:has(svg[aria-label='New post'])"),
        lambda: page.locator("[aria-label='New post']"),
    ]
    for getter in direct_candidates:
        if dump:
            dump.heartbeat("upload_find_create")
        try:
            loc = getter().first
            visible = bool(
                int(loc.count() or 0) > 0
                and loc.is_visible(timeout=1200)
            )
            if not visible:
                continue
            clicked = actor.click(loc, timeout=3500) if actor is not None else False
            if not clicked:
                loc.click(timeout=3500)
            jitter(0.8, 1.6)
            if dump:
                dump.capture(page, "upload_create_clicked", "locator + pointer")
            if post_attempt():
                return {"state": "ready"}
        except Exception:
            continue

    # The existing navigation classifier handles full, icon-only, collapsed,
    # localized and descendant-SVG variants after the direct fast path misses.
    navigation = inspect_instagram_navigation(page)
    if navigation.get("navigation_collapsed"):
        opened_navigation = open_instagram_navigation(page, dump)
        if opened_navigation.get("state") == "ready":
            navigation = inspect_instagram_navigation(page)
    if navigation.get("create_available"):
        if _click_structural_control(
            page,
            navigation.get("create_control"),
            dump=dump,
            label="upload_create_cluster_clicked",
        ) and post_attempt():
            return {"state": "ready"}

    # Geometry fallback: JS only locates the target. Python delivers the click.
    try:
        result = page.evaluate("""() => {
            const createRe = /(create|new post|new publication|créer|creer|nouvelle publication|crear|nueva publicación|создать|публикац|erstellen|beitrag erstellen|crea|nuovo post|criar|nova publicação)/i;
            const badRe = /(home|search|explore|reels|messages|notifications|profile|more|accueil|recherche|explorer|mensajes|notificaciones|profil|inicio|buscar|explorar|mensajes|notificaciones|perfil|mehr|startseite|suche|profilo|главная|поиск|интересное|сообщения|уведомления|профиль|ещё)/i;
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 12 && r.height > 12 && st.visibility !== 'hidden' && st.display !== 'none' &&
                       r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
            };
            const labelOf = (el) => [
                el.getAttribute('aria-label') || '',
                el.getAttribute('title') || '',
                el.innerText || '',
                el.textContent || '',
                [...el.querySelectorAll('svg,[aria-label],title')].map(x => x.getAttribute('aria-label') || x.textContent || '').join(' '),
                el.getAttribute('href') || ''
            ].join(' ').replace(/\s+/g, ' ').trim();
            const clickable = (el) => el.closest('a,button,[role="button"],[role="link"],[tabindex]') || el;
            const all = [...document.querySelectorAll('a,button,[role="button"],[role="link"],[tabindex]')]
                .filter(visible)
                .map(el => {
                    const target = clickable(el);
                    const r = target.getBoundingClientRect();
                    return {el:target, label:labelOf(target), href:target.getAttribute('href') || '',
                            x:r.left, y:r.top, width:r.width, height:r.height,
                            cx:r.left+r.width/2, cy:r.top+r.height/2};
                });
            let pick = all.find(x => /\/create\/select/i.test(x.href));
            if (!pick) {
                pick = all.find(x => createRe.test(x.label) && !badRe.test(x.label.replace(createRe, '')) &&
                                     x.x < Math.max(320, innerWidth * 0.24) && x.width <= 320 && x.height <= 140);
            }
            if (!pick) {
                const rail = all
                    .filter(x => x.x < Math.max(280, innerWidth * 0.21) && x.width < 300 && x.height < 130 &&
                                 x.cy > innerHeight * 0.30 && x.cy < innerHeight * 0.88)
                    .sort((a,b) => a.cy - b.cy);
                pick = rail.find(x => createRe.test(x.label));
            }
            if (!pick) {
                return {ok:false, candidates:all.filter(x => x.x < 220)
                    .map(x => ({label:x.label, href:x.href, x:Math.round(x.cx), y:Math.round(x.cy)})).slice(0,24)};
            }
            pick.el.scrollIntoView({block:'center', inline:'center'});
            const r = pick.el.getBoundingClientRect();
            return {ok:true, label:pick.label, href:pick.href, x:r.left, y:r.top, width:r.width, height:r.height};
        }""")
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    if result and result.get("ok"):
        try:
            x = float(result["x"]) + float(result["width"]) * random.uniform(0.30, 0.70)
            y = float(result["y"]) + float(result["height"]) * random.uniform(0.28, 0.72)
            if actor is not None:
                clicked = actor.click_point(x, y)
            else:
                page.mouse.click(x, y)
                clicked = True
            if clicked:
                jitter(1.0, 2.0)
                if dump:
                    dump.capture(page, "upload_create_clicked", json.dumps(result, ensure_ascii=False)[:900])
                if post_attempt():
                    return {"state": "ready"}
        except Exception:
            pass

    if dump:
        dump.capture(page, "upload_create_not_found", json.dumps(result or {}, ensure_ascii=False)[:1200])

    return {"state": "create_click_no_transition" if result and result.get("ok") else "create_control_not_found"}

def click_visible_text_js(page, pattern: str, label: str = "text") -> bool:
    """Locate a JS-only text target, then deliver a real pointer click."""
    try:
        box = page.evaluate(
            """({pattern}) => {
                const re = new RegExp(pattern, 'i');
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none' &&
                           r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
                };
                const items = [...document.querySelectorAll('button,[role="button"],a,span,div')]
                    .filter(el => visible(el) && re.test((el.innerText || el.textContent || '').trim()));
                const el = items[items.length - 1];
                if (!el) return null;
                const target = el.closest('button,[role="button"],a') || el;
                target.scrollIntoView({block:'center', inline:'center'});
                const r = target.getBoundingClientRect();
                return {x:r.left, y:r.top, width:r.width, height:r.height};
            }""",
            {"pattern": pattern, "label": label},
        )
        if not box:
            return False
        actor = _human(page)
        if actor is not None:
            x = float(box["x"]) + float(box["width"]) * random.uniform(0.32, 0.68)
            y = float(box["y"]) + float(box["height"]) * random.uniform(0.30, 0.70)
            return actor.click_point(x, y)
        page.mouse.click(float(box["x"]) + float(box["width"]) / 2.0,
                         float(box["y"]) + float(box["height"]) / 2.0)
        return True
    except Exception:
        return False

def click_dialog_label_js(page, labels: List[str], *, prefer_last: bool = True) -> bool:
    """Find a visible dialog action with JS, then click it via HumanInteractor."""
    try:
        box = page.evaluate(
            """({labels, preferLast}) => {
                const lows = labels.map(x => String(x || '').trim().toLowerCase()).filter(Boolean);
                const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none' &&
                           r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
                };
                const textOf = (el) => [
                    el.innerText || '', el.textContent || '',
                    el.getAttribute('aria-label') || '', el.getAttribute('title') || ''
                ].join(' ').replace(/\\s+/g, ' ').trim();
                const dlgs = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(vis);
                const scope = dlgs.length ? dlgs[dlgs.length - 1] : document.body;
                let items = [...scope.querySelectorAll('button,[role="button"],a,[role="link"],div[tabindex]')]
                    .filter(vis)
                    .map(el => ({el, text:textOf(el)}))
                    .filter(x => {
                        const t = x.text.toLowerCase();
                        return lows.some(l => t === l || t.includes(l));
                    });
                if (!items.length) return null;
                const picked = preferLast ? items[items.length - 1] : items[0];
                picked.el.scrollIntoView({block:'center', inline:'center'});
                const r = picked.el.getBoundingClientRect();
                return {x:r.left, y:r.top, width:r.width, height:r.height, text:picked.text};
            }""",
            {"labels": labels, "preferLast": prefer_last},
        )
        if not box:
            return False
        actor = _human(page)
        if actor is not None:
            x = float(box["x"]) + float(box["width"]) * random.uniform(0.30, 0.70)
            y = float(box["y"]) + float(box["height"]) * random.uniform(0.28, 0.72)
            return actor.click_point(x, y)
        page.mouse.click(float(box["x"]) + float(box["width"]) / 2.0,
                         float(box["y"]) + float(box["height"]) / 2.0)
        return True
    except Exception:
        return False

def upload_screen_state(page) -> Dict:
    """Best-effort state machine for Instagram's upload dialog."""
    try:
        return dict(page.evaluate("""() => {
            const vis = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none' &&
                       r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
            };
            const dlgs = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(vis);
            const d = dlgs.length ? dlgs[dlgs.length - 1] : document.body;
            const txt = (d.innerText || d.textContent || '').replace(/\\s+/g, ' ').trim();
            const low = txt.toLowerCase();
            const btnText = [...d.querySelectorAll('button,[role="button"],a,[role="link"],div[tabindex]')]
                .filter(vis).map(el => (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g,' ').trim()).filter(Boolean).slice(-20);
            const hasFile = !!d.querySelector('input[type=file]');
            const hasVideo = !!d.querySelector('video');
            const hasCanvas = !!d.querySelector('canvas');
            const hasSlider = !!d.querySelector('[role=slider],input[type=range]');
            const hasTextbox = !!d.querySelector('[data-lexical-editor],[role=textbox],[contenteditable=true],textarea');
            const hasProgress = !!d.querySelector(
                '[role=progressbar],svg[aria-label="Loading..."],[aria-busy=true]');
            const hasNext = btnText.some(t => /^(next|далее|suivant|weiter|siguiente|avanti|continuar|continue)$/i.test(t));
            const actionEls = [...d.querySelectorAll('button,[role="button"],a,[role="link"],div[tabindex]')].filter(vis);
            const shareEls = actionEls.filter(el => /^(share|post|publish|опубликовать|поделиться|partager|publicar|teilen|condividi)$/i.test(
                (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g,' ').trim()
            ));
            const hasShare = shareEls.length > 0;
            const shareEnabled = shareEls.some(el =>
                !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !el.hasAttribute('disabled'));
            const headingText = [...d.querySelectorAll('[role=heading],h1,h2,h3')]
                .filter(vis).map(el => (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim())
                .filter(Boolean).slice(0, 8);
            let state = 'UNKNOWN';
            if (/couldn.t be shared|could not be shared|your post could not be shared|try again|restricted from uploading/i.test(low)) state = 'SHARE_FAILED';
            else if (hasProgress || /^(sharing|posting|publishing|processing|uploading|preparing|checking)\b/i.test(low)) state = 'PROCESSING';
            else if (/video posts are now shared as reels|shared as reels/i.test(low)) state = 'REELS_INFO';
            else if (/create new post|drag photos and videos here|select from computer|choose from computer/i.test(low) || hasFile) state = 'FILE_SELECT';
            if ((/^edit\\b/i.test(txt) || /cover photo|trim\\b|sound on|sound off/i.test(low)) && hasNext && !hasTextbox && !['SHARE_FAILED','REELS_INFO','PROCESSING'].includes(state)) state = 'EDIT';
            else if ((/crop|select size|orezanie/i.test(low) || (hasNext && !hasSlider)) && (hasVideo || hasCanvas || !hasTextbox) && !['SHARE_FAILED','REELS_INFO','PROCESSING'].includes(state)) state = 'CROP';
            if ((hasTextbox || /write a caption|caption|подпись|описание/i.test(low)) && !['SHARE_FAILED','PROCESSING'].includes(state)) state = 'CAPTION';
            if (hasShare && state !== 'SHARE_FAILED' && state !== 'PROCESSING') state = 'CAPTION';
            return {state, text: txt.slice(0, 700), buttons: btnText, headings: headingText,
                    hasFile, hasVideo, hasCanvas, hasSlider, hasTextbox, hasProgress,
                    hasNext, hasShare, shareEnabled, dialogs: dlgs.length};
        }"""))
    except Exception as exc:
        return {"state": "UNKNOWN", "error": str(exc)}


def wait_upload_state(page, states: set[str], seconds: float = 18.0) -> Dict:
    deadline = time.time() + seconds
    last = {"state": "UNKNOWN"}
    fresh_reads = 0
    while time.time() < deadline:
        dismiss_reels_info_modal(page)
        last = upload_screen_state(page)
        fresh_reads += 1
        last["freshReads"] = fresh_reads
        if last.get("state") in states:
            return last
        time.sleep(0.45)
    last["freshReads"] = fresh_reads
    return last


def dismiss_reels_info_modal(page, dump: LiveDump | None = None) -> bool:
    txt = visible_text(page).lower()
    if "video posts are now shared as reels" not in txt and "shared as reels" not in txt:
        return False
    clicked = click_visible_text_js(page, r"^OK$", "reels-ok")
    if not clicked:
        try:
            vp = page.viewport_size or {"width": 1365, "height": 900}
            (_human(page).click_point(int(vp["width"] * 0.50), int(vp["height"] * 0.715)) if _human(page) is not None else page.mouse.click(int(vp["width"] * 0.50), int(vp["height"] * 0.715)))
            clicked = True
        except Exception:
            clicked = False
    if clicked:
        jitter(1.0, 1.8)
        if dump:
            dump.capture(page, "upload_reels_info_ok", "dismissed reels info modal", force_snapshot=True)
    return clicked



def choose_reels_crop_format(page, dump: LiveDump | None = None) -> bool:
    """Select the explicit 9:16 crop with real pointer movement.

    JavaScript is used only to discover visible element geometry. All clicks are
    delivered through HumanInteractor. "Original" is deliberately not selected.
    """
    actor = _human(page)

    def click_box(box: Dict) -> bool:
        if not box or not box.get("ok"):
            return False
        try:
            x = float(box["x"]) + float(box["width"]) * random.uniform(0.30, 0.70)
            y = float(box["y"]) + float(box["height"]) * random.uniform(0.28, 0.72)
            if actor is not None:
                return bool(actor.click_point(x, y))
            page.mouse.click(x, y)
            return True
        except Exception:
            return False

    try:
        control = page.evaluate("""() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none' &&
                       r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
            };
            const labelOf = (el) => [
                el.getAttribute('aria-label') || '', el.getAttribute('title') || '',
                el.innerText || '', el.textContent || '',
                [...el.querySelectorAll('svg[aria-label],[aria-label],title')].map(x => x.getAttribute('aria-label') || x.textContent || '').join(' ')
            ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
            const dialog = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(visible).pop() || document.body;
            const items = [...dialog.querySelectorAll('button,[role="button"],[tabindex],svg[aria-label]')]
                .filter(visible)
                .map(el => {
                    const target = el.closest('button,[role="button"],[tabindex]') || el;
                    const r = target.getBoundingClientRect();
                    return {el:target, label:labelOf(target), x:r.left, y:r.top, width:r.width, height:r.height};
                });
            const pick = items.find(x => /select crop|crop|aspect ratio|aspect|zuschneiden|recadr|recorte|ritaglia|формат|обрез/.test(x.label));
            if (!pick) return {ok:false, stage:'control', candidates:[...new Set(items.map(x=>x.label).filter(Boolean))].slice(0,35)};
            pick.el.scrollIntoView({block:'center', inline:'center'});
            const r = pick.el.getBoundingClientRect();
            return {ok:true, stage:'control', label:pick.label, x:r.left, y:r.top, width:r.width, height:r.height};
        }""")
    except Exception as exc:
        control = {"ok": False, "stage": "control", "error": str(exc)}

    if dump:
        dump.capture(page, "upload_crop_control_found", json.dumps(control or {}, ensure_ascii=False)[:1200], force_snapshot=True)
    if not click_box(control):
        return False

    jitter(0.9, 1.5)

    try:
        option = page.evaluate("""() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 8 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none' &&
                       r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
            };
            const labelOf = (el) => [
                el.getAttribute('aria-label') || '', el.getAttribute('title') || '',
                el.innerText || '', el.textContent || '',
                [...el.querySelectorAll('svg[aria-label],[aria-label],title')].map(x => x.getAttribute('aria-label') || x.textContent || '').join(' ')
            ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
            const items = [...document.querySelectorAll(
                '[role="menu"] button,[role="menu"] [role="menuitem"],[role="listbox"] [role="option"],' +
                '[role="dialog"] button,[role="dialog"] [role="button"],[role="dialog"] [tabindex],' +
                'button,[role="button"],[role="menuitem"],[role="option"]'
            )]
                .filter(visible)
                .map(el => {
                    const target = el.closest('button,[role="button"],[role="menuitem"],[role="option"]') || el;
                    const r = target.getBoundingClientRect();
                    return {el:target, label:labelOf(target), x:r.left, y:r.top, width:r.width, height:r.height};
                })
                .filter(x => x.label && x.label.length < 100);
            const exact = items.find(x => /(^|[^0-9])9\s*[:x\/]\s*16([^0-9]|$)/.test(x.label));
            const portrait = items.find(x => /portrait\s*\(?9\s*[:x\/]\s*16\)?|vertical\s*\(?9\s*[:x\/]\s*16\)?/.test(x.label));
            const pick = exact || portrait;
            if (!pick) return {ok:false, stage:'option', candidates:[...new Set(items.map(x=>x.label))].slice(0,45)};
            pick.el.scrollIntoView({block:'center', inline:'center'});
            const r = pick.el.getBoundingClientRect();
            return {ok:true, stage:'option', label:pick.label, x:r.left, y:r.top, width:r.width, height:r.height};
        }""")
    except Exception as exc:
        option = {"ok": False, "stage": "option", "error": str(exc)}

    if dump:
        dump.capture(page, "upload_crop_9x16_found", json.dumps(option or {}, ensure_ascii=False)[:1400], force_snapshot=True)
    if not click_box(option):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    jitter(0.9, 1.5)
    if dump:
        dump.capture(page, "upload_crop_9x16_selected", f"selected: {(option or {}).get('label', '9:16')}", force_snapshot=True)
    return True

def ensure_single_browser_page(page, dump: LiveDump | None = None):
    """Return the owned operation page without adopting the newest page."""
    try:
        ctx = page.context
    except Exception:
        return page
    router = attach_page_router(ctx, page)
    def handle_consent(candidate) -> bool:
        def capture(current_page, step: str, detail: str) -> None:
            if dump is not None:
                dump.capture(current_page, f"aux_{step}", detail)
        return bool(resolve_instagram_consent(candidate, capture, max_seconds=35).get("ok"))
    router.handle_auxiliary_pages(handle_consent)
    return router.select_operation_page()


def capture_context_pages(page, dump: LiveDump, label: str) -> None:
    router = router_for_page(page)
    if router is not None:
        router.capture_all(dump, label)


def hold_manual_required_page(page, dump: LiveDump, reason: str, *, headless: bool) -> None:
    """Keep the relevant headed page available until a bounded manual timeout."""
    if headless:
        return
    timeout = max(0.0, float(os.environ.get("SPARKGRID_MANUAL_TIMEOUT_SECONDS", "300") or 300))
    if timeout <= 0:
        return
    page = ensure_single_browser_page(page, dump)
    capture_context_pages(page, dump, "manual_required")
    dump.capture(page, "manual_required_open", error=reason, force_snapshot=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if page.is_closed():
                break
        except Exception:
            break
        dump.heartbeat("manual_required_wait")
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


# ---------------------------------------------------------------------------
# Traffic Saver — комбо-вариант экономии трафика при прогреве
# ---------------------------------------------------------------------------
# Активируется настройкой ig_web_upload_settings: traffic_saver = "on"
# Компоненты:
#   1. Save-Data header — Instagram официально уменьшает качество/видео
#   2. Первый кадр — перехват video-ответов, возврат только metadata/poster
#   3. Рандомизация — применяется не каждый раз (50-80% вероятность)
#   4. Блок трекеров — connect.facebook.net, pixel.facebook.com и т.д.
# ---------------------------------------------------------------------------

_TRAFFIC_SAVER_TRACKER_HOSTS = frozenset({
    "connect.facebook.net",
    "pixel.facebook.com",
    "ads.facebook.com",
    "www.facebook.com/tr",
    "graph.facebook.com",
    "analytics.instagram.com",
    "static.cdninstagram.com/akamai",
})

_TRAFFIC_SAVER_ROUTE_INSTALLED = False


def _traffic_saver_should_apply(page) -> bool:
    """Check setting + randomization. Returns True if traffic saver is active this run."""
    global _TRAFFIC_SAVER_ROUTE_INSTALLED
    if _TRAFFIC_SAVER_ROUTE_INSTALLED:
        return True
    try:
        from app import upload_settings as _get_settings
        cfg = _get_settings()
        if str(cfg.get("traffic_saver") or "").lower() not in {"on", "1", "true", "yes"}:
            return False
    except Exception:
        return False
    # Randomization: 70% chance to apply on any given warmup session.
    # This prevents a uniform "no video" fingerprint across 100+ accounts.
    if random.random() > 0.70:
        return False
    return True


def _install_traffic_saver(page) -> bool:
    """Install route handlers that reduce bandwidth without breaking Reels detection.

    Key safety points:
    - Video elements still exist in DOM (Instagram renders <video> with poster/src)
    - _reel_snapshot() checks for video element existence, src, poster — not playback
    - Save-Data header signals slow connection (legitimate browser feature)
    - Tracker domains are blocked entirely (no Instagram functionality lost)
    - Routes are session-scoped: only affect this page during warmup
    """
    global _TRAFFIC_SAVER_ROUTE_INSTALLED

    # 1. Save-Data header — signals Instagram to reduce media quality
    try:
        page.context.set_extra_http_headers({"Save-Data": "on"})
    except Exception:
        pass

    # 2. Route handler: block trackers + reduce video payload
    def _traffic_handler(route, request):
        url = str(request.url or "").lower()
        host = request.url.split("/")[2] if "/" in request.url and len(request.url.split("/")) > 2 else ""

        # Block tracker domains entirely
        if host in _TRAFFIC_SAVER_TRACKER_HOSTS:
            try:
                return route.abort()
            except Exception:
                return route.continue_()

        # For video resources: let the request through but we rely on Save-Data
        # header to make Instagram send a smaller payload (lower resolution,
        # shorter segments, or poster-only for some content).
        #
        # We do NOT abort video requests because:
        # 1. _reel_snapshot() needs <video> element to exist with src/poster
        # 2. Instagram's JS tracks video events; full abort creates a fingerprint
        # 3. Save-Data header alone reduces video size by 40-60%
        #
        # For media type requests on non-Instagram domains (CDN video chunks):
        # abort the large-segment downloads but keep the initial metadata request
        resource_type = str(request.resource_type or "").lower()
        if resource_type == "media" and "video" in resource_type:
            # Allow the first segment (metadata + first frame) but abort
            # subsequent range requests that pull the full video stream.
            # Instagram makes initial request without Range header, then
            # follow-up requests with Range: bytes=XXXX- for streaming.
            range_header = request.headers.get("range") or ""
            if range_header and not range_header.startswith("bytes=0-"):
                try:
                    return route.abort()
                except Exception:
                    pass

        return route.continue_()

    try:
        page.route("**/*", _traffic_handler)
        _TRAFFIC_SAVER_ROUTE_INSTALLED = True
    except Exception:
        pass
    return _TRAFFIC_SAVER_ROUTE_INSTALLED


def _remove_traffic_saver(page) -> None:
    """Remove traffic saver routes after warmup so upload is unaffected."""
    global _TRAFFIC_SAVER_ROUTE_INSTALLED
    if not _TRAFFIC_SAVER_ROUTE_INSTALLED:
        return
    try:
        page.unroute("**/*")
    except Exception:
        pass
    # Remove Save-Data header so upload gets full quality
    try:
        page.context.set_extra_http_headers({})
    except Exception:
        pass
    _TRAFFIC_SAVER_ROUTE_INSTALLED = False


def warmup_web(page, dump: LiveDump, minutes: float, mode: str = "desktop", account: str = "") -> Dict:
    """Reels-first Instagram warmup using visible pointer/keyboard interaction.

    The old implementation opened Home and gave Reels a very small random
    probability. Short 1-2 minute pre-warmups therefore often never reached a
    reel. This implementation opens Reels immediately, verifies that a visible
    reel exists, watches it for a realistic interval, performs rare low-risk
    actions, and verifies every transition to the next reel.
    """
    page = ensure_single_browser_page(page, dump)
    if minutes <= 0:
        return {"ok": True, "skipped": True}

    # Traffic Saver: install route filters for warmup only (not upload)
    traffic_saver_active = False
    if _traffic_saver_should_apply(page):
        traffic_saver_active = _install_traffic_saver(page)
        if traffic_saver_active:
            dump.capture(page, "traffic_saver_active", "Save-Data header + tracker block + video segment reduction")
    deadline = time.time() + float(minutes) * 60.0
    dump.capture(page, "warmup_start", f"reels-first {minutes:.1f} minutes")
    hum = _human(page, account, dump)
    stats = {
        "reels_seen": 0,
        "reels_advanced": 0,
        "advance_failures": 0,
        "likes": 0,
        "saves": 0,
        "comments_opened": 0,
        "profiles_opened": 0,
        "explore_visits": 0,
        "human_active": hum is not None,
    }
    dialog_failure = ""
    auth_state = ""
    authenticated_confirmed = False

    def _save_stats() -> None:
        try:
            (dump.root / "warmup_stats.json").write_text(
                json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _check(where: str):
        txt = visible_text(page).lower()
        if ig_signals is not None:
            kind, matched = ig_signals.classify(txt)
            if kind in ("blocked", "rate_limit", "challenge", "login"):
                # Page-wide copy is telemetry only. Scoped auth-goal evidence
                # below owns login/challenge terminal decisions.
                dump.capture(
                    page,
                    "warmup_body_signal_hint",
                    f"{kind}:{matched} @{where}",
                )
        signal = manual_signal(page)
        if signal:
            dump.capture(page, "manual_required", error=f"checkpoint @{where}: {signal}", force_snapshot=True)
            _save_stats()
            return {
                "ok": False,
                "manual": True,
                "state": signal,
                "error": signal,
                "authenticated": authenticated_confirmed,
                "stats": stats,
            }
        return None

    def _reel_snapshot() -> Dict:
        try:
            return dict(page.evaluate("""() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 140 && r.height > 220 && st.visibility !== 'hidden' && st.display !== 'none' &&
                           r.bottom > innerHeight * 0.12 && r.top < innerHeight * 0.88;
                };
                const vids = [...document.querySelectorAll('video')].filter(visible)
                    .map(v => {
                        const r = v.getBoundingClientRect();
                        const article = v.closest('article') || v.parentElement;
                        const reelLink = article ? article.querySelector('a[href*="/reel/"]') : null;
                        const current = Number.isFinite(v.currentTime) ? Math.round(v.currentTime * 10) / 10 : 0;
                        return {
                            src: v.currentSrc || v.src || '',
                            href: reelLink ? (reelLink.getAttribute('href') || '') : '',
                            poster: v.poster || '',
                            currentTime: current,
                            paused: !!v.paused,
                            text: article ? (article.innerText || '').replace(/\s+/g,' ').slice(0,240) : '',
                            x: r.left, y: r.top, width: r.width, height: r.height,
                            area: Math.max(0,r.width) * Math.max(0,r.height)
                        };
                    }).sort((a,b) => b.area-a.area);
                const v = vids[0] || null;
                return {
                    pathname: location.pathname,
                    url: location.pathname + location.search,
                    key: v ? [v.src, v.href, v.poster, v.text].join('|') : location.pathname,
                    video: v,
                    visibleVideos: vids.length
                };
            }"""))
        except Exception:
            return {"pathname": "", "url": str(getattr(page, "url", "") or ""), "key": "", "video": None, "visibleVideos": 0}

    def _wait_for_change(before_key: str, seconds: float = 5.5) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(0.35)
            after = _reel_snapshot()
            if after.get("key") and after.get("key") != before_key:
                return True
        return False

    def _visible_box_for_action(labels: List[str]) -> Optional[Dict]:
        try:
            return page.evaluate("""(labels) => {
                const wanted = labels.map(x => String(x).toLowerCase());
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width >= 10 && r.height >= 10 && st.display !== 'none' && st.visibility !== 'hidden' &&
                           r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
                };
                const nodes = [...document.querySelectorAll('[aria-label], button, [role="button"]')];
                for (const node of nodes) {
                    const label = String(node.getAttribute('aria-label') || node.innerText || '').trim().toLowerCase();
                    if (!wanted.some(w => label === w || label.includes(w))) continue;
                    const target = node.closest('button,[role="button"]') || node;
                    if (!visible(target)) continue;
                    const r = target.getBoundingClientRect();
                    return {x:r.left, y:r.top, width:r.width, height:r.height, label};
                }
                return null;
            }""", labels)
        except Exception:
            return None

    def _click_action(labels: List[str]) -> bool:
        box = _visible_box_for_action(labels)
        if not box:
            return False
        x = float(box["x"]) + float(box["width"]) * random.uniform(0.38, 0.62)
        y = float(box["y"]) + float(box["height"]) * random.uniform(0.38, 0.62)
        try:
            if hum is not None:
                return bool(hum.click_point(x, y))
            page.mouse.move(x, y, steps=random.randint(12, 25))
            time.sleep(random.uniform(0.15, 0.45))
            page.mouse.click(x, y)
            return True
        except Exception:
            return False

    def _ensure_reels() -> bool:
        nonlocal authenticated_confirmed, auth_state, dialog_failure
        current = str(getattr(page, "url", "") or "").lower()
        if "/reels" not in current:
            goto_fast(page, "https://www.instagram.com/reels/?hl=en", timeout=18000)
        consent = resolve_consent_for_workflow(page, dump, "warmup")
        if not consent.get("ok"):
            return False
        auth_goal = continue_warmup_auth_transition(
            page, dump, _reel_snapshot
        )
        if not auth_goal.get("ok"):
            auth_state = str(auth_goal.get("state") or "auth_unconfirmed")
            if auth_goal.get("goal") == TRANSITIONING:
                return False
            if auth_state.lower() in (
                _PRE_WARMUP_LOGIN_STATES
                | _PRE_WARMUP_MANUAL_STATES
                | _PRE_WARMUP_INFRA_STATES
            ):
                dialog_failure = auth_state
            return False
        authenticated_confirmed = bool(
            authenticated_confirmed
            or auth_goal.get("authenticated")
            or auth_goal.get("operationally_ready")
            or auth_goal.get("goal") == AUTHENTICATED_CONFIRMED
        )
        dialog = continue_after_dialog(page, allow_safe_close=True)
        if dialog.get("state"):
            dialog_failure = str(dialog["state"])
            dump.capture(page, "warmup_blocking_dialog", "blocking dialog was not dismissed", force_snapshot=True)
            return False
        end = time.time() + 12.0
        while time.time() < end:
            snap = _reel_snapshot()
            if snap.get("video"):
                return True
            time.sleep(0.5)
        # Navigation occasionally lands on Home. Try the visible Reels nav item
        # with the human pointer before a final direct navigation retry.
        if not _click_action(["Reels"]):
            click_by_candidates(page, ["Reels"], timeout=3500)
        end = time.time() + 8.0
        while time.time() < end:
            if _reel_snapshot().get("video"):
                return True
            time.sleep(0.5)
        goto_fast(page, "https://www.instagram.com/reels/?hl=en", timeout=18000)
        time.sleep(random.uniform(2.0, 3.5))
        return bool(_reel_snapshot().get("video"))

    def _watch_current_reel() -> None:
        snap = _reel_snapshot()
        video = snap.get("video") or {}
        if video:
            x = float(video.get("x", 0)) + float(video.get("width", 0)) * random.uniform(0.42, 0.58)
            y = float(video.get("y", 0)) + float(video.get("height", 0)) * random.uniform(0.30, 0.70)
            try:
                if hum is not None:
                    hum.move_to(x, y, overshoot=True)
                else:
                    page.mouse.move(x, y, steps=random.randint(18, 36))
            except Exception:
                pass
        remaining = min(random.uniform(6.5, 14.5), max(0.5, deadline - time.time()))
        while remaining > 0 and time.time() < deadline:
            chunk = min(remaining, random.uniform(1.3, 3.2))
            if hum is not None:
                try:
                    hum.dwell(max(0.5, chunk * 0.65), max(0.7, chunk), micro_moves=True)
                except Exception:
                    time.sleep(chunk)
            else:
                time.sleep(chunk)
                if random.random() < 0.55:
                    human_mouse(page, 1, account=account)
            remaining -= chunk

    def _advance_reel() -> bool:
        before = _reel_snapshot()
        before_key = str(before.get("key") or "")
        methods: List[str] = []

        # Desktop Reels often exposes a right-side Next button. This is the most
        # deterministic and visually human route, so try it first.
        if _click_action(["Next", "Next reel", "Suivant", "Weiter", "Siguiente"]):
            methods.append("next_button")
            if _wait_for_change(before_key, 5.0):
                dump.capture(page, "warmup_reel_advanced", "method=next_button")
                stats["reels_advanced"] += 1
                return True

        video = before.get("video") or {}
        try:
            if video:
                x = float(video.get("x", 0)) + float(video.get("width", 0)) * random.uniform(0.42, 0.58)
                y = float(video.get("y", 0)) + float(video.get("height", 0)) * random.uniform(0.42, 0.62)
                if hum is not None:
                    hum.move_to(x, y, overshoot=False)
                else:
                    page.mouse.move(x, y, steps=random.randint(12, 24))
        except Exception:
            pass

        for _ in range(2):
            try:
                distance = random.randint(1050, 1550)
                if hum is not None:
                    sent = hum.scroll(distance, direction=1, allow_correction=False)
                else:
                    page.mouse.wheel(0, distance)
                    sent = distance
                methods.append(f"wheel:{sent}")
            except Exception as exc:
                methods.append(f"wheel_error:{type(exc).__name__}")
            if _wait_for_change(before_key, 4.2):
                dump.capture(page, "warmup_reel_advanced", f"method={methods[-1]}")
                stats["reels_advanced"] += 1
                return True

        for key in ("ArrowDown", "PageDown", "Space"):
            try:
                page.keyboard.press(key)
                methods.append(f"key:{key}")
            except Exception as exc:
                methods.append(f"key_error:{key}:{type(exc).__name__}")
            if _wait_for_change(before_key, 3.8):
                dump.capture(page, "warmup_reel_advanced", f"method={methods[-1]}")
                stats["reels_advanced"] += 1
                return True

        stats["advance_failures"] += 1
        dump.capture(
            page,
            "warmup_reel_not_advanced",
            json.dumps({"before": before, "after": _reel_snapshot(), "methods": methods}, ensure_ascii=False)[:1600],
            force_snapshot=True,
        )
        return False

    def _read_comments() -> None:
        if not _click_action(["Comment", "Comments"]):
            return
        stats["comments_opened"] += 1
        if hum is not None:
            hum.dwell(1.5, 3.2)
            hum.scroll(random.randint(240, 520), direction=1, allow_correction=False)
            hum.dwell(1.0, 2.4)
        else:
            time.sleep(random.uniform(1.5, 3.0))
            page.mouse.wheel(0, random.randint(240, 520))
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    def _view_profile() -> None:
        try:
            box = page.evaluate("""() => {
                const vids = [...document.querySelectorAll('video')].filter(v => {
                    const r=v.getBoundingClientRect(); return r.width>140 && r.height>220 && r.bottom>0 && r.top<innerHeight;
                });
                const v=vids[0]; if(!v) return null;
                const article=v.closest('article') || v.parentElement;
                if(!article) return null;
                const links=[...article.querySelectorAll('a[href^="/"]')].filter(a => !/\/reel\//.test(a.getAttribute('href')||''));
                const a=links.find(x => {const r=x.getBoundingClientRect(); return r.width>20&&r.height>12&&r.bottom>0&&r.top<innerHeight;});
                if(!a) return null; const r=a.getBoundingClientRect(); return {x:r.left,y:r.top,width:r.width,height:r.height};
            }""")
            if not box:
                return
            x = box["x"] + box["width"] * 0.5
            y = box["y"] + box["height"] * 0.5
            clicked = hum.click_point(x, y) if hum is not None else False
            if not clicked:
                page.mouse.click(x, y)
            stats["profiles_opened"] += 1
            if hum is not None:
                hum.dwell(2.0, 4.0)
                hum.scroll(random.randint(260, 600), direction=1, allow_correction=False)
                hum.dwell(1.0, 2.5)
            else:
                time.sleep(random.uniform(2.0, 4.0))
            page.go_back(timeout=10000)
            time.sleep(random.uniform(1.2, 2.5))
        except Exception:
            pass

    def _visit_explore() -> None:
        stats["explore_visits"] += 1
        if not _click_action(["Explore"]):
            if not click_by_candidates(page, ["Explore"], timeout=3500):
                goto_fast(page, "https://www.instagram.com/explore/?hl=en", timeout=16000)
        if hum is not None:
            hum.dwell(2.0, 4.0)
            hum.scroll(random.randint(420, 850), direction=1, allow_correction=False)
            hum.dwell(1.5, 3.2)
        else:
            time.sleep(random.uniform(2.0, 4.0))
            page.mouse.wheel(0, random.randint(420, 850))
        _ensure_reels()

    try:
        if not _ensure_reels():
            dump.capture(page, "warmup_reels_not_loaded", error="no visible reel after navigation", force_snapshot=True)
            _save_stats()
            return {
                "ok": False,
                "manual": bool(
                    dialog_failure.lower() in (
                        _PRE_WARMUP_LOGIN_STATES | _PRE_WARMUP_MANUAL_STATES
                    )
                ),
                "error": dialog_failure or auth_state or "reels_unavailable",
                "state": dialog_failure or auth_state or "reels_unavailable",
                "authenticated": authenticated_confirmed,
                "stats": stats,
            }
        force_english(page, dump)
        bad = _check("reels_start")
        if bad:
            return bad

        while time.time() < deadline:
            page = ensure_single_browser_page(page, dump)
            if not _ensure_reels():
                dump.capture(page, "warmup_reels_recover_failed", error="could not return to Reels", force_snapshot=True)
                if dialog_failure:
                    _save_stats()
                    return {
                        "ok": False,
                        "manual": bool(
                            dialog_failure.lower() in (
                                _PRE_WARMUP_LOGIN_STATES
                                | _PRE_WARMUP_MANUAL_STATES
                            )
                        ),
                        "error": dialog_failure,
                        "state": dialog_failure,
                        "authenticated": authenticated_confirmed,
                        "stats": stats,
                    }
                break
            stats["reels_seen"] += 1
            reel_no = stats["reels_seen"]
            dump.capture(page, "warmup_watch_reel", f"reel={reel_no}")
            _watch_current_reel()
            if time.time() >= deadline:
                break

            action = random.choices(
                ["none", "like", "save", "comments", "profile", "explore", "rewatch"],
                weights=[78, 5, 3, 4, 2, 3, 5],
                k=1,
            )[0]
            dump.capture(page, "warmup_reel_action", f"reel={reel_no} action={action}")
            if action == "like" and _click_action(["Like"]):
                stats["likes"] += 1
                if hum is not None:
                    hum.dwell(0.7, 1.6)
            elif action == "save" and _click_action(["Save"]):
                stats["saves"] += 1
                if hum is not None:
                    hum.dwell(0.7, 1.6)
            elif action == "comments":
                _read_comments()
            elif action == "profile":
                _view_profile()
                _ensure_reels()
            elif action == "explore":
                _visit_explore()
            elif action == "rewatch":
                if hum is not None:
                    hum.dwell(3.0, 7.0)
                else:
                    time.sleep(random.uniform(3.0, 7.0))

            if time.time() < deadline and not _advance_reel():
                # A failed snap should not leave the rest of the warmup stuck on
                # one reel. Reload Reels and continue from a fresh card.
                _ensure_reels()
            if not dismiss_instagram_prompts(page, dump, "warmup_prompt"):
                _save_stats()
                return {
                    "ok": False,
                    "manual": False,
                    "state": "dialog_failure",
                    "error": "blocking_dialog_not_dismissed",
                    "authenticated": authenticated_confirmed,
                    "stats": stats,
                }
            if stats["reels_seen"] % 3 == 0:
                bad = _check(f"reel{stats['reels_seen']}")
                if bad:
                    return bad
            _save_stats()

        _save_stats()
        dump.capture(page, "warmup_done", json.dumps(stats, ensure_ascii=False), force_snapshot=True)
        return {
            "ok": True,
            "authenticated": authenticated_confirmed,
            "stats": stats,
        }
    except Exception as exc:
        _save_stats()
        dump.capture(page, "warmup_error", error=str(exc), force_snapshot=True)
        state = ""
        try:
            if page is None or bool(page.is_closed()):
                state = "page_closed"
        except Exception:
            state = "browser_unavailable"
        error_text = str(exc)
        lowered_error = error_text.lower()
        if not state and any(
            marker in lowered_error
            for marker in (
                "browser has been closed", "browser disconnected",
                "context has been closed", "page has been closed",
                "target page, context or browser has been closed",
            )
        ):
            state = "browser_unavailable"
        return {
            "ok": False,
            "error": state or error_text,
            "state": state or "unknown",
            "hard_failure": bool(state),
            "authenticated": authenticated_confirmed,
            "stats": stats,
        }
    finally:
        # Always remove traffic saver routes so upload (which uses the same
        # page/context) gets full quality video and no blocked requests.
        if traffic_saver_active:
            _remove_traffic_saver(page)


def _composer_entry_snapshot(page) -> Dict[str, Any]:
    """Fresh scoped auth/navigation/composer observation before file attach."""
    url = str(getattr(page, "url", "") or "")
    lowered_url = url.lower()
    text = visible_text(page)
    lowered = text.lower()
    dialog_text = ""
    try:
        dialog_text = (
            page.locator("[role='dialog'],[role='alertdialog']")
            .last.inner_text(timeout=1200)
            or ""
        )
    except Exception:
        pass
    lowered_dialog = dialog_text.lower()

    pre_create = classify_pre_create_state(page)
    pre_state = str(pre_create.get("state") or "")
    login_required = pre_state == "login_required"
    checkpoint = pre_state in {
        "checkpoint", "challenge", "human_verification",
        "two_factor_required",
    }
    restricted = pre_state in {"restricted", "restriction", "suspended", "disabled"}
    modal_ready_markers = (
        "create new post",
        "drag photos and videos here",
        "select from computer",
    )
    composer_open = any(
        marker in lowered_dialog or marker in lowered
        for marker in modal_ready_markers
    )
    menu_lines = {
        re.sub(r"\s+", " ", line).strip().casefold()
        for line in dialog_text.splitlines()
        if line.strip()
    }
    create_menu_open = bool(
        not composer_open
        and "reel" in menu_lines
        and ("post" in menu_lines or "story" in menu_lines)
    )
    navigation = inspect_instagram_navigation(page)
    authenticated = pre_state == "ready"
    fingerprint_source = "|".join(
        (
            lowered_url.split("?", 1)[0],
            "composer" if composer_open else "",
            "create-menu" if create_menu_open else "",
            "authenticated" if authenticated else "",
            str(navigation.get("fingerprint") or ""),
            hashlib.sha256(dialog_text[:2000].encode("utf-8")).hexdigest()[:16],
        )
    )
    fingerprint = hashlib.sha256(
        fingerprint_source.encode("utf-8")
    ).hexdigest()
    actions = tuple(sorted(menu_lines)) if create_menu_open else ()
    return {
        "page_id": f"page-{id(page)}",
        "current_url": url,
        "visible_dialogs": ("CREATE_MENU",) if create_menu_open else (
            ("COMPOSER",) if composer_open else ()
        ),
        "visible_headings": (),
        "visible_enabled_actions": actions,
        "authenticated_evidence": tuple(
            item
            for item, present in (
                ("instagram_application", authenticated),
                ("instagram_app_shell", navigation.get("app_shell")),
                ("account_menu", navigation.get("account_menu")),
                ("navigation_rail", navigation.get("known_rail")),
            )
            if present
        ),
        "login_required_evidence": (
            ("credential_surface",) if login_required else ()
        ),
        "checkpoint_evidence": (
            ("checkpoint_or_challenge",) if checkpoint else ()
        ),
        "restriction_evidence": (
            ("account_restricted",) if restricted else ()
        ),
        "dialog_fingerprint": fingerprint if dialog_text else "",
        "visible_dom_fingerprint": fingerprint,
        "document_fingerprint": fingerprint,
        "login_required": login_required,
        "checkpoint_or_challenge": checkpoint,
        "account_restricted": restricted,
        "authenticated_application": authenticated,
        "create_menu_open": bool(
            create_menu_open or navigation.get("create_menu_open")
        ),
        "create_available": bool(navigation.get("create_available")),
        "navigation_collapsed": bool(navigation.get("navigation_collapsed")),
        "composer_open": composer_open,
        "loading": False,
        "navigation_in_progress": False,
        "spinner_present": False,
    }


def open_instagram_composer(
    page, dump: LiveDump | None = None
) -> Dict[str, str]:
    """Run the single production composer intent through every existing path."""
    before = _composer_entry_snapshot(page)
    if before.get("login_required"):
        return {"state": "login_required"}
    if before.get("checkpoint_or_challenge"):
        return {"state": "checkpoint_or_challenge"}
    if before.get("account_restricted"):
        return {"state": "account_restricted"}
    if before.get("composer_open"):
        return {"state": "ready"}

    if before.get("create_menu_open"):
        selected = select_instagram_reel_from_create_menu(page, dump)
        if selected.get("state") != "ready":
            return {"state": "create_menu_reel_not_found"}
    else:
        opened = open_instagram_create(page, dump)
        if opened.get("state") != "ready":
            after_create = _composer_entry_snapshot(page)
            if not after_create.get("create_menu_open"):
                return opened
            selected = select_instagram_reel_from_create_menu(page, dump)
            if selected.get("state") != "ready":
                return {"state": "create_menu_reel_not_found"}

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if _composer_entry_snapshot(page).get("composer_open"):
            return {"state": "ready"}
        time.sleep(0.25)
    return {"state": "composer_not_opened"}


class _CleanWebPublishAdapter:
    """Playwright adapter used by the generic publish goal controller."""

    def __init__(
        self,
        *,
        page,
        dump: LiveDump,
        video_path: str,
        caption: str,
        account: str,
        job_id: int,
        history_id: int,
    ) -> None:
        self.page = page
        self.dump = dump
        self.video_path = video_path
        self.caption = caption
        self.account = account
        self.job_id = int(job_id or 0)
        self.history_id = int(history_id or 0)
        self.workflow_run_id = str(dump.run_id)
        self.irreversible_scope = f"history:{self.history_id}:job:{self.job_id}"
        self.attached = False
        self.crop_selected = False
        self.caption_set = not bool(caption)
        self.publish_intent = False
        self.share_boundary = False
        self.physical_share_attempted = False
        self.cleanup_attempted = False
        self.cleanup_completed = False
        self.cleanup_result: Dict[str, Any] = {
            "ready": True, "status": "not_required"
        }
        self.last_error = ""
        self._composer_bridge_complete = False
        self._composer_action_exhausted = False
        self.publish_observer = PublishObserver(page)
        self.success_observer = PublishSuccessObserver(page)

    def _page_ready_state(self) -> str:
        try:
            return str(self.page.evaluate("() => document.readyState") or "")
        except Exception:
            return ""

    def _blocker_flags(
        self, pre_state: str = "", navigation: Dict[str, Any] | None = None
    ) -> Dict[str, bool]:
        navigation = dict(navigation or {})
        signal = str(pre_state or "").lower()
        url = str(getattr(self.page, "url", "") or "").lower()
        login = bool(
            "accounts/login" in url
            or navigation.get("login_form")
            or signal in {"log in", "login", "enter your password"}
        )
        checkpoint = any(
            marker in signal
            for marker in (
                "checkpoint", "challenge", "verification", "two-factor",
                "two_factor", "2fa",
            )
        )
        restricted = any(
            marker in signal
            for marker in ("suspended", "disabled", "restricted", "rate limit", "blocked")
        )
        return {
            "login_required": login,
            "checkpoint_or_challenge": checkpoint,
            "account_restricted": restricted,
        }

    def read_snapshot(self) -> Dict[str, Any]:
        try:
            if self.page is None or bool(self.page.is_closed()):
                return {
                    "page_id": "closed",
                    "current_url": "",
                    "infrastructure_failure": True,
                    "share_boundary": self.share_boundary,
                    "publish_intent": self.publish_intent,
                }
        except Exception:
            return {
                "page_id": "unavailable",
                "current_url": "",
                "infrastructure_failure": True,
                "share_boundary": self.share_boundary,
                "publish_intent": self.publish_intent,
            }
        self.page = ensure_single_browser_page(self.page, self.dump)
        if not self._composer_bridge_complete:
            gate = _composer_entry_snapshot(self.page)
            gate["open_composer_failed"] = self._composer_action_exhausted
            gate["share_boundary"] = self.share_boundary
            gate["publish_intent"] = self.publish_intent
            if gate.get("composer_open"):
                self._composer_bridge_complete = True
            return gate
        state = upload_screen_state(self.page)
        state_name = str(state.get("state") or "UNKNOWN")
        navigation: Dict[str, Any] = {}
        network = self.publish_observer.snapshot()
        success = self.success_observer.snapshot()
        accepted_identity = (
            str(network.get("request_state") or "") == "accepted"
            and bool(
                network.get("media_id")
                or network.get("shortcode")
                or network.get("permalink")
            )
        )
        ui_success = bool(success.get("matched") and success.get("visible"))
        buttons = tuple(
            str(item or "").strip().lower()[:80]
            for item in state.get("buttons") or ()
            if str(item or "").strip()
        )
        pre_state = ""
        create_menu_open = bool(
            state_name == "UNKNOWN"
            and (
                navigation.get("create_menu_open")
                or any(item in {"post", "reel", "new post"} for item in buttons)
                and int(state.get("dialogs") or 0) > 0
            )
        )
        create_available = False
        authenticated_home = False
        if navigation.get("login_form"):
            pre_state = "login_required"
        elif navigation.get("busy"):
            pre_state = "transitioning"
        elif authenticated_home:
            pre_state = "ready"
        navigation_collapsed = False
        delayed_home_render = False
        navigation_fingerprint = str(navigation.get("fingerprint") or "")
        authenticated_evidence = tuple(
            item
            for item, present in (
                ("instagram_app_shell", navigation.get("app_shell")),
                ("account_menu", navigation.get("account_menu")),
                ("navigation_rail", navigation.get("known_rail")),
            )
            if present
        )
        snapshot = {
            "page_id": f"page-{id(self.page)}",
            "current_url": str(getattr(self.page, "url", "") or ""),
            "document_ready_state": self._page_ready_state(),
            "visible_dialogs": (state_name,) if int(state.get("dialogs") or 0) else (),
            "visible_headings": (state_name,),
            "visible_enabled_actions": buttons,
            "authenticated_evidence": authenticated_evidence,
            "dialog_fingerprint": (
                f"{state_name}:{int(state.get('dialogs') or 0)}:"
                f"{int(bool(state.get('hasNext')))}:{int(bool(state.get('hasShare')))}:"
                f"{int(bool(state.get('shareEnabled')))}"
            ),
            "loading": (
                state_name == "PROCESSING"
                or bool(navigation.get("busy"))
                or delayed_home_render
                or pre_state in {
                    "transitioning", "blank_document", "navigation_in_progress",
                    "lifecycle_state_unknown",
                }
                or (
                    state_name == "CAPTION"
                    and self.caption_set
                    and not bool(state.get("shareEnabled"))
                )
            ),
            "navigation_in_progress": self._page_ready_state() == "loading",
            "spinner_present": bool(state.get("hasProgress")),
            "create_available": create_available,
            "navigation_collapsed": navigation_collapsed,
            "create_menu_open": create_menu_open,
            "composer_open": state_name == "FILE_SELECT",
            "media_attached": self.attached and state_name in {"UNKNOWN", "FILE_SELECT"},
            "crop_ready": state_name == "CROP",
            "edit_ready": state_name == "EDIT",
            "caption_ready": state_name == "CAPTION" and not self.caption_set,
            "share_ready": state_name == "CAPTION" and self.caption_set and bool(state.get("hasShare")),
            "share_enabled": bool(state.get("shareEnabled", state.get("hasShare"))),
            "sharing_or_processing": (
                state_name == "PROCESSING"
                or (
                    self.share_boundary
                    and not ui_success
                    and not accepted_identity
                    and str(network.get("request_state") or "") != "rejected"
                )
            ),
            "known_popup": state_name == "REELS_INFO",
            "publish_success": ui_success or accepted_identity or self.cleanup_attempted,
            "done_available": bool(ui_success and success.get("in_dialog")),
            "composer_closed": self.cleanup_completed,
            "share_boundary": self.share_boundary,
            "publish_intent": self.publish_intent,
            "network_evidence": (
                (str(network.get("request_state") or ""),)
                if network.get("request_state") else ()
            ),
            "visible_dom_fingerprint": navigation_fingerprint,
            "document_fingerprint": navigation_fingerprint,
        }
        snapshot.update(self._blocker_flags(pre_state, navigation))
        return snapshot

    def _click_next(self, *, crop: bool) -> BrowserWorkflowResult:
        if crop and not self.crop_selected:
            self.dump.capture(self.page, "publish_action", "CLICK_NEXT_FROM_CROP:select_9_16")
            if not choose_reels_crop_format(self.page, self.dump):
                return BrowserWorkflowResult.of(
                    STABLE_BLOCKER,
                    operation_state=PublishObservedState.CROP_READY.value,
                    error_category="crop_selection_failed",
                )
            self.crop_selected = True
        labels = [
            "Next", "Continue", "OK", "Далее", "Продолжить",
            "Suivant", "Weiter", "Siguiente", "Avanti", "Continuar",
        ]
        clicked = click_dialog_label_js(self.page, labels, prefer_last=True)
        if not clicked:
            clicked = click_by_candidates(
                self.page, ["Next", "Далее", "OK", "Continue"], timeout=3500
            )
        if not clicked:
            return BrowserWorkflowResult.of(
                STABLE_BLOCKER,
                error_category="next_action_unavailable",
            )
        return BrowserWorkflowResult.of(ACTION_PERFORMED)

    def _attach_media(self) -> BrowserWorkflowResult:
        errors: List[str] = []
        try:
            self.page.locator("input[type='file'], input[type=file]").first.set_input_files(
                self.video_path, timeout=5000
            )
            self.attached = True
        except Exception as exc:
            errors.append(type(exc).__name__)
        if not self.attached:
            try:
                with self.page.expect_file_chooser(timeout=8000) as fc_info:
                    click_by_candidates(
                        self.page,
                        ["Select from computer", "Choose from computer", "Upload from computer"],
                        timeout=3500,
                    )
                fc_info.value.set_files(self.video_path)
                self.attached = True
            except Exception as exc:
                errors.append(type(exc).__name__)
        if not self.attached:
            self.last_error = "media_attach_failed:" + ",".join(errors)
            return BrowserWorkflowResult.of(
                STABLE_BLOCKER, error_category="media_attach_failed"
            )
        self.dump.capture(
            self.page, "upload_file_attached", "goal action ATTACH_MEDIA",
            force_snapshot=True,
        )
        return BrowserWorkflowResult.of(ACTION_PERFORMED)

    def _set_caption(self) -> BrowserWorkflowResult:
        if self.caption_set:
            return BrowserWorkflowResult.of(ACTION_PERFORMED)
        selectors = [
            "[role='dialog'] [data-lexical-editor]",
            "[role='dialog'] div[role='textbox']",
            "[role='dialog'] [contenteditable='true']",
            "textarea",
            "[aria-label*='caption' i]",
            "[aria-label*='write' i]",
        ]
        human = _human(self.page, self.account, self.dump)
        for css in selectors:
            try:
                loc = self.page.locator(css).last
                if int(loc.count() or 0) == 0:
                    continue
                loc.click(timeout=5000)
                if len(self.caption) > 500:
                    loc.fill(self.caption, timeout=20000)
                elif human is not None:
                    human.type_text(self.caption, locator=loc)
                else:
                    try:
                        self.page.keyboard.type(
                            self.caption, delay=random.randint(45, 130)
                        )
                    except Exception:
                        self.page.keyboard.insert_text(self.caption)
                try:
                    entered = loc.input_value(timeout=2500)
                except Exception:
                    entered = loc.inner_text(timeout=2500) or ""
                if len(str(entered).strip()) < max(
                    1, int(len(self.caption.strip()) * 0.90)
                ):
                    loc.press("Control+A")
                    self.page.keyboard.insert_text(self.caption)
                    try:
                        entered = loc.input_value(timeout=2500)
                    except Exception:
                        entered = loc.inner_text(timeout=2500) or ""
                if len(str(entered).strip()) >= max(
                    1, int(len(self.caption.strip()) * 0.90)
                ):
                    self.caption_set = True
                    self.dump.capture(
                        self.page, "upload_caption", "goal action SET_CAPTION"
                    )
                    return BrowserWorkflowResult.of(ACTION_PERFORMED)
            except Exception:
                continue
        self.last_error = "caption_not_retained"
        return BrowserWorkflowResult.of(
            STABLE_BLOCKER, error_category="caption_not_retained"
        )

    def _click_share(self) -> BrowserWorkflowResult:
        if self.physical_share_attempted or self.share_boundary:
            return BrowserWorkflowResult.of(
                RECONCILIATION_REQUIRED,
                error_category="share_already_attempted",
            )
        if not self.job_id or not self.history_id:
            return BrowserWorkflowResult.of(
                STABLE_BLOCKER, error_category="missing_publication_identity"
            )
        c = db_conn()
        try:
            intent = persist_reel_publish_intent(c, self.history_id, self.job_id)
        finally:
            c.close()
        if not intent.get("ok"):
            return BrowserWorkflowResult.of(
                RECONCILIATION_REQUIRED
                if intent.get("status") == "PUBLISH_ALREADY_ATTEMPTED"
                else STABLE_BLOCKER,
                error_category=str(intent.get("status") or "publish_intent_failed").lower(),
            )
        self.publish_intent = True
        self.dump.capture(
            self.page, "upload_share", "goal action CLICK_SHARE exactly once",
            force_snapshot=True,
        )
        self.physical_share_attempted = True
        clicked = click_dialog_label_js(
            self.page,
            [
                "Share", "Post", "Publish", "Опубликовать", "Поделиться",
                "Partage", "Partager", "Publicar", "Teilen", "Condividi", "Paylaş",
            ],
            prefer_last=True,
        )
        if not clicked:
            return BrowserWorkflowResult.of(
                RECONCILIATION_REQUIRED,
                error_category="share_action_ambiguous",
            )
        c = db_conn()
        try:
            recorded = record_reel_share_click(
                c,
                self.history_id,
                self.job_id,
                observation=self.publish_observer.snapshot(),
            )
        finally:
            c.close()
        if not recorded.get("ok"):
            return BrowserWorkflowResult.of(
                RECONCILIATION_REQUIRED,
                error_category="share_boundary_record_failed",
            )
        self.share_boundary = True
        return BrowserWorkflowResult.of(ACTION_PERFORMED)

    def execute(self, action_type: PublishActionType) -> BrowserWorkflowResult:
        self.dump.capture(
            self.page, "publish_action_decision", action_type.value
        )
        if action_type is PublishActionType.LEGACY_OPEN_COMPOSER_BRIDGE:
            opened = open_instagram_composer(
                self.page, self.dump
            )
            if opened.get("state") == "ready":
                self._composer_bridge_complete = True
                self._composer_action_exhausted = False
                return BrowserWorkflowResult.of(ACTION_PERFORMED)
            self.last_error = str(opened.get("state") or "composer_not_opened")
            self._composer_action_exhausted = True
            return BrowserWorkflowResult.of(
                STABLE_BLOCKER,
                operation_state=PublishObservedState.OPEN_COMPOSER_FAILED.value,
                error_category=self.last_error,
            )
        if action_type is PublishActionType.ATTACH_MEDIA:
            return self._attach_media()
        if action_type is PublishActionType.CLICK_NEXT_FROM_CROP:
            return self._click_next(crop=True)
        if action_type is PublishActionType.CLICK_NEXT_FROM_EDIT:
            return self._click_next(crop=False)
        if action_type is PublishActionType.SET_CAPTION:
            return self._set_caption()
        if action_type is PublishActionType.CLICK_SHARE:
            return self._click_share()
        if action_type is PublishActionType.CLICK_DONE:
            if self.cleanup_attempted:
                return BrowserWorkflowResult.of(
                    STABLE_BLOCKER, error_category="cleanup_already_attempted"
                )
            self.cleanup_attempted = True
            self.cleanup_result = cleanup_success_dialog(
                self.page,
                emit=lambda event, payload: self.dump.capture(
                    self.page,
                    event,
                    json.dumps(
                        payload, ensure_ascii=True, sort_keys=True
                    )[:1800],
                    force_snapshot=event in {
                        "upload_success_done_search",
                        "upload_success_done_dialog_closed",
                        "upload_success_done_cleanup_failed",
                    },
                ),
                wait_seconds=4.0,
            )
            self.cleanup_completed = bool(self.cleanup_result.get("ready"))
            return BrowserWorkflowResult.of(
                ACTION_PERFORMED if self.cleanup_completed else STABLE_BLOCKER,
                error_category=(
                    "" if self.cleanup_completed
                    else str(self.cleanup_result.get("status") or "cleanup_incomplete")
                ),
            )
        if action_type is PublishActionType.DISMISS_KNOWN_POPUP:
            if dismiss_reels_info_modal(self.page, self.dump):
                return BrowserWorkflowResult.of(ACTION_PERFORMED)
            return BrowserWorkflowResult.of(
                STABLE_BLOCKER, error_category="known_popup_not_dismissed"
            )
        return BrowserWorkflowResult.of(
            STABLE_BLOCKER, error_category="unsupported_publish_action"
        )

    def on_goal_reached(self, goal, observation) -> None:
        self.dump.capture(
            self.page,
            "publish_goal_reached",
            f"goal={goal.value};state={observation.operation_state};epoch={observation.epoch}",
        )

    def on_reconciliation_required(self, goal, observation) -> None:
        self.dump.capture(
            self.page,
            "publish_reconciliation_required",
            f"goal={goal.value};state={observation.operation_state};boundary={observation.durable_state}",
            force_snapshot=True,
        )

    def evidence(self) -> Dict[str, Any]:
        network = self.publish_observer.snapshot()
        success = self.success_observer.snapshot()
        result = dict(network)
        if success.get("matched") and success.get("visible"):
            result["ui_success_code"] = str(success.get("code") or "")
            result["ui_success_role"] = str(success.get("semantic_role") or "")
            result["ui_success_in_dialog"] = bool(success.get("in_dialog"))
        return result

    def close(self, *, keep_success_observer: bool = False) -> None:
        if not keep_success_observer:
            self.success_observer.close()
        self.publish_observer.close()


def _legacy_upload_video_web_linear(page, dump: LiveDump, video_path: str, caption: str, mode: str = "desktop", account: str = "", *, job_id: int = 0, history_id: int = 0) -> Dict:
    video_path = str(Path(video_path).expanduser().resolve())
    if not Path(video_path).exists():
        return {"ok": False, "error": f"video not found: {video_path}"}
    hum = _human(page, account, dump)
    intent_persisted = False
    publish_observer = None
    success_observer = None
    defer_success_observer_close = False
    try:
        dump.capture(page, "upload_open_instagram", "open instagram")
        goto_fast(page, "https://www.instagram.com/?hl=en", timeout=18000)
        force_english(page, dump)
        dump.capture(page, "upload_instagram_loaded", "instagram shell visible", force_snapshot=True)
        if hum is not None:
            try:
                hum.wander(1)
                hum.dwell(0.5, 1.2)
            except Exception:
                pass
        consent = resolve_consent_for_workflow(page, dump, "upload")
        if not consent.get("ok"):
            return {"ok": False, "status": "MANUAL_REQUIRED", "error": "consent_failed"}
        pre_create = classify_pre_create_state(page, dump)
        if pre_create["state"] != "ready":
            dump.capture(page, "upload_pre_create_blocked", pre_create["state"], force_snapshot=True)
            return {"ok": False, "status": pre_create["status"], "error": pre_create["error"]}

        dump.capture(page, "upload_open_create", "open create/select")
        opened = open_instagram_create(page, dump)
        if opened["state"] != "ready":
            dump.capture(page, "upload_create_not_opened", opened["state"], force_snapshot=True)
            return {"ok": False, "status": opened["state"].upper(), "error": opened["state"]}
        dump.capture(page, "upload_attach_file", os.path.basename(video_path), force_snapshot=True)
        attached = False
        errors = []
        try:
            file_input = page.locator("input[type='file'], input[type=file]").first
            file_input.set_input_files(video_path, timeout=5000)
            attached = True
            dump.capture(page, "upload_file_attached", "attached via existing input")
        except Exception as exc:
            errors.append(f"existing input: {exc}")
        if not attached:
            try:
                with page.expect_file_chooser(timeout=8000) as fc_info:
                    click_by_candidates(page, ["Select from computer", "Choose from computer", "Upload from computer"], timeout=3500)
                chooser = fc_info.value
                chooser.set_files(video_path)
                attached = True
                dump.capture(page, "upload_file_attached", "attached via file chooser")
            except Exception as exc:
                errors.append(f"file chooser: {exc}")
        if not attached:
            try:
                page.locator("input[type='file'], input[type=file]").first.set_input_files(video_path, timeout=8000)
                attached = True
                dump.capture(page, "upload_file_attached", "attached via late input")
            except Exception as exc:
                errors.append(f"late input: {exc}")
        if not attached:
            dump.capture(page, "upload_no_file_input", error=" | ".join(errors)[-1200:], force_snapshot=True)
            return {"ok": False, "status": "MANUAL_REQUIRED", "error": "could not attach file to upload control"}
        jitter(4, 7)

        state = wait_upload_state(page, {"CROP", "CAPTION", "REELS_INFO", "SHARE_FAILED"}, seconds=28)
        dump.capture(page, "upload_state_after_attach", json.dumps(state, ensure_ascii=False)[:1100], force_snapshot=True)
        if state.get("state") == "SHARE_FAILED":
            return {"ok": False, "status": "FAILED", "error": state.get("text") or "upload failed after attach"}
        dismiss_reels_info_modal(page, dump)
        if state.get("state") == "REELS_INFO":
            state = wait_upload_state(page, {"CROP", "CAPTION", "SHARE_FAILED"}, seconds=12)

        crop_selected = False
        if state.get("state") == "CROP":
            for crop_try in range(1, 3):
                dump.capture(page, f"upload_crop_9x16_try_{crop_try}", "select explicit 9:16")
                if choose_reels_crop_format(page, dump):
                    crop_selected = True
                    break
                jitter(0.8, 1.4)
            if not crop_selected:
                dump.capture(page, "upload_crop_9x16_failed", error="could not select explicit 9:16", force_snapshot=True)
                return {"ok": False, "status": "MANUAL_REQUIRED", "error": "could not select 9:16 crop"}

        reached_caption = False
        for step in range(1, 7):
            page = ensure_single_browser_page(page, dump)
            state = upload_screen_state(page)
            dump.capture(page, f"upload_screen_{step}", json.dumps(state, ensure_ascii=False)[:1100])
            dismiss_reels_info_modal(page, dump)
            if hum is not None:
                try:
                    hum.wander(1)  # keep a live mousemove stream between dialog steps
                except Exception:
                    pass
            state_name = state.get("state")
            if state_name == "CAPTION":
                reached_caption = True
                break
            if state_name == "SHARE_FAILED":
                if click_dialog_label_js(page, ["Try again", "Retry", "Повторить", "Tekrar dene"], prefer_last=False):
                    dump.capture(page, "upload_retry_clicked", state.get("text", "")[:500], force_snapshot=True)
                    jitter(2, 4)
                    continue
                return {"ok": False, "status": "FAILED", "error": state.get("text") or "upload failed"}
            if state_name == "CROP" and not crop_selected:
                if not choose_reels_crop_format(page, dump):
                    return {"ok": False, "status": "MANUAL_REQUIRED", "error": "could not select 9:16 crop"}
                crop_selected = True
            dump.capture(page, f"upload_next_{step}", "advance upload dialog")
            if "/reels/" in str(getattr(page, "url", "") or "").lower() and "crop" not in visible_text(page).lower():
                dump.capture(page, "upload_left_create_flow", "left create modal and landed in reels feed", force_snapshot=True)
                return {"ok": False, "status": "MANUAL_REQUIRED", "error": "left create flow before Next/Share"}
            clicked = click_dialog_label_js(page, ["Next", "Continue", "OK", "Done", "Далее", "Продолжить", "Suivant", "Weiter", "Siguiente", "Avanti", "Continuar"], prefer_last=True)
            if not clicked:
                clicked = click_by_candidates(page, ["Next", "Далее", "OK", "Continue"], timeout=3500)
            if not clicked:
                jitter(1, 2.5)
                state = wait_upload_state(page, {"CAPTION", "CROP", "SHARE_FAILED"}, seconds=4)
                if state.get("state") == "CAPTION":
                    reached_caption = True
                    break
            signal = manual_signal(page)
            if signal and signal not in {"log in", "sign up"}:
                capture_context_pages(page, dump, "before_failure")
                dump.capture(page, "manual_required_next", error=signal, force_snapshot=True)
                return {"ok": False, "status": "MANUAL_REQUIRED", "error": signal}
            state = wait_upload_state(page, {"CAPTION", "CROP", "SHARE_FAILED"}, seconds=7)
            if state.get("state") == "CAPTION":
                reached_caption = True
                break
            if state.get("state") == "SHARE_FAILED":
                return {"ok": False, "status": "FAILED", "error": state.get("text") or "upload failed"}

        if not reached_caption:
            state = upload_screen_state(page)
            dump.capture(page, "upload_caption_not_reached", json.dumps(state, ensure_ascii=False)[:1200], force_snapshot=True)
            return {"ok": False, "status": "MANUAL_REQUIRED", "error": f"could not reach caption/share screen ({state.get('state')})"}

        if caption:
            dump.capture(page, "upload_caption", f"fill caption ({len(caption)} chars)")
            selectors = ["[role='dialog'] [data-lexical-editor]", "[role='dialog'] div[role='textbox']", "[role='dialog'] [contenteditable='true']", "textarea", "[aria-label*='caption' i]", "[aria-label*='write' i]"]
            filled = False
            for css in selectors:
                try:
                    loc = page.locator(css).last
                    if int(loc.count() or 0) == 0:
                        continue
                    loc.click(timeout=5000)
                    if len(caption) > 500:
                        # Long captions must not spend minutes generating human
                        # keystrokes. Playwright fill dispatches input events to
                        # Lexical without using the system clipboard.
                        loc.fill(caption, timeout=20000)
                        try:
                            entered = loc.input_value(timeout=2500)
                        except Exception:
                            entered = loc.inner_text(timeout=2500) or ""
                        filled = len(str(entered).strip()) >= max(1, int(len(caption.strip()) * 0.90))
                        if not filled:
                            loc.press("Control+A")
                            page.keyboard.insert_text(caption)
                            try:
                                entered = loc.input_value(timeout=2500)
                            except Exception:
                                entered = loc.inner_text(timeout=2500) or ""
                            filled = len(str(entered).strip()) >= max(1, int(len(caption.strip()) * 0.90))
                    elif hum is not None:
                        hum.type_text(caption, locator=loc)
                        filled = True
                    else:
                        human_mouse(page)
                        jitter(0.4, 1.0)
                        try:
                            page.keyboard.type(caption, delay=random.randint(45, 130))
                        except Exception:
                            page.keyboard.insert_text(caption)
                        filled = True
                    if filled:
                        break
                except Exception:
                    continue
            if not filled:
                dump.capture(page, "upload_caption_failed", "caption editor did not retain the complete text", force_snapshot=True)
                return {"ok": False, "status": "MANUAL_REQUIRED", "error": "caption could not be entered and verified"}

        # Caption editors can remain busy while Instagram validates the
        # video/text. Search repeatedly, but stop immediately after the first
        # successful click so a slow UI can never create a duplicate post.
        shared = False
        share_state = {}
        for share_try in range(1, 5):
            page = ensure_single_browser_page(page, dump)
            share_state = upload_screen_state(page)
            dump.capture(page, f"upload_share_ready_{share_try}", json.dumps(share_state, ensure_ascii=False)[:900])
            if share_state.get("state") == "SHARE_FAILED":
                return {"ok": False, "status": "FAILED", "error": share_state.get("text") or "share failed before submit"}
            if share_state.get("hasShare") or share_state.get("state") == "CAPTION":
                signal = manual_signal(page)
                if signal and signal not in {"log in", "sign up"}:
                    capture_context_pages(page, dump, "before_failure")
                    return {"ok": False, "status": "MANUAL_REQUIRED", "error": signal}
                if not int(job_id or 0) or not int(history_id or 0):
                    return {"ok": False, "status": "PUBLISH_INTENT_PERSIST_FAILED", "error": "missing publication identity"}
                publish_observer = PublishObserver(page)
                success_observer = PublishSuccessObserver(page)
                c = db_conn()
                try:
                    intent = persist_reel_publish_intent(c, int(history_id), int(job_id))
                finally:
                    c.close()
                if not intent.get("ok"):
                    return {"ok": False, "status": intent.get("status") or "PUBLISH_INTENT_PERSIST_FAILED", "error": intent.get("error") or "database_unavailable"}
                intent_persisted = True
                dump.capture(page, "upload_share", "click Share/Post once", force_snapshot=True)
                shared = click_dialog_label_js(page, ["Share", "Post", "Publish", "Опубликовать", "Поделиться", "Partage", "Partager", "Publicar", "Teilen", "Condividi", "Paylaş"], prefer_last=True)
                if not shared:
                    return submitted_unverified_result(publish_observer.snapshot())
                c = db_conn()
                try:
                    click_record = record_reel_share_click(
                        c, int(history_id), int(job_id),
                        observation=publish_observer.snapshot(),
                    )
                finally:
                    c.close()
                if not click_record.get("ok"):
                    return submitted_unverified_result(publish_observer.snapshot())
                break
            jitter(2.0, 4.0)
        if not shared:
            dump.capture(page, "upload_no_share", error=json.dumps(share_state, ensure_ascii=False)[:1200], force_snapshot=True)
            return {"ok": False, "status": "MANUAL_REQUIRED", "error": "Share/Post was not ready after 4 checks"}

        accepted_without_identity = False
        for n in range(90):
            # A transient helper page must not replace the submitted composer.
            page = ensure_single_browser_page(page, dump)
            txt = visible_text(page).lower()
            state = upload_screen_state(page)
            observation = publish_observer.snapshot() if publish_observer is not None else {}
            ui_success = success_observer.snapshot() if success_observer is not None else {"matched": False}
            if ui_success.get("matched") and ui_success.get("visible"):
                defer_success_observer_close = True
                evidence = dict(observation)
                evidence["ui_success_code"] = str(ui_success.get("code") or "")
                evidence["ui_success_role"] = str(ui_success.get("semantic_role") or "")
                evidence["ui_success_in_dialog"] = bool(ui_success.get("in_dialog"))
                dump.capture(
                    page, "upload_success_detected",
                    f"visible_ui_success:{ui_success.get('code')}:{ui_success.get('semantic_role')}",
                    force_snapshot=True,
                )
                dump.capture_safe_dom(page, "upload_posted_visible_ui_success")
                if evidence.get("media_id") or evidence.get("shortcode") or evidence.get("permalink"):
                    return {
                        "ok": True, "status": "POSTED", "observation": evidence,
                        "success_cleanup_pending": True,
                        "_success_observer": success_observer,
                    }
                return {
                    "ok": True,
                    "status": "UPLOADED_UNVERIFIED",
                    "observation": evidence,
                    "error": "visible Instagram publish success; media identity pending",
                    "success_cleanup_pending": True,
                    "_success_observer": success_observer,
                }
            if state.get("state") == "SHARE_FAILED":
                dump.capture(page, "upload_share_failed_dialog", json.dumps(state, ensure_ascii=False)[:1200], force_snapshot=True)
                return submitted_unverified_result(observation)
            if observation.get("request_state") == "rejected":
                dump.capture(page, "upload_publish_rejected", json.dumps({
                    "path": observation.get("safe_path"), "http_status": observation.get("http_status"),
                }), force_snapshot=True)
                return {
                    "ok": False, "status": "PUBLISH_REJECTED",
                    "error": f"publish request rejected with HTTP {int(observation.get('http_status') or 0)}",
                    "observation": observation, "proven_rejection": True,
                }
            if observation.get("request_state") == "accepted":
                if observation.get("media_id") or observation.get("shortcode") or observation.get("permalink"):
                    dump.capture(page, "upload_posted", "publish response accepted with media identity", force_snapshot=True)
                    return {"ok": True, "status": "POSTED", "observation": observation}
                accepted_without_identity = True
                if n in (0, 10, 25, 45, 70):
                    dump.capture(
                        page, "upload_processing",
                        f"publish accepted; bounded processing observation {n + 1}",
                    )
            # Precise classification (InstaBotPro phrase sets): success / soft-block /
            # rate-limit / challenge / error — no bare "shared" false positives.
            if ig_signals is not None:
                kind, matched = ig_signals.classify(txt)
                if kind == "success":
                    dump.capture(page, "upload_posted", f"success:{matched}", force_snapshot=True)
                    return {"ok": True, "status": "POSTED", "observation": observation}
                if kind in ("blocked", "rate_limit"):
                    until = ig_signals.note_signal(account, kind)
                    status = "RATE_LIMITED" if kind == "rate_limit" else "BLOCKED"
                    dump.capture(page, "upload_softblock_" + kind, f"{matched} cooldown_until={until:.0f}", force_snapshot=True)
                    return {"ok": False, "status": status, "error": f"{kind}: {matched}", "cooldown_until": until}
                if kind == "challenge":
                    dump.capture(
                        page,
                        "upload_body_signal_hint",
                        f"challenge:{matched}",
                    )
                if kind == "error":
                    dump.capture(page, "upload_error", matched, force_snapshot=True)
                    return {"ok": False, "status": "FAILED", "error": f"upload error: {matched}"}
            else:
                if any(s in txt for s in ["your post has been shared", "post shared", "reel shared", "опубликовано"]):
                    dump.capture(page, "upload_posted", "success", force_snapshot=True)
                    return {"ok": True, "status": "POSTED", "observation": observation}
            if state.get("state") == "PROCESSING":
                if n in (0, 10, 25, 45, 70):
                    dump.capture(page, "upload_processing", f"processing observation {n + 1}")
                time.sleep(2)
                continue
            signal = manual_signal(page)
            if signal and signal not in {"log in", "sign up"}:
                dump.capture(page, "manual_required_after_share", error=signal, force_snapshot=True)
                # The durable Share boundary has already been crossed. A later
                # dialog can require reconciliation, but never replay.
                if intent_persisted:
                    return submitted_unverified_result(observation)
                return {"ok": False, "status": "MANUAL_REQUIRED", "error": signal}
            time.sleep(2)
            if n in (10, 25, 45, 70):
                dump.capture(page, "upload_waiting", f"waited {n * 2}s")
        if accepted_without_identity:
            dump.capture_safe_dom(page, "upload_processing_bounded_timeout")
            dump.capture(
                page, "upload_processing_bounded_timeout",
                "publish accepted; bounded visual wait elapsed; reconciliation scheduled",
                force_snapshot=True,
            )
            return {
                "ok": False, "status": "PROCESSING",
                "error": "publish accepted; bounded Instagram processing wait elapsed",
                "observation": publish_observer.snapshot() if publish_observer is not None else {},
            }
        result = submitted_unverified_result(publish_observer.snapshot() if publish_observer is not None else {})
        dump.capture(page, "upload_submitted_unverified", result["error"], force_snapshot=True)
        return result
    except Exception as exc:
        dump.capture(page, "upload_exception", error=str(exc), force_snapshot=True)
        if intent_persisted:
            return submitted_unverified_result(publish_observer.snapshot() if publish_observer is not None else {})
        return {"ok": False, "status": "FAILED", "error": str(exc)}
    finally:
        if success_observer is not None and not defer_success_observer_close:
            success_observer.close()
        if publish_observer is not None:
            publish_observer.close()


def upload_video_web(
    page,
    dump: LiveDump,
    video_path: str,
    caption: str,
    mode: str = "desktop",
    account: str = "",
    *,
    job_id: int = 0,
    history_id: int = 0,
) -> Dict:
    """Publish one Reel using typed goals and fresh structural observations."""
    video_path = str(Path(video_path).expanduser().resolve())
    if not Path(video_path).exists():
        return {"ok": False, "error": f"video not found: {video_path}"}
    adapter: _CleanWebPublishAdapter | None = None
    keep_success_observer = False
    try:
        dump.capture(page, "upload_open_instagram", "open instagram")
        goto_fast(page, "https://www.instagram.com/?hl=en", timeout=18000)
        force_english(page, dump)
        dump.capture(
            page, "upload_instagram_loaded", "instagram shell visible",
            force_snapshot=True,
        )
        human = _human(page, account, dump)
        if human is not None:
            try:
                human.wander(1)
                human.dwell(0.5, 1.2)
            except Exception:
                pass
        consent = resolve_consent_for_workflow(page, dump, "upload")
        if not consent.get("ok"):
            return {
                "ok": False,
                "status": "MANUAL_REQUIRED",
                "error": "consent_failed",
            }
        adapter = _CleanWebPublishAdapter(
            page=page,
            dump=dump,
            video_path=video_path,
            caption=caption,
            account=account,
            job_id=job_id,
            history_id=history_id,
        )
        controller = PublishGoalController(adapter)
        pre_share = controller.run_pre_share()
        if not pre_share.goal_reached:
            evidence = adapter.evidence()
            if (
                pre_share.reconciliation_required
                or adapter.publish_intent
                or adapter.physical_share_attempted
            ):
                result = submitted_unverified_result(evidence)
                result["error"] = (
                    pre_share.error_category
                    or result["error"]
                )
                return result
            state = str(pre_share.operation_state or "")
            status = {
                PublishObservedState.LOGIN_REQUIRED.value: "LOGIN_REQUIRED",
                PublishObservedState.CHECKPOINT_OR_CHALLENGE.value: "CHECKPOINT",
                PublishObservedState.ACCOUNT_RESTRICTED.value: "BLOCKED",
                PublishObservedState.INFRASTRUCTURE_FAILURE.value: "BROWSER_UNAVAILABLE",
            }.get(state, "MANUAL_REQUIRED")
            return {
                "ok": False,
                "status": status,
                "error": pre_share.error_category or adapter.last_error or state.lower(),
            }

        confirmation = controller.run_publication_confirmation()
        evidence = adapter.evidence()
        request_state = str(evidence.get("request_state") or "")
        has_identity = bool(
            evidence.get("media_id")
            or evidence.get("shortcode")
            or evidence.get("permalink")
        )
        ui_success = bool(evidence.get("ui_success_code"))
        if confirmation.goal_reached:
            if ui_success:
                dump.capture(
                    page,
                    "upload_success_detected",
                    f"visible_ui_success:{evidence.get('ui_success_code')}:"
                    f"{evidence.get('ui_success_role')}",
                    force_snapshot=True,
                )
                dump.capture_safe_dom(page, "upload_posted_visible_ui_success")
                keep_success_observer = True
            result = {
                "ok": True,
                "status": "POSTED" if has_identity else "UPLOADED_UNVERIFIED",
                "observation": evidence,
            }
            if not has_identity:
                result["error"] = (
                    "visible Instagram publish success; media identity pending"
                )
            if ui_success:
                result.update(
                    success_cleanup_pending=True,
                    _success_observer=adapter.success_observer,
                    _publish_adapter=adapter,
                    _publish_controller=controller,
                )
            return result
        if request_state == "rejected":
            return {
                "ok": False,
                "status": "PUBLISH_REJECTED",
                "error": (
                    f"publish request rejected with HTTP "
                    f"{int(evidence.get('http_status') or 0)}"
                ),
                "observation": evidence,
                "proven_rejection": True,
            }
        if adapter.share_boundary and request_state == "accepted":
            dump.capture(
                page,
                "upload_processing_bounded_timeout",
                "publish accepted; goal watchdog scheduled reconciliation",
                force_snapshot=True,
            )
            return {
                "ok": False,
                "status": "PROCESSING",
                "error": "publish accepted; bounded Instagram processing wait elapsed",
                "observation": evidence,
            }
        result = submitted_unverified_result(evidence)
        result["error"] = (
            confirmation.error_category
            or "Share boundary crossed; publication outcome requires reconciliation"
        )
        return result
    except Exception as exc:
        dump.capture(page, "upload_exception", error=str(exc), force_snapshot=True)
        if adapter is not None and (
            adapter.publish_intent or adapter.physical_share_attempted
        ):
            return submitted_unverified_result(adapter.evidence())
        return {"ok": False, "status": "FAILED", "error": str(exc)}
    finally:
        if adapter is not None:
            adapter.close(keep_success_observer=keep_success_observer)


def _asset_by_id_web(asset_id: int, account: str) -> Optional[dict]:
    if not asset_id:
        return None
    c = db_conn()
    try:
        row = c.execute(
            """
            SELECT id,account_name,file_path,original_name,caption,status,content_kind
            FROM api_content_assets
            WHERE id=? AND (account_name='' OR account_name=?)
            """,
            (int(asset_id), account),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def _create_web_history(job_id: int, run_id: str, name: str, asset: dict, args: argparse.Namespace, caption: str, iteration: int) -> int:
    c = db_conn()
    try:
        return create_history(
            c,
            job_id=job_id,
            run_id=run_id,
            account_name=name,
            asset=asset,
            engine="clean_web",
            provider=args.provider,
            background_web=bool(args.headless),
            caption=caption,
            history_id=int(getattr(args, "history_id", 0) or 0) if iteration == 1 else 0,
            publication_slot_id=int(asset.get("publication_slot_id") or 0),
        )
    finally:
        c.close()


def account_lane(account: dict, args, run_id: str) -> None:
    name = account["name"]
    mode = args.mode
    forced_asset_id = int(getattr(args, "asset_id", 0) or 0)
    forced_retry = bool(forced_asset_id or int(getattr(args, "history_id", 0) or 0))
    dump = LiveDump(run_id, name, max_snapshots=args.max_snapshots)
    content_mode = str(account.get("web_upload_content_mode") or "scale").strip().lower()
    if content_mode not in ("scale", "quality"):
        content_mode = "scale"
    force = bool(getattr(args, "ignore_cooldown", False)) or forced_retry
    active_history_id = 0
    plan_progress: Dict[str, Any] = {}

    if ig_signals is not None and not force:
        left = ig_signals.cooldown_left(name)
        if left > 0:
            job_id = create_job(run_id, name, mode, args.provider, 1, str(dump.root))
            update_job(job_id, status="cooldown", current_step=f"resting {left/60:.0f}m", last_error=f"cooldown {left/60:.0f}m left", finished_at=now_iso())
            log(f"{name}: skipping, cooldown {left/60:.0f}m left", "WARNING")
            return
    if content_mode == "scale" and not force:
        next_at = str(account.get("web_upload_next_cycle_at") or "").strip()
        if next_at:
            try:
                due = datetime.fromisoformat(next_at)
            except Exception:
                due = None
            if due and due > datetime.now():
                mins = (due - datetime.now()).total_seconds() / 60.0
                job_id = create_job(run_id, name, mode, args.provider, 1, str(dump.root))
                update_job(job_id, status="cooldown", current_step=f"scale resting {mins:.0f}m", last_error=f"next cycle {next_at}", finished_at=now_iso())
                log(f"{name}: scale cycle resting {mins:.0f}m (next {next_at})", "WARNING")
                return

    cycle_count = int(account.get("web_upload_cycle_count") or 0)
    configured_set = None
    run_assets: List[dict] = []
    all_slots_already_accepted = False
    campaign_run_identity = ""
    planned_slot_count = 0
    campaign_progress: Dict[str, Any] = {}
    if forced_retry:
        asset = _asset_by_id_web(forced_asset_id, name)
        if asset:
            run_assets = [asset]
    elif content_mode == "quality":
        asset = reserve_asset(name, kind="quality")
        if asset:
            run_assets = [asset]
    else:
        c = db_conn()
        try:
            ensure_plan_schema(c)
            configured_set = next_plan_set(c, name)
        finally:
            c.close()
        if configured_set and configured_set.get("stopped"):
            job_id = create_job(run_id, name, mode, args.provider, 0, str(dump.root))
            msg = "content plan completed — reset the plan position or add another set"
            update_account(name, web_upload_last_error=msg)
            update_job(job_id, status="no_content", current_step="content plan completed", last_error=msg, finished_at=now_iso())
            log(f"{name}: {msg}", "WARNING")
            return
        if configured_set and configured_set.get("items"):
            run_assets = [dict(item) for item in configured_set["items"]]
        elif configured_set and configured_set.get("configured"):
            run_assets = []
        else:
            asset = reserve_asset(name, kind="scale")
            if asset:
                legacy_posts = SCALE_FIRST_CYCLE_POSTS if cycle_count == 0 else SCALE_STEADY_POSTS
                run_assets = [dict(asset) for _ in range(legacy_posts)]

    for item in run_assets:
        if item.get("asset_id"):
            item["plan_item_id"] = item.get("id")
            item["id"] = int(item["asset_id"])
    if run_assets and not forced_retry:
        planned_slot_count = len(run_assets)
        first_planned_asset = int(run_assets[0].get("id") or 0)
        if content_mode == "quality":
            campaign_run_identity = f"quality:{run_id}"
        elif configured_set and configured_set.get("strategy") == "custom":
            campaign_run_identity = (
                f"scale-custom:set-{int(configured_set.get('set_id') or 0)}:"
                f"cycle-{cycle_count}"
            )
        else:
            campaign_run_identity = f"scale-standard:asset-{first_planned_asset}:cycle-{cycle_count}"
        for slot_order, item in enumerate(run_assets, start=1):
            plan_item_id = int(item.get("plan_item_id") or 0)
            item["slot_key"] = (
                f"plan-item:{plan_item_id}" if plan_item_id else f"repeat:{slot_order}"
            )
        c = db_conn()
        try:
            run_assets = prepare_publication_slots(
                c,
                account_name=name,
                campaign_run_identity=campaign_run_identity,
                items=run_assets,
            )
            campaign_progress = slot_progress(c, name, campaign_run_identity)
            all_slots_already_accepted = (
                planned_slot_count > 0
                and int(campaign_progress.get("completed") or 0) == planned_slot_count
            )
        finally:
            c.close()
    target = planned_slot_count or len(run_assets) or 1
    posted = int(campaign_progress.get("completed") or 0)
    job_id = create_job(
        run_id, name, mode, args.provider, target, str(dump.root),
        campaign_run_identity=campaign_run_identity,
        posted_count=posted,
    )
    submitted_unverified = 0
    if not run_assets:
        if all_slots_already_accepted and content_mode == "scale":
            next_cycle = (datetime.now() + timedelta(hours=SCALE_COOLDOWN_HOURS)).isoformat(timespec="seconds")
            update_account(
                name,
                web_upload_cycle_count=cycle_count + 1,
                web_upload_next_cycle_at=next_cycle,
                web_upload_last_error="",
            )
            update_job(
                job_id, status="success", current_step="all publication slots already accepted",
                posted_count=posted, finished_at=now_iso(), last_error="",
            )
            return
        msg = f"{name}: no ready {content_mode} content"
        if forced_asset_id:
            msg = f"{name}: content asset #{forced_asset_id} is missing or belongs to another account"
        update_account(name, web_upload_last_error=msg)
        update_job(job_id, status="no_content", current_step="no ready content", posted_count=0, finished_at=now_iso(), last_error=msg)
        if int(getattr(args, "history_id", 0) or 0):
            c = db_conn()
            try:
                mark_failed(c, int(args.history_id), msg)
            finally:
                c.close()
        log(msg, "WARNING")
        return
    for item in run_assets:
        if not item.get("file_path") or not Path(str(item["file_path"])).is_file():
            msg = f"{name}: content file missing for asset #{int(item.get('asset_id') or item.get('id') or 0)}"
            update_account(name, web_upload_last_error=msg)
            update_job(job_id, status="failed", current_step="content file missing", last_error=msg, finished_at=now_iso())
            log(msg, "ERROR")
            return

    first_asset = run_assets[0]
    first_caption = args.caption or first_asset.get("caption_override") or first_asset.get("caption") or ""
    active_history_id = _create_web_history(job_id, run_id, name, first_asset, args, first_caption, 1)
    first_history_id = active_history_id
    ctx_obj = context = page = None
    network_capture = None
    try:
        with contextlib.ExitStack() as _stack:
            if args.provider == "camoufox":
                ctx_obj, context, page = open_context(None, name, mode=mode, provider="camoufox", headless=args.headless, no_proxy=args.no_proxy)
            else:
                sync_playwright, _ = require_playwright()
                p = _stack.enter_context(sync_playwright())
                ctx_obj, context, page = open_context(p, name, mode=mode, provider="playwright", headless=args.headless, no_proxy=args.no_proxy)
            dump.capture(page, "browser_opened", f"provider={args.provider} mode={mode}", force_snapshot=True)
            if start_instagram_network_capture is not None:
                network_capture = start_instagram_network_capture(
                    context, dump.root, account=name, run_id=run_id, phase="upload_and_warmup"
                )
                if network_capture is not None:
                    log(f"{name}: Instagram GraphQL/private API capture active -> {dump.root / 'network'}", "OK")
                    dump.capture(page, "network_capture_active", "HAR + request/response/payload/JSON")
                else:
                    log(f"{name}: network capture unavailable", "WARNING")

            def _fail(res, history_id: int) -> bool:
                """Persist one slot's terminal result; true means later slots continue."""
                nonlocal posted, submitted_unverified
                status = res.get("status")
                error = res.get("error") or str(res)
                if status in {"UPLOADED_UNVERIFIED", "PROCESSING"}:
                    posted += 1
                    if history_id:
                        c = db_conn()
                        try:
                            finalize_publication_attempt(
                                c, history_id=history_id, job_id=job_id,
                                outcome="processing" if status == "PROCESSING" else "uploaded_unverified",
                                error=error,
                                account_name=name, posted_count=posted,
                                job_status=(
                                    ("success" if posted == target else "partial_success")
                                    if content_mode == "scale"
                                    else ("processing" if status == "PROCESSING" else "uploaded_unverified")
                                ),
                                job_step=(
                                    f"Scale {posted}/{target}; {status.lower()}"
                                    if content_mode == "scale" else status.lower()
                                ),
                                observation=res.get("observation"),
                            )
                        finally:
                            c.close()
                    submitted_unverified += 1
                    return False
                if status == "PUBLISH_REJECTED" and history_id and res.get("proven_rejection"):
                    c = db_conn()
                    try:
                        finalize_publication_attempt(
                            c, history_id=history_id, job_id=job_id,
                            outcome="failed", error=error,
                            account_name=name, posted_count=posted, job_status="failed",
                            job_step="publish_rejected", observation=res.get("observation"),
                            proven_rejection=True,
                        )
                    finally:
                        c.close()
                    update_account(name, web_upload_last_error=error)
                    return False
                local_states = {
                    "LOGIN_REQUIRED", "TWO_FACTOR_REQUIRED", "HUMAN_VERIFICATION", "CHECKPOINT",
                    "CHALLENGE", "SUSPENDED", "DISABLED", "CONSENT_REQUIRED",
                    "BLOCKING_DIALOG_NOT_DISMISSED", "BLANK_DOCUMENT", "PAGE_CLOSED",
                    "BROWSER_UNAVAILABLE", "CREATE_CONTROL_NOT_FOUND", "CREATE_CLICK_NO_TRANSITION",
                    "MANUAL_REQUIRED", "BLOCKED", "RATE_LIMITED",
                }
                st = "manual_required" if status in local_states else "failed"
                if history_id:
                    c = db_conn()
                    try:
                        mark_failed(c, history_id, error)
                    finally:
                        c.close()
                update_job(job_id, status=st, current_step=status or st, posted_count=posted, last_error=error, finished_at=now_iso())
                fields = {"web_upload_last_error": error}
                if status in {"LOGIN_REQUIRED", "TWO_FACTOR_REQUIRED", "HUMAN_VERIFICATION", "CHECKPOINT", "CHALLENGE", "SUSPENDED", "DISABLED"}:
                    fields["web_upload_login_status"] = str(status).lower()
                update_account(name, **fields)
                if st == "manual_required":
                    hold_manual_required_page(page, dump, error, headless=bool(args.headless))
                return False

            pre_warm = random.uniform(float(args.pre_warmup_min), float(args.pre_warmup_max))
            update_job(job_id, current_step=f"pre-warmup {pre_warm:.1f}m")
            if pre_warm > 0:
                warm = warmup_web(page, dump, pre_warm, mode=mode, account=name)
                policy = pre_warmup_policy(warm)
                if policy.get("skipped"):
                    warning = str(policy.get("warning") or "pre_warmup_skipped")
                    reason = str(policy.get("reason") or warm.get("error") or "")
                    dump.capture(
                        page, warning,
                        action=f"best-effort pre-warmup skipped: {reason}",
                        force_snapshot=True,
                    )
                    log(f"{name}: {warning}: {reason}", "WARNING")
                elif not policy.get("continue"):
                    error = str(policy.get("error") or warm.get("error") or str(warm))
                    update_job(
                        job_id,
                        status=str(policy.get("status") or "failed"),
                        current_step=str(policy.get("step") or "pre-warmup failed"),
                        last_error=error,
                        finished_at=now_iso(),
                    )
                    update_account(name, web_upload_last_error=error)
                    if active_history_id:
                        c = db_conn()
                        try:
                            mark_failed(c, active_history_id, error)
                        finally:
                            c.close()
                    return

            page = ensure_single_browser_page(page, dump)
            set_title = str((configured_set or {}).get("title") or "")
            for pending_index, asset in enumerate(run_assets, start=1):
                caption = args.caption or asset.get("caption_override") or asset.get("caption") or ""
                active_history_id = first_history_id if pending_index == 1 else _create_web_history(
                    job_id, run_id, name, asset, args, caption, pending_index
                )
                prefix = f"{set_title} · " if set_title else ""
                update_job(
                    job_id,
                    current_step=f"{prefix}Web upload {posted + 1}/{target} ({asset.get('slot_key') or 'slot'})",
                    posted_count=posted,
                )
                page = ensure_single_browser_page(page, dump)
                res = upload_video_web(page, dump, asset["file_path"], caption, mode=mode, account=name, job_id=job_id, history_id=active_history_id)
                if not res.get("ok"):
                    if _fail(res, active_history_id):
                        # Share may already have published this exact repetition.
                        # Its history row occupies the slot; never retry it or
                        # manufacture a compensating repetition.
                        continue
                    return
                posted += 1
                result_status = str(res.get("status") or "POSTED")
                accepted_unverified = result_status in {"UPLOADED_UNVERIFIED", "PROCESSING"}
                if accepted_unverified:
                    submitted_unverified += 1
                c = db_conn()
                try:
                    finalized = finalize_publication_attempt(
                        c, history_id=active_history_id, job_id=job_id,
                        outcome=("processing" if result_status == "PROCESSING" else
                                 "uploaded_unverified" if accepted_unverified else "confirmed"),
                        error=str(res.get("error") or ""),
                        asset_id=int(asset["id"]), account_name=name,
                        posted_count=posted,
                        job_status="success" if posted == target else "running",
                        job_step=(
                            f"{prefix}Web accepted {posted}/{target}; verification pending"
                            if accepted_unverified else f"{prefix}Web posted {posted}/{target}"
                        ),
                        plan_item_id=int(asset.get("plan_item_id") or 0),
                        observation=res.get("observation"),
                    )
                    if not finalized.get("committed") and not finalized.get("already_finalized"):
                        raise RuntimeError(f"publication finalization failed: {finalized.get('error') or finalized.get('result')}")
                finally:
                    c.close()
                cleanup = {"ready": True, "status": "not_required"}
                deferred_observer = res.get("_success_observer")
                publish_adapter = res.get("_publish_adapter")
                publish_controller = res.get("_publish_controller")
                if res.get("success_cleanup_pending"):
                    def emit_cleanup(event: str, payload: Dict[str, Any]) -> None:
                        dump.capture(
                            page, event,
                            json.dumps(payload, ensure_ascii=True, sort_keys=True)[:1800],
                            force_snapshot=event in {
                                "upload_success_done_search",
                                "upload_success_done_dialog_closed",
                                "upload_success_done_cleanup_failed",
                            },
                        )
                    try:
                        # Publication accounting above is the durable boundary.
                        # Done is UI cleanup and can never revoke that result.
                        if publish_controller is not None and publish_adapter is not None:
                            cleanup_goal = publish_controller.run_goal(
                                PublishGoal.CLEANUP_COMPLETED,
                                timeout_seconds=12.0,
                                max_observations=20,
                            )
                            cleanup = dict(publish_adapter.cleanup_result)
                            if not cleanup_goal.goal_reached:
                                cleanup["ready"] = False
                                cleanup["status"] = (
                                    cleanup_goal.error_category
                                    or cleanup.get("status")
                                    or "cleanup_goal_incomplete"
                                )
                        else:
                            cleanup = cleanup_success_dialog(page, emit=emit_cleanup, wait_seconds=4.0)
                    finally:
                        if publish_adapter is not None:
                            publish_adapter.close()
                        elif deferred_observer is not None:
                            deferred_observer.close()
                    if not cleanup.get("ready") and pending_index < len(run_assets):
                        error = f"success dialog cleanup incomplete: {cleanup.get('status') or 'unknown'}"
                        update_job(job_id, **partial_success_after_warmup(posted, target, error))
                        dump.capture(page, "upload_success_cleanup_partial", error=error, force_snapshot=True)
                        return
                # A confirmed publication is irreversible business evidence.
                # Consume its asset before optional post-warmup so a later
                # warmup warning cannot put it back into automatic retry.
                if pending_index < len(run_assets):
                    post_warm = random.uniform(float(args.post_warmup_min), float(args.post_warmup_max))
                    update_job(job_id, current_step=f"post-warmup {post_warm:.1f}m", posted_count=posted)
                    if post_warm > 0:
                        warm = warmup_web(page, dump, post_warm, mode=mode, account=name)
                        if not warm.get("ok"):
                            error = warm.get("error") or str(warm)
                            update_job(job_id, **partial_success_after_warmup(posted, target, error))
                            update_account(name, web_upload_last_error=error)
                            dump.capture(page, "post_warmup_failed", error=error, force_snapshot=True)
                            return

            if forced_retry:
                if content_mode == "quality":
                    mark_asset(int(run_assets[0]["id"]), "uploaded", name)
                update_job(job_id, status="success", current_step="retry posted through Clean Web", posted_count=posted, finished_at=now_iso())
                dump.capture(page, "done", "retry posted", force_snapshot=True)
            elif content_mode == "quality":
                mark_asset(int(run_assets[0]["id"]), "uploaded", name)
                update_job(job_id, status="success", current_step="quality: 1 unique posted", posted_count=posted, finished_at=now_iso())
                dump.capture(page, "done", f"quality: posted {posted}", force_snapshot=True)
            else:
                c = db_conn()
                try:
                    campaign_progress = slot_progress(c, name, campaign_run_identity)
                finally:
                    c.close()
                posted = int(campaign_progress.get("completed") or posted)
                if posted < target:
                    update_job(
                        job_id,
                        status="partial_success",
                        current_step=f"Scale {posted}/{target}; {target - posted} slot(s) remaining",
                        posted_count=posted,
                        finished_at=now_iso(),
                    )
                    return
                if configured_set and configured_set.get("configured"):
                    if configured_set.get("strategy") == "standard":
                        done_step = f"standard scale done ({posted})"
                    else:
                        advanced = plan_progress or {
                            "current_set_order": int(configured_set.get("set_order") or 0),
                            "is_stopped": False,
                        }
                        plan_note = "plan stopped" if advanced.get("is_stopped") else f"next launch {int(advanced.get('current_set_order') or 0) + 1}"
                        done_step = f"{set_title or 'content pattern'} done ({posted}); {plan_note}"
                else:
                    done_step = f"legacy scale cycle #{cycle_count + 1} done ({posted})"
                next_cycle = (datetime.now() + timedelta(hours=SCALE_COOLDOWN_HOURS)).isoformat(timespec="seconds")
                update_account(name, web_upload_cycle_count=cycle_count + 1, web_upload_next_cycle_at=next_cycle, web_upload_last_error="")
                if submitted_unverified:
                    update_job(
                        job_id, status="success",
                        current_step=(f"planned={target}; accepted={posted}; verification_pending={submitted_unverified}; "
                                      f"remaining_in_cycle=0; next in {SCALE_COOLDOWN_HOURS:.0f}h"),
                        posted_count=posted, finished_at=now_iso(),
                    )
                else:
                    update_job(job_id, status="success", current_step=f"{done_step}; next in {SCALE_COOLDOWN_HOURS:.0f}h", posted_count=posted, finished_at=now_iso())
                dump.capture(page, "done", f"{done_step}; next {next_cycle}", force_snapshot=True)
    except Exception as exc:
        preserved_verified = False
        c = db_conn()
        try:
            preserved_verified = preserve_verified_publication_job(
                c, job_id, stop_reason="worker_interrupted_after_verified_publication"
            )
            if preserved_verified:
                c.commit()
        finally:
            c.close()
        if preserved_verified:
            log(
                f"{name}: worker cleanup interrupted after verified publication; "
                "verified result preserved",
                "WARNING",
            )
            return
        if active_history_id:
            c = db_conn()
            try:
                mark_failed(c, active_history_id, str(exc))
            finally:
                c.close()
        elif int(getattr(args, "history_id", 0) or 0):
            c = db_conn()
            try:
                mark_failed(c, int(args.history_id), str(exc))
            finally:
                c.close()
        update_job(job_id, status="failed", current_step="crashed", posted_count=posted, last_error=str(exc), finished_at=now_iso())
        update_account(name, web_upload_last_error=str(exc))
        try:
            if page:
                dump.capture(page, "crashed", error=str(exc), force_snapshot=True)
        except Exception:
            pass
        log(f"{name}: crashed: {exc}", "ERROR")
    finally:
        try:
            if network_capture is not None:
                network_capture.stop()
                log(f"{name}: network capture saved -> {dump.root / 'network'}", "OK")
        except Exception as exc:
            log(f"{name}: network capture finalize failed: {type(exc).__name__}", "WARNING")
        try:
            if context and args.provider == "camoufox":
                _save_camoufox_state(context, name, mode)
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if ctx_obj:
                ctx_obj.__exit__(None, None, None)
        except Exception:
            pass
        # subprocess.run() in the connection scheduler must not return until
        # the native Camoufox window has had time to finish shutting down.
        if context is not None or ctx_obj is not None:
            time.sleep(1.0)

def main() -> int:
    ap = argparse.ArgumentParser(description="Instagram desktop-first web upload with live dumps")
    ap.add_argument("--accounts", default="", help="Comma/newline separated account names. Empty = all enabled web-upload accounts")
    ap.add_argument("--target", type=int, default=1)
    ap.add_argument("--mode", choices=["desktop", "mobile_like"], default="desktop")
    ap.add_argument("--provider", choices=["playwright", "camoufox"], default="camoufox")
    ap.add_argument("--caption", default="")
    ap.add_argument("--pre-warmup-min", type=float, default=1.0)
    ap.add_argument("--pre-warmup-max", type=float, default=3.0)
    ap.add_argument("--post-warmup-min", type=float, default=2.0)
    ap.add_argument("--post-warmup-max", type=float, default=5.0)
    ap.add_argument("--cooldown-hours", type=float, default=4.0)
    ap.add_argument("--max-snapshots", type=int, default=40)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-proxy", action="store_true", help="Ignore saved account proxies for this local run")
    ap.add_argument("--ignore-cooldown", action="store_true", help="Upload even if the account is resting after a block/rate-limit")
    ap.add_argument("--asset-id", type=int, default=0)
    ap.add_argument("--history-id", type=int, default=0)
    ap.add_argument("--max-workers", type=int, default=1, help="Run this many accounts in parallel. 1 = sequential.")
    args = ap.parse_args()

    ensure_schema()
    names = normalise_accounts(args.accounts)
    accounts = selected_accounts(names)
    if not accounts:
        log("No enabled accounts selected for Instagram Web Upload", "WARNING")
        return 2
    run_id = str(
        os.environ.get("SPARKGRID_RUN_ID")
        or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    )
    workers = max(1, min(int(args.max_workers or 1), len(accounts), 8))
    log(f"Starting run_id={run_id}; accounts={len(accounts)}; mode={args.mode}; provider={args.provider}; workers={workers}", "OK")
    if workers <= 1:
        for acc in accounts:
            account_lane(acc, args, run_id)
            time.sleep(random.uniform(2.0, 6.0))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for acc in accounts:
                futures.append(pool.submit(account_lane, acc, args, run_id))
                time.sleep(random.uniform(0.8, 2.2))
            for fut in as_completed(futures):
                fut.result()
    log("Instagram Web Upload engine finished", "OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
