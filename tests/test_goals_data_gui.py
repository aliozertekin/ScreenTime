"""GTK widgets for goals, export/backup and updates (run under xvfb-run)."""
import json
import os

import pytest

pytest.importorskip("gi")
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("GTK widget tests need a display (run under xvfb-run)", allow_module_level=True)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib

Adw.init()

from screentime import goals as G
from screentime import stats, updates
from screentime.config import Config
from screentime.db import Database
from screentime.gui import dialogs
from screentime.gui.views.data_group import DataGroup
from screentime.gui.views.goals_group import GoalsGroup
from screentime.gui.views.settings import SettingsView
from screentime.gui.views.updates_group import UpdatesGroup


@pytest.fixture(autouse=True)
def quiet_dialogs(monkeypatch):
    shown = []
    monkeypatch.setattr(dialogs, "notice", lambda parent, heading, body, extra=None: shown.append((heading, body)))
    monkeypatch.setattr(dialogs, "confirm", lambda parent, heading, body, ok, cb, **k: shown.append((heading, body, cb)))
    return shown


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.get_or_create_app("firefox", "Firefox", None, None)
    d.get_or_create_app("code", "VS Code", None, None)
    yield d
    d.close()


def test_goals_group_starts_off_and_disables_dependent_rows(db):
    g = GoalsGroup(db)
    assert not g.enabled_row.get_active() and not g.warn_row.get_sensitive() and not g.total_row.get_sensitive()


def test_goals_group_persists_every_control(db):
    g = GoalsGroup(db)
    g.enabled_row.set_active(True)
    g.total_row.set_value(360)
    g.warn_row.set_value(70)
    g.notify_row.set_active(False)
    saved = G.load(db)
    assert (saved.enabled, saved.daily_total_seconds, saved.warn_percent, saved.notify) == (True, 21600, 70, False)
    again = GoalsGroup(db)                                     # a fresh window after a restart
    assert again.total_row.get_value() == 360 and again.warn_row.get_value() == 70 and again.enabled_row.get_active()
    assert again.warn_row.get_sensitive()


def test_goals_group_adds_changes_and_removes_an_application_goal(db):
    g = GoalsGroup(db)
    g.enabled_row.set_active(True)
    names = [a.display_name for a in g._app_choices]
    g.add_row.set_selected(names.index("Firefox"))
    g._on_add_app(None)
    assert G.load(db).app_seconds == {"firefox": 3600}
    assert [a.display_name for a in g._app_choices] == ["VS Code"]
    g._rows[0].set_value(120)
    assert G.load(db).app_seconds == {"firefox": 7200}
    g._rows[0].set_value(0)
    assert G.load(db).app_seconds == {}


def test_goals_status_rows_show_usage_goal_and_threshold_separately(db):
    app = db.get_app_by_key("firefox")
    db._bump_daily_total(app.id, stats.today_str(), 6000, 1)
    G.save(db, G.Goals(enabled=True, app_seconds={"firefox": 7200}))
    g = GoalsGroup(db)
    text = g._status_rows[0].get_subtitle()
    assert "Usage 1h 40m of 2h" in text and "Close to the limit" in text and "Warning at 1h 36m" in text


def test_goals_group_empty_state(db):
    assert GoalsGroup(db)._status_rows[0].get_title() == "No goals to show"


def test_settings_view_contains_the_new_groups_and_refreshes(db):
    v = SettingsView(db, Config(db))
    assert isinstance(v.goals_group, GoalsGroup) and isinstance(v.data_group, DataGroup)
    assert isinstance(v.updates_group, UpdatesGroup)
    v.refresh()


def test_export_actions_write_files_and_say_they_are_unencrypted(db, tmp_path, quiet_dialogs):
    d = DataGroup(db)
    app = db.get_app_by_key("firefox")
    sid = db.open_session(app.id, 1_790_000_000)
    db.close_session(sid, 1_790_000_100, "idle")
    msg = d.do_export("json", str(tmp_path / "x.json"))
    assert json.loads((tmp_path / "x.json").read_text())["sessions"][0]["duration_seconds"] == 100
    assert "not encrypted" in msg
    d.do_export("csv", str(tmp_path / "x.csv"))
    assert (tmp_path / "x.csv").read_text().startswith("session_id,")
    bad = d.do_export("csv", str(tmp_path / "no" / "such" / "dir" / "\0x"))
    assert "could not be written" in bad and quiet_dialogs[-1][0] == "Export failed"


def test_updates_group_is_manual_and_can_be_disabled(db):
    got = []
    u = UpdatesGroup(db, fetch=lambda url: got.append(url) or json.dumps(
        {"tag_name": "v99.0.0", "html_url": f"https://github.com/{updates.REPO}/releases/tag/v99.0.0"}).encode())
    assert got == []                                          # constructing the widget does not touch the network
    assert u.allow_row.get_active() and u.check_btn.get_sensitive()
    u.show_result(updates.check(allowed=True, fetch=u._fetch))
    assert u.open_btn.get_visible() and "99.0.0" in u.check_row.get_subtitle()
    u.allow_row.set_active(False)
    assert db.get_setting(updates.SETTING_ENABLED) == "false" and not u.check_btn.get_sensitive()
    assert not u.open_btn.get_visible()
    u2 = UpdatesGroup(db)
    assert not u2.allow_row.get_active() and not u2.check_btn.get_sensitive()


def test_updates_group_reports_offline_without_raising(db):
    import urllib.error
    def offline(url): raise urllib.error.URLError("down")
    u = UpdatesGroup(db, fetch=offline)
    u.show_result(updates.check(allowed=True, fetch=offline))
    assert "offline" in u.check_row.get_subtitle().lower() and not u.open_btn.get_visible()


def test_updates_check_runs_off_the_ui_thread_and_reports_back(db):
    u = UpdatesGroup(db, fetch=lambda url: json.dumps({"tag_name": "v0.0.1"}).encode())
    u.start_check()
    ctx = GLib.MainContext.default()
    for _ in range(200):
        while ctx.iteration(False):
            pass
        if "latest version" in u.check_row.get_subtitle():
            break
        import time; time.sleep(0.01)
    assert "latest version" in u.check_row.get_subtitle()


from pathlib import Path as _P
from test_updates import _imports as _imports, PKG as _PKG


def test_tracking_and_storage_code_never_import_the_update_checker():
    importers = sorted(str(p.relative_to(_PKG)) for p in _PKG.rglob("*.py")
                       if any(m.endswith("updates") or m.endswith(".updates") for m in _imports(p)) and p.name != "updates.py")
    assert importers == ["gui/views/updates_group.py"]
