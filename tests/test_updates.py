"""Opt-in update checking: explicit, minimal, offline-safe, and the only network code in the package."""
import ast
import json
import socket
import urllib.error
from pathlib import Path

import pytest

from screentime import updates as U
from screentime.db import Database

PKG = Path(U.__file__).resolve().parent
ANSWER = lambda tag, url=None: json.dumps({"tag_name": tag, "html_url": url or f"https://github.com/{U.REPO}/releases/tag/{tag}"}).encode()


def test_newer_release_is_reported_with_its_page():
    r = U.compare("1.5.0", ANSWER("v1.6.0"))
    assert (r.status, r.latest, r.current) == (U.AVAILABLE, "1.6.0", "1.5.0") and r.url.endswith("/tag/v1.6.0")


@pytest.mark.parametrize("cur,tag,status", [("1.5.0", "v1.5.0", U.UP_TO_DATE), ("1.5.0", "v1.4.9", U.UP_TO_DATE),
                                             ("1.9.0", "v1.10.0", U.AVAILABLE), ("1.5.0", "1.5.1", U.AVAILABLE)])
def test_version_comparison_is_numeric(cur, tag, status):
    assert U.compare(cur, ANSWER(tag)).status == status


@pytest.mark.parametrize("payload", [b"", b"not json", b"[]", b"{}", ANSWER("nightly"), ANSWER("v1.5"), ANSWER("v1.5.0-rc1")])
def test_unexpected_answers_fail_safely(payload):
    assert U.compare("1.5.0", payload).status == U.FAILED


def test_an_unexpected_link_is_never_offered():
    r = U.compare("1.0.0", ANSWER("v2.0.0", "https://evil.example/download"))
    assert r.status == U.AVAILABLE and r.url == U.RELEASES_URL
    assert U.compare("1.0.0", ANSWER("v2.0.0", "http://github.com/aliozertekin/ScreenTime/x")).url == U.RELEASES_URL


def test_offline_and_http_errors_are_reported_not_raised():
    def offline(url): raise urllib.error.URLError("no route")
    r = U.check(fetch=offline)
    assert r.status == U.FAILED and "offline" in r.message.lower()
    def timeout(url): raise TimeoutError()
    assert U.check(fetch=timeout).status == U.FAILED
    def limited(url): raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)
    assert "rate" in U.check(fetch=limited).message
    def missing(url): raise urllib.error.HTTPError(url, 404, "nf", {}, None)
    assert "No release" in U.check(fetch=missing).message


def test_real_http_get_fails_cleanly_with_the_network_blocked(monkeypatch):
    def forbid(*a, **k): raise OSError("network unreachable")
    monkeypatch.setattr(socket, "create_connection", forbid)
    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    assert U.check().status == U.FAILED


def test_http_get_refuses_non_https():
    for url in ("http://api.github.com/x", "file:///etc/passwd", "ftp://x/y"):
        with pytest.raises(ValueError):
            U.http_get(url)


def test_check_asks_github_for_this_project_only_and_sends_nothing_else():
    seen = []
    U.check(fetch=lambda url: seen.append(url) or ANSWER("v9.9.9"))
    assert seen == [f"https://api.github.com/repos/{U.REPO}/releases/latest"]


def test_request_headers_carry_only_the_version(monkeypatch):
    captured = {}
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return ANSWER("v9.9.9")
    class Op:
        def open(self, req, timeout): captured["req"], captured["timeout"] = req, timeout; return Resp()
    monkeypatch.setattr(U, "_opener", lambda: Op())
    assert U.http_get(U.API_URL).startswith(b"{")
    req = captured["req"]
    assert req.get_method() == "GET" and req.data is None and captured["timeout"] == U.TIMEOUT_SECONDS
    assert {k.lower() for k in req.headers} == {"accept", "user-agent"}
    assert req.get_header("User-agent").startswith("ScreenTime/")


def test_can_be_switched_off_and_then_does_not_even_try(tmp_path):
    db = Database(tmp_path / "t.db")
    assert U.enabled(db)                                      # default: the button works; nothing runs by itself
    db.set_setting(U.SETTING_ENABLED, "false")
    calls = []
    r = U.check(db, fetch=lambda url: calls.append(url) or ANSWER("v9.9.9"))
    assert r.status == U.DISABLED and calls == []
    db.close()


def test_config_has_an_update_switch_and_no_automatic_check_setting():
    from screentime.config import DEFAULTS
    assert DEFAULTS["update_checks_enabled"] == "true"
    assert not [k for k in DEFAULTS if "interval" in k and "update" in k]


# ------------------------------------------------------------ privacy architecture
def _imports(path: Path) -> set:
    tree = ast.parse(path.read_text())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            base = ("." * n.level) + (n.module or "")
            out.add(base)
            out |= {f"{base}.{a.name}" for a in n.names}
    return out


NETWORK = {"socket", "urllib", "urllib3", "http", "requests", "httpx", "aiohttp", "ssl", "ftplib", "smtplib",
           "telnetlib", "xmlrpc", "websockets"}


def test_updates_module_is_the_only_one_that_imports_a_network_library():
    offenders = [f"{p.relative_to(PKG)}: {m}" for p in PKG.rglob("*.py") if p.name != "updates.py"
                 for m in _imports(p) if m.split(".")[0] in NETWORK]
    assert offenders == []
