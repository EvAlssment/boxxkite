"""FastAPI dependencies: DB session, JWT-authenticated user (dashboard
routes), and API-key-authenticated account (sandbox-management routes).

The two credential types are intentionally never interchangeable:
- `/v1/api-keys/*` (managing keys) requires a user JWT from /v1/auth/login.
- `/v1/sandboxes/*` (using boxkite) requires a long-lived API key.
A JWT presented to a sandbox route is rejected, and vice versa — see
`get_current_account_via_api_key` and `get_current_user`. The one
deliberate, narrow exception is `POST /v1/sandboxes` itself (GitHub issue
#221), which also accepts a short-lived, single-use `sandbox_create` token
minted from a dashboard JWT session — see
`get_current_account_via_api_key_or_sandbox_create_token` below.
"""

from __future__ import annotations

import time

import jwt
from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from boxkite import get_sandbox_manager

from .db import get_db
from .email_sender import EmailSender, get_email_sender
from .errors import ApiError
from .image_builder import FakeImageBuildRunner, ImageBuildRunner, KanikoJobBuildRunner
from .volume_builder import FakeVolumeProvisioner, K8sVolumeProvisioner, VolumeProvisioner
from .models_orm import Account, ApiKey
from .repository import (
    AccountRepository,
    AdminAccessLogRepository,
    ApiKeyRepository,
    McpConnectionRepository,
    SandboxSessionRepository,
    SecretRepository,
)
from .security import decode_access_token, decode_sandbox_create_token, hash_secret, looks_like_api_key
from .storage_client import SnapshotStorageClient, get_snapshot_storage_client
from .usage_policy import UsagePolicy

_image_build_runner: ImageBuildRunner | None = None
_volume_provisioner: VolumeProvisioner | None = None


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "missing_credentials", "Missing or malformed Authorization header")
    token = authorization[len("bearer "):].strip()
    if not token:
        raise ApiError(401, "missing_credentials", "Missing or malformed Authorization header")
    return token


def _reject_if_scim_deactivated(account: Account) -> None:
    """The actual enforcement point for SCIM (Directory Sync,
    routers/scim.py) deactivation: called from every path that resolves an
    already-issued credential (API key or dashboard JWT) back to an
    Account, so deactivation takes effect on the VERY NEXT authenticated
    request -- not just at a future login attempt (routers/auth.py's
    `_reject_if_scim_deactivated`/routers/social_login.py's `_finish_login`
    additionally check at credential-issuance time, but this is the check
    that actually revokes access for a credential minted before
    deactivation happened). See models_orm.py's
    Account.scim_deactivated_at docstring."""
    if account.scim_deactivated_at is not None:
        raise ApiError(401, "account_deactivated", "This account has been deactivated")


async def _resolve_account_and_key_by_api_key_token(token: str, db: AsyncSession) -> tuple[Account, ApiKey]:
    """Shared lookup once a raw API-key token string is in hand, regardless
    of whether it came from an Authorization header or (for the two
    browser-native streaming routes that can't set custom headers --
    EventSource and WebSocket) an `api_key` query parameter. See
    `get_current_account_via_api_key` (header only, every normal REST
    route), `get_current_account_via_api_key_or_query` (header or query,
    /watch only), and `get_current_account_and_key_via_api_key` (header
    only, but also returns the ApiKey row for routes that must check its
    `role` -- e.g. POST .../takeover-token) below."""
    if not looks_like_api_key(token):
        raise ApiError(401, "wrong_credential_type", "This endpoint requires an API key, not a user session token")

    api_keys = ApiKeyRepository(db)
    key_row = await api_keys.get_active_by_hash(hash_secret(token))
    if key_row is None:
        raise ApiError(401, "invalid_api_key", "API key is invalid or has been revoked")
    await api_keys.touch_last_used(key_row.id)

    accounts = AccountRepository(db)
    account = await accounts.get_by_id(key_row.account_id)
    if account is None:
        raise ApiError(401, "invalid_api_key", "Account for this API key no longer exists")
    _reject_if_scim_deactivated(account)
    return account, key_row


async def _resolve_account_by_api_key_token(token: str, db: AsyncSession) -> Account:
    """Convenience wrapper over `_resolve_account_and_key_by_api_key_token`
    for the majority of callers that only need the account, not the
    specific key's role."""
    account, _key_row = await _resolve_account_and_key_by_api_key_token(token, db)
    return account


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Resolve the caller from a dashboard JWT (`/v1/auth/login`). Rejects
    API keys outright so the two credential types can never be swapped."""
    token = _extract_bearer_token(authorization)
    if looks_like_api_key(token):
        raise ApiError(401, "wrong_credential_type", "This endpoint requires a user session token, not an API key")
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token", "Invalid or expired token") from None

    accounts = AccountRepository(db)
    account = await accounts.get_by_id(str(payload.get("sub", "")))
    if account is None:
        raise ApiError(401, "invalid_token", "Account no longer exists")
    _reject_if_scim_deactivated(account)
    return account


async def get_current_account_via_api_key(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Resolve the caller from a long-lived API key
    (`Authorization: Bearer bxk_live_...`). This is the ONLY credential type
    accepted by /v1/sandboxes/* — a dashboard JWT is rejected here too, for
    the same reason in reverse."""
    token = _extract_bearer_token(authorization)
    return await _resolve_account_by_api_key_token(token, db)


async def get_current_admin_account(
    request: Request,
    account: Account = Depends(get_current_account_via_api_key),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Same API-key credential as `get_current_account_via_api_key`, plus an
    `Account.is_admin` check -- gates every `/v1/admin/*` route
    (docs/ADMIN-ROLE-DESIGN.md). A non-admin, otherwise-valid API key gets a
    403, not a 404 -- unlike cross-tenant resource lookups elsewhere in this
    codebase, there is nothing to hide about whether admin routes exist; the
    thing being protected is the cross-account DATA those routes return, not
    their existence.

    Every call durably logs to `AdminAccessLog` (`AdminAccessLogRepository`)
    BEFORE returning, independent of whatever the handler itself does next
    -- cross-account visibility is new, sensitive surface, and the
    accountability story is "every access is logged," not "trust the
    handler to log it."
    """
    if not account.is_admin:
        raise ApiError(403, "admin_required", "This endpoint requires an admin account")
    await AdminAccessLogRepository(db).record(admin_account_id=account.id, endpoint=request.url.path)
    return account


async def get_current_account_and_key_via_api_key(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> tuple[Account, ApiKey]:
    """Same credential and header-only behavior as
    `get_current_account_via_api_key`, but also returns the ApiKey row
    itself -- needed by routes that must check the *specific key's*
    `role`, not just which account it belongs to (currently only
    `POST /v1/sandboxes/{id}/takeover-token` -- see
    security.py's `can_initiate_takeover`)."""
    token = _extract_bearer_token(authorization)
    return await _resolve_account_and_key_by_api_key_token(token, db)


async def get_current_account_via_api_key_or_query(
    authorization: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Same credential (a long-lived API key) as `get_current_account_via_api_key`,
    but also accepts it as an `api_key` query parameter when no Authorization
    header is present. Exists ONLY for `/watch` (EventSource) and `/takeover`
    (WebSocket) -- the two browser-native APIs that cannot set a custom
    Authorization header at all, unlike `fetch`. Every normal REST route
    keeps using the header-only dependency above; do not switch a route to
    this one unless it has that same browser-header limitation, since a
    query-string credential is more exposed (access logs, browser history)
    than a header one -- see docs/SANDBOX-OBSERVABILITY-DESIGN.md and
    SECURITY.md's "Known follow-ups" entry for this exact tradeoff."""
    if authorization:
        token = _extract_bearer_token(authorization)
    elif api_key:
        token = api_key
    else:
        raise ApiError(401, "missing_credentials", "Missing Authorization header or api_key query parameter")
    return await _resolve_account_by_api_key_token(token, db)


# In-process single-use guard for sandbox-create tokens (security.py's
# create_sandbox_create_token/decode_sandbox_create_token) -- same
# per-replica jti-replay-guard shape as routers/sandboxes.py's
# _takeover_jti_seen/_desktop_jti_seen, kept here instead of there since
# this token is consumed inside a FastAPI dependency rather than manually
# inside a route function, and deps.py cannot import from routers/sandboxes.py
# without a circular import (sandboxes.py already imports this module).
# Same documented limitation: this state is per-process, not shared across
# replicas of a multi-instance deployment, bounded either way by the
# token's short TTL (BOXKITE_SANDBOX_CREATE_TOKEN_TTL_SECONDS).
_sandbox_create_jti_seen: dict[str, float] = {}


def _consume_sandbox_create_jti(jti: str, *, exp: float | int | None) -> bool:
    """Returns True the first time this jti is seen (and records it), False
    on any repeat -- i.e. single-use enforcement. Also opportunistically
    prunes entries past their own expiry, same shape as
    routers/sandboxes.py's _consume_takeover_jti/_consume_desktop_jti."""
    now = time.time()
    expired = [seen_jti for seen_jti, seen_exp in _sandbox_create_jti_seen.items() if seen_exp <= now]
    for seen_jti in expired:
        del _sandbox_create_jti_seen[seen_jti]
    if jti in _sandbox_create_jti_seen:
        return False
    _sandbox_create_jti_seen[jti] = float(exp) if exp else now + 60
    return True


def reset_sandbox_create_jti_guard_for_tests() -> None:
    """Test-only helper to avoid cross-test bleed of the in-memory replay guard."""
    _sandbox_create_jti_seen.clear()


async def get_current_account_via_api_key_or_sandbox_create_token(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Same long-lived API key as `get_current_account_via_api_key`, but ALSO
    accepts the short-lived, single-use `sandbox_create` token minted by
    `POST /v1/account/sandbox-create-token` (a dashboard-JWT-authenticated
    route) -- the one deliberate, narrow exception to this module's
    docstring's "two credential types are never interchangeable" rule.
    Exists ONLY for `POST /v1/sandboxes` (GitHub issue #221): the
    dashboard's create-sandbox form can mint one of these from the user's
    already-logged-in session and use it immediately, instead of requiring
    the user to paste a long-lived API key into browser JS. Every other
    `/v1/sandboxes/*` route keeps using `get_current_account_via_api_key`
    unchanged -- this widening is scoped to creation only."""
    token = _extract_bearer_token(authorization)
    if looks_like_api_key(token):
        return await _resolve_account_by_api_key_token(token, db)

    try:
        payload = decode_sandbox_create_token(token)
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token", "Invalid or expired token") from None

    jti = payload.get("jti")
    if not jti or not _consume_sandbox_create_jti(jti, exp=payload.get("exp")):
        raise ApiError(401, "invalid_token", "This token has already been used")

    accounts = AccountRepository(db)
    account = await accounts.get_by_id(str(payload.get("account_id", "")))
    if account is None:
        raise ApiError(401, "invalid_token", "Account no longer exists")
    _reject_if_scim_deactivated(account)
    return account


def get_manager():
    """Overridable in tests via `app.dependency_overrides[get_manager]`."""
    return get_sandbox_manager()


async def get_usage_policy(
    db: AsyncSession = Depends(get_db),
    manager=Depends(get_manager),
) -> UsagePolicy:
    return UsagePolicy(
        manager, SandboxSessionRepository(db), SecretRepository(db), McpConnectionRepository(db)
    )


def get_snapshot_storage() -> SnapshotStorageClient:
    """Overridable in tests via `app.dependency_overrides[get_snapshot_storage]`
    -- see storage_client.py's module docstring for why this is a distinct
    credential/client from `get_manager`'s SandboxManager."""
    return get_snapshot_storage_client()


def get_email_sender_dep() -> EmailSender:
    """Overridable in tests via `app.dependency_overrides[get_email_sender_dep]`
    -- see email_sender.py's module docstring for the default
    (non-production) LoggingEmailSender this resolves to otherwise."""
    return get_email_sender()


def get_image_build_runner() -> ImageBuildRunner:
    """Overridable in tests via `app.dependency_overrides[get_image_build_runner]`.

    RUNTIME_MODE=k8s gets the real, isolated Kaniko-Job runner
    (image_builder.py's module docstring has the full isolation model);
    every other mode (compose/local-dev, or tests) gets the deterministic
    in-process fake, since there is no cluster to run a real builder Job
    against -- mirrors main.py's own os.environ.get("RUNTIME_MODE") check
    for WarmPoolManager."""
    global _image_build_runner
    if _image_build_runner is None:
        import os

        if os.environ.get("RUNTIME_MODE") == "k8s":
            _image_build_runner = KanikoJobBuildRunner()
        else:
            _image_build_runner = FakeImageBuildRunner()
    return _image_build_runner


def get_volume_provisioner() -> VolumeProvisioner:
    """Overridable in tests via `app.dependency_overrides[get_volume_provisioner]`.
    Same RUNTIME_MODE split as get_image_build_runner -- RUNTIME_MODE=k8s
    gets the real K8sVolumeProvisioner (implemented against a real
    CoreV1Api, see volume_builder.py, though never exercised against a
    LIVE cluster in this repo's own test suite); every other mode
    (compose/local-dev, or tests) gets the deterministic in-process fake,
    since there is no cluster to provision a real PVC against."""
    global _volume_provisioner
    if _volume_provisioner is None:
        import os

        if os.environ.get("RUNTIME_MODE") == "k8s":
            _volume_provisioner = K8sVolumeProvisioner()
        else:
            _volume_provisioner = FakeVolumeProvisioner()
    return _volume_provisioner
