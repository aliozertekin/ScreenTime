"""Keep every test away from the developer's real XDG dirs, autostart entries
and daemon lock -- autostart/lock code writes files, and a test run must never
touch a real ~/.config or a real running daemon's lock."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolated_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-runtime"))
    (tmp_path / "xdg-runtime").mkdir()


@pytest.fixture(autouse=True)
def _no_real_daemon_visible(request, monkeypatch):
    """The machine-wide process scan must not see a real ScreenTime daemon (the
    developer's own, or another test run's) -- that made results depend on what
    else was running. Tests that exercise the scan patch it themselves."""
    if request.node.get_closest_marker("real_process_scan"):
        return  # this test patches psutil itself to exercise the scan
    from screentime import autostart
    monkeypatch.setattr(autostart, "_find_daemon_processes", lambda: [])
