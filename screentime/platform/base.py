"""Interfaces shared by every platform, plus the POSIX path provider.

Interfaces (see the matching modules for Linux/Windows implementations):

  WindowDetector      screentime/window_detector.py   (Linux)  platform/windows/window_detector.py
  IdleDetector        screentime/idle_detector.py     (Linux)  platform/windows/idle_detector.py
  PowerEventSource    -- below --                              platform/windows/power_events.py
  AutostartManager    screentime/autostart.py         (Linux)  platform/windows/autostart.py
  InstanceLock        screentime/instance_lock.py     (Linux)  platform/windows/instance_lock.py
  SecureKeyStore      screentime/keystore.py backends          platform/windows/keystore.py
  PlatformPaths       -- below --                              platform/windows/paths.py
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional


class PlatformPaths(ABC):
    @abstractmethod
    def data_dir(self) -> Path:
        """Where the encrypted store lives. Created (private) on demand."""

    @abstractmethod
    def config_dir(self) -> Path:
        """Non-secret settings and the key-file fallback. Deliberately NOT inside data_dir()."""

    @abstractmethod
    def runtime_dir(self) -> Path:
        """Ephemeral per-session files (status file). Not persisted, not sensitive."""

    @abstractmethod
    def lock_dir(self) -> Path:
        """Where file-based locks live."""

    def log_dir(self) -> Optional[Path]:
        """None = log to stderr/journal only."""
        return None


class PosixPaths(PlatformPaths):
    """XDG paths, resolved at call time (tests redirect them with env vars)."""

    def data_dir(self) -> Path:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        d = Path(base) / "screentime"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    def config_dir(self) -> Path:
        return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "screentime"

    def runtime_dir(self) -> Path:
        r = os.environ.get("XDG_RUNTIME_DIR")
        return Path(r) if r else Path(f"/tmp/screentime-{os.getuid()}")

    def lock_dir(self) -> Path:
        return self.runtime_dir()


# --------------------------------------------------------------- power events
class PowerEventSource(ABC):
    """Tells the daemon about suspend/resume (and session end).

    Contract:
      * `on_suspend` is called BEFORE the machine sleeps whenever the OS gives
        us the chance; the source waits (bounded) for it to return.
      * `on_resume` is called after wake.
      * `on_session_end` is called when the user is logging off / Windows is
        shutting down, so the daemon can close its session cleanly.
      * Display-off, screen lock and idle are NOT suspend and never call these.
      * Callbacks are invoked on the daemon's main thread via `dispatch`.
    """
    name = "none"

    @abstractmethod
    def start(self, on_suspend: Callable[[], None], on_resume: Callable[[], None],
              on_session_end: Callable[[], None]) -> bool:
        """Begin delivering events. False if the backend could not start."""

    @abstractmethod
    def stop(self) -> None:
        ...


class NullPowerEventSource(PowerEventSource):
    name = "none"

    def start(self, on_suspend, on_resume, on_session_end) -> bool:
        return False

    def stop(self) -> None:
        pass
