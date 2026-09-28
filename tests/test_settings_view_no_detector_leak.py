"""
Regression test for a real, serious bug: SettingsView used to call
create_window_detector()/create_idle_detector() just to display a status
string. On KDE, create_window_detector() can return a KWinPushDetector,
whose constructor claims exclusive ownership of a D-Bus well-known name
(org.screentime.KWinFocus) for as long as the instance is alive.
SettingsView is built once and lives for the entire GUI session, so this
silently and permanently starved the real daemon's detector of the events
KWin sends -- tracking would show 0s forever, with no error anywhere,
regardless of whether the KWin script itself was correctly installed.

The fix: the daemon records which backend it selected into the settings
table, and the GUI only ever reads that cached value. This test enforces
that SettingsView construction never touches the live-instantiation
functions, so this class of bug can't silently come back.
"""
import ast
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("gi")
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    # GTK4 widget construction without any display connection can segfault
    # rather than raise a clean Python exception -- skip before even
    # importing gi.repository.Gtk in that case (e.g. headless CI without
    # Xvfb), rather than crashing the whole test run.
    pytest.skip("No DISPLAY/WAYLAND_DISPLAY -- GTK widget tests need a display "
                "(run under xvfb-run on headless systems)", allow_module_level=True)

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from screentime.db import Database  # noqa: E402
from screentime.config import Config  # noqa: E402


def test_settings_view_source_does_not_import_live_detector_constructors():
    """Static check: a fast, always-available guard that doesn't need a
    display. If someone re-adds `from ...window_detector import
    create_window_detector` to settings.py, this fails immediately."""
    src = Path(__file__).resolve().parent.parent / "screentime/gui/views/settings.py"
    tree = ast.parse(src.read_text())
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)
    assert "create_window_detector" not in imported_names
    assert "create_idle_detector" not in imported_names


def test_settings_view_construction_never_calls_live_detectors(tmp_path):
    """Dynamic check: actually build the widget and assert the
    side-effecting constructors are never invoked, even indirectly."""
    from screentime.gui.views.settings import SettingsView

    db = Database(tmp_path / "s.db")
    config = Config(db)

    with patch("screentime.window_detector.create_window_detector") as wd_mock, \
         patch("screentime.idle_detector.create_idle_detector") as id_mock:
        view = SettingsView(db, config)
        wd_mock.assert_not_called()
        id_mock.assert_not_called()

    db.close()


def test_settings_view_reads_backend_names_from_settings_table(tmp_path):
    """The GUI should reflect what the daemon actually reported, not guess
    on its own."""
    from screentime.gui.views.settings import SettingsView

    db = Database(tmp_path / "s2.db")
    config = Config(db)
    db.set_setting("active_window_backend", "kwin-push")
    db.set_setting("active_idle_backend", "logind")

    view = SettingsView(db, config)
    assert view._window_backend_row.get_subtitle() == "kwin-push"
    assert view._idle_backend_row.get_subtitle() == "logind"
    db.close()


def test_settings_view_shows_unknown_when_daemon_never_ran(tmp_path):
    from screentime.gui.views.settings import SettingsView

    db = Database(tmp_path / "s3.db")
    config = Config(db)

    view = SettingsView(db, config)
    assert "Unknown" in view._window_backend_row.get_subtitle()
    assert "Unknown" in view._idle_backend_row.get_subtitle()
    db.close()
