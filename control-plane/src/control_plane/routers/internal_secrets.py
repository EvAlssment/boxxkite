"""Internal (non-`/v1`) secret-resolve endpoint (docs/SECRETS-DESIGN.md §4).

Called ONLY by a session's own sidecar, from its `POST /http-request` route,
to fetch the plaintext value of one secret it was granted at session-create
time. Authenticated with a short-lived, session-bound capability token
(secret_capability.py) -- a THIRD credential type, distinct from both the
dashboard JWT and the long-lived API key `/v1/secrets`/`/v1/sandboxes`
require. Never accepts either of those.

`404 secret_not_referenced_by_session` for a name that exists for the
account but wasn't granted to this session, exactly like `404
secret_not_found` for a name that doesn't exist at all -- the caller must
not be able to distinguish the two (docs/SECRETS-DESIGN.md §3).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..errors import ApiError
from ..repository import SecretRepository
from ..secret_capability import InvalidCapabilityToken, decode_capability_token
from ..secrets_kms import EncryptedSecret, get_secrets_kms_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/secrets", tags=["internal"])


class SecretResolveRequest(BaseModel):
    session_id: str
    secret_name: str


class SecretResolveResponse(BaseModel):
    name: str
    value: str


def _capability_token_from_header(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "missing_credentials", "Missing capability token")
    token = authorization[len("bearer "):].strip()
    if not token:
        raise ApiError(401, "missing_credentials", "Missing capability token")
    return token


@router.post(
    "/resolve",
    response_model=SecretResolveResponse,
    summary="[internal] Resolve a secret's plaintext value for one session",
    description=(
        "Called only by a sandbox session's own sidecar, authenticated with the "
        "short-lived capability token minted at session-create time (never a "
        "dashboard JWT or API key). Never exposed to the agent-facing SDKs."
    ),
)
async def resolve_secret(
    body: SecretResolveRequest,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> SecretResolveResponse:
    token = _capability_token_from_header(authorization)
    try:
        payload = decode_capability_token(token, expected_session_id=body.session_id)
    except InvalidCapabilityToken:
        raise ApiError(401, "invalid_capability_token", "Invalid or expired capability token") from None

    # The token's own embedded grant list is the authority on "was this
    # secret granted to this session" -- a name valid for the account but
    # not in this list is treated identically to a name that doesn't exist
    # at all (see module docstring): a compromised session's sidecar cannot
    # distinguish "not granted to me" from "doesn't exist".
    if body.secret_name not in set(payload.get("secret_names", [])):
        raise ApiError(
            404, "secret_not_referenced_by_session", "Secret not found or not granted to this session"
        )

    account_id = payload["account_id"]
    secrets = SecretRepository(db)
    row = await secrets.get_by_name_for_account(account_id=account_id, name=body.secret_name)
    if row is None:
        raise ApiError(
            404, "secret_not_referenced_by_session", "Secret not found or not granted to this session"
        )

    kms = get_secrets_kms_client()
    encrypted = EncryptedSecret(
        ciphertext_b64=row.ciphertext,
        nonce_b64=row.nonce,
        wrapped_data_key_b64=row.wrapped_data_key,
        encryption_key_id=row.encryption_key_id,
    )
    try:
        value = kms.decrypt(encrypted)
    except Exception:
        logger.error(
            "[internal_secrets] decrypt failed for secret_id=%s account_id=%s",
            row.id,
            account_id,
        )
        raise ApiError(500, "secret_decrypt_failed", "Failed to resolve secret") from None

    await secrets.touch_last_used(row.id)
    return SecretResolveResponse(name=row.name, value=value)
