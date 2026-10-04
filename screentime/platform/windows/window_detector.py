"""Foreground-window detection with the Win32 API.

    GetForegroundWindow -> GetWindowThreadProcessId -> OpenProcess(limited)
    -> QueryFullProcessImageNameW -> canonical executable identity

Identity is the executable, never the window title. Titles are not even read
(they would be sensitive and ScreenTime does not store them).

Anything we cannot determine yields `None` ("no data") -- never the previous
app and never a guess.
"""
from __future__ import annotations

import logging
import ntpath
import os
from typing import Callable, Optional

from ...window_detector import RawFocus, WindowDetector
from .win32 import (ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, Win32Api, Win32Error, default_api)

log = logging.getLogger("screentime.window.windows")

# Shell chrome that owns the foreground when the user clicks the desktop or
# taskbar. These are "nothing focused", the same as an empty desktop on Linux.
# (File Explorer windows use class CabinetWClass and ARE tracked.)
SHELL_CHROME_CLASSES = frozenset({
    "progman", "workerw", "shell_traywnd", "shell_secondarytraywnd",
    "notifyiconoverflowwindow", "toplevelwindowforoverflowxamlisland",
    "xamlexplorerhostislandwindow",
    "foregroundstaging", "multitaskingviewframe", "taskswitcherwnd", "forwarddestinationwnd",
})
# Lock screen / sign-in UI: a locked session is not usage.
LOCK_SCREEN_EXES = frozenset({"logonui", "lockapp"})
# UWP apps are hosted: the foreground window belongs to this frame process and
# the real app is a child window owned by another process.
UWP_FRAME_EXES = frozenset({"applicationframehost"})
# Never "app usage": ScreenTime's own windows (see Windows notes in the README).
# The packaged build ships the GUI and daemon launchers under these names, so
# the GUI process is recognisable from the daemon without any IPC.
SELF_EXES = frozenset({"screentime-gui", "screentime-daemon", "screentime"})


def exe_stem(path: str) -> str:
    """'C:\\Program Files\\Mozilla Firefox\\firefox.exe' -> 'firefox'."""
    base = ntpath.basename(path.replace("/", "\\"))
    stem, ext = ntpath.splitext(base)
    return (stem if ext.lower() in (".exe", ".scr", ".com") else base).strip().lower()


class WindowsDetector(WindowDetector):
    name = "windows"

    def __init__(self, api: Optional[Win32Api] = None,
                 name_for_exe: Optional[Callable[[str], Optional[str]]] = None,
                 steam_key_for_exe: Optional[Callable[[str], Optional[str]]] = None,
                 own_pids: Optional[set[int]] = None):
        self._api = api or default_api()
        self._name_for_exe = name_for_exe
        self._steam_key_for_exe = steam_key_for_exe
        self._own_pids = own_pids if own_pids is not None else {os.getpid()}
        self.last_error: Optional[str] = None          # for diagnostics only; never contains titles

    def is_supported(self) -> bool:
        try:
            self._api.get_foreground_window()
            return True
        except (OSError, AttributeError):
            return False

    # ------------------------------------------------------------------ pid
    def _pid_of(self, hwnd: int) -> Optional[int]:
        try:
            _tid, pid = self._api.get_window_thread_process_id(hwnd)
        except Win32Error as e:
            self.last_error = f"GetWindowThreadProcessId: {e.code}"
            return None
        return pid or None

    def _class_of(self, hwnd: int) -> str:
        try:
            return self._api.get_class_name(hwnd).lower()
        except Win32Error:
            return ""

    def _image_of(self, pid: int) -> Optional[str]:
        try:
            return self._api.query_process_image(pid)
        except Win32Error as e:
            # Access denied = protected/elevated process; invalid parameter =
            # the process exited between calls. Both are "unknown", not errors.
            if e.code not in (ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER):
                log.debug("query_process_image(%s) failed: %s", pid, e)
            self.last_error = f"QueryFullProcessImageNameW: {e.code}"
            return None

    def _resolve_uwp_child(self, hwnd: int, frame_pid: int) -> Optional[int]:
        """The real app's pid inside an ApplicationFrameHost window."""
        try:
            for child in self._api.enum_child_windows(hwnd):
                pid = self._pid_of(child)
                if pid and pid != frame_pid:
                    return pid
        except Win32Error as e:
            log.debug("enumerating UWP children failed: %s", e)
        return None

    # ------------------------------------------------------------------ main
    def get_focused(self) -> Optional[RawFocus]:
        try:
            hwnd = self._api.get_foreground_window()
        except Win32Error as e:
            self.last_error = f"GetForegroundWindow: {e.code}"
            return None
        if not hwnd:
            return None                                # e.g. mid-switch, or secure desktop
        if self._class_of(hwnd) in SHELL_CHROME_CLASSES:
            return None
        pid = self._pid_of(hwnd)
        if pid is None:
            return None
        if pid in self._own_pids:
            return None
        image = self._image_of(pid)
        if not image:
            return None
        stem = exe_stem(image)
        if stem in UWP_FRAME_EXES:
            real_pid = self._resolve_uwp_child(hwnd, pid)
            real_image = self._image_of(real_pid) if real_pid else None
            if not real_image:
                return None
            pid, image, stem = real_pid, real_image, exe_stem(real_image)
        if stem in LOCK_SCREEN_EXES or stem in SELF_EXES:
            return None
        key = None
        if self._steam_key_for_exe is not None:
            try:
                key = self._steam_key_for_exe(image)         # steam_app_<AppID> for games in a Steam library
            except Exception:                                 # resolver must never break tracking
                log.debug("steam key lookup failed", exc_info=True)
        hint = None
        if self._name_for_exe is not None and key is None:
            try:
                hint = self._name_for_exe(image)
            except Exception:
                log.debug("friendly-name lookup failed", exc_info=True)
        return RawFocus(identifier=key or stem, pid=pid, title=None, display_hint=hint)


def probe(api: Optional[Win32Api] = None) -> int:
    """`python -m screentime.platform.windows.window_detector`: print what the
    detector sees every second (the Windows twin of the Linux probe). Window
    titles are never printed."""
    import time
    from .app_info import friendly_name_for_exe
    det = WindowsDetector(api, name_for_exe=friendly_name_for_exe)
    print("Foreground detector: Windows Win32 (GetForegroundWindow). Ctrl+C to stop.")
    try:
        while True:
            f = det.get_focused()
            print(f"identifier={f.identifier!r} pid={f.pid} name={f.display_hint!r}" if f
                  else f"no data ({det.last_error or 'nothing trackable focused'})")
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(probe())
