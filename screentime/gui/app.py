from __future__ import annotations

import logging

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio

from ..db import Database
from ..config import Config
from .window import MainWindow
from .tray import try_create_indicator
from .. import wayland_setup

log = logging.getLogger("screentime.gui")


class ScreenTimeApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.screentime.App", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.db = Database()
        self.config = Config(self.db)
        wayland_setup.auto_install_if_needed(self.db)
        self.window: MainWindow | None = None
        self._indicator = None
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        if self.window is None:
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
        self.db.close()
        self.quit()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = ScreenTimeApp()
    return app.run(None)


if __name__ == "__main__":
    main()
