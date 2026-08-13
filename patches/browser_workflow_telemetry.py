"""Allowlisted structural telemetry for browser goal primitives."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


TELEMETRY_EVENTS = frozenset(
    {
        "browser_observation_created",
        "browser_observation_epoch_started",
        "browser_goal_evaluated",
        "browser_goal_reached",
        "browser_action_selected",
        "browser_action_skipped_by_ledger",
        "browser_action_started",
        "browser_action_finished",
        "browser_progress_recorded",
        "browser_transition_retry",
        "browser_stable_blocker_candidate",
        "browser_stable_blocker_confirmed",
        "browser_no_progress_timeout",
        "browser_reconciliation_required",
        "publish_observation",
        "publish_terminal_result",
        "check_session_observation",
        "check_session_authenticated",
        "check_session_login_required",
        "check_session_known_dialog_handled",
        "check_session_terminal_blocker",
        "session_refresh_observation",
        "session_refresh_authenticated",
        "session_refresh_storage_write_started",
        "session_refresh_storage_write_completed",
        "session_refresh_storage_validation_failed",
        "session_refresh_login_required",
        "session_refresh_success",
    }
)

SAFE_METADATA = frozenset(
    {
        "workflow_run_id",
        "goal",
        "epoch",
        "observation_id",
        "page_id",
        "normalized_url",
        "document_fingerprint",
        "visible_dom_fingerprint",
        "dialog_fingerprint",
        "action_type",
        "target_fingerprint",
        "attempt_count",
        "result",
        "error_category",
        "operation_state",
        "durable_state",
        "progress_reason",
        "observation_count",
        "stable_blocker_count",
        "repeated_fingerprint_count",
        "loading",
        "navigation_in_progress",
        "spinner_present",
        "publish_goal",
        "observed_state",
        "observation_epoch",
        "action_decision",
        "ledger_decision",
        "watchdog_state",
        "share_boundary_state",
        "reconciliation_decision",
        "terminal_result",
    }
)


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    event: str
    timestamp: float
    metadata: tuple[tuple[str, Any], ...]


class SafeBrowserTelemetry:
    """Drop all metadata outside a small structural allowlist."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._events: list[TelemetryEvent] = []

    @property
    def events(self) -> tuple[TelemetryEvent, ...]:
        return tuple(self._events)

    def emit(self, event: str, **metadata: Any) -> TelemetryEvent:
        if event not in TELEMETRY_EVENTS:
            raise ValueError(f"unsupported telemetry event: {event}")
        safe: list[tuple[str, Any]] = []
        for key in sorted(SAFE_METADATA.intersection(metadata)):
            value = metadata[key]
            if value is None:
                continue
            if isinstance(value, bool):
                safe.append((key, value))
            elif isinstance(value, (int, float)):
                safe.append((key, value))
            else:
                rendered = str(value)
                lowered = rendered.lower()
                if any(
                    marker in lowered
                    for marker in (
                        "password",
                        "sessionid",
                        "csrftoken",
                        "authorization",
                        "bearer ",
                        "two_factor",
                        "2fa",
                        "otp",
                        "token",
                    )
                ):
                    rendered = "[redacted]"
                elif key == "normalized_url":
                    try:
                        parts = urlsplit(rendered)
                        rendered = urlunsplit(
                            (
                                parts.scheme.lower(),
                                parts.netloc.lower(),
                                parts.path or "/",
                                "",
                                "",
                            )
                        )
                    except Exception:
                        rendered = rendered.split("?", 1)[0].split("#", 1)[0]
                safe.append((key, rendered[:256]))
        item = TelemetryEvent(event, float(self._clock()), tuple(safe))
        self._events.append(item)
        return item
