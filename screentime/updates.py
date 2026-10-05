"""Opt-in update check -- the ONLY place in ScreenTime that can touch the network.

What it does, exactly: when the user presses "Check for updates" (Settings -> Updates), it makes one HTTPS
GET request to GitHub's public Releases API for this project, reads the newest release's version number
and page URL, and compares it with the installed version. Nothing is downloaded or installed; the user is
shown the release page and decides.

What it never does:
* run on its own -- there is no background polling, no check at start-up, no timer;
* send usage data, settings, identifiers or cookies. The request carries only the standard headers plus a
  User-Agent naming the ScreenTime version. (GitHub necessarily sees your IP address, like any web request.)
* get imported by the tracking daemon or the storage layer -- tracking works with the network unplugged;
* run when the user has switched update checks off (`update_checks_enabled`): `check()` then refuses.

Tests enforce that no other module in the package imports a network library.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from . import __version__

REPO = "aliozertekin/ScreenTime"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
SETTING_ENABLED = "update_checks_enabled"
MAX_BYTES = 1 << 20
TIMEOUT_SECONDS = 10.0
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

UP_TO_DATE, AVAILABLE, FAILED, DISABLED = "up-to-date", "available", "failed", "disabled"


@dataclass
class UpdateResult:
    status: str                 # up-to-date | available | failed | disabled
    current: str
    latest: Optional[str] = None
    url: str = RELEASES_URL     # always a github.com page for this project
    message: str = ""


def parse_version(text: str) -> Optional[tuple]:
    m = _TAG.match(str(text).strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _opener() -> urllib.request.OpenerDirector:
    """HTTPS only (no http://, ftp:// or file://), no cookies, system proxy settings honoured."""
    op = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(), urllib.request.HTTPSHandler(),
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPRedirectHandler(),
              urllib.request.HTTPErrorProcessor()):
        op.add_handler(h)
    return op


def http_get(url: str, timeout: float = TIMEOUT_SECONDS) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"ScreenTime/{__version__} (update check)"})
    with _opener().open(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("response too large")
    return data


def compare(current: str, payload: bytes) -> UpdateResult:
    """Interpret a Releases API response. Pure function: no I/O."""
    try:
        doc = json.loads(payload)
        tag = doc["tag_name"]
    except (ValueError, KeyError, TypeError):
        return UpdateResult(FAILED, current, message="GitHub's answer was not understood.")
    latest, cur = parse_version(tag), parse_version(current)
    if latest is None or cur is None:
        return UpdateResult(FAILED, current, message="The latest release has an unexpected version number.")
    url = doc.get("html_url") if isinstance(doc.get("html_url"), str) else ""
    if not url.startswith(f"https://github.com/{REPO}/"):
        url = RELEASES_URL                                   # never follow a link we did not expect
    latest_text = ".".join(map(str, latest))
    if latest > cur:
        return UpdateResult(AVAILABLE, current, latest_text, url, f"ScreenTime {latest_text} is available.")
    return UpdateResult(UP_TO_DATE, current, latest_text, url, "You have the latest version.")


def enabled(db) -> bool:
    return (db.get_setting(SETTING_ENABLED, "true") or "true").lower() in ("1", "true", "yes")


def check(db=None, current: str = __version__, fetch: Optional[Callable[[str], bytes]] = None,
          allowed: Optional[bool] = None) -> UpdateResult:
    """One explicit update check. `db` (or `allowed`) carries the user's on/off choice."""
    if allowed is None:
        allowed = True if db is None else enabled(db)
    if not allowed:
        return UpdateResult(DISABLED, current, message="Update checks are turned off.")
    try:
        payload = (fetch or http_get)(API_URL)
    except urllib.error.HTTPError as e:
        msg = "GitHub is rate-limiting requests; try again later." if e.code in (403, 429) else \
              "No release information was found." if e.code == 404 else f"GitHub answered with an error ({e.code})."
        return UpdateResult(FAILED, current, message=msg)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return UpdateResult(FAILED, current, message="Could not reach GitHub. You appear to be offline.")
    return compare(current, payload)
