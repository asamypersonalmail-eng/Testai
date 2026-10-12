"""Persistent service-health state.

Only the minimum operational state is persisted (counters, timestamps,
classifications, incident metadata). Response bodies, headers, payloads and
credentials are never written.

Writes are atomic (temp file + fsync + ``os.replace``) and serialised with an
in-process lock plus an advisory ``fcntl`` lock on a side-car lock file, so a
concurrent ``status`` reader or a second process never sees a torn file.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Iterable, Iterator, Optional

from logging_config import get_logger, log_event

try:  # POSIX (Linux, AIX)
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms are not supported for production
    fcntl = None  # type: ignore[assignment]

STATE_VERSION = 1
LOG = get_logger("state")


@contextmanager
def file_lock(lock_path: str, exclusive: bool) -> Iterator[None]:
    """Advisory inter-process lock on a side-car file (no-op where fcntl is unavailable)."""
    if fcntl is None:
        yield
        return
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass
class Incident:
    incident_id: str
    kind: str                          # DOWN | DEGRADED
    started_at: str                    # first failing check of the streak
    detected_at: str                   # threshold crossed
    first_classification: Optional[str] = None
    last_classification: Optional[str] = None
    alerts_sent: int = 0
    last_alert_at: Optional[str] = None
    alert_pending: bool = False
    pending_event_type: Optional[str] = None


@dataclass
class ServiceState:
    service_id: str
    state: str = "UNKNOWN"
    last_check_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    consecutive_degraded: int = 0
    failure_streak_started_at: Optional[str] = None
    last_classification: Optional[str] = None
    last_http_status: Optional[int] = None
    last_response_time_ms: Optional[float] = None
    last_alert_at: Optional[str] = None
    last_recovery_at: Optional[str] = None
    last_notification_status: Optional[str] = None
    alert_delivery_failures: int = 0
    last_delivery_failure_at: Optional[str] = None
    pending_recovery: Optional[Dict[str, Any]] = None
    incident: Optional[Incident] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServiceState":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        incident = values.get("incident")
        if isinstance(incident, dict):
            inc_known = {f.name for f in fields(Incident)}
            values["incident"] = Incident(**{k: v for k, v in incident.items() if k in inc_known})
        return cls(**values)


@dataclass
class NotifierState:
    consecutive_failures: int = 0
    open_until_epoch: float = 0.0
    last_failure_at: Optional[str] = None
    total_delivery_failures: int = 0


class StateManager:
    def __init__(self, path: str, persist: bool = True) -> None:
        self.path = path
        self.persist = persist  # False: read the file but never write it (dry-run CLI commands)
        self.lock_path = path + ".lock"
        self._lock = threading.RLock()
        self._services: Dict[str, ServiceState] = {}
        self._notifier = NotifierState()
        if persist:
            os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o750, exist_ok=True)

    # ------------------------------------------------------------------ load / save
    def load(self) -> None:
        if not os.path.exists(self.path):
            log_event(LOG, logging.INFO, "STATE_INITIALISED", detail="no previous state file")
            return
        with self._lock, file_lock(self.lock_path, exclusive=False):
            if not os.path.exists(self.path):
                log_event(LOG, logging.INFO, "STATE_INITIALISED", detail="no previous state file")
                return
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                    raise ValueError("unsupported state file version")
                self._services = {
                    sid: ServiceState.from_dict(entry) for sid, entry in (data.get("services") or {}).items()
                }
                notifier = data.get("notifier") or {}
                self._notifier = NotifierState(**{k: v for k, v in notifier.items()
                                                  if k in {f.name for f in fields(NotifierState)}})
                log_event(LOG, logging.INFO, "STATE_LOADED", services=len(self._services))
            except (ValueError, TypeError, OSError) as exc:
                if not self.persist:
                    log_event(LOG, logging.ERROR, "STATE_CORRUPT", detail=str(exc)[:200])
                    self._services, self._notifier = {}, NotifierState()
                    return
                quarantine = f"{self.path}.corrupt-{int(time.time())}"
                try:
                    os.replace(self.path, quarantine)
                except OSError:
                    quarantine = "<not moved>"
                self._services = {}
                self._notifier = NotifierState()
                log_event(LOG, logging.ERROR, "STATE_CORRUPT", detail=str(exc)[:200], quarantined_to=quarantine)

    def _write(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "saved_at_epoch": int(time.time()),
            "services": {sid: st.to_dict() for sid, st in sorted(self._services.items())},
            "notifier": asdict(self._notifier),
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        with file_lock(self.lock_path, exclusive=True):
            fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o640)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            try:  # make the rename durable
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass

    def save(self) -> None:
        if not self.persist:
            return
        with self._lock:
            try:
                self._write()
            except OSError as exc:
                log_event(LOG, logging.ERROR, "STATE_WRITE_FAILED", detail=str(exc)[:200])

    # ------------------------------------------------------------------ accessors
    def get(self, service_id: str) -> ServiceState:
        with self._lock:
            state = self._services.get(service_id)
            return copy.deepcopy(state) if state else ServiceState(service_id=service_id)

    def put(self, state: ServiceState, persist: bool = True) -> None:
        with self._lock:
            self._services[state.service_id] = copy.deepcopy(state)
            if persist:
                self.save()

    def all(self) -> Dict[str, ServiceState]:
        with self._lock:
            return copy.deepcopy(self._services)

    def prune(self, keep_ids: Iterable[str]) -> None:
        """Drop state for services that are no longer configured (bounded file size)."""
        keep = set(keep_ids)
        with self._lock:
            removed = [sid for sid in self._services if sid not in keep]
            for sid in removed:
                del self._services[sid]
            if removed:
                log_event(LOG, logging.INFO, "STATE_PRUNED", removed=removed)
                self.save()

    def get_notifier(self) -> NotifierState:
        with self._lock:
            return copy.deepcopy(self._notifier)

    def put_notifier(self, notifier: NotifierState, persist: bool = True) -> None:
        with self._lock:
            self._notifier = copy.deepcopy(notifier)
            if persist:
                self.save()


def read_state_file(path: str) -> Dict[str, Any]:
    """Read-only snapshot for the ``status`` CLI command."""
    if not os.path.exists(path):
        return {}
    with file_lock(path + ".lock", exclusive=False):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
