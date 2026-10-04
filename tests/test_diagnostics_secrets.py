"""The diagnostics report may MENTION that it excludes keys, but must never contain one.
smoke.ps1 greps for these same shapes (not for the word "recovery", which the disclaimer contains)."""
import re

from screentime import diagnostics, keystore

SHAPES = [r"\b[A-Z2-7]{4}(?:-[A-Z2-7]{4}){5,}\b", r"\b[0-9a-fA-F]{32,}\b", r"[A-Za-z0-9+/]{40,}={0,2}",
          r"BEGIN [A-Z ]*(?:KEY|CERTIFICATE)"]


def test_report_contains_no_secret_shapes():
    out = diagnostics.render(diagnostics.collect())
    assert [p for p in SHAPES if re.search(p, out)] == []


def test_the_shapes_do_catch_real_key_material():
    key = keystore.generate_key()
    for secret in (keystore.encode_recovery_key(key), key.hex()):
        assert any(re.search(p, f"x: {secret}") for p in SHAPES)


def test_smoke_script_does_not_grep_for_bare_words():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "packaging" / "windows" / "smoke.ps1").read_text()
    assert "recovery|BEGIN|password" not in text
