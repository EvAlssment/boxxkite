"""Drift guard for issue #180's own explicit sequencing: no CRIU-based
pause/resume implementation should land until its documented preconditions
are met. See private/docs/FULL-STATE-SNAPSHOT-SCOPING.md and
private/docs/KATA-CONTAINERS-SCOPING.md for the full tracking (in the
private/ tree, not this one -- see private/docs/OSS-VS-HOSTED-SPLIT-POSITION.md).

Same shape as tests/test_kata_template_parity.py's doc-reference checks and
control-plane's test_migrations.py drift check -- these are static guards,
not live-cluster verification.
"""

import os
from pathlib import Path

import pytest

from boxkite.checkpoint_backend import (
    CheckpointRestoreNotSupportedError,
    KubeletForensicCheckpointBackend,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_KATA_LIVE_SCRIPT = REPO_ROOT / "scripts" / "verify-kata-live.sh"


async def test_restore_still_raises_unconditionally():
    """Regression guard: nobody quietly ships a partial CRIU restore path
    on top of the forensic-only kubelet checkpoint API without this plan
    and its docs being updated first."""
    backend = KubeletForensicCheckpointBackend(core_api=None)
    with pytest.raises(CheckpointRestoreNotSupportedError):
        await backend.restore()


def test_kata_live_verification_harness_landed_on_main():
    """scripts/verify-kata-live.sh (issue #179's turnkey harness) is the
    precondition issue #180's own step 1 depends on -- it must exist and
    be runnable, not just referenced in prose."""
    assert VERIFY_KATA_LIVE_SCRIPT.is_file()
    assert os.access(VERIFY_KATA_LIVE_SCRIPT, os.X_OK)
