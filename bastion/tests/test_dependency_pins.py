"""Regression guard for issue #154's concrete finding: bastion/pyproject.toml's
`asyncssh` version constraint must never have a floor low enough to admit a
version with a known, fixed CVE.

The old `asyncssh>=2.14,<3` floor was satisfiable by 2.14.0 and 2.14.1, both
of which carry three GitHub-reviewed advisories fixed only as of 2.14.2:
GHSA-c35q-ffpf-5qpm (CVE-2023-46446, "Rogue Session Attack"),
GHSA-cfc2-wr2v-gxm5 (CVE-2023-46445, "Rogue Extension Negotiation"), and
GHSA-hfmc-7525-mj55 (the Terrapin prefix-truncation attack). This does not
re-implement a general-purpose version specifier parser (no `packaging`
dependency is declared for this small, standalone component, see
config.py's module docstring on minimizing dependencies) -- it only checks
the one thing that actually matters here: the declared floor, read
directly out of pyproject.toml, is at or above the fixed version.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# The version each of GHSA-c35q-ffpf-5qpm / GHSA-cfc2-wr2v-gxm5 /
# GHSA-hfmc-7525-mj55 was fixed in -- the minimum safe floor.
_MIN_SAFE_ASYNCSSH_VERSION = (2, 14, 2)

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _asyncssh_dependency_spec() -> str:
    data = tomllib.loads(_PYPROJECT_PATH.read_text())
    dependencies = data["project"]["dependencies"]
    for dep in dependencies:
        if dep.startswith("asyncssh"):
            return dep
    raise AssertionError("no asyncssh entry found in bastion/pyproject.toml dependencies")


def _parse_lower_bound(spec: str) -> tuple[int, ...]:
    match = re.search(r">=\s*(\d+(?:\.\d+)*)", spec)
    assert match is not None, f"asyncssh dependency spec has no >= lower bound: {spec!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def test_asyncssh_lower_bound_excludes_known_vulnerable_versions():
    spec = _asyncssh_dependency_spec()
    lower_bound = _parse_lower_bound(spec)
    assert lower_bound >= _MIN_SAFE_ASYNCSSH_VERSION, (
        f"asyncssh dependency spec {spec!r} has a floor of {lower_bound}, which is "
        f"below {_MIN_SAFE_ASYNCSSH_VERSION} -- this would silently permit installing "
        "a version with a known, fixed CVE (GHSA-c35q-ffpf-5qpm / GHSA-cfc2-wr2v-gxm5 / "
        "GHSA-hfmc-7525-mj55). Bump the floor, don't relax this test."
    )


def test_asyncssh_dependency_still_caps_the_major_version():
    """Guards against the fix for this drifting into an unbounded/major-version
    upgrade by accident -- the upper bound is a separate, deliberate choice
    (see pyproject.toml's comment) and this test isn't about that axis, but a
    silently dropped upper bound would be an unrelated regression worth
    catching here too."""
    spec = _asyncssh_dependency_spec()
    assert "<3" in spec, f"asyncssh dependency spec {spec!r} no longer caps the major version at <3"
