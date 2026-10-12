"""Execution of a single health check: request -> validation -> retries -> state/alerts -> log."""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from typing import Optional

from alert_manager import AlertManager, ProcessOutcome
from config_loader import AppConfig, ServiceConfig
from http_client import AttemptResult, RequestSpec, SecureHttpClient
from logging_config import get_logger, log_event
from models import CheckResult, FailureClass, HealthState, NotificationStatus, Outcome, utc_now
from rate_limiter import TokenBucket
from reporting import StatsCollector
from state_manager import StateManager
from validators import validate_response

LOG = get_logger("checker")


def build_request_spec(service: ServiceConfig) -> RequestSpec:
    return RequestSpec(
        url=service.url, method=service.method, headers=service.headers, query_params=service.query_params,
        body=service.body, auth=service.auth, connect_timeout=service.connect_timeout,
        read_timeout=service.read_timeout, total_timeout=service.total_timeout,
        follow_redirects=service.follow_redirects, max_redirects=service.max_redirects,
        ca_bundle=service.ca_bundle, client_cert=service.client_cert, url_label=service.endpoint_label,
    )


def is_retryable(result: CheckResult, retry_on: tuple) -> bool:
    if result.outcome != Outcome.FAILURE or result.classification is None:
        return False
    cls = result.classification.value
    if cls == FailureClass.HTTP_ERROR.value:
        return "HTTP_5XX" in retry_on and result.http_status is not None and 500 <= result.http_status <= 599 \
            and result.http_status != 501
    return cls in retry_on


class ServiceChecker:
    def __init__(self, config: AppConfig, http: SecureHttpClient, alerts: AlertManager, state: StateManager,
                 stats: StatsCollector, request_limiter: TokenBucket, retry_limiter: TokenBucket,
                 stop_event: threading.Event, rng: Optional[random.Random] = None) -> None:
        self.config = config
        self.http = http
        self.alerts = alerts
        self.state = state
        self.stats = stats
        self.request_limiter = request_limiter
        self.retry_limiter = retry_limiter
        self.stop_event = stop_event
        self.rng = rng or random.Random()

    def _evaluate(self, service: ServiceConfig, attempt: AttemptResult, base: CheckResult) -> CheckResult:
        base.response_time_ms = round(attempt.elapsed_ms, 1)
        if attempt.response is None:
            base.outcome = Outcome.FAILURE
            base.classification = attempt.classification or FailureClass.CONNECTION_ERROR
            base.expected = "HTTP " + "/".join(str(c) for c in service.validation.expected_status)
            base.actual = attempt.detail
            base.detail = attempt.detail
            base.http_status = None
            return base
        report = validate_response(attempt.response, service.validation)
        base.outcome = report.outcome
        base.classification = report.classification
        base.http_status = attempt.response.status_code
        base.expected = report.expected
        base.actual = report.actual
        base.validation_failures = report.failures
        base.detail = "; ".join(report.failures)[:300]
        return base

    def backoff_delay(self, service: ServiceConfig, attempt_number: int) -> float:
        policy = service.retry
        delay = policy.backoff_initial_seconds * (policy.backoff_multiplier ** (attempt_number - 1))
        delay = min(delay, policy.backoff_max_seconds)
        return delay + self.rng.uniform(0, policy.jitter_seconds)

    def run(self, service: ServiceConfig, *, apply_state: bool = True, trigger: str = "schedule") -> CheckResult:
        """Run one check (with bounded retries). Never raises."""
        execution_id = uuid.uuid4().hex[:16]
        started = utc_now()
        base = CheckResult(service_id=service.id, execution_id=execution_id, started_at=started,
                           outcome=Outcome.FAILURE, trigger=trigger)
        try:
            result = self._run_with_retries(service, base, apply_state)
        except Exception as exc:  # monitor bug: log loudly, do not mark the service down
            log_event(LOG, logging.ERROR, "CHECK_INTERNAL_ERROR", service_id=service.id,
                      execution_id=execution_id, failure_reason=FailureClass.INTERNAL_ERROR.value,
                      detail=f"{exc.__class__.__name__}: {exc}"[:300])
            LOG.debug("internal error detail", exc_info=True)
            base.classification = FailureClass.INTERNAL_ERROR
            base.detail = exc.__class__.__name__
            return base

        outcome: Optional[ProcessOutcome] = None
        if apply_state and result.classification != FailureClass.INTERNAL_ERROR:
            try:
                outcome = self.alerts.process(service, result)
                result.state_after = outcome.state
                if outcome.event_type:
                    result.notification = f"{outcome.event_type}:{outcome.notification_status.value}"
            except Exception as exc:
                log_event(LOG, logging.ERROR, "ALERT_PROCESSING_ERROR", service_id=service.id,
                          execution_id=execution_id, detail=f"{exc.__class__.__name__}: {exc}"[:300])
        try:
            self.stats.record(result)
        except Exception:  # statistics must never affect monitoring
            LOG.debug("stats record failed", exc_info=True)

        level = logging.INFO if result.outcome == Outcome.SUCCESS else logging.WARNING
        log_event(
            LOG, level, "CHECK_COMPLETED",
            service_id=service.id, service_name=service.name, execution_id=execution_id, trigger=trigger,
            endpoint=service.endpoint_label, http_status=result.http_status,
            response_time_ms=result.response_time_ms, validation_outcome=result.outcome.value,
            failure_reason=result.classification.value if result.classification else None,
            detail=result.detail or None, attempts=result.attempts,
            state=outcome.state if outcome else None,
            notification_status=(outcome.notification_status.value if outcome
                                 else NotificationStatus.DRY_RUN.value),
            notification_event=outcome.event_type if outcome else None,
            incident_id=outcome.incident_id if outcome else None,
        )
        return result

    def _run_with_retries(self, service: ServiceConfig, base: CheckResult, apply_state: bool) -> CheckResult:
        spec = build_request_spec(service)
        policy = service.retry
        start = time.monotonic()
        known_down = self.state.get(service.id).state == HealthState.DOWN.value if apply_state else False
        attempts = 0
        result = base
        while True:
            attempts += 1
            attempt = self.http.execute(spec)
            result = self._evaluate(service, attempt, base)
            result.attempts = attempts
            if result.outcome != Outcome.FAILURE or not is_retryable(result, policy.retry_on):
                return result
            if attempts >= policy.max_attempts:
                return result
            if known_down and not policy.retry_when_down:
                return result  # do not hammer a backend that is already known to be down
            delay = self.backoff_delay(service, attempts)
            if time.monotonic() - start + delay + service.total_timeout > policy.max_total_seconds:
                log_event(LOG, logging.INFO, "RETRY_SKIPPED", service_id=service.id,
                          execution_id=base.execution_id, reason="retry time budget exhausted")
                return result
            if not self.retry_limiter.try_acquire() or not self.request_limiter.try_acquire():
                log_event(LOG, logging.WARNING, "RETRY_SKIPPED", service_id=service.id,
                          execution_id=base.execution_id, reason="global retry/rate budget exhausted")
                return result
            log_event(LOG, logging.INFO, "CHECK_RETRY", service_id=service.id, execution_id=base.execution_id,
                      attempt=attempts + 1, delay_seconds=round(delay, 2),
                      failure_reason=result.classification.value if result.classification else None)
            if self.stop_event.wait(delay):
                return result  # shutting down
