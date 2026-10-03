"""Shown instead of the main window when the protected database cannot be
opened (keyring locked, key lost, wrong key...). It explains in plain language
and offers the ways out: try again, or restore the key from the recovery key."""
from __future__ import annotations

import logging
import os
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from .. import storage

log = logging.getLogger("screentime.gui")


class LockedApp(Adw.Application):
    def __init__(self, error: BaseException):
        super().__init__(application_id="org.screentime.App", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.error = error
        self.connect("activate", self._on_activate)

    def _on_activate(self, _app):
        win = Adw.ApplicationWindow(application=self, title="ScreenTime", default_width=560, default_height=520)
        page = Adw.StatusPage(icon_name="dialog-password-symbolic",
                              title="ScreenTime can't open its protected database",
                              description=storage.explain_open_error(self.error))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER)

        retry = Gtk.Button(label="Try again")
        retry.add_css_class("suggested-action")
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_: self._restart())
        box.append(retry)

        self._entry = Gtk.Entry(placeholder_text="Recovery key (XXXX-XXXX-...)", width_chars=44)
        box.append(self._entry)
        restore = Gtk.Button(label="Restore from recovery key")
        restore.connect("clicked", self._on_restore)
        box.append(restore)

        self._result = Gtk.Label(wrap=True, max_width_chars=50)
        box.append(self._result)
        quit_btn = Gtk.Button(label="Quit")
        quit_btn.connect("clicked", lambda *_: self.quit())
        box.append(quit_btn)

        page.set_child(box)
        win.set_content(page)
        win.present()

    def _on_restore(self, _btn):
        try:
            backend = storage.restore_from_recovery_key(self._entry.get_text())
        except (ValueError, storage.StorageError, storage.KeyStoreError) as e:
            self._result.set_text(str(e))
            self._result.add_css_class("error")
            return
        self._result.remove_css_class("error")
        self._result.set_text(f"Key restored ({backend}). Reopening...")
        self._restart()

    def _restart(self):
        os.execv(sys.executable, [sys.executable, "-m", "screentime.gui.app"])
