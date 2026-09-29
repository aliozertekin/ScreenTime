"""A package upgrade replaces files on disk but doesn't restart a running per-user
daemon, so fixes appeared not to work until the next login. These tests cover
detecting that (version handshake via the settings table), restarting safely,
and the pacman post-upgrade script."""
import os
import stat
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from screentime import autostart, instance_lock

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ version logic
@pytest.mark.parametrize("recorded,current,outdated", [
    (None, "1.1.0", True),          # daemon predates the version handshake
    ("", "1.1.0", True),
    ("1.0.4", "1.1.0", True),
    ("1.0.9", "1.0.10", True),      # numeric, not lexicographic, comparison
    ("1.1.0", "1.1.0", False),
    ("1.2.0", "1.1.0", False),      # newer daemon + old GUI window: leave it alone
    ("garbage", "1.1.0", False),    # unparseable: never restart on a guess
])
def test_daemon_is_outdated(recorded, current, outdated):
    assert autostart.daemon_is_outdated(recorded, current) is outdated


def test_daemon_records_its_version(tmp_path, monkeypatch):
    import screentime
    from screentime.daemon import Daemon
    from screentime.db import Database
    db = Database(tmp_path / "v.db")
    Daemon(db)
    assert db.get_setting("daemon_version") == screentime.__version__
    db.close()


# --------------------------------------------------------------- restart_now
@pytest.fixture
def sysd(monkeypatch):
    calls = []
    state = {"active": "active"}

    def run(cmd, **kw):
        calls.append(cmd[2:])
        r = MagicMock(returncode=0, stdout="")
        if cmd[2] == "is-active":
            r.stdout = state["active"] + "\n"
        return r
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(autostart, "_systemd_available", lambda: True)
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: True)
    return calls, state


def test_restart_now_restarts_running_unit(sysd):
    calls, _ = sysd
    assert autostart.restart_now() is True
    assert ["restart", autostart.UNIT_NAME] in calls


def test_restart_now_never_starts_a_daemon_that_is_not_running(sysd, monkeypatch):
    """A user who stopped tracking on purpose must not be overridden."""
    calls, state = sysd
    state["active"] = "inactive"                     # unit stopped...
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: False)   # ...no lock holder...
    monkeypatch.setattr(autostart, "_find_daemon_processes", lambda: [])     # ...and no stray process
    calls.clear()
    assert autostart.restart_now() is False
    assert not any(c[0] in ("restart", "start") for c in calls)


def test_restart_now_for_daemon_launched_outside_the_unit(monkeypatch):
    """XDG-autostart / Settings-button daemons aren't the unit: stop it, wait for
    the lock to clear, start a fresh one."""
    monkeypatch.setattr(autostart, "_systemd_available", lambda: False)
    held = iter([True, True, False])                   # running, running, lock released
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: next(held, False))
    order = []
    monkeypatch.setattr(autostart, "stop_now", lambda: order.append("stop") or True)
    monkeypatch.setattr(autostart, "start_now", lambda: order.append("start") or True)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert autostart.restart_now(wait_seconds=5) is True
    assert order == ["stop", "start"]


def test_refresh_if_outdated_only_when_running_and_older(sysd):
    calls, _ = sysd
    assert autostart.refresh_if_outdated("1.0.4", "1.1.0") is True
    calls.clear()
    assert autostart.refresh_if_outdated("1.1.0", "1.1.0") is False       # current: no restart
    assert autostart.refresh_if_outdated("1.5.0", "1.1.0") is False       # newer: no restart
    assert not any(c[0] == "restart" for c in calls)


def test_refresh_does_nothing_if_daemon_not_running(monkeypatch):
    monkeypatch.setattr(autostart, "daemon_is_running", lambda: False)
    monkeypatch.setattr(autostart, "restart_now", lambda: pytest.fail("must not restart"))
    assert autostart.refresh_if_outdated(None, "1.1.0") is False


def test_no_restart_loop_after_the_new_daemon_records_its_version(sysd):
    calls, _ = sysd
    assert autostart.refresh_if_outdated(None, "1.1.0") is True           # old daemon -> restart once
    assert autostart.refresh_if_outdated("1.1.0", "1.1.0") is False       # new daemon wrote its version


# ------------------------------------------------- pacman post_upgrade script
def _fake_bin(tmp_path, loginctl_out, systemctl_rc=0, with_loginctl=True):
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "systemctl.log"
    if with_loginctl:
        (b / "loginctl").write_text(f"#!/bin/sh\nprintf '%s\\n' '{loginctl_out}'\n")
    (b / "systemctl").write_text(f"#!/bin/sh\necho \"$@\" >> '{log}'\nexit {systemctl_rc}\n")
    (b / "timeout").write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    for f in b.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return b, log


def _run_post_upgrade(bindir, path_only=True):
    env = {"PATH": f"{bindir}:/usr/bin:/bin" if path_only else "/usr/bin:/bin"}
    return subprocess.run(["bash", "-c", f"source {ROOT / 'screentime.install'}; post_upgrade"],
                          capture_output=True, text=True, env=env)


def test_post_upgrade_try_restarts_for_each_logged_in_user(tmp_path):
    b, log = _fake_bin(tmp_path, "1000 alice no active\n1001 bob no active")
    r = _run_post_upgrade(b)
    assert r.returncode == 0
    lines = log.read_text().splitlines()
    assert "--user --machine=alice@.host try-restart screentime-daemon.service" in lines
    assert "--user --machine=bob@.host try-restart screentime-daemon.service" in lines
    assert not any(" start " in l or l.endswith(" restart screentime-daemon.service") for l in lines)  # try-restart only


def test_post_upgrade_never_fails_the_upgrade(tmp_path):
    b, _ = _fake_bin(tmp_path, "1000 alice no active", systemctl_rc=1)
    assert _run_post_upgrade(b).returncode == 0


def test_post_upgrade_without_loginctl_is_a_noop(tmp_path):
    b, log = _fake_bin(tmp_path, "", with_loginctl=False)
    r = _run_post_upgrade(b)
    assert r.returncode == 0 and not log.exists()


def test_pkgbuild_ships_the_install_script():
    text = (ROOT / "PKGBUILD").read_text()
    assert "install=screentime.install" in text
    assert (ROOT / "screentime.install").exists()
