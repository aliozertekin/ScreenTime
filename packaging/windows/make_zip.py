#!/usr/bin/env python3
"""Deterministic portable zip: sorted entries, fixed timestamps (SOURCE_DATE_EPOCH), top folder ScreenTime/."""
import os
import sys
import time
import zipfile
from pathlib import Path


def make_zip(bundle: Path, dest: Path, epoch: int, top: str = "ScreenTime") -> int:
    ts = time.gmtime(max(epoch, 315532800))[:6]                      # zip cannot represent < 1980
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(bundle.rglob("*")):
            rel = f"{top}/{p.relative_to(bundle).as_posix()}"
            if p.is_dir():
                zi = zipfile.ZipInfo(rel + "/", ts)
                zi.external_attr = (0o40755 << 16) | 0x10
                z.writestr(zi, b"")
            else:
                zi = zipfile.ZipInfo(rel, ts)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o100644 << 16
                z.writestr(zi, p.read_bytes(), compresslevel=9)
                n += 1
    return n


if __name__ == "__main__":
    bundle, dest = Path(sys.argv[1]), Path(sys.argv[2])
    print(f"zipped {make_zip(bundle, dest, int(os.environ.get('SOURCE_DATE_EPOCH', '0')))} files -> {dest}")
