"""Unit tests for K8sVolumeProvisioner.provision/deprovision -- the real
CoreV1Api-backed implementation that closes GitHub issue #70's storage
follow-up (previously `NotImplementedError`).

Mirrors tests/test_manager.py's own mocking convention for K8s pod
create/poll: a lightweight in-process fake CoreV1Api simulating PVC
lifecycle transitions, not a real cluster -- there is no live Kubernetes
API in this test suite (see volume_builder.py's module docstring).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from control_plane.config import settings
from control_plane.volume_builder import K8sVolumeProvisioner


class _FakePvcCoreApi:
    """Simulates just enough of CoreV1Api's PVC surface for these tests.

    `phases`: a list of phase strings returned by successive
    `read_namespaced_persistent_volume_claim` calls for the same PVC name
    (the last entry repeats once exhausted) -- lets a test simulate
    "Pending, Pending, Bound" without a real poll loop's timing.
    """

    def __init__(self, phases=("Bound",), create_raises: ApiException | None = None):
        self.phases = list(phases)
        self.create_raises = create_raises
        self.create_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self._read_index = 0

    async def create_namespaced_persistent_volume_claim(self, *, namespace, body):
        self.create_calls.append({"namespace": namespace, "body": body})
        if self.create_raises is not None:
            raise self.create_raises

    async def read_namespaced_persistent_volume_claim(self, *, name, namespace):
        phase = self.phases[min(self._read_index, len(self.phases) - 1)]
        self._read_index += 1
        return SimpleNamespace(status=SimpleNamespace(phase=phase))

    async def delete_namespaced_persistent_volume_claim(self, *, name, namespace):
        self.delete_calls.append({"name": name, "namespace": namespace})


@pytest.mark.asyncio
async def test_provision_creates_pvc_and_reports_ready_once_bound():
    fake_api = _FakePvcCoreApi(phases=["Pending", "Pending", "Bound"])
    provisioner = K8sVolumeProvisioner(k8s_core_api=fake_api)

    outcome = await provisioner.provision(volume_id="11111111-abcd", account_id="acct-1", size_gb=5.0)

    assert outcome.status == "ready"
    assert outcome.pvc_name is not None
    assert len(fake_api.create_calls) == 1
    assert fake_api.create_calls[0]["body"]["kind"] == "PersistentVolumeClaim"
    # Internal bookkeeping keys must never reach the real K8s API call.
    assert "_boxkite_pvc_name" not in fake_api.create_calls[0]["body"]


@pytest.mark.asyncio
async def test_provision_tolerates_already_exists_conflict():
    fake_api = _FakePvcCoreApi(
        phases=["Bound"], create_raises=ApiException(status=409, reason="already exists")
    )
    provisioner = K8sVolumeProvisioner(k8s_core_api=fake_api)

    outcome = await provisioner.provision(volume_id="22222222-abcd", account_id="acct-1", size_gb=5.0)

    assert outcome.status == "ready"


@pytest.mark.asyncio
async def test_provision_reports_failed_on_non_conflict_create_error():
    fake_api = _FakePvcCoreApi(
        create_raises=ApiException(status=500, reason="quota exceeded")
    )
    provisioner = K8sVolumeProvisioner(k8s_core_api=fake_api)

    outcome = await provisioner.provision(volume_id="33333333-abcd", account_id="acct-1", size_gb=5.0)

    assert outcome.status == "failed"
    assert outcome.pvc_name is None
    assert outcome.failure_reason is not None


@pytest.mark.asyncio
async def test_provision_reports_failed_when_pvc_lost():
    fake_api = _FakePvcCoreApi(phases=["Pending", "Lost"])
    provisioner = K8sVolumeProvisioner(k8s_core_api=fake_api)

    outcome = await provisioner.provision(volume_id="44444444-abcd", account_id="acct-1", size_gb=5.0)

    assert outcome.status == "failed"
    assert "Lost" in outcome.failure_reason


@pytest.mark.asyncio
async def test_provision_times_out_if_never_bound(monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_VOLUME_PROVISION_TIMEOUT_SECONDS", 0)
    fake_api = _FakePvcCoreApi(phases=["Pending"])
    provisioner = K8sVolumeProvisioner(k8s_core_api=fake_api)

    outcome = await provisioner.provision(volume_id="55555555-abcd", account_id="acct-1", size_gb=5.0)

    assert outcome.status == "failed"
    assert "not Bound" in outcome.failure_reason


@pytest.mark.asyncio
async def test_provision_tolerates_transient_404_during_poll():
    class _TransientNotFoundApi(_FakePvcCoreApi):
        async def read_namespaced_persistent_volume_claim(self, *, name, namespace):
            if self._read_index == 0:
                self._read_index += 1
                raise ApiException(status=404)
            return await super().read_namespaced_persistent_volume_claim(name=name, namespace=namespace)

    fake_api = _TransientNotFoundApi(phases=["Bound"])
    provisioner = K8sVolumeProvisioner(k8s_core_api=fake_api)

    outcome = await provisioner.provision(volume_id="66666666-abcd", account_id="acct-1", size_gb=5.0)

    assert outcome.status == "ready"


@pytest.mark.asyncio
async def test_deprovision_deletes_pvc():
    fake_api = _FakePvcCoreApi()
    provisioner = K8sVolumeProvisioner(k8s_core_api=fake_api)

    await provisioner.deprovision(pvc_name="boxkite-vol-acct1-vol1")

    assert fake_api.delete_calls == [
        {"name": "boxkite-vol-acct1-vol1", "namespace": fake_api.delete_calls[0]["namespace"]}
    ]


@pytest.mark.asyncio
async def test_deprovision_is_idempotent_on_already_deleted():
    class _AlreadyDeletedApi(_FakePvcCoreApi):
        async def delete_namespaced_persistent_volume_claim(self, *, name, namespace):
            raise ApiException(status=404)

    provisioner = K8sVolumeProvisioner(k8s_core_api=_AlreadyDeletedApi())

    # Must not raise.
    await provisioner.deprovision(pvc_name="already-gone")


@pytest.mark.asyncio
async def test_deprovision_reraises_non_404_errors():
    class _BrokenApi(_FakePvcCoreApi):
        async def delete_namespaced_persistent_volume_claim(self, *, name, namespace):
            raise ApiException(status=500, reason="internal error")

    provisioner = K8sVolumeProvisioner(k8s_core_api=_BrokenApi())

    with pytest.raises(ApiException):
        await provisioner.deprovision(pvc_name="some-pvc")
