from __future__ import annotations

from types import SimpleNamespace

import pytest

from control_plane.config import settings
from control_plane.image_admission import (
    ImageAdmissionConfigurationError,
    ImageAdmissionUnavailableError,
    ImageReferenceError,
    ImageSignatureRejectedError,
    verify_custom_image,
)


def _settings(monkeypatch, *, enabled: bool):
    monkeypatch.setattr(settings, "BOXXKITE_IMAGE_ADMISSION_VERIFY_SIGNATURES", enabled)
    monkeypatch.setattr(settings, "BOXXKITE_IMAGE_ADMISSION_SIGNATURE_IDENTITY_REGEXP", "^workflow$")
    monkeypatch.setattr(settings, "BOXXKITE_IMAGE_ADMISSION_SIGNATURE_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setattr(settings, "BOXXKITE_IMAGE_ADMISSION_COSIGN_BINARY", "cosign")
    monkeypatch.setattr(settings, "BOXXKITE_IMAGE_ADMISSION_TIMEOUT_SECONDS", 1.0)


def _ref() -> str:
    return "registry.internal/boxxkite-images/account/image@sha256:" + "a" * 64


@pytest.mark.asyncio
async def test_digest_is_accepted_without_signature_gate(monkeypatch):
    _settings(monkeypatch, enabled=False)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("cosign must not run while admission verification is disabled")

    monkeypatch.setattr("control_plane.image_admission.asyncio.create_subprocess_exec", fail_if_called)
    assert await verify_custom_image(_ref(), expected_digest="sha256:" + "a" * 64) == _ref()


@pytest.mark.asyncio
async def test_valid_signature_is_accepted_with_exact_digest(monkeypatch):
    _settings(monkeypatch, enabled=True)
    monkeypatch.setattr("control_plane.image_admission.shutil.which", lambda name: "/usr/local/bin/cosign")
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"signature details", b""

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr("control_plane.image_admission.asyncio.create_subprocess_exec", fake_exec)
    assert await verify_custom_image(_ref(), expected_digest="sha256:" + "a" * 64) == _ref()
    assert calls == [
        (
            (
                "/usr/local/bin/cosign",
                "verify",
                "--certificate-identity-regexp",
                "^workflow$",
                "--certificate-oidc-issuer",
                "https://issuer.example",
                _ref(),
            ),
            {"stdout": -1, "stderr": -1},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [1, 2])
async def test_missing_or_invalid_signature_fails_closed(monkeypatch, returncode):
    _settings(monkeypatch, enabled=True)
    monkeypatch.setattr("control_plane.image_admission.shutil.which", lambda _name: "/usr/local/bin/cosign")

    process = SimpleNamespace(returncode=returncode)

    async def communicate():
        return b"", b"verification failed"

    process.communicate = communicate
    async def fake_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr("control_plane.image_admission.asyncio.create_subprocess_exec", fake_exec)

    with pytest.raises(ImageSignatureRejectedError):
        await verify_custom_image(_ref(), expected_digest="sha256:" + "a" * 64)


@pytest.mark.asyncio
async def test_enabled_gate_requires_cosign_and_trust_policy(monkeypatch):
    _settings(monkeypatch, enabled=True)
    monkeypatch.setattr(settings, "BOXXKITE_IMAGE_ADMISSION_SIGNATURE_IDENTITY_REGEXP", "")
    with pytest.raises(ImageAdmissionConfigurationError):
        await verify_custom_image(_ref())

    monkeypatch.setattr(settings, "BOXXKITE_IMAGE_ADMISSION_SIGNATURE_IDENTITY_REGEXP", "^workflow$")
    monkeypatch.setattr("control_plane.image_admission.shutil.which", lambda _name: None)
    with pytest.raises(ImageAdmissionUnavailableError):
        await verify_custom_image(_ref())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image_ref,expected_digest",
    [
        ("registry.internal/boxxkite-images/account/image:latest", None),
        (_ref(), "sha256:" + "b" * 64),
        ("registry.internal/boxxkite-images/account/image@sha256:" + "a" * 63, None),
    ],
)
async def test_digest_and_recorded_digest_boundaries_are_rejected(monkeypatch, image_ref, expected_digest):
    _settings(monkeypatch, enabled=False)
    with pytest.raises(ImageReferenceError):
        await verify_custom_image(image_ref, expected_digest=expected_digest)
