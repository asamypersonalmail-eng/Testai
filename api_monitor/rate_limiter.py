"""Thread-safe, non-blocking rate limiting primitives (monotonic clock based)."""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable, Deque


class TokenBucket:
    """Classic token bucket: ``rate_per_minute`` tokens per minute, burst = capacity."""

    def __init__(self, rate_per_minute: float, capacity: float = 0, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate_per_second = max(0.0, float(rate_per_minute)) / 60.0
        self.capacity = float(capacity or rate_per_minute)
        self._tokens = self.capacity
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate_per_second)
            self._last = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


class SlidingWindowCounter:
    """At most ``limit`` events per ``window_seconds``. Memory is bounded by ``limit``."""

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = int(limit)
        self.window = float(window_seconds)
        self._clock = clock
        self._events: Deque[float] = collections.deque(maxlen=max(1, self.limit))
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            now = self._clock()
            while self._events and now - self._events[0] >= self.window:
                self._events.popleft()
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True

    def set_limit(self, limit: int) -> None:
        with self._lock:
            self.limit = int(limit)
            self._events = collections.deque(self._events, maxlen=max(1, self.limit))
