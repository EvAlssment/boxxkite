"""Module-level configuration, constants, and free helper/validator functions
for SandboxManager. Extracted verbatim from manager.py so that
``boxkite.manager`` re-exports every name unchanged via
``from ._manager_config import *``.
"""

import asyncio
import base64
from collections import OrderedDict
import hashlib
import json
import logging
import os
import re
import ssl
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast
from uuid import UUID

import httpx
from aiohttp.client_exceptions import ClientConnectionError
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from .azure_identity import (
    build_azure_workload_identity_pod_labels,
    build_azure_workload_identity_skip_annotations,
    build_sidecar_azure_storage_env,
)
from .aws_identity import (
    build_aws_web_identity_volume,
    build_pod_identity_webhook_skip_annotations,
    build_sidecar_aws_auth_env,
    build_sidecar_aws_web_identity_volume_mount,
)
from .k8s_auth import build_kubernetes_api_client, load_kubernetes_config
from .pod_claim_policy import compute_max_claimable_age_seconds, pod_age_seconds
from .resource_config import (
    DEFAULT_SANDBOX_SIZE,
    SANDBOX_SIZE_LABEL,
    SANDBOX_SIZE_PRESETS,
    build_sandbox_container_resources,
    build_sandbox_pod_volumes,
    build_sidecar_container_resources,
    build_sidecar_exec_network_isolation_env,
    fast_claim_enabled,
    gpu_enabled,
    kata_runtime_class_name,
    max_active_deadline_seconds,
    max_gpu_count_per_session,
    max_volume_size_limit_gi,
    size_at_least,
)
from .secrets_network_policy import (
    build_secrets_egress_network_policy,
    secrets_egress_policy_name,
)
from .browser_network_policy import (
    build_browser_egress_network_policy,
    browser_egress_policy_name,
)
from .session_store import NoOpSessionMetadataStore, SessionMetadataStore
from .sidecar_auth import (
    SIDECAR_AUTH_HEADER,
    SIDECAR_AUTH_SECRET_KEY,
    SIDECAR_AUTH_TOKEN_ENV,
    generate_sidecar_auth_token,
    sidecar_auth_secret_name,
)
from .tls import (
    SIDECAR_TLS_CERT_FILENAME,
    SIDECAR_TLS_CERT_SECRET_KEY,
    SIDECAR_TLS_KEY_FILENAME,
    SIDECAR_TLS_KEY_SECRET_KEY,
    SIDECAR_TLS_MOUNT_PATH,
    build_pinned_ssl_context,
    build_sidecar_tls_env,
    generate_pod_self_signed_cert,
    sidecar_tls_disabled,
)

logger = logging.getLogger(__name__)

# Configuration
SANDBOX_NAMESPACE = os.environ.get("SANDBOX_NAMESPACE", "default")
SANDBOX_SERVICE_ACCOUNT_NAME = os.environ.get(
    "SANDBOX_SERVICE_ACCOUNT_NAME", "sandbox-service-account"
)
# Image defaults use ACR registry - overridden by ConfigMap in K8s deployment
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "boxkite-sandbox:latest")
SIDECAR_IMAGE = os.environ.get("SIDECAR_IMAGE", "boxkite-sidecar:latest")
SIDECAR_PORT = 8080
REQUEST_TIMEOUT = 120  # seconds
# Backstop deadline: safety net in case the activity-based reaper fails.
# Set high enough to never interfere with normal idle-based reaping.
SANDBOX_ACTIVE_DEADLINE_SECONDS = int(os.environ.get("SANDBOX_ACTIVE_DEADLINE_SECONDS", "86400"))
# Higher than in-cluster deployments need by default: a manager reaching the
# K8s API over the public internet (e.g. out-of-cluster, no VPC peering to
# the pod network) pays real per-call latency on every poll in
# _wait_for_pod_ready, on top of actual pod startup time.
SANDBOX_POD_READY_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_POD_READY_TIMEOUT_SECONDS", "60"))
# Stale pod protection: reject warm pods too close to the activeDeadlineSeconds
# backstop.  With defaults (buffer=3600, min_remaining=60), pods older than 23h
# are skipped at claim time.  See pod_claim_policy.py for the math.
SANDBOX_WARM_CLAIM_AGE_BUFFER_SECONDS = int(
    os.environ.get("SANDBOX_WARM_CLAIM_AGE_BUFFER_SECONDS", "3600")
)
SANDBOX_WARM_MIN_REMAINING_LIFETIME_SECONDS = int(
    os.environ.get("SANDBOX_WARM_MIN_REMAINING_LIFETIME_SECONDS", "60")
)
WARM_POOL_RECYCLE = os.environ.get("WARM_POOL_RECYCLE", "true").lower() == "true"
WARM_POOL_MAX = int(os.environ.get("WARM_POOL_MAX", "15"))
# New attack surface, off by default (same discipline as
# BOXKITE_IMAGE_BUILDER_ENABLED/BOXKITE_AGENT_PTY_ENABLED): provisions one
# dynamically-scoped NetworkPolicy per session, granting sidecar egress to
# exactly that session's granted secrets' allowed_hosts (issue #74,
# docs/SECRETS-DESIGN.md, src/boxkite/secrets_network_policy.py). Requires
# the deploying cluster's CNI to actually enforce NetworkPolicy (see
# deploy/network-policy.yaml's own verification guidance) and RBAC granting
# the manager's ServiceAccount create/get/replace/delete on
# networkpolicies.networking.k8s.io in SANDBOX_NAMESPACE. Off by default so
# enabling it is a deliberate, reviewed operator decision, not a silent
# default -- same as every other new credential/capability path in this
# codebase.
BOXKITE_SECRETS_NETWORK_POLICY_ENABLED = (
    os.environ.get("BOXKITE_SECRETS_NETWORK_POLICY_ENABLED", "false").lower() == "true"
)

# docs/BROWSER-EXEC-DESIGN.md §3 -- the per-session NetworkPolicy scoping
# mechanism for browser-enabled sessions (src/boxkite/browser_network_policy.py).
# Same off-by-default, deliberate-operator-decision posture as
# BOXKITE_SECRETS_NETWORK_POLICY_ENABLED above, but this one gates a
# genuinely riskier grant: broad, non-enumerable public HTTPS/DNS egress
# (minus an unconditional link-local/RFC1918/loopback carve-out), not a
# narrow per-secret host allowlist. Requires the deploying cluster's CNI to
# actually enforce NetworkPolicy -- and, specifically, to enforce
# `ipBlock.except` correctly (see browser_network_policy.py's own
# docstring on that CNI-conformance risk) -- and RBAC granting the
# manager's ServiceAccount create/get/replace/delete on
# networkpolicies.networking.k8s.io in SANDBOX_NAMESPACE, same as the
# secrets-egress flag above.
BOXKITE_BROWSER_NETWORK_POLICY_ENABLED = (
    os.environ.get("BOXKITE_BROWSER_NETWORK_POLICY_ENABLED", "false").lower() == "true"
)


def _secret_configure_fields(
    secret_grants: Optional[list[dict]],
    secret_capability_token: Optional[str],
    secrets_control_plane_url: Optional[str],
) -> dict:
    """Build the `/configure` JSON fields for the secrets broker
    (docs/SECRETS-DESIGN.md §3/4). Omits the fields entirely (rather than
    sending empty ones) when no secrets were granted, so a sidecar/manager
    version skew where one side doesn't know about this feature yet degrades
    to "no secrets available" instead of erroring on an unexpected field."""
    if not secret_grants:
        return {}
    return {
        "secret_names": [g["name"] for g in secret_grants],
        "secret_allowed_hosts": {g["name"]: g["allowed_hosts"] for g in secret_grants},
        "secret_capability_token": secret_capability_token,
        "secrets_control_plane_url": secrets_control_plane_url,
    }


def _validate_sandbox_size(size: str) -> str:
    if size not in SANDBOX_SIZE_PRESETS:
        raise ValueError(f"Unknown sandbox size {size!r}; must be one of {sorted(SANDBOX_SIZE_PRESETS)}")
    return size


def _validate_browser_resource_floor(size: str, browser_enabled: bool) -> None:
    """docs/BROWSER-EXEC-DESIGN.md §4: a real headless Chromium process is
    heavier than 'small''s ~1Gi enforced (sidecar) memory budget can be
    expected to absorb alongside the sidecar server and whatever else the
    session is already doing -- reject rather than silently let an agent OOM
    the pod the first time it navigates anywhere."""
    if browser_enabled and not size_at_least(size, "medium"):
        raise ValueError(
            f"browser_enabled=True requires size='medium' or 'large' (got {size!r}) -- "
            "a headless Chromium process needs more headroom than the 'small' tier "
            "provides (docs/BROWSER-EXEC-DESIGN.md §4)"
        )


def _validate_desktop_resource_floor(size: str, desktop_enabled: bool) -> None:
    """A full X server + window manager + x11vnc needs materially more
    headroom than 'small' (docs/GUI-COMPUTER-USE-SCOPING.md) -- same floor
    as _validate_browser_resource_floor above, reusing 'medium' as a
    placeholder pending real RSS measurement of the Xvfb/WM/x11vnc stack
    under load. Do not guess a tighter number and skip this ValueError --
    see docs/BROWSER-EXEC-DESIGN.md §4's precedent for why a missing floor
    here is a real OOM footgun, not a hypothetical one."""
    if desktop_enabled and not size_at_least(size, "medium"):
        raise ValueError(
            f"desktop_enabled=True requires size='medium' or 'large' (got {size!r}) -- "
            "Xvfb + a window manager + x11vnc need more headroom than 'small' provides "
            "(docs/GUI-COMPUTER-USE-SCOPING.md)"
        )


def _warn_if_browser_enabled_without_network_policy(
    session_id: str, browser_enabled: bool, network_policy_enabled: bool
) -> None:
    """docs/BROWSER-EXEC-DESIGN.md §5: browser_enabled=True with the
    operator-level BOXKITE_BROWSER_NETWORK_POLICY_ENABLED flag off fails
    closed (no egress NetworkPolicy provisioned, so the browser tool, if
    exposed, can't reach anything) -- but the fix is to set that flag or
    this per-session one, never to widen some other, static NetworkPolicy
    as a workaround. Logged here, at session-creation time, to point at
    the actual fix instead of leaving an operator to guess."""
    if browser_enabled and not network_policy_enabled:
        logger.warning(
            f"[SandboxManager] create_session(browser_enabled=True) for session "
            f"{session_id} but BOXKITE_BROWSER_NETWORK_POLICY_ENABLED is not set -- "
            "no browser-egress NetworkPolicy will be provisioned and the browser "
            "tool, if exposed, will be unable to reach any external host."
        )


def _validate_storage_gb(storage_gb: Optional[float]) -> Optional[str]:
    """Validates a caller-requested storage override and converts it to a
    Kubernetes quantity string, or returns None to keep the env/default limit."""
    if storage_gb is None:
        return None
    ceiling = max_volume_size_limit_gi()
    if storage_gb <= 0 or storage_gb > ceiling:
        raise ValueError(f"storage_gb must be greater than 0 and at most {ceiling} (Gi)")
    return f"{storage_gb:g}Gi"


def _validate_lifetime_seconds(lifetime_seconds: Optional[int]) -> Optional[int]:
    if lifetime_seconds is None:
        return None
    ceiling = max_active_deadline_seconds()
    if lifetime_seconds <= 0 or lifetime_seconds > ceiling:
        raise ValueError(f"lifetime_seconds must be greater than 0 and at most {ceiling}")
    return lifetime_seconds


def _validate_gpu_count(gpu_count: Optional[int]) -> Optional[int]:
    """docs/GPU-SUPPORT-SCOPING.md: rejected (ValueError) rather than
    silently ignored when BOXKITE_GPU_ENABLED is off -- a caller asking for
    a GPU on a deployment that hasn't opted into this experimental,
    hardware-dependent configuration should get a loud, actionable error,
    not a session that silently comes up with no GPU. Bounded the same way
    storage_gb/lifetime_seconds are: a per-session ceiling, since a GPU is
    a scarce, physically-limited shared resource a single tenant should
    not be able to request an unbounded slice of."""
    if gpu_count is None:
        return None
    if not gpu_enabled():
        raise ValueError(
            "gpu_count was requested but BOXKITE_GPU_ENABLED is not set -- this is an "
            "experimental, opt-in configuration (docs/GPU-SUPPORT-SCOPING.md); an operator "
            "must enable it and provision a GPU-equipped node pool with a device plugin first."
        )
    ceiling = max_gpu_count_per_session()
    if gpu_count <= 0 or gpu_count > ceiling:
        raise ValueError(f"gpu_count must be greater than 0 and at most {ceiling}")
    return gpu_count


# docs/DECLARATIVE-BUILDER-DESIGN.md section 4/5: a caller-supplied image
# reference (from control-plane's SandboxImage build feature) must be pinned
# to an immutable digest, never a mutable tag -- a pod spec built from a
# `registry_ref` with only a tag could have its target silently swapped
# after the build's vulnerability-scan gate already passed. This is the
# ONLY validation this function performs; it deliberately does not (and
# cannot) check who is allowed to reference this image or whether it passed
# a scan -- that ownership/status check happens in the control plane
# (control-plane/src/control_plane/routers/sandboxes.py resolves an
# `image_id` to a `registry_ref` only for images owned by the caller's
# account with `status == "completed"`) before this value ever reaches
# SandboxManager.
_IMAGE_DIGEST_REF_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._/:-]*@sha256:[0-9a-f]{64}$"
)


def _validate_image_ref(image_ref: Optional[str]) -> Optional[str]:
    if image_ref is None:
        return None
    if not _IMAGE_DIGEST_REF_RE.match(image_ref):
        raise ValueError(
            f"image_ref must be a digest-pinned reference (repo@sha256:<64-hex>), got {image_ref!r}"
        )
    return image_ref


# Roots a volume mount must never collide with -- docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's
# Volume addendum requires mount_path to be "outside the sandbox's typed
# roots," not just any absolute path, so a caller-supplied PVC mount can
# never shadow /workspace (or the other already-meaningful directories)
# and silently break sync/prefetch or the sidecar's own file tools.
_RESERVED_VOLUME_MOUNT_PREFIXES = ("/workspace", "/mnt", "/tmp", "/proc", "/sys", "/dev", "/etc", "/root")


def _validate_volume_mounts(volume_mounts: Optional[list[dict]]) -> Optional[list[dict]]:
    """Validate a list of {"pvc_name": str, "mount_path": str} dicts
    (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum). Each
    mount_path must be an absolute path outside every reserved root above;
    pvc_name must be non-empty (the control-plane router is what actually
    resolves a volume_id to a real, "ready"-status pvc_name -- this
    function only guards the shape once it reaches SandboxManager)."""
    if not volume_mounts:
        return None
    for entry in volume_mounts:
        pvc_name = entry.get("pvc_name")
        mount_path = entry.get("mount_path")
        if not pvc_name or not isinstance(pvc_name, str):
            raise ValueError(f"volume_mounts entry missing a non-empty pvc_name: {entry!r}")
        if not mount_path or not mount_path.startswith("/") or mount_path == "/":
            raise ValueError(f"volume_mounts mount_path must be an absolute, non-root path: {mount_path!r}")
        if any(mount_path == prefix or mount_path.startswith(prefix + "/") for prefix in _RESERVED_VOLUME_MOUNT_PREFIXES):
            raise ValueError(
                f"volume_mounts mount_path {mount_path!r} collides with a reserved sandbox root "
                f"({_RESERVED_VOLUME_MOUNT_PREFIXES})"
            )
    return volume_mounts


SANDBOX_CLAIMED_PRIORITY_CLASS = os.environ.get("SANDBOX_CLAIMED_PRIORITY_CLASS", "").strip()
SANDBOX_HTTP_CLIENT_CACHE_MAX_ENTRIES = max(
    32, int(os.environ.get("SANDBOX_HTTP_CLIENT_CACHE_MAX_ENTRIES", "512"))
)
SANDBOX_RECOVERY_LOCK_CACHE_MAX_ENTRIES = max(
    32, int(os.environ.get("SANDBOX_RECOVERY_LOCK_CACHE_MAX_ENTRIES", "2048"))
)
SANDBOX_CREATE_LOCK_CACHE_MAX_ENTRIES = max(
    32, int(os.environ.get("SANDBOX_CREATE_LOCK_CACHE_MAX_ENTRIES", "2048"))
)
SANDBOX_SKILLS_CACHE_MAX_ENTRIES = max(
    32, int(os.environ.get("SANDBOX_SKILLS_CACHE_MAX_ENTRIES", "2048"))
)
SANDBOX_SESSION_ENDPOINT_CACHE_MAX_ENTRIES = max(
    32, int(os.environ.get("SANDBOX_SESSION_ENDPOINT_CACHE_MAX_ENTRIES", "2048"))
)
SANDBOX_SESSION_ENDPOINT_TTL_SECONDS = max(
    1, int(os.environ.get("SANDBOX_SESSION_ENDPOINT_TTL_SECONDS", "30"))
)

# Storage configuration - uses same secrets as backend/workers.
# Read STORAGE_BACKEND (the var the control-plane/deployment actually sets and
# the one injected into the sidecar at manager.py); fall back to the legacy
# STORAGE_TYPE name for older self-host configs, then default. Reading only
# STORAGE_TYPE here silently defaulted every sidecar to "azure" even when the
# deployment set STORAGE_BACKEND=s3, breaking /configure + prefetch + workspace
# chown (and thus str_replace) on S3/GCS deployments.
STORAGE_BACKEND = (
    os.environ.get("STORAGE_BACKEND") or os.environ.get("STORAGE_TYPE") or "azure"
)  # 's3' or 'azure'


def _get_s3_bucket() -> str:
    return os.environ.get("STORAGE_S3_BUCKET") or os.environ.get("S3_BUCKET", "boxkite-sandbox")


S3_BUCKET = _get_s3_bucket()
STORAGE_S3_REGION = os.environ.get("STORAGE_S3_REGION") or os.environ.get("AWS_REGION", "us-east-1")
STORAGE_S3_ENDPOINT = os.environ.get("STORAGE_S3_ENDPOINT") or os.environ.get("S3_ENDPOINT", "")
STORAGE_S3_KMS_KEY_ID = os.environ.get("STORAGE_S3_KMS_KEY_ID") or os.environ.get("S3_KMS_KEY_ID", "")
STORAGE_S3_BUCKET_KEY_ENABLED = (
    os.environ.get("STORAGE_S3_BUCKET_KEY_ENABLED") or os.environ.get("S3_BUCKET_KEY_ENABLED", "true")
)
# Name of an existing Secret holding S3 credentials; override per environment
STORAGE_CREDENTIALS_SECRET = os.environ.get("STORAGE_CREDENTIALS_SECRET", "boxkite-storage-credentials")

SAFE_TO_EVICT_ANNOTATION = "cluster-autoscaler.kubernetes.io/safe-to-evict"
SESSION_ID_ANNOTATION = "sandbox.boxkite.dev/session-id"
ORGANIZATION_ID_ANNOTATION = "sandbox.boxkite.dev/organization-id"
WORK_ITEM_ID_ANNOTATION = "sandbox.boxkite.dev/work-item-id"
# Purely observational: which filesystem snapshot (if any) this session's
# workspace was seeded from at creation time (docs/SNAPSHOT-DESIGN.md).
# SandboxManager does NOT use this to change the pod's storage_prefix or
# any other lifecycle behavior -- the caller (control plane) is responsible
# for copying the snapshot's data into this session's own live
# storage_prefix *before* calling create_session, so a restored session's
# ongoing sync writes land in its own prefix, never back into the
# snapshot's immutable one. See the design doc's security section on why a
# restored pod must not get special-cased pod-spec/capability treatment.
RESTORED_FROM_SNAPSHOT_ANNOTATION = "sandbox.boxkite.dev/restored-from-snapshot"

_T = TypeVar("_T")
_K8S_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$")


def _to_k8s_label_value(value: str, *, prefix: str) -> str:
    """Return a label-safe value for arbitrary identifiers."""
    raw = str(value or "").strip()
    if raw and len(raw) <= 63 and _K8S_LABEL_VALUE_RE.fullmatch(raw):
        return raw

    safe_prefix = re.sub(r"[^a-z0-9]", "", prefix.lower()) or "id"
    safe_prefix = safe_prefix[:10]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
    return f"{safe_prefix}-{digest}"



# Re-export every module-level name (including the underscore-prefixed
# validators and imported symbols) so manager.py and the mixin modules can
# pull the full namespace with ``from ._manager_config import *`` exactly as
# the original single-module namespace provided it.
__all__ = [_name for _name in list(globals()) if not _name.startswith("__")]
