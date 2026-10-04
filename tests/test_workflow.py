"""The CI workflow must keep producing downloadable Windows files (artifacts on every push, draft release on tags)."""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
WF = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github" / "workflows" / "windows.yml").read_text())
ON = WF.get("on", WF.get(True))


def test_branch_pushes_and_tags_both_trigger_it():
    assert "main" in ON["push"]["branches"] and "v*" in ON["push"]["tags"]      # tags without branches would silence pushes


def _uploads():
    return [x for x in WF["jobs"]["build"]["steps"] if str(x.get("uses", "")).startswith("actions/upload-artifact")]


def test_build_uploads_installer_and_portable_as_separate_uncompressed_artifacts():
    ups = {u["with"]["name"]: u["with"] for u in _uploads()}
    assert set(ups) == {"screentime-windows-installer", "screentime-windows-portable"}
    assert "setup.exe" in ups["screentime-windows-installer"]["path"]
    assert "portable.zip" in ups["screentime-windows-portable"]["path"] and "SHA256SUMS" in ups["screentime-windows-portable"]["path"]
    assert all(u["compression-level"] == 0 and u["if-no-files-found"] == "error" for u in ups.values())   # .exe/.zip are already compressed


def test_every_job_has_a_timeout_so_a_stall_cannot_burn_six_hours():
    assert all("timeout-minutes" in j for j in WF["jobs"].values())


def test_windows_smoke_downloads_only_the_installer_and_retries_a_stalled_download():
    steps = WF["jobs"]["smoke-on-windows"]["steps"]
    dls = [x for x in steps if str(x.get("uses", "")).startswith("actions/download-artifact")]
    assert len(dls) == 2 and all(d["with"]["name"] == "screentime-windows-installer" for d in dls)
    assert all(d["timeout-minutes"] <= 5 for d in dls) and dls[0]["continue-on-error"] is True
    assert "dl1.outcome" in dls[1]["if"]


def test_tag_creates_a_release_from_the_built_files_only_after_the_build():
    rel = WF["jobs"]["release"]
    dl = next(x for x in rel["steps"] if str(x.get("uses", "")).startswith("actions/download-artifact"))
    assert dl["with"]["pattern"] == "screentime-windows-*" and dl["with"]["merge-multiple"] is True
    assert rel["needs"] == "build" and "refs/tags/v" in rel["if"]
    assert rel["permissions"]["contents"] == "write" and WF["permissions"]["contents"] == "read"
    script = "\n".join(s.get("run", "") for s in rel["steps"])
    assert "gh release create" in script and "setup.exe" in script and "SHA256SUMS" in script
    assert "does not match the built version" in script               # tag must equal the packaged version
