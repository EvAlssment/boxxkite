"""Regression test: deploy/package.json's `overrides` block previously used
`>=` lower-bound ranges (e.g. "lodash": ">=4.18.0") despite the file's own
description stating "network is disabled at runtime, so all transitive deps
must be pinned via overrides here." A `>=` range is not a pin -- it still
lets an unreviewed newer (and potentially compromised/regressed) transitive
version resolve silently at the next `npm install`/lockfile regen. Every
override must be an exact version now.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PACKAGE_JSON_PATH = Path(__file__).resolve().parent.parent / "deploy" / "package.json"

_EXACT_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_every_override_is_an_exact_pinned_version():
    doc = json.loads(PACKAGE_JSON_PATH.read_text())
    overrides = doc["overrides"]
    assert overrides, "expected at least one override in deploy/package.json"

    non_exact = {name: version for name, version in overrides.items() if not _EXACT_SEMVER_RE.match(version)}
    assert not non_exact, (
        "deploy/package.json overrides must be exact pinned versions (no "
        f">=, ^, ~, or range operators), found: {non_exact}"
    )
