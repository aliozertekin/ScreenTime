#!/usr/bin/env bash
# Build the Windows installer and portable zip from Linux. One command, no Windows, no Wine on the host:
# everything runs in a container (see packaging/windows/Containerfile).
#
#   ./scripts/build-windows.sh                # build  -> dist/ScreenTime-<version>-{setup.exe,portable.zip}
#   ./scripts/build-windows.sh --clean        # rebuild the bundle from scratch (keeps the download cache)
#   ./scripts/build-windows.sh --purge        # also delete downloads, Wine prefix and the build image
#   ./scripts/build-windows.sh --update-lock  # re-resolve the MSYS2 packages and rewrite msys2.lock
#   ./scripts/build-windows.sh --verbose --non-interactive --skip-smoke --engine podman|docker
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$REPO/packaging/windows"
DIST="$REPO/dist"
CLEAN=0; PURGE=0; VERBOSE=0; INTERACTIVE=1; UPDATE_LOCK=0; SKIP_SMOKE=0; ENGINE="${CONTAINER_ENGINE:-}"

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die()   { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean) CLEAN=1 ;;
    --purge) PURGE=1; CLEAN=1 ;;
    --verbose|-v) VERBOSE=1 ;;
    --non-interactive) INTERACTIVE=0 ;;
    --update-lock) UPDATE_LOCK=1 ;;
    --skip-smoke) SKIP_SMOKE=1 ;;
    --engine) shift; ENGINE="${1:?--engine needs podman or docker}" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

# ---- host prerequisites: a container engine is the only unavoidable one
if [[ -z "$ENGINE" ]]; then
  if command -v podman >/dev/null 2>&1; then ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  else die "no container engine found. Install one, e.g. on Arch/CachyOS:  sudo pacman -S podman
(or docker). That is the only host tool this build needs."; fi
fi
command -v "$ENGINE" >/dev/null 2>&1 || die "container engine '$ENGINE' not found"
"$ENGINE" info >/dev/null 2>&1 || die "'$ENGINE info' failed. For docker: is the daemon running and are you in the docker group?
For rootless podman: see 'podman info' (needs /etc/subuid and /etc/subgid entries for your user)."

# ---- version: pyproject.toml is authoritative; the other copies must agree
VERSION="$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$REPO/pyproject.toml" | head -n1)"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "could not read a MAJOR.MINOR.PATCH version from pyproject.toml (got '$VERSION')"
PYV="$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$REPO/screentime/__init__.py" | head -n1)"
[[ "$PYV" == "$VERSION" ]] || die "version mismatch: pyproject.toml says $VERSION but screentime/__init__.py says $PYV"

COMMIT="nogit"; EPOCH="$(date +%s)"
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  COMMIT="$(git -C "$REPO" rev-parse --short=12 HEAD)"
  EPOCH="$(git -C "$REPO" log -1 --format=%ct)"        # artifact timestamps follow the commit, not the wall clock
  if [[ -n "$(git -C "$REPO" status --porcelain --untracked-files=no -- . ':!dist')" ]]; then COMMIT="$COMMIT-dirty"; fi
fi

# shellcheck source=/dev/null
source "$PKG/toolchain.env"
CACHE="${SCREENTIME_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/screentime/windows-build}"
TAG="screentime-winbuild:$(cat "$PKG/Containerfile" "$PKG/toolchain.env" | sha256sum | cut -c1-12)"

echo "Building ScreenTime for Windows..."
echo "Version: $VERSION  (commit $COMMIT)"
echo "Build environment: $ENGINE image $TAG"
echo "Cache: $CACHE"

if [[ $PURGE -eq 1 ]]; then
  echo "Purging cache and build image..."
  rm -rf "$CACHE"
  "$ENGINE" images --format '{{.Repository}}:{{.Tag}}' | grep '^screentime-winbuild:' | xargs -r "$ENGINE" rmi -f >/dev/null 2>&1 || true
elif [[ $CLEAN -eq 1 ]]; then
  echo "Clean rebuild: discarding the previous bundle (downloads are kept)..."
  rm -rf "$CACHE/work" "$CACHE/stage"
fi
mkdir -p "$CACHE" "$DIST"
if [[ $CLEAN -eq 1 ]]; then rm -f "$DIST"/ScreenTime-*-setup.exe "$DIST"/ScreenTime-*-portable.zip "$DIST"/build-manifest.txt "$DIST"/SHA256SUMS; fi

avail_kb="$(df -Pk "$CACHE" | awk 'NR==2{print $4}')"
(( avail_kb > 6*1024*1024 )) || echo "WARNING: less than 6 GB free in $CACHE; the first build downloads and unpacks about 2 GB."

# ---- build image (cached by content hash of Containerfile + toolchain.env)
if ! "$ENGINE" image inspect "$TAG" >/dev/null 2>&1; then
  echo "Preparing the build environment (first run only; downloads Ubuntu, Wine and tools)..."
  build_out=/dev/stdout; [[ $VERBOSE -eq 1 ]] || build_out=$(mktemp)
  if ! "$ENGINE" build -f "$PKG/Containerfile" -t "$TAG" --build-arg "UBUNTU_IMAGE=$UBUNTU_IMAGE" "$PKG" >"$build_out" 2>&1; then
    [[ $VERBOSE -eq 1 ]] || tail -40 "$build_out"
    die "building the container image failed (see output above; re-run with --verbose)"
  fi
  [[ $VERBOSE -eq 1 ]] || rm -f "$build_out"
fi

# ---- run the build
run=("$ENGINE" run --rm)
if [[ "$ENGINE" == podman && "$("$ENGINE" info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" == true ]]; then
  run+=(--userns=keep-id --security-opt label=disable)
else
  run+=(--user "$(id -u):$(id -g)")
  [[ "$ENGINE" == podman ]] && run+=(--security-opt label=disable)
fi
[[ $INTERACTIVE -eq 1 && -t 0 && -t 1 ]] && run+=(-t)
run+=(-v "$REPO:/src:ro" -v "$CACHE:/cache" -v "$DIST:/out"
      -e "VERSION=$VERSION" -e "COMMIT=$COMMIT" -e "SOURCE_DATE_EPOCH=$EPOCH"
      -e "VERBOSE=$VERBOSE" -e "UPDATE_LOCK=$UPDATE_LOCK" -e "SKIP_SMOKE=$SKIP_SMOKE"
      "$TAG" /src/packaging/windows/container/build.sh)
"${run[@]}" || die "the Windows build failed (output above). Try --clean, or --verbose for more detail."

# ---- lock files generated by this run belong in the repository
for f in msys2.lock toolchain.lock; do
  if [[ -f "$DIST/$f.generated" ]]; then
    mv -f "$DIST/$f.generated" "$PKG/$f"
    echo "Wrote packaging/windows/$f  <-- review and commit it so builds stay reproducible."
  fi
done

# ---- verify and report
SETUP="$DIST/ScreenTime-$VERSION-setup.exe"; ZIP="$DIST/ScreenTime-$VERSION-portable.zip"; MAN="$DIST/build-manifest.txt"
for f in "$SETUP" "$ZIP" "$MAN"; do [[ -s "$f" ]] || die "expected artifact missing or empty: $f"; done
echo
echo "Build complete."
echo
printf 'Installer:\n  %s  (%s)\n' "${SETUP#"$REPO"/}" "$(du -h "$SETUP" | cut -f1)"
printf 'Portable:\n  %s  (%s)\n' "${ZIP#"$REPO"/}" "$(du -h "$ZIP" | cut -f1)"
printf 'Manifest:\n  %s\n' "${MAN#"$REPO"/}"
echo
echo "Upload the setup.exe (and optionally the zip and SHA256SUMS) to a GitHub Release: see docs/RELEASING.md"
