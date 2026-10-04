"""Friendly application names for Windows executables.

Reads the executable's own version resource (FileDescription, falling back to
ProductName) -- the same text Task Manager shows -- so `firefox.exe` becomes
"Firefox", `chrome.exe` "Google Chrome", `Code.exe` "Visual Studio Code". This
is display only: the canonical app *key* stays the executable stem, so history
keeps grouping the same way even if a vendor changes its description.

No admin rights needed for ordinary applications; unreadable files simply yield
None and the caller falls back to a prettified executable name.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Callable, Optional

log = logging.getLogger("screentime.appinfo")

_GENERIC = {"", "application", "windows application", "app", "setup", "launcher"}
_cache: dict[tuple, Optional[str]] = {}
_CACHE_MAX = 512


def pick_name(strings: dict[str, str], exe_stem: str) -> Optional[str]:
    """Choose the best human name from version-resource strings."""
    for field in ("FileDescription", "ProductName"):
        v = (strings.get(field) or "").strip()
        if v and v.lower() not in _GENERIC and v.lower() != exe_stem.lower() + ".exe":
            return v
    return None


def read_version_strings(path: str) -> dict[str, str]:
    """Query a PE file's string table with version.dll. {} when unavailable."""
    if sys.platform != "win32":
        return {}
    from ctypes import wintypes
    ver = ctypes.WinDLL("version", use_last_error=True)
    ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    ver.GetFileVersionInfoW.restype = wintypes.BOOL
    ver.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(wintypes.UINT)]
    ver.VerQueryValueW.restype = wintypes.BOOL
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return {}
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) or n.value < 4:
        translations = [(0x0409, 0x04B0), (0x0409, 0x04E4), (0x0000, 0x04B0)]
    else:
        raw = ctypes.string_at(ptr.value, n.value)
        translations = [(int.from_bytes(raw[i:i + 2], "little"), int.from_bytes(raw[i + 2:i + 4], "little"))
                        for i in range(0, len(raw) - 3, 4)]
    out: dict[str, str] = {}
    for lang, cp in translations:
        for field in ("FileDescription", "ProductName"):
            if field in out:
                continue
            sub = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{field}"
            if ver.VerQueryValueW(buf, sub, ctypes.byref(ptr), ctypes.byref(n)) and n.value:
                out[field] = ctypes.wstring_at(ptr.value, n.value).rstrip("\0")
        if len(out) == 2:
            break
    return out


def friendly_name_for_exe(path: str, reader: Callable[[str], dict] = read_version_strings) -> Optional[str]:
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    ck = (path.lower(), mtime)
    if ck in _cache:
        return _cache[ck]
    try:
        strings = reader(path)
    except OSError as e:
        log.debug("version info for %s unreadable: %s", path, e)
        strings = {}
    stem = os.path.splitext(os.path.basename(path))[0]
    name = pick_name(strings, stem)
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[ck] = name
    return name
