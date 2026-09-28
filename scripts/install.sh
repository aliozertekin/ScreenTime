#!/usr/bin/env bash
# Manual installer for ScreenTime, for people who don't want to build the
# Arch package. Installs into the current user's ~/.local so no root is
# needed (except for the small set of system packages via pacman below).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"

echo "==> Checking system dependencies (pacman)..."
MISSING=()
for pkg in python python-psutil python-gobject python-cairo gtk4 libadwaita; do
    pacman -Qi "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "The following packages are required and not installed: ${MISSING[*]}"
    echo "Install them with: sudo pacman -S ${MISSING[*]}"
    read -rp "Install now? [y/N] " ans
    if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
        sudo pacman -S --needed "${MISSING[@]}"
    else
        echo "Aborting."
        exit 1
    fi
fi

echo "==> Installing the Python package into a user venv-free layout..."
pip install --user "$HERE" --break-system-packages

echo "==> Installing desktop integration files..."
mkdir -p "$HOME/.local/share/applications" \
         "$HOME/.local/share/icons/hicolor/scalable/apps" \
         "$HOME/.config/systemd/user"

install -Dm644 "$HERE/data/org.screentime.App.desktop" \
    "$HOME/.local/share/applications/org.screentime.App.desktop"
install -Dm644 "$HERE/data/icons/screentime.svg" \
    "$HOME/.local/share/icons/hicolor/scalable/apps/screentime.svg"
install -Dm644 "$HERE/data/screentime-daemon.service" \
    "$HOME/.config/systemd/user/screentime-daemon.service"

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "==> Reloading systemd user units..."
systemctl --user daemon-reload

cat <<'EOF'

Install complete.

Next steps:
  1. Start the tracker now:            systemctl --user start screentime-daemon
  2. Track automatically at login:     systemctl --user enable screentime-daemon
     (or toggle "Start at login" in ScreenTime's Settings page, which does the same thing)
  3. Open the app:                     screentime-gui   (or find "ScreenTime" in your launcher)

If you're on GNOME or KDE Plasma under Wayland, the small companion
extension/script needed for active-window detection installs and enables
itself automatically the first time you run screentime-daemon or
screentime-gui. Check Settings -> Diagnostics to confirm it worked (GNOME
needs one log out/in afterward before it takes effect -- see the README's
"Wayland compatibility" section for details, and a manual Install/Reinstall
button is there too in case the automatic attempt fails).

To remove ScreenTime later, including all tracked usage data, run:
  ./scripts/uninstall.sh
(add --keep-data to remove the program but keep your history, or --dry-run
to preview what it would do first).
EOF
