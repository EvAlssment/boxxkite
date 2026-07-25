"""Independent Storage Volumes API —
docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum.

E2B's `e2b.Volume` equivalent: an independent, dynamically-created
PersistentVolumeClaim with its own lifecycle apart from any single sandbox
session, mountable at a custom path in a newly created sandbox via
`SandboxCreateRequest.volume_mounts`. NOT the FUSE object-storage mount
the rest of that design doc scopes.

Ownership scoping follows routers/images.py's exact pattern: every lookup
is scoped to `account.id` at the database layer, so a foreign `volume_id`
404s, never distinguishing "doesn't exist" from "belongs to someone else".

Gated by `BOXKITE_VOLUMES_ENABLED` (off by default) -- every route here
404s if it's disabled.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Path, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db, get_session_factory
from ..deps import get_current_account_via_api_key, get_volume_provisioner
from ..errors import ApiError, LimitExceededError
from ..models_orm import Account
from ..rate_limit import enforce_rate_limit
from ..repository import SandboxVolumeRepository
from ..schemas import VolumeAccepted, VolumeCreateRequest, VolumeOut
from ..volume_builder import VolumeProvisioner, dispatch_volume_creation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/volumes", tags=["volumes"])


async def _enforce_volume_rate_limit(request: Request, response: Response, account: Account) -> None:
    """Volume creation provisions real cluster storage -- its own,
    deliberately conservative rate-limit bucket, same rationale as
    routers/images.py's image-build bucket."""
    await enforce_rate_limit(
        request,
        bucket="volume_ops",
        subject=str(account.id),
        limit=settings.BOXKITE_IMAGE_BUILD_RATE_LIMIT_PER_MINUTE,
        response=response,
    )


def _require_volumes_enabled() -> None:
    if not settings.BOXKITE_VOLUMES_ENABLED:
        raise ApiError(404, "not_found", "Independent storage volumes are not enabled on this deployment.")


def _to_out(row) -> VolumeOut:
    return VolumeOut.model_validate(row)


async def _provision_in_background(
    *, provisioner: VolumeProvisioner, volume_id: str, account_id: str, size_gb: float
) -> None:
    """Runs with its OWN DB session, same rationale as routers/images.py's
    _run_build_in_background -- provisioning is asynchronous, so it isn't
    tied to the triggering request's own session lifetime."""
    session_factory = get_session_factory()
    async with session_factory() as db:
        repo = SandboxVolumeRepository(db)
        await dispatch_volume_creation(
            repo=repo, provisioner=provisioner, volume_id=volume_id, account_id=account_id, size_gb=size_gb
        )


@router.post(
    "",
    response_model=VolumeAccepted,
    status_code=202,
    summary="Create an independent storage volume",
    description=(
        "Queues creation of a PVC-backed storage volume. Always "
        "asynchronous -- returns 202 with status='queued' immediately; "
        "poll GET /v1/volumes/{id} for progress. 429 with "
        "`volume_limit_reached` if this account is already at "
        "BOXKITE_MAX_VOLUMES_PER_ACCOUNT. 404s if volumes aren't enabled "
        "on this deployment."
    ),
)
async def create_volume(
    body: VolumeCreateRequest,
    request: Request,
    response: Response,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    provisioner: VolumeProvisioner = Depends(get_volume_provisioner),
) -> VolumeAccepted:
    _require_volumes_enabled()
    await _enforce_volume_rate_limit(request, response, account)

    volumes = SandboxVolumeRepository(db)
    active_count = await volumes.count_active_for_account(account.id)
    if active_count >= settings.BOXKITE_MAX_VOLUMES_PER_ACCOUNT:
        raise LimitExceededError(
            code="volume_limit_reached",
            message=(
                f"Volume limit reached ({settings.BOXKITE_MAX_VOLUMES_PER_ACCOUNT} at a time). "
                "Delete an existing volume before creating another."
            ),
            details={"limit": settings.BOXKITE_MAX_VOLUMES_PER_ACCOUNT, "active": active_count},
        )

    volume_id = str(uuid4())
    row = await volumes.create(
        volume_id=volume_id, account_id=account.id, label=body.label, size_gb=body.size_gb, status="queued"
    )

    asyncio.create_task(
        _provision_in_background(
            provisioner=provisioner, volume_id=volume_id, account_id=account.id, size_gb=body.size_gb
        )
    )

    return VolumeAccepted(id=row.id, label=row.label, status=row.status, created_at=row.created_at)


@router.get("", response_model=list[VolumeOut], summary="List this account's volumes")
async def list_volumes(
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[VolumeOut]:
    _require_volumes_enabled()
    rows = await SandboxVolumeRepository(db).list_for_account(account_id=account.id)
    return [_to_out(r) for r in rows]


@router.get(
    "/{volume_id}",
    response_model=VolumeOut,
    summary="Get a single volume's status",
    description="404s for a volume_id owned by a different account or one that never existed or has been deleted, identically.",
)
async def get_volume(
    volume_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> VolumeOut:
    _require_volumes_enabled()
    row = await SandboxVolumeRepository(db).get_for_account(volume_id=volume_id, account_id=account.id)
    if row is None or row.deleted_at is not None:
        raise ApiError(404, "not_found", "Volume not found")
    return _to_out(row)


@router.delete(
    "/{volume_id}",
    status_code=204,
    summary="Delete a volume",
    description=(
        "Deletes the control-plane's bookkeeping row for this volume and "
        "its underlying PVC. Does NOT retroactively unmount it from any "
        "already-running sandbox session -- Kubernetes itself keeps a PVC "
        "bound to a running pod alive until the pod is gone, same "
        "'control-plane deletion isn't retroactive' rule as image deletion."
    ),
)
async def delete_volume(
    request: Request,
    response: Response,
    volume_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
    provisioner: VolumeProvisioner = Depends(get_volume_provisioner),
) -> Response:
    _require_volumes_enabled()
    await _enforce_volume_rate_limit(request, response, account)
    volumes = SandboxVolumeRepository(db)
    row = await volumes.get_for_account(volume_id=volume_id, account_id=account.id)
    if row is None or row.deleted_at is not None:
        raise ApiError(404, "not_found", "Volume not found")

    if row.pvc_name:
        try:
            await provisioner.deprovision(pvc_name=row.pvc_name)
        except NotImplementedError:
            # Same "no live cluster in this test suite / compose mode"
            # status as K8sVolumeProvisioner.provision -- the control-plane
            # row is still marked deleted below; the PVC itself would need
            # the real provisioner implementation to actually be removed.
            logger.warning(f"[volumes] deprovision not implemented for pvc={row.pvc_name}")

    await volumes.mark_deleted(volume_id=volume_id)
    return Response(status_code=204, headers=dict(response.headers))
