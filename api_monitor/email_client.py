"""Adapter for the existing middleware e-mail API.

The middleware contract is NOT hard-coded. ``notifications.email_api`` maps
alert fields onto whatever request the middleware expects:

* ``payload_format``  json | form | raw (e.g. a SOAP envelope)
* ``payload_template`` object (json/form) or string (raw) containing
  ``{{placeholder}}`` tokens. A JSON string that is exactly ``"{{name}}"`` is
  replaced by the typed value (so ``"to": "{{recipients}}"`` can become a list).
* ``recipient_format`` list | comma | semicolon | object_list
* ``${env:...}`` / ``${file:...}`` references are resolved in the template
  *before* alert values are substituted, so text coming from a monitored
  response can never trigger secret resolution.

Protection against alert loops / storms:
* bounded attempts per notification (``max_attempts`` <= 3);
* a circuit breaker (persisted) that stops calling the API after repeated
  failures for ``open_seconds``;
* a global hourly cap (``notifications.max_emails_per_hour``);
* sends are serialised (one e-mail API call at a time);
* failures are reported back to the caller - never re-queued internally.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote_plus
from xml.sax.saxutils import escape as xml_escape

from config_loader import AppConfig
from http_client import RequestSpec, SecureHttpClient
from logging_config import get_events_logger, get_logger, log_event
from models import FailureClass, NotificationStatus, Outcome, to_iso, utc_now
from rate_limiter import SlidingWindowCounter
from secrets_resolver import SecretResolutionError, resolve
from state_manager import StateManager
from validators import ValidationSpec, compile_rule, validate_response

LOG = get_logger("email")
EVENTS = get_events_logger()
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
FULL_PLACEHOLDER_RE = re.compile(r"^\{\{\s*([A-Za-z0-9_]+)\s*\}\}$")
_SEND_LOCK = threading.Lock()
RETRYABLE = {FailureClass.CONNECTION_ERROR, FailureClass.TIMEOUT, FailureClass.DNS_ERROR}

DEFAULT_PRIORITY = {"CRITICAL": "HIGH", "HIGH": "HIGH", "MEDIUM": "NORMAL", "LOW": "LOW"}
BODY_FIELDS = [
    ("Environment", "environment"),
    ("Service", "service_label"),
    ("Endpoint", "endpoint"),
    ("Event", "event_type"),
    ("Event time (UTC)", "timestamp"),
    ("Check result", "check_result"),
    ("Current state", "state"),
    ("Severity", "severity"),
    ("Failure classification", "classification"),
    ("HTTP status", "http_status"),
    ("Response time (ms)", "response_time_ms"),
    ("Expected result", "expected"),
    ("Actual result", "actual"),
    ("Consecutive failures", "consecutive_failures"),
    ("Consecutive successes", "consecutive_successes"),
    ("Incident ID", "incident_id"),
    ("Incident started (UTC)", "incident_started_at"),
    ("Incident detected (UTC)", "incident_detected_at"),
    ("Incident duration", "incident_duration"),
    ("Alerts sent for incident", "alert_count"),
    ("Monitoring host", "monitor_host"),
    ("Execution ID", "execution_id"),
    ("Alert ID", "alert_id"),
]


@dataclass
class DeliveryResult:
    status: NotificationStatus
    detail: str = ""
    http_status: Optional[int] = None
    attempts: int = 0


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def render_text(template: str, values: Dict[str, Any], escape: Optional[str] = None) -> str:
    def repl(match: "re.Match[str]") -> str:
        value = values.get(match.group(1), "")
        text = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else ("" if value is None else str(value))
        if escape == "html":
            return html.escape(text)
        if escape == "xml":
            return xml_escape(text, {'"': "&quot;", "'": "&apos;"})
        if escape == "json":
            return json.dumps(text)[1:-1]
        return text

    return PLACEHOLDER_RE.sub(repl, template)


def render_payload(template: Any, values: Dict[str, Any]) -> Any:
    """Substitute ``{{name}}`` placeholders in a JSON-like template (typed for exact matches)."""
    if isinstance(template, str):
        full = FULL_PLACEHOLDER_RE.match(template)
        if full:
            return values.get(full.group(1), "")
        return render_text(template, values)
    if isinstance(template, dict):
        return {key: render_payload(val, values) for key, val in template.items()}
    if isinstance(template, list):
        return [render_payload(item, values) for item in template]
    return template


def default_body(values: Dict[str, Any], body_format: str) -> str:
    title = f"API health {values.get('event_type', '')} - {values.get('service_name', '')}"
    if body_format == "html":
        rows = "".join(
            f"<tr><th align='left'>{html.escape(label)}</th><td>{html.escape(_fmt(values.get(key)))}</td></tr>"
            for label, key in BODY_FIELDS
        )
        return (f"<html><body><h3>{html.escape(title)}</h3><table border='1' cellpadding='4' "
                f"cellspacing='0'>{rows}</table><p>Automated message from the API synthetic monitor. "
                f"Do not reply.</p></body></html>")
    width = max(len(label) for label, _ in BODY_FIELDS)
    lines = [title, "=" * len(title), ""]
    lines += [f"{label.ljust(width)} : {_fmt(values.get(key))}" for label, key in BODY_FIELDS]
    lines += ["", "Automated message from the API synthetic monitor. Do not reply."]
    return "\n".join(lines)


def format_recipients(addresses: Sequence[str], api: Dict[str, Any]) -> Any:
    fmt = api.get("recipient_format", "list")
    if fmt == "comma":
        return ",".join(addresses)
    if fmt == "semicolon":
        return ";".join(addresses)
    if fmt == "object_list":
        key = api["recipient_object_key"]
        extra = api.get("recipient_object_extra") or {}
        return [dict(extra, **{key: addr}) for addr in addresses]
    return list(addresses)


class EmailClient:
    def __init__(self, config: AppConfig, http: SecureHttpClient, state: StateManager,
                 hourly_limiter: SlidingWindowCounter, sleep=time.sleep) -> None:
        self.config = config
        self.notifications = config.notifications
        self.api = config.notifications["email_api"]
        self.http = http
        self.state = state
        self.hourly_limiter = hourly_limiter
        self._sleep = sleep
        breaker = self.api.get("circuit_breaker") or {}
        self.breaker_threshold = int(breaker.get("failure_threshold", 3))
        self.breaker_open_seconds = float(breaker.get("open_seconds", 600))
        rules = self.api.get("success_rules")
        self.success_spec = ValidationSpec(
            expected_status=list(self.api.get("success_status_codes") or [200, 201, 202, 204]),
            content_type=None,
            body_format=self.api.get("success_body_format", "none") if rules else "none",
            rules=compile_rule(rules) if rules else None,
            warning_ms=None, critical_ms=None,
        )

    # ------------------------------------------------------------------ build
    def build_values(self, ctx: Dict[str, Any], recipients: Sequence[str], cc: Sequence[str]) -> Dict[str, Any]:
        values = dict(ctx)
        values.setdefault("service_label", f"{ctx.get('service_name', '')} ({ctx.get('service_id', '')})")
        values["recipients"] = format_recipients(recipients, self.api)
        values["cc"] = format_recipients(cc, self.api)
        values["recipients_csv"] = ",".join(recipients)
        values["sender"] = self.notifications.get("sender", "")
        priority_map = dict(DEFAULT_PRIORITY, **(self.api.get("severity_priority_map") or {}))
        values["priority"] = priority_map.get(str(ctx.get("severity", "")), "NORMAL")
        body_format = self.notifications.get("body_format", "text")
        values["body_format"] = body_format
        values["content_type"] = "text/html" if body_format == "html" else "text/plain"
        values["subject"] = render_text(self.notifications["subject_template"], values)[:250]
        body_template = self.notifications.get("body_template")
        if body_template:
            values["body"] = render_text(body_template, values, "html" if body_format == "html" else None)
        else:
            values["body"] = default_body(values, body_format)
        return values

    def build_request(self, values: Dict[str, Any]) -> RequestSpec:
        api = self.api
        # Resolve secrets in the operator-controlled template FIRST, then substitute alert values.
        template = resolve(api["payload_template"])
        fmt = api["payload_format"]
        if fmt == "raw":
            content = render_text(template, values, api.get("raw_escape", "xml"))
            body = {"type": "raw", "content": content,
                    "content_type": api.get("raw_content_type", "application/xml; charset=utf-8")}
        elif fmt == "form":
            payload = render_payload(template, values)
            form = "&".join(
                f"{_quote(k)}={_quote(v if not isinstance(v, (list, dict)) else json.dumps(v))}"
                for k, v in payload.items()
            )
            body = {"type": "raw", "content": form, "content_type": "application/x-www-form-urlencoded"}
        else:
            body = {"type": "json", "content": render_payload(template, values)}
        headers = dict(api.get("headers") or {})
        if body["type"] == "raw":  # the payload format decides the content type
            headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            headers["Content-Type"] = body["content_type"]
        return RequestSpec(
            url=api["url"], method=api["method"], headers=headers,
            query_params={}, body=body, auth=dict(api.get("auth") or {"type": "none"}),
            connect_timeout=float(api.get("connect_timeout_seconds", 5)),
            read_timeout=float(api.get("read_timeout_seconds", 15)),
            total_timeout=float(api.get("connect_timeout_seconds", 5)) + float(api.get("read_timeout_seconds", 15)),
            ca_bundle=api.get("ca_bundle"), client_cert=api.get("client_cert"),
            url_label="email-api", resolve_body=False,
        )

    # ------------------------------------------------------------------ send
    def send(self, ctx: Dict[str, Any], recipients: Sequence[str], cc: Sequence[str] = ()) -> DeliveryResult:
        """Send one notification. Never raises."""
        if not self.notifications.get("enabled", False):
            return DeliveryResult(NotificationStatus.DISABLED, "notifications disabled")
        if not recipients:
            return DeliveryResult(NotificationStatus.FAILED, "no recipients configured")
        with _SEND_LOCK:
            notifier = self.state.get_notifier()
            now = time.time()
            if notifier.open_until_epoch > now:
                return DeliveryResult(NotificationStatus.SKIPPED_CIRCUIT_OPEN,
                                      f"e-mail API circuit open for {notifier.open_until_epoch - now:.0f}s")
            if not self.hourly_limiter.try_acquire():
                log_event(LOG, logging.WARNING, "EMAIL_RATE_LIMITED",
                          max_emails_per_hour=self.notifications.get("max_emails_per_hour"))
                return DeliveryResult(NotificationStatus.SKIPPED_RATE_LIMIT, "hourly e-mail cap reached")
            try:
                values = self.build_values(ctx, recipients, cc)
                spec = self.build_request(values)
            except SecretResolutionError as exc:
                result = DeliveryResult(NotificationStatus.FAILED, f"e-mail API secret resolution failed: {exc}")
                self._record(result, ctx)
                return result
            except Exception as exc:  # template problems must not break monitoring
                result = DeliveryResult(NotificationStatus.FAILED, f"payload rendering failed ({exc.__class__.__name__})")
                self._record(result, ctx)
                return result
            result = self._deliver(spec)
            self._record(result, ctx)
            return result

    def _deliver(self, spec: RequestSpec) -> DeliveryResult:
        max_attempts = int(self.api.get("max_attempts", 2))
        backoff = float(self.api.get("retry_backoff_seconds", 2))
        last = DeliveryResult(NotificationStatus.FAILED, "not attempted")
        for attempt in range(1, max_attempts + 1):
            outcome = self.http.execute(spec, extra_methods=("POST", "PUT"))
            if outcome.response is not None:
                report = validate_response(outcome.response, self.success_spec)
                status = outcome.response.status_code
                if report.outcome != Outcome.FAILURE:
                    return DeliveryResult(NotificationStatus.SENT, "accepted", status, attempt)
                last = DeliveryResult(NotificationStatus.FAILED, f"rejected: {report.actual}"[:200], status, attempt)
                if status < 500:
                    return last  # 4xx / business rejection: retrying will not help
            else:
                last = DeliveryResult(NotificationStatus.FAILED,
                                      f"{outcome.classification.value if outcome.classification else 'ERROR'}: "
                                      f"{outcome.detail}", None, attempt)
                if outcome.classification not in RETRYABLE:
                    return last
            if attempt < max_attempts:
                self._sleep(backoff * attempt)
        return last

    def _record(self, result: DeliveryResult, ctx: Dict[str, Any]) -> None:
        notifier = self.state.get_notifier()
        if result.status == NotificationStatus.SENT:
            if notifier.consecutive_failures >= self.breaker_threshold:
                log_event(EVENTS, logging.INFO, "EMAIL_CIRCUIT_CLOSED")
            notifier.consecutive_failures = 0
            notifier.open_until_epoch = 0.0
        else:
            notifier.consecutive_failures += 1
            notifier.total_delivery_failures += 1
            notifier.last_failure_at = to_iso(utc_now())
            if notifier.consecutive_failures >= self.breaker_threshold:
                notifier.open_until_epoch = time.time() + self.breaker_open_seconds
                log_event(EVENTS, logging.ERROR, "EMAIL_CIRCUIT_OPENED",
                          consecutive_failures=notifier.consecutive_failures,
                          open_seconds=self.breaker_open_seconds)
        self.state.put_notifier(notifier)
        log_event(LOG, logging.INFO if result.status == NotificationStatus.SENT else logging.ERROR,
                  "EMAIL_DELIVERY", notification_status=result.status.value, http_status=result.http_status,
                  attempts=result.attempts, detail=result.detail, service_id=ctx.get("service_id"),
                  alert_id=ctx.get("alert_id"), event_type=ctx.get("event_type"))


def _quote(value: Any) -> str:
    return quote_plus("" if value is None else str(value))


def make_test_context(config: AppConfig) -> Dict[str, Any]:
    """Context for ``monitor.py test-email``."""
    now = to_iso(utc_now())
    return {
        "environment": config.environment, "service_id": "monitor-self-test",
        "service_name": "API monitor e-mail self-test", "endpoint": "n/a", "timestamp": now,
        "event_type": "TEST", "check_result": "TEST", "state": "TEST", "severity": "LOW",
        "classification": None, "http_status": None, "response_time_ms": None,
        "expected": "e-mail delivered", "actual": "test message", "consecutive_failures": 0,
        "consecutive_successes": 0, "incident_id": None, "incident_started_at": None,
        "incident_detected_at": None, "incident_duration": None, "alert_count": 0,
        "monitor_host": config.monitor_host_id, "execution_id": None, "alert_id": "self-test",
    }


def recipients_for(config: AppConfig, override: Optional[List[str]] = None) -> List[str]:
    return list(override or config.notifications.get("default_recipients") or [])
