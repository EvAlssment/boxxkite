"""Tests for the Azure Key Vault and GCP Cloud KMS `SecretsKmsClient`
backends (GitHub issue #127, docs/SECRETS-DESIGN.md section 4/6).

Neither `azure-keyvault-keys`/`azure-identity` nor `google-cloud-kms` is an
installed dependency of this test environment (same posture as `boto3` for
`AwsKmsSecretsClient` -- see secrets_kms.py's docstring: these SDKs are only
ever lazily imported inside the relevant class's `__init__`, precisely so a
deployment that never configures that backend doesn't need the SDK
installed at all). Real cloud credentials are never available here either.

So, mirroring this repo's existing fake-transport precedent for cloud SDKs
(`conftest.py`'s `FakeSnapshotStorageClient`), these tests inject fake
modules into `sys.modules` for the exact dotted import paths
`AzureKeyVaultSecretsKmsClient`/`GcpCloudKmsSecretsKmsClient` use, so the
classes' own lazy imports resolve to test doubles that implement just the
wrap/unwrap (Azure) and encrypt/decrypt (GCP) primitives actually used --
never a broader mock of the entire vendor SDK.
"""

from __future__ import annotations

import base64
import os
import sys
import types

import pytest

from control_plane import secrets_kms
from control_plane.config import settings


def _install_fake_module(name: str, module: types.ModuleType) -> None:
    """Register `module` at `sys.modules[name]` and, if its parent package
    is already registered, also set it as an attribute on the parent --
    belt-and-suspenders so `from a.b import c` resolves `c` via a direct
    getattr on `a.b` without relying on import-machinery fallback behavior
    for an already-cached submodule."""
    sys.modules[name] = module
    if "." in name:
        parent_name, attr = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, attr, module)


class _FakeAzureWrapResult:
    def __init__(self, encrypted_key: bytes) -> None:
        self.encrypted_key = encrypted_key


class _FakeAzureUnwrapResult:
    def __init__(self, key: bytes) -> None:
        self.key = key


class _FakeCryptographyClient:
    """Stands in for `azure.keyvault.keys.crypto.CryptographyClient`. Wraps
    with a fake-HSM key *derived from `key_id`* (not a single fixed
    constant) -- not a stand-in for real Key Vault security properties, but
    enough to prove both `AzureKeyVaultSecretsKmsClient`'s envelope-
    encryption plumbing AND that `decrypt()` actually unwraps against the
    key that wrapped a given record, not whichever key is currently
    configured: unwrapping with a *different* `key_id` than the one used to
    wrap produces garbage/fails here, exactly as real Key Vault would."""

    def __init__(self, key_id: str, credential: object) -> None:
        self.key_id = key_id
        self.credential = credential

    def _vault_key_for(self, key_id: str) -> bytes:
        import hashlib

        return hashlib.sha256(key_id.encode("utf-8")).digest()

    def wrap_key(self, algorithm: str, key: bytes) -> _FakeAzureWrapResult:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ciphertext = AESGCM(self._vault_key_for(self.key_id)).encrypt(nonce, key, None)
        return _FakeAzureWrapResult(encrypted_key=nonce + ciphertext)

    def unwrap_key(self, algorithm: str, encrypted_key: bytes) -> _FakeAzureUnwrapResult:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce, ciphertext = encrypted_key[:12], encrypted_key[12:]
        plaintext = AESGCM(self._vault_key_for(self.key_id)).decrypt(nonce, ciphertext, None)
        return _FakeAzureUnwrapResult(key=plaintext)


class _FakeKeyWrapAlgorithm:
    rsa_oaep_256 = "RSA-OAEP-256"


class _FakeDefaultAzureCredential:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


@pytest.fixture
def fake_azure_sdk(monkeypatch: pytest.MonkeyPatch):
    for name in [
        "azure",
        "azure.identity",
        "azure.keyvault",
        "azure.keyvault.keys",
        "azure.keyvault.keys.crypto",
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    azure_module = types.ModuleType("azure")
    identity_module = types.ModuleType("azure.identity")
    identity_module.DefaultAzureCredential = _FakeDefaultAzureCredential
    keyvault_module = types.ModuleType("azure.keyvault")
    keys_module = types.ModuleType("azure.keyvault.keys")
    crypto_module = types.ModuleType("azure.keyvault.keys.crypto")
    crypto_module.CryptographyClient = _FakeCryptographyClient
    crypto_module.KeyWrapAlgorithm = _FakeKeyWrapAlgorithm

    for name, module in [
        ("azure", azure_module),
        ("azure.identity", identity_module),
        ("azure.keyvault", keyvault_module),
        ("azure.keyvault.keys", keys_module),
        ("azure.keyvault.keys.crypto", crypto_module),
    ]:
        monkeypatch.setitem(sys.modules, name, module)
        _install_fake_module(name, module)

    yield


class _FakeGcpEncryptResponse:
    def __init__(self, ciphertext: bytes) -> None:
        self.ciphertext = ciphertext


class _FakeGcpDecryptResponse:
    def __init__(self, plaintext: bytes) -> None:
        self.plaintext = plaintext


class _FakeKeyManagementServiceClient:
    """Stands in for `google.cloud.kms.KeyManagementServiceClient`. Cloud
    KMS has no separate GenerateDataKey call the way AWS KMS does -- its
    symmetric `encrypt`/`decrypt` on a CryptoKey resource IS the wrap/unwrap
    step `GcpCloudKmsSecretsKmsClient` uses it for."""

    _FAKE_VAULT_KEY = b"\x24" * 32

    def encrypt(self, request: dict) -> _FakeGcpEncryptResponse:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ciphertext = AESGCM(self._FAKE_VAULT_KEY).encrypt(nonce, request["plaintext"], None)
        return _FakeGcpEncryptResponse(ciphertext=nonce + ciphertext)

    def decrypt(self, request: dict) -> _FakeGcpDecryptResponse:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        blob = request["ciphertext"]
        nonce, ciphertext = blob[:12], blob[12:]
        plaintext = AESGCM(self._FAKE_VAULT_KEY).decrypt(nonce, ciphertext, None)
        return _FakeGcpDecryptResponse(plaintext=plaintext)


@pytest.fixture
def fake_gcp_sdk(monkeypatch: pytest.MonkeyPatch):
    for name in ["google", "google.cloud", "google.cloud.kms"]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    google_module = types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    kms_module = types.ModuleType("google.cloud.kms")
    kms_module.KeyManagementServiceClient = _FakeKeyManagementServiceClient

    for name, module in [
        ("google", google_module),
        ("google.cloud", cloud_module),
        ("google.cloud.kms", kms_module),
    ]:
        monkeypatch.setitem(sys.modules, name, module)
        _install_fake_module(name, module)

    yield


@pytest.fixture(autouse=True)
def _reset_kms_state(monkeypatch: pytest.MonkeyPatch):
    original_backend = settings.SECRETS_KMS_BACKEND
    original_key_id = settings.SECRETS_KMS_KEY_ID
    secrets_kms.reset_secrets_kms_client_for_tests()
    yield
    settings.SECRETS_KMS_BACKEND = original_backend
    settings.SECRETS_KMS_KEY_ID = original_key_id
    secrets_kms.reset_secrets_kms_client_for_tests()


def test_azure_backend_requires_key_id(fake_azure_sdk, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "SECRETS_KMS_KEY_ID", "")
    with pytest.raises(ValueError, match="SECRETS_KMS_KEY_ID"):
        secrets_kms.AzureKeyVaultSecretsKmsClient()


def test_azure_backend_round_trip_recovers_plaintext(
    fake_azure_sdk, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        settings,
        "SECRETS_KMS_KEY_ID",
        "https://fake-vault.vault.azure.net/keys/fake-key/abc123",
    )
    client = secrets_kms.AzureKeyVaultSecretsKmsClient()

    plaintext = "sk_live_azure_secret_value"
    encrypted = client.encrypt(plaintext)

    assert plaintext not in encrypted.ciphertext_b64
    assert plaintext not in encrypted.wrapped_data_key_b64
    assert encrypted.encryption_key_id == settings.SECRETS_KMS_KEY_ID

    assert client.decrypt(encrypted) == plaintext


def test_azure_backend_decrypts_after_key_rotation_using_the_persisted_key_id(
    fake_azure_sdk, monkeypatch: pytest.MonkeyPatch
):
    """Regression test: a secret encrypted under key-v1 must still decrypt
    correctly after SECRETS_KMS_KEY_ID is rotated to point at key-v2 -- the
    client must unwrap using `encrypted.encryption_key_id` (the key that
    actually wrapped this record), never whichever key is currently
    configured. Before the fix, this failed: decrypt() always used the
    client's own `self._key_id`/`self._client`, so a record wrapped under
    key-v1 became silently permanently undecryptable once the process was
    reconfigured (or restarted) with key-v2 as the active key."""
    key_v1 = "https://fake-vault.vault.azure.net/keys/fake-key/v1"
    key_v2 = "https://fake-vault.vault.azure.net/keys/fake-key/v2"

    monkeypatch.setattr(settings, "SECRETS_KMS_KEY_ID", key_v1)
    client_v1 = secrets_kms.AzureKeyVaultSecretsKmsClient()
    encrypted = client_v1.encrypt("secret-wrapped-under-v1")
    assert encrypted.encryption_key_id == key_v1

    # Simulate a key rotation: a fresh client instance configured with the
    # NEW key (as a real process restart after rotating SECRETS_KMS_KEY_ID
    # would produce), asked to decrypt a record wrapped under the OLD key.
    monkeypatch.setattr(settings, "SECRETS_KMS_KEY_ID", key_v2)
    client_v2 = secrets_kms.AzureKeyVaultSecretsKmsClient()

    assert client_v2.decrypt(encrypted) == "secret-wrapped-under-v1"


def test_azure_backend_produces_different_ciphertext_for_same_plaintext(
    fake_azure_sdk, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        settings, "SECRETS_KMS_KEY_ID", "https://fake-vault.vault.azure.net/keys/fake-key/abc123"
    )
    client = secrets_kms.AzureKeyVaultSecretsKmsClient()

    a = client.encrypt("same-value")
    b = client.encrypt("same-value")
    assert a.ciphertext_b64 != b.ciphertext_b64 or a.wrapped_data_key_b64 != b.wrapped_data_key_b64


def test_azure_backend_selected_via_get_secrets_kms_client(
    fake_azure_sdk, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "SECRETS_KMS_BACKEND", "azure")
    monkeypatch.setattr(
        settings, "SECRETS_KMS_KEY_ID", "https://fake-vault.vault.azure.net/keys/fake-key/abc123"
    )
    client = secrets_kms.get_secrets_kms_client()
    assert isinstance(client, secrets_kms.AzureKeyVaultSecretsKmsClient)


def test_gcp_backend_requires_key_id(fake_gcp_sdk, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "SECRETS_KMS_KEY_ID", "")
    with pytest.raises(ValueError, match="SECRETS_KMS_KEY_ID"):
        secrets_kms.GcpCloudKmsSecretsKmsClient()


def test_gcp_backend_round_trip_recovers_plaintext(fake_gcp_sdk, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        settings,
        "SECRETS_KMS_KEY_ID",
        "projects/fake-project/locations/us-central1/keyRings/fake-ring/cryptoKeys/fake-key",
    )
    client = secrets_kms.GcpCloudKmsSecretsKmsClient()

    plaintext = "sk_live_gcp_secret_value"
    encrypted = client.encrypt(plaintext)

    assert plaintext not in encrypted.ciphertext_b64
    assert plaintext not in encrypted.wrapped_data_key_b64
    assert encrypted.encryption_key_id == settings.SECRETS_KMS_KEY_ID

    assert client.decrypt(encrypted) == plaintext


def test_gcp_backend_produces_different_ciphertext_for_same_plaintext(
    fake_gcp_sdk, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        settings,
        "SECRETS_KMS_KEY_ID",
        "projects/fake-project/locations/us-central1/keyRings/fake-ring/cryptoKeys/fake-key",
    )
    client = secrets_kms.GcpCloudKmsSecretsKmsClient()

    a = client.encrypt("same-value")
    b = client.encrypt("same-value")
    assert a.ciphertext_b64 != b.ciphertext_b64 or a.wrapped_data_key_b64 != b.wrapped_data_key_b64


def test_gcp_backend_selected_via_get_secrets_kms_client(
    fake_gcp_sdk, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "SECRETS_KMS_BACKEND", "gcp")
    monkeypatch.setattr(
        settings,
        "SECRETS_KMS_KEY_ID",
        "projects/fake-project/locations/us-central1/keyRings/fake-ring/cryptoKeys/fake-key",
    )
    client = secrets_kms.get_secrets_kms_client()
    assert isinstance(client, secrets_kms.GcpCloudKmsSecretsKmsClient)


def test_unknown_backend_falls_back_to_local_dev(monkeypatch: pytest.MonkeyPatch):
    """Unrecognized `SECRETS_KMS_BACKEND` values fall back to the local/dev
    backend, matching the existing dispatcher's `else` branch -- not a
    silent no-op, but also not a hard error, consistent with how "aws" was
    the only special-cased value before this change."""
    monkeypatch.setattr(settings, "SECRETS_KMS_BACKEND", "not-a-real-backend")
    client = secrets_kms.get_secrets_kms_client()
    assert isinstance(client, secrets_kms.LocalDevSecretsKmsClient)
