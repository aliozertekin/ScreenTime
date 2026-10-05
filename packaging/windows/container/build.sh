#!/usr/bin/env bash
# Runs INSIDE the build container. Mounts: /src (repo, read-only), /cache (persistent), /out (dist/).
# Inputs (env): VERSION COMMIT SOURCE_DATE_EPOCH  [VERBOSE=1] [UPDATE_LOCK=1] [SKIP_SMOKE=1]
set -euo pipefail

SRC=/src; CACHE=/cache; OUT=/out
PKG=$SRC/packaging/windows
# shellcheck source=/dev/null
source "$PKG/toolchain.env"
: "${VERSION:?VERSION not set}" "${COMMIT:?COMMIT not set}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
export HOME="$CACHE/home" WINEPREFIX="$CACHE/wineprefix"
WORK="$CACHE/work"; DL="$CACHE/downloads"
mkdir -p "$HOME" "$DL/msys2" "$WORK" "$OUT"
[[ "${VERBOSE:-0}" == 1 ]] && set -x

step() { printf '\n==> %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
wine_x() { xvfb-run -a wine "$@"; }

# ---- 1. private copy of the sources (the repo mount is read-only; never builds from stale artifacts)
step "Preparing sources ($VERSION @ ${COMMIT})"
rm -rf "$WORK/src" "$WORK/bundle" "$WORK/inno-out"
mkdir -p "$WORK/src" "$WORK/bundle" "$WORK/inno-out"
tar -C "$SRC" --exclude=.git --exclude=dist --exclude=__pycache__ --exclude=.pytest_cache --exclude='*.pyc' -cf - . \
  | tar -C "$WORK/src" -xf -

# ---- 2. Windows runtime: Python + GTK4 + libadwaita + PyGObject from MSYS2 packages (no Windows code runs)
step "Fetching the MSYS2 ${MSYS2_ENV} runtime (cached in $DL/msys2)"
[[ -f "$PKG/msys2.lock" ]] && cp "$PKG/msys2.lock" "$WORK/msys2.lock"
lock_before=""; [[ -f "$WORK/msys2.lock" ]] && lock_before=$(sha256sum "$WORK/msys2.lock" | cut -d' ' -f1)
fetch_args=(--lock "$WORK/msys2.lock" --packages "$PKG/packages.txt" --cache "$DL/msys2" --stage "$CACHE/stage"
            --repo-urls "$MSYS2_REPO_URLS" --prefix "$MSYS2_PREFIX" --env "$MSYS2_ENV")
if [[ "${UPDATE_LOCK:-0}" == 1 ]]; then
  fetch_args+=(--update-lock)
else
  fetch_args+=(--require-lock)      # reproducibility: never resolve a fresh package set implicitly
fi
python3 "$PKG/msys2_fetch.py" "${fetch_args[@]}"
lock_after=$(sha256sum "$WORK/msys2.lock" | cut -d' ' -f1)
if [[ "$lock_before" != "$lock_after" ]]; then
  cp "$WORK/msys2.lock" "$OUT/msys2.lock.generated"
  echo "NOTE: a new/updated msys2.lock was produced (the build script will install it into packaging/windows/)."
fi

# ---- 3. application bundle
step "Assembling the application bundle"
BUNDLE="$WORK/bundle/ScreenTime"
python3 "$PKG/assemble_bundle.py" --runtime "$CACHE/stage/$MSYS2_ENV" --repo "$WORK/src" --out "$BUNDLE" \
  --version "$VERSION" --lock "$WORK/msys2.lock" --work "$WORK"
du -sh "$BUNDLE" | sed 's/^/bundle size: /'

# ---- 4. static validation (no Windows needed)
step "Validating the bundle (required files + every imported DLL is present)"
python3 "$PKG/bundle_check.py" "$BUNDLE" || die "bundle validation failed"

# ---- 5. Wine: Inno Setup + smoke test
step "Preparing Wine and Inno Setup ${INNO_VERSION}"
INNO_EXE="$DL/innosetup-${INNO_VERSION}.exe"
if [[ ! -s "$INNO_EXE" ]]; then
  curl -fL --retry 3 --retry-delay 3 -o "$INNO_EXE.part" "$INNO_URL" || die "could not download Inno Setup from $INNO_URL"
  mv "$INNO_EXE.part" "$INNO_EXE"
fi
inno_sha=$(sha256sum "$INNO_EXE" | cut -d' ' -f1)
if [[ -f "$PKG/toolchain.lock" ]] && grep -q "^innosetup-${INNO_VERSION}.exe " "$PKG/toolchain.lock"; then
  want=$(awk -v f="innosetup-${INNO_VERSION}.exe" '$1==f{print $2}' "$PKG/toolchain.lock")
  [[ "$want" == "$inno_sha" ]] || die "Inno Setup checksum mismatch (expected $want, got $inno_sha). Delete the cached file and investigate before trusting it."
elif [[ "${UPDATE_LOCK:-0}" == 1 ]]; then
  echo "innosetup-${INNO_VERSION}.exe $inno_sha" > "$OUT/toolchain.lock.generated"
  echo "NOTE: recorded the Inno Setup checksum $inno_sha (commit packaging/windows/toolchain.lock)."
else
  die "packaging/windows/toolchain.lock has no pinned SHA-256 for innosetup-${INNO_VERSION}.exe. Verification is mandatory; run with --update-lock, review the checksum against the official release, and commit it."
fi
ISCC="$WINEPREFIX/drive_c/Program Files (x86)/Inno Setup 6/ISCC.exe"
# The prefix lives in the persistent cache: reinstall whenever the pinned installer changed, so an
# ISCC.exe left by an older pin is never reused while the manifest claims the new version.
INNO_MARK="$WINEPREFIX/.screentime-inno-installed"
if [[ ! -f "$ISCC" || "$(cat "$INNO_MARK" 2>/dev/null)" != "$INNO_VERSION $inno_sha" ]]; then
  wine_x wineboot -u >/dev/null 2>&1 || true
  wine_x "$INNO_EXE" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- >/dev/null 2>&1 || true
  [[ -f "$ISCC" ]] || die "Inno Setup did not install under Wine (expected $ISCC). Re-run with --clean --verbose."
  echo "$INNO_VERSION $inno_sha" > "$INNO_MARK"
fi

if [[ "${SKIP_SMOKE:-0}" == 1 ]]; then
  echo "(smoke test skipped by request)"
else
  step "Smoke test under Wine (imports GTK4 + libadwaita + PyGObject + ScreenTime from the bundle only)"
  # shellcheck disable=SC2016
  smoke_py='
import sys, os
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, GdkPixbuf
import cairo, psutil
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
assert AESGCM(b"0" * 32).decrypt(b"1" * 12, AESGCM(b"0" * 32).encrypt(b"1" * 12, b"x", None), None) == b"x"
import screentime
from screentime import platform as p
from screentime.platform.windows import win32, window_detector, idle_detector, power_events, instance_lock, keystore, autostart
assert p.current().name == "windows", p.current().name
assert os.path.dirname(os.path.dirname(sys.executable)).lower() in screentime.__file__.lower(), screentime.__file__
print("OK gtk %d.%d adw %d.%d screentime %s" % (Gtk.get_major_version(), Gtk.get_minor_version(),
      Adw.get_major_version(), Adw.get_minor_version(), screentime.__version__))
'
  out=$(wine_x "$BUNDLE/bin/screentime-cli.exe" -c "$smoke_py" 2>&1) || { echo "$out" | tail -40; die "bundle failed to import its runtime under Wine"; }
  echo "$out" | grep -q '^OK gtk 4' || { echo "$out" | tail -40; die "smoke test printed no success line"; }
  echo "$out" | grep '^OK'
  # Best-effort GUI start: a missing DLL/typelib/module is fatal; anything else Wine can't do is only a warning.
  log="$WORK/gui-smoke.log"
  set +e; timeout 25 xvfb-run -a wine "$BUNDLE/bin/screentime-gui.exe" -m screentime.gui.app >"$log" 2>&1; rc=$?; set -e
  if grep -Eiq 'import_dll|Library .* not found|ModuleNotFoundError|ImportError|Typelib|Namespace .* not available' "$log"; then
    tail -30 "$log"; die "GUI start-up under Wine hit a missing library/module"
  elif [[ $rc -eq 124 ]]; then echo "GUI stayed up for 25 s under Wine."
  else echo "WARNING: GUI exited with status $rc under Wine (not a missing-library error); see $log. Verify on a real Windows machine."; fi
fi

# ---- 6. installer + portable zip
step "Building the installer (Inno Setup via Wine)"
wine_x "$ISCC" "/DAppVersion=$VERSION" "/DBundleDir=$(winepath -w "$BUNDLE")" "/O$(winepath -w "$WORK/inno-out")" \
  "$(winepath -w "$PKG/installer.iss")" 2>&1 | tail -25
SETUP="$WORK/inno-out/ScreenTime-${VERSION}-setup.exe"
[[ -s "$SETUP" ]] || die "Inno Setup produced no installer ($SETUP)"
head -c2 "$SETUP" | grep -q MZ || die "installer is not a Windows executable"

step "Building the portable archive"
ZIP="$WORK/ScreenTime-${VERSION}-portable.zip"
python3 "$PKG/make_zip.py" "$BUNDLE" "$ZIP"

# ---- 7. publish into dist/
step "Writing artifacts"
cp "$SETUP" "$OUT/"; cp "$ZIP" "$OUT/"
tool_versions=(
  "inno_setup=${INNO_VERSION}" "inno_setup_sha256=$inno_sha"
  "wine=$(wine --version 2>/dev/null || echo unknown)" "python_build_host=$(python3 --version | cut -d' ' -f2)"
  "msys2_env=$MSYS2_ENV" "msys2_lock_sha256=$lock_after" "msys2_packages=$(grep -vc '^#' "$WORK/msys2.lock")"
  "bundle_python=$(find "$BUNDLE/lib" -maxdepth 1 -name 'python3.*' -printf '%f' | head -c 20)"
  "base_image=$UBUNTU_IMAGE"
)
python3 "$PKG/make_manifest.py" "$OUT" "$VERSION" "$COMMIT" "${tool_versions[@]}"
echo "container build finished"
