from __future__ import annotations

import re
import time
from typing import Any, Callable

from instagram_dialog_gate import (
    HANDLED_REEVALUATE,
    TRANSITIONING_RETRY,
    continue_after_dialog,
    inspect_dialog,
)
from blocking_popup_transaction import (
    inspect_topmost_blocker,
    resolve_typed_consent_chain,
)
from log_config import get_logger

logger = get_logger("automation")
try:
    from ig_human import make_human
except Exception as _exc:  # pragma: no cover - optional in narrow test runtimes
    logger.debug("%s: %s", type(_exc).__name__, _exc)
    make_human = None

Capture = Callable[[Any, str, str], None]


def _body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1500) or "")
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return ""


def _click(page: Any, patterns: tuple[str, ...], human: Any = None) -> str:
    for pattern in patterns:
        matcher = re.compile(pattern, re.I)
        getters = (
            lambda: page.get_by_role("button", name=matcher),
            lambda: page.get_by_role("radio", name=matcher),
            lambda: page.get_by_text(matcher, exact=True),
            lambda: page.locator("label").filter(has_text=matcher),
        )
        for getter in getters:
            try:
                locator = getter()
                for index in range(min(int(locator.count() or 0), 20)):
                    candidate = locator.nth(index)
                    if not candidate.is_visible(timeout=350):
                        continue
                    enabled_probe = getattr(candidate, "is_enabled", None)
                    if enabled_probe is not None:
                        try:
                            if not enabled_probe(timeout=350):
                                continue
                        except Exception as _exc:
                            logger.debug("%s: %s", type(_exc).__name__, _exc)
                            continue
                    if human is not None:
                        if not human.click(candidate, timeout=3500):
                            continue
                    else:
                        # Compatibility for non-Playwright structural test
                        # doubles. Production pages expose frames and receive
                        # the existing HumanInteractor above.
                        candidate.click(timeout=3500)
                    return pattern
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                continue
    return ""


def _choice_is_selected(page: Any, patterns: tuple[str, ...]) -> bool:
    """Require evidence that the Ads option, not merely its text, was selected."""
    for pattern in patterns:
        matcher = re.compile(pattern, re.I)
        for getter in (
            lambda: page.get_by_role("radio", name=matcher),
            lambda: page.get_by_text(matcher, exact=True),
            lambda: page.locator("label").filter(has_text=matcher),
        ):
            try:
                locator = getter()
                for index in range(min(int(locator.count() or 0), 20)):
                    candidate = locator.nth(index)
                    if not candidate.is_visible(timeout=350):
                        continue
                    try:
                        if candidate.is_checked(timeout=350):
                            return True
                    except Exception as _exc:
                        logger.debug("%s: %s", type(_exc).__name__, _exc)
                        pass
                    try:
                        if str(candidate.get_attribute("aria-checked") or "").lower() == "true":
                            return True
                    except Exception as _exc:
                        logger.debug("%s: %s", type(_exc).__name__, _exc)
                        pass
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                continue
    return False


FREE_WITH_ADS = (
    r"^use for free with ads$",
    r"^use free of charge with ads$",
    r"^continue using our products free of charge with ads$",
)


def _capture(capture: Capture | None, page: Any, step: str, detail: str) -> None:
    if capture is None:
        return
    try:
        capture(page, step, detail)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def consent_present(page: Any) -> bool:
    if hasattr(page, "frames"):
        observed = inspect_topmost_blocker(page)
        return str(observed.get("category") or "") in {
            "cookie_consent",
            "regional_ads_consent",
            "request_processing",
        }
    url = str(getattr(page, "url", "") or "").lower()
    text = _body_text(page).lower()
    return bool(
        "/consent/" in url
        or "allow the use of cookies by instagram" in text
        or "make a choice about your ads" in text
        or "as part of laws in your region" in text
        or "choose if we process your data for ads" in text
        or "subscribe or continue using our products" in text
        or "use for free with ads" in text
        or "continue using our products free of charge with ads" in text
        or "use our products free of charge with ads" in text
        or "continue with personalized ads" in text
        or _request_processing_text(text)
    )


def _request_processing_text(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "your request couldn't be processed",
            "your request can't be processed",
            "your request couldn’t be processed",
            "your request can’t be processed",
        )
    )


def request_failed(page: Any) -> bool:
    return _request_processing_text(_body_text(page).lower())


def resolve_instagram_consent(
    page: Any,
    capture: Capture | None = None,
    *,
    max_seconds: float = 35.0,
    human: Any = None,
) -> dict[str, Any]:
    """Finish Instagram's regional cookies/ads consent flow.

    The July 2026 flow is a multi-page wizard, not a dismissible popup.  This
    routine deliberately acts only while a consent URL or one of the exact
    consent texts is visible, so generic Continue/Agree buttons elsewhere on
    Instagram cannot be clicked accidentally.
    """
    if hasattr(page, "frames") and human is None and make_human is not None:
        try:
            human = make_human(page)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            human = None

    if hasattr(page, "frames"):

        def consent_event(event: str, payload: dict[str, Any]) -> None:
            detail = ";".join(
                f"{key}={payload[key]}" for key in sorted(payload)
            )
            _capture(capture, page, event, detail)

        return resolve_typed_consent_chain(
            page,
            max_steps=8,
            overall_timeout=max(5.0, float(max_seconds)),
            transition_timeout=min(8.0, max(0.5, float(max_seconds) / 4.0)),
            event_fn=consent_event,
            human=human,
        )

    if not consent_present(page):
        return {"handled": False, "ok": True, "step": "not_present"}

    deadline = time.time() + max(5.0, float(max_seconds))
    handled = False
    last_step = "detected"
    saw_request_error = False
    request_error_attempted = False
    choice_selection_attempted = False
    idle_rounds = 0
    while time.time() < deadline:
        if not consent_present(page):
            _capture(capture, page, "consent_completed", last_step)
            return {"handled": True, "ok": True, "step": "completed"}

        text = _body_text(page).lower()
        clicked = ""
        step = ""

        if _request_processing_text(text):
            saw_request_error = True
            if request_error_attempted:
                break
            clicked = _click(page, (r"^ok$",), human)
            request_error_attempted = bool(clicked)
            step = "request_error_closed"
        elif "allow the use of cookies by instagram" in text or "optional cookies" in text:
            visible_dialog = inspect_dialog(page)
            continuation = (
                continue_after_dialog(
                    page,
                    wait_seconds=min(8.0, max(0.5, deadline - time.time())),
                    cookie_action="allow_all_cookies",
                )
                if visible_dialog.get("category") == "cookie_consent"
                else {}
            )
            if continuation.get("clicked_action") == "allow_all_cookies":
                clicked = r"^allow all cookies$"
            elif continuation.get("outcome") == TRANSITIONING_RETRY:
                last_step = "cookies_transitioning"
                time.sleep(0.4)
                continue
            elif continuation.get("outcome") == HANDLED_REEVALUATE:
                last_step = "cookies_allowed"
                continue
            else:
                # Some regional consent pages are full-page wizards rather
                # than role=dialog. They retain the exact-action fallback.
                clicked = _click(
                    page,
                    (r"^allow all cookies$", r"^allow essential and optional cookies$"),
                    human,
                )
            step = "cookies_allowed"
        # The actual choice page repeats the regional introduction copy. It
        # must be classified before the generic Get started page, otherwise we
        # keep searching for a button that is no longer present.
        elif "subscribe or continue using our products" in text or "continue using our products free of charge with ads" in text:
            if choice_selection_attempted:
                break
            clicked = _click(page, FREE_WITH_ADS, human)
            step = "free_with_ads_selected"
            if clicked and _choice_is_selected(page, FREE_WITH_ADS):
                time.sleep(0.7)
                if _click(page, (r"^continue$",), human):
                    step = "free_with_ads_continued"
            elif clicked:
                # Do not submit a disabled/stale Continue button. The next
                # round must observe actual selected-state evidence.
                clicked = ""
                choice_selection_attempted = True
        elif (
            "choose if we process your data for ads" in text
            or "make a choice about your ads" in text
            or "as part of laws in your region" in text
        ):
            clicked = _click(page, (r"^get started$",), human)
            step = "ads_started"
        elif "agree to meta processing your data" in text or "use our products free of charge with ads" in text:
            clicked = _click(page, (r"^agree$", r"^continue$"), human)
            step = "ads_processing_agreed"
        elif "manage your ad experience" in text or "continue with personalized ads" in text:
            clicked = _click(page, (r"^ok$", r"^continue with personalized ads$", r"^continue$"), human)
            step = "ad_experience_saved"
        else:
            # A consent URL alone is not enough to identify an action. In
            # particular, never click a generic button in an unknown popup.
            step = "unrecognized_popup"

        if clicked:
            handled = True
            idle_rounds = 0
            last_step = step
            _capture(capture, page, "consent_" + step, clicked)
            # The regional wizard frequently replaces one React tree with the
            # next without changing URL. Give the visible state time to move;
            # clicking a stale duplicate is the main source of false loops.
            time.sleep(1.8)
            continue

        idle_rounds += 1
        if idle_rounds >= 3:
            break
        time.sleep(1.0)

    remaining = consent_present(page)
    _capture(capture, page, "consent_unresolved", last_step)
    return {
        "handled": handled,
        "ok": not remaining,
        "step": "unresolved" if remaining else "completed",
        "consent_state": "consent_pending" if remaining else "resolved",
        "manual_required": remaining,
        "request_failed": saw_request_error or request_failed(page),
    }
