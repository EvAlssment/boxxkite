"""Decide which CI jobs a pull request's diff can reach.

    affected.py --base SHA --head SHA    (pull requests)
    affected.py --all                    (main, merge queue: run everything)

Writes one `<job>=true|false` line per job, and the matrix entries for
`docker-images` and `python-packages` as JSON, to $GITHUB_OUTPUT (and
stderr, for the log). Only pull requests are narrowed: the merge
queue and main always run the full suite.

The map below comes from recording every repo file each test suite opens
or imports during a full run. When a suite starts reading a new path, add
it here. tests/test_ci_affected.py checks the map against the tracked
files and against ci.yml.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys

# A change here can change what any job does, so it reruns everything.
EVERYTHING = [".github/*", "scripts/ci/*"]

# Paths each job reads. A job runs when any changed file matches.
JOBS: dict[str, list[str]] = {
    "lint": ["src/*", "tests/*", "control-plane/*", "pyproject.toml"],
    # The audit scans installed dependencies, which only these change. The
    # merge queue and main still run it every time, so a new CVE shows up.
    "audit": ["pyproject.toml", "control-plane/pyproject.toml"],
    "control-plane": ["control-plane/*", "src/*", "pyproject.toml"],
    "sdk-python": ["sdk-python/*"],
    "mcp-server": ["mcp-server/*", "sdk-python/*"],
    "sdk-js": ["sdk-js/*"],
    "sdk-go": ["sdk-go/*"],
    "sdk-rust": ["sdk-rust/*"],
    "sdk-java": ["sdk-java/*"],
    "sdk-csharp": ["sdk-csharp/*"],
}

# Small Python packages, run as one matrix job. Each entry is a matrix
# include: `extra` is the pip extra with its test deps, `local-deps` a
# sibling it must be tested against instead of the PyPI release.
PYTHON_PACKAGES = [
    {"package": "bastion", "extra": "dev", "inputs": ["bastion/*"]},
    {"package": "boxxkite-eval", "extra": "dev", "local-deps": "./sdk-python",
     "inputs": ["boxxkite-eval/*", "sdk-python/*"]},
    {"package": "boxxkite-gym", "extra": "test,gymnasium", "local-deps": "./sdk-python",
     "inputs": ["boxxkite-gym/*", "sdk-python/*"]},
    {"package": "reward-functions", "extra": "test", "inputs": ["reward-functions/*"]},
    {"package": "memory-model-server", "extra": "dev", "inputs": ["memory-model-server/*"]},
]

# The root suite reads across the repo (deploy/ manifests, Dockerfiles,
# workflows, the other SDKs' sources for parity tests), so it runs unless
# every changed file is somewhere it provably never reads.
ROOT_NEVER_READS = [
    "control-plane/*",
    "mcp-server/*",
    "sdk-csharp/*",
    "sdk-java/*",
    "bastion/*",
    "boxxkite-eval/*",
    "boxxkite-gym/*",
    "reward-functions/*",
    "memory-model-server/*",
    "benchmarks/*",
    "beps/*",
    "docs/*",
    "assets/*",
    "examples/*",
    "AGENTS.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "ROADMAP.md",
    "LICENSE",
]
# ...except these files under those paths, which root tests do read.
ROOT_READS_ANYWAY = [
    "README.md",
    "SECURITY.md",
    "control-plane/.env.example",
    "control-plane/Dockerfile",
]

# Each image's Dockerfile plus everything it COPYs from the repo.
DOCKER_IMAGES = [
    {"image": "sandbox", "dockerfile": "deploy/sandbox.Dockerfile",
     "inputs": ["deploy/package.json", "deploy/package-lock.json",
                "deploy/requirements.txt", "deploy/requirements.lock"]},
    {"image": "sandbox-minimal", "dockerfile": "deploy/sandbox-minimal.Dockerfile",
     "platforms": "linux/amd64,linux/arm64", "inputs": []},
    {"image": "sandbox-node", "dockerfile": "deploy/sandbox-node.Dockerfile", "inputs": []},
    {"image": "sandbox-go", "dockerfile": "deploy/sandbox-go.Dockerfile", "inputs": []},
    {"image": "sandbox-rust", "dockerfile": "deploy/sandbox-rust.Dockerfile", "inputs": []},
    {"image": "sandbox-claude-code", "dockerfile": "deploy/sandbox-claude-code.Dockerfile",
     "inputs": []},
    {"image": "sidecar", "dockerfile": "deploy/sidecar.Dockerfile",
     "platforms": "linux/amd64,linux/arm64", "inputs": ["sidecar/*"]},
    {"image": "control-plane", "dockerfile": "control-plane/Dockerfile",
     "platforms": "linux/amd64,linux/arm64",
     "inputs": ["src/*", "pyproject.toml", "control-plane/*"]},
]


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def select(changed: list[str] | None) -> dict[str, object]:
    """None means run everything."""
    if changed is None or any(matches(f, EVERYTHING) for f in changed):
        jobs = {job: True for job in [*JOBS, "root"]}
        images = DOCKER_IMAGES
        packages = PYTHON_PACKAGES
    else:
        jobs = {job: any(matches(f, pats) for f in changed) for job, pats in JOBS.items()}
        jobs["root"] = any(
            matches(f, ROOT_READS_ANYWAY) or not matches(f, ROOT_NEVER_READS) for f in changed
        )
        images = [
            img for img in DOCKER_IMAGES
            if any(f == img["dockerfile"] or matches(f, img["inputs"]) for f in changed)
        ]
        packages = [
            pkg for pkg in PYTHON_PACKAGES if any(matches(f, pkg["inputs"]) for f in changed)
        ]
    return {
        **{job: str(run).lower() for job, run in jobs.items()},
        "docker-images": _matrix(images),
        "python-packages": _matrix(packages),
    }


def _matrix(entries: list[dict]) -> str:
    return json.dumps([{k: v for k, v in e.items() if k != "inputs"} for e in entries])


def changed_files(base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--base")
    ap.add_argument("--head")
    a = ap.parse_args()
    changed = None if a.all else changed_files(a.base, a.head)
    result = select(changed)
    lines = [f"{k}={v}" for k, v in result.items()]
    if changed is not None:
        print(f"{len(changed)} changed files", file=sys.stderr)
    print("\n".join(lines), file=sys.stderr)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
