"""Enable/disable autostart of the tracking daemon with the graphical
session, following the freedesktop.org Desktop Application Autostart Spec
(~/.config/autostart/*.desktop). We autostart the *daemon*, not the GUI --
the GUI is opened on demand; tracking should always be running in the
background regardless of whether anyone opens the window.

We prefer the systemd user service (see data/screentime-daemon.service) when
systemd is available, since it gives us restart-on-crash and proper
integration with logind's session lifecycle; the XDG autostart entry is a
portable fallback for non-systemd setups.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

AUTOSTART_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "autostart"
DESKTOP_FILE = AUTOSTART_DIR / "screentime-daemon.desktop"

DESKTOP_CONTENT = """[Desktop Entry]
Type=Application
Name=ScreenTime Tracking Daemon
Comment=Background usage tracker (starts silently)
Exec=screentime-daemon
Icon=screentime
NoDisplay=true
X-GNOME-Autostart-enabled=true
Terminal=false
"""


def _systemd_available() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def enable(prefer_systemd: bool = True) -> str:
    """Returns a short string describing which mechanism was used."""
    if prefer_systemd and _systemd_available():
        try:
            subprocess.run(["systemctl", "--user", "enable", "--now", "screentime-daemon.service"],
                            check=True, capture_output=True, text=True)
            return "systemd"
        except subprocess.CalledProcessError:
            pass  # fall through to XDG autostart
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(DESKTOP_CONTENT)
    return "xdg-autostart"


def disable():
    if _systemd_available():
        subprocess.run(["systemctl", "--user", "disable", "--now", "screentime-daemon.service"],
                        capture_output=True, text=True)
    if DESKTOP_FILE.exists():
        DESKTOP_FILE.unlink()


def is_enabled() -> bool:
    if _systemd_available():
        try:
            out = subprocess.run(["systemctl", "--user", "is-enabled", "screentime-daemon.service"],
                                  capture_output=True, text=True)
            if out.stdout.strip() == "enabled":
                return True
        except Exception:
            pass
    return DESKTOP_FILE.exists()


def daemon_is_running() -> bool:
    if _systemd_available():
        try:
            out = subprocess.run(["systemctl", "--user", "is-active", "screentime-daemon.service"],
                                  capture_output=True, text=True)
            return out.stdout.strip() == "active"
        except Exception:
            pass
    try:
        import psutil
        for p in psutil.process_iter(attrs=["name", "cmdline"]):
            cmdline = " ".join(p.info.get("cmdline") or [])
            if "screentime.daemon" in cmdline or "screentime-daemon" in cmdline:
                return True
    except Exception:
        pass
    return False


def start_now() -> bool:
    """Starts the daemon for the current session only -- does not persist
    across reboots (that's what enable() is for). Used by the Settings ->
    Diagnostics "Start" button so tracking can begin immediately without
    also committing to autostart-at-login, which is a separate decision."""
    if _systemd_available():
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
            subprocess.run(["systemctl", "--user", "start", "screentime-daemon.service"],
                            check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError:
            pass  # fall through to a direct, detached spawn
    exe = shutil.which("screentime-daemon")
    if not exe:
        return False
    try:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def stop_now() -> bool:
    """Stops the daemon for the current session. Does not change whether it
    autostarts next login (use disable() for that)."""
    if _systemd_available():
        try:
            subprocess.run(["systemctl", "--user", "stop", "screentime-daemon.service"],
                            check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError:
            pass
    # Fallback for a daemon that was started directly (not via systemd),
    # e.g. by start_now()'s own fallback path above.
    try:
        import psutil
        stopped = False
        for p in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
            cmdline = " ".join(p.info.get("cmdline") or [])
            if "screentime.daemon" in cmdline or "screentime-daemon" in cmdline:
                p.terminate()
                stopped = True
        return stopped
    except Exception:
        return False
