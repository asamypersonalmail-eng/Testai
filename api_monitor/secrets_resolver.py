"""Resolution of secret references and request-time placeholders.

Secrets are never stored in the JSON configuration. Instead, configuration
values reference them:

    ${env:NAME}       value of environment variable NAME
    ${file:/path}     content of a protected file (must not be group/world readable)
    ${gen:uuid}       generated value (uuid, uuid_short, timestamp_iso, epoch_ms, date)

Every secret resolved through this module is registered so the logging layer
can redact it if it ever appears in a log record.
"""
from __future__ import annotations

import os
import re
import stat
import threading
import uuid
from typing import Any, FrozenSet, Iterator, List, Set, Tuple

from models import utc_now

REF_RE = re.compile(r"\$\{(env|file|gen):([^}]+)\}")
PURE_SECRET_REF_RE = re.compile(r"^\$\{(env|file):[^}]+\}$")
MAX_SECRET_FILE_BYTES = 64 * 1024
GENERATORS = ("uuid", "uuid_short", "timestamp_iso", "epoch_ms", "date")


class SecretResolutionError(Exception):
    """A referenced secret could not be resolved."""


class SecretRegistry:
    """Thread-safe registry of resolved secret values, used for log redaction."""

    MIN_LENGTH = 4
    MAX_ENTRIES = 1000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: Set[str] = set()

    def register(self, value: str) -> None:
        if not value or len(value) < self.MIN_LENGTH:
            return
        with self._lock:
            if len(self._values) < self.MAX_ENTRIES:
                self._values.add(value)

    def values(self) -> FrozenSet[str]:
        with self._lock:
            return frozenset(self._values)


REGISTRY = SecretRegistry()


def _read_secret_file(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError as exc:
        raise SecretResolutionError(f"secret file is not accessible ({exc.__class__.__name__})") from None
    if not stat.S_ISREG(st.st_mode):
        raise SecretResolutionError("secret file is not a regular file")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SecretResolutionError("secret file must not be accessible by group/other (use chmod 600)")
    if st.st_size > MAX_SECRET_FILE_BYTES:
        raise SecretResolutionError("secret file is too large")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().rstrip("\r\n")


def _generate(name: str) -> str:
    now = utc_now()
    if name == "uuid":
        return str(uuid.uuid4())
    if name == "uuid_short":
        return uuid.uuid4().hex[:16]
    if name == "timestamp_iso":
        return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if name == "epoch_ms":
        return str(int(now.timestamp() * 1000))
    if name == "date":
        return now.strftime("%Y%m%d")
    raise SecretResolutionError(f"unknown generator '{name}'")


def resolve_string(value: str) -> str:
    """Replace every ${...} reference in ``value``."""

    def _replace(match: "re.Match[str]") -> str:
        kind, ref = match.group(1), match.group(2).strip()
        if kind == "env":
            resolved = os.environ.get(ref)
            if resolved is None:
                raise SecretResolutionError(f"environment variable '{ref}' is not set")
            REGISTRY.register(resolved)
            return resolved
        if kind == "file":
            resolved = _read_secret_file(ref)
            REGISTRY.register(resolved)
            return resolved
        return _generate(ref)

    return REF_RE.sub(_replace, value)


def resolve(obj: Any) -> Any:
    """Recursively resolve references in strings inside dicts/lists."""
    if isinstance(obj, str):
        return resolve_string(obj)
    if isinstance(obj, dict):
        return {key: resolve(val) for key, val in obj.items()}
    if isinstance(obj, list):
        return [resolve(item) for item in obj]
    return obj


def iter_references(obj: Any) -> Iterator[Tuple[str, str]]:
    """Yield (kind, reference) for every ${...} reference in ``obj``."""
    if isinstance(obj, str):
        for match in REF_RE.finditer(obj):
            yield match.group(1), match.group(2).strip()
    elif isinstance(obj, dict):
        for val in obj.values():
            yield from iter_references(val)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_references(item)


def check_references(obj: Any) -> List[str]:
    """Return human-readable problems for unresolved secret references."""
    problems: List[str] = []
    for kind, ref in iter_references(obj):
        if kind == "env" and ref not in os.environ:
            problems.append(f"environment variable '{ref}' is not set")
        elif kind == "file":
            try:
                _read_secret_file(ref)
            except SecretResolutionError as exc:
                problems.append(f"secret file '{ref}': {exc}")
        elif kind == "gen" and ref not in GENERATORS:
            problems.append(f"unknown generator '{ref}'")
    return problems
