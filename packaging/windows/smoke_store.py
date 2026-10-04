"""Smoke check for the protected store a freshly started ScreenTime leaves behind.

screentime.sec is deliberately an ORDINARY SQLite file (that keeps SQLite's crash safety, WAL and cross-process
locking -- see screentime/secure_log.py). What is sealed with AES-256-GCM is every record INSIDE it, so "the file
starts with 'SQLite format 3'" is expected and says nothing about confidentiality. This checks what does:

  * no plaintext legacy screentime.db (or -wal/-shm/-journal) was created as a side effect
  * the container is a ScreenTime secure store: its four tables, the format tag and a sealed key-check in `meta`,
    and NO readable usage tables (apps / sessions / settings / daily_totals)
  * every events / snapshot_chunks row is a BLOB nonce (12 bytes) + ciphertext (>= the 16-byte GCM tag)
  * a marker written through the normal API reads back, yet appears in NO file on disk
  * no record is readable without the key: the app zlib-compresses every payload BEFORE sealing it, so an unsealed
    record parses as zlib (and a raw marker scan alone cannot see it), while real ciphertext never does
  * every record authenticates under the stored key, and a wrong key is rejected

Run by packaging/windows/smoke.ps1 with the BUNDLED interpreter (so it exercises the installed app), and unit-tested
on Linux in tests/test_windows_packaging.py. The marker is a fixed, non-secret test string; no real key, credential or
user data is used (the wrong key is random throwaway bytes).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import zlib
from pathlib import Path
from typing import Optional

from screentime import storage
from screentime.keystore import KeyManager
from screentime.secure_log import FORMAT_TAG, KEY_BYTES, SecureLog, SecureStoreError, WrongKeyError

MARKER = "SCREENTIME_SMOKE_PLAINTEXT_MARKER"
SQLITE_MAGIC = b"SQLite format 3\x00"
REQUIRED_TABLES = {"meta", "events", "snapshots", "snapshot_chunks"}
USAGE_TABLES = {"apps", "sessions", "settings", "daily_totals"}          # what the old plaintext database had
NONCE_BYTES, TAG_BYTES, STORE_ID_BYTES = 12, 16, 16
KEY_CHECK_BYTES = NONCE_BYTES + len(b"key-check") + TAG_BYTES            # nonce + AES-GCM("key-check")


class SmokeFail(Exception):
    pass


def _need(out: list, cond: bool, ok: str, fail: Optional[str] = None) -> None:
    if not cond:
        raise SmokeFail(fail or ok)
    out.append("ok  - " + ok)


def _parses_as_zlib(blob: bytes) -> bool:
    try:
        zlib.decompress(blob)
    except zlib.error:
        return False
    return True


def _read(paths) -> bytes:
    return b"".join(p.read_bytes() for p in paths if p.is_file())


def check(dd: Path, key_manager: Optional[KeyManager] = None, out: Optional[list] = None) -> list:
    """Run every check against the store in `dd`. Returns the 'ok' lines; raises SmokeFail on the first failure."""
    out = [] if out is None else out
    km = key_manager or KeyManager()
    sp = storage.store_path(dd)

    # -- no plaintext database as a side effect
    legacy = [p.name for s in ("", "-wal", "-shm", "-journal")
              if (p := Path(str(storage.legacy_path(dd)) + s)).exists()]
    _need(out, not legacy, "no plaintext legacy database (screentime.db*) exists",
          f"a plaintext legacy database exists next to the store: {legacy}")

    # -- the container: an ordinary SQLite file, by design
    _need(out, sp.is_file(), f"{sp.name} exists")
    with open(sp, "rb") as f:
        _need(out, f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC,
              "the container is an ordinary SQLite file (intended: the records inside are what is sealed)",
              f"{sp.name} is not a SQLite container")

    # -- write a marker through the normal protected-storage API; it must read back but never reach any file
    db = storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False)
    try:
        db.set_setting("smoke_marker", MARKER)
        _need(out, db.get_setting("smoke_marker") == MARKER, "a marker written through the normal API reads back")
        # while open, the -wal holds the newest records. (-shm is only the wal-index: no record data, and it is
        # byte-range locked on Windows, so it is left to the after-close scan below.)
        live = _read([sp, Path(str(sp) + "-wal")])
    finally:
        db.close()
    again = storage.open_database(data_dir_=dd, key_manager=km, recover_orphans=False)
    try:
        persisted = again.get_setting("smoke_marker")
    finally:
        again.close()
    _need(out, persisted == MARKER, "the marker persists and decrypts after reopening with the stored key")
    raw = live + _read(p for p in dd.iterdir())
    needles = (MARKER.encode("ascii"), MARKER.encode("utf-16-le"))
    _need(out, not any(n in raw for n in needles), "the marker appears in no file on disk (store, -wal, locks)",
          "the plaintext marker was found in the raw files: the store is NOT encrypting")

    # -- the container's structure: sealed blobs, no readable usage data
    con = sqlite3.connect(str(sp))
    try:
        con.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _need(out, REQUIRED_TABLES <= tables, f"container has the secure-store tables {sorted(REQUIRED_TABLES)}",
              f"not a ScreenTime secure store: tables are {sorted(tables)}")
        _need(out, not tables & USAGE_TABLES, "container has no readable usage tables (apps/sessions/settings/...)",
              f"readable usage tables in the container: {sorted(tables & USAGE_TABLES)}")
        meta = dict(con.execute("SELECT k, v FROM meta"))
        _need(out, meta.get("format") == FORMAT_TAG.encode(), f"meta.format is {FORMAT_TAG}")
        _need(out, len(meta.get("store_id", b"")) == STORE_ID_BYTES, "meta.store_id is a random 16-byte id")
        _need(out, len(meta.get("key_check", b"")) == KEY_CHECK_BYTES,
              "meta.key_check is a sealed value (12-byte nonce + AES-GCM of the check string)")
        sealed = 0
        for table in ("events", "snapshot_chunks"):
            n, bad = con.execute(
                f"SELECT COUNT(*), COALESCE(SUM(typeof(nonce) <> 'blob' OR typeof(ct) <> 'blob' "
                f"OR length(nonce) <> {NONCE_BYTES} OR length(ct) < {TAG_BYTES}), 0) FROM {table}").fetchone()
            _need(out, bad == 0, f"all {n} {table} record(s) are BLOB nonce(12) + ciphertext(>=16)",
                  f"{bad} of {n} {table} record(s) are not sealed (nonce/ciphertext malformed)")
            sealed += n
        _need(out, sealed > 0, "the store holds sealed records, so the checks above are not vacuous")
        # Payloads are zlib-compressed and then sealed (protected_store.encode_statements, SecureLog.write_snapshot).
        # AES-GCM output is indistinguishable from random bytes, which cannot form a valid zlib stream (its Adler-32
        # trailer would have to match, ~2^-32); a record that was compressed but NOT sealed parses at once -- with or
        # without a trailing tag-sized suffix. That is what the raw marker scan alone could not see.
        unsealed = 0
        for table in ("events", "snapshot_chunks"):
            for (ct,) in con.execute(f"SELECT ct FROM {table}"):
                ct = bytes(ct)
                unsealed += _parses_as_zlib(ct) or _parses_as_zlib(ct[:-TAG_BYTES])
        _need(out, unsealed == 0, "no record is readable without the key (none parses as compressed plaintext)",
              f"{unsealed} record(s) are compressed PLAINTEXT, not ciphertext: the store is NOT encrypting")
    finally:
        con.close()

    # -- the key: every record authenticates under the stored key; a wrong key is rejected
    try:
        stats = storage.verify_store(dd, km, interactive=False)
    except SecureStoreError as e:
        raise SmokeFail(f"a record failed authentication under the stored key: {e}") from e
    _need(out, True, f"every record authenticates under the stored key ({stats['events']} event(s) after the "
                     f"snapshot, snapshot {stats['snapshot_bytes']} bytes)")
    try:
        SecureLog(sp, os.urandom(KEY_BYTES)).close()
    except WrongKeyError:
        out.append("ok  - a wrong key is rejected (WrongKeyError)")
    else:
        raise SmokeFail("a random key opened the store")
    return out


def main() -> int:
    out: list = []
    try:
        check(storage.data_dir(), out=out)
    except SmokeFail as e:
        print("\n".join(out))
        print(f"FAIL - {e}")
        return 1
    print("\n".join(out))
    print("STORE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
