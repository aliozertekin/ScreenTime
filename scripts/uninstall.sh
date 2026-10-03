#!/usr/bin/env bash
# Uninstaller for ScreenTime. Mirrors scripts/install.sh: undoes every
# system-integration step it (or the PKGBUILD) performed, and -- separately,
# because it's destructive and irreversible -- your tracked usage history.
#
# Usage:
#   ./scripts/uninstall.sh              interactive: asks before deleting data
#   ./scripts/uninstall.sh --keep-data  removes the program, keeps your usage history
#   ./scripts/uninstall.sh --yes        no prompts; deletes everything, including data
#   ./scripts/uninstall.sh --dry-run    prints what would happen, changes nothing
set -uo pipefail

KEEP_DATA=false
ASSUME_YES=false
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --keep-data) KEEP_DATA=true ;;
        --yes|-y) ASSUME_YES=true ;;
        --dry-run) DRY_RUN=true ;;
        --help|-h)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown option: $arg (see --help)"; exit 1 ;;
    esac
done

run() {
    # Runs a command, or just prints it under --dry-run. Never aborts the
    # script on failure (e.g. "systemctl disable" on a unit that's already
    # gone) -- uninstalling is best-effort by nature.
    if $DRY_RUN; then
        echo "  [dry-run] $*"
    else
        "$@" >/dev/null 2>&1 || true
    fi
}

say() { echo "==> $*"; }

XDG_DATA_HOME_="${XDG_DATA_HOME:-$HOME/.local/share}"
XDG_CONFIG_HOME_="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_DIR="$XDG_DATA_HOME_/screentime"
GNOME_EXT_DIR="$XDG_DATA_HOME_/gnome-shell/extensions/screentime-focus@screentime.local"

echo "ScreenTime uninstaller"
echo "======================"

# ------------------------------------------------------------- via pacman?
PACMAN_MANAGED=false
if command -v pacman >/dev/null 2>&1 && pacman -Qi screentime >/dev/null 2>&1; then
    PACMAN_MANAGED=true
    echo
    echo "screentime is installed as an Arch package. This script removes"
    echo "everything it can from your user account (background service,"
    echo "autostart entries, the GNOME/KDE Wayland helper, and -- if you"
    echo "choose -- your usage data), but the package files themselves"
    echo "(under /usr) must be removed with pacman, not by this script:"
    echo
    echo "    sudo pacman -Rns screentime"
    echo
    if ! $ASSUME_YES && ! $DRY_RUN; then
        read -rp "Continue removing the per-user parts now? [y/N] " ans
        [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 0
    fi
fi

# ---------------------------------------------------------- stop tracking
say "Stopping and disabling the tracking daemon..."
run systemctl --user stop screentime-daemon.service
run systemctl --user disable screentime-daemon.service
# Fallback for non-systemd setups, or a daemon started manually in a terminal.
if command -v pkill >/dev/null 2>&1; then
    run pkill -f "screentime.daemon"
    run pkill -f "screentime-daemon"
fi

# --------------------------------------------------------------- autostart
say "Removing autostart entries..."
run rm -f "$XDG_CONFIG_HOME_/autostart/screentime-daemon.desktop"
if ! $PACMAN_MANAGED; then
    run rm -f "$XDG_CONFIG_HOME_/systemd/user/screentime-daemon.service"
    run systemctl --user daemon-reload
fi
# /etc/xdg/autostart/screentime-daemon.desktop (shipped disabled-by-default
# by the Arch package) is a system file removed by `pacman -Rns`, not here.

# ---------------------------------------------- Wayland companion pieces
say "Removing the GNOME/KDE Wayland focus helper (if installed)..."
if command -v kpackagetool6 >/dev/null 2>&1; then
    run kpackagetool6 --type KWin/Script -r screentime-focus
elif command -v kpackagetool5 >/dev/null 2>&1; then
    run kpackagetool5 --type KWin/Script -r screentime-focus
fi
if command -v gnome-extensions >/dev/null 2>&1; then
    run gnome-extensions disable screentime-focus@screentime.local
    run gnome-extensions uninstall screentime-focus@screentime.local
fi
# Belt-and-suspenders: gnome-extensions uninstall doesn't always exist on
# older versions, so also remove the directory the auto-installer copied
# directly (this is the actual source of truth for whether it's "installed").
if [ -d "$GNOME_EXT_DIR" ]; then
    run rm -rf "$GNOME_EXT_DIR"
fi

# ------------------------------------------------------- desktop entry etc.
if ! $PACMAN_MANAGED; then
    say "Removing the desktop entry and icon..."
    run rm -f "$XDG_DATA_HOME_/applications/org.screentime.App.desktop"
    run rm -f "$XDG_DATA_HOME_/icons/hicolor/scalable/apps/screentime.svg"
    run update-desktop-database "$XDG_DATA_HOME_/applications"
    run gtk-update-icon-cache "$XDG_DATA_HOME_/icons/hicolor"
fi

# ------------------------------------------------------------ the program
if ! $PACMAN_MANAGED; then
    say "Uninstalling the screentime Python package..."
    if $DRY_RUN; then
        echo "  [dry-run] pip uninstall -y screentime"
    else
        pip uninstall -y screentime --break-system-packages >/dev/null 2>&1 \
            || pip uninstall -y screentime >/dev/null 2>&1 \
            || echo "  (pip uninstall reported nothing to remove -- already gone?)"
    fi
fi

# ------------------------------------------------------------------- data
echo
if $KEEP_DATA; then
    say "Keeping your usage data at: $DATA_DIR"
elif [ ! -d "$DATA_DIR" ]; then
    say "No data directory found at $DATA_DIR -- nothing to remove."
else
    DELETE_DATA=$ASSUME_YES
    if ! $ASSUME_YES && ! $DRY_RUN; then
        echo "Your usage history lives (encrypted) at: $DATA_DIR"
        echo "Its encryption key is stored separately; without the key the history is unreadable anyway."
        du -sh "$DATA_DIR" 2>/dev/null | sed 's/^/  /'
        read -rp "Delete it permanently? This cannot be undone. [y/N] " ans
        [[ "$ans" == "y" || "$ans" == "Y" ]] && DELETE_DATA=true
    fi
    if $DELETE_DATA; then
        say "Deleting $DATA_DIR ..."
        run rm -rf "$DATA_DIR"
        # The data is encrypted; its key lives elsewhere (key file under the config
        # dir, or an entry in the system keyring). Delete it too, or an orphaned key
        # is left behind for data that no longer exists.
        say "Deleting the database key ..."
        run rm -rf "$XDG_CONFIG_HOME_/screentime/keys" "$XDG_CONFIG_HOME_/screentime/security.json"
        rmdir "$XDG_CONFIG_HOME_/screentime" 2>/dev/null || true
        if command -v secret-tool >/dev/null 2>&1; then
            run secret-tool clear application screentime purpose database-key || true
        else
            say "If you stored the key in your keyring, remove the 'ScreenTime database key' entry there."
        fi
    else
        say "Keeping your usage data at: $DATA_DIR"
    fi
fi

echo
if $DRY_RUN; then
    echo "Dry run complete -- nothing was actually changed."
else
    echo "Uninstall complete."
    $PACMAN_MANAGED && echo "Remember to run: sudo pacman -Rns screentime"
fi
