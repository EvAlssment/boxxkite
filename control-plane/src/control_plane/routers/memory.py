"""Owned, account-scoped MemoryBase API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_account_via_api_key
from ..errors import ApiError
from ..memory_embeddings import get_memory_embedding_provider, get_memory_reranker
from ..models_orm import Account, MemoryRecord
from ..rate_limit import enforce_rate_limit
from ..repository import MemoryRepository
from ..schemas import (
    MemoryCreateRequest,
    MemoryImportRequest,
    MemoryIngestRequest,
    MemoryIngestResponse,
    MemoryOut,
    MemoryProfileResponse,
    MemoryRelationOut,
    MemoryRelationsResponse,
    MemorySearchHit,
    MemorySearchResponse,
)

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _repository(db: AsyncSession) -> MemoryRepository:
    return MemoryRepository(
        db,
        embedding_provider=get_memory_embedding_provider(),
        reranker_provider=get_memory_reranker(),
    )


def _memory_out(row: MemoryRecord) -> MemoryOut:
    return MemoryOut(
        id=row.id,
        scope=row.scope,
        kind=row.kind,
        content=row.content,
        metadata=row.metadata_json or {},
        source_session_id=row.source_session_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        source_content=row.source_content,
        document_date=row.document_date,
        event_dates=row.event_dates_json or [],
        importance=row.importance,
        access_count=row.access_count,
        last_accessed_at=row.last_accessed_at,
        superseded_at=row.superseded_at,
        superseded_by_id=row.superseded_by_id,
    )


async def _enforce_memory_rate_limit(request: Request, response: Response, account: Account) -> None:
    await enforce_rate_limit(
        request,
        bucket="memory_ops",
        subject=str(account.id),
        limit=settings.BOXXKITE_MEMORY_RATE_LIMIT_PER_MINUTE,
        response=response,
    )


@router.post(
    "",
    response_model=MemoryOut,
    status_code=201,
    summary="Remember durable information",
    description="Stores one account-scoped memory in Boxkite's own control-plane database.",
)
async def remember(
    body: MemoryCreateRequest,
    request: Request,
    response: Response,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> MemoryOut:
    await _enforce_memory_rate_limit(request, response, account)
    row = await _repository(db).remember(
        account_id=account.id,
        scope=body.scope,
        kind=body.kind,
        content=body.content,
        metadata=body.metadata,
        source_session_id=body.source_session_id,
        expires_at=body.expires_at,
        source_content=body.source_content,
        document_date=body.document_date,
        event_dates=body.event_dates,
        importance=body.importance,
    )
    return _memory_out(row)


@router.post(
    "/ingest",
    response_model=MemoryIngestResponse,
    status_code=201,
    summary="Ingest text into atomic memories",
    description=(
        "Splits a bounded document or conversation into atomic memories. "
        "The default extractor is deterministic and model-free; a future "
        "model provider can implement the same persistence contract."
    ),
)
async def ingest(
    body: MemoryIngestRequest,
    request: Request,
    response: Response,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> MemoryIngestResponse:
    await _enforce_memory_rate_limit(request, response, account)
    rows = await _repository(db).ingest(
        account_id=account.id,
        scope=body.scope,
        content=body.content,
        kind=None if body.kind == "auto" else body.kind,
        metadata=body.metadata,
        source_session_id=body.source_session_id,
        expires_at=body.expires_at,
        document_date=body.document_date,
    )
    return MemoryIngestResponse(source_session_id=body.source_session_id, memories=[_memory_out(row) for row in rows])


@router.post(
    "/import",
    response_model=MemoryIngestResponse,
    status_code=201,
    summary="Import account-scoped memories",
)
async def import_memories(
    body: MemoryImportRequest,
    request: Request,
    response: Response,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> MemoryIngestResponse:
    await _enforce_memory_rate_limit(request, response, account)
    rows = await _repository(db).import_for_account(
        account_id=account.id,
        memories=[memory.model_dump() for memory in body.memories],
        relations=[relation.model_dump() for relation in body.relations],
    )
    return MemoryIngestResponse(source_session_id=None, memories=[_memory_out(row) for row in rows])


@router.get("/metrics", summary="Summarize live memory records")
async def metrics(
    scope: str | None = Query(default=None, max_length=128),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _repository(db).metrics_for_account(account_id=account.id, scope=scope)


@router.get(
    "/search",
    response_model=MemorySearchResponse,
    summary="Recall durable memories",
    description="Ranks live account memories using lexical evidence, phrase match, recency, and importance.",
)
async def search(
    q: str = Query(min_length=1, max_length=400),
    scope: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=10, ge=1, le=50),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> MemorySearchResponse:
    hits = await _repository(db).search(
        account_id=account.id,
        query_text=q,
        scope=scope,
        limit=limit,
    )
    return MemorySearchResponse(
        query=q,
        memories=[MemorySearchHit(**_memory_out(row).model_dump(), score=score) for row, score in hits],
    )


@router.get("/profile", response_model=MemoryProfileResponse, summary="Build a compact memory profile")
async def profile(
    scope: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=20, ge=1, le=100),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> MemoryProfileResponse:
    static, dynamic = await _repository(db).profile(account_id=account.id, scope=scope, limit=limit)
    return MemoryProfileResponse(
        static=[_memory_out(row) for row in static],
        dynamic=[_memory_out(row) for row in dynamic],
    )


@router.get("/export", summary="Export live memories for the account")
async def export_memories(
    scope: str | None = Query(default=None, max_length=128),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _repository(db).export_for_account(account_id=account.id, scope=scope)


@router.get(
    "",
    response_model=list[MemoryOut],
    summary="List durable memories",
    description="Lists live memories for the authenticated account, optionally restricted to one scope.",
)
async def list_memories(
    scope: str | None = Query(default=None, max_length=128),
    include_superseded: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryOut]:
    rows = await _repository(db).list_for_account(
        account_id=account.id,
        scope=scope,
        limit=limit,
        offset=offset,
        include_superseded=include_superseded,
    )
    return [_memory_out(row) for row in rows]


@router.get("/{memory_id}/relations", response_model=MemoryRelationsResponse)
async def relations(
    memory_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> MemoryRelationsResponse:
    row = await _repository(db).get_for_account(account_id=account.id, memory_id=memory_id)
    if row is None:
        raise ApiError(404, "memory_not_found", "Memory not found")
    edges = await _repository(db).relations_for_account(
        account_id=account.id, memory_id=memory_id, limit=limit
    )
    return MemoryRelationsResponse(
        memory_id=memory_id,
        relations=[
            MemoryRelationOut(
                source_memory_id=edge.source_memory_id,
                target_memory_id=edge.target_memory_id,
                relation_type=edge.relation_type,
                confidence=edge.confidence,
                created_at=edge.created_at,
            )
            for edge in edges
        ],
    )


@router.get("/{memory_id}", response_model=MemoryOut, summary="Get one live memory")
async def get_memory(
    memory_id: str,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> MemoryOut:
    row = await _repository(db).get_for_account(account_id=account.id, memory_id=memory_id)
    if row is None:
        raise ApiError(404, "memory_not_found", "Memory not found")
    return _memory_out(row)


@router.delete("/{memory_id}", status_code=204, summary="Forget one memory permanently")
async def forget_memory(
    memory_id: str,
    request: Request,
    response: Response,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _enforce_memory_rate_limit(request, response, account)
    deleted = await _repository(db).forget(account_id=account.id, memory_id=memory_id)
    if not deleted:
        raise ApiError(404, "memory_not_found", "Memory not found")
    response.status_code = 204
    return response
