"""Split a pytest suite's test files across CI shards by measured duration.

    shard.py plan  --tests DIR --durations FILE --shard I --of N
        Print the test files shard I (1-based) of N should run.
    shard.py check --tests DIR --junit-dir DIR --write-durations FILE
        Fail unless the shards' JUnit reports cover every test file exactly
        once, then write per-file durations for the next run to balance on.

Run it from the package directory, with pytest writing JUnit as xunit1
(`-o junit_family=xunit1`), so report paths match `--tests` paths.

Files with no recorded duration (new files, or no durations at all) count
as the median of the known ones, so a missing durations file still gives
every shard a share of the work.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def test_files(tests: Path) -> list[str]:
    return sorted(p.as_posix() for p in tests.rglob("test_*.py"))


def plan(files: list[str], durations: dict[str, float], shards: int) -> list[list[str]]:
    default = statistics.median(durations.values()) if durations else 1.0
    weight = {f: durations.get(f, default) for f in files}
    bins: list[tuple[float, list[str]]] = [(0.0, []) for _ in range(shards)]
    # Longest first onto the lightest shard (LPT). Ties break on the name so
    # every shard computes the same plan.
    for f in sorted(files, key=lambda f: (-weight[f], f)):
        i = min(range(shards), key=lambda i: (bins[i][0], i))
        total, members = bins[i]
        bins[i] = (total + weight[f], members + [f])
    return [sorted(members) for _, members in bins]


def durations_from_junit(junit_dir: Path) -> dict[str, float]:
    seen: dict[str, float] = {}
    for report in sorted(junit_dir.rglob("*.xml")):
        for case in ET.parse(report).iter("testcase"):
            f = case.get("file")
            seen[f] = seen.get(f, 0.0) + float(case.get("time") or 0)
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "check"])
    ap.add_argument("--tests", type=Path, required=True)
    ap.add_argument("--durations", type=Path)
    ap.add_argument("--shard", type=int)
    ap.add_argument("--of", type=int)
    ap.add_argument("--junit-dir", type=Path)
    ap.add_argument("--write-durations", type=Path)
    a = ap.parse_args()

    files = test_files(a.tests)
    if a.mode == "plan":
        durations = {}
        if a.durations and a.durations.is_file():
            durations = json.loads(a.durations.read_text())
        print(" ".join(plan(files, durations, a.of)[a.shard - 1]))
        return 0

    # Each shard writes its own report, so a file that shows up in two
    # reports ran twice and a file in none never ran.
    ran: dict[str, list[str]] = {}
    for report in sorted(a.junit_dir.rglob("*.xml")):
        for f in {c.get("file") for c in ET.parse(report).iter("testcase")}:
            ran.setdefault(f, []).append(report.name)
    missing = sorted(set(files) - ran.keys())
    twice = sorted(f for f, r in ran.items() if len(r) > 1)
    for f in missing:
        print(f"::error::{f} ran in no shard")
    for f in twice:
        print(f"::error::{f} ran in {len(ran[f])} shards: {', '.join(ran[f])}")
    if missing or twice:
        return 1
    print(f"all {len(files)} test files ran exactly once across shards")
    if a.write_durations:
        per_file = {f: round(s, 2) for f, s in durations_from_junit(a.junit_dir).items()}
        a.write_durations.write_text(json.dumps(per_file, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
