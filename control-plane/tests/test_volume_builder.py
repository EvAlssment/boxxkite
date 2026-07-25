"""volume_builder.py -- docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum.

Mirrors test_image_builder_dockerfile.py's structure for the equivalent
build_pvc_spec/provisioner seam.
"""

from __future__ import annotations

import pytest

from control_plane.volume_builder import FakeVolumeProvisioner, VolumeOutcome, build_pvc_spec


def test_build_pvc_spec_uses_configured_storage_class(monkeypatch):
    from control_plane.config import settings

    monkeypatch.setattr(settings, "BOXKITE_VOLUME_STORAGE_CLASS", "fast-ssd")
    spec = build_pvc_spec(volume_id="vol-1", account_id="acct-1", size_gb=10)

    assert spec["spec"]["storageClassName"] == "fast-ssd"
    assert spec["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert spec["spec"]["accessModes"] == ["ReadWriteOnce"]


def test_build_pvc_spec_names_are_deterministic_and_dns_safe():
    spec = build_pvc_spec(volume_id="vol-1234567890", account_id="acct-1234567890", size_gb=5)

    name = spec["metadata"]["name"]
    assert name == spec["_boxkite_pvc_name"]
    assert name.islower() or not any(c.isupper() for c in name)
    assert "/" not in name


def test_build_pvc_spec_labels_namespace_by_account_and_volume():
    spec = build_pvc_spec(volume_id="vol-1", account_id="acct-1", size_gb=5)

    labels = spec["metadata"]["labels"]
    assert labels["boxkite.dev/account-id"] == "acct-1"
    assert labels["boxkite.dev/volume-id"] == "vol-1"


@pytest.mark.asyncio
async def test_fake_volume_provisioner_returns_ready_with_pvc_name():
    provisioner = FakeVolumeProvisioner()

    outcome = await provisioner.provision(volume_id="vol-1", account_id="acct-1", size_gb=10)

    assert isinstance(outcome, VolumeOutcome)
    assert outcome.status == "ready"
    assert outcome.pvc_name is not None


@pytest.mark.asyncio
async def test_fake_volume_provisioner_fails_oversized_requests():
    provisioner = FakeVolumeProvisioner()

    outcome = await provisioner.provision(volume_id="vol-1", account_id="acct-1", size_gb=901)

    assert outcome.status == "failed"
    assert outcome.failure_reason


@pytest.mark.asyncio
async def test_fake_volume_provisioner_tracks_deprovision_calls():
    provisioner = FakeVolumeProvisioner()

    await provisioner.deprovision(pvc_name="boxkite-vol-abc")

    assert provisioner.deprovisioned == ["boxkite-vol-abc"]
