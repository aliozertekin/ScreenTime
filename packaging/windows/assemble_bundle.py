#!/usr/bin/env python3
"""Assemble the relocatable Windows application folder (dist/ScreenTime) from an
unpacked MSYS2 ucrt64 tree plus the ScreenTime sources. Runs on Linux.

Layout (identical to what installer.iss, smoke.ps1 and the README describe):
  bin/        python.exe, pythonw.exe, all DLLs, screentime-{daemon,gui,cli}.exe
  lib/ share/ etc/   Python stdlib + site-packages, typelibs, schemas, icons, fontconfig
  app/screentime/    the application   (found through a .pth file next to the stdlib)
  ScreenTime.cmd, screentime-diagnose.cmd, screentime.ico, LICENSE, build-manifest.txt

Only the Linux-native tools glib-compile-schemas (data format is OS independent),
rsvg-convert and ImageMagick are used; nothing Windows has to run to assemble it.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

KEEP_BIN_EXE = ("python.exe", "pythonw.exe", "gspawn-win64-helper.exe", "gspawn-win64-helper-console.exe",
                "gdk-pixbuf-query-loaders.exe")
SKIP_TOP = {"include", "ssl", "var", "tmp", "doc", "man", "licenses_unused"}
SKIP_PATH_GLOBS = [
    "share/doc*", "share/man*", "share/info*", "share/locale*", "share/gtk-doc*", "share/gir-1.0*",
    "share/vala*", "share/aclocal*", "share/bash-completion*", "share/pkgconfig*", "share/applications*",
    "share/readline*", "share/gdb*", "share/gettext*", "share/themes/Default/gtk-2.0*",
    "lib/pkgconfig*", "lib/cmake*", "lib/gettext*", "lib/engines*", "lib/ossl-modules*",
    "lib/tcl*", "lib/tk*", "lib/itcl*", "lib/tdbc*", "lib/thread*", "lib/sqlite*",
    "lib/python3.*/test", "lib/python3.*/test/*", "lib/python3.*/idlelib*", "lib/python3.*/tkinter*",
    "lib/python3.*/turtledemo*", "lib/python3.*/ensurepip*", "lib/python3.*/lib2to3*",
    "lib/python3.*/site-packages/pip*", "lib/python3.*/site-packages/setuptools*",
    "lib/python3.*/site-packages/pkg_resources*", "lib/python3.*/site-packages/_distutils_hack*",
    "lib/python3.*/config-3.*", "lib/python3.*/site-packages/*/tests*", "lib/python3.*/site-packages/*/test",
    "lib/python3.*/__pycache__*",
]
SKIP_SUFFIXES = (".a", ".la", ".dll.a", ".def", ".h", ".hpp", ".pc", ".cmake", ".c", ".o", ".exe.manifest")


def skip_dir(rel: str) -> bool:
    p = PurePosixPath(rel)
    return (p.parts[0] in SKIP_TOP or "__pycache__" in p.parts
            or any(fnmatch.fnmatch(rel, g) for g in SKIP_PATH_GLOBS))


def skip_file(rel: str) -> bool:
    p = PurePosixPath(rel)
    if len(p.parts) == 1:                                           # loose files at the prefix root
        return True
    if skip_dir(p.parent.as_posix()) or rel.endswith(SKIP_SUFFIXES):
        return True
    if p.parts[0] == "bin":
        return not (p.name.lower().endswith(".dll") or p.name.lower() in KEEP_BIN_EXE)   # drop every other tool/script
    return any(fnmatch.fnmatch(rel, g) for g in SKIP_PATH_GLOBS)


def copy_runtime(src: Path, out: Path) -> int:
    n = 0
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src).as_posix()
        base = "" if rel_root == "." else rel_root + "/"
        dirs[:] = sorted(d for d in dirs if not skip_dir(base + d))
        for f in sorted(files):
            rel = base + f
            if skip_file(rel):
                continue
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / f, dst, follow_symlinks=True)
            n += 1
    return n


def python_dir(out: Path) -> Path:
    cands = sorted(p for p in (out / "lib").glob("python3.*") if p.is_dir())
    if not cands:
        raise SystemExit("assemble: no lib/python3.* in the runtime")
    return cands[-1]


def copy_app(repo: Path, out: Path) -> None:
    dst = out / "app" / "screentime"
    shutil.copytree(repo / "screentime", dst, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "gnome-extension", "kde-script"))   # Linux-only desktop helpers


def write_crlf(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


def make_icon(svg: Path, out_ico: Path, tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    pngs = []
    for size in (16, 32, 48, 64, 128, 256):
        png = tmp / f"icon-{size}.png"
        subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(png), str(svg)], check=True)
        pngs.append(str(png))
    subprocess.run(["convert", *pngs, str(out_ico)], check=True)


def write_launchers(out: Path, version: str) -> None:
    b = out / "bin"
    shutil.copy2(b / "pythonw.exe", b / "screentime-daemon.exe")
    shutil.copy2(b / "pythonw.exe", b / "screentime-gui.exe")
    shutil.copy2(b / "python.exe", b / "screentime-cli.exe")
    site = python_dir(out) / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "screentime-app.pth").write_text("../../../app\n")      # lib/python3.X/site-packages -> <bundle>/app
    write_crlf(out / "ScreenTime.cmd",
               '@echo off\nstart "" "%~dp0bin\\screentime-gui.exe" -m screentime.gui.app\n')
    write_crlf(out / "screentime-diagnose.cmd",
               '@echo off\n"%~dp0bin\\screentime-cli.exe" -m screentime.diagnostics %*\npause\n')
    write_crlf(out / "VERSION.txt", f"ScreenTime {version}\n")


def compile_schemas(out: Path) -> None:
    d = out / "share" / "glib-2.0" / "schemas"
    if not d.is_dir():
        raise SystemExit("assemble: share/glib-2.0/schemas missing")
    subprocess.run(["glib-compile-schemas", str(d)], check=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", type=Path, required=True, help="unpacked MSYS2 env dir, e.g. stage/ucrt64")
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--lock", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    a = ap.parse_args(argv)
    if not (a.runtime / "bin" / "python.exe").exists():
        print(f"assemble: {a.runtime}/bin/python.exe missing -- the MSYS2 runtime is incomplete", file=sys.stderr)
        return 1
    if a.out.exists():
        shutil.rmtree(a.out)
    a.out.mkdir(parents=True)
    n = copy_runtime(a.runtime, a.out)
    print(f"copied {n} runtime files")
    copy_app(a.repo, a.out)
    write_launchers(a.out, a.version)
    compile_schemas(a.out)
    make_icon(a.repo / "data" / "icons" / "screentime.svg", a.out / "screentime.ico", a.work / "icon")
    shutil.copy2(a.out / "screentime.ico", a.out / "app" / "screentime" / "resources" / "screentime.ico")
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        if (a.repo / name).exists():
            shutil.copy2(a.repo / name, a.out / name)
    shutil.copy2(a.lock, a.out / "build-manifest.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
