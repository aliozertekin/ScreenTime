#!/usr/bin/env bash
# Refuse to release unless the pushed tag, the packaged version and the changelog all agree.
#   scripts/ci/check-release-tag.sh v1.5.0      (prints the bare version on success)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tag="${1:?usage: check-release-tag.sh vMAJOR.MINOR.PATCH}"
die() { echo "release check failed: $*" >&2; exit 1; }

[[ "$tag" =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || die "tag '$tag' is not vMAJOR.MINOR.PATCH"
version="${tag#v}"
pyproject="$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$REPO/pyproject.toml" | head -n1)"
pkgver="$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$REPO/screentime/__init__.py" | head -n1)"
arch="$(sed -n 's/^pkgver=\(.*\)$/\1/p' "$REPO/PKGBUILD" | head -n1)"
[[ "$pyproject" == "$version" ]] || die "tag is $version but pyproject.toml says $pyproject"
[[ "$pkgver" == "$version" ]] || die "tag is $version but screentime/__init__.py says $pkgver"
[[ "$arch" == "$version" ]] || die "tag is $version but PKGBUILD says $arch"
grep -q "^## ${version} " "$REPO/CHANGELOG.md" || die "CHANGELOG.md has no '## ${version} ' section"
echo "$version"
