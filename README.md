# ScreenTime

A native, fully local application usage tracker for Arch Linux — a personal
"Screen Time" for the desktop. Tracks how long each application is actually
**focused and in use** (not just running), persists it forever in a local
SQLite database, and shows it in a GTK4/Libadwaita dashboard.

**100% offline.** No telemetry, no cloud sync, no external analytics, no
network access anywhere in the codebase. Everything lives in
`~/.local/share/screentime/screentime.db`.

---

## Architecture

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│      screentime-daemon        │        │         screentime-gui          │
│   (systemd --user service)    │        │      (opened on demand)         │
│                                │        │                                  │
│  window_detector ──┐          │        │  stats.py (read-only queries)   │
│  idle_detector   ──┼─▶ SessionManager   │  gui/views/* (Dashboard,        │
│  process_monitor ──┘      │   │        │   Applications, History,        │
│                            ▼   │        │   Statistics, Settings)         │
│                      db.py (writes)     │                                  │
└─────────────┬──────────────────┘        └───────────────┬──────────────────┘
              │                                              │
              └────────────────► screentime.db ◄─────────────┘
                              (SQLite, WAL mode)
```

The **daemon is the only writer**. The GUI is a pure read-only client of the
same SQLite file (WAL mode lets both hold connections concurrently without
blocking each other), plus a couple of write paths for settings/exclusions
that go through the same `Database` class. This means:

* Closing the GUI window never stops tracking — the daemon is a separate
  process (a systemd `--user` service).
* The GUI crashing, or never being opened at all, cannot corrupt usage data.
* Re-opening the GUI later just re-reads whatever the daemon has persisted.

### Components

| Component | File | Responsibility |
|---|---|---|
| Tracking daemon | `screentime/daemon.py` | GLib main loop tying everything together; suspend/resume via logind D-Bus; signal handling for clean shutdown |
| Active-window detection | `screentime/window_detector.py` | Strategy pattern: X11 (Xlib/xprop), sway, Hyprland, GNOME (companion extension), KDE (companion KWin script) |
| Idle detection | `screentime/idle_detector.py` | `xprintidle`/Xlib for X11, logind `IdleHint` for Wayland |
| Process monitoring | `screentime/process_monitor.py` | psutil-based enumeration; groups multiple processes into one app |
| App identity resolution | `screentime/app_identity.py` | Maps raw wm_class/app_id → canonical key + `.desktop` metadata (name, icon) |
| Session/timing engine | `screentime/session_manager.py` | The core correctness logic — see below |
| Persistence | `screentime/db.py` | SQLite schema, crash-safe writes, crash recovery |
| Statistics/query layer | `screentime/stats.py` | Read-only aggregation for the GUI (today/week/month/all-time, per-app detail) |
| Configuration | `screentime/config.py` | Typed accessors over the `settings` table |
| Autostart | `screentime/autostart.py` | Picks and configures the start-at-login mechanism (systemd user unit or XDG autostart entry), self-heals it, reports startup status |
| Steam names | `screentime/steam_library.py` | Offline Steam AppID -> game-name resolution from Steam's local manifests (`steam_app_<id>` windows); cached, invalidated when Steam's files change |
| Packaging hook | `screentime.install` | pacman post-upgrade: restarts running per-user daemons |
| Single-instance guard | `screentime/instance_lock.py` | `flock`-based lock so only one daemon ever tracks, however it was launched |
| Wayland companion installer | `screentime/wayland_setup.py` | Auto-installs/enables the GNOME extension or KWin script on first run |
| GUI | `screentime/gui/` | GTK4 + Libadwaita: sidebar nav, Dashboard/Applications/History/Statistics/Settings |

---

## How active-application detection works

1. **`process_monitor.py`** enumerates the user's running processes with
   `psutil`, filters out kernel threads and known noise (shells, portals,
   the shell/compositor itself), and groups multiple PIDs belonging to the
   same application (e.g. a browser's dozens of renderer processes) under
   one canonical key. This is used for presence/diagnostics, **not** for
   driving the clock — a backgrounded process sitting idle for hours is not
   "usage".
2. **`window_detector.py`** is what actually drives the clock: it reports
   which window currently has focus. This is a strategy pattern because
   Wayland has no single cross-compositor API for this (by design — Wayland
   doesn't let arbitrary clients snoop on each other's windows):
   * **X11**: `python-xlib` reading `_NET_ACTIVE_WINDOW`/`WM_CLASS`, or
     `xprop` subprocess calls if `python-xlib` isn't installed. Deliberately
     does **not** depend on `xdotool`.
   * **sway** (and other IPC-compatible wlroots compositors): `swaymsg -t
     get_tree`, walking the tree for the focused node's `app_id`/`pid`.
   * **Hyprland**: `hyprctl -j activewindow`.
   * **GNOME on Wayland**: GNOME Shell exposes no public API for this.
     A tiny companion extension (`screentime/resources/gnome-extension/`)
     runs inside the shell process (where the focused window is naturally
     known) and relays it over D-Bus, pull-style (the daemon calls a method
     on it). See "Wayland compatibility" below.
   * **KDE Plasma on Wayland**: same problem, a push-style solution instead.
     A KWin script (`screentime/resources/kde-script/`) can't host a D-Bus
     service, but it *can* call out to one whenever `workspace.windowActivated`
     fires. The daemon hosts a small local service (`org.screentime.KWinFocus`)
     that the script pushes updates to, and `KWinPushDetector.get_focused()`
     just returns whatever was last pushed.
     **Important constraint this implies:** owning that D-Bus well-known
     name is exclusive and sticky — whichever process asks first keeps it
     until it disconnects, with no automatic takeover. `KWinPushDetector`
     must therefore only ever be instantiated by the one process that's
     actually supposed to receive KWin's events (the daemon). A real bug
     shipped briefly where the GUI's Settings page built one too, just to
     display a status string — since that page is constructed once and
     lives for the whole GUI session, if the GUI happened to open before or
     independently of the daemon, it would silently and permanently steal
     ownership, leaving the real daemon's detector starved with no error
     anywhere. The fix: the daemon records which backend it selected into
     the `settings` table on startup (and clears it on clean shutdown), and
     everything else — the GUI, `scripts/diagnose.sh` — only ever reads
     that cached value instead of instantiating a live detector.
     `tests/test_settings_view_no_detector_leak.py` and `tests/test_daemon.py`
     guard against this regressing.
   * If none of the above is available, detection degrades to "no data"
     rather than guessing — surfaced clearly in Settings → Diagnostics.
3. The raw identifier (wm_class/app_id/pid) is resolved via
   **`app_identity.py`** against installed `.desktop` files (matching on
   `StartupWMClass`, the `Exec` basename, or the filename) to get a stable
   canonical key plus a friendly display name and icon. This is what makes
   multiple windows/processes of one app collapse into a single tracked
   application.

## How usage time is calculated (accuracy & crash-safety)

This is the part the brief asked to get right, so it's worth being
explicit about the design in `session_manager.py`:

* **Timestamps stored for display are wall-clock** (`time.time()`), but
  **all elapsed durations are computed from a monotonic clock**
  (`time.monotonic()`). An NTP step, a manual clock change, or a timezone
  change cannot inflate or corrupt a session's duration — only real
  elapsed seconds count. (Verified by
  `test_wall_clock_jump_does_not_corrupt_duration`.)
* **No polling-counter accumulation.** The daemon does not "add 1 second
  every second the app seems active" — it stores actual session
  **start/end timestamps** in `sessions`, and durations are always
  `end - start`, computed once, per session.
* **Heartbeat = crash safety, not double-counting.** Every poll tick
  (default 2s) advances a `last_heartbeat` checkpoint column — a *separate*
  column from `end_time`, which stays `NULL` for a session's entire open
  lifetime. If the daemon is `SIGKILL`'d or the machine loses power, at
  most one heartbeat interval (a few seconds) of usage is lost on restart
  — never a whole session, never previously-closed sessions. (Verified by
  `test_crash_recovery_does_not_fabricate_time`,
  `test_crash_then_restart_keeps_prior_progress`.)
* **Suspend/resume never counts sleep time.** The daemon subscribes to
  `org.freedesktop.login1`'s `PrepareForSleep` D-Bus signal and closes the
  open session the instant the system is about to sleep — before any
  suspended wall-clock time can be misattributed. As defense in depth, Linux's
  `CLOCK_MONOTONIC` itself does not advance during suspend, so even without
  the signal, the elapsed-time math can't fabricate hours of fake usage.
  (Verified by `test_suspend_resume_does_not_count_sleep_time`.)
* **A session in storage never spans midnight.** The daemon splits an
  open session into two rows at the exact local-midnight instant (both
  rows share that instant as close/open time, so no real elapsed second is
  lost at the boundary). This keeps day/week/month aggregation a plain
  `GROUP BY day` instead of interval-splitting at query time. (Verified by
  `test_midnight_split_keeps_each_row_single_day`.)
* **Multiple processes/windows of one app never double-count**, because
  `on_focus_change()` is a no-op when the canonical app key hasn't actually
  changed (e.g. alt-tabbing between two Firefox windows). (Verified by
  `test_multiple_windows_same_app_counted_once`.)
* **Idle time is excluded precisely**, not approximately: when idle is
  detected, the session is closed at the *actual last-input timestamp*
  (computed from the idle-seconds reading), not "now" — so the idle gap
  itself is never counted. (Verified by `test_idle_stops_counting`.)

## How data is persisted

SQLite in WAL mode (`schema in screentime/migrations/001_init.sql`):

* **`apps`** — one row per canonical application identity, never deleted.
* **`sessions`** — append-only log of focused-usage intervals. This is the
  single source of truth for everything else.
* **`daily_totals`** — a materialized cache of per-app-per-day seconds,
  updated incrementally in the same transaction as the session write that
  produced the delta (so it can't meaningfully drift), used for fast
  dashboard queries. It can always be rebuilt from `sessions` alone (see
  Settings → Data → "Rebuild statistics cache", which calls
  `Database.rebuild_daily_totals()`).
* **`settings`** — key/value config (idle timeout, poll interval, etc.),
  shared by daemon and GUI so they never disagree about current settings.

Every mutating call commits immediately (small, frequent transactions), so
a crash or power loss loses at most the last uncommitted heartbeat tick.

## How Wayland/X11 compatibility is handled

See the detection strategy list above. In short: native protocols where
they exist (sway/Hyprland), a documented companion component where they
don't (GNOME extension, KWin script), and honest degradation (no fabricated
data) everywhere else, surfaced in the GUI's Settings → Diagnostics section
so the user always knows *why* if something isn't being tracked.

---

## Runtime dependencies

| Package (Arch) | Why |
|---|---|
| `python` | ≥3.11 |
| `python-psutil` | Process enumeration |
| `python-gobject`, `gtk4`, `libadwaita` | GUI + the daemon's GLib main loop |
| `python-cairo` | Chart widget rendering (GTK4's DrawingArea draw callback) |
| `python-xlib` | X11 active-window/idle detection (optional but recommended) |
| `xorg-xprop` | X11 fallback if `python-xlib` isn't installed |
| `xprintidle` | X11 idle detection |
| `systemd` | `--user` service management + logind suspend/resume signal |

Optional: `libayatana-appindicator` (tray icon), `sway`/`hyprland` (native
Wayland detection), `gnome-shell`/`plasma-workspace` (to install the
companion extension/script for GNOME/KDE Wayland detection).

## Development dependencies

`python-build`, `python-installer`, `python-wheel`, `python-setuptools`
(packaging), `pytest` (tests).

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

263 unit + integration tests cover the session/timing engine, the DB layer,
window/idle detector parsing against realistic canned compositor output,
the Wayland companion installer, start-at-login logic (against a fake
`systemctl`), real-process daemon lifecycle (second instance, SIGTERM,
SIGKILL-then-restart), and full app-switching / restart / crash /
midnight scenarios end-to-end. A handful that build real GTK widgets need a
display and skip cleanly without one (GTK can segfault rather than raise a
clean exception if built with zero display connection at all) — on a
headless machine or over SSH, run those under `xvfb-run -a pytest tests/ -v`
instead to actually exercise them.

---

## Running it

The recommended way is to toggle **Settings → "Start tracking automatically
at login"** in the app. It chooses the right mechanism for your session (see
"How start-at-login works" below), starts the daemon immediately, and is safe
to toggle repeatedly.

```sh
screentime-gui                                 # open the dashboard
```

Manual equivalent on a session that reaches `graphical-session.target`
(GNOME, Plasma with systemd startup, uwsm, ...):

```sh
systemctl --user daemon-reload
systemctl --user enable --now screentime-daemon
```

Do not run `systemctl --user enable` on a session that doesn't reach that
target — it succeeds but never starts anything (see below).

The daemon runs independently of the GUI: you can close the ScreenTime
window (or never open it) and tracking continues in the background. A tray
icon (if `libayatana-appindicator` is installed) lets you reopen the window
without hunting through your launcher.

---

### Steam game names

Proton/Wine games show up as a window class `steam_app_<AppID>` with no
`.desktop` file, which used to display as "Steam App 2620". Names are now
resolved **entirely offline** from Steam's own files (no Steam Web API, no
network; `tests/test_steam_library.py` fails if any network module is ever
imported):

1. A `.desktop` entry (e.g. a Steam shortcut with `steam://rungameid/<id>`)
   still wins if one exists.
2. Otherwise `<library>/steamapps/appmanifest_<AppID>.acf` (`name`, falling
   back to `installdir`). Libraries come from every Steam root
   (`~/.local/share/Steam`, `~/.steam/steam|root`, Flatpak
   `~/.var/app/com.valvesoftware.Steam/...`, Snap; symlinked duplicates
   collapsed) plus each path in `libraryfolders.vdf` (current and legacy
   formats). Libraries on unmounted drives are skipped.
3. Native Linux Steam games (whose window class doesn't say "steam") are named
   via the `SteamGameId`/`SteamAppId` environment Steam sets on the game's
   process; their app key is unchanged.

The app *key* never changes, so existing history keeps grouping under the same
app. If nothing is found (uninstalled game, unknown AppID, no Steam) it falls
back to "Steam App <id>" and tracking is unaffected; a good name already stored
is never overwritten by that fallback. On daemon start, rows still holding the
old "Steam App <id>" name are renamed if the game is now resolvable.

Performance: results, including "unknown", are cached; the daemon's per-poll
lookups do no disk I/O. The cache is dropped when `libraryfolders.vdf` or a
library's `steamapps` directory changes (checked at most every 30 s), so newly
installed games appear without a restart.

Diagnosing a name that still shows as "Steam App <id>": run
`python -m screentime.steam_library` (lists the Steam locations searched and
every game found; pass AppIDs to test specific ones). The daemon also logs once
per unresolved AppID which locations it searched (`journalctl --user -u
screentime-daemon`), and `scripts/diagnose.sh` includes both. If the name is
still old right after an upgrade, the running daemon is probably pre-upgrade;
see "Upgrades".

Limitations: only *installed* games can be named (uninstalled games have no
manifest; Steam's binary `appinfo.vdf` is not parsed). Names come from the
manifest, so they are in the game's default language.

### Sleep and resume

Time while the machine is suspended is never counted, and tracking restarts
by itself on wake:

* The daemon subscribes to logind's `PrepareForSleep` and holds a logind
  **delay inhibitor** (`Inhibit("sleep", ..., "delay")`), so the open session is
  closed *before* the system sleeps rather than racing the freeze. The inhibitor
  is released right after the session is closed and re-taken on wake. If logind
  refuses it, tracking still works (see the next point).
* Durations use the monotonic clock, which does not advance during suspend, so
  even if the daemon is frozen before it can handle the signal, both signals
  are delivered on wake and the sleep is still excluded.
* Between "going to sleep" and the resume signal no new session may open (a poll
  tick in that window would otherwise create one that spans the sleep).
* Fail-safes so tracking can never stay paused: if the wall clock has run
  ahead of the monotonic clock (the machine really slept and the wake signal was
  missed), or 30 s pass with no resume (a cancelled suspend), tracking resumes.
* On wake it starts a session for whichever window is focused at that moment.

Settings -> Diagnostics shows the last sleep and wake times the daemon saw
(`last_suspend`/`last_resume` settings), so you can confirm it after sleeping.
This covers *suspend*. A locked screen or a display that merely turns off is not
treated as sleep; those are covered by idle detection only.

### Upgrades

A package upgrade replaces the Python files but does not restart a running
`systemd --user` service, so the old daemon kept running the old code until the
next login (new fixes seemed not to work). Now:

* `screentime.install` (a pacman install script referenced by the PKGBUILD)
  runs `systemctl --user try-restart screentime-daemon` for every logged-in
  user after an upgrade. `try-restart` only acts on an already-running service.
* The daemon records its version in the settings table (`daemon_version`). When
  the GUI opens it restarts a running daemon that is *older* than the installed
  app (a daemon that never recorded a version counts as older). It never starts a
  daemon you stopped, and never restarts a newer one.
* Settings -> Diagnostics shows the running daemon's version.

### How start-at-login works

`screentime-daemon` (not the GUI) is what starts at login. Closing or never
opening the window has no effect on tracking.

* **systemd user service** (preferred: restarts the daemon after a crash).
  The unit is `WantedBy=graphical-session.target`, which only sessions that
  manage that target through systemd ever reach. Settings therefore checks
  `graphical-session.target` in the *current* session and only uses the unit
  if it is active.
* **XDG autostart entry** (`~/.config/autostart/screentime-daemon.desktop`,
  absolute `Exec=` path) otherwise — honoured by every mainstream desktop,
  including sessions that never reach the target (legacy Plasma startup,
  plain sway/Hyprland).
* Only one mechanism is left in place at a time.
* If the package binary `/usr/bin/screentime-daemon` doesn't exist (e.g. a
  `pip install --user` / `install.sh` install, where the script lives in
  `~/.local/bin`), a per-user unit with the real absolute `ExecStart` is
  written to `~/.config/systemd/user/`. (The packaged unit's `/usr/bin` path
  used to make the service fail at every login on such installs.)
* The daemon takes a `flock` lock in `$XDG_RUNTIME_DIR`, so launching it
  twice (unit + autostart entry, the Settings button, a shell) never yields
  two trackers: the second exits with status 0. The kernel releases the lock
  if the holder dies, so a crash can't leave a stale lock.
* The GUI reads/writes the database with orphan-session recovery disabled;
  only the daemon (lock holder) recovers sessions left open by a crash.
  Previously, opening the GUI could close the live daemon's open session.
* If a backend isn't available at daemon start (session environment or
  compositor helper not ready yet during login), the daemon retries detection
  every 20 s while on the "unsupported"/"disabled" backend instead of
  tracking nothing until the next restart. A working detector is never
  replaced, so the KWin D-Bus ownership rule is unaffected.
* On GUI start, if the setting is on but the mechanism has gone missing (e.g.
  a package upgrade replaced the unit), it is re-established and the daemon
  started if needed. This never disables anything you enabled by hand.

**Settings → Diagnostics** shows: whether the daemon is installed, whether
start-at-login is enabled (and via which mechanism), whether it is running,
when it last started, and the detection backends it selected.

## Troubleshooting

**Tracking doesn't resume after reboot/login?** Check Settings → Diagnostics
first. Then:

```sh
systemctl --user status screentime-daemon            # "enabled" but never started?
systemctl --user is-active graphical-session.target   # "inactive" => use the XDG entry
journalctl --user -u screentime-daemon -b             # exit 203/EXEC => bad ExecStart path
ls ~/.config/autostart/screentime-daemon.desktop
```

Toggling the Settings switch off and on re-picks the mechanism for the
current session type.

If tracking isn't recording anything, run:

```sh
./scripts/diagnose.sh
```

This collects everything useful into one timestamped file (also printed to
the terminal): your session/desktop environment, the daemon's systemd
status and recent logs, which active-window/idle backend actually gets
selected right now, the Wayland companion helper's install status, and a
summary of what's (or isn't) in the database. By default it also briefly
stops the background service and runs it with verbose logging for 10
seconds so you can switch between a couple of windows and see whether focus
events are actually being received — pass `--no-live` to skip that and only
collect passive info, or `--output PATH` to choose where it's saved. It
restarts the normal background service automatically when the live capture
finishes.

It's read-only aside from that one live-capture step, and it tells you
up front what it collects (app names and session timestamps from your
tracking database, plus session/desktop info) so you can review before
sharing.

## Uninstalling

```sh
./scripts/uninstall.sh              # interactive: asks before deleting your usage data
./scripts/uninstall.sh --keep-data  # removes the program, keeps ~/.local/share/screentime
./scripts/uninstall.sh --yes        # no prompts -- removes everything, including data
./scripts/uninstall.sh --dry-run    # prints what it would do without changing anything
```

This stops and disables the daemon (`systemctl --user`, falling back to
`pkill` for non-systemd setups), removes the autostart entry, uninstalls
the GNOME extension / KWin script if the auto-installer set one up, removes
the desktop entry and icon, and uninstalls the Python package. Data
deletion is handled as a separate, explicit step (interactive confirmation
by default, since it's the one irreversible part) rather than bundled
silently into the rest.

If you installed via the Arch package (`makepkg -si` / `pacman -Qi
screentime` succeeds), the script detects this, tells you to run `sudo
pacman -Rns screentime` for the system-owned files, and only handles the
per-user parts pacman doesn't know about (autostart, the Wayland helper,
your data).

---

## Configuration

All settings live in the `settings` table and are editable from
**Settings** in the app, or directly:

| Key | Default | Meaning |
|---|---|---|
| `idle_timeout_seconds` | `300` (5 min) | Stop counting active time after this long without input |
| `poll_interval_seconds` | `2` | How often the daemon checks the focused window |
| `heartbeat_interval_seconds` | `10` | Crash-safety checkpoint granularity |
| `autostart_enabled` | `false` | The user's start-at-login opt-in (written by the Settings switch; used to self-heal on GUI start) |
| `daemon_last_start` | — | Written by the daemon at startup (unix time); shown in Diagnostics |
| `active_window_backend` / `active_idle_backend` | — | Written by the daemon; read-only for the GUI/diagnostics |
| `minimize_to_tray` | `true` | Closing the window hides it instead of quitting, if a tray icon is available |

App exclusions (Settings → "Excluded applications") are per-app, stored on
the `apps.excluded` column — excluded apps are never tracked and contribute
no usage time (verified by `test_excluded_app_is_not_tracked`).

---

## Wayland compatibility

**sway / Hyprland**: works out of the box, no extra setup — the daemon
talks to `swaymsg`/`hyprctl` directly.

**GNOME (Wayland) and KDE Plasma (Wayland)**: these desktops expose no
public API for "what window is focused" (see the Architecture section
above for why), so a small companion piece is required — a GNOME Shell
extension or a KWin script. **This is installed and enabled automatically**
at daemon/GUI startup on these desktops (see `screentime/wayland_setup.py`):
it's bundled inside the Python package itself, so it works regardless of
whether you installed via the Arch package or `pip`. It's content-aware
rather than a one-shot "attempted once" flag: what's on disk is compared
against what's currently bundled, so a screentime upgrade that changes the
companion piece's content (e.g. a bug fix in it) reinstalls automatically
on the next startup, with no manual "Reinstall" click required. A
persistent, unrelated failure (e.g. `kpackagetool6` genuinely missing) is
remembered per exact bundled version so it doesn't retry — and spam logs —
on every single startup; it tries again once that version's content
actually changes.

You'll know it worked if **Settings → Diagnostics** shows "Installed and
enabled" next to "Wayland focus helper". If it instead says "Not
installed", "An older copy is installed", or reports an error, that page
also has an **Install / Reinstall** button to retry by hand — useful for an
environmental problem (missing tooling) rather than a content change,
since that's the one case the automatic self-heal above doesn't cover.

What automatic setup does, and its known caveat on each desktop:

* **KDE Plasma**: runs `kpackagetool6` (falling back to `kpackagetool5`) to
  install the KWin script, `kwriteconfig6`/`5` to enable it, and then two
  reload mechanisms: `qdbus .../KWin reconfigure` and, more reliably, the
  explicitly-documented `qdbus .../Scripting.loadScript` call, which loads
  and starts the fresh script content live without needing a restart.
  Install status is checked on disk (`~/.local/share/kwin/scripts/screentime-focus/`):
  the copy's *content* must match what's currently bundled, **and its
  `metadata.json` must declare `"KPackageStructure": "KWin/Script"`**.
  That key is what lets KWin discover a packaged script at session start.
  Without it the script still installs and even runs when pushed live with
  `Scripting.loadScript`, so tracking appears to work right after
  installation, but KWin never loads it at login, so tracking silently stops
  after every reboot. (v1.0.0-1.0.1 shipped without the key.) If
  `kpackagetool6 --type KWin/Script --list` prints `KPackageStructure of
  KPluginMetaData(...screentime-focus...) does not match requested format
  "KWin/Script"`, **that is this bug, not a cosmetic message**; restarting
  the daemon (or opening the GUI) reinstalls the corrected script
  automatically. The bundled key is also checked by the test suite.
  The persistent script reports every focus change to the daemon, **including
  "no window focused"** (an empty report). Dropping that event used to leave
  the daemon attributing time to whichever window it had heard about last.
  The script only reports *changes* and loads before the daemon exists at
  login, so about 2 s after the daemon owns its D-Bus name it loads a separate
  one-shot script (`announce.js`) that reports the already-focused window once
  and is unloaded again. It never unloads or reloads the persistent script
  (an earlier version did, and could leave it loaded but not running). Note
  that `Scripting.loadScript` only registers a script; the installer and the
  announce step call `Script<id>.run` explicitly. `announce.js` and the
  persistent script's reporting logic are executed against a mocked KWin API
  in the test suite (requires `node`; those tests skip without it).
  The KWin script itself prints a line to KWin's own log at every
  meaningful step — load, which activation signal it connected to, each
  window activation, each D-Bus send outcome — specifically because a
  silent failure here is otherwise invisible from the daemon's side (it
  just sees "nothing ever arrives," with no way to tell whether the script
  never loaded, connected to the wrong signal, or loaded and fired but the
  D-Bus call itself failed). Read it with:
  ```sh
  journalctl -f SYSLOG_IDENTIFIER=kwin_wayland
  ```
  and look for lines starting `screentime-focus:` (or use
  `./scripts/diagnose.sh`, which captures this automatically during its
  live-capture step). No output at all there while switching windows means
  the script isn't running — try logging out and back in once, since a
  script whose on-disk content just changed isn't guaranteed to be
  re-read by `reconfigure`/`loadScript` alone if KWin had it cached from a
  previous, different version.
* **GNOME**: copies the extension into
  `~/.local/share/gnome-shell/extensions/` and runs `gnome-extensions
  enable`. **GNOME Shell on Wayland only loads a brand-new extension's
  code after the shell process restarts** — this means one log out/in is
  required the first time, even though the install itself succeeds
  immediately. Diagnostics will show "Installed but not enabled" (or
  "enabled" if `gnome-extensions` reports it as such) until you've done
  that once.

If the automatic install can't run at all (e.g. `kpackagetool6` isn't on
`PATH` because `plasma-workspace` isn't installed), you can still do it by
hand with the same commands the auto-installer runs — see
`screentime/wayland_setup.py` for the exact subprocess calls, or just click
**Install** in Settings → Diagnostics after installing the missing tool.

**Idle detection on wlroots compositors (sway etc.)**: idle detection uses
`systemd-logind`'s `IdleHint`, which GNOME/KDE update automatically but
sway does not by default. Configure `swayidle` to toggle it:
```
swayidle -w \
    timeout 300 'loginctl session-status ${XDG_SESSION_ID} >/dev/null; busctl --user call org.freedesktop.login1 /org/freedesktop/login1/session/self org.freedesktop.login1.Session SetIdleHint b true' \
    resume 'busctl --user call org.freedesktop.login1 /org/freedesktop/login1/session/self org.freedesktop.login1.Session SetIdleHint b false'
```

Settings → Diagnostics always shows which backend is actually active, so
you can confirm this worked.

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

---

## Privacy

All data is stored locally in `~/.local/share/screentime/screentime.db`.
This application makes **no network requests of any kind** — there is no
HTTP client anywhere in the codebase, no update checker, no crash reporter,
no analytics SDK. You can verify this yourself: `grep -r "socket\|requests\|urllib\|http" screentime/` turns up nothing beyond D-Bus (a purely
local IPC mechanism, used only to talk to `logind`/`gnome-shell`/`kwin` on
your own machine).

---

## Project structure

```
screentime/
├── screentime/                  # Python package
│   ├── db.py                     # SQLite persistence layer
│   ├── session_manager.py        # Core timing/session logic
│   ├── app_identity.py           # .desktop-file resolution & app grouping
│   ├── process_monitor.py        # psutil-based process enumeration
│   ├── window_detector.py        # X11/Wayland active-window strategies
│   ├── idle_detector.py          # X11/Wayland idle detection
│   ├── config.py                 # Settings accessor
│   ├── autostart.py               # start-at-login mechanism selection, self-heal, status
│   ├── instance_lock.py           # single-instance daemon lock
│   ├── stats.py                   # Read-only query layer
│   ├── daemon.py                  # Tracking daemon entry point
│   ├── migrations/001_init.sql    # Schema
│   └── gui/
│       ├── app.py                  # Adw.Application
│       ├── window.py                # Sidebar nav + content stack
│       ├── tray.py                  # Optional tray indicator
│       ├── views/                    # Dashboard, Applications, History, Statistics, Settings
│       └── widgets/                   # Shared widgets (usage row, bar chart)
├── tests/                          # unit + integration tests (see "Running tests")
├── data/                            # systemd unit, .desktop files, icon, Wayland scripts
├── scripts/install.sh                # Manual (non-package) installer
├── scripts/uninstall.sh              # Removes the program and (optionally) all data
├── scripts/diagnose.sh               # Collects logs/state into one file for bug reports
├── PKGBUILD                          # Arch package build recipe
└── pyproject.toml
```
