# Releasing (maintainers)

Back to the [README](../README.md). Windows users never build anything: they download
`ScreenTime-<version>-setup.exe` or `ScreenTime-<version>-portable.zip` from the project's **GitHub Releases** page.

## Publishing a release: one tag

1. Bump the version in `pyproject.toml`, `PKGBUILD` (`pkgver`) and `screentime/__init__.py`; add a
   `## X.Y.Z -- title` section to `CHANGELOG.md` (it becomes the release notes); run `python -m pytest -q`.
2. Merge that to `main`, then:

   ```bash
   git tag v1.5.0
   git push origin v1.5.0
   ```

GitHub Actions (`.github/workflows/release.yml`) does the rest:

```text
verify-tag  tag is vMAJOR.MINOR.PATCH, equals the packaged version, CHANGELOG has the section, commit is on main
   -> tests     full suite on the plain and the encrypted storage backend (tests.yml)
   -> windows   Linux container build with the pinned toolchain, then the REAL Windows smoke test (windows-build.yml)
   -> publish   SHA256SUMS computed from the exact files, checked, then the GitHub Release is created
```

`publish` runs only if every earlier job succeeded, and is the only job with write access. Nothing is uploaded by hand.

The release contains:

```text
ScreenTime-1.5.0-setup.exe        installer (per-user, no admin)
ScreenTime-1.5.0-portable.zip     portable build
SHA256SUMS                        '<sha256>  <file>' for every file above (sha256sum -c compatible)
build-manifest.txt                version, commit, build date, tool versions
```

### Retrying

Re-running a failed release workflow is safe. `scripts/ci/publish-release.sh` creates the release as a **draft**
with all assets attached and publishes it last; if a release already exists it uploads only **missing** assets and
never replaces one that is published, so a retry cannot change bytes users already verified, and never creates a
second release. Runs for one tag are serialised (`concurrency`). If the *build* must be redone after a release was
already published, delete the release and the tag on GitHub and release as a new patch version.

### Pull requests and `main`

`ci.yml` runs the tests and the same Windows build + real-Windows smoke test, and publishes nothing.

### Required GitHub configuration

* No secrets are needed: the workflows use the built-in `GITHUB_TOKEN` (`contents: write` only in `publish`).
* Settings -> Actions -> General -> Workflow permissions may stay at *read*; the workflow requests what it needs.
* Optional: protect `v*` tags (Settings -> Rules) so only maintainers can push release tags.
* Windows code signing is not part of this pipeline; installers are unsigned (SmartScreen warns).

## Reproducible Windows builds

Pinned in `packaging/windows/`: `toolchain.env` (the one place the Inno Setup version, URL and MSYS2 repository
are written), `toolchain.lock` (SHA-256 of the Inno Setup installer; verification is mandatory),
and `msys2.lock` (exact MSYS2 package files with SHA-256). Builds **require** `msys2.lock` and never resolve
packages themselves; every download is hash-checked.

Changing the lock is a deliberate act: run the **update-msys2-lock** workflow (Actions tab -> Run workflow). It
regenerates the lock from the pinned repository and pushes it to branch `chore/update-msys2-lock`; review the diff,
open a PR and merge. Locally: `./scripts/build-windows.sh --update-lock` (also records a missing Inno checksum).

**First-time setup:** a repository that does not yet contain `packaging/windows/msys2.lock` fails its Windows build
with a message saying so. Run the workflow above once and merge the result.

What is and is not reproducible: same commit + same `msys2.lock` + same `toolchain.env` give the same bundle and
portable zip contents. The Inno Setup installer is not bit-for-bit reproducible, and the base image is the floating
`ubuntu:24.04` tag with unpinned Ubuntu packages (the manifest records the Wine version).

## Building locally

```bash
git pull
./scripts/build-windows.sh        # or: make windows
```

Result in `dist/`: the setup, the portable zip, `build-manifest.txt`, `SHA256SUMS`. Only a container engine is needed
(`sudo pacman -S podman` or docker; rootless podman needs `/etc/subuid` and `/etc/subgid`), about 6 GB of disk and an
internet connection for the first build. The version comes from `pyproject.toml`.

| Option | What it does |
| --- | --- |
| `--clean` | rebuild the bundle from scratch; keeps downloaded packages |
| `--purge` | also delete the download cache, the Wine prefix and the build image |
| `--update-lock` | re-resolve the MSYS2 packages and rewrite `msys2.lock` (the only way it changes) |
| `--verbose` | show every command (and the image build output) |
| `--non-interactive` | no TTY; plain output (CI) |
| `--skip-smoke` | skip the Wine smoke test (local use only; CI never does) |
| `--engine podman\|docker` | choose the engine (or set `CONTAINER_ENGINE`) |

Cache: `~/.cache/screentime/windows-build/` (override with `SCREENTIME_CACHE`).

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `msys2.lock is missing` | run the `update-msys2-lock` workflow (or `--update-lock`), review, commit |
| `toolchain.lock has no pinned SHA-256` | bump `INNO_VERSION`/`INNO_URL` in `toolchain.env` together with `toolchain.lock` |
| `no container engine found` | `sudo pacman -S podman` |
| `could not download ...` / hash mismatch for an MSYS2 file | MSYS2 removed or changed it: update the lock and review the diff |
| `UNRESOLVED DLL imports` | add the DLL name to `system-dlls.txt` if it ships with Windows, else add the package to `packages.txt` |
| Wine smoke test fails | read the printed tail; `--clean --verbose` |
| weird state | `./scripts/build-windows.sh --purge` |

Arch package: unchanged, `makepkg -si`.
