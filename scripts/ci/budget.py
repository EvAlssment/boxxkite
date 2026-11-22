"""Fail a CI job that went over its time budget.

Each job sets BUDGET_SETUP and BUDGET_RUN (seconds), records CI_T0 when it
starts and CI_T1 when setup is done (both `date +%s` in $GITHUB_ENV), and
runs this as its last step. Waiting for a runner is outside the job and
not counted here; scripts/ci/audit.py measures it.
"""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    t0, t1, now = int(os.environ["CI_T0"]), int(os.environ["CI_T1"]), int(time.time())
    setup, running = t1 - t0, now - t1
    over = []
    for name, took, budget in (
        ("setup", setup, int(os.environ["BUDGET_SETUP"])),
        ("running", running, int(os.environ["BUDGET_RUN"])),
    ):
        print(f"{name}: {took}s (budget {budget}s)")
        if took > budget:
            over.append(f"{name} took {took}s, over its {budget}s budget")
    for msg in over:
        print(f"::error::{msg}. Make it faster, or raise the budget in ci.yml with the reason.")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
