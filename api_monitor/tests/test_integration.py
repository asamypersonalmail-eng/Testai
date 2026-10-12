"""End-to-end tests: real HTTP stack, mocked backends and a mocked e-mail middleware API."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from tests.helpers import MockServer, close_logging, json_reply, make_raw_config, make_service, write_config

import monitor
from config_loader import load_config
from logging_config import setup_logging
from models import FailureClass, Outcome
from state_manager import read_state_file

SECRET_TOKEN = "tok-SUPERSECRET-998877"
TEST_ACCOUNT = "4455667788990011"
BODY_MARKER = "Mr Synthetic Customer"
OK_BODY = {"status": "SUCCESS", "data": {"accountNumber": TEST_ACCOUNT, "holder": BODY_MARKER, "balance": 10}}
BIZ_ERROR = {"status": "FAILED", "errorCode": "CORE_TIMEOUT", "holder": BODY_MARKER}


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = MockServer().__enter__()
        self.mail = MockServer().__enter__()
        self.mail.routes["/mail"] = json_reply({"status": "QUEUED"}, status=202)
        self.env = mock.patch.dict(os.environ, {"TEST_EMAIL_KEY": "mail-key-ABCDEF",
                                                "SVC_TOKEN": SECRET_TOKEN, "SVC_ACCT": TEST_ACCOUNT})
        self.env.start()

    def tearDown(self):
        close_logging()
        self.env.stop()
        self.backend.__exit__(None, None, None)
        self.mail.__exit__(None, None, None)
        self.tmp.cleanup()

    def service(self, sid="acct-inquiry", path="/acct", **kw):
        defaults = dict(
            request={"url": self.backend.url(path), "method": "POST",
                     "headers": {"Accept": "application/json"},
                     "auth": {"type": "bearer", "token": "${env:SVC_TOKEN}"},
                     "body": {"type": "json", "content": {"accountNumber": "${env:SVC_ACCT}"}}},
            alerting={"failure_threshold": 2, "recovery_threshold": 2, "suppression_interval_seconds": 3600,
                      "reminder_interval_seconds": 0},
        )
        defaults.update(kw)
        return make_service(sid, self.backend.url(path), **defaults)

    def build(self, services, **overrides):
        raw = make_raw_config(self.tmp.name, services, email_url=self.mail.url("/mail"), **overrides)
        self.config_path = write_config(self.tmp.name, raw)
        self.config = load_config(self.config_path)
        setup_logging(self.config.logging, self.config.environment)
        self.runtime = monitor.Runtime(self.config, threading.Event())
        self.checker = self.runtime.checker_factory(self.config)
        return self.checker

    def run_check(self, sid="acct-inquiry"):
        return self.checker.run(self.config.service(sid))

    def mails(self):
        return [json.loads(r["body"]) for r in self.mail.requests_for("/mail")]

    def read_logs(self):
        text = ""
        for name in ("monitor.log", "events.log"):
            path = os.path.join(self.config.logging["directory"], name)
            if os.path.exists(path):
                with open(path) as handle:
                    text += handle.read()
        return text


class EndToEndTests(IntegrationBase):
    def test_outage_alert_suppression_and_recovery(self):
        self.backend.routes["/acct"] = json_reply(BIZ_ERROR)
        self.build([self.service()])
        r1 = self.run_check()
        self.assertEqual(r1.outcome, Outcome.FAILURE)
        self.assertEqual(r1.classification, FailureClass.RESPONSE_VALIDATION_FAILED)
        self.assertEqual(r1.http_status, 200)
        self.assertEqual(self.mails(), [])
        self.run_check()
        self.assertEqual(len(self.mails()), 1)
        for _ in range(3):
            self.run_check()
        self.assertEqual(len(self.mails()), 1, "duplicate alerts must be suppressed")
        alert = self.mails()[0]
        self.assertEqual(alert["meta"]["event"], "ALERT")
        self.assertIn("RESPONSE_VALIDATION_FAILED", alert["body"])
        self.assertIn('"FAILED"', alert["body"])
        self.assertIn("HTTP status", alert["body"])

        self.backend.routes["/acct"] = json_reply(OK_BODY)
        self.run_check()
        self.assertEqual(read_state_file(self.config.state_file)["services"]["acct-inquiry"]["state"], "RECOVERING")
        self.run_check()
        self.assertEqual(len(self.mails()), 2)
        self.assertEqual(self.mails()[1]["meta"]["event"], "RECOVERY")
        self.assertEqual(read_state_file(self.config.state_file)["services"]["acct-inquiry"]["state"], "HEALTHY")

        # the backend received the resolved secret, but logs and state never contain it
        req = self.backend.requests_for("/acct")[-1]
        self.assertEqual(req["headers"]["Authorization"], f"Bearer {SECRET_TOKEN}")
        logs = self.read_logs()
        self.assertNotIn(SECRET_TOKEN, logs)
        self.assertNotIn(TEST_ACCOUNT, logs)
        self.assertNotIn(BODY_MARKER, logs)
        with open(self.config.state_file) as handle:
            state_text = handle.read()
        for secret in (SECRET_TOKEN, TEST_ACCOUNT, BODY_MARKER):
            self.assertNotIn(secret, state_text)

    def test_structured_log_fields(self):
        self.backend.routes["/acct"] = json_reply(OK_BODY)
        self.build([self.service()])
        self.run_check()
        records = [json.loads(line) for line in self.read_logs().splitlines() if line.startswith("{")]
        done = [r for r in records if r["event"] == "CHECK_COMPLETED"][-1]
        for key in ("ts", "service_id", "execution_id", "environment", "http_status", "response_time_ms",
                    "validation_outcome", "failure_reason", "state", "notification_status"):
            self.assertIn(key, done)
        self.assertTrue(done["ts"].endswith("Z"))
        self.assertEqual(done["environment"], "UAT")
        self.assertEqual(done["validation_outcome"], "SUCCESS")
        self.assertEqual(done["state"], "HEALTHY")

    def test_timeout_and_invalid_responses_are_classified(self):
        self.backend.routes["/slow"] = json_reply(OK_BODY, delay=1.2)
        self.backend.routes["/bad"] = (200, {"Content-Type": "application/json"}, "{oops", 0)
        self.backend.routes["/shape"] = json_reply({"unexpected": True})
        self.backend.routes["/err"] = json_reply({"error": "x"}, status=500)
        services = [
            self.service("slow-svc", "/slow", timeout={"connect_seconds": 1, "read_seconds": 0.4, "total_seconds": 1}),
            self.service("bad-svc", "/bad"),
            self.service("shape-svc", "/shape"),
            self.service("err-svc", "/err"),
        ]
        self.build(services)
        self.assertEqual(self.run_check("slow-svc").classification, FailureClass.TIMEOUT)
        self.assertEqual(self.run_check("bad-svc").classification, FailureClass.INVALID_RESPONSE)
        self.assertEqual(self.run_check("shape-svc").classification, FailureClass.RESPONSE_VALIDATION_FAILED)
        err = self.run_check("err-svc")
        self.assertEqual(err.classification, FailureClass.HTTP_ERROR)
        self.assertEqual(err.http_status, 500)

    def test_slow_backend_degraded(self):
        self.backend.routes["/acct"] = json_reply(OK_BODY, delay=0.3)
        self.build([self.service(response_time={"warning_ms": 100, "critical_ms": 2000},
                                 alerting={"alert_on_slow_response": True, "slow_response_threshold": 2,
                                           "suppression_interval_seconds": 3600, "reminder_interval_seconds": 0})])
        r = self.run_check()
        self.assertEqual(r.outcome, Outcome.DEGRADED)
        self.assertEqual(r.classification, FailureClass.RESPONSE_TIME_EXCEEDED)
        self.run_check()
        self.assertEqual(len(self.mails()), 1)
        self.assertEqual(read_state_file(self.config.state_file)["services"]["acct-inquiry"]["state"], "DEGRADED")

    def test_retry_with_backoff_recovers_transient_error(self):
        self.backend.routes["/acct"] = [json_reply({}, status=503), json_reply(OK_BODY)]
        self.build([self.service(retry={"max_attempts": 3, "backoff_initial_seconds": 0.05, "jitter_seconds": 0,
                                        "retry_on": ["HTTP_5XX"], "max_total_seconds": 20})])
        result = self.run_check()
        self.assertEqual(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.attempts, 2)

    def test_no_retries_against_a_service_known_to_be_down(self):
        self.backend.routes["/acct"] = json_reply({}, status=503)
        self.build([self.service(retry={"max_attempts": 3, "backoff_initial_seconds": 0.01, "jitter_seconds": 0,
                                        "max_total_seconds": 20, "retry_when_down": False})])
        self.assertEqual(self.run_check().attempts, 3)
        self.run_check()  # -> DOWN
        before = len(self.backend.requests_for("/acct"))
        self.assertEqual(self.run_check().attempts, 1)
        self.assertEqual(len(self.backend.requests_for("/acct")) - before, 1)

    def test_email_api_unavailable_does_not_stop_monitoring(self):
        self.backend.routes["/acct"] = json_reply(BIZ_ERROR)
        self.mail.routes["/mail"] = json_reply({"error": "down"}, status=503)
        self.build([self.service()])
        for _ in range(10):
            result = self.run_check()
            self.assertEqual(result.outcome, Outcome.FAILURE)
        state = read_state_file(self.config.state_file)
        svc = state["services"]["acct-inquiry"]
        self.assertEqual(svc["state"], "DOWN")
        self.assertEqual(svc["consecutive_failures"], 10)
        self.assertTrue(svc["incident"]["alert_pending"])
        self.assertGreaterEqual(svc["alert_delivery_failures"], 1)
        # circuit breaker (3 failed sends x 2 attempts) bounds traffic to the e-mail API
        self.assertLessEqual(len(self.mail.requests_for("/mail")), 6)
        self.assertGreater(state["notifier"]["open_until_epoch"], 0)
        self.assertEqual(len(self.backend.requests_for("/acct")), 10)

    def test_multiple_services_failing_with_hourly_cap(self):
        self.backend.routes["/acct"] = json_reply(BIZ_ERROR)
        services = [self.service(f"svc-{i}", "/acct") for i in range(4)]
        self.build(services, notifications={**make_raw_config("x", [])["notifications"],
                                            "email_api": {**make_raw_config("x", [], email_url=self.mail.url("/mail"))
                                                          ["notifications"]["email_api"]},
                                            "max_emails_per_hour": 2})
        for _ in range(2):
            threads = [threading.Thread(target=self.run_check, args=(f"svc-{i}",)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(self.mails()), 2)
        state = read_state_file(self.config.state_file)["services"]
        self.assertTrue(all(s["state"] == "DOWN" for s in state.values()))
        pending = [sid for sid, s in state.items() if s["incident"]["alert_pending"]]
        self.assertEqual(len(pending), 2)


class GetPositionSampleTests(IntegrationBase):
    """The getPosition sample: GET /v1/customers/{id}/position?online=true, healthy = HTTP 200 + data."""

    def position_service(self, customer_id="1000649149"):
        return make_service(
            "get-position", "unused",
            variables={"customer_id": customer_id},
            request={"url": self.backend.url("/v1/customers/${var:customer_id}/position"), "method": "GET",
                     "headers": {"Accept": "application/json"}, "query_params": {"online": "true"}},
            validation={"expected_status": [200], "body_format": "json", "content_type": None,
                        "rules": {"all": [{"type": "json_exists", "path": "$.data"},
                                          {"type": "json_min_items", "path": "$.data", "min": 1}]}})

    def test_get_position(self):
        path = "/v1/customers/1000649149/position"
        self.build([self.position_service()])
        self.backend.routes[path] = json_reply({"data": [{"accountId": "x", "position": 10}]})
        ok = self.run_check("get-position")
        self.assertEqual(ok.outcome, Outcome.SUCCESS, ok.actual)
        req = self.backend.requests_for(path)[-1]
        self.assertEqual(req["method"], "GET")
        self.assertIn("online=true", req["raw_path"])
        for body in ({"data": []}, {"data": {}}, {"result": "no data"}):
            self.backend.routes[path] = json_reply(body)
            self.assertEqual(self.run_check("get-position").classification,
                             FailureClass.RESPONSE_VALIDATION_FAILED, body)
        self.backend.routes[path] = json_reply({"data": [1]}, status=500)
        self.assertEqual(self.run_check("get-position").classification, FailureClass.HTTP_ERROR)
        self.assertNotIn("1000649149", self.read_logs())

    def test_customer_id_change_needs_no_code_change(self):
        self.build([self.position_service(customer_id="2000000001")])
        self.backend.routes["/v1/customers/2000000001/position"] = json_reply({"data": [1]})
        self.assertEqual(self.run_check("get-position").outcome, Outcome.SUCCESS)

    def test_variable_value_can_come_from_environment(self):
        with mock.patch.dict(os.environ, {"POSITION_CUST": "3000000002"}):
            self.build([self.position_service(customer_id="${env:POSITION_CUST}")])
            self.backend.routes["/v1/customers/3000000002/position"] = json_reply({"data": [1]})
            self.assertEqual(self.run_check("get-position").outcome, Outcome.SUCCESS)


class CliTests(IntegrationBase):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = monitor.main(["-c", self.config_path, *args])
        return code, out.getvalue(), err.getvalue()

    def write(self, services):
        raw = make_raw_config(self.tmp.name, services, email_url=self.mail.url("/mail"))
        self.config_path = write_config(self.tmp.name, raw)
        self.config = load_config(self.config_path)

    def test_once_mode_persists_state_across_process_restarts(self):
        self.backend.routes["/acct"] = json_reply(BIZ_ERROR)
        self.write([self.service()])
        self.assertEqual(self.cli("once", "--all")[0], monitor.EXIT_CHECK_FAILED)
        self.assertEqual(self.mails(), [])
        self.assertEqual(self.cli("once", "--all")[0], monitor.EXIT_CHECK_FAILED)
        self.assertEqual(len(self.mails()), 1)          # threshold reached across two "processes"
        self.cli("once", "--all")
        self.assertEqual(len(self.mails()), 1)          # no duplicate after restart
        self.backend.routes["/acct"] = json_reply(OK_BODY)
        self.assertEqual(self.cli("once", "--all")[0], monitor.EXIT_OK)
        self.cli("once", "--all")
        self.assertEqual([m["meta"]["event"] for m in self.mails()], ["ALERT", "RECOVERY"])
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("HEALTHY", out)

    def test_once_mode_honours_service_intervals(self):
        self.backend.routes["/acct"] = json_reply(OK_BODY)
        self.write([self.service("hourly-svc", "/acct", interval={"value": 60, "unit": "minutes"},
                                 retry={"max_total_seconds": 30}),
                    self.service("quarter-svc", "/acct", interval={"value": 15, "unit": "minutes"},
                                 retry={"max_total_seconds": 30})])
        code, out, _ = self.cli("once")
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertEqual(len(out.strip().splitlines()), 2)      # first run: everything is due
        code, out, _ = self.cli("once")
        self.assertEqual(out.strip(), "")                        # nothing due yet
        code, out, _ = self.cli("once", "--service", "hourly-svc")
        self.assertEqual(json.loads(out)["service_id"], "hourly-svc")  # explicit selection always runs
        self.assertEqual(len(self.backend.requests_for("/acct")), 3)

    def test_on_demand_check_is_dry_run_by_default(self):
        self.backend.routes["/acct"] = json_reply(BIZ_ERROR)
        self.write([self.service()])
        code, out, _ = self.cli("check", "acct-inquiry")
        self.assertEqual(code, monitor.EXIT_CHECK_FAILED)
        self.assertEqual(json.loads(out)["classification"], "RESPONSE_VALIDATION_FAILED")
        self.assertFalse(os.path.exists(self.config.state_file))
        self.assertEqual(self.mails(), [])
        code, _, _ = self.cli("check", "acct-inquiry", "--apply")
        self.assertTrue(os.path.exists(self.config.state_file))

    def test_second_instance_is_refused(self):
        self.write([self.service()])
        lock = monitor.InstanceLock(self.config.runtime["lock_file"])
        self.assertTrue(lock.acquire())
        try:
            self.assertEqual(self.cli("once", "--all")[0], monitor.EXIT_LOCKED)
            self.assertEqual(self.cli("check", "acct-inquiry", "--apply")[0], monitor.EXIT_LOCKED)
            self.assertEqual(self.backend.requests_for("/acct"), [])
        finally:
            lock.release()

    def test_invalid_configuration_exit_code(self):
        self.config_path = os.path.join(self.tmp.name, "bad.json")
        with open(self.config_path, "w") as handle:
            json.dump({"config_version": 1}, handle)
        code, _, err = self.cli("validate")
        self.assertEqual(code, monitor.EXIT_CONFIG)
        self.assertIn("CONFIGURATION ERROR", err)

    def test_report_and_csv(self):
        self.backend.routes["/acct"] = [json_reply(OK_BODY), json_reply(OK_BODY), json_reply(BIZ_ERROR),
                                        json_reply(OK_BODY)]
        self.write([self.service()])
        for _ in range(4):
            self.cli("once", "--all")
        csv_path = os.path.join(self.tmp.name, "summary.csv")
        code, out, _ = self.cli("report", "--hours", "1", "--csv", csv_path)
        self.assertEqual(code, 0)
        report = json.loads(out)["services"]["acct-inquiry"]
        self.assertEqual(report["checks"], 4)
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["availability_pct"], 75.0)
        self.assertIsNotNone(report["p95_ms"])
        with open(csv_path) as handle:
            self.assertIn("acct-inquiry", handle.read())

    def test_show_response_prints_request_and_error_body(self):
        self.backend.routes["/acct"] = json_reply({"errorCode": "MISSING_HEADER", "message": "X-Channel-Id required"},
                                                  status=400)
        self.write([self.service()])
        code, out, _ = self.cli("check", "acct-inquiry", "--show-response")
        self.assertEqual(code, monitor.EXIT_CHECK_FAILED)
        self.assertIn("RESPONSE  HTTP 400", out)
        self.assertIn("X-Channel-Id required", out)
        self.assertIn("Authorization: ***", out)          # credentials masked
        self.assertNotIn(SECRET_TOKEN, out)
        self.assertFalse(os.path.exists(self.config.state_file))
        self.assertEqual(len(self.backend.requests_for("/acct")), 1)  # exactly one request

    def test_test_email_command(self):
        self.write([self.service()])
        code, out, _ = self.cli("test-email")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mails()[0]["meta"]["event"], "TEST")


if __name__ == "__main__":
    unittest.main()
