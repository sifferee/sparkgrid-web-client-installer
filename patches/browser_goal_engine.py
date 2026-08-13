"""Instagram-agnostic callback engine for future goal-driven workflows."""
from __future__ import annotations

from typing import Protocol

from browser_action_ledger import BrowserAction, BrowserActionLedger
from browser_progress_watchdog import ProgressWatchdog
from browser_workflow_goal import (
    ACTION_PERFORMED,
    NO_PROGRESS_TIMEOUT,
    RECONCILIATION_REQUIRED,
    STABLE_BLOCKER,
    TRANSITIONING_RETRY,
    BrowserWorkflowResult,
)
from browser_workflow_observation import BrowserObservation, ObservationEpoch
from browser_workflow_telemetry import SafeBrowserTelemetry


class BrowserGoalCallbacks(Protocol):
    def observe(self) -> BrowserObservation: ...

    def evaluate_goal(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None: ...

    def evaluate_durable_boundary(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None: ...

    def select_action(
        self,
        observation: BrowserObservation,
        ledger: BrowserActionLedger,
    ) -> BrowserAction | None: ...

    def execute_action(
        self, action: BrowserAction
    ) -> BrowserWorkflowResult: ...

    def classify_transition(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None: ...

    def classify_blocker(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None: ...

    def on_goal_reached(
        self,
        observation: BrowserObservation,
        result: BrowserWorkflowResult,
    ) -> None: ...

    def on_reconciliation_required(
        self,
        observation: BrowserObservation,
        result: BrowserWorkflowResult,
    ) -> None: ...


class BrowserGoalEngine:
    """Run a goal loop without knowing any Instagram screen sequence."""

    def __init__(
        self,
        *,
        workflow_run_id: str,
        goal: str,
        callbacks: BrowserGoalCallbacks,
        epochs: ObservationEpoch,
        ledger: BrowserActionLedger,
        watchdog: ProgressWatchdog,
        telemetry: SafeBrowserTelemetry | None = None,
        max_observations: int = 1000,
    ) -> None:
        self.workflow_run_id = str(workflow_run_id)
        self.goal = str(goal)
        self.callbacks = callbacks
        self.epochs = epochs
        self.ledger = ledger
        self.watchdog = watchdog
        self.telemetry = telemetry or SafeBrowserTelemetry()
        self.max_observations = max(1, int(max_observations))

    def _emit_observation(self, observation: BrowserObservation) -> None:
        self.telemetry.emit(
            "browser_observation_created",
            workflow_run_id=self.workflow_run_id,
            goal=self.goal,
            epoch=observation.epoch,
            observation_id=observation.observation_id,
            page_id=observation.page_id,
            normalized_url=observation.normalized_url,
            document_fingerprint=observation.document_fingerprint,
            visible_dom_fingerprint=observation.visible_dom_fingerprint,
            dialog_fingerprint=observation.dialog_fingerprint,
            operation_state=observation.operation_state,
            durable_state=observation.durable_state,
            loading=observation.loading,
            navigation_in_progress=observation.navigation_in_progress,
            spinner_present=observation.spinner_present,
        )

    def run(self) -> BrowserWorkflowResult:
        pending_reconciliation: BrowserWorkflowResult | None = None
        for _ in range(self.max_observations):
            observation = self.callbacks.observe()
            self.epochs.assert_current(observation)
            self._emit_observation(observation)
            progress = self.watchdog.observe(observation)
            if progress:
                self.telemetry.emit(
                    "browser_progress_recorded",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    epoch=observation.epoch,
                    observation_id=observation.observation_id,
                    progress_reason="+".join(progress),
                    watchdog_state=self.watchdog.last_progress_reason,
                )

            # Goal always has priority over transition/blocker classification.
            goal_result = self.callbacks.evaluate_goal(observation)
            self.telemetry.emit(
                "browser_goal_evaluated",
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
                epoch=observation.epoch,
                observation_id=observation.observation_id,
                result=goal_result.code.value if goal_result else "",
            )
            if goal_result is not None and goal_result.goal_reached:
                self.callbacks.on_goal_reached(observation, goal_result)
                self.telemetry.emit(
                    "browser_goal_reached",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    epoch=observation.epoch,
                    result=goal_result.code.value,
                    terminal_result=goal_result.code.value,
                )
                return goal_result

            # An irreversible action may itself report ambiguous acceptance.
            # Reobserve first, then reconcile before any further normal action.
            if pending_reconciliation is not None:
                self.callbacks.on_reconciliation_required(
                    observation, pending_reconciliation
                )
                self.telemetry.emit(
                    "browser_reconciliation_required",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    epoch=observation.epoch,
                    result=pending_reconciliation.code.value,
                    durable_state=observation.durable_state,
                    reconciliation_decision="required",
                )
                return pending_reconciliation

            # A durable irreversible boundary is evaluated before any UI action.
            durable = self.callbacks.evaluate_durable_boundary(observation)
            if durable is not None and durable.reconciliation_required:
                self.callbacks.on_reconciliation_required(observation, durable)
                self.telemetry.emit(
                    "browser_reconciliation_required",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    epoch=observation.epoch,
                    result=durable.code.value,
                    durable_state=observation.durable_state,
                    reconciliation_decision="required",
                )
                return durable

            action = self.callbacks.select_action(observation, self.ledger)
            if action is not None:
                self.telemetry.emit(
                    "browser_action_selected",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    epoch=observation.epoch,
                    action_type=action.action_type,
                    target_fingerprint=action.target_fingerprint,
                    action_decision="selected",
                )
                decision = self.ledger.start(action, observation)
                if not decision.allowed:
                    self.telemetry.emit(
                        "browser_action_skipped_by_ledger",
                        workflow_run_id=self.workflow_run_id,
                        goal=self.goal,
                        epoch=observation.epoch,
                        action_type=action.action_type,
                        target_fingerprint=action.target_fingerprint,
                        result=decision.disposition.value,
                        action_decision="skipped",
                        ledger_decision=decision.disposition.value,
                    )
                else:
                    self.telemetry.emit(
                        "browser_action_started",
                        workflow_run_id=self.workflow_run_id,
                        goal=self.goal,
                        epoch=observation.epoch,
                        action_type=action.action_type,
                        target_fingerprint=action.target_fingerprint,
                        attempt_count=decision.record.attempt_count,
                        action_decision="execute",
                        ledger_decision=decision.disposition.value,
                    )
                    result = self.callbacks.execute_action(action)
                    self.ledger.finish(
                        action,
                        observation,
                        result=result.code,
                        error_category=result.error_category,
                        state_after=result.operation_state,
                    )
                    self.telemetry.emit(
                        "browser_action_finished",
                        workflow_run_id=self.workflow_run_id,
                        goal=self.goal,
                        epoch=observation.epoch,
                        action_type=action.action_type,
                        target_fingerprint=action.target_fingerprint,
                        result=result.code.value,
                        error_category=result.error_category,
                    )
                    if result.code is ACTION_PERFORMED:
                        self.watchdog.record_action_performed()
                    if result.code is RECONCILIATION_REQUIRED:
                        pending_reconciliation = result
                    new_epoch = self.epochs.invalidate_after_action(
                        action.action_type
                    )
                    self.telemetry.emit(
                        "browser_observation_epoch_started",
                        workflow_run_id=self.workflow_run_id,
                        goal=self.goal,
                        epoch=new_epoch,
                        action_type=action.action_type,
                    )
                    # No decision may use the pre-action observation.
                    continue

            transition = self.callbacks.classify_transition(observation)
            if transition is not None and transition.code is TRANSITIONING_RETRY:
                self.telemetry.emit(
                    "browser_transition_retry",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    epoch=observation.epoch,
                    operation_state=observation.operation_state,
                )
                continue

            blocker = self.callbacks.classify_blocker(observation)
            if blocker is not None:
                self.telemetry.emit(
                    "browser_stable_blocker_candidate",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    epoch=observation.epoch,
                    operation_state=observation.operation_state,
                    stable_blocker_count=self.watchdog.stable_blocker_count,
                )
                if self.watchdog.can_confirm_stable_blocker(
                    observation,
                    known_action_available=action is not None,
                    goal_reached=False,
                ):
                    confirmed = (
                        blocker
                        if blocker.code is STABLE_BLOCKER
                        else BrowserWorkflowResult.of(
                            STABLE_BLOCKER,
                            operation_state=blocker.operation_state,
                            error_category=blocker.error_category,
                            metadata=blocker.metadata,
                        )
                    )
                    self.telemetry.emit(
                        "browser_stable_blocker_confirmed",
                        workflow_run_id=self.workflow_run_id,
                        goal=self.goal,
                        epoch=observation.epoch,
                        operation_state=observation.operation_state,
                        stable_blocker_count=self.watchdog.stable_blocker_count,
                    )
                    return confirmed

            if self.watchdog.timed_out():
                result = BrowserWorkflowResult.of(
                    NO_PROGRESS_TIMEOUT,
                    operation_state=observation.operation_state,
                    error_category="no_progress_timeout",
                )
                self.telemetry.emit(
                    "browser_no_progress_timeout",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    epoch=observation.epoch,
                    operation_state=observation.operation_state,
                    progress_reason=self.watchdog.last_progress_reason,
                )
                return result

        return BrowserWorkflowResult.of(
            NO_PROGRESS_TIMEOUT,
            error_category="observation_budget_exhausted",
        )
