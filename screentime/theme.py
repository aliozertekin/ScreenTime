"""Theme system: semantic color tokens, built-in themes, KDE color mapping and
CSS generation. Pure Python -- no GTK import -- so all of it is unit-testable;
`screentime/gui/theme_manager.py` is the thin layer that applies it live.

Concepts (these are three separate settings, and the UI keeps them separate):

* **Theme**        -- a named palette family ("default", "gruvbox-dark", "system", ...).
* **Color scheme** -- light / dark / follow-system. Only *adaptive* themes (ones
                      that ship both variants: "default", "system") honour it;
                      fixed themes ("gruvbox-dark", "dark", ...) *are* a scheme.
* **Accent color** -- optional user override of the theme's accent.

Views/widgets never know which theme is active: they consume semantic tokens
(`Palette.accent`, `Palette.muted_foreground`, ...) or the generated CSS
classes (`st-bg-<token>`, `st-fg-<token>`, `st-app-row`, ...).

Adding a theme = one `register_theme(ThemeDefinition(...))` call with a dict of
base colors; every missing token is derived (see `complete_palette`).
"""
from __future__ import annotations

import colorsys
import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------- tokens
TOKENS = (
    "background", "surface", "surface_alt",
    "foreground", "muted_foreground",
    "accent", "accent_hover", "accent_active", "accent_foreground", "accent_text",
    "border", "success", "warning", "error",
    "selected_bg", "selected_fg",
    "headerbar_bg", "headerbar_fg", "sidebar_bg", "sidebar_fg",
    "row_bg", "row_hover",
    "chart_1", "chart_2", "chart_3", "chart_4", "chart_5", "chart_6",
)
COLOR_SCHEMES = ("system", "light", "dark")
DEFAULT_THEME_ID = "default"
SYSTEM_THEME_ID = "system"

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# ---------------------------------------------------------------- color math
RGB = tuple  # (r, g, b) each 0..255


def parse_hex(value: Optional[str]) -> Optional[RGB]:
    """'#rgb' / '#rrggbb' -> (r,g,b), else None. Never raises."""
    if not isinstance(value, str) or not _HEX_RE.match(value.strip()):
        return None
    h = value.strip()[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def normalize_hex(value: Optional[str]) -> Optional[str]:
    rgb = parse_hex(value)
    return to_hex(rgb) if rgb else None


def to_hex(rgb: RGB) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(a: RGB, b: RGB, t: float) -> RGB:
    """a*(1-t) + b*t"""
    return tuple(a[i] * (1 - t) + b[i] * t for i in range(3))


def luminance(rgb: RGB) -> float:
    """WCAG relative luminance."""
    def chan(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: RGB, b: RGB) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


WHITE: RGB = (255, 255, 255)
BLACK: RGB = (0, 0, 0)
_PREFER_WHITE_MIN = 3.5   # white on the accent if it reads at least this well (Adwaita's own blue is ~4.0)


def readable_on(bg: RGB) -> RGB:
    """Text color for a filled surface: white when that reads acceptably (keeps
    the familiar look on mid-tone accents), otherwise whichever of black/white
    contrasts more."""
    if contrast_ratio(WHITE, bg) >= _PREFER_WHITE_MIN:
        return WHITE
    return WHITE if contrast_ratio(WHITE, bg) >= contrast_ratio(BLACK, bg) else BLACK


def ensure_contrast(fg: RGB, bg: RGB, minimum: float) -> RGB:
    """Nudge `fg` toward black/white (whichever helps) until it reads on `bg`."""
    if contrast_ratio(fg, bg) >= minimum:
        return fg
    target = WHITE if luminance(bg) < 0.5 else BLACK
    for step in range(1, 21):
        cand = mix(fg, target, step / 20)
        if contrast_ratio(cand, bg) >= minimum:
            return cand
    return target


def rotate_hue(rgb: RGB, degrees: float, sat: Optional[tuple] = None, light: Optional[float] = None) -> RGB:
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    h = (h + degrees / 360.0) % 1.0
    if sat is not None:
        s = max(sat[0], min(sat[1], s))
    if light is not None:
        l = light
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return (r * 255, g * 255, b * 255)


# ------------------------------------------------------------------- palette
@dataclass(frozen=True)
class Palette:
    """Fully resolved colors (all '#rrggbb'), plus which theme/scheme produced them."""
    theme_id: str
    is_dark: bool
    colors: dict = field(default_factory=dict)
    source: str = "theme"      # "theme" | "kde" (built from the desktop's KDE color scheme)

    def __getattr__(self, name):          # palette.accent, palette.chart_1, ...
        try:
            return self.__dict__["colors"][name]
        except KeyError:
            raise AttributeError(name) from None

    def chart_colors(self) -> list:
        return [self.colors[f"chart_{i}"] for i in range(1, 7)]

    def rgb(self, token: str) -> RGB:
        return parse_hex(self.colors[token])


def complete_palette(spec: dict, is_dark: bool, theme_id: str = "custom", source: str = "theme") -> Palette:
    """Fill every token from a *partial* spec. Required: background, foreground,
    accent (surface defaults to background). Everything else is derived, so a
    new theme only has to say what makes it distinctive."""
    c: dict = {}

    def rgb(key, default=None):
        v = parse_hex(spec.get(key))
        return v if v else default

    bg = rgb("background")
    fg = rgb("foreground")
    accent = rgb("accent")
    if bg is None or fg is None or accent is None:
        raise ValueError("palette spec needs valid background, foreground and accent")
    surface = rgb("surface", bg)

    def put(key, value):
        c[key] = to_hex(value)

    put("background", bg)
    put("surface", surface)
    put("surface_alt", rgb("surface_alt", mix(surface, fg, 0.05)))
    put("foreground", fg)
    put("muted_foreground", rgb("muted_foreground", ensure_contrast(mix(fg, bg, 0.42), bg, 3.0)))
    put("accent", accent)
    put("accent_foreground", rgb("accent_foreground", readable_on(accent)))
    put("accent_hover", rgb("accent_hover", mix(accent, WHITE, 0.12) if is_dark else mix(accent, BLACK, 0.08)))
    put("accent_active", rgb("accent_active", mix(accent, BLACK, 0.15 if is_dark else 0.18)))
    put("accent_text", rgb("accent_text", ensure_contrast(accent, bg, 4.5)))
    put("border", rgb("border", mix(bg, fg, 0.18)))
    put("success", rgb("success", (38, 162, 105) if not is_dark else (87, 227, 137)))
    put("warning", rgb("warning", (229, 165, 10) if not is_dark else (248, 228, 92)))
    put("error", rgb("error", (224, 27, 36) if not is_dark else (255, 123, 99)))
    put("selected_bg", rgb("selected_bg", accent))
    put("selected_fg", rgb("selected_fg", rgb("accent_foreground", readable_on(rgb("selected_bg", accent)))))
    put("headerbar_bg", rgb("headerbar_bg", mix(bg, fg, 0.04)))
    put("headerbar_fg", rgb("headerbar_fg", fg))
    put("sidebar_bg", rgb("sidebar_bg", mix(bg, fg, 0.03)))
    put("sidebar_fg", rgb("sidebar_fg", fg))
    put("row_bg", rgb("row_bg", surface))
    put("row_hover", rgb("row_hover", mix(surface, accent, 0.14)))
    # Chart palette: chart_1 is always the accent; the rest step around the hue
    # wheel unless the theme lists its own.
    put("chart_1", rgb("chart_1", accent))
    for i, deg in zip(range(2, 7), (150, 60, 210, 285, 100)):
        put(f"chart_{i}", rgb(f"chart_{i}", rotate_hue(accent, deg, sat=(0.5, 0.75),
                                                        light=0.62 if is_dark else 0.42)))
    return Palette(theme_id=theme_id, is_dark=is_dark, colors=c, source=source)


# -------------------------------------------------------------------- themes
@dataclass(frozen=True)
class ThemeDefinition:
    id: str
    name: str
    description: str
    # {"light": spec, "dark": spec}. Both present => adaptive (honours the color
    # scheme setting); exactly one => the theme *is* that scheme.
    variants: dict = field(default_factory=dict)
    is_system: bool = False        # palette comes from the desktop (see resolve_palette)

    @property
    def adaptive(self) -> bool:
        return self.is_system or ("light" in self.variants and "dark" in self.variants)

    @property
    def fixed_scheme(self) -> Optional[str]:
        if self.adaptive:
            return None
        return next(iter(self.variants))


_THEMES: dict = {}


def register_theme(theme: ThemeDefinition) -> ThemeDefinition:
    """Add a theme. Validates every variant up front so a bad theme fails at
    registration, not when a user selects it."""
    for scheme, spec in theme.variants.items():
        if scheme not in ("light", "dark"):
            raise ValueError(f"theme {theme.id!r}: variant must be 'light' or 'dark'")
        complete_palette(spec, scheme == "dark", theme.id)
    if not theme.variants and not theme.is_system:
        raise ValueError(f"theme {theme.id!r} has no colors")
    _THEMES[theme.id] = theme
    return theme


def list_themes() -> list:
    return list(_THEMES.values())


def get_theme(theme_id: Optional[str]) -> ThemeDefinition:
    """Unknown/removed/garbage ids fall back to the default theme -- a stale
    setting must never break the UI."""
    return _THEMES.get(theme_id or "", _THEMES[DEFAULT_THEME_ID])


def is_known_theme(theme_id: Optional[str]) -> bool:
    return (theme_id or "") in _THEMES


# ---- built-in themes -------------------------------------------------------
_DEFAULT_LIGHT = {
    "background": "#f6f5f4", "surface": "#ffffff", "foreground": "#241f31",
    "muted_foreground": "#5e5c64", "accent": "#3584e4", "border": "#d8d4d0",
    "success": "#1a7f4b", "warning": "#b56c00", "error": "#c01c28",
    "chart_2": "#33a06f", "chart_3": "#e66100", "chart_4": "#9141ac", "chart_5": "#2190a4", "chart_6": "#c88800",
}
_DEFAULT_DARK = {
    "background": "#242424", "surface": "#303030", "foreground": "#ebebeb",
    "muted_foreground": "#9a9996", "accent": "#3584e4", "border": "#454545",
    "success": "#57e389", "warning": "#f8e45c", "error": "#ff7b63",
    "chart_2": "#57e389", "chart_3": "#ff9c4a", "chart_4": "#dc8add", "chart_5": "#5bc8d8", "chart_6": "#f5c211",
}

register_theme(ThemeDefinition(
    "system", "System colors",
    "Uses the desktop's colors: KDE Plasma's color scheme and accent where available, "
    "otherwise the system light/dark preference and accent color.",
    variants={"light": _DEFAULT_LIGHT, "dark": _DEFAULT_DARK},   # used when the desktop gives us nothing
    is_system=True,
))
register_theme(ThemeDefinition(
    "default", "ScreenTime",
    "The default ScreenTime look. Follows the light/dark color scheme setting.",
    variants={"light": _DEFAULT_LIGHT, "dark": _DEFAULT_DARK},
))
register_theme(ThemeDefinition(
    "light", "Light", "A neutral light theme.",
    variants={"light": {
        "background": "#fafafa", "surface": "#ffffff", "foreground": "#1a1a1a",
        "muted_foreground": "#595959", "accent": "#0b6cf0", "border": "#dcdcdc",
        "success": "#137a3f", "warning": "#a15c00", "error": "#c4161c",
        "chart_2": "#1f9d55", "chart_3": "#e8590c", "chart_4": "#7048e8", "chart_5": "#0c8599", "chart_6": "#c2900a",
    }},
))
register_theme(ThemeDefinition(
    "dark", "Dark", "A neutral dark theme.",
    variants={"dark": {
        "background": "#121212", "surface": "#1e1e1e", "foreground": "#e8e8e8",
        "muted_foreground": "#a0a0a0", "accent": "#4a90f2", "border": "#333333",
        "success": "#5ad08a", "warning": "#f2c94c", "error": "#ff6b6b",
        "chart_2": "#5ad08a", "chart_3": "#ff922b", "chart_4": "#b197fc", "chart_5": "#4dd0e1", "chart_6": "#f2c94c",
    }},
))
# Gruvbox (https://github.com/morhetz/gruvbox): canonical hard/medium palette values.
register_theme(ThemeDefinition(
    "gruvbox-dark", "Gruvbox Dark", "Retro-groove dark palette (medium contrast).",
    variants={"dark": {
        "background": "#282828", "surface": "#3c3836", "surface_alt": "#504945",
        "foreground": "#ebdbb2", "muted_foreground": "#a89984",
        "accent": "#fe8019", "accent_foreground": "#282828",
        "border": "#665c54",
        "success": "#b8bb26", "warning": "#fabd2f", "error": "#fb4934",
        "headerbar_bg": "#1d2021", "sidebar_bg": "#1d2021",
        "chart_2": "#83a598", "chart_3": "#b8bb26", "chart_4": "#d3869b", "chart_5": "#8ec07c", "chart_6": "#fabd2f",
    }},
))
register_theme(ThemeDefinition(
    "gruvbox-light", "Gruvbox Light", "Retro-groove light palette (medium contrast).",
    variants={"light": {
        "background": "#fbf1c7", "surface": "#f2e5bc", "surface_alt": "#ebdbb2",
        "foreground": "#3c3836", "muted_foreground": "#7c6f64",
        "accent": "#af3a03", "accent_foreground": "#fbf1c7",
        "border": "#d5c4a1",
        "success": "#79740e", "warning": "#b57614", "error": "#9d0006",
        "headerbar_bg": "#ebdbb2", "sidebar_bg": "#ebdbb2",
        "chart_2": "#076678", "chart_3": "#79740e", "chart_4": "#8f3f71", "chart_5": "#427b58", "chart_6": "#b57614",
    }},
))


def _theme(tid, name, desc, scheme, spec):
    register_theme(ThemeDefinition(tid, name, desc, variants={scheme: spec}))


# Well-known community palettes, using each project's published values. Where a
# published pairing falls below the readability floors enforced by the tests
# (Solarized: body text on the card color, dim text), the text color is nudged
# just far enough to pass, and the card color is lightened -- accents and the
# rest of the palette are unchanged.
_theme("nord", "Nord", "Arctic, north-bluish dark palette.", "dark", {
    "background": "#2e3440", "surface": "#3b4252", "surface_alt": "#434c5e",
    "foreground": "#d8dee9", "muted_foreground": "#8f99ad", "accent": "#88c0d0", "accent_foreground": "#2e3440",
    "border": "#4c566a", "success": "#a3be8c", "warning": "#ebcb8b", "error": "#bf616a",
    "headerbar_bg": "#272c36", "sidebar_bg": "#272c36",
    "chart_2": "#a3be8c", "chart_3": "#ebcb8b", "chart_4": "#b48ead", "chart_5": "#d08770", "chart_6": "#81a1c1"})
_theme("dracula", "Dracula", "Dark theme with vivid purple and pink.", "dark", {
    "background": "#282a36", "surface": "#343746", "surface_alt": "#44475a",
    "foreground": "#f8f8f2", "muted_foreground": "#9aa5d6", "accent": "#bd93f9", "accent_foreground": "#282a36",
    "border": "#44475a", "success": "#50fa7b", "warning": "#ffb86c", "error": "#ff5555",
    "headerbar_bg": "#21222c", "sidebar_bg": "#21222c",
    "chart_2": "#50fa7b", "chart_3": "#ffb86c", "chart_4": "#ff79c6", "chart_5": "#8be9fd", "chart_6": "#f1fa8c"})
_theme("solarized-dark", "Solarized Dark", "Precision colors for machines and people (dark).", "dark", {
    "background": "#002b36", "surface": "#073642", "surface_alt": "#0d4150",
    "foreground": "#93a1a1", "muted_foreground": "#7a9094", "accent": "#268bd2", "accent_foreground": "#ffffff",
    "border": "#27505c", "success": "#859900", "warning": "#b58900", "error": "#dc322f",
    "headerbar_bg": "#00222b", "sidebar_bg": "#00222b",
    "chart_2": "#859900", "chart_3": "#cb4b16", "chart_4": "#d33682", "chart_5": "#2aa198", "chart_6": "#6c71c4"})
_theme("solarized-light", "Solarized Light", "Precision colors for machines and people (light).", "light", {
    "background": "#fdf6e3", "surface": "#f5eedb", "surface_alt": "#eee8d5",
    "foreground": "#3f565e", "muted_foreground": "#657b83", "accent": "#268bd2", "accent_foreground": "#ffffff",
    "border": "#d9d2bd", "success": "#6f7d00", "warning": "#946f00", "error": "#c4251f",
    "headerbar_bg": "#eee8d5", "sidebar_bg": "#eee8d5",
    "chart_2": "#859900", "chart_3": "#cb4b16", "chart_4": "#d33682", "chart_5": "#2aa198", "chart_6": "#6c71c4"})
_theme("catppuccin-mocha", "Catppuccin Mocha", "Soothing pastel dark palette.", "dark", {
    "background": "#1e1e2e", "surface": "#313244", "surface_alt": "#45475a",
    "foreground": "#cdd6f4", "muted_foreground": "#a6adc8", "accent": "#cba6f7", "accent_foreground": "#1e1e2e",
    "border": "#45475a", "success": "#a6e3a1", "warning": "#f9e2af", "error": "#f38ba8",
    "headerbar_bg": "#181825", "sidebar_bg": "#181825",
    "chart_2": "#89b4fa", "chart_3": "#a6e3a1", "chart_4": "#fab387", "chart_5": "#f5c2e7", "chart_6": "#94e2d5"})
_theme("catppuccin-latte", "Catppuccin Latte", "Soothing pastel light palette.", "light", {
    "background": "#eff1f5", "surface": "#e6e9ef", "surface_alt": "#dce0e8",
    "foreground": "#4c4f69", "muted_foreground": "#5c5f77", "accent": "#8839ef", "accent_foreground": "#eff1f5",
    "border": "#bcc0cc", "success": "#40a02b", "warning": "#df8e1d", "error": "#d20f39",
    "headerbar_bg": "#dce0e8", "sidebar_bg": "#e6e9ef",
    "chart_2": "#1e66f5", "chart_3": "#40a02b", "chart_4": "#fe640b", "chart_5": "#ea76cb", "chart_6": "#179299"})
_theme("tokyo-night", "Tokyo Night", "A clean dark theme inspired by Tokyo at night.", "dark", {
    "background": "#1a1b26", "surface": "#24283b", "surface_alt": "#292e42",
    "foreground": "#c0caf5", "muted_foreground": "#9aa5ce", "accent": "#7aa2f7", "accent_foreground": "#1a1b26",
    "border": "#3b4261", "success": "#9ece6a", "warning": "#e0af68", "error": "#f7768e",
    "headerbar_bg": "#16161e", "sidebar_bg": "#16161e",
    "chart_2": "#9ece6a", "chart_3": "#e0af68", "chart_4": "#bb9af7", "chart_5": "#7dcfff", "chart_6": "#ff9e64"})
_theme("one-dark", "One Dark", "Atom's classic dark theme.", "dark", {
    "background": "#282c34", "surface": "#2c313a", "surface_alt": "#3e4451",
    "foreground": "#abb2bf", "muted_foreground": "#8b93a1", "accent": "#61afef", "accent_foreground": "#282c34",
    "border": "#3e4451", "success": "#98c379", "warning": "#e5c07b", "error": "#e06c75",
    "headerbar_bg": "#21252b", "sidebar_bg": "#21252b",
    "chart_2": "#98c379", "chart_3": "#e5c07b", "chart_4": "#c678dd", "chart_5": "#56b6c2", "chart_6": "#d19a66"})
_theme("rose-pine", "Rose Pine", "All natural pine, faux fur and a bit of soho vibes (dark).", "dark", {
    "background": "#191724", "surface": "#1f1d2e", "surface_alt": "#26233a",
    "foreground": "#e0def4", "muted_foreground": "#908caa", "accent": "#c4a7e7", "accent_foreground": "#191724",
    "border": "#403d52", "success": "#9ccfd8", "warning": "#f6c177", "error": "#eb6f92",
    "headerbar_bg": "#14121f", "sidebar_bg": "#14121f",
    "chart_2": "#9ccfd8", "chart_3": "#f6c177", "chart_4": "#eb6f92", "chart_5": "#ebbcba", "chart_6": "#31748f"})
_theme("rose-pine-dawn", "Rose Pine Dawn", "All natural pine, faux fur and a bit of soho vibes (light).", "light", {
    "background": "#faf4ed", "surface": "#fffaf3", "surface_alt": "#f2e9e1",
    "foreground": "#575279", "muted_foreground": "#6e6a86", "accent": "#907aa9", "accent_foreground": "#ffffff",
    "border": "#dfdad9", "success": "#286983", "warning": "#b87514", "error": "#b4637a",
    "headerbar_bg": "#f2e9e1", "sidebar_bg": "#f2e9e1",
    "chart_2": "#286983", "chart_3": "#ea9d34", "chart_4": "#b4637a", "chart_5": "#d7827e", "chart_6": "#56949f"})
# Accessibility: maximum contrast (tests hold these to a 7:1 floor, WCAG AAA).
_theme("high-contrast-dark", "High Contrast Dark", "Maximum contrast on black, for low vision.", "dark", {
    "background": "#000000", "surface": "#0a0a0a", "surface_alt": "#1a1a1a",
    "foreground": "#ffffff", "muted_foreground": "#d0d0d0", "accent": "#ffd60a", "accent_foreground": "#000000",
    "border": "#ffffff", "success": "#4cff7a", "warning": "#ffd60a", "error": "#ff6b6b",
    "chart_2": "#4cff7a", "chart_3": "#ff9f43", "chart_4": "#d28aff", "chart_5": "#4dd8ff", "chart_6": "#ff6bd5"})
_theme("high-contrast-light", "High Contrast Light", "Maximum contrast on white, for low vision.", "light", {
    "background": "#ffffff", "surface": "#ffffff", "surface_alt": "#ececec",
    "foreground": "#000000", "muted_foreground": "#333333", "accent": "#0040c0", "accent_foreground": "#ffffff",
    "border": "#000000", "success": "#006b2c", "warning": "#7a4a00", "error": "#b00020",
    "chart_2": "#006b2c", "chart_3": "#a84a00", "chart_4": "#6a1b9a", "chart_5": "#00607a", "chart_6": "#8a0050"})


# ---------------------------------------------------------------- system info
@dataclass
class SystemInfo:
    """What the desktop tells us. Collected by the GTK layer; consumed here."""
    is_dark: Optional[bool] = None       # system light/dark preference, if known
    accent: Optional[str] = None         # system accent color '#rrggbb', if the platform exposes one
    kde: Optional[dict] = None           # merged kdeglobals: {section: {key: value}}


# ------------------------------------------------------------------- KDE colors
# Which KDE setting feeds which token. Tokens not listed are *derived* from the
# ones that are (so a KDE scheme always yields a complete, consistent palette).
# Kept as data so the README table and a test can't drift from the code.
KDE_MAPPING = {
    "background":        "[Colors:Window] BackgroundNormal",
    "surface":           "[Colors:View] BackgroundNormal",
    "surface_alt":       "[Colors:View] BackgroundAlternate",
    "foreground":        "[Colors:Window] ForegroundNormal",
    "muted_foreground":  "[Colors:Window] ForegroundInactive",
    "accent":            "[General] AccentColor, else [Colors:Selection] BackgroundNormal",
    "accent_foreground": "[Colors:Selection] ForegroundNormal (when accent is the selection color), else derived",
    "accent_hover":      "[Colors:Window] DecorationHover (when accent is the selection color), else derived",
    "accent_active":     "derived from accent",
    "accent_text":       "derived from accent for contrast on the background",
    "border":            "derived (mix of foreground and background)",
    "success":           "[Colors:View] ForegroundPositive",
    "warning":           "[Colors:View] ForegroundNeutral",
    "error":             "[Colors:View] ForegroundNegative",
    "selected_bg":       "[Colors:Selection] BackgroundNormal",
    "selected_fg":       "[Colors:Selection] ForegroundNormal",
    "headerbar_bg":      "[Colors:Header] BackgroundNormal (falls back to Window)",
    "headerbar_fg":      "[Colors:Header] ForegroundNormal (falls back to Window)",
    "sidebar_bg":        "[Colors:Window] BackgroundAlternate (falls back to derived)",
    "sidebar_fg":        "[Colors:Window] ForegroundNormal",
    "row_bg":            "same as surface",
    "row_hover":         "derived (surface tinted with the accent)",
    "chart_1":           "accent",
    "chart_2":           "[Colors:View] ForegroundPositive",
    "chart_3":           "[Colors:View] ForegroundNeutral",
    "chart_4":           "[Colors:View] ForegroundNegative",
    "chart_5":           "[Colors:View] ForegroundLink",
    "chart_6":           "[Colors:View] ForegroundVisited",
}

_KDE_RGB_RE = re.compile(r"^\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*\d{1,3})?\s*$")


def parse_kde_color(value: Optional[str]) -> Optional[RGB]:
    """KDE stores colors as 'R,G,B' (optionally with alpha); accept '#hex' too."""
    if not isinstance(value, str):
        return None
    m = _KDE_RGB_RE.match(value)
    if m:
        rgb = tuple(int(x) for x in m.groups())
        return rgb if all(0 <= c <= 255 for c in rgb) else None
    return parse_hex(value)


def parse_kdeglobals(text: str) -> dict:
    """Parse KDE's INI. Tolerant: garbage in -> {} out."""
    parser = configparser.RawConfigParser(strict=False, interpolation=None, delimiters=("=",),
                                          comment_prefixes=("#", ";"), inline_comment_prefixes=None)
    parser.optionxform = str                         # KDE keys are case-sensitive
    try:
        parser.read_string(text)
    except (configparser.Error, ValueError):
        return {}
    return {sec: dict(parser.items(sec)) for sec in parser.sections()}


def kde_config_paths(env: Optional[dict] = None, home: Optional[Path] = None) -> list:
    """kdeglobals files, highest priority first: the user's, then each
    XDG_CONFIG_DIRS entry (Plasma puts defaults in ~/.config/kdedefaults), then /etc/xdg."""
    env = os.environ if env is None else env
    home = home or Path.home()
    user = Path(env.get("XDG_CONFIG_HOME") or (home / ".config")) / "kdeglobals"
    dirs = [Path(d) / "kdeglobals" for d in (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d]
    out, seen = [], set()
    for p in [user, *dirs]:
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def load_kde_colors(paths: Optional[list] = None) -> Optional[dict]:
    """Merge kdeglobals files (earlier paths win per key). None if no file has
    a usable Window color pair -- i.e. not a KDE session / no scheme applied."""
    merged: dict = {}
    for path in reversed(paths if paths is not None else kde_config_paths()):
        try:
            data = parse_kdeglobals(Path(path).read_text(errors="replace"))
        except OSError:
            continue
        for sec, kv in data.items():
            merged.setdefault(sec, {}).update(kv)
    win = merged.get("Colors:Window", {})
    if parse_kde_color(win.get("BackgroundNormal")) and parse_kde_color(win.get("ForegroundNormal")):
        return merged
    return None


def kde_palette_spec(kde: dict) -> Optional[tuple]:
    """kdeglobals -> (partial palette spec, is_dark). See KDE_MAPPING."""
    def get(section, key, *fallbacks):
        for sec in (section, *fallbacks):
            v = parse_kde_color(kde.get(sec, {}).get(key))
            if v:
                return v
        return None

    bg = get("Colors:Window", "BackgroundNormal")
    fg = get("Colors:Window", "ForegroundNormal")
    if not bg or not fg:
        return None
    view_bg = get("Colors:View", "BackgroundNormal") or bg
    sel_bg = get("Colors:Selection", "BackgroundNormal")
    sel_fg = get("Colors:Selection", "ForegroundNormal")
    general_accent = parse_kde_color(kde.get("General", {}).get("AccentColor"))
    accent = general_accent or sel_bg or get("Colors:Window", "DecorationFocus") or (53, 132, 228)

    spec = {"background": to_hex(bg), "foreground": to_hex(fg), "surface": to_hex(view_bg), "accent": to_hex(accent)}

    def opt(token, value):
        if value:
            spec[token] = to_hex(value)

    opt("surface_alt", get("Colors:View", "BackgroundAlternate", "Colors:Window"))
    opt("muted_foreground", get("Colors:Window", "ForegroundInactive", "Colors:View"))
    opt("success", get("Colors:View", "ForegroundPositive", "Colors:Window"))
    opt("warning", get("Colors:View", "ForegroundNeutral", "Colors:Window"))
    opt("error", get("Colors:View", "ForegroundNegative", "Colors:Window"))
    opt("selected_bg", sel_bg)
    opt("selected_fg", sel_fg)
    opt("headerbar_bg", get("Colors:Header", "BackgroundNormal", "Colors:Window"))
    opt("headerbar_fg", get("Colors:Header", "ForegroundNormal", "Colors:Window"))
    opt("sidebar_bg", get("Colors:Window", "BackgroundAlternate"))
    opt("sidebar_fg", fg)
    if sel_bg and accent == sel_bg:
        # The accent *is* the selection color: KDE's own text/hover colors for it apply.
        opt("accent_foreground", sel_fg)
        opt("accent_hover", get("Colors:Window", "DecorationHover"))
    opt("chart_2", get("Colors:View", "ForegroundPositive"))
    opt("chart_3", get("Colors:View", "ForegroundNeutral"))
    opt("chart_4", get("Colors:View", "ForegroundNegative"))
    opt("chart_5", get("Colors:View", "ForegroundLink"))
    opt("chart_6", get("Colors:View", "ForegroundVisited"))
    return spec, luminance(bg) < 0.5


# ------------------------------------------------------------------ resolution
def resolve_palette(theme_id: Optional[str], color_scheme: Optional[str],
                    accent_override: Optional[str], system: Optional[SystemInfo] = None) -> Palette:
    """The one place the three settings + the desktop's state become colors."""
    system = system or SystemInfo()
    theme = get_theme(theme_id)
    scheme = color_scheme if color_scheme in COLOR_SCHEMES else "system"
    spec: Optional[dict] = None
    is_dark: bool
    source = "theme"

    if theme.fixed_scheme:                                  # e.g. Gruvbox Dark: the theme *is* the scheme
        is_dark = theme.fixed_scheme == "dark"
        spec = dict(theme.variants[theme.fixed_scheme])
    else:
        kde = None
        if theme.is_system and scheme == "system" and system.kde:
            kde = kde_palette_spec(system.kde)
        if kde:
            spec, is_dark = kde[0], kde[1]
            source = "kde"
        else:
            # Adaptive theme, or System theme with nothing usable from the desktop /
            # an explicit light/dark override: the variant for the effective scheme.
            is_dark = (system.is_dark is True) if scheme == "system" else scheme == "dark"
            spec = dict(theme.variants["dark" if is_dark else "light"])
            if theme.is_system and system.accent:
                spec["accent"] = system.accent               # portal / libadwaita accent color
                for k in ("accent_foreground", "accent_hover", "accent_active", "chart_1", "selected_bg"):
                    spec.pop(k, None)

    custom = normalize_hex(accent_override)
    if custom:
        spec["accent"] = custom
        # Everything derived from the accent must follow it, not the theme's old one.
        for k in ("accent_foreground", "accent_hover", "accent_active", "accent_text",
                  "selected_bg", "selected_fg", "chart_1", "row_hover"):
            spec.pop(k, None)
    return complete_palette(spec, is_dark, theme.id, source)


# ------------------------------------------------------------------------ CSS
def _adw_colors(p: Palette) -> dict:
    """libadwaita named colors -> values. These recolor every stock widget
    (headerbar, sidebar, cards, buttons, switches, progress bars...)."""
    bg, fg = p.rgb("background"), p.rgb("foreground")
    dark = p.is_dark

    def fill_fg(token):
        return to_hex(readable_on(p.rgb(token)))

    def text_on_bg(token):
        return to_hex(ensure_contrast(p.rgb(token), bg, 4.5))

    return {
        "window_bg_color": p.background, "window_fg_color": p.foreground,
        "view_bg_color": p.surface, "view_fg_color": p.foreground,
        "accent_bg_color": p.accent, "accent_fg_color": p.accent_foreground, "accent_color": p.accent_text,
        "destructive_bg_color": p.error, "destructive_fg_color": fill_fg("error"), "destructive_color": text_on_bg("error"),
        "success_bg_color": p.success, "success_fg_color": fill_fg("success"), "success_color": text_on_bg("success"),
        "warning_bg_color": p.warning, "warning_fg_color": fill_fg("warning"), "warning_color": text_on_bg("warning"),
        "error_bg_color": p.error, "error_fg_color": fill_fg("error"), "error_color": text_on_bg("error"),
        "headerbar_bg_color": p.headerbar_bg, "headerbar_fg_color": p.headerbar_fg,
        "headerbar_border_color": p.border, "headerbar_backdrop_color": p.background,
        "card_bg_color": p.surface, "card_fg_color": p.foreground,
        "dialog_bg_color": p.background, "dialog_fg_color": p.foreground,
        "popover_bg_color": p.surface, "popover_fg_color": p.foreground,
        "sidebar_bg_color": p.sidebar_bg, "sidebar_fg_color": p.sidebar_fg,
        "sidebar_backdrop_color": p.background, "sidebar_border_color": p.border,
    }


def build_css(p: Palette, css_variables: bool = False) -> str:
    """Stylesheet for the whole app from one Palette.

    * `@define-color` lines override libadwaita's named colors (works on every
      libadwaita 1.x). When `css_variables` is set (GTK >= 4.16, libadwaita >= 1.6
      -- older GTK would log a parse error for custom properties) the equivalent
      `--name` variables are emitted too.
    * `st-bg-<token>` / `st-fg-<token>` utility classes give any widget a semantic
      color without a color literal in Python.
    * `st-*` component classes style app-specific pieces (usage rows, swatches).
    """
    adw = _adw_colors(p)
    out = [f"/* ScreenTime theme: {p.theme_id} ({'dark' if p.is_dark else 'light'}) */"]
    out += [f"@define-color {k} {v};" for k, v in adw.items()]
    if css_variables:
        out.append(":root {")
        out += [f"  --{k.replace('_', '-')}: {v};" for k, v in adw.items()]
        out.append("}")
    for tok in TOKENS:
        cls = tok.replace("_", "-")
        out.append(f".st-bg-{cls} {{ background-color: {p.colors[tok]}; }}")
        out.append(f".st-fg-{cls} {{ color: {p.colors[tok]}; }}")
    out += [
        f".st-muted {{ color: {p.muted_foreground}; }}",
        f".st-app-row {{ background-color: {p.row_bg}; border-radius: 8px; }}",
        f".st-app-row:hover {{ background-color: {p.row_hover}; }}",
        f".navigation-sidebar > row:selected {{ background-color: {p.selected_bg}; color: {p.selected_fg}; }}",
        f".st-swatch {{ min-width: 26px; min-height: 26px; border-radius: 6px; border: 1px solid {p.border}; }}",
        f".st-preview {{ background-color: {p.surface}; color: {p.foreground}; border: 1px solid {p.border}; border-radius: 10px; padding: 10px; }}",
    ]
    return "\n".join(out) + "\n"


def fallback_palette() -> Palette:
    """Used by widgets constructed with no theme manager (tests, embedding)."""
    return resolve_palette(DEFAULT_THEME_ID, "light", None)
