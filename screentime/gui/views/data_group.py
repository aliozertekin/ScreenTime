"""Settings -> Export and backup: plaintext CSV/JSON export, encrypted backup, restore."""
from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ... import backup as backup_mod
from ... import exporter
from ...db import Database
from ...keystore import KeyStoreError
from ...secure_log import SecureStoreError
from .. import dialogs

BACKUP_FILTER = [("ScreenTime backups", ["*.screentime"]), ("All files", ["*"])]


class DataGroup(Adw.PreferencesGroup):
    def __init__(self, db: Database):
        super().__init__(
            title="Export and backup",
            description="Two different things: an export is a readable spreadsheet file; a backup is encrypted "
                        "and is the way to move or restore your history.")
        self.db = db

        exp = Adw.ActionRow(
            title="Export usage data",
            subtitle="Every tracked session as CSV or JSON. NOT encrypted: anyone who gets the file can read "
                     "which applications you used and when. Contains no keys.")
        for fmt in ("CSV", "JSON"):
            b = Gtk.Button(label=fmt, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, f=fmt.lower(): self.start_export(f))
            exp.add_suffix(b)
        self.add(exp)

        bk = Adw.ActionRow(
            title="Export encrypted backup",
            subtitle="A .screentime file protected with your existing key. Opening it later needs your key or "
                     "recovery key. Stays on this computer unless you copy it.")
        b = Gtk.Button(label="Export backup\u2026", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self.start_backup())
        bk.add_suffix(b)
        self.add(bk)

        rs = Adw.ActionRow(
            title="Import backup and restore history",
            subtitle="Adds sessions from a backup that are missing here. Nothing existing is changed or deleted.")
        b = Gtk.Button(label="Import backup\u2026", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: self.start_restore())
        rs.add_suffix(b)
        self.add(rs)

    def _parent(self):
        return self.get_root()

    # ------------------------------------------------------------- export
    def start_export(self, fmt: str):
        dialogs.pick_file(self._parent(), f"Export usage data ({fmt.upper()})", lambda p: self.do_export(fmt, p),
                          save=True, initial_name=exporter.default_filename(fmt))

    def do_export(self, fmt: str, path: str) -> str:
        try:
            n = exporter.export_to_file(self.db, path, fmt, overwrite=True)   # the picker already confirmed replacing
        except (OSError, ValueError) as e:
            msg = f"The file could not be written: {e}"
            dialogs.notice(self._parent(), "Export failed", msg)
            return msg
        msg = f"{n} session(s) written to {path}.\n\nThis file is not encrypted."
        dialogs.notice(self._parent(), "Export finished", msg)
        return msg

    # ------------------------------------------------------------- backup
    def start_backup(self):
        dialogs.pick_file(self._parent(), "Export encrypted backup", self.do_backup, save=True,
                          initial_name=backup_mod.default_filename(), filters=BACKUP_FILTER)

    def do_backup(self, path: str) -> str:
        try:
            info = backup_mod.create_backup(self.db, path, overwrite=True, interactive=True)
        except (backup_mod.BackupError, SecureStoreError, KeyStoreError, OSError) as e:
            msg = f"No backup was created: {e}"
            dialogs.notice(self._parent(), "Backup failed", msg)
            return msg
        msg = (f"Saved and verified: {info.sessions} session(s) from {info.apps} application(s).\n\n{path}\n\n"
               "It is encrypted with your database key. Keep your recovery key safe: without the key or the "
               "recovery key the backup cannot be opened.")
        dialogs.notice(self._parent(), "Backup created", msg)
        return msg

    # ------------------------------------------------------------ restore
    def start_restore(self):
        dialogs.pick_file(self._parent(), "Import backup", lambda p: self.inspect_for_restore(p),
                          filters=BACKUP_FILTER)

    def inspect_for_restore(self, path: str, recovery_key: str | None = None):
        """Validate the whole backup first. Needs the key from the machine that made it, which is found
        automatically here or asked for. Nothing is changed until the user confirms."""
        try:
            info = backup_mod.verify_backup(path, recovery_key=recovery_key, interactive=True)
        except backup_mod.NeedKeyError:
            return self._ask_recovery_key(path)
        except backup_mod.WrongBackupKeyError as e:
            if recovery_key:
                return self._ask_recovery_key(path, str(e))
            dialogs.notice(self._parent(), "Can't open the backup", str(e))
            return None
        except (backup_mod.BackupError, SecureStoreError, KeyStoreError, OSError) as e:
            dialogs.notice(self._parent(), "Can't restore this backup", str(e))
            return None
        span = f"{info.first_day} to {info.last_day}" if info.first_day else "no sessions"
        dialogs.confirm(
            self._parent(), "Restore history?",
            f"{info.sessions} session(s) from {info.apps} application(s), {span}.\n\n"
            "Sessions that are not already here will be added. Your current history is not changed or deleted, "
            "and restoring the same backup again adds nothing.",
            "Restore", lambda: self.do_restore(path, recovery_key))
        return info

    def _ask_recovery_key(self, path: str, error: str = ""):
        entry = Adw.EntryRow(title="Recovery key")
        entry.add_css_class("monospace")
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.append(entry)
        dialogs.confirm(
            self._parent(), "Recovery key needed",
            (error + "\n\n" if error else "") +
            "This backup was made with a key that is not on this computer. Enter the recovery key of the "
            "ScreenTime installation that created it (Settings \u2192 Security \u2192 Show recovery key there).",
            "Continue", lambda: self.inspect_for_restore(path, entry.get_text()), extra=box)

    def do_restore(self, path: str, recovery_key: str | None = None) -> str:
        try:
            res = backup_mod.restore_backup(self.db, path, recovery_key=recovery_key, interactive=True)
        except (backup_mod.BackupError, SecureStoreError, KeyStoreError, OSError) as e:
            msg = f"Nothing was restored: {e}"
            dialogs.notice(self._parent(), "Restore failed", msg)
            return msg
        msg = (f"Added {res.sessions_added} session(s) and {res.apps_added} new application(s). "
               f"{res.sessions_skipped} were already here.")
        dialogs.notice(self._parent(), "History restored", msg)
        return msg
