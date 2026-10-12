#!/usr/bin/env python3
"""API Synthetic Monitor - application entry point.

Commands
--------
  run            run continuously (systemd / SRC / inittab)
  once           run every enabled service once and exit (cron / Control-M / TWS)
  check ID       run one service on demand (dry-run unless --apply)
  validate       validate the configuration (and optionally secret references)
  status         print the persisted health state
  report         statistics, latency percentiles, failure trends, alert history
  test-email     send a test notification through the middleware e-mail API

Exit codes: 0 OK, 1 check failure(s), 2 configuration error,
            3 another instance is running, 4 runtime error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import signal
import sys
import threading
from typing import Any, Dict, List, Optional

# Make sibling modules importable regardless of the working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alert_manager import AlertManager  # noqa: E402
from checker import ServiceChecker  # noqa: E402
from config_loader import AppConfig, ConfigError, load_config, secret_reference_problems  # noqa: E402
from email_client import EmailClient, make_test_context, recipients_for  # noqa: E402
from http_client import SecureHttpClient  # noqa: E402
from logging_config import get_logger, log_event, setup_logging  # noqa: E402
from models import Outcome  # noqa: E402
from rate_limiter import SlidingWindowCounter, TokenBucket  # noqa: E402
from reporting import StatsCollector, build_report, write_csv  # noqa: E402
from scheduler import MonitorScheduler  # noqa: E402
from state_manager import StateManager, read_state_file  # noqa: E402

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

EXIT_OK, EXIT_CHECK_FAILED, EXIT_CONFIG, EXIT_LOCKED, EXIT_RUNTIME = 0, 1, 2, 3, 4
LOG = get_logger("main")


class InstanceLock:
    """Single-instance guard (non-blocking ``flock`` on a PID file)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), mode=0o750, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o640)
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class Runtime:
    """Objects shared across configuration reloads."""

    def __init__(self, config: AppConfig, stop_event: threading.Event, persist_state: bool = True) -> None:
        self.stop_event = stop_event
        self.state = StateManager(config.state_file, persist=persist_state)
        self.state.load()
        self.stats = StatsCollector(int(config.reporting.get("history_size", 500)))
        self.hourly_emails = SlidingWindowCounter(int(config.notifications.get("max_emails_per_hour", 30)), 3600)
        self._buckets: Dict[str, TokenBucket] = {}

    def _bucket(self, name: str, rate: float) -> TokenBucket:
        key = f"{name}:{rate}"
        if key not in self._buckets:
            self._buckets = {k: v for k, v in self._buckets.items() if not k.startswith(name + ":")}
            self._buckets[key] = TokenBucket(rate)
        return self._buckets[key]

    def checker_factory(self, config: AppConfig) -> ServiceChecker:
        self.hourly_emails.set_limit(int(config.notifications.get("max_emails_per_hour", 30)))
        http = SecureHttpClient(config.security, int(config.limits["max_response_bytes"]))
        email = EmailClient(config, http, self.state, self.hourly_emails)
        alerts = AlertManager(config, self.state, email)
        return ServiceChecker(
            config, http, alerts, self.state, self.stats,
            self._bucket("requests", float(config.limits["max_requests_per_minute"])),
            self._bucket("retries", float(config.limits["max_retries_per_minute"])),
            self.stop_event,
        )


# --------------------------------------------------------------------------- helpers

def _load(path: str) -> AppConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR ({path}):", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)


def _report_secret_problems(config: AppConfig) -> List[str]:
    problems = secret_reference_problems(config)
    for problem in problems:
        log_event(LOG, logging.ERROR, "SECRET_REFERENCE_PROBLEM", failure_reason="CONFIGURATION_ERROR",
                  detail=problem)
    return problems


def _result_dict(result: Any) -> Dict[str, Any]:
    return {
        "service_id": result.service_id,
        "execution_id": result.execution_id,
        "outcome": result.outcome.value,
        "classification": result.classification.value if result.classification else None,
        "http_status": result.http_status,
        "response_time_ms": result.response_time_ms,
        "attempts": result.attempts,
        "state": result.state_after,
        "notification": result.notification,
        "expected": result.expected,
        "actual": result.actual,
    }


def _install_signals(stop: threading.Event, scheduler: Optional[MonitorScheduler], config: AppConfig) -> None:
    def _stop(signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if scheduler is not None and hasattr(signal, "SIGHUP") and config.reload.get("on_sighup", True):
        signal.signal(signal.SIGHUP, lambda s, f: scheduler.request_reload())


# --------------------------------------------------------------------------- commands

def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args.config)
    setup_logging(config.logging, config.environment, console_override=args.console or None)
    _report_secret_problems(config)
    lock = InstanceLock(config.runtime["lock_file"])
    if not lock.acquire():
        log_event(LOG, logging.ERROR, "ALREADY_RUNNING", lock_file=config.runtime["lock_file"])
        print("another monitor instance is already running", file=sys.stderr)
        return EXIT_LOCKED
    stop = threading.Event()
    try:
        runtime = Runtime(config, stop)
        scheduler = MonitorScheduler(config, runtime.checker_factory, stop)
        _install_signals(stop, scheduler, config)
        log_event(LOG, logging.INFO, "MONITOR_STARTED", pid=os.getpid(), config_path=config.path,
                  monitor_host=config.monitor_host_id, services=len(config.services))
        scheduler.run_forever()
        log_event(LOG, logging.INFO, "MONITOR_STOPPED")
        return EXIT_OK
    except Exception as exc:
        log_event(LOG, logging.CRITICAL, "MONITOR_CRASHED", detail=f"{exc.__class__.__name__}: {exc}"[:300])
        LOG.debug("crash detail", exc_info=True)
        return EXIT_RUNTIME
    finally:
        lock.release()


def cmd_once(args: argparse.Namespace) -> int:
    config = _load(args.config)
    setup_logging(config.logging, config.environment, console_override=args.console or None)
    _report_secret_problems(config)
    lock = InstanceLock(config.runtime["lock_file"])
    if not lock.acquire():
        print("another monitor instance is running; skipping this cycle", file=sys.stderr)
        return EXIT_LOCKED
    stop = threading.Event()
    try:
        runtime = Runtime(config, stop)
        scheduler = MonitorScheduler(config, runtime.checker_factory, stop)
        _install_signals(stop, None, config)
        results = scheduler.run_single_cycle(args.service or None, only_due=not args.all)
        scheduler.shutdown()
        for result in results:
            print(json.dumps(_result_dict(result)))
        return EXIT_CHECK_FAILED if any(r.outcome == Outcome.FAILURE for r in results) else EXIT_OK
    finally:
        lock.release()


def cmd_check(args: argparse.Namespace) -> int:
    config = _load(args.config)
    setup_logging(config.logging, config.environment, console_override=args.console or None)
    service = config.service(args.service_id)
    if service is None:
        print(f"unknown service id '{args.service_id}'", file=sys.stderr)
        return EXIT_CONFIG
    lock: Optional[InstanceLock] = None
    if args.apply:
        lock = InstanceLock(config.runtime["lock_file"])
        if not lock.acquire():
            print("--apply needs exclusive access; stop the running monitor or run without --apply",
                  file=sys.stderr)
            return EXIT_LOCKED
    try:
        runtime = Runtime(config, threading.Event(), persist_state=bool(args.apply))
        checker = runtime.checker_factory(config)
        result = checker.run(service, apply_state=bool(args.apply), trigger="manual")
        print(json.dumps(_result_dict(result), indent=2))
        return EXIT_CHECK_FAILED if result.outcome == Outcome.FAILURE else EXIT_OK
    finally:
        if lock is not None:
            lock.release()


def cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args.config)
    print(f"configuration OK: environment={config.environment} services={len(config.services)} "
          f"enabled={sum(1 for s in config.services if s.enabled)}")
    for svc in config.services:
        print(f"  - {svc.id:<32} every {svc.interval_seconds:>6.0f}s  {svc.method:<4} {svc.endpoint_label}"
              f"{'' if svc.enabled else '  (disabled)'}")
    if args.check_secrets:
        problems = secret_reference_problems(config)
        for problem in problems:
            print(f"SECRET PROBLEM: {problem}", file=sys.stderr)
        if problems:
            return EXIT_CONFIG
        print("secret references OK")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args.config)
    data = read_state_file(config.state_file)
    if args.json:
        print(json.dumps(data, indent=2))
        return EXIT_OK
    services = data.get("services", {})
    if not services:
        print("no state recorded yet")
        return EXIT_OK
    print(f"{'SERVICE':<32} {'STATE':<11} {'FAILS':>5} {'OKS':>4} {'LAST CHECK':<21} {'LAST MS':>8} INCIDENT")
    for sid, st in sorted(services.items()):
        incident = (st.get("incident") or {}).get("incident_id", "-")
        rt = st.get("last_response_time_ms")
        print(f"{sid:<32} {st.get('state', ''):<11} {st.get('consecutive_failures', 0):>5} "
              f"{st.get('consecutive_successes', 0):>4} {str(st.get('last_check_at') or '-'):<21} "
              f"{(f'{rt:.0f}' if rt is not None else '-'):>8} {incident}")
    notifier = data.get("notifier") or {}
    if notifier.get("consecutive_failures"):
        print(f"\ne-mail API: {notifier['consecutive_failures']} consecutive delivery failure(s)")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    config = _load(args.config)
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=args.hours)
    report = build_report(config.logging["directory"], since, args.service or None)
    if args.csv:
        write_csv(report["services"], args.csv)
        print(f"CSV summary written to {args.csv}", file=sys.stderr)
    print(json.dumps(report, indent=2))
    return EXIT_OK


def cmd_test_email(args: argparse.Namespace) -> int:
    config = _load(args.config)
    setup_logging(config.logging, config.environment, console_override=args.console or None)
    runtime = Runtime(config, threading.Event(), persist_state=False)
    http = SecureHttpClient(config.security, int(config.limits["max_response_bytes"]))
    client = EmailClient(config, http, runtime.state, runtime.hourly_emails)
    recipients = recipients_for(config, args.recipient)
    result = client.send(make_test_context(config), recipients)
    print(json.dumps({"status": result.status.value, "http_status": result.http_status,
                      "attempts": result.attempts, "detail": result.detail}, indent=2))
    return EXIT_OK if result.status.value == "SENT" else EXIT_RUNTIME


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monitor.py", description="API synthetic monitoring and alerting tool")
    parser.add_argument("-c", "--config", default=os.environ.get("API_MONITOR_CONFIG", "config.json"),
                        help="path to config.json (env API_MONITOR_CONFIG)")
    parser.add_argument("--console", action="store_true", help="also log to stderr")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run continuously").set_defaults(func=cmd_run)
    p_once = sub.add_parser("once", help="run one monitoring cycle and exit")
    p_once.add_argument("--service", action="append", help="run only this service id (repeatable, always runs)")
    p_once.add_argument("--all", action="store_true", help="run every enabled service even if not due")
    p_once.set_defaults(func=cmd_once)
    p_check = sub.add_parser("check", help="run one service on demand")
    p_check.add_argument("service_id")
    p_check.add_argument("--apply", action="store_true", help="update state and send alerts (default: dry run)")
    p_check.set_defaults(func=cmd_check)
    p_val = sub.add_parser("validate", help="validate configuration")
    p_val.add_argument("--check-secrets", action="store_true", help="also verify referenced env vars / files")
    p_val.set_defaults(func=cmd_validate)
    p_status = sub.add_parser("status", help="show persisted health state")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)
    p_rep = sub.add_parser("report", help="statistics from log files")
    p_rep.add_argument("--hours", type=float, default=24.0)
    p_rep.add_argument("--service", action="append")
    p_rep.add_argument("--csv", help="write CSV summary to this path")
    p_rep.set_defaults(func=cmd_report)
    p_mail = sub.add_parser("test-email", help="send a test notification")
    p_mail.add_argument("--recipient", action="append")
    p_mail.set_defaults(func=cmd_test_email)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
