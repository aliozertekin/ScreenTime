"""Windows support must not add any network capability."""
import re
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "screentime"
NET_DLLS = re.compile(r"winhttp|wininet|ws2_32|wsock32|urlmon|iphlpapi|dnsapi", re.I)


def test_no_windows_source_loads_a_network_dll():
    offenders = [p.name for p in ROOT.rglob("*.py") if NET_DLLS.search(p.read_text())]
    assert offenders == []


def test_no_updater_telemetry_or_crash_reporter_hooks():
    banned = re.compile(r"sentry|telemetry\.|analytics|crashpad|check_for_update|auto_?update|api\.steampowered", re.I)
    offenders = [p.name for p in ROOT.rglob("*.py") if banned.search(p.read_text())]
    assert offenders == []


def test_windows_code_paths_make_no_connections(monkeypatch, tmp_path):
    def forbid(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    from screentime.platform.windows import window_detector as wd, idle_detector as idl, power_events
    from windows_fakes import FakeWin32
    api = FakeWin32()
    api.add_window(1, 2, image=r"C:\x\a.exe")
    api.foreground = 1
    assert wd.WindowsDetector(api, own_pids=set()).get_focused().identifier == "a"
    idl.WindowsIdleDetector(api).get_idle_seconds()
    src = power_events.WindowsPowerEventSource(lambda f, w=False: f())
    src._on_suspend = lambda: None
    src.handle_message(0, 0x0218, 4, 0)
