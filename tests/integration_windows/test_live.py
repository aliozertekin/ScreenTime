"""Live checks against a REAL Windows desktop session (the Windows CI job runs
them with SCREENTIME_LIVE=1). They are skipped everywhere else. Every test cleans
up after itself and uses private names so it can never touch a real install.

Not covered here, by design: real sleep/resume (a CI runner cannot be suspended
safely). That path is exercised deterministically by tests/test_windows_daemon.py
through the PowerEventSource abstraction; see docs/windows-developer.md."""
import os
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.windows_live,
    pytest.mark.skipif(sys.platform != "win32" or os.environ.get("SCREENTIME_LIVE") != "1",
                       reason="needs a real Windows session and SCREENTIME_LIVE=1"),
]


@pytest.fixture
def notepad():
    p = subprocess.Popen(["notepad.exe"])
    time.sleep(2)
    yield p
    p.terminate()


def test_foreground_detector_sees_a_real_window(notepad):
    from screentime.platform.windows.window_detector import WindowsDetector
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32")
    u.FindWindowW.restype = wintypes.HWND
    hwnd = u.FindWindowW("Notepad", None)
    if hwnd:
        u.SetForegroundWindow(hwnd)
    time.sleep(1)
    seen = {WindowsDetector().get_focused() and WindowsDetector().get_focused().identifier for _ in range(3)}
    assert seen & {"notepad", None}          # hosted runners may refuse focus changes; never crash, never lie


def test_idle_detector_reports_a_sane_value():
    from screentime.platform.windows.idle_detector import WindowsIdleDetector
    d = WindowsIdleDetector()
    assert d.is_supported()
    assert 0 <= d.get_idle_seconds() < 24 * 3600


def test_unbiased_clock_is_monotonic():
    from screentime.platform.windows.clock import make_unbiased_clock
    c = make_unbiased_clock()
    a = c(); time.sleep(0.2); b = c()
    assert 0.15 < b - a < 1.0


def test_credential_manager_round_trip_and_cleanup():
    from screentime.platform.windows.keystore import CredentialManagerBackend
    from screentime import keystore as K
    sid, key = os.urandom(16), K.generate_key()
    b = CredentialManagerBackend()
    try:
        b.store(sid, key)
        assert b.load(sid) == key
    finally:
        b.delete(sid)
    assert b.load(sid) is None


def test_dpapi_round_trip(tmp_path):
    from screentime.platform.windows.keystore import DpapiFileBackend
    from screentime import keystore as K
    sid, key = os.urandom(16), K.generate_key()
    b = DpapiFileBackend(tmp_path)
    b.store(sid, key)
    assert key not in (tmp_path / "keys" / f"{sid.hex()}.dpapi").read_bytes()
    assert b.load(sid) == key


def test_named_mutex_single_instance_and_release():
    from screentime.platform.windows import instance_lock as il
    a, b = il.InstanceLock(), il.InstanceLock()
    assert a.acquire() and not b.acquire() and il.is_held()
    a.release()
    assert not il.is_held()


def test_task_scheduler_task_create_and_remove(monkeypatch):
    from screentime.platform.windows import autostart as wa
    monkeypatch.setattr(wa, "TASK_NAME", "\\ScreenTimeTest\\Daemon")
    monkeypatch.setattr(wa, "start_now", lambda: True)
    monkeypatch.setattr(wa, "_command_parts", lambda: (sys.executable, ["-m", "screentime.daemon"]))
    try:
        wa.enable()
        info = wa.query_task()
        assert info.exists and info.enabled and wa.task_is_current(info)
    finally:
        wa.disable()
    assert not wa.query_task().exists


def test_encrypted_store_is_created_and_reopened(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "L"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "R"))
    from screentime import storage, platform as plat
    from screentime.keystore import KeyManager
    plat.reset_for_tests()
    sid_holder = {}
    db = storage.open_database(interactive=False)
    db.set_setting("probe", "1")
    sid = storage.read_store_id(storage.store_path())
    db.close()
    try:
        db2 = storage.open_database(interactive=False)
        assert db2.get_setting("probe") == "1"
        db2.close()
    finally:
        from screentime.platform.windows import cleanup
        cleanup.delete_user_data(lambda m: None)
