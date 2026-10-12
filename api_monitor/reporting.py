"""Execution statistics, latency percentiles, failure trends and CSV export.

* :class:`StatsCollector` keeps a bounded in-memory history per service for the
  running process (periodically logged as ``STATS_SUMMARY``).
* :func:`build_report` reconstructs statistics for any time window from the
  JSON log files (including rotated ones), so reporting needs no database.
"""
from __future__ import annotations

import collections
import csv
import datetime as _dt
import glob
import json
import math
import os
import threading
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from logging_config import EVENTS_LOG_FILE, MAIN_LOG_FILE
from models import CheckResult, from_iso

CSV_FIELDS = ["service_id", "checks", "success", "degraded", "failures", "availability_pct",
              "p50_ms", "p95_ms", "p99_ms", "max_ms", "top_failure_reason", "alerts", "recoveries"]


def percentile(values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return round(ordered[rank - 1], 1)


class _Agg:
    def __init__(self) -> None:
        self.checks = 0
        self.outcomes: Dict[str, int] = collections.Counter()
        self.reasons: Dict[str, int] = collections.Counter()
        self.latencies: List[float] = []
        self.alerts = 0
        self.recoveries = 0
        self.daily_failures: Dict[str, int] = collections.Counter()

    def add(self, outcome: str, reason: Optional[str], latency: Optional[float], day: str) -> None:
        self.checks += 1
        self.outcomes[outcome] += 1
        if reason and outcome != "SUCCESS":
            self.reasons[reason] += 1
        if outcome == "FAILURE":
            self.daily_failures[day] += 1
        if latency is not None:
            self.latencies.append(float(latency))

    def summary(self) -> Dict[str, Any]:
        ok = self.outcomes.get("SUCCESS", 0)
        degraded = self.outcomes.get("DEGRADED", 0)
        failures = self.outcomes.get("FAILURE", 0)
        top = max(self.reasons.items(), key=lambda kv: kv[1])[0] if self.reasons else ""
        return {
            "checks": self.checks,
            "success": ok,
            "degraded": degraded,
            "failures": failures,
            "availability_pct": round(100.0 * (ok + degraded) / self.checks, 2) if self.checks else None,
            "p50_ms": percentile(self.latencies, 50),
            "p95_ms": percentile(self.latencies, 95),
            "p99_ms": percentile(self.latencies, 99),
            "max_ms": round(max(self.latencies), 1) if self.latencies else None,
            "failure_reasons": dict(self.reasons),
            "top_failure_reason": top,
            "failure_trend_by_day": dict(sorted(self.daily_failures.items())),
            "alerts": self.alerts,
            "recoveries": self.recoveries,
        }


class StatsCollector:
    """Bounded in-memory statistics for the running process."""

    def __init__(self, history_size: int = 500) -> None:
        self.history_size = history_size
        self._lock = threading.Lock()
        self._history: Dict[str, Deque[Tuple[str, str, Optional[str], Optional[float]]]] = {}

    def record(self, result: CheckResult) -> None:
        with self._lock:
            history = self._history.setdefault(result.service_id, collections.deque(maxlen=self.history_size))
            history.append((result.started_at.strftime("%Y-%m-%d"), result.outcome.value,
                            result.classification.value if result.classification else None,
                            result.response_time_ms))

    def forget(self, keep_ids: Iterable[str]) -> None:
        keep = set(keep_ids)
        with self._lock:
            for sid in [s for s in self._history if s not in keep]:
                del self._history[sid]

    def summary(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            snapshot = {sid: list(h) for sid, h in self._history.items()}
        result = {}
        for sid, rows in snapshot.items():
            agg = _Agg()
            for day, outcome, reason, latency in rows:
                agg.add(outcome, reason, latency, day)
            result[sid] = agg.summary()
        return result


def _iter_log_lines(directory: str, base_name: str) -> Iterable[Dict[str, Any]]:
    paths = sorted(glob.glob(os.path.join(directory, base_name + ".*"))) + [os.path.join(directory, base_name)]
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def build_report(log_directory: str, since: _dt.datetime, service_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Aggregate CHECK_COMPLETED / alert events from log files newer than ``since``."""
    aggs: Dict[str, _Agg] = collections.defaultdict(_Agg)
    history: List[Dict[str, Any]] = []
    for record in _iter_log_lines(log_directory, MAIN_LOG_FILE):
        if record.get("event") != "CHECK_COMPLETED":
            continue
        ts = from_iso(record.get("ts"))
        sid = record.get("service_id")
        if ts is None or ts < since or not sid or (service_ids and sid not in service_ids):
            continue
        aggs[sid].add(record.get("validation_outcome", "FAILURE"), record.get("failure_reason"),
                      record.get("response_time_ms"), ts.strftime("%Y-%m-%d"))
    for record in _iter_log_lines(log_directory, EVENTS_LOG_FILE):
        event = record.get("event", "")
        ts = from_iso(record.get("ts"))
        sid = record.get("service_id")
        if ts is None or ts < since or not sid or (service_ids and sid not in service_ids):
            continue
        if event in ("ALERT_SENT", "REMINDER_SENT", "ESCALATION_SENT"):
            aggs[sid].alerts += 1
        elif event == "RECOVERY_SENT":
            aggs[sid].recoveries += 1
        if event in ("INCIDENT_OPENED", "INCIDENT_RESOLVED", "INCIDENT_ESCALATED", "ALERT_SENT", "REMINDER_SENT",
                     "ESCALATION_SENT", "RECOVERY_SENT", "ALERT_DELIVERY_FAILED", "ALERT_SUPPRESSED",
                     "EMAIL_CIRCUIT_OPENED", "EMAIL_CIRCUIT_CLOSED"):
            history.append({k: record.get(k) for k in ("ts", "event", "service_id", "incident_id",
                                                       "failure_reason", "notification_status", "duration")
                            if record.get(k) is not None})
    return {
        "since": since.isoformat(),
        "services": {sid: agg.summary() for sid, agg in sorted(aggs.items())},
        "event_history": history[-500:],
    }


def write_csv(summary: Dict[str, Dict[str, Any]], path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for sid, row in sorted(summary.items()):
            writer.writerow(dict(row, service_id=sid))
    os.replace(tmp, path)
