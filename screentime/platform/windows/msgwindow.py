"""A hidden top-level window on its own thread with its own message pump.

Windows delivers broadcast messages (WM_POWERBROADCAST, WM_QUERYENDSESSION,
WM_ENDSESSION) only to *top-level* windows, not message-only ones, so this is an
invisible, zero-size, unowned window. It runs on a dedicated thread because the
daemon's GLib main loop does not pump Win32 messages, and a handler that blocks
the main loop would also block suspend. Handlers are expected to marshal work to
the main thread themselves (see power_events.make_dispatcher).

Real-Windows-only; covered by the Windows integration job, not by the fake-API
unit tests (the logic that decides what messages MEAN lives in power_events.py
and tray.py, which are unit tested).
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Optional

from .win32 import MSG, WM_CLOSE, WM_DESTROY, WNDCLASSW, _WNDPROC, Win32Api, default_api

log = logging.getLogger("screentime.msgwindow")

# handler(hwnd, msg, wparam, lparam) -> int result, or None for DefWindowProc.
Handler = Callable[[int, int, int, int], Optional[int]]


class MessageWindow:
    def __init__(self, class_name: str, handler: Handler, api: Optional[Win32Api] = None):
        self._api = api or default_api()
        self._class_name = class_name
        self._handler = handler
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self.hwnd: int = 0
        self.error: Optional[str] = None
        self._wndproc = None            # keep a reference: ctypes callbacks are GC'd otherwise

    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name=f"screentime-{self._class_name}", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return bool(self.hwnd)

    def post(self, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
        return bool(self.hwnd) and bool(self._api.user32.PostMessageW(self.hwnd, msg, wparam, lparam))

    def stop(self) -> None:
        if self.hwnd:
            self.post(WM_CLOSE)
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self):
        u = self._api.user32

        def proc(hwnd, msg, wparam, lparam):
            try:
                result = self._handler(int(hwnd or 0), msg, wparam or 0, lparam or 0)
            except Exception:
                log.exception("window message handler failed (msg=0x%x)", msg)
                result = None
            if result is not None:
                return result
            if msg == WM_CLOSE:
                u.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = _WNDPROC(proc)
        hinst = self._api.module_handle()
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        if not u.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:   # 1410 = class exists
            self.error = f"RegisterClassW failed ({ctypes.get_last_error()})"
            self._ready.set()
            return
        hwnd = u.CreateWindowExW(0, self._class_name, self._class_name, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            self.error = f"CreateWindowExW failed ({ctypes.get_last_error()})"
            self._ready.set()
            return
        self.hwnd = int(hwnd)
        self._ready.set()
        msg = MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        self.hwnd = 0
