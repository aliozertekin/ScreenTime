"""
SQLite persistence layer for ScreenTime.

Design goals:
  * Single source of truth is the `sessions` table (append-only). `daily_totals`
    is a derived cache updated incrementally in the same transaction as the
    session write that produced the delta, so it can never drift for long, and
    can always be rebuilt from `sessions` if it ever does (see rebuild_daily_totals).
  * Every mutating call commits immediately (small, frequent transactions) so a
    daemon crash or power loss loses at most the last uncommitted heartbeat tick
    (a few seconds), never historical totals.
  * WAL mode is used so readers (the GUI) never block the writer (the daemon)
    and vice versa.
"""

from __future__ import annotations

import functools
import sqlite3
import time
import datetime
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Iterable

SCHEMA_PATH = Path(__file__).resolve().parent / "migrations" / "001_init.sql"


def default_db_path() -> Path:
    from . import platform as _platform
    return _platform.paths().data_dir() / "screentime.db"


def local_day(ts: float) -> str:
    """Local calendar day string for a unix timestamp."""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def day_end_epoch(day: str) -> float:
    """Unix timestamp of the last instant (23:59:59) of a local day string."""
    d = datetime.datetime.strptime(day, "%Y-%m-%d")
    d = d.replace(hour=23, minute=59, second=59, microsecond=999000)
    return d.timestamp()


def day_start_epoch(day: str) -> float:
    d = datetime.datetime.strptime(day, "%Y-%m-%d")
    return d.timestamp()


def _writes(fn):
    """Mark a Database method as a write. With a protected store it runs as one
    atomic unit of work (cross-process write lock, catch up, record, seal); with
    a plain SQLite file it is a no-op, exactly as before."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if self._store is None:
            return fn(self, *args, **kwargs)
        with self._store.unit():
            return fn(self, *args, **kwargs)
    return wrapper


@dataclass
class App:
    id: int
    key: str
    display_name: str
    icon_name: Optional[str]
    desktop_file: Optional[str]
    excluded: bool
    first_seen: int
    last_seen: int


class Database:
    def __init__(self, path: Optional[Path] = None, recover_orphans: bool = True, store=None):
        """recover_orphans: close sessions left open by a crashed daemon.
        Only the daemon (which holds the single-instance lock) may do this --
        a GUI connection must pass False, or merely opening the window would
        close the *live* daemon's open session.

        store: a `ProtectedStore` (see protected_store.py). Production code gets
        one from `storage.open_database()`; the usage data then lives encrypted
        on disk. Without it this is a plain SQLite file -- used by tests and
        never by the daemon or GUI."""
        self._store = store
        self._recover_orphans = recover_orphans
        if store is not None:
            self.path = store.path
            self._plain = None
        else:
            if path is None:
                # A bare Database() used to open the default plaintext file. That is
                # now a mistake: it would create an unencrypted usage database next
                # to the protected one. Production code must use storage.open_database().
                raise RuntimeError(
                    "Database() needs an explicit path (tests) or a protected store; "
                    "use screentime.storage.open_database() to open ScreenTime's real database")
            self.path = path
            self._plain = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            self._plain.row_factory = sqlite3.Row
            self._plain.execute("PRAGMA foreign_keys = ON")
            self._plain.execute("PRAGMA journal_mode = WAL")
            self._plain.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    @property
    def _conn(self):
        """The connection Database methods use. For a protected store, reading
        first picks up whatever other processes (the daemon) have written."""
        if self._store is None:
            return self._plain
        self._store.refresh()
        return self._store.conn

    @property
    def protected(self) -> bool:
        return self._store is not None

    def maintenance(self) -> bool:
        """Fold the encrypted log into a snapshot when it has grown (daemon,
        periodically). No-op for a plain SQLite file. True if it compacted."""
        return self._store.compact() if self._store is not None else False

    # ------------------------------------------------------------------ setup
    @_writes
    def _migrate(self):
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        )
        if cur.fetchone() is None:
            sql = SCHEMA_PATH.read_text()
            self._conn.executescript(sql)
        # Recover any session left open by a previous crash (no GUI/daemon
        # ever left it open on purpose past process exit).
        if self._recover_orphans:
            self._recover_orphaned_sessions()

    def _recover_orphaned_sessions(self):
        """If the daemon died without closing its open session, close it now
        using the last plausible timestamp we have (the session's own start
        time is a safe lower bound; we use 'now' capped to end-of-that-day so
        we never fabricate large fake usage)."""
        rows = self._conn.execute(
            "SELECT id, start_time, last_heartbeat, day FROM sessions WHERE end_time IS NULL"
        ).fetchall()
        for row in rows:
            # Close at the last committed heartbeat checkpoint (never later),
            # so recovery cannot fabricate usage time that was never observed
            # -- at most one heartbeat interval's worth of real usage is lost.
            recovered_end = row["last_heartbeat"] if row["last_heartbeat"] is not None else row["start_time"]
            self._close_session(row["id"], recovered_end, "crash_recovered")

    # --------------------------------------------------------------- apps
    @_writes
    def get_or_create_app(self, key: str, display_name: str, icon_name: Optional[str],
                           desktop_file: Optional[str]) -> App:
        now = int(time.time())
        row = self._conn.execute("SELECT * FROM apps WHERE key = ?", (key,)).fetchone()
        if row:
            self._conn.execute(
                "UPDATE apps SET last_seen = ?, display_name = ?, icon_name = COALESCE(?, icon_name), "
                "desktop_file = COALESCE(?, desktop_file) WHERE id = ?",
                (now, display_name, icon_name, desktop_file, row["id"]),
            )
            row = self._conn.execute("SELECT * FROM apps WHERE id = ?", (row["id"],)).fetchone()
        else:
            self._conn.execute(
                "INSERT INTO apps(key, display_name, icon_name, desktop_file, excluded, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, 0, ?, ?)",
                (key, display_name, icon_name, desktop_file, now, now),
            )
            row = self._conn.execute("SELECT * FROM apps WHERE key = ?", (key,)).fetchone()
        return self._row_to_app(row)

    def get_app_by_key(self, key: str) -> Optional[App]:
        row = self._conn.execute("SELECT * FROM apps WHERE key = ?", (key,)).fetchone()
        return self._row_to_app(row) if row else None

    @_writes
    def rename_app(self, app_id: int, display_name: str, icon_name: Optional[str] = None):
        self._conn.execute(
            "UPDATE apps SET display_name = ?, icon_name = COALESCE(?, icon_name) WHERE id = ?",
            (display_name, icon_name, app_id),
        )

    @_writes
    def set_excluded(self, app_id: int, excluded: bool):
        self._conn.execute("UPDATE apps SET excluded = ? WHERE id = ?", (1 if excluded else 0, app_id))

    def list_apps(self, include_excluded: bool = True) -> list[App]:
        q = "SELECT * FROM apps"
        if not include_excluded:
            q += " WHERE excluded = 0"
        q += " ORDER BY display_name COLLATE NOCASE"
        return [self._row_to_app(r) for r in self._conn.execute(q)]

    def get_app(self, app_id: int) -> Optional[App]:
        row = self._conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        return self._row_to_app(row) if row else None

    @staticmethod
    def _row_to_app(row: sqlite3.Row) -> App:
        return App(
            id=row["id"], key=row["key"], display_name=row["display_name"],
            icon_name=row["icon_name"], desktop_file=row["desktop_file"],
            excluded=bool(row["excluded"]), first_seen=row["first_seen"], last_seen=row["last_seen"],
        )

    # ----------------------------------------------------------- sessions
    @_writes
    def open_session(self, app_id: int, start_time: float) -> int:
        day = local_day(start_time)
        cur = self._conn.execute(
            "INSERT INTO sessions(app_id, start_time, end_time, day, end_reason) VALUES (?, ?, NULL, ?, NULL)",
            (app_id, int(start_time), day),
        )
        return cur.lastrowid

    @_writes
    def heartbeat_session(self, session_id: int, new_progress_time: float):
        """Advance the open session's progress checkpoint so a crash loses at
        most one heartbeat interval of data, while `end_time` stays NULL for
        the session's entire open lifetime (open/closed stays a simple
        boolean check). Rolls the daily_totals cache forward incrementally by
        the delta since the last checkpoint."""
        row = self._conn.execute(
            "SELECT app_id, start_time, last_heartbeat, day FROM sessions WHERE id = ? AND end_time IS NULL",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        prev = row["last_heartbeat"] if row["last_heartbeat"] is not None else row["start_time"]
        delta = max(0, int(new_progress_time) - int(prev))
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "UPDATE sessions SET last_heartbeat = ? WHERE id = ?", (int(new_progress_time), session_id)
            )
            if delta > 0:
                self._bump_daily_total(row["app_id"], row["day"], delta, session_delta=0)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    @_writes
    def _close_session(self, session_id: int, end_time: float, reason: str):
        row = self._conn.execute(
            "SELECT app_id, start_time, last_heartbeat, end_time, day FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return
        if row["end_time"] is not None:
            return  # already closed, nothing to do
        prev = row["last_heartbeat"] if row["last_heartbeat"] is not None else row["start_time"]
        end_time = max(end_time, row["start_time"])
        delta = max(0, int(end_time) - int(prev))
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "UPDATE sessions SET end_time = ?, end_reason = ? WHERE id = ?",
                (int(end_time), reason, session_id),
            )
            if delta > 0:
                self._bump_daily_total(row["app_id"], row["day"], delta, session_delta=0)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    @_writes
    def close_session(self, session_id: int, end_time: float, reason: str):
        self._close_session(session_id, end_time, reason)
        # Count the completed session once it is finalized.
        row = self._conn.execute("SELECT app_id, day FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row:
            self._bump_daily_total(row["app_id"], row["day"], 0, session_delta=1)

    def _bump_daily_total(self, app_id: int, day: str, seconds_delta: int, session_delta: int):
        self._conn.execute(
            "INSERT INTO daily_totals(app_id, day, seconds, session_count) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(app_id, day) DO UPDATE SET "
            "seconds = seconds + excluded.seconds, session_count = session_count + excluded.session_count",
            (app_id, day, seconds_delta, session_delta),
        )

    @_writes
    def split_session_at_midnight(self, session_id: int, close_time: float, open_time: float) -> int:
        """Close the currently open session at the last instant of its local
        day and open a fresh one starting at 00:00:00 the next day for the
        same app, so a session row in storage never spans two calendar days."""
        row = self._conn.execute("SELECT app_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        app_id = row["app_id"]
        self._close_session(session_id, close_time, "midnight_split")
        return self.open_session(app_id, open_time)

    @_writes
    def rebuild_daily_totals(self):
        """Recompute daily_totals from scratch off the sessions table. Useful
        after manual DB edits or if a bug is ever suspected in the cache."""
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM daily_totals")
            self._conn.execute(
                "INSERT INTO daily_totals(app_id, day, seconds, session_count) "
                "SELECT app_id, day, "
                "SUM(COALESCE(end_time, last_heartbeat, start_time) - start_time), COUNT(*) "
                "FROM sessions GROUP BY app_id, day"
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_open_session(self) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM sessions WHERE end_time IS NULL").fetchone()

    def sessions_for_app(self, app_id: int, day: Optional[str] = None) -> list[sqlite3.Row]:
        if day:
            return list(self._conn.execute(
                "SELECT * FROM sessions WHERE app_id = ? AND day = ? ORDER BY start_time",
                (app_id, day),
            ))
        return list(self._conn.execute(
            "SELECT * FROM sessions WHERE app_id = ? ORDER BY start_time", (app_id,)
        ))

    def sessions_in_range(self, start_day: str, end_day: str, app_id: Optional[int] = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM sessions WHERE day >= ? AND day <= ?"
        params: list = [start_day, end_day]
        if app_id is not None:
            q += " AND app_id = ?"
            params.append(app_id)
        q += " ORDER BY start_time"
        return list(self._conn.execute(q, params))

    # ----------------------------------------------------------- settings
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    @_writes
    def set_setting(self, key: str, value: str):
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --------------------------------------------------------------- misc
    def close(self):
        if self._store is not None:
            self._store.close()
        else:
            self._plain.close()

    @property
    def conn(self) -> sqlite3.Connection:
        """Read-side connection for stats queries (always current)."""
        if self._store is not None:
            self._store.refresh()
            return self._store.conn.mem
        return self._plain
