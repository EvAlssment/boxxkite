"""Declarative-builder API — docs/DECLARATIVE-BUILDER-DESIGN.md.

Deliberately NOT a Dockerfile-passthrough API: a pre-approved `base` plus an
exact-version-pinned package list (`SandboxImageBuildRequest`), rejected with
400 otherwise (see schemas.py's `_validate_pinned_packages`). Builds run in a
strictly separate, one-shot builder Job -- see `image_builder.py`'s module
docstring for the full isolation model this router leans on.

Ownership scoping follows `routers/snapshots.py`'s exact pattern: every
lookup is scoped to `account.id` at the database layer
(`SandboxImageRepository.get_for_account`), so a foreign `image_id` 404s,
never distinguishing "doesn't exist" from "belongs to someone else".

This entire feature is gated by `BOXKITE_IMAGE_BUILDER_ENABLED` (off by
default) -- every route here 404s if it's disabled, so a deployment that
hasn't opted in exposes no trace of this API beyond the route existing in
the OpenAPI schema.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Path, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db, get_session_factory
from ..deps import get_current_account_via_api_key, get_image_build_runner
from ..errors import ApiError, LimitExceededError
from ..image_builder import ImageBuildRunner, cache_key_for, cache_window_start, dispatch_build
from ..models_orm import Account
from ..rate_limit import enforce_rate_limit
from ..repository import SandboxImageRepository
from ..schemas import SandboxImageBuildAccepted, SandboxImageBuildRequest, SandboxImageOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/images", tags=["images"])


async def _enforce_image_build_rate_limit(request: Request, response: Response, account: Account) -> None:
    """Image builds are the heaviest operation this service exposes (a real
    container build plus a vulnerability scan) -- a distinct, deliberately
    low bucket, per image_builder.py's / the design doc's security section."""
    await enforce_rate_limit(
        request,
        bucket="image_build_ops",
        subject=str(account.id),
        limit=settings.BOXKITE_IMAGE_BUILD_RATE_LIMIT_PER_MINUTE,
        response=response,
    )


def _require_builder_enabled() -> None:
    if not settings.BOXKITE_IMAGE_BUILDER_ENABLED:
        raise ApiError(
            404,
            "not_found",
            "The declarative image builder is not enabled on this deployment.",
        )


def _to_out(row) -> SandboxImageOut:
    return SandboxImageOut.model_validate(row)


async def _run_build_in_background(
    *,
    runner: ImageBuildRunner,
    image_id: str,
    account_id: str,
    base: str,
    python_packages: list[str],
    apt_packages: list[str],
    npm_packages: list[str],
) -> None:
    """Runs with its OWN DB session (not the triggering request's) so the
    build isn't tied to the request/response lifecycle -- builds are
    asynchronous per the design doc, and a request-scoped session would be
    closed by the time a real multi-minute build finishes."""
    session_factory = get_session_factory()
    async with session_factory() as db:
        repo = SandboxImageRepository(db)
        await dispatch_build(
            repo=repo,
            runner=runner,
            image_id=image_id,
            account_id=account_id,
            base=base,
            python_packages=python_packages,
            apt_packages=apt_packages,
            npm_packages=npm_packages,
        )


@router.post(
    "",
    response_model=SandboxImageBuildAccepted,
    status_code=202,
    summary="Submit a declarative-builder custom image build request",
    description=(
        "Queues a build of a custom sandbox image: a pre-approved `base` "
        "plus an exact-version-pinned package list. Always asynchronous -- "
        "returns 202 with status='queued' immediately; poll GET "
        "/v1/images/{id} for progress. If an identical (base + sorted "
        "package list) build already completed for this account within "
        "BOXKITE_IMAGE_BUILD_CACHE_HOURS, this reuses that build's digest "
        "instead of re-running it (still returns a NEW image row, but with "
        "status='completed' immediately). 429 with "
        "`image_build_limit_reached` if this account is already at "
        "BOXKITE_MAX_IMAGES_PER_ACCOUNT. 404s if the declarative builder "
        "isn't enabled on this deployment."
    ),
)
async def create_image_build(
    body: SandboxImageBuildRequest,
    request: Request,
    response: Response,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    runner: ImageBuildRunner = Depends(get_image_build_runner),
) -> SandboxImageBuildAccepted:
    _require_builder_enabled()
    await _enforce_image_build_rate_limit(request, response, account)

    images = SandboxImageRepository(db)
    active_count = await images.count_active_for_account(account.id)
    if active_count >= settings.BOXKITE_MAX_IMAGES_PER_ACCOUNT:
        raise LimitExceededError(
            code="image_build_limit_reached",
            message=(
                "Custom image limit reached "
                f"({settings.BOXKITE_MAX_IMAGES_PER_ACCOUNT} at a time). "
                "Delete an existing image before building another."
            ),
            details={"limit": settings.BOXKITE_MAX_IMAGES_PER_ACCOUNT, "active": active_count},
        )

    in_flight_total = await images.count_in_flight_total()
    if in_flight_total >= settings.BOXKITE_GLOBAL_MAX_CONCURRENT_IMAGE_BUILDS:
        raise LimitExceededError(
            code="global_build_capacity_reached",
            message=(
                "This deployment's cluster-wide concurrent build capacity is "
                "in use. Try again shortly."
            ),
            details={
                "limit": settings.BOXKITE_GLOBAL_MAX_CONCURRENT_IMAGE_BUILDS,
                "in_flight": in_flight_total,
            },
        )

    cache_key = cache_key_for(
        base=body.base,
        python_packages=body.python_packages,
        apt_packages=body.apt_packages,
        npm_packages=body.npm_packages,
    )

    cached = await images.find_cached_completed(
        account_id=account.id, cache_key=cache_key, not_before=cache_window_start()
    )
    if cached is not None:
        # Cache hit (design doc's 24h build-cache requirement): create a new
        # row for this request (so it has its own id/label to reference) but
        # reuse the already-scanned digest/registry_ref instead of paying
        # for another build+scan. Still scoped to THIS account -- see
        # SandboxImageRepository.find_cached_completed's docstring.
        image_id = str(uuid4())
        row = await images.create(
            image_id=image_id,
            account_id=account.id,
            label=body.label,
            base=body.base,
            python_packages=body.python_packages,
            apt_packages=body.apt_packages,
            npm_packages=body.npm_packages,
            cache_key=cache_key,
            status="queued",
        )
        await images.mark_completed(
            image_id=image_id,
            digest=cached.digest,
            registry_ref=cached.registry_ref,
            scan_result=cached.scan_result or {},
        )
        row.status = "completed"
        row.digest = cached.digest
        row.registry_ref = cached.registry_ref
        return SandboxImageBuildAccepted(id=row.id, label=row.label, status=row.status, created_at=row.created_at)

    image_id = str(uuid4())
    row = await images.create(
        image_id=image_id,
        account_id=account.id,
        label=body.label,
        base=body.base,
        python_packages=body.python_packages,
        apt_packages=body.apt_packages,
        npm_packages=body.npm_packages,
        cache_key=cache_key,
        status="queued",
    )

    asyncio.create_task(
        _run_build_in_background(
            runner=runner,
            image_id=image_id,
            account_id=account.id,
            base=body.base,
            python_packages=body.python_packages,
            apt_packages=body.apt_packages,
            npm_packages=body.npm_packages,
        )
    )

    return SandboxImageBuildAccepted(id=row.id, label=row.label, status=row.status, created_at=row.created_at)


@router.get(
    "",
    response_model=list[SandboxImageOut],
    summary="List this account's custom images",
)
async def list_images(
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[SandboxImageOut]:
    _require_builder_enabled()
    rows = await SandboxImageRepository(db).list_for_account(account_id=account.id)
    return [_to_out(r) for r in rows]


@router.get(
    "/{image_id}",
    response_model=SandboxImageOut,
    summary="Get a single custom image's build status",
    description=(
        "404s for an image_id owned by a different account or one that "
        "never existed or has been deleted, identically."
    ),
)
async def get_image(
    image_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> SandboxImageOut:
    _require_builder_enabled()
    row = await SandboxImageRepository(db).get_for_account(image_id=image_id, account_id=account.id)
    if row is None or row.deleted_at is not None:
        raise ApiError(404, "not_found", "Image not found")
    return _to_out(row)


@router.delete(
    "/{image_id}",
    status_code=204,
    summary="Delete a custom image",
    description=(
        "Deletes the control-plane's bookkeeping row for this image. This "
        "is metadata/registry cleanup only -- it does NOT retroactively "
        "tear down any already-running sandbox session created from this "
        "image's digest; those keep running against the digest they were "
        "created with, per the design doc's explicit DELETE section."
    ),
)
async def delete_image(
    request: Request,
    response: Response,
    image_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    _require_builder_enabled()
    await _enforce_image_build_rate_limit(request, response, account)
    images = SandboxImageRepository(db)
    row = await images.get_for_account(image_id=image_id, account_id=account.id)
    if row is None or row.deleted_at is not None:
        raise ApiError(404, "not_found", "Image not found")
    await images.mark_deleted(image_id=image_id)
    return Response(status_code=204, headers=dict(response.headers))
