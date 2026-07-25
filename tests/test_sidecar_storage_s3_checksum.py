"""Regression tests for the sidecar S3Backend's boto3 checksum config.

boto3/botocore >= 1.36 attach flexible-checksum headers (x-amz-checksum-crc32)
to PutObject by default. Non-AWS S3-compatible endpoints -- GCS's XML API,
MinIO, Cloudflare R2 -- reject those with SignatureDoesNotMatch, so every
workspace/outputs upload fails (list/download, which don't send those headers,
still work -- which is why the failure was upload-only). When a custom
STORAGE_S3_ENDPOINT is set, S3Backend must build its client with
request/response checksum calculation set to "when_required" (the pre-1.36
behavior those endpoints accept). Verified against the live GCS bucket:
default config -> SignatureDoesNotMatch; when_required -> PutObject succeeds.
"""

import main as sidecar_main
import sidecar_storage


def _stub_storage_env(monkeypatch, *, endpoint):
    monkeypatch.setattr(sidecar_main, "S3_ENDPOINT", endpoint)
    monkeypatch.setattr(sidecar_main, "AWS_REGION", "us-central1")
    monkeypatch.setattr(sidecar_main, "AWS_ACCESS_KEY_ID", "GOOG1EXAMPLE")
    monkeypatch.setattr(sidecar_main, "AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setattr(sidecar_main, "AWS_SESSION_TOKEN", "")
    monkeypatch.setattr(sidecar_main, "S3_BUCKET", "bucket")
    monkeypatch.setattr(sidecar_main, "S3_KMS_KEY_ID", "")


def _capture_client_kwargs(monkeypatch):
    import boto3

    captured: dict = {}

    def fake_client(service, **kwargs):
        captured["service"] = service
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)
    return captured


def test_s3backend_uses_when_required_checksums_for_custom_endpoint(monkeypatch):
    captured = _capture_client_kwargs(monkeypatch)
    _stub_storage_env(monkeypatch, endpoint="https://storage.googleapis.com")

    sidecar_storage.S3Backend()

    cfg = captured["kwargs"].get("config")
    assert cfg is not None, "custom-endpoint S3 client must set a botocore Config"
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"


def test_s3backend_keeps_sdk_defaults_for_aws(monkeypatch):
    """No custom endpoint == real AWS S3, where the SDK defaults are correct;
    don't override checksum behavior there."""
    captured = _capture_client_kwargs(monkeypatch)
    _stub_storage_env(monkeypatch, endpoint=None)

    sidecar_storage.S3Backend()

    assert "config" not in captured["kwargs"]
