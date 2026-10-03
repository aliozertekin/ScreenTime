"""Authenticated-encryption container for the ScreenTime database.

The container is an ordinary SQLite file (so we keep SQLite's crash safety,
WAL and cross-process locking) in which **every value that matters is an
AES-256-GCM ciphertext**: no readable tables, no readable rows. Its `-wal` and
`-shm` files therefore contain only ciphertext too.

Layout (all sensitive data is in the blobs)::

    meta(k, v)            plaintext: format tag, random store id, key-check blob
    events(id, nonce, ct) append-only: one sealed record per database change
    snapshots(id, upto, chunks)            sealed full-database snapshots
    snapshot_chunks(snapshot_id, idx, nonce, ct)

Security properties
-------------------
* Confidentiality + integrity: AES-256-GCM, a fresh random 96-bit nonce per
  record, and a subkey derived with HKDF-SHA256 from the master key and the
  store's random id (so two stores never share a key stream).
* Binding: each record's associated data includes the store id and its
  position (event id / snapshot id, chunk index and count). A record cannot be
  swapped with another, moved between stores, reordered, or have chunks dropped
  or reshuffled without failing authentication.
* Wrong key vs. tampering are told apart by a sealed key-check value.

Not provided (by design, documented in the README): rollback protection (an
attacker who can write the file can replace it with an *older valid* copy, or
cut off the most recent events), and hiding of metadata such as the file size,
the number of records and when each was written.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Iterator, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT_TAG = "screentime-secure-store/1"
KEY_BYTES = 32
CHUNK_BYTES = 1 << 20          # snapshots are sealed in 1 MiB chunks
_HKDF_INFO = b"screentime/secure-store/v1/aes-256-gcm"


class SecureStoreError(Exception):
    """Base class for everything this module raises."""


class WrongKeyError(SecureStoreError):
    """The key does not open this store (nothing has been tampered with)."""


class TamperError(SecureStoreError):
    """A record failed authentication: modified, truncated/reordered, or corrupt."""


class EventGapError(TamperError):
    """Event ids are not consecutive. Benign if a compaction pruned them while we
    were behind (the caller then reloads from the snapshot); tampering otherwise."""


class StoreFormatError(SecureStoreError):
    """Not a ScreenTime secure store, or an unsupported version."""


def derive_key(master: bytes, store_id: bytes) -> bytes:
    if len(master) != KEY_BYTES:
        raise ValueError(f"master key must be {KEY_BYTES} bytes")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=store_id, info=_HKDF_INFO).derive(master)


def _i(n: int, width: int = 8) -> bytes:
    return int(n).to_bytes(width, "big")


class SecureLog:
    def __init__(self, path: Path, master_key: bytes, create: bool = False, busy_timeout_ms: int = 15000,
                 store_id: Optional[bytes] = None):
        self.path = Path(path)
        if create:
            if self.path.exists():
                raise SecureStoreError(f"{self.path} already exists")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)   # private from the first byte
            os.close(fd)
        elif not self.path.exists():
            raise StoreFormatError(f"{self.path} does not exist")
        self._db = sqlite3.connect(str(self.path), isolation_level=None, timeout=busy_timeout_ms / 1000)
        self._in_txn = False
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            if create:
                self._create(master_key, store_id)
            self._open(master_key)
        except sqlite3.DatabaseError as e:
            self._db.close()
            raise StoreFormatError(f"{self.path} is not a ScreenTime secure store: {e}") from e
        except BaseException:
            self._db.close()
            raise

    # ---------------------------------------------------------------- setup
    def _create(self, master: bytes, store_id: Optional[bytes] = None):
        store_id = store_id or secrets.token_bytes(16)
        self._db.executescript(
            "CREATE TABLE meta(k TEXT PRIMARY KEY, v BLOB NOT NULL);"
            "CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, nonce BLOB NOT NULL, ct BLOB NOT NULL);"
            "CREATE TABLE snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, upto INTEGER NOT NULL, "
            "chunks INTEGER NOT NULL, created INTEGER NOT NULL);"
            "CREATE TABLE snapshot_chunks(snapshot_id INTEGER NOT NULL, idx INTEGER NOT NULL, "
            "nonce BLOB NOT NULL, ct BLOB NOT NULL, PRIMARY KEY(snapshot_id, idx));"
        )
        aead = AESGCM(derive_key(master, store_id))
        nonce = secrets.token_bytes(12)
        check = nonce + aead.encrypt(nonce, b"key-check", b"ST1|kc|" + store_id)
        self._db.executemany("INSERT INTO meta(k, v) VALUES (?, ?)",
                             [("format", FORMAT_TAG.encode()), ("store_id", store_id), ("key_check", check)])

    def _open(self, master: bytes):
        try:
            meta = dict(self._db.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.DatabaseError as e:
            raise StoreFormatError(f"not a ScreenTime secure store: {e}") from e
        if meta.get("format") != FORMAT_TAG.encode():
            raise StoreFormatError("unsupported store format")
        self.store_id: bytes = meta["store_id"]
        self._aead = AESGCM(derive_key(master, self.store_id))
        check = meta["key_check"]
        try:
            ok = self._aead.decrypt(check[:12], check[12:], b"ST1|kc|" + self.store_id) == b"key-check"
        except InvalidTag:
            ok = False
        if not ok:
            raise WrongKeyError("this key does not open the database")

    # -------------------------------------------------------------- sealing
    def _seal(self, plaintext: bytes, aad: bytes):
        nonce = secrets.token_bytes(12)
        return nonce, self._aead.encrypt(nonce, plaintext, aad)

    def _unseal(self, nonce: bytes, ct: bytes, aad: bytes, what: str) -> bytes:
        try:
            return self._aead.decrypt(nonce, ct, aad)
        except (InvalidTag, ValueError):                 # ValueError: nonce of an impossible length
            raise TamperError(f"{what} failed authentication (modified, reordered or corrupt)") from None

    @staticmethod
    def _blob(value) -> bytes:
        """A record column that is not a BLOB (e.g. edited into TEXT/NULL) is tampering."""
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TamperError("a record has the wrong type (edited or corrupt)")
        return bytes(value)

    def _aad_event(self, event_id: int) -> bytes:
        return b"ST1|ev|" + self.store_id + _i(event_id)

    def _aad_chunk(self, snap_id: int, upto: int, idx: int, n: int) -> bytes:
        return b"ST1|sn|" + self.store_id + _i(snap_id) + _i(upto) + _i(idx, 4) + _i(n, 4)

    # --------------------------------------------------------- transactions
    def begin_write(self):
        """Take the cross-process write lock (SQLite's RESERVED lock)."""
        self._db.execute("BEGIN IMMEDIATE")
        self._in_txn = True

    def commit(self):
        if self._in_txn:
            self._db.execute("COMMIT")
            self._in_txn = False

    def rollback(self):
        if self._in_txn:
            self._db.execute("ROLLBACK")
            self._in_txn = False

    def data_version(self) -> int:
        """Changes whenever *another* connection commits -- a cheap 'is there news?'."""
        return self._db.execute("PRAGMA data_version").fetchone()[0]

    # --------------------------------------------------------------- events
    def _last_event_id(self) -> int:
        row = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return int(row[0]) if row else 0

    def append_event(self, payload: bytes) -> int:
        """Seal and store one record; must be called inside begin_write()."""
        event_id = self._last_event_id() + 1
        nonce, ct = self._seal(payload, self._aad_event(event_id))
        self._db.execute("INSERT INTO events(id, nonce, ct) VALUES (?, ?, ?)", (event_id, nonce, ct))
        return event_id

    def events_after(self, last_id: int) -> Iterator[tuple]:
        """Yield (id, payload) for every event after `last_id`, verifying each.
        Raises TamperError on a bad record and StoreFormatError on a gap."""
        expected = last_id + 1
        try:
            rows = self._db.execute("SELECT id, nonce, ct FROM events WHERE id > ? ORDER BY id", (last_id,))
            for eid, nonce, ct in rows:
                if eid != expected:
                    raise EventGapError(
                        f"event {expected} is missing (next is {eid}): the log was truncated, edited or compacted")
                yield eid, self._unseal(self._blob(nonce), self._blob(ct), self._aad_event(eid), f"event {eid}")
                expected += 1
        except sqlite3.DatabaseError as e:
            raise TamperError(f"the event log is unreadable (corrupt or edited): {e}") from None

    def event_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def head(self) -> int:
        """Id of the newest record (events or the snapshot's coverage)."""
        snap = self.latest_snapshot()
        return max(self._last_event_id(), snap[1] if snap else 0)

    # ------------------------------------------------------------ snapshots
    def latest_snapshot(self) -> Optional[tuple]:
        """(snapshot_id, upto_event_id) of the newest snapshot, or None."""
        row = self._db.execute("SELECT id, upto FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else None

    def write_snapshot(self, upto: int, data: bytes) -> int:
        """Seal a full-database image covering events <= upto (inside begin_write())."""
        packed = zlib.compress(data, 3)
        pieces = [packed[i:i + CHUNK_BYTES] for i in range(0, len(packed), CHUNK_BYTES)] or [b""]
        cur = self._db.execute("INSERT INTO snapshots(upto, chunks, created) VALUES (?, ?, ?)",
                               (upto, len(pieces), int(time.time())))
        snap_id = cur.lastrowid
        for idx, piece in enumerate(pieces):
            nonce, ct = self._seal(piece, self._aad_chunk(snap_id, upto, idx, len(pieces)))
            self._db.execute("INSERT INTO snapshot_chunks(snapshot_id, idx, nonce, ct) VALUES (?, ?, ?, ?)",
                             (snap_id, idx, nonce, ct))
        return snap_id

    def read_snapshot(self, snap_id: int) -> bytes:
        row = self._db.execute("SELECT upto, chunks FROM snapshots WHERE id = ?", (snap_id,)).fetchone()
        if row is None:
            raise StoreFormatError(f"snapshot {snap_id} does not exist")
        upto, n = row
        parts = []
        try:
            rows = self._db.execute("SELECT idx, nonce, ct FROM snapshot_chunks WHERE snapshot_id = ? ORDER BY idx",
                                    (snap_id,)).fetchall()
        except sqlite3.DatabaseError as e:
            raise TamperError(f"snapshot {snap_id} is unreadable (corrupt or edited): {e}") from None
        if [r[0] for r in rows] != list(range(n)):
            raise TamperError(f"snapshot {snap_id} is missing chunks")
        for idx, nonce, ct in rows:
            parts.append(self._unseal(self._blob(nonce), self._blob(ct), self._aad_chunk(snap_id, upto, idx, n),
                                      f"snapshot {snap_id} chunk {idx}"))
        try:
            return zlib.decompress(b"".join(parts))
        except zlib.error as e:
            raise TamperError(f"snapshot {snap_id} is corrupt: {e}") from None

    def prune(self, upto: int, keep_snapshot: int):
        """Drop events covered by a snapshot, and every older snapshot."""
        self._db.execute("DELETE FROM events WHERE id <= ?", (upto,))
        old = [r[0] for r in self._db.execute("SELECT id FROM snapshots WHERE id < ?", (keep_snapshot,))]
        for sid in old:
            self._db.execute("DELETE FROM snapshot_chunks WHERE snapshot_id = ?", (sid,))
            self._db.execute("DELETE FROM snapshots WHERE id = ?", (sid,))

    # ------------------------------------------------------------- plaintext meta
    # Non-secret bookkeeping only (e.g. "migrated_at"); never usage data.
    def set_meta(self, k: str, v: str):
        self._db.execute("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                         (k, v.encode()))

    def get_meta(self, k: str) -> Optional[str]:
        row = self._db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return bytes(row[0]).decode() if row else None

    # ------------------------------------------------------------- utilities
    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        return total

    def verify_all(self) -> dict:
        """Authenticate every record. Raises TamperError/StoreFormatError on the first bad one."""
        snap = self.latest_snapshot()
        snap_bytes = len(self.read_snapshot(snap[0])) if snap else 0
        n = 0
        for _ in self.events_after(snap[1] if snap else 0):
            n += 1
        return {"snapshot_bytes": snap_bytes, "events": n}

    def checkpoint(self):
        """Fold the WAL into the main file (still only ciphertext either way)."""
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self):
        try:
            self.rollback()
        finally:
            self._db.close()
