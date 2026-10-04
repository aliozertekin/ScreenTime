r"""Windows light/dark preference and accent colour, for the existing theme
system (theme.SystemInfo). No second theme engine: this only supplies the two
facts the GTK layer cannot reliably get on Windows.

  * apps light/dark:  HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize
                      AppsUseLightTheme (0 = dark, 1 = light)
  * accent colour:    HKCU\Software\Microsoft\Windows\DWM  AccentColor
                      (a DWORD laid out 0xAABBGGRR)

Anything missing or malformed yields None, and the caller keeps the theme's own
accent -- we never invent a colour.
"""
from __future__ import annotations

from typing import Callable, Optional

PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
DWM = r"Software\Microsoft\Windows\DWM"


def _read_dword(subkey: str, name: str) -> Optional[int]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as k:
            value, kind = winreg.QueryValueEx(k, name)
            return int(value) if kind == winreg.REG_DWORD else None
    except (ImportError, OSError, ValueError):
        return None


def abgr_to_hex(value: Optional[int]) -> Optional[str]:
    if value is None or not (0 <= value <= 0xFFFFFFFF):
        return None
    r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


def system_is_dark(read: Callable[[str, str], Optional[int]] = _read_dword) -> Optional[bool]:
    v = read(PERSONALIZE, "AppsUseLightTheme")
    return None if v is None else v == 0


def system_accent(read: Callable[[str, str], Optional[int]] = _read_dword) -> Optional[str]:
    return abgr_to_hex(read(DWM, "AccentColor"))
