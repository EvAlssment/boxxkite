"""Tests for High #5: deploy/pod-template.yaml must match manager.py's real pod spec.

manager.py builds pod specs programmatically (that's what actually runs);
pod-template.yaml is a checked-in reference manifest for self-hosters
deploying without embedding SandboxManager. Nothing enforced them staying in
sync, so pod-template.yaml drifted to advertise only SYS_PTRACE while the
real code has always granted SYS_PTRACE + SYS_ADMIN — a self-hoster copying
the template literally would get a broken sidecar (nsenter's setns() needs
CAP_SYS_ADMIN) or silently miss the actual capability grant.

This test reads both sources (the actual pod-spec-building code path, and
the static YAML) and compares them directly, so a future edit to one without
the other fails CI instead of silently drifting again.
"""

import re
from pathlib import Path

import yaml

from boxkite import resource_config
from boxkite.sidecar_auth import SIDECAR_AUTH_SECRET_KEY
from boxkite.tls import (
    SIDECAR_TLS_CERT_FILENAME,
    SIDECAR_TLS_KEY_FILENAME,
    SIDECAR_TLS_MOUNT_PATH,
)

POD_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "deploy" / "pod-template.yaml"
MANAGER_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "boxkite" / "manager.py"
)
WARM_POOL_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "boxkite" / "warm_pool.py"
)


def _sidecar_capabilities_add_from_source(source_text: str) -> set[str]:
    """
    Extract the `capabilities=client.V1Capabilities(add=[...])` list for the
    sidecar container definition.

    manager.py/warm_pool.py each build exactly one sidecar container with a
    security_context; this regex is intentionally narrow (looks for the
    `add=[...]` immediately following a `run_as_user=0` sidecar security
    context) rather than a general Python parser, since the goal is a
    lightweight drift check, not a full pod-spec model.
    """
    match = re.search(
        r"run_as_user=0.*?capabilities=client\.V1Capabilities\(add=\[([^\]]*)\]",
        source_text,
        re.DOTALL,
    )
    assert match, "Could not find sidecar V1Capabilities(add=[...]) in source"
    raw_list = match.group(1)
    return {item.strip().strip('"').strip("'") for item in raw_list.split(",") if item.strip()}


def _sidecar_capabilities_drop_from_source(source_text: str) -> set[str]:
    """
    Extract the `drop=[...]` list from the sidecar container's
    V1Capabilities(...) call. Regression test for the sidecar pod spec
    granting SYS_PTRACE/SYS_ADMIN via `add=[...]` with no accompanying
    `drop=["ALL"]` -- without an explicit drop, Kubernetes leaves the
    container runtime's full default capability set in place *in addition
    to* SYS_PTRACE/SYS_ADMIN (CAP_NET_RAW, CAP_SETUID, CAP_DAC_OVERRIDE,
    etc.), silently granting the "near-root" sidecar container a broader
    capability set than deploy/pod-template.yaml documents and reviewers
    have signed off on.
    """
    match = re.search(
        r"run_as_user=0.*?capabilities=client\.V1Capabilities\([^)]*drop=\[([^\]]*)\]",
        source_text,
        re.DOTALL,
    )
    assert match, "Could not find sidecar V1Capabilities(..., drop=[...]) in source"
    raw_list = match.group(1)
    return {item.strip().strip('"').strip("'") for item in raw_list.split(",") if item.strip()}


def _pod_template_sidecar_capabilities_add() -> set[str]:
    text = POD_TEMPLATE_PATH.read_text()
    doc = yaml.safe_load(text)
    for container in doc["spec"]["containers"]:
        if container["name"] == "sidecar":
            return set(container["securityContext"]["capabilities"]["add"])
    raise AssertionError("No 'sidecar' container found in pod-template.yaml")


def _pod_template_sidecar_capabilities_drop() -> set[str]:
    text = POD_TEMPLATE_PATH.read_text()
    doc = yaml.safe_load(text)
    for container in doc["spec"]["containers"]:
        if container["name"] == "sidecar":
            return set(container["securityContext"]["capabilities"]["drop"])
    raise AssertionError("No 'sidecar' container found in pod-template.yaml")


def test_manager_py_grants_sys_ptrace_and_sys_admin():
    caps = _sidecar_capabilities_add_from_source(MANAGER_SOURCE_PATH.read_text())
    assert caps == {"SYS_PTRACE", "SYS_ADMIN", "CHOWN", "SYS_CHROOT", "SETUID", "SETGID"}


def test_warm_pool_py_grants_sys_ptrace_and_sys_admin():
    caps = _sidecar_capabilities_add_from_source(WARM_POOL_SOURCE_PATH.read_text())
    assert caps == {"SYS_PTRACE", "SYS_ADMIN", "CHOWN", "SYS_CHROOT", "SETUID", "SETGID"}


def test_manager_py_drops_all_before_adding_sidecar_capabilities():
    assert _sidecar_capabilities_drop_from_source(MANAGER_SOURCE_PATH.read_text()) == {"ALL"}


def test_warm_pool_py_drops_all_before_adding_sidecar_capabilities():
    assert _sidecar_capabilities_drop_from_source(WARM_POOL_SOURCE_PATH.read_text()) == {"ALL"}


def test_pod_template_matches_manager_capabilities():
    template_caps = _pod_template_sidecar_capabilities_add()
    manager_caps = _sidecar_capabilities_add_from_source(MANAGER_SOURCE_PATH.read_text())
    assert template_caps == manager_caps, (
        "deploy/pod-template.yaml's sidecar capabilities have drifted from "
        "manager.py's actual pod spec — keep them in sync (see the comment "
        "on the sidecar securityContext in pod-template.yaml)."
    )


def test_pod_template_matches_manager_capabilities_drop():
    template_drop = _pod_template_sidecar_capabilities_drop()
    manager_drop = _sidecar_capabilities_drop_from_source(MANAGER_SOURCE_PATH.read_text())
    assert template_drop == manager_drop == {"ALL"}, (
        "deploy/pod-template.yaml's sidecar capabilities.drop has drifted from "
        "manager.py's actual pod spec -- keep them in sync."
    )


_SECCOMP_RUNTIME_DEFAULT_RE = re.compile(
    r'seccomp_profile=client\.V1SeccompProfile\(\s*type=["\']RuntimeDefault["\']\s*\)'
)


def _seccomp_runtime_default_count_in_source(source_text: str) -> int:
    """Count `seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault")`
    occurrences -- one per container security context (sandbox + sidecar)."""
    return len(_SECCOMP_RUNTIME_DEFAULT_RE.findall(source_text))


def _pod_template_container_seccomp_type(container_name: str) -> str | None:
    doc = yaml.safe_load(POD_TEMPLATE_PATH.read_text())
    for container in doc["spec"]["containers"]:
        if container["name"] == container_name:
            return (
                container.get("securityContext", {})
                .get("seccompProfile", {})
                .get("type")
            )
    raise AssertionError(f"No '{container_name}' container found in pod-template.yaml")


def test_manager_py_sets_seccomp_runtime_default_on_both_containers():
    """Regression test for Issue #10: pod-template.yaml advertised
    seccompProfile: RuntimeDefault but manager.py's real pod spec set none,
    so runtime pods ran with the unconfined seccomp profile the template
    claimed to constrain. Both the sandbox and sidecar security contexts
    must set it."""
    assert _seccomp_runtime_default_count_in_source(MANAGER_SOURCE_PATH.read_text()) == 2


def test_warm_pool_py_sets_seccomp_runtime_default_on_both_containers():
    assert _seccomp_runtime_default_count_in_source(WARM_POOL_SOURCE_PATH.read_text()) == 2


def test_pod_template_sets_seccomp_runtime_default_on_both_containers():
    assert _pod_template_container_seccomp_type("sandbox") == "RuntimeDefault"
    assert _pod_template_container_seccomp_type("sidecar") == "RuntimeDefault"


def test_pod_template_documents_sys_admin_residual_risk():
    text = POD_TEMPLATE_PATH.read_text()
    assert "CAP_SYS_ADMIN" in text
    assert "residual risk" in text.lower() or "near-root" in text.lower()


def _pod_template_container_resources(container_name: str) -> dict:
    doc = yaml.safe_load(POD_TEMPLATE_PATH.read_text())
    for container in doc["spec"]["containers"]:
        if container["name"] == container_name:
            return container["resources"]
    raise AssertionError(f"No '{container_name}' container found in pod-template.yaml")


def test_pod_template_sandbox_resources_match_resource_config_defaults():
    """
    Regression test for the ~4-13x request/limit mismatch found during
    hosted-tier cost scoping: pod-template.yaml previously requested 512Mi/
    250m and limited 4Gi/2 cores for the sandbox container, while
    resource_config.py's actual runtime defaults (used whenever no
    SANDBOX_CONTAINER_*_ENV override is set) are 64Mi/25m request and
    128Mi/150m limit. A self-hoster sizing a cluster off this template, or
    anyone quoting real per-sandbox cost, would be badly misled.
    """
    resources = _pod_template_container_resources("sandbox")
    assert resources["requests"]["cpu"] == resource_config.DEFAULT_SANDBOX_CONTAINER_CPU_REQUEST
    assert (
        resources["requests"]["memory"] == resource_config.DEFAULT_SANDBOX_CONTAINER_MEMORY_REQUEST
    )
    assert resources["limits"]["cpu"] == resource_config.DEFAULT_SANDBOX_CONTAINER_CPU_LIMIT
    assert resources["limits"]["memory"] == resource_config.DEFAULT_SANDBOX_CONTAINER_MEMORY_LIMIT


def test_pod_template_sidecar_resources_match_resource_config_defaults():
    resources = _pod_template_container_resources("sidecar")
    assert resources["requests"]["cpu"] == resource_config.DEFAULT_SANDBOX_SIDECAR_CPU_REQUEST
    assert (
        resources["requests"]["memory"] == resource_config.DEFAULT_SANDBOX_SIDECAR_MEMORY_REQUEST
    )
    assert resources["limits"]["cpu"] == resource_config.DEFAULT_SANDBOX_SIDECAR_CPU_LIMIT
    assert resources["limits"]["memory"] == resource_config.DEFAULT_SANDBOX_SIDECAR_MEMORY_LIMIT


def _pod_template_volume_size_limits() -> dict[str, str]:
    doc = yaml.safe_load(POD_TEMPLATE_PATH.read_text())
    return {
        v["name"]: v["emptyDir"]["sizeLimit"]
        for v in doc["spec"]["volumes"]
        if "emptyDir" in v and "sizeLimit" in v["emptyDir"]
    }


def test_pod_template_volume_size_limits_match_resource_config_defaults():
    """
    Regression test: manager.py/warm_pool.py used to build workspace/
    uploads/outputs/skills as emptyDir volumes with NO size_limit at all
    (only tmp had one), while pod-template.yaml documented 5Gi on all four
    -- giving a false sense that disk was bounded everywhere. Both paths now
    go through resource_config.build_sandbox_pod_volumes(), so this checks
    the template and the runtime defaults it's meant to match stay in sync.
    """
    limits = _pod_template_volume_size_limits()
    assert limits["workspace"] == resource_config.DEFAULT_SANDBOX_WORKSPACE_VOLUME_SIZE_LIMIT
    assert limits["uploads"] == resource_config.DEFAULT_SANDBOX_UPLOADS_VOLUME_SIZE_LIMIT
    assert limits["outputs"] == resource_config.DEFAULT_SANDBOX_OUTPUTS_VOLUME_SIZE_LIMIT
    assert limits["skills"] == resource_config.DEFAULT_SANDBOX_SKILLS_VOLUME_SIZE_LIMIT
    assert limits["tmp"] == resource_config.DEFAULT_SANDBOX_TMP_VOLUME_SIZE_LIMIT


def test_build_sandbox_pod_volumes_sets_a_size_limit_on_every_volume():
    volumes = resource_config.build_sandbox_pod_volumes()
    assert {v.name for v in volumes} == {"workspace", "uploads", "outputs", "skills", "tmp"}
    for volume in volumes:
        assert volume.empty_dir.size_limit, f"{volume.name} has no emptyDir size_limit"


def _pod_template_sidecar_auth_token_env_var() -> dict:
    doc = yaml.safe_load(POD_TEMPLATE_PATH.read_text())
    for container in doc["spec"]["containers"]:
        if container["name"] != "sidecar":
            continue
        for env_var in container["env"]:
            if env_var["name"] == "SIDECAR_AUTH_TOKEN":
                return env_var
    raise AssertionError("No SIDECAR_AUTH_TOKEN env var found on the sidecar container")


def test_pod_template_sidecar_auth_token_uses_secret_key_ref_not_a_literal_value():
    """
    Regression test: this env var must be sourced via secretKeyRef, matching
    the real runtime code (manager.py/warm_pool.py create a per-pod Secret
    and reference it this way) -- never a literal `value:`. A literal value
    here would be readable by anything with mere `pods: get` RBAC (the pod
    spec, same as the plaintext annotation this design replaced), which
    defeats the entire point of requiring the separate `secrets: get` grant
    (see deploy/rbac.yaml). A self-hoster who copies this template verbatim
    without creating the referenced Secret gets a pod that fails to start
    (missing secretKeyRef target) -- a loud failure, not a silently-working
    guessable credential.
    """
    env_var = _pod_template_sidecar_auth_token_env_var()
    assert "value" not in env_var, (
        "SIDECAR_AUTH_TOKEN must not be a literal value in pod-template.yaml -- "
        "use valueFrom.secretKeyRef instead (see the comment above this env var)."
    )
    assert env_var["valueFrom"]["secretKeyRef"]["key"] == SIDECAR_AUTH_SECRET_KEY


# =============================================================================
# Manager-to-sidecar TLS (docs/SIDECAR-TRANSPORT-TLS-DESIGN.md) parity
# =============================================================================


def _pod_template_sidecar_container() -> dict:
    doc = yaml.safe_load(POD_TEMPLATE_PATH.read_text())
    for container in doc["spec"]["containers"]:
        if container["name"] == "sidecar":
            return container
    raise AssertionError("No 'sidecar' container found in pod-template.yaml")


def test_pod_template_probes_use_https_scheme_matching_manager_default():
    """manager.py's/warm_pool.py's real pod spec serves the sidecar over
    HTTPS by default (tls_enabled True unless SIDECAR_TLS_DISABLED=true) --
    the reference template's probes must match that default, not the old
    plain-HTTP behavior, or a self-hoster copying this file gets a sidecar
    whose probes never succeed against the TLS-terminated /health route."""
    sidecar = _pod_template_sidecar_container()
    assert sidecar["livenessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert sidecar["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"


def test_pod_template_mounts_sidecar_tls_secret_volume():
    sidecar = _pod_template_sidecar_container()
    mount_names = {m["name"] for m in sidecar["volumeMounts"]}
    assert "sidecar-tls" in mount_names

    mount = next(m for m in sidecar["volumeMounts"] if m["name"] == "sidecar-tls")
    assert mount["mountPath"] == SIDECAR_TLS_MOUNT_PATH
    assert mount.get("readOnly") is True


def test_pod_template_sidecar_tls_volume_sources_the_same_secret_as_auth_token():
    """The TLS cert/key volume must reference the SAME Secret name as
    SIDECAR_AUTH_TOKEN's secretKeyRef -- one per-pod Secret, not two."""
    doc = yaml.safe_load(POD_TEMPLATE_PATH.read_text())
    volume = next(v for v in doc["spec"]["volumes"] if v["name"] == "sidecar-tls")

    auth_token_env = _pod_template_sidecar_auth_token_env_var()
    assert volume["secret"]["secretName"] == auth_token_env["valueFrom"]["secretKeyRef"]["name"]

    item_keys = {item["key"]: item["path"] for item in volume["secret"]["items"]}
    assert item_keys.get("tls_cert") == SIDECAR_TLS_CERT_FILENAME
    assert item_keys.get("tls_key") == SIDECAR_TLS_KEY_FILENAME


# =============================================================================
# Helm chart (deploy/helm/boxkite/values.yaml) resource-default parity
# =============================================================================

HELM_VALUES_PATH = (
    Path(__file__).resolve().parent.parent / "deploy" / "helm" / "boxkite" / "values.yaml"
)


def _helm_values() -> dict:
    return yaml.safe_load(HELM_VALUES_PATH.read_text())


def test_helm_values_defaults_match_resource_config_defaults():
    """
    deploy/helm/boxkite/values.yaml's `resources`/`volumeSizeLimits` are
    literal copies of resource_config.py's DEFAULT_SANDBOX_CONTAINER_*/
    DEFAULT_SANDBOX_SIDECAR_*/DEFAULT_SANDBOX_*_VOLUME_SIZE_LIMIT constants
    (Helm can't import the Python module) -- this is the third parity-drift
    guard alongside pod-template.yaml's own tests above, for the same
    ~4-13x-mismatch failure mode found previously.
    """
    values = _helm_values()
    sandbox = values["resources"]["sandboxContainer"]
    sidecar = values["resources"]["sidecarContainer"]

    assert sandbox["cpuRequest"] == resource_config.DEFAULT_SANDBOX_CONTAINER_CPU_REQUEST
    assert sandbox["memoryRequest"] == resource_config.DEFAULT_SANDBOX_CONTAINER_MEMORY_REQUEST
    assert sandbox["cpuLimit"] == resource_config.DEFAULT_SANDBOX_CONTAINER_CPU_LIMIT
    assert sandbox["memoryLimit"] == resource_config.DEFAULT_SANDBOX_CONTAINER_MEMORY_LIMIT

    assert sidecar["cpuRequest"] == resource_config.DEFAULT_SANDBOX_SIDECAR_CPU_REQUEST
    assert sidecar["memoryRequest"] == resource_config.DEFAULT_SANDBOX_SIDECAR_MEMORY_REQUEST
    assert sidecar["cpuLimit"] == resource_config.DEFAULT_SANDBOX_SIDECAR_CPU_LIMIT
    assert sidecar["memoryLimit"] == resource_config.DEFAULT_SANDBOX_SIDECAR_MEMORY_LIMIT

    volumes = values["volumeSizeLimits"]
    assert str(volumes["workspace"]) == resource_config.DEFAULT_SANDBOX_WORKSPACE_VOLUME_SIZE_LIMIT
    assert str(volumes["uploads"]) == resource_config.DEFAULT_SANDBOX_UPLOADS_VOLUME_SIZE_LIMIT
    assert str(volumes["outputs"]) == resource_config.DEFAULT_SANDBOX_OUTPUTS_VOLUME_SIZE_LIMIT
    assert str(volumes["skills"]) == resource_config.DEFAULT_SANDBOX_SKILLS_VOLUME_SIZE_LIMIT
    assert str(volumes["tmp"]) == resource_config.DEFAULT_SANDBOX_TMP_VOLUME_SIZE_LIMIT


def test_pod_template_sidecar_tls_disabled_env_defaults_unset():
    """SIDECAR_TLS_DISABLED must default to empty/unset (TLS on) in the
    reference template, matching manager.py's/warm_pool.py's own default --
    never a literal "true" that would silently ship the template with TLS
    off."""
    sidecar = _pod_template_sidecar_container()
    env_by_name = {e["name"]: e for e in sidecar["env"]}
    assert "SIDECAR_TLS_DISABLED" in env_by_name
    assert env_by_name["SIDECAR_TLS_DISABLED"].get("value", "") == ""
