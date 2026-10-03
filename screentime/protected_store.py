"""A normal SQLite database that lives in memory, persisted as an encrypted log.

Why this shape: without SQLCipher we cannot have SQLite encrypt its own pages,
but we want to keep *SQL* (the stats code is all SQL) and keep SQLite's crash
safety and multi-process locking. So:

* The working database is a plain in-memory SQLite connection (RAM only;
  `temp_store=MEMORY` so even big sorts never spill plaintext to /tmp).
* Every write the `Database` class performs is recorded as (sql, params) and
  sealed into one AES-GCM record in the `SecureLog` (which is a SQLite file, so
  the append is atomic and durable, and the WAL holds only ciphertext).
* Any process rebuilds the same database by replaying snapshot + records. The
  daemon and the GUI therefore stay consistent: a writer takes the log's write
  lock, first applies whatever other processes appended, then does its work and
  appends its own record.
* The daemon periodically folds the log into an encrypted snapshot (compaction)
  so startup stays fast and the file doesn't grow without bound.

Every heartbeat is logged (none are coalesced): the daily-totals updates are
deltas relative to the previous checkpoint, so replaying only the latest
heartbeat would undercount.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import sqlite3
import zlib
from contextlib import contextmanager
from typing import Optional

from .secure_log import EventGapError, SecureLog, SecureStoreError, StoreFormatError, TamperError

log = logging.getLogger("screentime.protected")

COMPACT_EVENT_THRESHOLD = 20000       # ~ half a day of 2s heartbeats
_TXN_WORDS = {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}
_DDL_WORDS = {"CREATE", "DROP", "ALTER", "REINDEX", "VACUUM"}


class ReplayError(StoreFormatError):
    """A record authenticated fine but could not be applied (a bug or a schema mismatch)."""


# ---------------------------------------------------------------- encoding
def _enc_value(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return {"$f": repr(v)}
    return v


def _dec_value(v):
    if isinstance(v, dict):
        if "$b" in v:
            return base64.b64decode(v["$b"])
        if "$f" in v:
            return float(v["$f"])
    return v


def encode_statements(stmts: list) -> bytes:
    out = []
    for kind, sql, params in stmts:
        if kind == "script":
            out.append(["script", sql])
        elif isinstance(params, dict):
            out.append(["sql", sql, {"$named": {k: _enc_value(v) for k, v in params.items()}}])
        else:
            out.append(["sql", sql, [_enc_value(v) for v in params]])
    return zlib.compress(json.dumps(out, separators=(",", ":"), allow_nan=False).encode(), 1)


def decode_statements(payload: bytes) -> list:
    try:
        items = json.loads(zlib.decompress(payload))
    except (zlib.error, ValueError) as e:
        raise ReplayError(f"record is not valid: {e}") from None
    out = []
    for item in items:
        if item[0] == "script":
            out.append(("script", item[1], None))
        else:
            params = item[2]
            if isinstance(params, dict) and "$named" in params:
                params = {k: _dec_value(v) for k, v in params["$named"].items()}
            else:
                params = [_dec_value(v) for v in params]
            out.append(("sql", item[1], params))
    return out


def normalize_image(data: bytes) -> bytes:
    """A serialized *WAL-mode* database carries "WAL" flags in its header (bytes
    18/19 == 2) and an in-memory copy cannot open such an image (there is no WAL
    file to go with it). Rewrite them to the rollback-journal value (1); the data
    pages are untouched. See https://www.sqlite.org/fileformat2.html#the_database_header"""
    if len(data) > 19 and data[18] == 2 and data[19] == 2:
        buf = bytearray(data)
        buf[18] = buf[19] = 1
        return bytes(buf)
    return data


# --------------------------------------------------------- recording connection
class RecordingConnection:
    """What `Database` writes through. Forwards to the in-memory connection and
    remembers every *successful* write statement so the enclosing unit of work
    can seal them into one record. Rolled-back transactions are forgotten."""

    def __init__(self):
        self.mem: Optional[sqlite3.Connection] = None
        self.buffer: list = []
        self._mark: Optional[int] = None

    def _note(self, sql: str, params, changes_before: int):
        word = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if word == "BEGIN":
            self._mark = len(self.buffer)
        elif word in ("COMMIT", "END"):
            self._mark = None
        elif word == "ROLLBACK":
            if self._mark is not None:
                del self.buffer[self._mark:]
            self._mark = None
        elif word in _TXN_WORDS or word == "PRAGMA":
            pass
        elif word in _DDL_WORDS or self.mem.total_changes != changes_before:
            self.buffer.append(("sql", sql, params if isinstance(params, dict) else list(params)))

    def execute(self, sql, params=()):
        before = self.mem.total_changes
        cur = self.mem.execute(sql, params)
        self._note(sql, params, before)
        return cur

    def executemany(self, sql, seq):
        seq = list(seq)
        cur = None
        for params in seq:
            cur = self.execute(sql, params)
        return cur

    def executescript(self, script):
        self.mem.executescript(script)
        self.buffer.append(("script", script, None))

    def __getattr__(self, name):                 # row_factory, cursor, in_transaction, ...
        return getattr(self.mem, name)


# --------------------------------------------------------------------- store
class ProtectedStore:
    def __init__(self, log_: SecureLog, compact_threshold: int = COMPACT_EVENT_THRESHOLD):
        self.log = log_
        self.path = log_.path
        self.compact_threshold = compact_threshold
        self.conn = RecordingConnection()
        self._snap_id: Optional[int] = None
        self._applied = 0
        self._depth = 0
        self._seen_version: Optional[int] = None
        self._load()

    # ---- building the in-memory database
    @staticmethod
    def _new_mem(serialized: Optional[bytes]) -> sqlite3.Connection:
        c = sqlite3.connect(":memory:", isolation_level=None)
        if serialized:
            c.deserialize(normalize_image(serialized))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA temp_store=MEMORY")          # never spill plaintext to a temp file
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _load(self):
        """(Re)build the database from the newest snapshot plus later records."""
        version = self.log.data_version()
        snap = self.log.latest_snapshot()
        data = self.log.read_snapshot(snap[0]) if snap else None
        old = self.conn.mem
        self.conn.mem = self._new_mem(data)
        self.conn.buffer.clear()
        self.conn._mark = None
        if old is not None:
            old.close()
        self._snap_id = snap[0] if snap else None
        self._applied = snap[1] if snap else 0
        self._replay_new_events()
        self._seen_version = version

    def _replay_new_events(self):
        for eid, payload in self.log.events_after(self._applied):
            self._apply(eid, decode_statements(payload))
            self._applied = eid

    def _apply(self, eid: int, stmts: list):
        mem = self.conn.mem                      # raw connection: replays are never re-recorded
        try:
            if any(kind == "script" for kind, _s, _p in stmts):
                # Schema scripts manage their own transactions (executescript
                # commits first); they are only ever recorded on their own.
                for kind, sql, params in stmts:
                    mem.executescript(sql) if kind == "script" else mem.execute(sql, params)
                return
            mem.execute("BEGIN")
            try:
                for _kind, sql, params in stmts:
                    mem.execute(sql, params)
                mem.execute("COMMIT")
            except BaseException:
                mem.execute("ROLLBACK")
                raise
        except Exception as e:
            raise ReplayError(f"record {eid} could not be applied: {e}") from e

    def _catch_up(self):
        snap = self.log.latest_snapshot()
        if (snap[0] if snap else None) != self._snap_id:
            self._load()
            return
        try:
            self._replay_new_events()
        except EventGapError:
            if self.log.latest_snapshot() != snap:          # compacted while we were behind
                self._load()
            else:
                raise

    def refresh(self):
        """Pick up what other processes wrote. Cheap when nothing changed."""
        if self._depth:
            return
        version = self.log.data_version()
        if version == self._seen_version:
            return
        self._catch_up()
        self._seen_version = version

    # ---- writes
    @contextmanager
    def unit(self):
        """One atomic, durable change: take the cross-process write lock, catch up,
        run the caller's statements, seal them into one record. Re-entrant."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.log.begin_write()
        try:
            version = self.log.data_version()
            if version != self._seen_version:
                self._catch_up()
                self._seen_version = version
            self.conn.buffer.clear()
            self._depth = 1
            try:
                yield
            finally:
                self._depth = 0
            if self.conn.buffer:
                eid = self.log.append_event(encode_statements(self.conn.buffer))
                self.log.commit()
                self._applied = eid
            else:
                self.log.commit()
            self.conn.buffer.clear()
        except BaseException:
            dirty = bool(self.conn.buffer)
            self.conn.buffer.clear()
            try:
                self.log.rollback()
            finally:
                if dirty:
                    # Memory changed but the record was not committed: rebuild
                    # from the log so memory can never disagree with disk.
                    self._load()
            raise

    # ---- maintenance
    def compact(self, force: bool = False) -> bool:
        """Fold the log into an encrypted snapshot (daemon only; it is the
        long-lived process). Safe against concurrent readers/writers: they
        detect the new snapshot and reload from it."""
        if self._depth:
            return False
        if not force and self.log.event_count() < self.compact_threshold:
            return False
        self.log.begin_write()
        try:
            self._catch_up()
            data = self.conn.mem.serialize()
            sid = self.log.write_snapshot(self._applied, data)
            self.log.prune(self._applied, sid)
            self.log.commit()
            self._snap_id = sid
            self._seen_version = self.log.data_version()
        except BaseException:
            self.log.rollback()
            raise
        log.info("compacted the database log into snapshot %s", sid)
        return True

    def table_digests(self) -> dict:
        self.refresh()                      # compare *current* state, not whatever we last happened to read
        return table_digests(self.conn.mem)

    def close(self):
        try:
            self.log.close()
        finally:
            if self.conn.mem is not None:
                self.conn.mem.close()


def table_digests(conn: sqlite3.Connection) -> dict:
    """{table: (row_count, sha256)} over every user table, row by row. Used to
    prove a migrated database is identical to its source."""
    import hashlib
    out = {}
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for name in names:
        h, n = hashlib.sha256(), 0
        for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
            h.update(repr(tuple(row)).encode())
            n += 1
        out[name] = (n, h.hexdigest())
    return out
