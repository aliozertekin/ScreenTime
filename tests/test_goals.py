"""Goals: usage vs goal vs warning threshold, persistence, and de-duplicated local notifications."""
import json

import pytest

from screentime import goals as G
from screentime import stats
from screentime.db import Database
from screentime.notifications import Notifier, NullNotifier, clip

DAY = "2026-10-05"


class Recorder(Notifier):
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, title, body):
        if self.ok:
            self.sent.append((title, body))
        return self.ok


def add_usage(db, key, seconds, name=None, day=DAY):
    app = db.get_or_create_app(key, name or key.title(), None, None)
    db._bump_daily_total(app.id, day, seconds, 1)
    return app


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def test_defaults_are_off_and_nothing_is_evaluated(db):
    g = G.load(db)
    assert not g.enabled and g.notify and g.warn_percent == 80 and not g.has_any()
    assert G.evaluate(db, g, DAY) == []


def test_goals_persist_and_are_validated(db):
    G.save(db, G.Goals(enabled=True, notify=False, warn_percent=150, daily_total_seconds=99 * 3600,
                       app_seconds={"firefox": 7200, "": 5, "gone": 0}))
    g = G.load(db)
    assert g.enabled and not g.notify
    assert g.warn_percent == 99                       # clamped
    assert g.daily_total_seconds == 24 * 3600         # capped at a day
    assert g.app_seconds == {"firefox": 7200}         # empty key / zero goal dropped


def test_damaged_setting_degrades_to_defaults_without_being_rewritten(db):
    db.set_setting(G.SETTING_KEY, "{not json")
    assert G.load(db) == G.Goals()
    assert db.get_setting(G.SETTING_KEY) == "{not json"
    db.set_setting(G.SETTING_KEY, json.dumps({"enabled": True, "warn_percent": "high", "app_seconds": []}))
    g = G.load(db)
    assert g.enabled and g.warn_percent == 80 and g.app_seconds == {}


@pytest.mark.parametrize("used,state", [(0, G.OK), (5759, G.OK), (5760, G.WARNING), (7199, G.WARNING),
                                         (7200, G.REACHED), (9000, G.REACHED)])
def test_usage_goal_and_threshold_are_distinct(db, used, state):
    add_usage(db, "firefox", used)
    G.save(db, G.Goals(enabled=True, app_seconds={"firefox": 7200}))      # 80% of 2h = 1h36m = 5760 s
    (s,) = G.evaluate(db, day=DAY)
    assert (s.used, s.limit, s.warn_at, s.state) == (used, 7200, 5760, state)
    assert s.remaining == max(0, 7200 - used)


def test_total_goal_uses_the_same_numbers_as_the_dashboard_and_skips_excluded_apps(db):
    add_usage(db, "a", 3600)
    b = add_usage(db, "b", 3600)
    db.set_excluded(b.id, True)
    G.save(db, G.Goals(enabled=True, daily_total_seconds=7200))
    (s,) = G.evaluate(db, day=DAY)
    assert s.used == stats.usage_in_range(db, DAY, DAY).total_seconds == 3600 and s.state == G.OK


def test_goal_for_excluded_or_unseen_app(db):
    ex = add_usage(db, "x", 9000)
    db.set_excluded(ex.id, True)
    G.save(db, G.Goals(enabled=True, app_seconds={"x": 60, "never-seen": 60}))
    statuses = {s.key: s for s in G.evaluate(db, day=DAY)}
    assert "x" not in statuses and statuses["never-seen"].used == 0


def test_disabled_goals_evaluate_to_nothing(db):
    add_usage(db, "a", 99999)
    G.save(db, G.Goals(enabled=False, daily_total_seconds=60))
    assert G.evaluate(db, day=DAY) == []


def make(db, notifier=None):
    return G.GoalNotifier(db, notifier or Recorder(), today=lambda: DAY)


def test_warning_then_reached_each_notify_once(db):
    rec = Recorder()
    n = make(db, rec)
    G.save(db, G.Goals(enabled=True, app_seconds={"firefox": 1000}, warn_percent=80))
    app = add_usage(db, "firefox", 500)
    assert n.check() == 0
    db._bump_daily_total(app.id, DAY, 350, 0)          # 850 >= 800
    assert n.check() == 1 and n.check() == 0 and n.check() == 0       # no spam on later checks
    assert "80%" in rec.sent[0][0] and "Firefox" in rec.sent[0][1]
    db._bump_daily_total(app.id, DAY, 200, 0)          # 1050 >= 1000
    assert n.check() == 1 and n.check() == 0
    assert rec.sent[1][0] == "Daily goal reached"


def test_jumping_straight_past_the_limit_sends_only_the_limit_notification(db):
    rec = Recorder()
    G.save(db, G.Goals(enabled=True, daily_total_seconds=1000))
    add_usage(db, "a", 5000)
    n = make(db, rec)
    assert n.check() == 1 and n.check() == 0
    assert rec.sent[0][0] == "Daily goal reached"


def test_a_restart_does_not_repeat_notifications(db):
    rec = Recorder()
    G.save(db, G.Goals(enabled=True, daily_total_seconds=1000))
    add_usage(db, "a", 2000)
    assert make(db, rec).check() == 1
    assert make(db, rec).check() == 0                  # a brand-new notifier object (daemon restart)
    assert len(rec.sent) == 1


def test_a_new_day_notifies_again(db):
    rec = Recorder()
    G.save(db, G.Goals(enabled=True, daily_total_seconds=1000))
    add_usage(db, "a", 2000, day="2026-10-05")
    add_usage(db, "a", 2000, day="2026-10-06")
    day = {"d": "2026-10-05"}
    n = G.GoalNotifier(db, rec, today=lambda: day["d"])
    assert n.check() == 1 and n.check() == 0
    day["d"] = "2026-10-06"
    assert n.check() == 1


def test_raising_the_goal_then_hitting_it_is_a_new_event(db):
    rec = Recorder()
    G.save(db, G.Goals(enabled=True, daily_total_seconds=1000))
    app = add_usage(db, "a", 1100)
    n = make(db, rec)
    assert n.check() == 1
    G.save(db, G.Goals(enabled=True, daily_total_seconds=2000))
    assert n.check() == 0
    db._bump_daily_total(app.id, DAY, 1000, 0)
    assert n.check() == 1


def test_notifications_can_be_disabled_while_goals_stay_visible(db):
    rec = Recorder()
    G.save(db, G.Goals(enabled=True, notify=False, daily_total_seconds=10))
    add_usage(db, "a", 100)
    assert make(db, rec).check() == 0 and rec.sent == []
    assert G.evaluate(db, day=DAY)[0].state == G.REACHED


def test_undelivered_notification_is_retried_not_lost_and_not_duplicated(db):
    rec = Recorder(ok=False)
    G.save(db, G.Goals(enabled=True, daily_total_seconds=10))
    add_usage(db, "a", 100)
    n = make(db, rec)
    assert n.check() == 0 and n.check() == 0
    rec.ok = True
    assert n.check() == 1 and n.check() == 0


def test_null_notifier_and_text_clipping():
    assert NullNotifier().send("a", "b") is False
    assert clip("x" * 500, 20) == "x" * 19 + "\u2026" and clip("a\n  b", 20) == "a b"


def test_notifications_make_no_network_use():
    import ast, pathlib
    for mod in ("notifications.py", "goals.py", "platform/windows/notify.py"):
        tree = ast.parse((pathlib.Path(G.__file__).parent / mod).read_text())
        names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
        assert not names & {"socket", "urllib", "http", "ssl", "requests"}


def test_windows_balloon_text_is_clipped_to_the_shell_structure():
    from screentime.platform.windows import notify
    t, b = notify.fit("T" * 200, "B" * 900)
    assert len(t) <= 63 and len(b) <= 255


def test_freedesktop_notifier_reports_failure_without_a_session_bus():
    from screentime.notifications import FreedesktopNotifier
    assert FreedesktopNotifier().send("t", "b") is False          # conftest points D-Bus at nothing


# ------------------------------------------------------------- daemon integration
def test_daemon_checks_goals_from_its_tick_and_throttles(db):
    from screentime.daemon import Daemon, GOAL_CHECK_INTERVAL_SECONDS
    rec = Recorder()
    d = Daemon(db)
    d.goal_notifier = G.GoalNotifier(db, rec)                 # today's real date
    G.save(db, G.Goals(enabled=True, daily_total_seconds=60))
    app = db.get_or_create_app("a", "A", None, None)
    db._bump_daily_total(app.id, stats.today_str(), 600, 1)
    clock = {"t": 1000.0}
    d._mono = lambda: clock["t"]
    d._last_goal_check = float("-inf")
    d._maybe_check_goals()
    assert len(rec.sent) == 1
    calls = []
    d.goal_notifier.check = lambda: calls.append(1)
    clock["t"] += GOAL_CHECK_INTERVAL_SECONDS - 1
    d._maybe_check_goals()
    assert calls == []                                        # throttled
    clock["t"] += 2
    d._maybe_check_goals()
    assert calls == [1]


def test_a_failing_goal_check_never_breaks_tracking(db):
    from screentime.daemon import Daemon
    d = Daemon(db)
    d.goal_notifier.check = lambda: 1 / 0
    d._last_goal_check = float("-inf")
    d._maybe_check_goals()                                    # logged, not raised
