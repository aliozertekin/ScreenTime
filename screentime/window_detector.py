"""
Active-window detection, abstracted behind a small strategy interface so the
daemon never needs to know whether it's running under X11, sway, Hyprland,
GNOME (Wayland), or KDE (Wayland).

Why not just use xdotool everywhere? Two reasons:
  1. It's an extra dependency this project doesn't otherwise need (X11-only
     anyway).
  2. It simply does not work on Wayland: Wayland's security model has no
     concept of "any client can query any other window's title/class" by
     design, so each compositor exposes its own (different) mechanism, or
     none at all without an explicit companion component.

Coverage matrix implemented here:
  * X11 (any WM)         -> python-xlib if available, else `xprop` subprocess.
  * sway / other wlroots  -> `swaymsg -t get_tree` (works for wlr-based
    compositors that speak the sway IPC protocol, e.g. sway itself).
  * Hyprland              -> `hyprctl activewindow -j`.
  * GNOME on Wayland      -> GNOME Shell has no public API for this by
    design. We look for a tiny companion GNOME Shell extension (shipped in
    data/gnome-extension/) that exposes the focused window over D-Bus. If
    it isn't installed, we degrade gracefully (see NullDetector) and the
    GUI's Settings page tells the user how to enable it.
  * KDE Plasma on Wayland -> similarly no stable public API; a companion
    KWin script (data/kde-script/) exposes the focused window over D-Bus.
    Degrades gracefully if not installed.

In all "degrades gracefully" cases, the daemon still tracks presence via
process_monitor, it just cannot attribute *focused* time, and it reports
this clearly to the GUI so the user isn't silently missing data without
knowing why.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from . import platform as _platform

log = logging.getLogger("screentime.window")


@dataclass
class RawFocus:
    identifier: str            # wm_class / app_id / exe name -- used for app_identity.resolve()
    pid: Optional[int] = None
    title: Optional[str] = None
    # A friendlier name the detector already knows (Windows: the executable's
    # FileDescription). Only used as the display name when nothing better resolves.
    display_hint: Optional[str] = None


class WindowDetector(ABC):
    name: str = "unknown"

    @abstractmethod
    def get_focused(self) -> Optional[RawFocus]:
        """Return info about the currently focused window, or None if
        nothing is focused (e.g. all windows minimized) or unsupported."""

    def is_supported(self) -> bool:
        return True


class NullDetector(WindowDetector):
    name = "unsupported"

    def get_focused(self) -> Optional[RawFocus]:
        return None

    def is_supported(self) -> bool:
        return False


# --------------------------------------------------------------------- X11
class X11Detector(WindowDetector):
    name = "x11"

    def __init__(self):
        self._xlib_ok = False
        try:
            from Xlib import display  # noqa
            self._display = display.Display()
            self._xlib_ok = True
        except Exception:
            self._xlib_ok = False
            self._has_xprop = shutil.which("xprop") is not None

    def is_supported(self) -> bool:
        return self._xlib_ok or shutil.which("xprop") is not None

    def get_focused(self) -> Optional[RawFocus]:
        if self._xlib_ok:
            return self._get_focused_xlib()
        return self._get_focused_xprop()

    # -- python-xlib path (fast, no subprocess spawn every poll) --
    def _get_focused_xlib(self) -> Optional[RawFocus]:
        from Xlib import X
        try:
            root = self._display.screen().root
            net_active = self._display.intern_atom("_NET_ACTIVE_WINDOW")
            resp = root.get_full_property(net_active, X.AnyPropertyType)
            if not resp or not resp.value:
                return None
            win_id = resp.value[0]
            if win_id == 0:
                return None
            window = self._display.create_resource_object("window", win_id)
            wm_class = window.get_wm_class()
            pid = self._get_pid(window)
            title = None
            try:
                title = window.get_wm_name()
            except Exception:
                pass
            if wm_class:
                # wm_class is (instance, class); class name is the stable identity.
                identifier = wm_class[1] or wm_class[0]
            elif pid:
                identifier = None
            else:
                return None
            if identifier:
                return RawFocus(identifier=identifier, pid=pid, title=title)
            if pid:
                return RawFocus(identifier=f"pid:{pid}", pid=pid, title=title)
            return None
        except Exception as e:
            log.debug("xlib focus lookup failed: %s", e)
            return None

    def _get_pid(self, window) -> Optional[int]:
        try:
            from Xlib import X
            net_pid = self._display.intern_atom("_NET_WM_PID")
            resp = window.get_full_property(net_pid, X.AnyPropertyType)
            if resp and resp.value:
                return int(resp.value[0])
        except Exception:
            pass
        return None

    # -- xprop subprocess fallback (no python-xlib installed) --
    def _get_focused_xprop(self) -> Optional[RawFocus]:
        try:
            out = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"], capture_output=True, text=True, timeout=2
            ).stdout
            m = re.search(r"# (0x[0-9a-fA-F]+)", out)
            if not m:
                return None
            win_id = m.group(1)
            if win_id in ("0x0",):
                return None
            wm = subprocess.run(
                ["xprop", "-id", win_id, "WM_CLASS", "_NET_WM_PID"],
                capture_output=True, text=True, timeout=2,
            ).stdout
            class_m = re.search(r'WM_CLASS\(STRING\) = "[^"]*", "([^"]*)"', wm)
            pid_m = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", wm)
            pid = int(pid_m.group(1)) if pid_m else None
            if class_m:
                return RawFocus(identifier=class_m.group(1), pid=pid)
            if pid:
                return RawFocus(identifier=f"pid:{pid}", pid=pid)
            return None
        except Exception as e:
            log.debug("xprop focus lookup failed: %s", e)
            return None


# --------------------------------------------------------------------- sway
class SwayDetector(WindowDetector):
    name = "sway"

    def is_supported(self) -> bool:
        return shutil.which("swaymsg") is not None and bool(os.environ.get("SWAYSOCK"))

    def get_focused(self) -> Optional[RawFocus]:
        try:
            out = subprocess.run(["swaymsg", "-t", "get_tree"], capture_output=True, text=True, timeout=2)
            tree = json.loads(out.stdout)
        except Exception as e:
            log.debug("swaymsg failed: %s", e)
            return None
        node = self._find_focused(tree)
        if not node:
            return None
        app_id = node.get("app_id")
        pid = node.get("pid")
        if not app_id:
            wp = node.get("window_properties") or {}
            app_id = wp.get("class")
        if app_id:
            return RawFocus(identifier=app_id, pid=pid, title=node.get("name"))
        if pid:
            return RawFocus(identifier=f"pid:{pid}", pid=pid, title=node.get("name"))
        return None

    def _find_focused(self, node: dict) -> Optional[dict]:
        if not isinstance(node, dict):
            return None
        if node.get("focused"):
            return node
        for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
            found = self._find_focused(child)
            if found:
                return found
        return None


# ---------------------------------------------------------------- hyprland
class HyprlandDetector(WindowDetector):
    name = "hyprland"

    def is_supported(self) -> bool:
        return shutil.which("hyprctl") is not None and bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))

    def get_focused(self) -> Optional[RawFocus]:
        try:
            out = subprocess.run(["hyprctl", "-j", "activewindow"], capture_output=True, text=True, timeout=2)
            data = json.loads(out.stdout)
        except Exception as e:
            log.debug("hyprctl failed: %s", e)
            return None
        if not data:
            return None
        app_id = data.get("class")
        pid = data.get("pid")
        if app_id:
            return RawFocus(identifier=app_id, pid=pid, title=data.get("title"))
        if pid:
            return RawFocus(identifier=f"pid:{pid}", pid=pid)
        return None


# ---------------------------------------------- GNOME Shell (companion ext)
class GnomeShellExtensionDetector(WindowDetector):
    name = "gnome-shell"
    _BUS = "org.gnome.Shell"
    _PATH = "/org/gnome/Shell/Extensions/ScreenTime"
    _IFACE = "org.gnome.Shell.Extensions.ScreenTime"

    def __init__(self):
        self._gdbus = shutil.which("gdbus")

    def is_supported(self) -> bool:
        if not self._gdbus:
            return False
        try:
            out = subprocess.run(
                ["gdbus", "call", "--session", "--dest", self._BUS, "--object-path", self._PATH,
                 "--method", f"{self._IFACE}.GetFocusedWindow"],
                capture_output=True, text=True, timeout=2,
            )
            return out.returncode == 0
        except Exception:
            return False

    def get_focused(self) -> Optional[RawFocus]:
        try:
            out = subprocess.run(
                ["gdbus", "call", "--session", "--dest", self._BUS, "--object-path", self._PATH,
                 "--method", f"{self._IFACE}.GetFocusedWindow"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode != 0:
                return None
            # gdbus prints something like ('{"app_id": "firefox", "pid": 1234},)
            m = re.search(r"\{.*\}", out.stdout)
            if not m:
                return None
            data = json.loads(m.group(0))
            app_id = data.get("app_id")
            pid = data.get("pid")
            if app_id:
                return RawFocus(identifier=app_id, pid=pid, title=data.get("title"))
            if pid:
                return RawFocus(identifier=f"pid:{pid}", pid=pid)
            return None
        except Exception as e:
            log.debug("gnome extension focus lookup failed: %s", e)
            return None


# ------------------------------------------------------- KDE (KWin, push model)
class KWinPushDetector(WindowDetector):
    """KDE Plasma on Wayland has no public pull API for 'what's focused'
    either. Unlike GNOME, a KWin script also cannot host its own D-Bus
    service -- but it *can* call out to an arbitrary D-Bus method whenever
    `workspace.windowActivated` fires (see data/kde-script/). So the model
    here is inverted from every other detector: this class owns a small
    D-Bus service (org.screentime.KWinFocus) that the KWin script pushes
    updates to, and get_focused() just returns the last cached value --
    there is no round-trip on the polling tick.
    """
    name = "kwin-push"
    _BUS_NAME = "org.screentime.KWinFocus"
    _PATH = "/org/screentime/KWinFocus"
    _IFACE_XML = """
    <node>
      <interface name="org.screentime.KWinFocus">
        <method name="ReportFocus">
          <arg type="s" direction="in" name="json"/>
        </method>
      </interface>
    </node>"""

    def __init__(self):
        self._latest: Optional[RawFocus] = None
        self._owned = False
        self._register()

    def _register(self):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib

            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            node_info = Gio.DBusNodeInfo.new_for_xml(self._IFACE_XML)
            iface_info = node_info.interfaces[0]

            def handle_call(connection, sender, object_path, interface_name,
                             method_name, parameters, invocation):
                if method_name == "ReportFocus":
                    (payload,) = parameters.unpack()
                    self._handle_report(payload)
                    invocation.return_value(None)

            self._bus.register_object(self._PATH, iface_info, handle_call, None, None)
            Gio.bus_own_name_on_connection(self._bus, self._BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
            self._owned = True
        except Exception as e:
            log.debug("Could not register KWin push D-Bus service: %s", e)
            self._owned = False

    def _handle_report(self, payload: str):
        try:
            data = json.loads(payload)
            app_id = data.get("resourceClass") or ""
            pid = data.get("pid") or None
            title = data.get("caption")
            if app_id:
                self._latest = RawFocus(identifier=app_id, pid=pid, title=title)
            elif pid:
                self._latest = RawFocus(identifier=f"pid:{pid}", pid=pid, title=title)
            else:
                self._latest = None
            log.debug("KWin script pushed focus update: %r", self._latest)
        except Exception as e:
            log.debug("Bad payload from KWin script: %s", e)

    def is_supported(self) -> bool:
        # We can always register the D-Bus service; whether the KWin script
        # is actually installed and pushing data is a separate question the
        # Settings diagnostics page surfaces (falls back to "no data yet").
        return self._owned

    def get_focused(self) -> Optional[RawFocus]:
        return self._latest


# Backwards-compatible alias used by create_window_detector() below.
KWinScriptDetector = KWinPushDetector


def detect_session_type() -> str:
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


def create_window_detector() -> WindowDetector:
    """Pick the best available detector for the current session, preferring
    native/precise Wayland compositor protocols over X11/XWayland, since a
    Wayland session may still have DISPLAY set for XWayland compatibility."""
    if _platform.is_windows():
        return _create_windows_detector()
    session = detect_session_type()
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()

    candidates: list[WindowDetector] = []
    if session == "wayland":
        if os.environ.get("SWAYSOCK"):
            candidates.append(SwayDetector())
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            candidates.append(HyprlandDetector())
        if "gnome" in desktop:
            candidates.append(GnomeShellExtensionDetector())
        if "kde" in desktop or "plasma" in desktop:
            candidates.append(KWinScriptDetector())
        # XWayland fallback still catches focus on X11-only apps in some setups,
        # but cannot see native Wayland client focus, so it's a last resort.
        candidates.append(X11Detector())
    else:
        candidates.append(X11Detector())

    for c in candidates:
        try:
            if c.is_supported():
                log.info("Using window detector: %s", c.name)
                return c
        except Exception as e:
            log.debug("detector %s support check failed: %s", getattr(c, "name", c), e)

    log.warning(
        "No active-window detection method is available for this session "
        "(session=%s desktop=%s). Focused-app time cannot be measured until "
        "one is installed -- see README 'Wayland compatibility'.", session, desktop,
    )
    return NullDetector()


def _create_windows_detector() -> WindowDetector:
    """Win32 foreground detector, with friendly names and Steam-game identity
    wired in. Falls back to the honest NullDetector (and the daemon retries)
    if Win32 is not usable yet."""
    try:
        from .platform.windows.window_detector import WindowsDetector
        from .platform.windows.app_info import friendly_name_for_exe
        from . import steam_library
        det = WindowsDetector(name_for_exe=friendly_name_for_exe,
                              steam_key_for_exe=lambda exe: steam_library.default_resolver().key_for_exe(exe))
        if det.is_supported():
            log.info("Using window detector: %s", det.name)
            return det
    except (OSError, RuntimeError, ImportError) as e:
        log.debug("Windows detector unavailable: %s", e)
    log.warning("The Win32 foreground-window API is not available yet; focused-app time cannot be measured.")
    return NullDetector()
