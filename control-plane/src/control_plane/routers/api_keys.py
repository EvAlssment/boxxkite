"""API-key management — authenticated with a dashboard JWT (`get_current_user`),
never with an API key itself (you can't create a key using a key; that would
make revocation-after-leak awkward to reason about).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..errors import ApiError
from ..models_orm import Account
from ..repository import ApiKeyRepository
from ..schemas import ApiKeyCreated, ApiKeyCreateRequest, ApiKeyOut
from ..security import generate_api_key

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key",
    description=(
        "Creates a new long-lived API key for the authenticated account. "
        "The raw key is returned exactly once, in this response — it is "
        "never stored or retrievable again. Use it as `Authorization: Bearer "
        "<key>` on every /v1/sandboxes request. `role` defaults to 'admin' "
        "(every capability, matching this project's original behavior); "
        "pass 'member' to mint a key that cannot initiate "
        "`WS /v1/sandboxes/{id}/takeover` -- see ApiKeyCreateRequest.role."
    ),
)
async def create_api_key(
    body: ApiKeyCreateRequest,
    user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreated:
    full_key, prefix, key_hash = generate_api_key()
    row = await ApiKeyRepository(db).create(
        account_id=user.id, name=body.name, prefix=prefix, key_hash=key_hash, role=body.role
    )
    return ApiKeyCreated(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        role=row.role,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        key=full_key,
    )


@router.get(
    "",
    response_model=list[ApiKeyOut],
    summary="List API keys",
    description="Lists API keys for the authenticated account. Raw key values are never returned here.",
)
async def list_api_keys(
    user: Account = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[ApiKeyOut]:
    rows = await ApiKeyRepository(db).list_for_account(user.id)
    return [ApiKeyOut.model_validate(r) for r in rows]


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
    description="Revokes an API key belonging to the authenticated account. Idempotent-ish: 404 if already gone or never owned by this account.",
)
async def revoke_api_key(
    key_id: str = Path(...),
    user: Account = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    revoked = await ApiKeyRepository(db).revoke(account_id=user.id, key_id=key_id)
    if not revoked:
        raise ApiError(404, "not_found", "API key not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
