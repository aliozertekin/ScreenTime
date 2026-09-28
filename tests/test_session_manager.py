import time
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime.db import Database, local_day
from screentime.session_manager import SessionManager, FocusInfo


class FakeClock:
    """Deterministic wall+monotonic clock for tests. Advancing it moves both
    clocks together by default; jump_wall_only simulates an NTP step/clock
    change that must NOT affect measured durations."""
    def __init__(self, start=1_700_000_000.0):
        self.wall = start
        self.mono = 100_000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def jump_wall_only(self, seconds):
        self.wall += seconds  # monotonic untouched -> simulates clock step

    def now_wall(self):
        return self.wall

    def now_mono(self):
        return self.mono


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "sm.db")
    yield d
    d.close()


@pytest.fixture
def sm(db, clock):
    return SessionManager(db, wall_clock=clock.now_wall, mono_clock=clock.now_mono)


def total_seconds(db, key):
    app = next(a for a in db.list_apps() if a.key == key)
    rows = db.conn.execute("SELECT SUM(seconds) s FROM daily_totals WHERE app_id=?", (app.id,)).fetchone()
    return rows["s"] or 0


def test_basic_focus_and_close(sm, db, clock):
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(120)
    sm.on_focus_change(FocusInfo("code", "VS Code"))
    assert total_seconds(db, "firefox") == 120


def test_switching_apps_does_not_doublecount(sm, db, clock):
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(60)
    sm.on_focus_change(FocusInfo("code", "VS Code"))
    clock.advance(30)
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(10)
    sm.on_focus_change(None)

    assert total_seconds(db, "firefox") == 70
    assert total_seconds(db, "code") == 30


def test_repeated_focus_change_same_app_is_noop(sm, db, clock):
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(10)
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))  # same app again, e.g. new window
    clock.advance(10)
    sm.on_focus_change(None)
    assert total_seconds(db, "firefox") == 20


def test_idle_stops_counting(sm, db, clock):
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(30)
    idle_at = clock.now_wall()
    sm.on_idle_start_at(idle_at)
    clock.advance(600)  # 10 minutes of idle time must NOT be counted
    sm.on_idle_end(FocusInfo("firefox", "Firefox"))
    clock.advance(15)
    sm.on_focus_change(None)

    assert total_seconds(db, "firefox") == 30 + 15


def test_suspend_resume_does_not_count_sleep_time(sm, db, clock):
    sm.on_focus_change(FocusInfo("code", "VS Code"))
    clock.advance(45)
    sm.on_suspend()
    clock.advance(8 * 3600)  # 8 hours asleep
    sm.on_resume(FocusInfo("code", "VS Code"))
    clock.advance(20)
    sm.on_focus_change(None)

    assert total_seconds(db, "code") == 65


def test_wall_clock_jump_does_not_corrupt_duration(sm, db, clock):
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(30)
    clock.jump_wall_only(3600)  # e.g. NTP correction / manual clock change forward 1hr
    clock.advance(30)  # another 30s of *real* elapsed time (monotonic) passes
    sm.on_focus_change(None)

    # Only real elapsed (monotonic) time should be counted: 60s, not 3660s.
    assert total_seconds(db, "firefox") == 60


def test_heartbeat_is_crash_safe(sm, db, clock, tmp_path):
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(40)
    sm.heartbeat()
    # Simulate crash: never call on_focus_change/shutdown again.
    row = db.get_open_session()
    assert row is not None
    assert row["end_time"] is None  # still open -- crash safety uses last_heartbeat, not end_time
    assert row["last_heartbeat"] == int(clock.wall)


def test_excluded_app_is_not_tracked(sm, db, clock):
    app = db.get_or_create_app("steam", "Steam", None, None)
    db.set_excluded(app.id, True)

    sm.on_focus_change(FocusInfo("steam", "Steam"))
    clock.advance(100)
    sm.on_focus_change(None)

    assert total_seconds(db, "steam") == 0
    assert db.get_open_session() is None


def test_midnight_split_keeps_each_row_single_day(sm, db, clock):
    # Start 30 seconds before local midnight.
    midnight = 1_700_000_000.0 - (1_700_000_000.0 % 86400) + 86400
    clock.wall = midnight - 30
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(15)
    sm.heartbeat()  # still before midnight
    clock.advance(30)  # now past midnight
    sm.heartbeat()  # should trigger the split
    clock.advance(20)
    sm.on_focus_change(None)

    app = next(a for a in db.list_apps() if a.key == "firefox")
    rows = db.conn.execute(
        "SELECT day, seconds FROM daily_totals WHERE app_id=? ORDER BY day", (app.id,)
    ).fetchall()
    assert len(rows) == 2
    day1_seconds, day2_seconds = rows[0]["seconds"], rows[1]["seconds"]
    assert day1_seconds + day2_seconds == 65
    assert day1_seconds == 30  # the 30s that occurred before midnight
    assert day2_seconds == 35

    # Each stored session row's start_time must fall on its own `day` (the
    # end_time may legitimately equal the exact midnight boundary instant,
    # which is the shared closing/opening point between the two rows).
    for s in db.conn.execute("SELECT day, start_time, end_time FROM sessions WHERE app_id=?", (app.id,)):
        assert local_day(s["start_time"]) == s["day"]


def test_quick_open_close_still_recorded(sm, db, clock):
    sm.on_focus_change(FocusInfo("tiny", "Tiny App"))
    clock.advance(1)
    sm.on_focus_change(None)
    assert total_seconds(db, "tiny") == 1


def test_shutdown_closes_open_session(sm, db, clock):
    sm.on_focus_change(FocusInfo("firefox", "Firefox"))
    clock.advance(12)
    sm.shutdown()
    assert total_seconds(db, "firefox") == 12
    assert db.get_open_session() is None
