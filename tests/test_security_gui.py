"""Settings -> Security, and the screen shown when the database cannot be opened."""
import os

import pytest

pytest.importorskip("gi")
if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("GTK widget tests need a display (run under xvfb-run)", allow_module_level=True)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

Adw.init()

from screentime import keystore as K
from screentime import storage
from screentime.config import Config
from screentime.db import Database
from screentime.gui.views import settings as settings_mod
from screentime.gui.views.settings import SettingsView


def status(**kw):
    base = dict(protected=True, store_exists=True, backend="keyfile", key_id="abcd", store_bytes=64 * 1024,
                legacy_plaintext_present=False, state="ok", detail="")
    base.update(kw)
    return storage.SecurityStatus(**base)


@pytest.fixture
def protected_view(monkeypatch):
    db = storage.open_database(recover_orphans=False)
    cfg = Config(db)
    view = SettingsView(db, cfg)
    yield view, monkeypatch
    db.close()


def refresh_with(view, monkeypatch, st):
    monkeypatch.setattr(storage, "security_status", lambda *a, **k: st)
    view._refresh_security_rows()


def walk(w):
    yield w
    c = w.get_first_child()
    while c is not None:
        yield from walk(c)
        c = c.get_next_sibling()


# ------------------------------------------------------------------ settings rows
@pytest.mark.skipif(bool(os.environ.get("SCREENTIME_TEST_PROTECTED")),
                    reason="asserts plain-SQLite behavior, which that switch deliberately replaces")
def test_plain_database_is_shown_as_not_protected(tmp_path):
    db = Database(tmp_path / "plain.db")
    view = SettingsView(db, Config(db))
    assert "Not protected" in view._sec_protect_row.get_subtitle()
    assert not view._sec_key_row.get_visible() and not view._sec_state_row.get_visible()
    db.close()


def test_key_file_mode_is_labelled_weaker_and_offers_the_move(protected_view):
    view, mp = protected_view
    refresh_with(view, mp, status(backend="keyfile"))
    assert "AES-256-GCM" in view._sec_protect_row.get_subtitle()
    assert "weaker" in view._sec_key_row.get_subtitle() and view._sec_move_btn.get_visible()
    assert not view._sec_state_row.get_visible() and not view._sec_legacy_row.get_visible()


def test_keyring_mode_hides_the_move_button(protected_view):
    view, mp = protected_view
    refresh_with(view, mp, status(backend="secret-service"))
    assert "keyring" in view._sec_key_row.get_subtitle().lower() and not view._sec_move_btn.get_visible()


def test_waiting_state_explains_tracking_is_paused_and_offers_unlock(protected_view):
    view, mp = protected_view
    refresh_with(view, mp, status(state="waiting"))
    assert view._sec_state_row.get_visible() and "Paused" in view._sec_state_row.get_subtitle()
    assert view._sec_unlock_btn.get_visible()


@pytest.mark.parametrize("state,needle", [("key-missing", "recovery key"), ("wrong-key", "does not open")])
def test_permanent_key_problems_are_explained_without_an_unlock_button(protected_view, state, needle):
    view, mp = protected_view
    refresh_with(view, mp, status(state=state))
    assert needle in view._sec_state_row.get_subtitle() and not view._sec_unlock_btn.get_visible()


def test_a_plaintext_copy_is_flagged(protected_view):
    view, mp = protected_view
    refresh_with(view, mp, status(legacy_plaintext_present=True))
    assert view._sec_legacy_row.get_visible() and "unencrypted" in view._sec_legacy_row.get_subtitle()


def test_healthy_state_shows_no_warnings(protected_view):
    view, mp = protected_view
    refresh_with(view, mp, status(backend="secret-service"))
    assert not view._sec_state_row.get_visible() and not view._sec_legacy_row.get_visible()


# ------------------------------------------------------------------------ actions
class Captured:
    def __init__(self, view):
        self.calls = []
        view._alert = lambda heading, body, extra=None: self.calls.append((heading, body, extra))


def test_show_recovery_key_displays_the_key_selectable_with_a_copy_button(protected_view):
    view, mp = protected_view
    cap = Captured(view)
    key = K.encode_recovery_key(K.generate_key())
    mp.setattr(storage, "export_recovery_key", lambda *a, **k: key)
    view._on_show_recovery_key(None)
    heading, body, extra = cap.calls[0]
    assert heading == "Recovery key" and "offline" in body
    labels = [w for w in walk(extra) if isinstance(w, Gtk.Label)]
    assert labels[0].get_label() == key and labels[0].get_selectable()
    assert any(isinstance(w, Gtk.Button) and w.get_label() == "Copy to clipboard" for w in walk(extra))


def test_show_recovery_key_failure_is_explained_not_a_traceback(protected_view):
    view, mp = protected_view
    cap = Captured(view)
    def fail(*a, **k):
        raise K.KeyNotFoundError("gone")
    mp.setattr(storage, "export_recovery_key", fail)
    view._on_show_recovery_key(None)
    assert cap.calls[0][0] == "Can't read the key" and "recovery key" in cap.calls[0][1]


class FakeKM:
    outcome = "secret-service"
    def move_to_keyring(self, sid):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome
    def get_key(self, sid, interactive=False):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return b"k" * 32


def test_move_key_success_and_failure_messages(protected_view):
    view, mp = protected_view
    cap = Captured(view)
    mp.setattr(settings_mod, "KeyManager", FakeKM)
    FakeKM.outcome = "secret-service"
    view._on_move_key(None)
    assert cap.calls[-1][0] == "Key moved" and "securely removed" in cap.calls[-1][1]
    FakeKM.outcome = K.KeyStoreError("keyring said no")
    view._on_move_key(None)
    assert cap.calls[-1][0] == "Key not moved" and "key file was kept" in cap.calls[-1][1]


def test_unlock_success_and_failure_messages(protected_view):
    view, mp = protected_view
    cap = Captured(view)
    mp.setattr(settings_mod, "KeyManager", FakeKM)
    FakeKM.outcome = "x"
    view._on_unlock_keyring(None)
    assert cap.calls[-1][0] == "Unlocked"
    FakeKM.outcome = K.KeyUnavailableError("still locked", True)
    view._on_unlock_keyring(None)
    assert cap.calls[-1][0] == "Still locked" and "locked or unavailable" in cap.calls[-1][1]


# ------------------------------------------------------------ the "can't open" screen
def release_app(app):
    """Really release a GApplication that a test registered by hand.

    `register()` exports the application on the session bus (object /org/screentime/App, interface
    org.gtk.Application) and Gio has no public "unregister". `quit()` does not undo that: it only flags a running
    main loop to stop, so a manually registered app stays registered, and the next app with the same application
    id fails with "An object is already exported for the interface org.gtk.Application". The exported action
    group also holds a reference to the app, so it is never garbage-collected either.

    The one supported way to tear the registration down is the lifecycle `run()` ends with (shutdown, then
    unregister). `run()` returns immediately if `quit()` was already called, so quit has to happen *inside* it:
    schedule it from an idle callback. `activate` is disconnected first because `run()` would emit it again.
    """
    for win in list(app.get_windows()):
        win.destroy()
    app.disconnect_by_func(app._on_activate)
    GLib.idle_add(lambda: (app.quit(), GLib.SOURCE_REMOVE)[1])
    app.run([])
    assert not app.get_is_registered(), "the application is still registered and would block the next test"


@pytest.fixture
def locked_app():
    from screentime.gui.unlock import LockedApp
    app = LockedApp(storage.KeyLostError("x"))
    try:
        app.register(None)
        app._on_activate(app)
        yield app
    finally:                      # runs for a failing test and for a failing setup too
        release_app(app)


def test_locked_apps_can_be_created_one_after_another_in_one_process():
    """Regression: the fixed application id used to make every LockedApp after the first fail to register."""
    from screentime.gui.unlock import LockedApp
    for _ in range(4):
        app = LockedApp(storage.KeyLostError("x"))
        assert app.get_application_id() == "org.screentime.App"          # production id is unchanged
        assert app.get_flags() == Gio.ApplicationFlags.FLAGS_NONE       # still a unique (single-instance) app
        try:
            app.register(None)
            app._on_activate(app)
            assert app.get_is_registered() and len(app.get_windows()) == 1
        finally:
            release_app(app)
        assert not app.get_is_registered() and not app.get_windows()


def test_locked_screen_explains_in_plain_language(locked_app):
    win = locked_app.get_windows()[0]
    texts = " ".join(w.get_description() for w in walk(win) if isinstance(w, Adw.StatusPage))
    assert "recovery key" in texts
    labels = [w.get_label() for w in walk(win) if isinstance(w, Gtk.Button)]
    assert {"Try again", "Restore from recovery key", "Quit"} <= set(labels)


def test_locked_screen_restores_a_lost_key_and_restarts(locked_app, monkeypatch):
    db = storage.open_database()
    db.set_setting("precious", "data")
    db.close()
    recovery = storage.export_recovery_key(interactive=False)
    for f in (K.default_config_dir() / "keys").iterdir():
        f.unlink()
    restarted = []
    monkeypatch.setattr(locked_app, "_restart", lambda: restarted.append(True))
    locked_app._entry.set_text(recovery)
    locked_app._on_restore(None)
    assert restarted == [True]
    again = storage.open_database(recover_orphans=False)                 # access really is back
    assert again.get_setting("precious") == "data"
    again.close()


@pytest.mark.parametrize("text", ["", "AAAA-BBBB", "not a key"])
def test_locked_screen_rejects_bad_recovery_keys_without_restarting(locked_app, monkeypatch, text):
    storage.open_database().close()
    restarted = []
    monkeypatch.setattr(locked_app, "_restart", lambda: restarted.append(True))
    locked_app._entry.set_text(text)
    locked_app._on_restore(None)
    assert restarted == [] and locked_app._result.get_text()
    assert "error" in locked_app._result.get_css_classes()


def test_gui_main_shows_the_locked_screen_when_the_database_cannot_be_opened(monkeypatch):
    from screentime.gui import app as app_mod
    from screentime.gui import unlock

    def fail(**kw):
        raise storage.KeyLostError("no key")
    monkeypatch.setattr(storage, "open_database", fail)
    shown = []
    monkeypatch.setattr(unlock.LockedApp, "run", lambda self, argv: shown.append(self.error) or 42)
    assert app_mod.main() == 42 and isinstance(shown[0], storage.KeyLostError)


def test_gui_main_does_not_swallow_unrelated_errors(monkeypatch):
    from screentime.gui import app as app_mod

    def fail(**kw):
        raise RuntimeError("a genuine bug")
    monkeypatch.setattr(storage, "open_database", fail)
    with pytest.raises(RuntimeError):
        app_mod.main()
