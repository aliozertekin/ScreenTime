"""
The tracking daemon. This process is the only thing that ever writes
sessions -- the GUI is a pure read-only client of the same SQLite file (WAL
mode lets both hold connections concurrently) plus a couple of write-paths
for settings/exclusions that go through the same Database class.

Runs on a GLib main loop (no GTK/display dependency for the daemon itself,
so it works fine as a systemd --user service without a GUI toolkit loaded):
  * A periodic timeout source polls the active window + idle state.
  * A D-Bus signal subscription on org.freedesktop.login1 catches
    PrepareForSleep so suspend/resume never miscounts sleep time as usage,
    even if the poll interval would otherwise be too coarse to catch it.
  * Unix signal handlers (SIGTERM/SIGINT) guarantee a clean session close on
    `systemctl --user stop` / logout, so normal shutdowns never rely on
    crash-recovery.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

import gi
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gio

from .db import Database
from .config import Config
from .session_manager import SessionManager, FocusInfo
from .window_detector import create_window_detector, RawFocus
from .idle_detector import create_idle_detector
from . import app_identity
from .process_monitor import resolve_pid_to_app
from . import wayland_setup

log = logging.getLogger("screentime.daemon")


class Daemon:
    def __init__(self, db: Database):
        self.db = db
        self.config = Config(db)
        self.session_manager = SessionManager(db)
        install_result = wayland_setup.auto_install_if_needed(db)
        if install_result is not None:
            ok, msg = install_result
            (log.info if ok else log.warning)("Wayland focus helper: %s", msg)
        self.window_detector = create_window_detector()
        self.idle_detector = create_idle_detector()
        # Recorded so the GUI can display this without ever instantiating
        # its own detector -- see the module docstring below for why that
        # matters for KWinPushDetector specifically.
        db.set_setting("active_window_backend", self.window_detector.name)
        db.set_setting("active_idle_backend", self.idle_detector.name)
        self._was_idle = False
        self._loop = GLib.MainLoop()
        self._sleep_sub_id = None
        self._system_bus = None

    # ------------------------------------------------------------- helpers
    def _raw_to_focus(self, raw: RawFocus) -> FocusInfo | None:
        if raw is None:
            return None
        if raw.identifier.startswith("pid:"):
            pid = raw.pid
            resolved = resolve_pid_to_app(pid) if pid else None
            if resolved is None:
                return None
        else:
            resolved = app_identity.resolve(raw.identifier)
        return FocusInfo(
            key=resolved.key, display_name=resolved.display_name,
            icon_name=resolved.icon_name, desktop_file=resolved.desktop_file,
        )

    # --------------------------------------------------------------- tick
    def _tick(self) -> bool:
        try:
            idle_seconds = self.idle_detector.get_idle_seconds()
            timeout = self.config.idle_timeout_seconds
            is_idle = idle_seconds >= timeout > 0

            if is_idle and not self._was_idle:
                last_input = GLib.get_real_time() / 1_000_000.0 - idle_seconds
                self.session_manager.on_idle_start_at(last_input)
                self._was_idle = True
                log.debug("User went idle (%.0fs)", idle_seconds)
            elif not is_idle and self._was_idle:
                raw = self.window_detector.get_focused()
                focus = self._raw_to_focus(raw)
                self.session_manager.on_idle_end(focus)
                self._was_idle = False
                log.debug("User active again, focus=%s", focus.key if focus else None)
            elif not is_idle:
                raw = self.window_detector.get_focused()
                focus = self._raw_to_focus(raw)
                log.debug("tick: raw=%r focus=%s", raw, focus.key if focus else None)
                self.session_manager.on_focus_change(focus)

            self.session_manager.heartbeat()
        except Exception:
            log.exception("Error in tracking tick (continuing)")
        return True  # keep the GLib timeout source alive

    # --------------------------------------------------------- suspend/resume
    def _on_prepare_for_sleep(self, connection, sender, path, interface, signal_name, params):
        try:
            (going_to_sleep,) = params.unpack()
        except Exception:
            return
        if going_to_sleep:
            log.info("System suspending: closing open session")
            self.session_manager.on_suspend()
        else:
            log.info("System resumed")
            raw = self.window_detector.get_focused()
            focus = self._raw_to_focus(raw)
            self.session_manager.on_resume(focus)

    def _setup_sleep_watcher(self):
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._sleep_sub_id = self._system_bus.signal_subscribe(
                "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
            )
            log.info("Subscribed to logind PrepareForSleep for suspend/resume handling")
        except Exception as e:
            log.warning("Could not subscribe to logind sleep signal (%s); suspend/resume will be "
                        "handled by clock-jump immunity + idle detection instead", e)

    # ------------------------------------------------------------- lifecycle
    def _shutdown(self, reason="daemon_stop"):
        log.info("Shutting down (%s)", reason)
        self.session_manager.shutdown(reason)
        self.db.set_setting("active_window_backend", "")
        self.db.set_setting("active_idle_backend", "")
        if self._system_bus and self._sleep_sub_id:
            self._system_bus.signal_unsubscribe(self._sleep_sub_id)
        self._loop.quit()
        return False

    def run(self):
        self._setup_sleep_watcher()
        interval_ms = max(500, int(self.config.poll_interval_seconds * 1000))
        GLib.timeout_add(interval_ms, self._tick)

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: self._shutdown("shutdown"))
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: self._shutdown("shutdown"))

        log.info("ScreenTime daemon started (window backend=%s, idle backend=%s)",
                  self.window_detector.name, self.idle_detector.name)
        self._loop.run()


def main():
    parser = argparse.ArgumentParser(description="ScreenTime tracking daemon")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db = Database()
    daemon = Daemon(db)
    try:
        daemon.run()
    finally:
        db.close()


if __name__ == "__main__":
    main()
