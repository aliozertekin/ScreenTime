"""`screentime-security`: status, verify, recovery, and failure exits."""
import sqlite3

import pytest

from screentime import keystore as K
from screentime import security_cli, storage


@pytest.fixture
def protected():
    """A protected database in the isolated default locations (key in a key file,
    since the test session bus points at nothing)."""
    db = storage.open_database()
    db.set_setting("precious", "data")
    db.close()
    return storage.store_path()


def run(capsys, *argv):
    rc = security_cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_status_before_and_after_creation(capsys):
    rc, out, _ = run(capsys, "status")
    assert rc == 0 and "NO (not created yet)" in out
    storage.open_database().close()
    rc, out, _ = run(capsys, "status")
    assert rc == 0 and "yes (AES-256-GCM)" in out and "key file" in out and "less secure" in out


def test_status_warns_about_a_plaintext_copy(capsys, protected):
    (protected.parent / "screentime.db").write_bytes(b"x")
    rc, out, _ = run(capsys, "status")
    assert "WARNING" in out and "plaintext" in out


def test_verify_ok_then_fails_after_tampering(capsys, protected):
    rc, out, _ = run(capsys, "verify")
    assert rc == 0 and out.startswith("OK")
    con = sqlite3.connect(protected)
    con.execute("UPDATE events SET ct = CAST(X'00' || substr(ct, 2) AS BLOB) WHERE id = 1")
    con.commit()
    con.close()
    rc, _out, err = run(capsys, "verify")
    assert rc == 1 and "FAILED" in err


def test_export_prints_a_valid_recovery_key(capsys, protected):
    rc, out, _ = run(capsys, "export-recovery-key")
    assert rc == 0
    key_line = [l.strip() for l in out.splitlines() if "-" in l and l.strip()[:4].isalnum()][-1]
    assert len(K.decode_recovery_key(key_line)) == 32


def test_lost_key_recovery_roundtrip_via_cli(capsys, protected):
    _rc, out, _ = run(capsys, "export-recovery-key")
    recovery = [l.strip() for l in out.splitlines() if l.strip().count("-") >= 8][-1]
    for f in (K.default_config_dir() / "keys").iterdir():
        f.unlink()
    rc, _o, err = run(capsys, "verify")
    assert rc == 1 and "recovery key" in err                          # explained, not a traceback
    rc, out, _ = run(capsys, "import-recovery-key", recovery)
    assert rc == 0 and "restored" in out.lower()
    rc, out, _ = run(capsys, "verify")
    assert rc == 0
    db = storage.open_database(recover_orphans=False)
    assert db.get_setting("precious") == "data"
    db.close()


def test_import_rejects_wrong_and_mistyped_keys(capsys, protected):
    rc, _o, err = run(capsys, "import-recovery-key", K.encode_recovery_key(K.generate_key()))
    assert rc == 1 and "does not belong" in err
    rc, _o, err = run(capsys, "import-recovery-key", "AAAA-BBBB")
    assert rc == 1 and err


def test_import_prompts_when_no_argument_is_given(capsys, protected, monkeypatch):
    good = storage.export_recovery_key(interactive=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": good)
    rc, out, _ = run(capsys, "import-recovery-key")
    assert rc == 0


def test_move_to_keyring_without_a_keyring_fails_safely_and_keeps_the_key(capsys, protected):
    rc, _o, err = run(capsys, "move-key-to-keyring")
    assert rc == 1 and "Not moved" in err
    assert list((K.default_config_dir() / "keys").iterdir())            # key file still there
    assert security_cli.main(["verify"]) == 0


def test_compact_command(capsys, protected):
    rc, out, _ = run(capsys, "compact")
    assert rc == 0 and out.strip() == "compacted"


def test_unknown_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        security_cli.main(["frobnicate"])
    assert e.value.code == 2


def test_cli_without_a_database_gives_a_clear_message(capsys):
    rc, _o, err = run(capsys, "verify")
    assert rc == 1 and err
