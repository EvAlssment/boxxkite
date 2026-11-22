"""Split CI time into waiting, setup and running, at p50 and p95.

    audit.py --repo OWNER/NAME [--runs 150] [--branch B] [--event pull_request] [--ids ID ...]

Needs `gh` logged in. For each job: waiting is created -> started (queued
for a runner); setup is checkout, toolchains, installs, cache restores and
post-job cleanup; running is every other step. Per run, "result" is the
time from the run being created to its last job finishing, which is what
a PR author waits for.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from collections import defaultdict
from datetime import datetime

SETUP_STEP = re.compile(
    r"^(Set up job|Complete job|Post |Run actions/(checkout|setup-|cache|download-artifact|upload-artifact)"
    r"|Run astral-sh/setup-uv|Run oven-sh/setup-bun|Run dtolnay/|Run Swatinem/|Run docker/setup-"
    r"|Install|Restore|Upload|Save)",
)


def gh(path: str) -> dict:
    return json.loads(subprocess.check_output(["gh", "api", path]))


def ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def cell(xs: list[float]) -> str:
    return f"{pct(xs, .5):4.0f}/{pct(xs, .95):<4.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--runs", type=int, default=150)
    ap.add_argument("--branch")
    ap.add_argument("--event")
    ap.add_argument("--workflow", default="CI")
    ap.add_argument("--ids", nargs="*", type=int)
    a = ap.parse_args()

    if a.ids:
        runs = [gh(f"repos/{a.repo}/actions/runs/{i}") for i in a.ids]
    else:
        runs, page = [], 1
        while len(runs) < a.runs:
            q = f"per_page=100&page={page}&status=completed"
            q += f"&branch={a.branch}" if a.branch else ""
            q += f"&event={a.event}" if a.event else ""
            batch = gh(f"repos/{a.repo}/actions/runs?{q}")["workflow_runs"]
            if not batch:
                break
            runs += [r for r in batch if r["name"] == a.workflow]
            page += 1
        runs = runs[: a.runs]

    per_job: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    results, totals = [], defaultdict(float)
    billed_minutes = 0  # GitHub bills each job rounded up to the minute
    for run in runs:
        jobs = gh(f"repos/{a.repo}/actions/runs/{run['id']}/jobs?per_page=100&filter=latest")["jobs"]
        done = [ts(j["completed_at"]) for j in jobs if j["conclusion"] not in (None, "skipped")]
        if done:
            results.append((max(done) - ts(run["created_at"])).total_seconds())
        for j in jobs:
            if j["conclusion"] in (None, "skipped") or not j["started_at"]:
                continue
            name = re.sub(r"\(py3\.\d+(, shard \d+)?\)", "(py*)", j["name"])
            wait = max((ts(j["started_at"]) - ts(j["created_at"])).total_seconds(), 0)
            setup = running = 0.0
            for s in j["steps"]:
                if s.get("started_at") and s.get("completed_at"):
                    d = (ts(s["completed_at"]) - ts(s["started_at"])).total_seconds()
                    if SETUP_STEP.match(s["name"]):
                        setup += d
                    else:
                        running += d
            billed_minutes += math.ceil((ts(j["completed_at"]) - ts(j["started_at"])).total_seconds() / 60)
            for bucket, v in (("wait", wait), ("setup", setup), ("run", running)):
                per_job[name][bucket].append(v)
                totals[bucket] += v

    print(f"{len(runs)} runs of {a.workflow} in {a.repo}")
    if results:
        print(f"PR result time: p50 {pct(results, .5):.0f}s  p95 {pct(results, .95):.0f}s  (n={len(results)})")
    print(f"\n{'job':52} {'n':>3} | {'wait':>9} | {'setup':>9} | {'running':>9}   (p50/p95 s)")
    for name in sorted(per_job, key=lambda n: -pct(per_job[n]["run"], .5) - pct(per_job[n]["setup"], .5)):
        b = per_job[name]
        print(f"{name[:52]:52} {len(b['run']):3} | {cell(b['wait'])} | {cell(b['setup'])} | {cell(b['run'])}")
    total = sum(totals.values()) or 1
    print("\nshare of job time: " + ", ".join(f"{k} {v / total:.0%} ({v / 60:.0f} min)" for k, v in totals.items()))
    print(f"billed job-minutes per run: {billed_minutes / max(len(runs), 1):.0f}")

if __name__ == "__main__":
    main()
