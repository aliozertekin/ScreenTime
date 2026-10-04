# Linux guide

Everything specific to Linux: running, start-at-login, sleep, upgrades, Wayland, troubleshooting, uninstalling. Back to the [README](../README.md).

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

**"ScreenTime can't open its protected database" / tracking paused?**
Run `screentime-security status`. *Waiting for the keyring*: unlock your wallet
(or press Unlock in Settings -> Security). *Key not found*: restore it with
`screentime-security import-recovery-key`. *Failed integrity check*: the file
was modified or corrupted; restore a backup of `screentime.sec`.
`screentime-security verify` authenticates every record. The daemon exits with
status 78 (and is not restart-looped) for problems a restart cannot fix.


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
./scripts/uninstall.sh --keep-data  # removes the program, keeps ~/.local/share/screentime (and the key)
./scripts/uninstall.sh --yes        # no prompts -- removes everything, including data
./scripts/uninstall.sh --dry-run    # prints what it would do without changing anything
```

This stops and disables the daemon (`systemctl --user`, falling back to
`pkill` for non-systemd setups), removes the autostart entry, uninstalls
the GNOME extension / KWin script if the auto-installer set one up, removes
the desktop entry and icon, and uninstalls the Python package. Data
deletion is handled as a separate, explicit step (interactive confirmation
by default, since it's the one irreversible part) rather than bundled
silently into the rest. Deleting the data also deletes the database key (the
key file under `~/.config/screentime/keys`, and the keyring entry via
`secret-tool` if available); with `--keep-data` the key is kept too, otherwise
the kept data would be unreadable.

If you installed via the Arch package (`makepkg -si` / `pacman -Qi
screentime` succeeds), the script detects this, tells you to run `sudo
pacman -Rns screentime` for the system-owned files, and only handles the
per-user parts pacman doesn't know about (autostart, the Wayland helper,
your data).


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

