"""Typed vocabulary shared by goal-driven browser workflows."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class BrowserWorkflowCode(str, Enum):
    GOAL_REACHED = "GOAL_REACHED"
    ACTION_AVAILABLE = "ACTION_AVAILABLE"
    ACTION_PERFORMED = "ACTION_PERFORMED"
    TRANSITIONING_RETRY = "TRANSITIONING_RETRY"
    STABLE_BLOCKER = "STABLE_BLOCKER"
    NO_PROGRESS_TIMEOUT = "NO_PROGRESS_TIMEOUT"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    AUTHENTICATED_CONFIRMED = "AUTHENTICATED_CONFIRMED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    STORAGE_STATE_SAVED = "STORAGE_STATE_SAVED"
    SESSION_REFRESH_SUCCESS = "SESSION_REFRESH_SUCCESS"
    STORAGE_PERSISTENCE_FAILED = "STORAGE_PERSISTENCE_FAILED"
    SESSION_EXPORT_INCOMPLETE = "SESSION_EXPORT_INCOMPLETE"


GOAL_REACHED = BrowserWorkflowCode.GOAL_REACHED
ACTION_AVAILABLE = BrowserWorkflowCode.ACTION_AVAILABLE
ACTION_PERFORMED = BrowserWorkflowCode.ACTION_PERFORMED
TRANSITIONING_RETRY = BrowserWorkflowCode.TRANSITIONING_RETRY
STABLE_BLOCKER = BrowserWorkflowCode.STABLE_BLOCKER
NO_PROGRESS_TIMEOUT = BrowserWorkflowCode.NO_PROGRESS_TIMEOUT
RECONCILIATION_REQUIRED = BrowserWorkflowCode.RECONCILIATION_REQUIRED
LOGIN_REQUIRED = BrowserWorkflowCode.LOGIN_REQUIRED
AUTHENTICATED_CONFIRMED = BrowserWorkflowCode.AUTHENTICATED_CONFIRMED
FAILED_CONFIRMED = BrowserWorkflowCode.FAILED_CONFIRMED
STORAGE_STATE_SAVED = BrowserWorkflowCode.STORAGE_STATE_SAVED
SESSION_REFRESH_SUCCESS = BrowserWorkflowCode.SESSION_REFRESH_SUCCESS
STORAGE_PERSISTENCE_FAILED = BrowserWorkflowCode.STORAGE_PERSISTENCE_FAILED
SESSION_EXPORT_INCOMPLETE = BrowserWorkflowCode.SESSION_EXPORT_INCOMPLETE


@dataclass(frozen=True, slots=True)
class BrowserWorkflowResult:
    code: BrowserWorkflowCode
    operation_state: str = ""
    error_category: str = ""
    metadata: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", BrowserWorkflowCode(self.code))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata or {}))
        )

    @property
    def goal_reached(self) -> bool:
        return self.code in {
            GOAL_REACHED,
            AUTHENTICATED_CONFIRMED,
            LOGIN_REQUIRED,
            FAILED_CONFIRMED,
            SESSION_REFRESH_SUCCESS,
            STORAGE_PERSISTENCE_FAILED,
            SESSION_EXPORT_INCOMPLETE,
        }

    @property
    def reconciliation_required(self) -> bool:
        return self.code is RECONCILIATION_REQUIRED

    @classmethod
    def of(
        cls,
        code: BrowserWorkflowCode,
        *,
        operation_state: str = "",
        error_category: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "BrowserWorkflowResult":
        return cls(
            code=code,
            operation_state=str(operation_state or ""),
            error_category=str(error_category or ""),
            metadata=metadata or {},
        )
