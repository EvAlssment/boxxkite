"""Tests for deploy/pod-template-kata.yaml (docs/KATA-CONTAINERS-SCOPING.md)
and resource_config.py's kata_runtime_class_name()/kata_runtime_class_enabled().

Mirrors test_pod_template_parity.py's drift-guard shape against the Kata
sibling template, plus the opt-in flag's own behavior. These are static
drift-guards; the actual live-cluster verification issue #179 asks for is
scripts/verify-kata-live.sh (a one-command harness, validated end-to-end
under runc on a live AKS cluster, and run against a real Kata cluster on
2026-07-16 -- see docs/KATA-CONTAINERS-SCOPING.md's dated updates). That
live run CONFIRMED emptyDir.sizeLimit is NOT enforced under Kata's shipped
defaults; the disclosure test below pins the template header to that
confirmed finding.
"""

import os
from pathlib import Path

import pytest
import yaml

from boxkite import resource_config

KATA_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "deploy" / "pod-template-kata.yaml"
VERIFY_HARNESS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify-kata-live.sh"


def _kata_template_doc() -> dict:
    return yaml.safe_load(KATA_TEMPLATE_PATH.read_text())


def _kata_template_container(name: str) -> dict:
    doc = _kata_template_doc()
    for container in doc["spec"]["containers"]:
        if container["name"] == name:
            return container
    raise AssertionError(f"No {name!r} container found in pod-template-kata.yaml")


def test_kata_template_sets_runtime_class_name():
    doc = _kata_template_doc()
    assert doc["spec"]["runtimeClassName"] == "kata"


def test_kata_template_discloses_the_confirmed_emptydir_regression():
    """issue #179's live run (2026-07-16, GKE + kata-deploy) confirmed
    sizeLimit is NOT enforced under Kata's shipped defaults -- the header
    must disclose it as a confirmed regression, not an open question."""
    text = KATA_TEMPLATE_PATH.read_text()
    assert "emptyDir.sizeLimit" in text
    assert "NOT enforced" in text
    assert "CONFIRMED" in text


def test_live_verification_harness_exists_and_is_executable():
    """issue #179: the turnkey harness that actually answers the live-cluster
    questions must exist and be runnable, not just documented."""
    assert VERIFY_HARNESS_PATH.is_file()
    assert os.access(VERIFY_HARNESS_PATH, os.X_OK)


def test_kata_template_points_to_the_live_verification_harness():
    """Drift-guard: the template header must tell operators how to verify
    (keeps the template and scripts/verify-kata-live.sh from silently
    drifting apart, the same discipline as the parity checks above)."""
    assert "verify-kata-live.sh" in KATA_TEMPLATE_PATH.read_text()


def test_kata_template_sidecar_capabilities_match_pod_template():
    """Same capability grant as deploy/pod-template.yaml's sidecar --
    docs/KATA-CONTAINERS-SCOPING.md §3 found no documented incompatibility
    with any of these being ordinary OCI runtime-spec fields under Kata."""
    sidecar = _kata_template_container("sidecar")
    caps = sidecar["securityContext"]["capabilities"]
    assert set(caps["add"]) == {"SYS_PTRACE", "SYS_ADMIN", "CHOWN", "SYS_CHROOT", "SETUID", "SETGID"}
    assert set(caps["drop"]) == {"ALL"}


def test_kata_template_sandbox_resources_match_resource_config_defaults():
    resources = _kata_template_container("sandbox")["resources"]
    assert resources["requests"]["cpu"] == resource_config.DEFAULT_SANDBOX_CONTAINER_CPU_REQUEST
    assert resources["requests"]["memory"] == resource_config.DEFAULT_SANDBOX_CONTAINER_MEMORY_REQUEST
    assert resources["limits"]["cpu"] == resource_config.DEFAULT_SANDBOX_CONTAINER_CPU_LIMIT
    assert resources["limits"]["memory"] == resource_config.DEFAULT_SANDBOX_CONTAINER_MEMORY_LIMIT


def test_kata_template_sidecar_resources_match_resource_config_defaults():
    resources = _kata_template_container("sidecar")["resources"]
    assert resources["requests"]["cpu"] == resource_config.DEFAULT_SANDBOX_SIDECAR_CPU_REQUEST
    assert resources["requests"]["memory"] == resource_config.DEFAULT_SANDBOX_SIDECAR_MEMORY_REQUEST
    assert resources["limits"]["cpu"] == resource_config.DEFAULT_SANDBOX_SIDECAR_CPU_LIMIT
    assert resources["limits"]["memory"] == resource_config.DEFAULT_SANDBOX_SIDECAR_MEMORY_LIMIT


def test_kata_template_volume_size_limits_match_resource_config_defaults():
    doc = _kata_template_doc()
    limits = {
        v["name"]: v["emptyDir"]["sizeLimit"] for v in doc["spec"]["volumes"] if "emptyDir" in v
    }
    assert limits["workspace"] == resource_config.DEFAULT_SANDBOX_WORKSPACE_VOLUME_SIZE_LIMIT
    assert limits["uploads"] == resource_config.DEFAULT_SANDBOX_UPLOADS_VOLUME_SIZE_LIMIT
    assert limits["outputs"] == resource_config.DEFAULT_SANDBOX_OUTPUTS_VOLUME_SIZE_LIMIT
    assert limits["skills"] == resource_config.DEFAULT_SANDBOX_SKILLS_VOLUME_SIZE_LIMIT
    assert limits["tmp"] == resource_config.DEFAULT_SANDBOX_TMP_VOLUME_SIZE_LIMIT


def test_kata_template_sidecar_auth_token_uses_secret_key_ref_not_a_literal_value():
    sidecar = _kata_template_container("sidecar")
    env_by_name = {e["name"]: e for e in sidecar["env"]}
    env_var = env_by_name["SIDECAR_AUTH_TOKEN"]
    assert "value" not in env_var
    assert env_var["valueFrom"]["secretKeyRef"]["key"] == "token"


def test_kata_template_probes_use_https_scheme():
    sidecar = _kata_template_container("sidecar")
    assert sidecar["livenessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert sidecar["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"


# ── resource_config.py's opt-in flag itself ─────────────────────────────


@pytest.fixture(autouse=True)
def _clean_kata_env(monkeypatch):
    monkeypatch.delenv(resource_config.BOXKITE_KATA_RUNTIME_CLASS_ENABLED_ENV, raising=False)
    monkeypatch.delenv(resource_config.SANDBOX_KATA_RUNTIME_CLASS_NAME_ENV, raising=False)


def test_kata_runtime_class_name_is_none_by_default():
    assert resource_config.kata_runtime_class_enabled() is False
    assert resource_config.kata_runtime_class_name() is None


def test_kata_runtime_class_name_defaults_to_kata_when_enabled(monkeypatch):
    monkeypatch.setenv(resource_config.BOXKITE_KATA_RUNTIME_CLASS_ENABLED_ENV, "true")
    assert resource_config.kata_runtime_class_enabled() is True
    assert resource_config.kata_runtime_class_name() == "kata"


def test_kata_runtime_class_name_respects_override(monkeypatch):
    monkeypatch.setenv(resource_config.BOXKITE_KATA_RUNTIME_CLASS_ENABLED_ENV, "true")
    monkeypatch.setenv(resource_config.SANDBOX_KATA_RUNTIME_CLASS_NAME_ENV, "kata-fc")
    assert resource_config.kata_runtime_class_name() == "kata-fc"


def test_manager_py_and_warm_pool_py_reference_kata_runtime_class_name():
    """Regression test for the two pod-spec-building call sites staying in
    sync with the opt-in flag -- both must set runtime_class_name from the
    same resource_config helper, not a hardcoded/independent value."""
    manager_source = (Path(__file__).resolve().parent.parent / "src" / "boxkite" / "manager.py").read_text()
    warm_pool_source = (Path(__file__).resolve().parent.parent / "src" / "boxkite" / "warm_pool.py").read_text()
    assert "runtime_class_name=kata_runtime_class_name()" in manager_source
    assert "runtime_class_name=kata_runtime_class_name()" in warm_pool_source
