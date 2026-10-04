# Windows support: developer notes

## Architecture

One central switch, `screentime/platform/__init__.py` (`current()`, `is_windows()`),
decides the platform. Everything else asks it. `SCREENTIME_PLATFORM=windows|linux`
overrides detection for tests only.

| Concept | Linux (unchanged) | Windows |
| --- | --- | --- |
| WindowDetector | `window_detector.py` backends | `platform/windows/window_detector.py` (Win32) |
| IdleDetector | `idle_detector.py` backends | `platform/windows/idle_detector.py` (`GetLastInputInfo`) |
| PowerEventSource | logind code kept inline in `daemon.py` | `platform/windows/power_events.py` (`WM_POWERBROADCAST`) |
| AutostartManager | `autostart.py` (systemd / XDG) | `platform/windows/autostart.py` (Task Scheduler) |
| InstanceLock | `instance_lock.py` (`flock`) | `platform/windows/instance_lock.py` (named mutex) |
| SecureKeyStore | Secret Service, key file | Credential Manager, DPAPI file (`platform/windows/keystore.py`) |
| PlatformPaths | XDG (`platform/base.py`) | `%LOCALAPPDATA%` / `%APPDATA%` (`platform/windows/paths.py`) |
| Diagnostics | `scripts/diagnose.sh` | `screentime/diagnostics.py` (also runs on Linux) |
| Monotonic clock | `time.monotonic` | `QueryUnbiasedInterruptTime` (`platform/windows/clock.py`) |

Linux modules keep their historical names and public functions (existing tests
monkeypatch them); where Windows differs, the module ends with one
`if _platform.is_windows(): from .platform.windows... import ...` block.
`SessionManager`, `db.py`, `secure_log.py`, `protected_store.py`, `stats.py`, the GUI
views and the theme definitions are shared and were not forked.

All Windows modules talk to Windows only through `platform/windows/win32.py`
(`Win32Api`, with correct `argtypes`/`restype`). Tests inject `tests/windows_fakes.py::FakeWin32`,
so the Windows logic runs on any OS. `Win32Api()` refuses to construct off Windows.

### Why the clock is special

`SessionManager` derives session ends from monotonic elapsed time so suspended
time can never become usage. Linux `CLOCK_MONOTONIC` stops during suspend.
`time.monotonic()` on Windows is not guaranteed to, so the Windows platform
supplies `QueryUnbiasedInterruptTime` (documented to exclude sleep/hibernation).
The daemon additionally runs a wall-vs-monotonic drift guard on Windows
(`Daemon._detect_missed_suspend`) for suspend notifications that never arrive.

### Power events

A hidden top-level window on its own thread (`msgwindow.py`) receives
`WM_POWERBROADCAST`/`WM_ENDSESSION`. Suspend is dispatched to the GLib main loop
(SQLite connections are thread-bound) and the message thread waits up to 2 s so the
session is closed before Windows sleeps. Display-off, lock/unlock and idle are
deliberately *not* subscribed: they are not suspend.

### Security review (least privilege)

* No admin/SYSTEM/SeDebugPrivilege anywhere. `OpenProcess` uses only
  `PROCESS_QUERY_LIMITED_INFORMATION`; elevated/protected processes give
  `ERROR_ACCESS_DENIED`, which the detector reports as *no data*.
* The Task Scheduler task runs as the user, `LeastPrivilege`, interactive token, logon trigger for that user only.
* No Windows service. No network DLLs (`tests/test_no_network_windows.py`).
* Daemon file log is capped at INFO so no application names are written in plaintext.

## Running the tests

```
python -m pytest -q                       # everything, any OS (Windows logic uses the fake Win32 layer)
SCREENTIME_LIVE=1 python -m pytest tests/integration_windows   # on a real Windows desktop
```

Not testable in CI: a real suspend/resume. The path is covered deterministically
by `tests/test_windows_daemon.py` (event-driven and missed-event cases) and must be
confirmed by hand on a laptop before a release.

## Building

`./scripts/build-windows.sh` on Linux runs the whole pipeline in a container (`packaging/windows/README.md`, [RELEASING.md](RELEASING.md)). `runtime_env.py` generates the gdk-pixbuf loader cache at first launch because the bundle is relocatable.

## Verification status (honest)

Done and passing on Linux: the full existing suite plus the Windows unit tests against fakes, and tests of the build
tooling (MSYS2 resolver/downloader against a synthetic repository with hash verification, bundle assembly and pruning,
the DLL-import checker on a real PE file, deterministic zip, manifest, shellcheck, failure paths of the host script).

**Not yet done:** nothing here has been executed on real Windows, and the container pipeline itself (image build, real
MSYS2 downloads, Inno Setup under Wine, Wine smoke test) has not been run end to end. Unverified until a real build and a
clean-machine run: the ctypes prototypes against the real DLLs, the message-window/tray threads, `schtasks` XML
acceptance, that the MSYS2 ucrt64 runtime is complete enough (the static DLL check helps but cannot prove GTK works),
the gdk-pixbuf loader-cache workaround, the renamed-`pythonw.exe` launchers, GUI behaviour under Wine, the Inno Setup
script, "Restart Windows -> daemon starts", and real sleep/resume. Treat the Windows release as a release candidate.
