import ast
import os
import time
from pathlib import Path

import pytest

from screentime import app_identity, steam_library
from screentime.steam_library import SteamResolver, parse_vdf


def acf(appid, name, extra=""):
    return (f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"Universe"\t\t"1"\n'
            f'\t"name"\t\t"{name}"\n\t"installdir"\t\t"{name.replace(" ", "")}"\n{extra}}}\n')


def libraryfolders(*paths):
    body = "".join(f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{p}"\n\t\t"label"\t\t""\n\t\t"apps"\n\t\t{{\n\t\t}}\n\t}}\n'
                   for i, p in enumerate(paths))
    return f'"libraryfolders"\n{{\n{body}}}\n'


class FakeClock:
    def __init__(self):
        self.t = 1000.0
    def __call__(self):
        return self.t


@pytest.fixture
def steam(tmp_path):
    """~/.local/share/Steam (with ~/.steam/steam symlinked to it) plus a second
    library on another 'drive', and one library that is not mounted."""
    home = tmp_path / "home"
    root = home / ".local/share/Steam"
    (root / "steamapps").mkdir(parents=True)
    (home / ".steam").mkdir()
    (home / ".steam/steam").symlink_to(root)
    lib2 = tmp_path / "mnt/games/SteamLibrary"
    (lib2 / "steamapps").mkdir(parents=True)
    gone = tmp_path / "mnt/unmounted/SteamLibrary"
    (root / "steamapps/libraryfolders.vdf").write_text(libraryfolders(root, lib2, gone))
    (root / "steamapps/appmanifest_2620.acf").write_text(acf(2620, "Call of Duty 2"))
    (lib2 / "steamapps/appmanifest_730.acf").write_text(acf(730, "Counter-Strike 2"))

    class S:
        pass
    s = S()
    s.home, s.root, s.lib2, s.gone = home, root, lib2, gone
    s.clock = FakeClock()
    s.resolver = SteamResolver(home=home, clock=s.clock, recheck_seconds=30)
    return s


# ------------------------------------------------------------------ parsing
def test_parse_vdf_nested_quotes_comments_and_case():
    d = parse_vdf('"AppState" // top\n{\n "Name" "Say \\"hi\\" \\\\ ok"\n "UserConfig" { "language" "english" }\n}')
    assert d["appstate"]["name"] == 'Say "hi" \\ ok'
    assert d["appstate"]["userconfig"]["language"] == "english"


@pytest.mark.parametrize("bad", ["", "{{{", '"a" "b" "c"', '"AppState" {', '"x" }', "\x00\xff garbage", '"a" {' * 500])
def test_parse_vdf_never_raises_on_garbage(bad):
    assert isinstance(parse_vdf(bad), dict)


def test_parse_vdf_unicode_name():
    assert parse_vdf(acf(1, "Ünïcode ゲーム"))["appstate"]["name"] == "Ünïcode ゲーム"


def test_library_paths_new_and_legacy_format():
    new = parse_vdf(libraryfolders("/a", "/b"))
    assert steam_library.library_paths_from_vdf(new) == [Path("/a"), Path("/b")]
    legacy = parse_vdf('"LibraryFolders"\n{\n "TimeNextStatsReport" "123"\n "1" "/mnt/x"\n "2" "/mnt/y"\n}')
    assert steam_library.library_paths_from_vdf(legacy) == [Path("/mnt/x"), Path("/mnt/y")]


# ----------------------------------------------------------- AppID parsing
@pytest.mark.parametrize("key,expected", [
    ("steam_app_2620", 2620), ("steam_app_1", 1), ("steam_app_4294967295", 4294967295),
    ("steam_app_0", None), ("steam_app_", None), ("steam_app_abc", None), ("steam_app_99999999999", None),
    ("steam_app_2620x", None), ("steam", None), ("", None), ("firefox", None), ("xsteam_app_5", None),
])
def test_appid_from_key(key, expected):
    assert steam_library.appid_from_key(key) == expected


def test_appid_from_exec_line():
    assert steam_library.appid_from_exec("steam steam://rungameid/2620") == 2620
    assert steam_library.appid_from_exec("/usr/bin/steam %U") is None


# --------------------------------------------------------- local resolution
def test_resolves_game_in_main_library(steam):
    assert steam.resolver.name_for_appid(2620) == "Call of Duty 2"


def test_resolves_game_in_second_library(steam):
    assert steam.resolver.name_for_appid(730) == "Counter-Strike 2"


def test_duplicate_symlinked_roots_scanned_once(steam):
    assert len(steam.resolver.roots()) == 1
    libs = steam.resolver.libraries()
    assert len(libs) == len({os.path.realpath(p) for p in libs}) == 2


def test_unmounted_library_is_skipped_not_fatal(steam):
    libs = steam.resolver.libraries()
    assert steam.gone not in libs and not steam.gone.exists()
    assert steam.lib2 in libs and steam.root.resolve() in libs      # the real ones survive


@pytest.mark.parametrize("bad", [0, -5, "abc", None, "", 2**40, 999999])
def test_unknown_or_invalid_appid_returns_none(steam, bad):
    assert steam.resolver.name_for_appid(bad) is None


def test_no_steam_installed_returns_none(tmp_path):
    r = SteamResolver(home=tmp_path / "nohome")
    assert r.roots() == [] and r.name_for_appid(2620) is None


def test_missing_libraryfolders_vdf_still_uses_root_library(steam):
    (steam.root / "steamapps/libraryfolders.vdf").unlink()
    r = SteamResolver(home=steam.home, clock=steam.clock)
    assert r.name_for_appid(2620) == "Call of Duty 2"      # root itself
    assert r.name_for_appid(730) is None                    # second library unknown without the vdf


def test_corrupt_manifest_is_ignored(steam):
    (steam.root / "steamapps/appmanifest_555.acf").write_text("\x00\x01 not vdf {{{")
    assert steam.resolver.name_for_appid(555) is None
    assert steam.resolver.name_for_appid(2620) == "Call of Duty 2"


def test_manifest_without_name_falls_back_to_installdir(steam):
    (steam.root / "steamapps/appmanifest_77.acf").write_text('"AppState" { "appid" "77" "installdir" "SomeDir" }')
    assert steam.resolver.name_for_appid(77) == "SomeDir"


def test_uppercase_steamapps_dir_and_flatpak_root(tmp_path):
    home = tmp_path / "h"
    root = home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    (root / "SteamApps").mkdir(parents=True)
    (root / "SteamApps/appmanifest_10.acf").write_text(acf(10, "Counter-Strike"))
    assert SteamResolver(home=home).name_for_appid(10) == "Counter-Strike"


# ------------------------------------------------------------------ caching
def test_repeated_lookups_do_not_touch_disk(steam, monkeypatch):
    r = steam.resolver
    assert r.name_for_appid(2620) == "Call of Duty 2"
    assert r.name_for_appid(999) is None
    reads = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (reads.append(self), real(self, *a, **k))[1])
    stats = []
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: (stats.append(self), real_stat(self, *a, **k))[1])
    for _ in range(500):                       # ~ 500 polls, clock not advanced
        assert r.name_for_appid(2620) == "Call of Duty 2"
        assert r.name_for_appid(999) is None
    assert reads == [] and stats == []


def test_new_install_is_picked_up_after_recheck_interval(steam):
    r = steam.resolver
    assert r.name_for_appid(4000) is None                       # cached as unknown
    (steam.root / "steamapps/appmanifest_4000.acf").write_text(acf(4000, "Garry's Mod"))
    bump = time.time() + 5
    os.utime(steam.root / "steamapps", (bump, bump))            # dir mtime moves on add
    assert r.name_for_appid(4000) is None                       # interval not elapsed: still cached
    steam.clock.t += 31
    assert r.name_for_appid(4000) == "Garry's Mod"


def test_new_library_added_to_vdf_is_picked_up(steam, tmp_path):
    r = steam.resolver
    assert r.name_for_appid(620) is None
    lib3 = tmp_path / "mnt/extra/SteamLibrary"
    (lib3 / "steamapps").mkdir(parents=True)
    (lib3 / "steamapps/appmanifest_620.acf").write_text(acf(620, "Portal 2"))
    vdf = steam.root / "steamapps/libraryfolders.vdf"
    vdf.write_text(libraryfolders(steam.root, steam.lib2, lib3))
    bump = time.time() + 5
    os.utime(vdf, (bump, bump))
    steam.clock.t += 31
    assert r.name_for_appid(620) == "Portal 2"


def test_explicit_invalidate_clears_cache(steam):
    r = steam.resolver
    assert r.name_for_appid(4001) is None
    (steam.root / "steamapps/appmanifest_4001.acf").write_text(acf(4001, "Late Game"))
    r.invalidate()
    assert r.name_for_appid(4001) == "Late Game"


def test_removed_game_stops_resolving_after_recheck(steam):
    r = steam.resolver
    assert r.name_for_appid(2620) == "Call of Duty 2"
    (steam.root / "steamapps/appmanifest_2620.acf").unlink()
    bump = time.time() + 5
    os.utime(steam.root / "steamapps", (bump, bump))
    steam.clock.t += 31
    assert r.name_for_appid(2620) is None


# ----------------------------------------------------- process information
def _proc(tmp_path, pid, env: dict):
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    return str(tmp_path / "proc")


def test_appid_for_pid_reads_steam_env(steam, tmp_path):
    root = _proc(tmp_path, 4242, {"HOME": "/h", "SteamGameId": "730", "PATH": "/bin"})
    r = SteamResolver(home=steam.home, proc_root=root)
    assert r.appid_for_pid(4242) == 730
    assert r.appid_for_pid(1) is None            # no such fake pid
    assert r.appid_for_pid(None) is None and r.appid_for_pid(0) is None


def test_appid_for_pid_ignores_non_steam_and_garbage(steam, tmp_path):
    root = _proc(tmp_path, 5, {"HOME": "/h"})
    _proc(tmp_path, 6, {"SteamGameId": "not-a-number"})
    r = SteamResolver(home=steam.home, proc_root=root)
    assert r.appid_for_pid(5) is None and r.appid_for_pid(6) is None


# ------------------------------------------------- app_identity integration
@pytest.fixture
def identity(steam, monkeypatch, tmp_path):
    """app_identity with an empty desktop index and our fake Steam tree."""
    monkeypatch.setattr(app_identity, "_desktop_file_index", lambda: {})
    monkeypatch.setattr(steam_library, "_default", steam.resolver)
    yield steam
    steam_library.reset_default_resolver()


def test_resolve_replaces_steam_app_id_with_game_name(identity):
    r = app_identity.resolve("steam_app_2620")
    assert r.display_name == "Call of Duty 2"
    assert r.key == "steam_app_2620"             # key unchanged: existing history still groups


def test_resolve_second_library_game(identity):
    assert app_identity.resolve("steam_app_730").display_name == "Counter-Strike 2"


def test_resolve_unknown_steam_id_falls_back_to_current_display(identity):
    r = app_identity.resolve("steam_app_31337")
    assert r.display_name == "Steam App 31337" and r.key == "steam_app_31337"


def test_resolve_without_any_steam_install_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr(app_identity, "_desktop_file_index", lambda: {})
    monkeypatch.setattr(steam_library, "_default", SteamResolver(home=tmp_path / "none"))
    assert app_identity.resolve("steam_app_2620").display_name == "Steam App 2620"
    steam_library.reset_default_resolver()


def test_desktop_file_takes_priority_over_manifest(steam, monkeypatch):
    monkeypatch.setattr(steam_library, "_default", steam.resolver)
    monkeypatch.setattr(app_identity, "_desktop_file_index",
                        lambda: {"steam_app_2620": ("My Shortcut Name", "steam_icon_2620", "/x.desktop")})
    r = app_identity.resolve("steam_app_2620")
    assert r.display_name == "My Shortcut Name" and r.desktop_file == "/x.desktop"
    steam_library.reset_default_resolver()


def test_steam_shortcut_desktop_file_is_indexed_by_appid(tmp_path):
    d = tmp_path / "apps"
    d.mkdir()
    (d / "Half-Life.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Half-Life\nExec=steam steam://rungameid/70\nIcon=steam_icon_70\n")
    idx = app_identity._build_index_from_dirs([str(d)])
    assert idx["steam_app_70"][0] == "Half-Life"


def test_native_steam_game_named_via_process_env(identity, tmp_path):
    proc = _proc(tmp_path, 777, {"SteamGameId": "730"})
    identity.resolver._proc_root = proc
    r = app_identity.resolve("cs2", pid=777)
    assert r.display_name == "Counter-Strike 2"
    assert r.key == "cs2"                        # native key preserved


def test_non_steam_app_unaffected(identity):
    assert app_identity.resolve("firefox").display_name == "Firefox"


def test_icon_prefers_steam_icon_only_if_present(identity):
    assert app_identity.resolve("steam_app_2620").icon_name == "steam"
    icon = identity.home / ".local/share/icons/hicolor/32x32/apps/steam_icon_2620.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"x")
    assert app_identity.resolve("steam_app_2620").icon_name == "steam_icon_2620"


# ------------------------------------------------------- daemon integration
def test_repair_renames_only_fallback_named_steam_rows(steam, tmp_path):
    from screentime.daemon import repair_steam_names
    from screentime.db import Database
    db = Database(tmp_path / "r.db")
    a = db.get_or_create_app("steam_app_2620", "Steam App 2620", "steam_app_2620", None)
    b = db.get_or_create_app("steam_app_730", "My Custom Name", None, None)     # already named
    c = db.get_or_create_app("steam_app_31337", "Steam App 31337", None, None)  # unknown
    d = db.get_or_create_app("firefox", "Firefox", None, None)
    assert repair_steam_names(db, steam.resolver) == 1
    assert db.get_app(a.id).display_name == "Call of Duty 2"
    assert db.get_app(b.id).display_name == "My Custom Name"
    assert db.get_app(c.id).display_name == "Steam App 31337"
    assert db.get_app(d.id).display_name == "Firefox"
    assert repair_steam_names(db, steam.resolver) == 0                            # idempotent
    db.close()


def test_repair_keeps_history_intact(steam, tmp_path):
    from screentime.daemon import repair_steam_names
    from screentime.db import Database
    db = Database(tmp_path / "h.db")
    app = db.get_or_create_app("steam_app_2620", "Steam App 2620", None, None)
    sid = db.open_session(app.id, 1_700_000_000)
    db.close_session(sid, 1_700_000_600, "focus_change")
    repair_steam_names(db, steam.resolver)
    assert db.get_app(app.id).key == "steam_app_2620"
    assert len(db.sessions_for_app(app.id)) == 1
    db.close()


def test_daemon_does_not_downgrade_known_name_when_metadata_vanishes(steam, monkeypatch, tmp_path):
    import screentime.daemon as d
    from screentime.db import Database
    from screentime.window_detector import RawFocus
    monkeypatch.setattr(app_identity, "_desktop_file_index", lambda: {})
    monkeypatch.setattr(steam_library, "_default", SteamResolver(home=tmp_path / "no-steam"))  # nothing found
    db = Database(tmp_path / "dd.db")
    db.get_or_create_app("steam_app_2620", "Call of Duty 2", "steam", None)
    daemon = d.Daemon(db)
    focus = daemon._raw_to_focus(RawFocus(identifier="steam_app_2620", pid=None, title=None))
    assert focus.display_name == "Call of Duty 2"
    unknown = daemon._raw_to_focus(RawFocus(identifier="steam_app_999", pid=None, title=None))
    assert unknown.display_name == "Steam App 999"
    steam_library.reset_default_resolver()
    db.close()


def test_daemon_start_repairs_stored_names(steam, monkeypatch, tmp_path):
    import screentime.daemon as d
    from screentime.db import Database
    monkeypatch.setattr(steam_library, "_default", steam.resolver)
    db = Database(tmp_path / "start.db")
    app = db.get_or_create_app("steam_app_2620", "Steam App 2620", None, None)
    d.Daemon(db)
    assert db.get_app(app.id).display_name == "Call of Duty 2"
    steam_library.reset_default_resolver()
    db.close()


# --------------------------------------------- offline / privacy guarantee
def test_package_imports_no_network_modules():
    banned = {"socket", "urllib", "urllib3", "http", "requests", "httpx", "aiohttp", "ssl",
              "ftplib", "smtplib", "telnetlib", "xmlrpc", "websockets"}
    root = Path(__file__).resolve().parent.parent / "screentime"
    offenders = []
    for py in root.rglob("*.py"):
        if py.name == "updates.py":
            continue          # the one opt-in network module (explicit "Check for updates"); see tests/test_updates.py
        for node in ast.walk(ast.parse(py.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in banned:
                    offenders.append(f"{py.name}: {n}")
    assert offenders == []


# ------------------------------------------------------------- diagnostics
def test_installed_games_lists_every_library(steam):
    assert steam.resolver.installed_games() == [(730, "Counter-Strike 2"), (2620, "Call of Duty 2")]


def test_describe_names_the_locations_searched(steam):
    text = steam.resolver.describe()
    assert str(steam.lib2) in text and "roots=" in text


def test_cli_prints_roots_and_resolution(steam, monkeypatch, capsys):
    monkeypatch.setattr(steam_library, "SteamResolver", lambda: steam.resolver)
    assert steam_library.main(["2620", "999"]) == 0
    out = capsys.readouterr().out
    assert "AppID 2620: Call of Duty 2" in out
    assert "AppID 999: NOT FOUND" in out
    steam_library.main([])
    assert "Counter-Strike 2" in capsys.readouterr().out


def test_daemon_logs_unresolved_steam_id_once_with_search_locations(steam, monkeypatch, tmp_path, caplog):
    import logging
    import screentime.daemon as d
    from screentime.db import Database
    from screentime.window_detector import RawFocus
    monkeypatch.setattr(app_identity, "_desktop_file_index", lambda: {})
    monkeypatch.setattr(steam_library, "_default", steam.resolver)
    db = Database(tmp_path / "log.db")
    daemon = d.Daemon(db)
    with caplog.at_level(logging.INFO, logger="screentime.daemon"):
        for _ in range(5):
            daemon._raw_to_focus(RawFocus(identifier="steam_app_31337", pid=None, title=None))
    msgs = [r.getMessage() for r in caplog.records if "no local manifest" in r.getMessage()]
    assert len(msgs) == 1 and "31337" in msgs[0] and str(steam.lib2) in msgs[0]
    steam_library.reset_default_resolver()
    db.close()
