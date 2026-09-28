"""
Resolves a raw window/process identifier (X11 WM_CLASS, Wayland app_id, or a
bare executable name) into a stable canonical "app key" plus a friendly
display name and icon, using the user's installed .desktop files where
possible.

This is what makes "multiple processes/windows belong to one application"
correct: Firefox might show up as wm_class "Navigator"/"firefox", have
dozens of content-process PIDs, and multiple windows -- all of it collapses
to the single canonical key "firefox".
"""

from __future__ import annotations

import configparser
import functools
import glob
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ResolvedApp:
    key: str
    display_name: str
    icon_name: Optional[str]
    desktop_file: Optional[str]


def _canonicalize(raw: str) -> str:
    raw = raw.strip().lower()
    raw = re.sub(r"\.desktop$", "", raw)
    # Common noisy suffixes/prefixes seen in wm_class across distros/flatpak
    raw = re.sub(r"^(org\.|com\.|io\.|net\.)[a-z0-9._-]*\.", "", raw)
    raw = raw.replace(" ", "-")
    return raw


@functools.lru_cache(maxsize=1)
def _desktop_file_index() -> dict:
    """Maps canonical keys (from StartupWMClass, Exec basename, and the
    .desktop filename itself) -> parsed (name, icon, path)."""
    search_dirs = [
        "/usr/share/applications",
        "/usr/local/share/applications",
        str(Path.home() / ".local/share/applications"),
        "/var/lib/flatpak/exports/share/applications",
        str(Path.home() / ".local/share/flatpak/exports/share/applications"),
    ]
    return _build_index_from_dirs(search_dirs)


def _build_index_from_dirs(search_dirs: list[str]) -> dict:
    index: dict[str, tuple[str, Optional[str], str]] = {}
    for d in search_dirs:
        for path in glob.glob(os.path.join(d, "*.desktop")):
            try:
                cp = configparser.RawConfigParser(strict=False, interpolation=None)
                cp.read(path, encoding="utf-8")
                if "Desktop Entry" not in cp:
                    continue
                entry = cp["Desktop Entry"]
                if entry.get("NoDisplay", "false").lower() == "true":
                    continue
                name = entry.get("Name", os.path.basename(path))
                icon = entry.get("Icon")
                keys = set()
                wm_class = entry.get("StartupWMClass")
                if wm_class:
                    keys.add(_canonicalize(wm_class))
                exec_line = entry.get("Exec", "")
                if exec_line:
                    exe = exec_line.split()[0] if exec_line.split() else ""
                    exe = os.path.basename(exe)
                    if exe:
                        keys.add(_canonicalize(exe))
                keys.add(_canonicalize(os.path.basename(path)))
                for k in keys:
                    if k and k not in index:
                        index[k] = (name, icon, path)
            except Exception:
                continue
    return index


def resolve(raw_identifier: str, fallback_display: Optional[str] = None) -> ResolvedApp:
    """raw_identifier: WM_CLASS instance/class, wayland app_id, or exe basename."""
    key = _canonicalize(raw_identifier)
    idx = _desktop_file_index()
    if key in idx:
        name, icon, path = idx[key]
        return ResolvedApp(key=key, display_name=name, icon_name=icon, desktop_file=path)
    # No .desktop match: still group correctly by canonical key, just use a
    # human-friendlier capitalization of the raw name as display name.
    display = fallback_display or raw_identifier
    display = re.sub(r"^(org\.|com\.|io\.|net\.)[a-zA-Z0-9._-]*\.", "", display)
    display = re.sub(r"[-_.]+", " ", display).strip()
    display = " ".join(w.capitalize() for w in display.split()) or raw_identifier
    return ResolvedApp(key=key, display_name=display, icon_name=key, desktop_file=None)


def refresh_desktop_index():
    _desktop_file_index.cache_clear()
