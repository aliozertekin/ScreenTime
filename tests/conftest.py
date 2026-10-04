"""Keep every test away from the developer's real XDG dirs, autostart entries
and daemon lock -- autostart/lock code writes files, and a test run must never
touch a real ~/.config or a real running daemon's lock."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared test helpers (windows_fakes)


@pytest.fixture(autouse=True)
def _isolated_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-runtime"))
    # Same guarantee on Windows: never touch a real %LOCALAPPDATA% / %APPDATA%.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "win-local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "win-roaming"))
    (tmp_path / "xdg-runtime").mkdir()
    # Tests must never reach a real keyring (a developer's KWallet / GNOME Keyring):
    # point the session bus at nothing. That is exactly "no Secret Service here", so
    # the key falls back to a key file inside the isolated XDG_CONFIG_HOME. Child
    # processes (the real-daemon tests) inherit this.
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/screentime-test-bus")


@pytest.fixture(autouse=True)
def _no_real_daemon_visible(request, monkeypatch):
    """The machine-wide process scan must not see a real ScreenTime daemon (the
    developer's own, or another test run's) -- that made results depend on what
    else was running. Tests that exercise the scan patch it themselves."""
    if request.node.get_closest_marker("real_process_scan"):
        return  # this test patches psutil itself to exercise the scan
    from screentime import autostart
    monkeypatch.setattr(autostart, "_find_daemon_processes", lambda: [])


@pytest.fixture(autouse=True)
def _reset_theme_manager():
    """The theme manager is process-wide state (it owns a CSS provider on the
    display); never let one test's leak into the next."""
    yield
    import sys
    tm = sys.modules.get("screentime.gui.theme_manager")
    if tm is not None:
        tm.set_theme_manager(None)


@pytest.fixture(autouse=True)
def _protected_backend_for_whole_suite(monkeypatch, tmp_path):
    """Opt-in: `SCREENTIME_TEST_PROTECTED=1 pytest tests/` re-runs the ENTIRE
    suite (session timing, crash recovery, midnight splitting, stats, daemon...)
    with every `Database(path)` backed by the encrypted store instead of a plain
    SQLite file. It is how we know encryption did not change any behavior."""
    if not os.environ.get("SCREENTIME_TEST_PROTECTED"):
        yield
        return
    from screentime import db as db_mod
    from screentime.protected_store import ProtectedStore
    from screentime.secure_log import SecureLog
    key = bytes(range(32))
    original = db_mod.Database.__init__

    def protected_init(self, path=None, recover_orphans=True, store=None):
        if store is None and path is not None:
            sec = Path(str(path) + ".sec")
            log_ = SecureLog(sec, key, create=not sec.exists())
            store = ProtectedStore(log_)
        original(self, path, recover_orphans, store)

    monkeypatch.setattr(db_mod.Database, "__init__", protected_init)
    yield
