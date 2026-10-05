# Development

Building, dependencies and tests. Back to the [README](../README.md).

## Runtime dependencies

| Package (Arch) | Why |
|---|---|
| `python` | ≥3.11 |
| `python-psutil` | Process enumeration |
| `python-gobject`, `gtk4`, `libadwaita` | GUI + the daemon's GLib main loop |
| `python-cairo` | Chart widget rendering (GTK4's DrawingArea draw callback) |
| `python-cryptography` | AES-256-GCM for the protected database (official repo, no AUR needed) |
| `python-xlib` | X11 active-window/idle detection (optional but recommended) |
| `xorg-xprop` | X11 fallback if `python-xlib` isn't installed |
| `xprintidle` | X11 idle detection |
| `systemd` | `--user` service management + logind suspend/resume signal |

Optional: `kwallet` / `gnome-keyring` (keep the database key in the system
keyring; any Secret Service provider works), `libayatana-appindicator` (tray icon), `sway`/`hyprland` (native
Wayland detection), `gnome-shell`/`plasma-workspace` (to install the
companion extension/script for GNOME/KDE Wayland detection).

## Development dependencies

`python-build`, `python-installer`, `python-wheel`, `python-setuptools`
(packaging), `pytest` (tests). `nodejs` is optional: with it, the test suite also executes the KWin
scripts against a mocked KWin API (those tests skip without it). The theme
feature adds no runtime dependency.

---

## Building on Arch Linux

### Option A: makepkg (recommended)

`PKGBUILD` builds directly from this source tree (there's no separate
tarball to fetch), so `makepkg -si` must be run from inside it, in the same
directory as `PKGBUILD` itself:

```sh
# if you cloned the repo:
git clone <this-repo> screentime && cd screentime && makepkg -si
# if you extracted a source tarball instead, cd into wherever it extracted to
# (the directory containing PKGBUILD) before running makepkg -si
```

This builds a wheel and installs the systemd service, desktop entry, and
icon. The Wayland companion scripts (GNOME extension / KWin script) are
bundled inside the wheel itself and install+enable themselves automatically
on first run — see "Wayland compatibility" below.

### Option B: manual install (no packaging, good for development)

```sh
sudo pacman -S --needed python python-psutil python-gobject python-cairo \
    gtk4 libadwaita python-xlib xorg-xprop xprintidle systemd
./scripts/install.sh
```

Installs into `~/.local` — no root needed beyond the `pacman` step.

### Running tests

```sh
pip install pytest --break-system-packages   # or: pacman -S python-pytest
pytest tests/ -v
```

Hundreds of unit and integration tests cover the session/timing engine, the DB layer,
window/idle detector parsing against realistic canned compositor output,
the Wayland companion installer, start-at-login logic (against a fake
`systemctl`), real-process daemon lifecycle (second instance, SIGTERM,
SIGKILL-then-restart), and full app-switching / restart / crash /
midnight scenarios end-to-end. A handful that build real GTK widgets need a
display and skip cleanly without one (GTK can segfault rather than raise a
clean exception if built with zero display connection at all) — on a
headless machine or over SSH, run those under `xvfb-run -a pytest tests/ -v`
instead to actually exercise them.

The encryption work is covered by tests of the container (confidentiality,
every tamper case), key management (including a real keyring), migration and
its failure paths, multi-process concurrency, crash safety, and real-daemon
upgrade scenarios. Two extra ways to run the suite:

```sh
SCREENTIME_TEST_PROTECTED=1 pytest tests/    # ENTIRE suite on the encrypted backend
```

Optional tools enable more tests: `nodejs` (executes the KWin scripts), and
`dbus` + `gnome-keyring` (`dbus-run-session`, `gnome-keyring-daemon`) for the
real-keyring integration test, which runs in a private D-Bus session and never
touches your own wallet. Tests never reach a real keyring: the session bus is
pointed at nothing, so keys fall back to a key file in a temporary directory.


## Testing on X11

Just log into an X11 session (or `startx`) and run `screentime-gui` /
`systemctl --user start screentime-daemon`. Detection uses `python-xlib` if
installed, otherwise `xprop`. Idle detection uses `xprintidle`.

To sanity-check detection manually:
```sh
python3 -c "from screentime.window_detector import create_window_detector as c; print(c().get_focused())"
```
Switch focus between windows and re-run — the identifier should change.

## Testing on Wayland

After setting up the relevant companion piece above:
```sh
python3 -c "from screentime.window_detector import create_window_detector as c; d=c(); print(d.name, d.get_focused())"
```
On sway/Hyprland this works immediately. On GNOME/KDE, switch focus to a
different window first (the KDE bridge only has data after the *first*
activation event; GNOME's is a live pull so it's correct immediately).


## Windows development

* Tests run on any OS: the Windows logic is tested against a fake Win32 layer (`tests/windows_fakes.py`).
  `python -m pytest -q` is enough on Linux. On a real Windows desktop add
  `SCREENTIME_LIVE=1` to also run `tests/integration_windows/`.
* Run from source on Windows: `scripts\windows-dev.cmd` (see [WINDOWS.md](WINDOWS.md)).
* Build the installer (from Linux, containerised): `./scripts/build-windows.sh`; see [RELEASING.md](RELEASING.md) and `packaging/windows/README.md`.
* Design, security review and verification status: [windows-developer.md](windows-developer.md).

## Versioning and releases

Semantic versioning (`MAJOR.MINOR.PATCH`). The version is written in exactly three places, which
`tests/test_version.py` requires to match:

1. `pyproject.toml` (`version = "..."`), which the Windows build also reads for the installer name,
2. `PKGBUILD` (`pkgver=...`),
3. `screentime/__init__.py` (`__version__`).

To release: bump those three, move the `Unreleased` notes in `CHANGELOG.md` under the new version heading,
run the tests, merge to `main`, then push the tag `vMAJOR.MINOR.PATCH`: GitHub Actions builds, tests, smoke-tests and publishes the release ([RELEASING.md](RELEASING.md)). Patch = bug fixes, minor = new features/platforms/themes
(backwards compatible with existing data), major = anything that breaks existing data or settings.
Files that may only exist after a build (`ScreenTime.cmd`, the installer, `dist/`) must not be referenced
in the README as if they were in the source tree; a test checks that README links and script paths exist.
