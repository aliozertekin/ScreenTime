"""scripts/ci/*.sh: tag validation, SHA256SUMS, and an idempotent release (tested against a fake `gh`)."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import screentime

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / "scripts" / "ci"
V = screentime.__version__
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash needed")


def run(cmd, env=None, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)


# ---------------------------------------------------------------- check-release-tag
def test_tag_check_accepts_the_current_version_and_prints_it():
    r = run([str(CI / "check-release-tag.sh"), f"v{V}"])
    assert r.returncode == 0 and r.stdout.strip() == V, r.stderr


@pytest.mark.parametrize("tag", ["1.5.0", "v1.5", "v1.5.0-rc1", "v1.5.0.1", "latest", "v01.x.0"])
def test_tag_check_rejects_malformed_tags(tag):
    r = run([str(CI / "check-release-tag.sh"), tag])
    assert r.returncode != 0 and "not vMAJOR.MINOR.PATCH" in r.stderr


def test_tag_check_rejects_a_tag_that_differs_from_the_packaged_version():
    major, minor, patch = map(int, V.split("."))
    r = run([str(CI / "check-release-tag.sh"), f"v{major}.{minor}.{patch + 1}"])
    assert r.returncode != 0 and "pyproject.toml says" in r.stderr


def test_tag_check_requires_a_changelog_section(tmp_path):
    repo = tmp_path / "r"
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "dist"))
    (repo / "CHANGELOG.md").write_text("# Changelog\n")
    r = run([str(repo / "scripts" / "ci" / "check-release-tag.sh"), f"v{V}"])
    assert r.returncode != 0 and "CHANGELOG.md has no" in r.stderr


# ----------------------------------------------------------------- publish-release
FAKE_GH = r'''#!/usr/bin/env bash
# minimal stand-in for the GitHub CLI: state lives in $FAKE_GH_DIR
d="$FAKE_GH_DIR"; echo "$*" >> "$d/calls"
case "$1 $2" in
  "release view")
    [[ -f "$d/exists" ]] || exit 1
    if [[ "$*" == *"--json assets"* ]]; then cat "$d/assets" 2>/dev/null; exit 0; fi
    if [[ "$*" == *"--json isDraft"* ]]; then cat "$d/draft"; exit 0; fi ;;
  "release create")
    [[ -f "$d/exists" ]] && { echo "already_exists" >&2; exit 1; }
    touch "$d/exists"; echo true > "$d/draft"
    shift 2; shift                       # tag
    notes="$(printf '%s\n' "$@" | grep -A1 -x -- --notes-file | tail -1)"
    for a in "$@"; do [[ -f "$a" && "$a" != "$notes" ]] && basename "$a" >> "$d/assets"; done
    cp "$notes" "$d/notes.md" ;;
  "release upload")
    shift 2; shift; for a in "$@"; do [[ -f "$a" ]] && basename "$a" >> "$d/assets"; done ;;
  "release edit") echo false > "$d/draft" ;;
esac
'''


@pytest.fixture
def env(tmp_path):
    bindir, state, dist = tmp_path / "bin", tmp_path / "state", tmp_path / "dist"
    for d in (bindir, state, dist):
        d.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (dist / f"ScreenTime-{V}-setup.exe").write_bytes(b"MZ" + b"installer" * 100)
    (dist / f"ScreenTime-{V}-portable.zip").write_bytes(b"PK" + b"zip" * 100)
    (dist / "build-manifest.txt").write_text(f"version={V}\ncommit=abc\n")
    e = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(state), "GH_TOKEN": "x"}
    return e, state, dist


def publish(env_, dist):
    e, _s, _d = env_
    return run([str(CI / "publish-release.sh"), f"v{V}", str(dist)], env=e)


def test_publish_creates_a_draft_with_every_asset_then_publishes_it_once(env):
    e, state, dist = env
    r = publish(env, dist)
    assert r.returncode == 0, r.stdout + r.stderr
    assets = (state / "assets").read_text().split()
    assert sorted(assets) == sorted([f"ScreenTime-{V}-setup.exe", f"ScreenTime-{V}-portable.zip", "SHA256SUMS", "build-manifest.txt"])
    calls = (state / "calls").read_text()
    assert "--draft" in calls and "--verify-tag" in calls and "release edit" in calls and calls.count("release create") == 1
    assert (state / "draft").read_text().strip() == "false"                      # published at the very end


def test_sha256sums_is_exact_deterministic_and_verifies(env):
    e, _state, dist = env
    assert publish(env, dist).returncode == 0
    text = (dist / "SHA256SUMS").read_text()
    lines = text.splitlines()
    assert lines == sorted(lines, key=lambda l: l.split("  ", 1)[1]) and text.endswith("\n") and "\r" not in text
    for line in lines:
        digest, name = line.split("  ", 1)                                       # exactly two spaces: `sha256sum -c` format
        assert digest == hashlib.sha256((dist / name).read_bytes()).hexdigest()
    assert {l.split("  ", 1)[1] for l in lines} == {f"ScreenTime-{V}-setup.exe", f"ScreenTime-{V}-portable.zip", "build-manifest.txt"}
    assert run(["sha256sum", "-c", "SHA256SUMS"], cwd=dist).returncode == 0


def test_release_notes_come_from_the_changelog_and_include_the_checksums(env):
    e, state, dist = env
    assert publish(env, dist).returncode == 0
    notes = (state / "notes.md").read_text()
    assert f"## {V} " in notes and "Verify your download" in notes and f"ScreenTime-{V}-setup.exe" in notes


def test_retry_after_success_creates_no_second_release_and_uploads_nothing(env):
    e, state, dist = env
    assert publish(env, dist).returncode == 0
    assert publish(env, dist).returncode == 0
    calls = (state / "calls").read_text()
    assert calls.count("release create") == 1 and "release upload" not in calls


def test_retry_after_a_partial_failure_uploads_only_the_missing_assets_and_publishes(env):
    e, state, dist = env
    (state / "exists").touch()
    (state / "draft").write_text("true\n")
    (state / "assets").write_text(f"ScreenTime-{V}-setup.exe\nbuild-manifest.txt\n")
    assert publish(env, dist).returncode == 0
    calls = (state / "calls").read_text()
    assert "release create" not in calls and calls.count("release upload") == 1
    upload = next(l for l in calls.splitlines() if l.startswith("release upload"))
    assert "portable.zip" in upload and "SHA256SUMS" in upload and "setup.exe" not in upload     # never replaces assets
    assert "--clobber" not in calls and (state / "draft").read_text().strip() == "false"


@pytest.mark.parametrize("breakage,msg", [("missing_setup", "missing or empty"), ("not_exe", "not a Windows executable"),
                                           ("manifest_version", "not for version"), ("changed", "changed between the build")])
def test_publish_refuses_bad_artifacts_before_touching_github(env, breakage, msg):
    e, state, dist = env
    setup = dist / f"ScreenTime-{V}-setup.exe"
    if breakage == "missing_setup":
        setup.unlink()
    elif breakage == "not_exe":
        setup.write_bytes(b"<html>404</html>")
    elif breakage == "manifest_version":
        (dist / "build-manifest.txt").write_text("version=0.0.1\n")
    else:                                         # the build recorded one hash, the file being shipped differs
        (dist / "SHA256SUMS").write_text(f"{'0' * 64}  {setup.name}\n")
    r = publish(env, dist)
    assert r.returncode != 0 and msg in r.stderr
    assert not (state / "calls").exists()                                       # gh was never called


def test_a_build_sha256sums_that_matches_is_accepted(env):
    e, _state, dist = env
    setup, zipf = dist / f"ScreenTime-{V}-setup.exe", dist / f"ScreenTime-{V}-portable.zip"
    (dist / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in (setup, zipf)))
    assert publish(env, dist).returncode == 0


# ------------------------------------------------------------ update-msys2-lock.sh
def test_lock_update_script_uses_only_the_pinned_environment():
    t = (CI / "update-msys2-lock.sh").read_text()
    assert "--lock-only" in t and "toolchain.env" in t and "MSYS2_REPO_URLS" in t
