"""Validation for the Helm-rendered Kustomize base and its overlays."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KUSTOMIZE_ROOT = REPO_ROOT / "deploy" / "kustomize"
GENERATOR = KUSTOMIZE_ROOT / "generate.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("kubectl") is None,
    reason="helm and kubectl are required for Kustomize validation",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _documents(output: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(output) if doc]


def _resource(documents: list[dict], kind: str, name: str) -> dict:
    matches = [
        doc
        for doc in documents
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected one {kind}/{name}, found {len(matches)}"
    return matches[0]


def test_generated_base_matches_helm_render():
    result = _run("bash", str(GENERATOR), "check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_base_build_contains_only_chart_rendered_prerequisites():
    result = _run("kubectl", "kustomize", str(KUSTOMIZE_ROOT / "base"))
    assert result.returncode == 0, result.stdout + result.stderr
    documents = _documents(result.stdout)
    assert {doc["kind"] for doc in documents} == {
        "ConfigMap",
        "NetworkPolicy",
        "Role",
        "RoleBinding",
        "Secret",
        "ServiceAccount",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }
    assert not any(
        doc.get("metadata", {}).get("name") == "boxxkite-image-builder-dispatch-role"
        for doc in documents
    )


@pytest.mark.parametrize(
    ("environment", "namespace", "warm_pool", "cidr"),
    [
        ("dev", "boxxkite-dev", "1", None),
        ("staging", "boxxkite-staging", "6", "198.51.100.0/24"),
        ("prod", "boxxkite-prod", "20", "203.0.113.0/24"),
    ],
)
def test_overlay_builds_with_environment_scoped_references(
    environment: str, namespace: str, warm_pool: str, cidr: str | None
):
    result = _run(
        "kubectl",
        "kustomize",
        str(KUSTOMIZE_ROOT / "overlays" / environment),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    documents = _documents(result.stdout)

    assert _resource(documents, "Namespace", namespace)
    namespaced_kinds = {
        "ConfigMap",
        "NetworkPolicy",
        "Role",
        "RoleBinding",
        "Secret",
        "ServiceAccount",
    }
    for doc in documents:
        if doc.get("kind") in namespaced_kinds:
            assert doc["metadata"]["namespace"] == namespace

    policy = _resource(
        documents,
        "ValidatingAdmissionPolicy",
        f"boxxkite-{environment}-sandbox-pod-hardening",
    )
    binding = _resource(
        documents,
        "ValidatingAdmissionPolicyBinding",
        f"boxxkite-{environment}-sandbox-pod-hardening-binding",
    )
    assert "namespace" not in policy["metadata"]
    assert "namespace" not in binding["metadata"]
    assert binding["spec"]["policyName"] == policy["metadata"]["name"]
    assert (
        binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"]
        == {"kubernetes.io/metadata.name": namespace}
    )

    role_binding = _resource(documents, "RoleBinding", "sandbox-manager-binding")
    assert role_binding["subjects"][0]["namespace"] == namespace
    configmap = _resource(documents, "ConfigMap", "sandbox-config")
    assert configmap["data"]["warm-pool-size"] == warm_pool

    network_policy = _resource(documents, "NetworkPolicy", "sandbox-network-policy")
    rendered = yaml.safe_dump(network_policy)
    assert "0.0.0.0/0" not in rendered
    if cidr is None:
        assert "ipBlock" not in rendered
    else:
        assert cidr in rendered
