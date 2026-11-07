"""The version is declared in one place and every copy agrees with it."""
import re
import tomllib
from pathlib import Path

from app import __version__
from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_and_settings_follow_the_package_version():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__
    assert get_settings().project_version == __version__


def test_changelog_top_release_is_the_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", text)
    assert releases, "CHANGELOG.md has no dated release heading"
    assert releases[0] == __version__
    assert "## [Unreleased]" in text


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
