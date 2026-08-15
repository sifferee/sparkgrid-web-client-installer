"""Typed, privacy-safe browser lifecycle evidence used by ISSUE-018.

This module intentionally does not launch browsers or own proxy recovery.  It
normalizes evidence already available to a worker/scheduler so callers retain
their existing workflow and publication contracts.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LIFECYCLE_RESULTS = {
    "blank_document", "main_frame_network_failed", "navigation_timeout", "page_closed",
    "context_closed", "browser_unavailable", "renderer_unavailable", "worker_process_exited",
    "worker_process_missing", "browser_process_exited", "browser_start_stalled",
    "workflow_stalled", "lifecycle_state_unknown", "recovery_exhausted",
    "startup_reconciliation_required", "heartbeat_transport_error",
    "worker_liveness_lost", "browser_process_tree_missing",
}
IRREVERSIBLE_MARKERS = (
    "share_clicked", "reel_publish_intent", "publish_intent", "publish_clicked", "submitted_unverified", "confirmed", "uploaded",
    "story_share_clicked", "story_publish_clicked", "credentials_submitted", "otp_submitted",
    "two_factor_code_submitted",
)

def route_category(url: str) -> str:
    path = urlparse(str(url or "")).path.lower()
    if "login" in path or "emailsignup" in path: return "login"
    if "two_factor" in path or "two_step" in path: return "two_factor"
    if "challenge" in path or "checkpoint" in path: return "challenge"
    if "suspend" in path: return "suspended"
    if "disabled" in path: return "disabled"
    if "create" in path: return "create"
    return "instagram" if path else "unknown"

def safe_structural_hash(ready_state: str, body_length: int, child_count: int, title_length: int) -> str:
    value = f"{ready_state}|{int(body_length)}|{int(child_count)}|{int(title_length)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

def _alive(page: Any, attr: str) -> bool | None:
    try:
        obj = getattr(page, attr)
        if callable(obj): obj = obj()
        if hasattr(obj, "is_closed"): return not bool(obj.is_closed())
        return True
    except AttributeError:
        return None
    except Exception:
        return False

def classify_blank_document(page: Any, *, navigation_started: bool = True,
                            main_frame_failure: str = "", navigation_timeout: bool = False,
                            better_state: str = "") -> dict[str, Any]:
    """Classify only evidence-backed blank documents; never persist DOM text."""
    evidence: dict[str, Any] = {"page_alive": False, "context_alive": None, "browser_alive": None,
                                "route_category": "unknown", "ready_state": "unknown", "body_length": 0,
                                "body_child_count": 0, "title_length": 0, "main_frame_failure_category": str(main_frame_failure or "")}
    if page is None: return {"state": "browser_unavailable", "evidence": evidence}
    try:
        if page.is_closed(): return {"state": "page_closed", "evidence": evidence}
    except Exception:
        return {"state": "page_closed", "evidence": evidence}
    evidence["page_alive"] = True
    context_alive = _alive(page, "context")
    evidence["context_alive"] = context_alive
    if context_alive is False: return {"state": "context_closed", "evidence": evidence}
    browser_alive = _alive(getattr(page, "context", None), "browser") if getattr(page, "context", None) is not None else None
    evidence["browser_alive"] = browser_alive
    if browser_alive is False: return {"state": "browser_unavailable", "evidence": evidence}
    if main_frame_failure: return {"state": "main_frame_network_failed", "evidence": evidence}
    if navigation_timeout: return {"state": "navigation_timeout", "evidence": evidence}
    url = str(getattr(page, "url", "") or "")
    evidence["route_category"] = route_category(url)
    try:
        evidence["ready_state"] = str(page.evaluate("document.readyState") or "unknown")
        evidence["body_length"] = int(page.evaluate("document.body ? document.body.innerText.length : 0") or 0)
        evidence["body_child_count"] = int(page.evaluate("document.body ? document.body.children.length : 0") or 0)
        evidence["title_length"] = len(str(page.evaluate("document.title") or ""))
    except Exception:
        return {"state": "renderer_unavailable", "evidence": evidence}
    state = str(better_state or "")
    if state: return {"state": state, "evidence": evidence}
    if evidence["route_category"] in {"login", "two_factor", "challenge", "suspended", "disabled", "create"}:
        return {"state": evidence["route_category"], "evidence": evidence}
    if navigation_started and evidence["body_length"] == 0 and evidence["body_child_count"] == 0:
        return {"state": "blank_document", "evidence": evidence}
    return {"state": "lifecycle_state_unknown", "evidence": evidence}

def irreversible_stage(step: str) -> str:
    lowered = str(step or "").lower()
    return next((marker for marker in IRREVERSIBLE_MARKERS if marker in lowered), "none")

def retry_safe(step: str, workflow: str = "") -> bool:
    marker = irreversible_stage(step)
    if marker != "none": return False
    # credential/OTP retries are prohibited after their durable submit marker.
    return True

def heartbeat_payload(*, job_ref: str = "", account_ref: str = "", task_ref: str = "",
                      role: str = "worker",
                      workflow: str = "", operation: str = "", last_completed: str = "",
                      irreversible: str = "none", sequence: int = 0, started: float | None = None,
                      recovery_attempt: int = 0, build: str = "") -> dict[str, Any]:
    digest = lambda value: hashlib.sha256(str(value or "").encode()).hexdigest()[:12] if value else ""
    monotonic_now = time.monotonic()
    return {"job_ref": digest(job_ref), "run_ref": digest(job_ref),
            "account_ref": digest(account_ref), "task_ref": digest(task_ref),
            "worker_pid": os.getpid(), "worker_role": role, "process_role": role,
            "workflow_type": workflow, "current_operation": operation, "last_completed_operation": last_completed,
            "irreversible_stage": irreversible, "heartbeat_sequence": int(sequence),
            "monotonic_timestamp_ms": int(monotonic_now * 1000),
            "monotonic_elapsed_ms": int((monotonic_now - (started or monotonic_now)) * 1000),
            "recovery_attempt": int(recovery_attempt), "build": str(build or "unknown")[:64]}

def write_heartbeat(path: str, payload: dict[str, Any]) -> bool:
    target = Path(path)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, target)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


class IndependentHeartbeat:
    """Lightweight worker liveness pulse independent of Playwright calls."""

    def __init__(
        self,
        path: str,
        *,
        run_ref: str,
        account_ref: str,
        task_ref: str = "",
        workflow: str,
        role: str,
        interval_seconds: float = 5.0,
        recovery_attempt: int = 0,
        build: str = "",
        error_paths: tuple[str, ...] = (),
        writer=write_heartbeat,
    ) -> None:
        self.path = str(path or "")
        self.run_ref = str(run_ref or "")
        self.account_ref = str(account_ref or "")
        self.task_ref = str(task_ref or "")
        self.workflow = str(workflow or "")
        self.role = str(role or "worker")
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.recovery_attempt = int(recovery_attempt)
        self.build = str(build or "")
        self.error_paths = tuple(str(item) for item in error_paths if str(item))
        self.writer = writer
        self.started = time.monotonic()
        self.sequence = 0
        self.phase = "worker_ready"
        self.last_completed = ""
        self.transport_error = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _transport_error_payload(self) -> dict[str, Any]:
        return {
            "reason": "heartbeat_transport_error",
            "worker_pid": os.getpid(),
            "process_role": self.role,
            "heartbeat_sequence": self.sequence,
            "monotonic_timestamp_ms": int(time.monotonic() * 1000),
        }

    def _record_transport_error(self) -> None:
        self.transport_error = True
        encoded = json.dumps(
            self._transport_error_payload(), sort_keys=True
        )
        for target in self.error_paths:
            try:
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_text(encoded, encoding="utf-8")
            except OSError:
                pass

    def _clear_transport_error(self) -> None:
        # Diagnosed 2026-08-15: _record_transport_error() had no
        # counterpart. Once a SINGLE heartbeat write failed (plausible
        # under I/O contention when several browsers launch in parallel —
        # 3 of 4 hit this within the first few minutes in one observed
        # run), connection_scheduler.py's watchdog saw the error file and
        # `continue`d past ALL stall/hang detection for the rest of that
        # worker's life, because nothing ever deleted the file again even
        # after later heartbeat writes succeeded fine. One transient blip
        # near startup permanently disabled the safety net for the whole
        # run — including the already-known "Firefox freezes when its
        # window is backgrounded" failure mode (AGENTS.md, 2026-08-11),
        # which is exactly the kind of stall this watchdog exists to catch.
        #
        # Clearing on the next SUCCESSFUL write restores protection as
        # soon as the transport is actually healthy again, while leaving
        # the original defensive behavior untouched for a transport that
        # stays genuinely broken (no success ever arrives to trigger this,
        # so `continue`-past-stall-detection still applies for as long as
        # every write keeps failing — that caution was reasonable and is
        # not what this fix removes).
        if not self.transport_error:
            return
        self.transport_error = False
        for target in self.error_paths:
            try:
                Path(target).unlink(missing_ok=True)
            except OSError:
                pass

    def pulse(self) -> bool:
        if not self.path:
            return False
        with self._lock:
            self.sequence += 1
            payload = heartbeat_payload(
                job_ref=self.run_ref,
                account_ref=self.account_ref,
                task_ref=self.task_ref,
                role=self.role,
                workflow=self.workflow,
                operation=self.phase,
                last_completed=self.last_completed,
                sequence=self.sequence,
                started=self.started,
                recovery_attempt=self.recovery_attempt,
                build=self.build,
            )
            ok = bool(self.writer(self.path, payload))
            if not ok:
                self._record_transport_error()
            else:
                self._clear_transport_error()
            return ok

    def update_phase(self, phase: str, *, pulse: bool = True) -> bool:
        with self._lock:
            previous = self.phase
            self.phase = str(phase or self.phase)
            if previous and previous != self.phase:
                self.last_completed = previous
        return self.pulse() if pulse else True

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.pulse()

    def start(self) -> "IndependentHeartbeat":
        if not self.path or self._thread is not None:
            return self
        self.pulse()
        self._thread = threading.Thread(
            target=self._run,
            name=f"sparkgrid-liveness-{self.role}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
