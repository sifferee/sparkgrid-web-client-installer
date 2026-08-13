"""Immutable, secret-free observations for goal-driven browser workflows.

This module deliberately does not import Playwright or any Instagram module.
Callers provide a fresh structural snapshot through ``fresh_observe``.  The
epoch controller makes stale observations unusable after an action, navigation,
page replacement, or a structural DOM change.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit


class StaleObservationError(RuntimeError):
    """Raised when control flow tries to reuse an invalid observation."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(sorted({_text(item) for item in value if _text(item)}))


def _timestamp(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def normalize_url(value: str) -> str:
    """Return a stable URL identity without query values or fragments."""
    raw = _text(value)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        path = parts.path or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
    except Exception:
        return raw.split("?", 1)[0].split("#", 1)[0]


def structural_fingerprint(value: Any) -> str:
    """Hash structural metadata without retaining raw DOM or form values."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    observation_id: str
    epoch: int
    timestamp: float
    page_id: str
    current_url: str
    normalized_url: str
    document_ready_state: str
    visible_dom_fingerprint: str
    document_fingerprint: str
    dialog_fingerprint: str
    visible_dialogs: tuple[str, ...]
    visible_headings: tuple[str, ...]
    visible_enabled_actions: tuple[str, ...]
    loading: bool
    navigation_in_progress: bool
    spinner_present: bool
    authenticated_evidence: tuple[str, ...]
    login_required_evidence: tuple[str, ...]
    restriction_evidence: tuple[str, ...]
    checkpoint_evidence: tuple[str, ...]
    operation_state: str
    durable_state: str
    network_evidence: tuple[str, ...]

    @property
    def structural_identity(self) -> tuple[Any, ...]:
        return (
            self.page_id,
            self.normalized_url,
            self.document_fingerprint,
            self.visible_dom_fingerprint,
            self.dialog_fingerprint,
            self.visible_enabled_actions,
            self.operation_state,
            self.durable_state,
            self.authenticated_evidence,
            self.login_required_evidence,
            self.loading,
            self.navigation_in_progress,
        )


class ObservationEpoch:
    """Own observation freshness and invalidate stale control-flow inputs."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._epoch = 0
        self._counter = 0
        self._active_observation_id = ""
        self._last_structure: tuple[str, str, str] | None = None
        self._last_epoch_reason = "workflow_started"

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def last_epoch_reason(self) -> str:
        return self._last_epoch_reason

    def start_new_epoch(self, reason: str) -> int:
        self._epoch += 1
        self._active_observation_id = ""
        self._last_structure = None
        self._last_epoch_reason = _text(reason) or "state_invalidated"
        return self._epoch

    def invalidate_after_action(self, action_type: str = "") -> int:
        return self.start_new_epoch(
            "action:" + (_text(action_type) or "unspecified")
        )

    def assert_current(self, observation: BrowserObservation) -> None:
        if (
            observation.epoch != self._epoch
            or observation.observation_id != self._active_observation_id
        ):
            raise StaleObservationError(
                f"observation {observation.observation_id!r} is stale; "
                f"active epoch is {self._epoch}"
            )

    def is_current(self, observation: BrowserObservation) -> bool:
        try:
            self.assert_current(observation)
            return True
        except StaleObservationError:
            return False

    def fresh_observe(
        self, reader: Callable[[], Mapping[str, Any]]
    ) -> BrowserObservation:
        """Invoke ``reader`` now and build the sole current observation."""
        snapshot = dict(reader() or {})
        page_id = _text(snapshot.get("page_id")) or "page"
        current_url = _text(snapshot.get("current_url") or snapshot.get("url"))
        normalized = normalize_url(current_url)
        visible_dom = _text(snapshot.get("visible_dom_fingerprint"))
        document = _text(snapshot.get("document_fingerprint"))
        if not visible_dom:
            visible_dom = structural_fingerprint(
                {
                    "dialogs": _strings(snapshot.get("visible_dialogs")),
                    "headings": _strings(snapshot.get("visible_headings")),
                    "actions": _strings(snapshot.get("visible_enabled_actions")),
                    "operation_state": _text(snapshot.get("operation_state")),
                }
            )
        if not document:
            document = structural_fingerprint(
                {
                    "url": normalized,
                    "ready": _text(snapshot.get("document_ready_state")),
                    "visible_dom": visible_dom,
                }
            )

        structure = (page_id, normalized, document)
        if self._last_structure is not None and structure != self._last_structure:
            reason = (
                "page_replaced"
                if page_id != self._last_structure[0]
                else "navigation_or_dom_changed"
            )
            self.start_new_epoch(reason)
        self._last_structure = structure
        self._counter += 1
        observation_id = f"obs-{self._epoch}-{self._counter}"
        self._active_observation_id = observation_id
        return BrowserObservation(
            observation_id=observation_id,
            epoch=self._epoch,
            timestamp=_timestamp(snapshot.get("timestamp"), self._clock()),
            page_id=page_id,
            current_url=current_url,
            normalized_url=normalized,
            document_ready_state=_text(snapshot.get("document_ready_state")),
            visible_dom_fingerprint=visible_dom,
            document_fingerprint=document,
            dialog_fingerprint=_text(snapshot.get("dialog_fingerprint")),
            visible_dialogs=_strings(snapshot.get("visible_dialogs")),
            visible_headings=_strings(snapshot.get("visible_headings")),
            visible_enabled_actions=_strings(
                snapshot.get("visible_enabled_actions")
            ),
            loading=bool(snapshot.get("loading")),
            navigation_in_progress=bool(snapshot.get("navigation_in_progress")),
            spinner_present=bool(snapshot.get("spinner_present")),
            authenticated_evidence=_strings(
                snapshot.get("authenticated_evidence")
            ),
            login_required_evidence=_strings(
                snapshot.get("login_required_evidence")
            ),
            restriction_evidence=_strings(snapshot.get("restriction_evidence")),
            checkpoint_evidence=_strings(snapshot.get("checkpoint_evidence")),
            operation_state=_text(snapshot.get("operation_state")),
            durable_state=_text(snapshot.get("durable_state")),
            network_evidence=_strings(snapshot.get("network_evidence")),
        )
