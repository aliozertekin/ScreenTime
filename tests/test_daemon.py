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
