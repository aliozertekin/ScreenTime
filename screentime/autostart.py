"""Enable/disable autostart of the tracking daemon with the graphical
session. We autostart the *daemon*, not the GUI -- tracking must run whether
or not anyone opens the window.

Mechanism choice (this is what makes tracking survive a reboot):

* systemd user unit -- preferred, because it restarts the daemon on a crash.
  But the unit is `WantedBy=graphical-session.target`, and that target is only
  reached by sessions whose desktop manages it through systemd (GNOME, Plasma
  with systemd startup, uwsm, ...). On a session that doesn't (legacy Plasma
  startup, plain sway/Hyprland) an enabled unit silently never starts. So we
  only choose systemd when `graphical-session.target` is active *right now*
  (proof this session type reaches it); otherwise we use an XDG autostart
  entry, which every mainstream desktop honours.
* The unit's `ExecStart` must point at a binary that exists. The packaged unit
  says /usr/bin/screentime-daemon, but `pip install --user` puts the script in
  ~/.local/bin, which made the unit fail at every login. When the packaged
  binary is absent we write a per-user unit with the real absolute path.
* Only one mechanism is ever left active (avoids two launchers), and the
  daemon's own single-instance lock (instance_lock.py) makes any residual
  double-launch harmless.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import instance_lock

UNIT_NAME = "screentime-daemon.service"
PACKAGED_EXE = "/usr/bin/screentime-daemon"
SYSTEM_UNIT_DIRS = ("/usr/lib/systemd/user", "/etc/systemd/user", "/usr/local/lib/systemd/user")

UNIT_TEMPLATE = """[Unit]
Description=ScreenTime application usage tracking daemon
Documentation=https://github.com/screentime/screentime
# Only makes sense once there's a graphical session to read window focus from.
After=graphical-session.target
PartOf=graphical-session.target
# Never give up restarting: a start-rate limit would otherwise leave tracking
# permanently off after a few early-boot failures.
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={exec_start}
# Restart after a crash. A clean exit (SIGTERM at logout, or "another daemon
# already holds the lock") is deliberately not restarted.
Restart=on-failure
RestartSec=3
# 78 = EX_CONFIG: the protected database can't be opened (lost/mismatched key).
# Restarting can't fix that, so don't loop; the GUI explains and offers recovery.
RestartPreventExitStatus=78
TimeoutStopSec=10
# Keep resource usage negligible.
MemoryHigh=64M
CPUWeight=10
# Session environment (WAYLAND_DISPLAY, DISPLAY, XDG_CURRENT_DESKTOP, ...) is
# inherited from the user manager. If it isn't ready yet at start, the daemon
# re-runs backend detection until one is available (see daemon.py), so no
# ordering hacks are needed here.

[Install]
WantedBy=graphical-session.target
"""

DESKTOP_TEMPLATE = """[Desktop Entry]
Type=Application
Name=ScreenTime Tracking Daemon
Comment=Background usage tracker (starts silently)
Exec={exec_line}
Icon=screentime
NoDisplay=true
X-GNOME-Autostart-enabled=true
Terminal=false
"""


# ------------------------------------------------------------------ paths
def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def autostart_dir() -> Path:
    return _config_home() / "autostart"


def desktop_file() -> Path:
    return autostart_dir() / "screentime-daemon.desktop"


def user_unit_dir() -> Path:
    return _config_home() / "systemd" / "user"


def user_unit_file() -> Path:
    return user_unit_dir() / UNIT_NAME


def render_unit(exec_start: str) -> str:
    return UNIT_TEMPLATE.format(exec_start=exec_start)


def _quote_exec_arg(arg: str) -> str:
    """Quote one argument per the Desktop Entry spec's Exec rules."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def daemon_command() -> list[str]:
    """Absolute command that launches the daemon, wherever it was installed.
    Autostart environments don't reliably have ~/.local/bin on PATH, so a bare
    `screentime-daemon` is not good enough."""
    exe = shutil.which("screentime-daemon")
    if exe:
        return [os.path.abspath(exe)]
    local = Path.home() / ".local" / "bin" / "screentime-daemon"
    if local.exists():
        return [str(local)]
    return [sys.executable, "-m", "screentime.daemon"]


def _systemd_available() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)


# ------------------------------------------------------------- unit files
def ensure_user_unit() -> Optional[Path]:
    """Make sure a unit with a *valid* ExecStart is visible to `systemctl
    --user`. Returns the per-user unit path if one was written."""
    if Path(PACKAGED_EXE).exists():
        return None  # packaged unit's /usr/bin path is valid
    cmd = " ".join(_quote_exec_arg(a) for a in daemon_command())
    text = render_unit(cmd)
    path = user_unit_file()
    if path.exists() and path.read_text() == text:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def unit_installed() -> bool:
    if user_unit_file().exists():
        return True
    return any((Path(d) / UNIT_NAME).exists() for d in SYSTEM_UNIT_DIRS)


def _graphical_session_managed() -> bool:
    """True if this login session reaches graphical-session.target, i.e. an
    enabled `WantedBy=graphical-session.target` unit will start next login."""
    try:
        return _systemctl("is-active", "graphical-session.target").stdout.strip() == "active"
    except Exception:
        return False


# ------------------------------------------------------------ enable/disable
def _write_xdg_entry():
    autostart_dir().mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(_quote_exec_arg(a) for a in daemon_command())
    desktop_file().write_text(DESKTOP_TEMPLATE.format(exec_line=exec_line))


def enable(prefer_systemd: bool = True) -> str:
    """Configure start-at-login and make sure the daemon is running now.
    Idempotent. Returns "systemd" or "xdg-autostart"."""
    mechanism = "xdg-autostart"
    if prefer_systemd and _systemd_available():
        try:
            ensure_user_unit()
            _systemctl("daemon-reload")
            if _graphical_session_managed():
                _systemctl("enable", UNIT_NAME).check_returncode()
                mechanism = "systemd"
            else:
                # Enabling here would be silently inert at next login.
                _systemctl("disable", UNIT_NAME)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            mechanism = "xdg-autostart"
    if mechanism == "systemd":
        if desktop_file().exists():
            desktop_file().unlink()  # never leave two launchers behind
    else:
        _write_xdg_entry()
    start_now()  # no-op if already running
    return mechanism


def disable():
    if _systemd_available():
        try:
            _systemctl("disable", "--now", UNIT_NAME)
        except Exception:
            pass
    if desktop_file().exists():
        desktop_file().unlink()
    # A daemon started directly (XDG entry / Settings button) isn't managed by
    # the unit, so `disable --now` doesn't stop it.
    if instance_lock.is_held():
        stop_now()


def is_enabled() -> bool:
    if _systemd_available():
        try:
            out = _systemctl("is-enabled", UNIT_NAME)
            if out.stdout.strip() == "enabled":
                return True
        except Exception:
            pass
    return desktop_file().exists()


def _is_daemon_cmdline(argv: list[str]) -> bool:
    """Match the daemon by what it executes, not by any process whose command
    line merely mentions the name (an editor with the log open, a shell...)."""
    if not argv:
        return False
    if os.path.basename(argv[0]) == "screentime-daemon":
        return True
    # `python /path/to/screentime-daemon` or `python -m screentime.daemon`
    if os.path.basename(argv[0]).startswith("python") and len(argv) > 1:
        if os.path.basename(argv[1]) == "screentime-daemon":
            return True
        if argv[1] == "-m" and len(argv) > 2 and argv[2] == "screentime.daemon":
            return True
    return False


def _find_daemon_processes() -> list:
    """Every process on the machine that is a screentime daemon. This process
    scan exists for one reason: a daemon from before the single-instance lock
    existed (i.e. still running right after upgrading) holds no lock, so the
    lock probe alone can't see it. Kept as a separate function so tests can
    make it hermetic -- otherwise a developer's *real* running daemon would
    change the outcome of unit tests."""
    try:
        import psutil
        return [p for p in psutil.process_iter(attrs=["pid", "name", "cmdline"])
                if _is_daemon_cmdline(p.info.get("cmdline") or [])]
    except Exception:
        return []


def daemon_is_running() -> bool:
    # The single-instance lock is authoritative regardless of who launched it.
    if instance_lock.is_held():
        return True
    if _systemd_available():
        try:
            if _systemctl("is-active", UNIT_NAME).stdout.strip() == "active":
                return True
        except Exception:
            pass
    # Not the unit and not holding the lock: could still be a daemon launched
    # by hand / by an XDG entry / from before the lock existed. Starting the
    # unit on top of it would double-track, so look at the process list too.
    return bool(_find_daemon_processes())


def start_now() -> bool:
    """Starts the daemon for the current session only (persisting across
    reboots is enable()'s job). Idempotent: returns True without launching
    anything if a daemon is already running."""
    if daemon_is_running():
        return True
    if _systemd_available():
        try:
            ensure_user_unit()
            _systemctl("daemon-reload")
            _systemctl("start", UNIT_NAME).check_returncode()
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
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
            _systemctl("stop", UNIT_NAME).check_returncode()
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
    stopped = False
    for p in _find_daemon_processes():
        try:
            p.terminate()
            stopped = True
        except Exception:
            pass
    return stopped


def _version_tuple(v: Optional[str]) -> tuple:
    try:
        return tuple(int(x) for x in (v or "").split("."))
    except ValueError:
        return ()


def daemon_is_outdated(recorded_version: Optional[str], current_version: str) -> bool:
    """True if the running daemon is older than this code. `recorded_version` is
    what the daemon wrote to the settings table at startup; a daemon that never
    wrote one predates the check and is therefore older. A *newer* daemon than
    the caller (an old GUI window left open across an upgrade) is not outdated."""
    if not recorded_version:
        return True
    rec, cur = _version_tuple(recorded_version), _version_tuple(current_version)
    if not rec or not cur:
        return False
    return rec < cur


def restart_now(wait_seconds: float = 8.0) -> bool:
    """Restart a daemon that is *already running* (never starts one that isn't,
    so a user who stopped tracking on purpose isn't overridden). A package
    upgrade doesn't restart per-user services, so an upgraded install would
    otherwise keep executing the old code until the next login."""
    if not daemon_is_running():
        return False
    if _systemd_available():
        try:
            if _systemctl("is-active", UNIT_NAME).stdout.strip() == "active":
                _systemctl("restart", UNIT_NAME).check_returncode()
                return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
    # Started outside the unit (XDG entry / Settings button): stop it, wait for
    # its lock to clear, then start a fresh one.
    if not stop_now():
        return False
    deadline = time.monotonic() + wait_seconds
    while instance_lock.is_held() and time.monotonic() < deadline:
        time.sleep(0.1)
    return start_now()


def refresh_if_outdated(recorded_version: Optional[str], current_version: str) -> bool:
    """Restart the running daemon if it is older than the installed code."""
    if daemon_is_running() and daemon_is_outdated(recorded_version, current_version):
        return restart_now()
    return False


def reconcile(autostart_wanted: bool) -> Optional[str]:
    """Self-heal, called when the GUI opens. If the user opted in (the
    `autostart_enabled` setting) but the mechanism has gone missing -- a
    package upgrade replaced the unit, the config dir was restored from
    backup -- re-establish it, and start the daemon if it isn't running.
    Never launches a second daemon and never *disables* anything: a user who
    enabled the unit by hand with systemctl isn't overridden."""
    if not autostart_wanted:
        return None
    if not is_enabled():
        return enable()
    if not daemon_is_running():
        start_now()
    return "already-enabled"


# --------------------------------------------------------------- diagnostics
@dataclass
class DaemonStatus:
    installed: bool
    enabled: bool
    mechanism: Optional[str]      # "systemd" | "xdg-autostart" | None
    running: bool
    last_start: Optional[int]     # unix time, as recorded by the daemon itself
    window_backend: str
    idle_backend: str


def get_status(db=None) -> DaemonStatus:
    """Everything Settings -> Diagnostics shows about startup. Backend/last
    start come from what the *daemon* recorded in the settings table; this
    never instantiates a detector (see the KWin D-Bus ownership note in
    the README)."""
    mechanism = None
    if desktop_file().exists():
        mechanism = "xdg-autostart"
    if _systemd_available():
        try:
            if _systemctl("is-enabled", UNIT_NAME).stdout.strip() == "enabled":
                mechanism = "systemd"
        except Exception:
            pass
    last_start = None
    window_backend = idle_backend = ""
    if db is not None:
        raw = db.get_setting("daemon_last_start")
        last_start = int(raw) if raw and raw.isdigit() else None
        window_backend = db.get_setting("active_window_backend") or ""
        idle_backend = db.get_setting("active_idle_backend") or ""
    return DaemonStatus(
        installed=unit_installed() or shutil.which("screentime-daemon") is not None,
        enabled=mechanism is not None,
        mechanism=mechanism,
        running=daemon_is_running(),
        last_start=last_start,
        window_backend=window_backend,
        idle_backend=idle_backend,
    )
