"""Incident detection, state transitions and notification suppression.

State machine (per service)::

    UNKNOWN/HEALTHY --failure (< threshold)--> DEGRADED
    any            --failure (>= threshold)--> DOWN        (incident opened -> ALERT)
    HEALTHY        --slow  (>= slow threshold, if enabled)--> DEGRADED (incident opened -> ALERT)
    DEGRADED incident --failure >= threshold--> DOWN       (ESCALATION)
    DOWN/DEGRADED  --success (< recovery threshold)--> RECOVERING
    RECOVERING     --success (>= recovery threshold)--> HEALTHY (incident closed -> RECOVERY)
    RECOVERING     --failure--> DOWN (same incident, no new alert)

Notification rules:
* first alert only after ``failure_threshold`` consecutive failures;
* no alert while inside ``suppression_interval_seconds`` of the previous one;
* REMINDER every ``reminder_interval_seconds`` while the incident is open (0 = off);
* a failed delivery leaves ``alert_pending`` set; the next check of the same
  service retries it once (bounded by the check interval, circuit breaker and
  hourly cap) - there is no tight retry loop;
* RECOVERY only if an alert for the incident was actually delivered.

Timestamps that must survive restarts use UTC wall-clock time.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from config_loader import AppConfig, ServiceConfig
from email_client import DeliveryResult, EmailClient
from logging_config import get_events_logger, get_logger, log_event
from models import CheckResult, HealthState, NotificationStatus, Outcome, from_iso, to_iso, utc_now
from state_manager import Incident, ServiceState, StateManager

LOG = get_logger("alerts")
EVENTS = get_events_logger()
MAX_RECOVERY_DELIVERY_ATTEMPTS = 3


@dataclass
class ProcessOutcome:
    state: str
    previous_state: str
    notification_status: NotificationStatus
    event_type: Optional[str] = None
    incident_id: Optional[str] = None


def _duration(start: Optional[str], now: _dt.datetime) -> str:
    begin = from_iso(start)
    if not begin:
        return ""
    seconds = int((now - begin).total_seconds())
    hours, rem = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def _seconds_since(value: Optional[str], now: _dt.datetime) -> Optional[float]:
    when = from_iso(value)
    return None if when is None else (now - when).total_seconds()


class AlertManager:
    def __init__(self, config: AppConfig, state: StateManager, email: Optional[EmailClient],
                 clock: Callable[[], _dt.datetime] = utc_now) -> None:
        self.config = config
        self.state = state
        self.email = email
        self.clock = clock
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, service_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(service_id, threading.Lock())

    # ------------------------------------------------------------------ public
    def process(self, service: ServiceConfig, result: CheckResult) -> ProcessOutcome:
        with self._lock_for(service.id):
            st = self.state.get(service.id)
            previous = st.state
            now = self.clock()
            now_iso = to_iso(now)
            st.last_check_at = now_iso
            st.last_http_status = result.http_status
            st.last_response_time_ms = round(result.response_time_ms, 1) if result.response_time_ms is not None else None
            st.last_classification = result.classification.value if result.classification else None

            if result.outcome == Outcome.SUCCESS:
                notification = self._on_success(service, result, st, now)
            elif result.outcome == Outcome.DEGRADED:
                notification = self._on_degraded(service, result, st, now)
            else:
                notification = self._on_failure(service, result, st, now)

            status = notification.status if notification else NotificationStatus.NONE
            event_type = notification.event_type if notification else None
            st.last_notification_status = status.value if notification else st.last_notification_status
            self.state.put(st)
            if st.state != previous:
                log_event(EVENTS, logging.WARNING if st.state in ("DOWN", "DEGRADED") else logging.INFO,
                          "STATE_CHANGED", service_id=service.id, previous_state=previous, state=st.state,
                          execution_id=result.execution_id,
                          failure_reason=st.last_classification)
            return ProcessOutcome(st.state, previous, status, event_type,
                                  st.incident.incident_id if st.incident else None)

    # ------------------------------------------------------------------ transitions
    def _on_success(self, service: ServiceConfig, result: CheckResult, st: ServiceState,
                    now: _dt.datetime) -> Optional["_Sent"]:
        st.consecutive_successes += 1
        st.consecutive_failures = 0
        st.consecutive_degraded = 0
        st.failure_streak_started_at = None
        st.last_success_at = to_iso(now)
        policy = service.alerting
        if st.incident is None:
            st.state = HealthState.HEALTHY.value
            return self._retry_pending_recovery(service, result, st, now)
        if st.consecutive_successes < policy.recovery_threshold:
            st.state = HealthState.RECOVERING.value
            return None
        incident = st.incident
        st.state = HealthState.HEALTHY.value
        st.incident = None
        st.last_recovery_at = to_iso(now)
        log_event(EVENTS, logging.INFO, "INCIDENT_RESOLVED", service_id=service.id,
                  incident_id=incident.incident_id, duration=_duration(incident.started_at, now),
                  alerts_sent=incident.alerts_sent)
        wants_recovery = policy.send_recovery and self.config.notifications.get("recovery_notifications", True)
        if not wants_recovery:
            return None
        if incident.alerts_sent == 0:
            log_event(EVENTS, logging.INFO, "RECOVERY_NOT_NOTIFIED", service_id=service.id,
                      incident_id=incident.incident_id, detail="no alert was delivered for this incident")
            return None
        sent = self._notify(service, result, st, incident, "RECOVERY", now)
        if sent.status != NotificationStatus.SENT and sent.status not in (NotificationStatus.DISABLED,):
            st.pending_recovery = {"incident_id": incident.incident_id, "started_at": incident.started_at,
                                   "detected_at": incident.detected_at, "alerts_sent": incident.alerts_sent,
                                   "attempts": 1}
        return sent

    def _retry_pending_recovery(self, service: ServiceConfig, result: CheckResult, st: ServiceState,
                                now: _dt.datetime) -> Optional["_Sent"]:
        pending = st.pending_recovery
        if not pending:
            return None
        incident = Incident(incident_id=pending["incident_id"], kind="DOWN", started_at=pending["started_at"],
                            detected_at=pending["detected_at"], alerts_sent=pending.get("alerts_sent", 1))
        sent = self._notify(service, result, st, incident, "RECOVERY", now)
        attempts = int(pending.get("attempts", 1)) + 1
        if sent.status == NotificationStatus.SENT or attempts >= MAX_RECOVERY_DELIVERY_ATTEMPTS:
            st.pending_recovery = None
        else:
            pending["attempts"] = attempts
        return sent

    def _on_degraded(self, service: ServiceConfig, result: CheckResult, st: ServiceState,
                     now: _dt.datetime) -> Optional["_Sent"]:
        policy = service.alerting
        st.consecutive_degraded += 1
        st.consecutive_successes = 0
        st.consecutive_failures = 0
        st.failure_streak_started_at = None
        st.state = HealthState.DEGRADED.value
        if st.incident is not None:
            st.incident.last_classification = st.last_classification
            return self._follow_up(service, result, st, now)
        if policy.alert_on_slow_response and st.consecutive_degraded >= policy.slow_response_threshold:
            st.incident = self._open_incident(service, st, "DEGRADED", now)
            return self._notify(service, result, st, st.incident, "ALERT", now)
        return None

    def _on_failure(self, service: ServiceConfig, result: CheckResult, st: ServiceState,
                    now: _dt.datetime) -> Optional["_Sent"]:
        policy = service.alerting
        st.consecutive_failures += 1
        st.consecutive_successes = 0
        st.consecutive_degraded = 0
        st.last_failure_at = to_iso(now)
        if st.consecutive_failures == 1 or not st.failure_streak_started_at:
            st.failure_streak_started_at = to_iso(result.started_at)
        if st.incident is not None:
            st.state = HealthState.DOWN.value if (
                st.incident.kind == "DOWN" or st.consecutive_failures >= policy.failure_threshold
            ) else HealthState.DEGRADED.value
            st.incident.last_classification = st.last_classification
            if st.incident.kind == "DEGRADED" and st.consecutive_failures >= policy.failure_threshold:
                st.incident.kind = "DOWN"
                log_event(EVENTS, logging.ERROR, "INCIDENT_ESCALATED", service_id=service.id,
                          incident_id=st.incident.incident_id, failure_reason=st.last_classification)
                return self._notify(service, result, st, st.incident, "ESCALATION", now)
            return self._follow_up(service, result, st, now)
        if st.consecutive_failures < policy.failure_threshold:
            st.state = HealthState.DEGRADED.value
            return None
        st.state = HealthState.DOWN.value
        st.incident = self._open_incident(service, st, "DOWN", now)
        return self._notify(service, result, st, st.incident, "ALERT", now)

    # ------------------------------------------------------------------ helpers
    def _open_incident(self, service: ServiceConfig, st: ServiceState, kind: str, now: _dt.datetime) -> Incident:
        incident = Incident(
            incident_id=f"{service.id}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
            kind=kind,
            started_at=st.failure_streak_started_at or to_iso(now),
            detected_at=to_iso(now),
            first_classification=st.last_classification,
            last_classification=st.last_classification,
        )
        log_event(EVENTS, logging.ERROR, "INCIDENT_OPENED", service_id=service.id,
                  incident_id=incident.incident_id, kind=kind, failure_reason=st.last_classification,
                  consecutive_failures=st.consecutive_failures)
        return incident

    def _follow_up(self, service: ServiceConfig, result: CheckResult, st: ServiceState,
                   now: _dt.datetime) -> Optional["_Sent"]:
        """Open incident: retry a pending alert or send a reminder when due."""
        incident = st.incident
        assert incident is not None
        policy = service.alerting
        if incident.alert_pending:
            return self._notify(service, result, st, incident, incident.pending_event_type or "ALERT", now)
        if policy.reminder_interval_seconds > 0:
            since = _seconds_since(incident.last_alert_at, now)
            if since is not None and since >= policy.reminder_interval_seconds:
                return self._notify(service, result, st, incident, "REMINDER", now)
        return None

    def _notify(self, service: ServiceConfig, result: CheckResult, st: ServiceState,
                incident: Incident, event_type: str, now: _dt.datetime) -> "_Sent":
        policy = service.alerting
        if not policy.enabled or not self.config.notifications.get("enabled", False) or self.email is None:
            log_event(EVENTS, logging.INFO, "NOTIFICATION_DISABLED", service_id=service.id,
                      event_type=event_type, incident_id=incident.incident_id)
            return _Sent(NotificationStatus.DISABLED, event_type)

        bypass_suppression = event_type in ("RECOVERY", "ESCALATION")
        since_last = _seconds_since(st.last_alert_at, now)
        if not bypass_suppression and since_last is not None and since_last < policy.suppression_interval_seconds:
            if event_type in ("ALERT",):
                incident.alert_pending = True
                incident.pending_event_type = event_type
            log_event(EVENTS, logging.INFO, "ALERT_SUPPRESSED", service_id=service.id, event_type=event_type,
                      incident_id=incident.incident_id, seconds_since_last_alert=int(since_last))
            return _Sent(NotificationStatus.SUPPRESSED, event_type)

        alert_id = uuid.uuid4().hex[:12]
        ctx = self.build_context(service, result, st, incident, event_type, now, alert_id)
        delivery: DeliveryResult = self.email.send(ctx, policy.recipients, policy.cc)
        if delivery.status == NotificationStatus.SENT:
            if event_type != "RECOVERY":
                incident.alerts_sent += 1
                incident.last_alert_at = to_iso(now)
                incident.alert_pending = False
                incident.pending_event_type = None
                st.last_alert_at = to_iso(now)
            st.alert_delivery_failures = 0
            log_event(EVENTS, logging.WARNING if event_type != "RECOVERY" else logging.INFO,
                      f"{event_type}_SENT", service_id=service.id, incident_id=incident.incident_id,
                      alert_id=alert_id, failure_reason=ctx.get("classification"), state=st.state)
        else:
            if event_type != "RECOVERY":
                incident.alert_pending = True
                incident.pending_event_type = event_type
            if delivery.status == NotificationStatus.FAILED:
                st.alert_delivery_failures += 1
                st.last_delivery_failure_at = to_iso(now)
            log_event(EVENTS, logging.ERROR, "ALERT_DELIVERY_FAILED", service_id=service.id,
                      incident_id=incident.incident_id, alert_id=alert_id, event_type=event_type,
                      notification_status=delivery.status.value, detail=delivery.detail)
        return _Sent(delivery.status, event_type)

    def build_context(self, service: ServiceConfig, result: CheckResult, st: ServiceState,
                      incident: Incident, event_type: str, now: _dt.datetime, alert_id: str) -> Dict[str, Any]:
        return {
            "environment": self.config.environment,
            "service_id": service.id,
            "service_name": service.name,
            "endpoint": service.endpoint_label,
            "timestamp": to_iso(now),
            "event_type": event_type,
            "check_result": result.outcome.value,
            "state": st.state,
            "severity": service.alerting.severity if event_type != "RECOVERY" else "INFO",
            "classification": result.classification.value if result.classification else None,
            "http_status": result.http_status,
            "response_time_ms": round(result.response_time_ms) if result.response_time_ms is not None else None,
            "expected": result.expected,
            "actual": result.actual,
            "consecutive_failures": st.consecutive_failures,
            "consecutive_successes": st.consecutive_successes,
            "incident_id": incident.incident_id,
            "incident_kind": incident.kind,
            "incident_started_at": incident.started_at,
            "incident_detected_at": incident.detected_at,
            "incident_duration": _duration(incident.started_at, now),
            "alert_count": incident.alerts_sent + (0 if event_type == "RECOVERY" else 1),
            "monitor_host": self.config.monitor_host_id,
            "execution_id": result.execution_id,
            "alert_id": alert_id,
            "attempts": result.attempts,
        }


@dataclass
class _Sent:
    status: NotificationStatus
    event_type: str
