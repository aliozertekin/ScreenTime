from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib
import datetime

from ...db import Database
from ...config import Config
from ... import autostart
from ... import wayland_setup
from ... import theme as theme_mod
from ... import storage
from ...keystore import KeyManager, KeyStoreError
from ...secure_log import SecureStoreError
from ..theme_manager import ensure_theme_manager
from ..widgets.bar_chart import BarChart


class SettingsView(Gtk.Box):
    def __init__(self, db: Database, config: Config, theme_manager=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.db = db
        self.config = config
        self.theme_manager = theme_manager or ensure_theme_manager(config)
        self._syncing = False
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

        col.append(self._build_appearance_group())
        col.append(self._build_security_group())

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
        self._autostart_row = autostart_row
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
        # Startup diagnostics: lets a failed start-at-login be diagnosed from
        # here. Reads only systemd/file state and what the daemon recorded in
        # the settings table -- never instantiates a detector.
        self._installed_row = Adw.ActionRow(title="Daemon installed")
        self._enabled_row = Adw.ActionRow(title="Start at login")
        self._last_start_row = Adw.ActionRow(title="Last daemon start")
        self._version_row = Adw.ActionRow(title="Daemon version")
        self._sleep_row = Adw.ActionRow(title="Last sleep / wake seen by daemon")
        diag_group.add(self._installed_row)
        diag_group.add(self._enabled_row)
        diag_group.add(self._last_start_row)
        diag_group.add(self._version_row)
        diag_group.add(self._sleep_row)
        self._window_backend_row = Adw.ActionRow(title="Active-window detection")
        self._idle_backend_row = Adw.ActionRow(title="Idle detection")
        diag_group.add(self._window_backend_row)
        diag_group.add(self._idle_backend_row)
        self._refresh_backend_rows()
        self._refresh_startup_rows()

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



    # ---- Security ------------------------------------------------------------
    def _build_security_group(self):
        group = Adw.PreferencesGroup(
            title="Security",
            description="Your usage data is encrypted on disk. It is decrypted only in memory while ScreenTime runs.")
        self._sec_protect_row = Adw.ActionRow(title="Database protection")
        self._sec_key_row = Adw.ActionRow(title="Key storage")
        self._sec_move_btn = Gtk.Button(label="Move to keyring", valign=Gtk.Align.CENTER)
        self._sec_move_btn.connect("clicked", self._on_move_key)
        self._sec_key_row.add_suffix(self._sec_move_btn)
        self._sec_state_row = Adw.ActionRow(title="Tracking")
        self._sec_unlock_btn = Gtk.Button(label="Unlock", valign=Gtk.Align.CENTER)
        self._sec_unlock_btn.connect("clicked", self._on_unlock_keyring)
        self._sec_state_row.add_suffix(self._sec_unlock_btn)
        self._sec_legacy_row = Adw.ActionRow(title="Plaintext copy found")
        recovery_row = Adw.ActionRow(
            title="Recovery key",
            subtitle="If the key is lost the data cannot be recovered. Save a recovery key somewhere safe, offline.")
        rec_btn = Gtk.Button(label="Show recovery key", valign=Gtk.Align.CENTER)
        rec_btn.connect("clicked", self._on_show_recovery_key)
        recovery_row.add_suffix(rec_btn)
        for row in (self._sec_protect_row, self._sec_key_row, self._sec_state_row, self._sec_legacy_row, recovery_row):
            group.add(row)
        self._refresh_security_rows()
        return group

    def _refresh_security_rows(self):
        if not self.db.protected:
            self._sec_protect_row.set_subtitle("Not protected (plain SQLite file: development/test mode)")
            for w in (self._sec_key_row, self._sec_state_row, self._sec_legacy_row):
                w.set_visible(False)
            return
        st = storage.security_status()
        self._sec_protect_row.set_subtitle(
            f"Encrypted with AES-256-GCM ({st.store_bytes / 1024:.0f} KiB on disk)")
        keyring = st.backend == "secret-service"
        self._sec_key_row.set_subtitle(
            "System keyring (Secret Service / KWallet)" if keyring else
            "Key file \u2014 weaker: anyone who can read your home directory can read the data. "
            "Move the key into the keyring for stronger protection.")
        self._sec_move_btn.set_visible(st.backend == "keyfile")
        waiting = st.state in ("waiting", "key-missing", "wrong-key")
        self._sec_state_row.set_visible(waiting)
        if waiting:
            self._sec_state_row.set_subtitle(
                {"waiting": "Paused: waiting for the keyring to unlock. Nothing is tracked until it does.",
                 "key-missing": "Paused: the database key was not found. Restore it with your recovery key.",
                 "wrong-key": "Paused: the stored key does not open the database."}[st.state])
            self._sec_unlock_btn.set_visible(st.state == "waiting")
        self._sec_legacy_row.set_visible(st.legacy_plaintext_present)
        if st.legacy_plaintext_present:
            self._sec_legacy_row.set_subtitle(
                "An unencrypted screentime.db is still next to the protected database (an older ScreenTime "
                "may still be running). Restart the daemon; if it persists, delete that file yourself.")

    def _alert(self, heading, body, extra=None):
        root = self.get_root()
        if hasattr(Adw, "AlertDialog"):
            d = Adw.AlertDialog(heading=heading, body=body)
            d.add_response("close", "Close")
            d.set_default_response("close")
            if extra is not None:
                d.set_extra_child(extra)
            d.present(root)
        else:                                               # libadwaita < 1.5
            d = Adw.MessageDialog(transient_for=root, heading=heading, body=body)
            d.add_response("close", "Close")
            if extra is not None:
                d.set_extra_child(extra)
            d.present()

    def _on_show_recovery_key(self, _btn):
        try:
            key = storage.export_recovery_key()
        except (SecureStoreError, KeyStoreError, storage.StorageError) as e:
            self._alert("Can't read the key", storage.explain_open_error(e))
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        label = Gtk.Label(label=key, selectable=True, wrap=True, max_width_chars=30)
        label.add_css_class("monospace")
        label.add_css_class("title-4")
        box.append(label)
        copy = Gtk.Button(label="Copy to clipboard", halign=Gtk.Align.CENTER)
        copy.connect("clicked", lambda *_: self.get_clipboard().set(key))
        box.append(copy)
        self._alert("Recovery key",
                    "Anyone who has this can read your ScreenTime data. Keep it offline and private. "
                    "It is the only way back if your keyring or key file is lost.", box)

    def _on_move_key(self, _btn):
        try:
            store_id = storage.read_store_id(storage.store_path())
            backend = KeyManager().move_to_keyring(store_id)
        except (SecureStoreError, KeyStoreError, storage.StorageError) as e:
            self._alert("Key not moved", f"{storage.explain_open_error(e)}\n\nThe key file was kept.")
        else:
            self._alert("Key moved", f"The key is now kept in: {backend}. The key file was securely removed.")
        self._refresh_security_rows()

    def _on_unlock_keyring(self, _btn):
        try:
            KeyManager().get_key(storage.read_store_id(storage.store_path()), interactive=True)
        except (SecureStoreError, KeyStoreError, storage.StorageError) as e:
            self._alert("Still locked", storage.explain_open_error(e))
        else:
            self._alert("Unlocked", "Tracking resumes within a few seconds.")
        self._refresh_security_rows()

    # ---- Appearance ----------------------------------------------------------
    SCHEME_CHOICES = (("system", "Follow system"), ("light", "Light"), ("dark", "Dark"))

    def _build_appearance_group(self):
        group = Adw.PreferencesGroup(
            title="Appearance",
            description="Theme, color scheme and accent color are three independent choices.")
        self._themes = theme_mod.list_themes()

        self._theme_row = Adw.ComboRow(title="Theme")
        self._theme_row.set_model(Gtk.StringList.new([t.name for t in self._themes]))
        self._theme_row.connect("notify::selected", self._on_theme_selected)
        group.add(self._theme_row)

        self._scheme_row = Adw.ComboRow(title="Color scheme")
        self._scheme_row.set_model(Gtk.StringList.new([label for _k, label in self.SCHEME_CHOICES]))
        self._scheme_row.connect("notify::selected", self._on_scheme_selected)
        group.add(self._scheme_row)

        self._accent_row = Adw.ActionRow(title="Accent color")
        dialog = Gtk.ColorDialog()
        dialog.set_title("Choose accent color")
        dialog.set_with_alpha(False)
        self._accent_btn = Gtk.ColorDialogButton(dialog=dialog, valign=Gtk.Align.CENTER)
        self._accent_btn.connect("notify::rgba", self._on_accent_picked)
        self._accent_clear_btn = Gtk.Button(label="Use theme accent", valign=Gtk.Align.CENTER)
        self._accent_clear_btn.connect("clicked", self._on_accent_cleared)
        self._accent_row.add_suffix(self._accent_clear_btn)
        self._accent_row.add_suffix(self._accent_btn)
        group.add(self._accent_row)

        group.add(self._build_preview())

        reset_row = Adw.ActionRow(title="Reset appearance",
                                  subtitle="Back to the ScreenTime theme, following the system color scheme, theme accent")
        reset_btn = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        reset_btn.connect("clicked", self._on_appearance_reset)
        reset_row.add_suffix(reset_btn)
        group.add(reset_row)

        self.theme_manager.connect_changed(lambda _p: self._sync_appearance_rows())
        self._sync_appearance_rows()
        return group

    def _build_preview(self):
        """A live preview drawn with the same semantic tokens as the rest of the
        app -- it can't disagree with the real UI because it *is* the real widgets."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("st-preview")
        box.set_margin_top(6)
        title = Gtk.Label(label="Preview", xalign=0)
        title.add_css_class("heading")
        box.append(title)

        def swatches(tokens):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for tok in tokens:
                sw = Gtk.Box()
                sw.add_css_class("st-swatch")
                sw.add_css_class("st-bg-" + tok.replace("_", "-"))
                sw.set_tooltip_text(tok.replace("_", " "))
                row.append(sw)
            return row

        box.append(swatches(("accent", "accent_hover", "accent_active", "success", "warning", "error")))
        box.append(swatches(("chart_1", "chart_2", "chart_3", "chart_4", "chart_5", "chart_6")))
        chart = BarChart(height=120)
        chart.set_data(["M", "T", "W", "T", "F", "S", "S"], [3, 5, 2, 6, 4, 1, 2])
        box.append(chart)
        return box

    def _sync_appearance_rows(self):
        """Make the controls reflect the config/theme (used after any change,
        including a reset). Guarded so it doesn't re-trigger the handlers."""
        self._syncing = True
        try:
            theme = self.theme_manager.theme
            self._theme_row.set_selected(next((i for i, t in enumerate(self._themes) if t.id == theme.id), 0))
            self._theme_row.set_subtitle(theme.description)

            keys = [k for k, _ in self.SCHEME_CHOICES]
            # A fixed theme *is* one scheme: show that, not the stored preference
            # (which is kept, and applies again once an adaptive theme is chosen).
            shown = theme.fixed_scheme or self.config.color_scheme
            self._scheme_row.set_selected(keys.index(shown))
            if theme.fixed_scheme:
                self._scheme_row.set_sensitive(False)
                self._scheme_row.set_subtitle(f"Fixed by the {theme.name} theme ({theme.fixed_scheme})")
            else:
                self._scheme_row.set_sensitive(True)
                self._scheme_row.set_subtitle("Light or dark; \"Follow system\" tracks your desktop")

            palette = self.theme_manager.palette
            rgba = Gdk.RGBA()
            rgba.parse(palette.accent)
            self._accent_btn.set_rgba(rgba)
            custom = self.config.accent_color
            self._accent_clear_btn.set_sensitive(bool(custom))
            self._accent_row.set_subtitle(f"Custom ({custom})" if custom else f"From the theme ({palette.accent})")
        finally:
            self._syncing = False

    def _on_theme_selected(self, row, _pspec):
        if self._syncing:
            return
        self.theme_manager.set_theme(self._themes[row.get_selected()].id)

    def _on_scheme_selected(self, row, _pspec):
        if self._syncing:
            return
        self.theme_manager.set_color_scheme(self.SCHEME_CHOICES[row.get_selected()][0])

    def _on_accent_picked(self, btn, _pspec):
        if self._syncing:
            return
        rgba = btn.get_rgba()
        self.theme_manager.set_accent_color(theme_mod.to_hex((rgba.red * 255, rgba.green * 255, rgba.blue * 255)))

    def _on_accent_cleared(self, _btn):
        self.theme_manager.set_accent_color(None)

    def _on_appearance_reset(self, _btn):
        self.theme_manager.reset()

    def _on_idle_changed(self, row, _pspec):
        self.config.set("idle_timeout_seconds", int(row.get_value()))

    def _on_poll_changed(self, row, _pspec):
        self.config.set("poll_interval_seconds", int(row.get_value()))

    def _on_autostart_changed(self, row, _pspec):
        wanted = row.get_active()
        self.config.set("autostart_enabled", "true" if wanted else "false")
        if wanted:
            autostart.enable()
        else:
            autostart.disable()
        self._refresh_startup_rows()

    def refresh(self):
        running = autostart.daemon_is_running()
        self._daemon_row.set_subtitle("Running" if running else "Not running")
        self._daemon_btn.set_label("Stop" if running else "Start")
        self._refresh_backend_rows()
        self._refresh_startup_rows()
        self._refresh_security_rows()

    def _refresh_startup_rows(self):
        st = autostart.get_status(self.db)
        self._installed_row.set_subtitle("Yes" if st.installed else "No \u2014 screentime-daemon was not found")
        if st.enabled:
            how = {"systemd": "systemd user service", "xdg-autostart": "XDG autostart entry"}[st.mechanism]
            self._enabled_row.set_subtitle(f"Yes ({how})")
        else:
            self._enabled_row.set_subtitle("No \u2014 tracking will not resume after you log in again")
        from ... import __version__
        recorded = self.db.get_setting("daemon_version")
        if not recorded:
            self._version_row.set_subtitle(
                "Not recorded yet \u2014 the running daemon is older than this app and will be restarted"
                if st.running else "Not running")
        elif autostart.daemon_is_outdated(recorded, __version__):
            self._version_row.set_subtitle(f"{recorded} \u2014 older than this app ({__version__}); it will be restarted")
        elif recorded != __version__:
            self._version_row.set_subtitle(f"{recorded} (newer than this window, {__version__}; reopen the app)")
        else:
            self._version_row.set_subtitle(recorded)
        def _fmt(key):
            raw = self.db.get_setting(key)
            if raw and raw.isdigit():
                return datetime.datetime.fromtimestamp(int(raw)).strftime("%Y-%m-%d %H:%M:%S")
            return "never"
        self._sleep_row.set_subtitle(f"slept {_fmt('last_suspend')}  \u2022  woke {_fmt('last_resume')}")
        if st.last_start:
            when = datetime.datetime.fromtimestamp(st.last_start).strftime("%Y-%m-%d %H:%M:%S")
            self._last_start_row.set_subtitle(when)
        else:
            self._last_start_row.set_subtitle("Never recorded")

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
