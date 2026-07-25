"""Tests for the sidecar's read-only S3 FUSE mount seam
(POST /mount-bucket, docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md).

Covers:
- build_s3fs_mount_command/build_s3fs_unmount_command produce the expected,
  read-only-only argv (no read-write mode exists to accidentally select).
- Input validation on bucket/mount_path.
- /mount-bucket 404s when BOXKITE_FUSE_MOUNT_ENABLED is off (the default).
- /mount-bucket requires the same sidecar auth as every other route.
- Even when enabled, /mount-bucket 501s because /dev/fuse isn't present in
  this container (real assertion against the actual filesystem, not
  mocked) -- proving the "fails closed at every unreviewed layer" claim.
"""

import pytest

import main as sidecar_main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def test_build_s3fs_mount_command_is_always_read_only():
    cmd = sidecar_main.build_s3fs_mount_command(bucket="my-bucket", mount_path="/data")

    assert cmd[0] == "s3fs"
    assert "my-bucket" in cmd
    assert "/data" in cmd
    assert "-o" in cmd
    ro_index = cmd.index("ro")
    assert cmd[ro_index - 1] == "-o"
    # No parameter anywhere in this function offers a read-write mode.
    import inspect

    sig = inspect.signature(sidecar_main.build_s3fs_mount_command)
    assert "read_only" not in sig.parameters
    assert "write" not in sig.parameters


def test_build_s3fs_mount_command_includes_region():
    cmd = sidecar_main.build_s3fs_mount_command(bucket="my-bucket", mount_path="/data", region="eu-west-1")

    assert "endpoint=eu-west-1" in cmd


def test_build_s3fs_mount_command_rejects_invalid_bucket():
    with pytest.raises(ValueError):
        sidecar_main.build_s3fs_mount_command(bucket="../etc", mount_path="/data")
    with pytest.raises(ValueError):
        sidecar_main.build_s3fs_mount_command(bucket="", mount_path="/data")


def test_build_s3fs_mount_command_rejects_invalid_mount_path():
    with pytest.raises(ValueError):
        sidecar_main.build_s3fs_mount_command(bucket="my-bucket", mount_path="relative/path")
    with pytest.raises(ValueError):
        sidecar_main.build_s3fs_mount_command(bucket="my-bucket", mount_path="/")


def test_build_s3fs_unmount_command_uses_fusermount():
    cmd = sidecar_main.build_s3fs_unmount_command(mount_path="/data")

    assert cmd == ["fusermount", "-u", "/data"]


def test_build_s3fs_unmount_command_rejects_invalid_mount_path():
    with pytest.raises(ValueError):
        sidecar_main.build_s3fs_unmount_command(mount_path="/")


def test_mount_bucket_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    response = client.post(
        "/mount-bucket",
        json={"bucket": "my-bucket", "mount_path": "/data"},
        headers=_auth_headers(),
    )

    assert response.status_code == 404


def test_mount_bucket_requires_auth_like_every_other_route(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "BOXKITE_FUSE_MOUNT_ENABLED", True)
    client = _client()

    response = client.post("/mount-bucket", json={"bucket": "my-bucket", "mount_path": "/data"})

    assert response.status_code == 401


def test_mount_bucket_501s_without_dev_fuse_even_when_enabled(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "BOXKITE_FUSE_MOUNT_ENABLED", True)
    client = _client()

    # Real assertion, not mocked: this test environment (like every current
    # reference manifest) genuinely has no /dev/fuse device.
    assert not __import__("os").path.exists(sidecar_main.FUSE_DEVICE_PATH)

    response = client.post(
        "/mount-bucket",
        json={"bucket": "my-bucket", "mount_path": "/data"},
        headers=_auth_headers(),
    )

    assert response.status_code == 501
    assert "/dev/fuse" in response.json()["detail"]


def test_mount_bucket_501s_even_with_dev_fuse_present(monkeypatch):
    """If an operator DID add /dev/fuse to their own pod manifest, this
    route still refuses to actually mount anything -- see the route's own
    docstring for why (s3fs isn't installed, credential handling isn't
    reviewed)."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "BOXKITE_FUSE_MOUNT_ENABLED", True)

    def _fake_exists(path):
        return path == sidecar_main.FUSE_DEVICE_PATH

    monkeypatch.setattr(sidecar_main.os.path, "exists", _fake_exists)
    client = _client()

    response = client.post(
        "/mount-bucket",
        json={"bucket": "my-bucket", "mount_path": "/data"},
        headers=_auth_headers(),
    )

    assert response.status_code == 501
    assert "not implemented" in response.json()["detail"].lower()
