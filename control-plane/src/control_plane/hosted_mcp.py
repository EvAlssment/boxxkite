"""Hosted, remote MCP server — docs/HOSTED-MCP-DESIGN.md, closing GitHub
issue #85.

Unlike `mcp-server/` (published as `boxkite-mcp` on PyPI), which is a
*local*, `stdio`-transport MCP server an MCP client spawns as a subprocess
on the user's own machine, this module mounts a *remote* MCP endpoint
(`GET/POST /mcp`, Streamable HTTP transport) directly on the control-plane
itself — an MCP client just adds a URL + a bearer token to its config, no
local install. See the design doc for why this uses a static bearer token
(the same long-lived API keys every other `/v1/*` route already accepts)
rather than FastMCP's built-in OAuth-oriented `token_verifier`/`AuthSettings`
hooks, and for why tool implementations call `SandboxManager`/`UsagePolicy`/
the repositories directly instead of `boxkite_client`'s HTTP wrapper (that
would be a pointless self-loopback from inside the same process).

Tool names and parameter shapes intentionally match `boxkite-mcp`'s
existing tool set (`mcp-server/src/boxkite_mcp/server.py`) so the same
agent prompt/tool-calling behavior works against either transport — the
two share no code, since they run against genuinely different starting
objects (an HTTP client vs. an in-process manager).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from uuid import uuid4

import jwt
from boxkite import get_sandbox_manager
from boxkite.command_whitelist import validate_command_whitelist
from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request as _StarletteRequest

from . import db as db_module
from .config import settings
from .deps import _reject_if_scim_deactivated, _resolve_account_by_api_key_token, get_image_build_runner, get_volume_provisioner
from .errors import ApiError
from .image_builder import cache_key_for, cache_window_start
from .models_orm import Account
from .rate_limit import enforce_rate_limit
from .repository import (
    AccountRepository,
    SandboxImageRepository,
    SandboxSessionRepository,
    SandboxVolumeRepository,
    SecretRepository,
)
from .routers.images import _run_build_in_background
from .routers.sandboxes import _resolve_image_ref_or_404, _resolve_volume_mounts_or_404
from .routers.volumes import _provision_in_background
from .schemas import SandboxImageBuildRequest, VolumeCreateRequest
from .security import decode_mcp_access_token, mcp_resource_identifier
from .usage_policy import UsagePolicy

logger = logging.getLogger(__name__)

# Mirrors SandboxCreateRequest.count's `Field(ge=1, le=10)` in schemas.py --
# a single hosted MCP call must not be able to request unbounded sandboxes.
_MAX_SANDBOX_COUNT_PER_CALL = 10

_current_account: contextvars.ContextVar[Account] = contextvars.ContextVar(
    "hosted_mcp_current_account"
)


def get_manager():
    """Same override point as `deps.get_manager` -- but MCP tool dispatch
    doesn't go through FastAPI's dependency-injection system at all
    (FastMCP has no equivalent to `Depends()`), so `app.dependency_overrides`
    has no effect on tool calls in this module. Tests override this
    directly instead: `monkeypatch.setattr(hosted_mcp, "get_manager", lambda:
    fake_manager)`. Tools call this by name (not a bound reference captured
    at registration time) so a monkeypatch takes effect on every subsequent
    call, the same pattern `sidecar/main.py`'s tests already rely on for
    functions tests monkeypatch directly on the module."""
    return get_sandbox_manager()


def _get_image_build_runner():
    """Same override point / by-name-call rationale as `get_manager` above,
    for the image/volume tools' background provisioning dispatch."""
    return get_image_build_runner()


def _get_volume_provisioner():
    """Same override point / by-name-call rationale as `get_manager` above."""
    return get_volume_provisioner()


def _current_account_or_raise() -> Account:
    try:
        return _current_account.get()
    except LookupError:  # pragma: no cover - middleware always sets this first
        raise RuntimeError(
            "hosted_mcp: no authenticated account in context -- "
            "BearerTokenAuthMiddleware must run before any tool call reaches here"
        ) from None


def _extract_bearer_token(raw_authorization_header: str) -> str | None:
    if not raw_authorization_header.lower().startswith("bearer "):
        return None
    token = raw_authorization_header[len("bearer "):].strip()
    return token or None


def _expected_mcp_audience(scope) -> str:
    """This deployment's own RFC 8707 resource identifier for `/mcp/` --
    the audience an MCP access token's `aud` claim must match to be
    accepted here (GitHub issue #115). Mirrors `routers/oauth.py`'s
    `_expected_resource`/`_base_url`: prefers `BOXKITE_PUBLIC_URL` when
    configured (the stable, operator-set identity a real deployment
    should have), else falls back to the incoming request's own origin --
    consistent with what `/oauth/token` embedded as `aud` when it minted
    the token, since both endpoints live on the same deployment."""
    if settings.BOXKITE_PUBLIC_URL:
        base = settings.BOXKITE_PUBLIC_URL.rstrip("/")
    else:
        base = str(_StarletteRequest(scope).base_url).rstrip("/")
    return mcp_resource_identifier(base)


async def _resolve_account_for_bearer_token(token: str, db, *, expected_audience: str) -> Account:
    """Try the token as an MCP OAuth access token (JWT, `routers/oauth.py`'s
    token endpoint) first -- cheap, no DB hit -- then fall back to the
    existing API-key DB lookup. Both paths resolve to the same `Account`
    object, so no tool handler in this module needs to know which
    credential kind authenticated a given request. See
    docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.5 -- a deployment that
    never enables MCP OAuth or registers any client keeps authenticating
    with a static API key exactly as it does today, since a non-JWT bearer
    token simply fails `decode_mcp_access_token` and falls through.

    `decode_mcp_access_token` also enforces the RFC 8707 audience check
    against `expected_audience` -- a token minted for a different resource
    server fails here (wrong `aud`) exactly like an expired or malformed
    one, and falls through to the API-key lookup, which then correctly
    rejects it as an unrecognized credential (GitHub issue #115).

    Checks `_reject_if_scim_deactivated` here too, mirroring the API-key
    fallback branch's own check inside `_resolve_account_by_api_key_token`
    -- an MCP access token is a bearer credential minted once at
    `/oauth/token` (`BOXKITE_MCP_ACCESS_TOKEN_TTL_MINUTES`, default 15) and
    then presented on every `/mcp` tool call afterward; without this, a
    token minted before deactivation would keep authenticating every
    subsequent tool call for its full remaining lifetime, the exact
    already-issued-credential TOCTOU gap `deps.py`'s docstring on that
    helper describes."""
    try:
        payload = decode_mcp_access_token(token, audience=expected_audience)
    except jwt.PyJWTError:
        return await _resolve_account_by_api_key_token(token, db)

    account = await AccountRepository(db).get_by_id(str(payload.get("sub", "")))
    if account is None:
        raise ApiError(401, "invalid_token", "Account for this token no longer exists")
    _reject_if_scim_deactivated(account)
    return account


async def _send_json_error(send, status_code: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerTokenAuthMiddleware:
    """Pure ASGI middleware gating the mounted `/mcp` app: every request must
    carry `Authorization: Bearer <token>`, resolved to an `Account` via
    `_resolve_account_for_bearer_token` -- either a static API key
    (`bxk_live_...`, the exact same DB-backed lookup every REST route
    already uses) or, once `BOXKITE_MCP_OAUTH_ENABLED` is on, an MCP OAuth
    access token minted by `routers/oauth.py`'s token endpoint (see
    docs/MCP-OAUTH-AND-SOCIAL-LOGIN-DESIGN.md §3.5).

    Sets the resolved `Account` into `_current_account` for the duration of
    the request. Safe because a single ASGI request is handled in one
    asyncio task: the contextvar set here is visible to every tool handler
    `await`ed underneath this middleware, without needing the MCP SDK to
    support threading extra context through its own tool-dispatch machinery
    (it doesn't have a first-class hook for that).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_auth = headers.get(b"authorization", b"").decode("latin-1")
        token = _extract_bearer_token(raw_auth)
        if token is None:
            await _send_json_error(
                send, 401, "missing_credentials", "Missing or malformed Authorization header"
            )
            return

        expected_audience = _expected_mcp_audience(scope)
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            try:
                account = await _resolve_account_for_bearer_token(token, db, expected_audience=expected_audience)
            except ApiError as e:
                await _send_json_error(send, e.status_code, e.code, e.message)
                return

        reset_token = _current_account.set(account)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_account.reset(reset_token)


def _describe_exception(action: str, exc: Exception) -> str:
    """Never lets a raw traceback reach an MCP client -- same posture as
    `routers/sandboxes.py`'s `_sandbox_operation_error` for the REST API,
    and `mcp-server`'s own `_describe_api_error`/`_describe_connection_error`."""
    return f"Error {action}: {exc}"


async def _enforce_mcp_rate_limit(*, bucket: str, account: Account, limit: int) -> str | None:
    """MCP-tool equivalent of the REST routers' `_enforce_image_build_rate_limit`/
    `_enforce_volume_rate_limit` -- deliberately reuses the SAME bucket names
    (`image_build_ops`/`volume_ops`) and subject (`account.id`) those routers
    pass to `enforce_rate_limit`, so an account's REST and MCP calls share one
    limit rather than a caller doubling their effective rate by splitting
    requests across both transports. `enforce_rate_limit` only dereferences
    its `request` param when `subject` is None (see rate_limit.py's
    `_client_key`) -- since every caller here always has an authenticated
    `account`, `request=None` is safe. Returns None if the call is allowed,
    or a plain message (this file's no-raw-exception convention for tools)
    if the caller is rate-limited."""
    try:
        await enforce_rate_limit(None, bucket=bucket, subject=str(account.id), limit=limit, response=None)
    except HTTPException as e:
        detail = e.detail
        message = (
            detail.get("error", {}).get("message")
            if isinstance(detail, dict)
            else "Too many requests. Please wait a moment and try again."
        )
        return message or "Too many requests. Please wait a moment and try again."
    return None


def build_hosted_mcp() -> FastMCP:
    """Builds the FastMCP server and registers every tool. Split out from
    wherever it's mounted so tests can build one without a real ASGI
    server. Callers must still call `.streamable_http_app()` once (this
    lazily creates the SDK's `StreamableHTTPSessionManager`) and enter
    `.session_manager.run()` in the parent app's own lifespan -- see
    `main.py`."""

    mcp = FastMCP(
        name="boxkite",
        instructions=(
            "Tools for creating, using, and destroying boxkite sandboxes -- "
            "isolated, Kubernetes-backed environments for running shell "
            "commands and editing files. Call create_sandbox first to get a "
            "session_id, pass that session_id to exec/file_create/view/"
            "str_replace/ls/glob/grep, and call destroy_sandbox when done. "
            "Use create_sandbox_image/get_sandbox_image/list_sandbox_images/"
            "delete_sandbox_image to build a custom sandbox image with extra "
            "packages baked in, then pass its id as create_sandbox's "
            "image_id. Use create_sandbox_volume/get_sandbox_volume/"
            "list_sandbox_volumes/delete_sandbox_volume to create "
            "independent, persistent storage volumes, then pass a "
            "{volume_id: mount_path} mapping as create_sandbox's "
            "volume_mounts to mount them into a sandbox."
        ),
        streamable_http_path="/",
        # FastMCP's default DNS-rebinding protection only allow-lists
        # localhost/127.0.0.1 Host headers -- meant for a locally-run dev
        # server reachable from a browser, not this control-plane, which is
        # served over HTTPS behind a real hostname in production (same
        # trust model every other `/v1/*` route already has, with no
        # comparable Host-header allowlist). The actual security boundary
        # here is `BearerTokenAuthMiddleware`'s API-key check, not Host
        # header matching -- disable this check rather than have it 421 in
        # production for the one route that happens to add it.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def create_sandbox(
        label: str | None = None,
        size: str | None = None,
        storage_gb: float | None = None,
        lifetime_minutes: int | None = None,
        count: int | None = None,
        image_id: str | None = None,
        volume_mounts: dict[str, str] | None = None,
        secret_names: list[str] | None = None,
    ) -> str:
        """Create one or more new boxkite sandboxes and return each one's
        session id and status. Call this before any other sandbox tool --
        every other tool needs the session_id this returns.

        size: sandbox size, one of "small", "medium", or "large". Defaults to
            "small" when omitted.
        storage_gb: size of the sandbox's persistent workspace volume, in
            gigabytes. Defaults to the control-plane's default when omitted.
        lifetime_minutes: how many minutes the sandbox may run before it is
            automatically destroyed. Defaults to the control-plane's default
            lifetime when omitted.
        count: how many identical sandboxes to create in this call, from 1
            to 10. Defaults to a single sandbox when omitted. Each is
            created and limit-checked one at a time -- a later item can
            still fail a capacity limit even if earlier ones succeeded.
        image_id: id of a completed custom image built via
            create_sandbox_image -- starts the sandbox from that digest-
            pinned image instead of the operator's default. Must belong to
            this account and be status "completed", or this errors.
        volume_mounts: optional {volume_id: mount_path} mapping of
            independent storage volumes created via create_sandbox_volume to
            mount into this sandbox. Every volume_id must already belong to
            this account and be status "ready", or this errors.
        secret_names: names of this account's secrets (see the secrets API)
            this session should be granted access to via the sidecar's
            http-request secrets broker. Every name must already exist for
            this account.
        """
        requested_count = count if count is not None else 1
        if not (1 <= requested_count <= _MAX_SANDBOX_COUNT_PER_CALL):
            return f"count must be between 1 and {_MAX_SANDBOX_COUNT_PER_CALL} (got {requested_count})"

        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            try:
                image_ref = await _resolve_image_ref_or_404(image_id=image_id, account=account, db=db)
                resolved_volume_mounts = await _resolve_volume_mounts_or_404(
                    volume_mounts=volume_mounts, account=account, db=db
                )
            except ApiError as e:
                return _describe_exception("resolving sandbox create request", e)

            policy = UsagePolicy(manager, SandboxSessionRepository(db), SecretRepository(db))
            summaries: list[str] = []
            for _ in range(requested_count):
                try:
                    row, _manager_result = await policy.create_session(
                        account,
                        label=label,
                        size=size or "small",
                        storage_gb=storage_gb,
                        lifetime_minutes=lifetime_minutes,
                        secret_names=secret_names,
                        image_ref=image_ref,
                        volume_mounts=resolved_volume_mounts,
                    )
                except ApiError as e:
                    summaries.append(_describe_exception("creating sandbox", e))
                    break
                summaries.append(f"Created sandbox {row.id} (status: active)")
        return "\n".join(summaries)

    @mcp.tool()
    async def destroy_sandbox(session_id: str) -> str:
        """Tear down a boxkite sandbox by session id. Always call this when
        you're done with a sandbox to free the resource."""
        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            row = await SandboxSessionRepository(db).get_for_account(
                session_id=session_id, account_id=account.id
            )
            if row is None or row.destroyed_at is not None:
                return f"Sandbox {session_id} not found"
            policy = UsagePolicy(manager, SandboxSessionRepository(db), SecretRepository(db))
            try:
                await policy.destroy_session(row, reason="mcp_caller_requested")
            except Exception as e:
                return _describe_exception(f"destroying sandbox {session_id}", e)
        return f"Destroyed sandbox {session_id}"

    @mcp.tool()
    async def get_sandbox(session_id: str) -> str:
        """Look up a single boxkite sandbox's current status by session id."""
        account = _current_account_or_raise()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            row = await SandboxSessionRepository(db).get_for_account(
                session_id=session_id, account_id=account.id
            )
        if row is None:
            return f"Sandbox {session_id} not found"
        status = "destroyed" if row.destroyed_at else "active"
        return f"{row.id} (status: {status}, label: {row.label or '(none)'})"

    @mcp.tool()
    async def list_sandboxes(active_only: bool = False) -> str:
        """List sandboxes on this account. Set active_only=true to only see
        sandboxes that are still running."""
        account = _current_account_or_raise()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            rows = await SandboxSessionRepository(db).list_for_account(
                account_id=account.id, active_only=active_only
            )
        if not rows:
            return "No sandboxes found."
        lines = [
            f"- {row.id} (status: {'destroyed' if row.destroyed_at else 'active'}, "
            f"label: {row.label or '(none)'})"
            for row in rows
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def create_sandbox_image(
        label: str | None = None,
        base: str = "boxkite-default",
        python_packages: list[str] | None = None,
        apt_packages: list[str] | None = None,
        npm_packages: list[str] | None = None,
    ) -> str:
        """Start building a custom sandbox image with extra packages baked
        in. Returns the new image's id and status -- poll
        get_sandbox_image(id) until status is "completed", then pass
        image_id=id to create_sandbox to start a sandbox from it.

        base: base image to build from, one of "boxkite-default" (the
            operator's standard sandbox image), "boxkite-minimal" (a smaller
            base with fewer preinstalled tools), "boxkite-node" (drops
            Python entirely, for pure JS/TS workloads -- only
            apt_packages/npm_packages are installable), "boxkite-go" (drops
            both Python and Node entirely, for pure Go workloads -- only
            apt_packages are installable), "boxkite-nextjs" (same
            Node-only runtime as "boxkite-node", plus a pre-installed
            Next.js App Router starter vendored at /opt/nextjs-template --
            only apt_packages/npm_packages are installable), or "boxkite-rust"
            (also drops both Python and Node entirely, for pure Rust
            workloads -- only apt_packages are installable). Defaults to
            "boxkite-default".
        python_packages: packages to pip-install into the image. Each entry
            must be exact-version-pinned ("name==version") -- version ranges
            or bare names are rejected. Not supported on base="boxkite-node",
            base="boxkite-nextjs", base="boxkite-go", or base="boxkite-rust".
        apt_packages: packages to apt-install into the image. Same
            exact-version-pinning rule as python_packages.
        npm_packages: packages to npm-install into the image. Same
            exact-version-pinning rule as python_packages. Not supported on
            base="boxkite-go" or base="boxkite-rust".
        """
        if not settings.BOXKITE_IMAGE_BUILDER_ENABLED:
            return "Custom sandbox images are not enabled on this deployment."

        account = _current_account_or_raise()
        rate_limit_message = await _enforce_mcp_rate_limit(
            bucket="image_build_ops", account=account, limit=settings.BOXKITE_IMAGE_BUILD_RATE_LIMIT_PER_MINUTE
        )
        if rate_limit_message:
            return rate_limit_message

        try:
            body = SandboxImageBuildRequest(
                label=label,
                base=base,
                python_packages=python_packages or [],
                apt_packages=apt_packages or [],
                npm_packages=npm_packages or [],
            )
        except ValidationError as e:
            return _describe_exception("validating sandbox image request", e)

        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            images = SandboxImageRepository(db)
            active_count = await images.count_active_for_account(account.id)
            if active_count >= settings.BOXKITE_MAX_IMAGES_PER_ACCOUNT:
                return (
                    f"Custom image limit reached ({settings.BOXKITE_MAX_IMAGES_PER_ACCOUNT} at a "
                    "time). Delete an existing image before building another."
                )
            in_flight_total = await images.count_in_flight_total()
            if in_flight_total >= settings.BOXKITE_GLOBAL_MAX_CONCURRENT_IMAGE_BUILDS:
                return (
                    "This deployment's cluster-wide concurrent build capacity is in use. "
                    "Try again shortly."
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
            if cached is not None:
                # Same 24h build-cache reuse as routers/images.py's
                # create_image_build -- avoids re-running an identical
                # build+scan for this account within the cache window.
                await images.mark_completed(
                    image_id=image_id,
                    digest=cached.digest,
                    registry_ref=cached.registry_ref,
                    scan_result=cached.scan_result or {},
                )
                return f"Started building image {image_id} (status: completed, reused a cached build)."

        asyncio.create_task(
            _run_build_in_background(
                runner=_get_image_build_runner(),
                image_id=image_id,
                account_id=account.id,
                base=body.base,
                python_packages=body.python_packages,
                apt_packages=body.apt_packages,
                npm_packages=body.npm_packages,
            )
        )
        return (
            f"Started building image {image_id} (status: queued). Poll get_sandbox_image "
            'with this id until status is "completed", then pass image_id to create_sandbox.'
        )

    @mcp.tool()
    async def get_sandbox_image(image_id: str) -> str:
        """Look up a single custom sandbox image's current build status by
        image id."""
        if not settings.BOXKITE_IMAGE_BUILDER_ENABLED:
            return "Custom sandbox images are not enabled on this deployment."
        account = _current_account_or_raise()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            row = await SandboxImageRepository(db).get_for_account(image_id=image_id, account_id=account.id)
        if row is None or row.deleted_at is not None:
            return f"Sandbox image {image_id} not found"
        summary = f"{row.id} (status: {row.status}, label: {row.label or '(none)'})"
        if row.digest:
            summary += f", digest: {row.digest}"
        if row.failure_reason:
            summary += f", failure_reason: {row.failure_reason}"
        return summary

    @mcp.tool()
    async def list_sandbox_images() -> str:
        """List custom sandbox images built on this account."""
        if not settings.BOXKITE_IMAGE_BUILDER_ENABLED:
            return "Custom sandbox images are not enabled on this deployment."
        account = _current_account_or_raise()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            rows = await SandboxImageRepository(db).list_for_account(account_id=account.id)
        if not rows:
            return "No images found."
        lines = [
            f"- {row.id} (status: {row.status}, label: {row.label or '(none)'})" for row in rows
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def delete_sandbox_image(image_id: str) -> str:
        """Delete a custom sandbox image's control-plane record by image id.
        This only removes the bookkeeping row for the image -- any sandboxes
        already running from that image's digest keep running unaffected."""
        if not settings.BOXKITE_IMAGE_BUILDER_ENABLED:
            return "Custom sandbox images are not enabled on this deployment."
        account = _current_account_or_raise()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            images = SandboxImageRepository(db)
            row = await images.get_for_account(image_id=image_id, account_id=account.id)
            if row is None or row.deleted_at is not None:
                return f"Sandbox image {image_id} not found"
            await images.mark_deleted(image_id=image_id)
        return f"Deleted sandbox image {image_id}"

    @mcp.tool()
    async def create_sandbox_volume(label: str | None = None, size_gb: float = 1.0) -> str:
        """Create an independent, persistent storage volume that can later
        be mounted into one or more sandboxes. Returns the new volume's id
        and status -- poll get_sandbox_volume(id) until status is "ready",
        then pass {volume_id: mount_path} as create_sandbox's volume_mounts
        to mount it into a sandbox.

        label: optional human-readable label for the volume.
        size_gb: requested volume size in gigabytes (max 1024). Defaults to
            1.0.
        """
        if not settings.BOXKITE_VOLUMES_ENABLED:
            return "Independent storage volumes are not enabled on this deployment."

        account = _current_account_or_raise()
        rate_limit_message = await _enforce_mcp_rate_limit(
            bucket="volume_ops", account=account, limit=settings.BOXKITE_IMAGE_BUILD_RATE_LIMIT_PER_MINUTE
        )
        if rate_limit_message:
            return rate_limit_message

        try:
            body = VolumeCreateRequest(label=label, size_gb=size_gb)
        except ValidationError as e:
            return _describe_exception("validating sandbox volume request", e)

        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            volumes = SandboxVolumeRepository(db)
            active_count = await volumes.count_active_for_account(account.id)
            if active_count >= settings.BOXKITE_MAX_VOLUMES_PER_ACCOUNT:
                return (
                    f"Volume limit reached ({settings.BOXKITE_MAX_VOLUMES_PER_ACCOUNT} at a "
                    "time). Delete an existing volume before creating another."
                )
            volume_id = str(uuid4())
            row = await volumes.create(
                volume_id=volume_id, account_id=account.id, label=body.label, size_gb=body.size_gb, status="queued"
            )

        asyncio.create_task(
            _provision_in_background(
                provisioner=_get_volume_provisioner(),
                volume_id=volume_id,
                account_id=account.id,
                size_gb=body.size_gb,
            )
        )
        return (
            f"Started creating volume {row.id} (status: queued). Poll get_sandbox_volume "
            'with this id until status is "ready", then pass its id in create_sandbox\'s '
            "volume_mounts."
        )

    @mcp.tool()
    async def get_sandbox_volume(volume_id: str) -> str:
        """Look up a single independent storage volume's current status by
        volume id."""
        if not settings.BOXKITE_VOLUMES_ENABLED:
            return "Independent storage volumes are not enabled on this deployment."
        account = _current_account_or_raise()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            row = await SandboxVolumeRepository(db).get_for_account(volume_id=volume_id, account_id=account.id)
        if row is None or row.deleted_at is not None:
            return f"Sandbox volume {volume_id} not found"
        summary = f"{row.id} (status: {row.status}, label: {row.label or '(none)'})"
        if row.failure_reason:
            summary += f", failure_reason: {row.failure_reason}"
        return summary

    @mcp.tool()
    async def list_sandbox_volumes() -> str:
        """List independent storage volumes on this account."""
        if not settings.BOXKITE_VOLUMES_ENABLED:
            return "Independent storage volumes are not enabled on this deployment."
        account = _current_account_or_raise()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            rows = await SandboxVolumeRepository(db).list_for_account(account_id=account.id)
        if not rows:
            return "No volumes found."
        lines = [
            f"- {row.id} (status: {row.status}, label: {row.label or '(none)'})" for row in rows
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def delete_sandbox_volume(volume_id: str) -> str:
        """Delete an independent storage volume's control-plane record and
        underlying storage by volume id. Does not retroactively unmount it
        from any already-running sandbox session."""
        if not settings.BOXKITE_VOLUMES_ENABLED:
            return "Independent storage volumes are not enabled on this deployment."
        account = _current_account_or_raise()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            volumes = SandboxVolumeRepository(db)
            row = await volumes.get_for_account(volume_id=volume_id, account_id=account.id)
            if row is None or row.deleted_at is not None:
                return f"Sandbox volume {volume_id} not found"
            if row.pvc_name:
                try:
                    await _get_volume_provisioner().deprovision(pvc_name=row.pvc_name)
                except NotImplementedError:
                    # Same "no live cluster in this test suite / compose
                    # mode" status as routers/volumes.py's delete_volume.
                    logger.warning(f"[hosted_mcp] deprovision not implemented for pvc={row.pvc_name}")
            await volumes.mark_deleted(volume_id=volume_id)
        return f"Deleted sandbox volume {volume_id}"

    async def _active_session_or_none(db, *, session_id: str, account: Account):
        row = await SandboxSessionRepository(db).get_for_account(
            session_id=session_id, account_id=account.id
        )
        if row is None or row.destroyed_at is not None:
            return None
        return row

    @mcp.tool()
    async def exec(session_id: str, command: str, timeout: int | None = None) -> str:
        """Run a shell command in a sandbox. Returns stdout, or stderr and
        the exit code if the command failed."""
        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            if await _active_session_or_none(db, session_id=session_id, account=account) is None:
                return f"Sandbox {session_id} not found"
            if account.custom_allowed_commands:
                allowed, reason = validate_command_whitelist(command, account.custom_allowed_commands)
                if not allowed:
                    return f"Command not allowed: {reason}"
        try:
            result = await manager.execute(
                session_id=session_id, command=command, timeout=timeout or 30
            )
        except Exception as e:
            return _describe_exception(f"running command in sandbox {session_id}", e)
        if result.get("exit_code") != 0:
            return (
                f"Command exited {result.get('exit_code')}. stdout:\n{result.get('stdout', '')}\n"
                f"stderr:\n{result.get('stderr', '')}"
            )
        return result.get("stdout", "")

    @mcp.tool()
    async def file_create(session_id: str, path: str, content: str) -> str:
        """Create or overwrite a file in a sandbox's workspace."""
        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            if await _active_session_or_none(db, session_id=session_id, account=account) is None:
                return f"Sandbox {session_id} not found"
        try:
            result = await manager.file_create(session_id=session_id, path=path, content=content)
        except Exception as e:
            return _describe_exception(f"creating file {path} in sandbox {session_id}", e)
        return f"Wrote {result.get('path', path)} ({result.get('size', len(content))} bytes)"

    @mcp.tool()
    async def view(session_id: str, path: str, view_range: list[int] | None = None) -> str:
        """View a file's contents (optionally a line range via view_range
        [start, end]), or list a directory's entries, in a sandbox."""
        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            if await _active_session_or_none(db, session_id=session_id, account=account) is None:
                return f"Sandbox {session_id} not found"
        try:
            result = await manager.view(session_id=session_id, path=path, view_range=view_range)
        except Exception as e:
            return _describe_exception(f"viewing {path} in sandbox {session_id}", e)
        return result["content"] if "content" in result else str(result)

    @mcp.tool()
    async def str_replace(
        session_id: str,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
    ) -> str:
        """Replace a string in a sandbox file. By default old_str must appear
        exactly once; set replace_all=true to replace every occurrence."""
        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            if await _active_session_or_none(db, session_id=session_id, account=account) is None:
                return f"Sandbox {session_id} not found"
        try:
            result = await manager.str_replace(
                session_id=session_id,
                path=path,
                old_str=old_str,
                new_str=new_str,
                replace_all=replace_all,
            )
        except Exception as e:
            return _describe_exception(f"editing {path} in sandbox {session_id}", e)
        return f"Replaced in {result.get('path', path)} ({result.get('occurrences', 1)} replacement(s))"

    @mcp.tool()
    async def ls(session_id: str, path: str = "/") -> str:
        """List the direct children of a directory in a sandbox's workspace.
        Use this before `view` on a directory you haven't explored yet, or
        instead of `exec(session_id, "ls ...")` -- same result, no shell
        round trip."""
        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            if await _active_session_or_none(db, session_id=session_id, account=account) is None:
                return f"Sandbox {session_id} not found"
        try:
            entries = await manager.ls(session_id=session_id, path=path)
        except Exception as e:
            return _describe_exception(f"listing {path} in sandbox {session_id}", e)
        if not entries:
            return f"No entries in {path}."
        return "\n".join(str(entry) for entry in entries)

    @mcp.tool()
    async def glob(session_id: str, pattern: str, path: str = "/") -> str:
        """Find files by name pattern (e.g. '**/*.py') under a sandbox's
        workspace, starting from path (defaults to the workspace root)."""
        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            if await _active_session_or_none(db, session_id=session_id, account=account) is None:
                return f"Sandbox {session_id} not found"
        try:
            matches = await manager.glob(session_id=session_id, pattern=pattern, path=path)
        except Exception as e:
            return _describe_exception(f"globbing {pattern!r} in sandbox {session_id}", e)
        if not matches:
            return f"No files match {pattern!r} under {path}."
        return "\n".join(str(match) for match in matches)

    @mcp.tool()
    async def grep(
        session_id: str,
        pattern: str,
        path: str = "/",
        glob: str | None = None,
        max_matches: int = 500,
    ) -> str:
        """Search file contents by regex pattern in a sandbox's workspace.
        glob optionally restricts which files are searched (e.g. '*.py')."""
        account = _current_account_or_raise()
        manager = get_manager()
        session_factory = db_module.get_session_factory()
        async with session_factory() as db:
            if await _active_session_or_none(db, session_id=session_id, account=account) is None:
                return f"Sandbox {session_id} not found"
        try:
            result = await manager.grep(
                session_id=session_id,
                pattern=pattern,
                path=path,
                glob=glob,
                max_matches=max_matches,
            )
        except Exception as e:
            return _describe_exception(f"grepping {pattern!r} in sandbox {session_id}", e)
        matches = result.get("matches", [])
        if not matches:
            return f"No matches for {pattern!r} under {path}."
        lines = [f"{m.get('path')}:{m.get('line')}: {m.get('text')}" for m in matches]
        if result.get("truncated"):
            lines.append("(truncated)")
        return "\n".join(lines)

    return mcp


def build_hosted_mcp_asgi_app() -> tuple[FastMCP, Starlette]:
    """Convenience wrapper: builds the FastMCP instance, materializes its
    Streamable HTTP ASGI app (required before `.session_manager` is
    accessible), and wraps it with `BearerTokenAuthMiddleware`. Returns
    `(mcp, wrapped_asgi_app)` -- callers need `mcp` to enter
    `mcp.session_manager.run()` in their own lifespan, and `wrapped_asgi_app`
    to actually mount."""
    mcp = build_hosted_mcp()
    inner_app = mcp.streamable_http_app()
    return mcp, BearerTokenAuthMiddleware(inner_app)
