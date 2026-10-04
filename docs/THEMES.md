# Themes and appearance

Back to the [README](../README.md).

## Appearance and themes

Settings -> **Appearance** has three *independent* choices (kept separate on
purpose), plus a live preview and a reset button. Changes apply immediately,
with no restart, and are stored in the existing `settings` table
(`theme`, `color_scheme`, `accent_color`); there is no second config system.

| Setting | Values | Notes |
|---|---|---|
| **Theme** | see below | a named palette family |
| **Color scheme** | Follow system / Light / Dark | only *adaptive* themes honour it; a *fixed* theme **is** one scheme, so the row is disabled and shows that scheme (your stored preference is kept and applies again when you pick an adaptive theme) |
| **Accent color** | any color, or "Use theme accent" | chosen with the standard GTK color chooser (`Gtk.ColorDialogButton`), never typed as hex |

| id | Name | Kind |
|---|---|---|
| `system` | System colors | adaptive (honours the color scheme) |
| `default` | ScreenTime | adaptive (honours the color scheme) |
| `light` | Light | fixed (light) |
| `dark` | Dark | fixed (dark) |
| `gruvbox-dark` | Gruvbox Dark | fixed (dark) |
| `gruvbox-light` | Gruvbox Light | fixed (light) |
| `nord` | Nord | fixed (dark) |
| `dracula` | Dracula | fixed (dark) |
| `solarized-dark` | Solarized Dark | fixed (dark) |
| `solarized-light` | Solarized Light | fixed (light) |
| `catppuccin-mocha` | Catppuccin Mocha | fixed (dark) |
| `catppuccin-latte` | Catppuccin Latte | fixed (light) |
| `tokyo-night` | Tokyo Night | fixed (dark) |
| `one-dark` | One Dark | fixed (dark) |
| `rose-pine` | Rose Pine | fixed (dark) |
| `rose-pine-dawn` | Rose Pine Dawn | fixed (light) |
| `high-contrast-dark` | High Contrast Dark | fixed (dark) |
| `high-contrast-light` | High Contrast Light | fixed (light) |

The community palettes use each project's published colors, except where a
published pairing misses the readability floors the tests enforce (Solarized's
body/dim text and card color are nudged just enough to pass). The two High
Contrast themes are held to 7:1 (WCAG AAA), including their borders.

Unknown, removed or corrupted stored values fall back to the default theme /
"follow system" / theme accent without being overwritten in the database.

### How it is built

* `screentime/theme.py` is pure Python (no GTK import): semantic tokens, the
  themes, color math, the KDE mapping, and the CSS generator. It is what the
  unit tests exercise.
* `screentime/gui/theme_manager.py` is the thin GTK layer: it loads the
  generated stylesheet into a `Gtk.CssProvider`, sets the libadwaita color
  scheme, and re-applies when the desktop's colors change.
* Views and widgets never know which theme is active. They use **semantic
  tokens** (`background`, `surface`, `surface_alt`, `foreground`,
  `muted_foreground`, `accent`, `accent_hover`, `accent_active`, `border`,
  `success`, `warning`, `error`, `chart_1`..`chart_6`, plus selected, headerbar,
  sidebar and row colors) or the generated CSS classes `st-bg-<token>`,
  `st-fg-<token>`, `st-app-row`, `st-muted`. A test fails if a color literal
  appears in any GUI module.
* The stylesheet overrides libadwaita's named colors (`window_bg_color`,
  `accent_bg_color`, `headerbar_bg_color`, `sidebar_bg_color`, `card_bg_color`,
  ...), which restyles every stock widget. On GTK >= 4.16 the equivalent CSS
  variables are emitted as well; older GTK would log a parse error for them, so
  they are omitted there.
* Text on an accent, and the accent used as text, are contrast-corrected
  automatically, so any color you pick stays readable. Every built-in theme is
  tested against contrast floors in both variants.

### System theme and KDE Plasma

Choose **System colors**. With the color scheme on "Follow
system" it builds the palette from KDE's own color scheme; otherwise it falls
back to the system light/dark preference (and the system accent, where
libadwaita >= 1.6 and the desktop expose one).

Sources, highest priority first: `$XDG_CONFIG_HOME/kdeglobals`, then each
`$XDG_CONFIG_DIRS` entry (Plasma keeps defaults in `~/.config/kdedefaults`),
then `/etc/xdg`. Merged per key. The files are watched, so applying a different
Plasma color scheme re-themes the open window without a restart (covered by a
test that atomically replaces the file the way KConfig does).

| Token | KDE source |
|---|---|
| `background` | [Colors:Window] BackgroundNormal |
| `surface` | [Colors:View] BackgroundNormal |
| `surface_alt` | [Colors:View] BackgroundAlternate |
| `foreground` | [Colors:Window] ForegroundNormal |
| `muted_foreground` | [Colors:Window] ForegroundInactive |
| `accent` | [General] AccentColor, else [Colors:Selection] BackgroundNormal |
| `accent_hover` | [Colors:Window] DecorationHover (when accent is the selection color), else derived |
| `accent_active` | derived from accent |
| `accent_foreground` | [Colors:Selection] ForegroundNormal (when accent is the selection color), else derived |
| `accent_text` | derived from accent for contrast on the background |
| `border` | derived (mix of foreground and background) |
| `success` | [Colors:View] ForegroundPositive |
| `warning` | [Colors:View] ForegroundNeutral |
| `error` | [Colors:View] ForegroundNegative |
| `selected_bg` | [Colors:Selection] BackgroundNormal |
| `selected_fg` | [Colors:Selection] ForegroundNormal |
| `headerbar_bg` | [Colors:Header] BackgroundNormal (falls back to Window) |
| `headerbar_fg` | [Colors:Header] ForegroundNormal (falls back to Window) |
| `sidebar_bg` | [Colors:Window] BackgroundAlternate (falls back to derived) |
| `sidebar_fg` | [Colors:Window] ForegroundNormal |
| `row_bg` | same as surface |
| `row_hover` | derived (surface tinted with the accent) |
| `chart_1` | accent |
| `chart_2` | [Colors:View] ForegroundPositive |
| `chart_3` | [Colors:View] ForegroundNeutral |
| `chart_4` | [Colors:View] ForegroundNegative |
| `chart_5` | [Colors:View] ForegroundLink |
| `chart_6` | [Colors:View] ForegroundVisited |

Tokens KDE does not define (border, hover/active shades, ...) are derived from
the ones it does, so any scheme yields a complete, consistent palette. If you
force Light or Dark while the desktop is the other, the KDE palette is skipped
(it *is* one specific scheme) and the ScreenTime palette for the chosen scheme
is used with the desktop's accent.

**What this does not do.** GTK4/libadwaita cannot reproduce KDE's Qt widget
style, fonts, icon theme, window-decoration theme, title-bar button layout,
transparency/blur, high-contrast or per-widget-state colors (`Colors:Button`,
`Colors:Tooltip`, `Colors:Complementary`, inactive-window colors are not
read). The goal is that ScreenTime *feels* like a KDE application in its colors,
not that it is one.

**Verification status.** Tested: parsing and mapping of Breeze-style
`kdeglobals` (dark and light), priority merging, malformed input, live update on
file replacement, every theme x scheme parsed as valid CSS by GTK 4.14 /
libadwaita 1.5, and rendered screenshots of several themes. Not verified in this
repository's test environment: a live Plasma session, and libadwaita >= 1.6's
accent-color API and GTK >= 4.16's CSS variables (both are guarded, and the
accent path is tested with a faked API). The `gtk-application-prefer-dark-theme`
warning libadwaita prints comes from your GTK settings file (KDE's GTK
integration writes it), not from ScreenTime, which sets the scheme through
`AdwStyleManager`.

### Adding a theme

```python
from screentime.theme import register_theme, ThemeDefinition

register_theme(ThemeDefinition(
    "nord", "Nord", "Arctic, north-bluish palette.",
    variants={"dark": {"background": "#2e3440", "foreground": "#d8dee9", "accent": "#88c0d0"}},
))
```

Only `background`, `foreground` and `accent` are required; every other token is
derived unless you list it. Give both `"light"` and `"dark"` variants for an
adaptive theme. A malformed theme is rejected at registration.

