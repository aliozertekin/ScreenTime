"""Plaintext usage-data export (CSV and JSON).

These files are NOT encrypted and NOT a backup: they are for opening in a spreadsheet or feeding to other
tools. They contain what you did and when (application names and times) and nothing else -- never the
encryption key, the recovery key, keyring/credential-manager data, or settings. Use
`screentime.backup` for an encrypted copy that can restore your history.

Rows are the real tracked sessions (one per focus interval), not dashboard summaries. Applications you
excluded are left out unless `include_excluded=True`.

JSON layout (stable field names; `schema_version` changes only when a field is removed or re-typed):

    {"schema": "screentime-export", "schema_version": 1, "app_version": "...", "generated_at": "<ISO 8601>",
     "includes_excluded_apps": false,
     "apps": [{"key", "name", "first_seen", "last_seen"}...],
     "sessions": [{"session_id", "app_key", "app_name", "start", "start_epoch", "end", "end_epoch",
                   "duration_seconds", "day", "end_reason", "in_progress"}...]}

Times: `*_epoch` are Unix seconds (UTC); `start`/`end` are ISO 8601 in the local zone with its offset;
`day` is the local calendar day the session belongs to. A session still open when the export was made
has `in_progress: true` and ends at its last recorded progress point.

CSV: RFC 4180, UTF-8, CRLF line ends, one header row with the same names as the JSON session fields. Text
cells that start with = + - @ (or a tab/CR) are prefixed with an apostrophe so a spreadsheet never runs
them as a formula.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Iterator, Optional

from . import __version__
from .db import Database

SCHEMA = "screentime-export"
SCHEMA_VERSION = 1
SESSION_FIELDS = ["session_id", "app_key", "app_name", "start", "start_epoch", "end", "end_epoch",
                  "duration_seconds", "day", "end_reason", "in_progress"]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_CHUNK = 5000


def _iso(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def iter_sessions(db: Database, include_excluded: bool = False) -> Iterator[dict]:
    """Every tracked session, oldest first, fetched in chunks so a large history never sits in memory."""
    q = ("SELECT s.id AS id, a.key AS app_key, a.display_name AS app_name, s.start_time AS start_time, "
         "s.end_time AS end_time, s.last_heartbeat AS last_heartbeat, s.day AS day, s.end_reason AS end_reason "
         "FROM sessions s JOIN apps a ON a.id = s.app_id "
         + ("" if include_excluded else "WHERE a.excluded = 0 ") + "ORDER BY s.start_time, s.id")
    cur = db.conn.execute(q)
    while True:
        rows = cur.fetchmany(_CHUNK)
        if not rows:
            return
        for r in rows:
            start = int(r["start_time"])
            open_ = r["end_time"] is None
            end = max(start, int(r["end_time"] if not open_ else (r["last_heartbeat"] or start)))
            yield {"session_id": r["id"], "app_key": r["app_key"], "app_name": r["app_name"],
                   "start": _iso(start), "start_epoch": start, "end": _iso(end), "end_epoch": end,
                   "duration_seconds": end - start, "day": r["day"], "end_reason": r["end_reason"] or "",
                   "in_progress": open_}


def iter_apps(db: Database, include_excluded: bool = False) -> Iterator[dict]:
    q = "SELECT key, display_name, first_seen, last_seen FROM apps " + \
        ("" if include_excluded else "WHERE excluded = 0 ") + "ORDER BY key"
    for r in db.conn.execute(q):
        yield {"key": r["key"], "name": r["display_name"], "first_seen": int(r["first_seen"]),
               "last_seen": int(r["last_seen"])}


def _csv_cell(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str) and v.startswith(_FORMULA_PREFIXES):
        return "'" + v
    return v


def write_csv(db: Database, fh, include_excluded: bool = False) -> int:
    w = csv.writer(fh, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(SESSION_FIELDS)
    n = 0
    for s in iter_sessions(db, include_excluded):
        w.writerow([_csv_cell(s[f]) for f in SESSION_FIELDS])
        n += 1
    return n


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ": "))


def write_json(db: Database, fh, include_excluded: bool = False) -> int:
    """Streams the document: header, then each app and session on its own line."""
    head = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "app_version": __version__,
            "generated_at": _iso(int(datetime.datetime.now().timestamp())),
            "includes_excluded_apps": bool(include_excluded)}
    fh.write("{" + ",\n ".join(f"{_dumps(k)}: {_dumps(v)}" for k, v in head.items()) + ",\n")
    fh.write(' "apps": [')
    fh.write(",".join("\n  " + _dumps(a) for a in iter_apps(db, include_excluded)))
    fh.write('\n ],\n "sessions": [')
    n = 0
    for s in iter_sessions(db, include_excluded):
        fh.write(("," if n else "") + "\n  " + _dumps(s))
        n += 1
    fh.write("\n ]\n}\n")
    return n


def export_to_file(db: Database, dest, fmt: str, include_excluded: bool = False, overwrite: bool = False) -> int:
    """Write `fmt` ("csv" | "json") to `dest` atomically (a failed export never leaves a half-written file
    and never replaces an existing file unless `overwrite`). Returns the number of sessions written."""
    fmt = fmt.lower()
    if fmt not in ("csv", "json"):
        raise ValueError(f"unknown export format {fmt!r} (use csv or json)")
    dest = Path(dest)
    if dest.exists() and not overwrite:
        raise FileExistsError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            n = (write_csv if fmt == "csv" else write_json)(db, fh, include_excluded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return n


def default_filename(fmt: str, today: Optional[datetime.date] = None) -> str:
    return f"ScreenTime-export-{(today or datetime.date.today()).isoformat()}.{fmt.lower()}"
