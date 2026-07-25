"""Smoke test for deploy/helm/boxkite: `helm lint` plus a `helm template`
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

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_PATH = REPO_ROOT / "deploy" / "helm" / "boxkite"

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


def test_helm_lint_passes():
    result = _run_helm("lint", str(CHART_PATH))
    assert result.returncode == 0, result.stdout + result.stderr


def test_helm_template_renders_with_defaults():
    result = _run_helm("template", "boxkite", str(CHART_PATH))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "kind: NetworkPolicy" in result.stdout
    assert "kind: ServiceAccount" in result.stdout
    # imageBuilder is off by default -- its objects must not render (its
    # ServiceAccount/Role/NetworkPolicy names still appear in comment
    # headers regardless, so check for an actual rendered object instead).
    assert "name: boxkite-image-builder-dispatch-role" not in result.stdout


def test_helm_template_renders_with_image_builder_enabled():
    result = _run_helm(
        "template", "boxkite", str(CHART_PATH), "--set", "imageBuilder.enabled=true"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "name: boxkite-image-builder-dispatch-role" in result.stdout


def test_helm_template_in_cluster_storage_egress_mode():
    result = _run_helm(
        "template",
        "boxkite",
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
        "template", "boxkite", str(CHART_PATH), "--set", "networkPolicy.storageEgress.mode=ipBlock"
    )
    assert result.returncode != 0
    assert "RFC 5737 placeholder" in result.stderr


def test_helm_template_fqdn_mode_requires_explicit_cni_acknowledgement():
    result = _run_helm(
        "template", "boxkite", str(CHART_PATH), "--set", "networkPolicy.storageEgress.mode=fqdn"
    )
    assert result.returncode != 0
    assert "fqdnEgressSupported" in result.stderr
