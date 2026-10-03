"""ThemeManager + Settings appearance UI against real GTK4/libadwaita (needs a
display: run under xvfb-run on a headless box)."""
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("gi")
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("GTK widget tests need a display (run under xvfb-run)", allow_module_level=True)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

Adw.init()

from screentime import theme as T
from screentime.config import Config
from screentime.db import Database
from screentime.gui import theme_manager as tm_mod
from screentime.gui.theme_manager import ThemeManager, ensure_theme_manager

from test_theme import BREEZE_DARK, BREEZE_LIGHT


def pump(cond, timeout=4.0):
    ctx = GLib.MainContext.default()
    end = time.time() + timeout
    while time.time() < end:
        while ctx.pending():
            ctx.iteration(False)
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "gui.db")
    cfg = Config(db)
    kde = tmp_path / "kdeglobals"
    mgr = ThemeManager(cfg, kde_paths=[kde])
    tm_mod.set_theme_manager(mgr)

    class E: pass
    e = E()
    e.db, e.cfg, e.mgr, e.kde, e.tmp = db, cfg, mgr, kde, tmp_path
    yield e
    tm_mod.set_theme_manager(None)
    db.close()


# ------------------------------------------------------------- CSS validity
@pytest.mark.parametrize("theme_id", [t.id for t in T.list_themes()])
@pytest.mark.parametrize("scheme", ["system", "light", "dark"])
def test_every_theme_and_scheme_is_valid_gtk_css(env, theme_id, scheme):
    """Parsed by GTK itself: a typo in the generated stylesheet would show up
    as a parsing error here rather than as a silently unstyled widget."""
    env.kde.write_text(BREEZE_DARK)
    env.cfg.set_theme(theme_id)
    env.cfg.set_color_scheme(scheme)
    env.cfg.set_accent_color("#e01b8a")
    env.mgr.apply()
    assert env.mgr.css_errors == []
    assert env.mgr.palette.theme_id == theme_id


def test_css_variable_block_is_valid_when_forced_on(env):
    env.mgr._css_variables_supported = lambda: True
    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 16):
        pytest.skip("GTK < 4.16 does not support CSS custom properties (the manager correctly omits them)")
    env.mgr.apply()
    assert env.mgr.css_errors == []


def test_css_variables_omitted_on_old_gtk(env):
    if (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 16):
        pytest.skip("only meaningful on GTK < 4.16")
    env.mgr.apply()
    assert ":root" not in env.mgr.css


# --------------------------------------------------------------- live changes
def test_switching_theme_updates_palette_css_and_notifies_listeners(env):
    seen = []
    env.mgr.connect_changed(lambda p: seen.append(p.theme_id))
    env.mgr.apply()
    before = env.mgr.css
    env.mgr.set_theme("gruvbox-dark")
    assert env.mgr.palette.background == "#282828"
    assert env.mgr.css != before and "#282828" in env.mgr.css
    env.mgr.set_theme("gruvbox-light")
    assert env.mgr.palette.background == "#fbf1c7"
    assert seen[-2:] == ["gruvbox-dark", "gruvbox-light"]


def test_changes_are_persisted_through_config(env):
    env.mgr.set_theme("gruvbox-dark")
    env.mgr.set_accent_color("#123456")
    env.mgr.set_color_scheme("light")
    other = Config(env.db)                                  # a different Config over the same DB
    assert (other.theme, other.accent_color, other.color_scheme) == ("gruvbox-dark", "#123456", "light")


def test_disconnected_listener_is_not_called(env):
    calls = []
    hid = env.mgr.connect_changed(lambda p: calls.append(1))
    env.mgr.apply()
    env.mgr.disconnect_changed(hid)
    env.mgr.apply()
    assert len(calls) == 1


def test_a_failing_listener_does_not_break_others_or_apply(env):
    ok = []
    env.mgr.connect_changed(lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    env.mgr.connect_changed(lambda p: ok.append(1))
    env.mgr.apply()
    assert ok == [1]


def test_invalid_stored_theme_still_renders(env):
    env.cfg.set("theme", "vanished")
    env.mgr.apply()
    assert env.mgr.palette.theme_id == "default" and env.mgr.css_errors == []


# ----------------------------------------------------- libadwaita color scheme
def test_fixed_theme_forces_matching_adwaita_scheme(env):
    sm = env.mgr.style_manager
    env.mgr.set_theme("gruvbox-dark")
    assert sm.get_color_scheme() == Adw.ColorScheme.FORCE_DARK and sm.get_dark()
    env.mgr.set_theme("gruvbox-light")
    assert sm.get_color_scheme() == Adw.ColorScheme.FORCE_LIGHT and not sm.get_dark()


def test_adaptive_theme_follows_system_when_scheme_is_system(env):
    env.mgr.set_theme("default")
    env.mgr.set_color_scheme("system")
    assert env.mgr.style_manager.get_color_scheme() == Adw.ColorScheme.DEFAULT


@pytest.mark.parametrize("scheme,dark", [("light", False), ("dark", True)])
def test_explicit_scheme_forces_adwaita(env, scheme, dark):
    env.mgr.set_color_scheme(scheme)
    assert env.mgr.style_manager.get_dark() is dark and env.mgr.palette.is_dark is dark


def test_system_theme_uses_kde_scheme_and_forces_matching_adwaita(env):
    env.kde.write_text(BREEZE_DARK)
    env.mgr.set_theme("system")
    assert env.mgr.palette.source == "kde" and env.mgr.palette.background == "#232627"
    assert env.mgr.style_manager.get_dark()
    env.kde.write_text(BREEZE_LIGHT)
    env.mgr.apply()
    assert env.mgr.palette.background == "#eff0f1" and not env.mgr.style_manager.get_dark()


def test_system_theme_without_kde_falls_back_gracefully(env):
    env.mgr.set_theme("system")                             # no kdeglobals file exists
    assert env.mgr.palette.source == "theme" and env.mgr.css_errors == []


def test_system_accent_from_libadwaita_is_used_when_available(env, monkeypatch):
    """Newer libadwaita (>= 1.6) exposes the desktop accent; older doesn't. Fake
    the newer API so this path is exercised whichever is installed."""
    monkeypatch.setattr(env.mgr, "_system_accent", lambda: "#e66100")
    env.mgr.set_theme("system")
    assert env.mgr.palette.accent == "#e66100"


def test_missing_accent_api_is_tolerated(env, monkeypatch):
    class Old:                                              # libadwaita < 1.6: no accent methods at all
        def get_system_supports_accent_colors(self):
            raise AttributeError
    monkeypatch.setattr(env.mgr, "style_manager", Old(), raising=False)
    assert env.mgr._system_accent() is None


# ---------------------------------------------------- watching the desktop
def test_plasma_color_scheme_change_updates_the_open_app(env):
    """End to end: KConfig replaces kdeglobals atomically; the running app must
    notice and re-theme itself with no restart."""
    env.kde.write_text(BREEZE_DARK)
    env.mgr.set_theme("system")
    env.mgr.start_watching()
    try:
        assert env.mgr.palette.background == "#232627"
        tmp = env.kde.with_name("kdeglobals.tmp")
        tmp.write_text(BREEZE_LIGHT)
        os.replace(tmp, env.kde)                            # atomic replace, like KConfig
        assert pump(lambda: env.mgr.palette.background == "#eff0f1"), "app did not follow the Plasma scheme change"
        assert not env.mgr.palette.is_dark
    finally:
        env.mgr.close()


def test_desktop_changes_are_ignored_by_fixed_themes(env):
    env.mgr.set_theme("gruvbox-dark")
    env.mgr.start_watching()
    try:
        env.kde.write_text(BREEZE_LIGHT)
        pump(lambda: False, timeout=0.6)
        assert env.mgr.palette.background == "#282828"
    finally:
        env.mgr.close()


def test_burst_of_changes_is_debounced_into_one_apply(env):
    env.kde.write_text(BREEZE_DARK)
    env.mgr.set_theme("system")
    n = []
    env.mgr.connect_changed(lambda p: n.append(1))
    for _ in range(20):
        env.mgr._on_system_changed()
    pump(lambda: len(n) >= 1)
    pump(lambda: False, timeout=0.5)
    assert len(n) == 1


def test_close_removes_provider_and_listeners(env):
    env.mgr.connect_changed(lambda p: None)
    env.mgr.start_watching()
    env.mgr.close()
    assert env.mgr._listeners == {} and env.mgr._monitors == [] and env.mgr._signal_ids == []


# ------------------------------------------------------------- manager lookup
def test_ensure_theme_manager_reuses_for_same_database(env):
    other_config_same_db = Config(env.db)                   # like MainWindow's own Config(db)
    assert ensure_theme_manager(other_config_same_db) is env.mgr


def test_ensure_theme_manager_replaces_manager_bound_to_another_database(env, tmp_path):
    db2 = Database(tmp_path / "other.db")
    m2 = ensure_theme_manager(Config(db2))
    assert m2 is not env.mgr and m2.config.db is db2
    tm_mod.set_theme_manager(None)
    db2.close()


# ----------------------------------------------------------------- bar chart
cairo = pytest.importorskip("cairo")


def _bar_pixel(chart):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 120)
    cr = cairo.Context(surf)
    try:
        chart._draw(None, cr, 200, 120)
    except TypeError:
        pytest.skip("PyGObject cairo bridge unavailable")
    data, stride = surf.get_data(), surf.get_stride()
    x, y = 100, 60                                          # middle of the single full-height bar
    b, g, r, a = data[y * stride + x * 4: y * stride + x * 4 + 4]
    assert a > 0, "bar was not drawn"
    return (r * 255 // a, g * 255 // a, b * 255 // a)       # un-premultiply


def test_bar_chart_draws_with_the_palette_and_follows_theme_changes(env):
    from screentime.gui.widgets.bar_chart import BarChart
    chart = BarChart()
    chart.set_data(["a"], [1.0])
    env.mgr.set_theme("default")
    got = _bar_pixel(chart)
    want = T.parse_hex(env.mgr.palette.chart_1)
    assert all(abs(g - w) <= 3 for g, w in zip(got, want)), (got, want)
    env.mgr.set_theme("gruvbox-dark")
    got2 = _bar_pixel(chart)
    want2 = T.parse_hex("#fe8019")
    assert all(abs(g - w) <= 3 for g, w in zip(got2, want2)), (got2, want2)
    assert got != got2


def test_bar_chart_without_a_theme_manager_uses_fallback_palette(env):
    from screentime.gui.widgets.bar_chart import BarChart
    tm_mod.set_theme_manager(None)
    chart = BarChart()
    chart.set_data(["a"], [1.0])
    want = T.parse_hex(T.fallback_palette().chart_1)
    got = _bar_pixel(chart)
    assert all(abs(g - w) <= 3 for g, w in zip(got, want))


# --------------------------------------------------------------- Settings UI
@pytest.fixture
def view(env):
    from screentime.gui.views.settings import SettingsView
    return SettingsView(env.db, env.cfg, theme_manager=env.mgr)


def theme_index(view, theme_id):
    return next(i for i, t in enumerate(view._themes) if t.id == theme_id)


def test_settings_lists_every_registered_theme(view):
    names = [view._theme_row.get_model().get_string(i) for i in range(view._theme_row.get_model().get_n_items())]
    assert names == [t.name for t in T.list_themes()]
    assert any("Gruvbox Dark" in n for n in names) and any("System" in n for n in names)


def test_choosing_a_theme_in_settings_persists_and_applies(view, env):
    view._theme_row.set_selected(theme_index(view, "gruvbox-dark"))
    assert env.cfg.theme == "gruvbox-dark"
    assert env.mgr.palette.background == "#282828"


def test_color_scheme_row_is_disabled_and_shows_fixed_scheme_for_fixed_themes(view, env):
    env.cfg.set_color_scheme("light")
    view._theme_row.set_selected(theme_index(view, "gruvbox-dark"))
    assert not view._scheme_row.get_sensitive()
    assert view._scheme_row.get_selected() == 2             # "Dark": what the theme actually is...
    assert env.cfg.color_scheme == "light"                  # ...while the user's stored preference is kept
    assert "Gruvbox Dark" in view._scheme_row.get_subtitle()
    view._theme_row.set_selected(theme_index(view, "default"))
    assert view._scheme_row.get_sensitive()
    assert view._scheme_row.get_selected() == 1             # back to their "Light"


def test_choosing_a_color_scheme_persists_and_applies(view, env):
    view._scheme_row.set_selected(2)
    assert env.cfg.color_scheme == "dark" and env.mgr.palette.is_dark
    view._scheme_row.set_selected(1)
    assert env.cfg.color_scheme == "light" and not env.mgr.palette.is_dark


def test_accent_uses_a_real_gtk_color_chooser(view):
    assert isinstance(view._accent_btn, Gtk.ColorDialogButton)
    assert isinstance(view._accent_btn.get_dialog(), Gtk.ColorDialog)


def test_picking_an_accent_persists_applies_and_can_be_cleared(view, env):
    rgba = Gdk.RGBA()
    rgba.parse("#e01b8a")
    view._accent_btn.set_rgba(rgba)
    assert env.cfg.accent_color == "#e01b8a" and env.mgr.palette.accent == "#e01b8a"
    assert view._accent_clear_btn.get_sensitive() and "Custom" in view._accent_row.get_subtitle()
    view._accent_clear_btn.emit("clicked")
    assert env.cfg.accent_color == "" and env.mgr.palette.accent != "#e01b8a"
    assert not view._accent_clear_btn.get_sensitive() and "theme" in view._accent_row.get_subtitle()


def test_reset_restores_defaults_and_the_controls(view, env):
    view._theme_row.set_selected(theme_index(view, "gruvbox-light"))
    view._scheme_row.set_selected(2)
    rgba = Gdk.RGBA()
    rgba.parse("#123456")
    view._accent_btn.set_rgba(rgba)
    view._on_appearance_reset(None)
    assert (env.cfg.theme, env.cfg.color_scheme, env.cfg.accent_color) == ("default", "system", "")
    assert view._theme_row.get_selected() == theme_index(view, "default")
    assert view._scheme_row.get_selected() == 0 and view._scheme_row.get_sensitive()
    assert not view._accent_clear_btn.get_sensitive()


def test_syncing_the_controls_never_writes_to_the_config(view, env):
    """Programmatic updates of the rows must not be mistaken for user input."""
    env.cfg.set("theme", "gruvbox-light")                   # changed behind the UI's back
    view._sync_appearance_rows()
    assert env.cfg.theme == "gruvbox-light"
    assert view._theme_row.get_selected() == theme_index(view, "gruvbox-light")


def test_external_theme_change_is_reflected_in_the_controls(view, env):
    env.mgr.set_theme("gruvbox-dark")                       # e.g. changed elsewhere; listener re-syncs
    assert view._theme_row.get_selected() == theme_index(view, "gruvbox-dark")
    assert not view._scheme_row.get_sensitive()


def test_preview_exists_and_uses_semantic_classes_only(view):
    def walk(w):
        yield w
        c = w.get_first_child()
        while c is not None:
            yield from walk(c)
            c = c.get_next_sibling()
    classes = {cls for w in walk(view) for cls in w.get_css_classes()}
    assert {"st-preview", "st-swatch", "st-bg-accent", "st-bg-chart-1", "st-bg-error"} <= classes


def test_displaying_the_theme_accent_never_pins_it_as_a_custom_accent(view, env):
    """Regression guard: refreshing the accent button shows the theme's accent as
    its color. If that programmatic change were treated as user input it would be
    saved as a *custom* accent, silently pinning the user to the current theme's
    color (and 'Use theme accent' would then look like it did nothing)."""
    assert env.cfg.accent_color == ""
    for theme_id in ("gruvbox-dark", "gruvbox-light", "default", "dark"):
        env.mgr.set_theme(theme_id)
        view._sync_appearance_rows()
        assert env.cfg.accent_color == "", theme_id
        assert "theme" in view._accent_row.get_subtitle()


def test_sync_performs_no_database_writes(view, env, monkeypatch):
    writes = []
    real = env.db.set_setting
    monkeypatch.setattr(env.db, "set_setting", lambda k, v: (writes.append(k), real(k, v))[1])
    view._sync_appearance_rows()
    view._sync_appearance_rows()
    assert writes == []
