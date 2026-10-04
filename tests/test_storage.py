"""Opening, creating, migrating and recovering the protected database."""
import hashlib
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from screentime import keystore as K
from screentime import storage
from screentime.keystore import (KeyFileBackend, KeyManager, KeyNotFoundError, KeyUnavailableError)
from screentime.protected_store import ProtectedStore, table_digests
from screentime.secure_log import SecureLog, TamperError, WrongKeyError

ROOT = Path(__file__).resolve().parent.parent
LEGACY_APPS = [("brave-browser", "Brave"), ("steam_app_2620", "Call of Duty"), ("konsole", "Konsole")]


@pytest.fixture
def dirs(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d, tmp_path / "cfg"


def file_km(cfg):
    return KeyManager(cfg, backends=[lambda interactive: KeyFileBackend(cfg)])


def make_legacy(dd: Path, leave_wal: bool = True, sessions: int = 40):
    """A genuine old-format plaintext database, written by the OLD (plain) code
    path in a fresh interpreter. leave_wal: exit without closing, so an
    uncheckpointed -wal file is left behind like a running/crashed old daemon."""
    script = textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        from screentime.db import Database
        db = Database(Path({str(dd / 'screentime.db')!r}))
        now = 1_790_000_000
        for k, n in {LEGACY_APPS!r}:
            a = db.get_or_create_app(k, n, None, None)
            for j in range({sessions}):
                t = now + j * 600
                sid = db.open_session(a.id, t)
                db.heartbeat_session(sid, t + 200)
                db.close_session(sid, t + 420, "focus_change")
        db.set_setting("theme", "gruvbox-dark")
        db.set_setting("idle_timeout_seconds", "120")
        {'os._exit(0)' if leave_wal else 'db.close()'}
    """)
    env = {k: v for k, v in os.environ.items() if k != "SCREENTIME_TEST_PROTECTED"}
    subprocess.run([sys.executable, "-c", script], check=True, env=env, timeout=60)


def legacy_digests(dd: Path):
    con = sqlite3.connect(dd / "screentime.db")
    try:
        return table_digests(con)
    finally:
        con.close()


def names_in(dd: Path):
    return sorted(p.name for p in dd.iterdir())


def raw_bytes(dd: Path) -> bytes:
    return b"".join(p.read_bytes() for p in dd.iterdir() if p.is_file())


# ------------------------------------------------------------------- first run
def test_first_run_creates_a_protected_store_a_key_and_no_plaintext(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    db = storage.open_database(data_dir_=dd, key_manager=km)
    assert db.protected and (dd / "screentime.sec").exists() and not (dd / "screentime.db").exists()
    db.set_setting("hello", "world")
    db.close()
    assert km.current_backend() == "keyfile" and (cfg / "keys").exists()
    assert not [n for n in names_in(dd) if n.endswith(".db")]


def test_permissions_are_private(dirs):
    dd, cfg = dirs
    db = storage.open_database(data_dir_=dd, key_manager=file_km(cfg))
    db.set_setting("a", "b")
    assert stat.S_IMODE((dd / "screentime.sec").stat().st_mode) == 0o600
    for f in dd.glob("screentime.sec-*"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    keyfile = next((cfg / "keys").iterdir())
    assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
    db.close()


def test_data_survives_close_and_reopen(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    db = storage.open_database(data_dir_=dd, key_manager=km)
    app = db.get_or_create_app("kate", "Kate", None, None)
    sid = db.open_session(app.id, 1_790_000_000)
    db.close_session(sid, 1_790_000_500, "focus_change")
    db.close()
    again = storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False)
    assert again.conn.execute("SELECT seconds FROM daily_totals").fetchone()[0] == 500
    again.close()


def test_the_store_id_is_readable_without_a_key_but_nothing_else_is(dirs):
    dd, cfg = dirs
    db = storage.open_database(data_dir_=dd, key_manager=file_km(cfg))
    db.get_or_create_app("secret-app", "Secret App", None, None)
    db.close()
    assert len(storage.read_store_id(dd / "screentime.sec")) == 16
    assert b"secret-app" not in raw_bytes(dd)


# ------------------------------------------------------------------- migration
def test_migration_preserves_every_row_setting_and_total(dirs):
    dd, cfg = dirs
    make_legacy(dd)
    assert {"screentime.db", "screentime.db-wal"} <= set(names_in(dd))       # a live, uncheckpointed WAL
    want = legacy_digests(dd)
    db = storage.open_database(data_dir_=dd, key_manager=file_km(cfg), recover_orphans=False)
    assert db.protected
    assert table_digests(db.conn) == want                                      # identical, row for row
    assert db.get_setting("theme") == "gruvbox-dark" and db.get_setting("idle_timeout_seconds") == "120"
    assert db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 120
    assert sorted(a.display_name for a in db.list_apps()) == sorted(n for _k, n in LEGACY_APPS)
    db.close()


def test_migration_leaves_no_plaintext_copy_of_any_kind(dirs):
    dd, cfg = dirs
    make_legacy(dd)
    db = storage.open_database(data_dir_=dd, key_manager=file_km(cfg), recover_orphans=False)
    db.set_setting("x", "y")
    db.close()
    left = names_in(dd)
    assert not [n for n in left if n.startswith("screentime.db")], left        # no .db / -wal / -shm / -journal
    assert not [n for n in left if n.endswith(".tmp")], left
    assert set(left) <= {".store.lock", "screentime.sec", "screentime.sec-wal", "screentime.sec-shm"}


def test_no_app_name_or_timestamp_pattern_is_readable_after_migration(dirs):
    dd, cfg = dirs
    make_legacy(dd)
    storage.open_database(data_dir_=dd, key_manager=file_km(cfg), recover_orphans=False).close()
    blob = raw_bytes(dd)
    for needle in (b"Brave", b"Call of Duty", b"Konsole", b"steam_app_2620", b"brave-browser", b"gruvbox-dark"):
        assert needle not in blob, needle


def test_migration_overwrites_the_plaintext_before_removing_it(dirs):
    """A hard link keeps the old inode reachable: what remains must be zeros, not data."""
    dd, cfg = dirs
    make_legacy(dd, leave_wal=False)
    link = dd.parent / "kept-link"
    os.link(dd / "screentime.db", link)
    assert b"Brave" in link.read_bytes()
    storage.open_database(data_dir_=dd, key_manager=file_km(cfg), recover_orphans=False).close()
    assert not (dd / "screentime.db").exists()
    assert b"Brave" not in link.read_bytes() and set(link.read_bytes()) == {0}


def test_migration_works_for_a_cleanly_closed_legacy_database(dirs):
    dd, cfg = dirs
    make_legacy(dd, leave_wal=False)
    want = legacy_digests(dd)
    db = storage.open_database(data_dir_=dd, key_manager=file_km(cfg), recover_orphans=False)
    assert table_digests(db.conn) == want
    db.close()


def test_migrated_database_is_fully_usable_and_persists_new_writes(dirs):
    dd, cfg = dirs
    make_legacy(dd)
    km = file_km(cfg)
    db = storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False)
    app = db.get_or_create_app("kate", "Kate", None, None)
    db.set_setting("post", "migration")
    db.close()
    again = storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False)
    assert again.get_setting("post") == "migration" and again.get_setting("theme") == "gruvbox-dark"
    assert "Kate" in [a.display_name for a in again.list_apps()]
    again.close()


def test_migration_runs_once_and_is_idempotent(dirs):
    dd, cfg = dirs
    make_legacy(dd)
    km = file_km(cfg)
    storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False).close()
    key_files = sorted((cfg / "keys").iterdir())
    storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False).close()
    assert sorted((cfg / "keys").iterdir()) == key_files                       # no second key / second migration


def test_orphaned_open_session_in_legacy_data_is_recovered_by_the_daemon_after_migration(dirs):
    dd, cfg = dirs
    make_legacy(dd)
    con = sqlite3.connect(dd / "screentime.db")
    app_id = con.execute("SELECT id FROM apps LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO sessions(app_id, start_time, end_time, last_heartbeat, day) VALUES (?, 1790999000, NULL, 1790999100, '2026-09-30')", (app_id,))
    con.commit()
    con.close()
    db = storage.open_database(data_dir_=dd, key_manager=file_km(cfg), recover_orphans=True)
    assert db.get_open_session() is None                                       # closed at its last heartbeat
    db.close()


# -------------------------------------------------------- migration failure safety
def assert_original_untouched(dd, before_digests, before_hash):
    assert (dd / "screentime.db").exists()
    assert legacy_digests(dd) == before_digests
    assert not (dd / "screentime.sec").exists()
    assert not [n for n in names_in(dd) if n.endswith(".tmp") or ".sec.tmp" in n]


def fingerprint(dd):
    con = sqlite3.connect(dd / "screentime.db")
    try:
        return table_digests(con)
    finally:
        con.close()


def test_failed_verification_aborts_and_leaves_the_original_untouched(dirs, monkeypatch):
    dd, cfg = dirs
    make_legacy(dd)
    want = legacy_digests(dd)
    real = ProtectedStore.table_digests
    monkeypatch.setattr(ProtectedStore, "table_digests", lambda self: {**real(self), "sessions": (1, "tampered")})
    with pytest.raises(storage.MigrationError) as e:
        storage.open_database(data_dir_=dd, key_manager=file_km(cfg), recover_orphans=False)
    assert "sessions" in str(e.value) and "untouched" in str(e.value)
    assert_original_untouched(dd, want, None)


def test_failure_to_store_the_key_aborts_without_touching_the_original(dirs):
    dd, cfg = dirs
    make_legacy(dd)
    want = legacy_digests(dd)
    err = KeyUnavailableError("nowhere to put it", False)

    class Broken:
        name = "keyfile"
        def load(self, sid): return None
        def store(self, sid, key): raise err
    km = KeyManager(cfg, backends=[lambda i: Broken()])
    with pytest.raises(K.KeyStoreError):
        storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False)
    assert_original_untouched(dd, want, None)


def test_a_key_that_cannot_be_read_back_aborts_the_migration(dirs):
    """Verification uses the key fetched back from the keystore; if that fails, do not delete anything."""
    dd, cfg = dirs
    make_legacy(dd)
    want = legacy_digests(dd)
    real = KeyFileBackend(cfg)
    calls = {"n": 0}

    class Flaky:
        name = "keyfile"
        def store(self, sid, key): real.store(sid, key)
        def load(self, sid):
            calls["n"] += 1
            return real.load(sid) if calls["n"] == 1 else K.generate_key()       # 1st read-back ok, later reads wrong
        def delete(self, sid): real.delete(sid)
    km = KeyManager(cfg, backends=[lambda i: Flaky()])
    with pytest.raises((WrongKeyError, storage.MigrationError)):
        storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False)
    assert_original_untouched(dd, want, None)


def test_corrupt_legacy_database_is_not_migrated(dirs):
    dd, cfg = dirs
    make_legacy(dd, leave_wal=False)
    raw = bytearray((dd / "screentime.db").read_bytes())
    for i in range(4096 * 2, min(len(raw), 4096 * 6)):
        raw[i] = (raw[i] * 7 + 13) & 0xFF                                        # trash data pages
    (dd / "screentime.db").write_bytes(bytes(raw))
    with pytest.raises((storage.MigrationError, sqlite3.DatabaseError)):
        storage.open_database(data_dir_=dd, key_manager=file_km(cfg), recover_orphans=False)
    assert (dd / "screentime.db").exists() and not (dd / "screentime.sec").exists()


def test_crash_after_the_swap_but_before_plaintext_removal_is_finished_next_run(dirs, monkeypatch):
    dd, cfg = dirs
    make_legacy(dd, leave_wal=False)
    km = file_km(cfg)
    monkeypatch.setattr(storage, "_remove_legacy_files", lambda dd_: False)      # "crash": plaintext survives
    storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False).close()
    assert (dd / "screentime.db").exists() and (dd / "screentime.sec").exists()
    monkeypatch.undo()
    db = storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False)
    db.close()
    assert not [n for n in names_in(dd) if n.startswith("screentime.db")]        # leftover removed


def test_plaintext_modified_after_migration_is_kept_and_warned_about(dirs, monkeypatch, caplog):
    dd, cfg = dirs
    make_legacy(dd, leave_wal=False)
    km = file_km(cfg)
    monkeypatch.setattr(storage, "_remove_legacy_files", lambda dd_: False)
    storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False).close()
    monkeypatch.undo()
    future = (dd / "screentime.sec").stat().st_mtime + 3600
    os.utime(dd / "screentime.db", (future, future))                              # an old daemon wrote to it later
    with caplog.at_level(logging.WARNING, logger="screentime.storage"):
        storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False).close()
    assert (dd / "screentime.db").exists()
    assert any("modified after migration" in r.getMessage() for r in caplog.records)
    assert storage.security_status(dd, km).legacy_plaintext_present is True


def test_an_unrelated_plaintext_file_next_to_a_store_is_left_alone(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    storage.open_database(data_dir_=dd, key_manager=km).close()
    (dd / "screentime.db").write_bytes(b"someone else's file")
    storage.open_database(data_dir_=dd, key_manager=km).close()                   # no 'migrated_at' flag => not ours to delete
    assert (dd / "screentime.db").read_bytes() == b"someone else's file"


def test_two_processes_starting_at_once_migrate_exactly_once(dirs):
    dd, cfg = dirs
    make_legacy(dd)
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        from screentime import storage
        from screentime.keystore import KeyManager
        db = storage.open_database(data_dir_=Path({str(dd)!r}), key_manager=KeyManager(Path({str(cfg)!r})), recover_orphans=False)
        print(db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    """)
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent")
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env) for _ in range(3)]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0] and outs == ["120"] * 3
    assert len(list((cfg / "keys").iterdir())) == 1                                # one key, one migration


# ------------------------------------------------------------ key problems at open
def test_a_missing_key_is_reported_as_lost_with_recovery_instructions(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    storage.open_database(data_dir_=dd, key_manager=km).close()
    for f in (cfg / "keys").iterdir():
        f.unlink()
    with pytest.raises(storage.KeyLostError) as e:
        storage.open_database(data_dir_=dd, key_manager=km)
    assert "recovery key" in str(e.value) and storage.read_status()["state"] == "key-missing"


def test_a_wrong_key_is_reported_not_silently_accepted(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    storage.open_database(data_dir_=dd, key_manager=km).close()
    sid = storage.read_store_id(dd / "screentime.sec")
    KeyFileBackend(cfg).store(sid, K.generate_key())                              # some other key
    with pytest.raises(WrongKeyError):
        storage.open_database(data_dir_=dd, key_manager=km)
    assert storage.read_status()["state"] == "wrong-key"


class Scripted:
    """A KeyManager stand-in whose get_key follows a script of outcomes."""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
    def get_key(self, sid, interactive=False):
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def real_key_for(dd, cfg):
    return file_km(cfg).get_key(storage.read_store_id(dd / "screentime.sec"))


def test_daemon_waits_for_a_locked_keyring_then_proceeds(dirs):
    dd, cfg = dirs
    storage.open_database(data_dir_=dd, key_manager=file_km(cfg)).close()
    key = real_key_for(dd, cfg)
    locked = KeyUnavailableError("the keyring is locked", True)
    scripted = Scripted([locked, locked, locked, key])
    sleeps, states = [], []
    def fake_sleep(s):
        sleeps.append(s)
        states.append(storage.read_status()["state"])
    db = storage.open_database(data_dir_=dd, key_manager=scripted, wait_for_key=True, poll_seconds=7, sleep=fake_sleep)
    assert sum(sleeps) == pytest.approx(21) and max(sleeps) <= 0.2 + 1e-9        # 3 polls of 7s, in short slices
    assert set(states) == {"waiting"}
    assert storage.read_status()["state"] == "ok" and db.protected
    db.close()


def test_without_wait_a_locked_keyring_raises_immediately(dirs):
    dd, cfg = dirs
    storage.open_database(data_dir_=dd, key_manager=file_km(cfg)).close()
    with pytest.raises(KeyUnavailableError):
        storage.open_database(data_dir_=dd, key_manager=Scripted([KeyUnavailableError("locked", True)]), wait_for_key=False)
    assert storage.read_status()["state"] == "waiting"


def test_a_non_retryable_problem_is_not_waited_on(dirs):
    dd, cfg = dirs
    storage.open_database(data_dir_=dd, key_manager=file_km(cfg)).close()
    sleeps = []
    with pytest.raises(KeyUnavailableError):
        storage.open_database(data_dir_=dd, key_manager=Scripted([KeyUnavailableError("corrupt", False)]),
                              wait_for_key=True, sleep=sleeps.append)
    assert sleeps == []


def test_waiting_logs_once_not_on_every_poll(dirs, caplog):
    dd, cfg = dirs
    storage.open_database(data_dir_=dd, key_manager=file_km(cfg)).close()
    key = real_key_for(dd, cfg)
    locked = KeyUnavailableError("the keyring is locked", True)
    with caplog.at_level(logging.INFO, logger="screentime.storage"):
        storage.open_database(data_dir_=dd, key_manager=Scripted([locked] * 50 + [key]), wait_for_key=True,
                              poll_seconds=0, sleep=lambda s: None).close()
    waits = [r for r in caplog.records if "waiting for the keyring" in r.getMessage()]
    assert len(waits) == 1


def test_waiting_is_interruptible(dirs):
    """SIGTERM raises SystemExit inside the wait; it must propagate (clean shutdown)."""
    dd, cfg = dirs
    storage.open_database(data_dir_=dd, key_manager=file_km(cfg)).close()
    def stop(_s):
        raise SystemExit(0)
    with pytest.raises(SystemExit):
        storage.open_database(data_dir_=dd, key_manager=Scripted([KeyUnavailableError("locked", True)] * 3),
                              wait_for_key=True, sleep=stop)


# ----------------------------------------------------------------------- recovery
def test_recovery_key_restores_access_after_the_key_is_lost(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    db = storage.open_database(data_dir_=dd, key_manager=km)
    db.set_setting("precious", "data")
    db.close()
    recovery = storage.export_recovery_key(dd, km, interactive=False)
    for f in (cfg / "keys").iterdir():
        f.unlink()
    with pytest.raises(storage.KeyLostError):
        storage.open_database(data_dir_=dd, key_manager=km)
    assert storage.restore_from_recovery_key(recovery, dd, km, interactive=False) == "keyfile"
    again = storage.open_database(data_dir_=dd, key_manager=km)
    assert again.get_setting("precious") == "data" and storage.read_status()["state"] == "ok"
    again.close()


def test_a_wrong_recovery_key_is_rejected_and_never_replaces_the_working_key(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    storage.open_database(data_dir_=dd, key_manager=km).close()
    sid = storage.read_store_id(dd / "screentime.sec")
    good = km.get_key(sid)
    with pytest.raises(ValueError, match="does not belong"):
        storage.restore_from_recovery_key(K.encode_recovery_key(K.generate_key()), dd, km, interactive=False)
    assert km.get_key(sid) == good                                                # the working key is untouched
    storage.open_database(data_dir_=dd, key_manager=km).close()


def test_a_mistyped_recovery_key_is_rejected_before_anything_is_written(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    storage.open_database(data_dir_=dd, key_manager=km).close()
    text = storage.export_recovery_key(dd, km, interactive=False)
    typo = text[:5] + ("A" if text[5] != "A" else "B") + text[6:]
    with pytest.raises(ValueError, match="checksum"):
        storage.restore_from_recovery_key(typo, dd, km, interactive=False)


def test_restore_without_a_database_is_a_clear_error(dirs):
    dd, cfg = dirs
    with pytest.raises(ValueError, match="no protected database"):
        storage.restore_from_recovery_key(K.encode_recovery_key(K.generate_key()), dd, file_km(cfg), interactive=False)


def test_verify_store_authenticates_and_detects_damage(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    db = storage.open_database(data_dir_=dd, key_manager=km)
    for i in range(5):
        db.set_setting(f"k{i}", "v")
    db.close()
    assert storage.verify_store(dd, km, interactive=False)["events"] >= 5
    con = sqlite3.connect(dd / "screentime.sec")
    con.execute("UPDATE events SET ct = CAST((CASE WHEN substr(ct, 1, 1) = X'00' THEN X'01' ELSE X'00' END) || substr(ct, 2) AS BLOB) WHERE id = 3")
    con.commit()
    con.close()
    with pytest.raises(TamperError):
        storage.verify_store(dd, km, interactive=False)


# ------------------------------------------------------------------------- status
def test_security_status_reports_what_the_ui_shows(dirs):
    dd, cfg = dirs
    km = file_km(cfg)
    assert storage.security_status(dd, km).protected is False
    db = storage.open_database(data_dir_=dd, key_manager=km)
    db.set_setting("a", "b")
    st = storage.security_status(dd, km)
    assert st.protected and st.backend == "keyfile" and st.store_bytes > 0 and st.state == "ok"
    assert st.key_id == K.key_fingerprint(km.get_key(storage.read_store_id(dd / "screentime.sec")))
    assert st.legacy_plaintext_present is False
    db.close()


def test_security_status_flags_a_plaintext_database_present(dirs):
    dd, cfg = dirs
    (dd / "screentime.db").write_bytes(b"x")
    assert storage.security_status(dd, file_km(cfg)).legacy_plaintext_present is True


@pytest.mark.parametrize("err,needle", [
    (storage.KeyLostError("x"), "recovery key"),
    (KeyNotFoundError("x"), "recovery key"),
    (WrongKeyError("x"), "does not open"),
    (KeyUnavailableError("locked", True), "locked or unavailable"),
    (TamperError("x"), "integrity check"),
    (storage.MigrationError("boom"), "untouched"),
    (RuntimeError("weird"), "could not open"),
])
def test_open_errors_are_explained_in_plain_language(err, needle):
    assert needle in storage.explain_open_error(err)


def test_default_paths_follow_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xd"))
    assert storage.data_dir() == tmp_path / "xd" / "screentime"
    assert stat.S_IMODE(storage.data_dir().stat().st_mode) == 0o700
    assert storage.store_path().name == "screentime.sec" and storage.legacy_path().name == "screentime.db"


# ------------------------------------------- creation is atomic; stop requests are never lost
def test_interrupted_creation_leaves_no_half_built_store(dirs, monkeypatch):
    dd, cfg = dirs
    real = os.replace
    def boom(src, dst):
        raise KeyboardInterrupt("stopped mid-creation")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        storage.open_database(data_dir_=dd, key_manager=file_km(cfg))
    monkeypatch.setattr(os, "replace", real)
    assert not (dd / "screentime.sec").exists()
    assert not [n for n in names_in(dd) if ".sec.new" in n]
    db = storage.open_database(data_dir_=dd, key_manager=file_km(cfg))             # and the next start just works
    assert db.protected
    db.close()


def test_a_zero_byte_leftover_store_is_replaced_not_fatal(dirs):
    dd, cfg = dirs
    (dd / "screentime.sec").write_bytes(b"")
    db = storage.open_database(data_dir_=dd, key_manager=file_km(cfg))
    db.set_setting("a", "b")
    assert db.get_setting("a") == "b"
    db.close()


def test_a_non_empty_unreadable_store_is_never_deleted(dirs):
    """Only an *empty* file is treated as unfinished; anything else may be data."""
    dd, cfg = dirs
    (dd / "screentime.sec").write_bytes(b"not a database but not empty either")
    with pytest.raises(Exception):
        storage.open_database(data_dir_=dd, key_manager=file_km(cfg))
    assert (dd / "screentime.sec").read_bytes() == b"not a database but not empty either"


def test_wait_for_key_stops_when_the_stop_flag_is_set_even_if_the_signal_exception_was_lost(dirs):
    dd, cfg = dirs
    storage.open_database(data_dir_=dd, key_manager=file_km(cfg)).close()
    locked = KeyUnavailableError("locked", True)
    flag = {"stop": False}
    def sleep(_s):
        flag["stop"] = True                                  # the signal arrived, but raised nothing
    with pytest.raises(SystemExit):
        storage.open_database(data_dir_=dd, key_manager=Scripted([locked] * 5), wait_for_key=True,
                              sleep=sleep, should_stop=lambda: flag["stop"])


def test_sleep_is_sliced_so_a_stop_request_is_honored_quickly(dirs):
    slept = []
    flag = {"stop": False}
    def sleep(sec):
        slept.append(sec)
        if len(slept) == 3:
            flag["stop"] = True                               # the signal arrives during the 3rd slice
    with pytest.raises(SystemExit):
        storage._interruptible_sleep(sleep, 300, lambda: flag["stop"])
    assert sum(slept) < 1.0                                   # stopped within ~a second, not after 300s


def test_zero_length_sleep_still_checks_the_flag():
    with pytest.raises(SystemExit):
        storage._interruptible_sleep(lambda s: None, 0, lambda: True)


def test_waiting_for_the_setup_lock_honors_a_stop_request(dirs):
    import fcntl
    dd, cfg = dirs
    holder = os.open(dd / ".store.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)                        # another process is mid-migration
    try:
        with pytest.raises(SystemExit):
            storage.open_database(data_dir_=dd, key_manager=file_km(cfg), should_stop=lambda: True)
    finally:
        os.close(holder)
