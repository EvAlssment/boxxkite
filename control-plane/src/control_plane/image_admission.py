"""Admission checks for caller-selected sandbox images.

The control plane only admits immutable image references. Signature verification
is an explicit operator opt-in and uses the installed cosign binary's exit code
as the trust decision; command output is never persisted or returned to callers.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from typing import Protocol

from .config import settings

_DIGEST_REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:-]*@sha256:[0-9a-f]{64}$")


class ImageAdmissionError(RuntimeError):
    """Base class for fail-closed custom-image admission failures."""


class ImageReferenceError(ImageAdmissionError):
    """The image reference is not the expected immutable digest form."""


class ImageAdmissionConfigurationError(ImageAdmissionError):
    """Signature verification is enabled without a complete trust policy."""


class ImageAdmissionUnavailableError(ImageAdmissionError):
    """Cosign could not be executed or did not complete in time."""


class ImageSignatureRejectedError(ImageAdmissionError):
    """Cosign rejected or could not find a signature for the image."""


class _Settings(Protocol):
    BOXXKITE_IMAGE_ADMISSION_VERIFY_SIGNATURES: bool
    BOXXKITE_IMAGE_ADMISSION_SIGNATURE_IDENTITY_REGEXP: str
    BOXXKITE_IMAGE_ADMISSION_SIGNATURE_OIDC_ISSUER: str
    BOXXKITE_IMAGE_ADMISSION_COSIGN_BINARY: str
    BOXXKITE_IMAGE_ADMISSION_TIMEOUT_SECONDS: float


def validate_digest_pinned_image_ref(image_ref: str, *, expected_digest: str | None = None) -> str:
    if not _DIGEST_REF_RE.fullmatch(image_ref):
        raise ImageReferenceError("custom sandbox image must use repo@sha256:<64-hex>")

    actual_digest = image_ref.rsplit("@", 1)[1]
    if expected_digest is not None and actual_digest != expected_digest:
        raise ImageReferenceError("custom sandbox image digest does not match its recorded digest")
    return image_ref


async def verify_custom_image(
    image_ref: str,
    *,
    expected_digest: str | None = None,
    settings_obj: _Settings = settings,
) -> str:
    """Validate and, when enabled, verify one exact image digest.

    No shell, registry credential, key file, or network client is managed here.
    Cosign uses its normal operator-provided runtime configuration. Any missing
    binary, trust policy, or verification result fails closed when enabled.
    """
    validate_digest_pinned_image_ref(image_ref, expected_digest=expected_digest)
    if not settings_obj.BOXXKITE_IMAGE_ADMISSION_VERIFY_SIGNATURES:
        return image_ref

    identity_regexp = settings_obj.BOXXKITE_IMAGE_ADMISSION_SIGNATURE_IDENTITY_REGEXP.strip()
    oidc_issuer = settings_obj.BOXXKITE_IMAGE_ADMISSION_SIGNATURE_OIDC_ISSUER.strip()
    if not identity_regexp or not oidc_issuer:
        raise ImageAdmissionConfigurationError(
            "signature verification requires an identity regexp and OIDC issuer"
        )

    cosign_path = shutil.which(settings_obj.BOXXKITE_IMAGE_ADMISSION_COSIGN_BINARY)
    if cosign_path is None:
        raise ImageAdmissionUnavailableError("cosign binary is unavailable")

    args = (
        cosign_path,
        "verify",
        "--certificate-identity-regexp",
        identity_regexp,
        "--certificate-oidc-issuer",
        oidc_issuer,
        image_ref,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ImageAdmissionUnavailableError("cosign could not be executed") from exc

    try:
        await asyncio.wait_for(process.communicate(), timeout=settings_obj.BOXXKITE_IMAGE_ADMISSION_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ImageAdmissionUnavailableError("cosign verification timed out") from exc

    if process.returncode != 0:
        raise ImageSignatureRejectedError("cosign did not verify the image signature")
    return image_ref
