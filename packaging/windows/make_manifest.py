#!/usr/bin/env python3
"""dist/build-manifest.txt (machine-readable key=value) + dist/SHA256SUMS."""
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def build(dist: Path, version: str, commit: str, extra: dict[str, str]) -> str:
    installer, portable = dist / f"ScreenTime-{version}-setup.exe", dist / f"ScreenTime-{version}-portable.zip"
    for f in (installer, portable):
        if not f.exists() or f.stat().st_size == 0:
            raise SystemExit(f"manifest: missing or empty artifact {f}")
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    rows = {
        "version": version, "commit": commit,
        "build_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_date": datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if epoch else "",
        "installer": installer.name, "installer_size": str(installer.stat().st_size), "installer_sha256": sha256(installer),
        "portable": portable.name, "portable_size": str(portable.stat().st_size), "portable_sha256": sha256(portable),
        **extra,
    }
    (dist / "SHA256SUMS").write_text(f"{rows['installer_sha256']}  {installer.name}\n{rows['portable_sha256']}  {portable.name}\n")
    return "".join(f"{k}={v}\n" for k, v in rows.items())


if __name__ == "__main__":
    dist, version, commit = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    extra = dict(a.split("=", 1) for a in sys.argv[4:])
    (dist / "build-manifest.txt").write_text(build(dist, version, commit, extra))
    print((dist / "build-manifest.txt").read_text())
