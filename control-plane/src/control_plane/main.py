"""boxkite control-plane FastAPI app.

A separate service from `sidecar/main.py` — see `__init__.py`'s module
docstring for the architecture this sits on top of. Auto-generated
OpenAPI/Swagger docs are available at `/docs` (Swagger UI) and `/redoc`
(ReDoc) once the app is running *and* `settings.api_docs_enabled` is true
(see config.py) — enabled automatically in dev/test, disabled elsewhere
unless explicitly overridden via `ENABLE_API_DOCS`; every route below
carries a `summary` and `description` specifically so those generated docs
are usable, not just a bare list of paths.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from boxkite import close_sandbox_manager, close_warm_pool, get_sandbox_manager, get_warm_pool

from .config import settings
from .db import dispose_engine, get_engine, init_schema
from .errors import ApiError, api_error_handler
from .idempotency import IdempotencyMiddleware
from .observability import RequestMetricsMiddleware, configure_logging, render_metrics
from .hosted_mcp import build_hosted_mcp_asgi_app
from .reaper import run_reaper_loop
from .routers import (
    account,
    admin,
    api_keys,
    auth,
    demo_playground,
    enterprise_sso,
    images,
    internal_secrets,
    mcp_connections,
    oauth,
    organizations,
    sandboxes,
    scim,
    secrets,
    snapshots,
    social_login,
    usage,
    volumes,
    webhooks,
)
from .webhook_delivery import close_http_client as close_webhook_http_client
from .webhook_delivery import run_webhook_delivery_loop

logger = logging.getLogger(__name__)

_reaper_stop_event: asyncio.Event | None = None
_reaper_task: asyncio.Task | None = None
_webhook_delivery_stop_event: asyncio.Event | None = None
_webhook_delivery_task: asyncio.Task | None = None

# Built at module load time (not inside lifespan) so `_hosted_mcp_asgi_app`
# is available for `app.mount()` below before the app object even exists —
# `FastMCP.streamable_http_app()` must be called once before
# `.session_manager` is accessible, which the lifespan below needs.
# docs/HOSTED-MCP-DESIGN.md.
_hosted_mcp, _hosted_mcp_asgi_app = build_hosted_mcp_asgi_app()


def verify_startup_config(cfg) -> None:
    """Fail-fast guard against insecure defaults leaking into a real deploy.

    In a dev/test ENVIRONMENT these downgrade to warnings so zero-config local
    iteration keeps working; anywhere else they raise RuntimeError and abort
    startup. Pure function of the settings object so it's unit-testable without
    driving the whole app lifespan.
    """
    if cfg.JWT_SECRET == "insecure-dev-secret-change-me-32-bytes-minimum":
        if cfg.is_dev_environment:
            logger.warning(
                "[control-plane] JWT_SECRET is at its insecure default placeholder. "
                "Set a real secret via the JWT_SECRET env var before deploying "
                "this outside local development."
            )
        else:
            raise RuntimeError(
                "[control-plane] JWT_SECRET is at its insecure default placeholder "
                f"while ENVIRONMENT={cfg.ENVIRONMENT!r}. Refusing to start: set "
                "a real secret via the JWT_SECRET env var (e.g. `openssl rand -hex "
                "32`) before deploying this outside local development."
            )

    # The "local" secrets-KMS backend protects Secret.ciphertext with a local
    # dev key (ephemeral when SECRETS_LOCAL_DEV_KMS_KEY is unset) and is
    # documented as NOT a real KMS. Refuse it outside dev unless explicitly
    # accepted, so a deploy that forgot to wire up a cloud KMS fails loudly
    # instead of silently shipping weakly-protected secrets.
    if cfg.SECRETS_KMS_BACKEND == "local" and not cfg.is_dev_environment:
        if cfg.BOXKITE_ALLOW_INSECURE_LOCAL_KMS:
            logger.warning(
                "[control-plane] SECRETS_KMS_BACKEND='local' while "
                f"ENVIRONMENT={cfg.ENVIRONMENT!r}: Secret.ciphertext is protected by "
                "a local dev key, not a real KMS. Running anyway because "
                "BOXKITE_ALLOW_INSECURE_LOCAL_KMS=true is set."
            )
        else:
            raise RuntimeError(
                "[control-plane] SECRETS_KMS_BACKEND='local' is not a real KMS and "
                f"must not be used while ENVIRONMENT={cfg.ENVIRONMENT!r}. Refusing to "
                "start: set SECRETS_KMS_BACKEND to aws/azure/gcp with SECRETS_KMS_KEY_ID, "
                "or set BOXKITE_ALLOW_INSECURE_LOCAL_KMS=true to explicitly accept a "
                "local dev key outside development."
            )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _reaper_stop_event, _reaper_task, _webhook_delivery_stop_event, _webhook_delivery_task

    configure_logging(settings)
    verify_startup_config(settings)

    await init_schema()

    manager = get_sandbox_manager()
    _reaper_stop_event = asyncio.Event()
    _reaper_task = asyncio.create_task(run_reaper_loop(manager, stop_event=_reaper_stop_event))

    # Webhook delivery worker (docs/WEBHOOKS-DESIGN.md) -- same "plain
    # background asyncio task, started/stopped alongside the reaper" shape.
    # Runs unconditionally (not gated by RUNTIME_MODE, unlike the warm pool
    # below): it only ever does anything once an account has registered a
    # webhook subscription, so there's no cost to it running in compose/
    # local-dev either.
    _webhook_delivery_stop_event = asyncio.Event()
    _webhook_delivery_task = asyncio.create_task(
        run_webhook_delivery_loop(stop_event=_webhook_delivery_stop_event)
    )

    # Warm pool pre-warms K8s pods for fast session startup. get_warm_pool()
    # already starts its background tasks internally, but it also talks to
    # the real Kubernetes API to do so, so it's only wired up for the k8s
    # runtime -- standalone/compose deployments have no cluster to reach and
    # `get_warm_pool()` isn't guaranteed to no-op for those modes the way it
    # already does for RUNTIME_MODE=compose.
    if os.environ.get("RUNTIME_MODE") == "k8s":
        await get_warm_pool()

    try:
        # The mounted /mcp app's own Streamable HTTP session manager needs
        # its run() context active for the lifetime of the process -- it
        # isn't started automatically just because the ASGI app is mounted
        # (Starlette doesn't recurse into a mounted sub-app's own lifespan).
        async with _hosted_mcp.session_manager.run():
            yield
    finally:
        if _reaper_stop_event is not None:
            _reaper_stop_event.set()
        if _reaper_task is not None:
            await _reaper_task
        if _webhook_delivery_stop_event is not None:
            _webhook_delivery_stop_event.set()
        if _webhook_delivery_task is not None:
            await _webhook_delivery_task
        await close_webhook_http_client()
        await close_warm_pool()
        await close_sandbox_manager()
        await dispose_engine()


app = FastAPI(
    title="boxkite control plane",
    description=(
        "Multi-tenant control-plane API for boxkite: sign up, generate an "
        "API key, and create/manage sandbox sessions through an "
        "authenticated HTTP API. Sandbox pod lifecycle is delegated to "
        "boxkite's SandboxManager/WarmPoolManager; this service adds "
        "accounts, API keys, and configurable fair-use limits on top. "
        "No billing or payment concepts exist here — usage limits are "
        "purely fair-use caps, not pricing tiers."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)

app.add_exception_handler(ApiError, api_error_handler)

# Bearer-token auth (dashboard JWT or API key), never cookies, so this
# carries no CSRF risk regardless of allowed origins -- there's no ambient
# credential for a cross-origin page to ride on. But CORS also gates
# *response-body confidentiality*: a wildcard origin would let ANY page
# that somehow gets hold of a valid key (e.g. one pasted into another
# tool's browser JS) read this API's responses for it. Scoped to the
# actual first-party browser client(s) instead -- server-side/SDK callers
# (not running in a browser) are never subject to CORS at all, so this
# costs them nothing.
# Added before CORS so CORS stays outermost and still wraps idempotent replays.
# A strict no-op unless a request is a POST carrying an Idempotency-Key header.
app.add_middleware(IdempotencyMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.CORS_ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Outermost of the add_middleware set so it times the full response and stamps
# every reply with X-Request-ID. Pure-ASGI: never buffers the body.
app.add_middleware(RequestMetricsMiddleware)


@app.middleware("http")
async def deny_framing(request, call_next):
    """Applied globally rather than scoped to `/oauth/authorize*` alone: the
    rest of this API is JSON and unaffected by a browser honoring these
    headers, so there's no cost to setting them everywhere, and it removes
    the risk of a future new HTML-rendering route (this app has exactly one
    today -- the OAuth consent screen, oauth_consent.py) shipping without
    the same protection because someone forgot to scope it in.

    The concrete risk this closes: `/oauth/authorize`'s consent screen is
    the first cookie-authenticated, browser-rendered page in this
    control-plane (everything else is a stateless bearer-token API -- see
    the CORS middleware's own comment above for why that carries no CSRF
    risk, which does NOT extend to this cookie-based page). Any caller can
    self-register an OAuth client (`POST /oauth/register`, RFC 7591's own
    open-registration model) and control its displayed `client_name`, so
    without this header a logged-in victim could be framed under an
    attacker page and clickjacked into approving that attacker's access.
    """
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic v2's exc.errors() entries carry the offending submitted value in
    # `input` (and sometimes `ctx`), so returning them verbatim echoes secrets
    # like a rejected password back to the client. Surface only type/loc/msg.
    safe_details = [
        {"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": "Invalid request", "details": safe_details}},
    )


app.include_router(auth.router)
app.include_router(api_keys.router)
app.include_router(sandboxes.router)
app.include_router(snapshots.sandbox_snapshots_router)
app.include_router(snapshots.snapshots_router)
app.include_router(usage.router)
app.include_router(account.router)
app.include_router(secrets.router)
app.include_router(internal_secrets.router)
app.include_router(mcp_connections.router)
app.include_router(images.router)
app.include_router(volumes.router)
app.include_router(admin.router)
app.include_router(oauth.router)
app.include_router(social_login.router)
app.include_router(enterprise_sso.router)
app.include_router(scim.router)
app.include_router(webhooks.router)
app.include_router(demo_playground.router)
app.include_router(organizations.router)

# Hosted, remote MCP endpoint (docs/HOSTED-MCP-DESIGN.md) -- Streamable
# HTTP transport, bearer-token auth via BearerTokenAuthMiddleware (wrapped
# in already, see hosted_mcp.py), reusing the same long-lived API keys
# every /v1/* route accepts. Not an APIRouter like the others above: it's a
# full ASGI sub-application (FastMCP's own Starlette app), mounted rather
# than included.
app.mount("/mcp", _hosted_mcp_asgi_app)


@app.get("/health", tags=["meta"], summary="Liveness check")
async def health() -> dict:
    # Cheap liveness probe: "is the process up?" — deliberately does no
    # dependency I/O so a transient DB blip can't trigger a pod restart.
    # Use /health/ready for a dependency-aware readiness signal.
    return {"status": "ok"}


@app.get(
    "/health/ready",
    tags=["meta"],
    summary="Readiness check (verifies database connectivity)",
)
async def readiness(response: Response) -> dict:
    """Verify the control-plane can actually serve traffic by round-tripping a
    trivial query to the database. Returns 503 when the DB is unreachable so a
    Kubernetes readiness probe pulls the pod out of rotation instead of routing
    requests that would fail."""
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — any failure means "not ready"
        logger.warning("Readiness check failed: database unreachable: %s", exc)
        response.status_code = 503
        return {"status": "unavailable", "checks": {"database": "unreachable"}}
    return {"status": "ready", "checks": {"database": "ok"}}


@app.get(
    "/metrics",
    tags=["meta"],
    summary="Prometheus metrics exposition",
    include_in_schema=False,
)
async def metrics() -> Response:
    """Prometheus exposition of request counts + latency. 404s when
    BOXKITE_METRICS_ENABLED is false (same opt-out convention as the API docs)."""
    if not settings.BOXKITE_METRICS_ENABLED:
        return Response(status_code=404)
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
