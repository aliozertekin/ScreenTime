"""Typed accessors over the `settings` key/value table, with defaults.
Kept in the DB (not a separate config file) so daemon and GUI always agree
on the current value without needing IPC just to read a setting."""

from __future__ import annotations

from .db import Database

DEFAULTS = {
    "idle_timeout_seconds": "300",     # 5 minutes
    "poll_interval_seconds": "2",      # how often to check the focused window
    "heartbeat_interval_seconds": "10",  # how often to persist progress (crash-safety granularity)
    "autostart_enabled": "false",
    "minimize_to_tray": "true",
}


class Config:
    def __init__(self, db: Database):
        self.db = db
        for k, v in DEFAULTS.items():
            if self.db.get_setting(k) is None:
                self.db.set_setting(k, v)

    def get_int(self, key: str) -> int:
        return int(self.db.get_setting(key, DEFAULTS.get(key, "0")))

    def get_bool(self, key: str) -> bool:
        return self.db.get_setting(key, DEFAULTS.get(key, "false")).lower() in ("1", "true", "yes")

    def set(self, key: str, value):
        self.db.set_setting(key, str(value))

    @property
    def idle_timeout_seconds(self) -> int:
        return self.get_int("idle_timeout_seconds")

    @property
    def poll_interval_seconds(self) -> float:
        return self.get_int("poll_interval_seconds")

    @property
    def heartbeat_interval_seconds(self) -> float:
        return self.get_int("heartbeat_interval_seconds")

    @property
    def autostart_enabled(self) -> bool:
        return self.get_bool("autostart_enabled")

    @property
    def minimize_to_tray(self) -> bool:
        return self.get_bool("minimize_to_tray")
