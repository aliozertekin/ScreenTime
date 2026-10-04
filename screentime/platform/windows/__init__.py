"""Windows implementations of the platform interfaces (see platform/base.py).

Importing this package never touches Win32: everything that calls into Windows
takes an injectable `Win32Api`, constructed lazily, so the whole package is
importable and unit-testable on any OS.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import PlatformPaths

if TYPE_CHECKING:
    from .. import Platform


def build_platform() -> "Platform":
    import sys
    import time
    from .. import Platform
    from .paths import WindowsPaths
    clock = time.monotonic
    if sys.platform == "win32":                      # only a real Windows has the real API
        from .clock import make_unbiased_clock
        clock = make_unbiased_clock()
    return Platform(name="windows", paths=WindowsPaths(), monotonic=clock)
