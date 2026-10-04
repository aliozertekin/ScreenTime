"""Thin, typed wrapper over the handful of Win32 calls ScreenTime needs.

Why a wrapper: every Windows module (foreground detector, idle, power events,
instance lock, credential store, ...) talks to this one object instead of
touching `ctypes.windll` directly. That gives us

  * correct `argtypes`/`restype` in ONE place (64-bit HANDLE/HWND truncation
    is the classic ctypes-on-Windows bug),
  * a seam: unit tests hand each module a fake `Win32Api`, so all the Windows
    logic runs on a Linux CI box with no desktop,
  * no import-time dependency on Windows: `ctypes.windll` is only touched when
    a real `Win32Api` is constructed, which only happens on Windows.

Every method either returns a plain Python value or raises `Win32Error`
carrying the Win32 error code. Nothing here ever logs secrets.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable, Optional

# ---- constants -------------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000   # the minimum right QueryFullProcessImageNameW needs
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87                 # what OpenProcess reports for a PID that no longer exists
ERROR_ALREADY_EXISTS = 183
ERROR_NOT_FOUND = 1168
ERROR_NO_SUCH_LOGON_SESSION = 1312
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2               # per-user credential, never roamed to other machines
CRYPTPROTECT_UI_FORBIDDEN = 0x1

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
WM_USER = 0x0400
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMECRITICAL = 0x0006
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012


class Win32Error(OSError):
    """A Win32 call failed. `code` is GetLastError()."""
    def __init__(self, code: int, what: str = ""):
        super().__init__(f"{what or 'Win32 call'} failed (error {code})")
        self.code = code
        self.what = what


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM) \
    if sys.platform == "win32" else None


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


class Win32Api:
    """The real thing. Only construct on Windows."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Win32Api can only be created on Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._declare()

    # ---- prototypes: get these right once ----------------------------------
    def _declare(self):
        u, k, a, c = self.user32, self.kernel32, self.advapi32, self.crypt32
        H = wintypes.HANDLE
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetForegroundWindow.argtypes = []
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumChildWindows.restype = wintypes.BOOL
        u.EnumChildWindows.argtypes = [wintypes.HWND, self._enum_proc, wintypes.LPARAM]
        u.GetLastInputInfo.restype = wintypes.BOOL
        u.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        u.RegisterClassW.restype = wintypes.ATOM
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.DefWindowProcW.restype = _LRESULT
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.GetMessageW.restype = ctypes.c_int
        u.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.restype = _LRESULT
        u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        u.PostMessageW.restype = wintypes.BOOL
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DestroyWindow.restype = wintypes.BOOL
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.PostQuitMessage.argtypes = [ctypes.c_int]

        k.OpenProcess.restype = H
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [H, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [H]
        k.GetTickCount.restype = wintypes.DWORD
        k.GetTickCount.argtypes = []
        k.QueryUnbiasedInterruptTime.restype = wintypes.BOOL
        k.QueryUnbiasedInterruptTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
        k.CreateMutexW.restype = H
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenMutexW.restype = H
        k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = H
        k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.OpenEventW.restype = H
        k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.SetEvent.restype = wintypes.BOOL
        k.SetEvent.argtypes = [H]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = [H, wintypes.DWORD]
        k.LocalFree.restype = wintypes.HLOCAL
        k.LocalFree.argtypes = [wintypes.HLOCAL]
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        a.CredWriteW.restype = wintypes.BOOL
        a.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        a.CredReadW.restype = wintypes.BOOL
        a.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        a.CredDeleteW.restype = wintypes.BOOL
        a.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        a.CredFree.argtypes = [wintypes.LPVOID]

        c.CryptProtectData.restype = wintypes.BOOL
        c.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
                                       wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        c.CryptUnprotectData.restype = wintypes.BOOL
        c.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
                                         ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID,
                                         wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]

    def _err(self, what: str) -> Win32Error:
        return Win32Error(ctypes.get_last_error(), what)

    # ---- windows / processes ----------------------------------------------
    def get_foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def get_window_thread_process_id(self, hwnd: int) -> tuple[int, int]:
        pid = wintypes.DWORD(0)
        tid = self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not tid:
            raise self._err("GetWindowThreadProcessId")
        return int(tid), int(pid.value)

    def get_class_name(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        n = self.user32.GetClassNameW(hwnd, buf, 256)
        if n <= 0:
            raise self._err("GetClassNameW")
        return buf.value

    def enum_child_windows(self, hwnd: int) -> list[int]:
        found: list[int] = []

        @self._enum_proc
        def cb(child, _lparam):
            found.append(int(child))
            return True
        self.user32.EnumChildWindows(hwnd, cb, 0)
        return found

    def query_process_image(self, pid: int) -> str:
        """Full executable path of `pid`, opened with the least right that
        works (PROCESS_QUERY_LIMITED_INFORMATION; no admin needed for the
        user's own processes). Raises Win32Error(ERROR_ACCESS_DENIED) for
        protected processes and (ERROR_INVALID_PARAMETER) for exited ones."""
        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            raise self._err("OpenProcess")
        try:
            size = wintypes.DWORD(32768)                       # long-path safe
            buf = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                raise self._err("QueryFullProcessImageNameW")
            return buf.value
        finally:
            self.kernel32.CloseHandle(h)

    # ---- time / input -------------------------------------------------------
    def get_tick_count(self) -> int:
        return int(self.kernel32.GetTickCount())

    def get_last_input_tick(self) -> int:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not self.user32.GetLastInputInfo(ctypes.byref(info)):
            raise self._err("GetLastInputInfo")
        return int(info.dwTime)

    def unbiased_interrupt_seconds(self) -> float:
        """Seconds since boot EXCLUDING time asleep/hibernating."""
        v = ctypes.c_ulonglong(0)
        if not self.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(v)):
            raise self._err("QueryUnbiasedInterruptTime")
        return v.value / 10_000_000.0                          # 100 ns units

    # ---- named mutex / event ------------------------------------------------
    def create_mutex(self, name: str) -> tuple[int, bool]:
        """(handle, already_existed)."""
        h = self.kernel32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not h:
            raise Win32Error(err, "CreateMutexW")
        return int(h), err == ERROR_ALREADY_EXISTS

    def open_mutex_exists(self, name: str) -> bool:
        SYNCHRONIZE = 0x00100000
        h = self.kernel32.OpenMutexW(SYNCHRONIZE, False, name)
        if h:
            self.kernel32.CloseHandle(h)
            return True
        return False

    def close_handle(self, handle: int) -> None:
        self.kernel32.CloseHandle(handle)

    def create_event(self, name: str) -> int:
        h = self.kernel32.CreateEventW(None, True, False, name)    # manual-reset, unsignalled
        if not h:
            raise self._err("CreateEventW")
        return int(h)

    def open_event_and_set(self, name: str) -> bool:
        EVENT_MODIFY_STATE = 0x0002
        h = self.kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
        if not h:
            return False
        try:
            return bool(self.kernel32.SetEvent(h))
        finally:
            self.kernel32.CloseHandle(h)

    def event_is_set(self, handle: int) -> bool:
        return self.kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0

    # ---- Credential Manager -------------------------------------------------
    def cred_write(self, target: str, user: str, blob: bytes, comment: str = "") -> None:
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = CREDENTIALW()
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = comment
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = user
        if not self.advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise self._err("CredWriteW")

    def cred_read(self, target: str) -> Optional[bytes]:
        p = ctypes.POINTER(CREDENTIALW)()
        if not self.advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
            code = ctypes.get_last_error()
            if code == ERROR_NOT_FOUND:
                return None
            raise Win32Error(code, "CredReadW")
        try:
            c = p.contents
            return bytes(bytearray(c.CredentialBlob[:c.CredentialBlobSize]))
        finally:
            self.advapi32.CredFree(p)

    def cred_delete(self, target: str) -> None:
        if not self.advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            code = ctypes.get_last_error()
            if code != ERROR_NOT_FOUND:
                raise Win32Error(code, "CredDeleteW")

    # ---- DPAPI ----------------------------------------------------------------
    def dpapi_protect(self, data: bytes, description: str = "ScreenTime key") -> bytes:
        src = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                               ctypes.POINTER(ctypes.c_char)))
        out = DATA_BLOB()
        if not self.crypt32.CryptProtectData(ctypes.byref(src), description, None, None, None,
                                             CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise self._err("CryptProtectData")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            self.kernel32.LocalFree(ctypes.cast(out.pbData, wintypes.HLOCAL))

    def dpapi_unprotect(self, blob: bytes) -> bytes:
        src = DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob, len(blob)),
                                               ctypes.POINTER(ctypes.c_char)))
        out = DATA_BLOB()
        if not self.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None,
                                               CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise self._err("CryptUnprotectData")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            self.kernel32.LocalFree(ctypes.cast(out.pbData, wintypes.HLOCAL))

    # ---- hidden top-level window + message pump (power events, tray) ------------
    def module_handle(self) -> int:
        return int(self.kernel32.GetModuleHandleW(None) or 0)


_api: Optional["Win32Api"] = None


def default_api() -> "Win32Api":
    global _api
    if _api is None:
        _api = Win32Api()
    return _api
