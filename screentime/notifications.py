"""Desktop notifications, local only.

A `Notifier` shows one message on this computer's own notification system and
returns whether that worked. Nothing here touches the network:

* Linux   -- the freedesktop notification service on the *session* D-Bus
             (`org.freedesktop.Notifications`), the same one `notify-send` uses.
* Windows -- a shell notification (balloon / toast) from `platform.windows.notify`.

Failure is never fatal: a missing notification service just means no popup.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from . import platform as _platform

log = logging.getLogger("screentime.notifications")

MAX_TITLE = 60
MAX_BODY = 240
APP_NAME = "ScreenTime"


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


class Notifier(ABC):
    name = "none"

    @abstractmethod
    def send(self, title: str, body: str) -> bool:
        """Show a notification. True if the OS accepted it."""


class NullNotifier(Notifier):
    name = "none"

    def send(self, title: str, body: str) -> bool:
        return False


class FreedesktopNotifier(Notifier):
    name = "freedesktop"

    def __init__(self):
        self._bus = None

    def _connection(self):
        if self._bus is None:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus

    def send(self, title: str, body: str) -> bool:
        try:
            from gi.repository import GLib, Gio
            self._connection().call_sync(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", "Notify",
                GLib.Variant("(susssasa{sv}i)", (
                    APP_NAME, 0, "screentime", clip(title, MAX_TITLE), clip(body, MAX_BODY),
                    [], {"desktop-entry": GLib.Variant("s", "org.screentime.App")}, -1)),
                None, Gio.DBusCallFlags.NONE, 2000, None)
            return True
        except Exception as e:                       # no session bus / no notification daemon
            log.debug("desktop notification not delivered: %s", e)
            self._bus = None
            return False


def create_notifier() -> Notifier:
    if _platform.is_windows():
        try:
            from .platform.windows.notify import WindowsBalloonNotifier
            return WindowsBalloonNotifier()
        except Exception as e:
            log.info("Windows notifications unavailable: %s", e)
            return NullNotifier()
    return FreedesktopNotifier()
