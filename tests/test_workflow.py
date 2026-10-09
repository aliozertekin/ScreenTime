"""CI/release architecture: PR/push -> tests + build validation; version tag -> tests, build, REAL Windows smoke
test, checksums, GitHub Release. Nothing publishes unless everything before it passed."""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
WF_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name):
    wf = yaml.safe_load((WF_DIR / name).read_text())
    wf["_on"] = wf.get("on", wf.get(True))            # PyYAML parses a bare `on:` key as boolean True
    return wf


CI, REL, WIN, TESTS, LOCK = (load(n) for n in ("ci.yml", "release.yml", "windows-build.yml", "tests.yml",
                                               "update-msys2-lock.yml"))


def steps_text(job):
    return "\n".join(str(s.get("run", "")) for s in job["steps"])


def test_the_old_combined_workflow_is_gone():
    assert not (WF_DIR / "windows.yml").exists()


def test_ci_runs_on_pull_requests_and_main_but_never_on_tags_and_cannot_publish():
    on = CI["_on"]
    assert "pull_request" in on and on["push"]["branches"] == ["main"] and "tags" not in on["push"]
    assert CI["permissions"] == {"contents": "read"}
    assert "contents: write" not in (WF_DIR / "ci.yml").read_text()
    assert CI["jobs"]["tests"]["uses"].endswith("tests.yml")
    assert CI["jobs"]["windows"]["uses"].endswith("windows-build.yml") and CI["jobs"]["windows"]["needs"] == "tests"


def test_release_triggers_only_on_strict_version_tags():
    on = REL["_on"]
    assert on == {"push": {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}}


def test_release_chain_tests_then_windows_build_and_smoke_then_publish():
    jobs = REL["jobs"]
    assert jobs["tests"]["needs"] == "verify-tag"
    assert jobs["tests"]["uses"].endswith("tests.yml") and jobs["windows"]["uses"].endswith("windows-build.yml")
    assert set(jobs["windows"]["needs"]) == {"verify-tag", "tests"}
    assert set(jobs["publish"]["needs"]) == {"verify-tag", "tests", "windows"}     # every gate, no `if: always()`
    assert "if" not in jobs["publish"] and "continue-on-error" not in jobs["publish"]


def test_only_the_publish_job_can_write_and_the_tag_is_validated_first():
    assert REL["permissions"] == {"contents": "read"}
    writers = [n for n, j in REL["jobs"].items() if j.get("permissions", {}).get("contents") == "write"]
    assert writers == ["publish"]
    v = steps_text(REL["jobs"]["verify-tag"])
    assert "check-release-tag.sh" in v and "merge-base --is-ancestor" in v


def test_release_does_not_rebuild_or_hand_upload_it_publishes_the_built_artifacts():
    pub = REL["jobs"]["publish"]
    dl = next(s for s in pub["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact"))
    assert dl["with"]["pattern"] == "screentime-windows-*" and dl["with"]["merge-multiple"] is True
    assert "publish-release.sh" in steps_text(pub) and "build-windows" not in steps_text(pub)


def test_release_runs_are_serialised_per_tag_and_never_cancelled_midway():
    assert REL["concurrency"] == {"group": "release-${{ github.ref_name }}", "cancel-in-progress": False}


def test_windows_build_is_reusable_with_the_real_windows_smoke_test_and_the_lock_gate():
    assert "workflow_call" in WIN["_on"]
    assert WIN["jobs"]["smoke-on-windows"]["runs-on"].startswith("windows-")          # real Windows, not Wine-only
    assert WIN["jobs"]["smoke-on-windows"]["needs"] == "build"
    smoke = "\n".join(str(s.get("run", "")) for s in WIN["jobs"]["smoke-on-windows"]["steps"])
    assert "smoke.ps1 -Mode Installer" in smoke
    assert "msys2.lock" in steps_text(WIN["jobs"]["build"]) and "--non-interactive" in steps_text(WIN["jobs"]["build"])
    assert "--skip-smoke" not in (WF_DIR / "windows-build.yml").read_text()
    assert "continue-on-error" not in {k for j in WIN["jobs"].values() for k in j}


def test_build_uploads_installer_and_portable_as_separate_uncompressed_artifacts():
    ups = {u["with"]["name"]: u["with"] for u in WIN["jobs"]["build"]["steps"]
           if str(u.get("uses", "")).startswith("actions/upload-artifact")}
    assert set(ups) == {"screentime-windows-installer", "screentime-windows-portable"}
    assert "setup.exe" in ups["screentime-windows-installer"]["path"]
    assert "portable.zip" in ups["screentime-windows-portable"]["path"] and "SHA256SUMS" in ups["screentime-windows-portable"]["path"]
    assert all(u["compression-level"] == 0 and u["if-no-files-found"] == "error" for u in ups.values())


def test_windows_smoke_downloads_only_the_installer_and_retries_a_stalled_download():
    steps = WIN["jobs"]["smoke-on-windows"]["steps"]
    dls = [x for x in steps if str(x.get("uses", "")).startswith("actions/download-artifact")]
    assert len(dls) == 2 and all(d["with"]["name"] == "screentime-windows-installer" for d in dls)
    assert all(d["timeout-minutes"] <= 5 for d in dls) and dls[0]["continue-on-error"] is True
    assert "dl1.outcome" in dls[1]["if"]


def test_every_job_has_a_timeout_so_a_stall_cannot_burn_six_hours():
    for wf in (CI, REL, WIN, TESTS, LOCK):
        for name, job in wf["jobs"].items():
            assert "timeout-minutes" in job or "uses" in job, name        # `uses:` jobs get theirs from the callee


def test_tests_workflow_runs_gtk_tests_and_the_encrypted_backend_pass_with_yaml_installed():
    text = (WF_DIR / "tests.yml").read_text()
    assert "xvfb-run" in text and "SCREENTIME_TEST_PROTECTED=1" in text
    assert "python3-yaml" in text and "gir1.2-adw-1" in text                  # so these very tests are not skipped
    assert "-x" not in steps_text(TESTS["jobs"]["pytest"]).replace("-xvfb", "")


def test_lock_update_workflow_is_manual_and_pushes_to_main_only_when_asked():
    assert set(LOCK["_on"]) == {"workflow_dispatch"}
    inp = LOCK["_on"]["workflow_dispatch"]["inputs"]["commit_to_main"]
    assert inp["type"] == "boolean" and inp["default"] is False                 # review branch unless explicitly requested
    t = (WF_DIR / "update-msys2-lock.yml").read_text()
    assert "chore/update-msys2-lock" in t and 'if [ "$TO_MAIN" = "true" ]' in t
    assert t.count("git push") == 2 and "--force origin HEAD:refs/heads/chore/" in t     # force only ever touches the review branch
