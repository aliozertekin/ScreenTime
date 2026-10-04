# Releasing (maintainers)

Back to the [README](../README.md). Windows users never build anything: they download
`ScreenTime-<version>-setup.exe` from the project's **GitHub Releases** page, which you upload from your Linux machine.

## Windows build: one command

```bash
git pull
./scripts/build-windows.sh
```

(or `make windows`). Result in `dist/`:

```
dist/ScreenTime-<version>-setup.exe       <- upload this to the release
dist/ScreenTime-<version>-portable.zip
dist/build-manifest.txt                   version, commit, date, SHA-256 of both files, tool versions
dist/SHA256SUMS
```

The version comes from `pyproject.toml` (the script stops if `screentime/__init__.py` disagrees).

### Host prerequisites

Only a container engine: `sudo pacman -S podman` (or `docker`). Rootless podman needs `/etc/subuid` and `/etc/subgid`
entries for your user (`podman info` tells you). About 6 GB free disk and an internet connection for the first build.
No Wine, MSYS2, Inno Setup or Windows is installed on your host; everything runs in a container.

### Options

| Option | What it does |
| --- | --- |
| `--clean` | rebuild the bundle from scratch; keeps downloaded packages |
| `--purge` | also delete the download cache, the Wine prefix and the build image |
| `--update-lock` | re-resolve the MSYS2 packages to their latest versions and rewrite `msys2.lock` |
| `--verbose` | show every command (and the image build output) |
| `--non-interactive` | no TTY; plain output (for scripts) |
| `--skip-smoke` | skip the Wine smoke test (use only if Wine itself is misbehaving) |
| `--engine podman\|docker` | choose the engine (or set `CONTAINER_ENGINE`) |

### Caching

Cache lives in `~/.cache/screentime/windows-build/` (override with `SCREENTIME_CACHE`):
`downloads/` (MSYS2 packages, Inno Setup), `wineprefix/` (Inno Setup installed under Wine), `stage/` and `work/`.
The first build downloads roughly 1-2 GB; later builds reuse it and take minutes. The container image is rebuilt only
when `packaging/windows/Containerfile` or `toolchain.env` change.

### First build: commit the lock files

The first run creates `packaging/windows/msys2.lock` and `packaging/windows/toolchain.lock` and tells you so.
Review and commit them; from then on builds use exactly those package files and fail if a hash differs.
Refresh deliberately with `--update-lock` and review the diff.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `no container engine found` | `sudo pacman -S podman` |
| `'podman info' failed` | rootless podman: add subuid/subgid ranges (`sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER`), then `podman system migrate` |
| image build fails | re-run with `--verbose`; check network access to Ubuntu mirrors |
| `could not download ... ` / hash mismatch for an MSYS2 file | MSYS2 removed or changed it: run with `--update-lock` and review the lock diff |
| `UNRESOLVED DLL imports` | the bundle imports a DLL that is neither bundled nor a known Windows DLL. If it ships with Windows add its name to `packaging/windows/system-dlls.txt`; otherwise a package is missing from `packages.txt` |
| Wine smoke test fails | read the printed tail; `--clean --verbose`. If Wine itself is the problem, `--skip-smoke` and test on Windows with `packaging/windows/smoke.ps1` |
| Inno Setup did not install | `--purge` (resets the Wine prefix), then rebuild |
| weird state | `./scripts/build-windows.sh --purge` |

## Publishing a release

1. Bump the version in `pyproject.toml`, `PKGBUILD` and `screentime/__init__.py`; move the `CHANGELOG.md` notes under the
   new version (see [DEVELOPMENT.md](DEVELOPMENT.md#versioning-and-releases)); `python -m pytest -q`.
2. Commit, tag (`git tag vX.Y.Z && git push --tags`).
3. `./scripts/build-windows.sh`
4. On GitHub: **Releases -> Draft a new release**, choose the tag, paste the changelog section, drag in
   `ScreenTime-X.Y.Z-setup.exe` (plus the portable zip and `SHA256SUMS`), publish.
   With the GitHub CLI instead: `gh release create vX.Y.Z dist/ScreenTime-X.Y.Z-setup.exe dist/ScreenTime-X.Y.Z-portable.zip dist/SHA256SUMS --notes-file <file>`.
5. Before announcing: install it on a real Windows machine, or run `packaging/windows/smoke.ps1 -Mode Installer -Setup <path>` there.
   The installer is unsigned (SmartScreen will warn); code signing is not part of this pipeline.

Arch package: unchanged, `makepkg -si`.
