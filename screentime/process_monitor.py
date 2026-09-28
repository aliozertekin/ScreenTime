"""
Enumerates currently-running user processes and groups them into distinct
applications. This is used for two things:
  1. The "Applications" view can show what's currently running even if it
     doesn't have focus (presence), independent of the focus-tracking session
     log (which is the source of truth for *usage time*).
  2. As a fallback identity source when the window layer only gives us a PID
     (some Wayland compositors expose focused PID but not app_id directly).

It deliberately does NOT drive time tracking by itself -- process lifetime is
not usage time (a process can sit in the background for hours untouched).
Only window-focus events (see window_detector.py) drive the session clock.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import psutil

from .app_identity import resolve, ResolvedApp

# Processes that are never meaningful "applications" to a human, regardless
# of desktop-file resolution. The user-editable exclusion list in the DB is
# layered on top of this baseline.
_NOISE_EXE_NAMES = {
    "systemd", "dbus-daemon", "pipewire", "pipewire-pulse", "wireplumber",
    "gnome-shell", "kwin_wayland", "kwin_x11", "plasmashell", "Xwayland", "Xorg",
    "sway", "hyprland", "gvfsd", "gvfs-udisks2-volume-monitor", "xdg-desktop-portal",
    "xdg-desktop-portal-gtk", "xdg-desktop-portal-hyprland", "xdg-desktop-portal-wlr",
    "polkit-gnome-authentication-agent-1", "screentime-daemon", "screentime-gui",
    "bash", "zsh", "fish", "sh",
}


@dataclass
class RunningApp:
    key: str
    display_name: str
    icon_name: Optional[str]
    desktop_file: Optional[str]
    pids: list[int] = field(default_factory=list)


def _process_app_identifier(p: psutil.Process) -> Optional[str]:
    try:
        name = p.name()
        # Kernel threads (kworker/*, ksoftirqd, rcu_*, ...) have no backing
        # executable on disk -- exe() resolves to '' for them on Linux. Skip
        # them; they can never be a "used application" in any sense.
        try:
            if not p.exe():
                return None
        except (psutil.AccessDenied, psutil.ZombieProcess):
            return None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    if not name or name in _NOISE_EXE_NAMES:
        return None
    return name


def list_running_apps(uid: Optional[int] = None) -> dict[str, RunningApp]:
    """Returns canonical-key -> RunningApp for the current user's processes,
    with all PIDs belonging to the same app grouped together (e.g. a browser's
    many renderer/GPU child processes all collapse into one entry)."""
    uid = uid if uid is not None else os.getuid()
    grouped: dict[str, RunningApp] = {}
    for p in psutil.process_iter(attrs=["pid", "name", "uids"]):
        try:
            info = p.info
            if info.get("uids") and info["uids"].real != uid:
                continue
            ident = _process_app_identifier(p)
            if not ident:
                continue
            resolved = resolve(ident)
            entry = grouped.get(resolved.key)
            if entry is None:
                grouped[resolved.key] = RunningApp(
                    key=resolved.key, display_name=resolved.display_name,
                    icon_name=resolved.icon_name, desktop_file=resolved.desktop_file,
                    pids=[info["pid"]],
                )
            else:
                entry.pids.append(info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return grouped


def resolve_pid_to_app(pid: int) -> Optional[ResolvedApp]:
    """Given a PID (e.g. from a Wayland compositor that reports the focused
    window's PID but not its app_id), walk up to find a meaningful process
    name and resolve it. Falls back through parent processes since some
    toolkits fork/exec through a launcher wrapper."""
    try:
        p = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    seen = set()
    cur = p
    for _ in range(6):
        if cur.pid in seen:
            break
        seen.add(cur.pid)
        ident = _process_app_identifier(cur)
        if ident:
            return resolve(ident)
        try:
            cur = cur.parent()
            if cur is None:
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break
    return None
