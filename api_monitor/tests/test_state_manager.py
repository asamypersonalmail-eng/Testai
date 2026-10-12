"""Persistent state tests: atomic writes, restart, corruption recovery, no sensitive data."""
import json
import os
import stat
import tempfile
import threading
import unittest

from tests.helpers import ROOT  # noqa: F401

from state_manager import Incident, ServiceState, StateManager, read_state_file


class StateManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "nested", "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_across_restart(self):
        sm = StateManager(self.path)
        sm.load()
        st = ServiceState("svc", state="DOWN", consecutive_failures=3, last_response_time_ms=1234.5,
                          incident=Incident("inc-1", "DOWN", "2026-01-01T00:00:00Z", "2026-01-01T00:02:00Z",
                                            alerts_sent=1, alert_pending=True, pending_event_type="REMINDER"))
        sm.put(st)
        restarted = StateManager(self.path)
        restarted.load()
        loaded = restarted.get("svc")
        self.assertEqual(loaded.state, "DOWN")
        self.assertEqual(loaded.consecutive_failures, 3)
        self.assertIsInstance(loaded.incident, Incident)
        self.assertTrue(loaded.incident.alert_pending)
        self.assertEqual(restarted.get("unknown").state, "UNKNOWN")

    def test_atomic_write_leaves_no_temp_files_and_restricts_permissions(self):
        sm = StateManager(self.path)
        for i in range(5):
            sm.put(ServiceState("svc", consecutive_successes=i))
        files = os.listdir(os.path.dirname(self.path))
        self.assertEqual(sorted(files), ["state.json", "state.json.lock"])
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode & 0o007, 0)

    def test_corrupt_file_is_quarantined(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            handle.write("{ truncated")
        sm = StateManager(self.path)
        sm.load()
        self.assertEqual(sm.all(), {})
        quarantined = [f for f in os.listdir(os.path.dirname(self.path)) if ".corrupt-" in f]
        self.assertEqual(len(quarantined), 1)
        sm.put(ServiceState("svc"))
        self.assertEqual(read_state_file(self.path)["services"]["svc"]["state"], "UNKNOWN")

    def test_only_operational_fields_are_stored(self):
        sm = StateManager(self.path)
        sm.put(ServiceState("svc", last_classification="TIMEOUT", last_http_status=503))
        with open(self.path) as handle:
            data = json.load(handle)
        keys = set(data["services"]["svc"].keys())
        for forbidden in ("body", "response", "response_body", "headers", "payload", "request", "actual"):
            self.assertNotIn(forbidden, keys)

    def test_concurrent_updates_keep_file_valid(self):
        sm = StateManager(self.path)

        def worker(n):
            for i in range(20):
                sm.put(ServiceState(f"svc-{n}", consecutive_successes=i))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = read_state_file(self.path)
        self.assertEqual(len(data["services"]), 5)
        self.assertTrue(all(s["consecutive_successes"] == 19 for s in data["services"].values()))

    def test_prune_removes_unconfigured_services(self):
        sm = StateManager(self.path)
        sm.put(ServiceState("keep"))
        sm.put(ServiceState("gone"))
        sm.prune(["keep"])
        self.assertEqual(list(read_state_file(self.path)["services"]), ["keep"])

    def test_read_only_mode_never_writes(self):
        sm = StateManager(self.path, persist=False)
        sm.put(ServiceState("svc"))
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
