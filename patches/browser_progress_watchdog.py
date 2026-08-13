"""Progress and stable-blocker tracking for browser goal loops."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from browser_workflow_observation import BrowserObservation


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    workflow_started_at: float
    last_progress_at: float
    last_progress_reason: str
    repeated_fingerprint_count: int
    stable_blocker_count: int
    observation_count: int


class ProgressWatchdog:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        required_stable_observations: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        started = float(clock())
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.required_stable_observations = max(
            3, int(required_stable_observations)
        )
        self.workflow_started_at = started
        self.last_progress_at = started
        self.last_progress_reason = "workflow_started"
        self.repeated_fingerprint_count = 0
        self.stable_blocker_count = 0
        self.observation_count = 0
        self._last: BrowserObservation | None = None
        self._last_signature: tuple[object, ...] | None = None

    @property
    def snapshot(self) -> ProgressSnapshot:
        return ProgressSnapshot(
            workflow_started_at=self.workflow_started_at,
            last_progress_at=self.last_progress_at,
            last_progress_reason=self.last_progress_reason,
            repeated_fingerprint_count=self.repeated_fingerprint_count,
            stable_blocker_count=self.stable_blocker_count,
            observation_count=self.observation_count,
        )

    def record_progress(self, reason: str, *, at: float | None = None) -> None:
        self.last_progress_at = float(self._clock() if at is None else at)
        self.last_progress_reason = str(reason or "progress")
        self.stable_blocker_count = 0

    def observe(self, observation: BrowserObservation) -> tuple[str, ...]:
        self.observation_count += 1
        reasons: list[str] = []
        previous = self._last
        if previous is not None:
            if observation.normalized_url != previous.normalized_url:
                reasons.append("url_changed")
            if observation.page_id != previous.page_id:
                reasons.append("page_changed")
            if observation.document_fingerprint != previous.document_fingerprint:
                reasons.append("document_changed")
            if (
                observation.visible_dom_fingerprint
                != previous.visible_dom_fingerprint
            ):
                reasons.append("visible_dom_changed")
            if observation.dialog_fingerprint != previous.dialog_fingerprint:
                reasons.append("dialog_changed")
            if (
                observation.visible_enabled_actions
                != previous.visible_enabled_actions
            ):
                reasons.append("enabled_actions_changed")
            if observation.operation_state != previous.operation_state:
                reasons.append("operation_state_changed")
            if (
                observation.authenticated_evidence
                != previous.authenticated_evidence
                or observation.login_required_evidence
                != previous.login_required_evidence
            ):
                reasons.append("authentication_evidence_changed")
            if (
                observation.loading != previous.loading
                or observation.navigation_in_progress
                != previous.navigation_in_progress
            ):
                reasons.append("loading_changed")
            if observation.durable_state != previous.durable_state:
                reasons.append("durable_state_changed")
            if (
                observation.network_evidence != previous.network_evidence
                and observation.network_evidence
            ):
                reasons.append("network_acceptance")

        signature = observation.structural_identity
        if signature == self._last_signature:
            self.repeated_fingerprint_count += 1
        else:
            self.repeated_fingerprint_count = 1
            self.stable_blocker_count = 1
        self._last_signature = signature
        self._last = observation

        if reasons:
            self.record_progress("+".join(reasons), at=observation.timestamp)
            self.stable_blocker_count = 1
        elif self.repeated_fingerprint_count > 1:
            self.stable_blocker_count = self.repeated_fingerprint_count
        return tuple(reasons)

    def record_action_performed(self) -> None:
        self.record_progress("action_performed")

    def can_confirm_stable_blocker(
        self,
        observation: BrowserObservation,
        *,
        known_action_available: bool,
        goal_reached: bool,
    ) -> bool:
        if self._last is None or observation.observation_id != self._last.observation_id:
            return False
        if (
            observation.loading
            or observation.navigation_in_progress
            or observation.spinner_present
            or known_action_available
            or goal_reached
        ):
            return False
        return (
            self.repeated_fingerprint_count
            >= self.required_stable_observations
            and self.stable_blocker_count
            >= self.required_stable_observations
        )

    def timed_out(self, *, now: float | None = None) -> bool:
        current = float(self._clock() if now is None else now)
        return current - self.last_progress_at >= self.timeout_seconds
