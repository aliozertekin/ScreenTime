#!/usr/bin/env bash
# Create (or complete) the GitHub Release for a tag from the artifacts the build already produced.
#   scripts/ci/publish-release.sh vX.Y.Z DIST_DIR
# Needs: gh (authenticated via GH_TOKEN), sha256sum. Safe to re-run:
#   * SHA256SUMS is recomputed from the exact files being uploaded and checked before anything is published;
#   * the release is created as a draft and published last;
#   * if the release already exists, only MISSING assets are uploaded (a published asset is never replaced,
#     so a retry can never change bytes users already verified); a retry never creates a second release.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tag="${1:?usage: publish-release.sh vX.Y.Z DIST_DIR}"
dist="${2:?usage: publish-release.sh vX.Y.Z DIST_DIR}"
die() { echo "publish failed: $*" >&2; exit 1; }

version="$("$REPO/scripts/ci/check-release-tag.sh" "$tag")"
setup="ScreenTime-${version}-setup.exe"; portable="ScreenTime-${version}-portable.zip"
for f in "$setup" "$portable" build-manifest.txt; do [[ -s "$dist/$f" ]] || die "missing or empty artifact: $dist/$f"; done
head -c2 "$dist/$setup" | grep -q MZ || die "$setup is not a Windows executable"
grep -q "^version=${version}$" "$dist/build-manifest.txt" || die "build-manifest.txt is not for version ${version}"

# SHA-256 of the exact files users will download. Format: '<64 hex>  <name>' (two spaces, sha256sum -c compatible),
# one line per release file, sorted by name, LF endings.
(cd "$dist" && LC_ALL=C sha256sum "$setup" "$portable" build-manifest.txt | LC_ALL=C sort -k2 > SHA256SUMS.new)
if [[ -f "$dist/SHA256SUMS" ]]; then       # the build step's own sums: they must match what we are about to ship
  for f in "$setup" "$portable"; do
    a="$(grep -F "  $f" "$dist/SHA256SUMS" | cut -d' ' -f1)"; b="$(grep -F "  $f" "$dist/SHA256SUMS.new" | cut -d' ' -f1)"
    [[ -n "$a" && "$a" == "$b" ]] || die "$f changed between the build and publishing (build: ${a:-none}, now: $b)"
  done
fi
mv "$dist/SHA256SUMS.new" "$dist/SHA256SUMS"
(cd "$dist" && sha256sum -c SHA256SUMS) || die "SHA256SUMS does not verify"

assets=("$setup" "$portable" SHA256SUMS build-manifest.txt)
notes="$(mktemp)"; trap 'rm -f "$notes"' EXIT
awk -v v="$version" '/^## /{p = index($0, "## " v " ") == 1} p' "$REPO/CHANGELOG.md" > "$notes"
{ echo; echo "### Verify your download"; echo
  echo '```'; cat "$dist/SHA256SUMS"; echo '```'; echo
  echo "Windows (PowerShell): \`Get-FileHash .\\${setup} -Algorithm SHA256\`  -  Linux/macOS: \`sha256sum -c SHA256SUMS\`"
} >> "$notes"

# Created as a DRAFT with every asset attached and published only at the end, so users never see a release
# that is missing its installer or checksums, even if this job dies half-way (a retry then completes it).
flags=(--draft --title "ScreenTime ${version}" --notes-file "$notes" --verify-tag)
if gh release view "$tag" >/dev/null 2>&1; then
  have="$(gh release view "$tag" --json assets --jq '.assets[].name')"
  todo=()
  for a in "${assets[@]}"; do grep -qxF "$a" <<<"$have" || todo+=("$dist/$a"); done
  if ((${#todo[@]})); then
    echo "release $tag exists; uploading missing assets: ${todo[*]##*/}"
    gh release upload "$tag" "${todo[@]}"
  else
    echo "release $tag already has every asset"
  fi
else
  gh release create "$tag" "${assets[@]/#/$dist/}" "${flags[@]}"
fi
if [[ "$(gh release view "$tag" --json isDraft --jq .isDraft)" == "true" ]]; then
  gh release edit "$tag" --draft=false
fi
echo "release $tag published"
