"""E-mail middleware adapter tests (payload mapping, failures, circuit breaker, caps)."""
import json
import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs

from tests.helpers import MockServer, closed_port, json_reply, make_raw_config, make_service

from config_loader import parse_config
from email_client import EmailClient, render_payload
from http_client import SecureHttpClient
from models import NotificationStatus
from rate_limiter import SlidingWindowCounter
from state_manager import StateManager

CTX = {"environment": "UAT", "service_id": "svc", "service_name": "Svc", "endpoint": "http://h/x",
       "timestamp": "2026-01-01T08:00:00Z", "event_type": "ALERT", "check_result": "FAILURE", "state": "DOWN",
       "severity": "CRITICAL", "classification": "TIMEOUT", "http_status": None, "response_time_ms": 3000,
       "expected": "HTTP 200", "actual": "read timeout", "consecutive_failures": 2, "incident_id": "inc-1",
       "alert_id": "a1", "monitor_host": "test-host"}


class EmailClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = MockServer().__enter__()
        self.server.routes["/mail"] = json_reply({"status": "QUEUED"}, status=202)
        self.env = mock.patch.dict(os.environ, {"TEST_EMAIL_KEY": "mail-key-1234"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()

    def client(self, url=None, hourly=100, **api_overrides):
        raw = make_raw_config(self.tmp.name, [make_service("svc", "http://127.0.0.1:1/x")],
                              email_url=url or self.server.url("/mail"))
        raw["notifications"]["email_api"].update(api_overrides)
        config = parse_config(raw)
        self.state = StateManager(os.path.join(self.tmp.name, "state", "s.json"))
        http = SecureHttpClient(config.security, 65536)
        return EmailClient(config, http, self.state, SlidingWindowCounter(hourly, 3600), sleep=lambda s: None)

    def last_payload(self):
        return json.loads(self.server.requests_for("/mail")[-1]["body"])

    def test_json_payload_mapping(self):
        client = self.client(recipient_format="object_list", recipient_object_key="emailAddress",
                             recipient_object_extra={"type": "TO"}, severity_priority_map={"CRITICAL": "URGENT"})
        result = client.send(CTX, ["a@x.example", "b@x.example"], ["c@x.example"])
        self.assertEqual(result.status, NotificationStatus.SENT, result.detail)
        payload = self.last_payload()
        self.assertEqual(payload["to"], [{"type": "TO", "emailAddress": "a@x.example"},
                                         {"type": "TO", "emailAddress": "b@x.example"}])
        self.assertEqual(payload["priority"], "URGENT")
        self.assertEqual(payload["meta"], {"service": "svc", "event": "ALERT"})
        self.assertIn("[UAT][CRITICAL] ALERT: Svc is DOWN", payload["subject"])
        for text in ("UAT", "Svc (svc)", "TIMEOUT", "read timeout", "test-host", "inc-1", "3000"):
            self.assertIn(text, payload["body"])
        headers = self.server.requests_for("/mail")[-1]["headers"]
        self.assertEqual(headers["X-API-Key"], "mail-key-1234")

    def test_recipient_string_formats(self):
        self.assertEqual(render_payload({"to": "{{r}}", "s": "x {{r}}"}, {"r": ["a", "b"]}),
                         {"to": ["a", "b"], "s": "x a, b"})
        client = self.client(recipient_format="semicolon")
        client.send(CTX, ["a@x.example", "b@x.example"])
        self.assertEqual(self.last_payload()["to"], "a@x.example;b@x.example")

    def test_raw_xml_payload_is_escaped(self):
        template = ("<sendMail><to>{{recipients_csv}}</to><subject>{{subject}}</subject>"
                    "<body>{{body}}</body><key>${env:TEST_EMAIL_KEY}</key></sendMail>")
        client = self.client(payload_format="raw", payload_template=template, raw_escape="xml",
                             raw_content_type="application/xml")
        ctx = dict(CTX, actual="<script>&")
        self.assertEqual(client.send(ctx, ["a@x.example"]).status, NotificationStatus.SENT)
        body = self.server.requests_for("/mail")[-1]["body"].decode()
        self.assertIn("&lt;script&gt;&amp;", body)
        self.assertIn("<key>mail-key-1234</key>", body)
        self.assertEqual(self.server.requests_for("/mail")[-1]["headers"]["Content-Type"], "application/xml")

    def test_form_payload(self):
        client = self.client(payload_format="form", payload_template={"to": "{{recipients_csv}}", "subj": "{{subject}}"})
        client.send(CTX, ["a@x.example"])
        form = parse_qs(self.server.requests_for("/mail")[-1]["body"].decode())
        self.assertEqual(form["to"], ["a@x.example"])

    def test_secret_references_in_alert_content_are_not_resolved(self):
        client = self.client()
        client.send(dict(CTX, actual="${env:TEST_EMAIL_KEY}"), ["a@x.example"])
        payload = self.last_payload()
        self.assertNotIn("mail-key-1234", json.dumps(payload))
        self.assertIn("${env:TEST_EMAIL_KEY}", payload["body"])

    def test_server_error_retried_then_failed(self):
        self.server.routes["/mail"] = json_reply({"error": "down"}, status=503)
        result = self.client().send(CTX, ["a@x.example"])
        self.assertEqual(result.status, NotificationStatus.FAILED)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(self.server.requests_for("/mail")), 2)

    def test_client_error_not_retried(self):
        self.server.routes["/mail"] = json_reply({"error": "bad"}, status=400)
        result = self.client().send(CTX, ["a@x.example"])
        self.assertEqual(result.status, NotificationStatus.FAILED)
        self.assertEqual(len(self.server.requests_for("/mail")), 1)

    def test_success_rules_reject_business_failure(self):
        self.server.routes["/mail"] = json_reply({"status": "REJECTED"}, status=200)
        client = self.client(success_body_format="json",
                             success_rules={"type": "json_in", "path": "$.status", "values": ["QUEUED"]})
        self.assertEqual(client.send(CTX, ["a@x.example"]).status, NotificationStatus.FAILED)

    def test_circuit_breaker_opens_and_stops_calls(self):
        client = self.client(url=f"http://127.0.0.1:{closed_port()}/mail", max_attempts=1)
        statuses = [client.send(CTX, ["a@x.example"]).status for _ in range(6)]
        self.assertEqual(statuses[:3], [NotificationStatus.FAILED] * 3)
        self.assertEqual(statuses[3:], [NotificationStatus.SKIPPED_CIRCUIT_OPEN] * 3)
        self.assertGreater(self.state.get_notifier().open_until_epoch, 0)
        self.assertEqual(self.state.get_notifier().total_delivery_failures, 3)

    def test_circuit_closes_after_success(self):
        client = self.client()
        notifier = self.state.get_notifier()
        notifier.consecutive_failures = 5
        notifier.open_until_epoch = 1.0  # expired
        self.state.put_notifier(notifier)
        self.assertEqual(client.send(CTX, ["a@x.example"]).status, NotificationStatus.SENT)
        self.assertEqual(self.state.get_notifier().consecutive_failures, 0)

    def test_hourly_cap(self):
        client = self.client(hourly=2)
        statuses = [client.send(CTX, ["a@x.example"]).status for _ in range(4)]
        self.assertEqual(statuses, [NotificationStatus.SENT, NotificationStatus.SENT,
                                    NotificationStatus.SKIPPED_RATE_LIMIT, NotificationStatus.SKIPPED_RATE_LIMIT])

    def test_missing_secret_fails_cleanly(self):
        os.environ.pop("TEST_EMAIL_KEY")
        result = self.client().send(CTX, ["a@x.example"])
        self.assertEqual(result.status, NotificationStatus.FAILED)
        self.assertEqual(self.server.requests_for("/mail"), [])


if __name__ == "__main__":
    unittest.main()
