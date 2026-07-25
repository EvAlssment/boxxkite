"""GitHub/Google social login -- docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md
§4. Independent of MCP OAuth (routers/oauth.py) -- this is the
control-plane acting as an OAuth *client* to GitHub/Google, reused by the
`/oauth/authorize` consent screen's login step (`next` query param) but
equally usable standalone.

Each provider is gated on `BOXKITE_SOCIAL_LOGIN_ENABLED` AND that
provider's own client id/secret being configured (`settings.
github_oauth_configured`/`google_oauth_configured`) -- until then its
routes 404, per config.py's own docstring for these settings.

Four response shapes depending on where the caller arrived from and
whether login succeeded:
- **From the `/oauth/authorize` consent screen** (`next` query param,
  restricted to that one internal path to prevent open-redirect): sets the
  same short-lived `/oauth/*`-scoped session cookie `routers/oauth.py`'s
  login form sets, then redirects to `next`.
- **From the dashboard's own login page, success** (`next` exactly equal
  to `{BOXKITE_DASHBOARD_URL}/dashboard/oauth-callback`, an exact-match
  allowlisted absolute URL, only reachable when an operator has
  configured `BOXKITE_DASHBOARD_URL`): redirects there with the minted
  access token in the URL **fragment**
  (`#access_token=...&expires_in=...&token_type=bearer`), never a query
  param -- a fragment is never sent to the server in the HTTP request
  line, never appears in a `Referer` header on subsequent navigation, and
  is never written to this or the dashboard host's own access logs. The
  dashboard's `/dashboard/oauth-callback` page reads it client-side
  (`window.location.hash`) and stores it exactly like the password-login
  flow already does.
- **From the dashboard's own login page, failure** (same `next` as
  above, but `fetch_github_profile`/`fetch_google_profile`/
  `_resolve_or_create_account`/`_finish_login` raised an `ApiError` --
  e.g. an unverified provider email, a provider-identity conflict, or a
  SCIM-deactivated account): redirects there with `?error=<code>&
  error_description=<message>` as query params instead -- not a fragment,
  since an error code/message isn't sensitive the way a token is. Lets the
  dashboard render an actual error page instead of the browser sitting on
  this control-plane's raw JSON error response (see
  `_dashboard_error_redirect`).
- **Anything else (no `next`, an unrecognized value, or the `next`
  couldn't be trusted because `state` itself failed to decode)**: returns
  the same dashboard `TokenResponse` JSON `/v1/auth/login` already returns
  on success, or the normal `ApiError` JSON envelope on failure -- the
  pragmatic fallback for any caller integrating this route directly
  rather than through the dashboard or the MCP consent screen.

Also doubles as the callback for **linking** a provider identity onto an
already-authenticated account (routers/account.py's
`POST /v1/account/link/{provider}/start`), when `/{provider}/start` is
given a `link_token` -- see `_decode_link_token_or_raise` and
`_link_provider_to_account`. This is a completely different trust
decision from `_resolve_or_create_account`'s email-based auto-link: here,
the target account is already known (from the caller's own dashboard
session at mint time, not from matching this OAuth profile's email), so
there's no ambiguity about who's asking.
"""

from __future__ import annotations

from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..errors import ApiError
from ..oauth_providers import (
    GITHUB_AUTHORIZE_URL,
    GOOGLE_AUTHORIZE_URL,
    fetch_github_profile,
    fetch_google_profile,
)
from ..oauth_session import set_login_session_cookie
from ..repository import AccountRepository
from ..schemas import AccountOut, TokenResponse
from ..security import (
    SOCIAL_LOGIN_STATE_TTL_SECONDS,
    create_access_token,
    create_oauth_login_session_token,
    create_social_login_state_token,
    decode_account_link_intent_token,
    decode_social_login_state_token,
)
from .auth import issue_refresh_token

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_SAFE_NEXT_PREFIX = "/oauth/authorize"

# Binds `state` to the browser that started this flow (see
# create_social_login_state_token's docstring for the login-CSRF/session-
# fixation this closes) -- set at /{provider}/start, checked and cleared at
# /{provider}/callback. Scoped to /v1/auth so it isn't sent on unrelated
# requests, same rationale as OAUTH_LOGIN_SESSION_COOKIE's own `path`.
_STATE_NONCE_COOKIE = "boxkite_oauth_state_nonce"


def _set_state_nonce_cookie(response: Response, *, nonce: str) -> None:
    response.set_cookie(
        _STATE_NONCE_COOKIE,
        nonce,
        max_age=SOCIAL_LOGIN_STATE_TTL_SECONDS,
        httponly=True,
        secure=not settings.is_dev_environment,
        samesite="lax",
        path="/v1/auth",
    )


def _verify_and_clear_state_nonce(request: Request, response: Response, *, expected_nonce: str) -> bool:
    """True only if the cookie set at /start on this same browser matches
    the nonce embedded in `state`. Always clears the cookie afterward
    (single-use, whether the check passed or failed) so a captured
    callback URL can't be replayed against a second, later cookie."""
    presented = request.cookies.get(_STATE_NONCE_COOKIE)
    response.delete_cookie(_STATE_NONCE_COOKIE, path="/v1/auth")
    return presented is not None and presented == expected_nonce


def _safe_next(next_path: str | None) -> str | None:
    """Only ever allow redirecting back into this server's own
    `/oauth/authorize` flow -- any other value is dropped rather than
    honored, since `next` ultimately came from a query string an attacker
    could also construct (classic open-redirect surface)."""
    if next_path and next_path.startswith(_SAFE_NEXT_PREFIX):
        return next_path
    return None


def _dashboard_oauth_callback_url() -> str | None:
    """The one, exact, operator-configured dashboard URL this route is ever
    allowed to redirect a standalone login to -- `None` (never matches)
    unless `BOXKITE_DASHBOARD_URL` is explicitly set. Deliberately an EXACT
    string match against this single computed value in `_dashboard_safe_next`
    below, not a prefix/domain check, since the dashboard is a different
    origin than this control-plane and a prefix match against a caller-
    supplied `next` would reopen the exact open-redirect `_safe_next` above
    exists to prevent."""
    if not settings.BOXKITE_DASHBOARD_URL:
        return None
    return f"{settings.BOXKITE_DASHBOARD_URL.rstrip('/')}/dashboard/oauth-callback"


def _dashboard_safe_next(next_path: str | None) -> str | None:
    callback_url = _dashboard_oauth_callback_url()
    if callback_url is not None and next_path == callback_url:
        return callback_url
    return None


def _any_safe_next(next_path: str | None) -> str | None:
    """Used at the `/github/start` and `/google/start` step, before the
    provider round-trip, to decide what `next` value is worth preserving in
    the signed `state` token at all -- anything that survives this is later
    re-checked by `_safe_next`/`_dashboard_safe_next` independently in
    `_finish_login`, so this is a first filter, not the only one."""
    return _safe_next(next_path) or _dashboard_safe_next(next_path)


def _require_github_enabled() -> None:
    if not (settings.BOXKITE_SOCIAL_LOGIN_ENABLED and settings.github_oauth_configured):
        raise ApiError(404, "not_found", "Not found")


def _require_google_enabled() -> None:
    if not (settings.BOXKITE_SOCIAL_LOGIN_ENABLED and settings.google_oauth_configured):
        raise ApiError(404, "not_found", "Not found")


def _base_url(request: Request) -> str:
    if settings.BOXKITE_PUBLIC_URL:
        return settings.BOXKITE_PUBLIC_URL.rstrip("/")
    return str(request.base_url).rstrip("/")


async def _resolve_or_create_account(
    db: AsyncSession, *, provider: str, provider_user_id: str, email: str
):
    accounts = AccountRepository(db)
    id_field = "github_id" if provider == "github" else "google_id"
    lookup = accounts.get_by_github_id if provider == "github" else accounts.get_by_google_id
    existing = await lookup(provider_user_id)
    if existing is not None:
        return existing

    email_owner = await accounts.get_by_email(email)
    if email_owner is not None:
        if getattr(email_owner, id_field) is not None:
            # This email's account already has a DIFFERENT github_id/
            # google_id linked than the one currently authenticating (e.g.
            # the provider-side account was deleted and recreated under a
            # new id) -- an unusual case, not the common "signing in with a
            # second provider for the first time" one below, so don't
            # silently reassign it.
            raise ApiError(
                409,
                "account_provider_conflict",
                f"This account already has a different {provider.capitalize()} identity linked. "
                f"Log in with your existing method and update it from account settings.",
            )
        if (
            email_owner.password_hash is not None
            or email_owner.sso_provider_user_id is not None
            or email_owner.scim_directory_user_id is not None
        ):
            # NOT safe to auto-link, unlike the social-only case below.
            # `fetch_github_profile`/`fetch_google_profile` proves the
            # *authenticating* party currently controls this email inbox --
            # it proves nothing about who already controls the matched
            # account:
            #  - a `password_hash` means the account could have been
            #    created by anyone who merely typed this email at
            #    POST /v1/auth/signup, which performs no ownership
            #    verification at all (see routers/auth.py's own
            #    docstring) -- auto-linking here would let a pre-registered
            #    attacker account silently absorb a victim's real OAuth
            #    identity the first time the victim signs in.
            #  - `sso_provider_user_id`/`scim_directory_user_id` means this
            #    account is managed by an enterprise SSO connection/SCIM
            #    directory -- linking a personal Google/GitHub identity
            #    onto it would bypass whatever IdP-level policy (MFA,
            #    conditional access) that organization enforces, the exact
            #    boundary enterprise_sso.py's own `_resolve_or_create_account`
            #    protects with its `organization_id` check.
            raise ApiError(
                409,
                "account_email_exists",
                f"An account with this email already exists -- log in with your existing method first, "
                f"then link {provider.capitalize()} from account settings",
            )
        # Safe to auto-link: the matched account has no password and no
        # enterprise-SSO ties, so it was itself created purely through a
        # provider-verified social login (`create_social`) -- both
        # identities have independently proven control of this email
        # through their own OAuth consent, so this is the "signing in with
        # a second provider for the first time" case, not a takeover risk.
        return await accounts.link_provider_identity(email_owner, **{id_field: provider_user_id})

    kwargs = {id_field: provider_user_id}
    return await accounts.create_social(email=email, **kwargs)


def _decode_link_token_or_raise(link_token: str | None, *, provider: str) -> str | None:
    """Returns the `account_id` a `link_token` (minted by
    POST /v1/account/link/{provider}/start) proves this login was started
    to link into, or `None` if no `link_token` was given at all -- the
    normal login case. Raises rather than silently falling back to a
    normal login if a `link_token` WAS given but is invalid/expired/for
    the wrong provider, since silently logging the caller into a normal
    session instead of completing the link they asked for would be
    confusing at best."""
    if link_token is None:
        return None
    try:
        payload = decode_account_link_intent_token(link_token)
    except jwt.PyJWTError:
        raise ApiError(400, "invalid_link_token", "Invalid or expired account-link request") from None
    if payload.get("provider") != provider:
        raise ApiError(400, "invalid_link_token", "Account-link request does not match this provider")
    return payload["sub"]


async def _link_provider_to_account(
    db: AsyncSession, *, provider: str, provider_user_id: str, account_id: str
):
    """Links a GitHub/Google identity onto a SPECIFIC, already-known
    account -- the target came from the caller's own dashboard session at
    `link_token` mint time (routers/account.py's `start_link_provider`),
    not from matching this OAuth profile's email the way
    `_resolve_or_create_account` does, so there's no ambiguity about who's
    asking to link what."""
    accounts = AccountRepository(db)
    id_field = "github_id" if provider == "github" else "google_id"
    lookup = accounts.get_by_github_id if provider == "github" else accounts.get_by_google_id

    existing_owner = await lookup(provider_user_id)
    if existing_owner is not None and existing_owner.id == account_id:
        return existing_owner  # Already linked to this exact account -- idempotent no-op.
    if existing_owner is not None:
        raise ApiError(
            409,
            "provider_identity_in_use",
            f"This {provider.capitalize()} account is already linked to a different boxkite account.",
        )

    account = await accounts.get_by_id(account_id)
    if account is None:
        raise ApiError(404, "account_not_found", "Account not found")
    if getattr(account, id_field) is not None:
        raise ApiError(
            409,
            "account_provider_conflict",
            f"Your account already has a different {provider.capitalize()} identity linked. "
            f"Unlink it from account settings before linking a new one.",
        )

    return await accounts.link_provider_identity(account, **{id_field: provider_user_id})


def _dashboard_error_redirect(next_path: str | None, err: ApiError) -> RedirectResponse | None:
    """Turns an `ApiError` raised anywhere after a validly-decoded OAuth
    `state` into a redirect back to the dashboard's own callback page
    (query params, not a fragment -- an error code/message isn't sensitive
    the way an access token is), so the browser lands on a page this site
    controls and can render nicely instead of sitting on this
    control-plane's raw JSON error response. Returns `None` (caller should
    re-raise) when `next_path` isn't a recognized dashboard callback --
    same "anything else falls back to raw JSON" contract as `_finish_login`."""
    dashboard_next = _dashboard_safe_next(next_path)
    if dashboard_next is None:
        return None
    params = urlencode({"error": err.code, "error_description": err.message})
    return RedirectResponse(f"{dashboard_next}?{params}", status_code=status.HTTP_303_SEE_OTHER)


async def _finish_login(request: Request, response_next: str | None, account, db: AsyncSession) -> Response:
    if account.scim_deactivated_at is not None:
        # SCIM (Directory Sync, routers/scim.py) has deactivated this
        # account -- checked here since GitHub/Google/enterprise-SSO login
        # all funnel through this one function (see this function's
        # callers). Checked at the point of minting a NEW credential, not
        # just at deps.py's ongoing-request gate, so a deactivated account
        # gets an explicit, actionable error here rather than a generic
        # invalid-token failure on its very next API call.
        raise ApiError(
            403,
            "account_deactivated",
            "This account has been deactivated by your organization's administrator",
        )
    safe_next = _safe_next(response_next)
    if safe_next is not None:
        token, ttl = create_oauth_login_session_token(account_id=account.id)
        redirect = RedirectResponse(safe_next, status_code=status.HTTP_303_SEE_OTHER)
        set_login_session_cookie(redirect, token=token, ttl_seconds=ttl)
        return redirect

    access_token, expires_in = create_access_token(account_id=account.id, email=account.email)

    # Same opt-in refresh token password login already mints (see
    # routers/auth.py's _issue_token_response) -- without this, an
    # OAuth-authenticated dashboard session would expire at
    # ACCESS_TOKEN_TTL_MINUTES regardless of the flag, while a
    # password-authenticated one silently renews.
    refresh_token_raw: str | None = None
    if settings.BOXKITE_REFRESH_TOKENS_ENABLED:
        refresh_token_raw = await issue_refresh_token(db, account.id)

    dashboard_next = _dashboard_safe_next(response_next)
    if dashboard_next is not None:
        # Fragment, not a query param: never sent to any server in the HTTP
        # request line, never logged by this or the dashboard host's access
        # logs, never forwarded via `Referer` on a later navigation. The
        # dashboard's own /dashboard/oauth-callback page reads this
        # client-side (window.location.hash) and stores it exactly like the
        # password-login flow already does -- see _dashboard_oauth_callback_url's
        # docstring for why this is an exact-match allowlist, not a prefix.
        fragment_params = {"access_token": access_token, "expires_in": expires_in, "token_type": "bearer"}
        if refresh_token_raw is not None:
            fragment_params["refresh_token"] = refresh_token_raw
        fragment = urlencode(fragment_params)
        return RedirectResponse(f"{dashboard_next}#{fragment}", status_code=status.HTTP_303_SEE_OTHER)

    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=refresh_token_raw,
        account=AccountOut.model_validate(account),
    )


@router.get(
    "/github/start",
    summary="Start GitHub OAuth login",
    dependencies=[Depends(_require_github_enabled)],
)
async def github_start(
    request: Request, next: str | None = Query(default=None), link_token: str | None = Query(default=None)
) -> RedirectResponse:
    base = _base_url(request)
    link_account_id = _decode_link_token_or_raise(link_token, provider="github")
    state, nonce = create_social_login_state_token(
        provider="github", next_path=_any_safe_next(next), link_account_id=link_account_id
    )
    params = {
        "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
        "redirect_uri": f"{base}/v1/auth/github/callback",
        "scope": "read:user user:email",
        "state": state,
    }
    redirect = RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}", status_code=status.HTTP_302_FOUND)
    _set_state_nonce_cookie(redirect, nonce=nonce)
    return redirect


@router.get(
    "/github/callback",
    summary="GitHub OAuth callback",
    dependencies=[Depends(_require_github_enabled)],
)
async def github_callback(
    request: Request,
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    base = _base_url(request)
    try:
        state_payload = decode_social_login_state_token(state)
    except jwt.PyJWTError:
        raise ApiError(400, "invalid_state", "Invalid or expired OAuth state") from None
    if state_payload.get("provider") != "github":
        raise ApiError(400, "invalid_state", "OAuth state does not match this provider")
    if not _verify_and_clear_state_nonce(request, response, expected_nonce=state_payload.get("nonce")):
        # Valid signature, wrong browser -- the (code, state) pair wasn't
        # issued to whoever is presenting it now (see
        # create_social_login_state_token's docstring for the login-CSRF
        # scenario this catches).
        raise ApiError(400, "invalid_state", "OAuth state was not issued to this browser")

    next_path = state_payload.get("next")
    link_account_id = state_payload.get("link_account_id")
    try:
        profile = await fetch_github_profile(code=code, redirect_uri=f"{base}/v1/auth/github/callback")
        if link_account_id is not None:
            account = await _link_provider_to_account(
                db, provider="github", provider_user_id=profile.provider_user_id, account_id=link_account_id
            )
        else:
            account = await _resolve_or_create_account(
                db, provider="github", provider_user_id=profile.provider_user_id, email=profile.email
            )
        result = await _finish_login(request, next_path, account, db)
    except ApiError as err:
        redirect = _dashboard_error_redirect(next_path, err)
        if redirect is None:
            raise
        result = redirect
    if isinstance(result, Response):
        # _verify_and_clear_state_nonce already cleared it on the injected
        # `response` dependency, which FastAPI only merges into the final
        # response when a plain (non-Response) value is returned -- when
        # _finish_login/_dashboard_error_redirect hand back their own
        # concrete Response (a redirect), that merge doesn't happen, so
        # clear it again directly on the object actually being returned.
        result.delete_cookie(_STATE_NONCE_COOKIE, path="/v1/auth")
    return result


@router.get(
    "/google/start",
    summary="Start Google OAuth login",
    dependencies=[Depends(_require_google_enabled)],
)
async def google_start(
    request: Request, next: str | None = Query(default=None), link_token: str | None = Query(default=None)
) -> RedirectResponse:
    base = _base_url(request)
    link_account_id = _decode_link_token_or_raise(link_token, provider="google")
    state, nonce = create_social_login_state_token(
        provider="google", next_path=_any_safe_next(next), link_account_id=link_account_id
    )
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": f"{base}/v1/auth/google/callback",
        "scope": "openid email profile",
        "response_type": "code",
        "state": state,
    }
    redirect = RedirectResponse(f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}", status_code=status.HTTP_302_FOUND)
    _set_state_nonce_cookie(redirect, nonce=nonce)
    return redirect


@router.get(
    "/google/callback",
    summary="Google OAuth callback",
    dependencies=[Depends(_require_google_enabled)],
)
async def google_callback(
    request: Request,
    response: Response,
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    base = _base_url(request)
    try:
        state_payload = decode_social_login_state_token(state)
    except jwt.PyJWTError:
        raise ApiError(400, "invalid_state", "Invalid or expired OAuth state") from None
    if state_payload.get("provider") != "google":
        raise ApiError(400, "invalid_state", "OAuth state does not match this provider")
    if not _verify_and_clear_state_nonce(request, response, expected_nonce=state_payload.get("nonce")):
        raise ApiError(400, "invalid_state", "OAuth state was not issued to this browser")

    next_path = state_payload.get("next")
    link_account_id = state_payload.get("link_account_id")
    try:
        profile = await fetch_google_profile(code=code, redirect_uri=f"{base}/v1/auth/google/callback")
        if link_account_id is not None:
            account = await _link_provider_to_account(
                db, provider="google", provider_user_id=profile.provider_user_id, account_id=link_account_id
            )
        else:
            account = await _resolve_or_create_account(
                db, provider="google", provider_user_id=profile.provider_user_id, email=profile.email
            )
        result = await _finish_login(request, next_path, account, db)
    except ApiError as err:
        redirect = _dashboard_error_redirect(next_path, err)
        if redirect is None:
            raise
        result = redirect
    if isinstance(result, Response):
        result.delete_cookie(_STATE_NONCE_COOKIE, path="/v1/auth")
    return result
