"""Org-scoped secret CRUD for the proxy-substitution secrets broker
(docs/SECRETS-DESIGN.md). Authenticated with a long-lived API key -- the
same credential /v1/sandboxes/* requires -- since a secret only exists to be
granted to a sandbox session created via that same API.

The raw value is write-only: accepted on POST, never returned by any route
here (list/get/create-response all omit it) -- see SecretOut/
SecretCreatedResponse in schemas.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_account_via_api_key
from ..errors import ApiError, LimitExceededError
from ..host_safety import resolve_host_is_unsafe
from ..models_orm import Account
from ..repository import SecretRepository
from ..schemas import SecretCreatedResponse, SecretCreateRequest, SecretOut
from ..secrets_kms import get_secrets_kms_client

router = APIRouter(prefix="/v1/secrets", tags=["secrets"])


_ACCEPTED_TRUST_TIERS = {"testnet"}


def _validate_trust_tier(trust_tier: str | None) -> None:
    """Only 'testnet' is accepted today (docs/WALLET-SECRETS-DESIGN.md §3/
    §11) -- 'mainnet' is refused outright rather than accepted-but-
    unenforced, since the session-scoped signing mechanism a mainnet-tier
    grant requires (§4b) doesn't exist yet; any other value is just a typo
    guard."""
    if trust_tier is None:
        return
    if trust_tier not in _ACCEPTED_TRUST_TIERS:
        raise ApiError(
            422,
            "unsupported_trust_tier",
            f"trust_tier {trust_tier!r} is not supported. Only 'testnet' is accepted today -- "
            "'mainnet' requires session-scoped signing (docs/WALLET-SECRETS-DESIGN.md §4b), "
            "which isn't implemented yet.",
        )


def _validate_allowed_hosts(allowed_hosts: list[str]) -> None:
    """Best-effort, creation-time-only backstop (docs/SECRETS-DESIGN.md §5)
    -- rejects a host that resolves to a private/link-local/loopback/
    metadata address right now. NOT the real control; see
    sidecar/main.py's request-time re-resolution check for that."""
    for host in allowed_hosts:
        if not host or not host.strip():
            raise ApiError(422, "invalid_allowed_host", f"allowed_hosts entry is empty")
        if resolve_host_is_unsafe(host.strip()):
            raise ApiError(
                422,
                "unsafe_allowed_host",
                f"allowed_hosts entry {host!r} resolves to a private, link-local, "
                "loopback, or cloud-metadata address and cannot be used as a "
                "secrets-broker destination.",
            )


@router.post(
    "",
    response_model=SecretCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an org-scoped secret",
    description=(
        "Creates a new secret for the authenticated account, encrypted at rest via "
        "envelope encryption (see secrets_kms.py). `value` is accepted here and never "
        "returned by this or any other route -- grant a sandbox session access to it "
        "via SandboxCreateRequest.secret_names, then reference it from an agent tool "
        "call as {{secret:name}} in a POST /http-request body/header."
    ),
)
async def create_secret(
    body: SecretCreateRequest,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SecretCreatedResponse:
    secrets = SecretRepository(db)

    existing_count = await secrets.count_for_account(account.id)
    if existing_count >= settings.BOXKITE_MAX_SECRETS_PER_ACCOUNT:
        raise LimitExceededError(
            code="secret_limit_reached",
            message="Secret limit reached for this account.",
            details={"limit": settings.BOXKITE_MAX_SECRETS_PER_ACCOUNT},
        )

    if await secrets.get_by_name_for_account(account_id=account.id, name=body.name) is not None:
        raise ApiError(409, "secret_name_taken", f"A secret named {body.name!r} already exists")

    _validate_allowed_hosts(body.allowed_hosts)
    _validate_trust_tier(body.trust_tier)

    kms = get_secrets_kms_client()
    encrypted = kms.encrypt(body.value)

    row = await secrets.create(
        account_id=account.id,
        name=body.name,
        ciphertext=encrypted.ciphertext_b64,
        nonce=encrypted.nonce_b64,
        wrapped_data_key=encrypted.wrapped_data_key_b64,
        encryption_key_id=encrypted.encryption_key_id,
        allowed_hosts=body.allowed_hosts,
        trust_tier=body.trust_tier,
    )
    return SecretCreatedResponse.model_validate(row)


@router.get(
    "",
    response_model=list[SecretOut],
    summary="List secrets",
    description="Lists secrets for the authenticated account. Raw values are never returned here.",
)
async def list_secrets(
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[SecretOut]:
    rows = await SecretRepository(db).list_for_account(account.id)
    return [SecretOut.model_validate(r) for r in rows]


@router.delete(
    "/{secret_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a secret",
    description="Deletes a secret belonging to the authenticated account. 404 if already gone or never owned by this account.",
)
async def delete_secret(
    secret_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    deleted = await SecretRepository(db).delete(account_id=account.id, secret_id=secret_id)
    if not deleted:
        raise ApiError(404, "not_found", "Secret not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
