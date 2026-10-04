"""A fake `Win32Api` so every Windows module runs on any OS with no desktop."""
from __future__ import annotations

from screentime.platform.windows.win32 import (ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND,
                                               Win32Error)


class FakeWin32:
    def __init__(self):
        self.foreground = 0
        self.windows: dict[int, dict] = {}        # hwnd -> {pid, cls, children}
        self.images: dict[int, object] = {}       # pid -> path | Win32Error
        self.tick = 100_000
        self.last_input = 100_000
        self.fail_input = False
        self.fail_foreground = False
        self.unbiased = 1000.0
        self.fail_unbiased = False
        # named objects
        self.mutexes: set[str] = set()
        self.handles: dict[int, tuple] = {}
        self._next = 100
        self.events: dict[str, bool] = {}
        # credential manager / DPAPI
        self.creds: dict[str, bytes] = {}
        self.cred_error: int | None = None
        self.dpapi_error: int | None = None

    # -- windows / processes
    def add_window(self, hwnd, pid, cls="Chrome_WidgetWin_1", image=None, children=()):
        self.windows[hwnd] = {"pid": pid, "cls": cls, "children": list(children)}
        if image is not None:
            self.images[pid] = image

    def get_foreground_window(self):
        if self.fail_foreground:
            raise Win32Error(5, "GetForegroundWindow")
        return self.foreground

    def get_window_thread_process_id(self, hwnd):
        w = self.windows.get(hwnd)
        if w is None:
            raise Win32Error(1400, "GetWindowThreadProcessId")      # invalid window handle
        return 1, w["pid"]

    def get_class_name(self, hwnd):
        w = self.windows.get(hwnd)
        if w is None:
            raise Win32Error(1400, "GetClassNameW")
        return w["cls"]

    def enum_child_windows(self, hwnd):
        return list(self.windows.get(hwnd, {}).get("children", []))

    def query_process_image(self, pid):
        v = self.images.get(pid, Win32Error(ERROR_INVALID_PARAMETER, "OpenProcess"))
        if isinstance(v, Win32Error):
            raise v
        return v

    # -- idle / time
    def get_tick_count(self):
        return self.tick % (1 << 32)

    def get_last_input_tick(self):
        if self.fail_input:
            raise Win32Error(5, "GetLastInputInfo")
        return self.last_input % (1 << 32)

    def unbiased_interrupt_seconds(self):
        if self.fail_unbiased:
            raise Win32Error(1, "QueryUnbiasedInterruptTime")
        return self.unbiased

    # -- mutex / event
    def create_mutex(self, name):
        existed = name in self.mutexes
        self.mutexes.add(name)
        h = self._next
        self._next += 1
        self.handles[h] = ("mutex", name)
        return h, existed

    def open_mutex_exists(self, name):
        return name in self.mutexes

    def close_handle(self, h):
        kind, name = self.handles.pop(h, (None, None))
        if kind == "mutex" and not any(k == "mutex" and n == name for k, n in self.handles.values()):
            self.mutexes.discard(name)       # last handle closed -> object is gone (what Windows does on process death)

    def create_event(self, name):
        h = self._next
        self._next += 1
        self.handles[h] = ("event", name)
        self.events.setdefault(name, False)
        return h

    def open_event_and_set(self, name):
        if name not in self.events:
            return False
        self.events[name] = True
        return True

    def event_is_set(self, h):
        return self.events.get(self.handles[h][1], False)

    def crash(self):
        """Process dies: Windows closes every handle it held."""
        for h in list(self.handles):
            self.close_handle(h)

    # -- credentials / dpapi
    def cred_write(self, target, user, blob, comment=""):
        if self.cred_error:
            raise Win32Error(self.cred_error, "CredWriteW")
        self.creds[target] = bytes(blob)

    def cred_read(self, target):
        if self.cred_error:
            raise Win32Error(self.cred_error, "CredReadW")
        return self.creds.get(target)

    def cred_delete(self, target):
        self.creds.pop(target, None)

    def dpapi_protect(self, data, description=""):
        if self.dpapi_error:
            raise Win32Error(self.dpapi_error, "CryptProtectData")
        return b"DPAPI1" + bytes(b ^ 0x5A for b in data)

    def dpapi_unprotect(self, blob):
        if self.dpapi_error or not blob.startswith(b"DPAPI1"):
            raise Win32Error(self.dpapi_error or 13, "CryptUnprotectData")
        return bytes(b ^ 0x5A for b in blob[6:])
