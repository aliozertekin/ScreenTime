"""`python -m screentime.diagnostics` (or `screentime-diagnose`): a text report
of how this install is wired up, for Windows and Linux alike.

Safe by construction:
  * it reads the settings the DAEMON recorded (backends, versions, last
    suspend/resume) and the non-secret security metadata -- it never creates a
    window detector, idle detector or daemon lock, so it can run while the
    daemon is tracking without disturbing it;
  * it never opens the key store, never prints a key, recovery key,
    credential, or any ciphertext;
  * the encrypted store is not opened at all (no key needed): only its path and
    size are reported.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from . import platform as _platform

WARNING = (
    "NOTE: this report may include the names of applications you've used and\n"
    "timestamps (for example, when the daemon last started or the PC last\n"
    "slept). Skim it before sharing if that matters to you. It does not include\n"
    "window titles, file contents, encryption keys, recovery keys or credentials."
)

_BACKEND_LABELS = {
    "windows": "Windows Win32 (GetForegroundWindow)",
}


def _ts(raw: Optional[str]) -> str:
    if raw and raw.isdigit():
        return datetime.datetime.fromtimestamp(int(raw)).strftime("%Y-%m-%d %H:%M:%S")
    return "never"


def _read_settings_sqlite_free() -> dict:
    """The daemon's recorded state lives in the encrypted store; reading it
    needs the key. Diagnostics therefore uses the *status the GUI/daemon already
    expose* when the store can be opened non-interactively, and otherwise says so."""
    from . import storage
    if not storage.store_path().exists():
        return {"_error": "no store yet (ScreenTime has not run on this account)"}    # never create one here
    try:
        db = storage.open_database(recover_orphans=False, interactive=False)
    except Exception as e:                        # noqa: BLE001 - report, don't crash a diagnostics tool
        return {"_error": f"{type(e).__name__}: {e}"}
    try:
        keys = ("daemon_version", "daemon_last_start", "active_window_backend", "active_idle_backend",
                "active_power_backend", "last_suspend", "last_resume", "autostart_enabled")
        return {k: db.get_setting(k) or "" for k in keys}
    finally:
        db.close()


def collect() -> list[tuple[str, str]]:
    from . import autostart, storage, steam_library
    from .keystore import KeyManager
    rows: list[tuple[str, str]] = []
    add = lambda k, v: rows.append((k, str(v)))   # noqa: E731
    plat = _platform.current()
    add("ScreenTime version", __version__)
    add("OS", "Windows" if plat.is_windows else "Linux")
    add("Python", sys.version.split()[0])

    st = autostart.get_status(None)
    add("Daemon installed", "yes" if st.installed else "no")
    add("Daemon running", "yes" if st.running else "no")
    add("Autostart", "enabled" if st.enabled else "disabled")
    mech = {"task-scheduler": "Task Scheduler", "systemd": "systemd user service",
            "xdg-autostart": "XDG autostart"}.get(st.mechanism or "", st.mechanism or "none")
    add("Autostart mechanism", mech)
    if hasattr(st, "task_state"):
        add("Startup task state", st.task_state)

    rec = _read_settings_sqlite_free()
    if "_error" in rec:
        add("Recorded daemon state", f"unavailable ({rec['_error']})")
    else:
        add("Daemon version", rec["daemon_version"] or "not recorded")
        wb, ib, pb = rec["active_window_backend"], rec["active_idle_backend"], rec["active_power_backend"]
        add("Foreground detector", _BACKEND_LABELS.get(wb, wb) if wb else "unknown (daemon not running?)")
        add("Idle detector", ("Windows GetLastInputInfo" if (ib == "windows") else ib) if ib else "unknown")
        add("Power-event backend", ("Windows" if pb == "windows" else pb) if pb else "unknown")
        add("Last suspend", _ts(rec["last_suspend"]))
        add("Last resume", _ts(rec["last_resume"]))
        add("Daemon last started", _ts(rec["daemon_last_start"]))

    km = KeyManager()
    sec = storage.security_status(km=km)
    backend = {"credential-manager": "Windows Credential Manager", "dpapi-file": "DPAPI-protected key file",
               "secret-service": "Secret Service / KWallet", "keyfile": "key file"}.get(sec.backend or "", "unknown")
    state = ("protected" if sec.protected else "store not created yet")
    if sec.state in ("waiting", "key-missing", "wrong-key"):
        state = f"unavailable ({sec.state})"
    add("Secure store", state)
    add("Key storage backend", backend)
    add("Store path", storage.store_path())
    add("Store size on disk", f"{sec.store_bytes / 1024:.0f} KiB")
    resolver = steam_library.default_resolver()
    libs = resolver.libraries()
    add("Steam resolver", f"{len(libs)} librar{'y' if len(libs) == 1 else 'ies'} found (offline)" if libs
        else "no Steam installation found (offline)")
    return rows


def render(rows: list[tuple[str, str]]) -> str:
    width = max(len(k) for k, _ in rows) + 2
    return "\n".join(f"{k + ':':<{width}}{v}" for k, v in rows) + "\n\n" + WARNING + "\n"


def main(argv=None) -> int:
    print(render(collect()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
