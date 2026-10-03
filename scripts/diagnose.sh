#!/usr/bin/env bash
# Collects everything useful for debugging "nothing is being tracked" and
# similar issues into one file you can paste back for help. Read-only except
# for one optional step (see below) that briefly restarts the daemon with
# verbose logging to capture live focus-detection events -- it puts things
# back the way they were when it's done.
#
# Usage:
#   ./scripts/diagnose.sh              full run, including the live capture
#   ./scripts/diagnose.sh --no-live    skip the live capture (passive info only)
#   ./scripts/diagnose.sh --output FILE   write to a specific path
set -uo pipefail

LIVE_CAPTURE=true
OUTFILE="$HOME/screentime-diagnostics-$(date +%Y%m%d-%H%M%S).txt"
for arg in "$@"; do
    case "$arg" in
        --no-live) LIVE_CAPTURE=false ;;
        --output) ;;  # handled below
        --help|-h)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
    esac
done
# crude --output FILE parsing (kept simple on purpose)
prev=""
for arg in "$@"; do
    if [ "$prev" = "--output" ]; then OUTFILE="$arg"; fi
    prev="$arg"
done

exec > >(tee "$OUTFILE") 2>&1

section() { echo; echo "=========================================================="; echo "== $1"; echo "=========================================================="; }
run() { echo "\$ $*"; "$@" 2>&1; echo; }

echo "ScreenTime diagnostics -- $(date)"
echo "Written to: $OUTFILE"
echo
echo "NOTE: this includes the names of applications you've used (from the"
echo "tracking database) and your desktop environment/session details. Skim"
echo "it before sharing if that matters to you -- it does not include window"
echo "titles, file contents, or anything beyond app names and timestamps."

# ------------------------------------------------------------- system info
section "System"
run uname -a
[ -f /etc/os-release ] && run grep -E "^(NAME|VERSION)=" /etc/os-release

# ---------------------------------------------------------- session/env
section "Session environment"
for v in XDG_SESSION_TYPE XDG_CURRENT_DESKTOP WAYLAND_DISPLAY DISPLAY XDG_SESSION_ID \
         SWAYSOCK HYPRLAND_INSTANCE_SIGNATURE XDG_DATA_HOME XDG_CONFIG_HOME; do
    echo "$v=${!v-<unset>}"
done
echo
echo "DBUS_SESSION_BUS_ADDRESS is set: $([ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] && echo yes || echo no)"

# ------------------------------------------------------------ package info
section "Package"
if command -v pacman >/dev/null 2>&1 && pacman -Qi screentime >/dev/null 2>&1; then
    run pacman -Qi screentime
else
    run pip show screentime
fi

# -------------------------------------------------------- systemd service
section "systemd --user service"
run systemctl --user status screentime-daemon.service --no-pager -l
echo "--- environment systemd --user actually has (relevant vars) ---"
run bash -c "systemctl --user show-environment 2>&1 | grep -E 'DBUS|WAYLAND|XDG_|HYPRLAND|SWAYSOCK' || echo '(none of these are set in the systemd --user environment -- this can break Wayland detection)'"

section "Recent daemon log (journalctl)"
run journalctl --user -u screentime-daemon.service -n 150 --no-pager

# --------------------------------------------------------- detector state
section "Active-window / idle detector: what the running daemon actually selected"
echo "\$ python3 - (reading the daemon's own reported state, not re-instantiating a detector)"
echo "# NOTE: deliberately not calling create_window_detector() here. On KDE it"
echo "# would build a KWinPushDetector, which claims exclusive ownership of a"
echo "# D-Bus name for as long as it's alive -- doing that from a throwaway"
echo "# diagnostic script risks starving the real daemon of KWin's events, which"
echo "# is exactly the bug this script exists to help catch. The daemon records"
echo "# what it selected in the settings table specifically so nothing else ever"
echo "# needs to instantiate a live detector just to ask 'what backend is active'."
python3 <<'PYEOF' 2>&1
from screentime import storage
from screentime.window_detector import detect_session_type
try:
    # Non-interactive: a diagnostic script must never pop a keyring dialog.
    db = storage.open_database(recover_orphans=False, interactive=False)
except Exception as e:
    print('Could not open the protected database:', storage.explain_open_error(e))
    raise SystemExit(0)
print('session type:', detect_session_type())
print('daemon-reported window backend:', db.get_setting('active_window_backend') or '(not set -- daemon has not started since this was added, or is not running)')
print('daemon-reported idle backend:', db.get_setting('active_idle_backend') or '(not set)')
db.close()
PYEOF
echo

# ------------------------------------------------------- wayland helper
section "Wayland companion helper (GNOME extension / KWin script) status"
echo "\$ python3 - (checking Wayland helper install status)"
python3 <<'PYEOF' 2>&1
from screentime import wayland_setup
print('detected desktop:', wayland_setup.detect_wayland_desktop())
print('status:', wayland_setup.status_for_current_desktop())
PYEOF
echo
if command -v kpackagetool6 >/dev/null 2>&1; then
    run kpackagetool6 --type KWin/Script --list
elif command -v kpackagetool5 >/dev/null 2>&1; then
    run kpackagetool5 --type KWin/Script --list
fi
if command -v kreadconfig6 >/dev/null 2>&1; then
    run kreadconfig6 --file kwinrc --group Plugins --key screentime-focusEnabled
    echo
    echo "# metadata.json must declare KPackageStructure=KWin/Script or KWin will not load the"
    echo "# script at login (it may still work until the next reboot if pushed in live):"
    grep -H KPackageStructure "${XDG_DATA_HOME:-$HOME/.local/share}/kwin/scripts/screentime-focus/metadata.json" \
        || echo "  MISSING -- this is the 'tracking stops after reboot' bug; restart the daemon to reinstall the script"
    echo
    echo "# Does KWin currently hold the script? (true/false)"
    (command -v qdbus6 >/dev/null && qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.isScriptLoaded screentime-focus) \
        || (command -v qdbus >/dev/null && qdbus org.kde.KWin /Scripting org.kde.kwin.Scripting.isScriptLoaded screentime-focus) \
        || echo "  (qdbus not available)"
    echo
    echo "# Did KWin load the script since this boot? (empty = it did not)"
    journalctl -b --no-pager SYSLOG_IDENTIFIER=kwin_wayland 2>/dev/null | grep "screentime-focus:" | tail -5
fi
if command -v gnome-extensions >/dev/null 2>&1; then
    run gnome-extensions list --enabled
fi

# ------------------------------------------------------------------- data
section "Steam game-name resolution (offline, local files only)"
echo "\$ python3 -m screentime.steam_library"
python3 -m screentime.steam_library 2>&1 | head -40
echo
echo "# Daemon's own note for any Steam AppID it could not name:"
journalctl --user -u screentime-daemon.service -b --no-pager 2>/dev/null | grep "Steam AppID" | tail -5 || true
echo "# Which daemon version is running vs installed:"
python3 -c "import screentime; print('installed:', screentime.__version__)" 2>&1
echo

section "Security (encrypted storage)"
run screentime-security status
echo "# Every stored record is authenticated by 'screentime-security verify' (may prompt for the keyring)."
echo

section "Database contents"
echo "\$ python3 - (inspecting the tracking database directly)"
python3 <<'PYEOF' 2>&1
from screentime import storage
from screentime import stats
try:
    # Non-interactive: a diagnostic script must never pop a keyring dialog.
    db = storage.open_database(recover_orphans=False, interactive=False)
except Exception as e:
    print('Could not open the protected database:', storage.explain_open_error(e))
    raise SystemExit(0)
print('DB path:', db.path)
apps = db.list_apps()
print(f'{len(apps)} known app(s):')
for a in apps:
    print(f'  - {a.display_name!r} (key={a.key!r}, excluded={a.excluded})')
total_sessions = db.conn.execute('SELECT COUNT(*) c FROM sessions').fetchone()['c']
print(f'{total_sessions} session row(s) total (all time, all apps)')
open_session = db.get_open_session()
print('currently open session:', dict(open_session) if open_session else None)
print()
print('last 10 session rows:')
for row in db.conn.execute(
    'SELECT s.id, a.display_name, s.start_time, s.end_time, s.last_heartbeat, s.end_reason, s.day '
    'FROM sessions s JOIN apps a ON a.id = s.app_id ORDER BY s.id DESC LIMIT 10'
):
    print(f'  {dict(row)}')
today = stats.today_summary(db)
print()
print('today total seconds:', today.total_seconds)
db.close()
PYEOF
echo

# ----------------------------------------------------------- live capture
if $LIVE_CAPTURE; then
    section "Live capture (10s, verbose) -- switching windows during this should show up below"
    CAPTURE_START="$(date '+%Y-%m-%d %H:%M:%S')"
    WAS_ACTIVE=false
    if systemctl --user is-active screentime-daemon.service >/dev/null 2>&1; then
        WAS_ACTIVE=true
        systemctl --user stop screentime-daemon.service >/dev/null 2>&1
        sleep 1  # let its D-Bus connection (and any KWin bus-name ownership) fully release
    fi
    echo "Switch between 2-3 different windows over the next 10 seconds..."
    ( timeout 10 screentime-daemon --verbose 2>&1 | sed 's/^/  [daemon] /' ) &
    CAPTURE_PID=$!
    wait "$CAPTURE_PID"
    if $WAS_ACTIVE; then
        systemctl --user start screentime-daemon.service >/dev/null 2>&1
        echo "(restarted the normal background service afterward)"
    fi

    echo
    echo "--- KWin's own log for that same window (KDE only; the KWin script"
    echo "    prints \"screentime-focus: ...\" at load, connect, and every"
    echo "    activation -- this is the only way to see whether KWin actually"
    echo "    ran the script at all, independent of whether the daemon"
    echo "    received anything from it) ---"
    if command -v journalctl >/dev/null 2>&1; then
        FOUND_KWIN_LINES=false
        for ident in kwin_wayland kwin_x11; do
            OUT="$(journalctl --since "$CAPTURE_START" "SYSLOG_IDENTIFIER=$ident" --no-pager 2>/dev/null | grep -i screentime-focus)"
            if [ -n "$OUT" ]; then
                FOUND_KWIN_LINES=true
                # shellcheck disable=SC2001  # sed is clearer than param-expansion for multi-line prefixing
                echo "$OUT" | sed "s/^/  [$ident] /"
            fi
        done
        if ! $FOUND_KWIN_LINES; then
            echo "  (no \"screentime-focus:\" lines found in KWin's log for this window --"
            echo "   on KDE, this means the script did not load or did not fire during the"
            echo "   capture. Try again after logging out and back in once, in case the"
            echo "   installed script content changed since KWin last loaded it.)"
        fi
    else
        echo "  (journalctl not available)"
    fi
else
    section "Live capture skipped (--no-live)"
fi

section "Done"
echo "Full output saved to: $OUTFILE"
echo "Paste that file's contents back for help."
