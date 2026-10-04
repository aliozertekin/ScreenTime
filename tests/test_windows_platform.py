"""Windows paths, instance lock, power events, theme, app names, process identity."""
import os

import pytest

from screentime import platform as plat
from screentime.platform.windows import instance_lock as wl
from screentime.platform.windows import power_events as pe
from screentime.platform.windows import app_info, theme as wtheme
from screentime.platform.windows.paths import WindowsPaths
from screentime.platform.windows import win32 as w
from windows_fakes import FakeWin32


# ---------------------------------------------------------------------- paths
def test_paths_use_local_and_roaming_app_data_not_hardcoded_profiles(tmp_path, monkeypatch):
    local, roaming = tmp_path / "Ünal Çelik" / "Local Data", tmp_path / "Ünal Çelik" / "Roaming Data"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    p = WindowsPaths()
    assert p.data_dir() == local / "ScreenTime" and p.data_dir().is_dir()
    assert p.config_dir() == roaming / "ScreenTime"
    assert p.runtime_dir() == local / "ScreenTime" / "run"
    assert p.log_dir() == local / "ScreenTime" / "logs"
    assert not str(p.config_dir()).startswith(str(p.data_dir()))        # key material never inside the data dir


def test_store_path_on_windows_is_screentime_sec_in_local_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENTIME_PLATFORM", "windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "L"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "R"))
    plat.reset_for_tests()
    try:
        from screentime import storage
        assert storage.store_path() == tmp_path / "L" / "ScreenTime" / "screentime.sec"
    finally:
        monkeypatch.delenv("SCREENTIME_PLATFORM")
        plat.reset_for_tests()


def test_platform_selection_defaults_to_the_host_os(monkeypatch):
    monkeypatch.delenv("SCREENTIME_PLATFORM", raising=False)
    plat.reset_for_tests()
    assert plat.current().name == ("windows" if os.name == "nt" else "linux")


# ----------------------------------------------------------------- instance lock
def test_only_one_daemon_may_hold_the_lock():
    api = FakeWin32()
    a, b = wl.InstanceLock(api=api), wl.InstanceLock(api=api)
    assert a.acquire() is True
    assert b.acquire() is False                      # second daemon exits cleanly
    assert wl.is_held(api=api)
    a.release()
    assert not wl.is_held(api=api)
    assert b.acquire() is True


def test_crashed_holder_releases_the_lock_automatically():
    api = FakeWin32()
    a = wl.InstanceLock(api=api)
    assert a.acquire()
    api.crash()                                      # Windows closes the dead process's handles
    assert wl.InstanceLock(api=api).acquire() is True


def test_probe_does_not_take_the_lock():
    api = FakeWin32()
    assert wl.is_held(api=api) is False
    assert wl.is_held(api=api) is False
    assert wl.InstanceLock(api=api).acquire() is True        # probing didn't steal it


def test_graceful_stop_event_reaches_the_holder():
    api = FakeWin32()
    lock = wl.InstanceLock(api=api)
    lock.acquire()
    assert lock.stop_requested() is False
    assert wl.request_stop(api=api) is True
    assert lock.stop_requested() is True
    assert wl.request_stop(api=FakeWin32()) is False         # nobody running -> not delivered


def test_mutex_name_is_per_user_and_safe():
    assert wl.mutex_name().startswith("Local\\ScreenTimeDaemon-")


# ----------------------------------------------------------------- power events
class Recorder:
    def __init__(self):
        self.calls = []
    def make(self):
        events = {}
        def dispatch(fn, wait=False):
            self.calls.append((fn.__name__, wait))
            fn()
        src = pe.WindowsPowerEventSource(dispatch, clock=lambda: self.now)
        self.now = 0.0
        def suspend(): events.setdefault("log", []).append("suspend")
        def resume(): events.setdefault("log", []).append("resume")
        def end(): events.setdefault("log", []).append("end")
        src._on_suspend, src._on_resume, src._on_end = suspend, resume, end
        self.log = lambda: events.get("log", [])
        return src


def test_suspend_is_handled_synchronously_before_windows_proceeds():
    r = Recorder(); s = r.make()
    assert s.handle_message(0, w.WM_POWERBROADCAST, w.PBT_APMSUSPEND, 0) == 1
    assert r.log() == ["suspend"] and r.calls == [("suspend", True)]       # wait=True: the thread blocks


def test_resume_pair_is_deduplicated_but_a_later_resume_is_not():
    r = Recorder(); s = r.make()
    s.handle_message(0, w.WM_POWERBROADCAST, w.PBT_APMRESUMEAUTOMATIC, 0)
    s.handle_message(0, w.WM_POWERBROADCAST, w.PBT_APMRESUMESUSPEND, 0)    # arrives right after
    assert r.log() == ["resume"]
    r.now = 600.0
    s.handle_message(0, w.WM_POWERBROADCAST, w.PBT_APMRESUMEAUTOMATIC, 0)
    assert r.log() == ["resume", "resume"]


def test_lock_display_and_unrelated_messages_are_not_sleep():
    r = Recorder(); s = r.make()
    WM_WTSSESSION_CHANGE, WM_SYSCOMMAND, WM_DISPLAYCHANGE = 0x02B1, 0x0112, 0x007E
    for msg in (WM_WTSSESSION_CHANGE, WM_SYSCOMMAND, WM_DISPLAYCHANGE):
        assert s.handle_message(0, msg, 7, 0) is None
    # a power-setting change (e.g. display off) is not suspend either
    assert s.handle_message(0, w.WM_POWERBROADCAST, 0x8013, 0) == 1
    assert r.log() == []


def test_shutdown_is_never_vetoed_and_end_session_closes_cleanly():
    r = Recorder(); s = r.make()
    assert s.handle_message(0, w.WM_QUERYENDSESSION, 0, 0) == 1
    s.handle_message(0, w.WM_ENDSESSION, 0, 0)                 # session NOT ending (shutdown cancelled)
    assert r.log() == []
    s.handle_message(0, w.WM_ENDSESSION, 1, 0)
    assert r.log() == ["end"]


def test_dispatcher_runs_on_main_loop_and_bounds_the_wait():
    ran = []
    pending = []
    d = pe.make_dispatcher(lambda fn: pending.append(fn), wait_seconds=0.05)
    d(lambda: ran.append(1), True)                  # main loop never runs it -> wait times out, no hang
    assert ran == [] and len(pending) == 1
    pending.pop()()
    assert ran == [1]


def test_source_reports_failure_when_the_window_cannot_be_created():
    class Dead:
        error = "RegisterClassW failed"
        def start(self): return False
        def stop(self): pass
    s = pe.WindowsPowerEventSource(lambda f, w=False: f(), window_factory=lambda h: Dead())
    assert s.start(lambda: None, lambda: None, lambda: None) is False
    assert "RegisterClassW" in s.last_error


# ------------------------------------------------------------------------ theme
def test_accent_colour_is_decoded_from_abgr():
    assert wtheme.abgr_to_hex(0xFFD77800) == "#0078d7"
    assert wtheme.abgr_to_hex(None) is None and wtheme.abgr_to_hex(-1) is None


def test_dark_light_from_registry_value():
    assert wtheme.system_is_dark(lambda k, n: 0) is True
    assert wtheme.system_is_dark(lambda k, n: 1) is False
    assert wtheme.system_is_dark(lambda k, n: None) is None
    assert wtheme.system_accent(lambda k, n: None) is None


# --------------------------------------------------------------------- app names
def test_friendly_names_from_version_resources():
    assert app_info.pick_name({"FileDescription": "Firefox", "ProductName": "Firefox"}, "firefox") == "Firefox"
    assert app_info.pick_name({"FileDescription": "", "ProductName": "Visual Studio Code"}, "code") == "Visual Studio Code"
    assert app_info.pick_name({"FileDescription": "Application"}, "x") is None
    assert app_info.pick_name({}, "x") is None


def test_friendly_name_lookup_is_cached_and_tolerates_missing_files(tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"MZ")
    calls = []
    reader = lambda p: calls.append(p) or {"FileDescription": "Google Chrome"}   # noqa: E731
    app_info._cache.clear()
    assert app_info.friendly_name_for_exe(str(exe), reader) == "Google Chrome"
    assert app_info.friendly_name_for_exe(str(exe), reader) == "Google Chrome"
    assert len(calls) == 1
    assert app_info.friendly_name_for_exe(str(tmp_path / "gone.exe"), reader) is None


def test_windows_identity_keeps_key_and_uses_hint_as_display(monkeypatch):
    monkeypatch.setenv("SCREENTIME_PLATFORM", "windows")
    plat.reset_for_tests()
    try:
        from screentime import app_identity
        app_identity._desktop_file_index.cache_clear()
        r = app_identity.resolve("firefox", fallback_display="Firefox")
        assert (r.key, r.display_name) == ("firefox", "Firefox")
        r = app_identity.resolve("code", fallback_display="Visual Studio Code")
        assert (r.key, r.display_name) == ("code", "Visual Studio Code")
        assert app_identity.resolve("weirdtool").display_name == "Weirdtool"         # no hint -> prettified exe
    finally:
        monkeypatch.delenv("SCREENTIME_PLATFORM")
        plat.reset_for_tests()
        app_identity._desktop_file_index.cache_clear()


def test_process_presence_uses_the_same_identifier_as_the_foreground_detector(monkeypatch):
    monkeypatch.setenv("SCREENTIME_PLATFORM", "windows")
    plat.reset_for_tests()
    try:
        from screentime import process_monitor as pm
        assert pm.process_identifier_from_name("Firefox.EXE") == "firefox"
        assert pm._WINDOWS_NOISE >= {"svchost", "dwm", "explorer"}
    finally:
        monkeypatch.delenv("SCREENTIME_PLATFORM")
        plat.reset_for_tests()


def test_windows_modules_import_without_a_windows_host():
    import importlib
    for m in ("win32", "window_detector", "idle_detector", "power_events", "instance_lock", "keystore",
              "autostart", "paths", "app_info", "theme", "clock", "msgwindow", "tray", "logging_setup"):
        importlib.import_module(f"screentime.platform.windows.{m}")
    with pytest.raises(RuntimeError):
        w.Win32Api()                                  # the real API refuses to exist off Windows
