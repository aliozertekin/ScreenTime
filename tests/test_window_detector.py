"""
Unit tests for the active-window detection parsing logic, using realistic
canned output from each backend rather than a live compositor (which this
test environment doesn't have). These exist specifically to catch parsing
bugs in the sway tree walk, hyprctl JSON handling, xprop regexes, and the
GNOME/KDE D-Bus payload formats -- the part of window_detector.py that is
otherwise only exercised by hand on a real desktop.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime.window_detector import (
    SwayDetector, HyprlandDetector, X11Detector,
    GnomeShellExtensionDetector, KWinPushDetector, RawFocus,
)


# --------------------------------------------------------------------- sway
SWAY_TREE = {
    "type": "root",
    "nodes": [
        {
            "type": "output", "name": "eDP-1",
            "nodes": [
                {
                    "type": "workspace", "name": "1",
                    "nodes": [
                        {
                            "type": "con", "focused": False, "app_id": "firefox",
                            "pid": 1234, "name": "Mozilla Firefox",
                            "nodes": [], "floating_nodes": [],
                        },
                        {
                            "type": "con", "focused": True, "app_id": "foot",
                            "pid": 5678, "name": "terminal",
                            "nodes": [], "floating_nodes": [],
                        },
                    ],
                    "floating_nodes": [],
                }
            ],
        }
    ],
}

# Xwayland app inside sway reports class via window_properties instead of app_id.
SWAY_TREE_XWAYLAND = {
    "type": "root",
    "nodes": [{
        "type": "workspace", "name": "1", "nodes": [
            {
                "type": "con", "focused": True, "app_id": None, "pid": 999,
                "name": "GIMP", "window_properties": {"class": "Gimp"},
                "nodes": [], "floating_nodes": [],
            }
        ],
        "floating_nodes": [],
    }],
}

SWAY_TREE_NOTHING_FOCUSED = {
    "type": "root",
    "nodes": [{"type": "workspace", "name": "1", "nodes": [], "floating_nodes": []}],
}


def _mock_run(stdout: str, returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_sway_finds_focused_native_app():
    d = SwayDetector()
    with patch("subprocess.run", return_value=_mock_run(json.dumps(SWAY_TREE))):
        focus = d.get_focused()
    assert focus == RawFocus(identifier="foot", pid=5678, title="terminal")


def test_sway_handles_xwayland_app_via_window_properties():
    d = SwayDetector()
    with patch("subprocess.run", return_value=_mock_run(json.dumps(SWAY_TREE_XWAYLAND))):
        focus = d.get_focused()
    assert focus.identifier == "Gimp"
    assert focus.pid == 999


def test_sway_nothing_focused_returns_none():
    d = SwayDetector()
    with patch("subprocess.run", return_value=_mock_run(json.dumps(SWAY_TREE_NOTHING_FOCUSED))):
        assert d.get_focused() is None


def test_sway_malformed_json_does_not_raise():
    d = SwayDetector()
    with patch("subprocess.run", return_value=_mock_run("not json")):
        assert d.get_focused() is None


def test_sway_subprocess_error_does_not_raise():
    d = SwayDetector()
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        assert d.get_focused() is None


# ---------------------------------------------------------------- hyprland
def test_hyprland_parses_active_window():
    payload = {"class": "code-url-handler", "pid": 4321, "title": "main.py - Code"}
    d = HyprlandDetector()
    with patch("subprocess.run", return_value=_mock_run(json.dumps(payload))):
        focus = d.get_focused()
    assert focus == RawFocus(identifier="code-url-handler", pid=4321, title="main.py - Code")


def test_hyprland_empty_response_no_window_focused():
    d = HyprlandDetector()
    with patch("subprocess.run", return_value=_mock_run("{}")):
        assert d.get_focused() is None


def test_hyprland_no_windows_open_returns_empty_string():
    # hyprctl activewindow returns an empty payload if nothing is open.
    d = HyprlandDetector()
    with patch("subprocess.run", return_value=_mock_run("")):
        assert d.get_focused() is None


# --------------------------------------------------------------------- x11
XPROP_ACTIVE_WINDOW_OUT = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3800003\n"
XPROP_WM_CLASS_OUT = (
    'WM_CLASS(STRING) = "navigator", "firefox"\n'
    '_NET_WM_PID(CARDINAL) = 9001\n'
)
XPROP_NO_ACTIVE_WINDOW_OUT = "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"


def test_x11_xprop_fallback_parses_wm_class_and_pid():
    d = X11Detector.__new__(X11Detector)  # bypass __init__'s xlib probing
    d._xlib_ok = False
    with patch("subprocess.run", side_effect=[
        _mock_run(XPROP_ACTIVE_WINDOW_OUT),
        _mock_run(XPROP_WM_CLASS_OUT),
    ]):
        focus = d._get_focused_xprop()
    assert focus == RawFocus(identifier="firefox", pid=9001)


def test_x11_xprop_no_active_window():
    d = X11Detector.__new__(X11Detector)
    d._xlib_ok = False
    with patch("subprocess.run", return_value=_mock_run(XPROP_NO_ACTIVE_WINDOW_OUT)):
        assert d._get_focused_xprop() is None


def test_x11_xprop_survives_garbage_output():
    d = X11Detector.__new__(X11Detector)
    d._xlib_ok = False
    with patch("subprocess.run", return_value=_mock_run("garbage, no match here")):
        assert d._get_focused_xprop() is None


def test_x11_xprop_property_not_found():
    # When no WM has ever set _NET_ACTIVE_WINDOW (e.g. a bare X session with
    # no window manager), xprop prints this instead of "window id # 0x0".
    d = X11Detector.__new__(X11Detector)
    d._xlib_ok = False
    with patch("subprocess.run", return_value=_mock_run("_NET_ACTIVE_WINDOW:  not found.\n")):
        assert d._get_focused_xprop() is None


# ------------------------------------------------------------- gnome/kde
def test_gnome_extension_parses_gdbus_output():
    # gdbus call prints a tuple-repr line like: ('{"app_id": "firefox", "pid": 111, "title": "Mozilla Firefox"}',)
    gdbus_output = '(\'{"app_id": "firefox", "pid": 111, "title": "Mozilla Firefox"}\',)\n'
    d = GnomeShellExtensionDetector.__new__(GnomeShellExtensionDetector)
    d._gdbus = "/usr/bin/gdbus"
    with patch("subprocess.run", return_value=_mock_run(gdbus_output)):
        focus = d.get_focused()
    assert focus.identifier == "firefox"
    assert focus.pid == 111


def test_gnome_extension_no_window_focused():
    gdbus_output = "('{}',)\n"
    d = GnomeShellExtensionDetector.__new__(GnomeShellExtensionDetector)
    d._gdbus = "/usr/bin/gdbus"
    with patch("subprocess.run", return_value=_mock_run(gdbus_output)):
        assert d.get_focused() is None


def test_kwin_push_detector_handles_report_payload():
    d = KWinPushDetector.__new__(KWinPushDetector)  # skip D-Bus registration
    d._latest = None
    payload = json.dumps({"resourceClass": "org.kde.dolphin", "pid": 222, "caption": "Documents"})
    d._handle_report(payload)
    assert d.get_focused() == RawFocus(identifier="org.kde.dolphin", pid=222, title="Documents")


def test_kwin_push_detector_bad_payload_does_not_raise():
    d = KWinPushDetector.__new__(KWinPushDetector)
    d._latest = RawFocus(identifier="stale", pid=1)
    d._handle_report("not json at all")
    # Bad payload should be ignored, not clear the previous valid state via a crash.
    assert d.get_focused() == RawFocus(identifier="stale", pid=1)
