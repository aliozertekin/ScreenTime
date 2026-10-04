# ScreenTime on Windows

Back to the [README](../README.md). Developer details: [windows-developer.md](windows-developer.md).

Supported: 64-bit **Windows 10 and 11**. No administrator rights needed.

> **Status: release candidate.** The Windows code is covered by automated tests that simulate
> Windows, but it has not yet been run on a real Windows machine. Expect rough edges and please
> report them. Details: "Verification status" in [windows-developer.md](windows-developer.md).

## Three ways to get it running

### 1. The installer (easiest)

Download `ScreenTime-<version>-setup.exe` from the project's **Releases** page, run it, then open
**ScreenTime** from the Start menu. A portable `ScreenTime-<version>-portable.zip` is also built:
unzip it anywhere and double-click **`ScreenTime.cmd`** inside the unzipped folder.

**These files do not exist in the source code.** `ScreenTime.cmd` and the installer are *produced by the
build* (see way 3, a Linux build). If you downloaded the source from GitHub ("Code → Download ZIP" or `git clone`) you will
not find them. Use way 2 or build them.

The installer is unsigned, so SmartScreen may say "unknown publisher": choose *More info → Run anyway*.

### 2. Run straight from the source (works today, no build)

1. Install **MSYS2** from <https://www.msys2.org> (default folder `C:\msys64`).
2. Open a normal Command Prompt or PowerShell **in the ScreenTime folder** and run:

   ```bat
   scripts\windows-dev.cmd setup
   scripts\windows-dev.cmd gui
   ```

   `setup` is one-time (downloads GTK4, libadwaita and Python packages, then installs ScreenTime into
   that MSYS2 Python). `gui` opens the dashboard. Other commands:

   | Command | What it does |
   | --- | --- |
   | `scripts\windows-dev.cmd gui` | open the dashboard |
   | `scripts\windows-dev.cmd daemon` | run the background tracker once, without a window |
   | `scripts\windows-dev.cmd diagnose` | print the diagnostics report |
   | `scripts\windows-dev.cmd test` | run the test suite |

   If MSYS2 is somewhere else, set `SCREENTIME_MSYS2` first (for example `set SCREENTIME_MSYS2=D:\msys64`).
   After you update the source (`git pull`), run `setup` again so the startup task uses the new code.

### 3. Build the installer yourself (from Linux)

Releases are built by the maintainer on a Linux machine; there is no need to build on Windows. With podman or docker installed:

```bash
./scripts/build-windows.sh
```

Output (in `dist/`): `ScreenTime-<version>-setup.exe`, `ScreenTime-<version>-portable.zip`, `build-manifest.txt` and
`SHA256SUMS`. `ScreenTime.cmd` is inside the zip (and inside the installed folder). Details and troubleshooting:
[RELEASING.md](RELEASING.md).

## First launch

1. Open ScreenTime (Start menu, or `scripts\windows-dev.cmd gui`).
2. **Settings → "Start tracking automatically at login"**: turn it on. This starts the tracker now and
   registers a per-user Task Scheduler task (`\ScreenTime\Daemon`) so it starts every time you sign in.
3. Close the window whenever you like. Tracking continues in the background (the tray icon reopens it).

If the startup task is later deleted, or points at an old install folder, ScreenTime repairs it the next
time you open it, but only if you had turned startup on. Turning it off removes the task and stops the tracker.

## Where things are

| What | Where |
| --- | --- |
| Usage history (encrypted) | `%LOCALAPPDATA%\ScreenTime\screentime.sec` |
| Daemon log (no app names) | `%LOCALAPPDATA%\ScreenTime\logs\daemon.log` |
| Non-secret settings | `%APPDATA%\ScreenTime\security.json` |
| Encryption key | Windows Credential Manager (fallback: a DPAPI-protected file in `%APPDATA%\ScreenTime\keys`) |

Keep the **recovery key** from Settings → Security somewhere safe. The Windows key protection is tied to your
Windows account: it keeps the data safe at rest (copied disk, other accounts, backups) but is not a defence
against malware running as you. See [SECURITY.md](SECURITY.md).

## Diagnostics

Settings → Diagnostics, or run `scripts\windows-dev.cmd diagnose` (installed builds: *ScreenTime
diagnostics* in the Start menu). It shows whether the tracker is installed and running, the startup
mechanism, the foreground / idle / power-event backends, the store and key backend, Steam status and the
last sleep and wake. It never prints keys, recovery keys, credentials or window titles.

## Uninstall

* **Installed build:** Settings → Apps → ScreenTime. It stops the tracker and removes the startup task and the
  program. It asks whether to also delete your history and key; the default is **keep**. Silent uninstall keeps
  data unless you add `/DELETEDATA=1`.
* **Run from source:** in the app turn off *Start tracking automatically at login*, then delete the folder.
  To also delete your data, remove `%LOCALAPPDATA%\ScreenTime` and `%APPDATA%\ScreenTime`; without the key
  entry in Credential Manager the data is unreadable anyway.

## Known limitations

* Elevated (run-as-administrator) windows and some protected/anti-cheat processes cannot be identified. While
  one is focused ScreenTime records **no data** instead of guessing.
* The desktop, taskbar, lock screen and ScreenTime's own window are not counted.
* Locking the PC or turning the display off counts as idle (after the idle timeout), not as sleep. Only real
  suspend/hibernate is sleep.
* With Windows "Modern Standby" the sleep notification can be missed. Sleep time is still never counted (a
  sleep-excluding clock plus a drift check), but the open session may close a few seconds late.
* 64-bit x86 only for now.
