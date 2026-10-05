#!/usr/bin/env bash
# Regenerate packaging/windows/msys2.lock from the pinned MSYS2 repository, WITHOUT building anything.
# This is the one sanctioned way the lock changes. Run by the 'update-msys2-lock' workflow; also usable locally.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG="$REPO/packaging/windows"
# shellcheck source=/dev/null
source "$PKG/toolchain.env"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
python3 "$PKG/msys2_fetch.py" --lock-only --lock "$PKG/msys2.lock" --packages "$PKG/packages.txt" \
  --cache "$tmp/cache" --stage "$tmp/stage" --repo-urls "$MSYS2_REPO_URLS" --prefix "$MSYS2_PREFIX" --env "$MSYS2_ENV"
echo "wrote $PKG/msys2.lock ($(grep -vc '^#' "$PKG/msys2.lock") packages) -- review the diff and commit it"
