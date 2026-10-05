"""Typed accessors over the `settings` key/value table, with defaults.
Kept in the DB (not a separate config file) so daemon and GUI always agree
on the current value without needing IPC just to read a setting."""

from __future__ import annotations

from . import theme as _theme
from .db import Database

DEFAULTS = {
    "idle_timeout_seconds": "300",     # 5 minutes
    "poll_interval_seconds": "2",      # how often to check the focused window
    "heartbeat_interval_seconds": "10",  # how often to persist progress (crash-safety granularity)
    "autostart_enabled": "false",
    "minimize_to_tray": "true",
    "update_checks_enabled": "true",    # Settings -> Updates: lets the "Check for updates" button work. Nothing runs by itself.
    # Appearance -- three independent settings (see theme.py):
    "theme": _theme.DEFAULT_THEME_ID,   # a registered theme id
    "color_scheme": "system",           # system | light | dark (only adaptive themes honour it)
    "accent_color": "",                 # "" = use the theme's accent, else '#rrggbb'
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

    # ---- appearance ------------------------------------------------------
    # Getters validate: a stale/garbage stored value (a theme that no longer
    # exists, a hand-edited database) degrades to the default instead of
    # breaking the UI, and is never written back over the user's data.
    @property
    def theme(self) -> str:
        stored = self.db.get_setting("theme", DEFAULTS["theme"])
        return stored if _theme.is_known_theme(stored) else _theme.DEFAULT_THEME_ID

    @property
    def color_scheme(self) -> str:
        stored = self.db.get_setting("color_scheme", DEFAULTS["color_scheme"])
        return stored if stored in _theme.COLOR_SCHEMES else "system"

    @property
    def accent_color(self) -> str:
        return _theme.normalize_hex(self.db.get_setting("accent_color", "")) or ""

    def set_theme(self, theme_id: str):
        if not _theme.is_known_theme(theme_id):
            raise ValueError(f"unknown theme: {theme_id!r}")
        self.set("theme", theme_id)

    def set_color_scheme(self, scheme: str):
        if scheme not in _theme.COLOR_SCHEMES:
            raise ValueError(f"color scheme must be one of {_theme.COLOR_SCHEMES}, got {scheme!r}")
        self.set("color_scheme", scheme)

    def set_accent_color(self, value):
        """'#rrggbb' to override the theme's accent; '' / None to clear it."""
        if not value:
            self.set("accent_color", "")
            return
        norm = _theme.normalize_hex(value)
        if norm is None:
            raise ValueError(f"invalid accent color: {value!r}")
        self.set("accent_color", norm)

    def reset_appearance(self):
        for key in ("theme", "color_scheme", "accent_color"):
            self.set(key, DEFAULTS[key])
