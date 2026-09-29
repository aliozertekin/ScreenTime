"""Real-process checks of the startup guarantees (no mocks): a second daemon
exits cleanly, SIGTERM shuts down cleanly, SIGKILL never leaves a lock that
blocks the next start, and a crashed daemon's open session is recovered by the
restarted one."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from screentime.db import Database
from screentime.instance_lock import is_held

ROOT = Path(__file__).resolve().parent.parent


_SPAWNED: list = []


def _spawn(env):
    proc = subprocess.Popen([sys.executable, "-m", "screentime.daemon"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _SPAWNED.append(proc)
    return proc


@pytest.fixture(autouse=True)
def _reap_spawned_daemons():
    """A failing assertion must never leave a daemon running behind the test
    run (orphans pile up, hold CPU and make later runs flaky)."""
    yield
    while _SPAWNED:
        proc = _SPAWNED.pop()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def denv(tmp_path):
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    env["PYTHONPATH"] = str(ROOT)
    return env  # XDG_* already isolated per-test by conftest (inherited via os.environ)


def _db_path():
    return Path(os.environ["XDG_DATA_HOME"]) / "screentime" / "screentime.db"


def test_second_daemon_exits_zero_and_first_keeps_running(denv):
    a = _spawn(denv)
    try:
        assert _wait(is_held), "first daemon never took the lock"
        b = _spawn(denv)
        assert b.wait(timeout=15) == 0
        assert a.poll() is None            # first is unaffected
    finally:
        a.terminate()
        a.wait(timeout=10)


def test_sigterm_is_a_clean_exit_and_releases_lock(denv):
    a = _spawn(denv)
    assert _wait(is_held)
    a.send_signal(signal.SIGTERM)
    assert a.wait(timeout=10) == 0
    assert is_held() is False


def test_sigkill_then_restart_recovers(denv):
    a = _spawn(denv)
    assert _wait(is_held)
    assert _wait(lambda: _db_path().exists())
    a.send_signal(signal.SIGKILL)
    a.wait(timeout=10)
    assert is_held() is False              # kernel dropped the lock; nothing stale
    b = _spawn(denv)
    try:
        assert _wait(is_held), "restart after a crash was blocked"
        assert b.poll() is None
    finally:
        b.terminate()
        b.wait(timeout=10)


def test_gui_style_connection_while_daemon_runs_keeps_session_open(denv):
    a = _spawn(denv)
    try:
        assert _wait(is_held)
        assert _wait(lambda: _db_path().exists())
        # Give the daemon time to finish its own init before we add a session.
        time.sleep(1.0)
        gui = Database(_db_path(), recover_orphans=False)
        app = gui.get_or_create_app("x", "X", None, None)
        sid = gui.open_session(app.id, time.time())
        gui.close()
        gui2 = Database(_db_path(), recover_orphans=False)   # "opening the GUI" again
        row = gui2.get_open_session()
        assert row is not None and row["id"] == sid
        gui2.close()
    finally:
        a.terminate()
        a.wait(timeout=10)


def test_real_daemon_renames_stored_steam_app_from_local_library(denv, tmp_path):
    """The reported bug end to end, in a real daemon process: the database holds
    'Steam App 2620' from before, Steam's manifest exists locally, no network."""
    home = tmp_path / "home"
    lib = home / ".local/share/Steam/steamapps"
    lib.mkdir(parents=True)
    (lib / "appmanifest_2620.acf").write_text(
        '"AppState"\n{\n\t"appid"\t"2620"\n\t"name"\t"Call of Duty 2"\n}\n')
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    db = Database(_db_path())
    app = db.get_or_create_app("steam_app_2620", "Steam App 2620", "steam_app_2620", None)
    db.close()
    denv = dict(denv, HOME=str(home))
    a = _spawn(denv)
    try:
        assert _wait(is_held)
        def renamed():
            d = Database(_db_path(), recover_orphans=False)
            try:
                return d.get_app(app.id).display_name == "Call of Duty 2"
            finally:
                d.close()
        assert _wait(renamed), "daemon did not resolve the stored Steam name"
    finally:
        a.terminate()
        a.wait(timeout=10)


def test_sigterm_during_slow_startup_still_exits_cleanly(denv, tmp_path):
    """SIGTERM as soon as the lock is held (i.e. possibly mid-startup, before
    run() installs its loop handler) must be a clean exit, not death by signal."""
    for _ in range(5):
        a = _spawn(denv)
        assert _wait(is_held)
        a.send_signal(signal.SIGTERM)
        assert a.wait(timeout=10) == 0
        assert is_held() is False

