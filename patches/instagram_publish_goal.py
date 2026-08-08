"""Goal-driven control policy for Instagram Clean Web publishing.

The module is deliberately Playwright-free.  A small operation adapter owns
page reads and physical actions while this controller owns typed state,
fresh-observation epochs, action-ledger dispatch, progress timeouts, and the
irreversible Share contract.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Mapping, Protocol

from browser_action_ledger import BrowserAction, BrowserActionLedger
from browser_goal_engine import BrowserGoalEngine
from browser_progress_watchdog import ProgressWatchdog
from browser_workflow_goal import (
    ACTION_PERFORMED,
    GOAL_REACHED,
    RECONCILIATION_REQUIRED,
    STABLE_BLOCKER,
    TRANSITIONING_RETRY,
    BrowserWorkflowResult,
)
from browser_workflow_observation import BrowserObservation, ObservationEpoch
from browser_workflow_telemetry import SafeBrowserTelemetry


class PublishObservedState(str, Enum):
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CHECKPOINT_OR_CHALLENGE = "CHECKPOINT_OR_CHALLENGE"
    ACCOUNT_RESTRICTED = "ACCOUNT_RESTRICTED"
    TRANSITIONING = "TRANSITIONING"
    AUTHENTICATED_APPLICATION = "AUTHENTICATED_APPLICATION"
    CREATE_MENU_OPEN = "CREATE_MENU_OPEN"
    COMPOSER_OPEN = "COMPOSER_OPEN"
    MEDIA_ATTACHED = "MEDIA_ATTACHED"
    CROP_READY = "CROP_READY"
    EDIT_READY = "EDIT_READY"
    CAPTION_READY = "CAPTION_READY"
    SHARE_READY = "SHARE_READY"
    SHARING_OR_PROCESSING = "SHARING_OR_PROCESSING"
    PUBLISH_SUCCESS = "PUBLISH_SUCCESS"
    DONE_AVAILABLE = "DONE_AVAILABLE"
    COMPOSER_CLOSED = "COMPOSER_CLOSED"
    KNOWN_POPUP = "KNOWN_POPUP"
    STABLE_UNKNOWN_BLOCKER = "STABLE_UNKNOWN_BLOCKER"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    OPEN_COMPOSER_FAILED = "OPEN_COMPOSER_FAILED"


class PublishGoal(str, Enum):
    COMPOSER_OPENED = "COMPOSER_OPENED"
    MEDIA_ATTACHED_CONFIRMED = "MEDIA_ATTACHED_CONFIRMED"
    CAPTION_STAGE_REACHED = "CAPTION_STAGE_REACHED"
    SHARE_READY_CONFIRMED = "SHARE_READY_CONFIRMED"
    SHARE_ACCEPTED = "SHARE_ACCEPTED"
    PUBLICATION_CONFIRMED = "PUBLICATION_CONFIRMED"
    CLEANUP_COMPLETED = "CLEANUP_COMPLETED"


class PublishActionType(str, Enum):
    LEGACY_OPEN_COMPOSER_BRIDGE = "LEGACY_OPEN_COMPOSER_BRIDGE"
    ATTACH_MEDIA = "ATTACH_MEDIA"
    CLICK_NEXT_FROM_CROP = "CLICK_NEXT_FROM_CROP"
    CLICK_NEXT_FROM_EDIT = "CLICK_NEXT_FROM_EDIT"
    SET_CAPTION = "SET_CAPTION"
    CLICK_SHARE = "CLICK_SHARE"
    CLICK_DONE = "CLICK_DONE"
    DISMISS_KNOWN_POPUP = "DISMISS_KNOWN_POPUP"


TERMINAL_BLOCKERS = {
    PublishObservedState.LOGIN_REQUIRED,
    PublishObservedState.CHECKPOINT_OR_CHALLENGE,
    PublishObservedState.ACCOUNT_RESTRICTED,
    PublishObservedState.INFRASTRUCTURE_FAILURE,
}

TRANSITION_STATES = {
    PublishObservedState.TRANSITIONING,
    PublishObservedState.SHARING_OR_PROCESSING,
}


def _state(value: str) -> PublishObservedState:
    try:
        return PublishObservedState(str(value or ""))
    except ValueError:
        return PublishObservedState.STABLE_UNKNOWN_BLOCKER


def classify_publish_snapshot(snapshot: Mapping[str, Any]) -> PublishObservedState:
    """Classify a structural page snapshot with blocker-first precedence."""
    if snapshot.get("infrastructure_failure"):
        return PublishObservedState.INFRASTRUCTURE_FAILURE
    if snapshot.get("login_required"):
        return PublishObservedState.LOGIN_REQUIRED
    if snapshot.get("checkpoint_or_challenge"):
        return PublishObservedState.CHECKPOINT_OR_CHALLENGE
    if snapshot.get("account_restricted"):
        return PublishObservedState.ACCOUNT_RESTRICTED
    if snapshot.get("composer_closed") and snapshot.get("share_boundary"):
        return PublishObservedState.COMPOSER_CLOSED
    if snapshot.get("publish_success"):
        return (
            PublishObservedState.DONE_AVAILABLE
            if snapshot.get("done_available")
            else PublishObservedState.PUBLISH_SUCCESS
        )
    if snapshot.get("sharing_or_processing"):
        return PublishObservedState.SHARING_OR_PROCESSING
    if snapshot.get("known_popup"):
        return PublishObservedState.KNOWN_POPUP
    if snapshot.get("share_ready") and snapshot.get("share_enabled", True):
        return PublishObservedState.SHARE_READY
    if snapshot.get("caption_ready"):
        return PublishObservedState.CAPTION_READY
    if snapshot.get("edit_ready"):
        return PublishObservedState.EDIT_READY
    if snapshot.get("crop_ready"):
        return PublishObservedState.CROP_READY
    if snapshot.get("media_attached"):
        return PublishObservedState.MEDIA_ATTACHED
    if snapshot.get("composer_open"):
        return PublishObservedState.COMPOSER_OPEN
    if snapshot.get("create_menu_open"):
        return PublishObservedState.CREATE_MENU_OPEN
    if snapshot.get("authenticated_application"):
        return PublishObservedState.AUTHENTICATED_APPLICATION
    if (
        snapshot.get("loading")
        or snapshot.get("spinner_present")
        or snapshot.get("navigation_in_progress")
    ):
        return PublishObservedState.TRANSITIONING
    return PublishObservedState.STABLE_UNKNOWN_BLOCKER


class PublishOperationAdapter(Protocol):
    workflow_run_id: str
    irreversible_scope: str

    def read_snapshot(self) -> Mapping[str, Any]: ...

    def execute(self, action_type: PublishActionType) -> BrowserWorkflowResult: ...

    def on_goal_reached(
        self, goal: PublishGoal, observation: BrowserObservation
    ) -> None: ...

    def on_reconciliation_required(
        self, goal: PublishGoal, observation: BrowserObservation
    ) -> None: ...


_GOAL_STATES = {
    PublishGoal.COMPOSER_OPENED: {
        PublishObservedState.COMPOSER_OPEN,
        PublishObservedState.MEDIA_ATTACHED,
        PublishObservedState.CROP_READY,
        PublishObservedState.EDIT_READY,
        PublishObservedState.CAPTION_READY,
        PublishObservedState.SHARE_READY,
    },
    PublishGoal.MEDIA_ATTACHED_CONFIRMED: {
        PublishObservedState.MEDIA_ATTACHED,
        PublishObservedState.CROP_READY,
        PublishObservedState.EDIT_READY,
        PublishObservedState.CAPTION_READY,
        PublishObservedState.SHARE_READY,
    },
    PublishGoal.CAPTION_STAGE_REACHED: {
        PublishObservedState.CAPTION_READY,
        PublishObservedState.SHARE_READY,
    },
    PublishGoal.SHARE_READY_CONFIRMED: {PublishObservedState.SHARE_READY},
    PublishGoal.SHARE_ACCEPTED: {
        PublishObservedState.SHARING_OR_PROCESSING,
        PublishObservedState.PUBLISH_SUCCESS,
        PublishObservedState.DONE_AVAILABLE,
        PublishObservedState.COMPOSER_CLOSED,
    },
    PublishGoal.PUBLICATION_CONFIRMED: {
        PublishObservedState.PUBLISH_SUCCESS,
        PublishObservedState.DONE_AVAILABLE,
        PublishObservedState.COMPOSER_CLOSED,
    },
    PublishGoal.CLEANUP_COMPLETED: {PublishObservedState.COMPOSER_CLOSED},
}


class _GoalCallbacks:
    def __init__(
        self,
        *,
        controller: "PublishGoalController",
        goal: PublishGoal,
    ) -> None:
        self.controller = controller
        self.goal = goal

    def observe(self) -> BrowserObservation:
        if self.controller._observed_once:
            self.controller.sleep(self.controller.poll_seconds)
        self.controller._observed_once = True
        raw = dict(self.controller.adapter.read_snapshot() or {})
        state = classify_publish_snapshot(raw)
        raw["operation_state"] = state.value
        raw["durable_state"] = (
            "share_clicked" if raw.get("share_boundary") else
            "publish_intent" if raw.get("publish_intent") else ""
        )
        raw.setdefault("timestamp", self.controller.clock())
        self.controller.last_snapshot = raw
        observation = self.controller.epochs.fresh_observe(lambda: raw)
        self.controller.telemetry.emit(
            "publish_observation",
            publish_goal=self.goal.value,
            observed_state=state.value,
            observation_epoch=observation.epoch,
            share_boundary_state=observation.durable_state,
            watchdog_state="fresh_observation",
        )
        return observation

    def evaluate_goal(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None:
        state = _state(observation.operation_state)
        raw = self.controller.last_snapshot
        # SHARE_ACCEPTED is durable evidence, so it does not depend on a
        # transient processing surface remaining visible.
        if self.goal is PublishGoal.SHARE_ACCEPTED and (
            raw.get("share_boundary") or observation.durable_state == "share_clicked"
        ):
            return BrowserWorkflowResult.of(
                GOAL_REACHED, operation_state=state.value
            )
        if state in _GOAL_STATES[self.goal]:
            return BrowserWorkflowResult.of(
                GOAL_REACHED, operation_state=state.value
            )
        return None

    def evaluate_durable_boundary(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None:
        if self.goal in {
            PublishGoal.PUBLICATION_CONFIRMED,
            PublishGoal.CLEANUP_COMPLETED,
        }:
            return None
        if (
            observation.durable_state in {"publish_intent", "share_clicked"}
            and self.goal is not PublishGoal.SHARE_ACCEPTED
        ):
            return BrowserWorkflowResult.of(
                RECONCILIATION_REQUIRED,
                operation_state=observation.operation_state,
                error_category="durable_publish_boundary",
            )
        return None

    def select_action(
        self,
        observation: BrowserObservation,
        ledger: BrowserActionLedger,
    ) -> BrowserAction | None:
        state = _state(observation.operation_state)
        action: PublishActionType | None = None
        if state is PublishObservedState.KNOWN_POPUP:
            action = PublishActionType.DISMISS_KNOWN_POPUP
        elif self.goal is PublishGoal.COMPOSER_OPENED:
            if state in {
                PublishObservedState.AUTHENTICATED_APPLICATION,
                PublishObservedState.CREATE_MENU_OPEN,
            }:
                action = PublishActionType.LEGACY_OPEN_COMPOSER_BRIDGE
        elif self.goal is PublishGoal.MEDIA_ATTACHED_CONFIRMED:
            if state is PublishObservedState.COMPOSER_OPEN:
                action = PublishActionType.ATTACH_MEDIA
        elif self.goal is PublishGoal.CAPTION_STAGE_REACHED:
            if state in {
                PublishObservedState.MEDIA_ATTACHED,
                PublishObservedState.CROP_READY,
            }:
                action = PublishActionType.CLICK_NEXT_FROM_CROP
            elif state is PublishObservedState.EDIT_READY:
                action = PublishActionType.CLICK_NEXT_FROM_EDIT
        elif self.goal is PublishGoal.SHARE_READY_CONFIRMED:
            if state is PublishObservedState.CAPTION_READY:
                action = PublishActionType.SET_CAPTION
        elif self.goal is PublishGoal.SHARE_ACCEPTED:
            if state is PublishObservedState.SHARE_READY:
                action = PublishActionType.CLICK_SHARE
        elif self.goal is PublishGoal.CLEANUP_COMPLETED:
            if state is PublishObservedState.DONE_AVAILABLE:
                action = PublishActionType.CLICK_DONE
        if action is None:
            return None
        irreversible = action in {
            PublishActionType.CLICK_SHARE,
            PublishActionType.CLICK_DONE,
        }
        retryable = action in {
            PublishActionType.ATTACH_MEDIA,
            PublishActionType.DISMISS_KNOWN_POPUP,
        }
        return BrowserAction(
            workflow_run_id=self.controller.adapter.workflow_run_id,
            goal=self.goal.value,
            action_type=action.value,
            target_fingerprint=(
                observation.dialog_fingerprint
                or observation.visible_dom_fingerprint
                or observation.document_fingerprint
            ),
            retryable=retryable,
            max_attempts=2 if retryable else 1,
            once_per_fingerprint=(
                action is PublishActionType.LEGACY_OPEN_COMPOSER_BRIDGE
            ),
            irreversible=irreversible,
            irreversible_scope=(
                (
                    self.controller.adapter.irreversible_scope
                    if action is PublishActionType.CLICK_SHARE
                    else self.controller.adapter.irreversible_scope + ":cleanup"
                )
                if irreversible else ""
            ),
        )

    def execute_action(self, action: BrowserAction) -> BrowserWorkflowResult:
        return self.controller.adapter.execute(PublishActionType(action.action_type))

    def classify_transition(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None:
        if _state(observation.operation_state) in TRANSITION_STATES:
            return BrowserWorkflowResult.of(
                TRANSITIONING_RETRY,
                operation_state=observation.operation_state,
            )
        return None

    def classify_blocker(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None:
        state = _state(observation.operation_state)
        if state in TERMINAL_BLOCKERS or state is PublishObservedState.STABLE_UNKNOWN_BLOCKER:
            return BrowserWorkflowResult.of(
                STABLE_BLOCKER,
                operation_state=state.value,
                error_category=state.value.lower(),
            )
        return None

    def on_goal_reached(
        self,
        observation: BrowserObservation,
        result: BrowserWorkflowResult,
    ) -> None:
        self.controller.adapter.on_goal_reached(self.goal, observation)

    def on_reconciliation_required(
        self,
        observation: BrowserObservation,
        result: BrowserWorkflowResult,
    ) -> None:
        self.controller.adapter.on_reconciliation_required(self.goal, observation)


class PublishGoalController:
    """Run publish goals against fresh structural observations."""

    def __init__(
        self,
        adapter: PublishOperationAdapter,
        *,
        telemetry: SafeBrowserTelemetry | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
        poll_seconds: float = 0.35,
    ) -> None:
        self.adapter = adapter
        self.telemetry = telemetry or SafeBrowserTelemetry()
        self.clock = clock
        self.sleep = sleep
        self.poll_seconds = max(0.0, float(poll_seconds))
        self.epochs = ObservationEpoch(clock=clock)
        self.ledger = BrowserActionLedger(clock=clock)
        self.last_snapshot: dict[str, Any] = {}
        self._observed_once = False

    def run_goal(
        self,
        goal: PublishGoal,
        *,
        timeout_seconds: float,
        max_observations: int,
    ) -> BrowserWorkflowResult:
        self._observed_once = False
        callbacks = _GoalCallbacks(controller=self, goal=goal)
        engine = BrowserGoalEngine(
            workflow_run_id=self.adapter.workflow_run_id,
            goal=goal.value,
            callbacks=callbacks,
            epochs=self.epochs,
            ledger=self.ledger,
            watchdog=ProgressWatchdog(
                timeout_seconds=timeout_seconds,
                required_stable_observations=3,
                clock=self.clock,
            ),
            telemetry=self.telemetry,
            max_observations=max_observations,
        )
        result = engine.run()
        self.telemetry.emit(
            "publish_terminal_result",
            publish_goal=goal.value,
            observed_state=result.operation_state,
            terminal_result=result.code.value,
            reconciliation_decision=(
                "required" if result.reconciliation_required else "not_required"
            ),
            watchdog_state=engine.watchdog.last_progress_reason,
        )
        return result

    def run_pre_share(self) -> BrowserWorkflowResult:
        stages = (
            (PublishGoal.COMPOSER_OPENED, 45.0, 120),
            (PublishGoal.MEDIA_ATTACHED_CONFIRMED, 45.0, 120),
            (PublishGoal.CAPTION_STAGE_REACHED, 75.0, 220),
            (PublishGoal.SHARE_READY_CONFIRMED, 45.0, 120),
            (PublishGoal.SHARE_ACCEPTED, 45.0, 120),
        )
        result = BrowserWorkflowResult.of(STABLE_BLOCKER)
        for goal, timeout, observations in stages:
            result = self.run_goal(
                goal,
                timeout_seconds=timeout,
                max_observations=observations,
            )
            if not result.goal_reached:
                return result
        return result

    def run_publication_confirmation(self) -> BrowserWorkflowResult:
        return self.run_goal(
            PublishGoal.PUBLICATION_CONFIRMED,
            timeout_seconds=180.0,
            max_observations=520,
        )
