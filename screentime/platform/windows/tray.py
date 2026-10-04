"""Notification-area (system tray) icon for Windows, via Shell_NotifyIconW.

Same behaviour as the Linux AppIndicator: closing the window hides it (the
daemon keeps tracking), left-click / "Show ScreenTime" brings it back, "Quit"
exits the GUI only. Runs its own hidden window + message pump on a worker
thread (GTK4 has no tray API) and hands clicks to the GTK main loop through the
supplied `dispatch` (GLib.idle_add), so no GTK object is touched off-thread.

The icon is re-added when Explorer restarts (the TaskbarCreated broadcast),
otherwise it would silently vanish after an Explorer crash.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

from .msgwindow import MessageWindow
from .win32 import WM_DESTROY, WM_USER, Win32Api, default_api

log = logging.getLogger("screentime.tray.windows")

WM_TRAY = WM_USER + 1
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0203, 0x0205, 0x007B
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x0, 0x100, 0x2
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE, IDI_APPLICATION = 1, 0x10, 0x40, 32512
CMD_SHOW, CMD_QUIT = 1, 2


def classify_tray_event(lparam: int) -> Optional[str]:
    """What a tray callback means: 'show' (left click), 'menu' (right click), or None."""
    event = lparam & 0xFFFF
    if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
        return "show"
    if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
        return "menu"
    return None


def find_icon() -> Optional[Path]:
    here = Path(__file__).resolve()
    for p in (here.parents[2] / "resources" / "screentime.ico",
              Path(sys.prefix) / "share" / "screentime" / "screentime.ico"):
        if p.is_file():
            return p
    return None


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class WindowsTrayIcon:
    def __init__(self, on_show: Callable[[], None], on_quit: Callable[[], None],
                 dispatch: Callable[[Callable], None], api: Optional[Win32Api] = None, tooltip: str = "ScreenTime"):
        self._api = api or default_api()
        self._on_show, self._on_quit, self._dispatch = on_show, on_quit, dispatch
        self._tooltip = tooltip
        self._window = MessageWindow("ScreenTimeTrayWindow", self._handle, self._api)
        self._taskbar_created = 0
        self._hicon = None

    # ---- lifecycle -------------------------------------------------------------
    def start(self) -> bool:
        shell32, user32 = ctypes.WinDLL("shell32", use_last_error=True), self._api.user32
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._shell32 = shell32
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        if not self._window.start():
            log.info("tray: could not create its window (%s)", self._window.error)
            return False
        return self._add_icon()

    def stop(self) -> None:
        self._remove_icon()
        self._window.stop()

    # ---- icon ------------------------------------------------------------------
    def _load_icon(self):
        u = self._api.user32
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                                 ctypes.c_int, wintypes.UINT]
        u.LoadIconW.restype = wintypes.HICON
        u.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        path = find_icon()
        h = u.LoadImageW(None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE) if path else None
        return h or u.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._window.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon or self._load_icon()
        self._hicon = nid.hIcon
        nid.szTip = self._tooltip
        return nid

    def _add_icon(self) -> bool:
        return bool(self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid())))

    def _remove_icon(self) -> None:
        try:
            self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
        except (OSError, AttributeError):
            pass

    # ---- messages --------------------------------------------------------------
    def _handle(self, hwnd: int, msg: int, wparam: int, lparam: int) -> Optional[int]:
        if self._taskbar_created and msg == self._taskbar_created:
            self._add_icon()
            return 0
        if msg == WM_DESTROY:
            self._remove_icon()
            return None
        if msg != WM_TRAY:
            return None
        action = classify_tray_event(lparam)
        if action == "show":
            self._dispatch(self._on_show)
        elif action == "menu":
            cmd = self._popup_menu(hwnd)
            if cmd == CMD_SHOW:
                self._dispatch(self._on_show)
            elif cmd == CMD_QUIT:
                self._dispatch(self._on_quit)
        return 0

    def _popup_menu(self, hwnd: int) -> int:
        u = self._api.user32
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wintypes.HWND, ctypes.c_void_p]
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        menu = u.CreatePopupMenu()
        try:
            u.AppendMenuW(menu, MF_STRING, CMD_SHOW, "Show ScreenTime")
            u.AppendMenuW(menu, MF_STRING, CMD_QUIT, "Quit")
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.SetForegroundWindow(hwnd)                  # required so the menu dismisses when clicking away
            return int(u.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None))
        finally:
            u.DestroyMenu(menu)


def try_create(on_show, on_quit, dispatch) -> Optional[WindowsTrayIcon]:
    try:
        icon = WindowsTrayIcon(on_show, on_quit, dispatch)
        return icon if icon.start() else None
    except (OSError, RuntimeError) as e:
        log.info("tray icon unavailable: %s", e)
        return None
