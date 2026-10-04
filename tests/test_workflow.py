"""The CI workflow must keep producing downloadable Windows files (artifacts on every push, draft release on tags)."""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
WF = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github" / "workflows" / "windows.yml").read_text())
ON = WF.get("on", WF.get(True))


def test_branch_pushes_and_tags_both_trigger_it():
    assert "main" in ON["push"]["branches"] and "v*" in ON["push"]["tags"]      # tags without branches would silence pushes


def test_build_uploads_the_installer_and_zip_as_artifacts():
    steps = WF["jobs"]["build"]["steps"]
    up = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert up["with"]["path"].startswith("dist/ScreenTime-") and up["with"]["if-no-files-found"] == "error"


def test_tag_creates_a_release_from_the_built_files_only_after_the_build():
    rel = WF["jobs"]["release"]
    assert rel["needs"] == "build" and "refs/tags/v" in rel["if"]
    assert rel["permissions"]["contents"] == "write" and WF["permissions"]["contents"] == "read"
    script = "\n".join(s.get("run", "") for s in rel["steps"])
    assert "gh release create" in script and "setup.exe" in script and "SHA256SUMS" in script
    assert "does not match the built version" in script               # tag must equal the packaged version
