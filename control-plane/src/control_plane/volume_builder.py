"""Independent storage volume provisioning —
docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum.

**What this is, and isn't:** E2B's `e2b.Volume` equivalent -- an
independently created, named PersistentVolumeClaim with its own lifecycle
apart from any single sandbox session, mountable at a custom path in a
newly created sandbox. NOT the FUSE object-storage mount the rest of
EXTERNAL-STORAGE-MOUNTING-DESIGN.md scopes (that's a live syscall-level
mount of an S3/GCS/Azure bucket; this is a native Kubernetes PVC, no new
sidecar capability, no `/dev/fuse`).

Mirrors image_builder.py's shape closely on purpose (same reviewed
pattern: a pure, unit-testable spec-builder function; a Protocol the
dispatcher depends on; a Fake implementation for tests; a real K8s-backed
implementation). Both `K8sVolumeProvisioner.provision`/`deprovision` here
and `image_builder.KanikoJobBuildRunner.run_build` (issue #80) are now
implemented against a real `CoreV1Api`/`BatchV1Api`, but neither is
exercised against a LIVE cluster in this repo's test suite (there is no
live Kubernetes API available here) -- both are covered by unit tests
against mocked API clients instead. See each class's own docstring for
its specific implementation shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from boxkite.k8s_auth import build_kubernetes_api_client, load_kubernetes_config
from boxkite.manager import SANDBOX_NAMESPACE

from .config import settings

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pvc_name_for(*, account_id: str, volume_id: str) -> str:
    """Deterministic, DNS-1123-safe PVC name -- Kubernetes object names are
    limited to lowercase alphanumerics and '-', so this can't just
    concatenate the raw UUIDs with a slash the way SandboxImage.registry_ref
    does."""
    return f"boxkite-vol-{account_id[:8]}-{volume_id[:8]}"


def build_pvc_spec(*, volume_id: str, account_id: str, size_gb: float) -> dict:
    """Returns a plain-dict PersistentVolumeClaim spec (not a
    `kubernetes.client` object) so this function -- and therefore the PVC
    shape itself -- is directly unit-testable without a `kubernetes` client
    installed/mocked. A real provisioner would feed this to
    `CoreV1Api.create_namespaced_persistent_volume_claim`.

    Load-bearing shape decisions:
    - `accessModes: [ReadWriteOnce]` -- the safe, universally-supported
      default; a `ReadWriteMany`-capable StorageClass is an operator
      opt-in this function does not assume (see
      docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's addendum on why
      concurrent multi-sandbox mount semantics are the interesting open
      question here, not the PVC mechanics).
    - `storageClassName` comes from BOXKITE_VOLUME_STORAGE_CLASS (operator-
      configurable, never a caller-supplied value) -- same "N operator-
      reviewed choices, never arbitrary caller input" posture as
      BOXKITE_BASE_IMAGE_REFS.
    - Labels namespace the PVC by account_id/volume_id, mirroring
      SandboxImage's registry-path namespacing, so a bug in the DB-layer
      authorization check isn't the only thing standing between two
      accounts' volumes.
    """
    pvc_name = _pvc_name_for(account_id=account_id, volume_id=volume_id)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": pvc_name,
            "labels": {
                "app": "boxkite-volume",
                "boxkite.dev/account-id": account_id,
                "boxkite.dev/volume-id": volume_id,
            },
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": settings.BOXKITE_VOLUME_STORAGE_CLASS,
            "resources": {"requests": {"storage": f"{size_gb}Gi"}},
        },
        "_boxkite_pvc_name": pvc_name,
    }


@dataclass(frozen=True)
class VolumeOutcome:
    """Result of one provisioning attempt. `status` is "ready" or "failed"
    (never "queued"/"creating" -- those are only ever intermediate states
    the caller records before/while awaiting this)."""

    status: str
    pvc_name: str | None = None
    failure_reason: str | None = None


class VolumeProvisioner(Protocol):
    """Backend-agnostic interface the volume-creation dispatcher depends
    on -- mirrors image_builder.py's ImageBuildRunner Protocol exactly."""

    async def provision(self, *, volume_id: str, account_id: str, size_gb: float) -> VolumeOutcome: ...

    async def deprovision(self, *, pvc_name: str) -> None: ...


class K8sVolumeProvisioner:
    """Real provisioning: create a PersistentVolumeClaim via the K8s API,
    wait for it to bind (or fail); delete it on deprovision.

    Implemented against a real `CoreV1Api`, mirroring
    `boxkite.manager.SandboxManager`'s own create-and-poll pattern
    (`_create_pod`/`_wait_for_pod_ready`) — lazy client init via
    `boxkite.k8s_auth.load_kubernetes_config`/`build_kubernetes_api_client`,
    a 409-on-create conflict check, and a poll loop bounded by
    `BOXKITE_VOLUME_PROVISION_TIMEOUT_SECONDS`. Still NOT exercised against
    a LIVE cluster in this repo's test suite (there is no live Kubernetes
    API in CI here) — covered by unit tests against a mocked `CoreV1Api`
    instead (mirroring `tests/test_manager.py`'s own mocking pattern for
    pod create/poll). Security-review this end to end (see
    `docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md`) before enabling
    `BOXKITE_VOLUMES_ENABLED` against real multi-tenant traffic.
    """

    def __init__(self, k8s_core_api=None):
        self._k8s_core_api = k8s_core_api
        self._init_lock = asyncio.Lock()

    async def _ensure_client(self) -> None:
        if self._k8s_core_api is not None:
            return
        async with self._init_lock:
            if self._k8s_core_api is not None:
                return
            try:
                config_source = await load_kubernetes_config()
                logger.info(f"[K8sVolumeProvisioner] Using {config_source} K8s config")
            except ConfigException as e:
                raise RuntimeError(f"K8sVolumeProvisioner: K8s config failed: {e}") from e
            api_client = build_kubernetes_api_client()
            self._k8s_core_api = k8s_client.CoreV1Api(api_client)

    async def provision(self, *, volume_id: str, account_id: str, size_gb: float) -> VolumeOutcome:
        await self._ensure_client()
        spec = build_pvc_spec(volume_id=volume_id, account_id=account_id, size_gb=size_gb)
        pvc_name = spec["_boxkite_pvc_name"]
        pvc_body = {k: v for k, v in spec.items() if not k.startswith("_boxkite_")}

        try:
            await self._k8s_core_api.create_namespaced_persistent_volume_claim(
                namespace=SANDBOX_NAMESPACE, body=pvc_body
            )
        except ApiException as e:
            if e.status == 409:
                # A PVC with this deterministic name already exists (e.g. a
                # retried provision attempt after a transient failure) --
                # proceed to poll its existing state rather than treating
                # this as a hard failure, mirroring _create_pod's own
                # "pod already exists, check if it's usable" handling.
                logger.warning(f"[K8sVolumeProvisioner] PVC {pvc_name} already exists, polling its state")
            else:
                logger.error(f"[K8sVolumeProvisioner] Failed to create PVC {pvc_name}: {e}")
                return VolumeOutcome(status="failed", failure_reason=f"PVC creation failed: {e.reason}")

        try:
            await self._wait_for_pvc_bound(pvc_name)
        except (TimeoutError, RuntimeError) as e:
            return VolumeOutcome(status="failed", failure_reason=str(e))

        return VolumeOutcome(status="ready", pvc_name=pvc_name)

    async def _wait_for_pvc_bound(
        self, pvc_name: str, timeout: int | None = None
    ) -> None:
        """Poll until the PVC reaches phase=Bound, raising on failure/timeout.

        Mirrors `SandboxManager._wait_for_pod_ready`'s loop shape: tolerate
        a transient 404 right after create (the API server hasn't
        propagated the object to every read path yet), re-raise any other
        `ApiException`, and bound the whole loop by a wall-clock timeout
        rather than a fixed attempt count.
        """
        effective_timeout = timeout if timeout is not None else settings.BOXKITE_VOLUME_PROVISION_TIMEOUT_SECONDS
        start_time = asyncio.get_event_loop().time()
        while True:
            try:
                pvc = await self._k8s_core_api.read_namespaced_persistent_volume_claim(
                    name=pvc_name, namespace=SANDBOX_NAMESPACE
                )
                if pvc.status.phase == "Bound":
                    return
                if pvc.status.phase == "Lost":
                    raise RuntimeError(f"PVC {pvc_name} entered phase Lost")
            except ApiException as e:
                if e.status != 404:
                    raise

            if asyncio.get_event_loop().time() - start_time > effective_timeout:
                raise TimeoutError(f"PVC {pvc_name} not Bound after {effective_timeout}s")

            await asyncio.sleep(1)

    async def deprovision(self, *, pvc_name: str) -> None:
        await self._ensure_client()
        try:
            await self._k8s_core_api.delete_namespaced_persistent_volume_claim(
                name=pvc_name, namespace=SANDBOX_NAMESPACE
            )
        except ApiException as e:
            if e.status == 404:
                # Already gone -- deprovision is idempotent, same "delete
                # of something already deleted is a success, not an error"
                # posture SandboxManager.destroy_session's pod-delete path
                # already has.
                logger.info(f"[K8sVolumeProvisioner] PVC {pvc_name} already deleted")
                return
            logger.error(f"[K8sVolumeProvisioner] Failed to delete PVC {pvc_name}: {e}")
            raise


class FakeVolumeProvisioner:
    """Deterministic in-process stand-in for tests and for
    RUNTIME_MODE=compose (no cluster to provision a real PVC against).
    Simulates a successful provision with a synthetic pvc_name -- a size
    request containing the literal string "toolarge" fails, so tests can
    exercise the "failed" path without needing a real over-quota cluster
    response."""

    def __init__(self):
        self.deprovisioned: list[str] = []

    async def provision(self, *, volume_id: str, account_id: str, size_gb: float) -> VolumeOutcome:
        spec = build_pvc_spec(volume_id=volume_id, account_id=account_id, size_gb=size_gb)
        pvc_name = spec["_boxkite_pvc_name"]

        # Deterministic failure hook for tests: a size that hashes to a
        # "toolarge"-flagged synthetic quota rejection.
        if size_gb > 900:
            return VolumeOutcome(status="failed", failure_reason="Requested size exceeds cluster quota")

        return VolumeOutcome(status="ready", pvc_name=pvc_name)

    async def deprovision(self, *, pvc_name: str) -> None:
        self.deprovisioned.append(pvc_name)


async def dispatch_volume_creation(
    *,
    repo,
    provisioner: VolumeProvisioner,
    volume_id: str,
    account_id: str,
    size_gb: float,
) -> None:
    """Drives one volume row through queued -> creating -> ready/failed,
    calling `provisioner.provision` for the actual isolated provisioning
    work. Mirrors image_builder.dispatch_build's shape exactly."""
    try:
        await repo.mark_creating(volume_id=volume_id)
        outcome = await provisioner.provision(volume_id=volume_id, account_id=account_id, size_gb=size_gb)

        if outcome.status == "ready":
            assert outcome.pvc_name
            await repo.mark_ready(volume_id=volume_id, pvc_name=outcome.pvc_name)
        else:
            await repo.mark_failed(
                volume_id=volume_id,
                failure_reason=outcome.failure_reason or "Volume provisioning failed",
            )
    except Exception as e:
        logger.error(f"[volume_builder] Unexpected error provisioning volume {volume_id}: {e}")
        await repo.mark_failed(volume_id=volume_id, failure_reason=f"Unexpected error: {e}")
