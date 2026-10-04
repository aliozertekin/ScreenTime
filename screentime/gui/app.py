from __future__ import annotations

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio

from ..config import Config
from .. import storage
from .window import MainWindow
from .theme_manager import ThemeManager, set_theme_manager
from .tray import try_create_indicator
from .. import wayland_setup
from .. import autostart
from .. import platform as _platform
from .. import __version__

log = logging.getLogger("screentime.gui")


class ScreenTimeApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.screentime.App", flags=Gio.ApplicationFlags.FLAGS_NONE)
        # recover_orphans=False: only the daemon may close open sessions.
        # interactive: the GUI (unlike the daemon) may show the keyring's own
        # unlock / create dialog.
        self.db = storage.open_database(recover_orphans=False, interactive=True)
        self.config = Config(self.db)
        if not _platform.is_windows():
            wayland_setup.auto_install_if_needed(self.db)
        self.window: MainWindow | None = None
        self.theme: ThemeManager | None = None
        self._indicator = None
        self.connect("activate", self._on_activate)

    def _reconcile_autostart(self):
        """If the user opted in to start-at-login, make sure that's still true
        and the daemon is running. Idempotent (single-instance lock); runs off
        the UI thread since it shells out to systemctl."""
        import threading
        wanted = self.config.autostart_enabled   # read here: sqlite conn is thread-bound
        recorded_daemon_version = self.db.get_setting("daemon_version")

        def worker():
            try:
                autostart.reconcile(wanted)
                # A package upgrade leaves the old daemon running old code.
                if autostart.refresh_if_outdated(recorded_daemon_version, __version__):
                    log.info("Restarted the tracking daemon to load version %s", __version__)
            except Exception:
                log.exception("autostart reconcile failed (continuing)")
        threading.Thread(target=worker, daemon=True).start()

    def _on_activate(self, app):
        if self.window is None:
            # Theme first (needs the display, which exists by activate) so the
            # window is created already themed -- no flash of the wrong colors.
            self.theme = ThemeManager(self.config)
            set_theme_manager(self.theme)
            self.theme.apply()
            self.theme.start_watching()
            self._reconcile_autostart()
            self.window = MainWindow(self, self.db)
            self.window.connect("close-request", self._on_close_request)
            if self.config.minimize_to_tray:
                self._indicator = try_create_indicator(
                    on_show=self._show_window, on_quit=self._quit
                )
        self.window.present()

    def _on_close_request(self, *_args) -> bool:
        """Closing the window hides it (tracking keeps running in the
        daemon) rather than quitting the app, IF a tray icon is available to
        get back to it and the setting is enabled. Otherwise the GUI quits
        normally -- the daemon is a separate process either way and is
        unaffected by this window closing."""
        if self._indicator is not None and self.config.minimize_to_tray:
            self.window.set_visible(False)
            return True  # prevent default close/destroy
        self._quit()
        return False

    def _show_window(self):
        if self.window:
            self.window.set_visible(True)
            self.window.present()

    def _quit(self):
        if self._indicator is not None and hasattr(self._indicator, "stop"):
            self._indicator.stop()                    # Windows tray: remove the icon from the notification area
        if self.theme is not None:
            self.theme.close()
        self.db.close()
        self.quit()


_OPEN_ERRORS = (storage.StorageError, storage.SecureStoreError, storage.KeyStoreError)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if _platform.is_windows():
        from ..platform.windows import runtime_env
        runtime_env.prepare()           # packaged build: per-location gdk-pixbuf loader cache (no-op elsewhere)
    try:
        app = ScreenTimeApp()
    except _OPEN_ERRORS as e:
        log.error("cannot open the protected database: %s", e)
        from .unlock import LockedApp
        return LockedApp(e).run(None)
    return app.run(None)


if __name__ == "__main__":
    main()
