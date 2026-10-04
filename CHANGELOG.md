# Changelog

Versions follow semantic versioning. The version lives in `pyproject.toml`,
`PKGBUILD` (`pkgver`) and `screentime/__init__.py` (`__version__`);
`tests/test_version.py` fails if they disagree.

## 1.4.0 — Windows support; documentation reorganised

**Windows 10/11 (64-bit)** is now a platform of the same app, daemon and encrypted store
(release candidate: tested against a simulated Win32 layer, not yet on real machines).
* New `screentime/platform/` layer with one central platform switch; Linux behaviour is unchanged.
* Native Win32 foreground detection (executable identity, never window titles), `GetLastInputInfo`
  idle detection, `WM_POWERBROADCAST` suspend/resume, a sleep-excluding clock
  (`QueryUnbiasedInterruptTime`) plus a wall-vs-monotonic drift guard for missed suspend events.
* Per-user named-mutex single instance with a graceful stop event; Task Scheduler start-at-login
  (self-healing, never re-enables a startup you turned off); Credential Manager key storage with a
  DPAPI fallback; Windows tray icon; Windows light/dark and accent for the existing themes.
* Offline Steam on Windows: native libraries, games recognised by install folder, same
  `steam_app_<AppID>` keys as Linux.
* `screentime-diagnose`: cross-platform diagnostics (no secrets, no titles, never creates a store).
* Packaging: **`./scripts/build-windows.sh` builds the installer and portable zip entirely from Linux**
  (podman/docker only; no Windows, VM, host Wine or GitHub Actions). It fetches the MSYS2 ucrt64 runtime
  as hash-pinned packages without running any Windows code, assembles a relocatable bundle, statically checks
  that every imported DLL is present, smoke-tests it under Wine, builds the per-user no-admin Inno Setup
  installer and a deterministic zip, and writes `build-manifest.txt` + `SHA256SUMS`. Uninstall keeps data by
  default; `scripts/windows-dev.cmd` runs the app from a source checkout; the GitHub workflow is optional.
  The packaged build creates its gdk-pixbuf loader cache at first launch (`runtime_env.py`).
* Fixed: the recovery-key decoder accepted a typo in the last character's unused bits (the typo test was
  flaky about 1 run in 30); the canonical spelling is now enforced.
* Fixed: after a handled resume the Windows drift guard compared against pre-sleep clocks and split a
  session; Steam exe-path lookup is now case-insensitive on every OS.

**Documentation**
* The README is now a short user guide. The former 1,000-line README was split into `docs/`:
  `ARCHITECTURE`, `DEVELOPMENT`, `LINUX`, `STEAM`, `THEMES`, `SECURITY`, plus `WINDOWS` (user guide) and
  `windows-developer`, `RELEASING`. A test checks that README/docs links and script paths exist.
* Version is documented in `docs/DEVELOPMENT.md` ("Versioning and releases").

## 1.3.0 — Encrypted local database; 12 more themes

**Data protection** (details and limits in the README)
* The usage database is now AES-256-GCM encrypted at rest (`python-cryptography`,
  an official Arch package; no SQLCipher/AUR). `screentime.sec` is a SQLite file
  holding only authenticated ciphertext; each process rebuilds an in-memory SQL
  view by replaying a snapshot plus records, so all existing queries work
  unchanged. Records are bound to the store and their position, so tampering,
  reordering, removal from the middle and cross-store moves are detected; wrong
  key vs. tampering are distinguished.
* Daemon and GUI stay consistent through a cross-process write lock,
  catch-up-before-write, and `PRAGMA data_version`; the daemon compacts the log
  into encrypted snapshots; `kill -9` mid-write leaves a consistent store.
* Key: random 256 bits, kept in the system keyring (Secret Service / KWallet)
  or, when none can be used silently, a 0600 key file in the config directory
  (flagged as weaker in Settings, with a one-click move to the keyring). The
  daemon never shows a keyring dialog: it waits and retries while the keyring is
  locked, and Settings shows "Paused" with an Unlock button.
* Recovery key (checksummed Base32), GUI recovery screen, and the
  `screentime-security` CLI (`status`, `verify`, `export-recovery-key`,
  `import-recovery-key`, `move-key-to-keyring`, `compact`).
* Migration of an existing plaintext `screentime.db`: integrity check, build
  under a temp name, verify row-by-row using the key fetched back from the
  keystore, atomic swap, then overwrite-and-delete the plaintext, `-wal`,
  `-shm`, `-journal`. Any failure leaves the original untouched; an interrupted
  run is finished next start (unless the plaintext changed meanwhile).
* A bare `Database()` now raises instead of silently creating a plaintext file;
  `diagnose.sh` and `uninstall.sh` were updated (uninstall also deletes the key).
* Daemon exits with status 78 (not restart-looped) when the key is lost.

**Bugs found and fixed while testing this**
* Shutdown: the early SIGTERM handler raised `SystemExit`. Raised inside
  PyGObject's C-backed code (`GLib.Variant`) it was silently discarded or crashed
  the interpreter (exit -11), leaving a daemon that ignored stop requests. The
  handler now only sets a flag, checked at startup's own safe points and inside
  every wait loop (sliced sleeps, the setup-lock wait). 300 start/SIGTERM
  cycles in the failing environment: 0 abnormal exits (previously ~25% failed).
* First-time store creation is atomic (built under a temp name); a zero-byte
  leftover is replaced.
* Hardened the container: malformed record columns and impossible nonce lengths
  are reported as tampering instead of raw sqlite3/ValueError crashes; a
  WAL-mode image is normalized before loading into memory.
* The "waiting for the keyring" warning is logged once (then every 5 minutes),
  not on every poll.
* `KeyNotFoundError` now gives recovery guidance in the CLI and GUI.

**Themes:** Nord, Dracula, Solarized Dark/Light, Catppuccin Mocha/Latte, Tokyo
Night, One Dark, Rose Pine / Rose Pine Dawn, High Contrast Dark/Light (held to
7:1, WCAG AAA). 18 themes in total.

**Tests:** the whole existing suite also passes on the encrypted backend
(`SCREENTIME_TEST_PROTECTED=1`), plus a Secret Service integration test against
a real gnome-keyring in a private D-Bus session.

## 1.2.0 — Themes, KDE system colors, custom accent

* New theme system (`screentime/theme.py`, pure Python) with semantic color
  tokens (background, surface, foreground, muted, accent + hover/active, border,
  success/warning/error, selected, headerbar, sidebar, app-row, chart_1..6).
  Views/widgets consume tokens or generated CSS classes; a test fails if a color
  literal appears in any GUI module. The bar chart no longer hard-codes colors.
* Built-in themes: System (follows the desktop), ScreenTime (default), Light,
  Dark, Gruvbox Dark, Gruvbox Light. Adding a theme is one `register_theme()`
  call; invalid themes are rejected at registration.
* System theme reads KDE Plasma's color scheme (`kdeglobals`, including
  `AccentColor`), maps it onto the tokens (table in the README, generated from
  code), merges `kdedefaults`/`XDG_CONFIG_DIRS`, and re-themes the open window
  when the Plasma scheme changes. Falls back to the system light/dark preference
  and accent (libadwaita >= 1.6) elsewhere.
* Custom accent via the standard GTK color chooser; text on the accent and the
  accent-as-text are contrast-corrected so any pick stays readable; the value is
  validated `#rrggbb` only (cannot inject CSS).
* Settings -> Appearance: Theme, Color scheme (disabled and showing the fixed
  scheme for fixed themes), Accent color, live preview, Reset. Persisted in the
  existing `settings` table (`theme`, `color_scheme`, `accent_color`), with
  validation and non-destructive fallback for stale values.
* Libadwaita named colors are overridden, restyling stock widgets (headerbar,
  sidebar, cards, buttons); CSS variables are also emitted on GTK >= 4.16.
* Tests: pure theme/KDE/config tests plus GTK tests (every theme x scheme parsed
  as CSS by GTK, live switching, Plasma file-replace watching, Settings widgets,
  chart pixel colors). Mutation-checked.
* Fixed while building: the process-wide theme manager was bound to the first
  database it saw; it is now keyed by database.

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
