"""The encrypted-log-backed Database: it must behave exactly like the plain one,
stay consistent across processes, and survive crashes."""
import os
import random
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from screentime import protected_store as PS
from screentime.db import Database
from screentime.protected_store import ProtectedStore, ReplayError, table_digests
from screentime.secure_log import SecureLog, TamperError

KEY = bytes(range(32))
ROOT = Path(__file__).resolve().parent.parent


def new_db(path, recover=True):
    created = not Path(path).exists()
    return Database(store=ProtectedStore(SecureLog(path, KEY, create=created)), recover_orphans=recover)


@pytest.fixture
def p(tmp_path):
    return tmp_path / "x.sec"


def fresh_digests(path):
    """Digests of a database rebuilt from scratch by replaying the log."""
    store = ProtectedStore(SecureLog(path, KEY))
    try:
        return store.table_digests()
    finally:
        store.close()


# -------------------------------------------------- the suite switch really engages
@pytest.mark.skipif(not os.environ.get("SCREENTIME_TEST_PROTECTED"), reason="only meaningful with SCREENTIME_TEST_PROTECTED=1")
def test_whole_suite_switch_actually_uses_the_protected_backend(tmp_path):
    db = Database(tmp_path / "anything.db")
    assert db.protected is True
    assert not (tmp_path / "anything.db").exists()          # no plaintext SQLite file was created


# ------------------------------------------------------------- basic behavior
def test_plain_and_protected_databases_behave_identically(tmp_path, p):
    """Same operations -> same rows, same totals."""
    def exercise(db):
        a = db.get_or_create_app("brave-browser", "Brave", None, None)
        b = db.get_or_create_app("konsole", "Konsole", "konsole", "x.desktop")
        t0 = 1_790_000_000
        for i in range(30):
            sid = db.open_session(a.id if i % 2 else b.id, t0 + i * 100)
            db.heartbeat_session(sid, t0 + i * 100 + 30)
            db.close_session(sid, t0 + i * 100 + 70, "focus_change")
        db.set_setting("theme", "gruvbox-dark")
        db.set_excluded(b.id, True)
        db.rename_app(a.id, "Brave Browser", "brave")
        return db

    plain = exercise(Database(tmp_path / "plain.db"))
    prot = exercise(new_db(p))
    assert table_digests(plain.conn) == prot._store.table_digests()


def test_protected_database_creates_no_plaintext_sqlite_file(tmp_path, p):
    db = new_db(p)
    db.set_setting("a", "b")
    db.close()
    assert sorted(f.name for f in tmp_path.iterdir() if f.suffix == ".db") == []


def test_reopen_sees_everything_that_was_written(p):
    db = new_db(p)
    app = db.get_or_create_app("kate", "Kate", None, None)
    sid = db.open_session(app.id, 1_790_000_000)
    db.close_session(sid, 1_790_000_300, "focus_change")
    db.set_setting("k", "v")
    db.close()
    again = new_db(p, recover=False)
    assert [a.key for a in again.list_apps()] == ["kate"] and again.get_setting("k") == "v"
    assert again.conn.execute("SELECT seconds FROM daily_totals").fetchone()[0] == 300


# ---------------------------------------------------------- replay determinism
def test_replay_reproduces_the_live_database_exactly(p):
    db = new_db(p)
    a = db.get_or_create_app("a", "A", None, None)
    t = 1_790_000_000
    for i in range(50):
        sid = db.open_session(a.id, t)
        db.heartbeat_session(sid, t + 5)
        db.heartbeat_session(sid, t + 9)
        db.close_session(sid, t + 20, "focus_change")
        t += 100
    db.set_setting("x", "1")
    assert fresh_digests(p) == db._store.table_digests()


def test_random_operation_sequences_replay_identically(p):
    """Property-style: any mix of operations, then a cold replay == the live state."""
    rnd = random.Random(7)
    db = new_db(p)
    apps = [db.get_or_create_app(f"app{i}", f"App {i}", None, None) for i in range(5)]
    t = 1_790_000_000.0
    open_sid = None
    for _ in range(300):
        op = rnd.choice(["open", "beat", "close", "setting", "exclude", "rename", "app"])
        if op == "open" and open_sid is None:
            open_sid = db.open_session(rnd.choice(apps).id, t)
        elif op == "beat" and open_sid:
            t += rnd.uniform(0.5, 5.5)
            db.heartbeat_session(open_sid, t)
        elif op == "close" and open_sid:
            t += rnd.uniform(0.5, 5.5)
            db.close_session(open_sid, t, "focus_change")
            open_sid = None
        elif op == "setting":
            db.set_setting(f"k{rnd.randint(0, 9)}", str(rnd.random()))
        elif op == "exclude":
            db.set_excluded(rnd.choice(apps).id, rnd.random() < 0.5)
        elif op == "rename":
            db.rename_app(rnd.choice(apps).id, f"N{rnd.randint(0, 99)}", None)
        elif op == "app":
            apps.append(db.get_or_create_app(f"new{rnd.randint(0, 5)}", "New", None, None))
        t += 1
    assert fresh_digests(p) == db._store.table_digests()


# ---------------------------------------------------------- statement recording
def make_conn():
    conn = PS.RecordingConnection()
    conn.mem = ProtectedStore._new_mem(None)
    conn.mem.executescript("CREATE TABLE t(a INTEGER, b TEXT, c BLOB, d REAL)")
    return conn


def test_reads_are_not_recorded_writes_are():
    c = make_conn()
    c.execute("INSERT INTO t VALUES (?, ?, ?, ?)", (1, "x", b"\x00\x01", 1.5))
    c.execute("SELECT * FROM t")
    c.execute("PRAGMA foreign_keys = ON")
    assert len(c.buffer) == 1 and c.buffer[0][1].startswith("INSERT")


def test_rolled_back_transaction_is_forgotten():
    c = make_conn()
    c.execute("INSERT INTO t(a) VALUES (1)")
    c.execute("BEGIN")
    c.execute("INSERT INTO t(a) VALUES (2)")
    c.execute("UPDATE t SET b = 'x'")
    c.execute("ROLLBACK")
    assert [s[1] for s in c.buffer] == ["INSERT INTO t(a) VALUES (1)"]
    assert c.mem.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_committed_transaction_is_kept_in_order():
    c = make_conn()
    c.execute("BEGIN")
    c.execute("INSERT INTO t(a) VALUES (1)")
    c.execute("INSERT INTO t(a) VALUES (2)")
    c.execute("COMMIT")
    assert len(c.buffer) == 2


def test_no_op_updates_are_not_recorded():
    c = make_conn()
    c.execute("UPDATE t SET a = 1 WHERE a = 999")          # matches nothing: changes nothing
    assert c.buffer == []


@pytest.mark.parametrize("value", [None, 0, -7, 2**40, 3.14159265358979, 1e300, "text 'quoted' \"dq\" \n newline ü 日本",
                                   b"", b"\x00\xff\x10", float("inf"), float("-inf")])
def test_statement_encoding_roundtrips_every_value_type_exactly(value):
    stmts = [("sql", "INSERT INTO t VALUES (?, ?, ?, ?)", [value, value, value, value])]
    back = PS.decode_statements(PS.encode_statements(stmts))
    assert back[0][2] == [value] * 4 and all(type(x) is type(value) for x in back[0][2])


def test_nan_survives_encoding():
    import math
    back = PS.decode_statements(PS.encode_statements([("sql", "x", [float("nan")])]))
    assert math.isnan(back[0][2][0])


def test_named_parameters_roundtrip():
    stmts = [("sql", "INSERT INTO t(a) VALUES (:a)", {"a": 5, "b": b"x"})]
    assert PS.decode_statements(PS.encode_statements(stmts))[0][2] == {"a": 5, "b": b"x"}


def test_garbage_records_raise_replay_error():
    with pytest.raises(ReplayError):
        PS.decode_statements(b"not zlib")


def test_authentic_but_unappliable_record_is_reported_not_swallowed(p):
    """A record that authenticates but cannot run (e.g. version skew) must fail loudly."""
    db = new_db(p)
    db.close()
    log = SecureLog(p, KEY)
    log.begin_write()
    log.append_event(PS.encode_statements([("sql", "INSERT INTO no_such_table VALUES (1)", [])]))
    log.commit()
    log.close()
    with pytest.raises(ReplayError):
        ProtectedStore(SecureLog(p, KEY))


# -------------------------------------------------------------------- atomicity
def test_a_failing_write_leaves_neither_memory_nor_log_changed(p):
    db = new_db(p)
    db.set_setting("keep", "me")
    before = db._store.table_digests()
    events_before = db._store.log.event_count()
    with pytest.raises(Exception):
        with db._store.unit():
            db._conn.execute("INSERT INTO settings(key, value) VALUES ('half', 'done')")
            raise RuntimeError("boom after one statement")
    assert db._store.table_digests() == before and db._store.log.event_count() == events_before
    assert db.get_setting("half") is None and fresh_digests(p) == before


def test_commit_failure_rebuilds_memory_from_the_log(p, monkeypatch):
    db = new_db(p)
    db.set_setting("a", "1")
    log = db._store.log
    real_append = log.append_event
    monkeypatch.setattr(log, "append_event", lambda payload: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        db.set_setting("b", "2")
    monkeypatch.setattr(log, "append_event", real_append)
    assert db.get_setting("b") is None and db.get_setting("a") == "1"
    db.set_setting("c", "3")                                  # and the store is still usable
    assert fresh_digests(p) == db._store.table_digests()


def test_temp_store_is_in_memory_so_no_plaintext_temp_files(p):
    db = new_db(p)
    assert db.conn.execute("PRAGMA temp_store").fetchone()[0] == 2         # 2 == MEMORY


# ----------------------------------------------------------------- compaction
def test_compaction_preserves_state_and_shrinks_the_log(p):
    db = new_db(p)
    a = db.get_or_create_app("a", "A", None, None)
    for i in range(100):
        sid = db.open_session(a.id, 1_790_000_000 + i * 10)
        db.close_session(sid, 1_790_000_000 + i * 10 + 5, "focus_change")
    before = db._store.table_digests()
    n = db._store.log.event_count()
    assert n > 100 and db._store.compact(force=True) is True
    assert db._store.log.event_count() == 0 and db._store.table_digests() == before
    db.set_setting("after", "compaction")
    assert fresh_digests(p) == db._store.table_digests()


def test_compaction_is_a_noop_below_the_threshold(p):
    db = new_db(p)
    db.set_setting("a", "1")
    assert db.maintenance() is False


def test_maintenance_compacts_past_the_threshold(p):
    db = new_db(p)
    db._store.compact_threshold = 5
    for i in range(10):
        db.set_setting("k", str(i))
    assert db.maintenance() is True and db._store.log.event_count() == 0


def test_plain_database_maintenance_is_a_noop(tmp_path):
    assert Database(tmp_path / "plain.db").maintenance() is False


def test_a_lagging_reader_reloads_after_compaction(p):
    daemon, gui = new_db(p), new_db(p, recover=False)
    daemon.set_setting("one", "1")
    assert gui.get_setting("one") == "1"
    for i in range(5):
        daemon.set_setting("n", str(i))                       # gui falls behind
    daemon._store.compact(force=True)                         # events it hasn't read are now gone
    daemon.set_setting("after", "x")
    assert gui.get_setting("n") == "4" and gui.get_setting("after") == "x"
    assert gui._store.table_digests() == daemon._store.table_digests()


def test_a_writer_behind_a_compaction_still_writes_correctly(p):
    daemon, gui = new_db(p), new_db(p, recover=False)
    daemon.set_setting("a", "1")
    daemon._store.compact(force=True)
    gui.set_setting("from_gui", "yes")                        # gui never saw the compaction
    assert daemon.get_setting("from_gui") == "yes"
    assert fresh_digests(p) == daemon._store.table_digests() == gui._store.table_digests()


def test_tampering_after_compaction_is_still_detected(p):
    db = new_db(p)
    db.set_setting("a", "1")
    db._store.compact(force=True)
    db.close()
    import sqlite3
    con = sqlite3.connect(p)
    con.execute("UPDATE snapshot_chunks SET ct = CAST((CASE WHEN substr(ct, 1, 1) = X'00' THEN X'01' ELSE X'00' END) || substr(ct, 2) AS BLOB)")
    con.commit()
    con.close()
    with pytest.raises(TamperError):
        ProtectedStore(SecureLog(p, KEY))


# ------------------------------------------------------- multi-process behavior
WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {root!r})
    from screentime.db import Database
    from screentime.protected_store import ProtectedStore
    from screentime.secure_log import SecureLog
    path, name, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    db = Database(store=ProtectedStore(SecureLog(path, bytes(range(32)))), recover_orphans=False)
    for i in range(n):
        db.set_setting(f"{{name}}-{{i}}", "v")
    db.close()
""").format(root=str(ROOT))


def test_concurrent_writer_processes_never_lose_or_interleave_updates(p):
    new_db(p).close()
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(p), f"w{i}", "60"]) for i in range(4)]
    assert [pr.wait(timeout=120) for pr in procs] == [0, 0, 0, 0]
    db = new_db(p, recover=False)
    keys = {r[0] for r in db.conn.execute("SELECT key FROM settings")}
    assert {f"w{i}-{j}" for i in range(4) for j in range(60)} <= keys                 # all 240 writes survived
    assert fresh_digests(p) == db._store.table_digests()
    assert db._store.log.verify_all()["events"] >= 240


def test_a_second_process_sees_live_updates_through_data_version(p):
    gui = new_db(p, recover=False)
    assert gui.get_setting("late") is None
    subprocess.run([sys.executable, "-c", WRITER, str(p), "late", "1"], check=True, timeout=60)
    assert gui.get_setting("late-0") == "v"                                           # picked up without reopening


def test_killing_a_writer_mid_transaction_leaves_a_consistent_store(p):
    new_db(p).close()
    crasher = textwrap.dedent("""
        import os, sys
        sys.path.insert(0, {root!r})
        from screentime.db import Database
        from screentime.protected_store import ProtectedStore
        from screentime.secure_log import SecureLog
        db = Database(store=ProtectedStore(SecureLog(sys.argv[1], bytes(range(32)))), recover_orphans=False)
        db.set_setting("committed", "yes")
        db._store.log.begin_write()                       # a write is in flight...
        db._store.log.append_event(b"half-written garbage that was never committed")
        os._exit(9)                                       # ...and the process dies (like SIGKILL)
    """).format(root=str(ROOT))
    r = subprocess.run([sys.executable, "-c", crasher, str(p)], timeout=60)
    assert r.returncode == 9
    db = new_db(p, recover=False)                         # must open cleanly, lock released by the kernel
    assert db.get_setting("committed") == "yes"
    db.set_setting("after-crash", "ok")                   # and be writable again
    assert fresh_digests(p) == db._store.table_digests()


def test_two_databases_in_one_process_interleave_correctly(p):
    a, b = new_db(p), new_db(p, recover=False)
    for i in range(40):
        (a if i % 2 else b).set_setting(f"k{i}", str(i))
    assert a._store.table_digests() == b._store.table_digests() == fresh_digests(p)
