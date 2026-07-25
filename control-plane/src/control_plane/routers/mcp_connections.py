"""Org-scoped outbound-MCP connection-grant CRUD (GitHub issues #116/#117,
docs/OUTBOUND-MCP-DESIGN.md §3). Authenticated with a long-lived API key --
the same credential /v1/sandboxes/* requires -- since a connection only
exists to be granted to a sandbox session created via that same API.

`catalog_id` must resolve against the curated allowlist (mcp_catalog.py,
config.py's BOXKITE_MCP_CATALOG) -- never a caller-supplied hostname.

Scope note: this router only manages the connection-grant row and its
resolved catalog host. There is no MCP-proxy transport and no third-party
OAuth credential handling here -- both are explicitly out of scope for this
pass (docs/OUTBOUND-MCP-DESIGN.md §6/§7); granting a connection to a session
(SandboxCreateRequest.mcp_connection_names) only widens that session's
per-pod NetworkPolicy egress allowlist (issue #74's existing mechanism).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_account_via_api_key
from ..errors import ApiError, LimitExceededError
from ..mcp_catalog import resolve_catalog_host
from ..models_orm import Account
from ..repository import McpConnectionRepository
from ..schemas import McpConnectionCreatedResponse, McpConnectionCreateRequest, McpConnectionOut

router = APIRouter(prefix="/v1/mcp-connections", tags=["mcp-connections"])


@router.post(
    "",
    response_model=McpConnectionCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an org-scoped outbound-MCP connection grant",
    description=(
        "Creates a new outbound-MCP connection grant for the authenticated account. "
        "catalog_id must resolve against boxkite's curated MCP catalog (GitHub issue #117) "
        "-- grant a sandbox session access to it via "
        "SandboxCreateRequest.mcp_connection_names."
    ),
)
async def create_mcp_connection(
    body: McpConnectionCreateRequest,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> McpConnectionCreatedResponse:
    connections = McpConnectionRepository(db)

    existing_count = await connections.count_for_account(account.id)
    if existing_count >= settings.BOXKITE_MAX_MCP_CONNECTIONS_PER_ACCOUNT:
        raise LimitExceededError(
            code="mcp_connection_limit_reached",
            message="MCP connection limit reached for this account.",
            details={"limit": settings.BOXKITE_MAX_MCP_CONNECTIONS_PER_ACCOUNT},
        )

    if await connections.get_by_label_for_account(account_id=account.id, label=body.label) is not None:
        raise ApiError(409, "mcp_connection_label_taken", f"An MCP connection labeled {body.label!r} already exists")

    # catalog_id is already restricted to schemas.py's McpCatalogId Literal
    # at the request boundary -- this call is the drift guard (mirrors
    # image_builder.py's UnknownBaseError precedent), not the primary
    # input validation.
    host = resolve_catalog_host(body.catalog_id)

    row = await connections.create(
        account_id=account.id,
        label=body.label,
        catalog_id=body.catalog_id,
        host=host,
    )
    return McpConnectionCreatedResponse.model_validate(row)


@router.get(
    "",
    response_model=list[McpConnectionOut],
    summary="List MCP connections",
    description="Lists outbound-MCP connection grants for the authenticated account.",
)
async def list_mcp_connections(
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[McpConnectionOut]:
    rows = await McpConnectionRepository(db).list_for_account(account.id)
    return [McpConnectionOut.model_validate(r) for r in rows]


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an MCP connection",
    description="Deletes an MCP connection grant belonging to the authenticated account. 404 if already gone or never owned by this account.",
)
async def delete_mcp_connection(
    connection_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    deleted = await McpConnectionRepository(db).delete(account_id=account.id, connection_id=connection_id)
    if not deleted:
        raise ApiError(404, "not_found", "MCP connection not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
