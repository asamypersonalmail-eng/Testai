"""Configuration loading, schema validation, semantic checks and normalisation.

The JSON configuration is validated in three stages before monitoring starts
(or before a reload is applied):

1. JSON syntax (duplicate keys are rejected).
2. JSON Schema (``config.schema.json``) - structure, types, enums, secret refs.
3. Semantic checks - allowlists, forbidden keywords, timeout/interval budgets,
   rule compilation, sensitive headers, recipients.

The result is an immutable-by-convention :class:`AppConfig` with every
service's effective settings (defaults merged in).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import jsonschema

from secrets_resolver import PURE_SECRET_REF_RE, check_references, iter_references
from validators import RuleConfigError, ValidationSpec, compile_rule

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.schema.json")
MAX_CONFIG_BYTES = 5 * 1024 * 1024
UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600}
SENSITIVE_HEADER_NAMES = ("authorization", "proxy-authorization", "cookie", "x-api-key", "apikey",
                          "api-key", "x-auth-token", "x-access-token", "token", "password", "secret")

BUILTIN_DEFAULTS: Dict[str, Any] = {
    "interval": {"value": 5, "unit": "minutes"},
    "timeout": {"connect_seconds": 5, "read_seconds": 15, "total_seconds": 30},
    "expected_status": [200],
    "retry": {
        "max_attempts": 2, "backoff_initial_seconds": 2, "backoff_max_seconds": 15,
        "backoff_multiplier": 2, "jitter_seconds": 1, "max_total_seconds": 90,
        "retry_on": ["CONNECTION_ERROR", "TIMEOUT", "HTTP_5XX"], "retry_when_down": False,
    },
    "response_time": {"warning_ms": None, "critical_ms": None},
    "alerting": {
        "enabled": True, "severity": "HIGH", "failure_threshold": 2, "recovery_threshold": 2,
        "suppression_interval_seconds": 900, "reminder_interval_seconds": 3600,
        "alert_on_slow_response": False, "slow_response_threshold": 3, "send_recovery": True,
    },
}
BUILTIN_LIMITS: Dict[str, Any] = {
    "max_concurrent_checks": 4, "max_requests_per_cycle": 20, "max_requests_per_minute": 30,
    "max_retries_per_minute": 10, "max_response_bytes": 1048576, "min_interval_seconds": 60,
    "scheduler_tick_seconds": 1.0, "startup_jitter_seconds": 30, "schedule_jitter_seconds": 5,
    "shutdown_grace_seconds": 30,
}
BUILTIN_SECURITY: Dict[str, Any] = {
    "allowed_schemes": ["https"], "allowed_methods": ["GET", "POST"], "allow_redirects": False,
    "forbidden_url_keywords": [], "ca_bundle": None, "use_environment_proxies": False, "proxies": {},
}


class ConfigError(Exception):
    """Configuration is invalid. ``errors`` lists every problem found."""

    def __init__(self, errors: List[str]) -> None:
        self.errors = errors
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(errors))


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_initial_seconds: float
    backoff_max_seconds: float
    backoff_multiplier: float
    jitter_seconds: float
    max_total_seconds: float
    retry_on: Tuple[str, ...]
    retry_when_down: bool


@dataclass(frozen=True)
class AlertPolicy:
    enabled: bool
    severity: str
    failure_threshold: int
    recovery_threshold: int
    suppression_interval_seconds: float
    reminder_interval_seconds: float
    alert_on_slow_response: bool
    slow_response_threshold: int
    send_recovery: bool
    recipients: Tuple[str, ...]
    cc: Tuple[str, ...]


@dataclass
class ServiceConfig:
    id: str
    name: str
    enabled: bool
    endpoint_label: str
    url: str
    method: str
    headers: Dict[str, str]
    query_params: Dict[str, str]
    body: Optional[Dict[str, Any]]
    auth: Dict[str, Any]
    follow_redirects: bool
    max_redirects: int
    ca_bundle: Optional[str]
    client_cert: Optional[Dict[str, str]]
    interval_seconds: float
    connect_timeout: float
    read_timeout: float
    total_timeout: float
    retry: RetryPolicy
    validation: ValidationSpec
    alerting: AlertPolicy
    fingerprint: str
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)
    variables: Dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    path: str
    environment: str
    monitor_host_id: str
    monitoring_enabled: bool
    limits: Dict[str, Any]
    security: Dict[str, Any]
    logging: Dict[str, Any]
    state: Dict[str, Any]
    runtime: Dict[str, Any]
    reload: Dict[str, Any]
    reporting: Dict[str, Any]
    notifications: Dict[str, Any]
    services: List[ServiceConfig]
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)
    content_hash: str = ""

    def service(self, service_id: str) -> Optional[ServiceConfig]:
        for svc in self.services:
            if svc.id == service_id:
                return svc
        return None

    @property
    def state_file(self) -> str:
        return os.path.join(self.state["directory"], self.state.get("file", "health_state.json"))


# --------------------------------------------------------------------------- helpers

def deep_merge(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key '{key}'")
        result[key] = value
    return result


class HostAllowlist:
    """Matches URLs against ``security.allowed_hosts``.

    Entry formats: ``host`` (default port of the scheme only), ``host:8443``,
    ``host:*`` (any port), ``*.domain.internal`` (sub-domains), optionally with port.
    """

    DEFAULT_PORTS = {"https": 443, "http": 80}

    def __init__(self, entries: List[str], schemes: List[str], forbidden_keywords: Optional[List[str]] = None) -> None:
        self.schemes = [s.lower() for s in schemes]
        self.forbidden = [k.lower() for k in (forbidden_keywords or [])]
        self.entries: List[Tuple[str, Optional[str]]] = []
        for entry in entries:
            host, port = entry, None
            if entry.startswith("["):  # [ipv6]:port
                end = entry.find("]")
                host, rest = entry[1:end], entry[end + 1:]
                port = rest[1:] if rest.startswith(":") else None
            elif entry.count(":") == 1:
                host, port = entry.split(":")
            self.entries.append((host.lower(), port))

    def check(self, url: str) -> Optional[str]:
        """Return a reason string if the URL is NOT allowed, else ``None``."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return "malformed URL"
        scheme = parts.scheme.lower()
        if scheme not in self.schemes:
            return f"scheme '{scheme}' is not allowed"
        if parts.username or parts.password:
            return "credentials must not be embedded in URLs"
        host = (parts.hostname or "").lower()
        if not host:
            return "URL has no host"
        effective_port = port or self.DEFAULT_PORTS.get(scheme)
        for entry_host, entry_port in self.entries:
            if entry_host.startswith("*."):
                host_ok = host.endswith(entry_host[1:]) and host != entry_host[2:]
            else:
                host_ok = host == entry_host
            if not host_ok:
                continue
            if entry_port == "*":
                return self._check_keywords(parts)
            if entry_port is None and effective_port == self.DEFAULT_PORTS.get(scheme):
                return self._check_keywords(parts)
            if entry_port is not None and entry_port.isdigit() and int(entry_port) == effective_port:
                return self._check_keywords(parts)
        return f"host '{host}:{effective_port}' is not in security.allowed_hosts"

    def _check_keywords(self, parts: Any) -> Optional[str]:
        target = (parts.path + "?" + parts.query).lower()
        for keyword in self.forbidden:
            if keyword in target:
                return f"URL contains forbidden keyword '{keyword}'"
        return None


def sanitize_endpoint(url: str) -> str:
    """Endpoint identifier safe for logs and e-mails: no query string, numbers masked."""
    try:
        parts = urlsplit(url)
        port_value = parts.port
    except ValueError:
        return "<invalid-url>"
    segments = []
    for seg in parts.path.split("/"):
        digits = sum(ch.isdigit() for ch in seg)
        segments.append("{id}" if digits >= 6 else seg)
    port = f":{port_value}" if port_value else ""
    return f"{parts.scheme}://{parts.hostname}{port}{'/'.join(segments)}"


# --------------------------------------------------------------------------- loading

def _load_schema() -> Dict[str, Any]:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _schema_errors(raw: Dict[str, Any]) -> List[str]:
    validator_cls = jsonschema.validators.validator_for(_load_schema())
    validator = validator_cls(_load_schema())
    errors = []
    for err in sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "<root>"
        message = err.message
        if err.validator in ("oneOf", "anyOf") and err.context:
            message = "; ".join(sorted({c.message for c in err.context}))[:300]
        if len(message) > 300:
            message = message[:300] + "..."
        errors.append(f"{location}: {message}")
    return errors


def _build_service(raw_svc: Dict[str, Any], defaults: Dict[str, Any], top: Dict[str, Any],
                   allowlist: HostAllowlist, errors: List[str]) -> Optional[ServiceConfig]:
    sid = raw_svc["id"]
    prefix = f"services[{sid}]"
    limits = top["limits"]
    security = top["security"]
    request = raw_svc["request"]

    method = request["method"].upper()
    if method not in security["allowed_methods"]:
        errors.append(f"{prefix}: method {method} is not in security.allowed_methods")
    variables = {k: str(v) for k, v in (top.get("variables") or {}).items()}
    variables.update({k: str(v) for k, v in (raw_svc.get("variables") or {}).items()})
    for kind, ref in iter_references(request):
        if kind == "var" and ref not in variables:
            errors.append(f"{prefix}: ${{var:{ref}}} is used but not defined in 'variables'")
    for name, value in variables.items():
        if any(kind == "var" for kind, _ in iter_references(value)):
            errors.append(f"{prefix}: variable '{name}' must not reference another ${{var:...}}")
    if "${" in urlsplit(request["url"]).netloc or "${" in urlsplit(request["url"]).scheme:
        errors.append(f"{prefix}: url: ${{...}} references are only allowed in the path/query, not the host")
    reason = allowlist.check(request["url"])
    if reason:
        errors.append(f"{prefix}: url rejected: {reason}")
    for key in (request.get("query_params") or {}):
        if any(k in key.lower() for k in allowlist.forbidden):
            errors.append(f"{prefix}: query parameter '{key}' contains a forbidden keyword")

    for name, value in (request.get("headers") or {}).items():
        if name.lower() in SENSITIVE_HEADER_NAMES and not PURE_SECRET_REF_RE.match(value):
            errors.append(f"{prefix}: header '{name}' must be a secret reference like ${{env:NAME}}")
    follow = bool(request.get("follow_redirects", False))
    if follow and not security["allow_redirects"]:
        errors.append(f"{prefix}: follow_redirects requires security.allow_redirects=true")

    interval_cfg = raw_svc.get("interval") or defaults["interval"]
    interval = float(interval_cfg["value"]) * UNIT_SECONDS[interval_cfg["unit"]]
    if interval < float(limits["min_interval_seconds"]):
        errors.append(f"{prefix}: interval {interval:.0f}s is below limits.min_interval_seconds")

    timeout = deep_merge(defaults["timeout"], raw_svc.get("timeout"))
    connect, read, total = (float(timeout["connect_seconds"]), float(timeout["read_seconds"]),
                            float(timeout["total_seconds"]))
    if connect > total or read > total:
        errors.append(f"{prefix}: connect/read timeouts must not exceed total_seconds")

    retry_raw = deep_merge(defaults["retry"], raw_svc.get("retry"))
    retry = RetryPolicy(
        max_attempts=int(retry_raw["max_attempts"]),
        backoff_initial_seconds=float(retry_raw["backoff_initial_seconds"]),
        backoff_max_seconds=float(retry_raw["backoff_max_seconds"]),
        backoff_multiplier=float(retry_raw["backoff_multiplier"]),
        jitter_seconds=float(retry_raw["jitter_seconds"]),
        max_total_seconds=float(retry_raw["max_total_seconds"]),
        retry_on=tuple(retry_raw.get("retry_on", [])),
        retry_when_down=bool(retry_raw.get("retry_when_down", False)),
    )
    if retry.max_total_seconds >= interval:
        errors.append(f"{prefix}: retry.max_total_seconds must be lower than the execution interval")
    if total > retry.max_total_seconds:
        errors.append(f"{prefix}: timeout.total_seconds must not exceed retry.max_total_seconds")

    validation_raw = dict(raw_svc.get("validation") or {})
    validation_raw.setdefault("expected_status", defaults["expected_status"])
    response_time = deep_merge(defaults["response_time"], raw_svc.get("response_time"))
    try:
        spec = ValidationSpec.from_config(validation_raw, response_time)
    except RuleConfigError as exc:
        errors.append(f"{prefix}: validation: {exc}")
        spec = None

    alert_raw = deep_merge(defaults["alerting"], raw_svc.get("alerting"))
    notifications = top["notifications"]
    recipients = alert_raw.get("recipients") or notifications.get("default_recipients") or []
    cc = alert_raw.get("cc") or notifications.get("default_cc") or []
    policy = AlertPolicy(
        enabled=bool(alert_raw["enabled"]),
        severity=alert_raw["severity"],
        failure_threshold=int(alert_raw["failure_threshold"]),
        recovery_threshold=int(alert_raw["recovery_threshold"]),
        suppression_interval_seconds=float(alert_raw["suppression_interval_seconds"]),
        reminder_interval_seconds=float(alert_raw["reminder_interval_seconds"]),
        alert_on_slow_response=bool(alert_raw["alert_on_slow_response"]),
        slow_response_threshold=int(alert_raw["slow_response_threshold"]),
        send_recovery=bool(alert_raw["send_recovery"]),
        recipients=tuple(recipients),
        cc=tuple(cc),
    )
    if policy.enabled and notifications["enabled"] and not policy.recipients:
        errors.append(f"{prefix}: no alert recipients (set alerting.recipients or notifications.default_recipients)")
    if 0 < policy.reminder_interval_seconds < policy.suppression_interval_seconds:
        errors.append(f"{prefix}: reminder_interval_seconds must be >= suppression_interval_seconds")

    if spec is None:
        return None
    fingerprint = hashlib.sha256(json.dumps(raw_svc, sort_keys=True).encode()).hexdigest()[:16]
    return ServiceConfig(
        id=sid,
        name=raw_svc["name"],
        enabled=bool(raw_svc.get("enabled", True)),
        endpoint_label=raw_svc.get("endpoint_label") or sanitize_endpoint(request["url"]),
        url=request["url"],
        method=method,
        headers=dict(request.get("headers") or {}),
        query_params=dict(request.get("query_params") or {}),
        body=request.get("body"),
        auth=dict(request.get("auth") or {"type": "none"}),
        follow_redirects=follow,
        max_redirects=int(request.get("max_redirects", 2 if follow else 0)),
        ca_bundle=request.get("ca_bundle") or security.get("ca_bundle"),
        client_cert=request.get("client_cert"),
        interval_seconds=interval,
        connect_timeout=connect,
        read_timeout=read,
        total_timeout=total,
        retry=retry,
        validation=spec,
        alerting=policy,
        fingerprint=fingerprint,
        raw=raw_svc,
        variables=variables,
    )


def _check_email_api(top: Dict[str, Any], allowlist: HostAllowlist, errors: List[str]) -> None:
    notifications = top["notifications"]
    api = notifications["email_api"]
    reason = allowlist.check(api["url"])
    if reason:
        errors.append(f"notifications.email_api.url rejected: {reason}")
    for name, value in (api.get("headers") or {}).items():
        if name.lower() in SENSITIVE_HEADER_NAMES and not PURE_SECRET_REF_RE.match(value):
            errors.append(f"notifications.email_api: header '{name}' must be a secret reference")
    fmt = api["payload_format"]
    template = api["payload_template"]
    if fmt in ("json", "form") and not isinstance(template, dict):
        errors.append("notifications.email_api.payload_template must be an object for json/form payloads")
    if fmt == "raw" and not isinstance(template, str):
        errors.append("notifications.email_api.payload_template must be a string for raw payloads")
    if api.get("recipient_format") == "object_list" and not api.get("recipient_object_key"):
        errors.append("notifications.email_api.recipient_object_key is required for recipient_format=object_list")
    if api.get("success_rules"):
        try:
            compile_rule(api["success_rules"])
        except RuleConfigError as exc:
            errors.append(f"notifications.email_api.success_rules: {exc}")


def parse_config(raw: Dict[str, Any], path: str = "<memory>", content_hash: str = "") -> AppConfig:
    """Validate and normalise an already-parsed configuration object."""
    errors = _schema_errors(raw)
    if errors:
        raise ConfigError(errors)

    top = copy.deepcopy(raw)
    top["limits"] = deep_merge(BUILTIN_LIMITS, raw.get("limits"))
    top["security"] = deep_merge(BUILTIN_SECURITY, raw.get("security"))
    defaults = deep_merge(BUILTIN_DEFAULTS, raw.get("defaults"))
    allowlist = HostAllowlist(top["security"]["allowed_hosts"], top["security"]["allowed_schemes"],
                              top["security"]["forbidden_url_keywords"])

    seen: Dict[str, int] = {}
    for svc in raw["services"]:
        seen[svc["id"]] = seen.get(svc["id"], 0) + 1
    errors.extend(f"services: duplicate service id '{sid}'" for sid, n in seen.items() if n > 1)

    _check_email_api(top, allowlist, errors)
    services = []
    for raw_svc in raw["services"]:
        svc = _build_service(raw_svc, defaults, top, allowlist, errors)
        if svc is not None:
            services.append(svc)
    if errors:
        raise ConfigError(errors)

    return AppConfig(
        path=path,
        environment=raw["environment"],
        monitor_host_id=raw.get("monitor_host_id") or socket.gethostname(),
        monitoring_enabled=bool(raw.get("monitoring_enabled", True)),
        limits=top["limits"],
        security=top["security"],
        logging=dict(raw["logging"]),
        state=dict(raw["state"]),
        runtime=dict(raw["runtime"]),
        reload=deep_merge({"enabled": True, "watch_interval_seconds": 30, "on_sighup": True}, raw.get("reload")),
        reporting=deep_merge({"history_size": 500, "stats_log_interval_seconds": 3600, "csv_summary_path": None},
                             raw.get("reporting")),
        notifications=deep_merge({"recovery_notifications": True, "max_emails_per_hour": 30,
                                  "body_format": "text",
                                  "subject_template": "[{{environment}}][{{severity}}] {{event_type}}: {{service_name}} is {{state}}"},
                                 raw["notifications"]),
        services=services,
        raw=raw,
        content_hash=content_hash,
    )


def read_config_bytes(path: str) -> bytes:
    size = os.path.getsize(path)
    if size > MAX_CONFIG_BYTES:
        raise ConfigError([f"configuration file is too large ({size} bytes)"])
    with open(path, "rb") as handle:
        return handle.read()


def load_config(path: str) -> AppConfig:
    """Load, validate and normalise the configuration file at ``path``."""
    try:
        data = read_config_bytes(path)
    except OSError as exc:
        raise ConfigError([f"cannot read configuration file '{path}': {exc.strerror or exc}"]) from None
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError:
        raise ConfigError(["configuration file is not valid UTF-8"]) from None
    except ValueError as exc:
        raise ConfigError([f"invalid JSON: {exc}"]) from None
    if not isinstance(raw, dict):
        raise ConfigError(["configuration root must be a JSON object"])
    return parse_config(raw, os.path.abspath(path), hashlib.sha256(data).hexdigest())


def secret_reference_problems(config: AppConfig) -> List[str]:
    """Problems with secrets referenced by the configuration (missing env vars, bad files)."""
    problems = [f"notifications.email_api: {p}" for p in check_references(config.notifications["email_api"])]
    for svc in config.services:
        if svc.enabled:
            problems.extend(f"services[{svc.id}]: {p}" for p in check_references(svc.raw.get("request", {})))
            problems.extend(f"services[{svc.id}]: {p}" for p in check_references(svc.variables))
    return problems
