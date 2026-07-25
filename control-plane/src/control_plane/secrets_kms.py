"""Reversible envelope-encryption primitive for org-scoped secrets
(docs/SECRETS-DESIGN.md section 4/6).

This is a genuinely new primitive for this codebase: `security.py:hash_secret`
/`ApiKeyRepository` deliberately only ever store a one-way SHA-256 digest,
which is correct for "does this match" (API keys) but architecturally the
wrong tool for secrets a session needs the *real* value of at request time
(see the design doc's explicit callout on this point). This module is the
new, separate primitive that fills that gap.

Follows the same environment-dependent-backend shape already established by
`storage_client.py` (`SnapshotStorageClient` Protocol +
`S3SnapshotStorageClient`/`AzureSnapshotStorageClient` + a lazily-initialized,
test-resettable module singleton): a `SecretsKmsClient` Protocol, real
AWS-KMS/Azure-Key-Vault/GCP-Cloud-KMS-backed implementations, and a clearly-
marked local/dev implementation for environments (like this one) without a
real KMS available. Uses its own dedicated key (`SECRETS_KMS_KEY_ID` /
`SECRETS_LOCAL_DEV_KMS_KEY`) -- the same setting name across all three cloud
backends, never a separate `SECRETS_KMS_AZURE_KEY_ID`/`SECRETS_KMS_GCP_KEY_ID`
-- and never the storage-sync KMS key either: different blast radius,
different IAM policy, no reason to couple any of these (see the design
doc's security section).

Envelope encryption shape (every backend): a fresh random AES-256-GCM data
key is generated per secret value. Each real KMS backend wraps that data key
with its own cloud KMS's wrap/encrypt call (AWS `GenerateDataKey`, Azure Key
Vault `wrap_key`, GCP Cloud KMS symmetric `encrypt`) so the durable, at-rest
artifact is the KMS-wrapped data key, not the plaintext data key; the
local/dev backend wraps it with a local AES-256-GCM "key-wrapping key" read
from `SECRETS_LOCAL_DEV_KMS_KEY` instead of a real KMS. Either way, the
actual secret plaintext is only ever encrypted with the per-secret data key,
never directly with the long-lived wrapping key -- limits how much
cipher-text is ever encrypted under any single fixed key.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from .config import settings

logger = logging.getLogger(__name__)

_NONCE_LEN = 12  # AES-GCM standard nonce length.
_DATA_KEY_LEN = 32  # AES-256.


@dataclass(frozen=True)
class EncryptedSecret:
    """Everything needed to decrypt a secret value later. All fields are
    safe to persist in the `secrets` table as-is -- none of them is the
    plaintext, and the wrapped data key is useless without the wrapping
    key/KMS key that wrapped it."""

    ciphertext_b64: str
    nonce_b64: str
    wrapped_data_key_b64: str
    encryption_key_id: str


class SecretsKmsClient(Protocol):
    """Backend-agnostic interface the secrets repository depends on."""

    def encrypt(self, plaintext: str) -> EncryptedSecret: ...

    def decrypt(self, encrypted: EncryptedSecret) -> str: ...


def _aes_gcm_encrypt(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(_NONCE_LEN)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return ciphertext, nonce


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)


class AwsKmsSecretsClient:
    """Real envelope encryption via AWS KMS `GenerateDataKey`/`Decrypt`.

    Uses `SECRETS_KMS_KEY_ID` -- a dedicated key, never
    `SNAPSHOT_STORAGE_S3_KMS_KEY_ID` (storage_client.py's key), per the
    design doc's explicit "no reason to couple them" guidance.
    """

    def __init__(self) -> None:
        import boto3

        if not settings.SECRETS_KMS_KEY_ID:
            raise ValueError(
                "SECRETS_KMS_BACKEND=aws requires SECRETS_KMS_KEY_ID to be configured"
            )
        self._client = boto3.client("kms", region_name=settings.SECRETS_KMS_AWS_REGION)
        self._key_id = settings.SECRETS_KMS_KEY_ID

    def encrypt(self, plaintext: str) -> EncryptedSecret:
        response = self._client.generate_data_key(KeyId=self._key_id, KeySpec="AES_256")
        data_key = response["Plaintext"]
        wrapped_data_key = response["CiphertextBlob"]
        try:
            ciphertext, nonce = _aes_gcm_encrypt(data_key, plaintext.encode("utf-8"))
        finally:
            # Best-effort: Python bytes are immutable so this can't truly
            # zero the plaintext data key in memory, but drop our only
            # reference to it as soon as it's no longer needed.
            del data_key
        return EncryptedSecret(
            ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
            nonce_b64=base64.b64encode(nonce).decode("ascii"),
            wrapped_data_key_b64=base64.b64encode(wrapped_data_key).decode("ascii"),
            encryption_key_id=self._key_id,
        )

    def decrypt(self, encrypted: EncryptedSecret) -> str:
        wrapped_data_key = base64.b64decode(encrypted.wrapped_data_key_b64)
        response = self._client.decrypt(
            CiphertextBlob=wrapped_data_key, KeyId=encrypted.encryption_key_id
        )
        data_key = response["Plaintext"]
        plaintext = _aes_gcm_decrypt(
            data_key,
            base64.b64decode(encrypted.nonce_b64),
            base64.b64decode(encrypted.ciphertext_b64),
        )
        return plaintext.decode("utf-8")


class AzureKeyVaultSecretsKmsClient:
    """Real envelope encryption via Azure Key Vault key wrap/unwrap
    (`CryptographyClient.wrap_key`/`unwrap_key`, RSA-OAEP-256).

    Uses `SECRETS_KMS_KEY_ID` -- here, a full Key Vault key identifier URL
    (e.g. `https://<vault>.vault.azure.net/keys/<name>/<version>`) -- the
    same setting the AWS backend uses for its own KeyId, never a new
    Azure-specific key setting, per the design doc's "no reason to invent a
    parallel setting per cloud" guidance. Authenticates via
    `DefaultAzureCredential`, mirroring `storage_client.py`'s
    `AzureSnapshotStorageClient` ambient-identity path -- Key Vault key
    operations are always AAD-authenticated, so there's no connection-string
    alternative here the way there is for Blob Storage.
    """

    def __init__(self) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm

        if not settings.SECRETS_KMS_KEY_ID:
            raise ValueError(
                "SECRETS_KMS_BACKEND=azure requires SECRETS_KMS_KEY_ID to be configured "
                "(a full Key Vault key identifier URL)"
            )
        self._key_id = settings.SECRETS_KMS_KEY_ID
        self._algorithm = KeyWrapAlgorithm.rsa_oaep_256
        self._credential = DefaultAzureCredential()
        self._client = self._client_for_key(self._key_id)

    def _client_for_key(self, key_id: str):
        from azure.keyvault.keys.crypto import CryptographyClient

        return CryptographyClient(key_id, self._credential)

    def encrypt(self, plaintext: str) -> EncryptedSecret:
        data_key = os.urandom(_DATA_KEY_LEN)
        try:
            ciphertext, nonce = _aes_gcm_encrypt(data_key, plaintext.encode("utf-8"))
            wrap_result = self._client.wrap_key(self._algorithm, data_key)
        finally:
            del data_key
        return EncryptedSecret(
            ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
            nonce_b64=base64.b64encode(nonce).decode("ascii"),
            wrapped_data_key_b64=base64.b64encode(wrap_result.encrypted_key).decode("ascii"),
            encryption_key_id=self._key_id,
        )

    def decrypt(self, encrypted: EncryptedSecret) -> str:
        # SECURITY/CORRECTNESS: must unwrap against the key that actually
        # wrapped this record (`encrypted.encryption_key_id`), not whichever
        # key is currently configured (`self._key_id`) -- a key rotation
        # (SECRETS_KMS_KEY_ID pointed at a new key/version) would otherwise
        # make every secret wrapped under the OLD key permanently
        # undecryptable, since Key Vault correctly refuses to unwrap with the
        # wrong key. Mirrors the AWS/GCP backends, which already pass the
        # persisted key id explicitly on every decrypt call.
        client = (
            self._client
            if encrypted.encryption_key_id == self._key_id
            else self._client_for_key(encrypted.encryption_key_id)
        )
        wrapped_data_key = base64.b64decode(encrypted.wrapped_data_key_b64)
        unwrap_result = client.unwrap_key(self._algorithm, wrapped_data_key)
        data_key = unwrap_result.key
        plaintext = _aes_gcm_decrypt(
            data_key,
            base64.b64decode(encrypted.nonce_b64),
            base64.b64decode(encrypted.ciphertext_b64),
        )
        return plaintext.decode("utf-8")


class GcpCloudKmsSecretsKmsClient:
    """Real envelope encryption via GCP Cloud KMS symmetric encrypt/decrypt
    (`KeyManagementServiceClient.encrypt`/`decrypt`).

    Cloud KMS has no separate `GenerateDataKey` primitive the way AWS KMS
    does, so this class generates its own random AES-256 data key locally
    (same as the AWS and local-dev backends) and uses Cloud KMS's `encrypt`
    call purely as the "wrap" step for that data key -- never for the
    actual secret plaintext, which is only ever encrypted locally with the
    per-secret data key.

    Uses `SECRETS_KMS_KEY_ID` -- here, a full Cloud KMS CryptoKey resource
    name (`projects/*/locations/*/keyRings/*/cryptoKeys/*`), deliberately
    versionless: Cloud KMS resolves the correct key version for decryption
    from the ciphertext itself, so no version needs to be recorded
    alongside it. Authenticates via Application Default Credentials, the
    GCP equivalent of `DefaultAzureCredential`/boto3's default credential
    chain -- no explicit credential setting here either, same reasoning as
    the Azure backend above.
    """

    def __init__(self) -> None:
        from google.cloud import kms

        if not settings.SECRETS_KMS_KEY_ID:
            raise ValueError(
                "SECRETS_KMS_BACKEND=gcp requires SECRETS_KMS_KEY_ID to be configured "
                "(a full Cloud KMS CryptoKey resource name)"
            )
        self._key_name = settings.SECRETS_KMS_KEY_ID
        self._client = kms.KeyManagementServiceClient()

    def encrypt(self, plaintext: str) -> EncryptedSecret:
        data_key = os.urandom(_DATA_KEY_LEN)
        try:
            ciphertext, nonce = _aes_gcm_encrypt(data_key, plaintext.encode("utf-8"))
            wrap_response = self._client.encrypt(
                request={"name": self._key_name, "plaintext": data_key}
            )
        finally:
            del data_key
        return EncryptedSecret(
            ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
            nonce_b64=base64.b64encode(nonce).decode("ascii"),
            wrapped_data_key_b64=base64.b64encode(wrap_response.ciphertext).decode("ascii"),
            encryption_key_id=self._key_name,
        )

    def decrypt(self, encrypted: EncryptedSecret) -> str:
        wrapped_data_key = base64.b64decode(encrypted.wrapped_data_key_b64)
        unwrap_response = self._client.decrypt(
            request={"name": encrypted.encryption_key_id, "ciphertext": wrapped_data_key}
        )
        data_key = unwrap_response.plaintext
        plaintext = _aes_gcm_decrypt(
            data_key,
            base64.b64decode(encrypted.nonce_b64),
            base64.b64decode(encrypted.ciphertext_b64),
        )
        return plaintext.decode("utf-8")


class LocalDevSecretsKmsClient:
    """*** LOCAL/DEV ONLY -- NOT A REAL KMS. ***

    Stands in for `AwsKmsSecretsClient` when no real KMS is configured (e.g.
    local development, this environment's own test/CI run, or a self-hoster
    who hasn't wired a cloud KMS yet), following the exact "environment-
    dependent primitive" shape the rest of this codebase already uses (see
    `aws_identity.py`/`azure_identity.py` for the credential-resolution
    equivalent, and `storage_client.py`'s S3/Azure backend split for the
    architectural pattern this class mirrors).

    The data-key "wrapping" step uses a local AES-256-GCM key read from
    `SECRETS_LOCAL_DEV_KMS_KEY` (a base64-encoded 32-byte key) instead of a
    real KMS `Encrypt` call -- there is no HSM-backed root of trust here,
    only a symmetric key that lives in this process's own configuration.
    This is explicitly the same trust tier as any other "local dev secret in
    an env var" in this project and must never be treated as equivalent to
    a real KMS-backed deployment. A production deployment MUST set
    `SECRETS_KMS_BACKEND=aws` (or a future equivalent) with a real
    `SECRETS_KMS_KEY_ID` -- this class logs a loud warning on construction
    specifically so that never happens silently.
    """

    def __init__(self) -> None:
        raw_key = settings.SECRETS_LOCAL_DEV_KMS_KEY
        if not raw_key:
            # Zero-config local dev: derive a process-local key so tests and
            # `boxkite up` style local runs work without any setup, at the
            # cost of secrets not surviving a process restart (matching
            # JWT_SECRET's own "insecure default, fine for dev" posture).
            logger.warning(
                "[SecretsKMS] SECRETS_LOCAL_DEV_KMS_KEY is unset -- generating an "
                "ephemeral, process-local wrapping key. Secrets encrypted under it "
                "will NOT be decryptable after a restart. Set SECRETS_LOCAL_DEV_KMS_KEY "
                "(a stable base64-encoded 32-byte key) for anything beyond a single "
                "local session, and set SECRETS_KMS_BACKEND=aws with a real "
                "SECRETS_KMS_KEY_ID before handling real credentials."
            )
            self._wrapping_key = os.urandom(_DATA_KEY_LEN)
        else:
            self._wrapping_key = base64.b64decode(raw_key)
            if len(self._wrapping_key) != _DATA_KEY_LEN:
                raise ValueError(
                    "SECRETS_LOCAL_DEV_KMS_KEY must decode to exactly 32 bytes (AES-256)"
                )
        logger.warning(
            "[SecretsKMS] Using LocalDevSecretsKmsClient -- this is NOT a real KMS. "
            "Secrets are envelope-encrypted with a local symmetric key, not an "
            "HSM-backed one. Set SECRETS_KMS_BACKEND=aws for production deployments."
        )

    def encrypt(self, plaintext: str) -> EncryptedSecret:
        data_key = os.urandom(_DATA_KEY_LEN)
        ciphertext, nonce = _aes_gcm_encrypt(data_key, plaintext.encode("utf-8"))
        wrapped_data_key, wrap_nonce = _aes_gcm_encrypt(self._wrapping_key, data_key)
        return EncryptedSecret(
            ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
            nonce_b64=base64.b64encode(nonce).decode("ascii"),
            # Store the wrap nonce alongside the wrapped key itself (colon-
            # separated, both already base64) since this backend, unlike the
            # AWS one, has no KMS-side ciphertext blob format of its own to
            # carry it for us.
            wrapped_data_key_b64=f"{base64.b64encode(wrap_nonce).decode('ascii')}:{base64.b64encode(wrapped_data_key).decode('ascii')}",
            encryption_key_id="local-dev",
        )

    def decrypt(self, encrypted: EncryptedSecret) -> str:
        wrap_nonce_b64, wrapped_data_key_b64 = encrypted.wrapped_data_key_b64.split(":", 1)
        data_key = _aes_gcm_decrypt(
            self._wrapping_key,
            base64.b64decode(wrap_nonce_b64),
            base64.b64decode(wrapped_data_key_b64),
        )
        plaintext = _aes_gcm_decrypt(
            data_key,
            base64.b64decode(encrypted.nonce_b64),
            base64.b64decode(encrypted.ciphertext_b64),
        )
        return plaintext.decode("utf-8")


_secrets_kms_client: SecretsKmsClient | None = None


def get_secrets_kms_client() -> SecretsKmsClient:
    """Lazily-initialized singleton, overridable in tests via
    `app.dependency_overrides` is not applicable here (this isn't a FastAPI
    dependency) -- tests instead call `reset_secrets_kms_client_for_tests()`
    between cases, mirroring `storage_client.py`'s equivalent."""
    global _secrets_kms_client
    if _secrets_kms_client is None:
        if settings.SECRETS_KMS_BACKEND == "aws":
            _secrets_kms_client = AwsKmsSecretsClient()
        elif settings.SECRETS_KMS_BACKEND == "azure":
            _secrets_kms_client = AzureKeyVaultSecretsKmsClient()
        elif settings.SECRETS_KMS_BACKEND == "gcp":
            _secrets_kms_client = GcpCloudKmsSecretsKmsClient()
        else:
            _secrets_kms_client = LocalDevSecretsKmsClient()
    return _secrets_kms_client


def reset_secrets_kms_client_for_tests() -> None:
    global _secrets_kms_client
    _secrets_kms_client = None
