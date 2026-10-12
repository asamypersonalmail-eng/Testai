#!/usr/bin/env python3
"""Self-contained demonstration: healthy, failing (business error), degraded and recovered services.

Starts a local mock backend and a mock e-mail middleware API, runs eight monitoring
cycles through the real checker / alert manager / e-mail adapter, and prints a
timeline plus every e-mail the middleware received. Nothing leaves 127.0.0.1.

    python3 examples/demo.py
"""
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tests.helpers import MockServer, close_logging, json_reply, make_raw_config, make_service, write_config  # noqa: E402

import monitor  # noqa: E402
from config_loader import load_config  # noqa: E402
from logging_config import setup_logging  # noqa: E402

OK = {"status": "SUCCESS", "data": {"balance": 100}}
BUSINESS_ERROR = {"status": "FAILED", "errorCode": "CORE_TIMEOUT"}


def main() -> None:
    os.environ.setdefault("TEST_EMAIL_KEY", "demo-mail-key")
    with tempfile.TemporaryDirectory() as tmp, MockServer() as backend, MockServer() as mail:
        mail.routes["/mail"] = json_reply({"status": "QUEUED"}, status=202)
        backend.routes["/healthy"] = json_reply(OK)
        backend.routes["/slow"] = json_reply(OK, delay=0.25)
        alerting = {"failure_threshold": 2, "recovery_threshold": 2, "suppression_interval_seconds": 3600,
                    "reminder_interval_seconds": 0, "alert_on_slow_response": True, "slow_response_threshold": 2}
        services = [
            make_service("healthy-inquiry", backend.url("/healthy"), alerting=alerting),
            make_service("failing-inquiry", backend.url("/failing"), alerting=alerting),
            make_service("slow-inquiry", backend.url("/slow"), alerting=alerting,
                         response_time={"warning_ms": 150, "critical_ms": 2000}),
        ]
        raw = make_raw_config(tmp, services, email_url=mail.url("/mail"))
        raw["logging"]["level"] = "INFO"
        config = load_config(write_config(tmp, raw))
        setup_logging(config.logging, config.environment)
        runtime = monitor.Runtime(config, threading.Event())
        checker = runtime.checker_factory(config)

        # cycle -> backend behaviour of the failing service
        plan = {1: OK, 2: BUSINESS_ERROR, 3: BUSINESS_ERROR, 4: BUSINESS_ERROR, 5: BUSINESS_ERROR, 6: OK, 7: OK, 8: OK}
        print(f"{'CYCLE':<6}{'SERVICE':<18}{'OUTCOME':<10}{'CLASSIFICATION':<29}{'STATE':<12}{'NOTIFICATION'}")
        for cycle, body in plan.items():
            backend.routes["/failing"] = json_reply(body)
            for svc in config.services:
                result = checker.run(svc)
                print(f"{cycle:<6}{svc.id:<18}{result.outcome.value:<10}"
                      f"{(result.classification.value if result.classification else '-'):<29}"
                      f"{result.state_after:<12}{result.notification or '-'}")
            print()
        print("E-mails received by the mock middleware API:")
        for req in mail.requests_for("/mail"):
            payload = json.loads(req["body"])
            print(f"  - {payload['subject']}")
        print("\nLogs and state were written to a temporary directory that is deleted on exit.")
        close_logging()  # release log files so the temporary directory can be removed (Windows)


if __name__ == "__main__":
    main()
