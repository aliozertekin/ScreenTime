"""Dashboard data (pure) and the dashboard widget (empty state, populated, large data, theme, accessibility)."""
import datetime
import os
import time

import pytest

from screentime import stats
from screentime.db import Database

TODAY = datetime.date(2026, 10, 5)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "_today", lambda: TODAY)
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def put(db, key, days_ago, seconds, name=None):
    app = db.get_app_by_key(key) or db.get_or_create_app(key, name or key.title(), None, None)
    day = (TODAY - datetime.timedelta(days=days_ago)).isoformat()
    db._bump_daily_total(app.id, day, seconds, 1)
    return app


# ----------------------------------------------------------------- data layer
def test_empty_database_has_no_data_and_zero_everything(db):
    for p in stats.PERIODS:
        d = stats.dashboard_data(db, p)
        assert not d.has_data and d.summary.apps == [] and d.average_seconds == 0 and d.busiest_day is None
        assert len(d.daily) == (7 if p < 30 else 30) and all(s == 0 for _d, s in d.daily)


def test_today_vs_yesterday_and_percentages(db):
    put(db, "firefox", 0, 9000)
    put(db, "code", 0, 3000)
    put(db, "firefox", 1, 10000)
    d = stats.dashboard_data(db, 1)
    assert d.summary.total_seconds == 12000 and d.comparison.previous == 10000
    assert [a.key for a in d.summary.apps] == ["firefox", "code"]
    assert [round(a.percent) for a in d.summary.apps] == [75, 25]
    assert "20% more than yesterday" in stats.describe_change(d.comparison, d.previous_label)


def test_seven_and_thirty_day_windows_and_previous_periods(db):
    for ago in range(0, 14):
        put(db, "a", ago, 100)
    put(db, "a", 20, 1000)
    d7 = stats.dashboard_data(db, 7)
    assert d7.summary.total_seconds == 700 and d7.comparison.previous == 700 and d7.average_seconds == 100
    assert d7.daily[0][0] == (TODAY - datetime.timedelta(days=6)).isoformat() and d7.daily[-1][0] == TODAY.isoformat()
    d30 = stats.dashboard_data(db, 30)
    assert d30.summary.total_seconds == 1400 + 1000 and len(d30.daily) == 30
    assert d30.comparison.previous == 0 and "No usage recorded" in stats.describe_change(d30.comparison, d30.previous_label)


def test_zero_filled_days_and_busiest_day(db):
    put(db, "a", 2, 500)
    put(db, "a", 5, 900)
    d = stats.dashboard_data(db, 7)
    assert dict(d.daily)[(TODAY - datetime.timedelta(days=5)).isoformat()] == 900
    assert d.busiest_day == ((TODAY - datetime.timedelta(days=5)).isoformat(), 900)


def test_excluded_apps_are_not_counted(db):
    app = put(db, "secret", 0, 5000)
    put(db, "ok", 0, 100)
    db.set_excluded(app.id, True)
    d = stats.dashboard_data(db, 1)
    assert d.summary.total_seconds == 100 and [a.key for a in d.summary.apps] == ["ok"]


@pytest.mark.parametrize("cur,prev,text", [(7200, 3600, "100% more"), (1800, 3600, "50% less"),
                                            (3600, 3600, "About the same"), (3630, 3600, "About the same"),
                                            (100, 0, "No usage recorded")])
def test_change_wording(cur, prev, text):
    assert text in stats.describe_change(stats.Comparison(cur, prev), "yesterday")


def test_invalid_period_is_rejected(db):
    with pytest.raises(ValueError):
        stats.dashboard_data(db, 14)


def test_large_history_stays_fast(db):
    """400 applications x 1,000 days = 400,000 aggregate rows."""
    db._conn.execute("BEGIN")
    for i in range(400):
        db._conn.execute("INSERT INTO apps(key,display_name,excluded,first_seen,last_seen) VALUES (?,?,0,0,0)",
                         (f"app{i}", f"App {i}"))
    db._conn.execute(
        "INSERT INTO daily_totals(app_id,day,seconds,session_count) "
        "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i < 999) "
        "SELECT a.id, date(?, '-' || n.i || ' days'), 60 + (a.id * n.i) % 3000, 1 FROM apps a, n",
        (TODAY.isoformat(),))
    db._conn.execute("COMMIT")
    assert db._conn.execute("SELECT COUNT(*) FROM daily_totals").fetchone()[0] == 400_000
    t = time.perf_counter()
    for p in stats.PERIODS:
        d = stats.dashboard_data(db, p)
        assert d.has_data and len(d.summary.apps) == 400
    assert time.perf_counter() - t < 3.0


# ------------------------------------------------------------------- widgets
pytest.importorskip("gi")
HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
needs_display = pytest.mark.skipif(not HAVE_DISPLAY, reason="GTK widget tests need a display (run under xvfb-run)")


def walk(w):
    yield w
    c = w.get_first_child()
    while c is not None:
        yield from walk(c)
        c = c.get_next_sibling()


@pytest.fixture
def gtk():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    Adw.init()
    return Gtk, Adw


@needs_display
def test_empty_dashboard_shows_a_helpful_empty_state(db, gtk):
    Gtk, Adw = gtk
    from screentime.gui.views.dashboard import DashboardView
    v = DashboardView(db)
    pages = [w for w in walk(v) if isinstance(w, Adw.StatusPage)]
    assert len(pages) == 1 and pages[0].get_title() == "No usage recorded yet"
    assert "Diagnostics" in pages[0].get_description()


@needs_display
def test_populated_dashboard_shows_total_top_apps_trend_and_comparison(db, gtk):
    Gtk, Adw = gtk
    from screentime.gui.views.dashboard import DashboardView
    from screentime.gui.widgets.bar_chart import BarChart
    put(db, "firefox", 0, 9000, "Firefox")
    put(db, "code", 0, 3000, "VS Code")
    put(db, "firefox", 1, 10000)
    v = DashboardView(db)
    labels = [w.get_label() for w in walk(v) if isinstance(w, Gtk.Label)]
    assert "2h 30m" in labels and "Firefox" in labels and "VS Code" in labels and "75%" in labels
    assert any("20% more than yesterday" in t for t in labels)
    assert not [w for w in walk(v) if isinstance(w, Adw.StatusPage)]
    charts = [w for w in walk(v) if isinstance(w, BarChart)]
    assert len(charts) == 1 and len(charts[0]._values) == 7


@needs_display
def test_period_switch_changes_title_and_data_and_unchanged_data_does_not_rebuild(db, gtk):
    Gtk, Adw = gtk
    from screentime.gui.views.dashboard import DashboardView
    for ago in range(0, 30):
        put(db, "a", ago, 600)
    v = DashboardView(db)
    v._period_buttons[30].set_active(True)
    assert v.title_label.get_label() == "Last 30 days"
    labels = [w.get_label() for w in walk(v) if isinstance(w, Gtk.Label)]
    assert "5h 00m" in labels and any(t.startswith("Average") for t in labels)
    first = v.body.get_first_child()
    v.refresh()
    assert v.body.get_first_child() is first                     # same data -> same widgets (no flicker)
    put(db, "a", 0, 60)
    v.refresh()
    assert v.body.get_first_child() is not first


@needs_display
def test_dashboard_shows_goal_progress_with_accessible_text(db, gtk):
    Gtk, Adw = gtk
    from screentime import goals as G
    from screentime.gui.views.dashboard import DashboardView
    put(db, "firefox", 0, 7300, "Firefox")
    G.save(db, G.Goals(enabled=True, app_seconds={"firefox": 7200}, daily_total_seconds=36000))
    v = DashboardView(db)
    rows = [w for w in walk(v) if isinstance(w, Adw.ActionRow) and w.get_title() == "Firefox"]
    assert rows and "Goal reached" in rows[0].get_subtitle() and "warning at 1h 36m" in rows[0].get_subtitle()
    assert any(w.get_title() == "All applications" for w in walk(v) if isinstance(w, Adw.ActionRow))
    bars = [w for w in walk(v) if isinstance(w, Gtk.ProgressBar) and w.has_css_class("error")]
    assert len(bars) == 1                                         # state is also in the text, never colour alone


@needs_display
def test_dashboard_builds_quickly_with_a_large_history(db, gtk):
    from screentime.gui.views.dashboard import DashboardView
    db._conn.execute("BEGIN")
    for i in range(300):
        db._conn.execute("INSERT INTO apps(key,display_name,excluded,first_seen,last_seen) VALUES (?,?,0,0,0)", (f"a{i}", f"App {i}"))
    db._conn.execute("INSERT INTO daily_totals(app_id,day,seconds,session_count) "
                     "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i < 799) "
                     "SELECT a.id, date(?, '-' || n.i || ' days'), 100 + (a.id*n.i)%900, 1 FROM apps a, n", (TODAY.isoformat(),))
    db._conn.execute("COMMIT")
    t = time.perf_counter()
    v = DashboardView(db)
    v._period_buttons[30].set_active(True)
    assert time.perf_counter() - t < 3.0
    assert len([w for w in walk(v) if w.__class__.__name__ == "AppUsageRow"]) == 8      # only the top few are built
