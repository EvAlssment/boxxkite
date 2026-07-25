"""MCP OAuth 2.1 authorization server --
docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.

The control-plane acting as an OAuth *authorization server* for MCP
clients (Claude Code/Desktop, etc.) -- Dynamic Client Registration (RFC
7591), authorization-server/protected-resource metadata (RFC 8414/9728),
and the `authorize`/`token` endpoints (authorization_code + PKCE(S256) and
refresh_token grants, with refresh-token rotation and reuse detection).

Every route here is gated behind `BOXKITE_MCP_OAUTH_ENABLED` (off by
default, see config.py) -- this stands up an entire authorization server,
new attack surface that warrants its own security review before a
deployment turns it on, same posture `BOXKITE_IMAGE_BUILDER_ENABLED`/
`BOXKITE_AGENT_PTY_ENABLED` already established.

`/mcp/`'s own bearer-token check (hosted_mcp.py) is untouched by this
module -- it independently learns to accept the JWTs minted below (see
hosted_mcp.py's docstring for that half of the integration).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..errors import ApiError
from ..oauth_consent import render_consent_page, render_error_page, render_login_page
from ..oauth_session import account_from_login_cookie, set_login_session_cookie
from ..rate_limit import enforce_rate_limit
from ..repository import AccountRepository, OAuthAuthorizationCodeRepository, OAuthClientRepository, OAuthTokenRepository
from ..schemas import OAuthClientRegisterRequest, OAuthClientRegisterResponse
from ..security import (
    create_mcp_access_token,
    create_oauth_login_session_token,
    generate_authorization_code,
    generate_oauth_client_id,
    generate_refresh_token,
    hash_secret,
    mcp_resource_identifier,
    verify_password,
    verify_pkce_challenge,
)
from .auth import _reject_if_scim_deactivated

router = APIRouter(tags=["oauth"])


def _require_oauth_enabled() -> None:
    if not settings.BOXKITE_MCP_OAUTH_ENABLED:
        raise ApiError(404, "not_found", "Not found")


def _base_url(request: Request) -> str:
    if settings.BOXKITE_PUBLIC_URL:
        return settings.BOXKITE_PUBLIC_URL.rstrip("/")
    return str(request.base_url).rstrip("/")


def _expected_resource(request: Request) -> str:
    """The one RFC 8707 resource identifier this authorization server ever
    issues tokens for -- see `mcp_resource_identifier`."""
    return mcp_resource_identifier(_base_url(request))


def _resource_matches(resource: str | None, request: Request) -> bool:
    """Compares a caller-supplied `resource` parameter (RFC 8707) against
    this server's own resource identifier, modulo a trailing slash -- used
    at both `/oauth/authorize` and `/oauth/token` (both grant types) per
    GitHub issue #115."""
    return bool(resource) and resource.rstrip("/") == _expected_resource(request).rstrip("/")


async def _account_deactivated(db: AsyncSession, account_id: str) -> bool:
    """Used only by the token endpoint's two grant handlers, which must
    signal failure via `_oauth_error_json`'s RFC 6749 `{"error": ...}`
    envelope -- NOT `ApiError`'s `{"error": {"code", "message"}}` shape an
    MCP client SDK wouldn't recognize here (see `_oauth_error_json`'s own
    docstring). `authorize_login` below raises `ApiError` directly instead,
    since that HTML-form endpoint already relies on `ApiError`'s JSON
    envelope for its other gates (`_require_oauth_enabled`'s 404).

    This is the fix for a real gap: minting a fresh MCP access/refresh
    token pair from an already-issued authorization code or refresh token
    is exactly the "resolve an already-issued credential back to an
    Account" case `deps.py`'s `_reject_if_scim_deactivated` docstring
    describes -- a SCIM-deactivated account must not be able to complete
    the OAuth consent flow (or rotate a refresh token) and mint a brand
    new, long-lived MCP access token after being deactivated."""
    account = await AccountRepository(db).get_by_id(account_id)
    return account is None or account.scim_deactivated_at is not None


def _oauth_error_json(status_code: int, error: str, description: str | None = None) -> JSONResponse:
    """RFC 6749 §5.2 token-endpoint error shape -- `{"error": "...",
    "error_description": "..."}`, distinct from boxkite's usual
    `{"error": {"code", "message"}}` ApiError envelope, since MCP client
    SDKs parse this exact top-level `error` string to decide whether to
    retry/re-authenticate."""
    body: dict = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(status_code=status_code, content=body)


def _redirect_with_oauth_error(redirect_uri: str, *, error: str, state: str | None) -> RedirectResponse:
    params = {"error": error}
    if state is not None:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=status.HTTP_302_FOUND)


@router.get(
    "/.well-known/oauth-authorization-server",
    summary="MCP OAuth 2.1 authorization server metadata (RFC 8414)",
    dependencies=[Depends(_require_oauth_enabled)],
)
async def authorization_server_metadata(request: Request) -> dict:
    base = _base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@router.get(
    "/.well-known/oauth-protected-resource",
    summary="MCP protected-resource metadata (RFC 9728)",
    dependencies=[Depends(_require_oauth_enabled)],
)
async def protected_resource_metadata(request: Request) -> dict:
    base = _base_url(request)
    return {
        "resource": mcp_resource_identifier(base),
        "authorization_servers": [base],
    }


@router.post(
    "/oauth/register",
    response_model=OAuthClientRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Dynamic Client Registration (RFC 7591)",
    description=(
        "Unauthenticated by design -- RFC 7591's whole point is letting an MCP "
        "client register itself with no prior relationship. Rate-limited per-IP."
    ),
    dependencies=[Depends(_require_oauth_enabled)],
)
async def register_client(
    body: OAuthClientRegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> OAuthClientRegisterResponse:
    await enforce_rate_limit(
        request,
        bucket="oauth_dcr",
        limit=settings.BOXKITE_OAUTH_DCR_RATE_LIMIT_PER_MINUTE,
        response=response,
    )
    client_id = generate_oauth_client_id()
    client = await OAuthClientRepository(db).create(
        client_id=client_id, client_name=body.client_name, redirect_uris=body.redirect_uris
    )
    return OAuthClientRegisterResponse(
        client_id=client.client_id, client_name=client.client_name, redirect_uris=client.redirect_uris
    )


@router.get(
    "/oauth/authorize",
    response_class=HTMLResponse,
    summary="Authorization endpoint -- renders the consent screen",
    dependencies=[Depends(_require_oauth_enabled)],
)
async def authorize(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    params = request.query_params
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method")
    response_type = params.get("response_type")
    state = params.get("state")
    resource = params.get("resource")

    client = await OAuthClientRepository(db).get_by_client_id(client_id) if client_id else None
    if client is None:
        return HTMLResponse(render_error_page(message="Unknown client_id."), status_code=400)
    if redirect_uri not in client.redirect_uris:
        return HTMLResponse(
            render_error_page(message="redirect_uri does not match a registered redirect URI for this client."),
            status_code=400,
        )
    if response_type != "code":
        return _redirect_with_oauth_error(redirect_uri, error="unsupported_response_type", state=state)
    if code_challenge_method != "S256" or not code_challenge:
        return _redirect_with_oauth_error(redirect_uri, error="invalid_request", state=state)
    if not _resource_matches(resource, request):
        # RFC 8707: the client must declare which resource (protected API)
        # it wants an access token for, and this server must reject a
        # request for a resource it doesn't protect -- `invalid_target` is
        # RFC 8707's own error code for exactly this case.
        return _redirect_with_oauth_error(redirect_uri, error="invalid_target", state=state)

    account = await account_from_login_cookie(request, db)
    authorize_query = request.url.query
    if account is None:
        return HTMLResponse(
            render_login_page(
                client_name=client.client_name,
                authorize_query=authorize_query,
                github_enabled=settings.BOXKITE_SOCIAL_LOGIN_ENABLED and settings.github_oauth_configured,
                google_enabled=settings.BOXKITE_SOCIAL_LOGIN_ENABLED and settings.google_oauth_configured,
            )
        )
    return HTMLResponse(
        render_consent_page(
            client_name=client.client_name, account_email=account.email, authorize_query=authorize_query
        )
    )


@router.post(
    "/oauth/authorize/login",
    response_class=HTMLResponse,
    summary="Email+password login step of the consent screen",
    dependencies=[Depends(_require_oauth_enabled)],
)
async def authorize_login(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    await enforce_rate_limit(request, bucket="oauth_authorize_login", response=response)

    form = await request.form()
    authorize_query = str(form.get("authorize_query", ""))
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))

    accounts = AccountRepository(db)
    account = await accounts.get_by_email(email)
    if account is None or account.password_hash is None or not verify_password(password, account.password_hash):
        client_name = await _client_name_for_query(authorize_query, db)
        return HTMLResponse(
            render_login_page(
                client_name=client_name,
                authorize_query=authorize_query,
                github_enabled=settings.BOXKITE_SOCIAL_LOGIN_ENABLED and settings.github_oauth_configured,
                google_enabled=settings.BOXKITE_SOCIAL_LOGIN_ENABLED and settings.google_oauth_configured,
                error="Incorrect email or password.",
            ),
            status_code=401,
        )
    # Same credential-issuance-time gate routers/auth.py's login/refresh and
    # routers/social_login.py's _finish_login already apply -- a SCIM
    # (Directory Sync)-deactivated account must not be able to mint a new
    # login-session cookie for the MCP OAuth consent screen, even though it
    # still knows its own password.
    _reject_if_scim_deactivated(account)

    token, ttl = create_oauth_login_session_token(account_id=account.id)
    redirect = RedirectResponse(f"/oauth/authorize?{authorize_query}", status_code=status.HTTP_303_SEE_OTHER)
    set_login_session_cookie(redirect, token=token, ttl_seconds=ttl)
    return redirect


async def _client_name_for_query(authorize_query: str, db: AsyncSession) -> str:
    parsed = parse_qs(authorize_query)
    client_id = (parsed.get("client_id") or [None])[0]
    if not client_id:
        return "This application"
    client = await OAuthClientRepository(db).get_by_client_id(client_id)
    return client.client_name if client is not None else "This application"


@router.post(
    "/oauth/authorize/decide",
    summary="Allow/Deny decision step of the consent screen",
    dependencies=[Depends(_require_oauth_enabled)],
)
async def authorize_decide(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    authorize_query = str(form.get("authorize_query", ""))
    decision = str(form.get("decision", ""))
    parsed = parse_qs(authorize_query)

    def _first(key: str) -> str | None:
        values = parsed.get(key)
        return values[0] if values else None

    client_id = _first("client_id")
    redirect_uri = _first("redirect_uri")
    code_challenge = _first("code_challenge")
    code_challenge_method = _first("code_challenge_method")
    scope = _first("scope")
    state = _first("state")
    resource = _first("resource")

    client = await OAuthClientRepository(db).get_by_client_id(client_id) if client_id else None
    if client is None or redirect_uri not in client.redirect_uris:
        return HTMLResponse(render_error_page(message="Invalid authorization request."), status_code=400)
    if not _resource_matches(resource, request):
        # Re-validates `resource` from the posted-back `authorize_query`,
        # same defense-in-depth as the client_id/redirect_uri check above --
        # `GET /oauth/authorize` already validated it, but this is the step
        # that actually mints the authorization code.
        return _redirect_with_oauth_error(redirect_uri, error="invalid_target", state=state)

    account = await account_from_login_cookie(request, db)
    if account is None:
        return RedirectResponse(f"/oauth/authorize?{authorize_query}", status_code=status.HTTP_303_SEE_OTHER)

    if decision != "allow":
        return _redirect_with_oauth_error(redirect_uri, error="access_denied", state=state)

    code = generate_authorization_code()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.BOXKITE_MCP_AUTH_CODE_TTL_SECONDS)
    await OAuthAuthorizationCodeRepository(db).create(
        code=code,
        client_id=client.client_id,
        account_id=account.id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge or "",
        code_challenge_method=code_challenge_method or "S256",
        scope=scope,
        expires_at=expires_at,
    )
    params = {"code": code}
    if state is not None:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=status.HTTP_302_FOUND)


@router.post(
    "/oauth/token",
    summary="Token endpoint -- authorization_code+PKCE and refresh_token grants",
    dependencies=[Depends(_require_oauth_enabled)],
)
async def token(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        return await _handle_authorization_code_grant(form, db, request)
    if grant_type == "refresh_token":
        return await _handle_refresh_token_grant(form, db, request)
    return _oauth_error_json(400, "unsupported_grant_type", f"Unsupported grant_type: {grant_type!r}")


async def _handle_authorization_code_grant(form, db: AsyncSession, request: Request):
    code_value = form.get("code")
    redirect_uri = form.get("redirect_uri")
    client_id = form.get("client_id")
    code_verifier = form.get("code_verifier")
    resource = form.get("resource")
    if not (code_value and redirect_uri and client_id and code_verifier and resource):
        return _oauth_error_json(400, "invalid_request", "Missing required parameter")
    if not _resource_matches(str(resource), request):
        return _oauth_error_json(
            400, "invalid_target", "resource does not match a resource this server protects"
        )

    codes = OAuthAuthorizationCodeRepository(db)
    code_row = await codes.get_valid_by_code(str(code_value))
    if (
        code_row is None
        or code_row.client_id != client_id
        or code_row.redirect_uri != redirect_uri
    ):
        return _oauth_error_json(400, "invalid_grant", "Authorization code is invalid, expired, or already used")
    if not verify_pkce_challenge(code_verifier=str(code_verifier), code_challenge=code_row.code_challenge):
        return _oauth_error_json(400, "invalid_grant", "code_verifier does not match code_challenge")
    if await _account_deactivated(db, code_row.account_id):
        return _oauth_error_json(400, "invalid_grant", "Account has been deactivated")

    consumed = await codes.mark_consumed(code_id=code_row.id)
    if not consumed:
        # Lost a race with a concurrent exchange of the same code (e.g. a
        # leaked code, or a client retrying after a timeout) -- report the
        # same generic invalid_grant a never-existed/expired code gets,
        # never a more specific error (RFC 6749 §5.2).
        return _oauth_error_json(400, "invalid_grant", "Authorization code is invalid, expired, or already used")
    return await _issue_token_pair(
        db, client_id=code_row.client_id, account_id=code_row.account_id, scope=code_row.scope, request=request
    )


async def _handle_refresh_token_grant(form, db: AsyncSession, request: Request):
    refresh_token_value = form.get("refresh_token")
    client_id = form.get("client_id")
    resource = form.get("resource")
    if not refresh_token_value or not resource:
        return _oauth_error_json(400, "invalid_request", "Missing refresh_token or resource")
    if not _resource_matches(str(resource), request):
        return _oauth_error_json(
            400, "invalid_target", "resource does not match a resource this server protects"
        )

    tokens = OAuthTokenRepository(db)
    token_row = await tokens.get_by_hash(hash_secret(str(refresh_token_value)))
    if token_row is None or (client_id and token_row.client_id != client_id):
        return _oauth_error_json(400, "invalid_grant", "Refresh token is invalid")
    if token_row.revoked_at is not None:
        # Reuse of an already-rotated refresh token: strong signal of theft
        # (OAuth 2.1's own recommendation) -- revoke the whole rotation
        # chain, not just this row, so a stale copy held by an attacker (or
        # by the legitimate caller after a race) can't keep working either.
        await tokens.revoke_family(token_id=token_row.id)
        return _oauth_error_json(400, "invalid_grant", "Refresh token has already been used")
    if await _account_deactivated(db, token_row.account_id):
        return _oauth_error_json(400, "invalid_grant", "Account has been deactivated")

    revoked = await tokens.revoke(token_id=token_row.id)
    if not revoked:
        # Lost a race with a concurrent refresh_token grant presenting the
        # same not-yet-rotated token -- exactly the theft scenario reuse
        # detection exists to catch (an attacker and the legitimate holder
        # both using their copy around the same time). Revoke the whole
        # chain, including whichever request won the race, rather than
        # just erroring: two concurrent presentations of one refresh token
        # is itself the suspicious signal, independent of which one got
        # there first.
        await tokens.revoke_family(token_id=token_row.id)
        return _oauth_error_json(400, "invalid_grant", "Refresh token has already been used")
    return await _issue_token_pair(
        db,
        client_id=token_row.client_id,
        account_id=token_row.account_id,
        scope=None,
        request=request,
        rotated_from=token_row.id,
    )


async def _issue_token_pair(
    db: AsyncSession,
    *,
    client_id: str,
    account_id: str,
    scope: str | None,
    request: Request,
    rotated_from: str | None = None,
):
    audience = _expected_resource(request)
    access_token, expires_in = create_mcp_access_token(account_id=account_id, client_id=client_id, audience=audience)
    refresh_token_value, refresh_token_hash = generate_refresh_token()
    await OAuthTokenRepository(db).create(
        client_id=client_id,
        account_id=account_id,
        refresh_token_hash=refresh_token_hash,
        rotated_from=rotated_from,
    )
    body = {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "refresh_token": refresh_token_value,
    }
    if scope:
        body["scope"] = scope
    return JSONResponse(content=body)
