"""
Unit tests for Daemon recording which detector backends it selected into
the settings table -- the fix that lets the GUI display this information
without ever instantiating its own (potentially side-effecting) detector.
See test_settings_view_no_detector_leak.py for the GUI-side half of this.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime.db import Database
from screentime.daemon import Daemon


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "daemon.db")
    yield d
    d.close()


def test_daemon_records_selected_backends_on_init(db):
    daemon = Daemon(db)
    assert db.get_setting("active_window_backend") == daemon.window_detector.name
    assert db.get_setting("active_idle_backend") == daemon.idle_detector.name
    # In this sandbox there's no display/session bus, so these should be
    # the honest "nothing available" backends -- confirming the write path
    # itself works regardless of which backend actually got selected.
    assert daemon.window_detector.name  # non-empty string either way
    assert daemon.idle_detector.name


def test_daemon_clears_backend_settings_on_clean_shutdown(db):
    daemon = Daemon(db)
    assert db.get_setting("active_window_backend") != ""
    daemon._shutdown("test")
    assert db.get_setting("active_window_backend") == ""
    assert db.get_setting("active_idle_backend") == ""


def test_daemon_restart_updates_backend_settings_again(tmp_path):
    """A second Daemon instance (simulating a restart) must overwrite
    whatever the previous instance left behind, not get stuck on stale
    values from before."""
    dbfile = tmp_path / "restart.db"
    db1 = Database(dbfile)
    daemon1 = Daemon(db1)
    daemon1._shutdown("test")
    db1.close()

    db2 = Database(dbfile)
    daemon2 = Daemon(db2)
    assert db2.get_setting("active_window_backend") == daemon2.window_detector.name
    db2.close()


# ---------------------------------------------------------------- startup robustness
def test_daemon_records_last_start(db):
    import time
    before = int(time.time())
    Daemon(db)
    assert int(db.get_setting("daemon_last_start")) >= before


def test_daemon_redetects_backend_that_was_not_ready_at_login(db, monkeypatch):
    """Defect: at login the session env/compositor helper may not be ready when
    the daemon starts. It used to stay on the null backend forever (systemd only
    restarts on crash), tracking nothing while looking healthy."""
    import screentime.daemon as d
    from screentime.window_detector import NullDetector, WindowDetector

    class Ready(WindowDetector):
        name = "kwin-push"
        def is_supported(self): return True
        def get_focused(self): return None

    monkeypatch.setattr(d, "create_window_detector", lambda: NullDetector())
    daemon = Daemon(db)
    assert db.get_setting("active_window_backend") == "unsupported"

    monkeypatch.setattr(d, "create_window_detector", lambda: Ready())
    daemon._last_redetect -= d.REDETECT_INTERVAL_SECONDS + 1   # interval elapsed
    daemon._tick()
    assert daemon.window_detector.name == "kwin-push"
    assert db.get_setting("active_window_backend") == "kwin-push"


def test_redetect_is_rate_limited(db, monkeypatch):
    import screentime.daemon as d
    from screentime.window_detector import NullDetector
    calls = []
    monkeypatch.setattr(d, "create_window_detector", lambda: (calls.append(1), NullDetector())[1])
    daemon = Daemon(db)
    calls.clear()
    for _ in range(10):
        daemon._tick()
    assert calls == []          # interval hasn't elapsed


def test_redetect_never_replaces_a_working_detector(db, monkeypatch):
    """A live KWinPushDetector owns an exclusive D-Bus name; rebuilding it would
    drop KWin's events. Only *null* backends are ever retried."""
    import screentime.daemon as d
    from screentime.window_detector import WindowDetector

    class Live(WindowDetector):
        name = "kwin-push"
        def is_supported(self): return True
        def get_focused(self): return None

    monkeypatch.setattr(d, "create_window_detector", lambda: Live())
    daemon = Daemon(db)
    live = daemon.window_detector
    monkeypatch.setattr(d, "create_window_detector", lambda: pytest.fail("must not re-create"))
    daemon._last_redetect -= 1000
    daemon._tick()
    assert daemon.window_detector is live


def test_main_exits_cleanly_when_another_daemon_holds_lock(tmp_path, monkeypatch):
    import screentime.daemon as d
    from screentime.instance_lock import InstanceLock
    monkeypatch.setattr(sys, "argv", ["screentime-daemon"])
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    holder = InstanceLock()
    assert holder.acquire()
    monkeypatch.setattr(d, "Daemon", lambda db: pytest.fail("second daemon must not start"))
    assert d.main() == 0
    holder.release()


def test_daemon_asks_kwin_to_reannounce_focus_only_for_kwin_push(db, monkeypatch):
    """At login KWin loads the script before the daemon exists, so its one-time
    'current window' push is lost. The daemon re-triggers it once it owns its
    D-Bus name -- and only when the backend is actually kwin-push."""
    import screentime.daemon as d
    from screentime.window_detector import WindowDetector

    class Kwin(WindowDetector):
        name = "kwin-push"
        def is_supported(self): return True
        def get_focused(self): return None

    class Sway(Kwin):
        name = "sway"

    scheduled = []
    monkeypatch.setattr(d.GLib, "timeout_add_seconds", lambda s, cb: scheduled.append((s, cb)))
    monkeypatch.setattr(d.GLib, "timeout_add", lambda *a: None)
    monkeypatch.setattr(d.GLib, "unix_signal_add", lambda *a: None)

    for detector, expect in ((Kwin(), True), (Sway(), False)):
        scheduled.clear()
        monkeypatch.setattr(d, "create_window_detector", lambda det=detector: det)
        daemon = Daemon(db)
        monkeypatch.setattr(daemon._loop, "run", lambda: None)
        monkeypatch.setattr(daemon, "_setup_sleep_watcher", lambda: None)
        daemon.run()
        assert bool(scheduled) is expect
        if expect:
            assert scheduled[0][0] >= 1          # delayed until the name is acquired
            assert scheduled[0][1].__name__ == "_kick_kwin_script"


def test_early_signal_handler_only_sets_a_flag_and_never_raises(tmp_path, monkeypatch):
    """Regression: the handler used to raise SystemExit. Raised at an arbitrary point
    inside PyGObject's C-backed code (building a GLib.Variant for the keyring probe)
    that exception was silently discarded (inside a destructor) or crashed the
    interpreter outright (exit status -11). The handler must only set a flag."""
    import signal
    import screentime.daemon as d
    from screentime import storage
    from screentime.db import Database

    monkeypatch.setattr(sys, "argv", ["screentime-daemon"])
    seen = {}

    def open_and_deliver_sigterm(**kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        seen["raised"] = False
        try:
            handler(signal.SIGTERM, None)
        except BaseException:                                # must not happen
            seen["raised"] = True
        seen["should_stop"] = kwargs["should_stop"]
        return Database(tmp_path / "x.db")

    monkeypatch.setattr(storage, "open_database", open_and_deliver_sigterm)
    monkeypatch.setattr(d, "Daemon", lambda db: pytest.fail("daemon must not start after a stop request"))
    assert d.main() == 0
    assert seen["raised"] is False and seen["should_stop"]() is True


def test_stop_request_during_daemon_construction_skips_the_loop(tmp_path, monkeypatch):
    import signal
    import screentime.daemon as d
    from screentime.db import Database

    monkeypatch.setattr(sys, "argv", ["screentime-daemon"])
    monkeypatch.setattr("screentime.storage.open_database", lambda **kw: Database(tmp_path / "y.db"))

    class Constructed:
        def run(self, should_stop=None):
            pytest.fail("run() must not be entered after a stop request")

    def construct(db):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return Constructed()
    monkeypatch.setattr(d, "Daemon", construct)
    assert d.main() == 0


def test_run_stops_immediately_if_a_stop_was_requested_before_the_loop_existed(tmp_path, monkeypatch):
    import screentime.daemon as d
    from screentime.db import Database
    db = Database(tmp_path / "z.db")
    daemon = d.Daemon(db)
    scheduled = []
    monkeypatch.setattr(d.GLib, "idle_add", lambda cb: scheduled.append(cb))
    monkeypatch.setattr(d.GLib, "timeout_add", lambda *a: None)
    monkeypatch.setattr(d.GLib, "timeout_add_seconds", lambda *a: None)
    monkeypatch.setattr(d.GLib, "unix_signal_add", lambda *a: None)
    monkeypatch.setattr(daemon._loop, "run", lambda: None)
    monkeypatch.setattr(daemon, "_setup_sleep_watcher", lambda: None)
    shutdowns = []
    monkeypatch.setattr(daemon, "_shutdown", lambda reason: shutdowns.append(reason))
    daemon.run(should_stop=lambda: True)
    assert scheduled and (scheduled[0](), shutdowns == ["shutdown"])[1]
