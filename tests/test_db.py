import tempfile
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime.db import Database, local_day


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def test_get_or_create_app_dedupes(db):
    a1 = db.get_or_create_app("firefox", "Firefox", "firefox", None)
    a2 = db.get_or_create_app("firefox", "Firefox", "firefox", None)
    assert a1.id == a2.id
    assert len(db.list_apps()) == 1


def test_session_open_close_updates_daily_totals(db):
    app = db.get_or_create_app("firefox", "Firefox", None, None)
    t0 = time.mktime((2026, 1, 1, 10, 0, 0, 0, 0, -1))
    sid = db.open_session(app.id, t0)
    db.close_session(sid, t0 + 300, "focus_change")

    day = local_day(t0)
    row = db.conn.execute(
        "SELECT seconds, session_count FROM daily_totals WHERE app_id=? AND day=?", (app.id, day)
    ).fetchone()
    assert row["seconds"] == 300
    assert row["session_count"] == 1


def test_multiple_sessions_accumulate(db):
    app = db.get_or_create_app("code", "VS Code", None, None)
    t0 = time.mktime((2026, 1, 1, 9, 0, 0, 0, 0, -1))
    s1 = db.open_session(app.id, t0)
    db.close_session(s1, t0 + 100, "focus_change")
    s2 = db.open_session(app.id, t0 + 500)
    db.close_session(s2, t0 + 700, "focus_change")

    day = local_day(t0)
    row = db.conn.execute(
        "SELECT seconds, session_count FROM daily_totals WHERE app_id=? AND day=?", (app.id, day)
    ).fetchone()
    assert row["seconds"] == 300
    assert row["session_count"] == 2


def test_heartbeat_then_close_does_not_doublecount(db):
    app = db.get_or_create_app("term", "Terminal", None, None)
    t0 = time.mktime((2026, 1, 1, 9, 0, 0, 0, 0, -1))
    sid = db.open_session(app.id, t0)
    db.heartbeat_session(sid, t0 + 60)
    db.heartbeat_session(sid, t0 + 120)
    db.close_session(sid, t0 + 150, "focus_change")

    day = local_day(t0)
    row = db.conn.execute(
        "SELECT seconds, session_count FROM daily_totals WHERE app_id=? AND day=?", (app.id, day)
    ).fetchone()
    assert row["seconds"] == 150
    assert row["session_count"] == 1


def test_crash_recovery_does_not_fabricate_time(tmp_path):
    db1 = Database(tmp_path / "crash.db")
    app = db1.get_or_create_app("game", "Game", None, None)
    t0 = time.mktime((2026, 1, 1, 9, 0, 0, 0, 0, -1))
    sid = db1.open_session(app.id, t0)
    db1.heartbeat_session(sid, t0 + 30)
    # Simulate crash: no close_session ever called, connection just dies.
    db1.close()

    db2 = Database(tmp_path / "crash.db")
    row = db2.conn.execute("SELECT end_time, end_reason FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["end_reason"] == "crash_recovered"
    # Recovery closes at last committed end_time (30s in from heartbeat), never later.
    assert row["end_time"] == int(t0 + 30)
    assert db2.get_open_session() is None
    db2.close()


def test_exclusion_flag_persists(db):
    app = db.get_or_create_app("steam", "Steam", None, None)
    db.set_excluded(app.id, True)
    reloaded = db.get_app(app.id)
    assert reloaded.excluded is True


def test_rebuild_daily_totals_matches_sessions(db):
    app = db.get_or_create_app("firefox", "Firefox", None, None)
    t0 = time.mktime((2026, 1, 1, 9, 0, 0, 0, 0, -1))
    s1 = db.open_session(app.id, t0)
    db.close_session(s1, t0 + 100, "focus_change")
    s2 = db.open_session(app.id, t0 + 200)
    db.close_session(s2, t0 + 260, "focus_change")

    before = dict(db.conn.execute(
        "SELECT day, seconds FROM daily_totals WHERE app_id=?", (app.id,)
    ).fetchall()[0])
    db.rebuild_daily_totals()
    after = dict(db.conn.execute(
        "SELECT day, seconds FROM daily_totals WHERE app_id=?", (app.id,)
    ).fetchall()[0])
    assert before == after
    assert after["seconds"] == 160


def test_gui_connection_does_not_close_live_daemon_session(tmp_path):
    """Defect: every Database() ran orphan recovery, so merely opening the GUI
    closed the *running* daemon's open session (recover_orphans=False is what
    the GUI now passes)."""
    path = tmp_path / "shared.db"
    daemon_db = Database(path)
    app = daemon_db.get_or_create_app("firefox", "Firefox", None, None)
    sid = daemon_db.open_session(app.id, 1_700_000_000)

    gui_db = Database(path, recover_orphans=False)
    assert gui_db.get_open_session() is not None
    assert daemon_db.get_open_session()["id"] == sid
    gui_db.close()
    daemon_db.close()


def test_daemon_connection_still_recovers_orphans_by_default(tmp_path):
    path = tmp_path / "crash.db"
    db1 = Database(path)
    app = db1.get_or_create_app("firefox", "Firefox", None, None)
    db1.open_session(app.id, 1_700_000_000)
    db1.close()
    db2 = Database(path)   # a restarted daemon
    assert db2.get_open_session() is None
    db2.close()
