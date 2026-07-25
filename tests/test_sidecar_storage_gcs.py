"""Tests for the sidecar's GCSBackend storage sync path (GitHub issue #213).

Covers:
- GCSBackend.upload/download/list_objects call the expected
  google-cloud-storage SDK methods against the right bucket/blob key.
- download() returns False (rather than raising) when the SDK reports the
  object doesn't exist -- matching S3/Azure's "not found is a warning, not
  an error" pattern.
- get_storage_backend() returns a GCSBackend when STORAGE_BACKEND=gcs.

This is the periodic upload/download/list object-storage sync path, not the
separate, deliberately-unimplemented live FUSE bucket-mount feature covered
by test_sidecar_mount_bucket.py.
"""

from unittest.mock import MagicMock

from google.cloud import storage as gcs_storage
from google.cloud.exceptions import NotFound

import main as sidecar_main
import sidecar_storage


class FakeBlob:
    def __init__(self, name: str):
        self.name = name
        self.upload_from_filename = MagicMock()
        self.download_to_filename = MagicMock()


class FakeBucket:
    def __init__(self, name: str):
        self.name = name
        self._blobs: dict[str, FakeBlob] = {}

    def blob(self, key: str) -> FakeBlob:
        if key not in self._blobs:
            self._blobs[key] = FakeBlob(key)
        return self._blobs[key]

    def list_blobs(self, prefix: str = ""):
        return [blob for key, blob in self._blobs.items() if key.startswith(prefix)]


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self._buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        if name not in self._buckets:
            self._buckets[name] = FakeBucket(name)
        return self._buckets[name]


def _make_backend(monkeypatch, bucket: str = "my-gcs-bucket", project: str = "") -> sidecar_storage.GCSBackend:
    monkeypatch.setattr(gcs_storage, "Client", FakeClient)
    monkeypatch.setattr(sidecar_main, "GCS_BUCKET", bucket)
    monkeypatch.setattr(sidecar_main, "GCS_PROJECT", project)
    return sidecar_storage.GCSBackend()


def test_gcs_backend_uses_application_default_credentials_by_default(monkeypatch):
    backend = _make_backend(monkeypatch, project="")

    assert isinstance(backend.client, FakeClient)
    assert "project" not in backend.client.init_kwargs


def test_gcs_backend_passes_project_when_configured(monkeypatch):
    backend = _make_backend(monkeypatch, project="my-gcp-project")

    assert backend.client.init_kwargs.get("project") == "my-gcp-project"


async def test_gcs_backend_upload_calls_upload_from_filename(monkeypatch, tmp_path):
    backend = _make_backend(monkeypatch)
    local_file = tmp_path / "output.txt"
    local_file.write_text("hello world")

    result = await backend.upload(str(local_file), "some/remote/key.txt")

    assert result is True
    blob = backend.bucket.blob("some/remote/key.txt")
    blob.upload_from_filename.assert_called_once()
    args, kwargs = blob.upload_from_filename.call_args
    assert args[0] == str(local_file)
    assert kwargs["content_type"]


async def test_gcs_backend_upload_returns_false_on_failure(monkeypatch, tmp_path):
    backend = _make_backend(monkeypatch)
    local_file = tmp_path / "output.txt"
    local_file.write_text("hello world")
    backend.bucket.blob("some/remote/key.txt").upload_from_filename.side_effect = RuntimeError("boom")

    result = await backend.upload(str(local_file), "some/remote/key.txt")

    assert result is False


async def test_gcs_backend_download_calls_download_to_filename(monkeypatch, tmp_path):
    backend = _make_backend(monkeypatch)
    local_path = tmp_path / "downloaded" / "file.txt"

    result = await backend.download("some/remote/key.txt", str(local_path))

    assert result is True
    blob = backend.bucket.blob("some/remote/key.txt")
    blob.download_to_filename.assert_called_once_with(str(local_path))


async def test_gcs_backend_download_returns_false_gracefully_on_not_found(monkeypatch, tmp_path):
    backend = _make_backend(monkeypatch)
    local_path = tmp_path / "downloaded" / "file.txt"
    backend.bucket.blob("missing/key.txt").download_to_filename.side_effect = NotFound("no such object")

    result = await backend.download("missing/key.txt", str(local_path))

    assert result is False


async def test_gcs_backend_list_objects_uses_prefix(monkeypatch):
    backend = _make_backend(monkeypatch)
    backend.bucket.blob("outputs/a.txt")
    backend.bucket.blob("outputs/b.txt")
    backend.bucket.blob("uploads/c.txt")

    keys = await backend.list_objects("outputs/")

    assert sorted(keys) == ["outputs/a.txt", "outputs/b.txt"]


async def test_gcs_backend_list_objects_returns_empty_list_on_failure(monkeypatch):
    backend = _make_backend(monkeypatch)

    def _boom(prefix=""):
        raise RuntimeError("boom")

    monkeypatch.setattr(backend.bucket, "list_blobs", _boom)

    keys = await backend.list_objects("outputs/")

    assert keys == []


def test_get_storage_backend_returns_gcs_backend_when_configured(monkeypatch):
    monkeypatch.setattr(gcs_storage, "Client", FakeClient)
    monkeypatch.setattr(sidecar_main, "STORAGE_BACKEND", "gcs")
    monkeypatch.setattr(sidecar_main, "GCS_BUCKET", "my-gcs-bucket")
    monkeypatch.setattr(sidecar_main, "GCS_PROJECT", "")

    backend = sidecar_storage.get_storage_backend()

    assert isinstance(backend, sidecar_storage.GCSBackend)
