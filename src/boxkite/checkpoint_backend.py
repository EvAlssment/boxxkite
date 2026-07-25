"""Opt-in full-state (process/memory) checkpoint support
(docs/FULL-STATE-SNAPSHOT-SCOPING.md).

STATUS: implemented against the real Kubernetes API shape (the kubelet's
alpha `ContainerCheckpoint` endpoint, KEP-2008), never exercised against a
live cluster with the `ContainerCheckpoint` feature gate enabled -- same
"implemented against the real API shape, never exercised against the real
live service" honesty this project already applies to
K8sVolumeProvisioner/KanikoJobBuildRunner.

THIS IS NOT A "PAUSE AND RESUME" FEATURE. Read this before wiring it into
anything user-facing. The scoping doc's §2 finding, verified directly
against Kubernetes' own KEP-2008 text: the in-tree `ContainerCheckpoint`
feature is explicitly forensic/security-investigation-scoped -- it
produces a point-in-time checkpoint archive on the *node's local disk*
for offline inspection, and restore is deliberately out of scope for the
in-tree feature, left to third parties. There is no "resume this session
from where it left off" capability here, and this module does not
pretend otherwise: `restore()` raises `CheckpointRestoreNotSupportedError`
unconditionally. Building real resume would mean the node/CRI-level CRIU
integration the scoping doc's §2/§3/§7 found is a categorically larger,
still-unbuilt undertaking -- not something this module (or any amount of
control-plane/sidecar code) can add on top of the forensic-only kubelet
API by itself.

WHY THIS DOESN'T TOUCH THE SANDBOX CONTAINER'S SECURITY POSTURE: the
scoping doc's §3 finding was that CRIU checkpoint/restore of the `sandbox`
container's own process tree would require granting it CAP_SYS_ADMIN/
CAP_SYS_PTRACE/CAP_NET_ADMIN directly -- reversing the non-root,
all-capabilities-dropped isolation `deploy/pod-template.yaml`/SECURITY.md
document as boxkite's core security property. This module never does
that. It calls the kubelet's checkpoint endpoint via the Kubernetes API
server's node-proxy subresource (`POST /api/v1/nodes/{node}/proxy/
checkpoint/{namespace}/{pod}/{container}`) -- the SAME node-privileged
path `kubectl` itself would use, executed by the control-plane's own
already-elevated ServiceAccount, not by anything running inside the
sandbox or sidecar container. No new capability is added to either
container's securityContext anywhere in this codebase for this feature.

WHAT THIS DOES ADD, DISCLOSED PLAINLY: the control-plane's own
ServiceAccount needs a new, meaningfully broad RBAC grant --
`nodes/proxy` `create` (see deploy/full-state-snapshot-rbac-optin.yaml,
a SEPARATE, opt-in manifest, never merged into deploy/rbac.yaml's
default grant set). `nodes/proxy` is a well-known sensitive Kubernetes
permission: it lets its holder proxy arbitrary requests to any node's
kubelet API, which in a real cluster also fronts other node-level
functionality (e.g. kubelet's own /exec, /logs endpoints on OTHER pods,
not just this one). Granting this is a real, non-trivial expansion of
the control-plane's own blast radius if its credentials are compromised
-- treat enabling BOXKITE_FULL_STATE_SNAPSHOT_ENABLED with at least the
same scrutiny SECURITY.md already applies to the sidecar's own
near-root capability grant, not as a routine feature flag flip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

logger = logging.getLogger(__name__)


class CheckpointRestoreNotSupportedError(NotImplementedError):
    """Raised unconditionally by every CheckpointBackend.restore()
    implementation in this module. Kubernetes' in-tree ContainerCheckpoint
    feature (KEP-2008) is explicitly forensic-only -- it has no restore
    API, and no CRIU-based restore mechanism is implemented anywhere in
    this codebase (docs/FULL-STATE-SNAPSHOT-SCOPING.md §2/§7). This
    exception exists so a caller gets a loud, specific, actionable error
    instead of a generic AttributeError or a silent no-op if anything
    ever tries to build a "resume" feature on top of this module without
    reading this file's own module docstring first."""


@dataclass(frozen=True)
class CheckpointResult:
    """Mirrors the kubelet checkpoint API's own response shape (a list of
    archive paths, one per checkpointed container -- always one here,
    since this module always requests exactly one container). The path
    is on the NODE's local disk, not copied anywhere by this module --
    retrieving it requires separate node-level access (e.g. a privileged
    debug pod, or direct node SSH) this codebase does not provide. This
    is a real, disclosed gap, not an oversight: KEP-2008 itself doesn't
    define an artifact-retrieval mechanism either."""

    node_name: str
    namespace: str
    pod_name: str
    container_name: str
    archive_paths: list[str]


class CheckpointBackend(Protocol):
    """A pluggable checkpoint backend, per docs/FULL-STATE-SNAPSHOT-
    SCOPING.md §6's sketch of a future "checkpoint backend" abstraction in
    SandboxManager. Exactly one implementation exists today
    (KubeletForensicCheckpointBackend) -- the Protocol exists so a future,
    genuinely different backend (e.g. a Kata-VM-level snapshot, per the
    scoping doc's §7 revisit trigger #2) has a shape to implement against,
    not because there is a second implementation today."""

    async def checkpoint(
        self, *, node_name: str, namespace: str, pod_name: str, container_name: str
    ) -> CheckpointResult: ...

    async def restore(self, *args, **kwargs):
        """Always raises CheckpointRestoreNotSupportedError -- see this
        module's docstring for why."""
        ...


class KubeletForensicCheckpointBackend:
    """Calls the kubelet's alpha `ContainerCheckpoint` endpoint (KEP-2008)
    via the Kubernetes API server's node-proxy subresource. Requires the
    cluster to have the `ContainerCheckpoint` feature gate enabled on the
    kubelet AND a CRI that implements checkpoint support (containerd's criu
    plugin, or CRI-O's enable_criu_support) -- neither of which this class
    can detect or enable itself; see `probe_checkpoint_support` below for
    a best-effort availability check.

    Never exercised against a live cluster with this feature gate enabled
    -- see this module's own top docstring."""

    def __init__(self, core_api: client.CoreV1Api):
        self._core_api = core_api

    async def checkpoint(
        self, *, node_name: str, namespace: str, pod_name: str, container_name: str
    ) -> CheckpointResult:
        """POST /api/v1/nodes/{node_name}/proxy/checkpoint/{namespace}/{pod_name}/{container_name}
        per KEP-2008's documented path shape. Returns the archive path(s)
        the kubelet reports -- does NOT copy them anywhere; see
        CheckpointResult's own docstring."""
        path = f"/checkpoint/{namespace}/{pod_name}/{container_name}"
        try:
            response = await self._core_api.connect_post_node_proxy_with_path(
                name=node_name, path=path
            )
        except ApiException as exc:
            # 404/501-shaped failures here almost always mean the feature
            # gate or CRI support isn't actually enabled on this node --
            # surfaced as-is (not swallowed) so the caller sees the real
            # kubelet-reported reason, per this project's "fail loud, not
            # silently" convention (see resource_config.py's
            # _validate_gpu_count for the same posture on a different
            # opt-in feature).
            logger.warning(
                "[CheckpointBackend] kubelet checkpoint request failed for "
                "%s/%s/%s on node %s: %s",
                namespace, pod_name, container_name, node_name, exc,
            )
            raise
        # The kubelet's real response shape is {"items": ["/path/to/archive.tar"]}
        # per KEP-2008; connect_post_node_proxy_with_path returns the raw
        # response body as a str (it doesn't know the shape of whatever it's
        # proxying to), so this module parses it defensively rather than
        # assuming a specific client-side model exists for it.
        import json

        try:
            parsed = json.loads(response) if isinstance(response, str) else response
            archive_paths = list(parsed.get("items", []))
        except (ValueError, AttributeError):
            logger.warning(
                "[CheckpointBackend] Unexpected kubelet checkpoint response shape "
                "for %s/%s/%s: %r", namespace, pod_name, container_name, response,
            )
            archive_paths = []
        return CheckpointResult(
            node_name=node_name,
            namespace=namespace,
            pod_name=pod_name,
            container_name=container_name,
            archive_paths=archive_paths,
        )

    async def restore(self, *args, **kwargs):
        raise CheckpointRestoreNotSupportedError(
            "Kubernetes' in-tree ContainerCheckpoint feature (KEP-2008) has no restore "
            "API, and no CRIU-based restore mechanism is implemented in this codebase "
            "(docs/FULL-STATE-SNAPSHOT-SCOPING.md §2/§3/§7). This is a forensic-only "
            "checkpoint capability, not a pause/resume feature."
        )


async def probe_checkpoint_support(core_api: client.CoreV1Api, node_name: str) -> bool:
    """Best-effort availability check -- there is no direct Kubernetes API
    to ask "does this node's kubelet have ContainerCheckpoint enabled"
    short of attempting a checkpoint and inspecting the failure. This
    calls a deliberately-invalid path against the same node-proxy
    subresource the real feature uses and treats a 404 (path routing
    reached the kubelet, which reports "not found" for the bogus path) as
    weak evidence the proxy path itself works, vs. a 403 (RBAC not
    granted -- see deploy/full-state-snapshot-rbac-optin.yaml) or
    connection failure (proxy/network issue) as a hard "not available"
    signal. NOT a reliable feature-gate probe -- the only way to know for
    certain is to attempt a real checkpoint call and read the kubelet's
    own error message, which `KubeletForensicCheckpointBackend.checkpoint`
    already surfaces verbatim on failure. This function exists to give an
    operator a cheap, non-destructive pre-flight signal, not a guarantee."""
    try:
        await core_api.connect_get_node_proxy_with_path(
            name=node_name, path="/checkpoint/__boxkite_probe__/__boxkite_probe__/__boxkite_probe__"
        )
        return True
    except ApiException as exc:
        if exc.status == 404:
            return True
        logger.info(
            "[CheckpointBackend] Checkpoint support probe for node %s got status %s -- "
            "treating as unavailable (RBAC not granted, feature gate off, or CRI lacks "
            "checkpoint support)", node_name, exc.status,
        )
        return False
