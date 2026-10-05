"""Windows notifications via the shell (Shell_NotifyIconW with NIF_INFO).

Windows 10/11 present these as toast notifications. The daemon has no tray icon of its own, so each
notification adds a short-lived notification-area icon, asks the shell to show the balloon, and removes
the icon again a few seconds later. Local only: no WinRT, no PowerShell, no network.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Optional

from ...notifications import APP_NAME, MAX_BODY, MAX_TITLE, Notifier, clip
from .msgwindow import MessageWindow
from .tray import NIM_ADD, NIM_DELETE, NIF_ICON, NOTIFYICONDATAW, find_icon
from .win32 import Win32Api, default_api

log = logging.getLogger("screentime.notify.windows")

NIF_INFO = 0x10
NIIF_INFO = 0x1
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE, IDI_APPLICATION = 1, 0x10, 0x40, 32512
REMOVE_AFTER_SECONDS = 12.0
# Field sizes of NOTIFYICONDATAW.szInfo / szInfoTitle, minus the terminating NUL.
INFO_MAX, TITLE_MAX = 255, 63


def fit(title: str, body: str) -> tuple[str, str]:
    """Clip text to what the shell structure can hold."""
    return clip(title, min(MAX_TITLE, TITLE_MAX)), clip(body, min(MAX_BODY, INFO_MAX))


class WindowsBalloonNotifier(Notifier):
    name = "windows-shell"

    def __init__(self, api: Optional[Win32Api] = None):
        self._api = api or default_api()
        self._window: Optional[MessageWindow] = None
        self._shell32 = None
        self._lock = threading.Lock()
        self._counter = 100

    def _ensure(self) -> bool:
        if self._window is not None and self._window.hwnd:
            return True
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self._shell32 = shell32
        self._window = MessageWindow("ScreenTimeNotifyWindow", lambda *_a: None, self._api)
        if not self._window.start():
            log.info("notification window could not be created: %s", self._window.error)
            self._window = None
            return False
        return True

    def _icon(self):
        u = self._api.user32
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                                 ctypes.c_int, wintypes.UINT]
        u.LoadIconW.restype = wintypes.HICON
        u.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        path = find_icon()
        h = u.LoadImageW(None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE) if path else None
        return h or u.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def send(self, title: str, body: str) -> bool:
        title, body = fit(title, body)
        with self._lock:
            try:
                if not self._ensure():
                    return False
                self._counter += 1
                nid = NOTIFYICONDATAW()
                nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid.hWnd = self._window.hwnd
                nid.uID = self._counter
                nid.uFlags = NIF_ICON | NIF_INFO
                nid.hIcon = self._icon()
                nid.szInfoTitle = title
                nid.szInfo = body
                nid.dwInfoFlags = NIIF_INFO
                if not self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                    return False
                uid, hwnd, hicon = nid.uID, nid.hWnd, nid.hIcon
                threading.Timer(REMOVE_AFTER_SECONDS, self._remove, args=(hwnd, uid, hicon)).start()
                return True
            except (OSError, AttributeError) as e:
                log.info("notification failed: %s", e)
                return False

    def _remove(self, hwnd: int, uid: int, hicon) -> None:
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd, nid.uID = hwnd, uid
            self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except (OSError, AttributeError):
            pass
