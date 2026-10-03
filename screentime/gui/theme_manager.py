"""Applies the theme (see screentime/theme.py) to the running GTK4/libadwaita app.

One `ThemeManager` per process. It owns a `Gtk.CssProvider` on the display and
re-applies it whenever the theme/scheme/accent settings change *or the desktop's
own colors change* -- so switching theme in Settings, or switching the Plasma
color scheme, updates the open window immediately with no restart.

Widgets that draw themselves (the bar chart) call `connect_changed()` and read
semantic tokens from the palette; nothing outside this module and theme.py
knows which theme is active.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import theme as theme_mod
from ..config import Config

log = logging.getLogger("screentime.theme")

_manager: Optional["ThemeManager"] = None


def get_theme_manager() -> Optional["ThemeManager"]:
    return _manager


def set_theme_manager(manager: Optional["ThemeManager"]):
    global _manager
    if _manager is not None and _manager is not manager:
        _manager.close()
    _manager = manager


def ensure_theme_manager(config: Config) -> "ThemeManager":
    """Return the process-wide manager, creating it if needed (views can be
    built before/without the application object, e.g. in tests).

    "Same" means same *database*, not same Config object: the app hands each
    component its own cheap Config wrapper over one shared database. A manager
    bound to a different (e.g. closed) database is replaced, never reused."""
    global _manager
    if _manager is None or _manager.config.db is not config.db:
        set_theme_manager(ThemeManager(config))
        _manager.apply()
    return _manager


def _rgba_to_hex(rgba) -> str:
    return theme_mod.to_hex((rgba.red * 255, rgba.green * 255, rgba.blue * 255))


class ThemeManager:
    DEBOUNCE_MS = 200

    def __init__(self, config: Config, style_manager: Optional[Adw.StyleManager] = None,
                 display: Optional[Gdk.Display] = None, kde_paths: Optional[list] = None):
        self.config = config
        self.style_manager = style_manager or Adw.StyleManager.get_default()
        self.display = display if display is not None else Gdk.Display.get_default()
        self._kde_paths = kde_paths            # None => the real kdeglobals locations
        self.palette: theme_mod.Palette = theme_mod.fallback_palette()
        self.css: str = ""
        self.css_errors: list = []
        self._listeners: dict = {}
        self._next_id = 1
        self._applying = False
        self._debounce_id = 0
        self._monitors: list = []
        self._signal_ids: list = []
        self._provider = Gtk.CssProvider()
        self._provider.connect("parsing-error", self._on_css_error)
        if self.display is not None:
            Gtk.StyleContext.add_provider_for_display(
                self.display, self._provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ------------------------------------------------------------ listeners
    def connect_changed(self, callback: Callable[[theme_mod.Palette], None]) -> int:
        hid = self._next_id
        self._next_id += 1
        self._listeners[hid] = callback
        return hid

    def disconnect_changed(self, handler_id: int):
        self._listeners.pop(handler_id, None)

    # ------------------------------------------------------------ settings
    def set_theme(self, theme_id: str):
        self.config.set_theme(theme_id)
        self.apply()

    def set_color_scheme(self, scheme: str):
        self.config.set_color_scheme(scheme)
        self.apply()

    def set_accent_color(self, value: Optional[str]):
        self.config.set_accent_color(value)
        self.apply()

    def reset(self):
        self.config.reset_appearance()
        self.apply()

    @property
    def theme(self) -> theme_mod.ThemeDefinition:
        return theme_mod.get_theme(self.config.theme)

    # ------------------------------------------------------------- applying
    def _css_variables_supported(self) -> bool:
        # CSS custom properties: GTK >= 4.16 (libadwaita >= 1.6). On older GTK
        # they'd be a parse error, so only emit them where they exist.
        return (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 16)

    def _system_accent(self) -> Optional[str]:
        sm = self.style_manager
        try:
            if sm.get_system_supports_accent_colors():           # libadwaita >= 1.6
                return _rgba_to_hex(sm.get_accent_color_rgba())
        except AttributeError:
            pass                                                  # older libadwaita: no accent API
        return None

    def collect_system_info(self, read_system_scheme: bool) -> theme_mod.SystemInfo:
        info = theme_mod.SystemInfo(accent=self._system_accent())
        if self.theme.is_system:
            info.kde = theme_mod.load_kde_colors(self._kde_paths)
        if read_system_scheme:
            # With the scheme left at DEFAULT, get_dark() is the *system's*
            # preference; once we force a scheme it would just echo our choice.
            self.style_manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
            info.is_dark = self.style_manager.get_dark()
        return info

    def apply(self) -> theme_mod.Palette:
        if self._applying:
            return self.palette
        self._applying = True
        try:
            theme = self.theme
            scheme = self.config.color_scheme
            follows_system = (not theme.fixed_scheme) and scheme == "system"
            info = self.collect_system_info(read_system_scheme=follows_system)
            palette = theme_mod.resolve_palette(theme.id, scheme, self.config.accent_color or None, info)

            # Keep libadwaita's own (non-overridden) drawing -- shadows, focus
            # rings, symbolic icon tinting -- on the same side as our palette.
            if theme.fixed_scheme or palette.source == "kde" or scheme != "system":
                target = Adw.ColorScheme.FORCE_DARK if palette.is_dark else Adw.ColorScheme.FORCE_LIGHT
            else:
                target = Adw.ColorScheme.DEFAULT
            self.style_manager.set_color_scheme(target)

            self.palette = palette
            self.css = theme_mod.build_css(palette, css_variables=self._css_variables_supported())
            self.css_errors.clear()
            if hasattr(self._provider, "load_from_string"):       # GTK >= 4.12
                self._provider.load_from_string(self.css)
            else:
                self._provider.load_from_data(self.css.encode())
        finally:
            self._applying = False
        for cb in list(self._listeners.values()):
            try:
                cb(self.palette)
            except Exception:
                log.exception("theme listener failed")
        return self.palette

    def _on_css_error(self, _provider, section, error):
        msg = f"{error.message} (line {section.get_start_location().lines + 1})"
        self.css_errors.append(msg)
        log.warning("theme CSS problem: %s", msg)

    # ------------------------------------------------------------- watching
    def start_watching(self):
        """Re-apply when the desktop's colors change underneath us."""
        sm = self.style_manager
        for prop in ("notify::dark", "notify::accent-color-rgba"):
            try:
                self._signal_ids.append(sm.connect(prop, self._on_system_changed))
            except (TypeError, ValueError):
                pass                                              # property absent on this libadwaita
        paths = self._kde_paths if self._kde_paths is not None else theme_mod.kde_config_paths()
        for path in paths:
            try:
                mon = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
                mon.connect("changed", self._on_system_changed)
                self._monitors.append(mon)
            except GLib.Error as e:
                log.debug("cannot monitor %s: %s", path, e)

    def _on_system_changed(self, *_args):
        if self._applying:
            return
        # Only matters if our colors depend on the desktop.
        if not (self.theme.is_system or (not self.theme.fixed_scheme and self.config.color_scheme == "system")):
            return
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(self.DEBOUNCE_MS, self._debounced_apply)

    def _debounced_apply(self) -> bool:
        self._debounce_id = 0
        self.apply()
        return False

    def close(self):
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
            self._debounce_id = 0
        for sid in self._signal_ids:
            self.style_manager.disconnect(sid)
        self._signal_ids.clear()
        for mon in self._monitors:
            mon.cancel()
        self._monitors.clear()
        if self.display is not None:
            Gtk.StyleContext.remove_provider_for_display(self.display, self._provider)
        self._listeners.clear()
