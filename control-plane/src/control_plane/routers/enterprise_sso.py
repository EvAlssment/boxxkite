"""Enterprise SSO login -- docs/ENTERPRISE-SSO-DESIGN.md, issue #126 Phase 1
(interactive login). Phase 2 (SCIM 2.0 provisioning, routers/scim.py) is a
separate module -- see that module's docstring -- but this file's account
resolution has one deliberate interaction with it: see
`_resolve_or_create_account`'s SCIM-provisioned-account-linking branch
below.

Structurally mirrors routers/social_login.py as closely as possible: same
`next`-restricted-to-/oauth/authorize resume pattern, same signed-JWT
`state` CSRF defense, same email-collision anti-takeover refusal, same
`_finish_login` cookie-or-TokenResponse split, same `_dashboard_error_redirect`
treatment for turning a post-state-decode `ApiError` into a dashboard
redirect instead of a raw JSON response. The one real difference is *who*
asserts identity -- a hosted SSO broker (WorkOS) standing in front of an
enterprise's own SAML/OIDC IdP, keyed by an operator-assigned `connection`
identifier, rather than a public consumer identity provider.
"""

from __future__ import annotations

import jwt
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..enterprise_sso_client import EnterpriseSsoProfile, get_enterprise_sso_client
from ..errors import ApiError
from ..repository import AccountRepository
from ..security import create_enterprise_sso_state_token, decode_enterprise_sso_state_token
from .social_login import _any_safe_next, _base_url, _dashboard_error_redirect, _finish_login

router = APIRouter(prefix="/v1/auth/sso", tags=["auth"])


def _require_enterprise_sso_enabled() -> None:
    if not (settings.BOXKITE_ENTERPRISE_SSO_ENABLED and settings.enterprise_sso_configured):
        raise ApiError(404, "not_found", "Not found")


def _is_scim_only_shell(account) -> bool:
    """True only for an account with literally no other way to
    authenticate: no password, no GitHub/Google identity, and a
    `scim_directory_user_id` set (i.e. it exists solely because
    routers/scim.py's `create_scim` provisioned it). Deliberately narrow --
    an account that also has a password or a linked social identity does
    NOT qualify, so this exception can never be used to take over an
    account that already has a real credential attached."""
    return (
        account.password_hash is None
        and account.github_id is None
        and account.google_id is None
        and account.scim_directory_user_id is not None
    )


async def _resolve_or_create_account(db: AsyncSession, *, profile: EnterpriseSsoProfile):
    accounts = AccountRepository(db)
    existing = await accounts.get_by_sso_subject_id(profile.provider_user_id)
    if existing is not None:
        return existing

    email_owner = await accounts.get_by_email(profile.email)
    if email_owner is not None:
        # The ONE deliberate exception to the email-collision refusal
        # below: SCIM (Directory Sync, routers/scim.py) already created
        # this account's "shell" from the same enterprise's own
        # IdP-asserted identity, before this person ever completed an
        # interactive SSO login -- there is no password/GitHub/Google
        # credential on this row to protect, and unlike a random email
        # match against a GitHub/Google account (which anyone can create
        # under any email they merely control), this assertion comes from
        # the same admin-controlled IdP that provisioned the row in the
        # first place. Without this exception a SCIM-provisioned user
        # could never actually log in at all: they have no password to
        # satisfy the refusal below's "log in with your password first"
        # instruction.
        #
        # BUT this control-plane can serve multiple enterprise customers
        # over one WorkOS project (disclosed Phase-1 scope cut: `GET
        # /v1/auth/sso/start`'s `connection` query param is caller-supplied
        # -- see docs/ENTERPRISE-SSO-DESIGN.md §3's "Connection selection"
        # note). Without an organization check here, an admin of Customer
        # A's own IdP could assert an SSO login for an email that happens
        # to match a SCIM-provisioned shell account belonging to Customer
        # B, and get auto-linked to (i.e. take over) Customer B's account.
        # `create_scim` always stamps `sso_organization_id` from the
        # provisioning webhook's own `organization_id` (never
        # `sso_connection_id` -- SCIM directory-sync events don't carry a
        # connection id, only an organization id, so that field is never
        # set on a SCIM-only shell); requiring an exact, non-null match
        # against THIS login's `profile.organization_id` is therefore the
        # correct binding check -- comparing `connection_id` instead would
        # never match on a legitimate first login and would permanently
        # break this exception.
        if (
            _is_scim_only_shell(email_owner)
            and email_owner.sso_organization_id is not None
            and profile.organization_id == email_owner.sso_organization_id
        ):
            return await accounts.link_sso_identity(
                account_id=email_owner.id,
                sso_provider_user_id=profile.provider_user_id,
                sso_organization_id=profile.organization_id,
                sso_connection_id=profile.connection_id,
            )
        # Account-takeover protection: a matching email alone is not proof
        # of identity ownership from the SSO broker's side -- do NOT
        # silently link. Require an explicit password login first.
        # social_login._resolve_or_create_account auto-links a verified
        # GitHub/Google identity onto an *existing social-only* account for
        # the same reason the SCIM-shell exception above exists here (both
        # sides have independently proven control of the email), but it
        # explicitly excludes any account with `sso_provider_user_id`/
        # `scim_directory_user_id` set -- i.e. it defers to this function's
        # tenant boundary rather than reimplementing it, so an
        # enterprise-managed account can only ever be linked through the
        # organization-scoped check above, never through social login. This
        # branch is also the fallback for a SCIM shell whose
        # organization_id does NOT match this login's profile -- refuse
        # rather than auto-link across a tenant boundary.
        raise ApiError(
            409,
            "account_email_exists",
            "An account with this email already exists -- log in with your password first, "
            "then link SSO from account settings",
        )

    return await accounts.create_sso(
        email=profile.email,
        sso_provider_user_id=profile.provider_user_id,
        sso_organization_id=profile.organization_id,
        sso_connection_id=profile.connection_id,
    )


@router.get(
    "/start",
    summary="Start enterprise SSO login",
    dependencies=[Depends(_require_enterprise_sso_enabled)],
)
async def sso_start(
    request: Request,
    connection: str = Query(..., description="Operator-assigned SSO connection/organization identifier"),
    next: str | None = Query(default=None),
) -> RedirectResponse:
    base = _base_url(request)
    state = create_enterprise_sso_state_token(connection=connection, next_path=_any_safe_next(next))
    redirect_uri = f"{base}/v1/auth/sso/callback"
    client = get_enterprise_sso_client()
    authorize_url = client.authorization_url(connection_selector=connection, redirect_uri=redirect_uri, state=state)
    return RedirectResponse(authorize_url, status_code=302)


@router.get(
    "/callback",
    summary="Enterprise SSO callback",
    dependencies=[Depends(_require_enterprise_sso_enabled)],
)
async def sso_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    base = _base_url(request)
    try:
        state_payload = decode_enterprise_sso_state_token(state)
    except jwt.PyJWTError:
        raise ApiError(400, "invalid_state", "Invalid or expired SSO state") from None

    next_path = state_payload.get("next")
    try:
        client = get_enterprise_sso_client()
        profile = await client.fetch_profile(code=code, redirect_uri=f"{base}/v1/auth/sso/callback")
        account = await _resolve_or_create_account(db, profile=profile)
        return await _finish_login(request, next_path, account, db)
    except ApiError as err:
        # Same treatment as social_login.py's github_callback/
        # google_callback: without this, any ApiError here (invalid
        # profile fetch, the email-collision refusal, a SCIM-deactivated
        # account from _finish_login) would leave the browser sitting on
        # this control-plane's raw JSON response even when the login
        # arrived via the dashboard's next= redirect.
        redirect = _dashboard_error_redirect(next_path, err)
        if redirect is None:
            raise
        return redirect
