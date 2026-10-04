#!/usr/bin/env bash
# Assemble dist/ScreenTime from an MSYS2 MINGW64 environment.
# Run from the repo root inside an "MSYS2 MINGW64" shell:  packaging/windows/bundle.sh [--verify-lock]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${OUT:-$REPO/dist/ScreenTime}"
PREFIX="${MINGW_PREFIX:-/mingw64}"
PYVER="$("$PREFIX/bin/python" -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"

PKGS=(
  mingw-w64-x86_64-python
  mingw-w64-x86_64-python-gobject
  mingw-w64-x86_64-python-cairo
  mingw-w64-x86_64-python-psutil
  mingw-w64-x86_64-python-cryptography
  mingw-w64-x86_64-python-pip
  mingw-w64-x86_64-python-setuptools
  mingw-w64-x86_64-python-wheel
  mingw-w64-x86_64-python-pytest
  mingw-w64-x86_64-gtk4
  mingw-w64-x86_64-libadwaita
  mingw-w64-x86_64-gdk-pixbuf2
  mingw-w64-x86_64-librsvg
  mingw-w64-x86_64-adwaita-icon-theme
  mingw-w64-x86_64-imagemagick
)
pacman -S --needed --noconfirm "${PKGS[@]}"

pacman -Q "${PKGS[@]}" > "$REPO/build-manifest.txt"
LOCK="$REPO/packaging/windows/msys2-packages.lock"
if [[ "${1:-}" == "--verify-lock" ]]; then
  [[ -f "$LOCK" ]] || { echo "no $LOCK to verify against" >&2; exit 2; }
  diff -u "$LOCK" "$REPO/build-manifest.txt" || { echo "MSYS2 package versions drifted from the lock" >&2; exit 3; }
fi

rm -rf "$OUT"; mkdir -p "$OUT"/{bin,lib,share,app}

# --- interpreter + the closure of every DLL it and the extension modules need
copy_with_deps() {
  local f="$1"
  ldd "$f" 2>/dev/null | awk '/=> \/mingw64\//{print $3}' | while read -r dll; do
    [[ -f "$OUT/bin/$(basename "$dll")" ]] || { cp "$dll" "$OUT/bin/"; copy_with_deps "$dll"; }
  done
}
for exe in python.exe pythonw.exe; do cp "$PREFIX/bin/$exe" "$OUT/bin/"; copy_with_deps "$PREFIX/bin/$exe"; done
cp "$PREFIX/bin/libpython"*.dll "$OUT/bin/" 2>/dev/null || true

# --- Python stdlib + the third-party packages we use (no tests, no caches, no pip)
mkdir -p "$OUT/lib/python$PYVER"
rsync -a --exclude='__pycache__' --exclude='test' --exclude='tests' --exclude='idlelib' \
      --exclude='tkinter' --exclude='turtledemo' --exclude='ensurepip' \
      "$PREFIX/lib/python$PYVER/" "$OUT/lib/python$PYVER/"
find "$OUT/lib/python$PYVER" \( -name '*.pyd' -o -name '*.dll' \) -print0 | while IFS= read -r -d '' f; do copy_with_deps "$f"; done

# --- GObject-Introspection typelibs, GSettings schemas, icons, pixbuf loaders, GTK data
mkdir -p "$OUT/lib/girepository-1.0" "$OUT/share/glib-2.0"
cp -r "$PREFIX/lib/girepository-1.0/." "$OUT/lib/girepository-1.0/"
cp -r "$PREFIX/share/glib-2.0/schemas" "$OUT/share/glib-2.0/"
glib-compile-schemas "$OUT/share/glib-2.0/schemas"
mkdir -p "$OUT/share/icons"
cp -r "$PREFIX/share/icons/Adwaita" "$PREFIX/share/icons/hicolor" "$OUT/share/icons/" 2>/dev/null || true
mkdir -p "$OUT/lib/gdk-pixbuf-2.0"
cp -r "$PREFIX/lib/gdk-pixbuf-2.0/." "$OUT/lib/gdk-pixbuf-2.0/"
find "$OUT/lib/gdk-pixbuf-2.0" -name '*.dll' -print0 | while IFS= read -r -d '' f; do copy_with_deps "$f"; done
for d in gtk-4.0 libadwaita-1; do [[ -d "$PREFIX/share/$d" ]] && cp -r "$PREFIX/share/$d" "$OUT/share/" || true; done
cp -r "$PREFIX/lib/gio" "$OUT/lib/" 2>/dev/null || true       # GIO modules (TLS is NOT needed: ScreenTime never uses the network)

# --- the application itself
mkdir -p "$OUT/app"
cp -r "$REPO/screentime" "$OUT/app/"
find "$OUT/app" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$OUT/app/screentime/resources/gnome-extension" "$OUT/app/screentime/resources/kde-script"   # Linux-only helpers

# --- icon (.ico for the Start Menu, installer and tray) from the shared SVG
magick -background none -density 384 "$REPO/data/icons/screentime.svg" \
  -define icon:auto-resize=256,128,64,48,32,16 "$OUT/app/screentime/resources/screentime.ico"
cp "$OUT/app/screentime/resources/screentime.ico" "$OUT/screentime.ico"

# --- launchers: renamed windowless interpreters, so Task Manager (and the daemon's own
# "ignore ScreenTime itself" rule) see screentime-daemon.exe / screentime-gui.exe
cp "$OUT/bin/pythonw.exe" "$OUT/bin/screentime-daemon.exe"
cp "$OUT/bin/pythonw.exe" "$OUT/bin/screentime-gui.exe"
cp "$OUT/bin/python.exe"  "$OUT/bin/screentime-cli.exe"       # console flavour for diagnose/security/cleanup

# The app is found via a .pth file next to the stdlib (relative to the bundle, so it is relocatable).
mkdir -p "$OUT/lib/python$PYVER/site-packages"
echo "../../../app" > "$OUT/lib/python$PYVER/site-packages/screentime-app.pth"

cat > "$OUT/screentime-diagnose.cmd" <<'CMD'
@echo off
"%~dp0bin\screentime-cli.exe" -m screentime.diagnostics %*
pause
CMD
cat > "$OUT/ScreenTime.cmd" <<'CMD'
@echo off
start "" "%~dp0bin\screentime-gui.exe" -m screentime.gui.app
CMD
cp "$REPO/LICENSE" "$OUT/" 2>/dev/null || true
cp "$REPO/build-manifest.txt" "$OUT/"
echo "bundle ready: $OUT ($(du -sh "$OUT" | cut -f1))"
