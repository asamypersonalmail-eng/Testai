"""Cross-platform file locking (POSIX ``fcntl`` on Linux/AIX, ``msvcrt`` on Windows)."""
from __future__ import annotations

import os
import time

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:  # pragma: no cover - exercised on Windows hosts
    import msvcrt
    fcntl = None
else:
    import fcntl  # type: ignore[no-redef]
    msvcrt = None  # type: ignore[assignment]

WINDOWS_LOCK_TIMEOUT_SECONDS = 30.0


def lock_fd(fd: int, exclusive: bool = True, blocking: bool = True) -> bool:
    """Lock an open file descriptor. Returns False if ``blocking`` is False and the lock is held.

    Windows has no shared locks, so every lock is exclusive there. The lock covers the
    first byte of the file, which works even when the file is empty.
    """
    if not IS_WINDOWS:
        flags = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            return True
        except OSError:
            if blocking:
                raise
            return False
    os.lseek(fd, 0, os.SEEK_SET)
    if not blocking:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    deadline = time.monotonic() + WINDOWS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def unlock_fd(fd: int) -> None:
    if not IS_WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
