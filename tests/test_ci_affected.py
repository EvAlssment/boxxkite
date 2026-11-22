"""scripts/ci/affected.py decides which CI jobs a PR runs. A wrong map
silently skips tests, so pin it to the repo and to ci.yml."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("affected", REPO_ROOT / "scripts/ci/affected.py")
affected = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(affected)

# Tracked paths no CI job reads. Anything else must reach at least one job.
NO_JOB_READS = [
    ".cursor/*",
    ".env.example",
    ".gcloudignore",
    ".gitignore",
    "MANIFEST.in",
    "assets/*",
    "benchmarks/*",
    "beps/*",
    "docs/*",
    "examples/*",
    "AGENTS.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "ROADMAP.md",
    "LICENSE",
]


def _runs(changed: list[str]) -> set[str]:
    result = affected.select(changed)
    jobs = {job for job, run in result.items() if run == "true"}
    images = {img["image"] for img in yaml.safe_load(result["docker-images"])}
    packages = {pkg["package"] for pkg in yaml.safe_load(result["python-packages"])}
    return jobs | {f"docker:{i}" for i in images} | {f"package:{p}" for p in packages}


def test_every_tracked_file_reaches_a_job_or_is_listed_as_read_by_none():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    orphans = [f for f in tracked if not _runs([f]) and not affected.matches(f, NO_JOB_READS)]
    assert orphans == []


def test_every_selected_job_is_wired_into_ci_yml():
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    for output in [*affected.JOBS, "root", "docker-images", "python-packages"]:
        assert f"needs.changes.outputs.{output}" in ci, output


def test_every_matrix_input_path_exists():
    for img in affected.DOCKER_IMAGES:
        assert (REPO_ROOT / img["dockerfile"]).is_file()
    for entry in [*affected.DOCKER_IMAGES, *affected.PYTHON_PACKAGES]:
        for pattern in entry["inputs"]:
            assert (REPO_ROOT / pattern.rstrip("/*")).exists(), pattern


@pytest.mark.parametrize(
    "changed, must_run",
    [
        # control-plane installs and imports the root package.
        (["src/boxxkite/manager.py"], {"root", "control-plane", "lint", "docker:control-plane"}),
        # mcp-server, boxxkite-eval and boxxkite-gym install sdk-python.
        (["sdk-python/src/boxxkite_client/client.py"],
         {"sdk-python", "mcp-server", "package:boxxkite-eval", "package:boxxkite-gym", "root"}),
        # Root tests read every Dockerfile and manifest under deploy/.
        (["deploy/pod-template.yaml"], {"root"}),
        (["sidecar/main.py"], {"root", "docker:sidecar"}),
        # Root parity tests read the JS SDK's source.
        (["sdk-js/src/errors.ts"], {"sdk-js", "root"}),
        (["control-plane/Dockerfile"], {"control-plane", "root", "docker:control-plane"}),
    ],
)
def test_cross_package_edges_are_selected(changed, must_run):
    assert must_run <= _runs(changed)


@pytest.mark.parametrize("changed", [[".github/workflows/ci.yml"], ["scripts/ci/shard.py"]])
def test_ci_changes_run_everything(changed):
    assert _runs(changed) == _runs(None)


def test_docs_only_change_runs_nothing():
    assert _runs(["docs/README.md", "CHANGELOG.md"]) == set()


def test_unknown_new_top_level_path_still_runs_root():
    assert "root" in _runs(["new-package/thing.py"])
