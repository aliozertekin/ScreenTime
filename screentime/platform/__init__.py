"""Central platform selection.

This is the ONLY place that decides which OS a ScreenTime process is running
on. Everything else asks `current()` (or the small helpers below) instead of
testing `sys.platform` itself, so adding or fixing a platform never means
hunting through the code base.

Linux keeps its existing, mature implementations in their historical modules
(window_detector.py, idle_detector.py, autostart.py, instance_lock.py, ...).
Windows implementations live in `screentime.platform.windows` and are wired in
through the factories on `Platform`.

`SCREENTIME_PLATFORM=windows|linux` overrides detection. It exists for tests
(which run the Windows logic against a fake Win32 layer on a Linux CI box) and
is never needed by users.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .base import PlatformPaths, PosixPaths

__all__ = ["Platform", "current", "is_windows", "paths", "monotonic_clock", "reset_for_tests"]


def _detect_name() -> str:
    forced = os.environ.get("SCREENTIME_PLATFORM", "").strip().lower()
    if forced in ("windows", "linux"):
        return forced
    return "windows" if sys.platform == "win32" else "linux"


@dataclass(frozen=True)
class Platform:
    name: str                                  # "linux" | "windows"
    paths: PlatformPaths
    # Clock that does NOT advance while the machine is asleep. SessionManager's
    # correctness (no fake hours after suspend) depends on this property.
    monotonic: Callable[[], float]

    @property
    def is_windows(self) -> bool:
        return self.name == "windows"


_cached: Optional[Platform] = None


def current() -> Platform:
    global _cached
    name = _detect_name()
    if _cached is not None and _cached.name == name:
        return _cached
    if name == "windows":
        from .windows import build_platform
        _cached = build_platform()
    else:
        _cached = Platform(name="linux", paths=PosixPaths(), monotonic=lambda: time.monotonic())
    return _cached


def reset_for_tests() -> None:
    global _cached
    _cached = None


def is_windows() -> bool:
    return _detect_name() == "windows"


def paths() -> PlatformPaths:
    return current().paths


def monotonic_clock() -> Callable[[], float]:
    return current().monotonic
