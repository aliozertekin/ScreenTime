"""A monotonic clock that stops while Windows sleeps.

SessionManager derives a session's end from monotonic elapsed time precisely so
that suspended time can never turn into fake usage. On Linux that is
CLOCK_MONOTONIC, which does not advance during suspend. On Windows,
`time.monotonic()` is NOT guaranteed to have that property (it is backed by
QueryPerformanceCounter / GetTickCount64, which keep counting through sleep on
current Windows builds). QueryUnbiasedInterruptTime is documented to exclude
time in sleep/hibernation, so that is what Windows uses.

If the call ever fails we fall back to time.monotonic() and say so once; the
power-event source and the daemon's wall-vs-monotonic drift guard then carry
the load.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .win32 import Win32Api, Win32Error, default_api

log = logging.getLogger("screentime.clock")


def make_unbiased_clock(api: Optional[Win32Api] = None) -> Callable[[], float]:
    state = {"warned": False}
    try:
        api = api or default_api()
        api.unbiased_interrupt_seconds()          # probe once
    except (OSError, RuntimeError, AttributeError) as e:
        log.warning("QueryUnbiasedInterruptTime unavailable (%s); using time.monotonic()", e)
        return time.monotonic

    def clock() -> float:
        try:
            return api.unbiased_interrupt_seconds()
        except Win32Error:
            if not state["warned"]:
                state["warned"] = True
                log.warning("QueryUnbiasedInterruptTime started failing; falling back to time.monotonic()")
            return time.monotonic()
    return clock
