"""Independent per-service scheduler with global concurrency and rate limits.

* Each service has its own interval (from JSON) and next-due time (monotonic clock).
* A service is never dispatched while its previous check is still running.
* At most ``max_concurrent_checks`` checks run at once; the thread pool never
  queues work (dispatch only happens when a worker is free), so memory and
  threads stay bounded.
* ``max_requests_per_cycle`` caps dispatches per scheduler tick and per
  single-cycle run; ``max_requests_per_minute`` (token bucket) caps total HTTP
  requests including retries.
* Random jitter is applied at start-up and to every schedule to avoid bursts.
* After a restart, the next run is derived from the persisted
  ``last_check_at`` so restarts do not generate extra traffic.
* Configuration reloads (SIGHUP or file watch) are validated first; an invalid
  file is rejected and the running configuration is kept.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from checker import ServiceChecker
from config_loader import AppConfig, ConfigError, ServiceConfig, load_config
from logging_config import get_logger, log_event, set_level
from models import CheckResult, from_iso, utc_now

LOG = get_logger("scheduler")


@dataclass
class _Entry:
    service: ServiceConfig
    next_due: float
    running: bool = False


class MonitorScheduler:
    def __init__(self, config: AppConfig, checker_factory: Callable[[AppConfig], ServiceChecker],
                 stop_event: threading.Event, *, config_loader: Callable[[str], AppConfig] = load_config,
                 clock: Callable[[], float] = time.monotonic, rng: Optional[random.Random] = None,
                 on_config_applied: Optional[Callable[[AppConfig], None]] = None) -> None:
        self._factory = checker_factory
        self._loader = config_loader
        self._clock = clock
        self._rng = rng or random.Random()
        self._on_config_applied = on_config_applied
        self.stop_event = stop_event
        self._lock = threading.RLock()  # re-entrant: done-callbacks may run inside submit()
        self._entries: Dict[str, _Entry] = {}
        self._inflight = 0
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_size = 0
        self._reload_requested = threading.Event()
        self._file_sig: Tuple[int, int] = (0, 0)
        self.config: AppConfig
        self.checker: ServiceChecker
        self._apply(config, initial=True)

    # ------------------------------------------------------------------ configuration
    def _initial_due(self, service: ServiceConfig, now: float) -> float:
        jitter = self._rng.uniform(0, float(self.config.limits["startup_jitter_seconds"]))
        last = from_iso(self.checker.state.get(service.id).last_check_at)
        if last is not None:
            age = (utc_now() - last).total_seconds()
            if 0 <= age < service.interval_seconds:
                return now + (service.interval_seconds - age) + jitter
        return now + jitter

    def _apply(self, config: AppConfig, initial: bool = False) -> None:
        checker = self._factory(config)
        with self._lock:
            self.config = config
            self.checker = checker
            size = int(config.limits["max_concurrent_checks"])
            if self._executor is None or size != self._executor_size:
                old = self._executor
                self._executor = ThreadPoolExecutor(max_workers=size, thread_name_prefix="check")
                self._executor_size = size
                if old is not None:
                    old.shutdown(wait=False)  # running checks finish; nothing is queued
            now = self._clock()
            new_entries: Dict[str, _Entry] = {}
            for svc in config.services:
                existing = self._entries.get(svc.id)
                if existing is None:
                    new_entries[svc.id] = _Entry(svc, self._initial_due(svc, now))
                    continue
                if svc.interval_seconds != existing.service.interval_seconds:
                    existing.next_due = min(existing.next_due, now + svc.interval_seconds)
                existing.service = svc
                new_entries[svc.id] = existing
            removed = sorted(set(self._entries) - set(new_entries))
            self._entries = new_entries
        self._remember_file_signature(config.path)
        checker.state.prune(e.service.id for e in new_entries.values())
        checker.stats.forget(new_entries.keys())
        log_event(LOG, logging.INFO, "CONFIG_APPLIED" if not initial else "SCHEDULER_CONFIGURED",
                  services=len(config.services), enabled=sum(1 for s in config.services if s.enabled),
                  removed=removed or None, monitoring_enabled=config.monitoring_enabled,
                  config_hash=config.content_hash[:12])
        if self._on_config_applied is not None:
            self._on_config_applied(config)

    def _remember_file_signature(self, path: str) -> None:
        try:
            st = os.stat(path)
            self._file_sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._file_sig = (0, 0)

    def request_reload(self) -> None:
        """Signal-safe: only sets a flag."""
        self._reload_requested.set()

    def _config_changed_on_disk(self) -> bool:
        path = self.config.path
        try:
            st = os.stat(path)
        except OSError:
            return False
        if (st.st_mtime_ns, st.st_size) == self._file_sig:
            return False
        try:
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return False
        self._file_sig = (st.st_mtime_ns, st.st_size)
        return digest != self.config.content_hash

    def reload(self) -> bool:
        self._reload_requested.clear()
        try:
            new_config = self._loader(self.config.path)
        except ConfigError as exc:
            log_event(LOG, logging.ERROR, "CONFIG_RELOAD_REJECTED", failure_reason="CONFIGURATION_ERROR",
                      errors=exc.errors[:20], detail="keeping the previous configuration")
            return False
        except Exception as exc:
            log_event(LOG, logging.ERROR, "CONFIG_RELOAD_REJECTED", failure_reason="CONFIGURATION_ERROR",
                      detail=f"{exc.__class__.__name__}: {exc}"[:300])
            return False
        set_level(new_config.logging.get("level", "INFO"))
        self._apply(new_config)
        return True

    # ------------------------------------------------------------------ dispatch
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def _run_entry(self, checker: ServiceChecker, service: ServiceConfig) -> Optional[CheckResult]:
        try:
            return checker.run(service, apply_state=True, trigger="schedule")
        except Exception as exc:  # isolation: one failing task never stops the scheduler
            log_event(LOG, logging.ERROR, "CHECK_TASK_CRASHED", service_id=service.id,
                      detail=f"{exc.__class__.__name__}: {exc}"[:300])
            return None

    def _on_done(self, service_id: str, _future: "Future[Optional[CheckResult]]") -> None:
        with self._lock:
            self._inflight -= 1
            entry = self._entries.get(service_id)
            if entry is not None:
                entry.running = False

    def dispatch_due(self) -> int:
        """Dispatch due services. Returns the number of checks started."""
        if not self.config.monitoring_enabled:
            return 0
        now = self._clock()
        limits = self.config.limits
        started = 0
        with self._lock:
            due = sorted((e for e in self._entries.values()
                          if e.service.enabled and not e.running and e.next_due <= now),
                         key=lambda e: e.next_due)
            for entry in due:
                if started >= int(limits["max_requests_per_cycle"]):
                    break
                if self._inflight >= self._executor_size:
                    break
                if not self.checker.request_limiter.try_acquire():
                    log_event(LOG, logging.WARNING, "DISPATCH_RATE_LIMITED", service_id=entry.service.id,
                              detail="max_requests_per_minute reached; check deferred")
                    break
                interval = entry.service.interval_seconds
                next_due = entry.next_due + interval
                if next_due <= now:  # missed runs (e.g. host suspended): do not catch up in a burst
                    next_due = now + interval
                entry.next_due = next_due + self._rng.uniform(0, float(limits["schedule_jitter_seconds"]))
                entry.running = True
                self._inflight += 1
                assert self._executor is not None
                future = self._executor.submit(self._run_entry, self.checker, entry.service)
                future.add_done_callback(lambda f, sid=entry.service.id: self._on_done(sid, f))
                started += 1
        return started

    # ------------------------------------------------------------------ modes
    def run_forever(self) -> None:
        log_event(LOG, logging.INFO, "SCHEDULER_STARTED", services=len(self._entries),
                  max_concurrent_checks=self._executor_size)
        reload_cfg = self.config.reload
        next_watch = self._clock() + float(reload_cfg.get("watch_interval_seconds", 30))
        stats_every = float(self.config.reporting.get("stats_log_interval_seconds", 3600) or 0)
        next_stats = self._clock() + stats_every if stats_every else None
        disabled_logged = False
        while not self.stop_event.is_set():
            try:
                now = self._clock()
                if self._reload_requested.is_set():
                    self.reload()
                elif self.config.reload.get("enabled", True) and now >= next_watch:
                    next_watch = now + float(self.config.reload.get("watch_interval_seconds", 30))
                    if self._config_changed_on_disk():
                        log_event(LOG, logging.INFO, "CONFIG_CHANGE_DETECTED", config_path=self.config.path)
                        self.reload()
                if not self.config.monitoring_enabled:
                    if not disabled_logged:
                        log_event(LOG, logging.WARNING, "MONITORING_DISABLED", detail="monitoring_enabled=false")
                        disabled_logged = True
                else:
                    disabled_logged = False
                    self.dispatch_due()
                if next_stats is not None and now >= next_stats:
                    next_stats = now + stats_every
                    self.log_stats()
            except Exception as exc:  # the loop itself must survive anything
                log_event(LOG, logging.ERROR, "SCHEDULER_LOOP_ERROR", detail=f"{exc.__class__.__name__}: {exc}"[:300])
            self.stop_event.wait(float(self.config.limits["scheduler_tick_seconds"]))
        self.shutdown()

    def log_stats(self) -> None:
        summary = self.checker.stats.summary()
        for sid, row in summary.items():
            log_event(LOG, logging.INFO, "STATS_SUMMARY", service_id=sid, **{k: v for k, v in row.items()})
        csv_path = self.config.reporting.get("csv_summary_path")
        if csv_path:
            from reporting import write_csv
            try:
                write_csv(summary, csv_path)
            except OSError as exc:
                log_event(LOG, logging.ERROR, "CSV_EXPORT_FAILED", detail=str(exc)[:200])

    def shutdown(self) -> None:
        grace = float(self.config.limits.get("shutdown_grace_seconds", 30))
        log_event(LOG, logging.INFO, "SCHEDULER_STOPPING", inflight=self.inflight(), grace_seconds=grace)
        deadline = time.monotonic() + grace
        while self.inflight() > 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        executor = self._executor
        if executor is not None:
            executor.shutdown(wait=False)
        self.checker.state.save()
        log_event(LOG, logging.INFO, "SCHEDULER_STOPPED", abandoned_checks=self.inflight())

    def is_due(self, service: ServiceConfig, tolerance_seconds: float = 60.0) -> bool:
        """True when the persisted last check is older than the interval (minus a cron tolerance)."""
        last = from_iso(self.checker.state.get(service.id).last_check_at)
        if last is None:
            return True
        tolerance = min(tolerance_seconds, service.interval_seconds * 0.1)
        return (utc_now() - last).total_seconds() >= service.interval_seconds - tolerance

    def run_single_cycle(self, service_ids: Optional[Iterable[str]] = None, only_due: bool = True) -> List[CheckResult]:
        """Run enabled (or selected) services once - for cron / enterprise schedulers.

        With ``only_due`` (default) a service is skipped when its persisted last check is
        more recent than its configured interval, so a frequent cron entry still honours
        each service's own JSON interval. Explicitly selected services always run.
        """
        wanted = set(service_ids) if service_ids else None
        services = [s for s in self.config.services if (s.enabled if wanted is None else s.id in wanted)]
        if not self.config.monitoring_enabled and wanted is None:
            log_event(LOG, logging.WARNING, "MONITORING_DISABLED", detail="single cycle skipped")
            return []
        if only_due and wanted is None:
            not_due = [s.id for s in services if not self.is_due(s)]
            if not_due:
                log_event(LOG, logging.INFO, "CYCLE_SKIPPED_NOT_DUE", skipped=not_due)
            services = [s for s in services if s.id not in not_due]
        cap = int(self.config.limits["max_requests_per_cycle"])
        if len(services) > cap:
            log_event(LOG, logging.WARNING, "CYCLE_CAPPED", skipped=[s.id for s in services[cap:]],
                      max_requests_per_cycle=cap)
            services = services[:cap]
        results: List[CheckResult] = []
        futures = []
        with ThreadPoolExecutor(max_workers=self._executor_size, thread_name_prefix="cycle") as pool:
            for svc in services:
                if not self.checker.request_limiter.try_acquire():
                    log_event(LOG, logging.WARNING, "DISPATCH_RATE_LIMITED", service_id=svc.id)
                    continue
                futures.append(pool.submit(self.checker.run, svc, apply_state=True, trigger="cycle"))
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    log_event(LOG, logging.ERROR, "CHECK_TASK_CRASHED", detail=f"{exc.__class__.__name__}: {exc}"[:300])
        self.checker.state.save()
        return results

    def run_on_demand(self, service_id: str, apply_state: bool = False) -> CheckResult:
        service = self.config.service(service_id)
        if service is None:
            raise KeyError(service_id)
        return self.checker.run(service, apply_state=apply_state, trigger="manual")

    def entry_snapshot(self) -> Dict[str, Tuple[float, bool]]:
        with self._lock:
            return {sid: (e.next_due, e.running) for sid, e in self._entries.items()}
