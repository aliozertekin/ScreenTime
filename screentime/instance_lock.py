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

import os
from pathlib import Path
from typing import Optional

from . import platform as _platform
from .platform import filelock as _filelock


def lock_path() -> Path:
    base = _platform.paths().lock_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "screentime-daemon.lock"


class InstanceLock:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or lock_path()
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        if not _filelock.try_lock(fd):
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self):
        if self._fd is not None:
            try:
                _filelock.unlock(self._fd)
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
        if not _filelock.try_lock(fd):
            return True
        _filelock.unlock(fd)
        return False
    finally:
        os.close(fd)


# Windows uses a per-user named mutex instead of a lock file (see
# platform/windows/instance_lock.py). Selected once, here, by the central
# platform switch; the Linux implementation above is untouched.
if _platform.is_windows():
    from .platform.windows.instance_lock import InstanceLock, is_held, lock_path  # noqa: F811
