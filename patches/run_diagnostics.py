"""Privacy-safe, run-correlated packaged diagnostics and retention.

This module deliberately accepts only normalized categories, booleans, bounded
integers, run-local opaque references, and timestamps.  It never copies legacy
logs, page text, DOM, URLs, selectors, exception messages, or account data.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DIAGNOSTICS_ROOT = DATA_DIR / "diagnostics"
RUNS_ROOT = DIAGNOSTICS_ROOT / "runs"
GLOBAL_CLEANUP_LEDGER = DIAGNOSTICS_ROOT / "cleanup.jsonl"

SCHEMA_VERSION = 1
PER_RUN_CAP_BYTES = 100 * 1024 * 1024
GLOBAL_CAP_BYTES = 2 * 1024 * 1024 * 1024
SUCCESS_RETENTION = timedelta(hours=48)
FAILURE_RETENTION = timedelta(days=7)
PRESERVE_SUCCESSES = 2
PRESERVE_FAILURES = 10
MAX_SNAPSHOTS = 12
MAX_IMAGE_WIDTH = 1280
MAX_IMAGE_HEIGHT = 720

STREAMS = {
    "events": "events.jsonl",
    "process_events": "process_events.jsonl",
    "task_index": "task_index.jsonl",
    "outcomes": "outcomes.jsonl",
    "actions": "actions.jsonl",
    "cleanup": "cleanup.jsonl",
}
EVENT_TYPES = {
    "task_accepted", "task_rejected", "scheduler_start", "scheduler_exit",
    "disk_preflight", "connection_assignment", "connection_rotation",
    "child_spawn", "child_start", "child_exit", "browser_liveness",
    "context_liveness", "document_classified", "popup_classified",
    "regional_ads_consent_step", "login_surface_classified",
    "username_interaction", "password_interaction",
    "submission_readiness", "retry_counter", "domain_outcome",
    "infrastructure_outcome", "closure", "diagnostic_limit_reached",
    "visual_capture", "visual_capture_skipped", "retention_cleanup",
    "source_routing_fingerprint", "arrival_route_selected",
    "known_popup_handler_completed", "credential_workflow_started",
    "account_detail_finalized",
}
OUTCOMES = {
    "", "unknown", "success", "failed", "auto_login",
    "insufficient_disk_space", "scheduler_rejected",
    "connection_rotation_failed_before_browser_launch", "browser_start_failed",
    "browser_load_failed_after_retry", "worker_exit_nonzero", "worker_exit_0",
    "missing_persisted_workflow_outcome", "cancelled", "suspended",
    "challenge_required", "login_failed", "logged_in",
    "password_submission_blocked", "unsupported_login_state", "stopped",
    "rotation_request_failed", "rotation_request_accepted",
    "rotation_endpoint_timeout", "rotation_endpoint_connection_failure",
    "rotation_endpoint_auth_failure", "rotation_endpoint_rate_limited",
    "rotation_endpoint_busy", "proxy_auth_failed", "proxy_connection_failed",
    "proxy_connection_timeout", "proxy_unreachable", "proxy_readiness_timeout",
    "proxy_gate_internal_error", "static_proxy_pool_exhausted",
    "worker_not_started", "proxy_gate_failed", "rotation_stale_ip_confirmed",
    "rotation_stale_ip_after_retry", "rotation_accepted_but_not_ready",
    "rotation_verified", "not_started", "stopped_before_start",
    "unrecognized_surface", "ads_consent_action_unavailable",
    "ads_consent_loop_detected", "ads_consent_transition_timeout",
    "cookie_consent_action_unavailable", "cookie_consent_loop_detected",
    "cookie_consent_transition_timeout", "request_processing_action_unavailable",
    "request_processing_loop_detected", "request_processing_transition_timeout",
    "make_public_failed", "invalid_credentials_after_ip_retry",
    "mobile_rotation_not_stabilized", "consent_required",
    "login_loading_without_request", "username_field_not_ready",
    "profile_created", "two_factor_code_required", "challenge_detected",
    "browser_internal_error", "mobile_proxy_recovery_exhausted",
    "two_factor_transition_timeout", "incorrect_credentials",
    "manual_required", "auto_login_username_field_not_found",
    "auto_login_no_form", "auto_login_blank_reload", "consent_failed",
    "mobile_proxy_blank_document_after_retry",
    "mobile_proxy_network_failed_after_retry",
    "static_replacement_exit_ip_unchanged", "static_replacement_not_ready",
}
CATEGORIES = {
    "", "unknown", "accepted", "rejected", "pending", "starting", "running",
    "finished", "exited", "cancelled", "success", "failed", "active",
    "inactive", "available", "unavailable", "passed", "blocked", "skipped",
    "desktop", "mobile", "direct", "static", "phone", "camoufox",
    "playwright", "not_applicable", "rotation_required",
    "rotation_not_required", "rotation_lease_acquired", "rotation_lease_waiting",
    "rotation_lease_conflict", "rotation_request_dispatched",
    "rotation_request_failed", "rotation_request_timed_out",
    "rotation_ip_changed", "rotation_ip_unchanged",
    "rotation_validation_failed", "connection_unavailable_after_rotation",
    "malformed_rotation_configuration", "browser_internal_error",
    "blank_document", "instagram_document", "unknown_document",
    "login_combined", "login_username_first", "login_password_only",
    "authenticated", "challenge", "two_factor", "cookie_consent",
    "regional_ads_consent", "save_login_info", "notifications_prompt",
    "promo_or_ad", "open_in_app", "unknown_blocker", "none", "visible",
    "enabled", "editable", "verified", "not_ready", "ready", "attempted",
    "not_attempted", "dispatched", "duplicate_blocked", "transition_observed",
    "transition_timeout", "document_replaced", "react_mutation",
    "main_frame_timeout", "dns_failure", "tls_failure", "proxy_tunnel_failure",
    "connection_reset", "generic_navigation_failure", "operator_stop",
    "process_manager", "connection_scheduler", "browser_workflow",
    "worker_process", "user_stop", "startup", "finalization", "age_limit",
    "global_cap", "per_run_cap", "deduplicated", "sensitive_input_populated",
    "snapshot_limit", "unsupported_image_codec", "first_meaningful_surface",
    "recognized_blocker", "regional_ads_transition", "before_popup_action",
    "after_popup_action", "browser_load_failure", "unknown_state",
    "password_submission_blocker", "terminal_failure", "final_success",
    "instagram_root", "navigation_timeout", "navigation_failed",
    "unknown_failure",
    "blocker_detected", "consent_blocker", "transitioning",
    "unsupported_stable", "click_fill", "native_setter", "reacquire",
    "active_resource_conflict", "browser_start_failed",
    "process_exit_observed", "normal_exit", "process_exit_nonzero",
    "stop_all", "targeted_stop", "scheduler_cleanup",
    "worker_process_missing", "connection_rotation_failed_before_browser_launch",
    "browser_load_failed_after_retry", "missing_persisted_workflow_outcome",
    "known_popup", "credential_surface", "transitional",
    "unrecognized_surface", "completed", "handled_reevaluate",
    "no_blocker", "transitioning_retry", "action_unavailable",
    "worker", "scheduler", "synthetic",
    "scheduler_completed_before_account_start", "stopped_before_start",
}
VISUAL_REASONS = {
    "first_meaningful_surface", "recognized_blocker", "regional_ads_transition",
    "before_popup_action", "after_popup_action", "browser_load_failure",
    "unknown_state", "password_submission_blocker", "terminal_failure",
    "final_success",
}
SAFE_STRING_FIELDS = {
    "event_type", "state", "category", "document_category", "popup_category",
    "login_surface_category", "interaction_category", "readiness_category",
    "failure_category", "connection_type", "rotation_state", "scheduler_state",
    "parent_process_state", "child_process_state", "request_state",
    "normalized_exit_category", "domain_outcome", "infrastructure_outcome",
    "cancellation_owner", "rejection_owner", "closure_owner", "closure_reason",
    "capture_reason", "cleanup_reason", "target_category", "task_category",
    "route", "handler_result", "reason", "source",
}
BOOL_FIELDS = {
    "active", "browser_live", "context_live", "page_live", "navigation_timeout",
    "interaction_attempted", "value_verified", "submission_attempted",
    "container_disappeared", "fingerprint_changed", "mutation_changed",
    "document_changed", "images_included",
    "dirty_worktree", "username_ready", "password_ready",
    "source_live_debug", "fresh_reclassification_started", "worker_started",
}
INT_FIELDS = {
    "pid", "return_code", "retry_count", "document_epoch", "mutation_epoch",
    "transition_count", "snapshot_count", "bytes", "deleted_count",
    "document_epoch_before", "document_epoch_after",
    "mutation_epoch_before", "mutation_epoch_after", "real_job_id",
}
REF_FIELDS = {"account_ref", "connection_ref", "child_ref", "task_ref"}
SHA256_FIELDS = {
    "blocking_popup_transaction_sha256",
    "instagram_web_profile_workflow_sha256",
}
_EVENT_ONCE_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _valid_run_id(value: Any) -> str:
    run_id = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{5,127}", run_id):
        raise ValueError("invalid run id")
    return run_id


def _run_dir(run_id: str) -> Path:
    return RUNS_ROOT / _valid_run_id(run_id)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def normalize_task_category(value: Any) -> str:
    text = str(value or "").lower()
    for category in (
        "auto_login_setup", "auto_login", "check_login", "create_profiles",
        "open_profile", "web_warmup", "warmup", "post_story", "make_public",
        "convert_professional", "account_privacy", "view_analytics", "publication",
    ):
        if category.replace("_", " ") in text or category in text:
            return category
    return "workflow"


def ensure_run(
    run_id: str,
    *,
    task_category: str = "workflow",
    account_refs: Iterable[str] = (),
) -> Path:
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "snapshots").mkdir(exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        refs = [
            str(value) for value in account_refs
            if re.fullmatch(r"account-[0-9a-f]{16}", str(value))
        ]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": _valid_run_id(run_id),
            "task_category": normalize_task_category(task_category),
            "selected_account_refs": list(dict.fromkeys(refs)),
            "active": True,
            "result_class": "active",
            "started_at": _now(),
            "finished_at": "",
            "snapshot_count": 0,
            "snapshot_hashes": [],
        }
        _atomic_json(manifest_path, manifest)
        _atomic_json(run_dir / "latest_state.json", {
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "state": "starting", "updated_at": _now(),
        })
    for filename in STREAMS.values():
        (run_dir / filename).touch(exist_ok=True)
    return run_dir


def _manifest(run_id: str) -> dict[str, Any]:
    path = _run_dir(run_id) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_event(event_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueError("event type is not allowlisted")
    safe: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp_utc": _now(),
        "event_type": event_type,
    }
    for key, value in fields.items():
        if key == "routing_schema":
            safe[key] = (
                "stage1-minimal-v1"
                if str(value or "") == "stage1-minimal-v1"
                else "unknown"
            )
        elif key == "git_head":
            token = str(value or "").lower()
            safe[key] = token if re.fullmatch(r"[0-9a-f]{40}", token) else ""
        elif key in SHA256_FIELDS:
            token = str(value or "").lower()
            safe[key] = token if re.fullmatch(r"[0-9a-f]{64}", token) else ""
        elif key in {"domain_outcome", "infrastructure_outcome"}:
            safe[key] = _normalized_outcome(value)
        elif key in SAFE_STRING_FIELDS:
            token = str(value or "")
            allowed = OUTCOMES | CATEGORIES | {
                "workflow", "auto_login", "auto_login_setup", "check_login",
                "create_profiles", "open_profile", "web_warmup", "warmup",
                "post_story", "make_public", "convert_professional",
                "account_privacy", "view_analytics", "publication",
            }
            safe[key] = token if token in allowed else "unknown"
        elif key in BOOL_FIELDS:
            safe[key] = bool(value)
        elif key in INT_FIELDS:
            safe[key] = (
                None
                if key == "real_job_id" and value is None
                else max(-999_999_999, min(999_999_999, int(value or 0)))
            )
        elif key in REF_FIELDS:
            token = str(value or "")
            if re.fullmatch(r"(?:account|connection|child|task)-[0-9a-f]{8,64}", token):
                safe[key] = token
    return safe


def _normalized_outcome(value: Any) -> str:
    """Preserve one typed outcome or a typed additive infrastructure chain."""
    token = str(value or "")
    if token in OUTCOMES:
        return token
    parts = token.split(";")
    if len(parts) > 1 and all(part and part in OUTCOMES for part in parts):
        return ";".join(parts)
    return "unknown"


def append_event(
    run_id: str,
    event_type: str,
    *,
    stream: str = "events",
    **fields: Any,
) -> dict[str, Any]:
    run_dir = ensure_run(run_id)
    if stream not in STREAMS:
        raise ValueError("diagnostic stream is not allowlisted")
    record = _safe_event(event_type, fields)
    if directory_size(run_dir) >= PER_RUN_CAP_BYTES:
        if event_type != "diagnostic_limit_reached":
            _append_jsonl(run_dir / "cleanup.jsonl", _safe_event(
                "diagnostic_limit_reached", {"cleanup_reason": "per_run_cap"}
            ))
        return record
    _append_jsonl(run_dir / STREAMS[stream], record)
    return record


def account_worker_lifecycle(run_id: str) -> dict[str, dict[str, Any]]:
    """Return privacy-safe per-account child evidence recorded by the scheduler."""
    path = _run_dir(run_id) / STREAMS["process_events"]
    if not path.is_file():
        return {}
    lifecycle: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        account_ref = str(event.get("account_ref") or "")
        if not re.fullmatch(r"account-[0-9a-f]{16}", account_ref):
            continue
        state = lifecycle.setdefault(
            account_ref,
            {"worker_started": False, "worker_exited": False, "return_code": None},
        )
        if event.get("event_type") == "child_start":
            state["worker_started"] = True
        elif event.get("event_type") == "child_exit":
            state["worker_started"] = True
            state["worker_exited"] = True
            state["return_code"] = int(event.get("return_code") or 0)
    return lifecycle


def append_event_once(
    run_id: str,
    event_type: str,
    *,
    stream: str = "events",
    **fields: Any,
) -> dict[str, Any]:
    """Append one allowlisted event type at most once in this workflow run."""
    with _EVENT_ONCE_LOCK:
        path = ensure_run(run_id) / STREAMS[stream]
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("event_type") == event_type:
                    return _safe_event(event_type, fields)
            except (json.JSONDecodeError, AttributeError):
                continue
        return append_event(
            run_id,
            event_type,
            stream=stream,
            **fields,
        )


def update_latest_state(run_id: str, state: str, **fields: Any) -> None:
    safe = _safe_event("browser_liveness", {"state": state, **fields})
    safe["run_id"] = _valid_run_id(run_id)
    safe["updated_at"] = safe.pop("timestamp_utc")
    _atomic_json(ensure_run(run_id) / "latest_state.json", safe)


def _sensitive_input_populated(page: Any) -> bool:
    try:
        return bool(page.evaluate(
            """() => Array.from(document.querySelectorAll('input')).some(el => {
              const t=(el.type||'').toLowerCase();
              const a=(el.autocomplete||'').toLowerCase();
              return !!el.value && (t==='password' || a==='one-time-code');
            })"""
        ))
    except Exception:
        return True


def _compressed_image(raw: bytes) -> tuple[bytes, str]:
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        image.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT))
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=72, method=4)
        return output.getvalue(), ".webp"
    except Exception:
        return raw, ".png"


def capture_visual(page: Any, run_id: str, reason: str) -> bool:
    if reason not in VISUAL_REASONS:
        raise ValueError("visual reason is not allowlisted")
    run_dir = ensure_run(run_id)
    if _sensitive_input_populated(page):
        append_event(run_id, "visual_capture_skipped", stream="actions",
                     capture_reason=reason, category="sensitive_input_populated")
        return False
    manifest = _manifest(run_id)
    if int(manifest.get("snapshot_count") or 0) >= MAX_SNAPSHOTS:
        append_event(run_id, "visual_capture_skipped", stream="actions",
                     capture_reason=reason, category="snapshot_limit")
        return False
    try:
        raw = page.screenshot(full_page=False)
    except Exception:
        append_event(run_id, "visual_capture_skipped", stream="actions",
                     capture_reason=reason, category="unknown")
        return False
    data, suffix = _compressed_image(bytes(raw))
    digest = hashlib.sha256(data).hexdigest()
    latest = run_dir / ("latest" + suffix)
    latest.write_bytes(data)
    hashes = list(manifest.get("snapshot_hashes") or [])
    if digest in hashes:
        append_event(run_id, "visual_capture_skipped", stream="actions",
                     capture_reason=reason, category="deduplicated")
        return True
    target = run_dir / "snapshots" / f"{int(datetime.now(timezone.utc).timestamp() * 1000)}-{reason}{suffix}"
    target.write_bytes(data)
    hashes.append(digest)
    manifest["snapshot_hashes"] = hashes[-MAX_SNAPSHOTS:]
    manifest["snapshot_count"] = int(manifest.get("snapshot_count") or 0) + 1
    _atomic_json(run_dir / "manifest.json", manifest)
    append_event(run_id, "visual_capture", stream="actions",
                 capture_reason=reason, snapshot_count=manifest["snapshot_count"],
                 bytes=len(data))
    return True


def finalize_run(
    run_id: str,
    *,
    domain_outcome: str = "",
    infrastructure_outcome: str = "",
    closure_owner: str = "process_manager",
    closure_reason: str = "",
) -> None:
    run_dir = ensure_run(run_id)
    domain = _normalized_outcome(domain_outcome)
    infrastructure = _normalized_outcome(infrastructure_outcome)
    if domain:
        append_event(run_id, "domain_outcome", stream="outcomes", domain_outcome=domain)
    if infrastructure:
        append_event(run_id, "infrastructure_outcome", stream="outcomes",
                     infrastructure_outcome=infrastructure)
    append_event(run_id, "closure", stream="process_events",
                 closure_owner=closure_owner, closure_reason=closure_reason)
    manifest = _manifest(run_id)
    manifest.update({
        "active": False,
        "result_class": "success" if domain == "logged_in" else "failed",
        "domain_outcome": domain,
        "infrastructure_outcome": infrastructure,
        "closure_owner": closure_owner if closure_owner in CATEGORIES else "unknown",
        "closure_reason": (
            closure_reason
            if closure_reason in (CATEGORIES | OUTCOMES)
            else "unknown"
        ),
        "finished_at": _now(),
    })
    _atomic_json(run_dir / "manifest.json", manifest)
    cleanup_diagnostics(trigger="finalization")


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _parse_time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, timezone.utc)


def _safe_remove_run(run_dir: Path) -> int:
    root = RUNS_ROOT.resolve()
    resolved = run_dir.resolve()
    if resolved.parent != root or resolved.is_symlink():
        raise ValueError("retention target escaped diagnostics/runs")
    size = directory_size(resolved)
    tombstone = root / (".prune-" + resolved.name)
    os.replace(resolved, tombstone)
    shutil.rmtree(tombstone)
    return size


def cleanup_diagnostics(*, trigger: str = "startup", now: datetime | None = None) -> dict[str, int]:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    clock = now or datetime.now(timezone.utc)
    # A crash after the atomic rename but before removal leaves a tombstone.
    # Finishing that deletion on the next pass cannot affect an active run.
    for tombstone in RUNS_ROOT.glob(".prune-*"):
        try:
            if (
                tombstone.is_dir()
                and not tombstone.is_symlink()
                and tombstone.resolve().parent == RUNS_ROOT.resolve()
            ):
                shutil.rmtree(tombstone)
        except OSError:
            continue
    rows: list[dict[str, Any]] = []
    for child in RUNS_ROOT.iterdir():
        if not child.is_dir() or child.is_symlink() or child.name.startswith(".prune-"):
            continue
        try:
            manifest = json.loads((child / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("run_id") != child.name:
                continue
            rows.append({
                "path": child, "active": bool(manifest.get("active", True)),
                "result": str(manifest.get("result_class") or "failed"),
                "finished": _parse_time(manifest.get("finished_at")),
                "size": directory_size(child),
            })
        except Exception:
            continue
    finalized = [row for row in rows if not row["active"]]
    failures = sorted((row for row in finalized if row["result"] != "success"),
                      key=lambda row: row["finished"], reverse=True)
    successes = sorted((row for row in finalized if row["result"] == "success"),
                       key=lambda row: row["finished"], reverse=True)
    protected = {
        row["path"] for row in failures[:PRESERVE_FAILURES]
    } | {
        row["path"] for row in successes[:PRESERVE_SUCCESSES]
    }
    candidates: list[tuple[dict[str, Any], str]] = []
    for row in successes[PRESERVE_SUCCESSES:]:
        if clock - row["finished"] > SUCCESS_RETENTION:
            candidates.append((row, "age_limit"))
    for row in failures[PRESERVE_FAILURES:]:
        if clock - row["finished"] > FAILURE_RETENTION:
            candidates.append((row, "age_limit"))
    deleted: set[Path] = set()
    freed = 0
    for row, reason in sorted(candidates, key=lambda item: item[0]["finished"]):
        if row["path"] in protected or row["path"] in deleted:
            continue
        freed += _safe_remove_run(row["path"])
        deleted.add(row["path"])
        _append_jsonl(GLOBAL_CLEANUP_LEDGER, _safe_event(
            "retention_cleanup", {"cleanup_reason": reason, "bytes": row["size"]}
        ))
    remaining = [row for row in rows if row["path"] not in deleted]
    total = sum(int(row["size"]) for row in remaining)
    if total > GLOBAL_CAP_BYTES:
        cap_candidates = [
            row for row in sorted(
                (row for row in remaining if not row["active"] and row["path"] not in protected),
                key=lambda row: (row["result"] != "success", row["finished"]),
            )
        ]
        for row in cap_candidates:
            if total <= GLOBAL_CAP_BYTES:
                break
            freed_now = _safe_remove_run(row["path"])
            deleted.add(row["path"])
            freed += freed_now
            total -= int(row["size"])
            _append_jsonl(GLOBAL_CLEANUP_LEDGER, _safe_event(
                "retention_cleanup", {"cleanup_reason": "global_cap", "bytes": freed_now}
            ))
    return {"deleted_count": len(deleted), "freed_bytes": freed}


def build_run_archive(run_id: str, *, include_images: bool = False) -> Path:
    run_dir = _run_dir(run_id)
    manifest = _manifest(run_id)
    if manifest.get("active"):
        raise ValueError("active run diagnostics cannot be exported")
    fd, temp_name = tempfile.mkstemp(prefix="SparkGrid-run-diagnostic-", suffix=".zip")
    os.close(fd)
    output = Path(temp_name)
    allowed = {
        "manifest.json", "events.jsonl", "process_events.jsonl",
        "task_index.jsonl", "outcomes.jsonl", "actions.jsonl",
        "latest_state.json", "cleanup.jsonl",
    }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in run_dir.rglob("*"):
            if not item.is_file() or item.is_symlink():
                continue
            relative = item.relative_to(run_dir).as_posix()
            image = item.suffix.lower() in {".jpg", ".jpeg", ".webp", ".png"}
            if relative in allowed or (include_images and image and (
                relative.startswith("snapshots/") or relative.startswith("latest.")
            )):
                archive.write(item, relative)
    return output
