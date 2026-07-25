"""
Warm Pool Manager - K8s-backed warm pod lifecycle.

This manager keeps no in-memory pod/session state. K8s labels are the source of
truth for warm/claimed status.
"""

import asyncio
import base64
import logging
import os
import ssl
from collections import deque
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

import httpx
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
from .manager import (
    ORGANIZATION_ID_ANNOTATION,
    SESSION_ID_ANNOTATION,
    WORK_ITEM_ID_ANNOTATION,
)
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
    kata_runtime_class_name,
)
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
from .warm_pool_sizing import (
    CLAIM_RATE_TRACKER,
    adaptive_warm_pool_enabled,
    resolve_warm_pool_size_targets,
)

logger = logging.getLogger(__name__)

# Configuration - matches manager.py and K8s configs
SANDBOX_NAMESPACE = os.environ.get("SANDBOX_NAMESPACE", "default")
SANDBOX_SERVICE_ACCOUNT_NAME = os.environ.get(
    "SANDBOX_SERVICE_ACCOUNT_NAME", "sandbox-service-account"
)
# Image defaults use ACR registry - overridden by ConfigMap in K8s deployment
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "boxkite-sandbox:latest")
SIDECAR_IMAGE = os.environ.get("SIDECAR_IMAGE", "boxkite-sidecar:latest")
SIDECAR_PORT = 8080
# Backstop deadline: safety net in case the activity-based reaper fails.
# Normal lifecycle is handled by idle-based reaping, not this deadline.
SANDBOX_ACTIVE_DEADLINE_SECONDS = int(os.environ.get("SANDBOX_ACTIVE_DEADLINE_SECONDS", "86400"))
# Stale pod protection: reject warm pods too close to the activeDeadlineSeconds
# backstop.  With defaults (buffer=3600, min_remaining=60), pods older than 23h
# are skipped at claim time and proactively deleted during replenish scans.
# See pod_claim_policy.py for the math.
SANDBOX_WARM_CLAIM_AGE_BUFFER_SECONDS = int(
    os.environ.get("SANDBOX_WARM_CLAIM_AGE_BUFFER_SECONDS", "3600")
)
SANDBOX_WARM_MIN_REMAINING_LIFETIME_SECONDS = int(
    os.environ.get("SANDBOX_WARM_MIN_REMAINING_LIFETIME_SECONDS", "60")
)

# Activity-based idle timeout: how long a CLAIMED pod can be idle before reaping.
SANDBOX_IDLE_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_IDLE_TIMEOUT_SECONDS", "1800"))
SANDBOX_WARM_PRIORITY_CLASS = os.environ.get("SANDBOX_WARM_PRIORITY_CLASS", "").strip()

# How often the reaper checks for idle pods.
REAPER_CHECK_INTERVAL = int(os.environ.get("SANDBOX_REAPER_INTERVAL", "300"))
REAPER_HEALTH_TIMEOUT = 5  # seconds to wait for /health response
# Reaper flush: before deleting an idle pod, attempt to flush pending files to
# storage.  If the flush fails and REAPER_DELETE_ON_FLUSH_FAILURE is False, the
# pod is kept alive (data preservation over resource cleanup).
REAPER_FLUSH_TIMEOUT = int(os.environ.get("SANDBOX_REAPER_FLUSH_TIMEOUT", "60"))
REAPER_DELETE_ON_FLUSH_FAILURE = os.environ.get("SANDBOX_REAPER_DELETE_ON_FLUSH_FAILURE", "true").lower() == "true"

# Storage configuration - uses same secrets as backend/workers.
# Read STORAGE_BACKEND first (the var the deployment actually sets); fall back to
# the legacy STORAGE_TYPE name, then default. See _manager_config.py for why
# reading STORAGE_TYPE alone silently mis-defaulted warm pods to "azure".
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

# Pool configuration
WARM_POOL_SIZE = int(os.environ.get("WARM_POOL_SIZE", "3"))
WARM_POOL_MAX = int(os.environ.get("WARM_POOL_MAX", "15"))
WARM_POOL_RECYCLE = os.environ.get("WARM_POOL_RECYCLE", "true").lower() == "true"
WARM_POOL_REPLENISH_INTERVAL = 10  # seconds between replenish checks

# Per-size warm sub-pool targets. WARM_POOL_SIZE_SMALL defaults to the
# pre-existing WARM_POOL_SIZE so behavior is unchanged unless an operator
# opts into pre-warming medium/large sandboxes too (both default to 0 --
# pre-warming a bigger pod costs real idle CPU/memory, so it's off until
# asked for). All three still share the single WARM_POOL_MAX active-pod
# ceiling below.
WARM_POOL_SIZE_SMALL = int(os.environ.get("WARM_POOL_SIZE_SMALL", str(WARM_POOL_SIZE)))
WARM_POOL_SIZE_MEDIUM = int(os.environ.get("WARM_POOL_SIZE_MEDIUM", "0"))
WARM_POOL_SIZE_LARGE = int(os.environ.get("WARM_POOL_SIZE_LARGE", "0"))
WARM_POOL_SIZE_TARGETS: dict[str, int] = {
    "small": WARM_POOL_SIZE_SMALL,
    "medium": WARM_POOL_SIZE_MEDIUM,
    "large": WARM_POOL_SIZE_LARGE,
}

# K8s API proxy mode: on macOS + kind, pod IPs are inside the Docker VM and
# unreachable from the host.  Setting SANDBOX_USE_K8S_PROXY=true routes all
# sidecar HTTP through `kubectl proxy` (default :8001).
# Start with: kubectl proxy --context kind-boxkite-dev &
_USE_K8S_PROXY = os.environ.get("SANDBOX_USE_K8S_PROXY", "").lower() == "true"
_K8S_PROXY_URL = os.environ.get("SANDBOX_K8S_PROXY_URL", "http://localhost:8001").rstrip("/")


def _sidecar_url(pod_name: str, pod_ip: str, path: str = "") -> str:
    """Build sidecar URL, optionally routing through kubectl proxy.

    Mirrors manager.py's `_build_sidecar_url` exactly, including its two
    carve-outs: SANDBOX_USE_K8S_PROXY always stays plain HTTP (unverified
    whether the K8s pod-proxy subresource forwards to an HTTPS backend --
    see docs/SIDECAR-TRANSPORT-TLS-DESIGN.md §5), and SIDECAR_TLS_DISABLED
    falls back to plain HTTP everywhere else.
    """
    if _USE_K8S_PROXY:
        return f"{_K8S_PROXY_URL}/api/v1/namespaces/{SANDBOX_NAMESPACE}/pods/{pod_name}:{SIDECAR_PORT}/proxy{path}"
    scheme = "http" if sidecar_tls_disabled() else "https"
    return f"{scheme}://{pod_ip}:{SIDECAR_PORT}{path}"


def _pinned_verify_for_pod(cert_pem: str) -> bool | ssl.SSLContext:
    """Resolve httpx's `verify=` for a warm-pool sidecar call.

    WarmPoolManager keeps no in-memory cert cache (mirrors its existing
    no-cache design for auth tokens -- see `_get_pod_auth_token`'s
    docstring): every call re-reads the pod's Secret fresh via
    `_get_pod_tls_cert`, so there is no "cached vs. live" distinction to
    make here the way manager.py's `_pinned_verify_for_pod` has to. Falls
    back to `True` (default CA verification, which fails loudly against a
    self-signed cert) exactly when TLS is disabled, when routing through
    kubectl proxy, or when no cert was found for this pod -- never
    `verify=False`.
    """
    if sidecar_tls_disabled() or _USE_K8S_PROXY:
        return True
    if not cert_pem:
        return True
    return build_pinned_ssl_context(cert_pem)


def _decode_sidecar_auth_token_from_secret(secret) -> str:
    if not secret or not secret.data:
        return ""
    raw = secret.data.get(SIDECAR_AUTH_SECRET_KEY)
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError):
        return ""


def _decode_sidecar_tls_cert_from_secret(secret) -> str:
    """Mirrors manager.py's `_decode_sidecar_tls_cert_from_secret`. Returns
    "" if TLS wasn't enabled when this Secret was created."""
    if not secret or not secret.data:
        return ""
    raw = secret.data.get(SIDECAR_TLS_CERT_SECRET_KEY)
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError):
        return ""


@dataclass(frozen=True)
class ReadyPod:
    """A claim-ready warm pod snapshotted by the background scan for the
    opt-in fast-claim path (BOXKITE_FAST_CLAIM_ENABLED).

    Carries everything the claim hot path needs so it can skip both the
    per-request pod LIST and the per-pod Secret READ: the pod's identity/IP,
    the resourceVersion the compare-and-swap patch tests against, its size,
    and the precomputed sidecar auth token + pinned TLS cert. This is only a
    hint -- the CAS patch on the hot path stays the source of truth, so a
    stale entry (resourceVersion moved on, pod already claimed/gone) simply
    fails the CAS and the caller falls back to the list-based path.
    """

    pod_name: str
    pod_ip: str
    resource_version: str
    size: str
    auth_token: str
    tls_cert_pem: str


class WarmPoolManager:
    """Manages warm sandbox pods using K8s labels as source of truth."""

    def __init__(self):
        self._k8s_core_api: Optional[client.CoreV1Api] = None
        self._k8s_api_client: Optional[client.ApiClient] = None
        self._k8s_initialized = False
        self._k8s_init_lock = asyncio.Lock()
        self._replenish_task: Optional[asyncio.Task] = None
        self._reaper_task: Optional[asyncio.Task] = None
        # Request-driven replenish (see schedule_replenish): a single in-flight
        # top-up task, deduplicated so a burst of claims doesn't spawn N
        # overlapping scans.
        self._manual_replenish_task: Optional[asyncio.Task] = None
        self._running = False
        self._lifecycle_lock = asyncio.Lock()
        # Opt-in fast-claim ready-pod index (BOXKITE_FAST_CLAIM_ENABLED):
        # per-size FIFO of claim-ready pods snapshotted by the background
        # scan (see _refresh_ready_index). Empty and never touched when the
        # flag is off. Guarded by its own asyncio.Lock so a claim's popleft
        # and a scan's rebuild can't interleave.
        self._ready_index: dict[str, deque[ReadyPod]] = {}
        self._ready_index_lock = asyncio.Lock()

    async def _init_k8s(self):
        """Initialize K8s client."""
        if self._k8s_initialized:
            return
        async with self._k8s_init_lock:
            if self._k8s_initialized:
                return
            try:
                config_source = await load_kubernetes_config()
                logger.info(f"[WarmPool] Using {config_source} K8s config")
            except ConfigException as e:
                logger.error(f"[WarmPool] K8s config failed: {e}")
                raise RuntimeError("K8s configuration failed")

            self._k8s_api_client = build_kubernetes_api_client()
            self._k8s_core_api = client.CoreV1Api(self._k8s_api_client)
            self._k8s_initialized = True

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self):
        """Start background replenish + reaper loops (idempotent)."""
        await self._init_k8s()
        started = False
        async with self._lifecycle_lock:
            replenish_running = self._replenish_task is not None and not self._replenish_task.done()
            reaper_running = self._reaper_task is not None and not self._reaper_task.done()
            if self._running and replenish_running and reaper_running:
                return

            self._running = True
            if not replenish_running:
                self._replenish_task = asyncio.create_task(self._replenish_loop())
                started = True
            if not reaper_running:
                self._reaper_task = asyncio.create_task(self._reaper_loop())
                started = True

        if started:
            logger.info(
                f"[WarmPool] Started with target size {WARM_POOL_SIZE}, "
                f"idle reaper timeout={SANDBOX_IDLE_TIMEOUT_SECONDS}s"
            )

    async def stop(self):
        """Stop background tasks (idempotent)."""
        async with self._lifecycle_lock:
            self._running = False
            tasks = [
                task
                for task in (self._replenish_task, self._reaper_task, self._manual_replenish_task)
                if task
            ]
            self._replenish_task = None
            self._reaper_task = None
            self._manual_replenish_task = None

        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[WarmPool] Background task stop error: {e}")
        await self._close_k8s_client()
        logger.info("[WarmPool] Stopped")

    async def _close_k8s_client(self) -> None:
        """Close Kubernetes ApiClient to avoid unclosed aiohttp session warnings."""
        api_client = self._k8s_api_client
        self._k8s_api_client = None
        self._k8s_core_api = None
        self._k8s_initialized = False
        if api_client is None:
            return
        try:
            await api_client.close()
        except Exception as e:
            logger.warning(f"[WarmPool] Error closing Kubernetes ApiClient: {e}")

    # =========================================================================
    # Pod Operations
    # =========================================================================

    async def claim_pod(self, size: str = DEFAULT_SANDBOX_SIZE) -> Optional[tuple[str, str]]:
        """Claim a warm pod via K8s labels.

        Iterates Running warm pods and attempts an atomic compare-and-swap
        (JSON Patch with resourceVersion test) to transition warm→claimed.
        Pods that are too old, unhealthy, the wrong size, or concurrently
        claimed are skipped.

        Returns (pod_name, pod_ip) on success, or None if no suitable pod.
        """
        if not self._k8s_core_api:
            return None
        max_claimable_age = compute_max_claimable_age_seconds(
            active_deadline_seconds=SANDBOX_ACTIVE_DEADLINE_SECONDS,
            claim_age_buffer_seconds=SANDBOX_WARM_CLAIM_AGE_BUFFER_SECONDS,
            min_remaining_lifetime_seconds=SANDBOX_WARM_MIN_REMAINING_LIFETIME_SECONDS,
        )

        try:
            pods = await self._k8s_core_api.list_namespaced_pod(
                namespace=SANDBOX_NAMESPACE,
                label_selector="app=sandbox,pool=warm,sandbox.boxkite.dev/status=warm",
            )
        except Exception as e:
            logger.warning(f"[WarmPool] Failed to list warm pods: {e}")
            return None

        for pod in pods.items:
            if pod.status.phase != "Running" or not pod.status.pod_ip:
                continue
            pod_size = (pod.metadata.labels or {}).get(SANDBOX_SIZE_LABEL) or DEFAULT_SANDBOX_SIZE
            if pod_size != size:
                continue
            if not pod.status.container_statuses or not all(cs.ready for cs in pod.status.container_statuses):
                continue
            age_seconds = pod_age_seconds(pod.metadata.creation_timestamp)
            if age_seconds is not None and age_seconds >= max_claimable_age:
                logger.info(
                    f"[WarmPool] Skipping warm pod {pod.metadata.name}; "
                    f"too old ({age_seconds:.0f}s / {SANDBOX_ACTIVE_DEADLINE_SECONDS}s deadline)"
                )
                continue

            pod_name = pod.metadata.name
            pod_ip = pod.status.pod_ip
            resource_version = pod.metadata.resource_version
            if not await self._verify_pod_health(pod_name, pod_ip):
                continue

            if not resource_version:
                logger.warning(f"[WarmPool] Missing resourceVersion for warm pod {pod_name}; skipping")
                continue

            try:
                await self._k8s_core_api.patch_namespaced_pod(
                    name=pod_name,
                    namespace=SANDBOX_NAMESPACE,
                    body=[
                        {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
                        {"op": "test", "path": "/metadata/labels/pool", "value": "warm"},
                        {
                            "op": "test",
                            "path": "/metadata/labels/sandbox.boxkite.dev~1status",
                            "value": "warm",
                        },
                        {"op": "replace", "path": "/metadata/labels/pool", "value": "claimed"},
                        {
                            "op": "replace",
                            "path": "/metadata/labels/sandbox.boxkite.dev~1status",
                            "value": "claimed",
                        },
                    ],
                )
                try:
                    await self._k8s_core_api.patch_namespaced_pod(
                        name=pod_name,
                        namespace=SANDBOX_NAMESPACE,
                        body={
                            "metadata": {
                                "annotations": {
                                    SAFE_TO_EVICT_ANNOTATION: "false",
                                }
                            }
                        },
                    )
                except Exception as annotation_error:
                    logger.warning(
                        f"[WarmPool] Failed to mark claimed pod {pod_name} as non-evictable: "
                        f"{annotation_error}"
                    )
                logger.info(f"[WarmPool] Claimed pod {pod_name}")
                CLAIM_RATE_TRACKER.record_claim(size)
                return (pod_name, pod_ip)
            except ApiException as e:
                if e.status in {409, 422}:
                    logger.info(
                        f"[WarmPool] Warm pod {pod_name} was claimed concurrently ({e.status})"
                    )
                else:
                    logger.warning(f"[WarmPool] Failed to claim pod {pod_name}: {e.status}")

        logger.warning("[WarmPool] No warm pods available")
        return None

    async def _get_pod_auth_token(self, pod_name: str) -> str:
        """Read a pod's sidecar auth token from its per-pod Secret.

        WarmPoolManager keeps no in-memory per-pod state (K8s labels are the
        source of truth for everything), so this is a fresh read rather than
        a cache lookup -- called only on the low-frequency recycle path.

        SECURITY: the token used to live as a plaintext pod annotation,
        readable via mere `pods: get` RBAC. It's now in a Secret requiring a
        separate `secrets: get` grant -- see sidecar_auth.py's module
        docstring for why that matters.
        """
        if not self._k8s_core_api:
            return ""
        secret_name = sidecar_auth_secret_name(pod_name)
        try:
            secret = await self._k8s_core_api.read_namespaced_secret(
                name=secret_name, namespace=SANDBOX_NAMESPACE
            )
        except Exception as e:
            logger.warning(f"[WarmPool] Failed to read sidecar-auth secret {secret_name}: {e}")
            return ""
        return _decode_sidecar_auth_token_from_secret(secret)

    async def _get_pod_tls_cert(self, pod_name: str) -> str:
        """Read a pod's pinned TLS cert PEM from the same per-pod Secret the
        auth token lives in. Mirrors `_get_pod_auth_token` exactly -- fresh
        read, no cache, "" both on TLS-disabled and on any read failure."""
        if not self._k8s_core_api:
            return ""
        secret_name = sidecar_auth_secret_name(pod_name)
        try:
            secret = await self._k8s_core_api.read_namespaced_secret(
                name=secret_name, namespace=SANDBOX_NAMESPACE
            )
        except Exception as e:
            logger.warning(f"[WarmPool] Failed to read sidecar-auth secret {secret_name}: {e}")
            return ""
        return _decode_sidecar_tls_cert_from_secret(secret)

    # =========================================================================
    # Fast-claim ready-pod index (opt-in, BOXKITE_FAST_CLAIM_ENABLED)
    # =========================================================================

    async def pop_ready_pod(self, size: str = DEFAULT_SANDBOX_SIZE) -> Optional[ReadyPod]:
        """Atomically remove and return the next ready pod of `size` from the
        in-memory index, or None if that size's index is empty.

        A pop removes the entry so the same pod is never handed to two
        concurrent claims; the caller must still win the K8s compare-and-swap
        before treating it as claimed (a failed CAS just means the entry was
        stale -- pop the next one)."""
        async with self._ready_index_lock:
            dq = self._ready_index.get(size)
            if not dq:
                return None
            return dq.popleft()

    async def _read_pod_token_and_cert(self, pod_name: str) -> tuple[str, str]:
        """Read a pod's sidecar auth token AND pinned TLS cert from its
        single per-pod Secret in one round-trip. Used only at index-population
        time (the background scan), never on the claim hot path. Returns
        ("", "") on any read failure -- the fast claim then falls back to the
        manager's own Secret read for that pod."""
        if not self._k8s_core_api:
            return "", ""
        secret_name = sidecar_auth_secret_name(pod_name)
        try:
            secret = await self._k8s_core_api.read_namespaced_secret(
                name=secret_name, namespace=SANDBOX_NAMESPACE
            )
        except Exception as e:
            logger.warning(f"[WarmPool] Failed to read sidecar-auth secret {secret_name}: {e}")
            return "", ""
        return (
            _decode_sidecar_auth_token_from_secret(secret),
            _decode_sidecar_tls_cert_from_secret(secret),
        )

    async def _refresh_ready_index(
        self, ready_candidates: list[tuple[str, str, str, str]]
    ) -> None:
        """Rebuild the ready-pod index from the current scan's claim-ready
        warm pods.

        `ready_candidates` is a list of (pod_name, pod_ip, resource_version,
        size) tuples produced by _scan_pool_state -- already filtered to
        Running, all-containers-ready, right-age warm pods, so no extra pod
        LIST happens here. The precomputed sidecar auth token + TLS cert are
        carried forward from the previous index entry for a pod that's still
        present (its Secret is immutable for the pod's lifetime), and only
        read fresh (one Secret read) for a pod newly appearing in the pool --
        so a steady-state pool costs zero Secret reads per refresh.

        Rebuilds from scratch each call, so pods that have left the pool
        (claimed, deleted, aged out) drop out naturally.
        """
        async with self._ready_index_lock:
            previous = {
                rp.pod_name: rp for dq in self._ready_index.values() for rp in dq
            }

        new_index: dict[str, deque[ReadyPod]] = {}
        for pod_name, pod_ip, resource_version, size in ready_candidates:
            prior = previous.get(pod_name)
            if prior is not None and prior.auth_token:
                auth_token, tls_cert_pem = prior.auth_token, prior.tls_cert_pem
            else:
                auth_token, tls_cert_pem = await self._read_pod_token_and_cert(pod_name)
            new_index.setdefault(size, deque()).append(
                ReadyPod(
                    pod_name=pod_name,
                    pod_ip=pod_ip,
                    resource_version=resource_version,
                    size=size,
                    auth_token=auth_token,
                    tls_cert_pem=tls_cert_pem,
                )
            )

        async with self._ready_index_lock:
            self._ready_index = new_index

    async def _discard_from_ready_index(self, pod_name: str) -> None:
        """Remove any index entry for a pod being deleted, so a stale entry
        can't be popped between a delete and the next scan rebuild."""
        async with self._ready_index_lock:
            for dq in self._ready_index.values():
                for entry in list(dq):
                    if entry.pod_name == pod_name:
                        dq.remove(entry)

    async def recycle_pod(self, pod_name: str, pod_ip: str) -> bool:
        """Recycle pod by wiping sidecar state and restoring warm labels."""
        if not WARM_POOL_RECYCLE:
            await self._delete_pod(pod_name)
            return False
        if not self._k8s_core_api:
            return False

        auth_token = await self._get_pod_auth_token(pod_name)
        tls_cert_pem = await self._get_pod_tls_cert(pod_name)
        try:
            async with httpx.AsyncClient(
                timeout=30,
                headers={SIDECAR_AUTH_HEADER: auth_token} if auth_token else {},
                verify=_pinned_verify_for_pod(tls_cert_pem),
            ) as client:
                response = await client.post(
                    _sidecar_url(pod_name, pod_ip, "/configure"),
                    json={
                        "session_id": None,
                        "organization_id": None,
                        "work_item_id": None,
                        "storage_prefix": None,
                    },
                )
                response.raise_for_status()
        except Exception as e:
            logger.error(f"[WarmPool] Failed to wipe pod {pod_name} before recycle: {e}")
            await self._delete_pod(pod_name)
            return False

        try:
            warm_by_size, total_active, _ = await self._get_pool_counts()
            if total_active > WARM_POOL_MAX:
                logger.info(
                    f"[WarmPool] Pool full ({total_active}/{WARM_POOL_MAX}), deleting {pod_name}"
                )
                await self._delete_pod(pod_name)
                return False

            await self._k8s_core_api.patch_namespaced_pod(
                name=pod_name,
                namespace=SANDBOX_NAMESPACE,
                body={"metadata": {
                    "labels": {
                        "pool": "warm",
                        "sandbox.boxkite.dev/status": "warm",
                        "session-id": None,
                        "organization-id": None,
                        "work-item-id": None,
                    },
                    "annotations": {
                        "sandbox.boxkite.dev/storage-prefix": None,
                        "sandbox.boxkite.dev/upload-file-ids": None,
                        SESSION_ID_ANNOTATION: None,
                        ORGANIZATION_ID_ANNOTATION: None,
                        WORK_ITEM_ID_ANNOTATION: None,
                        SAFE_TO_EVICT_ANNOTATION: "true",
                    },
                }},
            )
            logger.info(
                f"[WarmPool] Recycled pod {pod_name} to warm pool "
                f"(warm_count={sum(warm_by_size.values()) + 1}, total_active={total_active})"
            )
            return True
        except Exception as e:
            logger.error(f"[WarmPool] Failed to patch recycled pod {pod_name}: {e}")
            await self._delete_pod(pod_name)
            return False

    async def _create_warm_pod(self, size: str = DEFAULT_SANDBOX_SIZE) -> Optional[str]:
        """Create and wait for a warm pod of the given size."""
        if not self._k8s_core_api:
            return None

        pod_name = f"sandbox-warm-{uuid4().hex[:8]}"
        logger.info(f"[WarmPool] Creating warm pod {pod_name} (size={size})")
        try:
            await self._create_k8s_pod(pod_name, size=size)
            await self._wait_for_pod_ready(pod_name)
            logger.info(f"[WarmPool] Warm pod {pod_name} ready")
            return pod_name
        except Exception as e:
            logger.error(f"[WarmPool] Failed to create warm pod {pod_name}: {e}")
            await self._delete_pod(pod_name)
            return None

    async def _create_sidecar_auth_secret(
        self,
        secret_name: str,
        token: str,
        tls_cert_pem: Optional[str] = None,
        tls_key_pem: Optional[str] = None,
    ) -> None:
        """Create the per-pod sidecar-auth Secret referenced by the warm
        pod's `SIDECAR_AUTH_TOKEN` env var (via secretKeyRef — see
        _create_k8s_pod). Tolerates 409: pod names include an 8-hex-char
        uuid4 segment, so a same-name collision is effectively only possible
        from an operator retry, not organic concurrent creation.

        When `tls_cert_pem`/`tls_key_pem` are given, the same Secret also
        carries the pod's pinned TLS keypair (see manager.py's matching
        method for why this is the same Secret, not a second one)."""
        if not self._k8s_core_api:
            return
        string_data = {SIDECAR_AUTH_SECRET_KEY: token}
        if tls_cert_pem and tls_key_pem:
            string_data[SIDECAR_TLS_CERT_SECRET_KEY] = tls_cert_pem
            string_data[SIDECAR_TLS_KEY_SECRET_KEY] = tls_key_pem
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name, namespace=SANDBOX_NAMESPACE),
            string_data=string_data,
            type="Opaque",
        )
        try:
            await self._k8s_core_api.create_namespaced_secret(namespace=SANDBOX_NAMESPACE, body=secret)
        except ApiException as e:
            if e.status != 409:
                raise

    async def _delete_sidecar_auth_secret(self, secret_name: str) -> None:
        if not self._k8s_core_api:
            return
        try:
            await self._k8s_core_api.delete_namespaced_secret(name=secret_name, namespace=SANDBOX_NAMESPACE)
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"[WarmPool] Error deleting sidecar-auth secret {secret_name}: {e}")

    async def _create_k8s_pod(self, pod_name: str, size: str = DEFAULT_SANDBOX_SIZE):
        """Create a warm K8s pod of the given size."""
        if not self._k8s_core_api:
            raise RuntimeError("K8s API not initialized")
        if size not in SANDBOX_SIZE_PRESETS:
            raise ValueError(f"Unknown sandbox size {size!r}; must be one of {sorted(SANDBOX_SIZE_PRESETS)}")

        sidecar_volume_mounts = [
            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
            client.V1VolumeMount(name="uploads", mount_path="/mnt/user-data/uploads"),
            client.V1VolumeMount(name="outputs", mount_path="/mnt/user-data/outputs"),
            client.V1VolumeMount(name="skills", mount_path="/mnt/skills"),
            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
        ]
        aws_web_identity_mount = build_sidecar_aws_web_identity_volume_mount()
        if aws_web_identity_mount:
            sidecar_volume_mounts.append(aws_web_identity_mount)

        pod_volumes = build_sandbox_pod_volumes()
        aws_web_identity_volume = build_aws_web_identity_volume()
        if aws_web_identity_volume:
            pod_volumes.append(aws_web_identity_volume)
        webhook_skip_annotations = {
            **build_pod_identity_webhook_skip_annotations(),
            **build_azure_workload_identity_skip_annotations(),
        }

        # SECURITY: fresh, unguessable per-pod secret for the sidecar's HTTP
        # API (defense in depth on top of NetworkPolicy — see sidecar_auth.py).
        # Stored in its own Kubernetes Secret (not a pod annotation — see
        # sidecar_auth.py's module docstring for why) so SandboxManager (a
        # different process/class) can recover it when it later claims this
        # pod, via `secrets: get` rather than merely `pods: get/list`.
        sidecar_auth_token = generate_sidecar_auth_token()
        sidecar_auth_secret = sidecar_auth_secret_name(pod_name)

        # SECURITY: fresh, short-lived, self-signed TLS keypair for the
        # sidecar's HTTP API, pinned by the manager instead of a public CA
        # (see tls.py, docs/SIDECAR-TRANSPORT-TLS-DESIGN.md). Stored in the
        # SAME Secret as the auth token above. Skipped when
        # SIDECAR_TLS_DISABLED=true.
        tls_cert_pem = tls_key_pem = ""
        if not sidecar_tls_disabled():
            tls_cert_pem, tls_key_pem = generate_pod_self_signed_cert(pod_name)

        await self._create_sidecar_auth_secret(
            sidecar_auth_secret, sidecar_auth_token, tls_cert_pem, tls_key_pem
        )
        tls_enabled = bool(tls_cert_pem and tls_key_pem)
        if tls_enabled:
            sidecar_volume_mounts.append(
                client.V1VolumeMount(
                    name="sidecar-tls", mount_path=SIDECAR_TLS_MOUNT_PATH, read_only=True
                )
            )
            pod_volumes.append(
                client.V1Volume(
                    name="sidecar-tls",
                    secret=client.V1SecretVolumeSource(
                        secret_name=sidecar_auth_secret,
                        items=[
                            client.V1KeyToPath(
                                key=SIDECAR_TLS_CERT_SECRET_KEY, path=SIDECAR_TLS_CERT_FILENAME
                            ),
                            client.V1KeyToPath(
                                key=SIDECAR_TLS_KEY_SECRET_KEY, path=SIDECAR_TLS_KEY_FILENAME
                            ),
                        ],
                    ),
                )
            )

        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=SANDBOX_NAMESPACE,
                labels={
                    "app": "sandbox",
                    "pool": "warm",
                    "sandbox.boxkite.dev/status": "warm",
                    SANDBOX_SIZE_LABEL: size,
                    **build_azure_workload_identity_pod_labels(),
                },
                annotations={
                    SAFE_TO_EVICT_ANNOTATION: "true",
                    **webhook_skip_annotations,
                },
            ),
            spec=client.V1PodSpec(
                share_process_namespace=True,
                # Opt-in, off by default -- see manager.py's identical field
                # and kata_runtime_class_name's own docstring
                # (docs/KATA-CONTAINERS-SCOPING.md) for the unverified risk
                # that must be confirmed before this is a supported
                # configuration.
                runtime_class_name=kata_runtime_class_name(),
                automount_service_account_token=False,
                service_account_name=SANDBOX_SERVICE_ACCOUNT_NAME,
                restart_policy="Never",
                enable_service_links=False,
                active_deadline_seconds=SANDBOX_ACTIVE_DEADLINE_SECONDS,
                priority_class_name=SANDBOX_WARM_PRIORITY_CLASS or None,
                containers=[
                    client.V1Container(
                        name="sandbox",
                        image=SANDBOX_IMAGE,
                        command=["tail", "-f", "/dev/null"],
                        security_context=client.V1SecurityContext(
                            run_as_user=1001,
                            run_as_non_root=True,
                            allow_privilege_escalation=False,
                            capabilities=client.V1Capabilities(drop=["ALL"]),
                            read_only_root_filesystem=True,
                            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                        ),
                        resources=build_sandbox_container_resources(size=size),
                        volume_mounts=[
                            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
                            client.V1VolumeMount(
                                name="uploads",
                                mount_path="/mnt/user-data/uploads",
                                read_only=True,
                            ),
                            client.V1VolumeMount(
                                name="outputs",
                                mount_path="/mnt/user-data/outputs",
                            ),
                            client.V1VolumeMount(
                                name="skills",
                                mount_path="/mnt/skills",
                                read_only=True,
                            ),
                            client.V1VolumeMount(name="tmp", mount_path="/tmp"),
                        ],
                        env=[
                            client.V1EnvVar(name="PATH", value="/usr/local/bin:/usr/bin:/bin"),
                            client.V1EnvVar(name="HOME", value="/workspace"),
                            client.V1EnvVar(name="LANG", value="C.UTF-8"),
                            client.V1EnvVar(name="PYTHONUNBUFFERED", value="1"),
                        ],
                    ),
                    client.V1Container(
                        name="sidecar",
                        image=SIDECAR_IMAGE,
                        ports=[client.V1ContainerPort(container_port=SIDECAR_PORT)],
                        security_context=client.V1SecurityContext(
                            run_as_user=0,
                            # CHOWN required by /configure (sidecar_sync.py) to chown
                            # /workspace and /outputs to SANDBOX_UID/GID; SYS_CHROOT
                            # required for nsenter's internal chroot() when switching
                            # mount namespaces; SETUID/SETGID required for nsenter's own
                            # --setuid/--setgid privilege drop -- see manager.py's
                            # matching securityContext for the full note on each.
                            capabilities=client.V1Capabilities(add=["SYS_PTRACE", "SYS_ADMIN", "CHOWN", "SYS_CHROOT", "SETUID", "SETGID"], drop=["ALL"]),
                            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                        ),
                        resources=build_sidecar_container_resources(size=size),
                        volume_mounts=sidecar_volume_mounts,
                        env=[
                            client.V1EnvVar(name="RUNTIME_MODE", value="k8s"),
                            # SECURITY: shared secret the sidecar requires on every
                            # HTTP request except /health (see sidecar_auth.py).
                            # Sourced from the Secret above via secretKeyRef,
                            # NOT a literal value -- see manager.py's matching
                            # comment for why a literal here would defeat the
                            # point.
                            client.V1EnvVar(
                                name=SIDECAR_AUTH_TOKEN_ENV,
                                value_from=client.V1EnvVarSource(
                                    secret_key_ref=client.V1SecretKeySelector(
                                        name=sidecar_auth_secret,
                                        key=SIDECAR_AUTH_SECRET_KEY,
                                    )
                                ),
                            ),
                            client.V1EnvVar(name="STORAGE_BACKEND", value=STORAGE_BACKEND),
                            client.V1EnvVar(name="S3_BUCKET", value=S3_BUCKET),
                            client.V1EnvVar(name="STORAGE_S3_REGION", value=STORAGE_S3_REGION),
                            client.V1EnvVar(name="STORAGE_S3_ENDPOINT", value=STORAGE_S3_ENDPOINT),
                            client.V1EnvVar(name="STORAGE_S3_KMS_KEY_ID", value=STORAGE_S3_KMS_KEY_ID),
                            client.V1EnvVar(
                                name="STORAGE_S3_BUCKET_KEY_ENABLED",
                                value=STORAGE_S3_BUCKET_KEY_ENABLED,
                            ),
                            build_sidecar_exec_network_isolation_env(),
                            *build_sidecar_aws_auth_env(STORAGE_CREDENTIALS_SECRET),
                            *build_sidecar_azure_storage_env(STORAGE_CREDENTIALS_SECRET),
                            build_sidecar_tls_env(tls_enabled),
                        ],
                        readiness_probe=client.V1Probe(
                            http_get=client.V1HTTPGetAction(
                                path="/health",
                                port=SIDECAR_PORT,
                                scheme="HTTPS" if tls_enabled else "HTTP",
                            ),
                            initial_delay_seconds=2,
                            period_seconds=5,
                        ),
                    ),
                ],
                volumes=pod_volumes,
            ),
        )
        await self._k8s_core_api.create_namespaced_pod(namespace=SANDBOX_NAMESPACE, body=pod)

    async def _wait_for_pod_ready(self, pod_name: str, timeout: int = 60) -> str:
        """Wait for pod to be ready and return pod IP."""
        if not self._k8s_core_api:
            raise RuntimeError("K8s API not initialized")

        start_time = asyncio.get_event_loop().time()
        while True:
            try:
                pod = await self._k8s_core_api.read_namespaced_pod(
                    name=pod_name,
                    namespace=SANDBOX_NAMESPACE,
                )
                if pod.status.phase == "Running" and pod.status.pod_ip:
                    if pod.status.container_statuses and all(cs.ready for cs in pod.status.container_statuses):
                        return pod.status.pod_ip
                if pod.status.phase in ("Failed", "Succeeded"):
                    raise RuntimeError(f"Pod {pod_name} terminated: {pod.status.phase}")
            except ApiException as e:
                if e.status != 404:
                    raise

            if asyncio.get_event_loop().time() - start_time > timeout:
                raise TimeoutError(f"Pod {pod_name} not ready after {timeout}s")
            await asyncio.sleep(1)

    async def _delete_pod(self, pod_name: str):
        """Delete a pod and its companion sidecar-auth Secret."""
        if not self._k8s_core_api:
            return
        await self._discard_from_ready_index(pod_name)
        try:
            await self._k8s_core_api.delete_namespaced_pod(
                name=pod_name,
                namespace=SANDBOX_NAMESPACE,
                body=client.V1DeleteOptions(grace_period_seconds=30),
            )
        except ApiException as e:
            if e.status != 404:
                logger.error(f"[WarmPool] Error deleting pod {pod_name}: {e}")

        await self._delete_sidecar_auth_secret(sidecar_auth_secret_name(pod_name))

    async def _verify_pod_health(self, pod_name: str, pod_ip: str) -> bool:
        """Check if sidecar health endpoint is reachable.

        `/health` is exempt from the SIDECAR_AUTH_TOKEN check (see
        sidecar_auth.py) but is NOT exempt from TLS -- uvicorn serves the
        whole app, including /health, over whichever single scheme it was
        started with -- so this still needs the pod's pinned cert when TLS
        is enabled, same as every other sidecar call.
        """
        tls_cert_pem = await self._get_pod_tls_cert(pod_name)
        try:
            async with httpx.AsyncClient(
                timeout=5, verify=_pinned_verify_for_pod(tls_cert_pem)
            ) as client:
                response = await client.get(_sidecar_url(pod_name, pod_ip, "/health"))
                return response.status_code == 200
        except Exception:
            return False

    # =========================================================================
    # Background Tasks
    # =========================================================================

    async def _replenish_loop(self):
        """Background task to maintain warm pool size."""
        while self._running:
            try:
                await self._replenish()
            except Exception as e:
                logger.error(f"[WarmPool] Replenish error: {e}")
            await asyncio.sleep(WARM_POOL_REPLENISH_INTERVAL)

    def schedule_replenish(self) -> None:
        """Kick a single best-effort replenish pass from the request path.

        The timer-driven `_replenish_loop` relies on `asyncio.sleep` firing on
        schedule. Under a serverless runtime that CPU-throttles the container
        between requests (e.g. Cloud Run without `--no-cpu-throttling` or a
        warm min-instance), those sleeps stall, so the pool is never topped
        back up after a claim drains it and every subsequent create falls
        through to a slow cold pod. Calling this from the create path makes
        the pool self-heal whenever there is traffic: replenishment makes
        progress while an instance is actively serving requests, independent
        of the timer.

        Non-blocking and deduplicated -- it never awaits pod creation (that
        would add ~pod-startup latency to the caller's request) and never
        stacks a second scan while one is already in flight. The underlying
        `_replenish` is itself bounded by WARM_POOL_MAX, so repeated nudges
        can't over-create.
        """
        if not self._running or not self._k8s_core_api:
            return
        existing = self._manual_replenish_task
        if existing is not None and not existing.done():
            return
        self._manual_replenish_task = asyncio.create_task(self._run_manual_replenish())

    async def _run_manual_replenish(self) -> None:
        try:
            await self._replenish()
        except Exception as e:
            logger.error(f"[WarmPool] Request-driven replenish error: {e}")

    async def _scan_pool_state(
        self,
    ) -> tuple[dict[str, int], int, dict[str, int], list[str], list[tuple[str, str, str, str]]]:
        """Scan all sandbox pods and classify them.

        Returns:
            warm_by_size: {size: count} of ready warm pods young enough to claim.
            total_active: All Pending/Running pods (warm + claimed, any size).
            by_state:     Breakdown by sandbox.boxkite.dev/status label.
            stale_pods:   Warm pod names past the max claimable age, plus any
                          pod (warm or claimed) that has already terminated
                          (Failed/Succeeded -- e.g. hit activeDeadlineSeconds).
                          A terminated pod never transitions back to Running,
                          so without also reaping these here they sit in the
                          namespace forever: nothing else in this scan (or in
                          _claim_warm_pod_via_k8s, which already filters on
                          phase == "Running") ever revisits a terminal pod.
                          These don't count toward warm_by_size so the
                          replenish loop creates fresh replacements.
            ready_candidates: (pod_name, pod_ip, resource_version, size) for
                          every pod counted in warm_by_size -- the same
                          claim-ready set, carried out so the opt-in
                          fast-claim ready index can be rebuilt from this one
                          scan without a second pod LIST. Only consumed when
                          BOXKITE_FAST_CLAIM_ENABLED is on.
        """
        if not self._k8s_core_api:
            return ({}, 0, {}, [], [])
        try:
            pods = await self._k8s_core_api.list_namespaced_pod(
                namespace=SANDBOX_NAMESPACE,
                label_selector="app=sandbox",
            )
        except Exception as e:
            logger.warning(f"[WarmPool] Failed to list pods for status: {e}")
            return ({}, 0, {}, [], [])

        max_claimable_age = compute_max_claimable_age_seconds(
            active_deadline_seconds=SANDBOX_ACTIVE_DEADLINE_SECONDS,
            claim_age_buffer_seconds=SANDBOX_WARM_CLAIM_AGE_BUFFER_SECONDS,
            min_remaining_lifetime_seconds=SANDBOX_WARM_MIN_REMAINING_LIFETIME_SECONDS,
        )
        warm_by_size: dict[str, int] = {size: 0 for size in WARM_POOL_SIZE_TARGETS}
        total_active = 0
        by_state: dict[str, int] = {}
        stale_pods: list[str] = []
        ready_candidates: list[tuple[str, str, str, str]] = []
        for pod in pods.items:
            labels = pod.metadata.labels or {}
            phase = pod.status.phase
            status = labels.get("sandbox.boxkite.dev/status") or "unknown"
            by_state[status] = by_state.get(status, 0) + 1

            if phase in {"Pending", "Running"}:
                total_active += 1

            if phase in {"Failed", "Succeeded"}:
                stale_pods.append(pod.metadata.name)
                continue

            is_warm = (
                labels.get("pool") == "warm"
                and labels.get("sandbox.boxkite.dev/status") == "warm"
                and phase == "Running"
            )
            if is_warm and pod.status.container_statuses and all(cs.ready for cs in pod.status.container_statuses):
                age_seconds = pod_age_seconds(pod.metadata.creation_timestamp)
                if age_seconds is not None and age_seconds >= max_claimable_age:
                    stale_pods.append(pod.metadata.name)
                    continue
                size = labels.get(SANDBOX_SIZE_LABEL) or DEFAULT_SANDBOX_SIZE
                warm_by_size[size] = warm_by_size.get(size, 0) + 1
                if pod.status.pod_ip and pod.metadata.resource_version:
                    ready_candidates.append(
                        (
                            pod.metadata.name,
                            pod.status.pod_ip,
                            pod.metadata.resource_version,
                            size,
                        )
                    )

        return (warm_by_size, total_active, by_state, stale_pods, ready_candidates)

    async def _get_pool_counts(self) -> tuple[dict[str, int], int, dict[str, int]]:
        """Return (warm_by_size, total_active, by_state) from K8s labels."""
        warm_by_size, total_active, by_state, _, _ = await self._scan_pool_state()
        return (warm_by_size, total_active, by_state)

    def _current_warm_pool_size_targets(self) -> dict[str, int]:
        """Per-size warm-pool targets to reconcile against right now.

        Byte-identical to WARM_POOL_SIZE_TARGETS when
        BOXKITE_ADAPTIVE_WARM_POOL_ENABLED is off (the default). When on,
        each size's static constant becomes that size's floor and
        WARM_POOL_MAX becomes the shared ceiling -- see
        warm_pool_sizing.resolve_warm_pool_size_targets and
        docs/ADAPTIVE-WARM-POOL-SIZING.md.
        """
        return resolve_warm_pool_size_targets(
            WARM_POOL_SIZE_TARGETS, WARM_POOL_MAX, CLAIM_RATE_TRACKER
        )

    async def _replenish(self):
        """Replenish each size's warm sub-pool to its target.

        Two-phase approach:
        1. Delete stale warm pods (past max claimable age) so they stop
           occupying slots in the pool and K8s resources.
        2. Create fresh pods per size to bring warm_by_size back up to each
           size's target (WARM_POOL_SIZE_TARGETS by default, or an
           adaptive claim-rate-driven value when
           BOXKITE_ADAPTIVE_WARM_POOL_ENABLED is set -- see
           _current_warm_pool_size_targets), capped by the shared
           WARM_POOL_MAX total active pods (sizes are filled in a fixed
           order -- small, medium, large -- so a small deficit is topped
           up before a large one when budget is scarce).
        """
        warm_by_size, total_active, _, stale_pods, ready_candidates = await self._scan_pool_state()
        # Refresh the opt-in fast-claim ready index off this same scan (no
        # extra pod LIST). Kept off the flag-off path entirely so it costs
        # nothing when disabled.
        if fast_claim_enabled():
            try:
                await self._refresh_ready_index(ready_candidates)
            except Exception as e:
                logger.warning(f"[WarmPool] Failed to refresh fast-claim ready index: {e}")
        max_claimable_age = compute_max_claimable_age_seconds(
            active_deadline_seconds=SANDBOX_ACTIVE_DEADLINE_SECONDS,
            claim_age_buffer_seconds=SANDBOX_WARM_CLAIM_AGE_BUFFER_SECONDS,
            min_remaining_lifetime_seconds=SANDBOX_WARM_MIN_REMAINING_LIFETIME_SECONDS,
        )
        for pod_name in stale_pods:
            logger.info(
                f"[WarmPool] Deleting stale warm pod {pod_name} "
                f"(age >= {max_claimable_age:.0f}s)"
            )
            try:
                await self._k8s_core_api.delete_namespaced_pod(
                    name=pod_name,
                    namespace=SANDBOX_NAMESPACE,
                    body=client.V1DeleteOptions(grace_period_seconds=0),
                )
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"[WarmPool] Failed to delete stale pod {pod_name}: {e}")
            except Exception as e:
                logger.warning(f"[WarmPool] Failed to delete stale pod {pod_name}: {e}")

            # Always attempt secret cleanup, even if the pod delete above
            # failed -- a benign 404 (e.g. racing the idle reaper's own
            # delete of the same pod) previously skipped this entirely
            # since both calls shared one try/except, permanently orphaning
            # the secret with no other cleanup path.
            await self._delete_sidecar_auth_secret(sidecar_auth_secret_name(pod_name))

        targets = self._current_warm_pool_size_targets()
        remaining_budget = max(WARM_POOL_MAX - total_active, 0)
        pods_to_create_by_size: dict[str, int] = {}
        for size, target in targets.items():
            if remaining_budget <= 0:
                break
            deficit = max(target - warm_by_size.get(size, 0), 0)
            take = min(deficit, remaining_budget)
            if take > 0:
                pods_to_create_by_size[size] = take
                remaining_budget -= take

        if pods_to_create_by_size:
            logger.info(
                f"[WarmPool] Replenishing {pods_to_create_by_size} "
                f"(warm={warm_by_size}, active={total_active}, targets={targets})"
            )
            tasks = [
                self._create_warm_pod(size=size)
                for size, count in pods_to_create_by_size.items()
                for _ in range(count)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    # =========================================================================
    # Idle Reaper
    # =========================================================================

    async def _reaper_loop(self):
        """Background task to reap idle claimed pods."""
        while self._running:
            try:
                await self._reap_idle_pods()
            except Exception as e:
                logger.error(f"[WarmPool:Reaper] Error: {e}")
            await asyncio.sleep(REAPER_CHECK_INTERVAL)

    async def _reap_idle_pods(self):
        """Check all claimed pods for idle timeout and reap them.

        Uses a double-check pattern: first pass identifies candidates, sleeps
        5s, then re-checks each candidate.  This prevents false positives from
        transient health-endpoint delays.
        """
        if not self._k8s_core_api:
            return
        try:
            pods = await self._k8s_core_api.list_namespaced_pod(
                namespace=SANDBOX_NAMESPACE,
                label_selector="app=sandbox,sandbox.boxkite.dev/status=claimed",
            )
        except Exception as e:
            logger.warning(f"[WarmPool:Reaper] Failed to list claimed pods: {e}")
            return

        # One extra Secret read per claimed pod (the token no longer rides
        # along for free on the list_namespaced_pod response the way the old
        # annotation did) -- this loop runs on the low-frequency reaper
        # cadence, so the extra round-trips are an acceptable cost for no
        # longer exposing tokens via mere `pods: list` RBAC.
        claimed_pods = []
        for pod in pods.items:
            if pod.status.phase == "Running" and pod.status.pod_ip:
                auth_token = await self._get_pod_auth_token(pod.metadata.name)
                claimed_pods.append((pod.metadata.name, pod.status.pod_ip, auth_token))
        if not claimed_pods:
            return

        candidates = []
        for pod_name, pod_ip, auth_token in claimed_pods:
            try:
                idle_seconds = await self._get_pod_idle_seconds(pod_name, pod_ip)
                if idle_seconds is None:
                    continue
                if idle_seconds >= SANDBOX_IDLE_TIMEOUT_SECONDS:
                    candidates.append((pod_name, pod_ip, auth_token))
            except Exception as e:
                logger.warning(f"[WarmPool:Reaper] Error checking {pod_name}: {e}")

        if not candidates:
            return

        await asyncio.sleep(5)

        for pod_name, pod_ip, auth_token in candidates:
            try:
                idle_seconds = await self._get_pod_idle_seconds(pod_name, pod_ip)
                if idle_seconds is not None and idle_seconds >= SANDBOX_IDLE_TIMEOUT_SECONDS:
                    logger.info(
                        f"[WarmPool:Reaper] Reaping idle pod {pod_name} "
                        f"(idle={idle_seconds:.0f}s, threshold={SANDBOX_IDLE_TIMEOUT_SECONDS}s)"
                    )
                    flush_ok = await self._flush_pod_before_reap(pod_name, pod_ip, auth_token)
                    if not flush_ok and not REAPER_DELETE_ON_FLUSH_FAILURE:
                        logger.info(
                            f"[WarmPool:Reaper] Skipping delete for {pod_name}; "
                            "flush did not complete"
                        )
                        continue
                    await self._delete_pod(pod_name)
            except Exception as e:
                logger.warning(f"[WarmPool:Reaper] Error in reap confirmation for {pod_name}: {e}")

    async def _get_pod_idle_seconds(self, pod_name: str, pod_ip: str) -> Optional[float]:
        """Query sidecar /health endpoint for idle duration."""
        try:
            tls_cert_pem = await self._get_pod_tls_cert(pod_name)
            async with httpx.AsyncClient(
                timeout=REAPER_HEALTH_TIMEOUT, verify=_pinned_verify_for_pod(tls_cert_pem)
            ) as http:
                response = await http.get(_sidecar_url(pod_name, pod_ip, "/health"))
                if response.status_code == 200:
                    return response.json().get("idle_seconds")
        except Exception:
            pass
        return None

    async def _flush_pod_before_reap(self, pod_name: str, pod_ip: str, auth_token: str = "") -> bool:
        """Best-effort flush of pending outputs before reaping."""
        try:
            headers = {SIDECAR_AUTH_HEADER: auth_token} if auth_token else {}
            tls_cert_pem = await self._get_pod_tls_cert(pod_name)
            async with httpx.AsyncClient(
                timeout=REAPER_FLUSH_TIMEOUT, headers=headers, verify=_pinned_verify_for_pod(tls_cert_pem)
            ) as http:
                response = await http.post(_sidecar_url(pod_name, pod_ip, "/flush"))
                response.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"[WarmPool:Reaper] Flush before reap failed: {e}")
            return False

    # =========================================================================
    # Status
    # =========================================================================

    async def get_status(self) -> dict:
        """Get warm pool status from K8s labels."""
        warm_by_size, total_active, by_state = await self._get_pool_counts()
        return {
            "target_size": WARM_POOL_SIZE,
            "target_sizes": self._current_warm_pool_size_targets(),
            "max_size": WARM_POOL_MAX,
            "recycle_enabled": WARM_POOL_RECYCLE,
            "warm_count": sum(warm_by_size.values()),
            "warm_by_size": warm_by_size,
            "total_pods": total_active,
            "by_state": by_state,
            "idle_timeout_seconds": SANDBOX_IDLE_TIMEOUT_SECONDS,
            "reaper_flush_timeout_seconds": REAPER_FLUSH_TIMEOUT,
            "backstop_deadline_seconds": SANDBOX_ACTIVE_DEADLINE_SECONDS,
            "adaptive_warm_pool_enabled": adaptive_warm_pool_enabled(),
            "claim_rate_per_second_by_size": {
                size: CLAIM_RATE_TRACKER.claim_rate_per_second(size)
                for size in WARM_POOL_SIZE_TARGETS
            },
        }


async def get_warm_pool() -> Optional[WarmPoolManager]:
    """Get or create a process-local warm pool manager. Returns None in compose mode."""
    global _warm_pool
    if os.environ.get("RUNTIME_MODE") == "compose":
        logger.info("[WarmPool] Skipping warm pool in Docker Compose mode")
        return None

    lock = _get_warm_pool_lock()
    async with lock:
        if _warm_pool is None:
            _warm_pool = WarmPoolManager()
        await _warm_pool.start()
        return _warm_pool


_warm_pool: Optional[WarmPoolManager] = None
_warm_pool_lock: Optional[asyncio.Lock] = None


def _get_warm_pool_lock() -> asyncio.Lock:
    """Lazy-create the warm-pool lock in the active event loop."""
    global _warm_pool_lock
    if _warm_pool_lock is None:
        _warm_pool_lock = asyncio.Lock()
    return _warm_pool_lock


async def close_warm_pool() -> None:
    """Close and reset the process-local warm pool singleton."""
    global _warm_pool
    warm_pool = _warm_pool
    _warm_pool = None
    if warm_pool is not None:
        await warm_pool.stop()
