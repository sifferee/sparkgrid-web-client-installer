"""Durable, local-only Instagram browser session snapshots.

The native persistent SparkBrowser profile remains authoritative. This file is
an atomic recovery/API-readiness snapshot containing cookies, storage state and
basic browser identity metadata. Secrets never leave the client.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


REQUIRED_COOKIES = {"sessionid", "csrftoken", "ds_user_id"}


class SessionPersistenceError(RuntimeError):
    """Storage state could not be durably written and validated."""


class SessionExportIncomplete(SessionPersistenceError):
    """Browser auth exists, but the required export cookie contract is incomplete."""


def _replace_with_retry(tmp: Path, path: Path, attempts: int = 5, delay_seconds: float = 0.15) -> None:
    """Same fix as browser_launcher.py/lifecycle_recovery.py, applied
    here 2026-08-16: this is the PRIMARY session-save path (browser_
    launcher.py's save_browser_state() tries this before falling back to
    its own raw os.replace), called on every successful login/session
    refresh — same exposure to a transient WinError 5 from a brief
    antivirus scan or lingering reader handle, previously with zero
    retry."""
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    assert last_exc is not None
    raise last_exc


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    _replace_with_retry(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def save_instagram_session(context: Any, profile_root: Path, storage_state_path: Path) -> dict[str, Any]:
    profile_root = Path(profile_root)
    storage_state_path = Path(storage_state_path)
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_state = storage_state_path.with_suffix(storage_state_path.suffix + ".tmp")
    context.storage_state(path=str(tmp_state))
    try:
        os.chmod(tmp_state, 0o600)
    except Exception:
        pass
    _replace_with_retry(tmp_state, storage_state_path)

    state = json.loads(storage_state_path.read_text(encoding="utf-8"))
    cookies = [dict(item) for item in state.get("cookies", []) if isinstance(item, dict)]
    cookie_names = {str(item.get("name") or "") for item in cookies}
    pages = [page for page in (getattr(context, "pages", []) or []) if not page.is_closed()]
    page = pages[-1] if pages else None
    user_agent = ""
    current_url = ""
    if page is not None:
        try:
            user_agent = str(page.evaluate("() => navigator.userAgent") or "")
        except Exception:
            pass
        try:
            current_url = str(page.url or "")
        except Exception:
            pass

    snapshot = {
        "schema_version": 1,
        "captured_at": int(time.time()),
        "captured_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "storage_state_path": str(storage_state_path),
        "profile_root": str(profile_root),
        "current_url": current_url,
        "user_agent": user_agent,
        "cookies": cookies,
        "origins": state.get("origins", []),
        "cookie_names": sorted(cookie_names),
        "logged_in_cookie_set": REQUIRED_COOKIES.issubset(cookie_names),
        "api_ready": REQUIRED_COOKIES.issubset(cookie_names),
    }
    _atomic_json(profile_root / "instagram_session.json", snapshot)
    return snapshot


def persist_instagram_session(
    context: Any,
    profile_root: Path,
    storage_state_path: Path,
    *,
    account: str,
) -> dict[str, Any]:
    """Atomically export and validate session state plus ownership metadata.

    Unlike the legacy best-effort helper, every failure is propagated.  Success
    means the storage file is readable, the export cookie contract is complete,
    and account-scoped metadata was updated.
    """
    profile_root = Path(profile_root)
    storage_state_path = Path(storage_state_path)
    owner = str(account or "").strip().lstrip("@").casefold()
    if not owner:
        raise SessionPersistenceError("session owner is missing")
    metadata_path = profile_root / "instagram_session_metadata.json"
    if metadata_path.is_file():
        try:
            prior = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SessionPersistenceError("session metadata is unreadable") from exc
        prior_owner = str(prior.get("account_owner") or "").casefold()
        if prior_owner and prior_owner != owner:
            raise SessionPersistenceError("session account ownership mismatch")

    profile_root.mkdir(parents=True, exist_ok=True)
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_state = storage_state_path.with_suffix(
        storage_state_path.suffix + f".{os.getpid()}.tmp"
    )
    try:
        context.storage_state(path=str(tmp_state))
        state = json.loads(tmp_state.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise SessionPersistenceError("storage state is not an object")
        cookies = [
            dict(item)
            for item in state.get("cookies", [])
            if isinstance(item, dict)
        ]
        cookie_names = {
            str(item.get("name") or "")
            for item in cookies
            if str(item.get("value") or "")
        }
        missing = REQUIRED_COOKIES.difference(cookie_names)
        if missing:
            raise SessionExportIncomplete("required session export is incomplete")
        _replace_with_retry(tmp_state, storage_state_path)
        try:
            os.chmod(storage_state_path, 0o600)
        except Exception:
            pass
        saved_state = json.loads(storage_state_path.read_text(encoding="utf-8"))
        saved_names = {
            str(item.get("name") or "")
            for item in saved_state.get("cookies", [])
            if isinstance(item, dict) and str(item.get("value") or "")
        }
        if not REQUIRED_COOKIES.issubset(saved_names):
            raise SessionPersistenceError("saved storage state validation failed")

        captured_at = int(time.time())
        snapshot = {
            "schema_version": 2,
            "captured_at": captured_at,
            "captured_at_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(captured_at)
            ),
            "saved_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(captured_at)
            ),
            "storage_state_path": str(storage_state_path),
            "profile_root": str(profile_root),
            "cookies": cookies,
            "origins": saved_state.get("origins", []),
            "cookie_names": sorted(saved_names),
            "logged_in_cookie_set": True,
            "api_ready": True,
        }
        _atomic_json(profile_root / "instagram_session.json", snapshot)
        metadata = {
            "schema_version": 1,
            "account_owner": owner,
            "storage_state_path": str(storage_state_path),
            "storage_saved_at": captured_at,
            "storage_saved_at_iso": snapshot["saved_at"],
            "session_export_complete": True,
        }
        _atomic_json(metadata_path, metadata)
        validated_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            str(validated_metadata.get("account_owner") or "").casefold() != owner
            or int(validated_metadata.get("storage_saved_at") or 0) != captured_at
        ):
            raise SessionPersistenceError("session metadata validation failed")
        return {
            "state": saved_state,
            "snapshot": snapshot,
            "metadata": validated_metadata,
        }
    finally:
        try:
            if tmp_state.exists():
                tmp_state.unlink()
        except Exception:
            pass


def load_instagram_session(profile_root: Path, max_age_hours: int = 24) -> dict[str, Any] | None:
    path = Path(profile_root) / "instagram_session.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(payload.get("captured_at") or 0)
        if age > max(1, int(max_age_hours)) * 3600:
            return None
        if not payload.get("api_ready"):
            return None
        return payload
    except Exception:
        return None
