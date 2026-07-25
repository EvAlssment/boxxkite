"""Password hashing, JWT issue/verify, and API-key/opaque-token generation.

- Passwords are hashed with argon2 (via passlib's `CryptContext`) — a modern,
  memory-hard KDF, per SECURITY.md's "web/security" expectations.
- JWTs are short-lived access tokens for the dashboard UI. Refresh-token
  rotation (opt-in, `BOXKITE_REFRESH_TOKENS_ENABLED`), password reset
  (opt-in, `BOXKITE_PASSWORD_RESET_ENABLED`), and email verification
  (opt-in, `BOXKITE_EMAIL_VERIFICATION_ENABLED`) are implemented in
  routers/auth.py — see that module's docstring for the gating rationale
  (issue #79).
- API keys and the three token kinds above are all opaque, high-entropy
  random strings. Only a SHA-256 digest of the full value is ever
  persisted or logged — the raw value is shown to the caller (or, for
  password-reset/email-verification tokens, handed to `EmailSender`)
  exactly once, at creation time. `generate_secure_token` is the one
  shared primitive behind all of them.

MCP OAuth 2.1 (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3) and GitHub/
Google social login (issue #86, the "OAuth / SSO login" follow-up issue
#79 deferred) add their own token kinds below, following the same
`type`-claim-distinguishes-JWTs pattern as `create_access_token`/
`create_preview_token` above, and the same opaque-secret/SHA-256-hash
pattern as `generate_api_key`/`generate_secure_token` for anything
DB-backed.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from .config import settings

_password_hasher = CryptContext(schemes=["argon2"], deprecated="auto")

ACCESS_TOKEN_TYPE = "access"


def hash_password(plain: str) -> str:
    return _password_hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _password_hasher.verify(plain, hashed)
    except Exception:
        # A malformed/foreign stored hash must never raise into the auth
        # path — treat it as a failed verification, not a 500.
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(*, account_id: str, email: str) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    ttl = timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES)
    expires = _now() + ttl
    payload = {
        "sub": account_id,
        "email": email,
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an access token. Raises jwt.PyJWTError on failure."""
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not an access token")
    return payload


PREVIEW_TOKEN_TYPE = "sandbox_preview"


def create_preview_token(*, session_id: str, port: int, ttl_seconds: int) -> tuple[str, datetime, str]:
    """Mint a signed, time-limited token binding one (session_id, port) pair
    for docs/NETWORK-INGRESS-DESIGN.md's preview-URL feature.

    Reuses JWT_SECRET/JWT_ALGORITHM (same signing key as the dashboard access
    token) rather than a separate secret -- both are HMAC-signed, server-only
    secrets with the same blast radius if leaked, and the `type` claim (like
    `create_access_token`'s own ACCESS_TOKEN_TYPE) keeps the two token kinds
    from ever being accepted in place of each other.

    Every token also carries a `jti` (JWT ID) claim -- a fresh, random,
    per-token identifier with no meaning beyond "this exact token" -- so a
    specific minted token can be revoked early without needing to revoke
    every token ever minted for that session/port (see
    `repository.PreviewTokenRevocationRepository` and
    `routers/sandboxes.py`'s `revoke_preview_url`/`proxy_sandbox_preview`).

    Returns (token, expires_at, jti) so the caller can echo the expiry back
    to the API caller without recomputing it, and hand back the jti as the
    handle a caller later revokes by.
    """
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    jti = secrets.token_urlsafe(16)
    payload = {
        "sid": session_id,
        "port": port,
        "type": PREVIEW_TOKEN_TYPE,
        "jti": jti,
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at, jti


def decode_preview_token(token: str) -> dict[str, Any]:
    """Decode and validate a preview token. Raises jwt.PyJWTError on failure
    (expired, malformed, or wrong `type` -- e.g. a dashboard access token
    presented here is rejected, same pattern as decode_access_token)."""
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != PREVIEW_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not a preview token")
    return payload


API_KEY_ROLE_ADMIN = "admin"
API_KEY_ROLE_MEMBER = "member"
VALID_API_KEY_ROLES = (API_KEY_ROLE_ADMIN, API_KEY_ROLE_MEMBER)


def can_initiate_takeover(role: str) -> bool:
    """Gate for who within an account may open `WS /v1/sandboxes/{id}/takeover`
    -- see docs/SANDBOX-OBSERVABILITY-DESIGN.md and SECURITY.md's "Human
    takeover" section for why this was previously unrestricted (any API-key
    holder for the account). Only "admin"-role keys pass; every other value,
    including an unrecognized one, fails closed (returns False) rather than
    defaulting to permissive."""
    return role == API_KEY_ROLE_ADMIN


TAKEOVER_TOKEN_TYPE = "sandbox_takeover"


def create_takeover_token(
    *,
    account_id: str,
    session_id: str,
    ttl_seconds: int,
    read_only: bool = False,
    api_key_id: str | None = None,
) -> tuple[str, datetime]:
    """Mint a short-lived, single-use token scoped to exactly this
    (account, session) pair, for `POST /v1/sandboxes/{id}/takeover-token` --
    see routers/sandboxes.py. Replaces putting the long-lived API key
    itself on a WebSocket URL as `?api_key=...` (SECURITY.md's previously
    disclosed, now-closed, "Known follow-up" for the JS SDK/dashboard).

    `jti` is a fresh random identifier the caller uses to enforce
    single-use redemption (see routers/sandboxes.py's in-process replay
    guard) -- the JWT signature alone only proves the token wasn't forged
    or tampered with; it does not, by itself, prevent reuse within the TTL
    window, so the caller must track `jti` separately.

    `read_only` (GitHub issue #131) is an additive claim, default `False`
    so every existing caller/token shape is unchanged. When `True`,
    `routers/sandboxes.py`'s `_authenticate_takeover_or_close` still
    accepts the WS upgrade and streams server->client PTY output, but the
    takeover proxy must never forward client->PTY input bytes for this
    connection -- an observer can watch a live session without being able
    to type into it.

    `api_key_id` (GitHub issue #132 design doc §5/§9) is a second additive
    claim, default `None`, following the exact same pattern `read_only`
    established: the id of the `ApiKey` row that minted this token (via
    `POST .../takeover-token`'s already-RBAC-checked, API-key-authenticated
    caller), threaded through so `routers/sandboxes.py` can recover *which*
    API key -- not just which account -- authenticated a `?token=`-based
    takeover connection, for the `ExecLogEntry.detail` identity fields.
    There is no `api_key_name` claim on the token itself -- the caller
    re-resolves the name from `api_key_id` via
    `ApiKeyRepository.get_by_id_for_account`, scoped to the token's own
    `account_id`, rather than trusting a caller-suppliable name string on
    the token."""
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    payload = {
        "type": TAKEOVER_TOKEN_TYPE,
        "account_id": account_id,
        "session_id": session_id,
        "read_only": read_only,
        "api_key_id": api_key_id,
        "jti": secrets.token_urlsafe(16),
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


def decode_takeover_token(token: str) -> dict[str, Any]:
    """Decode and validate a takeover token's signature/expiry/type. Raises
    jwt.PyJWTError on failure (expired, malformed, or wrong `type`). Does
    NOT check session binding or single-use consumption -- the caller
    (routers/sandboxes.py's `_authenticate_takeover_or_close`) must compare
    `payload["session_id"]` against the session_id the request itself
    claims to be for, and consume `payload["jti"]` exactly once, the same
    "always verify what the caller passed, never trust the token alone"
    discipline `decode_capability_token`/`decode_preview_token` already
    follow. Also does NOT interpret `payload.get("read_only", False)` or
    `payload.get("api_key_id")` -- the caller must read/resolve those claims
    itself (a token minted before either claim existed has neither key at
    all, so `.get(..., default)` is required, not just a stylistic
    default)."""
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != TAKEOVER_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not a takeover token")
    return payload


DESKTOP_TOKEN_TYPE = "sandbox_desktop"


def create_desktop_token(
    *,
    account_id: str,
    session_id: str,
    ttl_seconds: int,
    api_key_id: str | None = None,
) -> tuple[str, datetime]:
    """Mint a short-lived, single-use token scoped to exactly this
    (account, session) pair, for `POST /v1/sandboxes/{id}/desktop-token` --
    the `WS /desktop` (GitHub issue #184, docs/GUI-COMPUTER-USE-SCOPING.md)
    counterpart to `create_takeover_token` above. Same shape and same
    reasoning (replaces putting a long-lived API key on a WebSocket URL),
    minus the `read_only` claim entirely: there is no view-only concept for
    v1 here -- VNC's RFB stream isn't cleanly splittable into observe/
    control the way a PTY's read/write streams are (view-only VNC needs its
    own x11vnc `-viewonly`-per-connection wiring, explicitly deferred, see
    the scoping doc).

    `jti` is a fresh random identifier for single-use redemption, same
    discipline as `create_takeover_token`'s own docstring describes.

    `api_key_id` is the id of the `ApiKey` row that minted this token, so
    routers/sandboxes.py can recover which key authenticated a `?token=`-
    based desktop connection, mirroring `create_takeover_token`'s own
    `api_key_id` claim (GitHub issue #132 design doc §5/§9)."""
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    payload = {
        "type": DESKTOP_TOKEN_TYPE,
        "account_id": account_id,
        "session_id": session_id,
        "api_key_id": api_key_id,
        "jti": secrets.token_urlsafe(16),
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


def decode_desktop_token(token: str) -> dict[str, Any]:
    """Decode and validate a desktop token's signature/expiry/type. Raises
    jwt.PyJWTError on failure (expired, malformed, or wrong `type`). Does
    NOT check session binding or single-use consumption -- the caller
    (routers/sandboxes.py) must compare `payload["session_id"]` against the
    session_id the request itself claims to be for, and consume
    `payload["jti"]` exactly once, same "never trust the token alone"
    discipline `decode_takeover_token`/`decode_preview_token` already
    follow."""
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != DESKTOP_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not a desktop token")
    return payload


DEMO_SESSION_TOKEN_TYPE = "demo_session"


def create_demo_session_token(*, session_id: str, ttl_seconds: int) -> tuple[str, datetime]:
    """Mint a short-lived token binding exactly one demo `session_id`, for
    `POST /v1/demo/sandboxes` (docs issue #103's public playground). Closes
    the session-hijacking gap a bare, guessable-ish session_id would leave
    open: only the browser holding this token can act on this specific
    session's `/exec`/DELETE routes (routers/demo_playground.py checks both
    the signature and that `payload["sid"]` matches the session_id in the
    URL, same "never trust the token alone" discipline
    `decode_takeover_token`/`decode_preview_token` already follow).

    `ttl_seconds` is always the caller's own already-clamped
    `BOXKITE_DEMO_LIFETIME_MINUTES` window (see routers/demo_playground.py)
    -- a caller cannot request a longer-lived token than the sandbox itself
    will live."""
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    payload = {
        "sid": session_id,
        "type": DEMO_SESSION_TOKEN_TYPE,
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


def decode_demo_session_token(token: str) -> dict[str, Any]:
    """Decode and validate a demo session token's signature/expiry/type.
    Raises jwt.PyJWTError on failure (expired, malformed, or wrong `type`).
    Does NOT check session_id binding -- the caller
    (routers/demo_playground.py) must compare `payload["sid"]` against the
    session_id the request itself claims to be for."""
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != DEMO_SESSION_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not a demo session token")
    return payload


SANDBOX_CREATE_TOKEN_TYPE = "sandbox_create"


def create_sandbox_create_token(*, account_id: str, ttl_seconds: int) -> tuple[str, datetime]:
    """Mint a short-lived, single-use token scoped to exactly this account,
    for `POST /v1/sandboxes` (GitHub issue #221) -- lets the dashboard (a
    JWT-authenticated browser session, `/v1/auth/login`) create a sandbox
    directly from that session instead of requiring the user to paste a
    long-lived API key into the create-sandbox form. Same shape/reasoning
    as `create_takeover_token`/`create_desktop_token` above (a short-lived,
    single-use token in place of exposing a long-lived credential to a
    browser flow that only needs one moment of access), minus any
    session_id binding -- there is no session yet, only the account about
    to create one.

    `jti` is a fresh random identifier for single-use redemption -- the
    caller (`deps.py`'s `get_current_account_via_api_key_or_sandbox_create_token`)
    must consume it exactly once, same "never trust the token alone"
    discipline every other short-lived token type in this module follows.
    """
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    payload = {
        "type": SANDBOX_CREATE_TOKEN_TYPE,
        "account_id": account_id,
        "jti": secrets.token_urlsafe(16),
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


def decode_sandbox_create_token(token: str) -> dict[str, Any]:
    """Decode and validate a sandbox-create token's signature/expiry/type.
    Raises jwt.PyJWTError on failure (expired, malformed, or wrong `type`).
    Does NOT check single-use consumption -- the caller must consume
    `payload["jti"]` exactly once, same discipline `decode_takeover_token`/
    `decode_desktop_token` already document."""
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != SANDBOX_CREATE_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not a sandbox create token")
    return payload


def hash_secret(value: str) -> str:
    """SHA-256 hex digest used for API-key lookups. Never reversible, never logged."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (full_key, prefix, key_hash).

    The full key is returned to the caller exactly once, at creation time,
    and is never stored. `prefix` is safe to display/log (e.g. in a "your
    API keys" list) and safe to use for quick visual identification of a
    leaked credential; it is not itself a usable secret.
    """
    secret_part = secrets.token_urlsafe(32)
    full_key = f"{settings.API_KEY_PREFIX}_{secret_part}"
    prefix = f"{settings.API_KEY_PREFIX}_{secret_part[:8]}"
    return full_key, prefix, hash_secret(full_key)


def looks_like_api_key(value: str | None) -> bool:
    return bool(value) and value.startswith(f"{settings.API_KEY_PREFIX}_")


MCP_ACCESS_TOKEN_TYPE = "mcp_access"


def mcp_resource_identifier(base_url: str) -> str:
    """Canonical RFC 8707 resource identifier this authorization server
    issues MCP access tokens for -- must match exactly (modulo a trailing
    slash) what `/.well-known/oauth-protected-resource` (routers/oauth.py)
    advertises as its own `resource` value, and what `hosted_mcp.py`'s
    protected-resource boundary expects as an access token's `aud` claim.
    `base_url` should already be this deployment's own canonical origin
    (`BOXKITE_PUBLIC_URL` if configured, else the incoming request's own
    origin) -- this function only appends the fixed `/mcp/` resource path."""
    return f"{base_url.rstrip('/')}/mcp/"


def create_mcp_access_token(*, account_id: str, client_id: str, audience: str) -> tuple[str, int]:
    """Mint a short-lived MCP OAuth access token (JWT).

    Self-contained/stateless (no DB row, same tradeoff `create_access_token`
    already accepts) -- `hosted_mcp.py`'s auth middleware tries decoding this
    first before falling back to the API-key DB lookup, so keeping it a
    stateless JWT (rather than a DB-backed opaque token like the refresh
    token below) keeps that hot path cheap. `client_id` is embedded so a
    revoked/deregistered `OAuthClient` can, in principle, be cross-checked
    at verification time even though the access token itself can't be
    individually revoked before expiry -- only the refresh token is
    revocable.

    `audience` (RFC 8707 resource indicator, see `mcp_resource_identifier`)
    is embedded as the standard `aud` claim -- `decode_mcp_access_token`
    requires callers to pass back the audience they expect, so a token
    minted for one resource server can't be replayed against a different
    one that happens to trust the same `JWT_SECRET` (GitHub issue #115).
    """
    ttl = timedelta(minutes=settings.BOXKITE_MCP_ACCESS_TOKEN_TTL_MINUTES)
    expires = _now() + ttl
    payload = {
        "sub": account_id,
        "client_id": client_id,
        "aud": audience,
        "type": MCP_ACCESS_TOKEN_TYPE,
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_mcp_access_token(token: str, *, audience: str) -> dict[str, Any]:
    """Decode and validate an MCP access token. Raises jwt.PyJWTError on
    failure (expired, malformed, wrong `type`, or an `aud` claim that
    doesn't match `audience` -- e.g. a dashboard access token, or an MCP
    access token minted for a different resource server, is rejected here
    exactly like an expired one, same pattern as decode_access_token)."""
    payload = jwt.decode(
        token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM], audience=audience
    )
    if payload.get("type") != MCP_ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not an MCP access token")
    return payload


def generate_oauth_client_id() -> str:
    """Opaque public client identifier for RFC 7591 Dynamic Client
    Registration. Prefixed for the same greppability reason API keys are
    (`bxk_live_...`) -- distinct from `settings.API_KEY_PREFIX` so the two
    credential kinds are never confusable at a glance."""
    return f"mcp_client_{secrets.token_urlsafe(24)}"


def generate_authorization_code() -> str:
    """Opaque, high-entropy, single-use authorization code for
    `GET /oauth/authorize` -> `POST /oauth/token` (RFC 6749 §4.1)."""
    return secrets.token_urlsafe(32)


def generate_refresh_token() -> tuple[str, str]:
    """Return (full_token, token_hash) for a new OAuth refresh token.

    Same entropy/hashing approach as `generate_api_key` -- the raw value is
    returned to the caller exactly once (in the token response) and only
    the SHA-256 hash is ever persisted (`OAuthToken.refresh_token_hash`).
    """
    full_token = secrets.token_urlsafe(32)
    return full_token, hash_secret(full_token)


OAUTH_LOGIN_SESSION_TOKEN_TYPE = "oauth_login_session"


def create_oauth_login_session_token(*, account_id: str) -> tuple[str, int]:
    """Mint the short-lived login-session cookie value for the `/oauth/authorize`
    consent screen (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.4) --
    deliberately NOT the dashboard access token: this is scoped to proving
    "this browser just logged in for the purpose of approving an MCP
    client," set as an `HttpOnly`/`Secure` cookie restricted to `/oauth/*`
    (see routers/oauth.py), and carries no other API privilege even though
    it's signed with the same JWT_SECRET."""
    ttl = timedelta(minutes=settings.BOXKITE_MCP_LOGIN_SESSION_TTL_MINUTES)
    expires = _now() + ttl
    payload = {
        "sub": account_id,
        "type": OAUTH_LOGIN_SESSION_TOKEN_TYPE,
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_oauth_login_session_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != OAUTH_LOGIN_SESSION_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not an oauth login session token")
    return payload


SOCIAL_LOGIN_STATE_TOKEN_TYPE = "social_login_state"
# Public (not a leading-underscore module private): routers/social_login.py
# imports this to size its bound state-nonce cookie's max_age identically to
# the token's own exp, so the cookie never outlives the token it validates.
SOCIAL_LOGIN_STATE_TTL_SECONDS = 600


def create_social_login_state_token(
    *, provider: str, next_path: str | None, link_account_id: str | None = None
) -> tuple[str, str]:
    """Mint the `state` param round-tripped through GitHub/Google's own
    `/authorize` redirect (docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §4).

    A signed, short-lived, server-issued JWT plays the same CSRF-defense
    role the design doc describes as "stored server-side" -- an attacker
    cannot forge a `state` value this server will accept without
    JWT_SECRET, and `exp` bounds how long a captured `state` value could
    ever be replayed, without needing a separate DB table (the same
    "signed, time-limited token in place of a DB row" tradeoff
    `create_preview_token` already makes for a different flow).

    That alone only proves the state was issued by this server -- it says
    nothing about which browser it was issued to. Without a binding to the
    browser that started the flow, a `(code, state)` pair obtained from an
    attacker's own completed login round-trip could be handed to a victim
    as a link; if the victim's browser follows it, the callback would log
    the victim's browser into the *attacker's* account (login CSRF /
    session fixation -- RFC 6749 §10.12). The returned `nonce` closes that
    gap: the caller sets it in a short-lived HttpOnly cookie at `/start`
    time (see `routers/social_login.py`), and the callback rejects the
    request unless the state's embedded nonce matches the cookie the
    *same* browser presents -- an attacker can supply a valid `(code,
    state)` pair, but not the victim's own cookie.

    `link_account_id` is set only when this login was started from
    POST /v1/account/link/{provider}/start (see `create_account_link_intent_token`)
    -- routers/social_login.py's callback checks for it to link this
    provider identity onto that specific already-authenticated account
    instead of running its normal login/auto-register/auto-link
    resolution.
    """
    nonce = secrets.token_urlsafe(16)
    expires = _now() + timedelta(seconds=SOCIAL_LOGIN_STATE_TTL_SECONDS)
    payload = {
        "provider": provider,
        "next": next_path,
        "link_account_id": link_account_id,
        "nonce": nonce,
        "type": SOCIAL_LOGIN_STATE_TOKEN_TYPE,
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, nonce


def decode_social_login_state_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != SOCIAL_LOGIN_STATE_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not a social login state token")
    return payload


ACCOUNT_LINK_INTENT_TOKEN_TYPE = "account_link_intent"
_ACCOUNT_LINK_INTENT_TTL_SECONDS = 600


def create_account_link_intent_token(*, account_id: str, provider: str) -> str:
    """Mint a short-lived, single-purpose token proving "the currently
    logged-in dashboard session for this account asked to link <provider>"
    -- minted via POST /v1/account/link/{provider}/start (a normal
    Authorization: Bearer <dashboard JWT> request), then passed as
    ?link_token= on the browser's top-level navigation to
    GET /v1/auth/{provider}/start, since a redirect can't carry that
    endpoint's own Authorization header.

    Deliberately its own narrow-purpose token rather than reusing the
    dashboard access token itself as that query param: even if this value
    leaks via browser history or a proxy access log, it can only complete
    one specific link action for one specific account, within its short
    TTL -- not general API access the way the real access token would
    grant."""
    expires = _now() + timedelta(seconds=_ACCOUNT_LINK_INTENT_TTL_SECONDS)
    payload = {
        "sub": account_id,
        "provider": provider,
        "type": ACCOUNT_LINK_INTENT_TOKEN_TYPE,
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_account_link_intent_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != ACCOUNT_LINK_INTENT_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not an account link intent token")
    return payload


ENTERPRISE_SSO_STATE_TOKEN_TYPE = "enterprise_sso_state"


def create_enterprise_sso_state_token(*, connection: str, next_path: str | None) -> str:
    """Mint the `state` param round-tripped through the hosted SSO broker's
    own authorization redirect (docs/ENTERPRISE-SSO-DESIGN.md §4) -- same
    signed-JWT-in-place-of-a-server-side-store CSRF defense
    `create_social_login_state_token` already uses, kept as its own
    function/type constant since the two flows are otherwise independent
    and shouldn't be able to be replayed against each other."""
    expires = _now() + timedelta(seconds=settings.ENTERPRISE_SSO_STATE_TTL_SECONDS)
    payload = {
        "connection": connection,
        "next": next_path,
        "type": ENTERPRISE_SSO_STATE_TOKEN_TYPE,
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_enterprise_sso_state_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != ENTERPRISE_SSO_STATE_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not an enterprise sso state token")
    return payload


def verify_pkce_challenge(*, code_verifier: str, code_challenge: str) -> bool:
    """RFC 7636 S256 PKCE verification: BASE64URL-ENCODE(SHA256(code_verifier))
    (no padding) must match the `code_challenge` stored at authorization
    time. `code_challenge_method=plain` is never accepted anywhere in this
    codebase -- see docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.1."""
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return secrets.compare_digest(computed, code_challenge)


def generate_secure_token() -> tuple[str, str]:
    """Return (raw_token, token_hash) for a one-shot, single-use credential
    (refresh token, password-reset token, email-verification token).

    Same shape as `generate_api_key`'s (full_key, ..., key_hash) pair,
    minus the display-prefix concept (these tokens are never listed in a
    UI the way API keys are) -- 32 bytes of `secrets.token_urlsafe`
    entropy, SHA-256-hashed for storage. The raw value is the caller's
    responsibility to deliver exactly once (a redirect URL, an email body,
    a JSON response body) and never persist or log.

    `generate_refresh_token` above is functionally identical (same
    entropy/hash shape) but kept as its own function since it backs a
    conceptually distinct credential (an MCP OAuth refresh token, issue
    #86) from the three dashboard-auth tokens this one backs (issue #79)
    -- not worth collapsing into one shared name across two unrelated
    features landing at the same time.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_secret(raw)
