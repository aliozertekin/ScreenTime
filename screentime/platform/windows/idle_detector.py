"""Idle detection with GetLastInputInfo (no input hooks).

GetLastInputInfo reports the tick (GetTickCount, 32-bit milliseconds) of the
last input in THIS interactive session; idle time is "now tick - that tick".
Both are 32-bit values that wrap every ~49.7 days, so the subtraction is done
modulo 2**32. When the session is locked, input goes to the secure desktop and
the value stops advancing, so a lock simply ages into "idle" like any other
absence -- it is deliberately not treated as sleep.
"""
from __future__ import annotations

import logging
from typing import Optional

from ...idle_detector import IdleDetector
from .win32 import Win32Api, Win32Error, default_api

log = logging.getLogger("screentime.idle.windows")

TICK_MODULUS = 1 << 32


def idle_milliseconds(now_tick: int, last_input_tick: int) -> int:
    """Wrap-safe elapsed ms between two 32-bit tick values."""
    return (now_tick - last_input_tick) % TICK_MODULUS


class WindowsIdleDetector(IdleDetector):
    name = "windows"

    def __init__(self, api: Optional[Win32Api] = None):
        self._api = api or default_api()
        self._last_good = 0.0
        self.last_error: Optional[str] = None

    def is_supported(self) -> bool:
        try:
            self._api.get_last_input_tick()
            return True
        except (OSError, AttributeError):
            return False

    def get_idle_seconds(self) -> float:
        try:
            last = self._api.get_last_input_tick()
            now = self._api.get_tick_count()
        except Win32Error as e:
            # A failed query means "unknown", which must not look like "idle"
            # (that would silently stop the clock) -- report "active" like the
            # Linux detectors do on error.
            self.last_error = f"GetLastInputInfo: {e.code}"
            log.debug("idle query failed: %s", e)
            return 0.0
        self.last_error = None
        return idle_milliseconds(now, last) / 1000.0
