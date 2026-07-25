"""Regression test: every third-party GitHub Action referenced from this
repo's workflows must be pinned to an immutable 40-char commit SHA, not a
mutable tag (@v4, @release/v1, etc.). A tag can be re-pointed by a
compromised upstream repo; a commit SHA cannot. Actions authored by GitHub
itself under github/* are exempt at GitHub's own build-time trust boundary,
but this repo doesn't currently use any -- everything referenced is
actions/*, docker/*, or pypa/*, all third-party from this repo's
perspective.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)@(\S+?)(?:\s+#.*)?\s*$", re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _all_uses_refs() -> list[tuple[Path, str, str]]:
    refs = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        for match in _USES_RE.finditer(path.read_text()):
            action, ref = match.group(1), match.group(2)
            refs.append((path, action, ref))
    return refs


def test_at_least_one_action_reference_exists():
    # Sanity check that the regex above still matches this repo's actual
    # workflow syntax -- if this ever returns 0, the other test below would
    # pass vacuously.
    assert len(_all_uses_refs()) >= 10


def test_every_action_is_pinned_to_a_commit_sha():
    unpinned = [
        f"{path.name}: {action}@{ref}"
        for path, action, ref in _all_uses_refs()
        if not _SHA_RE.match(ref)
    ]
    assert not unpinned, (
        "Found action(s) pinned to a mutable ref instead of an immutable "
        f"commit SHA: {unpinned}"
    )
