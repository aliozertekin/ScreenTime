"""Logging for the windowless Windows daemon (pythonw has no stderr).

A small rotating file under %LOCALAPPDATA%\\ScreenTime\\logs. It is capped at
INFO on purpose: DEBUG lines name the focused application, and ScreenTime must
never leave a plaintext usage trail next to the encrypted store. `--verbose`
adds DEBUG output to the console only (when there is one).
"""
from __future__ import annotations

import logging
import logging.handlers
import sys

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure(verbose: bool = False, name: str = "daemon.log") -> None:
    from .. import paths
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    try:
        fh = logging.handlers.RotatingFileHandler(paths().log_dir() / name, maxBytes=512 * 1024,
                                                  backupCount=2, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(fh)
    except OSError:
        pass                                     # no log file is better than no daemon
    if sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG if verbose else logging.INFO)
        sh.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(sh)


_crash_file = None


def install_crash_handlers(name: str) -> None:
    """A windowless (pythonw) process has no console, so an uncaught exception or a native crash would vanish.
    Send Python tracebacks to the log and native faults (access violations in GTK/DLLs) to <name>.crash."""
    import faulthandler
    global _crash_file
    log = logging.getLogger("screentime.crash")

    def hook(exc_type, exc, tb):
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook
    try:
        from .. import paths
        _crash_file = open(paths().log_dir() / f"{name}.crash", "w", encoding="utf-8")   # kept open on purpose
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError:
        pass
