# Windows packaging

Approach: **MSYS2 (MINGW64) runtime, bundled** -- not PyInstaller.
ScreenTime needs GTK4 + libadwaita + PyGObject + GObject-Introspection typelibs +
gdk-pixbuf loaders + GSettings schemas. MSYS2 is the platform both GTK and
PyGObject document and test on Windows, and its GLib/GTK builds are relocatable,
so we copy the exact runtime the app was tested against into one folder.
PyInstaller can freeze PyGObject apps, but each typelib/loader/schema/DLL has to
be discovered through hooks and a missing one only shows at runtime; here the
dependency closure is computed from the real DLL graph (`ldd`) and verified by
starting the bundled interpreter and importing Gtk/Adw.

Layout of the result (`dist/ScreenTime/`):

    bin/                python.exe, pythonw.exe, all DLLs
    bin/screentime-daemon.exe   copy of pythonw.exe  (Task Manager shows this name)
    bin/screentime-gui.exe      copy of pythonw.exe
    lib/ share/         Python stdlib + site-packages, typelibs, schemas, icons
    app/screentime/     the application
    ScreenTime.cmd      (portable build only)

Pipeline (`scripts/build-windows.ps1`, also run by `.github/workflows/windows.yml`):

1. `bundle.sh`   inside MSYS2: install pinned packages, assemble `dist/ScreenTime`.
2. `smoke.ps1 -Bundle`  import Gtk4/Adw, run diagnostics from the bundle.
3. Inno Setup    `installer.iss` -> `ScreenTime-<version>-setup.exe` (per-user, no admin).
4. zip           `ScreenTime-<version>-portable.zip`.
5. `smoke.ps1 -Installer`  silent install, daemon start/stop, uninstall, assertions.

Reproducibility: `bundle.sh` writes `build-manifest.txt` (every MSYS2 package
and version used). Commit the first good one as `msys2-packages.lock`;
afterwards `bundle.sh --verify-lock` fails the build if MSYS2's rolling repos
drifted, and CI pins the runner image, action versions and Inno Setup version.

STATUS: written but not yet exercised on a clean Windows machine -- see the
"Verification status" section of docs/windows-developer.md.
