"""Encrypted backup: create -> verify -> restore -> verify recovered history, plus every way it can go wrong."""
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from screentime import backup as B
from screentime import storage
from screentime.keystore import KeyFileBackend, KeyManager, encode_recovery_key
from screentime.secure_log import SecureLog

T0 = 1_790_000_000
DAY = "2026-09-21"


def km_for(cfg):
    return KeyManager(cfg, backends=[lambda interactive: KeyFileBackend(cfg)])


def open_machine(root: Path, name: str):
    """A separate 'computer': its own data dir, its own key store, its own encrypted database."""
    dd, cfg = root / name / "data", root / name / "cfg"
    dd.mkdir(parents=True)
    km = km_for(cfg)
    db = storage.open_database(recover_orphans=False, data_dir_=dd, key_manager=km)
    return db, km


def record(db, key, name, start, length, reason="focus_change"):
    app = db.get_or_create_app(key, name, "icon", None)
    sid = db.open_session(app.id, start)
    db.heartbeat_session(sid, start + length)
    db.close_session(sid, start + length, reason)
    return app


def history(db):
    rows = db.conn.execute("SELECT a.key, s.start_time, s.end_time, s.day, s.end_reason FROM sessions s "
                           "JOIN apps a ON a.id = s.app_id ORDER BY a.key, s.start_time").fetchall()
    totals = db.conn.execute("SELECT a.key, d.day, d.seconds, d.session_count FROM daily_totals d "
                             "JOIN apps a ON a.id = d.app_id ORDER BY a.key, d.day").fetchall()
    return [tuple(r) for r in rows], [tuple(r) for r in totals]


@pytest.fixture
def machine_a(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "data_dir", lambda: tmp_path / "scratch")
    (tmp_path / "scratch").mkdir()
    db, km = open_machine(tmp_path, "a")
    for i in range(30):
        record(db, "firefox", "Firefox", T0 + i * 1000, 600)
    record(db, "code", "VS Code", T0 + 50_000, 1200, "idle")
    yield db, km, tmp_path
    db.close()


def test_filename_and_extension():
    assert B.default_filename().startswith("ScreenTime-backup-") and B.default_filename().endswith(".screentime")


def test_create_verify_restore_recovers_the_history_on_another_computer(machine_a):
    db_a, km_a, root = machine_a
    out = root / B.default_filename()
    info = B.create_backup(db_a, out, km=km_a)
    from screentime.db import local_day
    assert (info.apps, info.sessions, info.first_day, info.last_day) == (2, 31, local_day(T0), local_day(T0 + 50_000))
    assert info.header.format == "screentime-backup" and info.header.version == 1
    assert out.stat().st_size > 0 and not Path(str(out) + "-wal").exists()

    # verify on its own, using the recovery key of the machine that made it
    rk = encode_recovery_key(km_a.get_key(info.header.store_id))
    assert B.verify_backup(out, km=km_file_empty(root), recovery_key=rk).sessions == 31

    # a brand-new computer with no key for it
    db_b, km_b = open_machine(root, "b")
    try:
        with pytest.raises(B.NeedKeyError):
            B.restore_backup(db_b, out, km=km_b)
        assert history(db_b) == ([], [])                              # nothing changed by the failed attempt
        res = B.restore_backup(db_b, out, km=km_b, recovery_key=rk)
        assert (res.apps_added, res.sessions_added, res.sessions_skipped) == (2, 31, 0)
        assert history(db_b) == history(db_a)                         # sessions AND daily totals identical
        assert db_b.get_app_by_key("firefox").display_name == "Firefox"
    finally:
        db_b.close()


def km_file_empty(root):
    return km_for(root / "nobody")


def test_restoring_is_idempotent_and_never_duplicates(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    B.create_backup(db, out, km=km)
    before = history(db)
    res = B.restore_backup(db, out, km=km)                           # onto the machine it came from
    assert (res.apps_added, res.sessions_added, res.sessions_skipped) == (0, 0, 31)
    assert history(db) == before


def test_restore_adds_missing_history_and_keeps_what_is_already_there(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    B.create_backup(db, out, km=km)
    later = record(db, "firefox", "Firefox (renamed)", T0 + 90_000, 300)     # new work after the backup
    db.rename_app(later.id, "My Firefox")
    # simulate "lost history": a second machine that only has a newer session
    db_b, km_b = open_machine(root, "b")
    try:
        record(db_b, "firefox", "Firefox", T0 + 70_000, 100)
        rk = encode_recovery_key(km.get_key(B.read_header(out).store_id))
        res = B.restore_backup(db_b, out, km=km_b, recovery_key=rk)
        assert res.sessions_added == 31
        count = db_b.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == 32 and db_b.get_app_by_key("firefox").display_name == "Firefox"
    finally:
        db_b.close()


def test_backup_does_not_contain_the_key_or_readable_usage_data(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    info = B.create_backup(db, out, km=km)
    key = km.get_key(info.header.store_id)
    blob = out.read_bytes()
    assert key not in blob and encode_recovery_key(key).replace("-", "").encode() not in blob
    for needle in (b"Firefox", b"firefox", b"VS Code", b"sessions", b"daily_totals"):
        assert needle not in blob
    assert b"screentime-backup" in blob                                # only the format header is readable


def test_the_backup_file_is_never_modified_by_verifying_or_restoring(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    B.create_backup(db, out, km=km)
    digest = out.read_bytes()
    out.chmod(0o444)
    try:
        B.verify_backup(out, km=km)
        B.restore_backup(db, out, km=km)
    finally:
        out.chmod(0o644)
    assert out.read_bytes() == digest
    assert not [p for p in root.iterdir() if p.name.startswith("b.screentime-")]      # no -wal/-shm left behind
    assert not list((root / "scratch").iterdir())                                   # private copies cleaned up


def test_refuses_to_overwrite_an_existing_backup_and_leaves_no_partial_file(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    out.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        B.create_backup(db, out, km=km)
    assert out.read_bytes() == b"precious"
    B.create_backup(db, out, km=km, overwrite=True)
    assert B.verify_backup(out, km=km).sessions == 31
    assert not [p for p in root.iterdir() if p.name.endswith(".part")]


def test_not_a_backup_and_garbage_files_are_rejected_cleanly(machine_a, tmp_path):
    db, km, root = machine_a
    junk = root / "x.screentime"
    junk.write_bytes(b"this is not a database at all" * 50)
    empty = root / "empty.screentime"
    empty.write_bytes(b"")
    plain = root / "plain.screentime"
    con = sqlite3.connect(plain); con.execute("CREATE TABLE t(x)"); con.commit(); con.close()
    live_copy = root / "live.screentime"
    shutil.copy(storage.store_path(Path(db.path).parent), live_copy)      # a live store is not a backup
    for f in (junk, empty, plain, live_copy, root / "missing.screentime"):
        with pytest.raises(B.IncompatibleBackupError):
            B.restore_backup(db, f, km=km)
    assert history(db)[0]                                                  # untouched


def test_newer_backup_version_is_refused_with_an_upgrade_hint(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    info = B.create_backup(db, out, km=km)
    con = sqlite3.connect(out); con.execute("UPDATE meta SET v = ? WHERE k = 'backup_version'", (b"2",)); con.commit(); con.close()
    with pytest.raises(B.IncompatibleBackupError, match="newer ScreenTime"):
        B.verify_backup(out, km=km)


def test_corrupted_backup_is_detected_before_anything_changes(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    B.create_backup(db, out, km=km)
    before = history(db)
    con = sqlite3.connect(out)
    con.execute("UPDATE snapshot_chunks SET ct = substr(ct, 1, length(ct) - 1) || x'00' WHERE idx = 0")
    con.commit(); con.close()
    with pytest.raises(B.CorruptBackupError):
        B.restore_backup(db, out, km=km)
    assert history(db) == before


def test_truncated_backup_is_reported_not_crashed(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    B.create_backup(db, out, km=km)
    out.write_bytes(out.read_bytes()[:3000])
    with pytest.raises(B.BackupError):
        B.restore_backup(db, out, km=km)


def test_wrong_recovery_key_and_typo_are_reported(machine_a):
    db, km, root = machine_a
    out = root / "b.screentime"
    B.create_backup(db, out, km=km)
    other = encode_recovery_key(bytes(range(32)))
    db_b, km_b = open_machine(root, "b")
    try:
        with pytest.raises(B.WrongBackupKeyError):
            B.restore_backup(db_b, out, km=km_b, recovery_key=other)
        with pytest.raises(B.WrongBackupKeyError):
            B.restore_backup(db_b, out, km=km_b, recovery_key="AAAA-BBBB")
        assert history(db_b) == ([], [])
    finally:
        db_b.close()


def test_backup_of_an_empty_history_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "data_dir", lambda: tmp_path / "scratch")
    (tmp_path / "scratch").mkdir()
    db, km = open_machine(tmp_path, "a")
    try:
        out = tmp_path / "e.screentime"
        info = B.create_backup(db, out, km=km)
        assert (info.apps, info.sessions, info.first_day) == (0, 0, None)
        assert B.restore_backup(db, out, km=km) == B.RestoreResult(0, 0, 0)
    finally:
        db.close()


def test_open_session_in_backup_is_restored_as_a_closed_session(machine_a):
    db, km, root = machine_a
    app = db.get_or_create_app("live", "Live", None, None)
    sid = db.open_session(app.id, T0 + 200_000)
    db.heartbeat_session(sid, T0 + 200_300)                  # daemon still running: end_time is NULL
    out = root / "b.screentime"
    B.create_backup(db, out, km=km)
    db_b, km_b = open_machine(root, "b")
    try:
        rk = encode_recovery_key(km.get_key(B.read_header(out).store_id))
        B.restore_backup(db_b, out, km=km_b, recovery_key=rk)
        row = db_b.conn.execute("SELECT start_time, end_time FROM sessions WHERE start_time = ?", (T0 + 200_000,)).fetchone()
        assert (row["start_time"], row["end_time"]) == (T0 + 200_000, T0 + 200_300)
        assert db_b.get_open_session() is None
    finally:
        db_b.close()


def test_a_plain_database_cannot_be_backed_up_with_the_wrong_key_model(tmp_path):
    from screentime.db import Database
    d = Database(tmp_path / "p.db")
    if d._store is None:
        with pytest.raises(B.BackupError):
            B.create_backup(d, tmp_path / "x.screentime")
    d.close()


def test_restore_runs_in_bounded_batches_for_large_histories(machine_a, monkeypatch):
    db, km, root = machine_a
    app = db.get_or_create_app("bulk", "Bulk", None, None)
    for i in range(300):
        db._conn.execute("INSERT INTO sessions(app_id,start_time,end_time,last_heartbeat,day,end_reason) VALUES (?,?,?,?,?,?)",
                         (app.id, T0 + 300_000 + i * 10, T0 + 300_000 + i * 10 + 5, T0 + 300_000 + i * 10 + 5, "2026-09-25", "x"))
    out = root / "b.screentime"
    B.create_backup(db, out, km=km)
    monkeypatch.setattr(B, "_BATCH", 100)
    db_b, km_b = open_machine(root, "b")
    try:
        rk = encode_recovery_key(km.get_key(B.read_header(out).store_id))
        res = B.restore_backup(db_b, out, km=km_b, recovery_key=rk)
        assert res.sessions_added == 331
    finally:
        db_b.close()


# ------------------------------------------------------------------ command line
def test_cli_backup_verify_restore_round_trip(tmp_path, monkeypatch, capsys):
    from screentime import security_cli as cli
    monkeypatch.setattr(storage, "data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    km = km_for(tmp_path / "cfg")
    monkeypatch.setattr(cli, "KeyManager", lambda: km)
    db = storage.open_database(recover_orphans=False, key_manager=km)
    record(db, "firefox", "Firefox", T0, 600)
    db.close()
    out = tmp_path / "cli.screentime"
    assert cli.main(["backup", str(out)]) == 0
    assert cli.main(["backup", str(out)]) == 1                          # refuses to overwrite
    assert cli.main(["backup", str(out), "--force"]) == 0
    assert cli.main(["verify-backup", str(out)]) == 0
    assert "1 sessions" in capsys.readouterr().out
    assert cli.main(["restore-backup", str(out)]) == 0
    assert "1 sessions were already present" in capsys.readouterr().out
    out.write_bytes(b"junk" * 100)
    assert cli.main(["verify-backup", str(out)]) == 1
    assert cli.main(["restore-backup", str(out)]) == 1
