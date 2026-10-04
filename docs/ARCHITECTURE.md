# Architecture

How ScreenTime is put together. Back to the [README](../README.md). Windows-specific design: [windows-developer.md](windows-developer.md).

> **Two platforms, one design.** The text below describes the Linux implementation
> (systemd, logind, X11/Wayland). Windows uses the same daemon/GUI/store/timing code
> with native Windows replacements for the OS-specific parts (Win32 foreground and idle
> detection, power messages, Task Scheduler, named mutex, Credential Manager). The mapping
> is in [windows-developer.md](windows-developer.md); all platform choices are made in one
> place, `screentime/platform/__init__.py`.

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
              └────────────────► screentime.sec ◄────────────┘
                     (encrypted log in a SQLite file; each process keeps
                      an in-memory SQL view, kept in sync via the log)
```

The **daemon is the tracking authority**. The GUI is a client of the same
store (reads, plus a couple of write paths for settings/exclusions that go
through the same `Database` class). Both processes can write safely: a writer
takes the store's cross-process write lock, applies whatever the other process
appended, then adds its own record; readers pick up changes with a cheap
`PRAGMA data_version` check. This means:

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
| Persistence | `screentime/db.py` | SQL schema and queries, crash-safe writes, crash recovery |
| Encrypted container | `screentime/secure_log.py` | AES-256-GCM records/snapshots in a SQLite file; tamper detection |
| In-memory store | `screentime/protected_store.py` | Replays the log into an in-memory SQLite DB; records writes; compaction |
| Key management | `screentime/keystore.py` | Key generation, system keyring (Secret Service/KWallet), key file, recovery key |
| Storage entry point | `screentime/storage.py` | Open/create the store, migrate the legacy plaintext DB, status, recovery |
| Security CLI | `screentime/security_cli.py` | `screentime-security` status / verify / recovery |
| Statistics/query layer | `screentime/stats.py` | Read-only aggregation for the GUI (today/week/month/all-time, per-app detail) |
| Configuration | `screentime/config.py` | Typed accessors over the `settings` table |
| Autostart | `screentime/autostart.py` | Picks and configures the start-at-login mechanism (systemd user unit or XDG autostart entry), self-heals it, reports startup status |
| Theme system | `screentime/theme.py` | Semantic color tokens, built-in themes, KDE color mapping, CSS generation (pure Python) |
| Theme manager | `screentime/gui/theme_manager.py` | Applies the theme live to GTK/libadwaita; follows desktop color changes |
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
  (`time.monotonic()` on Linux; a sleep-excluding clock on Windows). An NTP step, a manual clock change, or a timezone
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

The logical schema is unchanged (`screentime/migrations/001_init.sql`); only
the physical storage is encrypted (see Data protection):

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

Every mutating call commits immediately (one small encrypted record per
change), so a crash or power loss loses at most the last uncommitted
heartbeat tick. Every heartbeat is logged individually (not coalesced): the
daily-totals updates are deltas, so replaying only the latest heartbeat would
undercount.

## How Wayland/X11 compatibility is handled

See the detection strategy list above. In short: native protocols where
they exist (sway/Hyprland), a documented companion component where they
don't (GNOME extension, KWin script), and honest degradation (no fabricated
data) everywhere else, surfaced in the GUI's Settings → Diagnostics section
so the user always knows *why* if something isn't being tracked.

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
| `theme` | `default` | Theme id (`system`, `default`, `light`, `dark`, `gruvbox-dark`, `gruvbox-light`); unknown ids fall back to `default` |
| `color_scheme` | `system` | `system` / `light` / `dark`; honoured by adaptive themes only |
| `accent_color` | (empty) | `#rrggbb` override of the theme's accent; empty = use the theme's |

App exclusions (Settings → "Excluded applications") are per-app, stored on
the `apps.excluded` column — excluded apps are never tracked and contribute
no usage time (verified by `test_excluded_app_is_not_tracked`).


## Project structure

```
screentime/
├── screentime/                  # Python package
│   ├── db.py                     # SQL schema/queries; routes writes through the protected store
│   ├── secure_log.py             # AES-256-GCM container (SQLite file of ciphertext)
│   ├── protected_store.py        # in-memory SQL view replayed from the log; compaction
│   ├── keystore.py               # key generation, keyring/key file backends, recovery key
│   ├── storage.py                # open/create/migrate the store; status; recovery
│   ├── security_cli.py           # `screentime-security`
│   ├── session_manager.py        # Core timing/session logic
│   ├── app_identity.py           # .desktop-file resolution & app grouping
│   ├── process_monitor.py        # psutil-based process enumeration
│   ├── window_detector.py        # X11/Wayland active-window strategies
│   ├── idle_detector.py          # X11/Wayland idle detection
│   ├── config.py                 # Settings accessor
│   ├── theme.py                   # theme tokens, built-in themes, KDE mapping, CSS
│   ├── autostart.py               # start-at-login mechanism selection, self-heal, status
│   ├── instance_lock.py           # single-instance daemon lock
│   ├── stats.py                   # Read-only query layer
│   ├── daemon.py                  # Tracking daemon entry point
│   ├── migrations/001_init.sql    # Schema
│   └── gui/
│       ├── app.py                  # Adw.Application
│       ├── window.py                # Sidebar nav + content stack
│       ├── theme_manager.py         # Applies themes to GTK/libadwaita live
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
