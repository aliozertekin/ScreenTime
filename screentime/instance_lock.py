"""Single-instance guard for the tracking daemon.

The daemon is the tracking authority, so two of them running at once would
double-count usage, and (worse) the second one's startup crash-recovery would
close the first one's live session. Whatever launches the daemon -- the
systemd user unit, an XDG autostart entry, the Settings "Start" button, or a
shell -- goes through this lock, which makes every startup path idempotent.

Implemented with flock(2) on a file in $XDG_RUNTIME_DIR: the kernel drops the
lock automatically when the holder dies (even on SIGKILL), so a crash can
never leave a stale lock that blocks the next start.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional


def lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(f"/tmp/screentime-{os.getuid()}")
    base.mkdir(parents=True, exist_ok=True)
    return base / "screentime-daemon.lock"


class InstanceLock:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or lock_path()
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def is_held(path: Optional[Path] = None) -> bool:
    """True if some live process currently holds the daemon lock. Probing
    never disturbs the holder: we either fail to lock (held) or lock and
    immediately release (not held)."""
    p = path or lock_path()
    try:
        fd = os.open(str(p), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
