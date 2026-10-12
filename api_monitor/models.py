"""Shared data types for the API synthetic monitor."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class FailureClass(str, Enum):
    """Classification of a failed or degraded health check."""

    CONNECTION_ERROR = "CONNECTION_ERROR"
    DNS_ERROR = "DNS_ERROR"
    TLS_ERROR = "TLS_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESPONSE_VALIDATION_FAILED = "RESPONSE_VALIDATION_FAILED"
    RESPONSE_TIME_EXCEEDED = "RESPONSE_TIME_EXCEEDED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class Outcome(str, Enum):
    """Overall result of a single health check."""

    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"  # functionally correct but slower than the warning threshold
    FAILURE = "FAILURE"


class HealthState(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    RECOVERING = "RECOVERING"


class NotificationStatus(str, Enum):
    NONE = "NONE"                      # nothing to notify
    SENT = "SENT"
    FAILED = "FAILED"                  # email API call failed
    SUPPRESSED = "SUPPRESSED"          # inside suppression window
    SKIPPED_CIRCUIT_OPEN = "SKIPPED_CIRCUIT_OPEN"
    SKIPPED_RATE_LIMIT = "SKIPPED_RATE_LIMIT"
    DISABLED = "DISABLED"              # notifications disabled by configuration
    DRY_RUN = "DRY_RUN"                # on-demand check without --apply


@dataclass
class HttpResponseData:
    """Minimal, in-memory view of an HTTP response. Never persisted or logged."""

    status_code: int
    headers: Dict[str, str]
    body: bytes
    elapsed_ms: float
    url_label: str = ""
    redirects_followed: int = 0

    @property
    def content_type(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value
        return ""


@dataclass
class CheckResult:
    """Outcome of one health check execution (after retries)."""

    service_id: str
    execution_id: str
    started_at: _dt.datetime
    outcome: Outcome
    classification: Optional[FailureClass] = None
    http_status: Optional[int] = None
    response_time_ms: Optional[float] = None
    attempts: int = 1
    expected: str = ""
    actual: str = ""
    detail: str = ""
    validation_failures: List[str] = field(default_factory=list)
    trigger: str = "schedule"
    state_after: Optional[str] = None          # health state after alert processing
    notification: Optional[str] = None         # e.g. "ALERT:SENT", "RECOVERY:FAILED", None

    @property
    def is_failure(self) -> bool:
        return self.outcome == Outcome.FAILURE


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def to_iso(value: Optional[_dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def from_iso(value: Optional[str]) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
