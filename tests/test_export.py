"""CSV / JSON analytics export: real session rows, stable fields, correct escaping, no secrets."""
import csv
import io
import json
import os
import time

import pytest

from screentime import exporter as E
from screentime.db import Database

T0 = 1_790_000_000


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    if hasattr(time, "tzset"):
        monkeypatch.setenv("TZ", "UTC")
        time.tzset()
    yield
    if hasattr(time, "tzset"):
        monkeypatch.undo()
        time.tzset()


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def session(db, key, name, start, length, reason="focus_change", close=True):
    app = db.get_or_create_app(key, name, None, None)
    sid = db.open_session(app.id, start)
    db.heartbeat_session(sid, start + length)
    if close:
        db.close_session(sid, start + length, reason)
    return app, sid


def parse_csv(text):
    return list(csv.DictReader(io.StringIO(text, newline="")))


def test_csv_has_real_sessions_with_timestamps_duration_and_identity(db, tmp_path):
    session(db, "firefox", "Firefox", T0, 600)
    session(db, "code", "VS Code", T0 + 1000, 90, "idle")
    out = tmp_path / "e.csv"
    assert E.export_to_file(db, out, "csv") == 2
    raw = out.read_bytes()
    assert raw.endswith(b"\r\n") and not raw.startswith(b"\xef\xbb\xbf")
    rows = parse_csv(raw.decode("utf-8"))
    assert list(rows[0]) == E.SESSION_FIELDS
    first = rows[0]
    assert (first["app_key"], first["app_name"], first["start_epoch"], first["end_epoch"],
            first["duration_seconds"], first["end_reason"], first["in_progress"]) == \
           ("firefox", "Firefox", str(T0), str(T0 + 600), "600", "focus_change", "false")
    assert first["start"].endswith("+00:00") and first["day"] == "2026-09-21"
    assert rows[1]["end_reason"] == "idle"


def test_csv_quotes_commas_quotes_newlines_and_unicode_and_neutralises_formulas(db, tmp_path):
    session(db, "k1", 'Say "hi", ok\nnext', T0, 5)
    session(db, "k2", "=HYPERLINK(\"http://x\")", T0 + 10, 5)
    session(db, "k3", "Türkçe İşlem 日本語 😀", T0 + 20, 5)
    session(db, "k4", "-1+1", T0 + 30, 5)
    out = tmp_path / "e.csv"
    E.export_to_file(db, out, "csv")
    rows = {r["app_key"]: r for r in parse_csv(out.read_text(encoding="utf-8"))}
    assert rows["k1"]["app_name"] == 'Say "hi", ok\nnext'            # round-trips exactly
    assert rows["k2"]["app_name"].startswith("'=")                    # never executable in a spreadsheet
    assert rows["k3"]["app_name"] == "Türkçe İşlem 日本語 😀"
    assert rows["k4"]["app_name"] == "'-1+1"


def test_open_session_is_flagged_and_ends_at_its_last_progress(db, tmp_path):
    session(db, "a", "A", T0, 120, close=False)
    out = tmp_path / "e.json"
    E.export_to_file(db, out, "json")
    s = json.loads(out.read_text(encoding="utf-8"))["sessions"][0]
    assert s["in_progress"] is True and s["end_epoch"] == T0 + 120 and s["duration_seconds"] == 120


def test_json_schema_fields_and_types_are_stable(db, tmp_path):
    session(db, "firefox", "Firefox", T0, 600)
    out = tmp_path / "e.json"
    E.export_to_file(db, out, "json")
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert list(doc)[:5] == ["schema", "schema_version", "app_version", "generated_at", "includes_excluded_apps"]
    assert doc["schema"] == "screentime-export" and doc["schema_version"] == 1
    assert doc["includes_excluded_apps"] is False
    assert list(doc["sessions"][0]) == E.SESSION_FIELDS
    assert doc["apps"] == [{"key": "firefox", "name": "Firefox", "first_seen": doc["apps"][0]["first_seen"],
                            "last_seen": doc["apps"][0]["last_seen"]}]
    assert isinstance(doc["sessions"][0]["duration_seconds"], int)


def test_empty_history_exports_valid_files(db, tmp_path):
    assert E.export_to_file(db, tmp_path / "e.json", "json") == 0
    doc = json.loads((tmp_path / "e.json").read_text())
    assert doc["sessions"] == [] and doc["apps"] == []
    assert E.export_to_file(db, tmp_path / "e.csv", "csv") == 0
    assert (tmp_path / "e.csv").read_bytes() == (",".join(E.SESSION_FIELDS) + "\r\n").encode()


def test_excluded_apps_are_left_out_unless_asked(db, tmp_path):
    app, _ = session(db, "secret", "Secret", T0, 10)
    session(db, "ok", "Ok", T0 + 100, 10)
    db.set_excluded(app.id, True)
    E.export_to_file(db, tmp_path / "a.json", "json")
    assert [s["app_key"] for s in json.loads((tmp_path / "a.json").read_text())["sessions"]] == ["ok"]
    E.export_to_file(db, tmp_path / "b.json", "json", include_excluded=True)
    b = json.loads((tmp_path / "b.json").read_text())
    assert b["includes_excluded_apps"] is True and len(b["sessions"]) == 2


def test_large_history_streams_in_order(db, tmp_path):
    app = db.get_or_create_app("a", "A", None, None)
    for i in range(12000):
        db._conn.execute("INSERT INTO sessions(app_id,start_time,end_time,last_heartbeat,day,end_reason) "
                         "VALUES (?,?,?,?,?,?)", (app.id, T0 + i * 10, T0 + i * 10 + 5, T0 + i * 10 + 5, "2026-09-21", "x"))
    assert E.export_to_file(db, tmp_path / "big.csv", "csv") == 12000
    rows = parse_csv((tmp_path / "big.csv").read_text())
    assert [int(r["start_epoch"]) for r in rows] == sorted(int(r["start_epoch"]) for r in rows)
    assert E.export_to_file(db, tmp_path / "big.json", "json") == 12000
    assert len(json.loads((tmp_path / "big.json").read_text())["sessions"]) == 12000


def test_existing_file_is_not_overwritten_unless_allowed_and_failures_leave_no_partial_file(db, tmp_path, monkeypatch):
    session(db, "a", "A", T0, 5)
    out = tmp_path / "e.csv"
    out.write_text("precious")
    with pytest.raises(FileExistsError):
        E.export_to_file(db, out, "csv")
    assert out.read_text() == "precious"
    E.export_to_file(db, out, "csv", overwrite=True)
    assert out.read_text().startswith("session_id")
    monkeypatch.setattr(E, "iter_sessions", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    target = tmp_path / "fresh.json"
    with pytest.raises(RuntimeError):
        E.export_to_file(db, target, "json")
    assert not target.exists() and not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]


def test_unknown_format_is_rejected(db, tmp_path):
    with pytest.raises(ValueError):
        E.export_to_file(db, tmp_path / "e.xml", "xml")


def test_export_contains_no_key_material_or_settings(db, tmp_path):
    db.set_setting("autostart_enabled", "true")
    session(db, "a", "A", T0, 5)
    E.export_to_file(db, tmp_path / "e.json", "json")
    text = (tmp_path / "e.json").read_text().lower()
    for word in ("recovery", "keyring", "credential", "autostart", "password", "secret"):
        assert word not in text


def test_exporter_never_imports_key_or_storage_modules():
    import ast, pathlib
    tree = ast.parse(pathlib.Path(E.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any("keystore" in m or "secure_log" in m or "storage" in m for m in mods)
    assert E.default_filename("csv").startswith("ScreenTime-export-") and E.default_filename("JSON").endswith(".json")
