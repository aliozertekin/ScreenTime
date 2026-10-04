"""Uninstall helper, run by the installer:

    python -m screentime.platform.windows.cleanup uninstall [--delete-data]

Always (program removal):
  * ask the daemon to stop gracefully (closing its open session), terminating
    it only if it does not exit in time;
  * delete the Task Scheduler logon task.

Only with --delete-data (the user explicitly chose it):
  * delete the encrypted store and its sidecars, the legacy plaintext files if
    any, logs and the status file;
  * delete the database key from Credential Manager and the DPAPI key file.
    Without the key the data is unrecoverable by anyone, including from backups.

Usage data is NEVER deleted by default: a plain uninstall/upgrade keeps it.
Returns a process exit code of 0 even if individual steps fail (an uninstaller
that errors out is worse than one that leaves a stale file); failures are
printed.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

from . import autostart as wa
from .keystore import CredentialManagerBackend, DpapiFileBackend


def remove_program_registration(say: Callable[[str], None] = print) -> None:
    try:
        wa.stop_now()
    except Exception as e:                      # noqa: BLE001 - uninstall must keep going
        say(f"could not stop the daemon cleanly: {e}")
    try:
        wa.disable()
    except Exception as e:                      # noqa: BLE001
        say(f"could not remove the startup task: {e}")


def delete_user_data(say: Callable[[str], None] = print, api=None) -> None:
    from ... import storage
    from ...keystore import KeyManager
    dd = storage.data_dir()
    sp = storage.store_path(dd)
    try:
        store_id = storage.read_store_id(sp) if sp.exists() else None
    except Exception as e:                      # noqa: BLE001 - unreadable header: still delete the files
        store_id = None
        say(f"store id unreadable ({type(e).__name__}); key entries may need manual removal")
    km = KeyManager()
    if store_id is not None:
        for backend in (CredentialManagerBackend(api), DpapiFileBackend(km.config_dir, api)):
            try:
                backend.delete(store_id)
            except Exception as e:              # noqa: BLE001
                say(f"could not remove {backend.name} key: {e}")
    for d in {dd, dd / "run", dd / "logs"}:
        for item in (d.iterdir() if d.is_dir() else []):
            try:
                item.unlink() if item.is_file() else shutil.rmtree(item)
            except OSError as e:
                say(f"could not remove {item.name}: {e}")
    for p in (km.settings_path,):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            say(f"could not remove {p.name}: {e}")
    for d in (km.config_dir / "keys", km.config_dir, dd / "run", dd / "logs", dd):
        try:
            d.rmdir()
        except OSError:
            pass                                 # not empty / not there: fine


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="screentime-cleanup")
    ap.add_argument("action", choices=["uninstall"])
    ap.add_argument("--delete-data", action="store_true",
                    help="also delete usage history and its encryption key (irreversible)")
    args = ap.parse_args(argv)
    remove_program_registration()
    if args.delete_data:
        delete_user_data()
    return 0


if __name__ == "__main__":
    sys.exit(main())
