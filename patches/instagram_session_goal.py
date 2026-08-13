"""Goal-driven Check Session and browser Session Refresh adapters."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from browser_action_ledger import BrowserAction, BrowserActionLedger
from browser_goal_engine import BrowserGoalEngine
from browser_progress_watchdog import ProgressWatchdog
from browser_workflow_goal import (
    ACTION_PERFORMED,
    AUTHENTICATED_CONFIRMED,
    FAILED_CONFIRMED,
    LOGIN_REQUIRED,
    SESSION_EXPORT_INCOMPLETE,
    SESSION_REFRESH_SUCCESS,
    STABLE_BLOCKER,
    STORAGE_PERSISTENCE_FAILED,
    TRANSITIONING_RETRY,
    BrowserWorkflowResult,
)
from browser_workflow_observation import BrowserObservation, ObservationEpoch
from browser_workflow_telemetry import SafeBrowserTelemetry
from instagram_auth_goal import _page_signals, confirm_authenticated_state
from instagram_dialog_gate import dismiss_known_dialog_once, inspect_dialog


CHECK_SESSION_GOAL = "CHECK_SESSION"
SESSION_REFRESH_GOAL = "SESSION_REFRESH"
KNOWN_DIALOGS = frozenset(
    {"cookie_consent", "save_login", "notification", "policy_notice"}
)
TERMINAL_DIALOGS = frozenset({"checkpoint", "restriction", "suspended"})


@dataclass(frozen=True, slots=True)
class StoragePersistenceOutcome:
    ok: bool
    category: str = ""
    export_complete: bool = True
    metadata_changed: bool = True


class InstagramSessionGoalCallbacks:
    """Thin Instagram adapter; control flow remains in BrowserGoalEngine."""

    def __init__(
        self,
        page: Any,
        *,
        workflow_run_id: str,
        goal: str,
        epochs: ObservationEpoch,
        telemetry: SafeBrowserTelemetry,
        persist_storage: Callable[[], StoragePersistenceOutcome] | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        self.page = page
        self.workflow_run_id = str(workflow_run_id)
        self.goal = str(goal)
        self.epochs = epochs
        self.telemetry = telemetry
        self.persist_storage = persist_storage
        self.poll_interval = max(0.0, float(poll_interval))
        self._observations = 0
        self._authenticated = False
        self._storage_outcome: StoragePersistenceOutcome | None = None

    @property
    def storage_outcome(self) -> StoragePersistenceOutcome | None:
        return self._storage_outcome

    def _snapshot(self) -> dict[str, Any]:
        signals = _page_signals(self.page)
        auth = confirm_authenticated_state(self.page, signals)
        dialog = inspect_dialog(self.page)
        category = str(dialog.get("category") or "")
        url = str(signals.get("url") or "")
        login_form = bool(signals.get("login_form"))
        login_url = "/accounts/login" in url.lower()
        authenticated = bool(auth.get("confirmed"))
        if authenticated:
            self._authenticated = True

        auth_evidence = tuple(
            key for key, value in dict(auth.get("evidence") or {}).items() if value
        )
        if authenticated:
            auth_evidence += ("authenticated_confirmed",)
        login_evidence: tuple[str, ...] = ()
        if not authenticated:
            if login_form:
                login_evidence += ("visible_login_form",)
            if login_url:
                login_evidence += ("login_url",)

        loading = bool(signals.get("loading") or dialog.get("progress"))
        if category in TERMINAL_DIALOGS:
            operation_state = category
        elif category:
            operation_state = category
        elif authenticated:
            operation_state = "authenticated"
        elif login_evidence:
            operation_state = "login_required"
        elif loading:
            operation_state = "loading"
        else:
            operation_state = "unknown"

        durable_state = ""
        if self._storage_outcome is not None:
            durable_state = (
                "storage_saved"
                if self._storage_outcome.ok
                else "storage_persistence_failed"
            )
        return {
            "page_id": f"page-{id(self.page)}",
            "current_url": url,
            "document_ready_state": signals.get("ready_state"),
            "visible_dom_fingerprint": signals.get("dom_fingerprint"),
            "document_fingerprint": signals.get("dom_fingerprint"),
            "dialog_fingerprint": str(dialog.get("fingerprint") or ""),
            "visible_dialogs": (category,) if dialog.get("present") else (),
            "visible_enabled_actions": (
                (f"dismiss:{category}",) if category in KNOWN_DIALOGS else ()
            ),
            "loading": loading,
            "navigation_in_progress": loading,
            "spinner_present": bool(dialog.get("progress")),
            "authenticated_evidence": auth_evidence,
            "login_required_evidence": login_evidence,
            "restriction_evidence": (
                (category,) if category in {"restriction", "suspended"} else ()
            ),
            "checkpoint_evidence": (
                (category,) if category == "checkpoint" else ()
            ),
            "operation_state": operation_state,
            "durable_state": durable_state,
            "network_evidence": (
                ("current_user_200",)
                if "current_user_endpoint" in auth_evidence
                else ()
            ),
        }

    def observe(self) -> BrowserObservation:
        if self._observations and self.poll_interval:
            time.sleep(self.poll_interval)
        self._observations += 1
        observation = self.epochs.fresh_observe(self._snapshot)
        self.telemetry.emit(
            "check_session_observation"
            if self.goal == CHECK_SESSION_GOAL
            else "session_refresh_observation",
            workflow_run_id=self.workflow_run_id,
            goal=self.goal,
            epoch=observation.epoch,
            observation_id=observation.observation_id,
            operation_state=observation.operation_state,
        )
        return observation

    @staticmethod
    def _known_dialog(observation: BrowserObservation) -> str:
        return next(
            (item for item in observation.visible_dialogs if item in KNOWN_DIALOGS),
            "",
        )

    def evaluate_goal(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None:
        if observation.operation_state in TERMINAL_DIALOGS:
            self.telemetry.emit(
                "check_session_terminal_blocker",
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
                operation_state=observation.operation_state,
            )
            return BrowserWorkflowResult.of(
                FAILED_CONFIRMED,
                operation_state=observation.operation_state,
                error_category=observation.operation_state,
            )

        authenticated = "authenticated_confirmed" in observation.authenticated_evidence
        known_dialog = self._known_dialog(observation)
        if authenticated:
            self._authenticated = True
        if self._authenticated and not known_dialog:
            event = (
                "check_session_authenticated"
                if self.goal == CHECK_SESSION_GOAL
                else "session_refresh_authenticated"
            )
            self.telemetry.emit(
                event,
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
                operation_state="authenticated",
            )
            if self.goal == CHECK_SESSION_GOAL:
                return BrowserWorkflowResult.of(
                    AUTHENTICATED_CONFIRMED,
                    operation_state="authenticated",
                )
            if self._storage_outcome is None:
                return None
            if not self._storage_outcome.ok:
                code = (
                    SESSION_EXPORT_INCOMPLETE
                    if not self._storage_outcome.export_complete
                    else STORAGE_PERSISTENCE_FAILED
                )
                return BrowserWorkflowResult.of(
                    code,
                    operation_state="authenticated",
                    error_category=self._storage_outcome.category
                    or "storage_persistence_failed",
                )
            if not self._storage_outcome.metadata_changed:
                return BrowserWorkflowResult.of(
                    STORAGE_PERSISTENCE_FAILED,
                    operation_state="authenticated",
                    error_category="session_metadata_not_updated",
                )
            self.telemetry.emit(
                "session_refresh_success",
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
                durable_state="storage_saved",
            )
            return BrowserWorkflowResult.of(
                SESSION_REFRESH_SUCCESS,
                operation_state="authenticated",
                metadata={"storage_state_saved": True},
            )

        if observation.login_required_evidence and not known_dialog:
            self.telemetry.emit(
                "check_session_login_required"
                if self.goal == CHECK_SESSION_GOAL
                else "session_refresh_login_required",
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
                operation_state="login_required",
            )
            return BrowserWorkflowResult.of(
                LOGIN_REQUIRED, operation_state="login_required"
            )
        return None

    def evaluate_durable_boundary(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None:
        return None

    def select_action(
        self,
        observation: BrowserObservation,
        ledger: BrowserActionLedger,
    ) -> BrowserAction | None:
        category = self._known_dialog(observation)
        if category:
            action = BrowserAction(
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
                action_type=f"dismiss:{category}",
                target_fingerprint=observation.dialog_fingerprint or category,
                irreversible=True,
                irreversible_scope=(
                    f"{category}:{observation.dialog_fingerprint or category}"
                ),
            )
            return None if ledger.irreversible_attempted(action) else action
        if (
            self.goal == SESSION_REFRESH_GOAL
            and self._authenticated
            and self._storage_outcome is None
        ):
            return BrowserAction(
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
                action_type="persist_storage_state",
                target_fingerprint="authenticated_session",
                irreversible=True,
                irreversible_scope="session_storage_export",
            )
        return None

    def execute_action(self, action: BrowserAction) -> BrowserWorkflowResult:
        if action.action_type.startswith("dismiss:"):
            category = action.action_type.split(":", 1)[1]
            handled = dismiss_known_dialog_once(self.page, category)
            if handled:
                self.telemetry.emit(
                    "check_session_known_dialog_handled",
                    workflow_run_id=self.workflow_run_id,
                    goal=self.goal,
                    action_type=action.action_type,
                    target_fingerprint=action.target_fingerprint,
                )
            return BrowserWorkflowResult.of(
                ACTION_PERFORMED if handled else FAILED_CONFIRMED,
                operation_state="dialog_handled" if handled else "dialog_not_dismissed",
                error_category="" if handled else "known_dialog_action_failed",
            )

        if action.action_type == "persist_storage_state":
            self.telemetry.emit(
                "session_refresh_storage_write_started",
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
            )
            try:
                if self.persist_storage is None:
                    raise RuntimeError("storage persistence callback is unavailable")
                self._storage_outcome = self.persist_storage()
            except Exception as exc:
                self._storage_outcome = StoragePersistenceOutcome(
                    False, category=type(exc).__name__.lower()
                )
            event = (
                "session_refresh_storage_write_completed"
                if self._storage_outcome.ok
                else "session_refresh_storage_validation_failed"
            )
            self.telemetry.emit(
                event,
                workflow_run_id=self.workflow_run_id,
                goal=self.goal,
                error_category=self._storage_outcome.category,
                durable_state=(
                    "storage_saved"
                    if self._storage_outcome.ok
                    else "storage_persistence_failed"
                ),
            )
            return BrowserWorkflowResult.of(
                ACTION_PERFORMED,
                operation_state=(
                    "storage_saved"
                    if self._storage_outcome.ok
                    else "storage_persistence_failed"
                ),
                error_category=self._storage_outcome.category,
            )
        return BrowserWorkflowResult.of(
            FAILED_CONFIRMED, error_category="unsupported_session_action"
        )

    def classify_transition(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None:
        if (
            observation.loading
            or observation.navigation_in_progress
            or observation.spinner_present
        ):
            return BrowserWorkflowResult.of(
                TRANSITIONING_RETRY,
                operation_state=observation.operation_state,
            )
        return None

    def classify_blocker(
        self, observation: BrowserObservation
    ) -> BrowserWorkflowResult | None:
        if (
            observation.operation_state in {"unknown", "unknown_dialog"}
            or any(item in KNOWN_DIALOGS for item in observation.visible_dialogs)
        ):
            return BrowserWorkflowResult.of(
                STABLE_BLOCKER,
                operation_state=observation.operation_state,
                error_category=observation.operation_state,
            )
        return None

    def on_goal_reached(
        self, observation: BrowserObservation, result: BrowserWorkflowResult
    ) -> None:
        return None

    def on_reconciliation_required(
        self, observation: BrowserObservation, result: BrowserWorkflowResult
    ) -> None:
        return None


def run_session_goal(
    page: Any,
    *,
    goal: str,
    workflow_run_id: str = "",
    persist_storage: Callable[[], StoragePersistenceOutcome] | None = None,
    timeout_seconds: float = 8.0,
    poll_interval: float = 0.2,
    telemetry: SafeBrowserTelemetry | None = None,
) -> tuple[BrowserWorkflowResult, InstagramSessionGoalCallbacks]:
    """Run one shared-engine session goal and return its adapter state."""
    run_id = str(workflow_run_id or uuid.uuid4().hex)
    epochs = ObservationEpoch()
    safe_telemetry = telemetry or SafeBrowserTelemetry()
    callbacks = InstagramSessionGoalCallbacks(
        page,
        workflow_run_id=run_id,
        goal=goal,
        epochs=epochs,
        telemetry=safe_telemetry,
        persist_storage=persist_storage,
        poll_interval=poll_interval,
    )
    engine = BrowserGoalEngine(
        workflow_run_id=run_id,
        goal=goal,
        callbacks=callbacks,
        epochs=epochs,
        ledger=BrowserActionLedger(),
        watchdog=ProgressWatchdog(
            timeout_seconds=timeout_seconds, required_stable_observations=3
        ),
        telemetry=safe_telemetry,
        max_observations=max(6, int(timeout_seconds / max(poll_interval, 0.05)) + 4),
    )
    return engine.run(), callbacks


def run_check_session_goal(page: Any, **kwargs: Any):
    return run_session_goal(page, goal=CHECK_SESSION_GOAL, **kwargs)


def run_session_refresh_goal(page: Any, **kwargs: Any):
    return run_session_goal(page, goal=SESSION_REFRESH_GOAL, **kwargs)
