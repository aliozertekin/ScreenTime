"""Static validation of a Windows bundle, runnable on Linux.

Two checks that catch the real failure modes of a hand-assembled GTK bundle
without needing Windows:

  required_files   the files GTK4/libadwaita/PyGObject/Python need at runtime
  dependencies     every DLL imported by any .dll/.exe/.pyd in the bundle is
                   either in the bundle or a known Windows system DLL
                   (the "missing DLL" error users would otherwise see)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable

# Windows-provided libraries (present on every Windows 10/11 install).
SYSTEM_DLLS = {
    "kernel32", "kernelbase", "ntdll", "user32", "gdi32", "gdi32full", "advapi32", "shell32", "ole32",
    "oleaut32", "comdlg32", "comctl32", "shlwapi", "version", "winmm", "imm32", "dwmapi", "uxtheme",
    "crypt32", "bcrypt", "ncrypt", "rpcrt4", "secur32", "userenv", "iphlpapi", "dnsapi", "mswsock",
    "ws2_32", "wsock32", "msvcrt", "ucrtbase", "shcore", "propsys", "setupapi", "cfgmgr32", "powrprof",
    "psapi", "netapi32", "wtsapi32", "msimg32", "mpr", "dbghelp", "dwrite", "d2d1", "d3d11", "d3d12",
    "dxgi", "dcomp", "opengl32", "glu32", "winspool", "normaliz", "wldap32", "avrt", "usp10", "windowscodecs",
    "uiautomationcore", "oleacc", "hid", "winhttp", "wininet", "bcryptprimitives", "msvcp_win", "combase",
    "sechost", "cryptbase", "cabinet", "shfolder", "d3dcompiler_47", "imagehlp", "mscoree", "ncrypt",
}
EXTRA_SYSTEM_PREFIXES = ("api-ms-win-", "ext-ms-")


def required_patterns(py_ver_glob: str = "python3.*") -> list[str]:
    return [
        "bin/python.exe", "bin/pythonw.exe", "bin/screentime-daemon.exe", "bin/screentime-gui.exe",
        "bin/screentime-cli.exe", "bin/libgtk-4-*.dll", "bin/libadwaita-1-*.dll", "bin/libglib-2.0-*.dll",
        "bin/libgobject-2.0-*.dll", "bin/libgio-2.0-*.dll", "bin/libgirepository-*.dll", "bin/libcairo-2.dll",
        "bin/libgdk_pixbuf-2.0-*.dll", "bin/gdk-pixbuf-query-loaders.exe",
        "lib/girepository-1.0/Gtk-4.0.typelib", "lib/girepository-1.0/Adw-1.typelib",
        "lib/girepository-1.0/Gdk-4.0.typelib", "lib/girepository-1.0/GLib-2.0.typelib",
        "lib/girepository-1.0/GObject-2.0.typelib", "lib/girepository-1.0/Gio-2.0.typelib",
        "lib/girepository-1.0/Pango-1.0.typelib", "lib/girepository-1.0/cairo-1.0.typelib",
        "lib/girepository-1.0/GdkPixbuf-2.0.typelib",
        "share/glib-2.0/schemas/gschemas.compiled", "share/icons/Adwaita/index.theme",
        "screentime.ico", "ScreenTime.cmd", "app/screentime/daemon.py", "app/screentime/gui/app.py",
        f"lib/{py_ver_glob}/site-packages/gi/__init__.py", f"lib/{py_ver_glob}/site-packages/psutil/__init__.py",
        f"lib/{py_ver_glob}/site-packages/cryptography/__init__.py",
        f"lib/{py_ver_glob}/site-packages/screentime-app.pth", f"lib/{py_ver_glob}/os.py",
    ]


def required_files(bundle: Path) -> list[str]:
    """Missing required paths (glob patterns allowed)."""
    return [pat for pat in required_patterns() if not list(bundle.glob(pat))]


def pe_imports(path: Path) -> set[str]:
    """Lower-case DLL names imported by a PE file (normal + delay-load)."""
    import pefile
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        names = {e.dll.decode("ascii", "replace").lower() for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])}
        names |= {e.dll.decode("ascii", "replace").lower() for e in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])}
        return names
    finally:
        pe.close()


def is_system(dll: str, extra: Iterable[str] = ()) -> bool:
    stem = re.sub(r"\.(dll|drv|exe)$", "", dll.lower())
    return stem in SYSTEM_DLLS or stem in set(extra) or dll.lower().startswith(EXTRA_SYSTEM_PREFIXES)


def check_dependencies(bundle: Path, imports: Callable[[Path], set] = pe_imports,
                       extra_system: Iterable[str] = ()) -> dict[str, set]:
    """{missing DLL name: {files that need it}} -- empty when the bundle is self-contained."""
    pes = [p for p in bundle.rglob("*") if p.suffix.lower() in (".dll", ".exe", ".pyd") and p.is_file()]
    present = {p.name.lower() for p in pes}
    missing: dict[str, set] = {}
    for p in pes:
        try:
            deps = imports(p)
        except Exception as e:                                      # noqa: BLE001 - unreadable PE = a build problem worth naming
            missing.setdefault(f"<unparseable: {type(e).__name__}>", set()).add(str(p.relative_to(bundle)))
            continue
        for d in deps:
            if d not in present and not is_system(d, extra_system):
                missing.setdefault(d, set()).add(str(p.relative_to(bundle)))
    return missing


def main(argv=None) -> int:
    import sys
    bundle = Path(argv[0] if argv else sys.argv[1])
    extra = []
    f = Path(__file__).with_name("system-dlls.txt")
    if f.exists():
        extra = [ln.strip().lower() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    bad = False
    miss = required_files(bundle)
    if miss:
        bad = True
        print("MISSING required bundle files:\n  " + "\n  ".join(miss))
    deps = check_dependencies(bundle, extra_system=extra)
    if deps:
        bad = True
        print("UNRESOLVED DLL imports (not in the bundle, not a known Windows DLL):")
        for dll, users in sorted(deps.items()):
            print(f"  {dll}  <- {', '.join(sorted(users)[:3])}{' ...' if len(users) > 3 else ''}")
        print("If one of these ships with Windows, add its name to packaging/windows/system-dlls.txt.")
    if not bad:
        print("bundle check OK: required files present, every imported DLL is bundled or a Windows system DLL")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
