import os
import sys
import importlib.resources as resources
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screentime.db import Database
from screentime import wayland_setup


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "ws.db")
    yield d
    d.close()


def _set_env(monkeypatch, session_type, desktop):
    monkeypatch.setenv("XDG_SESSION_TYPE", session_type)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)


def test_detect_kde_wayland(monkeypatch):
    _set_env(monkeypatch, "wayland", "KDE")
    assert wayland_setup.detect_wayland_desktop() == "kde"


def test_detect_plasma_wayland_alt_name(monkeypatch):
    _set_env(monkeypatch, "wayland", "plasma")
    assert wayland_setup.detect_wayland_desktop() == "kde"


def test_detect_gnome_wayland(monkeypatch):
    _set_env(monkeypatch, "wayland", "GNOME")
    assert wayland_setup.detect_wayland_desktop() == "gnome"


def test_detect_none_on_x11(monkeypatch):
    _set_env(monkeypatch, "x11", "KDE")
    assert wayland_setup.detect_wayland_desktop() is None


def test_detect_none_on_sway(monkeypatch):
    _set_env(monkeypatch, "wayland", "sway")
    assert wayland_setup.detect_wayland_desktop() is None


def test_kde_script_is_installed_checks_filesystem(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))
    assert wayland_setup.kde_script_is_installed() is False

    dest = fake_home / ".local/share/kwin/scripts/screentime-focus"
    dest.mkdir(parents=True)
    (dest / "metadata.json").write_text("{}")
    assert wayland_setup.kde_script_is_installed() is True


def test_install_kde_script_fails_cleanly_without_kpackagetool(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home/.local/share"))
    monkeypatch.setattr("shutil.which", lambda name: None)
    ok, msg = wayland_setup.install_kde_script()
    assert ok is False
    assert "kpackagetool" in msg


def test_install_kde_script_success_path(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))

    def fake_which(name):
        return f"/usr/bin/{name}" if name in (
            "kpackagetool6", "kwriteconfig6", "qdbus6"
        ) else None
    monkeypatch.setattr("shutil.which", fake_which)

    calls = []
    dest = fake_home / ".local/share/kwin/scripts/screentime-focus"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--install" in cmd:
            # Simulate what kpackagetool6 actually does: copies the source
            # package (including contents/code/main.js) into the KWin
            # scripts directory.
            (dest / "contents" / "code").mkdir(parents=True, exist_ok=True)
            (dest / "metadata.json").write_text("{}")
            (dest / "contents" / "code" / "main.js").write_text("// test")
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        ok, msg = wayland_setup.install_kde_script()

    assert ok is True
    assert any("kpackagetool6" in c[0] for c in calls)
    assert any("--install" in c for c in calls)
    assert any("kwriteconfig6" in c[0] for c in calls)
    assert any("reconfigure" in c for c in calls)
    assert any("loadScript" in " ".join(c) for c in calls)  # the stronger, documented live-reload mechanism
    assert "loaded live" in msg
    assert wayland_setup.kde_script_is_installed() is True


def test_install_kde_script_falls_back_message_without_qdbus(tmp_path, monkeypatch):
    """Without qdbus available, the install still succeeds (kpackagetool6 +
    kwriteconfig6 are enough to persist it for next login), but the message
    must not falsely claim it's working immediately."""
    fake_home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "kpackagetool6" else None)

    dest = fake_home / ".local/share/kwin/scripts/screentime-focus"

    def fake_run(cmd, **kwargs):
        if "--install" in cmd:
            (dest / "contents" / "code").mkdir(parents=True, exist_ok=True)
            (dest / "metadata.json").write_text("{}")
            (dest / "contents" / "code" / "main.js").write_text("// test")
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        ok, msg = wayland_setup.install_kde_script()

    assert ok is True
    assert "loaded live" not in msg
    assert "logging out" in msg.lower()


def test_install_kde_script_removes_stale_copy_first(tmp_path, monkeypatch):
    """A previous install with bad/incompatible metadata (exactly what
    happened in practice) must not block a clean reinstall -- the old
    directory should be wiped before the new one is installed."""
    fake_home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "kpackagetool6" else None)

    dest = fake_home / ".local/share/kwin/scripts/screentime-focus"
    dest.mkdir(parents=True)
    (dest / "metadata.json").write_text('{"KPlugin": {"Id": "screentime-focus"}}')  # old broken version
    (dest / "stale_marker.txt").write_text("leftover from a previous, broken install")

    def fake_run(cmd, **kwargs):
        if "--install" in cmd:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "metadata.json").write_text("{}")  # the "new" (fixed) copy
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        ok, msg = wayland_setup.install_kde_script()

    assert ok is True
    assert not (dest / "stale_marker.txt").exists()  # old contents actually gone, not merged


def test_install_kde_script_reports_kpackagetool_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home/.local/share"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "kpackagetool6" else None)

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "some kpackagetool error"
        return m

    with patch("subprocess.run", side_effect=fake_run):
        ok, msg = wayland_setup.install_kde_script()
    assert ok is False
    assert "some kpackagetool error" in msg


def test_install_kde_script_catches_silent_failure(tmp_path, monkeypatch):
    """kpackagetool6 can exit 0 without actually producing a package
    kde recognizes (this is exactly the bug that motivated this check) --
    verify install_kde_script() catches that rather than reporting success."""
    fake_home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "kpackagetool6" else None)

    def fake_run(cmd, **kwargs):
        # Reports success but never actually writes the file -- e.g. a
        # kpackagetool6 version that silently no-ops on some validation error.
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        ok, msg = wayland_setup.install_kde_script()
    assert ok is False
    assert "isn't on disk" in msg


def test_install_gnome_extension_copies_files(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))
    monkeypatch.setattr("shutil.which", lambda name: None)  # no gnome-extensions binary

    ok, msg = wayland_setup.install_gnome_extension()
    assert ok is True
    dest = fake_home / ".local/share/gnome-shell/extensions/screentime-focus@screentime.local"
    assert (dest / "metadata.json").exists()
    assert (dest / "extension.js").exists()
    assert "gnome-extensions enable" in msg  # told the user how to finish manually


def test_install_gnome_extension_reinstall_overwrites(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))
    monkeypatch.setattr("shutil.which", lambda name: None)

    wayland_setup.install_gnome_extension()
    dest = fake_home / ".local/share/gnome-shell/extensions/screentime-focus@screentime.local"
    (dest / "stale_marker.txt").write_text("old version")

    ok, msg = wayland_setup.install_gnome_extension()
    assert ok is True
    assert not (dest / "stale_marker.txt").exists()  # old install dir was replaced, not merged


def test_auto_install_skips_when_already_up_to_date(db, monkeypatch, tmp_path):
    """A real (mocked-subprocess) install followed by a second call must
    not reinstall -- the content-hash comparison should recognize it's
    already correct without needing any settings-table flag."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home/.local/share"))
    monkeypatch.setattr("shutil.which", lambda name: None)  # no gnome-extensions binary; copy still works

    r1 = wayland_setup.auto_install_if_needed(db)
    assert r1 is not None and r1[0] is True  # first time: actually installs
    assert wayland_setup.gnome_extension_is_up_to_date() is True

    r2 = wayland_setup.auto_install_if_needed(db)
    assert r2 is None  # already up to date -- must not reinstall


def test_auto_install_self_heals_a_stale_install(db, monkeypatch, tmp_path):
    """This is the exact real-world scenario that motivated this design: an
    old/broken copy is already on disk (e.g. from before a bug fix shipped),
    with no settings-table flag ever set for it (it predates this
    mechanism). auto_install_if_needed must recognize the mismatch and
    reinstall automatically, with no manual 'Reinstall' click required."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    fake_home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local/share"))
    monkeypatch.setattr("shutil.which", lambda name: None)

    # Simulate a stale install: right shape, wrong (old) content.
    dest = fake_home / ".local/share/gnome-shell/extensions/screentime-focus@screentime.local"
    dest.mkdir(parents=True)
    (dest / "metadata.json").write_text("{}")  # not a byte-for-byte match with the bundled version

    assert wayland_setup.gnome_extension_is_installed() is True
    assert wayland_setup.gnome_extension_is_up_to_date() is False

    result = wayland_setup.auto_install_if_needed(db)
    assert result is not None and result[0] is True
    assert wayland_setup.gnome_extension_is_up_to_date() is True
    assert (dest / "extension.js").exists()  # the real bundled file, not just metadata.json


def test_auto_install_does_not_spam_retry_same_failed_version(db, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home/.local/share"))

    calls = []

    def fake_install():
        calls.append(1)
        return False, "kpackagetool6 not found"

    monkeypatch.setattr(wayland_setup, "install_for_current_desktop", fake_install)

    r1 = wayland_setup.auto_install_if_needed(db)
    assert r1 == (False, "kpackagetool6 not found")
    assert len(calls) == 1

    r2 = wayland_setup.auto_install_if_needed(db)
    assert r2 is None  # same failure, same bundled version -- don't retry
    assert len(calls) == 1


def test_auto_install_retries_after_bundled_content_changes(db, monkeypatch):
    """Simulates a screentime package upgrade: once the bundled resource
    content differs from whatever a previous (failed) attempt recorded,
    auto-install must try again."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(wayland_setup, "kde_script_is_up_to_date", lambda: False)

    calls = []

    def fake_install():
        calls.append(1)
        return False, "still failing"

    monkeypatch.setattr(wayland_setup, "install_for_current_desktop", fake_install)

    hashes = iter(["hash-v1", "hash-v1", "hash-v2"])
    monkeypatch.setattr(wayland_setup, "_hash_resource_dir", lambda _: next(hashes))

    wayland_setup.auto_install_if_needed(db)  # v1, attempt #1 (fails)
    wayland_setup.auto_install_if_needed(db)  # v1 again -- should skip (same failed hash)
    wayland_setup.auto_install_if_needed(db)  # v2 (upgrade!) -- should attempt again
    assert len(calls) == 2


def test_auto_install_does_nothing_on_x11(db, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    called = []
    monkeypatch.setattr(wayland_setup, "install_for_current_desktop", lambda: called.append(1))
    assert wayland_setup.auto_install_if_needed(db) is None
    assert called == []


def test_auto_install_up_to_date_state_survives_new_db_connection(tmp_path, monkeypatch):
    """The gating is filesystem-content-based, not a settings-table flag,
    so it's inherently restart-safe -- but worth confirming explicitly: a
    fresh Database handle over the same file must still recognize a
    previously-successful install as up to date."""
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home/.local/share"))
    monkeypatch.setattr("shutil.which", lambda name: None)

    dbfile = tmp_path / "persist.db"
    db1 = Database(dbfile)
    wayland_setup.auto_install_if_needed(db1)
    db1.close()

    db2 = Database(dbfile)  # simulates a daemon restart
    result = wayland_setup.auto_install_if_needed(db2)
    db2.close()
    assert result is None


# ------------------------------------------------------- shipped metadata
def test_shipped_kde_metadata_has_required_fields():
    """Regression test for a real bug: kpackagetool6 refused to recognize
    our KWin script as type "KWin/Script" because metadata.json lacked the
    ServiceTypes field kpackagetool6 actually checks (confirmed against a
    real, working KWin script's metadata.json). Fails loudly if this ever
    regresses instead of silently shipping an unusable script again."""
    import json
    path = wayland_setup._kde_script_resource_dir() / "metadata.json"
    with resources.as_file(path) as p:
        data = json.loads(p.read_text())

    assert "KWin/Script" in data.get("KPlugin", {}).get("ServiceTypes", []), (
        "metadata.json must declare ServiceTypes: [\"KWin/Script\"] inside "
        "KPlugin, or kpackagetool6 --type KWin/Script won't recognize it"
    )
    assert data.get("X-Plasma-API") == "javascript"
    assert data.get("X-Plasma-MainScript"), "must point at the entry script, e.g. code/main.js"

    # The MainScript path must actually resolve under contents/.
    script_dir = wayland_setup._kde_script_resource_dir()
    with resources.as_file(script_dir) as script_path:
        main_script = script_path / "contents" / data["X-Plasma-MainScript"]
        assert main_script.exists(), f"X-Plasma-MainScript points at {main_script}, which doesn't exist"


# ---------------------------------------------------------------------------
# Regression: tracking worked after install but died at every reboot.
# Cause: metadata.json lacked "KPackageStructure": "KWin/Script", so KWin never
# discovered the packaged script at login (it only ran because the installer
# also pushed it live with Scripting.loadScript).
# ---------------------------------------------------------------------------
def test_bundled_kwin_metadata_declares_package_structure():
    import json
    from screentime import wayland_setup
    with wayland_setup.resources.as_file(wayland_setup._kde_script_resource_dir()) as d:
        meta = json.loads((d / "metadata.json").read_text())
        assert meta["KPackageStructure"] == "KWin/Script"
        assert meta["KPlugin"]["Id"] == d.name == "screentime-focus"      # Id must equal install dir
        assert (d / "contents" / "code" / "main.js").exists()             # MainScript resolves under contents/
        assert meta["X-Plasma-MainScript"] == "code/main.js"


def test_metadata_valid_detects_missing_key(tmp_path):
    import json
    from screentime import wayland_setup
    (tmp_path / "metadata.json").write_text(json.dumps({"KPlugin": {"Id": "screentime-focus"}}))
    assert wayland_setup.kde_script_metadata_valid(tmp_path) is False
    (tmp_path / "metadata.json").write_text(json.dumps({"KPackageStructure": "KWin/Script"}))
    assert wayland_setup.kde_script_metadata_valid(tmp_path) is True
    (tmp_path / "metadata.json").write_text("not json")
    assert wayland_setup.kde_script_metadata_valid(tmp_path) is False
    assert wayland_setup.kde_script_metadata_valid(tmp_path / "nope") is False


def test_old_install_without_key_is_reported_stale_and_reinstalled(tmp_path, monkeypatch):
    """The exact state on the affected machine: files present, content otherwise
    fine, key missing. It must count as NOT up to date so the auto-installer
    replaces it on the next daemon/GUI start."""
    import shutil
    import json
    from screentime import wayland_setup
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    dest = wayland_setup._kde_script_dest()
    with wayland_setup.resources.as_file(wayland_setup._kde_script_resource_dir()) as src:
        shutil.copytree(src, dest)
    assert wayland_setup.kde_script_is_up_to_date() is True           # a correct copy is fine
    meta = json.loads((dest / "metadata.json").read_text())
    del meta["KPackageStructure"]
    (dest / "metadata.json").write_text(json.dumps(meta))
    assert wayland_setup.kde_script_is_installed() is True            # still "on disk"...
    assert wayland_setup.kde_script_is_up_to_date() is False          # ...but not good enough


# ---------------------------------------------------------------------------
# KWin load/run helpers. loadScript only *registers* a script; Script<id>.run
# executes it. Loading without running leaves a script that looks loaded but
# never fires -- which silently freezes the daemon on its last-seen window.
# ---------------------------------------------------------------------------
class FakeQdbus:
    """Records qdbus calls; loadScript returns an id like real KWin."""
    def __init__(self, load_id="7", fail=()):
        self.calls, self.load_id, self.fail = [], load_id, set(fail)

    def __call__(self, cmd, **kw):
        from unittest.mock import MagicMock
        self.calls.append(cmd[2:])
        member = cmd[3].rsplit(".", 1)[1]
        if member in self.fail:
            return MagicMock(returncode=1, stdout="", stderr="err")
        out = self.load_id + "\n" if member == "loadScript" else ""
        return MagicMock(returncode=0, stdout=out, stderr="")

    def members(self):
        return [c[1].rsplit(".", 1)[1] for c in self.calls]


def test_load_and_run_runs_the_script_it_just_loaded():
    from pathlib import Path
    from screentime import wayland_setup
    fq = FakeQdbus(load_id="7")
    import subprocess
    orig = subprocess.run
    subprocess.run = fq
    try:
        assert wayland_setup.kwin_load_and_run("/usr/bin/qdbus6", Path("/x/main.js"), "screentime-focus")
    finally:
        subprocess.run = orig
    assert fq.members() == ["loadScript", "run"]
    assert fq.calls[1][0] == "/Scripting/Script7"          # runs *that* script id
    assert fq.calls[1][1] == "org.kde.kwin.Script.run"


@pytest.mark.parametrize("load_id,fail", [("-1", ()), ("garbage", ()), ("7", ("run",)), ("7", ("loadScript",))])
def test_load_and_run_reports_failure(monkeypatch, load_id, fail):
    from pathlib import Path
    from screentime import wayland_setup
    monkeypatch.setattr("subprocess.run", FakeQdbus(load_id, fail))
    assert wayland_setup.kwin_load_and_run("/q", Path("/x"), "n") is False


def _install_announce(tmp_path, monkeypatch):
    from screentime import wayland_setup
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    f = wayland_setup._kde_script_dest() / "contents" / "code" / "announce.js"
    f.parent.mkdir(parents=True)
    f.write_text("//")
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/qdbus6" if n == "qdbus6" else None)
    return f


def test_announce_never_touches_the_persistent_script(tmp_path, monkeypatch):
    """Regression: the old reload unloaded 'screentime-focus' and could leave it
    loaded-but-not-running, freezing focus on the last window seen."""
    from screentime import wayland_setup
    _install_announce(tmp_path, monkeypatch)
    fq = FakeQdbus()
    monkeypatch.setattr("subprocess.run", fq)
    assert wayland_setup.announce_focus_once(linger_seconds=0) is True
    # The persistent script's *plugin name* must never appear as an argument
    # (its file path legitimately contains "screentime-focus", so compare names).
    names = {a for c in fq.calls for a in c[2:] if not a.startswith("/")}
    assert "screentime-focus" not in names
    assert "screentime-announce" in names
    assert fq.members() == ["unloadScript", "loadScript", "run", "unloadScript"]   # stale-cleanup, load, run, cleanup
    assert all("screentime-announce" in " ".join(c) or "Script" in c[0] for c in fq.calls)


def test_announce_cleans_up_even_if_run_fails(tmp_path, monkeypatch):
    from screentime import wayland_setup
    _install_announce(tmp_path, monkeypatch)
    fq = FakeQdbus(fail=("run",))
    monkeypatch.setattr("subprocess.run", fq)
    assert wayland_setup.announce_focus_once(linger_seconds=0) is False
    assert fq.members()[-1] == "unloadScript"


def test_announce_safe_without_qdbus_script_or_session(tmp_path, monkeypatch):
    from screentime import wayland_setup
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert wayland_setup.announce_focus_once(0) is False
    _install_announce(tmp_path, monkeypatch)
    def boom(*a, **k):
        raise OSError("no session bus")
    monkeypatch.setattr("subprocess.run", boom)
    assert wayland_setup.announce_focus_once(0) is False


def test_installer_live_load_runs_the_script(tmp_path, monkeypatch):
    """install_kde_script must load AND run the fresh script, replacing the old one."""
    import shutil as _sh
    from screentime import wayland_setup
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    fq = FakeQdbus()
    def fake_run(cmd, **kw):
        from unittest.mock import MagicMock
        if "kpackagetool6" in cmd[0]:                      # emulate a successful package install
            with wayland_setup.resources.as_file(wayland_setup._kde_script_resource_dir()) as src:
                _sh.copytree(src, wayland_setup._kde_script_dest())
            return MagicMock(returncode=0, stdout="", stderr="")
        if "kwriteconfig" in cmd[0]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd[2] == "/KWin":
            return MagicMock(returncode=0, stdout="", stderr="")
        return fq(cmd)
    monkeypatch.setattr("shutil.which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr("subprocess.run", fake_run)
    ok, _msg = wayland_setup.install_kde_script()
    assert ok
    assert fq.members() == ["unloadScript", "loadScript", "run"]


# ---------------------------------------------------------------------------
# Execute the real KWin scripts in Node against a mocked KWin API.
# ---------------------------------------------------------------------------
import json as _json
import shutil as _shutil
import subprocess as _subprocess

_NODE = _shutil.which("node")
_CODE = Path(__file__).resolve().parent.parent / "screentime/resources/kde-script/screentime-focus/contents/code"


def _run_js(script: str, scenario: str):
    """scenario: JS that runs after the script loaded; may call window.fire(...).
    Returns the list of ReportFocus payloads sent via callDBus."""
    harness = r"""
    const sent = [];
    const listeners = [];
    globalThis.print = () => {};
    globalThis.callDBus = (svc, path, iface, method, payload) => { sent.push(JSON.parse(payload)); };
    globalThis.workspace = {
        activeWindow: __ACTIVE__,
        windowActivated: { connect: (f) => listeners.push(f) },
    };
    globalThis.fire = (w) => { workspace.activeWindow = w; listeners.forEach(f => f(w)); };
    """
    active = "null"
    if "__initial__" in scenario:
        active = '{resourceClass:"brave-browser", pid:11, caption:"tab"}'
    harness = harness.replace("__ACTIVE__", active)
    code = harness + (_CODE / script).read_text() + "\n" + scenario + "\nconsole.log(JSON.stringify(sent));"
    r = _subprocess.run([_NODE, "-e", code], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return _json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _NODE, reason="node not installed")
def test_main_js_reports_every_activation_including_focus_cleared():
    """Regression: 'no window focused' used to be dropped, so the daemon kept
    counting time for the previously focused window (a background Brave)."""
    sent = _run_js("main.js", '''
        fire({resourceClass:"brave-browser", pid:11, caption:"a"});
        fire({resourceClass:"steam_app_2620", pid:22, caption:"game"});
        fire(null);
        fire({resourceClass:"konsole", pid:33, caption:"k"});
    ''')
    assert [s["resourceClass"] for s in sent] == ["brave-browser", "steam_app_2620", "", "konsole"]
    assert sent[2] == {"resourceClass": "", "pid": 0, "caption": ""}
    assert sent[1]["pid"] == 22


@pytest.mark.skipif(not _NODE, reason="node not installed")
def test_main_js_announces_initial_window_at_load():
    sent = _run_js("main.js", "/* __initial__ */")
    assert [s["resourceClass"] for s in sent] == ["brave-browser"]


@pytest.mark.skipif(not _NODE, reason="node not installed")
def test_announce_js_reports_active_window_once():
    sent = _run_js("announce.js", "/* __initial__ */")
    assert sent == [{"resourceClass": "brave-browser", "pid": 11, "caption": "tab"}]


@pytest.mark.skipif(not _NODE, reason="node not installed")
def test_announce_js_is_silent_without_active_window():
    assert _run_js("announce.js", "") == []


@pytest.mark.skipif(not _NODE, reason="node not installed")
def test_main_js_survives_calldbus_throwing():
    harness_scenario = 'globalThis.callDBus = () => { throw new Error("no daemon"); }; fire({resourceClass:"x",pid:1,caption:""});'
    # must not raise: a daemon that isn't running yet is normal
    _run_js("main.js", harness_scenario)


def test_announce_script_ships_in_package_and_is_hashed():
    from screentime import wayland_setup
    with wayland_setup.resources.as_file(wayland_setup._kde_script_resource_dir()) as d:
        assert (d / "contents/code/announce.js").exists()
