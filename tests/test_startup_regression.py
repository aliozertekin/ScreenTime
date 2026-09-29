"""Regression tests for "tracking stops after reboot/login".

Each test names the specific defect it guards against. `FakeSystemd` stands in
for `systemctl --user` so the enable/disable/start decision logic is exercised
end to end without a real user manager.
"""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from screentime import autostart, instance_lock


class FakeSystemd:
    def __init__(self, graphical_active=True, start_fails=False, enable_fails=False):
        self.graphical_active = graphical_active
        self.start_fails = start_fails
        self.enable_fails = enable_fails
        self.enabled = False
        self.active = False
        self.calls: list[list[str]] = []

    def run(self, cmd, **kwargs):
        assert cmd[:2] == ["systemctl", "--user"], cmd
        args = cmd[2:]
        self.calls.append(args)
        r = MagicMock(returncode=0, stdout="")

        def fail():
            r.returncode = 1
            r.check_returncode.side_effect = subprocess.CalledProcessError(1, cmd)

        verb = args[0]
        if verb == "is-active":
            if args[1] == "graphical-session.target":
                r.stdout = "active\n" if self.graphical_active else "inactive\n"
            else:
                r.stdout = "active\n" if self.active else "inactive\n"
        elif verb == "is-enabled":
            r.stdout = "enabled\n" if self.enabled else "disabled\n"
        elif verb == "enable":
            if self.enable_fails:
                fail()
            else:
                self.enabled = True
        elif verb == "disable":
            self.enabled = False
            if "--now" in args:
                self.active = False
        elif verb == "start":
            if self.start_fails:
                fail()
            else:
                self.active = True
        elif verb == "stop":
            self.active = False
        return r

    def did(self, *args):
        return list(args) in self.calls


@pytest.fixture
def env(monkeypatch, tmp_path):
    """systemd present, package binary absent (a `pip --user` install), no
    daemon running, screentime-daemon findable on PATH."""
    sd = FakeSystemd()
    monkeypatch.setattr(autostart, "_systemd_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", sd.run)
    monkeypatch.setattr(autostart, "PACKAGED_EXE", str(tmp_path / "no-such-usr-bin-exe"))
    monkeypatch.setattr(autostart, "SYSTEM_UNIT_DIRS", (str(tmp_path / "no-system-units"),))
    fake_exe = tmp_path / "home-bin" / "screentime-daemon"
    fake_exe.parent.mkdir()
    fake_exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr("shutil.which", lambda n: str(fake_exe) if n == "screentime-daemon" else (
        "/usr/bin/systemctl" if n == "systemctl" else None))
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: False)
    popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    sd.popen = popen
    sd.exe = fake_exe
    return sd


# ---- Defect: unit hard-coded /usr/bin, pip --user installs put it elsewhere ----
def test_user_unit_uses_real_absolute_exec_path_when_not_packaged(env):
    autostart.enable()
    unit = autostart.user_unit_file().read_text()
    assert f"ExecStart={env.exe}" in unit
    assert "/usr/bin/screentime-daemon" not in unit


def test_no_user_unit_written_when_packaged_binary_exists(env, monkeypatch, tmp_path):
    packaged = tmp_path / "usr-bin-screentime-daemon"
    packaged.write_text("x")
    monkeypatch.setattr(autostart, "PACKAGED_EXE", str(packaged))
    assert autostart.ensure_user_unit() is None
    assert not autostart.user_unit_file().exists()


def test_exec_path_with_spaces_is_quoted(env, monkeypatch, tmp_path):
    spaced = tmp_path / "my bin" / "screentime-daemon"
    spaced.parent.mkdir()
    spaced.write_text("x")
    monkeypatch.setattr("shutil.which", lambda n: str(spaced) if n == "screentime-daemon" else None)
    unit_cmd = autostart.daemon_command()
    assert unit_cmd == [str(spaced)]
    assert autostart._quote_exec_arg(str(spaced)).startswith('"')


def test_bundled_unit_file_matches_template():
    shipped = (Path(__file__).resolve().parent.parent / "data" / "screentime-daemon.service").read_text()
    assert shipped == autostart.render_unit("/usr/bin/screentime-daemon")


def test_unit_never_gives_up_restarting_and_restarts_on_crash():
    unit = autostart.render_unit("/x")
    assert "StartLimitIntervalSec=0" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=graphical-session.target" in unit


# ---- Defect: enabling a unit the session never starts -------------------------
def test_enable_uses_systemd_when_session_reaches_graphical_target(env):
    assert autostart.enable() == "systemd"
    assert env.enabled is True
    assert not autostart.desktop_file().exists()
    assert not env.did("enable", "--now", autostart.UNIT_NAME)   # start is separate & idempotent


def test_enable_falls_back_to_xdg_when_session_does_not_reach_target(env):
    env.graphical_active = False
    assert autostart.enable() == "xdg-autostart"
    assert env.enabled is False                       # not left enabled-but-inert
    text = autostart.desktop_file().read_text()
    assert f"Exec={env.exe}" in text                  # absolute path, not bare name
    assert "NoDisplay=true" in text


def test_enable_falls_back_to_xdg_when_systemctl_enable_fails(env):
    env.enable_fails = True
    assert autostart.enable() == "xdg-autostart"
    assert autostart.desktop_file().exists()


def test_enable_without_systemd_uses_xdg(env, monkeypatch):
    monkeypatch.setattr(autostart, "_systemd_available", lambda: False)
    assert autostart.enable() == "xdg-autostart"
    assert autostart.desktop_file().exists()


# ---- Defect: possible double launcher (systemd + XDG) --------------------------
def test_switching_mechanism_never_leaves_two_launchers(env):
    env.graphical_active = False
    autostart.enable()
    assert autostart.desktop_file().exists()
    env.graphical_active = True                        # e.g. user moved to a systemd-managed session
    assert autostart.enable() == "systemd"
    assert not autostart.desktop_file().exists()
    assert env.enabled


def test_enable_is_idempotent(env):
    autostart.enable()
    first_unit = autostart.user_unit_file().read_text()
    autostart.enable()
    autostart.enable()
    assert autostart.user_unit_file().read_text() == first_unit
    assert env.enabled and not autostart.desktop_file().exists()


# ---- Defect: no single-instance guard --------------------------------------------
def test_start_now_does_nothing_when_daemon_already_running(env, monkeypatch):
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: True)
    assert autostart.start_now() is True
    assert not env.did("start", autostart.UNIT_NAME)
    env.popen.assert_not_called()


def test_enable_does_not_start_second_daemon_when_running(env, monkeypatch):
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: True)
    autostart.enable()
    assert not env.did("start", autostart.UNIT_NAME)
    env.popen.assert_not_called()


def test_enable_starts_daemon_now_when_not_running(env):
    autostart.enable()
    assert env.did("start", autostart.UNIT_NAME) and env.active


def test_running_detected_via_lock_even_if_systemd_says_inactive(env, monkeypatch):
    """A daemon launched by the XDG entry / Settings button isn't the unit."""
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: True)
    env.active = False
    assert autostart.daemon_is_running() is True


# ---- disable ------------------------------------------------------------------------
def test_disable_removes_both_mechanisms(env):
    autostart.enable()
    autostart._write_xdg_entry()
    autostart.disable()
    assert env.enabled is False
    assert not autostart.desktop_file().exists()
    assert env.did("disable", "--now", autostart.UNIT_NAME)


def test_disable_stops_directly_launched_daemon(env, monkeypatch):
    monkeypatch.setattr(autostart, "_systemd_available", lambda: False)
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: True)
    proc = MagicMock()
    proc.info = {"pid": 5, "name": "screentime-daemon", "cmdline": ["/x/screentime-daemon"]}
    monkeypatch.setattr(autostart, "_find_daemon_processes", lambda: [proc])
    autostart.disable()
    proc.terminate.assert_called_once()


# ---- reconcile (GUI startup self-heal) --------------------------------------------
def test_reconcile_noop_when_user_did_not_opt_in(env):
    assert autostart.reconcile(False) is None
    assert env.calls == []


def test_reconcile_reenables_when_mechanism_vanished(env):
    assert autostart.reconcile(True) == "systemd"
    assert env.enabled


def test_reconcile_starts_daemon_if_enabled_but_not_running(env):
    env.enabled = True
    assert autostart.reconcile(True) == "already-enabled"
    assert env.active


def test_reconcile_never_starts_duplicate(env, monkeypatch):
    env.enabled = True
    monkeypatch.setattr(instance_lock, "is_held", lambda path=None: True)
    autostart.reconcile(True)
    assert not env.did("start", autostart.UNIT_NAME)


# ---- process matching -----------------------------------------------------------------
@pytest.mark.parametrize("argv,expected", [
    (["/usr/bin/screentime-daemon"], True),
    (["screentime-daemon", "-v"], True),
    (["/usr/bin/python3", "/home/u/.local/bin/screentime-daemon"], True),
    (["/usr/bin/python3", "-m", "screentime.daemon"], True),
    (["vim", "screentime-daemon.service"], False),
    (["tail", "-f", "/tmp/screentime-daemon.log"], False),
    (["/bin/sh", "-c", "screentime-daemon"], False),
    ([], False),
])
def test_daemon_cmdline_matching(argv, expected):
    assert autostart._is_daemon_cmdline(argv) is expected


# ---- diagnostics ------------------------------------------------------------------------
def test_status_reports_all_fields(env, tmp_path):
    from screentime.db import Database
    db = Database(tmp_path / "s.db")
    db.set_setting("daemon_last_start", "1700000000")
    db.set_setting("active_window_backend", "kwin-push")
    db.set_setting("active_idle_backend", "logind")
    autostart.enable()
    st = autostart.get_status(db)
    assert st.installed and st.enabled and st.mechanism == "systemd"
    assert st.last_start == 1700000000
    assert (st.window_backend, st.idle_backend) == ("kwin-push", "logind")
    db.close()


def test_status_when_nothing_configured(env, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/systemctl" if n == "systemctl" else None)
    st = autostart.get_status(None)
    assert not st.installed and not st.enabled and st.mechanism is None and not st.running
    assert st.last_start is None


def test_packaged_xdg_entry_is_hidden_per_spec():
    """The shipped /etc/xdg/autostart entry must be disabled by the
    spec-defined Hidden=true; X-GNOME-Autostart-enabled is ignored by KDE."""
    text = (Path(__file__).resolve().parent.parent / "data" / "screentime-daemon.desktop").read_text()
    assert "\nHidden=true\n" in text


def test_user_xdg_entry_is_not_hidden(env):
    env.graphical_active = False
    autostart.enable()
    assert "Hidden" not in autostart.desktop_file().read_text()


def test_legacy_daemon_without_lock_is_still_detected(env, monkeypatch):
    """A pre-lock daemon (running across an upgrade) holds no flock; the process
    scan is what stops us from starting a second one on top of it."""
    proc = MagicMock()
    monkeypatch.setattr(autostart, "_find_daemon_processes", lambda: [proc])
    assert autostart.daemon_is_running() is True
    assert autostart.start_now() is True
    env.popen.assert_not_called()
    assert not env.did("start", autostart.UNIT_NAME)
