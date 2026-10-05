"""Settings -> Updates: a manual, opt-in check against GitHub Releases. Nothing runs on its own."""
from __future__ import annotations

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ... import updates
from ...db import Database


class UpdatesGroup(Adw.PreferencesGroup):
    def __init__(self, db: Database, fetch=None):
        super().__init__(
            title="Updates",
            description="ScreenTime never checks for updates by itself. When you press the button it contacts "
                        "github.com once to read the newest release number. No usage data or settings are sent.")
        self.db = db
        self._fetch = fetch
        self._url = updates.RELEASES_URL
        self._syncing = False

        self.allow_row = Adw.SwitchRow(
            title="Allow update checks",
            subtitle="Turn off to make sure ScreenTime never contacts the internet")
        self.allow_row.set_active(updates.enabled(db))
        self.allow_row.connect("notify::active", self._on_allow)
        self.add(self.allow_row)

        self.check_row = Adw.ActionRow(title="Check for updates", subtitle=f"Installed version: {self._current()}")
        self.check_btn = Gtk.Button(label="Check now", valign=Gtk.Align.CENTER)
        self.check_btn.connect("clicked", lambda *_: self.start_check())
        self.open_btn = Gtk.Button(label="Open release page", valign=Gtk.Align.CENTER, visible=False)
        self.open_btn.connect("clicked", self._on_open)
        self.check_row.add_suffix(self.open_btn)
        self.check_row.add_suffix(self.check_btn)
        self.add(self.check_row)
        self.check_btn.set_sensitive(self.allow_row.get_active())

    @staticmethod
    def _current() -> str:
        from ... import __version__
        return __version__

    def _on_allow(self, row, _p):
        if self._syncing:
            return
        self.db.set_setting(updates.SETTING_ENABLED, "true" if row.get_active() else "false")
        self.check_btn.set_sensitive(row.get_active())
        self.open_btn.set_visible(False)

    def start_check(self):
        """Runs the one request off the UI thread (the settings value is read here; sqlite is thread-bound)."""
        allowed = updates.enabled(self.db)
        self.check_btn.set_sensitive(False)
        self.check_row.set_subtitle("Contacting github.com\u2026")

        def worker():
            result = updates.check(allowed=allowed, fetch=self._fetch)
            GLib.idle_add(self.show_result, result)
        threading.Thread(target=worker, name="screentime-update-check", daemon=True).start()

    def show_result(self, r: "updates.UpdateResult") -> bool:
        self.check_btn.set_sensitive(updates.enabled(self.db))
        self._url = r.url
        self.open_btn.set_visible(r.status == updates.AVAILABLE)
        if r.status == updates.AVAILABLE:
            text = f"{r.message} You have {r.current}."
        elif r.status == updates.UP_TO_DATE:
            text = f"{r.message} ({r.current})"
        else:
            text = r.message
        self.check_row.set_subtitle(text)
        return False                                        # GLib one-shot

    def _on_open(self, _btn):
        Gio.AppInfo.launch_default_for_uri(self._url, None)   # the user's browser; ScreenTime downloads nothing
