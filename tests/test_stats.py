import datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime.db import Database
from screentime.session_manager import SessionManager, FocusInfo
from screentime import stats


def test_format_duration_boundaries():
    assert stats.format_duration(0) == "0s"
    assert stats.format_duration(5) == "5s"
    assert stats.format_duration(59) == "59s"
    assert stats.format_duration(60) == "1m"
    assert stats.format_duration(65) == "1m 05s"
    assert stats.format_duration(9 * 60 + 59) == "9m 59s"
    assert stats.format_duration(10 * 60) == "10m"
    assert stats.format_duration(3600) == "1h 00m"
    assert stats.format_duration(3600 + 61) == "1h 01m"
    assert stats.format_duration(2 * 3600 + 34 * 60) == "2h 34m"


def test_week_start_is_monday():
    # 2026-03-10 is a Tuesday.
    d = datetime.date(2026, 3, 10)
    assert stats.week_start_str(d) == "2026-03-09"  # the preceding Monday
    monday = datetime.date(2026, 3, 9)
    assert stats.week_start_str(monday) == "2026-03-09"  # Monday's own week starts on itself


def test_month_start():
    d = datetime.date(2026, 3, 17)
    assert stats.month_start_str(d) == "2026-03-01"


def test_usage_in_range_excludes_excluded_apps_by_default(tmp_path):
    db = Database(tmp_path / "s.db")
    t0 = time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1))

    class FakeClock:
        def __init__(self, w):
            self.w = w
            self.m = 1000.0
        def wall(self): return self.w
        def mono(self): return self.m
        def advance(self, s):
            self.w += s
            self.m += s

    c = FakeClock(t0)
    sm = SessionManager(db, wall_clock=c.wall, mono_clock=c.mono)
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    c.advance(100)
    sm.on_focus_change(FocusInfo("steam", "Steam"))
    c.advance(200)
    sm.shutdown()

    steam = next(a for a in db.list_apps() if a.key == "steam")
    db.set_excluded(steam.id, True)

    day = stats.today_str()  # not used for range, just to exercise the helper
    from screentime.db import local_day
    d = local_day(t0)
    summary = stats.usage_in_range(db, d, d, include_excluded=False)
    keys = {a.key for a in summary.apps}
    assert "steam" not in keys
    assert "firefox" in keys

    summary_incl = stats.usage_in_range(db, d, d, include_excluded=True)
    keys_incl = {a.key for a in summary_incl.apps}
    assert "steam" in keys_incl
    db.close()


def test_usage_in_range_percentages_sum_to_100(tmp_path):
    db = Database(tmp_path / "p.db")
    t0 = time.mktime((2026, 3, 10, 9, 0, 0, 0, 0, -1))

    class FakeClock:
        def __init__(self, w):
            self.w = w
            self.m = 1000.0
        def wall(self): return self.w
        def mono(self): return self.m
        def advance(self, s):
            self.w += s
            self.m += s

    c = FakeClock(t0)
    sm = SessionManager(db, wall_clock=c.wall, mono_clock=c.mono)
    sm.on_focus_change(FocusInfo("a", "A"))
    c.advance(300)
    sm.on_focus_change(FocusInfo("b", "B"))
    c.advance(700)
    sm.shutdown()

    from screentime.db import local_day
    d = local_day(t0)
    summary = stats.usage_in_range(db, d, d)
    total_pct = sum(a.percent for a in summary.apps)
    assert abs(total_pct - 100.0) < 0.01
    db.close()


def test_all_time_summary_empty_db_no_crash(tmp_path):
    db = Database(tmp_path / "empty.db")
    summary = stats.all_time_summary(db)
    assert summary.total_seconds == 0
    assert summary.apps == []
    db.close()
