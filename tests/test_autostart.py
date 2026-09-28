import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime import autostart


def _mock_run(returncode=0, stdout=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


def _mock_which_only(*names):
    def which(name):
        return f"/usr/bin/{name}" if name in names else None
    return which


# ------------------------------------------------------------- start_now
def test_start_now_uses_systemd_when_available(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", _mock_which_only("systemctl"))
    monkeypatch.setattr(Path, "exists", lambda self: True)  # fakes /run/systemd/system

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "--user"] and "start" in cmd:
            return _mock_run(returncode=0)
        return _mock_run(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        ok = autostart.start_now()

    assert ok is True
    assert any("start" in c and "screentime-daemon.service" in c for c in calls)


def test_start_now_falls_back_to_direct_spawn_without_systemd(monkeypatch):
    monkeypatch.setattr("shutil.which", _mock_which_only("screentime-daemon"))
    monkeypatch.setattr(Path, "exists", lambda self: False)  # no /run/systemd/system

    with patch("subprocess.Popen") as popen:
        ok = autostart.start_now()

    assert ok is True
    popen.assert_called_once()
    args = popen.call_args[0][0]
    assert args == ["/usr/bin/screentime-daemon"]
    assert popen.call_args.kwargs.get("start_new_session") is True


def test_start_now_fails_cleanly_with_no_systemd_and_no_binary(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert autostart.start_now() is False


def test_start_now_falls_back_when_systemctl_start_fails(monkeypatch):
    monkeypatch.setattr("shutil.which", _mock_which_only("systemctl", "screentime-daemon"))
    monkeypatch.setattr(Path, "exists", lambda self: True)

    import subprocess as sp

    def fake_run(cmd, **kwargs):
        if "start" in cmd:
            raise sp.CalledProcessError(1, cmd)
        return _mock_run(returncode=0)

    with patch("subprocess.run", side_effect=fake_run), patch("subprocess.Popen") as popen:
        ok = autostart.start_now()

    assert ok is True
    popen.assert_called_once()


# -------------------------------------------------------------- stop_now
def test_stop_now_uses_systemd_when_available(monkeypatch):
    monkeypatch.setattr("shutil.which", _mock_which_only("systemctl"))
    monkeypatch.setattr(Path, "exists", lambda self: True)

    with patch("subprocess.run", return_value=_mock_run(returncode=0)) as run:
        ok = autostart.stop_now()

    assert ok is True
    run.assert_called_once()
    assert "stop" in run.call_args[0][0]


def test_stop_now_falls_back_to_psutil_terminate(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)

    fake_proc = MagicMock()
    fake_proc.info = {"pid": 1234, "name": "screentime-daemon", "cmdline": ["screentime-daemon"]}

    with patch("psutil.process_iter", return_value=[fake_proc]):
        ok = autostart.stop_now()

    assert ok is True
    fake_proc.terminate.assert_called_once()


def test_stop_now_no_process_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)

    with patch("psutil.process_iter", return_value=[]):
        ok = autostart.stop_now()

    assert ok is False


# --------------------------------------------------------- daemon_is_running
def test_daemon_is_running_reflects_systemd_state(monkeypatch):
    monkeypatch.setattr("shutil.which", _mock_which_only("systemctl"))
    monkeypatch.setattr(Path, "exists", lambda self: True)

    with patch("subprocess.run", return_value=_mock_run(stdout="active\n")):
        assert autostart.daemon_is_running() is True

    with patch("subprocess.run", return_value=_mock_run(stdout="inactive\n")):
        assert autostart.daemon_is_running() is False
