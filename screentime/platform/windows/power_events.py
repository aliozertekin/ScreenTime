"""Suspend/resume/shutdown notifications from Windows.

WM_POWERBROADCAST carries PBT_APMSUSPEND just before sleep and
PBT_APMRESUMEAUTOMATIC / PBT_APMRESUMESUSPEND after wake; WM_ENDSESSION says the
user is logging off or Windows is shutting down.

What this source deliberately does NOT do: subscribe to display-off, console
display state, session lock/unlock, or idle. Those are not suspend. A locked or
dark-screen PC is handled by idle detection; only a real suspend closes a
session through here. (We therefore do not need RegisterPowerSettingNotification.)

Suspend must be handled before Windows proceeds, so the message thread waits --
bounded -- for the daemon's main thread to close the open session. If an event
is missed anyway (Modern Standby does not reliably send them to desktop apps)
the daemon's unbiased clock and wall-vs-monotonic drift guard still keep sleep
out of usage; see daemon._detect_missed_suspend.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ..base import PowerEventSource
from .win32 import (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMECRITICAL, PBT_APMRESUMESUSPEND, PBT_APMSUSPEND,
                    WM_ENDSESSION, WM_POWERBROADCAST, WM_QUERYENDSESSION)

log = logging.getLogger("screentime.power.windows")

SUSPEND_WAIT_SECONDS = 2.0       # Windows gives apps only a couple of seconds to react
RESUME_DEDUP_SECONDS = 2.0       # AUTOMATIC and SUSPEND resumes usually arrive back to back
TRUE = 1

Dispatch = Callable[[Callable[[], None], bool], None]


def make_dispatcher(idle_add: Callable, wait_seconds: float = SUSPEND_WAIT_SECONDS) -> Dispatch:
    """Run `fn` on the main loop (`idle_add` = GLib.idle_add). With wait=True the
    caller blocks until `fn` has finished or `wait_seconds` elapsed -- used for
    suspend so the session is closed before the machine sleeps."""
    def dispatch(fn: Callable[[], None], wait: bool = False) -> None:
        done = threading.Event()

        def run():
            try:
                fn()
            except Exception:
                log.exception("power event handler failed")
            finally:
                done.set()
            return False                                  # one-shot GLib source
        idle_add(run)
        if wait and not done.wait(wait_seconds):
            log.warning("main loop did not finish the power event within %.1fs", wait_seconds)
    return dispatch


class WindowsPowerEventSource(PowerEventSource):
    name = "windows"

    def __init__(self, dispatch: Dispatch, window_factory: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._dispatch = dispatch
        self._window_factory = window_factory
        self._clock = clock
        self._window = None
        self._on_suspend = self._on_resume = self._on_end = None
        self._last_resume = float("-inf")
        self.last_error: Optional[str] = None

    # ---- pure message logic (unit tested) ------------------------------------
    def handle_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> Optional[int]:
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                log.info("Windows is suspending")
                self._dispatch(self._on_suspend, True)
            elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND, PBT_APMRESUMECRITICAL):
                now = self._clock()
                if now - self._last_resume >= RESUME_DEDUP_SECONDS:
                    self._last_resume = now
                    log.info("Windows resumed")
                    self._dispatch(self._on_resume, False)
            return TRUE                                   # we handled (or ignored) it; never block power transitions
        if msg == WM_QUERYENDSESSION:
            return TRUE                                   # never veto a shutdown
        if msg == WM_ENDSESSION:
            if wparam:                                    # TRUE: the session really is ending
                log.info("Windows session ending")
                self._dispatch(self._on_end, True)
            return 0
        return None

    # ---- lifecycle ---------------------------------------------------------------
    def start(self, on_suspend, on_resume, on_session_end) -> bool:
        self._on_suspend, self._on_resume, self._on_end = on_suspend, on_resume, on_session_end
        factory = self._window_factory
        if factory is None:
            from .msgwindow import MessageWindow
            factory = lambda handler: MessageWindow("ScreenTimePowerSink", handler)   # noqa: E731
        self._window = factory(self.handle_message)
        if not self._window.start():
            self.last_error = getattr(self._window, "error", None) or "could not create the message window"
            log.warning("Windows power events unavailable: %s", self.last_error)
            self._window = None
            return False
        return True

    def stop(self) -> None:
        if self._window is not None:
            self._window.stop()
            self._window = None
