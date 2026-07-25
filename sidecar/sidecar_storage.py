"""Storage backends (S3/Azure/GCS) and the read-only FUSE bucket-mount route.

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change. Storage configuration and the
``_detect_content_type`` helper remain owned by ``main`` and are referenced
via ``main.<NAME>`` at call time.
"""

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


# ============================================================================
# Storage Backend Abstraction
# ============================================================================

class StorageBackend:
    """Abstract base for storage backends."""

    async def upload(self, local_path: str, remote_key: str) -> bool:
        raise NotImplementedError

    async def download(self, remote_key: str, local_path: str) -> bool:
        raise NotImplementedError

    async def list_objects(self, prefix: str) -> list[str]:
        raise NotImplementedError


class S3Backend(StorageBackend):
    """AWS S3 / MinIO storage backend."""

    def __init__(self):
        import boto3
        kwargs = {"region_name": main.AWS_REGION}
        if main.S3_ENDPOINT:
            kwargs["endpoint_url"] = main.S3_ENDPOINT
            # boto3/botocore >= 1.36 attach flexible-checksum headers
            # (x-amz-sdk-checksum-algorithm / x-amz-checksum-crc32) to PutObject
            # by default. Non-AWS S3-compatible endpoints -- GCS's XML API,
            # MinIO, Cloudflare R2 -- reject those and fail every upload with
            # SignatureDoesNotMatch. Restrict checksums to operations that truly
            # require them (the pre-1.36 behavior), which those endpoints accept.
            from botocore.config import Config

            kwargs["config"] = Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            )
        if main.AWS_ACCESS_KEY_ID:
            kwargs["aws_access_key_id"] = main.AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = main.AWS_SECRET_ACCESS_KEY
            if main.AWS_SESSION_TOKEN:
                kwargs["aws_session_token"] = main.AWS_SESSION_TOKEN
        self.client = boto3.client("s3", **kwargs)
        self.bucket = main.S3_BUCKET
        self.kms_key_id = main.S3_KMS_KEY_ID or None

    def _sse_kms_extra_args(self) -> dict:
        if not self.kms_key_id:
            return {}
        return {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": self.kms_key_id,
        }

    async def upload(self, local_path: str, remote_key: str) -> bool:
        try:
            content_type = main._detect_content_type(local_path)
            extra_args = {}
            extra_args["ContentType"] = content_type
            extra_args.update(self._sse_kms_extra_args())
            await asyncio.to_thread(
                self.client.upload_file,
                local_path,
                self.bucket,
                remote_key,
                ExtraArgs=extra_args,
            )
            logger.info(f"[S3] Uploaded {local_path} to s3://{self.bucket}/{remote_key}")
            return True
        except Exception as e:
            logger.error(f"[S3] Upload failed for {local_path}: {e}")
            return False

    async def download(self, remote_key: str, local_path: str) -> bool:
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            await asyncio.to_thread(
                self.client.download_file,
                self.bucket,
                remote_key,
                local_path,
            )
            logger.info(f"[S3] Downloaded s3://{self.bucket}/{remote_key} to {local_path}")
            return True
        except Exception as e:
            if hasattr(e, 'response') and e.response.get("Error", {}).get("Code") == "404":
                logger.warning(f"[S3] Key not found: {remote_key}")
            else:
                logger.error(f"[S3] Download failed for {remote_key}: {e}")
            return False

    async def list_objects(self, prefix: str) -> list[str]:
        def _list() -> list[str]:
            keys: list[str] = []
            paginator = self.client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    keys.append(obj['Key'])
            return keys

        try:
            return await asyncio.to_thread(_list)
        except Exception as e:
            logger.error(f"[S3] List failed for prefix {prefix}: {e}")
            return []


class AzureBlobBackend(StorageBackend):
    """Azure Blob Storage backend."""

    def __init__(self):
        from azure.storage.blob import BlobServiceClient

        account_url = (
            main.AZURE_STORAGE_ACCOUNT_URL
            or (
                f"https://{main.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
                if main.AZURE_STORAGE_ACCOUNT_NAME
                else ""
            )
        )
        if main.AZURE_STORAGE_AUTH_MODE in {"auto", "connection_string"} and main.AZURE_STORAGE_CONNECTION_STRING:
            self.service_client = BlobServiceClient.from_connection_string(main.AZURE_STORAGE_CONNECTION_STRING)
        elif main.AZURE_STORAGE_AUTH_MODE in {"auto", "account_key"} and account_url and main.AZURE_STORAGE_ACCOUNT_KEY:
            self.service_client = BlobServiceClient(
                account_url=account_url,
                credential=main.AZURE_STORAGE_ACCOUNT_KEY
            )
        elif main.AZURE_STORAGE_AUTH_MODE in {"auto", "sas"} and account_url and main.AZURE_STORAGE_SAS_TOKEN:
            self.service_client = BlobServiceClient(
                account_url=account_url,
                credential=main.AZURE_STORAGE_SAS_TOKEN.lstrip("?"),
            )
        elif main.AZURE_STORAGE_AUTH_MODE in {"auto", "managed_identity"} and account_url:
            from azure.identity import DefaultAzureCredential

            credential_kwargs = (
                {"managed_identity_client_id": main.AZURE_STORAGE_CLIENT_ID}
                if main.AZURE_STORAGE_CLIENT_ID
                else {}
            )
            self.service_client = BlobServiceClient(
                account_url=account_url,
                credential=DefaultAzureCredential(**credential_kwargs),
            )
        else:
            raise ValueError(
                "Azure sidecar storage requires a connection string, account key, "
                "or managed identity with account name/account URL."
            )
        self.container_client = self.service_client.get_container_client(main.AZURE_STORAGE_CONTAINER)

    async def upload(self, local_path: str, remote_key: str) -> bool:
        try:
            content_type = main._detect_content_type(local_path)
            blob_client = self.container_client.get_blob_client(remote_key)
            await asyncio.to_thread(
                self._upload_blob,
                blob_client,
                local_path,
                content_type,
            )
            logger.info(f"[Azure] Uploaded {local_path} to {main.AZURE_STORAGE_CONTAINER}/{remote_key}")
            return True
        except Exception as e:
            logger.error(f"[Azure] Upload failed for {local_path}: {e}")
            return False

    @staticmethod
    def _upload_blob(blob_client, local_path: str, content_type: str) -> None:
        from azure.storage.blob import ContentSettings
        with open(local_path, "rb") as f:
            blob_client.upload_blob(
                f,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )

    async def download(self, remote_key: str, local_path: str) -> bool:
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            blob_client = self.container_client.get_blob_client(remote_key)
            await asyncio.to_thread(self._download_blob, blob_client, local_path)
            logger.info(f"[Azure] Downloaded {main.AZURE_STORAGE_CONTAINER}/{remote_key} to {local_path}")
            return True
        except Exception as e:
            if "ResourceNotFoundError" in str(type(e)):
                logger.warning(f"[Azure] Blob not found: {remote_key}")
            else:
                logger.error(f"[Azure] Download failed for {remote_key}: {e}")
            return False

    @staticmethod
    def _download_blob(blob_client, local_path: str) -> None:
        with open(local_path, "wb") as f:
            download_stream = blob_client.download_blob()
            f.write(download_stream.readall())

    async def list_objects(self, prefix: str) -> list[str]:
        def _list() -> list[str]:
            keys: list[str] = []
            blobs = self.container_client.list_blobs(name_starts_with=prefix)
            for blob in blobs:
                keys.append(blob.name)
            return keys

        try:
            return await asyncio.to_thread(_list)
        except Exception as e:
            logger.error(f"[Azure] List failed for prefix {prefix}: {e}")
            return []


class GCSBackend(StorageBackend):
    """Google Cloud Storage backend.

    Auth defaults to Application Default Credentials, which covers GKE
    Workload Identity for free -- no bespoke credential-mode branching
    needed here, unlike Azure's connection-string/account-key/SAS/
    managed-identity fan-out above.
    """

    def __init__(self):
        from google.cloud import storage as gcs_storage

        kwargs = {}
        if main.GCS_PROJECT:
            kwargs["project"] = main.GCS_PROJECT
        self.client = gcs_storage.Client(**kwargs)
        self.bucket_name = main.GCS_BUCKET
        self.bucket = self.client.bucket(self.bucket_name)

    async def upload(self, local_path: str, remote_key: str) -> bool:
        try:
            content_type = main._detect_content_type(local_path)
            blob = self.bucket.blob(remote_key)
            await asyncio.to_thread(
                blob.upload_from_filename,
                local_path,
                content_type=content_type,
            )
            logger.info(f"[GCS] Uploaded {local_path} to gs://{self.bucket_name}/{remote_key}")
            return True
        except Exception as e:
            logger.error(f"[GCS] Upload failed for {local_path}: {e}")
            return False

    async def download(self, remote_key: str, local_path: str) -> bool:
        from google.cloud.exceptions import NotFound

        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            blob = self.bucket.blob(remote_key)
            await asyncio.to_thread(blob.download_to_filename, local_path)
            logger.info(f"[GCS] Downloaded gs://{self.bucket_name}/{remote_key} to {local_path}")
            return True
        except NotFound:
            logger.warning(f"[GCS] Object not found: {remote_key}")
            return False
        except Exception as e:
            logger.error(f"[GCS] Download failed for {remote_key}: {e}")
            return False

    async def list_objects(self, prefix: str) -> list[str]:
        def _list() -> list[str]:
            return [blob.name for blob in self.bucket.list_blobs(prefix=prefix)]

        try:
            return await asyncio.to_thread(_list)
        except Exception as e:
            logger.error(f"[GCS] List failed for prefix {prefix}: {e}")
            return []


def get_storage_backend() -> StorageBackend:
    """Get the configured storage backend."""
    if main.STORAGE_BACKEND == "azure":
        return AzureBlobBackend()
    elif main.STORAGE_BACKEND == "gcs":
        return GCSBackend()
    else:
        return S3Backend()


# Lazy-initialized storage backend
_storage_backend: Optional[StorageBackend] = None


def storage() -> StorageBackend:
    """Get or create the storage backend singleton."""
    global _storage_backend
    if _storage_backend is None:
        _storage_backend = get_storage_backend()
        logger.info(f"[Storage] Initialized {main.STORAGE_BACKEND} backend")
    return _storage_backend


# ============================================================================
# Read-only S3 FUSE bucket mounting (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md,
# §2.2 option 2 -- mounted from the sidecar container, which already holds
# CAP_SYS_ADMIN for nsenter, so this needs no NEW capability grant).
#
# What this section deliberately does NOT do: actually perform a live
# mount. Three separate, unreviewed things would need to happen first,
# each requiring its own maintainer sign-off per SECURITY.md's bar for new
# attack surface, none of which this pass attempts:
#   1. /dev/fuse device access granted to the sidecar container in
#      deploy/pod-template.yaml (not present today -- CAP_SYS_ADMIN alone
#      does not grant access to a device node that isn't mounted into the
#      container at all).
#   2. An actual FUSE client binary (s3fs-fuse, goofys, or rclone --
#      s3fs-fuse chosen here as the most mature single-cloud option, per
#      the design doc's "read-only, single-provider first" recommendation)
#      installed in deploy/sidecar.Dockerfile -- not present today.
#   3. A credential-handling decision for the bucket's own access
#      key/secret (a NEW credential type entering the pod, per the design
#      doc's §5) -- not scoped here at all.
# What IS real and unit-tested below: the command-construction logic
# itself (build_s3fs_mount_command/build_s3fs_unmount_command), so that
# once 1-3 above are each explicitly reviewed and done, wiring up the
# route is a small, mechanical change rather than starting from zero.
# ============================================================================


def build_s3fs_mount_command(*, bucket: str, mount_path: str, region: str = "us-east-1") -> list[str]:
    """Returns the argv for a read-only s3fs-fuse mount of `bucket` at
    `mount_path`. Pure, unit-testable function -- does not touch the
    filesystem, does not require s3fs to be installed, does not require
    /dev/fuse to exist. Mirrors control-plane's build_pvc_spec/
    build_job_spec pattern: the command shape is the reviewable,
    unit-tested artifact; actually executing it is a separate, gated step.

    `-o ro` (read-only) is non-negotiable here, matching the design doc's
    §2.3 "read-only mirrors first" scope decision -- this function has no
    read-write mode at all, not even behind a parameter, so a future
    caller can't accidentally flip it on without also updating this
    function (and re-reading why read-write was deliberately deferred).
    """
    if not bucket or "/" in bucket or bucket in {".", ".."}:
        raise ValueError(f"Invalid bucket name: {bucket!r}")
    if not mount_path.startswith("/") or mount_path == "/":
        raise ValueError(f"mount_path must be an absolute, non-root path: {mount_path!r}")

    return [
        "s3fs",
        bucket,
        mount_path,
        "-o", "ro",  # SECURITY: read-only -- see this function's own docstring
        "-o", f"endpoint={region}",
        "-o", "allow_other",
        "-o", "iam_role=auto",  # credential source is a separate, unreviewed decision -- see module docstring
    ]


def build_s3fs_unmount_command(*, mount_path: str) -> list[str]:
    """Returns the argv to cleanly unmount a bucket mounted by
    build_s3fs_mount_command -- fusermount -u, not a raw umount, so a busy
    mount fails with a clear "device or resource busy" rather than
    force-unmounting out from under an in-flight read (see this design
    doc's §2.1 "unclean kill can leave the mountpoint in a stuck state")."""
    if not mount_path.startswith("/") or mount_path == "/":
        raise ValueError(f"mount_path must be an absolute, non-root path: {mount_path!r}")
    return ["fusermount", "-u", mount_path]


@router.post("/mount-bucket", response_model=main.MountBucketResponse)
async def mount_bucket(req: main.MountBucketRequest):
    """Fails closed at every layer that isn't reviewed/wired yet -- see
    this section's own module-level docstring for the full list. Even
    with BOXKITE_FUSE_MOUNT_ENABLED=true, this 501s unless /dev/fuse is
    actually present, which it is not in any reference manifest today.
    """
    if not main.BOXKITE_FUSE_MOUNT_ENABLED:
        raise HTTPException(status_code=404, detail="FUSE bucket mounting is not enabled on this deployment.")

    if not os.path.exists(main.FUSE_DEVICE_PATH):
        raise HTTPException(
            status_code=501,
            detail=(
                f"{main.FUSE_DEVICE_PATH} is not present in this container. Granting device access requires "
                "a deploy/pod-template.yaml change and its own security review -- see "
                "docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md. Not enabled by this flag alone."
            ),
        )

    # Unreachable until the device grant + s3fs binary + credential
    # decision above are each made -- build_s3fs_mount_command exists and
    # is unit-tested, but this route does not yet call it, deliberately.
    raise HTTPException(
        status_code=501,
        detail="FUSE mount execution is not implemented yet -- see docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md.",
    )
