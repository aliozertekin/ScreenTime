#!/usr/bin/env python3
"""Fetch the Windows (MSYS2 ucrt64) runtime WITHOUT running any Windows code.

MSYS2 packages are plain zstd-compressed tarballs whose files live under
`<env>/` (ucrt64). Instead of installing MSYS2 and running pacman (which is fragile
under Wine), this tool

  1. reads the repository database (`<env>.db`) and resolves the dependency
     closure of the root packages in packages.txt              [--update-lock]
  2. writes msys2.lock: exact file names + SHA-256 for every package
  3. downloads those files (cached), verifies every SHA-256, and unpacks them
     into one staging tree.

With --require-lock (what every normal build and CI run passes) a missing lock is an ERROR, never a silent
re-resolve; with --lock-only the lock is (re)written and nothing is downloaded (used by the
"update-msys2-lock" workflow, which is the one sanctioned way to change the lock).

With a lock file present, step 1 is skipped entirely and the build is
reproducible: the same bytes every time, or a clear failure if the mirror no
longer has a locked file (run with --update-lock to refresh).

Trust model: the lock is created over HTTPS from the official MSYS2 mirror and
then pinned by hash; review `git diff packaging/windows/msys2.lock` when it
changes. Package signatures (.sig) are not checked.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

UA = {"User-Agent": "screentime-windows-build/1"}
MAGIC = {b"\x28\xb5\x2f\xfd": "zstd", b"\x1f\x8b": "gzip", b"\xfd7zXZ": "xz"}


class FetchError(RuntimeError):
    pass


# ----------------------------------------------------------------- download
def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:        # noqa: S310 - https URLs from toolchain.env
        return r.read()


def download(urls: Iterable[str], dest: Path, sha256: Optional[str] = None, retries: int = 3) -> Path:
    """Download the first working URL to `dest` (verified if `sha256` is given)."""
    if dest.exists() and (sha256 is None or sha256_file(dest) == sha256):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Optional[Exception] = None
    for url in urls:
        for attempt in range(retries):
            try:
                data = http_get(url)
                if sha256 and hashlib.sha256(data).hexdigest() != sha256:
                    raise FetchError(f"SHA-256 mismatch for {url}")
                tmp = dest.with_suffix(dest.suffix + ".part")
                tmp.write_bytes(data)
                tmp.replace(dest)
                return dest
            except FetchError as e:
                last = e
                break                                              # a bad hash will not fix itself; try the next mirror
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
    raise FetchError(f"could not download {dest.name}: {last}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------- repo database
def _open_tar_bytes(data: bytes) -> tarfile.TarFile:
    kind = next((v for k, v in MAGIC.items() if data.startswith(k)), None)
    if kind == "zstd":
        out = subprocess.run(["zstd", "-dc"], input=data, capture_output=True, check=True).stdout
        return tarfile.open(fileobj=io.BytesIO(out))
    return tarfile.open(fileobj=io.BytesIO(data))


def parse_db(data: bytes) -> dict[str, dict]:
    """pacman database -> {name: {version, filename, sha256, depends, provides}}."""
    pkgs: dict[str, dict] = {}
    with _open_tar_bytes(data) as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith("/desc"):
                continue
            sections: dict[str, list[str]] = {}
            cur = None
            for line in tf.extractfile(m).read().decode("utf-8", "replace").splitlines():
                if re.fullmatch(r"%[A-Z0-9]+%", line):
                    cur = line.strip("%")
                    sections[cur] = []
                elif line and cur:
                    sections[cur].append(line)
            try:
                name = sections["NAME"][0]
                pkgs[name] = {
                    "version": sections["VERSION"][0],
                    "filename": sections["FILENAME"][0],
                    "sha256": sections["SHA256SUM"][0],
                    "depends": sections.get("DEPENDS", []),
                    "provides": sections.get("PROVIDES", []),
                }
            except (KeyError, IndexError):
                continue                                           # malformed entry: not resolvable, ignore
    return pkgs


def _bare(dep: str) -> str:
    return re.split(r"[<>=]", dep, maxsplit=1)[0].strip()


def resolve(pkgs: dict[str, dict], roots: list[str], ignore: frozenset = frozenset()) -> list[str]:
    """Dependency closure (package names), sorted for a stable lock."""
    providers: dict[str, str] = {}
    for name, p in sorted(pkgs.items()):
        for prov in p["provides"]:
            providers.setdefault(_bare(prov), name)
    todo, seen, missing = list(roots), set(), []
    while todo:
        name = todo.pop()
        name = name if name in pkgs else providers.get(name, name)
        if name in seen or name in ignore:
            continue
        if name not in pkgs:
            missing.append(name)
            continue
        seen.add(name)
        todo.extend(_bare(d) for d in pkgs[name]["depends"])
    if missing:
        raise FetchError("unresolvable dependencies: " + ", ".join(sorted(set(missing))))
    return sorted(seen)


# --------------------------------------------------------------------- lock
def write_lock(path: Path, entries: list[tuple[str, str, str]], source: str) -> None:
    lines = ["# MSYS2 runtime lock -- generated by msys2_fetch.py --update-lock", f"# source: {source}",
             "# <filename> <sha256> <package>"]
    lines += [f"{f} {h} {n}" for f, h, n in sorted(entries, key=lambda e: e[2])]
    path.write_text("\n".join(lines) + "\n")


def read_lock(path: Path) -> list[tuple[str, str, str]]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}", parts[1]):
            raise FetchError(f"malformed lock line: {line!r}")
        out.append((parts[0], parts[1], parts[2]))
    if not out:
        raise FetchError(f"{path} is empty")
    return out


def read_roots(path: Path, prefix: str) -> list[str]:
    return [prefix + ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


# ------------------------------------------------------------------ pipeline
def make_lock(repo_urls: list[str], roots: list[str], lock: Path, cache: Path, env: str) -> None:
    db = None
    for base in repo_urls:
        try:
            db = http_get(base.rstrip("/") + f"/{env}.db")
            source = base
            break
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    if db is None:
        raise FetchError("could not download the MSYS2 package database from: " + ", ".join(repo_urls))
    (cache / f"{env}.db").write_bytes(db)
    pkgs = parse_db(db)
    names = resolve(pkgs, roots)
    write_lock(lock, [(pkgs[n]["filename"], pkgs[n]["sha256"], n) for n in names], source)
    print(f"locked {len(names)} packages -> {lock}")


def fetch_all(entries: list[tuple[str, str, str]], repo_urls: list[str], cache: Path, say=print) -> list[Path]:
    paths = []
    for i, (filename, sha, name) in enumerate(entries, 1):
        dest = cache / filename
        if not (dest.exists() and sha256_file(dest) == sha):
            say(f"  [{i}/{len(entries)}] downloading {filename}")
            download([f"{b.rstrip('/')}/{filename}" for b in repo_urls], dest, sha)
        paths.append(dest)
    return paths


def extract_all(files: list[Path], stage: Path, stamp: str) -> None:
    marker = stage / ".stage-complete"
    if marker.exists() and marker.read_text() == stamp:
        return
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for f in files:
        zst = subprocess.Popen(["zstd", "-dc", str(f)], stdout=subprocess.PIPE)
        tar = subprocess.run(["tar", "-x", "-C", str(stage), "--no-same-owner", "--no-same-permissions",
                              "--exclude=.PKGINFO", "--exclude=.BUILDINFO", "--exclude=.MTREE",
                              "--exclude=.INSTALL", "--exclude=.CHANGELOG"], stdin=zst.stdout)
        zst.stdout.close()
        if zst.wait() != 0 or tar.returncode != 0:
            raise FetchError(f"could not unpack {f.name}")
    marker.write_text(stamp)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lock", type=Path, required=True)
    ap.add_argument("--packages", type=Path, required=True)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--stage", type=Path, required=True)
    ap.add_argument("--repo-urls", required=True, help="space-separated mirror base URLs")
    ap.add_argument("--prefix", default="mingw-w64-ucrt-x86_64-")
    ap.add_argument("--env", default="ucrt64", help="MSYS2 environment / repository name")
    ap.add_argument("--update-lock", action="store_true")
    ap.add_argument("--require-lock", action="store_true",
                    help="fail instead of resolving fresh packages when the lock file is missing")
    ap.add_argument("--lock-only", action="store_true", help="write the lock, download nothing (implies --update-lock)")
    a = ap.parse_args(argv)
    if a.lock_only:
        a.update_lock = True
    if a.require_lock and a.update_lock and not a.lock_only:
        pass                                             # explicit refresh wins over "require"
    urls = a.repo_urls.split()
    a.cache.mkdir(parents=True, exist_ok=True)
    try:
        if a.require_lock and not a.update_lock and not a.lock.exists():
            print(f"ERROR: {a.lock.name} is missing, and this build is not allowed to resolve a fresh package set "
                  f"(that would make it irreproducible). Generate it once with the 'update-msys2-lock' workflow "
                  f"or ./scripts/build-windows.sh --update-lock, review it, and commit it.", file=sys.stderr)
            return 1
        if a.update_lock or not a.lock.exists():
            if not a.update_lock:
                print(f"NOTE: {a.lock.name} not found; resolving the current MSYS2 packages and creating it. "
                      f"Commit it so later builds are reproducible.")
            make_lock(urls, read_roots(a.packages, a.prefix), a.lock, a.cache, a.env)
            if a.lock_only:
                return 0
        entries = read_lock(a.lock)
        files = fetch_all(entries, urls, a.cache)
        extract_all(files, a.stage, sha256_file(a.lock))
    except (FetchError, subprocess.CalledProcessError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"MSYS2 runtime ready: {len(entries)} packages in {a.stage}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
