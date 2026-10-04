# ScreenTime

**A private, local screen-time tracker for Linux and Windows.**

ScreenTime shows how much time you actually spend in each desktop application. It counts the app you have
**focused and are using**, not programs that merely sit open in the background. Your history stays on your
computer, encrypted, with no account, cloud service, telemetry or network connection.

**Version 1.4.0** · [Changelog](CHANGELOG.md) · MIT licence

## What you get

* Real usage time per app: focused and active only. Idle time and sleep time are never counted
* Daily, weekly, monthly and all-time statistics, plus per-app history
* Keeps tracking after you close the window (a small background tracker does the work)
* Steam game names, read from your local Steam files (no internet)
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
| Windows 10 / 11 (64-bit) | Supported, **release candidate**: tested with simulated Windows, not yet on real machines |

## Install

### Windows

Releases are built by the project maintainer; Windows users just download `ScreenTime-<version>-setup.exe` from the GitHub **Releases** page and run it. The full guide is **[docs/WINDOWS.md](docs/WINDOWS.md)**; the options:

| If you want to… | Do this |
| --- | --- |
| Just install it | Download `ScreenTime-<version>-setup.exe` from the GitHub **Releases** page (once a release is published) |
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

Everything stays on your computer. There is no telemetry, analytics, cloud sync, account, update checker or
crash reporter, and no network code at all (automated tests enforce this on Linux and Windows). ScreenTime
does not take screenshots or record window titles.

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
| [docs/RELEASING.md](docs/RELEASING.md) | Maintainers: build the Windows installer on Linux, publish a release |
| [docs/windows-developer.md](docs/windows-developer.md) | Windows internals and verification status |

## Contributing and licence

Bug reports and ideas are welcome: please [open an issue](https://github.com/aliozertekin/ScreenTime/issues).
ScreenTime is released under the **MIT License**.
