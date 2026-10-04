"""Non-blocking advisory file locks: flock(2) on POSIX, LockFileEx-style
byte-range locking (msvcrt.locking) on Windows. Both are released by the OS if
the holder dies, so neither can leave a stale lock behind."""
from __future__ import annotations

import os

from . import is_windows


def try_lock(fd: int) -> bool:
    """Take an exclusive lock on `fd` without waiting. False if someone holds it."""
    if is_windows():
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(fd: int) -> None:
    if is_windows():
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
