# ScreenTime

**A private, local screen-time tracker for Linux and Windows.**

ScreenTime shows how much time you actually spend using your desktop applications.

It tracks the application you have **focused and in use**, rather than simply counting how long a program is running in the background. Your history is stored locally in SQLite and the application works without an account, cloud service, telemetry, or network connection.

## Features

* Track actual application usage
* Count focused applications rather than background processes
* Exclude idle time
* View daily, weekly, monthly, and all-time statistics
* See usage history by application
* Resolve Steam game names using local Steam files
* Exclude applications from tracking
* Exclude suspended/sleep time
* Continue tracking when the dashboard is closed
* Support X11 and major Wayland environments on Linux, and Windows 10/11
* Store all tracking data locally
* Built-in diagnostics for troubleshooting

## Privacy

ScreenTime is designed to run entirely on your computer.

There is:

* No telemetry
* No analytics service
* No cloud sync
* No user account
* No remote API required for tracking
* No update checker
* No crash-reporting service

Your tracking database is encrypted at rest (AES-256-GCM) and stored at:

| OS | Location |
| --- | --- |
| Linux | `~/.local/share/screentime/screentime.sec` |
| Windows | `%LOCALAPPDATA%\ScreenTime\screentime.sec` |

The encryption key is kept in your system keyring on Linux (Secret Service / KWallet) and in **Windows Credential Manager** on Windows (with a DPAPI-protected key file as a fallback). On Windows both are tied to your Windows account: programs running as you can ask Windows for the key, so this protects the file at rest (backups, other accounts, a copied disk) but is not a defence against malware running as you. Keep the recovery key from Settings → Security somewhere safe; it is the only way to open the data on another machine or account.

On Windows ScreenTime has no network code: no telemetry, no analytics, no update check, no crash reporter and no Steam Web API (game names come from Steam's local files). An automated test fails the build if application code imports a network library or loads a Windows networking DLL. Windows diagnostics (`screentime-diagnose`) never print keys, recovery keys or credentials and do not read window titles.

ScreenTime needs to inspect your desktop environment to determine which application is currently focused. It records application usage sessions locally, but does **not** take screenshots or upload your activity to a server.

[View the source code on GitHub](https://github.com/aliozertekin/ScreenTime)

## Supported platforms

| Environment | Support |
| --- | --- |
| Linux X11 | Supported |
| Linux Sway | Supported |
| Linux Hyprland | Supported |
| Linux GNOME Wayland | Supported (GNOME Shell extension) |
| Linux KDE Wayland | Supported (KWin script) |
| Windows 10/11 (64-bit) | Supported |

### Supported desktops (Linux)

| Environment           | Support               |
| --------------------- | --------------------- |
| X11                   | Supported             |
| Sway                  | Native                |
| Hyprland              | Native                |
| GNOME on Wayland      | GNOME Shell extension |
| KDE Plasma on Wayland | KWin script           |

### Wayland

Wayland intentionally prevents applications from freely inspecting other applications' windows. ScreenTime therefore uses the appropriate mechanism for each supported compositor.

* **Sway / Hyprland** use their native compositor interfaces.
* **GNOME** uses a small GNOME Shell extension.
* **KDE Plasma** uses a small KWin script.

The GNOME and KDE helpers are bundled with ScreenTime and installed automatically when required.

If no supported detection mechanism is available, ScreenTime reports no data rather than guessing.

## Installation

### Arch Linux

Clone the repository and build the package:

```bash
git clone https://github.com/aliozertekin/ScreenTime.git
cd ScreenTime
makepkg -si
```

The package installs ScreenTime together with its desktop entry, icon, systemd service, and Wayland helper components.

### Windows

**Supported versions:** 64-bit Windows 10 and Windows 11. No administrator rights are needed to install or run ScreenTime, and nothing else (Python, GTK, MSYS2) has to be installed.

**Install:** download `ScreenTime-<version>-setup.exe` from the releases page and run it. It installs for your account only and adds a Start Menu entry. A portable `.zip` is also provided (extract it anywhere and run `ScreenTime.cmd`). The installer is not code-signed, so Windows SmartScreen may show an "unknown publisher" warning the first time.

**First launch:** open *ScreenTime* from the Start Menu. Open **Settings** and switch on *Start tracking automatically at login*. This registers a per-user Task Scheduler task (`\ScreenTime\Daemon`) that starts only the background tracker, never the window. You can close the window at any time (or minimise it to the tray); tracking continues.

**Automatic startup:** if the task is later deleted or points at an old install path, ScreenTime repairs it the next time you open it, but only if you had startup enabled. Turning it off removes the task and stops the daemon.

**Data location:** `%LOCALAPPDATA%\ScreenTime` (encrypted store, logs) and `%APPDATA%\ScreenTime` (non-secret settings, key fallback).

**Diagnostics:** Settings → Diagnostics, or run *ScreenTime diagnostics* from the Start Menu (`screentime-diagnose`). It reports the foreground, idle and power-event backends, autostart state, store and key backend, and Steam status, without any secrets.

**Uninstall:** use *Settings → Apps → ScreenTime*. This stops the daemon, removes the startup task and the program files. You are asked whether to also delete your usage history and its encryption key; the default is to **keep** it. Silent uninstalls keep data unless you pass `/DELETEDATA=1`.

**Known limitations**

* Windows with higher privileges than ScreenTime (an elevated admin window, some system or anti-cheat protected processes) cannot be identified. While one is focused, ScreenTime records *no data* rather than guessing.
* The desktop, taskbar, lock screen and ScreenTime's own window are not counted as app usage.
* Locking the PC or turning the display off is handled as idle (after the idle timeout), not as sleep. Only real suspend/hibernate is treated as sleep.
* Windows' newer "Modern Standby" does not always notify desktop apps before sleeping. ScreenTime then uses a sleep-excluding clock and a clock-drift check, so sleep time is still never counted, but the open session may be closed a tick late.
* The Windows key protection is account-bound (see Privacy). There is no ARM64 build yet.

### Manual installation

Install the required dependencies:

```bash
sudo pacman -S --needed \
    python python-psutil python-gobject python-cairo \
    gtk4 libadwaita python-xlib xorg-xprop xprintidle systemd
```

Then run:

```bash
./scripts/install.sh
```

The manual installer installs ScreenTime into your user environment.

## Getting started

Launch the dashboard:

```bash
screentime-gui
```

Then open:

**Settings → Start tracking automatically at login**

Enable the option and ScreenTime will configure the appropriate startup mechanism for your current desktop session.

The background daemon handles tracking independently from the dashboard.

You can close the ScreenTime window at any time and tracking will continue in the background.

## How tracking works

ScreenTime measures **focused application usage**.

For example, if you use Firefox for 20 minutes and then switch to a terminal for 5 minutes, ScreenTime records approximately:

```text
Firefox     20 min
Terminal     5 min
```

A background application that remains open for several hours does not automatically receive several hours of usage.

### Idle time

By default, ScreenTime considers you idle after **5 minutes without input**.

Idle time is not added to application usage.

The timeout can be changed in Settings.

### Multiple windows

Switching between multiple windows belonging to the same application does not create duplicate usage.

For example, switching between several Firefox windows continues to count as Firefox usage.

### Sleep and suspend

Time spent while the computer is suspended is not counted.

When the computer wakes, tracking resumes for the currently focused application. On Windows the open session is closed from the system's suspend notification, and a clock-drift check catches any notification that never arrives.

## Steam games

Steam games can sometimes appear as identifiers such as:

```text
steam_app_2620
```

ScreenTime resolves these into game names using Steam's **local files**.

No Steam Web API is required.

ScreenTime checks common Steam installation locations and configured Steam libraries.

If a game cannot be identified, it falls back to a name such as:

```text
Steam App 2620
```

Tracking continues normally.

To inspect Steam game detection:

```bash
python -m screentime.steam_library
```

## Dashboard

The application provides several views.

### Dashboard

See a summary of your recent screen time.

### Applications

See how much time you've spent in individual applications.

### History

Review your usage over time.

### Statistics

View aggregated usage for:

* Today
* This week
* This month
* All time

### Settings

Configure ScreenTime and inspect its current status.

Settings include:

* Idle timeout
* Poll interval
* Start at login
* Application exclusions
* Minimize to tray
* Diagnostics
* Statistics cache rebuilding

### Themes

Themes are chosen in Settings and apply to every view. On Windows the *System colors* theme follows the Windows light/dark setting and accent colour.

| Theme | Name |
| --- | --- |
| `system` | System colors |
| `default` | ScreenTime |
| `light` | Light |
| `dark` | Dark |
| `gruvbox-dark` | Gruvbox Dark |
| `gruvbox-light` | Gruvbox Light |
| `nord` | Nord |
| `dracula` | Dracula |
| `solarized-dark` | Solarized Dark |
| `solarized-light` | Solarized Light |
| `catppuccin-mocha` | Catppuccin Mocha |
| `catppuccin-latte` | Catppuccin Latte |
| `tokyo-night` | Tokyo Night |
| `one-dark` | One Dark |
| `rose-pine` | Rose Pine |
| `rose-pine-dawn` | Rose Pine Dawn |
| `high-contrast-dark` | High Contrast Dark |
| `high-contrast-light` | High Contrast Light |

## Excluding applications

You can prevent specific applications from being tracked from:

**Settings → Excluded applications**

Excluded applications contribute no usage time to your statistics.

## Troubleshooting

Start with:

**Settings → Diagnostics**

Diagnostics can show:

* Whether the daemon is installed and running
* Whether automatic startup is enabled
* Which detection backend is active
* Whether the Wayland helper is installed
* The running daemon version
* Recent suspend/resume information

For more detailed diagnostics:

```bash
./scripts/diagnose.sh
```

By default, the diagnostic tool performs a short live test while the daemon runs in verbose mode. You can switch between applications during the test to verify that focus events are being detected.

For passive diagnostics without the live test:

```bash
./scripts/diagnose.sh --no-live
```

To specify an output file:

```bash
./scripts/diagnose.sh --output /path/to/report.txt
```

**Review diagnostic output before sharing it publicly.** It can contain application names, timestamps, desktop/session information, logs, and information from your local ScreenTime database.

### Tracking does not start after login

Check the daemon:

```bash
systemctl --user status screentime-daemon
```

Then check whether the current session reaches the systemd graphical target:

```bash
systemctl --user is-active graphical-session.target
```

You can also toggle:

**Settings → Start tracking automatically at login**

off and on again to let ScreenTime select the appropriate startup mechanism.

### The dashboard closes and tracking stops

The dashboard and tracker are separate processes.

Check:

```bash
systemctl --user status screentime-daemon
```

The daemon should continue running after `screentime-gui` is closed.

### KDE Wayland is not tracking

Check:

**Settings → Diagnostics → Wayland focus helper**

For additional KDE logging:

```bash
journalctl -f SYSLOG_IDENTIFIER=kwin_wayland
```

### GNOME Wayland is not tracking

The GNOME Shell extension may require a logout/login after its first installation.

Check the helper status under:

**Settings → Diagnostics**

## Data storage

ScreenTime stores its data locally:

```text
Linux:   ~/.local/share/screentime/screentime.sec
Windows: %LOCALAPPDATA%\ScreenTime\screentime.sec
```

The database (encrypted at rest) contains your application usage history and settings.

There is no separate online account or cloud history.

The usage history is stored as individual sessions. A cached daily summary is maintained for fast statistics and can be rebuilt from the session history.

## Updates

When ScreenTime is updated through the Arch package, the running background daemon is restarted when appropriate so that the new version takes effect.

The dashboard also checks that a running daemon is not older than the installed application.

## Uninstalling

To uninstall ScreenTime while keeping your usage data:

```bash
./scripts/uninstall.sh --keep-data
```

To remove ScreenTime and its stored data:

```bash
./scripts/uninstall.sh --yes
```

To see what would be removed without making changes:

```bash
./scripts/uninstall.sh --dry-run
```

If ScreenTime was installed using the Arch package:

```bash
sudo pacman -Rns screentime
```

The uninstall script handles remaining per-user files such as autostart configuration and Wayland helpers.

On Windows, uninstall from *Settings → Apps → ScreenTime* (see the Windows section above); the dialog lets you keep or delete your data.

## Development

ScreenTime is written in Python and uses GTK4 and Libadwaita for its interface.

The project includes an automated test suite covering tracking, persistence, crash recovery, application detection, Wayland helpers, startup behaviour, and other core functionality.

Run the tests with:

```bash
pytest tests/ -v
```

For headless environments:

```bash
xvfb-run -a pytest tests/ -v
```

## Project structure

```text
screentime/
├── screentime/
│   ├── daemon.py
│   ├── session_manager.py
│   ├── window_detector.py
│   ├── idle_detector.py
│   ├── app_identity.py
│   ├── steam_library.py
│   ├── db.py
│   ├── stats.py
│   ├── config.py
│   ├── autostart.py
│   ├── wayland_setup.py
│   └── gui/
├── data/
├── scripts/
├── tests/
├── PKGBUILD
└── pyproject.toml
```

## License

ScreenTime is released under the **MIT License**.

If you find a bug or have an idea for improving ScreenTime, please open an issue on GitHub.

[GitHub repository](https://github.com/aliozertekin/ScreenTime)
