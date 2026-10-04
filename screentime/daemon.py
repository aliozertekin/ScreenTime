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
import os
import signal
import sys
import time

import gi
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gio

from .db import Database
from .keystore import KeyStoreError
from .secure_log import SecureStoreError
from .config import Config
from .session_manager import SessionManager, FocusInfo
from .window_detector import create_window_detector, RawFocus
from .idle_detector import create_idle_detector
from . import app_identity
from . import steam_library
from . import storage
from . import __version__
from .process_monitor import resolve_pid_to_app
from . import wayland_setup
from .instance_lock import InstanceLock
from . import platform as _platform

# While a backend is the honest "nothing available" one, re-run detection this
# often. At login the session environment / compositor helper may simply not be
# ready yet when the daemon starts; without a retry the daemon would sit on
# "unsupported" until the next restart, tracking nothing, while systemd (which
# only restarts on a *crash*) reports it healthy.
MAINTENANCE_INTERVAL_SECONDS = 600.0
EXIT_CONFIG = 78      # EX_CONFIG: a problem a restart cannot fix (see the unit's RestartPreventExitStatus)
REDETECT_INTERVAL_SECONDS = 20.0
# Fail-safes for the suspend guard (see _suspend_guard_expired).
SUSPEND_GUARD_MAX_SECONDS = 30.0
SUSPEND_DRIFT_SECONDS = 5.0
NULL_WINDOW_BACKEND = "unsupported"
NULL_IDLE_BACKEND = "disabled"

log = logging.getLogger("screentime.daemon")


def repair_steam_names(db: Database, resolver=None) -> int:
    """One-time-per-start fix for names stored *before* Steam resolution existed:
    rows still showing the "Steam App <id>" fallback are renamed if the game's
    name can now be found locally. Only touches rows whose name is exactly the
    fallback, so a name the user or an earlier run already set is never
    overwritten. Returns how many rows were renamed."""
    resolver = resolver or steam_library.default_resolver()
    n = 0
    for app in db.list_apps():
        appid = steam_library.appid_from_key(app.key)
        if appid is None or app.display_name != app_identity.fallback_display_for_key(app.key):
            continue
        name = resolver.name_for_appid(appid)
        if name:
            db.rename_app(app.id, name, resolver.icon_for_appid(appid) or "steam")
            n += 1
    return n


class Daemon:
    def __init__(self, db: Database):
        self.db = db
        self.config = Config(db)
        # The monotonic clock must stop while the machine sleeps (Linux's does;
        # Windows needs QueryUnbiasedInterruptTime -- see platform/windows/clock.py).
        mono = _platform.monotonic_clock()
        self.session_manager = SessionManager(db, mono_clock=mono)
        install_result = None if _platform.is_windows() else wayland_setup.auto_install_if_needed(db)
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
        db.set_setting("daemon_last_start", str(int(time.time())))
        # Lets the GUI notice a daemon left running from before an upgrade
        # (a package upgrade doesn't restart per-user services by itself).
        db.set_setting("daemon_version", __version__)
        try:
            repaired = repair_steam_names(db)
            if repaired:
                log.info("Resolved %d stored Steam game name(s)", repaired)
        except Exception:
            log.exception("Steam name repair failed (continuing)")
        self._last_redetect = time.monotonic()
        self._last_maintenance = 0.0
        self._was_idle = False
        self._steam_unresolved_logged: set = set()
        self._suspended = False
        self._suspend_wall = 0.0
        self._suspend_mono = 0.0
        self._inhibitor_fd = None
        self._wall = time.time            # injectable for tests
        self._mono = mono
        self._power = None                # PowerEventSource (Windows); Linux keeps its logind code inline
        self._last_tick_clocks = None
        self._drift_guard = _platform.is_windows()
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
            resolved = app_identity.resolve(raw.identifier, fallback_display=raw.display_hint, pid=raw.pid)
            resolved = self._keep_known_steam_name(resolved)
        return FocusInfo(
            key=resolved.key, display_name=resolved.display_name,
            icon_name=resolved.icon_name, desktop_file=resolved.desktop_file,
        )

    # ------------------------------------------------------ late detection
    def _maybe_redetect(self):
        """Retry backend selection while we're on a null backend. Only ever
        replaces a *null* detector, so a live KWinPushDetector (which owns an
        exclusive D-Bus name) is never torn down and rebuilt."""
        window_null = self.window_detector.name == NULL_WINDOW_BACKEND
        idle_null = self.idle_detector.name == NULL_IDLE_BACKEND
        if not (window_null or idle_null):
            return
        now = time.monotonic()
        if now - self._last_redetect < REDETECT_INTERVAL_SECONDS:
            return
        self._last_redetect = now
        if window_null:
            detector = create_window_detector()
            if detector.name != NULL_WINDOW_BACKEND:
                log.info("Window backend became available: %s", detector.name)
                self.window_detector = detector
                self.db.set_setting("active_window_backend", detector.name)
        if idle_null:
            detector = create_idle_detector()
            if detector.name != NULL_IDLE_BACKEND:
                log.info("Idle backend became available: %s", detector.name)
                self.idle_detector = detector
                self.db.set_setting("active_idle_backend", detector.name)

    def _keep_known_steam_name(self, resolved):
        """If Steam metadata is unavailable right now (game uninstalled, library
        drive unmounted) don't overwrite a good name already stored for this
        app with the "Steam App <id>" fallback."""
        if steam_library.appid_from_key(resolved.key) is None:
            return resolved
        if resolved.display_name != app_identity.fallback_display_for_key(resolved.key):
            return resolved
        try:
            existing = self.db.get_app_by_key(resolved.key)
        except Exception:
            return resolved
        if existing and existing.display_name != resolved.display_name:
            resolved.display_name = existing.display_name
            resolved.icon_name = existing.icon_name or resolved.icon_name
        elif resolved.key not in self._steam_unresolved_logged:
            # Once per AppID: say where we looked, so "why is it still Steam App
            # 2620?" can be answered from the journal.
            self._steam_unresolved_logged.add(resolved.key)
            log.info("Steam AppID %s has no local manifest; searched %s",
                     steam_library.appid_from_key(resolved.key),
                     steam_library.default_resolver().describe())
        return resolved

    # --------------------------------------------------------------- tick
    def _maybe_maintain(self):
        """Periodically fold the encrypted log into a snapshot. Only the daemon
        (the long-lived process) does this; GUI readers reload transparently."""
        now = self._mono()
        if now - self._last_maintenance < MAINTENANCE_INTERVAL_SECONDS:
            return
        self._last_maintenance = now
        try:
            self.db.maintenance()
        except Exception:
            log.exception("database maintenance failed (continuing)")

    def _tick(self) -> bool:
        self._maybe_redetect()
        self._maybe_maintain()
        if self._drift_guard and not self._suspended:
            self._detect_missed_suspend()
        if self._suspended:
            # Between logind's "going to sleep" and the actual freeze (and until
            # the resume signal) nothing may open a new session: it would then
            # span the sleep. Fail-safe below prevents ever getting stuck here.
            if self._suspend_guard_expired():
                log.warning("Resume signal not seen; resuming tracking on the guard timeout/clock drift")
                self._resume_tracking()
            return True
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
            self._suspend_tracking()
        else:
            self._resume_tracking()

    def _suspend_tracking(self):
        log.info("System suspending: closing open session")
        self._suspended = True
        self._suspend_wall, self._suspend_mono = self._wall(), self._mono()
        try:
            self.session_manager.on_suspend()
            self.db.set_setting("last_suspend", str(int(self._suspend_wall)))
        except Exception:
            log.exception("Error closing session for suspend")
        # Only now let the system proceed: we held a delay inhibitor precisely so
        # the session is closed *before* the machine sleeps.
        self._release_sleep_inhibitor()

    def _resume_tracking(self):
        log.info("System resumed")
        self._suspended = False
        self._was_idle = False
        self._last_tick_clocks = None     # drift-guard baseline predates the sleep; restart it
        try:
            raw = self.window_detector.get_focused()
            self.session_manager.on_resume(self._raw_to_focus(raw))
            self.db.set_setting("last_resume", str(int(self._wall())))
        except Exception:
            log.exception("Error resuming tracking")
        self._take_sleep_inhibitor()

    def _detect_missed_suspend(self):
        """Windows only. If the wall clock advanced much more than the
        sleep-excluding monotonic clock since the last tick, the machine slept
        and no power event reached us (Modern Standby, or an event delivered
        late). Close the session at its true pre-sleep end (derived from the
        monotonic clock, so it excludes the sleep) and restart tracking."""
        wall, mono = self._wall(), self._mono()
        last, self._last_tick_clocks = self._last_tick_clocks, (wall, mono)
        if last is None:
            return
        if (wall - last[0]) - (mono - last[1]) > SUSPEND_DRIFT_SECONDS:
            log.warning("Wall clock ran %.0fs ahead of the monotonic clock: treating it as a missed suspend",
                        (wall - last[0]) - (mono - last[1]))
            self._suspend_tracking()
            self._resume_tracking()

    def _suspend_guard_expired(self) -> bool:
        """True if we've been "suspended" implausibly long without a resume
        signal. Two independent triggers: the wall clock ran ahead of the
        monotonic clock (the machine really slept and we missed/lost the
        signal), or a suspend that never happened (cancelled, inhibited)."""
        wall_elapsed = self._wall() - self._suspend_wall
        mono_elapsed = self._mono() - self._suspend_mono
        return (wall_elapsed - mono_elapsed > SUSPEND_DRIFT_SECONDS
                or mono_elapsed > SUSPEND_GUARD_MAX_SECONDS)

    # ---- logind delay inhibitor: gives us time to close the session first
    def _take_sleep_inhibitor(self):
        if self._inhibitor_fd is not None or self._system_bus is None:
            return
        try:
            result, fd_list = self._system_bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager", "Inhibit",
                GLib.Variant("(ssss)", ("sleep", "ScreenTime",
                                        "Closing the current usage session before sleep", "delay")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 2000, None, None)
            self._inhibitor_fd = fd_list.get(result.unpack()[0])
        except Exception as e:
            # Not fatal: monotonic timing keeps sleep time out of usage anyway.
            log.debug("Could not take logind sleep delay inhibitor: %s", e)

    def _release_sleep_inhibitor(self):
        fd, self._inhibitor_fd = self._inhibitor_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _setup_windows_power(self):
        from .platform.windows.power_events import WindowsPowerEventSource, make_dispatcher
        self._power = WindowsPowerEventSource(make_dispatcher(GLib.idle_add))
        ok = self._power.start(self._suspend_tracking, self._resume_tracking,
                               lambda: self._shutdown("shutdown"))
        self.db.set_setting("active_power_backend", self._power.name if ok else "none")
        if ok:
            log.info("Subscribed to Windows power notifications for suspend/resume handling")
        else:
            log.warning("Windows power notifications unavailable; relying on the sleep-excluding clock "
                        "and the clock-drift guard")

    def _setup_sleep_watcher(self):
        if _platform.is_windows():
            return self._setup_windows_power()
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._sleep_sub_id = self._system_bus.signal_subscribe(
                "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
            )
            self._take_sleep_inhibitor()
            self.db.set_setting("active_power_backend", "logind")
            log.info("Subscribed to logind PrepareForSleep for suspend/resume handling")
        except Exception as e:
            self.db.set_setting("active_power_backend", "none")
            log.warning("Could not subscribe to logind sleep signal (%s); suspend/resume will be "
                        "handled by clock-jump immunity + idle detection instead", e)

    # ------------------------------------------------------------- lifecycle
    def _shutdown(self, reason="daemon_stop"):
        log.info("Shutting down (%s)", reason)
        self.session_manager.shutdown(reason)
        self.db.set_setting("active_window_backend", "")
        self.db.set_setting("active_idle_backend", "")
        self.db.set_setting("active_power_backend", "")
        if self._power is not None:
            self._power.stop()
            self._power = None
        self._release_sleep_inhibitor()
        if self._system_bus and self._sleep_sub_id:
            self._system_bus.signal_unsubscribe(self._sleep_sub_id)
        self._loop.quit()
        return False

    def _kick_kwin_script(self) -> bool:
        """One-shot, off the main thread (it shells out to qdbus)."""
        import threading
        def worker():
            ok = wayland_setup.announce_focus_once()
            log.info("Asked KWin to re-announce the focused window: %s", "ok" if ok else "not available")
        threading.Thread(target=worker, daemon=True).start()
        return False  # GLib one-shot

    def run(self, should_stop=None):
        self._setup_sleep_watcher()
        if self.window_detector.name == "kwin-push":
            # Delay so the detector's D-Bus name is actually acquired (that
            # happens once the main loop is running) before KWin re-announces.
            GLib.timeout_add_seconds(2, self._kick_kwin_script)
        interval_ms = max(500, int(self.config.poll_interval_seconds * 1000))
        GLib.timeout_add(interval_ms, self._tick)

        if _platform.is_windows():
            # GLib.unix_signal_add does not exist on Windows. Poll the stop flag
            # (Ctrl+C / SIGTERM handler, or the graceful-stop event that
            # autostart.stop_now() sets); the poll also lets Python run its signal handlers.
            def _poll_stop():
                if should_stop is not None and should_stop():
                    self._shutdown("shutdown")
                    return False
                return True
            GLib.timeout_add(250, _poll_stop)
        else:
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: self._shutdown("shutdown"))
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: self._shutdown("shutdown"))
        if should_stop is not None and should_stop():
            # A stop request that arrived before the loop's own handlers existed.
            GLib.idle_add(lambda: (self._shutdown("shutdown"), False)[1])

        log.info("ScreenTime daemon started (window backend=%s, idle backend=%s)",
                  self.window_detector.name, self.idle_detector.name)
        self._loop.run()


def main():
    parser = argparse.ArgumentParser(description="ScreenTime tracking daemon")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if _platform.is_windows():
        from .platform.windows.logging_setup import configure as _configure_logging
        _configure_logging(args.verbose)
    else:
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    # Idempotent startup: whichever mechanism launched us (systemd unit, XDG
    # autostart, the Settings button, a shell), only one daemon may track.
    #
    # Handle SIGTERM/SIGINT from the very start: startup can take a while (opening
    # the protected database, installing the KWin/GNOME helper) and a stop request
    # in that window must still exit cleanly (status 0, lock released).
    #
    # The handler ONLY sets a flag. It must not raise: an exception thrown from a
    # signal handler lands at an arbitrary point, and when that point is inside
    # PyGObject's C-backed code (building a GLib.Variant, say) it is either
    # silently discarded (inside a destructor) or can crash the interpreter.
    # Startup instead checks the flag at its own safe points -- after each stage
    # and inside every wait loop -- and run() hands over to GLib's handlers.
    stop = {"requested": False}

    def _early_stop(signum, _frame):
        stop["requested"] = True
    signal.signal(signal.SIGTERM, _early_stop)
    signal.signal(signal.SIGINT, _early_stop)
    lock = InstanceLock()
    # On Windows a stop can also arrive as the named stop event (graceful stop
    # from Settings / the uninstaller); the lock object owns that event.
    stopping = lambda: stop["requested"] or bool(getattr(lock, "stop_requested", lambda: False)())

    if not lock.acquire():
        log.info("Another screentime-daemon is already running; exiting (this is not an error)")
        return 0
    try:
        try:
            # Only the lock holder may run crash recovery. wait_for_key: if the
            # key sits in a keyring that is merely locked/starting, keep
            # retrying (interruptibly) rather than failing; the daemon never
            # shows a keyring dialog -- the GUI offers "Unlock".
            db = storage.open_database(wait_for_key=True, should_stop=stopping)
        except (storage.StorageError, SecureStoreError, KeyStoreError) as e:
            log.error("cannot open the protected database: %s", e)
            return EXIT_CONFIG
        try:
            if stopping():
                return 0
            daemon = Daemon(db)
            if stopping():
                return 0
            daemon.run(should_stop=stopping)
        finally:
            db.close()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
