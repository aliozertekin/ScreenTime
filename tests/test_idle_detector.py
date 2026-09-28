"""Unit tests for idle_detector.py's parsing logic against realistic canned
`loginctl`/`xprintidle` output."""
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime.idle_detector import LogindIdleDetector, XPrintIdleDetector


def _mock_run(stdout: str, returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_logind_idle_hint_yes_computes_seconds():
    d = LogindIdleDetector.__new__(LogindIdleDetector)
    d._session_id = "3"
    d._loginctl = "/usr/bin/loginctl"
    now_us = int(time.time() * 1_000_000)
    since_us = now_us - 120_000_000  # 120 seconds ago
    out = f"IdleHint=yes\nIdleSinceHint={since_us}\n"
    with patch("subprocess.run", return_value=_mock_run(out)):
        idle = d.get_idle_seconds()
    assert 119 <= idle <= 121


def test_logind_idle_hint_no_returns_zero():
    d = LogindIdleDetector.__new__(LogindIdleDetector)
    d._session_id = "3"
    d._loginctl = "/usr/bin/loginctl"
    out = "IdleHint=no\nIdleSinceHint=0\n"
    with patch("subprocess.run", return_value=_mock_run(out)):
        assert d.get_idle_seconds() == 0.0


def test_logind_malformed_output_does_not_raise():
    d = LogindIdleDetector.__new__(LogindIdleDetector)
    d._session_id = "3"
    d._loginctl = "/usr/bin/loginctl"
    with patch("subprocess.run", return_value=_mock_run("garbage")):
        assert d.get_idle_seconds() == 0.0


def test_logind_subprocess_failure_does_not_raise():
    d = LogindIdleDetector.__new__(LogindIdleDetector)
    d._session_id = "3"
    d._loginctl = "/usr/bin/loginctl"
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        assert d.get_idle_seconds() == 0.0


def test_xprintidle_converts_ms_to_seconds():
    d = XPrintIdleDetector()
    with patch("subprocess.run", return_value=_mock_run("45231\n")):
        assert abs(d.get_idle_seconds() - 45.231) < 0.001


def test_xprintidle_bad_output_returns_zero():
    d = XPrintIdleDetector()
    with patch("subprocess.run", return_value=_mock_run("not a number\n")):
        assert d.get_idle_seconds() == 0.0
