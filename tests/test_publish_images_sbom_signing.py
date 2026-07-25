"""Regression test for GitHub issue #227: every image job in
.github/workflows/publish-images.yml must generate an SBOM and sign both the
image and its SBOM keylessly via cosign/Sigstore, ahead of the EU Cyber
Resilience Act's 2026-09-11 enforcement date.

There is no live registry to integration-test this against in CI (the whole
point of keyless signing is a GitHub Actions OIDC token this test
environment doesn't have) -- this is a structural/YAML-level check, same
"grep-based CI-config assertion" style as test_docker_sock_risk_documented.py,
guarding against the SBOM/signing steps being silently dropped from a future
edit rather than verifying the signing flow itself actually succeeds against
a real registry.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "publish-images.yml"
)

IMAGE_JOBS = ["sandbox", "sandbox-minimal", "sidecar", "control-plane"]


def _load_workflow() -> dict:
    # PyYAML parses the bare `on:` mapping key as boolean True unless
    # constructors are overridden -- irrelevant here since this test only
    # reads `jobs`, but noted so a future edit of this test isn't surprised.
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _step_uses(job: dict, action_prefix: str) -> dict | None:
    for step in job["steps"]:
        if step.get("uses", "").startswith(action_prefix):
            return step
    return None


def _step_named(job: dict, name_substring: str) -> dict | None:
    for step in job["steps"]:
        if name_substring.lower() in step.get("name", "").lower():
            return step
    return None


def test_every_image_job_grants_id_token_write_for_keyless_signing():
    workflow = _load_workflow()
    for job_name in IMAGE_JOBS:
        job = workflow["jobs"][job_name]
        assert job["permissions"].get("id-token") == "write", (
            f"{job_name} job must grant id-token: write for cosign's keyless "
            "Fulcio OIDC flow"
        )


def test_every_image_job_captures_the_build_digest():
    workflow = _load_workflow()
    for job_name in IMAGE_JOBS:
        job = workflow["jobs"][job_name]
        build_step = _step_uses(job, "docker/build-push-action@")
        assert build_step is not None, f"{job_name} has no build-push-action step"
        assert build_step.get("id") == "build", (
            f"{job_name}'s build-push-action step must set id: build so later "
            "steps can reference steps.build.outputs.digest"
        )


def test_every_image_job_generates_an_sbom_against_the_digest():
    workflow = _load_workflow()
    for job_name in IMAGE_JOBS:
        job = workflow["jobs"][job_name]
        sbom_step = _step_uses(job, "anchore/sbom-action@")
        assert sbom_step is not None, f"{job_name} has no SBOM generation step"
        image_input = sbom_step["with"]["image"]
        assert "steps.build.outputs.digest" in image_input, (
            f"{job_name}'s SBOM must be generated against the pushed digest, "
            "not a mutable tag"
        )
        assert sbom_step["with"]["format"] == "spdx-json"


def test_every_image_job_installs_cosign():
    workflow = _load_workflow()
    for job_name in IMAGE_JOBS:
        job = workflow["jobs"][job_name]
        assert _step_uses(job, "sigstore/cosign-installer@") is not None, (
            f"{job_name} has no cosign-installer step"
        )


def test_every_image_job_signs_the_image_keylessly():
    workflow = _load_workflow()
    for job_name in IMAGE_JOBS:
        job = workflow["jobs"][job_name]
        sign_step = _step_named(job, "Sign image")
        assert sign_step is not None, f"{job_name} has no image-signing step"
        run = sign_step.get("run", "")
        assert "cosign sign --yes" in run
        assert "steps.build.outputs.digest" in run


def test_every_image_job_attests_the_sbom_keylessly():
    workflow = _load_workflow()
    for job_name in IMAGE_JOBS:
        job = workflow["jobs"][job_name]
        attest_step = _step_named(job, "Attest SBOM")
        assert attest_step is not None, f"{job_name} has no SBOM attestation step"
        run = attest_step.get("run", "")
        assert "cosign attest --yes" in run
        assert "--type spdxjson" in run
        assert "steps.build.outputs.digest" in run


def test_actions_referenced_by_sha_are_pinned_not_floating_tags():
    """Same pinning discipline the rest of this workflow already follows
    (actions/checkout, docker/*) -- every `uses:` must be `owner/repo@<sha>`,
    not a mutable tag/branch ref."""
    workflow = _load_workflow()
    for job_name in IMAGE_JOBS:
        job = workflow["jobs"][job_name]
        for step in job["steps"]:
            uses = step.get("uses")
            if uses is None:
                continue
            ref = uses.split("@", 1)[1]
            assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (
                f"{job_name}'s step uses {uses!r} -- must be pinned to a full "
                "40-character commit SHA, not a tag/branch"
            )
