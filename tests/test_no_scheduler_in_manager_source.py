"""Regression test: docs/RL-TRAINING-FLEET-SCOPING.md (private/ tree, issue
#185) makes a specific, checkable claim about this codebase's architecture
-- that there is no job queue/scheduler/fleet abstraction in src/boxkite/
or control-plane/. If that becomes false (e.g. a scheduler gets added),
that doc's recommendation to defer fleet-scale RL/training orchestration
needs re-evaluating, not silently going stale.

The doc-content half of this drift guard lives in private/tests/ instead,
since docs/ and src/boxkite/ are no longer siblings after the
public/private split (see docs/OSS-VS-HOSTED-SPLIT-POSITION.md).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_no_job_queue_or_scheduler_exists_in_manager_source():
    # Guards the doc's central factual claim: one-pod-per-session, no
    # cross-pod scheduler. If this ever starts matching, the doc's
    # deferral recommendation needs re-evaluating, not silent drift.
    manager_src = (REPO_ROOT / "src" / "boxkite" / "manager.py").read_text()
    warm_pool_src = (REPO_ROOT / "src" / "boxkite" / "_manager_warm_pool.py").read_text()
    for banned in ("class Scheduler", "job_queue", "JobQueue"):
        assert banned not in manager_src
        assert banned not in warm_pool_src
