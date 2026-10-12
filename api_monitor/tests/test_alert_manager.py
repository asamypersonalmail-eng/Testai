"""Incident detection, suppression, reminders and recovery tests (fake clock, fake e-mail)."""
import os
import tempfile
import unittest
import uuid

from tests.helpers import FakeClock, make_raw_config, make_service

from alert_manager import AlertManager
from config_loader import parse_config
from email_client import DeliveryResult
from models import CheckResult, FailureClass, NotificationStatus, Outcome
from state_manager import StateManager


class FakeEmail:
    def __init__(self):
        self.sent = []
        self.next_status = NotificationStatus.SENT

    def send(self, ctx, recipients, cc=()):
        self.sent.append({"ctx": ctx, "recipients": list(recipients), "status": self.next_status})
        return DeliveryResult(self.next_status, "fake", 202 if self.next_status == NotificationStatus.SENT else None, 1)

    def events(self, only_sent=True):
        return [m["ctx"]["event_type"] for m in self.sent if not only_sent or m["status"] == NotificationStatus.SENT]


class AlertManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.email = FakeEmail()
        self.build()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **alerting):
        policy = {"failure_threshold": 2, "recovery_threshold": 2, "suppression_interval_seconds": 600,
                  "reminder_interval_seconds": 3600, "alert_on_slow_response": True, "slow_response_threshold": 2}
        policy.update(alerting)
        raw = make_raw_config(self.tmp.name, [make_service("svc", "http://127.0.0.1:1/x", alerting=policy)])
        self.config = parse_config(raw)
        self.service = self.config.services[0]
        self.state = StateManager(os.path.join(self.tmp.name, "state", "state.json"))
        self.state.load()
        self.am = AlertManager(self.config, self.state, self.email, clock=self.clock)

    def result(self, outcome, cls=None, status=200, rt=120.0):
        return CheckResult(service_id="svc", execution_id=uuid.uuid4().hex[:8], started_at=self.clock(),
                           outcome=outcome, classification=cls, http_status=status, response_time_ms=rt,
                           expected="$.status == SUCCESS", actual='$.status="FAILED"')

    def fail(self, cls=FailureClass.RESPONSE_VALIDATION_FAILED, advance=60):
        self.clock.advance(advance)
        return self.am.process(self.service, self.result(Outcome.FAILURE, cls))

    def ok(self, advance=60):
        self.clock.advance(advance)
        return self.am.process(self.service, self.result(Outcome.SUCCESS))

    def slow(self, advance=60):
        self.clock.advance(advance)
        return self.am.process(self.service, self.result(Outcome.DEGRADED, FailureClass.RESPONSE_TIME_EXCEEDED,
                                                         rt=5000))

    # ------------------------------------------------------------------
    def test_alert_only_after_threshold(self):
        first = self.fail()
        self.assertEqual(first.state, "DEGRADED")
        self.assertEqual(self.email.sent, [])
        second = self.fail()
        self.assertEqual(second.state, "DOWN")
        self.assertEqual(second.notification_status, NotificationStatus.SENT)
        self.assertEqual(self.email.events(), ["ALERT"])
        ctx = self.email.sent[0]["ctx"]
        for key in ("environment", "service_name", "endpoint", "timestamp", "check_result", "classification",
                    "http_status", "response_time_ms", "expected", "actual", "consecutive_failures",
                    "incident_detected_at", "incident_started_at", "monitor_host"):
            self.assertIn(key, ctx)
        self.assertEqual(ctx["consecutive_failures"], 2)
        self.assertEqual(ctx["classification"], "RESPONSE_VALIDATION_FAILED")
        self.assertEqual(ctx["monitor_host"], "test-host")

    def test_duplicate_alerts_suppressed_and_reminder_sent(self):
        self.fail(); self.fail()
        for _ in range(10):  # 10 more minutes of failures
            self.fail()
        self.assertEqual(self.email.events(), ["ALERT"])
        self.clock.advance(3600)
        out = self.fail()
        self.assertEqual(out.event_type, "REMINDER")
        self.assertEqual(self.email.events(), ["ALERT", "REMINDER"])
        self.assertEqual(self.email.sent[-1]["ctx"]["alert_count"], 2)

    def test_recovery_after_threshold(self):
        self.fail(); self.fail()
        first_ok = self.ok()
        self.assertEqual(first_ok.state, "RECOVERING")
        again = self.fail()
        self.assertEqual(again.state, "DOWN")  # same incident, no new alert
        self.assertEqual(self.email.events(), ["ALERT"])
        self.ok()
        rec = self.ok()
        self.assertEqual(rec.state, "HEALTHY")
        self.assertEqual(rec.event_type, "RECOVERY")
        self.assertEqual(self.email.events(), ["ALERT", "RECOVERY"])
        self.assertIsNone(self.state.get("svc").incident)
        self.assertEqual(self.email.sent[-1]["ctx"]["severity"], "INFO")

    def test_email_failure_is_retried_on_next_check_only(self):
        self.email.next_status = NotificationStatus.FAILED
        self.fail(); out = self.fail()
        self.assertEqual(out.notification_status, NotificationStatus.FAILED)
        st = self.state.get("svc")
        self.assertTrue(st.incident.alert_pending)
        self.assertEqual(st.alert_delivery_failures, 1)
        self.assertEqual(st.state, "DOWN")  # health tracking unaffected
        self.email.next_status = NotificationStatus.SENT
        out = self.fail()
        self.assertEqual(out.notification_status, NotificationStatus.SENT)
        self.assertEqual(self.email.events(), ["ALERT"])
        self.assertFalse(self.state.get("svc").incident.alert_pending)
        self.assertEqual(len(self.email.sent), 2)  # exactly one attempt per check

    def test_circuit_open_leaves_alert_pending(self):
        self.email.next_status = NotificationStatus.SKIPPED_CIRCUIT_OPEN
        self.fail(); self.fail()
        st = self.state.get("svc")
        self.assertTrue(st.incident.alert_pending)
        self.assertEqual(st.alert_delivery_failures, 0)  # not a delivery failure, a skip

    def test_no_recovery_mail_if_alert_never_delivered(self):
        self.email.next_status = NotificationStatus.FAILED
        self.fail(); self.fail()
        self.email.next_status = NotificationStatus.SENT
        self.email.sent.clear()
        # recovers before the pending alert could be delivered
        self.am.process(self.service, self.result(Outcome.SUCCESS))
        self.ok()
        self.assertEqual(self.email.sent, [])
        self.assertEqual(self.state.get("svc").state, "HEALTHY")

    def test_recovery_delivery_failure_retried_bounded(self):
        self.fail(); self.fail()
        self.email.next_status = NotificationStatus.FAILED
        self.ok(); self.ok()
        self.assertIsNotNone(self.state.get("svc").pending_recovery)
        self.email.next_status = NotificationStatus.SENT
        self.ok()
        self.assertEqual(self.email.events(), ["ALERT", "RECOVERY"])
        self.assertIsNone(self.state.get("svc").pending_recovery)

    def test_slow_responses_raise_degraded_incident(self):
        self.slow()
        self.assertEqual(self.email.sent, [])
        out = self.slow()
        self.assertEqual(out.state, "DEGRADED")
        self.assertEqual(out.event_type, "ALERT")
        self.assertEqual(self.state.get("svc").incident.kind, "DEGRADED")
        # escalates to DOWN when failures pass the threshold
        self.fail()
        out = self.fail()
        self.assertEqual(out.event_type, "ESCALATION")
        self.assertEqual(out.state, "DOWN")

    def test_slow_responses_ignored_when_disabled(self):
        self.build(alert_on_slow_response=False)
        for _ in range(5):
            out = self.slow()
        self.assertEqual(out.state, "DEGRADED")
        self.assertEqual(self.email.sent, [])

    def test_new_incident_inside_suppression_window_is_deferred(self):
        self.build(failure_threshold=1, recovery_threshold=1, suppression_interval_seconds=600,
                   reminder_interval_seconds=0)
        self.fail()                      # ALERT
        self.ok()                        # RECOVERY (bypasses suppression)
        out = self.fail()                # new incident within 600 s -> suppressed, pending
        self.assertEqual(out.notification_status, NotificationStatus.SUPPRESSED)
        self.assertTrue(self.state.get("svc").incident.alert_pending)
        out = self.fail(advance=600)     # window passed -> delivered
        self.assertEqual(out.notification_status, NotificationStatus.SENT)
        self.assertEqual(self.email.events(), ["ALERT", "RECOVERY", "ALERT"])

    def test_notifications_disabled_still_tracks_state(self):
        self.build(enabled=False)
        self.fail(); out = self.fail()
        self.assertEqual(out.state, "DOWN")
        self.assertEqual(out.notification_status, NotificationStatus.DISABLED)
        self.assertEqual(self.email.sent, [])

    def test_state_survives_restart_without_duplicate_alert(self):
        self.fail(); self.fail()
        self.assertEqual(self.email.events(), ["ALERT"])
        # simulate a process restart: new managers reading the same state file
        self.state = StateManager(self.state.path)
        self.state.load()
        self.am = AlertManager(self.config, self.state, self.email, clock=self.clock)
        restored = self.state.get("svc")
        self.assertEqual(restored.state, "DOWN")
        self.assertEqual(restored.consecutive_failures, 2)
        self.fail()
        self.assertEqual(self.email.events(), ["ALERT"])
        self.ok(); self.ok()
        self.assertEqual(self.email.events(), ["ALERT", "RECOVERY"])

    def test_multiple_services_fail_independently(self):
        raw = make_raw_config(self.tmp.name, [make_service(f"svc-{i}", "http://127.0.0.1:1/x") for i in range(3)])
        config = parse_config(raw)
        am = AlertManager(config, self.state, self.email, clock=self.clock)
        for _ in range(2):
            self.clock.advance(60)
            for svc in config.services:
                res = self.result(Outcome.FAILURE, FailureClass.TIMEOUT, status=None)
                res.service_id = svc.id
                am.process(svc, res)
        self.assertEqual(sorted(m["ctx"]["service_id"] for m in self.email.sent), ["svc-0", "svc-1", "svc-2"])


if __name__ == "__main__":
    unittest.main()
