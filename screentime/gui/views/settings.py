from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from ...db import Database
from ...config import Config
from ... import autostart
from ... import wayland_setup


class SettingsView(Gtk.Box):
    def __init__(self, db: Database, config: Config):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.db = db
        self.config = config
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.set_margin_start(24)
        self.set_margin_end(24)

        title = Gtk.Label(label="Settings", xalign=0)
        title.add_css_class("title-1")
        self.append(title)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        scroller.set_child(col)
        self.append(scroller)

        # ---- Tracking behavior -------------------------------------------------
        tracking_group = Adw.PreferencesGroup(title="Tracking")

        idle_row = Adw.SpinRow.new_with_range(30, 3600, 30)
        idle_row.set_title("Idle timeout")
        idle_row.set_subtitle("Stop counting active time after this many seconds without input")
        idle_row.set_value(config.idle_timeout_seconds)
        idle_row.connect("notify::value", self._on_idle_changed)
        tracking_group.add(idle_row)

        poll_row = Adw.SpinRow.new_with_range(1, 30, 1)
        poll_row.set_title("Poll interval (seconds)")
        poll_row.set_subtitle("How often the daemon checks the focused window")
        poll_row.set_value(config.poll_interval_seconds)
        poll_row.connect("notify::value", self._on_poll_changed)
        tracking_group.add(poll_row)

        autostart_row = Adw.SwitchRow()
        autostart_row.set_title("Start tracking automatically at login")
        autostart_row.set_subtitle("Runs the background tracker; this window is opened manually")
        autostart_row.set_active(autostart.is_enabled())
        autostart_row.connect("notify::active", self._on_autostart_changed)
        tracking_group.add(autostart_row)

        col.append(tracking_group)

        # ---- Diagnostics --------------------------------------------------------
        diag_group = Adw.PreferencesGroup(title="Diagnostics")
        running = autostart.daemon_is_running()

        self._daemon_row = Adw.ActionRow(
            title="Tracking daemon",
            subtitle="Running" if running else "Not running",
        )
        self._daemon_btn = Gtk.Button(label="Stop" if running else "Start", valign=Gtk.Align.CENTER)
        self._daemon_btn.connect("clicked", self._on_toggle_daemon)
        self._daemon_row.add_suffix(self._daemon_btn)
        diag_group.add(self._daemon_row)
        self._window_backend_row = Adw.ActionRow(title="Active-window detection")
        self._idle_backend_row = Adw.ActionRow(title="Idle detection")
        diag_group.add(self._window_backend_row)
        diag_group.add(self._idle_backend_row)
        self._refresh_backend_rows()

        wayland_status = wayland_setup.status_for_current_desktop()
        if wayland_status is not None:
            desktop_label = "KDE Plasma" if wayland_status["desktop"] == "kde" else "GNOME"
            installed = wayland_status.get("installed", False)
            up_to_date = wayland_status.get("up_to_date", installed)
            enabled = wayland_status.get("enabled", installed)  # KDE has no separate "enabled" concept
            if installed and up_to_date and enabled:
                subtitle = "Installed and enabled"
            elif installed and not up_to_date:
                subtitle = ("An older copy is installed \u2014 click Reinstall to update it "
                           "to the version bundled with this screentime install")
            elif installed:
                subtitle = "Installed but not enabled"
            else:
                subtitle = "Not installed \u2014 focus tracking will not work until this is set up"
            helper_row = Adw.ActionRow(
                title=f"{desktop_label} Wayland focus helper", subtitle=subtitle,
            )
            install_btn = Gtk.Button(label="Reinstall" if installed else "Install",
                                      valign=Gtk.Align.CENTER)
            install_btn.connect("clicked", self._on_install_wayland_helper)
            helper_row.add_suffix(install_btn)
            diag_group.add(helper_row)
            self._wayland_helper_row = helper_row
            self._wayland_helper_btn = install_btn
        elif (self.db.get_setting("active_window_backend") or "") == "unsupported":
            warn = Adw.ActionRow(
                title="No active-window backend available",
                subtitle="See README \u2192 Wayland compatibility for your desktop environment",
            )
            diag_group.add(warn)
        col.append(diag_group)

        # ---- Exclusions -----------------------------------------------------------
        self.excl_group = Adw.PreferencesGroup(
            title="Excluded applications",
            description="Excluded apps are never tracked and contribute no usage time",
        )
        col.append(self.excl_group)
        self._exclusion_rows = []
        self._refresh_exclusions()

        # ---- Data -------------------------------------------------------------------
        data_group = Adw.PreferencesGroup(title="Data")
        db_row = Adw.ActionRow(title="Database location", subtitle=str(self.db.path))
        data_group.add(db_row)
        rebuild_row = Adw.ActionRow(title="Rebuild statistics cache",
                                     subtitle="Recomputes daily totals from the raw session log")
        rebuild_btn = Gtk.Button(label="Rebuild", valign=Gtk.Align.CENTER)
        rebuild_btn.connect("clicked", self._on_rebuild)
        rebuild_row.add_suffix(rebuild_btn)
        data_group.add(rebuild_row)
        col.append(data_group)

        # ---- Privacy ------------------------------------------------------------------
        privacy_group = Adw.PreferencesGroup(title="Privacy")
        privacy_row = Adw.ActionRow(
            title="All data stays on this computer",
            subtitle="No telemetry, no cloud services, no network access. Ever.",
        )
        privacy_group.add(privacy_row)
        col.append(privacy_group)

    def _on_idle_changed(self, row, _pspec):
        self.config.set("idle_timeout_seconds", int(row.get_value()))

    def _on_poll_changed(self, row, _pspec):
        self.config.set("poll_interval_seconds", int(row.get_value()))

    def _on_autostart_changed(self, row, _pspec):
        if row.get_active():
            autostart.enable()
        else:
            autostart.disable()

    def refresh(self):
        running = autostart.daemon_is_running()
        self._daemon_row.set_subtitle("Running" if running else "Not running")
        self._daemon_btn.set_label("Stop" if running else "Start")
        self._refresh_backend_rows()

    def _refresh_backend_rows(self):
        # Deliberately reads what the daemon itself reported (via the
        # settings table) rather than ever instantiating a detector here.
        # See daemon.py's module docstring: a KWinPushDetector claims
        # exclusive ownership of a D-Bus name for as long as it's alive, so
        # a GUI-side instance built "just to show a status string" can
        # permanently starve the real daemon's detector of the events KWin
        # sends -- this bit us for real once already.
        window_backend = self.db.get_setting("active_window_backend") or ""
        idle_backend = self.db.get_setting("active_idle_backend") or ""
        self._window_backend_row.set_subtitle(window_backend or "Unknown \u2014 start the daemon to detect")
        self._idle_backend_row.set_subtitle(idle_backend or "Unknown \u2014 start the daemon to detect")

    def _on_rebuild(self, _btn):
        self.db.rebuild_daily_totals()

    def _on_toggle_daemon(self, btn):
        btn.set_sensitive(False)
        currently_running = autostart.daemon_is_running()
        btn.set_label("Stopping\u2026" if currently_running else "Starting\u2026")

        def worker():
            ok = autostart.stop_now() if currently_running else autostart.start_now()
            GLib.idle_add(self._on_toggle_daemon_done, ok, currently_running)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _on_toggle_daemon_done(self, ok: bool, was_running: bool):
        # Trust the action's own result rather than immediately re-querying
        # daemon_is_running(), which can lag a moment behind systemd
        # actually reporting the new state.
        now_running = (not was_running) if ok else autostart.daemon_is_running()
        self._daemon_row.set_subtitle("Running" if now_running else "Not running")
        self._daemon_btn.set_label("Stop" if now_running else "Start")
        self._daemon_btn.set_sensitive(True)
        if not ok:
            action = "stop" if was_running else "start"
            self._daemon_row.set_subtitle(
                f"Couldn't {action} it automatically. Try: systemctl --user {action} screentime-daemon"
            )
        self._refresh_backend_rows()
        return False  # one-shot GLib.idle_add callback

    def _on_install_wayland_helper(self, btn):
        btn.set_sensitive(False)
        btn.set_label("Installing\u2026")

        def worker():
            ok, msg = wayland_setup.install_for_current_desktop()
            GLib.idle_add(self._on_install_wayland_helper_done, ok, msg)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _on_install_wayland_helper_done(self, ok: bool, msg: str):
        self._wayland_helper_row.set_subtitle(msg)
        status = wayland_setup.status_for_current_desktop()
        installed = bool(status and status.get("installed"))
        self._wayland_helper_btn.set_sensitive(True)
        self._wayland_helper_btn.set_label("Reinstall" if installed else "Install")
        return False  # one-shot GLib.idle_add callback

    def _refresh_exclusions(self):
        for row in self._exclusion_rows:
            self.excl_group.remove(row)
        self._exclusion_rows = []
        for app in self.db.list_apps():
            row = Adw.SwitchRow()
            row.set_title(app.display_name)
            row.set_active(app.excluded)
            row.connect("notify::active", self._on_exclusion_toggled, app.id)
            self.excl_group.add(row)
            self._exclusion_rows.append(row)

    def _on_exclusion_toggled(self, row, _pspec, app_id):
        self.db.set_excluded(app_id, row.get_active())
