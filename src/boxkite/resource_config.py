"""Kubernetes resource configuration for sandbox pods."""

import os
from dataclasses import dataclass

from kubernetes_asyncio import client


SANDBOX_CONTAINER_CPU_REQUEST_ENV = "SANDBOX_CONTAINER_CPU_REQUEST"
SANDBOX_CONTAINER_MEMORY_REQUEST_ENV = "SANDBOX_CONTAINER_MEMORY_REQUEST"
SANDBOX_CONTAINER_CPU_LIMIT_ENV = "SANDBOX_CONTAINER_CPU_LIMIT"
SANDBOX_CONTAINER_MEMORY_LIMIT_ENV = "SANDBOX_CONTAINER_MEMORY_LIMIT"

SANDBOX_SIDECAR_CPU_REQUEST_ENV = "SANDBOX_SIDECAR_CPU_REQUEST"
SANDBOX_SIDECAR_MEMORY_REQUEST_ENV = "SANDBOX_SIDECAR_MEMORY_REQUEST"
SANDBOX_SIDECAR_CPU_LIMIT_ENV = "SANDBOX_SIDECAR_CPU_LIMIT"
SANDBOX_SIDECAR_MEMORY_LIMIT_ENV = "SANDBOX_SIDECAR_MEMORY_LIMIT"
SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV = "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED"

SANDBOX_WORKSPACE_VOLUME_SIZE_LIMIT_ENV = "SANDBOX_WORKSPACE_VOLUME_SIZE_LIMIT"
SANDBOX_UPLOADS_VOLUME_SIZE_LIMIT_ENV = "SANDBOX_UPLOADS_VOLUME_SIZE_LIMIT"
SANDBOX_OUTPUTS_VOLUME_SIZE_LIMIT_ENV = "SANDBOX_OUTPUTS_VOLUME_SIZE_LIMIT"
SANDBOX_SKILLS_VOLUME_SIZE_LIMIT_ENV = "SANDBOX_SKILLS_VOLUME_SIZE_LIMIT"
SANDBOX_TMP_VOLUME_SIZE_LIMIT_ENV = "SANDBOX_TMP_VOLUME_SIZE_LIMIT"

SANDBOX_MAX_VOLUME_SIZE_LIMIT_GI_ENV = "SANDBOX_MAX_VOLUME_SIZE_LIMIT_GI"
SANDBOX_MAX_ACTIVE_DEADLINE_SECONDS_ENV = "SANDBOX_MAX_ACTIVE_DEADLINE_SECONDS"


# WHERE AN AGENT'S CODE IS ACTUALLY CHARGED (memory-limit enforcement).
#
# Execution is sidecar-launched: `/exec` starts commands from the sidecar,
# enters the sandbox mount/PID namespaces via nsenter, and drops to the
# sandbox UID/GID. That gives user code the sandbox filesystem/tooling image
# and the *appearance* of the sandbox container's cgroup -- a process that
# reads /sys/fs/cgroup/memory.max sees the SANDBOX container's value -- but
# the process's real cgroup accounting stays with the SIDECAR container
# (nsenter changes namespaces, not cgroup membership). Verified against the
# live cluster: a "small" sandbox (sandbox cgroup 128Mi, sidecar cgroup
# 512Mi) allocated 400MB fine and only OOM-killed at ~700MB -- i.e. bound by
# the 512Mi SIDECAR limit, NOT the 128Mi sandbox limit the process reads.
#
# Consequence: the SIDECAR container's memory/CPU limit IS the enforced,
# usable per-sandbox budget for agent code. So the per-size budget below is
# carried on the sidecar (that's what OOM-kills runaway code), and the
# sandbox container keeps only a modest floor for its namespace-holding
# `tail -f /dev/null` PID1. Moving enforcement onto the sandbox container's
# own cgroup would require the sidecar-owned exec path to migrate the exec'd
# process into the sandbox cgroup (nsenter --cgroup + a cgroup write); until
# that lands, sizing the sidecar is the only real lever. Reserve ~128-256Mi
# of the sidecar budget for the sidecar server itself (uvicorn + flush
# buffers) -- the remainder is what agent code can use before OOM.
DEFAULT_SANDBOX_CONTAINER_CPU_REQUEST = "25m"
DEFAULT_SANDBOX_CONTAINER_MEMORY_REQUEST = "64Mi"
DEFAULT_SANDBOX_CONTAINER_CPU_LIMIT = "250m"
DEFAULT_SANDBOX_CONTAINER_MEMORY_LIMIT = "256Mi"

# "small" enforced (usable) budget: ~1Gi memory / 1 CPU, carried on the
# sidecar per the note above. 512Mi was unrealistically tight for real agent
# workloads (a pip install + a language runtime routinely exceeds it).
DEFAULT_SANDBOX_SIDECAR_CPU_REQUEST = "100m"
DEFAULT_SANDBOX_SIDECAR_MEMORY_REQUEST = "256Mi"
DEFAULT_SANDBOX_SIDECAR_CPU_LIMIT = "1000m"
DEFAULT_SANDBOX_SIDECAR_MEMORY_LIMIT = "1Gi"

# Matches deploy/pod-template.yaml (see test_pod_template_parity.py). Without
# an explicit sizeLimit, an emptyDir volume is bounded only by the node's
# backing disk -- one sandbox tenant writing until the node fills up can
# trigger kubelet eviction of co-located pods (a cross-tenant DoS lever), so
# every sandbox emptyDir needs one.
DEFAULT_SANDBOX_WORKSPACE_VOLUME_SIZE_LIMIT = "5Gi"
DEFAULT_SANDBOX_UPLOADS_VOLUME_SIZE_LIMIT = "5Gi"
DEFAULT_SANDBOX_OUTPUTS_VOLUME_SIZE_LIMIT = "5Gi"
DEFAULT_SANDBOX_SKILLS_VOLUME_SIZE_LIMIT = "5Gi"
DEFAULT_SANDBOX_TMP_VOLUME_SIZE_LIMIT = "1Gi"

# A ceiling on the "storage" a caller can request via SandboxManager.create_session's
# storage_limit param (applied to workspace/uploads/outputs/skills, not tmp -- tmp is
# transient scratch space, not caller-requested storage). Same DoS rationale as the
# volume size limits above: one tenant's oversized emptyDir can evict co-located pods.
DEFAULT_SANDBOX_MAX_VOLUME_SIZE_LIMIT_GI = "20"

# Ceiling on SandboxManager.create_session's lifetime_seconds override. Defaults to the
# same value as manager.py's SANDBOX_ACTIVE_DEADLINE_SECONDS today, so a caller can
# shorten a sandbox's life but not lengthen it beyond the current fixed behavior unless
# an operator raises this explicitly.
DEFAULT_SANDBOX_MAX_ACTIVE_DEADLINE_SECONDS = "86400"


@dataclass(frozen=True)
class SandboxSizeSpec:
    container_cpu_request: str
    container_memory_request: str
    container_cpu_limit: str
    container_memory_limit: str
    sidecar_cpu_request: str
    sidecar_memory_request: str
    sidecar_cpu_limit: str
    sidecar_memory_limit: str


# "small" mirrors the DEFAULT_SANDBOX_CONTAINER_*/DEFAULT_SANDBOX_SIDECAR_* constants
# above exactly, so build_sandbox_container_resources()/build_sidecar_container_resources()
# called with no size argument stay byte-identical to the pre-sizing behavior --
# deploy/pod-template.yaml and test_pod_template_parity.py assert against those constants
# directly and must not need to change.
SANDBOX_SIZE_PRESETS: dict[str, SandboxSizeSpec] = {
    "small": SandboxSizeSpec(
        container_cpu_request=DEFAULT_SANDBOX_CONTAINER_CPU_REQUEST,
        container_memory_request=DEFAULT_SANDBOX_CONTAINER_MEMORY_REQUEST,
        container_cpu_limit=DEFAULT_SANDBOX_CONTAINER_CPU_LIMIT,
        container_memory_limit=DEFAULT_SANDBOX_CONTAINER_MEMORY_LIMIT,
        sidecar_cpu_request=DEFAULT_SANDBOX_SIDECAR_CPU_REQUEST,
        sidecar_memory_request=DEFAULT_SANDBOX_SIDECAR_MEMORY_REQUEST,
        sidecar_cpu_limit=DEFAULT_SANDBOX_SIDECAR_CPU_LIMIT,
        sidecar_memory_limit=DEFAULT_SANDBOX_SIDECAR_MEMORY_LIMIT,
    ),
    # "medium"/"large" enforced (usable) memory is the SIDECAR limit (2Gi /
    # 4Gi) -- that's the cgroup agent code actually runs in (see the module
    # note above). The sandbox container keeps only a modest namespace-holder
    # floor, so a "medium" pod reserves ~2.5Gi total, not double-counted GBs.
    "medium": SandboxSizeSpec(
        container_cpu_request="50m",
        container_memory_request="128Mi",
        container_cpu_limit="500m",
        container_memory_limit="512Mi",
        sidecar_cpu_request="250m",
        sidecar_memory_request="512Mi",
        sidecar_cpu_limit="2000m",
        sidecar_memory_limit="2Gi",
    ),
    "large": SandboxSizeSpec(
        container_cpu_request="100m",
        container_memory_request="256Mi",
        container_cpu_limit="1000m",
        container_memory_limit="1Gi",
        sidecar_cpu_request="500m",
        sidecar_memory_request="1Gi",
        sidecar_cpu_limit="4000m",
        sidecar_memory_limit="4Gi",
    ),
}

DEFAULT_SANDBOX_SIZE = "small"

# Ordering (not a set) so callers can express "at least medium"-style resource
# floors -- see size_at_least() below and docs/BROWSER-EXEC-DESIGN.md §4's
# recommended browser-tool resource floor.
SANDBOX_SIZE_ORDER: tuple[str, ...] = ("small", "medium", "large")


def size_at_least(size: str, minimum: str) -> bool:
    return SANDBOX_SIZE_ORDER.index(size) >= SANDBOX_SIZE_ORDER.index(minimum)

# Shared label so warm_pool.py (producer) and manager.py (consumer) agree on
# how a warm pod's size is recorded/matched. Pods created before this label
# existed have none -- both sides treat a missing label as DEFAULT_SANDBOX_SIZE.
SANDBOX_SIZE_LABEL = "sandbox.boxkite.dev/size"


def _env_quantity(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def max_volume_size_limit_gi() -> float:
    return float(
        _env_quantity(SANDBOX_MAX_VOLUME_SIZE_LIMIT_GI_ENV, DEFAULT_SANDBOX_MAX_VOLUME_SIZE_LIMIT_GI)
    )


def max_active_deadline_seconds() -> int:
    return int(
        _env_quantity(SANDBOX_MAX_ACTIVE_DEADLINE_SECONDS_ENV, DEFAULT_SANDBOX_MAX_ACTIVE_DEADLINE_SECONDS)
    )


def sandbox_exec_network_isolation_enabled() -> bool:
    return _env_flag(SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV, "true")


def build_sidecar_exec_network_isolation_env() -> client.V1EnvVar:
    return client.V1EnvVar(
        name=SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV,
        value="true" if sandbox_exec_network_isolation_enabled() else "false",
    )


# Opt-in Kata Containers RuntimeClass (docs/KATA-CONTAINERS-SCOPING.md).
#
# STATUS: implemented against the real Kubernetes API shape (a pod's
# runtimeClassName field), NEVER exercised against a live Kata-enabled
# cluster -- same "implemented against the real API shape, never
# exercised against the real live service" honesty this project already
# applies to K8sVolumeProvisioner/KanikoJobBuildRunner. The scoping doc's
# own §3 flags one specific, concrete, unverified risk before this can be
# called supported rather than experimental: Kata's documented
# block-backed emptyDir modes do not honor Kubernetes' emptyDir.sizeLimit
# -- if the default (non-block) backend behaves the same way, every
# cross-tenant DoS control resource_config.py's own
# DEFAULT_SANDBOX_*_VOLUME_SIZE_LIMIT constants provide would silently
# stop working the moment this flag is enabled. Off by default; enabling
# it is an explicit operator opt-in onto an experimental configuration,
# not a supported one, until that's verified against a real cluster.
BOXKITE_KATA_RUNTIME_CLASS_ENABLED_ENV = "BOXKITE_KATA_RUNTIME_CLASS_ENABLED"
SANDBOX_KATA_RUNTIME_CLASS_NAME_ENV = "SANDBOX_KATA_RUNTIME_CLASS_NAME"
DEFAULT_SANDBOX_KATA_RUNTIME_CLASS_NAME = "kata"


def kata_runtime_class_enabled() -> bool:
    return _env_flag(BOXKITE_KATA_RUNTIME_CLASS_ENABLED_ENV, "false")


# Opt-in fast warm-pod claim path (docs/BENCHMARKS.md, issue #178 follow-up).
#
# When on, SandboxManager._claim_warm_pod_via_k8s pops a candidate from the
# WarmPoolManager's in-memory ready-pod index (populated off the existing
# background scan) instead of issuing a per-request list_namespaced_pod, and
# seeds the sidecar auth token / TLS cert from that index entry instead of
# reading the pod's Secret on the hot path -- removing the LIST and the
# Secret-READ round-trips from the claim. The compare-and-swap label patch
# still happens on the hot path and remains the source of truth, so the
# index is only a hint: a stale/lost entry fails the CAS and falls back to
# the unchanged list-based path. Off by default; flag-OFF leaves the claim
# path byte-identical to the pre-fast-claim behavior.
BOXKITE_FAST_CLAIM_ENABLED_ENV = "BOXKITE_FAST_CLAIM_ENABLED"


def fast_claim_enabled() -> bool:
    return _env_flag(BOXKITE_FAST_CLAIM_ENABLED_ENV, "false")


# Opt-in GPU support (docs/GPU-SUPPORT-SCOPING.md).
#
# STATUS: implemented against the real Kubernetes GPU-scheduling API shape
# (an extended resource limit, e.g. nvidia.com/gpu), NEVER exercised
# against a live GPU-equipped cluster or a real NVIDIA/AMD device plugin --
# same "implemented against the real API shape, never exercised against
# the real live service" honesty this project already applies to
# K8sVolumeProvisioner/KanikoJobBuildRunner and kata_runtime_class_name
# above. The scoping doc's own §3 flags a real, unverified question this
# does NOT resolve: whether GPU VRAM is reliably wiped between sandbox
# sessions reusing the same physical GPU -- a cross-tenant information-leak
# vector this project's CPU/memory isolation model has never had to reason
# about. Off by default; enabling it is an explicit operator opt-in onto
# an experimental configuration, not a supported one, until that's
# verified against real hardware. The device plugin DaemonSet
# (github.com/NVIDIA/k8s-device-plugin or the AMD equivalent) and a
# GPU-equipped node pool must already exist on the cluster -- this only
# sets the pod-spec field requesting the resource, the same division of
# responsibility kata_runtime_class_name has for its own RuntimeClass
# object.
BOXKITE_GPU_ENABLED_ENV = "BOXKITE_GPU_ENABLED"
GPU_RESOURCE_NAME_ENV = "BOXKITE_GPU_RESOURCE_NAME"
DEFAULT_GPU_RESOURCE_NAME = "nvidia.com/gpu"
# Ceiling on a single session's gpu_count -- a DoS control, same rationale
# as max_volume_size_limit_gi() (one tenant should not be able to request
# an unbounded slice of a shared, scarce, physically-limited resource).
BOXKITE_MAX_GPU_COUNT_PER_SESSION_ENV = "BOXKITE_MAX_GPU_COUNT_PER_SESSION"
DEFAULT_MAX_GPU_COUNT_PER_SESSION = "1"


def gpu_enabled() -> bool:
    return _env_flag(BOXKITE_GPU_ENABLED_ENV, "false")


def gpu_resource_name() -> str:
    """The Kubernetes extended-resource name to request, e.g.
    'nvidia.com/gpu' (default) or 'amd.com/gpu' for an AMD device-plugin
    deployment -- operator-configured, not caller-supplied, so a session
    request can never smuggle in an arbitrary extended-resource name."""
    return _env_quantity(GPU_RESOURCE_NAME_ENV, DEFAULT_GPU_RESOURCE_NAME)


def max_gpu_count_per_session() -> int:
    return int(
        _env_quantity(BOXKITE_MAX_GPU_COUNT_PER_SESSION_ENV, DEFAULT_MAX_GPU_COUNT_PER_SESSION)
    )


# Opt-in full-state (process/memory) checkpoint (docs/FULL-STATE-SNAPSHOT-SCOPING.md).
#
# STATUS: implemented against the real Kubernetes kubelet checkpoint API
# shape (KEP-2008's alpha ContainerCheckpoint feature), NEVER exercised
# against a live cluster with that feature gate enabled. Forensic-only --
# NOT a pause/resume mechanism; see src/boxkite/checkpoint_backend.py's
# module docstring for the full disclosure, including the real new RBAC
# grant (nodes/proxy) this requires and why it's a meaningfully bigger
# privilege expansion than this project's other opt-in flags. Off by
# default; enabling it requires both this flag AND applying the separate
# deploy/full-state-snapshot-rbac-optin.yaml manifest -- one without the
# other either does nothing (flag off) or grants unused RBAC (manifest
# applied, flag off).
BOXKITE_FULL_STATE_SNAPSHOT_ENABLED_ENV = "BOXKITE_FULL_STATE_SNAPSHOT_ENABLED"


def full_state_snapshot_enabled() -> bool:
    return _env_flag(BOXKITE_FULL_STATE_SNAPSHOT_ENABLED_ENV, "false")


def kata_runtime_class_name() -> str | None:
    """The runtimeClassName to set on sandbox/warm pods, or None to omit
    the field entirely (the default -- ordinary runc, no behavior change
    for every existing deployment). Only non-None when the operator has
    explicitly opted in; the RuntimeClass object itself
    (`kind: RuntimeClass`, handler: kata) must already exist on the
    cluster -- this only sets the pod-spec field referencing it, the same
    division of responsibility SIDECAR_AUTH_TOKEN's secretKeyRef has
    between "this code references a name" and "an operator provisions the
    thing that name points at"."""
    if not kata_runtime_class_enabled():
        return None
    return _env_quantity(SANDBOX_KATA_RUNTIME_CLASS_NAME_ENV, DEFAULT_SANDBOX_KATA_RUNTIME_CLASS_NAME)


def _resolve_size_spec(size: str) -> SandboxSizeSpec:
    try:
        return SANDBOX_SIZE_PRESETS[size]
    except KeyError:
        raise ValueError(
            f"Unknown sandbox size {size!r}; must be one of {sorted(SANDBOX_SIZE_PRESETS)}"
        ) from None


def build_sandbox_container_resources(
    size: str = DEFAULT_SANDBOX_SIZE, gpu_count: int | None = None
) -> client.V1ResourceRequirements:
    spec = _resolve_size_spec(size)
    is_default = size == DEFAULT_SANDBOX_SIZE
    limits = {
        "cpu": (
            _env_quantity(SANDBOX_CONTAINER_CPU_LIMIT_ENV, spec.container_cpu_limit)
            if is_default
            else spec.container_cpu_limit
        ),
        "memory": (
            _env_quantity(SANDBOX_CONTAINER_MEMORY_LIMIT_ENV, spec.container_memory_limit)
            if is_default
            else spec.container_memory_limit
        ),
    }
    if gpu_count is not None:
        # Kubernetes extended resources (docs/GPU-SUPPORT-SCOPING.md) are
        # limits-only and whole-unit -- no fractional/request half the way
        # cpu/memory have one. The scheduler auto-fills an equal request
        # for an extended resource with a limit set but no request, so
        # leaving requests alone here (no gpu key added there) is correct,
        # not an oversight.
        limits[gpu_resource_name()] = str(gpu_count)
    return client.V1ResourceRequirements(
        requests={
            "cpu": (
                _env_quantity(SANDBOX_CONTAINER_CPU_REQUEST_ENV, spec.container_cpu_request)
                if is_default
                else spec.container_cpu_request
            ),
            "memory": (
                _env_quantity(SANDBOX_CONTAINER_MEMORY_REQUEST_ENV, spec.container_memory_request)
                if is_default
                else spec.container_memory_request
            ),
        },
        limits=limits,
    )


def build_sidecar_container_resources(size: str = DEFAULT_SANDBOX_SIZE) -> client.V1ResourceRequirements:
    spec = _resolve_size_spec(size)
    is_default = size == DEFAULT_SANDBOX_SIZE
    return client.V1ResourceRequirements(
        requests={
            "cpu": (
                _env_quantity(SANDBOX_SIDECAR_CPU_REQUEST_ENV, spec.sidecar_cpu_request)
                if is_default
                else spec.sidecar_cpu_request
            ),
            "memory": (
                _env_quantity(SANDBOX_SIDECAR_MEMORY_REQUEST_ENV, spec.sidecar_memory_request)
                if is_default
                else spec.sidecar_memory_request
            ),
        },
        limits={
            "cpu": (
                _env_quantity(SANDBOX_SIDECAR_CPU_LIMIT_ENV, spec.sidecar_cpu_limit)
                if is_default
                else spec.sidecar_cpu_limit
            ),
            "memory": (
                _env_quantity(SANDBOX_SIDECAR_MEMORY_LIMIT_ENV, spec.sidecar_memory_limit)
                if is_default
                else spec.sidecar_memory_limit
            ),
        },
    )


def build_sandbox_pod_volumes(volume_size_limit: str | None = None) -> list[client.V1Volume]:
    """The standard workspace/uploads/outputs/skills/tmp emptyDir volumes
    shared by manager.py's SandboxManager and warm_pool.py's WarmPoolManager
    -- single source of truth for the size limits so the two pod-creation
    paths (and deploy/pod-template.yaml, checked by test_pod_template_parity.py)
    can't drift apart the way they did before every volume had a limit.

    volume_size_limit, when given, overrides workspace/uploads/outputs/skills
    (the caller-visible "storage" a sandbox gets) but not tmp, which is
    transient scratch space rather than caller-requested storage."""
    workspace_limit = volume_size_limit or _env_quantity(
        SANDBOX_WORKSPACE_VOLUME_SIZE_LIMIT_ENV, DEFAULT_SANDBOX_WORKSPACE_VOLUME_SIZE_LIMIT
    )
    uploads_limit = volume_size_limit or _env_quantity(
        SANDBOX_UPLOADS_VOLUME_SIZE_LIMIT_ENV, DEFAULT_SANDBOX_UPLOADS_VOLUME_SIZE_LIMIT
    )
    outputs_limit = volume_size_limit or _env_quantity(
        SANDBOX_OUTPUTS_VOLUME_SIZE_LIMIT_ENV, DEFAULT_SANDBOX_OUTPUTS_VOLUME_SIZE_LIMIT
    )
    skills_limit = volume_size_limit or _env_quantity(
        SANDBOX_SKILLS_VOLUME_SIZE_LIMIT_ENV, DEFAULT_SANDBOX_SKILLS_VOLUME_SIZE_LIMIT
    )
    tmp_limit = _env_quantity(SANDBOX_TMP_VOLUME_SIZE_LIMIT_ENV, DEFAULT_SANDBOX_TMP_VOLUME_SIZE_LIMIT)
    return [
        client.V1Volume(
            name="workspace",
            empty_dir=client.V1EmptyDirVolumeSource(size_limit=workspace_limit),
        ),
        client.V1Volume(
            name="uploads",
            empty_dir=client.V1EmptyDirVolumeSource(size_limit=uploads_limit),
        ),
        client.V1Volume(
            name="outputs",
            empty_dir=client.V1EmptyDirVolumeSource(size_limit=outputs_limit),
        ),
        client.V1Volume(
            name="skills",
            empty_dir=client.V1EmptyDirVolumeSource(size_limit=skills_limit),
        ),
        client.V1Volume(
            name="tmp",
            empty_dir=client.V1EmptyDirVolumeSource(size_limit=tmp_limit),
        ),
    ]
