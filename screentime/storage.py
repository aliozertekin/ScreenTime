"""Opening ScreenTime's protected database: locating it, finding the key,
creating it on first run, and migrating a legacy plaintext database.

Entry point for the daemon, the GUI and the CLI: `open_database()`. Nothing
else in the app opens the data file, and nothing here ever creates a plaintext
copy of usage data.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import platform as _platform
from .db import Database
from .platform import filelock as _filelock
from .keystore import (KeyManager, KeyNotFoundError, KeyStoreError, KeyUnavailableError, secure_delete_file)
from .protected_store import ProtectedStore, table_digests
from .secure_log import SecureLog, SecureStoreError, StoreFormatError, WrongKeyError  # noqa: F401 (re-exported)
from .keystore import KeyStoreError  # noqa: F401,E402 (re-exported)

log = logging.getLogger("screentime.storage")

STORE_NAME = "screentime.sec"
LEGACY_NAME = "screentime.db"
_SIDECARS = ("", "-wal", "-shm", "-journal")


class StorageError(Exception):
    pass


class KeyLostError(StorageError):
    """The database exists but no key for it can be found on this machine."""


class MigrationError(StorageError):
    """Migration was aborted; the original database has NOT been touched."""


# ----------------------------------------------------------------------- paths
def data_dir() -> Path:
    return _platform.paths().data_dir()


def store_path(dd: Optional[Path] = None) -> Path:
    return (dd or data_dir()) / STORE_NAME


def legacy_path(dd: Optional[Path] = None) -> Path:
    return (dd or data_dir()) / LEGACY_NAME


def _runtime_dir() -> Path:
    return _platform.paths().runtime_dir()


def status_file() -> Path:
    d = _runtime_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "screentime-security.json"


def write_status(state: str, detail: str = ""):
    """Tell the GUI what the daemon is doing about the key (ok / waiting /
    key-missing / wrong-key). Runtime-dir only: not persisted, not sensitive."""
    try:
        tmp = status_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"state": state, "detail": detail, "since": int(time.time())}))
        os.replace(tmp, status_file())
    except OSError:
        pass


def read_status() -> dict:
    try:
        return json.loads(status_file().read_text())
    except (OSError, ValueError):
        return {}


def _stop_if_requested(should_stop):
    if should_stop is not None and should_stop():
        raise SystemExit(0)


def _interruptible_sleep(sleep, seconds: float, should_stop):
    """Sleep in short slices so a stop request is honored within ~0.2s instead of
    after the whole poll interval (the signal handler only sets a flag)."""
    remaining = seconds
    while remaining > 0:
        _stop_if_requested(should_stop)
        step = min(0.2, remaining)
        sleep(step)
        remaining -= step
    _stop_if_requested(should_stop)


@contextmanager
def _exclusive(dd: Path, timeout: float = 60.0, should_stop=None):
    """Serialize create/migrate across processes (two starters must not both migrate)."""
    fd = os.open(dd / ".store.lock", os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if _filelock.try_lock(fd):
                break
            _stop_if_requested(should_stop)
            if time.monotonic() > deadline:
                raise StorageError("another ScreenTime process is setting up the database")
            time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def read_store_id(path: Path) -> bytes:
    """The store id is plaintext (it is how we find the key). No key needed."""
    try:
        con = sqlite3.connect(str(path))
        try:
            row = con.execute("SELECT v FROM meta WHERE k = 'store_id'").fetchone()
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        raise StoreFormatError(f"{path} is not a ScreenTime database: {e}") from e
    if row is None:
        raise StoreFormatError(f"{path} has no store id")
    return bytes(row[0])


# ------------------------------------------------------------------- open
def open_database(*, recover_orphans: bool = True, interactive: bool = False, wait_for_key: bool = False,
                  data_dir_: Optional[Path] = None, key_manager: Optional[KeyManager] = None,
                  poll_seconds: float = 5.0, sleep=time.sleep, should_stop=None) -> Database:
    """Open (creating or migrating first if needed) the encrypted database.

    should_stop  : optional callable; checked while waiting for the key so a stop
                   request can never be missed (see daemon.main).
    interactive  : may show the keyring's unlock/create dialog (GUI, CLI). The
                   daemon passes False and never shows a dialog.
    wait_for_key : if the key is in a keyring that is merely locked/starting,
                   keep retrying instead of failing (the daemon). Interruptible
                   by SIGTERM.
    Raises KeyLostError / WrongKeyError / KeyUnavailableError / MigrationError.
    """
    dd = Path(data_dir_) if data_dir_ else data_dir()
    dd.mkdir(parents=True, exist_ok=True)
    km = key_manager or KeyManager()
    sp, lp = store_path(dd), legacy_path(dd)

    log_: Optional[SecureLog] = None
    with _exclusive(dd, should_stop=should_stop):
        if sp.exists() and sp.stat().st_size == 0:
            sp.unlink()                      # an empty leftover holds no data; creation never finished
        if not sp.exists():
            log_ = _migrate_legacy(dd, km, interactive) if lp.exists() else _create_new(dd, km, interactive)
    if log_ is None:
        _remove_leftover_legacy(dd)
        log_ = _open_existing(sp, km, interactive, wait_for_key, poll_seconds, sleep, should_stop)
    else:
        _remove_leftover_legacy(dd)

    db = Database(store=ProtectedStore(log_), recover_orphans=recover_orphans)
    write_status("ok")
    return db


def _open_existing(sp: Path, km: KeyManager, interactive: bool, wait: bool, poll: float, sleep,
                   should_stop=None) -> SecureLog:
    store_id = read_store_id(sp)
    first_wait = None
    last_reminder = 0.0
    while True:
        try:
            key = km.get_key(store_id, interactive=interactive)
            if first_wait is not None:
                log.info("the keyring is available again after %.0fs; resuming", time.monotonic() - first_wait)
            break
        except KeyUnavailableError as e:
            write_status("waiting", e.reason)
            if not (wait and e.retryable):
                raise
            now = time.monotonic()
            if first_wait is None:                          # log once, then a reminder every 5 minutes
                first_wait = last_reminder = now
                log.warning("waiting for the keyring (%s); tracking is paused until it is available", e.reason)
            elif now - last_reminder >= 300:
                last_reminder = now
                log.warning("still waiting for the keyring (%.0f min): %s", (now - first_wait) / 60, e.reason)
            _interruptible_sleep(sleep, poll, should_stop)
        except KeyNotFoundError as e:
            write_status("key-missing", str(e))
            raise KeyLostError(
                "The database is encrypted but its key was not found (keyring entry / key file missing). "
                "Restore it with your recovery key: screentime-security import-recovery-key") from e
    try:
        return SecureLog(sp, key)
    except WrongKeyError:
        write_status("wrong-key", "the stored key does not open the database")
        raise


def _dir_fsync_supported() -> bool:
    """Can this OS fsync a directory? POSIX yes. Windows no: os.open() maps to the C runtime's _wopen, which calls
    CreateFile without FILE_FLAG_BACKUP_SEMANTICS, and Windows refuses to hand out a directory handle without that
    flag -- PermissionError(13) -- so there is nothing to fsync (NTFS journals the rename itself)."""
    return not _platform.is_windows()


def _fsync_dir(path: Path) -> None:
    """Make a rename inside `path` durable (POSIX: fsync the directory after os.replace()).

    Only "this OS has no directory fsync" is skipped. Every other failure -- EIO, or a PermissionError on a platform
    where opening a directory is supposed to work -- propagates like any other step of creating the store."""
    if not _dir_fsync_supported():
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_new(dd: Path, km: KeyManager, interactive: bool) -> SecureLog:
    """Create the store atomically: build it under a temporary name and swap it
    in only when complete, so being stopped mid-creation can never leave a
    half-built `screentime.sec` that later refuses to open."""
    sp = store_path(dd)
    tmp = dd / (STORE_NAME + ".new")
    for suffix in _SIDECARS:
        secure_delete_file(Path(str(tmp) + suffix))
    store_id = secrets.token_bytes(16)
    key, backend = km.create_key(store_id, interactive=interactive)
    try:
        SecureLog(tmp, key, create=True, store_id=store_id).close()
        os.replace(tmp, sp)
        _fsync_dir(dd)
    except BaseException:
        for suffix in _SIDECARS:
            secure_delete_file(Path(str(tmp) + suffix))
        raise
    log.info("created a new protected database (key kept in: %s)", backend)
    return SecureLog(sp, key)


# ------------------------------------------------------------------ migration
def _remove_legacy_files(dd: Path) -> bool:
    ok = True
    for suffix in _SIDECARS:
        p = Path(str(legacy_path(dd)) + suffix)
        if p.exists():
            ok = secure_delete_file(p) and ok
    return ok


def _remove_leftover_legacy(dd: Path):
    """If a previous run crashed between "new store in place" and "plaintext
    deleted", finish the job -- but only if the plaintext file has not changed
    since the migration (if it has, something wrote to it afterwards: keep it
    and warn rather than destroy data)."""
    lp = legacy_path(dd)
    if not any(Path(str(lp) + s).exists() for s in _SIDECARS):
        return
    try:
        con = sqlite3.connect(str(store_path(dd)))
        row = con.execute("SELECT v FROM meta WHERE k = 'migrated_at'").fetchone()
        con.close()
    except sqlite3.DatabaseError:
        return
    if row is None:
        log.warning("a plaintext %s exists next to the protected database (not from a migration); left alone", LEGACY_NAME)
        return
    migrated_at = float(bytes(row[0]).decode())
    newest = max((Path(str(lp) + s).stat().st_mtime for s in _SIDECARS if Path(str(lp) + s).exists()), default=0)
    if newest <= migrated_at + 2:
        log.info("removing the plaintext database left over from an interrupted migration")
        _remove_legacy_files(dd)
    else:
        log.warning("a plaintext %s was modified after migration (an older ScreenTime still running?); "
                    "left in place -- see Settings -> Security", LEGACY_NAME)


def _migrate_legacy(dd: Path, km: KeyManager, interactive: bool) -> SecureLog:
    """plaintext screentime.db -> encrypted screentime.sec, verified, then the
    plaintext (and its -wal/-shm) removed. Any failure leaves the original intact."""
    lp, sp = legacy_path(dd), store_path(dd)
    tmp = dd / (STORE_NAME + ".tmp")
    for suffix in _SIDECARS:                                   # stale staging from a crashed attempt
        secure_delete_file(Path(str(tmp) + suffix))
    log.info("migrating the plaintext database to protected storage")

    try:
        src = sqlite3.connect(str(lp))
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("the existing database failed its integrity check; nothing was changed")
            image = src.serialize()                            # consistent image incl. any WAL content
            expected = table_digests(src)
        finally:
            src.close()

        store_id = secrets.token_bytes(16)
        key, backend = km.create_key(store_id, interactive=interactive)
        staged = SecureLog(tmp, key, create=True, store_id=store_id)
        staged.begin_write()
        staged.write_snapshot(0, image)
        staged.commit()
        staged.set_meta("migrated_at", repr(time.time()))
        staged.close()

        # Verify with a key fetched back *from the keystore*: proves the key can
        # really be retrieved later, not just that we still hold it in memory.
        key_again = km.get_key(store_id, interactive=interactive)
        check = ProtectedStore(SecureLog(tmp, key_again))
        try:
            got = check.table_digests()
            if check.conn.mem.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("the migrated copy failed its integrity check")
        finally:
            check.log.checkpoint()
            check.close()
        if got != expected:
            diff = sorted(t for t in set(got) | set(expected) if got.get(t) != expected.get(t))
            raise MigrationError(f"verification failed for table(s) {diff}; your original database was left untouched")

        os.replace(tmp, sp)                                    # atomic
        _fsync_dir(dd)
    except BaseException:
        for suffix in _SIDECARS:
            secure_delete_file(Path(str(tmp) + suffix))
        raise

    if not _remove_legacy_files(dd):
        log.warning("could not remove every plaintext file; see Settings -> Security")
    log.info("migration complete (key kept in: %s)", backend)
    return SecureLog(sp, key_again)


# --------------------------------------------------------------------- status
@dataclass
class SecurityStatus:
    protected: bool
    store_exists: bool
    backend: Optional[str]          # "secret-service" | "keyfile" | None
    key_id: Optional[str]
    store_bytes: int
    legacy_plaintext_present: bool
    state: str                      # ok | waiting | key-missing | wrong-key | unknown
    detail: str


def security_status(dd: Optional[Path] = None, km: Optional[KeyManager] = None) -> SecurityStatus:
    """What Settings -> Security shows. Reads no secrets and never prompts."""
    dd = Path(dd) if dd else data_dir()
    km = km or KeyManager()
    sp, lp = store_path(dd), legacy_path(dd)
    settings = km.read_settings()
    st = read_status()
    size = 0
    for s in ("", "-wal"):
        try:
            size += os.path.getsize(str(sp) + s)
        except OSError:
            pass
    return SecurityStatus(
        protected=sp.exists(), store_exists=sp.exists(), backend=settings.get("backend"),
        key_id=settings.get("key_id"), store_bytes=size,
        legacy_plaintext_present=any(Path(str(lp) + s).exists() for s in _SIDECARS),
        state=st.get("state", "unknown"), detail=st.get("detail", ""))


# -------------------------------------------------------------------- recovery
def restore_from_recovery_key(text: str, dd: Optional[Path] = None, km: Optional[KeyManager] = None,
                              interactive: bool = True) -> str:
    """Put a recovery key back into the keystore -- but only after proving it
    opens *this* database, so a typo or another machine's key can never replace
    a working key. Returns the backend name. Raises ValueError with a message
    fit for the user."""
    from .keystore import decode_recovery_key
    dd = Path(dd) if dd else data_dir()
    km = km or KeyManager()
    sp = store_path(dd)
    if not sp.exists():
        raise ValueError("there is no protected database on this machine to restore a key for")
    key = decode_recovery_key(text)                       # ValueError: typo / wrong length
    store_id = read_store_id(sp)
    try:
        SecureLog(sp, key).close()
    except WrongKeyError:
        raise ValueError("this recovery key does not belong to this database") from None
    backend = km.import_key(store_id, key, interactive=interactive)
    write_status("ok")
    return backend


def export_recovery_key(dd: Optional[Path] = None, km: Optional[KeyManager] = None,
                        interactive: bool = True) -> str:
    from .keystore import encode_recovery_key
    dd = Path(dd) if dd else data_dir()
    km = km or KeyManager()
    return encode_recovery_key(km.get_key(read_store_id(store_path(dd)), interactive=interactive))


def verify_store(dd: Optional[Path] = None, km: Optional[KeyManager] = None, interactive: bool = True) -> dict:
    """Authenticate every record in the store (decrypt + check each tag)."""
    dd = Path(dd) if dd else data_dir()
    km = km or KeyManager()
    sp = store_path(dd)
    key = km.get_key(read_store_id(sp), interactive=interactive)
    log_ = SecureLog(sp, key)
    try:
        return log_.verify_all()
    finally:
        log_.close()


def explain_open_error(error: BaseException) -> str:
    """Plain-language text for a failure to open the database (GUI + CLI)."""
    from .keystore import KeyNotFoundError, KeyUnavailableError
    from .secure_log import TamperError
    if isinstance(error, (KeyLostError, KeyNotFoundError)):
        return ("ScreenTime's database is encrypted, but its key could not be found on this computer "
                "(the keyring entry or key file is missing). If you saved a recovery key, enter it below "
                "to restore access. Without the key the data cannot be read.")
    if isinstance(error, WrongKeyError):
        return ("The key stored on this computer does not open the database. It may belong to a different "
                "database. Enter your recovery key below to restore the correct one.")
    if isinstance(error, KeyUnavailableError):
        return (f"The system keyring that holds the database key is locked or unavailable ({error.reason}). "
                "Unlock it (for example, log in to your wallet) and try again. Tracking resumes automatically "
                "once the keyring is available.")
    if isinstance(error, TamperError):
        return ("The database file failed its integrity check: it was modified or is corrupted. "
                "Nothing has been changed. Restoring a backup of screentime.sec is the safest fix.")
    if isinstance(error, MigrationError):
        return f"Upgrading to encrypted storage was stopped and your existing data was left untouched: {error}"
    return f"ScreenTime could not open its database: {error}"
