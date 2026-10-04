"""Offline Steam resolution for native Windows installs, from fixture copies of
Steam's own files (no live Steam, no network)."""
import os
import shutil
from pathlib import Path

import pytest

from screentime import app_identity, steam_library
from screentime.steam_library import SteamResolver, parse_vdf, windows_roots

FIX = Path(__file__).parent / "fixtures" / "steam_windows"


@pytest.fixture
def steam(tmp_path):
    root = tmp_path / "Steam"
    shutil.copytree(FIX / "steamapps", root / "steamapps")
    # Point the fixture's libraryfolders.vdf at real temp dirs (the checked-in
    # copy keeps genuine Windows-style escaped paths for the parsing test).
    lib2 = tmp_path / "SteamLibrary"
    (lib2 / "steamapps" / "common" / "Portal 2").mkdir(parents=True)
    (root / "steamapps" / "common" / "ELDEN RING" / "Game").mkdir(parents=True)
    vdf = (root / "steamapps" / "libraryfolders.vdf")
    vdf.write_text(f'"libraryfolders"\n{{\n"0"\n{{\n"path" "{root.as_posix()}"\n}}\n"1"\n{{\n"path" "{lib2.as_posix()}"\n}}\n}}\n')
    shutil.move(str(root / "steamapps" / "appmanifest_620.acf"), str(lib2 / "steamapps" / "appmanifest_620.acf"))

    class S: pass
    s = S()
    s.root, s.lib2 = root, lib2
    s.resolver = SteamResolver(home=tmp_path, roots=[root])
    return s


def test_windows_style_escaped_library_paths_parse():
    d = parse_vdf((FIX / "steamapps" / "libraryfolders.vdf").read_text())
    paths = [v["path"] for v in d["libraryfolders"].values()]
    assert paths == ["C:\\Program Files (x86)\\Steam", "D:\\SteamLibrary"]


def test_acf_fixture_parses():
    d = parse_vdf((FIX / "steamapps" / "appmanifest_620.acf").read_text())
    assert d["appstate"]["name"] == "Portal 2" and d["appstate"]["installdir"] == "Portal 2"


def test_windows_roots_come_from_registry_then_program_files():
    env = {"ProgramFiles(x86)": r"C:\Program Files (x86)", "ProgramFiles": r"C:\Program Files"}
    roots = windows_roots(env=env, registry=lambda: [r"E:\Games\Steam"])
    assert roots[0] == Path(r"E:\Games\Steam")
    assert Path(r"C:\Program Files (x86)") / "Steam" in roots and Path(r"C:\Program Files") / "Steam" in roots


def test_windows_roots_tolerate_missing_registry_and_env():
    assert windows_roots(env={}, registry=lambda: []) == []


def _resolver(steam):
    return steam.resolver


def test_exe_inside_a_library_resolves_to_the_steam_app_key(steam, monkeypatch):
    r = _resolver(steam)
    exe = steam.lib2 / "steamapps" / "common" / "Portal 2" / "portal2.exe"
    assert r.appid_for_exe(str(exe)) == 620
    assert r.key_for_exe(str(exe)) == "steam_app_620"
    nested = steam.root / "steamapps" / "common" / "ELDEN RING" / "Game" / "eldenring.exe"
    assert r.key_for_exe(str(nested)) == "steam_app_1245620"


def test_exe_outside_steam_or_unknown_game_is_not_a_steam_game(steam):
    r = _resolver(steam)
    assert r.key_for_exe(r"C:\Program Files\Mozilla Firefox\firefox.exe") is None
    assert r.key_for_exe(str(steam.root / "steamapps" / "common" / "Unknown Game" / "x.exe")) is None
    assert r.key_for_exe(None) is None and r.key_for_exe("") is None


def test_installdir_index_is_refreshed_when_steam_files_change(steam):
    r = _resolver(steam)
    exe = steam.lib2 / "steamapps" / "common" / "Portal 2" / "portal2.exe"
    assert r.key_for_exe(str(exe)) == "steam_app_620"
    (steam.lib2 / "steamapps" / "appmanifest_620.acf").unlink()          # uninstalled
    r.invalidate()
    assert r.key_for_exe(str(exe)) is None


def test_steam_key_then_resolves_to_the_installed_game_name_offline(steam, monkeypatch):
    r = _resolver(steam)
    monkeypatch.setattr(steam_library, "default_resolver", lambda: r)
    got = app_identity.resolve("steam_app_620")
    assert got.key == "steam_app_620" and got.display_name == "Portal 2"


def test_windows_pid_lookup_reads_steam_env_via_psutil(steam, monkeypatch):
    import types, sys
    monkeypatch.setenv("SCREENTIME_PLATFORM", "windows")
    from screentime import platform as plat
    plat.reset_for_tests()
    try:
        class P:
            def __init__(self, pid): pass
            def create_time(self): return 1234.5
            def environ(self): return {"STEAMAPPID": "620", "PATH": "x"}       # Windows env names are case-insensitive
        fake = types.SimpleNamespace(Process=P)
        monkeypatch.setitem(sys.modules, "psutil", fake)
        assert _resolver(steam).appid_for_pid(4242) == 620
        class Denied(P):
            def environ(self): raise PermissionError("access denied")
        fake.Process = Denied
        assert SteamResolver(home=steam.root, roots=[steam.root]).appid_for_pid(4243) is None     # protected -> unknown, not an error
    finally:
        monkeypatch.delenv("SCREENTIME_PLATFORM")
        plat.reset_for_tests()
