"""The real Daemon wired for Windows: sleep never counts, a missed suspend event
is caught by the clock-drift guard, and the Windows power source drives the same
suspend/resume path Linux uses. Clock model: wall keeps running while asleep, the
sleep-excluding (unbiased) clock does not -- exactly what QueryUnbiasedInterruptTime gives."""
import pytest

import screentime.daemon as d
from screentime import platform as plat
from screentime.daemon import Daemon
from screentime.db import Database
from screentime.idle_detector import IdleDetector
from screentime.platform.windows import power_events as pe
from screentime.platform.windows import win32 as w
from screentime.session_manager import SessionManager
from screentime.window_detector import RawFocus, WindowDetector


class Clock:
    def __init__(self):
        self.wall, self.mono = 1_800_000_000.0, 5_000.0
    def advance(self, s): self.wall += s; self.mono += s
    def sleep(self, s): self.wall += s                      # unbiased clock stands still


class Win(WindowDetector):
    name = "windows"
    def __init__(self): self.focus = RawFocus(identifier="firefox", pid=None, title=None, display_hint="Firefox")
    def is_supported(self): return True
    def get_focused(self): return self.focus


class Idle(IdleDetector):
    name = "windows"
    def __init__(self): self.idle = 0.0
    def get_idle_seconds(self): return self.idle


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENTIME_PLATFORM", "windows")
    plat.reset_for_tests()
    clock, win, idle = Clock(), Win(), Idle()
    monkeypatch.setattr(d, "create_window_detector", lambda: win)
    monkeypatch.setattr(d, "create_idle_detector", lambda: idle)
    db = Database(tmp_path / "w.db")
    daemon = Daemon(db)
    assert daemon._drift_guard is True                       # only Windows needs the guard
    daemon.session_manager = SessionManager(db, wall_clock=lambda: clock.wall, mono_clock=lambda: clock.mono)
    daemon._wall, daemon._mono = (lambda: clock.wall), (lambda: clock.mono)

    class E: pass
    e = E()
    e.clock, e.win, e.idle, e.db, e.daemon = clock, win, idle, db, daemon
    e.rows = lambda: [dict(r) for r in db.sessions_in_range("2000-01-01", "2100-01-01")]
    e.total = lambda: sum((r["end_time"] or r["last_heartbeat"]) - r["start_time"] for r in e.rows())
    yield e
    db.close()
    monkeypatch.delenv("SCREENTIME_PLATFORM")
    plat.reset_for_tests()


def test_friendly_name_hint_reaches_the_app_record(env):
    env.daemon._tick()
    assert env.db.get_setting  # smoke
    apps = {r["display_name"] for r in env.db.conn.execute("SELECT display_name FROM apps")}
    assert "Firefox" in apps


def test_power_events_close_the_session_and_sleep_is_not_counted(env):
    e = env
    src = pe.WindowsPowerEventSource(lambda fn, wait=False: fn(), clock=lambda: e.clock.mono)
    src._on_suspend, src._on_resume = e.daemon._suspend_tracking, e.daemon._resume_tracking
    e.daemon._tick(); e.clock.advance(120); e.daemon._tick()
    src.handle_message(0, w.WM_POWERBROADCAST, w.PBT_APMSUSPEND, 0)
    assert e.daemon._suspended and e.rows()[-1]["end_time"] is not None       # closed BEFORE sleeping
    e.clock.sleep(8 * 3600)
    src.handle_message(0, w.WM_POWERBROADCAST, w.PBT_APMRESUMEAUTOMATIC, 0)
    assert not e.daemon._suspended                                            # wake restarts tracking
    e.clock.advance(60); e.daemon._tick()
    assert 175 <= e.total() <= 200                                            # ~120 + ~60, never 8 hours
    assert len(e.rows()) == 2                                                 # a session can't straddle the sleep


def test_missed_suspend_event_is_caught_by_the_drift_guard(env):
    e = env
    e.daemon._tick(); e.clock.advance(120); e.daemon._tick()
    e.clock.sleep(6 * 3600)                                                   # slept; Windows told us nothing
    e.clock.advance(1); e.daemon._tick()
    assert e.total() < 300                                                    # no fake hours
    assert not e.daemon._suspended                                            # and tracking carries on by itself
    assert len(e.rows()) == 2


def test_cancelled_suspend_does_not_leave_tracking_paused(env):
    e = env
    e.daemon._tick(); e.clock.advance(30); e.daemon._tick()
    e.daemon._suspend_tracking()                                              # suspend announced...
    e.clock.advance(d.SUSPEND_GUARD_MAX_SECONDS + 5)                          # ...but the machine never slept
    e.daemon._tick()
    assert not e.daemon._suspended
    e.clock.advance(10); e.daemon._tick()
    assert e.rows()[-1]["end_time"] is None                                   # a session is open again


def test_idle_closes_at_last_input_and_idle_time_is_not_counted(env):
    e = env
    e.daemon._tick(); e.clock.advance(100); e.daemon._tick()
    e.idle.idle = e.daemon.config.idle_timeout_seconds + 30
    e.daemon._tick()
    closed = e.rows()[-1]
    assert closed["end_time"] is not None and (closed["end_time"] - closed["start_time"]) <= 105


def test_unknown_foreground_is_no_data_not_the_previous_app(env):
    e = env
    e.daemon._tick(); e.clock.advance(20); e.daemon._tick()
    e.win.focus = None                                                        # protected / unknown window
    e.clock.advance(5); e.daemon._tick()
    assert e.rows()[-1]["end_time"] is not None
    e.clock.advance(300); e.daemon._tick()
    assert e.total() < 40                                                     # the 300 s of "unknown" was not invented


def test_power_backend_is_recorded_for_diagnostics(env, monkeypatch):
    class Src:
        name = "windows"
        def __init__(self, *a, **k): pass
        def start(self, *a): return True
        def stop(self): pass
    monkeypatch.setattr(pe, "WindowsPowerEventSource", Src)
    env.daemon._setup_sleep_watcher()
    assert env.db.get_setting("active_power_backend") == "windows"
