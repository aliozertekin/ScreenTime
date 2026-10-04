r"""Windows locations. Per-user, no admin, no hard-coded profile paths.

  data    %LOCALAPPDATA%\ScreenTime\screentime.sec      (the encrypted store)
  config  %APPDATA%\ScreenTime                          (security.json, key fallback)
  runtime %LOCALAPPDATA%\ScreenTime\run                 (status file)
  logs    %LOCALAPPDATA%\ScreenTime\logs                (daemon.log, INFO only)

The key-file fallback lives under %APPDATA% (Roaming) on purpose: it is a
DPAPI-wrapped blob that only decrypts for this user on this machine, so even if
a profile is roamed the blob is useless elsewhere, and it stays out of the data
directory exactly like the Linux layout. Environment variables are read at call
time so tests can redirect them; if one is missing we ask the shell for the
known folder instead of guessing C:\Users\<name>.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..base import PlatformPaths

APP_DIR = "ScreenTime"


def _known_folder(env_var: str, fallback_parts: tuple) -> Path:
    v = os.environ.get(env_var)
    if v:
        return Path(v)
    # Last resort only; every normal Windows session defines both variables.
    return Path.home().joinpath(*fallback_parts)


class WindowsPaths(PlatformPaths):
    def _local(self) -> Path:
        return _known_folder("LOCALAPPDATA", ("AppData", "Local")) / APP_DIR

    def data_dir(self) -> Path:
        d = self._local()
        d.mkdir(parents=True, exist_ok=True)
        # %LOCALAPPDATA% is already ACL'd to the user (+ SYSTEM/Administrators);
        # POSIX modes do not exist here, so no chmod is attempted.
        return d

    def config_dir(self) -> Path:
        return _known_folder("APPDATA", ("AppData", "Roaming")) / APP_DIR

    def runtime_dir(self) -> Path:
        d = self._local() / "run"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def lock_dir(self) -> Path:
        return self.runtime_dir()

    def log_dir(self) -> Path:
        d = self._local() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d
