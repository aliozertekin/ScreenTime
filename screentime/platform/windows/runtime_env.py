"""Run-time environment fix-ups for the packaged (relocatable) Windows build.

gdk-pixbuf finds its image loaders (SVG icons, PNG, ...) through a `loaders.cache`
file that records ABSOLUTE paths. A bundle that can be installed anywhere, or
unzipped to a USB stick, cannot ship one, so on first launch we ask the bundled
`gdk-pixbuf-query-loaders.exe` to write a cache for *this* location into the
per-user runtime folder and point GDK_PIXBUF_MODULE_FILE at it. It is rebuilt
automatically if the bundle moves or its loaders change.

No effect (and no cost) outside the packaged build or on Linux.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Optional

log = logging.getLogger("screentime.runtime_env")

CREATE_NO_WINDOW = 0x08000000


def bundle_root(executable: Optional[str] = None) -> Optional[Path]:
    """<bundle> if running from the packaged layout (<bundle>/bin/*.exe), else None."""
    exe = Path(executable or sys.executable)
    root = exe.resolve().parent.parent
    return root if (root / "bin" / "gdk-pixbuf-query-loaders.exe").exists() else None


def _loaders_dir(root: Path) -> Optional[Path]:
    hits = sorted((root / "lib" / "gdk-pixbuf-2.0").glob("*/loaders"))
    return hits[-1] if hits else None


def prepare(env: Optional[MutableMapping[str, str]] = None, executable: Optional[str] = None,
            cache_dir: Optional[Path] = None,
            run: Callable[..., "subprocess.CompletedProcess"] = subprocess.run) -> Optional[Path]:
    """Ensure GDK_PIXBUF_MODULE_FILE points at a valid cache. Returns its path, or None if nothing was done."""
    env = os.environ if env is None else env
    if env.get("GDK_PIXBUF_MODULE_FILE"):
        return None                                        # the user/packager already decided
    root = bundle_root(executable)
    if root is None:
        return None
    loaders = _loaders_dir(root)
    if loaders is None:
        log.info("no gdk-pixbuf loaders in the bundle; images other than built-ins may not load")
        return None
    if cache_dir is None:
        from .. import paths
        cache_dir = paths().runtime_dir()
    stamp = hashlib.sha256(f"{root}|{sorted(p.name for p in loaders.glob('*.dll'))}".encode()).hexdigest()[:16]
    cache = Path(cache_dir) / f"pixbuf-loaders-{stamp}.cache"
    if not cache.exists():
        child_env: Mapping[str, str] = {**env, "GDK_PIXBUF_MODULEDIR": str(loaders)}
        try:
            res = run([str(root / "bin" / "gdk-pixbuf-query-loaders.exe")], capture_output=True, text=True,
                      timeout=30, env=dict(child_env), creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("could not generate the gdk-pixbuf loader cache: %s", e)
            return None
        if res.returncode != 0 or not res.stdout.strip():
            log.warning("gdk-pixbuf-query-loaders failed (%s)", res.returncode)
            return None
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(res.stdout, encoding="utf-8")
        tmp.replace(cache)
    env["GDK_PIXBUF_MODULE_FILE"] = str(cache)
    return cache


def configure_renderer(env: Optional[MutableMapping[str, str]] = None) -> bool:
    """Default GTK to its Cairo (software) renderer on Windows.

    GTK4's default GL renderer needs a working WGL/ANGLE driver. In virtual machines, remote desktops, CI runners
    and on some old drivers context creation fails and the window never appears (or the process exits). ScreenTime
    is a lists-and-charts dashboard that the Cairo renderer draws fine, so reliability wins over GPU use. Anyone
    can still opt in to GL by setting GSK_RENDERER themselves (for example GSK_RENDERER=gl). Returns True if set."""
    env = os.environ if env is None else env
    if "GSK_RENDERER" in env:
        return False
    env["GSK_RENDERER"] = "cairo"
    return True
