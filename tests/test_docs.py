"""The README must only point at things that exist (a Windows user once hunted for a
`ScreenTime.cmd` that is only created by the build), and must show the current version."""
import re
from pathlib import Path

import pytest

import screentime

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)#\s]+)(?:#[^)]*)?\)")
SCRIPT = re.compile(r"(?<![\w/.-])scripts[\\/][\w.-]+\.(?:cmd|ps1|sh)")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(doc):
    missing = [t for t in LINK.findall(doc.read_text()) if not (doc.parent / t).exists()]
    assert missing == []


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_mentioned_scripts_exist_in_the_repo(doc):
    missing = [s for s in set(SCRIPT.findall(doc.read_text())) if not (ROOT / s.replace("\\", "/")).exists()]
    assert missing == []


def test_readme_states_the_current_version_and_both_platforms():
    text = (ROOT / "README.md").read_text()
    assert f"**Version {screentime.__version__}**" in text
    assert "Windows" in text and "Linux" in text


def test_readme_is_a_short_user_guide():
    assert len((ROOT / "README.md").read_text().splitlines()) < 200


def test_changelog_has_an_entry_for_the_current_version():
    assert f"## {screentime.__version__} " in (ROOT / "CHANGELOG.md").read_text()


def test_build_artifacts_are_documented_as_build_outputs():
    text = (ROOT / "docs" / "WINDOWS.md").read_text()
    assert "ScreenTime.cmd" in text and "produced by the" in text      # never implied to ship in the source tree
