"""Basic organizations / teams (GitHub follow-up to the audit's "no org/team
entity" gap). A named group an account creates and adds other accounts to,
with a coarse owner/admin/member role.

Deliberately minimal: this adds the org/team *entity* and membership CRUD so a
future change can scope sandbox ownership to an org without another migration.
It does NOT yet re-key sandboxes/usage onto organizations — those stay
account-scoped for now. No billing concepts (fair-use only, per this repo's
standing constraint).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..deps import get_current_account_via_api_key
from ..errors import ApiError
from ..models_orm import Account, Organization, OrganizationInvite, OrganizationMember
from ..schemas import (
    OrganizationCreateRequest,
    OrganizationInviteAcceptRequest,
    OrganizationInviteCreatedResponse,
    OrganizationInviteCreateRequest,
    OrganizationInviteResponse,
    OrganizationMemberAddRequest,
    OrganizationMemberResponse,
    OrganizationResponse,
)
from ..security import hash_secret

router = APIRouter(prefix="/v1/organizations", tags=["organizations"])

_MANAGER_ROLES = {"owner", "admin"}


async def _membership(db: AsyncSession, org_id: str, account_id: str) -> OrganizationMember | None:
    return (
        await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.account_id == account_id,
            )
        )
    ).scalar_one_or_none()


async def _require_membership(db: AsyncSession, org_id: str, account_id: str) -> OrganizationMember:
    member = await _membership(db, org_id, account_id)
    if member is None:
        # 404, not 403: don't reveal that an org the caller can't see exists.
        raise ApiError(404, "not_found", "Organization not found")
    return member


@router.post("", response_model=OrganizationResponse, status_code=201, summary="Create an organization")
async def create_organization(
    body: OrganizationCreateRequest,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> OrganizationResponse:
    org = Organization(name=body.name, created_by_account_id=account.id)
    db.add(org)
    await db.flush()
    db.add(OrganizationMember(organization_id=org.id, account_id=account.id, role="owner"))
    await db.commit()
    return OrganizationResponse(id=org.id, name=org.name, created_at=org.created_at, role="owner")


@router.get("", response_model=list[OrganizationResponse], summary="List organizations you belong to")
async def list_organizations(
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationResponse]:
    rows = (
        await db.execute(
            select(Organization, OrganizationMember.role)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(OrganizationMember.account_id == account.id)
            .order_by(Organization.created_at)
        )
    ).all()
    return [
        OrganizationResponse(id=org.id, name=org.name, created_at=org.created_at, role=role)
        for org, role in rows
    ]


@router.get("/{org_id}", response_model=OrganizationResponse, summary="Get an organization you belong to")
async def get_organization(
    org_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> OrganizationResponse:
    member = await _require_membership(db, org_id, account.id)
    org = await db.get(Organization, org_id)
    return OrganizationResponse(id=org.id, name=org.name, created_at=org.created_at, role=member.role)


@router.get(
    "/{org_id}/members",
    response_model=list[OrganizationMemberResponse],
    summary="List an organization's members",
)
async def list_members(
    org_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationMemberResponse]:
    await _require_membership(db, org_id, account.id)
    rows = (
        await db.execute(
            select(OrganizationMember, Account.email)
            .join(Account, Account.id == OrganizationMember.account_id)
            .where(OrganizationMember.organization_id == org_id)
            .order_by(OrganizationMember.created_at)
        )
    ).all()
    return [
        OrganizationMemberResponse(
            account_id=m.account_id, email=email, role=m.role, created_at=m.created_at
        )
        for m, email in rows
    ]


@router.post(
    "/{org_id}/members",
    response_model=OrganizationMemberResponse,
    status_code=201,
    summary="Add an existing account to an organization (owner/admin only)",
)
async def add_member(
    body: OrganizationMemberAddRequest,
    org_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> OrganizationMemberResponse:
    actor = await _require_membership(db, org_id, account.id)
    if actor.role not in _MANAGER_ROLES:
        raise ApiError(403, "forbidden", "Only an owner or admin can add members")

    target = (
        await db.execute(select(Account).where(Account.email == body.email))
    ).scalar_one_or_none()
    if target is None:
        raise ApiError(404, "not_found", "No account exists for that email")

    if await _membership(db, org_id, target.id) is not None:
        raise ApiError(409, "already_member", "That account is already a member of this organization")

    member = OrganizationMember(organization_id=org_id, account_id=target.id, role=body.role)
    db.add(member)
    await db.commit()
    return OrganizationMemberResponse(
        account_id=target.id, email=target.email, role=member.role, created_at=member.created_at
    )


@router.delete(
    "/{org_id}/members/{account_id}",
    status_code=204,
    summary="Remove a member from an organization (owner/admin only)",
)
async def remove_member(
    org_id: str = Path(...),
    account_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> None:
    actor = await _require_membership(db, org_id, account.id)
    if actor.role not in _MANAGER_ROLES:
        raise ApiError(403, "forbidden", "Only an owner or admin can remove members")

    target = await _membership(db, org_id, account_id)
    if target is None:
        raise ApiError(404, "not_found", "That account is not a member of this organization")

    if target.role == "owner":
        remaining_owners = (
            await db.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == org_id,
                    OrganizationMember.role == "owner",
                    OrganizationMember.account_id != account_id,
                )
            )
        ).first()
        if remaining_owners is None:
            raise ApiError(409, "last_owner", "Cannot remove the last owner of an organization")

    await db.delete(target)
    await db.commit()


async def _require_manager(db: AsyncSession, org_id: str, account_id: str) -> OrganizationMember:
    member = await _require_membership(db, org_id, account_id)
    if member.role not in _MANAGER_ROLES:
        raise ApiError(403, "forbidden", "Only an owner or admin can manage invites")
    return member


@router.post(
    "/{org_id}/invites",
    response_model=OrganizationInviteCreatedResponse,
    status_code=201,
    summary="Invite someone to an organization by email (owner/admin only)",
)
async def create_invite(
    body: OrganizationInviteCreateRequest,
    org_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> OrganizationInviteCreatedResponse:
    await _require_manager(db, org_id, account.id)

    # If the invitee already has an account and is already a member, there's
    # nothing to invite.
    existing = (
        await db.execute(select(Account).where(Account.email == body.email))
    ).scalar_one_or_none()
    if existing is not None and await _membership(db, org_id, existing.id) is not None:
        raise ApiError(409, "already_member", "That account is already a member of this organization")

    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    invite = OrganizationInvite(
        organization_id=org_id,
        email=body.email,
        role=body.role,
        token_hash=hash_secret(raw_token),
        invited_by_account_id=account.id,
        expires_at=now + timedelta(hours=settings.BOXKITE_ORG_INVITE_TTL_HOURS),
    )
    db.add(invite)
    await db.commit()
    return OrganizationInviteCreatedResponse(
        id=invite.id, email=invite.email, role=invite.role, expires_at=invite.expires_at, token=raw_token
    )


@router.get(
    "/{org_id}/invites",
    response_model=list[OrganizationInviteResponse],
    summary="List an organization's pending invites (owner/admin only)",
)
async def list_invites(
    org_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationInviteResponse]:
    await _require_manager(db, org_id, account.id)
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(OrganizationInvite)
            .where(
                OrganizationInvite.organization_id == org_id,
                OrganizationInvite.accepted_at.is_(None),
                OrganizationInvite.expires_at > now,
            )
            .order_by(OrganizationInvite.created_at)
        )
    ).scalars().all()
    return [
        OrganizationInviteResponse(
            id=i.id, email=i.email, role=i.role, created_at=i.created_at, expires_at=i.expires_at
        )
        for i in rows
    ]


@router.delete(
    "/{org_id}/invites/{invite_id}",
    status_code=204,
    summary="Revoke a pending invite (owner/admin only)",
)
async def revoke_invite(
    org_id: str = Path(...),
    invite_id: str = Path(...),
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_manager(db, org_id, account.id)
    invite = (
        await db.execute(
            select(OrganizationInvite).where(
                OrganizationInvite.id == invite_id,
                OrganizationInvite.organization_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if invite is None:
        raise ApiError(404, "not_found", "Invite not found")
    await db.delete(invite)
    await db.commit()


@router.post(
    "/accept-invite",
    response_model=OrganizationResponse,
    summary="Accept an organization invite with its token",
)
async def accept_invite(
    body: OrganizationInviteAcceptRequest,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> OrganizationResponse:
    invite = (
        await db.execute(
            select(OrganizationInvite).where(
                OrganizationInvite.token_hash == hash_secret(body.token)
            )
        )
    ).scalar_one_or_none()
    if invite is None:
        raise ApiError(404, "invalid_invite", "This invite token is invalid")
    if invite.accepted_at is not None:
        raise ApiError(409, "invite_used", "This invite has already been accepted")
    # SQLite round-trips DateTime(timezone=True) as naive; normalize to UTC so
    # the comparison works against both SQLite (tests) and Postgres (prod).
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise ApiError(400, "invite_expired", "This invite has expired")
    if invite.email.lower() != account.email.lower():
        raise ApiError(403, "email_mismatch", "This invite was issued to a different email address")

    if await _membership(db, invite.organization_id, account.id) is not None:
        raise ApiError(409, "already_member", "You are already a member of this organization")

    db.add(
        OrganizationMember(
            organization_id=invite.organization_id, account_id=account.id, role=invite.role
        )
    )
    invite.accepted_at = datetime.now(timezone.utc)
    await db.commit()

    org = await db.get(Organization, invite.organization_id)
    return OrganizationResponse(id=org.id, name=org.name, created_at=org.created_at, role=invite.role)
