"""Windows foreground / idle / clock logic against a fake Win32 layer."""
import pytest

from screentime.platform.windows import window_detector as wd
from screentime.platform.windows.clock import make_unbiased_clock
from screentime.platform.windows.idle_detector import WindowsIdleDetector, idle_milliseconds
from screentime.platform.windows.win32 import ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, Win32Error
from windows_fakes import FakeWin32

FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"


@pytest.fixture
def api():
    return FakeWin32()


def det(api, **kw):
    return wd.WindowsDetector(api, own_pids={9999}, **kw)


def test_exe_stem_is_the_identity_not_the_title():
    assert wd.exe_stem(FIREFOX) == "firefox"
    assert wd.exe_stem(r"C:\Users\Ünal Çelik\AppData\Local\Programs\Code.exe") == "code"
    assert wd.exe_stem("C:/x y/Some Tool.EXE") == "some tool"


def test_foreground_resolves_pid_and_executable(api):
    api.add_window(10, pid=4321, image=FIREFOX)
    api.foreground = 10
    f = det(api).get_focused()
    assert (f.identifier, f.pid, f.title) == ("firefox", 4321, None)      # titles are never read


def test_friendly_name_is_a_hint_not_the_identity(api):
    api.add_window(10, pid=1, image=FIREFOX)
    api.foreground = 10
    f = det(api, name_for_exe=lambda p: "Firefox").get_focused()
    assert f.identifier == "firefox" and f.display_hint == "Firefox"


def test_no_foreground_window_is_no_data(api):
    api.foreground = 0
    assert det(api).get_focused() is None


def test_foreground_api_failure_is_no_data_not_a_crash(api):
    api.fail_foreground = True
    d = det(api)
    assert d.get_focused() is None and "GetForegroundWindow" in d.last_error


def test_window_that_vanished_between_calls(api):
    api.foreground = 77                      # not in api.windows -> invalid handle
    assert det(api).get_focused() is None


def test_process_that_exited_before_the_path_query(api):
    api.add_window(10, pid=5, image=Win32Error(ERROR_INVALID_PARAMETER, "OpenProcess"))
    api.foreground = 10
    assert det(api).get_focused() is None


def test_protected_process_is_unknown_never_the_previous_app(api):
    api.add_window(10, pid=1, image=FIREFOX)
    api.add_window(11, pid=2, image=Win32Error(ERROR_ACCESS_DENIED, "OpenProcess"))
    d = det(api)
    api.foreground = 10
    assert d.get_focused().identifier == "firefox"
    api.foreground = 11
    assert d.get_focused() is None           # NOT firefox
    assert "QueryFullProcessImageNameW" in d.last_error


@pytest.mark.parametrize("cls", ["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"])
def test_desktop_and_taskbar_are_not_an_app(api, cls):
    api.add_window(10, pid=3, cls=cls, image=r"C:\Windows\explorer.exe")
    api.foreground = 10
    assert det(api).get_focused() is None


def test_file_explorer_window_is_tracked(api):
    api.add_window(10, pid=3, cls="CabinetWClass", image=r"C:\Windows\explorer.exe")
    api.foreground = 10
    assert det(api).get_focused().identifier == "explorer"


def test_uwp_app_inside_application_frame_host(api):
    api.add_window(10, pid=100, cls="ApplicationFrameWindow", image=r"C:\Windows\System32\ApplicationFrameHost.exe",
                   children=[11, 12])
    api.add_window(11, pid=100, cls="Windows.UI.Core.CoreWindow")                 # still the host
    api.add_window(12, pid=200, cls="Windows.UI.Core.CoreWindow",
                   image=r"C:\Program Files\WindowsApps\Calc\CalculatorApp.exe")
    api.foreground = 10
    f = det(api).get_focused()
    assert (f.identifier, f.pid) == ("calculatorapp", 200)


def test_uwp_host_with_no_resolvable_child_is_no_data(api):
    api.add_window(10, pid=100, image=r"C:\Windows\System32\ApplicationFrameHost.exe", children=[])
    api.foreground = 10
    assert det(api).get_focused() is None


def test_never_attributes_time_to_screentime_itself(api):
    api.add_window(10, pid=9999, image=r"C:\Python\pythonw.exe")                 # the daemon's own pid
    api.add_window(11, pid=8888, image=r"C:\Program Files\ScreenTime\bin\ScreenTime.exe")   # the GUI process
    d = det(api)
    api.foreground = 10
    assert d.get_focused() is None
    api.foreground = 11
    assert d.get_focused() is None


@pytest.mark.parametrize("exe", ["LogonUI.exe", "LockApp.exe"])
def test_lock_screen_is_not_usage(api, exe):
    api.add_window(10, pid=5, image=rf"C:\Windows\System32\{exe}")
    api.foreground = 10
    assert det(api).get_focused() is None


def test_steam_game_uses_the_stable_steam_app_key(api):
    api.add_window(10, pid=1, image=r"D:\SteamLibrary\steamapps\common\Portal 2\portal2.exe")
    api.foreground = 10
    f = det(api, steam_key_for_exe=lambda p: "steam_app_620").get_focused()
    assert f.identifier == "steam_app_620"


def test_broken_steam_or_name_lookup_never_breaks_tracking(api):
    api.add_window(10, pid=1, image=FIREFOX)
    api.foreground = 10
    def boom(_): raise RuntimeError("x")
    f = det(api, name_for_exe=boom, steam_key_for_exe=boom).get_focused()
    assert f.identifier == "firefox"


# ----------------------------------------------------------------------- idle
def test_idle_is_now_minus_last_input(api):
    api.tick, api.last_input = 100_000, 70_000
    assert WindowsIdleDetector(api).get_idle_seconds() == 30.0


def test_idle_survives_dword_tick_rollover(api):
    api.last_input = (1 << 32) - 5_000          # input just before the counter wrapped
    api.tick = (1 << 32) + 7_000                # 12 s later, counter now small again
    assert WindowsIdleDetector(api).get_idle_seconds() == 12.0
    assert idle_milliseconds(2_000, (1 << 32) - 3_000) == 5_000


def test_idle_api_failure_means_active_not_idle(api):
    api.fail_input = True
    d = WindowsIdleDetector(api)
    assert d.get_idle_seconds() == 0.0 and "GetLastInputInfo" in d.last_error
    assert not d.is_supported()


def test_idle_backend_is_named_windows(api):
    assert WindowsIdleDetector(api).name == "windows"
    assert wd.WindowsDetector(api).name == "windows"


# ---------------------------------------------------------------------- clock
def test_unbiased_clock_reads_the_sleep_excluding_counter(api):
    clock = make_unbiased_clock(api)
    api.unbiased = 50.0
    a = clock()
    api.unbiased = 80.0
    assert clock() - a == 30.0


def test_unbiased_clock_falls_back_if_the_call_fails(api):
    api.fail_unbiased = True
    import time
    assert make_unbiased_clock(api) is time.monotonic
    ok = FakeWin32()
    clock = make_unbiased_clock(ok)
    ok.fail_unbiased = True                      # starts failing later
    assert isinstance(clock(), float)
