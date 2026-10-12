"""Scheduler tests: intervals, overlap prevention, concurrency, rate limits, isolation, reload, restart."""
import datetime as _dt
import tempfile
import threading
import time
import unittest

from tests.helpers import FakeMonotonic, make_raw_config, make_service, wait_until, write_config

from config_loader import load_config
from models import CheckResult, Outcome, to_iso, utc_now
from rate_limiter import TokenBucket
from reporting import StatsCollector
from scheduler import MonitorScheduler
from state_manager import ServiceState, StateManager


class FakeChecker:
    """Stands in for ServiceChecker; optionally blocks until released."""

    def __init__(self, state, rate=600.0, block=False, fail_ids=()):
        self.state = state
        self.stats = StatsCollector()
        self.request_limiter = TokenBucket(rate)
        self.calls = []
        self.block = block
        self.release = threading.Event()
        self.fail_ids = set(fail_ids)
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def run(self, service, apply_state=True, trigger="schedule"):
        with self._lock:
            self.calls.append(service.id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.block:
                self.release.wait(5)
            if service.id in self.fail_ids:
                raise RuntimeError("boom")
            return CheckResult(service.id, "x", utc_now(), Outcome.SUCCESS)
        finally:
            with self._lock:
                self.active -= 1


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeMonotonic()
        self.stop = threading.Event()

    def tearDown(self):
        self.stop.set()
        self.tmp.cleanup()

    def make(self, services, checker_kwargs=None, **raw_overrides):
        raw = make_raw_config(self.tmp.name, services, **raw_overrides)
        self.path = write_config(self.tmp.name, raw)
        config = load_config(self.path)
        self.state = StateManager(config.state_file)
        self.checker = FakeChecker(self.state, **(checker_kwargs or {}))
        return MonitorScheduler(config, lambda cfg: self.checker, self.stop, clock=self.clock)

    def svc(self, sid, seconds):
        return make_service(sid, "http://127.0.0.1:1/x", interval={"value": seconds, "unit": "seconds"},
                            retry={"max_total_seconds": min(20, seconds - 1)})

    def drain(self, sched):
        self.assertTrue(wait_until(lambda: sched.inflight() == 0))

    def test_independent_intervals(self):
        sched = self.make([self.svc("fast", 15), self.svc("slow", 60)])
        for _ in range(121):  # 120 simulated seconds, 1 s ticks
            sched.dispatch_due()
            self.drain(sched)
            self.clock.advance(1)
        self.assertEqual(self.checker.calls.count("fast"), 9)   # t=0,15,...,120
        self.assertEqual(self.checker.calls.count("slow"), 3)   # t=0,60,120

    def test_no_overlapping_execution_of_same_service(self):
        sched = self.make([self.svc("svc", 10)], {"block": True})
        self.assertEqual(sched.dispatch_due(), 1)
        self.clock.advance(30)  # overdue while still running
        self.assertEqual(sched.dispatch_due(), 0)
        self.assertEqual(self.checker.calls, ["svc"])
        self.checker.release.set()
        self.drain(sched)
        self.assertEqual(sched.dispatch_due(), 1)

    def test_global_concurrency_limit(self):
        services = [self.svc(f"s{i}", 30) for i in range(6)]
        sched = self.make(services, {"block": True}, limits={**make_raw_config("x", [])["limits"],
                                                             "max_concurrent_checks": 2})
        self.assertEqual(sched.dispatch_due(), 2)
        self.assertEqual(sched.dispatch_due(), 0)
        self.checker.release.set()
        for _ in range(10):
            self.drain(sched)
            sched.dispatch_due()
        self.drain(sched)
        self.assertEqual(sorted(self.checker.calls), [f"s{i}" for i in range(6)])
        self.assertLessEqual(self.checker.max_active, 2)

    def test_max_requests_per_cycle(self):
        limits = {**make_raw_config("x", [])["limits"], "max_requests_per_cycle": 3}
        sched = self.make([self.svc(f"s{i}", 30) for i in range(5)], limits=limits)
        self.assertEqual(sched.dispatch_due(), 3)
        self.drain(sched)
        self.assertEqual(sched.dispatch_due(), 2)

    def test_rate_limit_defers_checks(self):
        sched = self.make([self.svc(f"s{i}", 30) for i in range(5)], {"rate": 2})
        self.assertEqual(sched.dispatch_due(), 2)
        self.drain(sched)
        self.assertEqual(sched.dispatch_due(), 0)  # bucket empty, deferred not dropped
        due = [sid for sid, (nd, _r) in sched.entry_snapshot().items() if nd <= self.clock()]
        self.assertEqual(len(due), 3)

    def test_failure_in_one_task_does_not_stop_others(self):
        sched = self.make([self.svc("bad", 15), self.svc("good", 15)], {"fail_ids": ["bad"]})
        for _ in range(3):
            sched.dispatch_due()
            self.drain(sched)
            self.clock.advance(15)
        self.assertEqual(self.checker.calls.count("good"), 3)
        self.assertEqual(self.checker.calls.count("bad"), 3)

    def test_disabled_services_and_monitoring_switch(self):
        disabled = self.svc("off", 15)
        disabled["enabled"] = False
        sched = self.make([disabled, self.svc("on", 15)])
        sched.dispatch_due()
        self.drain(sched)
        self.assertEqual(self.checker.calls, ["on"])
        sched2 = self.make([self.svc("on", 15)], monitoring_enabled=False)
        self.assertEqual(sched2.dispatch_due(), 0)

    def test_reload_applies_valid_and_rejects_invalid_config(self):
        sched = self.make([self.svc("a", 30)])
        raw = make_raw_config(self.tmp.name, [self.svc("a", 60), self.svc("b", 30)])
        write_config(self.tmp.name, raw)
        self.assertTrue(sched.reload())
        self.assertEqual({s.id for s in sched.config.services}, {"a", "b"})
        self.assertEqual(sched.config.service("a").interval_seconds, 60)
        with open(self.path, "w") as handle:
            handle.write("{broken")
        self.assertFalse(sched.reload())
        self.assertEqual({s.id for s in sched.config.services}, {"a", "b"})  # previous config kept

    def test_file_watch_detects_change(self):
        sched = self.make([self.svc("a", 30)])
        self.assertFalse(sched._config_changed_on_disk())
        time.sleep(0.01)
        write_config(self.tmp.name, make_raw_config(self.tmp.name, [self.svc("a", 45)]))
        self.assertTrue(sched._config_changed_on_disk())

    def test_restart_respects_previous_last_check(self):
        raw = make_raw_config(self.tmp.name, [self.svc("a", 60)])
        path = write_config(self.tmp.name, raw)
        config = load_config(path)
        state = StateManager(config.state_file)
        state.put(ServiceState("a", last_check_at=to_iso(utc_now() - _dt.timedelta(seconds=20))))
        self.checker = FakeChecker(state)
        sched = MonitorScheduler(config, lambda cfg: self.checker, self.stop, clock=self.clock)
        self.assertEqual(sched.dispatch_due(), 0)  # checked 20 s ago -> next run in ~40 s
        self.clock.advance(41)
        self.assertEqual(sched.dispatch_due(), 1)

    def test_single_cycle_mode(self):
        sched = self.make([self.svc("a", 30), self.svc("b", 30)])
        results = sched.run_single_cycle()
        self.assertEqual(sorted(r.service_id for r in results), ["a", "b"])
        only_b = sched.run_single_cycle(["b"])
        self.assertEqual([r.service_id for r in only_b], ["b"])

    def test_run_forever_stops_gracefully(self):
        self.clock = time.monotonic  # real clock from the start (fake clock values are unrelated to uptime)
        sched = self.make([self.svc("a", 30)])
        thread = threading.Thread(target=sched.run_forever)
        thread.start()
        self.assertTrue(wait_until(lambda: "a" in self.checker.calls))
        self.stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
