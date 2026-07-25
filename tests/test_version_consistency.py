"""Regression test: boxkite.__version__ previously drifted out of sync with
pyproject.toml's [project].version (0.1.0 vs 0.2.0) -- nothing enforced the
two ever matched. Asserts they're read from the same source of truth
(indirectly, by comparing values) so a future version bump that only
touches one of the two fails CI instead of shipping a stale __version__.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import boxkite

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def _cargo_version() -> str:
    with open(REPO_ROOT / "sdk-rust" / "Cargo.toml", "rb") as f:
        data = tomllib.load(f)
    return data["package"]["version"]


def test_dunder_version_matches_pyproject_toml():
    assert boxkite.__version__ == _pyproject_version(), (
        f"boxkite.__version__ ({boxkite.__version__!r}) does not match "
        f"pyproject.toml's version ({_pyproject_version()!r}) -- update "
        "src/boxkite/__init__.py's __version__ alongside any version bump."
    )


def test_dunder_version_is_a_valid_semver_string():
    assert re.match(r"^\d+\.\d+\.\d+$", boxkite.__version__), (
        f"boxkite.__version__ ({boxkite.__version__!r}) is not a plain x.y.z string"
    )


def test_sdk_rust_cargo_version_matches_pyproject_toml():
    assert _cargo_version() == _pyproject_version(), (
        f"sdk-rust/Cargo.toml version ({_cargo_version()!r}) does not match "
        f"pyproject.toml's version ({_pyproject_version()!r}) -- the published "
        "packages are versioned in lockstep; bump sdk-rust/Cargo.toml alongside "
        "any release cut (cut-public-release.sh does this)."
    )


def test_expected_sdk_go_tag_is_documented():
    """sdk-go is a subdirectory module, so its release tag must be prefixed
    with the subdir (sdk-go/vX.Y.Z), not a bare vX.Y.Z. Assert the shared
    version composes into that tag string so a release cut has an unambiguous
    tag to push (cut-public-release.sh prints it)."""
    expected_tag = f"sdk-go/v{_pyproject_version()}"
    assert re.match(r"^sdk-go/v\d+\.\d+\.\d+$", expected_tag), expected_tag
