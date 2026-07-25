"""Opt-in full-state (process/memory) checkpoint (docs/FULL-STATE-SNAPSHOT-SCOPING.md).

Read src/boxkite/checkpoint_backend.py's module docstring before touching
this file -- it is forensic-only (no restore), requires a real,
meaningfully-broad new RBAC grant (nodes/proxy), and has never been
exercised against a live cluster with the kubelet's ContainerCheckpoint
feature gate enabled. Off by default (BOXKITE_FULL_STATE_SNAPSHOT_ENABLED).
"""

from ._manager_config import *  # noqa: F401,F403

from .checkpoint_backend import KubeletForensicCheckpointBackend, CheckpointResult
from .resource_config import full_state_snapshot_enabled


class FullStateCheckpointMixin:
    async def create_full_state_checkpoint(
        self, session_id: str, container_name: str = "sandbox"
    ) -> CheckpointResult:
        """Forensic-only process/memory checkpoint of one container in this
        session's pod, via the kubelet's alpha ContainerCheckpoint API.

        NOT a pause/resume mechanism -- see checkpoint_backend.py's module
        docstring. Raises RuntimeError immediately if
        BOXKITE_FULL_STATE_SNAPSHOT_ENABLED is unset (fail loud, matching
        resource_config._validate_gpu_count's posture for a different
        opt-in feature) rather than silently no-op-ing. The returned
        CheckpointResult.archive_paths point at files on the NODE's local
        disk -- this method does not retrieve or copy them; see
        CheckpointResult's own docstring for why."""
        if not full_state_snapshot_enabled():
            raise RuntimeError(
                "create_full_state_checkpoint was called but BOXKITE_FULL_STATE_SNAPSHOT_ENABLED "
                "is not set -- this is an experimental, opt-in capability "
                "(docs/FULL-STATE-SNAPSHOT-SCOPING.md) requiring both this flag and the "
                "separate deploy/full-state-snapshot-rbac-optin.yaml RBAC grant."
            )
        if self._use_docker_compose:
            raise RuntimeError(
                "create_full_state_checkpoint requires K8s mode -- there is no kubelet "
                "checkpoint API equivalent in Docker Compose mode."
            )

        await self._init_k8s()
        if not self._k8s_core_api:
            raise RuntimeError("K8s API not initialized")

        pod_name, _pod_ip = await self._resolve_session(session_id)
        pod = await self._k8s_core_api.read_namespaced_pod(
            name=pod_name, namespace=SANDBOX_NAMESPACE
        )
        node_name = pod.spec.node_name
        if not node_name:
            raise RuntimeError(
                f"Pod {pod_name} for session {session_id} has no node_name yet "
                "(not scheduled?) -- cannot checkpoint."
            )

        backend = KubeletForensicCheckpointBackend(self._k8s_core_api)
        return await backend.checkpoint(
            node_name=node_name,
            namespace=SANDBOX_NAMESPACE,
            pod_name=pod_name,
            container_name=container_name,
        )
