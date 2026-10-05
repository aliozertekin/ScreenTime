"""Encrypted backup and restore of the usage history.

A backup file (`ScreenTime-backup-YYYY-MM-DD.screentime`) is a normal ScreenTime secure store holding ONE
sealed snapshot of the database: the same AES-256-GCM container, the same key, nothing new. So:

* The key is never written into the backup. To open a backup you need the key that made it: it is found
  automatically on the machine that created it, and on any other machine you enter the *recovery key*
  (Settings -> Security -> Show recovery key), exactly as for the live database.
* A header (plaintext, non-secret) records the format name/version, when it was made and by which
  ScreenTime version. Usage data is only in the ciphertext.
* Nothing in a backup file leaves this computer unless you copy it somewhere yourself.

Restoring is *additive*: sessions that are not in your current history are added; nothing existing is
changed or deleted, so restoring never overwrites a database, and restoring the same backup twice (or onto
the machine it came from) adds nothing. The backup is fully checked -- format, version, key, every record's
authentication tag, database integrity -- before the first change is made. The backup file itself is never
modified (it is opened from a private copy).
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__
from . import storage
from .db import Database, local_day
from .keystore import KeyManager, KeyNotFoundError, KeyStoreError, KeyUnavailableError, decode_recovery_key
from .protected_store import ProtectedStore
from .secure_log import SecureLog, SecureStoreError, StoreFormatError, WrongKeyError

BACKUP_FORMAT = "screentime-backup"
BACKUP_VERSION = 1
EXTENSION = ".screentime"
KNOWN_DB_SCHEMA = 1
_BATCH = 2000
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BackupError(Exception):
    """Base class; the message is fit to show to the user."""


class IncompatibleBackupError(BackupError):
    """Not a ScreenTime backup, or made by a newer, incompatible version."""


class CorruptBackupError(BackupError):
    """Damaged or modified: a record failed authentication or the database inside is broken."""


class NeedKeyError(BackupError):
    """The key for this backup is not on this machine; the recovery key of the machine that made it is needed."""


class WrongBackupKeyError(BackupError):
    """The key supplied does not open this backup."""


@dataclass
class BackupHeader:
    format: str
    version: int
    created: int
    app_version: str
    store_id: bytes


@dataclass
class BackupInfo:
    header: BackupHeader
    apps: int
    sessions: int
    first_day: Optional[str]
    last_day: Optional[str]


@dataclass
class RestoreResult:
    apps_added: int
    sessions_added: int
    sessions_skipped: int     # already present (or invalid) -- left as they were


def default_filename(today: Optional[datetime.date] = None) -> str:
    return f"ScreenTime-backup-{(today or datetime.date.today()).isoformat()}{EXTENSION}"


# ------------------------------------------------------------------- creating
def _live_log(db: Database) -> SecureLog:
    if db._store is None:
        raise BackupError("this database is not the encrypted store, so it cannot be backed up with the "
                          "same key (development/test mode)")
    return db._store.log


def create_backup(db: Database, dest, km: Optional[KeyManager] = None, interactive: bool = True,
                  overwrite: bool = False) -> BackupInfo:
    """Write an encrypted backup of the current history to `dest` and verify it before returning."""
    dest = Path(dest)
    if dest.exists() and not overwrite:
        raise FileExistsError(f"{dest} already exists")
    live = _live_log(db)
    km = km or KeyManager()
    try:
        key = km.get_key(live.store_id, interactive=interactive)
    except (KeyNotFoundError, KeyUnavailableError) as e:
        raise BackupError(storage.explain_open_error(e)) from e
    db._store.refresh()
    image = db._store.conn.mem.serialize()          # a consistent image of everything committed so far
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    try:
        out = SecureLog(tmp, key, create=True, store_id=live.store_id)
        try:
            out.begin_write()
            out.write_snapshot(0, image)
            out.commit()
            out.set_meta("backup_format", BACKUP_FORMAT)
            out.set_meta("backup_version", str(BACKUP_VERSION))
            out.set_meta("backup_created", str(int(time.time())))
            out.set_meta("backup_app_version", __version__)
            out.make_single_file()
        finally:
            out.close()
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
        info = verify_backup(tmp, km=km, key=key)               # never hand back a backup we cannot read
        os.replace(tmp, dest)
    except BaseException:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
        raise
    return info


# ------------------------------------------------------------------ inspecting
def read_header(path) -> BackupHeader:
    """Plaintext header only (no key needed). Opens the file read-only so the backup is never modified."""
    path = Path(path)
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            meta = {k: bytes(v) for k, v in con.execute("SELECT k, v FROM meta")}
        finally:
            con.close()
    except (sqlite3.DatabaseError, OSError) as e:
        raise IncompatibleBackupError(f"{path.name} is not a ScreenTime backup ({e})") from None
    if meta.get("backup_format", b"").decode() != BACKUP_FORMAT or "store_id" not in meta:
        raise IncompatibleBackupError(f"{path.name} is not a ScreenTime backup")
    try:
        version = int(meta["backup_version"].decode())
        created = int(meta.get("backup_created", b"0").decode())
    except (KeyError, ValueError):
        raise IncompatibleBackupError(f"{path.name} has a damaged header") from None
    if version > BACKUP_VERSION:
        raise IncompatibleBackupError(
            f"{path.name} was made by a newer ScreenTime (backup format {version}; this version understands "
            f"up to {BACKUP_VERSION}). Update ScreenTime to restore it.")
    if version < 1:
        raise IncompatibleBackupError(f"{path.name} has an unsupported backup format ({version})")
    return BackupHeader(BACKUP_FORMAT, version, created, meta.get("backup_app_version", b"?").decode(),
                        meta["store_id"])


def resolve_key(header: BackupHeader, km: Optional[KeyManager] = None, recovery_key: Optional[str] = None,
                interactive: bool = False) -> bytes:
    """The key for this backup: the recovery key if one is given, else whatever this machine holds."""
    if recovery_key:
        try:
            return decode_recovery_key(recovery_key)
        except ValueError as e:
            raise WrongBackupKeyError(str(e)) from None
    try:
        return (km or KeyManager()).get_key(header.store_id, interactive=interactive)
    except KeyNotFoundError:
        raise NeedKeyError("This backup was made with a key that is not on this computer. Enter the recovery "
                           "key from the computer (or install) that created it.") from None
    except KeyUnavailableError as e:
        raise BackupError(storage.explain_open_error(e)) from e


def _private_copy(path: Path) -> Path:
    d = Path(tempfile.mkdtemp(prefix="screentime-restore-", dir=storage.data_dir()))
    os.chmod(d, 0o700)
    target = d / "backup.sec"
    shutil.copyfile(path, target)
    return target


def _open_copy(path: Path, key: bytes):
    """Open a private copy of the backup. Returns (workdir, SecureLog). Caller removes workdir."""
    copy = _private_copy(path)
    try:
        return copy.parent, SecureLog(copy, key)
    except WrongKeyError:
        shutil.rmtree(copy.parent, ignore_errors=True)
        raise WrongBackupKeyError("That key does not open this backup.") from None
    except StoreFormatError as e:
        shutil.rmtree(copy.parent, ignore_errors=True)
        raise IncompatibleBackupError(f"{path.name} is not a usable ScreenTime backup: {e}") from None
    except BaseException:
        shutil.rmtree(copy.parent, ignore_errors=True)
        raise


def _load_checked(path: Path, key: bytes):
    """Fully validate and return (workdir, ProtectedStore) for a private copy of the backup."""
    work, log_ = _open_copy(path, key)
    try:
        log_.verify_all()                                  # every record authenticates
        store = ProtectedStore(log_)
        mem = store.conn.mem
        if mem.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CorruptBackupError(f"{path.name} contains a damaged database")
        tables = {r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"apps", "sessions", "daily_totals", "schema_meta"} <= tables:
            raise IncompatibleBackupError(f"{path.name} does not contain ScreenTime history")
        schema = mem.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] or 0
        if schema > KNOWN_DB_SCHEMA:
            raise IncompatibleBackupError(
                f"{path.name} uses a newer database layout (v{schema}); update ScreenTime to restore it.")
        return work, store
    except (SecureStoreError, sqlite3.DatabaseError) as e:
        try:
            log_.close()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)
        raise CorruptBackupError(f"{path.name} is damaged or was modified: {e}") from None
    except BaseException:
        try:
            log_.close()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)
        raise


def verify_backup(path, km: Optional[KeyManager] = None, recovery_key: Optional[str] = None,
                  key: Optional[bytes] = None, interactive: bool = False) -> BackupInfo:
    """Check a backup end to end without changing anything. Raises a BackupError subclass on any problem."""
    path = Path(path)
    header = read_header(path)
    key = key or resolve_key(header, km, recovery_key, interactive)
    work, store = _load_checked(path, key)
    try:
        mem = store.conn.mem
        apps = mem.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
        n, first, last = mem.execute("SELECT COUNT(*), MIN(day), MAX(day) FROM sessions").fetchone()
        return BackupInfo(header, apps, n, first, last)
    finally:
        store.close()
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------- restoring
def _clean_session(r: sqlite3.Row) -> Optional[dict]:
    try:
        start = int(r["start_time"])
        if start < 0:
            return None
        day = r["day"] if isinstance(r["day"], str) and _DAY.match(r["day"]) else local_day(start)
        return {"app_key": r["key"], "start_time": start, "end_time": r["end_time"],
                "last_heartbeat": r["last_heartbeat"], "day": day, "end_reason": r["end_reason"]}
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def restore_backup(db: Database, path, km: Optional[KeyManager] = None, recovery_key: Optional[str] = None,
                   interactive: bool = False) -> RestoreResult:
    """Add the history in `path` to `db`. The whole backup is validated first; nothing is changed if it
    fails. Existing history is never modified or deleted."""
    path = Path(path)
    header = read_header(path)
    key = resolve_key(header, km, recovery_key, interactive)
    work, store = _load_checked(path, key)
    try:
        mem = store.conn.mem
        apps = [dict(r) for r in mem.execute(
            "SELECT key, display_name, icon_name, desktop_file, excluded, first_seen, last_seen FROM apps")]
        total = RestoreResult(0, 0, 0)
        cur = mem.execute("SELECT a.key AS key, s.start_time, s.end_time, s.last_heartbeat, s.day, s.end_reason "
                          "FROM sessions s JOIN apps a ON a.id = s.app_id ORDER BY s.start_time, s.id")
        first = True
        while True:
            rows = cur.fetchmany(_BATCH)
            if not rows and not first:
                break
            batch = [c for c in (_clean_session(r) for r in rows) if c is not None]
            bad = len(rows) - len(batch)
            a_add, s_add, s_skip = db.merge_history(apps if first else [], batch)
            first = False
            total = RestoreResult(total.apps_added + a_add, total.sessions_added + s_add,
                                  total.sessions_skipped + s_skip + bad)
            if not rows:
                break
        return total
    finally:
        store.close()
        shutil.rmtree(work, ignore_errors=True)
