"""Sleep/resume behaviour of the real Daemon, driven by a fake clock that models
Linux suspend faithfully: wall time keeps going, CLOCK_MONOTONIC stands still.

Guarantees: time asleep is never counted; tracking resumes on wake by itself;
a session can never straddle a sleep; a lost/late signal can never leave
tracking paused forever."""
import pytest

import screentime.daemon as d
from screentime.daemon import Daemon
from screentime.db import Database
from screentime.idle_detector import IdleDetector
from screentime.session_manager import SessionManager
from screentime.window_detector import RawFocus, WindowDetector


class Clock:
    def __init__(self):
        self.wall = 1_800_000_000.0   # mid-day, far from midnight
        self.mono = 5_000.0

    def advance(self, s):             # machine awake
        self.wall += s
        self.mono += s

    def sleep(self, s):               # machine suspended: monotonic does NOT move
        self.wall += s


class FakeWindow(WindowDetector):
    name = "fake"
    def __init__(self): self.focus = RawFocus(identifier="brave-browser", pid=None, title=None)
    def is_supported(self): return True
    def get_focused(self): return self.focus


class FakeIdle(IdleDetector):
    name = "fake-idle"
    def __init__(self): self.idle = 0.0
    def get_idle_seconds(self): return self.idle


class FakeParams:
    def __init__(self, going): self.going = going
    def unpack(self): return (self.going,)


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock, win, idle = Clock(), FakeWindow(), FakeIdle()
    monkeypatch.setattr(d, "create_window_detector", lambda: win)
    monkeypatch.setattr(d, "create_idle_detector", lambda: idle)
    db = Database(tmp_path / "sleep.db")
    daemon = Daemon(db)
    daemon.session_manager = SessionManager(db, wall_clock=lambda: clock.wall, mono_clock=lambda: clock.mono)
    daemon._wall, daemon._mono = (lambda: clock.wall), (lambda: clock.mono)
    daemon.inhibitor_events = []
    monkeypatch.setattr(daemon, "_take_sleep_inhibitor", lambda: daemon.inhibitor_events.append("take"))
    monkeypatch.setattr(daemon, "_release_sleep_inhibitor", lambda: daemon.inhibitor_events.append("release"))

    class E: pass
    e = E()
    e.clock, e.win, e.idle, e.db, e.daemon = clock, win, idle, db, daemon
    e.tick = lambda: daemon._tick()
    e.signal = lambda going: daemon._on_prepare_for_sleep(None, None, None, None, None, FakeParams(going))
    e.rows = lambda: [dict(r) for r in db.sessions_in_range("2000-01-01", "2100-01-01")]
    yield e
    db.close()


def total(e):
    return sum((r["end_time"] or r["last_heartbeat"]) - r["start_time"] for r in e.rows())


def test_sleep_time_is_not_counted_and_tracking_resumes_on_wake(env):
    e = env
    e.tick()                       # brave focused, session opens
    e.clock.advance(120); e.tick()
    e.signal(True)                 # logind: going to sleep
    e.clock.sleep(8 * 3600)        # 8 hours asleep
    e.signal(False)                # woke up
    e.clock.advance(60); e.tick()
    rows = e.rows()
    assert len(rows) == 2
    assert rows[0]["end_reason"] == "suspend"
    assert rows[0]["end_time"] - rows[0]["start_time"] == pytest.approx(120, abs=1)
    assert rows[1]["end_time"] is None                                   # tracking is live again
    assert rows[1]["start_time"] >= rows[0]["end_time"] + 8 * 3600 - 1   # new session begins after the sleep
    assert total(e) == pytest.approx(180, abs=3)                          # 120s before + ~60s after; not 8h


def test_no_session_can_open_between_sleep_signal_and_freeze(env):
    """Regression: a poll tick landing after PrepareForSleep(true) but before the
    kernel froze us used to open a fresh session that then spanned the sleep."""
    e = env
    e.tick(); e.clock.advance(30); e.tick()
    e.signal(True)
    for _ in range(5):             # ticks that still fire while the system is going down
        e.clock.advance(0.5); e.tick()
    assert [r["end_reason"] for r in e.rows()] == ["suspend"]            # still exactly one, closed
    assert e.db.get_open_session() is None


def test_late_delivery_of_sleep_and_wake_signals_still_correct(env):
    """If we were frozen before handling PrepareForSleep(true), both signals arrive
    after wake, back to back. Monotonic accounting keeps the sleep uncounted."""
    e = env
    e.tick(); e.clock.advance(90); e.tick()
    e.clock.sleep(6 * 3600)        # frozen; nothing handled yet
    e.signal(True); e.signal(False)
    e.clock.advance(30); e.tick()
    assert total(e) == pytest.approx(120, abs=3)
    assert e.rows()[0]["end_time"] - e.rows()[0]["start_time"] == pytest.approx(90, abs=1)


def test_missed_resume_signal_does_not_leave_tracking_paused_forever(env):
    e = env
    e.tick(); e.clock.advance(60); e.tick()
    e.signal(True)
    e.clock.sleep(4 * 3600)        # slept, and the wake signal never reaches us
    e.tick()                       # first tick after wake: wall/mono drift proves we slept
    assert e.daemon._suspended is False
    e.clock.advance(20); e.tick()
    assert e.db.get_open_session() is not None                            # tracking is back


def test_suspend_that_never_happened_resumes_after_guard_timeout(env):
    e = env
    e.tick(); e.clock.advance(10); e.tick()
    e.signal(True)                 # e.g. suspend was cancelled and no "false" ever came
    e.clock.advance(d.SUSPEND_GUARD_MAX_SECONDS + 1); e.tick()
    assert e.daemon._suspended is False
    e.clock.advance(5); e.tick()
    assert e.db.get_open_session() is not None


def test_normal_ticks_while_suspended_do_not_trigger_guard_early(env):
    e = env
    e.tick(); e.signal(True)
    e.clock.advance(5); e.tick()
    assert e.daemon._suspended is True


def test_resume_while_user_was_idle_does_not_get_stuck_idle(env):
    e = env
    e.tick(); e.clock.advance(400); e.idle.idle = 400; e.tick()          # idle threshold crossed
    e.signal(True); e.clock.sleep(3600); e.signal(False)
    assert e.daemon._was_idle is False
    e.idle.idle = 0                                                        # the wake-up keypress
    e.clock.advance(15); e.tick()
    assert e.db.get_open_session() is not None
    assert total(e) < 600                                                  # nothing near an hour


def test_wake_up_starts_on_the_window_focused_at_wake(env):
    e = env
    e.tick(); e.clock.advance(30); e.tick()
    e.signal(True); e.clock.sleep(600)
    e.win.focus = RawFocus(identifier="konsole", pid=None, title=None)    # lock screen dismissed into another app
    e.signal(False); e.clock.advance(20); e.tick()
    apps = {a.id: a.key for a in e.db.list_apps()}
    keys = [apps[r["app_id"]] for r in e.rows()]
    assert keys == ["brave-browser", "konsole"]


def test_inhibitor_released_after_closing_session_and_retaken_on_wake(env):
    e = env
    e.tick(); e.signal(True)
    assert e.daemon.inhibitor_events == ["release"]
    assert e.db.get_open_session() is None                                # closed before release ordering matters
    e.signal(False)
    assert e.daemon.inhibitor_events == ["release", "take"]


def test_sleep_and_wake_times_are_recorded_for_diagnostics(env):
    e = env
    e.tick(); e.signal(True); e.clock.sleep(100); e.signal(False)
    assert int(e.db.get_setting("last_suspend")) > 0
    assert int(e.db.get_setting("last_resume")) >= int(e.db.get_setting("last_suspend")) + 99


def test_repeated_sleep_cycles_never_double_count(env):
    e = env
    e.tick()
    for _ in range(5):
        e.clock.advance(100); e.tick()
        e.signal(True); e.clock.sleep(3 * 3600); e.signal(False)
    e.clock.advance(10); e.tick()
    assert total(e) == pytest.approx(5 * 100 + 10, abs=8)


def test_inhibitor_failure_is_not_fatal(tmp_path, monkeypatch):
    """No logind / no permission: taking the inhibitor must fail quietly."""
    monkeypatch.setattr(d, "create_window_detector", lambda: FakeWindow())
    monkeypatch.setattr(d, "create_idle_detector", lambda: FakeIdle())
    db = Database(tmp_path / "x.db")
    daemon = Daemon(db)

    class BrokenBus:
        def call_with_unix_fd_list_sync(self, *a, **k):
            raise RuntimeError("no logind")
    daemon._system_bus = BrokenBus()
    daemon._take_sleep_inhibitor()
    assert daemon._inhibitor_fd is None
    daemon._release_sleep_inhibitor()                                       # releasing nothing is fine
    db.close()


def test_real_fd_is_closed_on_release(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(d, "create_window_detector", lambda: FakeWindow())
    monkeypatch.setattr(d, "create_idle_detector", lambda: FakeIdle())
    db = Database(tmp_path / "y.db")
    daemon = Daemon(db)
    r, w = os.pipe()
    daemon._inhibitor_fd = r
    daemon._release_sleep_inhibitor()
    assert daemon._inhibitor_fd is None
    with pytest.raises(OSError):
        os.fstat(r)                                                         # really closed
    os.close(w)
    db.close()
