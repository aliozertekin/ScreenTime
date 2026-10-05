"""`screentime-security`: inspect and recover the protected database.

    screentime-security status                 what is protected and where the key lives
    screentime-security verify                 authenticate every stored record
    screentime-security export-recovery-key    print the recovery key (keep it somewhere safe)
    screentime-security import-recovery-key    restore a lost key from a recovery key
    screentime-security move-key-to-keyring    move the key from a key file into the keyring
    screentime-security compact                fold the log into an encrypted snapshot
    screentime-security backup [FILE]          write an encrypted backup (.screentime)
    screentime-security verify-backup FILE     check a backup end to end; changes nothing
    screentime-security restore-backup FILE    add the history in a backup that is missing here

Everything is local: no network access of any kind.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import Optional

from . import storage
from .keystore import KeyManager, KeyStoreError
from .secure_log import SecureStoreError


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def cmd_status(args, km) -> int:
    st = storage.security_status(km=km)
    where = {"secret-service": "system keyring (Secret Service / KWallet)",
             "keyfile": "key file (less secure than the keyring)"}.get(st.backend or "", "unknown")
    print(f"Protected database : {'yes (AES-256-GCM)' if st.protected else 'NO (not created yet)'}")
    print(f"Store              : {storage.store_path()}  ({_fmt_bytes(st.store_bytes)})")
    print(f"Key stored in      : {where}")
    print(f"Key id             : {st.key_id or 'unknown'}")
    print(f"Daemon key state   : {st.state}{' - ' + st.detail if st.detail else ''}")
    if st.legacy_plaintext_present:
        print("WARNING            : a plaintext screentime.db is still present next to the protected store")
    return 0


def cmd_verify(args, km) -> int:
    try:
        res = storage.verify_store(km=km)
    except (SecureStoreError, KeyStoreError, storage.StorageError) as e:
        print(f"FAILED: {storage.explain_open_error(e)}", file=sys.stderr)
        return 1
    print(f"OK: snapshot {res['snapshot_bytes']} bytes and {res['events']} records authenticated")
    return 0


def cmd_export(args, km) -> int:
    key = storage.export_recovery_key(km=km)
    print("Recovery key (anyone with this can read your ScreenTime data; store it safely, offline):\n")
    print(f"    {key}\n")
    return 0


def cmd_import(args, km) -> int:
    text = args.key or getpass.getpass("Recovery key: ")
    try:
        backend = storage.restore_from_recovery_key(text, km=km)
    except (ValueError, SecureStoreError, KeyStoreError, storage.StorageError) as e:
        print(f"Not restored: {e}", file=sys.stderr)
        return 1
    print(f"Key restored into: {backend}")
    return 0


def cmd_move(args, km) -> int:
    from .secure_log import SecureLog
    store_id = storage.read_store_id(storage.store_path())
    try:
        backend = km.move_to_keyring(store_id)
    except (KeyStoreError, SecureStoreError) as e:
        print(f"Not moved: {e}", file=sys.stderr)
        return 1
    print(f"Key is now kept in: {backend} (the key file was securely removed)")
    return 0


def cmd_compact(args, km) -> int:
    db = storage.open_database(recover_orphans=False, interactive=True, key_manager=km)
    try:
        print("compacted" if db._store.compact(force=True) else "nothing to do")
    finally:
        db.close()
    return 0


def _backup_errors():
    from . import backup
    return (backup.BackupError, SecureStoreError, KeyStoreError, storage.StorageError, OSError)


def cmd_backup(args, km) -> int:
    from . import backup
    dest = args.file or backup.default_filename()
    db = storage.open_database(recover_orphans=False, interactive=True, key_manager=km)
    try:
        info = backup.create_backup(db, dest, km=km, interactive=True, overwrite=args.force)
    except FileExistsError:
        print(f"Not written: {dest} already exists (use --force to replace it)", file=sys.stderr)
        return 1
    except _backup_errors() as e:
        print(f"No backup was created: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    print(f"Backup written and verified: {dest}  ({info.sessions} sessions, {info.apps} applications)")
    print("It is encrypted with your database key; keep your recovery key safe, it is needed to open it elsewhere.")
    return 0


def _recovery_text(args) -> Optional[str]:
    return args.recovery_key


def cmd_verify_backup(args, km) -> int:
    from . import backup
    try:
        info = backup.verify_backup(args.file, km=km, recovery_key=_recovery_text(args), interactive=True)
    except _backup_errors() as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    span = f"{info.first_day} .. {info.last_day}" if info.first_day else "no sessions"
    print(f"OK: backup format {info.header.version}, made by ScreenTime {info.header.app_version}; "
          f"{info.sessions} sessions, {info.apps} applications, {span}")
    return 0


def cmd_restore_backup(args, km) -> int:
    from . import backup
    try:
        backup.verify_backup(args.file, km=km, recovery_key=_recovery_text(args), interactive=True)
    except _backup_errors() as e:
        print(f"Nothing was restored: {e}", file=sys.stderr)
        return 1
    db = storage.open_database(recover_orphans=False, interactive=True, key_manager=km)
    try:
        res = backup.restore_backup(db, args.file, km=km, recovery_key=_recovery_text(args), interactive=True)
    except _backup_errors() as e:
        print(f"Nothing was restored: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    print(f"Added {res.sessions_added} sessions and {res.apps_added} applications; "
          f"{res.sessions_skipped} sessions were already present. Existing history was not changed.")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="screentime-security", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("verify")
    sub.add_parser("export-recovery-key")
    imp = sub.add_parser("import-recovery-key")
    imp.add_argument("key", nargs="?", help="omit to be prompted (keeps it out of shell history)")
    sub.add_parser("move-key-to-keyring")
    sub.add_parser("compact")
    bk = sub.add_parser("backup")
    bk.add_argument("file", nargs="?", help="default: ScreenTime-backup-<date>.screentime in the current folder")
    bk.add_argument("--force", action="store_true", help="replace the file if it exists")
    for name in ("verify-backup", "restore-backup"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--recovery-key", help="needed only for a backup made by another installation")
    args = parser.parse_args(argv)
    km = KeyManager()
    handlers = {"status": cmd_status, "verify": cmd_verify, "export-recovery-key": cmd_export,
                "import-recovery-key": cmd_import, "move-key-to-keyring": cmd_move, "compact": cmd_compact,
                "backup": cmd_backup, "verify-backup": cmd_verify_backup, "restore-backup": cmd_restore_backup}
    try:
        return handlers[args.cmd](args, km)
    except (SecureStoreError, KeyStoreError, storage.StorageError) as e:
        print(storage.explain_open_error(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
