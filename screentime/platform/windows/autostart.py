"""Start-at-login for the tracking daemon on Windows, via Task Scheduler.

One per-user task, `\\ScreenTime\\Daemon`, with a logon trigger for THIS user,
run at the limited (non-elevated) level with the interactive token, so enabling
it never needs administrator rights. Settings are chosen so the daemon is not
killed on battery, is not started twice (IgnoreNew), has no execution time
limit, and restarts itself after a crash (the Windows analogue of
`Restart=on-failure`). The daemon, not the GUI, is what starts.

The same semantic contract as the Linux module (enable/disable/is_enabled/
start_now/stop_now/reconcile/get_status/...): `reconcile` only repairs when the
user opted in, repairs a task whose command path went stale after an upgrade or
move, and never re-enables a startup the user turned off. Only one task is ever
registered (the same name is overwritten, never duplicated).

All schtasks calls go through `_run`, which tests replace.
"""
from __future__ import annotations

import getpass
import logging
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape

from . import instance_lock as _lock

log = logging.getLogger("screentime.autostart.windows")

TASK_NAME = "\\ScreenTime\\Daemon"
MECHANISM = "task-scheduler"
_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the ScreenTime usage-tracking daemon when you sign in.</Description>
    <URI>{task_name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


# ------------------------------------------------------------------ plumbing
def _run(args: list[str], input_: Optional[str] = None, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input_,
                          creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)


def current_user() -> str:
    domain, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def _command_parts() -> tuple[str, list[str]]:
    """(executable, arguments) that launch the daemon -- the real installed
    path, never a bare name (Task Scheduler's PATH is not the user's)."""
    override = os.environ.get("SCREENTIME_DAEMON_EXE")
    if override:
        return override, ["-m", "screentime.daemon"]
    exe = Path(sys.executable)
    sibling = exe.with_name("screentime-daemon.exe")          # shipped launcher (a pythonw copy)
    if sibling.exists():
        return str(sibling), ["-m", "screentime.daemon"]
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe), ["-m", "screentime.daemon"]


def daemon_command() -> list[str]:
    """Same shape as the Linux module: the full argv that launches the daemon."""
    exe, args = _command_parts()
    return [exe, *args]


def render_task_xml(command: str, arguments: list[str], user: Optional[str] = None) -> str:
    return TASK_XML.format(
        task_name=escape(TASK_NAME), user=escape(user or current_user()), command=escape(command),
        arguments=escape(subprocess.list2cmdline(arguments)))      # list2cmdline quotes spaces correctly


@dataclass
class TaskInfo:
    exists: bool
    enabled: bool = False
    command: str = ""
    arguments: str = ""


def parse_task_xml(xml_text: str) -> TaskInfo:
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff").replace('encoding="UTF-16"', ""))
    except ET.ParseError:
        return TaskInfo(False)
    def txt(path):
        el = root.find(path, _NS)
        return (el.text or "").strip() if el is not None else ""
    enabled = txt("t:Settings/t:Enabled").lower() != "false"
    trig = root.find("t:Triggers/t:LogonTrigger/t:Enabled", _NS)
    if trig is not None and (trig.text or "").strip().lower() == "false":
        enabled = False
    return TaskInfo(True, enabled, txt("t:Actions/t:Exec/t:Command"), txt("t:Actions/t:Exec/t:Arguments"))


def query_task() -> TaskInfo:
    try:
        out = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("schtasks query failed: %s", e)
        return TaskInfo(False)
    if out.returncode != 0:
        return TaskInfo(False)
    return parse_task_xml(out.stdout)


def task_scheduler_available() -> bool:
    try:
        return _run(["schtasks", "/?"]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a.strip('"'))) == os.path.normcase(os.path.normpath(b.strip('"')))


def task_is_current(info: TaskInfo) -> bool:
    """True if the registered task launches the daemon we would launch today."""
    cmd, args = _command_parts()
    return info.exists and _same_path(info.command, cmd) and info.arguments == subprocess.list2cmdline(args)


# ----------------------------------------------------------- enable/disable
def enable(prefer_systemd: bool = True) -> str:
    """Register (or refresh) the logon task and make sure the daemon runs now.
    Idempotent. `prefer_systemd` exists only for signature parity."""
    cmd, args = _command_parts()
    xml = render_task_xml(cmd, args)
    xml_path = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / f"screentime-task-{os.getpid()}.xml"
    try:
        xml_path.write_text(xml, encoding="utf-16")               # schtasks wants UTF-16 for /XML
        out = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])
        if out.returncode != 0:
            raise RuntimeError(f"schtasks /Create failed ({out.returncode}): {(out.stderr or out.stdout).strip()[:300]}")
    finally:
        try:
            xml_path.unlink()
        except OSError:
            pass
    start_now()
    return MECHANISM


def disable():
    _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])      # absent task -> nonzero, which is fine
    if daemon_is_running():
        stop_now()


def is_enabled() -> bool:
    info = query_task()
    return info.exists and info.enabled


# ------------------------------------------------------------- run state
def _is_daemon_cmdline(argv: list[str]) -> bool:
    if not argv:
        return False
    base = os.path.basename(argv[0].replace("\\", "/")).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base == "screentime-daemon":
        return True
    if base.startswith("python") and len(argv) > 2 and argv[1] == "-m" and argv[2] == "screentime.daemon":
        return True
    return False


def _find_daemon_processes() -> list:
    try:
        import psutil
        return [p for p in psutil.process_iter(attrs=["pid", "name", "cmdline"])
                if _is_daemon_cmdline(p.info.get("cmdline") or [])]
    except Exception:                       # noqa: BLE001 - psutil can raise assorted OS errors; "none found" is safe
        return []


def daemon_is_running() -> bool:
    if _lock.is_held():
        return True
    return bool(_find_daemon_processes())


def start_now() -> bool:
    """Start the daemon for this session (detached, no console). Idempotent."""
    if daemon_is_running():
        return True
    cmd, args = _command_parts()
    if not Path(cmd).exists():
        return False
    try:
        subprocess.Popen([cmd, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
        return True
    except OSError as e:
        log.warning("could not start the daemon: %s", e)
        return False


def stop_now(wait_seconds: float = 8.0) -> bool:
    """Graceful stop: signal the daemon's stop event so it closes its session
    and exits by itself; only terminate if it does not do that in time."""
    if _lock.request_stop():
        deadline = time.monotonic() + wait_seconds
        while _lock.is_held() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not _lock.is_held():
            return True
    stopped = False
    for p in _find_daemon_processes():
        try:
            p.terminate()
            stopped = True
        except Exception:                   # noqa: BLE001
            pass
    return stopped


def restart_now(wait_seconds: float = 8.0) -> bool:
    if not daemon_is_running():
        return False
    if not stop_now(wait_seconds):
        return False
    deadline = time.monotonic() + wait_seconds
    while _lock.is_held() and time.monotonic() < deadline:
        time.sleep(0.1)
    return start_now()


def reconcile(autostart_wanted: bool) -> Optional[str]:
    """Self-heal on GUI open. Never acts if the user did not opt in; repairs a
    missing/disabled/stale task; never creates a second one."""
    if not autostart_wanted:
        return None
    info = query_task()
    if not (info.exists and info.enabled and task_is_current(info)):
        return enable()
    if not daemon_is_running():
        start_now()
    return "already-enabled"


@dataclass
class DaemonStatus:
    installed: bool
    enabled: bool
    mechanism: Optional[str]
    running: bool
    last_start: Optional[int]
    window_backend: str
    idle_backend: str
    task_state: str = "missing"        # enabled | disabled | missing | stale | unavailable


def get_status(db=None) -> DaemonStatus:
    info = query_task()
    if not info.exists:
        state = "missing" if task_scheduler_available() else "unavailable"
    elif not info.enabled:
        state = "disabled"
    elif not task_is_current(info):
        state = "stale"
    else:
        state = "enabled"
    last_start = None
    window_backend = idle_backend = ""
    if db is not None:
        raw = db.get_setting("daemon_last_start")
        last_start = int(raw) if raw and raw.isdigit() else None
        window_backend = db.get_setting("active_window_backend") or ""
        idle_backend = db.get_setting("active_idle_backend") or ""
    cmd, _ = _command_parts()
    return DaemonStatus(installed=Path(cmd).exists(), enabled=state in ("enabled", "stale"),
                        mechanism=MECHANISM if info.exists else None, running=daemon_is_running(),
                        last_start=last_start, window_backend=window_backend, idle_backend=idle_backend,
                        task_state=state)
