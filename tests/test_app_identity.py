import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime import app_identity


FIREFOX_DESKTOP = """[Desktop Entry]
Type=Application
Name=Firefox Web Browser
Icon=firefox
Exec=firefox %u
StartupWMClass=firefox
"""

CODE_DESKTOP = """[Desktop Entry]
Type=Application
Name=Visual Studio Code
Icon=vscode
Exec=/usr/share/code/code %F
StartupWMClass=Code
"""

NAUTILUS_DESKTOP = """[Desktop Entry]
Type=Application
Name=Files
Icon=org.gnome.Nautilus
Exec=nautilus %U
"""

HIDDEN_DESKTOP = """[Desktop Entry]
Type=Application
Name=Should Not Appear
Icon=hidden
Exec=hidden-app
StartupWMClass=hiddenapp
NoDisplay=true
"""


@pytest.fixture
def fake_applications_dir(tmp_path, monkeypatch):
    app_dir = tmp_path / "applications"
    app_dir.mkdir()
    (app_dir / "firefox.desktop").write_text(FIREFOX_DESKTOP)
    (app_dir / "code.desktop").write_text(CODE_DESKTOP)
    (app_dir / "org.gnome.Nautilus.desktop").write_text(NAUTILUS_DESKTOP)
    (app_dir / "hidden.desktop").write_text(HIDDEN_DESKTOP)

    import functools

    @functools.lru_cache(maxsize=1)
    def fake_index():
        return app_identity._build_index_from_dirs([str(app_dir)])

    monkeypatch.setattr(app_identity, "_desktop_file_index", fake_index)
    yield app_dir


def test_resolve_matches_by_startup_wm_class(fake_applications_dir):
    r = app_identity.resolve("firefox")
    assert r.key == "firefox"
    assert r.display_name == "Firefox Web Browser"
    assert r.icon_name == "firefox"
    assert r.desktop_file is not None


def test_resolve_matches_case_insensitively(fake_applications_dir):
    r = app_identity.resolve("Firefox")
    assert r.key == "firefox"
    assert r.display_name == "Firefox Web Browser"


def test_resolve_matches_startup_wm_class_with_different_case(fake_applications_dir):
    # Code.desktop declares StartupWMClass=Code (capitalized), as VS Code
    # actually does; the raw window class reported by X11/Wayland should
    # still resolve to it regardless of case.
    r = app_identity.resolve("Code")
    assert r.key == "code"
    assert r.display_name == "Visual Studio Code"


def test_resolve_falls_back_to_exec_basename_when_no_wm_class(fake_applications_dir):
    r = app_identity.resolve("nautilus")
    assert r.display_name == "Files"


def test_resolve_unknown_app_falls_back_to_capitalized_name(fake_applications_dir):
    r = app_identity.resolve("some-random-tool")
    assert r.key == "some-random-tool"
    assert r.display_name == "Some Random Tool"
    assert r.desktop_file is None


def test_resolve_strips_reverse_dns_prefix_for_fallback_display(fake_applications_dir):
    r = app_identity.resolve("org.example.UnknownApp")
    # No .desktop match for this one, so we fall back to a cleaned-up name
    # with the reverse-DNS prefix stripped for readability.
    assert "org.example" not in r.display_name
    assert r.desktop_file is None


def test_multiple_raw_identifiers_same_app_get_same_key(fake_applications_dir):
    # Two different windows of the same app reporting the same wm_class in
    # different case must still collapse to one canonical key.
    a = app_identity.resolve("firefox")
    b = app_identity.resolve("Firefox")
    assert a.key == b.key
