"""Keep published image platform claims aligned across source, CI, and Helm docs."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "deploy" / "image-platforms.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-images.yml"
CHART_PATH = REPO_ROOT / "deploy" / "helm" / "boxxkite"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text())


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _build_step(job: dict) -> dict:
    return next(step for step in job["steps"] if step.get("id") == "build")


def _platform_label(dockerfile: Path) -> list[str]:
    match = re.search(
        r'^LABEL com\.boxxkite\.supported-platforms="([^"]+)"$',
        dockerfile.read_text(),
        re.MULTILINE,
    )
    assert match, f"{dockerfile} must declare com.boxxkite.supported-platforms"
    return match.group(1).split(",")


def test_platform_contract_covers_exactly_the_published_images():
    contract = _contract()
    assert set(contract["images"]) == {
        "boxxkite-sandbox",
        "boxxkite-sandbox-minimal",
        "boxxkite-sidecar",
        "boxxkite-control-plane",
    }
    assert contract["schema_version"] == 1
    for metadata in contract["images"].values():
        if len(metadata["platforms"]) == 1:
            assert metadata.get("reason"), "single-platform images need a blocker reason"


def test_dockerfile_labels_match_the_platform_contract():
    for image, metadata in _contract()["images"].items():
        dockerfile = REPO_ROOT / metadata["dockerfile"]
        assert _platform_label(dockerfile) == metadata["platforms"]
        text = dockerfile.read_text()
        assert "ARG TARGETARCH" in text
        if metadata["platforms"] == ["linux/amd64"]:
            assert 'test "$TARGETARCH" = amd64' in text
        else:
            assert "amd64|arm64" in text


def test_release_build_jobs_match_the_platform_contract():
    workflow = _workflow()
    for image, metadata in _contract()["images"].items():
        job_name = image.removeprefix("boxxkite-")
        job = workflow["jobs"][job_name]
        build = _build_step(job)
        assert build["with"]["file"] == metadata["dockerfile"]
        assert build["with"]["platforms"].split(",") == metadata["platforms"]

        uses = {step.get("uses", "") for step in job["steps"]}
        has_qemu = any(use.startswith("docker/setup-qemu-action@") for use in uses)
        assert has_qemu is (len(metadata["platforms"]) > 1), image


def test_manifest_verification_matrix_matches_the_platform_contract():
    matrix = _workflow()["jobs"]["verify"]["strategy"]["matrix"]["include"]
    actual = {entry["image"]: entry["platforms"].split(",") for entry in matrix}
    expected = {image: data["platforms"] for image, data in _contract()["images"].items()}
    assert actual == expected


def test_chart_is_architecture_neutral_and_has_no_dead_workload_defaults():
    chart = yaml.safe_load((CHART_PATH / "Chart.yaml").read_text())
    assert chart["annotations"]["boxxkite.dev/supported-platforms"] == (
        "linux/amd64,linux/arm64"
    )
    values = yaml.safe_load((CHART_PATH / "values.yaml").read_text())
    assert "nodeSelector" not in values
    assert "image" not in values

    readme = (CHART_PATH / "README.md").read_text()
    assert "no image-bearing Deployment" in readme
    for image, metadata in _contract()["images"].items():
        assert f"`{image}`" in readme
        assert ", ".join(f"`{platform}`" for platform in metadata["platforms"]) in readme


def test_full_sandbox_fleet_examples_are_pinned_to_amd64_nodes():
    fleet = yaml.safe_load((REPO_ROOT / "deploy" / "fleet.yaml").read_text())
    for cluster in fleet["clusters"]:
        assert cluster["images"]["sandbox"] == "boxxkite-sandbox:latest"
        assert cluster["nodeSelector"]["kubernetes.io/arch"] == "amd64"
