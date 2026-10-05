# ScreenTime

**A private, local screen-time tracker for Linux and Windows.**

ScreenTime shows how much time you actually spend in each desktop application. It counts the app you have
**focused and are using**, not programs that merely sit open in the background. Your history stays on your
computer, encrypted, with no account, cloud service or telemetry. ScreenTime tracks fully offline; the only
network use is an update check that you start yourself.

**Version 1.5.0** · [Changelog](CHANGELOG.md) · MIT licence

## What you get

* Real usage time per app: focused and active only. Idle time and sleep time are never counted
* A dashboard with today, the last 7 and 30 days, your most-used apps, daily trends and a comparison with the previous period
* Optional daily goals (overall and per app) with a warning threshold and desktop notifications; they only notify, never block anything
* Daily, weekly, monthly and all-time statistics, plus per-app history
* Encrypted backups (restore your history on another computer) and plain CSV/JSON export
* Keeps tracking after you close the window (a small background tracker does the work)
* Steam game names, read from your local Steam files (no internet)
* An optional, manual "Check for updates" button (see Privacy)
* Exclude apps you don't want tracked
* 18 colour themes, including light, dark and high-contrast
* Your data is encrypted on disk, and built-in diagnostics help when something isn't working

## Supported platforms

| Environment | Support |
| --- | --- |
| Linux X11 | Supported |
| Linux Sway | Supported |
| Linux Hyprland | Supported |
| Linux GNOME Wayland | Supported (GNOME Shell extension, installed automatically) |
| Linux KDE Wayland | Supported (KWin script, installed automatically) |
| Windows 10 / 11 (64-bit) | Supported: installer or portable zip; every release is smoke-tested on a real Windows runner in CI |

## Install

### Windows

Releases are built and published automatically by GitHub Actions when a version tag is pushed. Windows users just download `ScreenTime-<version>-setup.exe` (installer) or `ScreenTime-<version>-portable.zip` from the GitHub **Releases** page. The full guide is **[docs/WINDOWS.md](docs/WINDOWS.md)**; the options:

| If you want to… | Do this |
| --- | --- |
| Just install it | Download `ScreenTime-<version>-setup.exe` (or the portable zip) from the GitHub **Releases** page, and check it against `SHA256SUMS` (below) |
| Run it from this source code today | Install [MSYS2](https://www.msys2.org), then run `scripts\windows-dev.cmd setup` and `scripts\windows-dev.cmd gui` |
| Build the installer yourself (on Linux) | `./scripts/build-windows.sh`, which needs only podman or docker ([docs/RELEASING.md](docs/RELEASING.md)) |

> The installer and `ScreenTime.cmd` are **created by the build**. They are not in the source download, so
> they won't be there if you only cloned or downloaded the repository.

### Arch Linux

```bash
git clone https://github.com/aliozertekin/ScreenTime.git
cd ScreenTime
makepkg -si
```

### Other Linux (manual)

```bash
sudo pacman -S --needed python python-psutil python-gobject python-cairo \
    gtk4 libadwaita python-xlib xorg-xprop xprintidle systemd
./scripts/install.sh
```

More options, other desktops and troubleshooting: **[docs/LINUX.md](docs/LINUX.md)**.

### Verify your download

Each release lists `SHA256SUMS`. Put it in the same folder as the files you downloaded, then:

```bash
sha256sum -c SHA256SUMS --ignore-missing          # Linux / macOS / Git Bash
```
```powershell
Get-FileHash .\ScreenTime-<version>-setup.exe -Algorithm SHA256   # compare with the line in SHA256SUMS
```

The checksums prove your download matches what the release build produced. They do not prove who built it:
the installer is not code-signed, so SmartScreen may warn.

## Getting started

1. Open ScreenTime (`screentime-gui` on Linux, the Start menu on Windows).
2. Go to **Settings → Start tracking automatically at login** and switch it on.
3. Close the window whenever you like. Tracking continues, and the same data is there when you reopen it.

## How it works

* A background **tracker** notices which window has focus and writes usage sessions to a local store.
* The **dashboard** only reads that store, so closing it never stops tracking.
* Going idle stops the clock at your last keypress or mouse move, and sleeping or suspending the computer is
  never counted.
* If a tracker can't tell what is focused (for example a protected window), it records nothing rather than guessing.

Deeper explanation: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Privacy

Everything stays on your computer. There is no telemetry, analytics, cloud sync, account or crash reporter.
Tracking, statistics, goals, notifications, backups and exports work completely offline and never use the network.
ScreenTime does not take screenshots or record window titles.

The **only** network feature is **Settings → Updates → Check now**. It runs only when you press the button
(never in the background or at start-up), makes one HTTPS request to `api.github.com` for the newest release
number of this project, and sends no usage data or settings (GitHub sees your IP address, as with any web
request). Nothing is downloaded or installed. You can switch it off completely under *Allow update checks*.
Automated tests enforce that no other part of ScreenTime can open a network connection.

**Backups and exports.** *Export encrypted backup* writes a `.screentime` file protected with your existing key;
opening it elsewhere needs your recovery key. *Import backup* only **adds** missing sessions and never overwrites or
deletes your history. *Export usage data* (CSV/JSON) is **not encrypted** and lists which apps you used and when, so
store it carefully; it never contains keys. Files stay where you save them; ScreenTime does not upload them.

| OS | Encrypted data file | Encryption key kept in |
| --- | --- | --- |
| Linux | `~/.local/share/screentime/screentime.sec` | your system keyring (KWallet / GNOME Keyring), else a key file |
| Windows | `%LOCALAPPDATA%\ScreenTime\screentime.sec` | Windows Credential Manager, else a DPAPI-protected file |

Save the **recovery key** from Settings → Security somewhere safe. What is and is not protected:
[docs/SECURITY.md](docs/SECURITY.md).

## Something not working?

Start with **Settings → Diagnostics**. On Windows you can also run `scripts\windows-dev.cmd diagnose`
(or `screentime-diagnose`); on Linux run `./scripts/diagnose.sh`. Reports can include app names and
timestamps, so skim them before sharing. Common fixes are in [docs/LINUX.md](docs/LINUX.md#troubleshooting)
and [docs/WINDOWS.md](docs/WINDOWS.md).

## Uninstall

* **Linux:** `./scripts/uninstall.sh --keep-data` (keeps your history) or `./scripts/uninstall.sh --yes`
  (removes everything). Arch package: `sudo pacman -Rns screentime`.
* **Windows:** Settings → Apps → ScreenTime. It asks whether to keep or delete your history (default: keep).

## Themes

Choose a theme in **Settings → Appearance**; it applies instantly. "System colors" follows your desktop
(KDE colour scheme on Plasma, the Windows light/dark setting and accent colour on Windows).

| Theme id | Name |
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

More on themes and custom accents: [docs/THEMES.md](docs/THEMES.md).

## Documentation

| Guide | For |
| --- | --- |
| [docs/WINDOWS.md](docs/WINDOWS.md) | Installing, running and uninstalling on Windows |
| [docs/LINUX.md](docs/LINUX.md) | Linux setup, Wayland, start-at-login, sleep, troubleshooting |
| [docs/STEAM.md](docs/STEAM.md) | How Steam game names are found (offline) |
| [docs/THEMES.md](docs/THEMES.md) | Themes, KDE colours, adding a theme |
| [docs/SECURITY.md](docs/SECURITY.md) | Encryption, key storage, recovery, limits |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it is built; timing accuracy; settings reference |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Building, tests, versioning |
| [docs/RELEASING.md](docs/RELEASING.md) | Maintainers: publish a release with one tag, reproducible Windows builds |
| [docs/windows-developer.md](docs/windows-developer.md) | Windows internals and verification status |

## Contributing and licence

Bug reports and ideas are welcome: please [open an issue](https://github.com/aliozertekin/ScreenTime/issues).
ScreenTime is released under the **MIT License**.
