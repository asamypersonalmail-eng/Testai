"""Test helpers: a local mock HTTP server and configuration builders."""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

Reply = Tuple[int, Dict[str, str], Union[bytes, str], float]  # status, headers, body, delay seconds


def json_reply(payload: Any, status: int = 200, delay: float = 0.0) -> Reply:
    return status, {"Content-Type": "application/json"}, json.dumps(payload), delay


def xml_reply(body: str, status: int = 200, delay: float = 0.0) -> Reply:
    return status, {"Content-Type": "text/xml; charset=utf-8"}, body, delay


class MockServer:
    """Threaded HTTP server. ``routes[path]`` is a Reply, a list of Replies (consumed
    in order, last one repeats) or a callable(request_dict) -> Reply."""

    def __init__(self) -> None:
        self.routes: Dict[str, Any] = {}
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                req = {"method": self.command, "path": path, "raw_path": self.path,
                       "headers": dict(self.headers), "body": body}
                with server_ref._lock:
                    server_ref.requests.append(req)
                    route = server_ref.routes.get(path)
                    if isinstance(route, list):
                        reply = route.pop(0) if len(route) > 1 else route[0]
                    else:
                        reply = route
                if callable(reply):
                    reply = reply(req)
                if reply is None:
                    reply = (404, {"Content-Type": "text/plain"}, "not found", 0.0)
                status, headers, payload, delay = reply
                if delay:
                    time.sleep(delay)
                data = payload.encode() if isinstance(payload, str) else payload
                try:
                    self.send_response(status)
                    for key, val in headers.items():
                        self.send_header(key, val)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PUT = do_HEAD = _handle  # noqa: N815

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "MockServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def requests_for(self, path: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [r for r in self.requests if r["path"] == path]


def closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_service(sid: str, url: str, **overrides: Any) -> Dict[str, Any]:
    service = {
        "id": sid,
        "name": sid.replace("-", " ").title(),
        "enabled": True,
        "operation_type": "INQUIRY",
        "read_only": True,
        "request": {"url": url, "method": "GET", "headers": {"Accept": "application/json"}},
        "interval": {"value": 60, "unit": "seconds"},
        "timeout": {"connect_seconds": 1, "read_seconds": 2, "total_seconds": 3},
        "retry": {"max_attempts": 1, "backoff_initial_seconds": 0, "jitter_seconds": 0, "max_total_seconds": 20},
        "validation": {
            "expected_status": [200],
            "content_type": "application/json",
            "body_format": "json",
            "rules": {"type": "json_equals", "path": "$.status", "value": "SUCCESS"},
        },
        "alerting": {"failure_threshold": 2, "recovery_threshold": 2, "suppression_interval_seconds": 600,
                     "reminder_interval_seconds": 3600},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(service.get(key), dict):
            service[key] = {**service[key], **value}
        else:
            service[key] = value
    return service


def make_raw_config(tmpdir: str, services: List[Dict[str, Any]], email_url: str = "http://127.0.0.1:9/mail",
                    **overrides: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {
        "config_version": 1,
        "environment": "UAT",
        "monitor_host_id": "test-host",
        "monitoring_enabled": True,
        "limits": {
            "max_concurrent_checks": 4, "max_requests_per_cycle": 50, "max_requests_per_minute": 600,
            "max_retries_per_minute": 100, "max_response_bytes": 65536, "min_interval_seconds": 10,
            "scheduler_tick_seconds": 0.1, "startup_jitter_seconds": 0, "schedule_jitter_seconds": 0,
            "shutdown_grace_seconds": 5,
        },
        "security": {
            "allowed_hosts": ["127.0.0.1:*"],
            "allowed_schemes": ["http", "https"],
            "allowed_methods": ["GET", "POST"],
            "forbidden_url_keywords": ["transfer", "payment"],
        },
        "logging": {"directory": os.path.join(tmpdir, "logs"), "level": "DEBUG", "retention_days": 2},
        "state": {"directory": os.path.join(tmpdir, "state")},
        "runtime": {"lock_file": os.path.join(tmpdir, "state", "monitor.lock")},
        "reload": {"enabled": True, "watch_interval_seconds": 5},
        "reporting": {"history_size": 50, "stats_log_interval_seconds": 0},
        "notifications": {
            "enabled": True,
            "max_emails_per_hour": 100,
            "sender": "monitor@test.example",
            "default_recipients": ["ops@test.example"],
            "email_api": {
                "url": email_url,
                "method": "POST",
                "headers": {"Content-Type": "application/json", "X-API-Key": "${env:TEST_EMAIL_KEY}"},
                "connect_timeout_seconds": 1,
                "read_timeout_seconds": 2,
                "max_attempts": 2,
                "retry_backoff_seconds": 0,
                "success_status_codes": [200, 202],
                "payload_format": "json",
                "recipient_format": "list",
                "payload_template": {"to": "{{recipients}}", "cc": "{{cc}}", "subject": "{{subject}}",
                                     "body": "{{body}}", "priority": "{{priority}}",
                                     "meta": {"service": "{{service_id}}", "event": "{{event_type}}"}},
                "circuit_breaker": {"failure_threshold": 3, "open_seconds": 60},
            },
        },
        "services": services,
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    return raw


def write_config(tmpdir: str, raw: Dict[str, Any], name: str = "config.json") -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2)
    return path


class FakeClock:
    def __init__(self, start: Optional[_dt.datetime] = None) -> None:
        self.now = start or _dt.datetime(2026, 1, 1, 8, 0, 0, tzinfo=_dt.timezone.utc)

    def __call__(self) -> _dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + _dt.timedelta(seconds=seconds)


class FakeMonotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def deep(obj: Any) -> Any:
    return copy.deepcopy(obj)


def close_logging() -> None:
    """Close monitor log handlers so temp directories can be deleted (required on Windows)."""
    import logging
    for name in ("apimon", "apimon.events"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()
