"""The encrypted container: confidentiality, authentication, and tamper detection."""
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

import pytest

from screentime.secure_log import (EventGapError, SecureLog, SecureStoreError, StoreFormatError,
                                   TamperError, WrongKeyError, derive_key)

KEY = bytes(range(32))


@pytest.fixture
def store(tmp_path):
    log = SecureLog(tmp_path / "t.sec", KEY, create=True)
    yield log
    log.close()


def add(log, *payloads):
    log.begin_write()
    ids = [log.append_event(p) for p in payloads]
    log.commit()
    return ids


def raw_update(path, sql, params=()):
    con = sqlite3.connect(path)
    con.execute(sql, params)
    con.commit()
    con.close()


# ------------------------------------------------------------ creation & roundtrip
def test_creation_makes_a_private_file_with_no_readable_tables(tmp_path):
    p = tmp_path / "t.sec"
    SecureLog(p, KEY, create=True).close()
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    con = sqlite3.connect(p)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert names >= {"meta", "events", "snapshots", "snapshot_chunks"}
    assert not names & {"apps", "sessions", "settings", "daily_totals"}            # no usage tables at all


def test_refuses_to_overwrite_an_existing_file(tmp_path):
    p = tmp_path / "t.sec"
    p.write_bytes(b"precious")
    with pytest.raises(SecureStoreError):
        SecureLog(p, KEY, create=True)
    assert p.read_bytes() == b"precious"


def test_events_roundtrip_in_order(store):
    add(store, b"one", b"two", b"three")
    assert [p for _i, p in store.events_after(0)] == [b"one", b"two", b"three"]
    assert [p for _i, p in store.events_after(2)] == [b"three"]
    assert store.head() == 3 and store.event_count() == 3


def test_reopening_with_the_right_key_works(tmp_path):
    p = tmp_path / "t.sec"
    log = SecureLog(p, KEY, create=True)
    add(log, b"persisted")
    log.close()
    again = SecureLog(p, KEY)
    assert [x for _i, x in again.events_after(0)] == [b"persisted"]
    again.close()


def test_snapshot_roundtrip_including_multi_chunk(store):
    data = os.urandom(3_500_000)                      # > 3 chunks even after compression (random => incompressible)
    store.begin_write()
    sid = store.write_snapshot(0, data)
    store.commit()
    assert store.read_snapshot(sid) == data
    assert store.latest_snapshot() == (sid, 0)


def test_empty_snapshot_roundtrip(store):
    store.begin_write()
    sid = store.write_snapshot(0, b"")
    store.commit()
    assert store.read_snapshot(sid) == b""


def test_key_derivation_is_bound_to_the_store_id():
    assert derive_key(KEY, b"a" * 16) != derive_key(KEY, b"b" * 16)
    assert derive_key(KEY, b"a" * 16) == derive_key(KEY, b"a" * 16)
    assert derive_key(KEY, b"a" * 16) != KEY                     # never the raw master key
    with pytest.raises(ValueError):
        derive_key(b"short", b"x" * 16)


# ------------------------------------------------------------------- confidentiality
MARKER = b"TOP-SECRET-APP-NAME-brave-browser-12345"


def test_plaintext_never_reaches_any_file_on_disk(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    p = data / "t.sec"
    log = SecureLog(p, KEY, create=True)
    add(log, MARKER + b" session 17:32")
    log.begin_write()
    log.write_snapshot(1, MARKER * 50)
    log.commit()
    # read *before* closing so the -wal/-shm files exist too
    blobs = b"".join(f.read_bytes() for f in data.iterdir())
    log.close()
    blobs += b"".join(f.read_bytes() for f in data.iterdir())
    assert b"TOP-SECRET" not in blobs and b"brave-browser" not in blobs
    assert KEY not in blobs                                                   # nor the key itself


def test_identical_plaintexts_produce_different_ciphertexts(store):
    add(store, b"same", b"same")
    con = sqlite3.connect(store.path)
    rows = con.execute("SELECT nonce, ct FROM events ORDER BY id").fetchall()
    con.close()
    assert rows[0][0] != rows[1][0] and rows[0][1] != rows[1][1]              # fresh nonce per record


# ----------------------------------------------------------------------- wrong key
def test_wrong_key_is_reported_as_wrong_key_not_tampering(tmp_path):
    p = tmp_path / "t.sec"
    SecureLog(p, KEY, create=True).close()
    with pytest.raises(WrongKeyError):
        SecureLog(p, secrets.token_bytes(32))


def test_one_flipped_key_bit_is_rejected(tmp_path):
    p = tmp_path / "t.sec"
    SecureLog(p, KEY, create=True).close()
    bad = bytearray(KEY)
    bad[0] ^= 1
    with pytest.raises(WrongKeyError):
        SecureLog(p, bytes(bad))


@pytest.mark.parametrize("junk", [b"", b"not sqlite at all", b"SQLite format 3\0" + b"\0" * 200])
def test_non_store_files_are_rejected_cleanly(tmp_path, junk):
    p = tmp_path / "x.sec"
    p.write_bytes(junk)
    with pytest.raises(SecureStoreError):
        SecureLog(p, KEY)


def test_plain_sqlite_database_is_not_mistaken_for_a_store(tmp_path):
    p = tmp_path / "legacy.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE apps(id INTEGER)")
    con.commit()
    con.close()
    with pytest.raises(StoreFormatError):
        SecureLog(p, KEY)


# ------------------------------------------------------------------- tamper detection
def test_flipping_any_ciphertext_bit_is_detected(store):
    add(store, b"alpha", b"beta")
    store.close()
    raw_update(store.path, "UPDATE events SET ct = CAST((CASE WHEN substr(ct, 1, 1) = X'00' THEN X'01' ELSE X'00' END) || substr(ct, 2) AS BLOB) WHERE id = 2")
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        list(log.events_after(0))
    log.close()


def test_editing_a_nonce_is_detected(store):
    add(store, b"alpha")
    store.close()
    raw_update(store.path, "UPDATE events SET nonce = randomblob(12) WHERE id = 1")
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        list(log.events_after(0))
    log.close()


def test_swapping_two_events_is_detected(store):
    """Each record is bound to its position, so reordering fails authentication."""
    add(store, b"first", b"second")
    store.close()
    con = sqlite3.connect(store.path)
    (n1, c1), (n2, c2) = con.execute("SELECT nonce, ct FROM events ORDER BY id").fetchall()
    con.execute("UPDATE events SET nonce=?, ct=? WHERE id=1", (n2, c2))
    con.execute("UPDATE events SET nonce=?, ct=? WHERE id=2", (n1, c1))
    con.commit()
    con.close()
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        list(log.events_after(0))
    log.close()


def test_deleting_an_event_from_the_middle_is_detected(store):
    add(store, b"a", b"b", b"c")
    store.close()
    raw_update(store.path, "DELETE FROM events WHERE id = 2")
    log = SecureLog(store.path, KEY)
    with pytest.raises(EventGapError):
        list(log.events_after(0))
    log.close()


def test_event_cannot_be_moved_between_stores(tmp_path):
    """A valid record from store A must not verify inside store B even with the same key."""
    a, b = tmp_path / "a.sec", tmp_path / "b.sec"
    la, lb = SecureLog(a, KEY, create=True), SecureLog(b, KEY, create=True)
    add(la, b"from A")
    add(lb, b"from B")
    la.close()
    lb.close()
    ca = sqlite3.connect(a)
    nonce, ct = ca.execute("SELECT nonce, ct FROM events WHERE id=1").fetchone()
    ca.close()
    raw_update(b, "UPDATE events SET nonce=?, ct=? WHERE id=1", (nonce, ct))
    log = SecureLog(b, KEY)
    with pytest.raises(TamperError):
        list(log.events_after(0))
    log.close()


def test_tampering_with_a_snapshot_chunk_is_detected(store):
    store.begin_write()
    sid = store.write_snapshot(0, os.urandom(2_500_000))
    store.commit()
    store.close()
    raw_update(store.path, "UPDATE snapshot_chunks SET ct = CAST((CASE WHEN substr(ct, 1, 1) = X'00' THEN X'01' ELSE X'00' END) || substr(ct, 2) AS BLOB) WHERE snapshot_id=? AND idx=1", (sid,))
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        log.read_snapshot(sid)
    log.close()


def test_dropping_or_reordering_snapshot_chunks_is_detected(store):
    store.begin_write()
    sid = store.write_snapshot(0, os.urandom(2_500_000))
    store.commit()
    store.close()
    raw_update(store.path, "DELETE FROM snapshot_chunks WHERE snapshot_id=? AND idx=2", (sid,))
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        log.read_snapshot(sid)
    log.close()


def test_snapshot_chunks_cannot_be_reshuffled(store):
    store.begin_write()
    sid = store.write_snapshot(0, os.urandom(2_500_000))
    store.commit()
    store.close()
    con = sqlite3.connect(store.path)
    # swap chunk 0 and 1 (via a temporary index: idx is part of the primary key)
    con.execute("UPDATE snapshot_chunks SET idx = idx + 100 WHERE snapshot_id=? AND idx IN (0, 1)", (sid,))
    con.execute("UPDATE snapshot_chunks SET idx = CASE idx WHEN 100 THEN 1 ELSE 0 END WHERE snapshot_id=? AND idx >= 100", (sid,))
    con.commit()
    con.close()
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        log.read_snapshot(sid)
    log.close()


def test_changing_a_snapshots_coverage_is_detected(store):
    """`upto` is authenticated: claiming the snapshot covers more/less than it does fails."""
    add(store, b"x")
    store.begin_write()
    sid = store.write_snapshot(1, b"image")
    store.commit()
    store.close()
    raw_update(store.path, "UPDATE snapshots SET upto = 0 WHERE id=?", (sid,))
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        log.read_snapshot(sid)
    log.close()


def test_verify_all_authenticates_everything_and_fails_on_damage(store):
    add(store, b"a", b"b")
    store.begin_write()
    store.write_snapshot(2, b"snap")
    store.commit()
    add(store, b"c")
    assert store.verify_all() == {"snapshot_bytes": 4, "events": 1}
    store.close()
    raw_update(store.path, "UPDATE events SET ct = CAST((CASE WHEN substr(ct, 1, 1) = X'00' THEN X'01' ELSE X'00' END) || substr(ct, 2) AS BLOB)")
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        log.verify_all()
    log.close()


def test_documented_limitation_truncating_the_newest_events_is_not_detected(store):
    """Honest about what is NOT protected: dropping the *tail* (or restoring an
    older valid copy) leaves a perfectly valid, merely older, log. The README
    documents this; this test pins the behavior so it is never overclaimed."""
    add(store, b"a", b"b", b"c")
    store.close()
    raw_update(store.path, "DELETE FROM events WHERE id = 3")
    log = SecureLog(store.path, KEY)
    assert [p for _i, p in log.events_after(0)] == [b"a", b"b"]
    log.close()


# ------------------------------------------------------------------ transactions / crash
def test_uncommitted_append_is_not_visible_and_is_discarded(tmp_path):
    p = tmp_path / "t.sec"
    log = SecureLog(p, KEY, create=True)
    add(log, b"committed")
    log.begin_write()
    log.append_event(b"never committed")
    log.rollback()
    assert [x for _i, x in log.events_after(0)] == [b"committed"]
    assert log.append_event if True else None
    log.close()


def test_write_lock_is_exclusive_across_connections(tmp_path):
    p = tmp_path / "t.sec"
    a = SecureLog(p, KEY, create=True)
    b = SecureLog(p, KEY, busy_timeout_ms=200)
    a.begin_write()
    with pytest.raises(sqlite3.OperationalError):
        b.begin_write()                                    # second writer must wait/fail, never interleave
    a.rollback()
    b.begin_write()
    b.rollback()
    a.close()
    b.close()


def test_data_version_signals_other_connections_commits(tmp_path):
    p = tmp_path / "t.sec"
    a = SecureLog(p, KEY, create=True)
    b = SecureLog(p, KEY)
    v = b.data_version()
    assert b.data_version() == v                           # nothing happened
    add(a, b"news")
    assert b.data_version() != v                           # another connection committed
    a.close()
    b.close()


def test_prune_removes_covered_events_and_older_snapshots(store):
    add(store, b"a", b"b", b"c")
    store.begin_write()
    s1 = store.write_snapshot(2, b"one")
    store.prune(2, s1)
    store.commit()
    assert store.event_count() == 1
    store.begin_write()
    s2 = store.write_snapshot(3, b"two")
    store.prune(3, s2)
    store.commit()
    assert store.event_count() == 0 and store.latest_snapshot() == (s2, 3)
    with pytest.raises(StoreFormatError):
        store.read_snapshot(s1)                             # the old one is gone
    assert store.head() == 3


def test_event_ids_are_never_reused_after_pruning(store):
    add(store, b"a", b"b")
    store.begin_write()
    sid = store.write_snapshot(2, b"s")
    store.prune(2, sid)
    store.commit()
    assert add(store, b"c") == [3]


@pytest.mark.parametrize("edit", [
    "UPDATE events SET ct = 'plain text instead of a blob'",
    "UPDATE events SET ct = NULL WHERE 0",            # no-op control (must stay valid)
    "UPDATE events SET nonce = 'text'",
    "UPDATE events SET nonce = X''",
    "UPDATE events SET ct = X''",
])
def test_malformed_record_columns_are_tampering_not_a_crash(store, edit):
    add(store, b"a")
    store.close()
    raw_update(store.path, edit)
    log = SecureLog(store.path, KEY)
    if "WHERE 0" in edit:
        assert [p for _i, p in log.events_after(0)] == [b"a"]
    else:
        with pytest.raises(TamperError):
            list(log.events_after(0))
    log.close()


def test_snapshot_chunk_of_wrong_type_is_tampering(store):
    store.begin_write()
    sid = store.write_snapshot(0, b"image")
    store.commit()
    store.close()
    raw_update(store.path, "UPDATE snapshot_chunks SET ct = 'text'")
    log = SecureLog(store.path, KEY)
    with pytest.raises(TamperError):
        log.read_snapshot(sid)
    log.close()


def test_damage_helper_changes_the_first_byte_whatever_it_was():
    """Regression: the tamper tests overwrite the first ciphertext byte with 0x00. Ciphertext is random, so one run in
    256 the byte already WAS 0x00, the 'damage' was a no-op and the test failed with 'DID NOT RAISE'."""
    import sqlite3
    expr = "CAST((CASE WHEN substr(ct, 1, 1) = X'00' THEN X'01' ELSE X'00' END) || substr(ct, 2) AS BLOB)"
    con = sqlite3.connect(":memory:")
    for first in range(256):
        ct = bytes([first]) + b"rest-of-ciphertext"
        assert con.execute(f"SELECT {expr} FROM (SELECT ? AS ct)", (ct,)).fetchone()[0] != ct
