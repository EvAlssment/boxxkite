"""Control-plane-owned storage client for filesystem snapshot/restore
(docs/SNAPSHOT-DESIGN.md), also reused for takeover-session PTY recordings
(pty_recording.py, GitHub issue #133).

This is deliberately a SEPARATE client from anything in `sidecar/main.py`'s
`StorageBackend`/`S3Backend`/`AzureBlobBackend` -- those exist inside the
sandbox pod and are scoped to per-session sync (`STORAGE_CREDENTIALS_SECRET`).
This module runs in the control-plane process and is configured from its own
`SNAPSHOT_STORAGE_*` settings (config.py), specifically so a snapshot
create/restore's storage-side copy uses its own least-privilege credential,
never the sidecar's broader one -- see the design doc's security section.

Snapshot copy/delete/list use server-side copy (S3 `CopyObject`, Azure Blob
"copy from URL") rather than routing bytes through this process -- the whole
point of doing the copy control-plane-side instead of via the sidecar is to
avoid spending the sidecar's own CPU/network budget on it, and a server-side
copy means this process never downloads/uploads the actual file bytes
either. `upload_bytes`/`download_bytes` are the one exception to that: a PTY
recording originates in-process (an in-memory asciicast buffer built up by
`pty_recording.PtyRecordingBuffer`, not an existing object anywhere in
storage), so there is nothing to server-side-copy -- the bytes have to be
routed through this process at least once, on the way in.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .config import settings

logger = logging.getLogger(__name__)


class SnapshotStorageClient(Protocol):
    """Backend-agnostic interface the snapshot routes depend on."""

    async def copy_prefix(
        self, *, source_prefix: str, dest_prefix: str, keys: list[str]
    ) -> int:
        """Server-side-copy each `{source_prefix}/{key}` to
        `{dest_prefix}/{key}`. Returns total bytes copied (best-effort; 0 if
        the backend can't cheaply report sizes)."""
        ...

    async def delete_prefix(self, *, prefix: str) -> None:
        """Delete every object under `prefix`. Must actually delete the
        underlying storage objects, not just be a DB-level no-op -- see the
        design doc's "a deleted snapshot must actually delete the underlying
        storage objects" requirement."""
        ...

    async def list_keys(self, *, prefix: str) -> list[str]:
        """List objects under `prefix`, returned as keys relative to
        `prefix` (i.e. with `{prefix}/` stripped) -- used by restore to
        enumerate exactly what a snapshot contains before copying it into a
        new session's live prefix."""
        ...

    async def upload_bytes(self, *, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Upload `data` as a single object at the exact key `key` (not a
        prefix) -- see this module's docstring for why this, unlike
        `copy_prefix`, actually routes bytes through this process."""
        ...

    async def download_bytes(self, *, key: str) -> bytes:
        """Download the single object at `key` in full. Sibling to
        `upload_bytes`, used to fetch a takeover recording back out for
        replay."""
        ...


class S3SnapshotStorageClient:
    """Server-side S3 `CopyObject`/`DeleteObjects`, preserving SSE-KMS
    settings on every copy so a snapshot never silently becomes an
    unencrypted or default-key copy of encrypted session data."""

    def __init__(self) -> None:
        import boto3

        kwargs: dict = {"region_name": settings.SNAPSHOT_STORAGE_S3_REGION}
        if settings.SNAPSHOT_STORAGE_S3_ENDPOINT:
            kwargs["endpoint_url"] = settings.SNAPSHOT_STORAGE_S3_ENDPOINT
        if settings.SNAPSHOT_STORAGE_S3_ACCESS_KEY_ID:
            kwargs["aws_access_key_id"] = settings.SNAPSHOT_STORAGE_S3_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = settings.SNAPSHOT_STORAGE_S3_SECRET_ACCESS_KEY
            if settings.SNAPSHOT_STORAGE_S3_SESSION_TOKEN:
                kwargs["aws_session_token"] = settings.SNAPSHOT_STORAGE_S3_SESSION_TOKEN
        self._client = boto3.client("s3", **kwargs)
        self._bucket = settings.SNAPSHOT_STORAGE_S3_BUCKET
        self._kms_key_id = settings.SNAPSHOT_STORAGE_S3_KMS_KEY_ID or None

    def _sse_kms_extra_args(self) -> dict:
        if not self._kms_key_id:
            return {}
        return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self._kms_key_id}

    def _copy_one(self, *, source_key: str, dest_key: str) -> int:
        extra_args = dict(self._sse_kms_extra_args())
        self._client.copy_object(
            Bucket=self._bucket,
            Key=dest_key,
            CopySource={"Bucket": self._bucket, "Key": source_key},
            **extra_args,
        )
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=dest_key)
            return int(head.get("ContentLength", 0))
        except Exception:
            return 0

    async def copy_prefix(self, *, source_prefix: str, dest_prefix: str, keys: list[str]) -> int:
        def _copy_all() -> int:
            total = 0
            for key in keys:
                source_key = f"{source_prefix}/{key}"
                dest_key = f"{dest_prefix}/{key}"
                try:
                    total += self._copy_one(source_key=source_key, dest_key=dest_key)
                except Exception as exc:
                    logger.error(f"[SnapshotStorage/S3] copy failed for {source_key}: {exc}")
                    raise
            return total

        return await asyncio.to_thread(_copy_all)

    async def delete_prefix(self, *, prefix: str) -> None:
        def _delete_all() -> None:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=f"{prefix}/"):
                objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if objects:
                    self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": objects})

        await asyncio.to_thread(_delete_all)

    async def list_keys(self, *, prefix: str) -> list[str]:
        def _list() -> list[str]:
            keys: list[str] = []
            paginator = self._client.get_paginator("list_objects_v2")
            namespace_prefix = f"{prefix}/"
            for page in paginator.paginate(Bucket=self._bucket, Prefix=namespace_prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"][len(namespace_prefix):])
            return keys

        return await asyncio.to_thread(_list)

    async def upload_bytes(self, *, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        def _put() -> None:
            extra_args = dict(self._sse_kms_extra_args())
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type, **extra_args)

        await asyncio.to_thread(_put)

    async def download_bytes(self, *, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()

        return await asyncio.to_thread(_get)


class AzureSnapshotStorageClient:
    """Server-side Azure Blob "copy from URL"/batch delete, mirroring the
    S3 implementation's semantics."""

    def __init__(self) -> None:
        from azure.storage.blob import BlobServiceClient

        if settings.SNAPSHOT_STORAGE_AZURE_CONNECTION_STRING:
            self._service_client = BlobServiceClient.from_connection_string(
                settings.SNAPSHOT_STORAGE_AZURE_CONNECTION_STRING
            )
        elif settings.SNAPSHOT_STORAGE_AZURE_ACCOUNT_URL:
            from azure.identity import DefaultAzureCredential

            self._service_client = BlobServiceClient(
                account_url=settings.SNAPSHOT_STORAGE_AZURE_ACCOUNT_URL,
                credential=DefaultAzureCredential(),
            )
        else:
            raise ValueError(
                "Snapshot storage requires SNAPSHOT_STORAGE_AZURE_CONNECTION_STRING or "
                "SNAPSHOT_STORAGE_AZURE_ACCOUNT_URL to be configured"
            )
        self._container_client = self._service_client.get_container_client(
            settings.SNAPSHOT_STORAGE_AZURE_CONTAINER
        )

    def _copy_one(self, *, source_key: str, dest_key: str) -> int:
        source_blob = self._container_client.get_blob_client(source_key)
        dest_blob = self._container_client.get_blob_client(dest_key)
        dest_blob.start_copy_from_url(source_blob.url)
        props = dest_blob.get_blob_properties()
        return int(props.size or 0)

    async def copy_prefix(self, *, source_prefix: str, dest_prefix: str, keys: list[str]) -> int:
        def _copy_all() -> int:
            total = 0
            for key in keys:
                source_key = f"{source_prefix}/{key}"
                dest_key = f"{dest_prefix}/{key}"
                try:
                    total += self._copy_one(source_key=source_key, dest_key=dest_key)
                except Exception as exc:
                    logger.error(f"[SnapshotStorage/Azure] copy failed for {source_key}: {exc}")
                    raise
            return total

        return await asyncio.to_thread(_copy_all)

    async def delete_prefix(self, *, prefix: str) -> None:
        def _delete_all() -> None:
            blob_names = [b.name for b in self._container_client.list_blobs(name_starts_with=f"{prefix}/")]
            if blob_names:
                self._container_client.delete_blobs(*blob_names)

        await asyncio.to_thread(_delete_all)

    async def list_keys(self, *, prefix: str) -> list[str]:
        def _list() -> list[str]:
            namespace_prefix = f"{prefix}/"
            return [
                b.name[len(namespace_prefix):]
                for b in self._container_client.list_blobs(name_starts_with=namespace_prefix)
            ]

        return await asyncio.to_thread(_list)

    async def upload_bytes(self, *, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        def _upload() -> None:
            from azure.storage.blob import ContentSettings

            blob = self._container_client.get_blob_client(key)
            blob.upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type=content_type))

        await asyncio.to_thread(_upload)

    async def download_bytes(self, *, key: str) -> bytes:
        def _download() -> bytes:
            blob = self._container_client.get_blob_client(key)
            return blob.download_blob().readall()

        return await asyncio.to_thread(_download)


_snapshot_storage_client: SnapshotStorageClient | None = None


def get_snapshot_storage_client() -> SnapshotStorageClient:
    """Lazily-initialized singleton, overridable in tests via
    `app.dependency_overrides[get_snapshot_storage_client]` (see deps.py)."""
    global _snapshot_storage_client
    if _snapshot_storage_client is None:
        if settings.SNAPSHOT_STORAGE_BACKEND == "azure":
            _snapshot_storage_client = AzureSnapshotStorageClient()
        else:
            _snapshot_storage_client = S3SnapshotStorageClient()
    return _snapshot_storage_client


def reset_snapshot_storage_client_for_tests() -> None:
    global _snapshot_storage_client
    _snapshot_storage_client = None
