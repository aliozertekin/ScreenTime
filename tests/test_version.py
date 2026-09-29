"""The version is exposed in three places; they must never drift apart."""
import re
import tomllib
from pathlib import Path

import screentime

ROOT = Path(__file__).resolve().parent.parent


def test_versions_are_consistent():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    pkgbuild = re.search(r"^pkgver=(\S+)$", (ROOT / "PKGBUILD").read_text(), re.M).group(1)
    assert screentime.__version__ == pyproject == pkgbuild


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", screentime.__version__)
