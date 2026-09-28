"""
Integration tests exercising the full path: SessionManager -> Database ->
stats query layer, simulating realistic app-switching behavior, a daemon
restart (new SessionManager/Database instance against the same file), and
verifying totals accumulate correctly rather than resetting.
"""
import time
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime.db import Database, local_day
from screentime.session_manager import SessionManager, FocusInfo
from screentime import stats


class FakeClock:
    def __init__(self, start):
        self.wall = start
        self.mono = 100000.0

    def advance(self, s):
        self.wall += s
        self.mono += s

    def now_wall(self):
        return self.wall

    def now_mono(self):
        return self.mono


def test_app_switching_persists_correctly(tmp_path):
    dbfile = tmp_path / "it.db"
    db = Database(dbfile)
    t0 = time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1))
    clock = FakeClock(t0)
    sm = SessionManager(db, wall_clock=clock.now_wall, mono_clock=clock.now_mono)

    # Simulate a realistic morning of app switching.
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(60 * 48)
    sm.heartbeat()
    sm.on_focus_change(FocusInfo("code", "VS Code"))
    clock.advance(60 * 42)
    sm.heartbeat()
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(60 * 20)
    sm.on_focus_change(FocusInfo("discord", "Discord"))
    clock.advance(60 * 5)
    sm.on_focus_change(None)

    day = local_day(t0)
    summary = stats.usage_in_range(db, day, day)
    by_key = {a.key: a.seconds for a in summary.apps}
    assert by_key["firefox"] == 60 * 48 + 60 * 20
    assert by_key["code"] == 60 * 42
    assert by_key["discord"] == 60 * 5
    assert summary.total_seconds == by_key["firefox"] + by_key["code"] + by_key["discord"]
    db.close()


def test_daemon_restart_does_not_reset_totals(tmp_path):
    """Reopening the same DB file (simulating tracker/computer restart) must
    add to existing totals, never reset them."""
    dbfile = tmp_path / "restart.db"
    t0 = time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1))

    db1 = Database(dbfile)
    clock1 = FakeClock(t0)
    sm1 = SessionManager(db1, wall_clock=clock1.now_wall, mono_clock=clock1.now_mono)
    sm1.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock1.advance(300)
    sm1.shutdown()  # clean close, e.g. `systemctl --user stop`
    db1.close()

    # "Reboot": brand new process, new Database/SessionManager over the same file.
    db2 = Database(dbfile)
    clock2 = FakeClock(t0 + 3600)  # an hour later
    sm2 = SessionManager(db2, wall_clock=clock2.now_wall, mono_clock=clock2.now_mono)
    sm2.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock2.advance(120)
    sm2.shutdown()

    day = local_day(t0)
    summary = stats.usage_in_range(db2, day, day)
    firefox = next(a for a in summary.apps if a.key == "firefox")
    assert firefox.seconds == 300 + 120
    assert firefox.session_count == 2
    db2.close()


def test_crash_then_restart_keeps_prior_progress(tmp_path):
    dbfile = tmp_path / "crash_restart.db"
    t0 = time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1))

    db1 = Database(dbfile)
    clock1 = FakeClock(t0)
    sm1 = SessionManager(db1, wall_clock=clock1.now_wall, mono_clock=clock1.now_mono)
    sm1.on_focus_change(FocusInfo("code", "VS Code"))
    clock1.advance(90)
    sm1.heartbeat()
    clock1.advance(500)  # daemon is killed here without a heartbeat/close for this stretch
    # No shutdown() call -- simulates SIGKILL / power loss.
    db1.close()

    # Recovery happens automatically when the DB is reopened.
    db2 = Database(dbfile)
    summary = stats.usage_in_range(db2, local_day(t0), local_day(t0))
    code = next(a for a in summary.apps if a.key == "code")
    assert code.seconds == 90  # only the last committed heartbeat, not the extra 500s
    assert db2.get_open_session() is None
    db2.close()


def test_multiple_windows_same_app_counted_once(tmp_path):
    """Two different windows of the same application (e.g. two Firefox
    windows) must resolve to the same canonical app and not double count
    when the daemon reports focus moving between them."""
    dbfile = tmp_path / "multiwin.db"
    t0 = time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1))
    db = Database(dbfile)
    clock = FakeClock(t0)
    sm = SessionManager(db, wall_clock=clock.now_wall, mono_clock=clock.now_mono)

    # Both windows share wm_class "firefox" (different titles, doesn't matter -- key is the same).
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(30)
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))  # user alt-tabbed to 2nd Firefox window
    clock.advance(30)
    sm.on_focus_change(None)

    assert len(db.list_apps()) == 1
    summary = stats.usage_in_range(db, local_day(t0), local_day(t0))
    assert summary.apps[0].seconds == 60
    assert summary.apps[0].session_count == 1  # a single continuous session, not two
    db.close()


def test_weekly_and_monthly_rollups(tmp_path):
    dbfile = tmp_path / "rollup.db"
    db = Database(dbfile)
    base = time.mktime((2026, 3, 2, 9, 0, 0, 0, 0, -1))  # a Monday
    for day_offset, seconds in [(0, 600), (1, 300), (2, 900), (10, 1200)]:
        clock = FakeClock(base + day_offset * 86400)
        sm = SessionManager(db, wall_clock=clock.now_wall, mono_clock=clock.now_mono)
        sm.on_focus_change(FocusInfo("firefox", "Firefox"))
        clock.advance(seconds)
        sm.shutdown()

    week1 = stats.usage_in_range(db, local_day(base), local_day(base + 6 * 86400))
    assert week1.total_seconds == 600 + 300 + 900

    everything = stats.usage_in_range(db, local_day(base), local_day(base + 20 * 86400))
    assert everything.total_seconds == 600 + 300 + 900 + 1200
    db.close()


def test_app_detail_stats(tmp_path):
    dbfile = tmp_path / "detail.db"
    db = Database(dbfile)
    # app_detail()'s "today" figures are anchored to the real wall-clock date
    # (datetime.date.today()), so this test must use "now" rather than a
    # fixed historical date to exercise that path meaningfully.
    t0 = time.time() - 4000  # a bit earlier today, comfortably clear of midnight
    clock = FakeClock(t0)
    sm = SessionManager(db, wall_clock=clock.now_wall, mono_clock=clock.now_mono)

    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(600)  # 10 min session
    sm.on_focus_change(None)
    clock.advance(3600)
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(1200)  # 20 min session
    sm.on_focus_change(None)

    app = next(a for a in db.list_apps() if a.key == "firefox")
    detail = stats.app_detail(db, app.id)
    assert detail.session_count == 2
    assert detail.longest_session_seconds == 1200
    assert detail.avg_session_seconds == (600 + 1200) / 2
    assert detail.today_seconds == 600 + 1200
    assert len(detail.sessions_today) == 2
    db.close()
