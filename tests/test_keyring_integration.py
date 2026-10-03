"""The Secret Service client against a REAL keyring implementation (gnome-keyring,
which speaks the same freedesktop API KWallet exposes), inside a private D-Bus
session so a developer's own wallet is never touched. Skipped if the tools are
not installed. Not covered here: KWallet itself, and the 'locked, then unlocked
later' flow (gnome-keyring cannot be re-unlocked non-interactively)."""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NEEDED = ("dbus-run-session", "gnome-keyring-daemon")
pytestmark = pytest.mark.skipif(any(shutil.which(t) is None for t in NEEDED),
                                reason="needs dbus-run-session and gnome-keyring-daemon")

CLIENT = textwrap.dedent("""
    import json, os, sys, tempfile, time
    sys.path.insert(0, %(root)r)
    from pathlib import Path
    from gi.repository import GLib
    from screentime import storage, keystore as K
    out = {}
    sid = os.urandom(16); key = K.generate_key()
    ss = K.SecretServiceBackend(interactive=False)
    out["empty_lookup"] = ss.load(sid) is None
    ss.store(sid, key)
    out["roundtrip"] = ss.load(sid) == key
    ss.store(sid, key)
    items = ss._call("/org/freedesktop/secrets", "org.freedesktop.Secret.Service", "SearchItems",
                     GLib.Variant("(a{ss})", (ss._attrs(sid),)), "(aoao)").unpack()
    out["no_duplicates"] = len(items[0]) + len(items[1]) == 1
    out["isolated_by_store_id"] = ss.load(os.urandom(16)) is None
    ss.delete(sid)
    out["deleted"] = ss.load(sid) is None

    # full stack: create a protected database whose key lives in the real keyring
    root = Path(tempfile.mkdtemp()); dd = root / "data"; cfg = root / "cfg"
    km = K.KeyManager(cfg)
    db = storage.open_database(data_dir_=dd, key_manager=km)
    db.set_setting("hello", "keyring"); db.close()
    out["backend"] = km.current_backend()
    out["no_keyfile_written"] = not (cfg / "keys").exists()
    db = storage.open_database(data_dir_=dd, key_manager=K.KeyManager(cfg), recover_orphans=False)
    out["reopen_value"] = db.get_setting("hello"); db.close()
    # the key is retrievable by the daemon's non-interactive path
    store_id = storage.read_store_id(dd / "screentime.sec")
    out["noninteractive_get"] = len(K.KeyManager(cfg).get_key(store_id, interactive=False)) == 32

    # migration with the keyring as the key store
    from tests_helpers import make_legacy
    dd2 = root / "data2"; dd2.mkdir(); cfg2 = root / "cfg2"
    make_legacy(dd2)
    db = storage.open_database(data_dir_=dd2, key_manager=K.KeyManager(cfg2), recover_orphans=False)
    out["migrated_sessions"] = db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    out["migrated_backend"] = K.KeyManager(cfg2).current_backend()
    out["plaintext_gone"] = not any(p.name.startswith("screentime.db") for p in dd2.iterdir())
    db.close()

    # a locked keyring must fail fast, retryably, and without prompting
    ss._call("/org/freedesktop/secrets", "org.freedesktop.Secret.Service", "Lock",
             GLib.Variant("(ao)", (["/org/freedesktop/secrets/aliases/default"],)), "(aoo)")
    t = time.time()
    try:
        K.KeyManager(cfg).get_key(store_id, interactive=False); out["locked"] = "unexpectedly opened"
    except K.KeyUnavailableError as e:
        out["locked"] = "retryable" if e.retryable else "not-retryable"
    out["locked_fast"] = time.time() - t < 3
    try:
        storage.open_database(data_dir_=dd, key_manager=K.KeyManager(cfg), wait_for_key=False); out["locked_open"] = "opened"
    except K.KeyUnavailableError:
        out["locked_open"] = "raised"
    out["status_waiting"] = storage.read_status().get("state")
    print("RESULT " + json.dumps(out))
""")

HARNESS = textwrap.dedent("""
    export XDG_RUNTIME_DIR=$(mktemp -d); export HOME=$(mktemp -d)
    export XDG_DATA_HOME=$HOME/.local/share; mkdir -p $XDG_DATA_HOME
    eval "$(echo -n testpw | gnome-keyring-daemon --unlock --components=secrets --daemonize 2>/dev/null)"
    sleep 1
    exec python3 "$1"
""")


def test_secret_service_client_and_full_stack_against_gnome_keyring(tmp_path):
    helpers = tmp_path / "tests_helpers.py"
    helpers.write_text(textwrap.dedent(f"""
        import os, subprocess, sys, textwrap
        from pathlib import Path
        def make_legacy(dd):
            script = textwrap.dedent('''
                import os, sys
                sys.path.insert(0, {str(ROOT)!r})
                from pathlib import Path
                from screentime.db import Database
                db = Database(Path(sys.argv[1]) / "screentime.db")
                a = db.get_or_create_app("brave-browser", "Brave", None, None)
                for j in range(25):
                    sid = db.open_session(a.id, 1790000000 + j * 600)
                    db.close_session(sid, 1790000000 + j * 600 + 400, "focus_change")
                os._exit(0)
            ''')
            subprocess.run([sys.executable, "-c", script, str(dd)], check=True, timeout=60)
    """))
    client = tmp_path / "client.py"
    client.write_text(f"import sys\nsys.path.insert(0, {str(tmp_path)!r})\n" + CLIENT % {"root": str(ROOT)})
    harness = tmp_path / "harness.sh"
    harness.write_text(HARNESS)
    env = {k: v for k, v in os.environ.items() if k not in ("DBUS_SESSION_BUS_ADDRESS", "SCREENTIME_TEST_PROTECTED")}
    r = subprocess.run(["dbus-run-session", "--", "bash", str(harness), str(client)],
                       capture_output=True, text=True, timeout=120, env=env)
    lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
    assert lines, f"no result (rc={r.returncode})\nSTDOUT: {r.stdout[-1500:]}\nSTDERR: {r.stderr[-1500:]}"
    out = json.loads(lines[-1][7:])
    assert out["empty_lookup"] and out["roundtrip"] and out["no_duplicates"] and out["isolated_by_store_id"] and out["deleted"]
    assert out["backend"] == "secret-service" and out["no_keyfile_written"]
    assert out["reopen_value"] == "keyring" and out["noninteractive_get"]
    assert out["migrated_sessions"] == 25 and out["migrated_backend"] == "secret-service" and out["plaintext_gone"]
    assert out["locked"] == "retryable" and out["locked_fast"] and out["locked_open"] == "raised"
    assert out["status_waiting"] == "waiting"
