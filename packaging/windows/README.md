# Windows packaging

Everything here is driven from Linux by **`./scripts/build-windows.sh`** (see [docs/RELEASING.md](../../docs/RELEASING.md)).
Nothing in this folder needs a Windows machine, a VM, MSYS2 on the host, or GitHub Actions.

## How the build works

```
host:       scripts/build-windows.sh          picks podman/docker, reads the version, caches, reports
container:  Containerfile                      Ubuntu 24.04 + Wine + Python + zstd + rsvg/ImageMagick
            container/build.sh                 the pipeline below
```

1. **`msys2_fetch.py`** gets the Windows runtime. MSYS2 packages are plain zstd tarballs, so we do *not* run
   MSYS2 or pacman under Wine. We read the package database, resolve the dependency closure of `packages.txt`
   (Python, PyGObject, pycairo, psutil, cryptography, GTK4, libadwaita, librsvg, icon themes), pin it in
   **`msys2.lock`** (file names + SHA-256), download with hash verification and unpack into one tree.
2. **`assemble_bundle.py`** builds the relocatable folder: prunes headers/docs/locales/tcl-tk/tests, keeps DLLs,
   typelibs, schemas (compiled with the Linux `glib-compile-schemas`; the format is OS independent), icons,
   fontconfig and the Python stdlib; copies the app; makes `screentime.ico` from the SVG; creates the
   `screentime-{daemon,gui,cli}.exe` launchers and `ScreenTime.cmd`.
3. **`bundle_check.py`** validates the bundle statically: required GTK/Adw/GI/Python files exist, and **every DLL
   imported by every `.dll/.exe/.pyd` is either in the bundle or a Windows system DLL** (this is the "missing DLL"
   error a user would otherwise hit). Unknown system DLLs can be allow-listed in `system-dlls.txt`.
4. **Wine smoke test** imports GTK4, libadwaita, PyGObject, cairo, psutil, cryptography and the ScreenTime Windows
   modules from the bundle only, then starts the GUI for 25 s looking for missing-library errors.
5. **Inno Setup 6** (`installer.iss`, run under Wine; unchanged from the validated script) builds the per-user,
   no-admin installer.
6. **`make_zip.py`** builds the deterministic portable zip; **`make_manifest.py`** writes `build-manifest.txt`
   (version, commit, date, SHA-256 of both artifacts, tool versions) and `SHA256SUMS`.

## Files

| File | Purpose |
| --- | --- |
| `toolchain.env` | pins: base image, Inno Setup version/URL, MSYS2 environment and mirrors |
| `packages.txt` | root MSYS2 packages |
| `msys2.lock` | exact package files + SHA-256. **Required**: builds refuse to run without it. Changed only by the `update-msys2-lock` workflow / `--update-lock` |
| `toolchain.lock` | SHA-256 of the Inno Setup installer; verification is mandatory (the version lives only in `toolchain.env`) |
| `system-dlls.txt` | extra Windows DLL names the bundle check should accept |
| `installer.iss` | Inno Setup script |
| `smoke.ps1` | optional check to run on a real Windows machine against the bundle or installer |

## Why not PyInstaller / a Python-only exe

GTK4 + libadwaita need typelibs, schemas, icons, loaders and ~100 DLLs that PyInstaller only finds through hooks, and
a missing one shows up at run time on the user's machine. Taking the known-good MSYS2 packages whole, then checking
the DLL graph statically, is more predictable.

## Reproducibility and limits

* Same commit + same `msys2.lock` + same `toolchain.env` give the same bundle and the same zip bytes
  (timestamps follow the commit date). The Inno Setup `.exe` is not bit-for-bit reproducible.
* The base image is a floating `ubuntu:24.04` tag and Ubuntu packages (Wine) are not pinned; the manifest records the
  Wine version used.
* The first lock is created over HTTPS from the official MSYS2 mirror and then pinned by hash; package signatures
  are not verified.
* Status: the container build, Inno Setup under Wine and the real-Windows installer smoke test run in GitHub Actions
  on every pull request, push to `main` and release (1.4.0 passed them). The smoke test does not cover real
  sleep/resume or a restart-and-autostart cycle. See "Verification status" in
  [docs/windows-developer.md](../../docs/windows-developer.md).
