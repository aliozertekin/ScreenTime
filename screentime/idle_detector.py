"""
Idle detection: "how many seconds since the user last provided input".

  * X11: `xprintidle` (tiny, near-universal Arch package `xprintidle`) which
    reads the XScreenSaver extension's idle counter. If not installed, we
    fall back to python-xlib's screensaver extension directly.
  * Wayland: there is no cross-compositor client API for "seconds since last
    input" (by design, for input-privacy reasons). The portable signal that
    *is* exposed system-wide is systemd-logind's `IdleHint`/`IdleSinceHint`
    session properties, which GNOME (mutter) and KDE (kwin_wayland) update
    automatically. wlroots compositors (sway, etc.) don't update it out of
    the box; the README documents how to wire `swayidle` to do so with two
    one-line hooks.

If neither source is available, idle detection degrades to "never idle"
(disabled) rather than guessing -- and the GUI's Settings page surfaces this
so the user knows why the idle timeout isn't taking effect.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod

log = logging.getLogger("screentime.idle")


class IdleDetector(ABC):
    name = "unknown"

    @abstractmethod
    def get_idle_seconds(self) -> float:
        ...

    def is_supported(self) -> bool:
        return True


class NullIdleDetector(IdleDetector):
    name = "disabled"

    def get_idle_seconds(self) -> float:
        return 0.0

    def is_supported(self) -> bool:
        return False


class XPrintIdleDetector(IdleDetector):
    name = "xprintidle"

    def is_supported(self) -> bool:
        return shutil.which("xprintidle") is not None

    def get_idle_seconds(self) -> float:
        try:
            out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=2)
            return int(out.stdout.strip()) / 1000.0
        except Exception as e:
            log.debug("xprintidle failed: %s", e)
            return 0.0


class XlibScreensaverIdleDetector(IdleDetector):
    name = "xlib-screensaver"

    def __init__(self):
        self._ok = False
        try:
            from Xlib import display
            from Xlib.ext import screensaver
            self._display = display.Display()
            self._display.screensaver_query_info(self._display.screen().root)
            self._ok = True
        except Exception:
            self._ok = False

    def is_supported(self) -> bool:
        return self._ok

    def get_idle_seconds(self) -> float:
        try:
            info = self._display.screensaver_query_info(self._display.screen().root)
            return info.idle / 1000.0
        except Exception as e:
            log.debug("xlib screensaver idle query failed: %s", e)
            return 0.0


class LogindIdleDetector(IdleDetector):
    """Reads org.freedesktop.login1's per-session IdleHint/IdleSinceHint.
    Works out of the box on GNOME and KDE Wayland sessions; on sway/wlroots
    it requires the user to configure swayidle to toggle the hint (see
    README)."""
    name = "logind"

    def __init__(self):
        self._session_id = os.environ.get("XDG_SESSION_ID")
        self._loginctl = shutil.which("loginctl")

    def is_supported(self) -> bool:
        return bool(self._session_id) and bool(self._loginctl)

    def get_idle_seconds(self) -> float:
        try:
            out = subprocess.run(
                ["loginctl", "show-session", self._session_id, "-p", "IdleHint", "-p", "IdleSinceHint"],
                capture_output=True, text=True, timeout=2,
            ).stdout
            idle_hint = "IdleHint=yes" in out
            if not idle_hint:
                return 0.0
            m = re.search(r"IdleSinceHint=(\d+)", out)
            if not m:
                return 0.0
            # IdleSinceHint is microseconds since epoch (CLOCK_REALTIME-based on most systemd versions).
            since_us = int(m.group(1))
            if since_us == 0:
                return 0.0
            return max(0.0, time.time() - since_us / 1_000_000.0)
        except Exception as e:
            log.debug("logind idle query failed: %s", e)
            return 0.0


def create_idle_detector() -> IdleDetector:
    session = os.environ.get("XDG_SESSION_TYPE", "")
    candidates: list[IdleDetector] = []
    if session != "wayland":
        candidates.append(XPrintIdleDetector())
        candidates.append(XlibScreensaverIdleDetector())
    candidates.append(LogindIdleDetector())
    if session != "wayland":
        # last resort even on ambiguous sessions
        candidates.append(XPrintIdleDetector())

    for c in candidates:
        try:
            if c.is_supported():
                log.info("Using idle detector: %s", c.name)
                return c
        except Exception:
            continue
    log.warning("No idle-detection method available; idle timeout will not take effect until one is configured.")
    return NullIdleDetector()
