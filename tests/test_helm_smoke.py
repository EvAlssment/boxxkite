"""Smoke test for deploy/helm/boxxkite: `helm lint` plus a `helm template`
dry-run across the chart's main opt-in toggles (imageBuilder, each
storageEgress mode). Gated on `helm` being present on PATH -- there is no
live registry/cluster to integration-test the chart's actual `kubectl apply`
behavior in CI, so this only verifies the chart renders valid YAML and Helm
itself considers it well-formed, mirroring the "implemented against the real
API shape, never exercised against a live service where that's genuinely not
possible" honesty this project already applies elsewhere (see
resource_config.py's Kata/GPU comments).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_PATH = REPO_ROOT / "deploy" / "helm" / "boxxkite"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm not installed on PATH"
)


def _run_helm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_helm_with_values(tmp_path: Path, values: str) -> subprocess.CompletedProcess:
    values_path = tmp_path / "values.yaml"
    values_path.write_text(values, encoding="utf-8")
    return _run_helm("template", "boxxkite", str(CHART_PATH), "--values", str(values_path))


def test_helm_lint_passes():
    result = _run_helm("lint", str(CHART_PATH))
    assert result.returncode == 0, result.stdout + result.stderr


def test_helm_template_renders_with_defaults():
    result = _run_helm("template", "boxxkite", str(CHART_PATH))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "kind: NetworkPolicy" in result.stdout
    assert "kind: ServiceAccount" in result.stdout
    # imageBuilder is off by default -- its objects must not render (its
    # ServiceAccount/Role/NetworkPolicy names still appear in comment
    # headers regardless, so check for an actual rendered object instead).
    assert "name: boxxkite-image-builder-dispatch-role" not in result.stdout


def test_helm_template_renders_with_image_builder_enabled():
    result = _run_helm(
        "template", "boxxkite", str(CHART_PATH), "--set", "imageBuilder.enabled=true"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "name: boxxkite-image-builder-dispatch-role" in result.stdout


def test_helm_template_in_cluster_storage_egress_mode():
    result = _run_helm(
        "template",
        "boxxkite",
        str(CHART_PATH),
        "--set",
        "networkPolicy.storageEgress.mode=inCluster",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'app: "minio"' in result.stdout


def test_helm_template_ip_block_mode_fails_closed_on_unfilled_placeholder():
    """The default ipBlock CIDR is the RFC 5737 placeholder from
    ../../network-policy.yaml -- selecting mode=ipBlock without also
    overriding the CIDR must fail the render rather than silently ship a
    NetworkPolicy that permits nothing while looking configured."""
    result = _run_helm(
        "template", "boxxkite", str(CHART_PATH), "--set", "networkPolicy.storageEgress.mode=ipBlock"
    )
    assert result.returncode != 0
    assert "RFC 5737 placeholder" in result.stderr


def test_helm_template_fqdn_mode_requires_explicit_cni_acknowledgement():
    result = _run_helm(
        "template", "boxxkite", str(CHART_PATH), "--set", "networkPolicy.storageEgress.mode=fqdn"
    )
    assert result.returncode != 0
    assert "fqdnEgressSupported" in result.stderr


@pytest.mark.parametrize(
    "cidr",
    [
        "203.0.113.0/24",
        "2001:db8::/32",
        "::ffff:192.0.2.1/128",
        "0:0:0:0:0:ffff:192.0.2.1/128",
    ],
)
def test_helm_template_accepts_valid_storage_ip_block_cidrs(cidr: str):
    result = _run_helm(
        "template",
        "boxxkite",
        str(CHART_PATH),
        "--set",
        "networkPolicy.storageEgress.mode=ipBlock",
        "--set-string",
        f"networkPolicy.storageEgress.ipBlock.cidr={cidr}",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "cidr", ["999.0.0.1/24", "2001:db8:::1/64", "::ffff:999.0.2.1/128"]
)
def test_helm_template_rejects_malformed_storage_ip_block_cidrs(cidr: str):
    result = _run_helm(
        "template",
        "boxxkite",
        str(CHART_PATH),
        "--set",
        "networkPolicy.storageEgress.mode=ipBlock",
        "--set-string",
        f"networkPolicy.storageEgress.ipBlock.cidr={cidr}",
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "value", ["networkPolicy.enabled=not-a-boolean", "unexpectedValue=true"]
)
def test_helm_template_rejects_structurally_invalid_values(value: str):
    result = _run_helm("template", "boxxkite", str(CHART_PATH), "--set", value)
    assert result.returncode != 0


def test_helm_template_renders_non_secret_fleet_registration_contract(tmp_path: Path):
    result = _run_helm_with_values(
        tmp_path,
        """
        fleet:
          primaryClusterName: primary
          clusters:
            - name: eu-west
              region: eu-west-1
              kubeconfigSecretRef:
                name: boxxkite-cluster-eu-west
                key: kubeconfig
              warmPool:
                maxSize: 3
                targets:
                  small: 2
                  medium: 1
                  large: 0
              images:
                sandbox: registry.example/sandbox:v1
                sidecar: registry.example/sidecar:v1
              nodeSelector:
                pool: sandbox
              tolerations:
                - key: dedicated
                  operator: Equal
                  value: sandbox
                  effect: NoSchedule
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    fleet_config = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap"
        and document["metadata"]["name"] == "boxxkite-fleet-clusters"
    )
    registration = yaml.safe_load(fleet_config["data"]["clusters.yaml"])
    assert registration == {
        "apiVersion": "boxxkite.dev/v1alpha1",
        "kind": "FleetRegistration",
        "primaryClusterName": "primary",
        "clusters": [
            {
                "name": "eu-west",
                "region": "eu-west-1",
                "kubeconfigSecretRef": {
                    "name": "boxxkite-cluster-eu-west",
                    "key": "kubeconfig",
                },
                "warmPool": {
                    "maxSize": 3,
                    "targets": {"small": 2, "medium": 1, "large": 0},
                },
                "images": {
                    "sandbox": "registry.example/sandbox:v1",
                    "sidecar": "registry.example/sidecar:v1",
                },
                "nodeSelector": {"pool": "sandbox"},
                "tolerations": [
                    {
                        "key": "dedicated",
                        "operator": "Equal",
                        "value": "sandbox",
                        "effect": "NoSchedule",
                    }
                ],
            }
        ],
    }
    secret_names = {
        document["metadata"]["name"]
        for document in documents
        if document["kind"] == "Secret"
    }
    assert "boxxkite-cluster-eu-west" not in secret_names
    assert not any(
        document["kind"] == "NetworkPolicy" and "eu-west" in document["metadata"]["name"]
        for document in documents
    )


def test_helm_template_rejects_ambiguous_fleet_entries(tmp_path: Path):
    result = _run_helm_with_values(
        tmp_path,
        """
        fleet:
          primaryClusterName: primary
          clusters:
            - name: primary
              region: us-east-1
              kubeconfigSecretRef: {name: boxxkite-primary, key: kubeconfig}
              warmPool: {maxSize: 1, targets: {small: 1, medium: 0, large: 0}}
        """,
    )
    assert result.returncode != 0
    assert "must not equal fleet.primaryClusterName" in result.stderr


def test_helm_template_rejects_fleet_capacity_overflow(tmp_path: Path):
    result = _run_helm_with_values(
        tmp_path,
        """
        fleet:
          primaryClusterName: primary
          clusters:
            - name: eu-west
              region: eu-west-1
              kubeconfigSecretRef: {name: boxxkite-eu-west, key: kubeconfig}
              warmPool: {maxSize: 1, targets: {small: 1, medium: 1, large: 0}}
        """,
    )
    assert result.returncode != 0
    assert "exceeds warmPool.maxSize" in result.stderr


def test_helm_template_rejects_cross_namespace_secret_reference(tmp_path: Path):
    result = _run_helm_with_values(
        tmp_path,
        """
        fleet:
          primaryClusterName: primary
          clusters:
            - name: eu-west
              region: eu-west-1
              kubeconfigSecretRef:
                name: boxxkite-eu-west
                key: kubeconfig
                namespace: other-namespace
              warmPool: {maxSize: 1, targets: {small: 1, medium: 0, large: 0}}
        """,
    )
    assert result.returncode != 0
    assert "namespace" in result.stderr
