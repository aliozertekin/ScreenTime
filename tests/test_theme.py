import re
from pathlib import Path

import pytest

from screentime import theme as T
from screentime.config import Config, DEFAULTS
from screentime.db import Database

ROOT = Path(__file__).resolve().parent.parent

# A realistic Plasma "Breeze Dark"-style kdeglobals (the parts we read).
BREEZE_DARK = """[$Version]
update_info=kdeglobals.upd:x

[Colors:Window]
BackgroundAlternate=29,31,32
BackgroundNormal=35,38,39
DecorationFocus=61,174,233
DecorationHover=61,174,233
ForegroundInactive=161,169,177
ForegroundNormal=252,252,252

[Colors:View]
BackgroundAlternate=35,38,39
BackgroundNormal=27,30,32
ForegroundLink=61,174,233
ForegroundNegative=218,68,83
ForegroundNeutral=246,116,0
ForegroundNormal=252,252,252
ForegroundPositive=39,174,96
ForegroundVisited=155,89,182

[Colors:Selection]
BackgroundNormal=61,174,233
ForegroundNormal=255,255,255

[Colors:Header]
BackgroundNormal=49,54,59
ForegroundNormal=252,252,252

[General]
ColorScheme=BreezeDark
"""
BREEZE_LIGHT = """[Colors:Window]
BackgroundNormal=239,240,241
BackgroundAlternate=227,229,231
ForegroundNormal=35,38,39
ForegroundInactive=127,140,141
[Colors:View]
BackgroundNormal=252,252,252
[Colors:Selection]
BackgroundNormal=61,174,233
ForegroundNormal=255,255,255
"""


# ------------------------------------------------------------------ color math
@pytest.mark.parametrize("value,expected", [
    ("#ff0000", (255, 0, 0)), ("#FF0000", (255, 0, 0)), ("#f00", (255, 0, 0)), (" #00ff7f ", (0, 255, 127)),
    ("ff0000", None), ("#ff00", None), ("#gggggg", None), ("", None), (None, None), (123, None), ("rgb(1,2,3)", None),
    ("#ff0000; color: red", None),
])
def test_parse_hex(value, expected):
    assert T.parse_hex(value) == expected


def test_normalize_and_to_hex():
    assert T.normalize_hex("#ABC") == "#aabbcc"
    assert T.normalize_hex("nope") is None
    assert T.to_hex((300, -5, 12.6)) == "#ff000d"          # clamped to 0..255, rounded (13 == 0x0d)


def test_contrast_ratio_reference_values():
    assert T.contrast_ratio(T.BLACK, T.WHITE) == pytest.approx(21.0, abs=0.01)
    assert T.contrast_ratio(T.WHITE, T.WHITE) == pytest.approx(1.0)


def test_readable_on_keeps_white_on_adwaita_blue_but_flips_on_yellow():
    assert T.readable_on(T.parse_hex("#3584e4")) == T.WHITE
    assert T.readable_on(T.parse_hex("#f5c211")) == T.BLACK


def test_ensure_contrast_fixes_low_contrast_and_leaves_good_alone():
    bg = T.parse_hex("#ffffff")
    faint = T.parse_hex("#cccccc")
    assert T.contrast_ratio(T.ensure_contrast(faint, bg, 4.5), bg) >= 4.5
    strong = T.parse_hex("#000000")
    assert T.ensure_contrast(strong, bg, 4.5) == strong
    dark_bg = T.parse_hex("#111111")
    assert T.contrast_ratio(T.ensure_contrast(T.parse_hex("#222222"), dark_bg, 4.5), dark_bg) >= 4.5


# ---------------------------------------------------------- built-in themes
def all_variants():
    for th in T.list_themes():
        for scheme in ("light", "dark"):
            yield th.id, scheme


def test_required_builtin_themes_exist():
    ids = {t.id for t in T.list_themes()}
    assert {"system", "default", "gruvbox-dark", "gruvbox-light", "dark", "light"} <= ids


def test_adaptive_vs_fixed_classification():
    assert T.get_theme("default").adaptive and T.get_theme("system").adaptive
    assert T.get_theme("gruvbox-dark").fixed_scheme == "dark"
    assert T.get_theme("gruvbox-light").fixed_scheme == "light"
    assert T.get_theme("dark").fixed_scheme == "dark" and T.get_theme("light").fixed_scheme == "light"
    assert T.get_theme("default").fixed_scheme is None


@pytest.mark.parametrize("theme_id,scheme", list(all_variants()))
def test_every_theme_yields_a_complete_valid_palette(theme_id, scheme):
    p = T.resolve_palette(theme_id, scheme, None, T.SystemInfo())
    assert set(p.colors) == set(T.TOKENS)
    for tok, value in p.colors.items():
        assert T.parse_hex(value) and value == value.lower() and len(value) == 7, (tok, value)


@pytest.mark.parametrize("theme_id,scheme", list(all_variants()))
def test_every_theme_is_readable(theme_id, scheme):
    """Contrast floors so no built-in theme can ship unreadable text."""
    p = T.resolve_palette(theme_id, scheme, None, T.SystemInfo())
    checks = [("foreground", "background", 4.5), ("foreground", "surface", 4.5),
              ("muted_foreground", "background", 3.0), ("accent_foreground", "accent", 3.5),
              ("accent_text", "background", 4.5), ("selected_fg", "selected_bg", 3.5),
              ("headerbar_fg", "headerbar_bg", 4.5), ("sidebar_fg", "sidebar_bg", 4.5)]
    for fg, bg, floor in checks:
        assert T.contrast_ratio(p.rgb(fg), p.rgb(bg)) >= floor, (theme_id, scheme, fg, bg)
    for i in range(1, 7):                                   # chart colors visible on the card they sit on
        assert T.contrast_ratio(p.rgb(f"chart_{i}"), p.rgb("surface")) >= 2.0, (theme_id, scheme, i)


def test_gruvbox_uses_canonical_palette_values():
    d = T.resolve_palette("gruvbox-dark", "system", None)
    assert (d.background, d.surface, d.foreground) == ("#282828", "#3c3836", "#ebdbb2")
    assert (d.success, d.warning, d.error) == ("#b8bb26", "#fabd2f", "#fb4934")
    assert d.is_dark
    l = T.resolve_palette("gruvbox-light", "system", None)
    assert (l.background, l.foreground) == ("#fbf1c7", "#3c3836")
    assert (l.success, l.warning, l.error) == ("#79740e", "#b57614", "#9d0006")
    assert not l.is_dark


@pytest.mark.parametrize("hue", range(0, 360, 20))
@pytest.mark.parametrize("lightness", (0.15, 0.5, 0.85))
def test_any_user_accent_stays_readable(hue, lightness):
    """Whatever the user picks, text on the accent and accent-as-text must read."""
    import colorsys
    r, g, b = colorsys.hls_to_rgb(hue / 360, lightness, 0.8)
    accent = T.to_hex((r * 255, g * 255, b * 255))
    for theme_id, scheme in (("default", "light"), ("default", "dark"), ("gruvbox-dark", "system"), ("gruvbox-light", "system")):
        p = T.resolve_palette(theme_id, scheme, accent, T.SystemInfo())
        assert T.contrast_ratio(p.rgb("accent_foreground"), p.rgb("accent")) >= 3.5, (accent, theme_id)
        assert T.contrast_ratio(p.rgb("accent_text"), p.rgb("background")) >= 4.5, (accent, theme_id)


# ----------------------------------------------------------------- resolution
def test_default_theme_follows_color_scheme():
    assert not T.resolve_palette("default", "light", None).is_dark
    assert T.resolve_palette("default", "dark", None).is_dark
    assert T.resolve_palette("default", "system", None, T.SystemInfo(is_dark=True)).is_dark
    assert not T.resolve_palette("default", "system", None, T.SystemInfo(is_dark=False)).is_dark
    assert not T.resolve_palette("default", "system", None, T.SystemInfo(is_dark=None)).is_dark   # unknown => light


def test_explicit_scheme_beats_system_preference():
    assert not T.resolve_palette("default", "light", None, T.SystemInfo(is_dark=True)).is_dark


@pytest.mark.parametrize("scheme", ("light", "dark", "system"))
def test_fixed_theme_ignores_color_scheme(scheme):
    assert T.resolve_palette("gruvbox-dark", scheme, None, T.SystemInfo(is_dark=False)).is_dark
    assert not T.resolve_palette("gruvbox-light", scheme, None, T.SystemInfo(is_dark=True)).is_dark


def test_custom_accent_overrides_and_derives_everything_from_it():
    base = T.resolve_palette("default", "light", None)
    p = T.resolve_palette("default", "light", "#E01B8A")
    assert p.accent == "#e01b8a" and p.chart_1 == "#e01b8a" and p.selected_bg == "#e01b8a"
    assert p.accent_hover != base.accent_hover and p.accent_active != base.accent_active
    assert p.background == base.background                      # only accent-derived things move
    assert p.chart_2 == base.chart_2


def test_custom_accent_applies_on_fixed_and_system_themes_too():
    assert T.resolve_palette("gruvbox-dark", "system", "#112233").accent == "#112233"
    assert T.resolve_palette("system", "system", "#112233", T.SystemInfo(kde=T.parse_kdeglobals(BREEZE_DARK))).accent == "#112233"


@pytest.mark.parametrize("bad", ["", None, "red", "#12", "#gggggg", "12,34,56", "javascript:1", "#ff0000; }"])
def test_invalid_accent_is_ignored(bad):
    assert T.resolve_palette("default", "light", bad).accent == T.resolve_palette("default", "light", None).accent


@pytest.mark.parametrize("bad", ["", None, "no-such-theme", "../../etc/passwd", "DEFAULT", 5])
def test_unknown_theme_falls_back_to_default(bad):
    assert T.resolve_palette(bad, "light", None).theme_id == "default"
    assert T.get_theme(bad).id == "default"


def test_invalid_color_scheme_falls_back_to_system():
    p = T.resolve_palette("default", "purple", None, T.SystemInfo(is_dark=True))
    assert p.is_dark                                            # treated as "system"


def test_palette_is_immutable_and_attribute_access_is_strict():
    p = T.resolve_palette("default", "light", None)
    with pytest.raises(Exception):
        p.theme_id = "x"
    with pytest.raises(AttributeError):
        p.not_a_token


# --------------------------------------------------------------- extensibility
def test_new_theme_needs_only_a_few_colors_and_is_selectable():
    T.register_theme(T.ThemeDefinition("nord-test", "Nord", "t", variants={"dark": {
        "background": "#2e3440", "foreground": "#d8dee9", "accent": "#88c0d0"}}))
    try:
        p = T.resolve_palette("nord-test", "light", None)      # fixed dark theme => scheme ignored
        assert p.is_dark and p.accent == "#88c0d0" and set(p.colors) == set(T.TOKENS)
        assert T.is_known_theme("nord-test")
    finally:
        T._THEMES.pop("nord-test", None)


@pytest.mark.parametrize("variants", [
    {"dark": {"foreground": "#fff", "accent": "#08f"}},                                   # no background
    {"dark": {"background": "#000", "foreground": "#fff", "accent": "not-a-color"}},
    {"sepia": {"background": "#000", "foreground": "#fff", "accent": "#08f"}},            # bad scheme key
    {},
])
def test_invalid_theme_definition_is_rejected_at_registration(variants):
    with pytest.raises(ValueError):
        T.register_theme(T.ThemeDefinition("bad-test", "Bad", "t", variants=variants))
    assert not T.is_known_theme("bad-test")


# ------------------------------------------------------------------ KDE colors
def test_parse_kde_color_formats():
    assert T.parse_kde_color("61,174,233") == (61, 174, 233)
    assert T.parse_kde_color(" 61 , 174 , 233 , 255 ") == (61, 174, 233)
    assert T.parse_kde_color("#3daee9") == (61, 174, 233)
    for bad in ("300,0,0", "1,2", "a,b,c", "", None, "1,2,3,4,5"):
        assert T.parse_kde_color(bad) is None


def test_breeze_dark_maps_to_semantic_tokens():
    spec, is_dark = T.kde_palette_spec(T.parse_kdeglobals(BREEZE_DARK))
    assert is_dark
    p = T.resolve_palette("system", "system", None, T.SystemInfo(kde=T.parse_kdeglobals(BREEZE_DARK)))
    assert p.source == "kde" and p.is_dark
    assert p.background == "#232627" and p.surface == "#1b1e20" and p.surface_alt == "#232627"
    assert p.foreground == "#fcfcfc" and p.muted_foreground == "#a1a9b1"
    assert p.accent == "#3daee9" and p.accent_foreground == "#ffffff"
    assert p.accent_hover == "#3daee9"                       # accent is the selection => KDE's DecorationHover
    assert (p.success, p.warning, p.error) == ("#27ae60", "#f67400", "#da4453")
    assert (p.selected_bg, p.selected_fg) == ("#3daee9", "#ffffff")
    assert (p.headerbar_bg, p.headerbar_fg) == ("#31363b", "#fcfcfc")
    assert p.sidebar_bg == "#1d1f20"
    assert p.chart_1 == "#3daee9" and p.chart_5 == "#3daee9" and p.chart_6 == "#9b59b6"


def test_breeze_light_is_detected_as_light():
    p = T.resolve_palette("system", "system", None, T.SystemInfo(kde=T.parse_kdeglobals(BREEZE_LIGHT)))
    assert p.source == "kde" and not p.is_dark and p.background == "#eff0f1"


def test_general_accentcolor_beats_selection_color():
    text = BREEZE_DARK.replace("[General]", "[General]\nAccentColor=233,30,99")
    p = T.resolve_palette("system", "system", None, T.SystemInfo(kde=T.parse_kdeglobals(text)))
    assert p.accent == "#e91e63" and p.selected_bg == "#3daee9"      # selection keeps its own color
    assert T.contrast_ratio(p.rgb("accent_foreground"), p.rgb("accent")) >= 3.5


def test_missing_optional_sections_fall_back_without_error():
    minimal = "[Colors:Window]\nBackgroundNormal=40,40,40\nForegroundNormal=240,240,240\n"
    p = T.resolve_palette("system", "system", None, T.SystemInfo(kde=T.parse_kdeglobals(minimal)))
    assert p.source == "kde" and set(p.colors) == set(T.TOKENS)
    assert p.headerbar_bg != "" and p.surface == p.background      # View/Header missing => Window colors


def test_garbage_values_in_kdeglobals_are_ignored():
    text = BREEZE_DARK.replace("ForegroundPositive=39,174,96", "ForegroundPositive=banana")
    p = T.resolve_palette("system", "system", None, T.SystemInfo(kde=T.parse_kdeglobals(text)))
    assert p.source == "kde" and T.parse_hex(p.success)


@pytest.mark.parametrize("text", ["", "not an ini at all", "[Colors:Window]\nBackgroundNormal=zzz\nForegroundNormal=1,2,3", "\x00\xff[[["])
def test_unusable_kdeglobals_means_not_kde(text, tmp_path):
    f = tmp_path / "kdeglobals"
    f.write_text(text)
    assert T.load_kde_colors([f]) is None


def test_no_kdeglobals_files_at_all(tmp_path):
    assert T.load_kde_colors([tmp_path / "nope"]) is None


def test_user_kdeglobals_overrides_defaults_per_key(tmp_path):
    defaults = tmp_path / "kdedefaults"
    defaults.mkdir()
    (defaults / "kdeglobals").write_text(BREEZE_DARK)
    user = tmp_path / "kdeglobals"
    user.write_text("[Colors:Window]\nBackgroundNormal=10,20,30\n")
    merged = T.load_kde_colors([user, defaults / "kdeglobals"])
    assert merged["Colors:Window"]["BackgroundNormal"] == "10,20,30"            # user wins
    assert merged["Colors:Window"]["ForegroundNormal"] == "252,252,252"         # rest from defaults


def test_kde_config_paths_priority_order(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_CONFIG_DIRS": f"{tmp_path}/kdedefaults:/etc/xdg"}
    paths = [str(p) for p in T.kde_config_paths(env, tmp_path)]
    assert paths == [f"{tmp_path}/cfg/kdeglobals", f"{tmp_path}/kdedefaults/kdeglobals", "/etc/xdg/kdeglobals"]
    assert [str(p) for p in T.kde_config_paths({}, tmp_path)][0] == f"{tmp_path}/.config/kdeglobals"


def test_system_theme_without_kde_uses_system_scheme_and_accent():
    p = T.resolve_palette("system", "system", None, T.SystemInfo(is_dark=True, accent="#e66100"))
    assert p.source == "theme" and p.is_dark and p.accent == "#e66100" and p.chart_1 == "#e66100"
    p2 = T.resolve_palette("system", "system", None, T.SystemInfo(is_dark=False))
    assert p2.accent == T.resolve_palette("default", "light", None).accent


def test_explicit_scheme_on_system_theme_skips_kde_palette_but_keeps_desktop_accent():
    info = T.SystemInfo(kde=T.parse_kdeglobals(BREEZE_DARK), accent="#e66100")
    p = T.resolve_palette("system", "light", None, info)
    assert p.source == "theme" and not p.is_dark                       # user asked for light on a dark desktop
    assert p.accent == "#e66100"


def test_kde_mapping_documents_every_token():
    assert set(T.KDE_MAPPING) == set(T.TOKENS)


# ------------------------------------------------------------------------ CSS
ADW_REQUIRED = ["window_bg_color", "window_fg_color", "view_bg_color", "accent_bg_color", "accent_fg_color",
                "accent_color", "destructive_bg_color", "success_bg_color", "warning_bg_color", "error_bg_color",
                "headerbar_bg_color", "headerbar_fg_color", "card_bg_color", "sidebar_bg_color", "sidebar_fg_color",
                "popover_bg_color", "dialog_bg_color"]


def test_css_defines_libadwaita_named_colors_from_the_palette():
    p = T.resolve_palette("gruvbox-dark", "system", None)
    css = T.build_css(p)
    for name in ADW_REQUIRED:
        assert re.search(rf"@define-color {name} #[0-9a-f]{{6}};", css), name
    assert f"@define-color window_bg_color {p.background};" in css
    assert f"@define-color accent_bg_color {p.accent};" in css
    assert f"@define-color headerbar_bg_color {p.headerbar_bg};" in css


def test_css_has_utility_classes_for_every_token():
    css = T.build_css(T.resolve_palette("default", "light", None))
    for tok in T.TOKENS:
        cls = tok.replace("_", "-")
        assert f".st-bg-{cls} " in css and f".st-fg-{cls} " in css


def test_css_variables_only_when_requested():
    p = T.resolve_palette("default", "dark", None)
    assert ":root" not in T.build_css(p)
    with_vars = T.build_css(p, css_variables=True)
    assert "--accent-bg-color: " + p.accent in with_vars and ":root {" in with_vars


def test_css_is_deterministic_and_contains_no_unsubstituted_placeholders():
    p = T.resolve_palette("default", "light", "#e01b8a")
    assert T.build_css(p) == T.build_css(p)
    css = T.build_css(p, css_variables=True)
    assert "{p." not in css and "None" not in css and "$" not in css
    assert css.count("{") == css.count("}")


def test_accent_value_cannot_inject_css():
    """The accent goes into a stylesheet; only a validated #rrggbb may get there."""
    p = T.resolve_palette("default", "light", "#ff0000; } * { display: none")
    assert "display" not in T.build_css(p)


# -------------------------------------------------------------------- config
@pytest.fixture
def cfg(tmp_path):
    db = Database(tmp_path / "t.db")
    yield Config(db)
    db.close()


def test_defaults(cfg):
    assert (cfg.theme, cfg.color_scheme, cfg.accent_color) == ("default", "system", "")
    assert {"theme", "color_scheme", "accent_color"} <= set(DEFAULTS)


def test_theme_selection_round_trip(cfg):
    for tid in ("gruvbox-dark", "gruvbox-light", "system", "dark", "light", "default"):
        cfg.set_theme(tid)
        assert cfg.theme == tid


def test_settings_persist_across_restart(tmp_path):
    path = tmp_path / "persist.db"
    db = Database(path)
    c = Config(db)
    c.set_theme("gruvbox-dark"); c.set_color_scheme("dark"); c.set_accent_color("#E01B8A")
    db.close()
    db2 = Database(path)
    c2 = Config(db2)
    assert (c2.theme, c2.color_scheme, c2.accent_color) == ("gruvbox-dark", "dark", "#e01b8a")
    db2.close()


def test_setters_reject_invalid_values_without_changing_state(cfg):
    cfg.set_theme("gruvbox-dark")
    with pytest.raises(ValueError):
        cfg.set_theme("nope")
    with pytest.raises(ValueError):
        cfg.set_color_scheme("sepia")
    with pytest.raises(ValueError):
        cfg.set_accent_color("blue")
    assert cfg.theme == "gruvbox-dark" and cfg.color_scheme == "system" and cfg.accent_color == ""


def test_stale_or_corrupt_stored_values_fall_back_but_are_not_overwritten(cfg):
    cfg.set("theme", "theme-removed-in-a-later-version")
    cfg.set("color_scheme", "???")
    cfg.set("accent_color", "not-a-color")
    assert (cfg.theme, cfg.color_scheme, cfg.accent_color) == ("default", "system", "")
    assert cfg.db.get_setting("theme") == "theme-removed-in-a-later-version"     # user's data untouched


def test_clearing_and_resetting_accent(cfg):
    cfg.set_accent_color("#123456")
    cfg.set_accent_color(None)
    assert cfg.accent_color == ""
    cfg.set_accent_color("#123456"); cfg.set_accent_color("")
    assert cfg.accent_color == ""


def test_reset_appearance(cfg):
    cfg.set_theme("gruvbox-light"); cfg.set_color_scheme("dark"); cfg.set_accent_color("#abcdef")
    cfg.reset_appearance()
    assert (cfg.theme, cfg.color_scheme, cfg.accent_color) == ("default", "system", "")


def test_appearance_shares_the_existing_settings_table(cfg):
    """No second configuration system: the values live in `settings`."""
    cfg.set_theme("gruvbox-dark")
    assert cfg.db.get_setting("theme") == "gruvbox-dark"


# ----------------------------------------------- views must not hard-code colors
def test_gui_modules_contain_no_color_literals():
    """Views consume semantic tokens/CSS classes; a hex color or rgb() literal in
    GUI code means someone bypassed the theme layer. Only *string tokens* are
    scanned (via tokenize), so comments and method names like `palette.rgb()`
    are not mistaken for literals."""
    import io
    import tokenize
    hex_re = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?\b")
    func_re = re.compile(r"(?<![\w.])(?:rgba?|hsla?)\s*\(")
    offenders = []
    for py in (ROOT / "screentime" / "gui").rglob("*.py"):
        for tok in tokenize.generate_tokens(io.StringIO(py.read_text()).readline):
            if tok.type == tokenize.STRING and (hex_re.search(tok.string) or func_re.search(tok.string)):
                offenders.append(f"{py.relative_to(ROOT)}:{tok.start[0]}: {tok.string[:60]}")
    assert offenders == []


def test_literal_scanner_actually_detects_literals(tmp_path):
    """Guard the guard: the same scan must flag real literals."""
    import io
    import tokenize
    hex_re = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?\b")
    func_re = re.compile(r"(?<![\w.])(?:rgba?|hsla?)\s*\(")
    src = 'a = "#3584e4"\nb = "rgba(0,0,0,.5)"\nc = palette.rgb("chart_1")  # #fed\n'
    hits = [t.string for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.STRING and (hex_re.search(t.string) or func_re.search(t.string))]
    assert hits == ['"#3584e4"', '"rgba(0,0,0,.5)"']


# ------------------------------------------------------------ community themes
NEW_THEMES = {
    "nord": "dark", "dracula": "dark", "solarized-dark": "dark", "solarized-light": "light",
    "catppuccin-mocha": "dark", "catppuccin-latte": "light", "tokyo-night": "dark", "one-dark": "dark",
    "rose-pine": "dark", "rose-pine-dawn": "light", "high-contrast-dark": "dark", "high-contrast-light": "light",
}


def test_all_new_themes_registered_as_fixed_schemes():
    for tid, scheme in NEW_THEMES.items():
        th = T.get_theme(tid)
        assert th.id == tid and th.fixed_scheme == scheme and not th.adaptive, tid


def test_theme_ids_and_names_are_unique_and_described():
    themes = T.list_themes()
    assert len({t.id for t in themes}) == len(themes) == len({t.name for t in themes})
    for t in themes:
        assert t.name.strip() and len(t.description) > 10, t.id
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", t.id), t.id


@pytest.mark.parametrize("tid", ["high-contrast-dark", "high-contrast-light"])
def test_high_contrast_themes_meet_wcag_aaa(tid):
    p = T.resolve_palette(tid, "system", None)
    for fg, bg in [("foreground", "background"), ("foreground", "surface"), ("muted_foreground", "background"),
                   ("accent_foreground", "accent"), ("accent_text", "background"),
                   ("headerbar_fg", "headerbar_bg"), ("sidebar_fg", "sidebar_bg")]:
        assert T.contrast_ratio(p.rgb(fg), p.rgb(bg)) >= 7.0, (tid, fg, bg)
    assert T.contrast_ratio(p.rgb("border"), p.rgb("background")) >= 7.0     # visible outlines too


@pytest.mark.parametrize("tid,expected", [
    ("nord", {"background": "#2e3440", "foreground": "#d8dee9", "accent": "#88c0d0", "error": "#bf616a"}),
    ("dracula", {"background": "#282a36", "foreground": "#f8f8f2", "accent": "#bd93f9", "error": "#ff5555"}),
    ("catppuccin-mocha", {"background": "#1e1e2e", "foreground": "#cdd6f4", "accent": "#cba6f7", "error": "#f38ba8"}),
    ("catppuccin-latte", {"background": "#eff1f5", "foreground": "#4c4f69", "accent": "#8839ef", "error": "#d20f39"}),
    ("tokyo-night", {"background": "#1a1b26", "foreground": "#c0caf5", "accent": "#7aa2f7", "success": "#9ece6a"}),
    ("one-dark", {"background": "#282c34", "foreground": "#abb2bf", "accent": "#61afef", "success": "#98c379"}),
    ("rose-pine", {"background": "#191724", "foreground": "#e0def4", "accent": "#c4a7e7", "error": "#eb6f92"}),
    ("rose-pine-dawn", {"background": "#faf4ed", "foreground": "#575279", "surface": "#fffaf3"}),
    ("solarized-dark", {"background": "#002b36", "surface": "#073642", "accent": "#268bd2"}),
    ("solarized-light", {"background": "#fdf6e3", "surface_alt": "#eee8d5", "accent": "#268bd2"}),
])
def test_new_themes_keep_their_published_signature_colors(tid, expected):
    p = T.resolve_palette(tid, "system", None)
    for token, value in expected.items():
        assert p.colors[token] == value, (tid, token)


def test_new_themes_honour_a_custom_accent():
    for tid in NEW_THEMES:
        p = T.resolve_palette(tid, "system", "#ff00aa")
        assert p.accent == "#ff00aa" and p.chart_1 == "#ff00aa"
        assert T.contrast_ratio(p.rgb("accent_foreground"), p.rgb("accent")) >= 3.5


def test_readme_theme_table_lists_every_registered_theme():
    text = (ROOT / "README.md").read_text()
    for t in T.list_themes():
        assert f"| `{t.id}` | {t.name} |" in text, t.id
