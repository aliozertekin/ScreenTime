"""Offline Steam AppID -> game name resolution.

Proton/Wine games run under a window class of ``steam_app_<AppID>`` and carry no
.desktop file of their own, so identity resolution alone can only show
"Steam App 2620". Steam records every installed game locally, so we read that:

    <steam root>/steamapps/libraryfolders.vdf   -> every library location
    <library>/steamapps/appmanifest_<AppID>.acf -> "name" of the installed game

Everything here is local file parsing: no Steam Web API, no network of any kind.

Cost model: the daemon resolves identity on every poll, so lookups must be
cheap. Results (including "unknown") are cached, and the cache is dropped only
when Steam's own files change -- detected by comparing mtimes of
libraryfolders.vdf and each library's steamapps directory (adding/removing a
game adds/removes a manifest, which bumps the directory mtime). That check is
itself rate-limited, so a steady-state poll does no filesystem work at all.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import platform as _platform

log = logging.getLogger("screentime.steam")

STEAM_APP_KEY_RE = re.compile(r"^steam_app_(\d+)$")
# `steam://rungameid/2620` in a Steam-generated shortcut's Exec= line.
STEAM_URL_RE = re.compile(r"steam://(?:rungameid|run)/(\d+)")
_APPID_ENV_VARS = ("SteamGameId", "SteamAppId")
_MAX_APPID = 2**32          # AppIDs are 32-bit; rejects absurd values from odd input
_PID_CACHE_MAX = 256


# ------------------------------------------------------------------- VDF
def parse_vdf(text: str) -> dict:
    """Parse Valve's KeyValues text format (libraryfolders.vdf, *.acf).

    Returns nested dicts with **lower-cased keys** (Steam's key casing is not
    consistent across files/versions). Tolerant by design: a malformed file
    yields whatever parsed cleanly rather than raising, since a corrupt
    manifest must never break tracking.
    """
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and text[i:i + 2] == "//":
            while i < n and text[i] != "\n":
                i += 1
        elif c in "{}":
            tokens.append(c)
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, nxt))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1  # closing quote
            tokens.append("s:" + "".join(buf))
        else:  # unquoted token (rare, but valid)
            j = i
            while j < n and text[j] not in " \t\r\n{}\"":
                j += 1
            tokens.append("s:" + text[i:j])
            i = j

    def parse_block(pos: int) -> tuple[dict, int]:
        out: dict = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                return out, pos + 1
            if not tok.startswith("s:"):
                pos += 1          # stray "{": skip
                continue
            key = tok[2:].lower()
            pos += 1
            if pos >= len(tokens):
                break
            nxt = tokens[pos]
            if nxt == "{":
                child, pos = parse_block(pos + 1)
                out[key] = child
            elif nxt.startswith("s:"):
                out[key] = nxt[2:]
                pos += 1
            else:                  # "}" right after a key: malformed, stop here
                break
        return out, pos

    try:
        result, _ = parse_block(0)
    except RecursionError:
        return {}
    return result


# ------------------------------------------------------------- discovery
def _registry_steam_paths() -> list[str]:
    """Steam's own record of where it is installed (HKCU/HKLM). Windows only."""
    out: list[str] = []
    try:
        import winreg
    except ImportError:
        return out
    for hive, sub, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as k:
                v, _ = winreg.QueryValueEx(k, value)
                if isinstance(v, str) and v:
                    out.append(v)
        except OSError:
            continue
    return out


def windows_roots(env: Optional[dict] = None, registry: Optional[Callable[[], list]] = None) -> list[Path]:
    """Common native Windows Steam install locations (registry first, then the
    default Program Files folders). `env`/`registry` are injectable for tests."""
    env = os.environ if env is None else env
    paths: list[str] = list((registry or _registry_steam_paths)())
    for var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = env.get(var)
        if base:
            paths.append(str(Path(base) / "Steam"))
    return [Path(p) for p in paths]


def _default_roots(home: Path) -> list[Path]:
    if _platform.is_windows():
        return windows_roots()
    return [
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".steam" / "root",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
        home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
    ]


def _steamapps_dir(library: Path) -> Optional[Path]:
    for name in ("steamapps", "SteamApps"):
        d = library / name
        if d.is_dir():
            return d
    return None


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    """~/.steam/steam and ~/.local/share/Steam are usually the same directory
    via symlinks; collapse by real path so nothing is scanned twice."""
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        try:
            real = os.path.realpath(p)
        except OSError:
            continue
        if real not in seen:
            seen.add(real)
            out.append(Path(real))
    return out


def library_paths_from_vdf(vdf: dict) -> list[Path]:
    """Handles both libraryfolders.vdf layouts: the current one
    ({"0": {"path": ...}}) and the legacy one ({"1": "/path"})."""
    root = vdf.get("libraryfolders", vdf)
    out: list[Path] = []
    for key, val in root.items():
        if not key.isdigit():
            continue                       # e.g. "contentstatsid"
        if isinstance(val, dict):
            p = val.get("path")
        else:
            p = val
        if isinstance(p, str) and p:
            out.append(Path(p))
    return out


def appid_from_key(key: str) -> Optional[int]:
    m = STEAM_APP_KEY_RE.match(key or "")
    return _valid_appid(m.group(1)) if m else None


def appid_from_exec(exec_line: str) -> Optional[int]:
    m = STEAM_URL_RE.search(exec_line or "")
    return _valid_appid(m.group(1)) if m else None


def _valid_appid(value) -> Optional[int]:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n < _MAX_APPID else None


class SteamResolver:
    def __init__(self, home: Optional[Path] = None, roots: Optional[list[Path]] = None,
                 clock: Callable[[], float] = time.monotonic, recheck_seconds: float = 30.0,
                 proc_root: str = "/proc"):
        self._home = home or Path.home()
        self._explicit_roots = roots
        self._clock = clock
        self._recheck = recheck_seconds
        self._proc_root = proc_root
        self._names: dict[int, Optional[str]] = {}
        self._libraries: Optional[list[Path]] = None
        self._sig: Optional[tuple] = None
        self._last_check = float("-inf")
        self._pid_cache: dict[tuple[int, int], Optional[int]] = {}
        self._installdirs: Optional[dict[str, int]] = None     # lower-case installdir -> AppID (Windows exe lookup)

    # ---- roots / libraries ------------------------------------------------
    def roots(self) -> list[Path]:
        cands = self._explicit_roots if self._explicit_roots is not None else _default_roots(self._home)
        return [r for r in _dedupe_paths(cands) if r.is_dir()]

    def libraries(self) -> list[Path]:
        """Every existing library (each root itself plus those listed in its
        libraryfolders.vdf), de-duplicated. A library on an unmounted drive is
        simply skipped."""
        if self._libraries is not None:
            return self._libraries
        found: list[Path] = []
        for root in self.roots():
            found.append(root)
            vdf_path = None
            sa = _steamapps_dir(root)
            if sa is not None and (sa / "libraryfolders.vdf").is_file():
                vdf_path = sa / "libraryfolders.vdf"
            elif (root / "config" / "libraryfolders.vdf").is_file():
                vdf_path = root / "config" / "libraryfolders.vdf"
            if vdf_path is not None:
                try:
                    found.extend(library_paths_from_vdf(parse_vdf(vdf_path.read_text(errors="replace"))))
                except OSError as e:
                    log.debug("could not read %s: %s", vdf_path, e)
        self._libraries = [p for p in _dedupe_paths(found) if _steamapps_dir(p) is not None]
        return self._libraries

    def _signature(self) -> tuple:
        parts = []
        for root in self.roots():
            for rel in ("steamapps/libraryfolders.vdf", "SteamApps/libraryfolders.vdf",
                        "config/libraryfolders.vdf"):
                parts.append(self._mtime(root / rel))
        for lib in self.libraries():
            sa = _steamapps_dir(lib)
            parts.append(self._mtime(sa) if sa else None)
        return tuple(parts)

    @staticmethod
    def _mtime(p: Path):
        try:
            return (str(p), p.stat().st_mtime_ns)
        except OSError:
            return (str(p), None)

    def _maybe_invalidate(self):
        now = self._clock()
        if now - self._last_check < self._recheck:
            return
        self._last_check = now
        # libraries() is cached, so build the signature from a fresh scan only
        # when we're actually re-checking (not on every lookup).
        stale_libs, self._libraries = self._libraries, None
        sig = self._signature()
        if self._sig is None:
            self._sig = sig
            return
        if sig != self._sig:
            log.debug("Steam library files changed; dropping name cache")
            self._sig = sig
            self._names.clear()
            self._pid_cache.clear()
            self._installdirs = None
        else:
            self._libraries = stale_libs if stale_libs is not None else self._libraries

    def invalidate(self):
        self._names.clear()
        self._pid_cache.clear()
        self._installdirs = None
        self._libraries = None
        self._sig = None
        self._last_check = float("-inf")

    # ---- lookup -----------------------------------------------------------
    def name_for_appid(self, appid) -> Optional[str]:
        """Installed game's name, or None if unknown (never raises)."""
        n = _valid_appid(appid)
        if n is None:
            return None
        self._maybe_invalidate()
        if n in self._names:
            return self._names[n]
        name = self._lookup(n)
        self._names[n] = name
        return name

    def _lookup(self, appid: int) -> Optional[str]:
        try:
            for lib in self.libraries():
                sa = _steamapps_dir(lib)
                if sa is None:
                    continue
                manifest = sa / f"appmanifest_{appid}.acf"
                if not manifest.is_file():
                    continue
                try:
                    state = parse_vdf(manifest.read_text(errors="replace")).get("appstate", {})
                except OSError:
                    continue
                if not isinstance(state, dict):
                    continue
                name = (state.get("name") or state.get("installdir") or "").strip()
                if name:
                    return name
        except Exception:
            log.debug("Steam lookup for %s failed", appid, exc_info=True)
        return None

    def installed_games(self) -> list[tuple[int, str]]:
        """Every (AppID, name) found in every library. Diagnostic use only."""
        out: dict[int, str] = {}
        for lib in self.libraries():
            sa = _steamapps_dir(lib)
            if sa is None:
                continue
            for mf in sorted(sa.glob("appmanifest_*.acf")):
                aid = _valid_appid(mf.stem.split("_", 1)[1])
                if aid and aid not in out:
                    name = self._lookup(aid)
                    if name:
                        out[aid] = name
        return sorted(out.items())

    def describe(self) -> str:
        return (f"roots={[str(r) for r in self.roots()]} "
                f"libraries={[str(l) for l in self.libraries()]}")

    def name_for_key(self, key: str) -> Optional[str]:
        appid = appid_from_key(key)
        return self.name_for_appid(appid) if appid else None

    # ---- Windows: games identified by where their executable lives ----------
    def _installdir_index(self) -> dict[str, int]:
        """installdir (the folder name under steamapps/common) -> AppID, from
        every appmanifest in every library. Dropped with the name cache when
        Steam's files change."""
        if self._installdirs is not None:
            return self._installdirs
        index: dict[str, int] = {}
        for lib in self.libraries():
            sa = _steamapps_dir(lib)
            if sa is None:
                continue
            for mf in sa.glob("appmanifest_*.acf"):
                aid = _valid_appid(mf.stem.split("_", 1)[1])
                if aid is None:
                    continue
                try:
                    state = parse_vdf(mf.read_text(errors="replace")).get("appstate", {})
                except OSError:
                    continue
                installdir = (state.get("installdir") or "").strip().lower() if isinstance(state, dict) else ""
                if installdir:
                    index.setdefault(installdir, aid)
        self._installdirs = index
        return index

    def appid_for_exe(self, exe_path: Optional[str]) -> Optional[int]:
        """AppID of an installed game whose executable lives under
        <library>/steamapps/common/<installdir>/ (offline, no process access
        needed, works for protected processes too)."""
        if not exe_path:
            return None
        self._maybe_invalidate()
        target = os.path.normcase(os.path.normpath(exe_path))
        try:
            for lib in self.libraries():
                sa = _steamapps_dir(lib)
                if sa is None:
                    continue
                common = os.path.normcase(os.path.normpath(str(sa / "common")))
                if target.startswith(common + os.sep):
                    first = target[len(common) + 1:].split(os.sep, 1)[0]
                    return self._installdir_index().get(first.lower())
        except Exception:
            log.debug("Steam exe lookup for %s failed", exe_path, exc_info=True)
        return None

    def key_for_exe(self, exe_path: Optional[str]) -> Optional[str]:
        """The canonical `steam_app_<AppID>` key for a game executable, so a
        Windows game groups under the same key a Proton game does on Linux."""
        appid = self.appid_for_exe(exe_path)
        return f"steam_app_{appid}" if appid else None

    # ---- process information ---------------------------------------------
    def appid_for_pid(self, pid: Optional[int]) -> Optional[int]:
        """AppID of a game Steam launched, read from that process's own
        environment (Steam sets SteamGameId/SteamAppId for every game,
        including native Linux ones whose window class says nothing about
        Steam). Only readable for our own user's processes, which is all we
        ever need. Cached per (pid, start time) so pid reuse can't mislead."""
        if not pid or pid <= 0:
            return None
        if _platform.is_windows():
            return self._appid_for_pid_psutil(pid)
        try:
            start = os.stat(f"{self._proc_root}/{pid}").st_ctime_ns
        except OSError:
            return None
        ck = (pid, start)
        if ck in self._pid_cache:
            return self._pid_cache[ck]
        appid = None
        try:
            with open(f"{self._proc_root}/{pid}/environ", "rb") as f:
                env = dict(
                    item.split(b"=", 1) for item in f.read().split(b"\0") if b"=" in item
                )
            for var in _APPID_ENV_VARS:
                v = env.get(var.encode())
                if v:
                    appid = _valid_appid(v.decode(errors="replace"))
                    if appid:
                        break
        except OSError:
            appid = None
        if len(self._pid_cache) >= _PID_CACHE_MAX:
            self._pid_cache.clear()
        self._pid_cache[ck] = appid
        return appid

    def _appid_for_pid_psutil(self, pid: int) -> Optional[int]:
        """Windows: Steam sets SteamAppId/SteamGameId in a game's environment.
        psutil can read it for the user's own processes; protected ones just
        raise AccessDenied, which is "unknown", not an error."""
        try:
            import psutil
            proc = psutil.Process(pid)
            ck = (pid, int(proc.create_time() * 1000))
            if ck in self._pid_cache:
                return self._pid_cache[ck]
            env = {k.lower(): v for k, v in proc.environ().items()}
        except Exception:                # noqa: BLE001 - NoSuchProcess/AccessDenied/OSError: all mean "unknown"
            return None
        appid = None
        for var in _APPID_ENV_VARS:
            appid = _valid_appid(env.get(var.lower()))
            if appid:
                break
        if len(self._pid_cache) >= _PID_CACHE_MAX:
            self._pid_cache.clear()
        self._pid_cache[ck] = appid
        return appid

    def icon_for_appid(self, appid: int) -> Optional[str]:
        """Steam installs `steam_icon_<AppID>` into the icon theme for games it
        makes shortcuts for; use it only if it really exists."""
        name = f"steam_icon_{appid}"
        base = self._home / ".local" / "share" / "icons" / "hicolor"
        try:
            if any(base.glob(f"*/apps/{name}.*")):
                return name
        except OSError:
            pass
        return None


_default: Optional[SteamResolver] = None


def default_resolver() -> SteamResolver:
    global _default
    if _default is None:
        _default = SteamResolver()
    return _default


def reset_default_resolver():
    global _default
    _default = None


def main(argv=None) -> int:
    """`python -m screentime.steam_library [AppID ...]` -- show what the offline
    resolver can see on this machine (no network involved)."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    r = SteamResolver()
    print("Steam roots:    ", [str(x) for x in r.roots()] or "none found")
    print("Steam libraries:", [str(x) for x in r.libraries()] or "none found")
    if args:
        for a in args:
            print(f"AppID {a}: {r.name_for_appid(a) or 'NOT FOUND in any local manifest'}")
    else:
        games = r.installed_games()
        print(f"{len(games)} installed game(s) with a readable manifest:")
        for aid, name in games:
            print(f"  {aid:>9}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
