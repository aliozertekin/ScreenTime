"""Single-instance guard for the daemon, as a per-user named mutex.

`Local\\ScreenTimeDaemon-<username>` lives in the user's session
namespace, so two different users (or Fast User Switching sessions) each get
their own daemon, which is exactly what we want. Windows closes the handle --
and so releases the mutex -- when the owning process dies for any reason, so a
crash can never leave a stale lock (same guarantee flock gives on Linux).

Only the daemon calls `InstanceLock.acquire()`. `is_held()` merely *opens* the
mutex to see whether it exists, so the GUI and diagnostics can ask "is a daemon
running?" without ever taking the tracking lock.

The graceful-stop event lives here too: `autostart.stop_now()` signals it so the
daemon closes its session and exits cleanly instead of being TerminateProcess'd.
"""
from __future__ import annotations

import getpass
import logging
import re
from pathlib import Path
from typing import Optional

from .win32 import Win32Api, Win32Error, default_api

log = logging.getLogger("screentime.lock.windows")


def _suffix() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", user)[:64]


def mutex_name() -> str:
    return f"Local\\ScreenTimeDaemon-{_suffix()}"


def stop_event_name() -> str:
    return f"Local\\ScreenTimeDaemonStop-{_suffix()}"


def lock_path() -> Path:
    """Kept for API symmetry with the POSIX module (and for diagnostics); the
    lock itself is the named mutex, not a file."""
    from .. import paths
    return paths().lock_dir() / "screentime-daemon.lock"


class InstanceLock:
    def __init__(self, path: Optional[Path] = None, api: Optional[Win32Api] = None):
        self.path = path or lock_path()
        self._api = api or default_api()
        self._mutex: Optional[int] = None
        self._stop_event: Optional[int] = None

    def acquire(self) -> bool:
        try:
            handle, existed = self._api.create_mutex(mutex_name())
        except Win32Error as e:
            log.error("cannot create the instance mutex: %s", e)
            return False
        if existed:
            # Someone else owns it. Drop our extra handle to the same object.
            self._api.close_handle(handle)
            return False
        self._mutex = handle
        try:
            self._stop_event = self._api.create_event(stop_event_name())
        except Win32Error as e:
            # Not fatal: without it stop falls back to terminate.
            log.warning("cannot create the stop event: %s", e)
        return True

    def stop_requested(self) -> bool:
        return self._stop_event is not None and self._api.event_is_set(self._stop_event)

    def release(self):
        for attr in ("_stop_event", "_mutex"):
            h = getattr(self, attr)
            if h is not None:
                try:
                    self._api.close_handle(h)
                finally:
                    setattr(self, attr, None)


def is_held(path: Optional[Path] = None, api: Optional[Win32Api] = None) -> bool:
    api = api or default_api()
    try:
        return api.open_mutex_exists(mutex_name())
    except Win32Error:
        return False


def request_stop(api: Optional[Win32Api] = None) -> bool:
    """Ask a running daemon to shut down cleanly. True if the request was delivered."""
    api = api or default_api()
    try:
        return api.open_event_and_set(stop_event_name())
    except Win32Error:
        return False
