# Changelog

Versions follow semantic versioning. The version lives in `pyproject.toml`,
`PKGBUILD` (`pkgver`) and `screentime/__init__.py` (`__version__`);
`tests/test_version.py` fails if they disagree.

## 1.1.0 — Reliable sleep/resume; upgrades restart the daemon; Steam diagnostics

* Sleep/resume: logind delay inhibitor so the session closes before the system
  sleeps; no session can open between the sleep signal and the freeze; fail-safes
  (clock drift, 30 s guard) so a missed resume signal can't leave tracking paused;
  last sleep/wake times shown in Diagnostics. 13 tests drive the real daemon with a
  fake clock that models suspend (wall time advances, monotonic doesn't).
* Upgrades: a pacman install script try-restarts running daemons, and the GUI
  restarts a daemon older than the installed app (version handshake through the
  settings table). Previously an upgraded install kept running the old daemon, so
  new fixes (including Steam names) appeared not to work.
* Steam: `python -m screentime.steam_library`, a once-per-AppID journal note of
  the locations searched, and a section in `diagnose.sh`.

## 1.0.4 — Fix: wrong focused window (time stuck on a background app)

* KWin script dropped "focus cleared" events, so the daemon kept crediting the
  last window it heard about (e.g. a background browser) while another window
  or nothing was focused. It now reports an empty focus, which ends the session.
* The 1.0.2 "re-announce" step unloaded and re-loaded the persistent script
  without ever running it, which could leave it loaded but inert. Replaced with
  a separate one-shot `announce.js` (load, run, unload) that never touches the
  persistent script. Installer live-load now also calls `Script<id>.run`.
* `diagnose.sh` reports whether KWin currently holds the script.
* Tests execute the real KWin scripts in Node against a mocked KWin API.
* Daemon installs its SIGTERM/SIGINT handler before taking the lock, so a stop
  during slow startup exits cleanly (status 0, lock released) instead of dying by
  signal. Found via a flaky test (8/25 runs failed before, 0/25 after).

* `autostart.daemon_is_running()` returned as soon as systemd said the unit was
  inactive, never consulting the process scan, so a hand-launched or pre-upgrade
  daemon (no lock, not the unit) looked "not running" and a second one could be
  started on top of it. It now checks lock, then unit, then the process list.
* Test hermeticity: the machine-wide process scan is now a stubbed seam
  (`_find_daemon_processes`), so a *real* running ScreenTime daemon (yours, or
  another test run's) can no longer change unit-test results. Real-process tests
  now always reap the daemons they spawn, even when an assertion fails.

## 1.0.3 — Fix: Steam games shown as "Steam App <id>"

* New `steam_library.py`: offline AppID -> name from Steam's local manifests
  across all libraries; wired into `app_identity.resolve()` (which now also takes
  an optional `pid`). Keys are unchanged, so history is preserved.
* Names stored before this fix are renamed on daemon start; a resolved name is
  never downgraded to the fallback if metadata later disappears.
* Cached with change-detection invalidation; no per-poll disk I/O.
* Test guard that the package imports no network modules.

## 1.0.2 — Fix: KDE Plasma tracking records nothing after reboot

* The bundled KWin script's `metadata.json` lacked `"KPackageStructure":
  "KWin/Script"`, so KWin never auto-loaded it at login. It only worked in the
  session where the installer had pushed it live via `Scripting.loadScript`,
  which is why tracking worked after install and died at every reboot (daemon
  running, backend `kwin-push`, but no focus events ever arriving). The installed
  copy is now also validated for this key, so an old copy counts as stale and is
  reinstalled automatically on the next daemon/GUI start.
* The daemon asks KWin to re-load the script once after it owns its D-Bus name,
  so the window already focused at login is counted.
* `scripts/diagnose.sh` reports whether the key is present and whether KWin
  loaded the script this boot.
* README no longer describes the `does not match requested format` warning as
  cosmetic; it was this bug.
* Test fix (pre-existing): `test_app_detail_stats` failed if run in the first
  hour after midnight; it is now anchored to local midnight (assertions unchanged).

## 1.0.1 — Fix: tracking not resuming after reboot/login

Root causes found and fixed:

* Packaged unit hard-coded `ExecStart=/usr/bin/screentime-daemon`; `pip
  --user`/`install.sh` installs put the script in `~/.local/bin`, so the unit
  failed at every login. A per-user unit with the real absolute path is now
  written when the packaged binary is absent, and `install.sh` rewrites it.
* Enabling a unit `WantedBy=graphical-session.target` is silently inert on
  sessions that never reach that target. The mechanism is now chosen by
  checking the current session; otherwise an XDG autostart entry (absolute
  `Exec=`) is used. Only one launcher is left in place.
* No single-instance guard: added a `flock` lock; a second daemon exits 0.
* Opening the GUI ran orphan-session recovery and could close the live
  daemon's open session; the GUI now connects with `recover_orphans=False`.
* Backends were detected once at start; a daemon started before the session
  environment was ready stayed on "unsupported" indefinitely. It now retries
  (null backends only).
* Packaged `/etc/xdg/autostart` entry used the GNOME-only
  `X-GNOME-Autostart-enabled=false`; now `Hidden=true` so KDE also honours it.
* `daemon_is_running()`/`stop_now()` matched any process whose command line
  mentioned the name; they now match the executable.
* Unit: `StartLimitIntervalSec=0` so early-boot failures never permanently
  stop restarts.

Added: Settings → Diagnostics rows (installed / start at login / last start),
`autostart_enabled` is now actually persisted, GUI-start self-heal.
