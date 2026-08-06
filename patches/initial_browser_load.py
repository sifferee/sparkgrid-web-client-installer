"""One-shot recovery for an unusable initial Instagram document.

This module is intentionally valid only before username, password, or OTP
submission.  It records normalized categories and never returns raw URLs,
document text, DOM, selectors, or exception messages.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from blocking_popup_transaction import inspect_topmost_blocker


INSTAGRAM_ROOT = "https://www.instagram.com/"
DOCUMENT_CATEGORIES = {
    "browser_internal_error",
    "blank_document",
    "instagram_document",
    "unknown_document",
}
FAILURE_CATEGORIES = {
    "dns_failure",
    "tls_failure",
    "proxy_tunnel_failure",
    "connection_reset",
    "navigation_timeout",
    "navigation_failed",
    "unknown_failure",
}


_DOCUMENT_SCRIPT = r"""() => { // IG_INITIAL_DOCUMENT_INSPECT
  const protocol=String(location.protocol||'').toLowerCase();
  const host=String(location.hostname||'').toLowerCase();
  const internal=!/^https?:$/.test(protocol);
  const body=document.body,root=document.documentElement;
  const visible=el=>{
    if(!el||!el.isConnected)return false;
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>1&&r.height>1&&r.bottom>0&&r.right>0&&
      s.display!=='none'&&s.visibility!=='hidden'&&
      Number.parseFloat(s.opacity||'1')>.01;
  };
  const any=selector=>[...document.querySelectorAll(selector)].some(visible);
  const login=any(
    "input[type='password'],input[autocomplete='current-password'],input[autocomplete='username']"
  );
  const otp=any("input[autocomplete='one-time-code'],input[inputmode='numeric']");
  const challenge=/\/(challenge|checkpoint)\//.test(String(location.pathname||'').toLowerCase());
  const authenticated=any(
    "a[href*='/direct/inbox'],a[href*='/accounts/edit'],svg[aria-label='Home'],svg[aria-label='New post']"
  );
  const blank=!root||!body||(body.childElementCount===0&&
    String(body.textContent||'').trim().length===0);
  const instagram=host==='instagram.com'||host.endsWith('.instagram.com');
  return {
    document_category:internal?'browser_internal_error':
      blank?'blank_document':instagram?'instagram_document':'unknown_document',
    login_surface:login,two_factor_surface:otp,challenge_surface:challenge,
    authenticated_surface:authenticated,
    ready_state:String(document.readyState||'unknown')
  };
}"""


def normalize_main_frame_failure(error: Any) -> str:
    """Allowlist only explicit transport markers from the navigation failure."""
    if error is None:
        return ""
    text = (type(error).__name__ + " " + str(error or "")).lower()
    if any(token in text for token in ("name_not_resolved", "dns", "unknown host")):
        return "dns_failure"
    if any(token in text for token in ("ssl", "tls", "certificate", "secure connection")):
        return "tls_failure"
    if any(
        token in text
        for token in (
            "proxy_connection_failed",
            "tunnel connection failed",
            "proxy tunnel",
        )
    ):
        return "proxy_tunnel_failure"
    if any(token in text for token in ("connection_reset", "connection reset")):
        return "connection_reset"
    if any(token in text for token in ("timeouterror", "timed out", "timeout")):
        return "navigation_timeout"
    if any(token in text for token in ("navigation", "net::err_", "ns_error_")):
        return "navigation_failed"
    return "unknown_failure"


def inspect_initial_document(page: Any) -> dict[str, Any]:
    defaults = {
        "document_category": "unknown_document",
        "login_surface": False,
        "two_factor_surface": False,
        "challenge_surface": False,
        "authenticated_surface": False,
        "popup_category": "",
        "page_live": False,
        "context_live": False,
    }
    try:
        defaults["page_live"] = not bool(page.is_closed())
    except Exception:
        defaults["page_live"] = True
    try:
        context = page.context
        defaults["context_live"] = bool(context) and not bool(
            getattr(context, "is_closed", lambda: False)()
        )
    except Exception:
        defaults["context_live"] = defaults["page_live"]
    try:
        raw = page.evaluate(_DOCUMENT_SCRIPT)
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        category = str(raw.get("document_category") or "unknown_document")
        defaults["document_category"] = (
            category if category in DOCUMENT_CATEGORIES else "unknown_document"
        )
        for key in (
            "login_surface",
            "two_factor_surface",
            "challenge_surface",
            "authenticated_surface",
        ):
            defaults[key] = bool(raw.get(key))
    try:
        blocker = inspect_topmost_blocker(page)
    except Exception:
        blocker = {}
    category = str(blocker.get("category") or "")
    if category in {
        "cookie_consent",
        "regional_ads_consent",
        "save_login_info",
        "notifications_prompt",
        "promo_or_ad",
        "open_in_app",
        "unknown_blocker",
    }:
        defaults["popup_category"] = category
    return defaults


def has_recognized_surface(observed: dict[str, Any]) -> bool:
    return bool(
        observed.get("login_surface")
        or observed.get("two_factor_surface")
        or observed.get("challenge_surface")
        or observed.get("authenticated_surface")
        or observed.get("popup_category")
    )


def _wait_after_fresh_get(
    page: Any,
    *,
    timeout_seconds: float,
    inspect_fn: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    last = inspect_fn(page)
    while time.monotonic() < deadline:
        if has_recognized_surface(last) or last.get("document_category") == (
            "instagram_document"
        ):
            return last
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        last = inspect_fn(page)
    return last


def recover_initial_browser_load(
    page: Any,
    *,
    initial_error: Any = None,
    target_category: str = "instagram_root",
    timeout_seconds: float = 20.0,
    inspect_fn: Callable[[Any], dict[str, Any]] = inspect_initial_document,
    navigate_fn: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Classify initial arrival and perform at most one fresh root GET."""
    observed = inspect_fn(page)
    failure = normalize_main_frame_failure(initial_error)
    base = {
        "target_category": str(target_category or "instagram_root"),
        "navigation_timeout": failure == "navigation_timeout",
        "main_frame_failure_category": failure,
        "context_live": bool(observed.get("context_live")),
        "page_live": bool(observed.get("page_live")),
        "browser_live": bool(
            observed.get("page_live") and observed.get("context_live")
        ),
        "document_category": str(
            observed.get("document_category") or "unknown_document"
        ),
        "retry_count": 0,
    }
    if has_recognized_surface(observed):
        return {**base, "ok": True, "outcome": "initial_surface_ready"}
    if (
        observed.get("document_category") == "instagram_document"
        and initial_error is None
    ):
        return {**base, "ok": True, "outcome": "initial_document_ready"}
    if not base["browser_live"]:
        return {
            **base,
            "ok": False,
            "outcome": "browser_load_failed_after_retry",
            "main_frame_failure_category": failure or "navigation_failed",
        }

    def navigate(current_page: Any) -> None:
        current_page.goto(
            INSTAGRAM_ROOT,
            wait_until="commit",
            timeout=max(1000, int(float(timeout_seconds) * 1000)),
        )

    retry_failure = ""
    try:
        (navigate_fn or navigate)(page)
    except Exception as exc:
        retry_failure = normalize_main_frame_failure(exc)
    after = _wait_after_fresh_get(
        page,
        timeout_seconds=timeout_seconds,
        inspect_fn=inspect_fn,
    )
    result = {
        **base,
        "retry_count": 1,
        "navigation_timeout": bool(
            base["navigation_timeout"]
            or retry_failure == "navigation_timeout"
        ),
        "main_frame_failure_category": retry_failure or failure,
        "browser_live": bool(
            after.get("page_live") and after.get("context_live")
        ),
        "context_live": bool(after.get("context_live")),
        "page_live": bool(after.get("page_live")),
        "document_category": str(
            after.get("document_category") or "unknown_document"
        ),
    }
    if has_recognized_surface(after) or (
        after.get("document_category") == "instagram_document"
        and retry_failure == ""
    ):
        return {**result, "ok": True, "outcome": "initial_load_recovered"}
    return {
        **result,
        "ok": False,
        "outcome": "browser_load_failed_after_retry",
        "main_frame_failure_category": (
            retry_failure or failure or "unknown_failure"
        ),
    }
