"""In-memory action ledger for future goal-driven browser workflows."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from browser_workflow_goal import ACTION_PERFORMED, BrowserWorkflowCode
from browser_workflow_observation import BrowserObservation


class ActionDisposition(str, Enum):
    ALLOWED = "ALLOWED"
    DUPLICATE_STATE = "DUPLICATE_STATE"
    IRREVERSIBLE_ALREADY_ATTEMPTED = "IRREVERSIBLE_ALREADY_ATTEMPTED"
    RETRY_LIMIT_REACHED = "RETRY_LIMIT_REACHED"


@dataclass(frozen=True, slots=True)
class BrowserAction:
    workflow_run_id: str
    goal: str
    action_type: str
    target_fingerprint: str
    retryable: bool = False
    max_attempts: int = 1
    once_per_fingerprint: bool = False
    irreversible: bool = False
    irreversible_scope: str = ""


@dataclass(frozen=True, slots=True)
class BrowserActionKey:
    workflow_run_id: str
    goal: str
    epoch: int
    page_id: str
    action_type: str
    target_fingerprint: str


@dataclass(frozen=True, slots=True)
class BrowserActionRecord:
    key: BrowserActionKey
    attempted_at: float
    completed_at: float | None
    result: BrowserWorkflowCode | None
    attempt_count: int
    error_category: str
    state_before: str
    state_after: str


@dataclass(frozen=True, slots=True)
class ActionDecision:
    allowed: bool
    disposition: ActionDisposition
    record: BrowserActionRecord | None = None


class BrowserActionLedger:
    """Prevent duplicate dispatch while allowing explicit bounded retries.

    The storage is intentionally in-memory in Commit 1.  ``records`` exposes
    immutable record values so a future durable adapter can preserve the same
    interface without affecting current workflows.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._records: dict[BrowserActionKey, BrowserActionRecord] = {}
        self._fingerprint_attempts: set[tuple[str, str, str, str, str]] = set()
        self._irreversible_attempts: set[tuple[str, str, str, str]] = set()

    @property
    def records(self) -> tuple[BrowserActionRecord, ...]:
        return tuple(self._records.values())

    @staticmethod
    def key_for(
        action: BrowserAction, observation: BrowserObservation
    ) -> BrowserActionKey:
        return BrowserActionKey(
            workflow_run_id=action.workflow_run_id,
            goal=action.goal,
            epoch=observation.epoch,
            page_id=observation.page_id,
            action_type=action.action_type,
            target_fingerprint=action.target_fingerprint,
        )

    @staticmethod
    def _irreversible_key(action: BrowserAction) -> tuple[str, str, str, str]:
        scope = action.irreversible_scope or action.target_fingerprint
        return (
            action.workflow_run_id,
            action.goal,
            action.action_type,
            scope,
        )

    @staticmethod
    def _fingerprint_key(
        action: BrowserAction, observation: BrowserObservation
    ) -> tuple[str, str, str, str, str]:
        return (
            action.workflow_run_id,
            action.goal,
            observation.page_id,
            action.action_type,
            action.target_fingerprint,
        )

    def start(
        self, action: BrowserAction, observation: BrowserObservation
    ) -> ActionDecision:
        key = self.key_for(action, observation)
        if (
            action.irreversible
            and self._irreversible_key(action) in self._irreversible_attempts
        ):
            return ActionDecision(
                False, ActionDisposition.IRREVERSIBLE_ALREADY_ATTEMPTED
            )
        if (
            action.once_per_fingerprint
            and self._fingerprint_key(action, observation)
            in self._fingerprint_attempts
        ):
            return ActionDecision(False, ActionDisposition.DUPLICATE_STATE)

        existing = self._records.get(key)
        max_attempts = max(1, int(action.max_attempts))
        if existing is not None:
            if not action.retryable:
                return ActionDecision(False, ActionDisposition.DUPLICATE_STATE)
            if existing.attempt_count >= max_attempts:
                return ActionDecision(
                    False, ActionDisposition.RETRY_LIMIT_REACHED, existing
                )
            record = replace(
                existing,
                attempted_at=self._clock(),
                completed_at=None,
                result=None,
                attempt_count=existing.attempt_count + 1,
                error_category="",
                state_before=observation.operation_state,
                state_after="",
            )
        else:
            record = BrowserActionRecord(
                key=key,
                attempted_at=self._clock(),
                completed_at=None,
                result=None,
                attempt_count=1,
                error_category="",
                state_before=observation.operation_state,
                state_after="",
            )
        self._records[key] = record
        if action.once_per_fingerprint:
            self._fingerprint_attempts.add(
                self._fingerprint_key(action, observation)
            )
        if action.irreversible:
            # Attempt, not visual success, crosses the exactly-once boundary.
            self._irreversible_attempts.add(self._irreversible_key(action))
        return ActionDecision(True, ActionDisposition.ALLOWED, record)

    def finish(
        self,
        action: BrowserAction,
        observation: BrowserObservation,
        *,
        result: BrowserWorkflowCode = ACTION_PERFORMED,
        error_category: str = "",
        state_after: str = "",
    ) -> BrowserActionRecord:
        key = self.key_for(action, observation)
        current = self._records.get(key)
        if current is None:
            raise KeyError("action was not started")
        finished = replace(
            current,
            completed_at=self._clock(),
            result=BrowserWorkflowCode(result),
            error_category=str(error_category or ""),
            state_after=str(state_after or ""),
        )
        self._records[key] = finished
        return finished

    def irreversible_attempted(self, action: BrowserAction) -> bool:
        return self._irreversible_key(action) in self._irreversible_attempts
