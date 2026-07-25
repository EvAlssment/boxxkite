"""Sync and async clients for a hosted boxkite control-plane.

Thin wrapper over the same v1 HTTP API the `boxkite` CLI itself calls
(control-plane/src/control_plane/routers/*.py) -- no behavior lives here
beyond request/response plumbing and the SandboxSession context-manager
convenience. `transport` is exposed on both constructors purely for
testing (httpx.MockTransport) -- real callers never need it.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
import websockets
import websockets.sync.client

from .exceptions import BoxkiteApiError, BoxkiteConnectionError

DEFAULT_TIMEOUT = 30.0
EXEC_TIMEOUT_HEADROOM = 15.0

_LOCALHOST_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

# 429 (rate limited) plus the transient 5xx family -- retrying a 500 that
# reflects a deterministic server bug won't help, but these are the codes a
# control-plane returns for load/restart/upstream blips worth re-attempting.
DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Only verbs the control-plane treats as idempotent are safe to blind-retry:
# a retried POST could double-create a sandbox/secret/webhook. PUT/DELETE on
# this API are idempotent by resource id, so they stay in.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})


@dataclass(frozen=True)
class RetryConfig:
    """Opt-in automatic retry for transient failures. Pass an instance as
    `retry=` to either client to enable it; the defaults here are the
    "sensible default" -- construct `RetryConfig()` to accept them.

    Only idempotent verbs (see `methods`) are ever retried, and only on a
    connection failure or a status in `statuses` (429 + transient 5xx). A
    non-idempotent POST is never retried, so enabling this can't
    double-create a resource. Backoff is exponential with full jitter; a
    `Retry-After` header (seconds or HTTP-date) is honored when present and
    `respect_retry_after` is set.
    """

    max_retries: int = 2
    backoff_base: float = 0.5
    backoff_max: float = 30.0
    statuses: frozenset[int] = DEFAULT_RETRY_STATUSES
    methods: frozenset[str] = IDEMPOTENT_METHODS
    respect_retry_after: bool = True


def _parse_retry_after(value: str) -> float | None:
    """Parse a Retry-After header (delta-seconds or an HTTP-date) into a
    non-negative delay in seconds, or None if it can't be understood."""
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _retry_delay(config: RetryConfig, attempt: int, retry_after: float | None) -> float:
    """Delay before the next attempt (0-indexed `attempt`). A server-supplied
    Retry-After wins over computed backoff; otherwise full-jitter exponential
    backoff, capped at `backoff_max`."""
    if retry_after is not None:
        return min(config.backoff_max, retry_after)
    ceiling = min(config.backoff_max, config.backoff_base * (2**attempt))
    return random.uniform(0, ceiling)


def _retry_after_from(config: RetryConfig, resp: httpx.Response) -> float | None:
    if not config.respect_retry_after:
        return None
    header = resp.headers.get("Retry-After")
    return _parse_retry_after(header) if header else None


def _should_retry(config: RetryConfig | None, method: str, attempt: int, status: int | None) -> bool:
    if config is None or attempt >= config.max_retries:
        return False
    if method.upper() not in config.methods:
        return False
    # status None == a connection-level failure (no response reached us);
    # safe to retry for an idempotent verb since the server may never have
    # seen the request.
    return status is None or status in config.statuses


def _validate_base_url_scheme(base_url: str) -> None:
    """Reject a non-https base_url unless it points at localhost -- every
    request sends `Authorization: Bearer <api_key>` (a full-privilege,
    long-lived account credential), so an http:// URL to anything else
    would put it on the wire in cleartext."""
    parsed = urlparse(base_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in _LOCALHOST_HOSTNAMES:
        return
    raise ValueError(
        f"Refusing to use non-https base_url {base_url!r}: this would send your API "
        "key in cleartext. Use an https:// URL, or http://localhost (local dev only)."
    )


def _iter_sse_events(lines: Iterator[str]) -> Iterator[dict]:
    """Parse a text/event-stream body into decoded JSON payloads. Only the
    `data:` field is used -- `watch` doesn't need `event:`/`id:` framing,
    just the ExecLogEntry payload each event carries."""
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            if data_lines:
                yield json.loads("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if data_lines:
        yield json.loads("\n".join(data_lines))


async def _aiter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if data_lines:
                yield json.loads("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if data_lines:
        yield json.loads("\n".join(data_lines))


def _to_ws_url(base_url: str, path: str) -> str:
    """https:// -> wss://, http:// -> ws:// -- base_url has already passed
    `_validate_base_url_scheme`, so it's always one of those two."""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + path
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :] + path
    raise ValueError(f"Unsupported base_url scheme: {base_url!r}")


def _default_sync_ws_connect(url: str, **kwargs: Any) -> Any:
    return websockets.sync.client.connect(url, **kwargs)


async def _default_async_ws_connect(url: str, **kwargs: Any) -> Any:
    return await websockets.connect(url, **kwargs)


def _raise_for_error(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    code = "error"
    message = f"HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = err.get("code", code)
            message = err.get("message", message)
    raise BoxkiteApiError(status_code=resp.status_code, code=code, message=message)


class BoxkiteClient:
    """Synchronous client. Safe to share across threads (httpx.Client is)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retry: RetryConfig | None = None,
        transport: httpx.BaseTransport | None = None,
        ws_connect: Callable[..., Any] | None = None,
    ) -> None:
        _validate_base_url_scheme(base_url)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._retry = retry
        self._ws_connect = ws_connect or _default_sync_ws_connect
        self._http = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BoxkiteClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if _should_retry(self._retry, method, attempt, None):
                    time.sleep(_retry_delay(self._retry, attempt, None))
                    attempt += 1
                    continue
                raise BoxkiteConnectionError(str(exc)) from exc
            if _should_retry(self._retry, method, attempt, resp.status_code):
                time.sleep(_retry_delay(self._retry, attempt, _retry_after_from(self._retry, resp)))
                attempt += 1
                continue
            _raise_for_error(resp)
            return resp.json() if resp.content else None

    def account(self) -> dict:
        """GET /v1/account -- identity for the API key in use."""
        return self._request("GET", "/v1/account")

    def usage(self) -> dict:
        """GET /v1/usage -- current usage against fair-use limits."""
        return self._request("GET", "/v1/usage")

    def request_password_reset(self, email: str) -> dict:
        """POST /v1/auth/password-reset/request -- opt-in on the
        control-plane (BOXKITE_PASSWORD_RESET_ENABLED); raises
        BoxkiteApiError(404, "feature_disabled") if the deployment hasn't
        enabled it. Always returns the same message whether or not the
        email is registered, so this call can never be used to enumerate
        accounts. Email delivery is stubbed server-side (see
        control-plane/src/control_plane/email_sender.py) unless the
        deployment has wired up a real EmailSender.
        """
        return self._request("POST", "/v1/auth/password-reset/request", json={"email": email})

    def confirm_password_reset(self, token: str, new_password: str) -> dict:
        """POST /v1/auth/password-reset/confirm -- consumes a single-use
        token minted by request_password_reset() and sets a new password.
        Also revokes every outstanding refresh token for the account, if
        refresh tokens are enabled server-side. Raises BoxkiteApiError(400,
        "invalid_or_expired_token") for an unknown, already-used, or
        expired token.
        """
        return self._request(
            "POST",
            "/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": new_password},
        )

    def verify_email(self, token: str) -> dict:
        """POST /v1/auth/verify-email -- opt-in
        (BOXKITE_EMAIL_VERIFICATION_ENABLED). Consumes a single-use token
        (minted automatically at signup, or by resend_verification()) and
        marks the account's email verified. Raises BoxkiteApiError(400,
        "invalid_or_expired_token") for an unknown, already-used, or
        expired token.
        """
        return self._request("POST", "/v1/auth/verify-email", json={"token": token})

    def resend_verification(self, access_token: str) -> dict:
        """POST /v1/auth/resend-verification -- opt-in
        (BOXKITE_EMAIL_VERIFICATION_ENABLED). Requires a dashboard session
        token (the JWT returned by /v1/auth/login or /v1/auth/signup), not
        this client's api_key -- api_key and a dashboard JWT are two
        different, non-interchangeable credential types on this
        control-plane (see control-plane/src/control_plane/deps.py), so
        the JWT is passed explicitly here and overrides this call's
        Authorization header rather than using self._api_key.
        """
        return self._request(
            "POST",
            "/v1/auth/resend-verification",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def refresh_token(self, refresh_token: str) -> dict:
        """POST /v1/auth/refresh -- opt-in (BOXKITE_REFRESH_TOKENS_ENABLED).
        Exchanges a still-valid refresh token for a brand new access_token +
        refresh_token pair, revoking the presented one in the same request
        (rotation, not reuse) -- store the new refresh_token from the
        response and discard the one you presented. Raises
        BoxkiteApiError(401, "invalid_refresh_token") if the token is
        unknown/expired, or (401, "refresh_token_reused") if it was already
        rotated out or revoked (which also revokes every other refresh
        token on the account as a precaution).
        """
        return self._request("POST", "/v1/auth/refresh", json={"refresh_token": refresh_token})

    def logout(self, refresh_token: str) -> None:
        """POST /v1/auth/logout -- opt-in (BOXKITE_REFRESH_TOKENS_ENABLED).
        Revokes one refresh token immediately. Always succeeds (204)
        whether or not the token was valid -- never leaks which.
        """
        self._request("POST", "/v1/auth/logout", json={"refresh_token": refresh_token})

    def create_sandbox(
        self,
        *,
        label: str | None = None,
        size: str | None = None,
        storage_gb: float | None = None,
        lifetime_minutes: int | None = None,
        count: int | None = None,
        secret_names: list[str] | None = None,
        image_id: str | None = None,
        mcp_connection_names: list[str] | None = None,
        volume_mounts: dict[str, str] | None = None,
        gpu_count: int | None = None,
    ) -> dict:
        """POST /v1/sandboxes -- create a new sandbox.

        Args:
            label: Optional human-readable label for the sandbox.
            size: Sandbox size -- one of "small", "medium", "large". Defaults
                to "small" server-side when omitted.
            storage_gb: Requested persistent storage in GB.
            lifetime_minutes: Maximum lifetime of the sandbox in minutes before
                automatic teardown.
            count: Number of sandboxes to create in this request.
            secret_names: Names of this account's secrets (see POST /v1/secrets)
                this session should be granted access to via the sidecar's
                secrets-broker http_request tool (docs/SECRETS-DESIGN.md). A
                name that doesn't exist for this account 404s before any
                sandbox is created.
            image_id: Id of a completed custom image built via create_image().
                If given, the sandbox uses that digest-pinned image instead of
                the operator's default. 404s if not owned by this account or
                not yet status "completed".
            mcp_connection_names: Labels of this account's outbound-MCP
                connections (see create_mcp_connection(), GitHub issues
                #116/#117) this session should be granted network egress to.
                A name that doesn't exist for this account 404s before any
                sandbox is created, same precedent as secret_names. This only
                widens the session's per-pod NetworkPolicy egress allowlist --
                there is no MCP-proxy transport yet
                (docs/OUTBOUND-MCP-DESIGN.md section 6), so a granted
                connection does not yet let an agent actually speak MCP to it.
            volume_mounts: Optional {volume_id: mount_path} mapping of
                independent PVC-backed volumes (see create_volume()) to mount
                into this sandbox. Every volume_id must already exist for this
                account and be status "ready" -- 404s otherwise. mount_path
                must be an absolute path outside the sandbox's typed roots
                (/workspace, /mnt/*, /tmp).
            gpu_count: Opt-in, experimental (docs/GPU-SUPPORT-SCOPING.md) --
                requests this many GPUs as a Kubernetes extended-resource
                limit. 422s (gpu_support_disabled) unless the deployment has
                BOXKITE_GPU_ENABLED set and a GPU-equipped node pool with a
                device plugin provisioned; not verified against real GPU
                hardware in this codebase.
        """
        body: dict[str, Any] = {}
        if label:
            body["label"] = label
        if size is not None:
            body["size"] = size
        if storage_gb is not None:
            body["storage_gb"] = storage_gb
        if lifetime_minutes is not None:
            body["lifetime_minutes"] = lifetime_minutes
        if count is not None:
            body["count"] = count
        if secret_names is not None:
            body["secret_names"] = secret_names
        if image_id is not None:
            body["image_id"] = image_id
        if mcp_connection_names is not None:
            body["mcp_connection_names"] = mcp_connection_names
        if volume_mounts is not None:
            body["volume_mounts"] = volume_mounts
        if gpu_count is not None:
            body["gpu_count"] = gpu_count
        return self._request("POST", "/v1/sandboxes", json=body)

    def get_sandbox(self, session_id: str) -> dict:
        return self._request("GET", f"/v1/sandboxes/{session_id}")

    def list_sandboxes(self, *, active_only: bool = False) -> list[dict]:
        result = self._request("GET", "/v1/sandboxes", params={"active_only": str(active_only).lower()})
        return result or []

    def destroy_sandbox(self, session_id: str) -> None:
        self._request("DELETE", f"/v1/sandboxes/{session_id}")

    def create_image(
        self,
        *,
        label: str | None = None,
        base: str = "boxkite-default",
        python_packages: list[str] | None = None,
        apt_packages: list[str] | None = None,
        npm_packages: list[str] | None = None,
    ) -> dict:
        """POST /v1/images -- build a custom sandbox image.

        Args:
            label: Optional human-readable label for the image.
            base: Base image to build from -- one of "boxkite-default",
                "boxkite-minimal", "boxkite-node", "boxkite-go",
                "boxkite-nextjs", "boxkite-rust". Defaults to
                "boxkite-default". "boxkite-node" drops Python entirely (no
                python_packages installable, only apt_packages/npm_packages).
                "boxkite-go" and "boxkite-rust" both drop Python and Node
                entirely (no python_packages or npm_packages installable,
                only apt_packages). "boxkite-nextjs" is the same Node-only
                runtime as "boxkite-node" plus a pre-installed Next.js App
                Router starter vendored at /opt/nextjs-template (same
                python_packages restriction as "boxkite-node").
            python_packages: Exact-version-pinned packages ("name==version",
                no ranges) to install into the image.
            apt_packages: Exact-version-pinned apt packages ("name==version",
                no ranges) to install into the image.
            npm_packages: Exact-version-pinned npm packages ("name==version",
                no ranges), e.g. "typescript==5.6.0", to install into the
                image. Not supported on base="boxkite-go" or
                base="boxkite-rust".
        """
        body: dict[str, Any] = {}
        if label:
            body["label"] = label
        if base is not None:
            body["base"] = base
        if python_packages is not None:
            body["python_packages"] = python_packages
        if apt_packages is not None:
            body["apt_packages"] = apt_packages
        if npm_packages is not None:
            body["npm_packages"] = npm_packages
        return self._request("POST", "/v1/images", json=body)

    def get_image(self, image_id: str) -> dict:
        return self._request("GET", f"/v1/images/{image_id}")

    def list_images(self) -> list[dict]:
        result = self._request("GET", "/v1/images")
        return result or []

    def delete_image(self, image_id: str) -> None:
        self._request("DELETE", f"/v1/images/{image_id}")

    def create_volume(self, *, label: str | None = None, size_gb: float) -> dict:
        """POST /v1/volumes -- create an independent, PVC-backed storage
        volume that can later be mounted into one or more sandboxes via
        create_sandbox(volume_mounts=...).

        Args:
            label: Optional human-readable label for the volume.
            size_gb: Requested volume size in GB (max 1024).
        """
        body: dict[str, Any] = {"size_gb": size_gb}
        if label:
            body["label"] = label
        return self._request("POST", "/v1/volumes", json=body)

    def get_volume(self, volume_id: str) -> dict:
        return self._request("GET", f"/v1/volumes/{volume_id}")

    def list_volumes(self) -> list[dict]:
        result = self._request("GET", "/v1/volumes")
        return result or []

    def delete_volume(self, volume_id: str) -> None:
        self._request("DELETE", f"/v1/volumes/{volume_id}")

    def create_webhook(
        self,
        *,
        url: str,
        event_types: list[str],
        description: str | None = None,
    ) -> dict:
        """POST /v1/webhooks -- register a webhook subscription.

        Args:
            url: HTTPS (or HTTP, for local testing) URL the control plane
                will POST events to. Checked at registration time against
                the same private/link-local/loopback/metadata-address
                denylist POST /v1/secrets uses for allowed_hosts.
            event_types: Event types this subscription should receive (at
                least one required) -- "sandbox.created",
                "sandbox.destroyed", or "audit_log.entry" (added per
                GitHub issue #125 for SIEM/audit-log export). See
                docs/WEBHOOKS-DESIGN.md for the full event catalog.
            description: Optional caller-supplied label for this
                subscription (e.g. "Slack notifier").

        Returns the subscription plus a `secret` field -- the raw signing
        secret, shown exactly once. Use it to verify the
        X-Boxkite-Webhook-Signature header on every delivery; it cannot be
        retrieved again after this response.
        """
        body: dict[str, Any] = {"url": url, "event_types": event_types}
        if description is not None:
            body["description"] = description
        return self._request("POST", "/v1/webhooks", json=body)

    def list_webhooks(self) -> list[dict]:
        """GET /v1/webhooks -- webhook subscriptions for this account. The
        signing secret is never returned here."""
        result = self._request("GET", "/v1/webhooks")
        return result or []

    def delete_webhook(self, subscription_id: str) -> None:
        """DELETE /v1/webhooks/{id} -- delete a webhook subscription owned
        by this account. 404s if already gone or never owned by this
        account."""
        self._request("DELETE", f"/v1/webhooks/{subscription_id}")

    def list_webhook_deliveries(
        self, subscription_id: str, *, limit: int | None = None, offset: int | None = None
    ) -> list[dict]:
        """GET /v1/webhooks/{id}/deliveries -- recent delivery attempts
        (pending/delivered/failed) for this subscription, newest first.

        Args:
            subscription_id: The webhook subscription id.
            limit: Maximum number of entries to return (server default 20,
                max 100).
            offset: Number of entries to skip, newest-first.
        """
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        result = self._request("GET", f"/v1/webhooks/{subscription_id}/deliveries", params=params)
        return result or []

    def create_mcp_connection(self, *, label: str, catalog_id: str) -> dict:
        """POST /v1/mcp-connections -- grant this account access to one
        curated outbound-MCP catalog entry (GitHub issues #116/#117,
        docs/OUTBOUND-MCP-DESIGN.md).

        Args:
            label: Unique (per-account) name for this connection -- pass it
                in create_sandbox(mcp_connection_names=[...]) to grant a
                session network egress to it.
            catalog_id: Which curated catalog entry to grant -- one of
                "slack", "notion", "linear", "github". Restricted to
                boxkite's own reviewed allowlist; never a caller-supplied
                hostname.

        Note: this only widens a granted session's per-pod NetworkPolicy
        egress allowlist to the connection's catalog hostname -- there is no
        MCP-proxy transport yet, so this does not yet let an agent speak MCP
        protocol to the destination.
        """
        return self._request(
            "POST", "/v1/mcp-connections", json={"label": label, "catalog_id": catalog_id}
        )

    def list_mcp_connections(self) -> list[dict]:
        """GET /v1/mcp-connections -- outbound-MCP connection grants for
        this account."""
        result = self._request("GET", "/v1/mcp-connections")
        return result or []

    def delete_mcp_connection(self, connection_id: str) -> None:
        """DELETE /v1/mcp-connections/{id} -- delete an outbound-MCP
        connection grant owned by this account. 404s if already gone or
        never owned by this account."""
        self._request("DELETE", f"/v1/mcp-connections/{connection_id}")

    def create_secret(
        self,
        *,
        name: str,
        value: str,
        allowed_hosts: list[str],
        trust_tier: str | None = None,
    ) -> dict:
        """POST /v1/secrets -- register a new org-scoped secret for the
        proxy-substitution secrets broker (docs/SECRETS-DESIGN.md).

        Args:
            name: Unique (per-account) name used to reference this secret
                from create_sandbox(secret_names=[...]) and from an agent
                tool call as {{secret:name}} in a POST /http-request
                body/header.
            value: The real credential value. Write-only -- accepted here
                and never returned by this or any other route, including
                this response.
            allowed_hosts: Destination hostnames this secret may be used
                against via POST /http-request. Required, not optional --
                an unscoped secret usable against any destination defeats
                the point of this feature. A host that resolves to a
                private/link-local/loopback/metadata address is rejected at
                creation time (a best-effort backstop; see
                docs/SECRETS-DESIGN.md §5 for why the real control is the
                sidecar's request-time check).
            trust_tier: Only meaningful for wallet/private-key-style
                secrets (docs/WALLET-SECRETS-DESIGN.md) -- omit for an
                ordinary API-key-style secret. The only accepted value
                today is "testnet"; "mainnet" is refused (422).

        Returns the created secret's metadata (id, name, allowed_hosts,
        trust_tier, created_at, last_used_at) -- never the raw value.
        """
        body: dict[str, Any] = {"name": name, "value": value, "allowed_hosts": allowed_hosts}
        if trust_tier is not None:
            body["trust_tier"] = trust_tier
        return self._request("POST", "/v1/secrets", json=body)

    def list_secrets(self) -> list[dict]:
        """GET /v1/secrets -- secrets registered for this account. Raw
        values are never returned here."""
        result = self._request("GET", "/v1/secrets")
        return result or []

    def delete_secret(self, secret_id: str) -> None:
        """DELETE /v1/secrets/{id} -- delete a secret owned by this
        account. 404s if already gone or never owned by this account."""
        self._request("DELETE", f"/v1/secrets/{secret_id}")

    def exec(
        self, session_id: str, command: str, *, timeout: int | None = None, description: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"command": command}
        if timeout is not None:
            body["timeout"] = timeout
        if description is not None:
            body["description"] = description
        kwargs: dict[str, Any] = {"json": body}
        if timeout is not None:
            kwargs["timeout"] = timeout + EXEC_TIMEOUT_HEADROOM
        return self._request("POST", f"/v1/sandboxes/{session_id}/exec", **kwargs)

    def http_request(
        self,
        session_id: str,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout: int | None = None,
    ) -> dict:
        """POST /v1/sandboxes/{id}/http-request -- the secrets-broker HTTP
        request (docs/SECRETS-DESIGN.md). `headers`/`body` may contain a
        literal `{{secret:name}}` reference for a secret granted to this
        session via `create_sandbox(secret_names=[...])`; the sidecar
        substitutes the real value in-process, this SDK/client never sees it.
        """
        payload: dict[str, Any] = {"method": method, "url": url}
        if headers is not None:
            payload["headers"] = headers
        if body is not None:
            payload["body"] = body
        if timeout is not None:
            payload["timeout"] = timeout
        kwargs: dict[str, Any] = {"json": payload}
        if timeout is not None:
            kwargs["timeout"] = timeout + EXEC_TIMEOUT_HEADROOM
        return self._request("POST", f"/v1/sandboxes/{session_id}/http-request", **kwargs)

    def file_create(self, session_id: str, path: str, content: str, *, description: str | None = None) -> dict:
        body: dict[str, Any] = {"path": path, "content": content}
        if description is not None:
            body["description"] = description
        return self._request("POST", f"/v1/sandboxes/{session_id}/files", json=body)

    def lsp_start(self, session_id: str, language: str) -> dict:
        """POST /v1/sandboxes/{id}/lsp/start -- starts a persistent language
        server (pyright for "python", typescript-language-server for
        "typescript"/"javascript"). Returns a dict with `lsp_id`, an opaque
        handle to pass to lsp_open/lsp_completion/lsp_stop."""
        return self._request("POST", f"/v1/sandboxes/{session_id}/lsp/start", json={"language": language})

    def lsp_open(self, session_id: str, lsp_id: str, path: str, content: str) -> dict:
        """POST /v1/sandboxes/{id}/lsp/{lsp_id}/open -- opens (or
        full-document-replaces) a document on a running language server."""
        return self._request(
            "POST", f"/v1/sandboxes/{session_id}/lsp/{lsp_id}/open", json={"path": path, "content": content}
        )

    def lsp_completion(self, session_id: str, lsp_id: str, path: str, line: int, character: int) -> dict:
        """POST /v1/sandboxes/{id}/lsp/{lsp_id}/completion -- requests
        completions at a 0-indexed (line, character) position. `path` must
        already be open on this handle (see lsp_open)."""
        return self._request(
            "POST",
            f"/v1/sandboxes/{session_id}/lsp/{lsp_id}/completion",
            json={"path": path, "line": line, "character": character},
        )

    def lsp_stop(self, session_id: str, lsp_id: str) -> dict:
        """POST /v1/sandboxes/{id}/lsp/{lsp_id}/stop -- gracefully shuts
        down a running language server."""
        return self._request("POST", f"/v1/sandboxes/{session_id}/lsp/{lsp_id}/stop")

    def view(
        self, session_id: str, path: str, *, view_range: list[int] | None = None, description: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"path": path}
        if view_range is not None:
            body["view_range"] = view_range
        if description is not None:
            body["description"] = description
        return self._request("POST", f"/v1/sandboxes/{session_id}/files/view", json=body)

    def str_replace(
        self,
        session_id: str,
        path: str,
        old_str: str,
        new_str: str,
        *,
        replace_all: bool = False,
        description: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "path": path,
            "old_str": old_str,
            "new_str": new_str,
            "replace_all": replace_all,
        }
        if description is not None:
            body["description"] = description
        return self._request("POST", f"/v1/sandboxes/{session_id}/files/str-replace", json=body)

    def ls(self, session_id: str, *, path: str = "/") -> dict:
        body: dict[str, Any] = {"path": path}
        return self._request("POST", f"/v1/sandboxes/{session_id}/files/ls", json=body)

    def glob(self, session_id: str, pattern: str, *, path: str = "/") -> dict:
        body: dict[str, Any] = {"pattern": pattern, "path": path}
        return self._request("POST", f"/v1/sandboxes/{session_id}/files/glob", json=body)

    def grep(
        self,
        session_id: str,
        pattern: str,
        *,
        path: str = "/",
        glob: str | None = None,
        max_matches: int = 500,
    ) -> dict:
        body: dict[str, Any] = {"pattern": pattern, "path": path, "max_matches": max_matches}
        if glob is not None:
            body["glob"] = glob
        return self._request("POST", f"/v1/sandboxes/{session_id}/files/grep", json=body)

    def get_log(self, session_id: str, *, limit: int | None = None, offset: int | None = None) -> dict:
        """GET /v1/sandboxes/{session_id}/log -- paginated exec/file-op audit
        history (`docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3).

        Args:
            limit: Maximum number of entries to return.
            offset: Number of entries to skip, oldest-first.
        """
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self._request("GET", f"/v1/sandboxes/{session_id}/log", params=params)

    def start_process(
        self,
        session_id: str,
        command: str,
        *,
        description: str | None = None,
        max_runtime_seconds: int = 3600,
    ) -> dict:
        """POST /v1/sandboxes/{session_id}/processes -- start a background
        process that keeps running after this call returns.

        Distinct from `exec()`: `exec()` is one-shot request/response,
        bounded by its own `timeout`. Poll the returned `process_id`'s
        output with `get_process_output()`, feed it input with
        `send_process_input()`, and stop it with `stop_process()`. See
        `docs/PROCESS-SESSIONS-DESIGN.md`.
        """
        body: dict[str, Any] = {"command": command, "max_runtime_seconds": max_runtime_seconds}
        if description is not None:
            body["description"] = description
        return self._request("POST", f"/v1/sandboxes/{session_id}/processes", json=body)

    def list_processes(self, session_id: str) -> dict:
        """GET /v1/sandboxes/{session_id}/processes -- every background
        process currently tracked for this session."""
        return self._request("GET", f"/v1/sandboxes/{session_id}/processes")

    def get_process_output(self, session_id: str, process_id: str, *, since_offset: int = 0) -> dict:
        """GET /v1/sandboxes/{session_id}/processes/{process_id}/output --
        poll a background process's output since a given byte offset.

        Polling-style, not streaming. `since_offset` (from a previous call's
        `next_offset`, or 0 the first time) lets you fetch only the new
        output since your last check.
        """
        params: dict[str, Any] = {"since_offset": since_offset}
        return self._request(
            "GET", f"/v1/sandboxes/{session_id}/processes/{process_id}/output", params=params
        )

    def stream_process_output(
        self, session_id: str, process_id: str, *, since_offset: int = 0
    ) -> Iterator[dict]:
        """GET /v1/sandboxes/{session_id}/processes/{process_id}/stream -- live
        SSE stream of a background process's stdout, the streaming counterpart
        to `get_process_output`'s polling. Yields one dict per event:
        `{"type": "output", "stdout_chunk", "next_offset", "truncated"}` for
        each new chunk, then a final `{"type": "exit", "status", "exit_code"}`
        when the process finishes.

        Blocks the calling thread while the stream is open; iterate it from a
        dedicated thread if you need this alongside other work.
        """
        params: dict[str, Any] = {"since_offset": since_offset}
        with self._http.stream(
            "GET",
            f"/v1/sandboxes/{session_id}/processes/{process_id}/stream",
            params=params,
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for_error(resp)
            yield from _iter_sse_events(resp.iter_lines())

    def send_process_input(self, session_id: str, process_id: str, data: str) -> dict:
        """POST /v1/sandboxes/{session_id}/processes/{process_id}/input --
        write to a tracked background process's stdin pipe."""
        return self._request(
            "POST",
            f"/v1/sandboxes/{session_id}/processes/{process_id}/input",
            json={"data": data},
        )

    def stop_process(self, session_id: str, process_id: str) -> dict:
        """POST /v1/sandboxes/{session_id}/processes/{process_id}/stop --
        stop a tracked background process (SIGTERM, then SIGKILL if it
        doesn't exit within a short grace period)."""
        return self._request("POST", f"/v1/sandboxes/{session_id}/processes/{process_id}/stop")

    def watch(self, session_id: str) -> Iterator[dict]:
        """GET /v1/sandboxes/{session_id}/watch -- streams new audit-log
        entries as they're written, one dict per Server-Sent Event `data:`
        line. This is a live feed of exec/file operations as control-plane
        logs them, not a live terminal -- see
        `docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 2 ("Live watch").

        Blocks the calling thread for as long as the stream stays open;
        iterate it from a dedicated thread if you need this alongside other
        work.
        """
        with self._http.stream("GET", f"/v1/sandboxes/{session_id}/watch") as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for_error(resp)
            yield from _iter_sse_events(resp.iter_lines())

    def takeover(self, session_id: str) -> websockets.sync.client.ClientConnection:
        """WS /v1/sandboxes/{session_id}/takeover -- interactive human
        takeover of a sandbox session's shell: a raw duplex byte stream
        proxied straight through to the sandbox's PTY, exactly as described
        in `docs/API.md`. There is no message envelope -- send and receive
        raw bytes on the returned connection exactly as you would over a
        local terminal.

        Returns a `websockets.sync.client.ClientConnection`. Use it as a
        context manager (``with client.takeover(session_id) as ws:``), or
        call `.send(data)` / `.recv()` / iterate it directly for incoming
        bytes. A missing/invalid API key closes the connection with WS close
        code 4401; an unowned or already-destroyed session_id closes it with
        4404 -- both surface as `websockets.exceptions.ConnectionClosed` (see
        its `.code` / `.reason`) the first time you `.send()` or `.recv()`,
        since the close happens after the opening handshake completes (see
        `docs/API.md`'s `WS .../takeover` section for why).
        """
        url = _to_ws_url(self._base_url, f"/v1/sandboxes/{session_id}/takeover")
        return self._ws_connect(url, additional_headers={"Authorization": f"Bearer {self._api_key}"})

    def desktop_takeover(self, session_id: str) -> websockets.sync.client.ClientConnection:
        """WS /v1/sandboxes/{session_id}/desktop -- interactive GUI/remote-desktop
        human takeover of a sandbox session (VNC over a raw byte stream,
        proxied straight through to the sidecar's `WS /desktop`), structurally
        identical to `takeover` but bridging a full desktop instead of a shell.
        See `docs/API.md`'s `WS .../desktop` section and `SECURITY.md`'s "New
        trust boundary: remote desktop takeover" section.

        Reuses `takeover`'s `can_initiate_takeover` RBAC gate as-is (an
        "admin"-role API key only; a "member"-role key closes the connection
        with code 4403) -- there is no dedicated `can_initiate_desktop`
        permission yet, and no `read_only` variant of this connection.
        4401/4404 close-code semantics are identical to `takeover`. 404s
        (returned as a normal WS close, not an HTTP response) when this
        deployment has not set `BOXKITE_DESKTOP_ENABLED`.

        Returns a `websockets.sync.client.ClientConnection`. Use it as a
        context manager, or call `.send(data)` / `.recv()` / iterate it
        directly for incoming bytes, exactly as with `takeover`.
        """
        url = _to_ws_url(self._base_url, f"/v1/sandboxes/{session_id}/desktop")
        return self._ws_connect(url, additional_headers={"Authorization": f"Bearer {self._api_key}"})

    def get_allowed_commands(self) -> dict:
        """GET /v1/account/allowed-commands -- current per-account command allowlist.

        Returns the raw ``{"rules": [...]}`` body. Each rule is either a plain
        command string (e.g. ``"git"``) or an object of the form
        ``{"command": str, "args_allow": [regex, ...]?, "args_deny": [regex, ...]?}``,
        where ``args_allow``/``args_deny`` are regexes matched against the
        command's argument string.

        This allowlist is an opt-in guardrail for narrowing what a sandbox's
        agent is permitted to run -- it is not a sandbox-escape boundary and
        does not substitute for the sandbox's own isolation.
        """
        return self._request("GET", "/v1/account/allowed-commands")

    def set_allowed_commands(self, rules: list) -> dict:
        """PUT /v1/account/allowed-commands -- replace the per-account command allowlist.

        Args:
            rules: List of rules, each either a plain command string or an
                object ``{"command": str, "args_allow": [regex, ...]?, "args_deny": [regex, ...]?}``.
                Replaces the existing rule set entirely.

        This allowlist is an opt-in guardrail, not a sandbox-escape boundary.
        """
        return self._request("PUT", "/v1/account/allowed-commands", json={"rules": rules})

    def clear_allowed_commands(self) -> None:
        """DELETE /v1/account/allowed-commands -- remove the per-account command allowlist."""
        self._request("DELETE", "/v1/account/allowed-commands")

    def create_preview_url(self, session_id: str, port: int, *, ttl_seconds: int | None = None) -> dict:
        """POST /v1/sandboxes/{session_id}/preview/{port} -- mint a signed,
        time-limited URL that proxies HTTP traffic to a port a background
        process opened inside this session (see start_process's
        `expose_port`). The returned URL carries its own authorization -- no
        API key is required to use it, only to mint it (docs/API.md's
        "Network ingress preview URLs" section).

        Args:
            port: The port a background process opened inside the sandbox.
            ttl_seconds: How long the minted URL stays valid, in seconds
                (30-86400). Defaults to 900 (15 minutes) server-side when
                omitted.

        Returns the raw ``{"url", "expires_at", "token_id"}`` body. Save
        `token_id` if you might need to revoke this one link early via
        `revoke_preview_url()`, without tearing down the session or affecting
        any other preview token minted for the same session/port.
        """
        body: dict[str, Any] = {}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return self._request("POST", f"/v1/sandboxes/{session_id}/preview/{port}", json=body)

    def revoke_preview_url(self, session_id: str, port: int, token_id: str) -> dict:
        """POST /v1/sandboxes/{session_id}/preview/{port}/revoke -- invalidate
        one specific preview-URL token (its `token_id` from
        `create_preview_url()`) before its TTL expires, without tearing down
        the sandbox session and without affecting any other preview token
        minted for the same session/port.

        Idempotent: revoking an already-revoked, already-expired, or
        unrecognized `token_id` still returns ``{"revoked": True, ...}``
        rather than raising -- the caller cannot distinguish "this token
        never existed" from "someone already revoked it".
        """
        return self._request(
            "POST", f"/v1/sandboxes/{session_id}/preview/{port}/revoke", json={"token_id": token_id}
        )

    def sandbox(
        self,
        *,
        label: str | None = None,
        secret_names: list[str] | None = None,
        mcp_connection_names: list[str] | None = None,
    ) -> SandboxSession:
        """Context-manager convenience: creates on enter, destroys on exit
        (even on exception) -- ``with client.sandbox() as sb: sb.exec(...)``."""
        return SandboxSession(
            self, label=label, secret_names=secret_names, mcp_connection_names=mcp_connection_names
        )


class SandboxSession:
    def __init__(
        self,
        client: BoxkiteClient,
        *,
        label: str | None = None,
        secret_names: list[str] | None = None,
        mcp_connection_names: list[str] | None = None,
    ) -> None:
        self._client = client
        self._label = label
        self._secret_names = secret_names
        self._mcp_connection_names = mcp_connection_names
        self.id: str | None = None

    def __enter__(self) -> SandboxSession:
        result = self._client.create_sandbox(
            label=self._label,
            secret_names=self._secret_names,
            mcp_connection_names=self._mcp_connection_names,
        )
        self.id = result["id"]
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.id is not None:
            try:
                self._client.destroy_sandbox(self.id)
            except BoxkiteApiError:
                pass  # best-effort teardown -- an already-gone session shouldn't raise on cleanup

    def exec(self, command: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.exec(self.id, command, **kwargs)

    def http_request(self, method: str, url: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.http_request(self.id, method, url, **kwargs)

    def file_create(self, path: str, content: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.file_create(self.id, path, content, **kwargs)

    def lsp_start(self, language: str) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.lsp_start(self.id, language)

    def lsp_open(self, lsp_id: str, path: str, content: str) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.lsp_open(self.id, lsp_id, path, content)

    def lsp_completion(self, lsp_id: str, path: str, line: int, character: int) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.lsp_completion(self.id, lsp_id, path, line, character)

    def lsp_stop(self, lsp_id: str) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.lsp_stop(self.id, lsp_id)

    def view(self, path: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.view(self.id, path, **kwargs)

    def str_replace(self, path: str, old_str: str, new_str: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.str_replace(self.id, path, old_str, new_str, **kwargs)

    def ls(self, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.ls(self.id, **kwargs)

    def glob(self, pattern: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.glob(self.id, pattern, **kwargs)

    def grep(self, pattern: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.grep(self.id, pattern, **kwargs)

    def get_log(self, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.get_log(self.id, **kwargs)

    def watch(self) -> Iterator[dict]:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.watch(self.id)

    def start_process(self, command: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.start_process(self.id, command, **kwargs)

    def list_processes(self) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.list_processes(self.id)

    def get_process_output(self, process_id: str, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.get_process_output(self.id, process_id, **kwargs)

    def send_process_input(self, process_id: str, data: str) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.send_process_input(self.id, process_id, data)

    def stop_process(self, process_id: str) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.stop_process(self.id, process_id)

    def takeover(self) -> websockets.sync.client.ClientConnection:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.takeover(self.id)

    def desktop_takeover(self) -> websockets.sync.client.ClientConnection:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.desktop_takeover(self.id)

    def create_preview_url(self, port: int, **kwargs: Any) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.create_preview_url(self.id, port, **kwargs)

    def revoke_preview_url(self, port: int, token_id: str) -> dict:
        assert self.id is not None, "SandboxSession must be used as a context manager"
        return self._client.revoke_preview_url(self.id, port, token_id)


class AsyncBoxkiteClient:
    """Async counterpart of BoxkiteClient, same method shapes throughout."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retry: RetryConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_connect: Callable[..., Any] | None = None,
    ) -> None:
        _validate_base_url_scheme(base_url)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._retry = retry
        self._ws_connect = ws_connect or _default_async_ws_connect
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncBoxkiteClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if _should_retry(self._retry, method, attempt, None):
                    await asyncio.sleep(_retry_delay(self._retry, attempt, None))
                    attempt += 1
                    continue
                raise BoxkiteConnectionError(str(exc)) from exc
            if _should_retry(self._retry, method, attempt, resp.status_code):
                await asyncio.sleep(_retry_delay(self._retry, attempt, _retry_after_from(self._retry, resp)))
                attempt += 1
                continue
            _raise_for_error(resp)
            return resp.json() if resp.content else None

    async def account(self) -> dict:
        return await self._request("GET", "/v1/account")

    async def usage(self) -> dict:
        return await self._request("GET", "/v1/usage")

    async def request_password_reset(self, email: str) -> dict:
        """POST /v1/auth/password-reset/request -- async counterpart of
        `BoxkiteClient.request_password_reset`. See that docstring."""
        return await self._request("POST", "/v1/auth/password-reset/request", json={"email": email})

    async def confirm_password_reset(self, token: str, new_password: str) -> dict:
        """POST /v1/auth/password-reset/confirm -- async counterpart of
        `BoxkiteClient.confirm_password_reset`. See that docstring."""
        return await self._request(
            "POST",
            "/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": new_password},
        )

    async def verify_email(self, token: str) -> dict:
        """POST /v1/auth/verify-email -- async counterpart of
        `BoxkiteClient.verify_email`. See that docstring."""
        return await self._request("POST", "/v1/auth/verify-email", json={"token": token})

    async def resend_verification(self, access_token: str) -> dict:
        """POST /v1/auth/resend-verification -- async counterpart of
        `BoxkiteClient.resend_verification`. See that docstring for why the
        JWT is passed explicitly instead of using self._api_key."""
        return await self._request(
            "POST",
            "/v1/auth/resend-verification",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def refresh_token(self, refresh_token: str) -> dict:
        """POST /v1/auth/refresh -- async counterpart of
        `BoxkiteClient.refresh_token`. See that docstring."""
        return await self._request("POST", "/v1/auth/refresh", json={"refresh_token": refresh_token})

    async def logout(self, refresh_token: str) -> None:
        """POST /v1/auth/logout -- async counterpart of
        `BoxkiteClient.logout`. See that docstring."""
        await self._request("POST", "/v1/auth/logout", json={"refresh_token": refresh_token})

    async def create_sandbox(
        self,
        *,
        label: str | None = None,
        size: str | None = None,
        storage_gb: float | None = None,
        lifetime_minutes: int | None = None,
        count: int | None = None,
        secret_names: list[str] | None = None,
        image_id: str | None = None,
        mcp_connection_names: list[str] | None = None,
        volume_mounts: dict[str, str] | None = None,
        gpu_count: int | None = None,
    ) -> dict:
        """POST /v1/sandboxes -- create a new sandbox.

        Args:
            label: Optional human-readable label for the sandbox.
            size: Sandbox size -- one of "small", "medium", "large". Defaults
                to "small" server-side when omitted.
            storage_gb: Requested persistent storage in GB.
            lifetime_minutes: Maximum lifetime of the sandbox in minutes before
                automatic teardown.
            count: Number of sandboxes to create in this request.
            secret_names: Names of this account's secrets (see POST /v1/secrets)
                this session should be granted access to via the sidecar's
                secrets-broker http_request tool (docs/SECRETS-DESIGN.md). A
                name that doesn't exist for this account 404s before any
                sandbox is created.
            image_id: Id of a completed custom image built via create_image().
                If given, the sandbox uses that digest-pinned image instead of
                the operator's default. 404s if not owned by this account or
                not yet status "completed".
            mcp_connection_names: Labels of this account's outbound-MCP
                connections (see create_mcp_connection(), GitHub issues
                #116/#117) this session should be granted network egress to.
                A name that doesn't exist for this account 404s before any
                sandbox is created, same precedent as secret_names. This only
                widens the session's per-pod NetworkPolicy egress allowlist --
                there is no MCP-proxy transport yet
                (docs/OUTBOUND-MCP-DESIGN.md section 6), so a granted
                connection does not yet let an agent actually speak MCP to it.
            volume_mounts: Optional {volume_id: mount_path} mapping of
                independent PVC-backed volumes (see create_volume()) to mount
                into this sandbox. Every volume_id must already exist for this
                account and be status "ready" -- 404s otherwise. mount_path
                must be an absolute path outside the sandbox's typed roots
                (/workspace, /mnt/*, /tmp).
            gpu_count: Opt-in, experimental (docs/GPU-SUPPORT-SCOPING.md) --
                requests this many GPUs as a Kubernetes extended-resource
                limit. 422s (gpu_support_disabled) unless the deployment has
                BOXKITE_GPU_ENABLED set and a GPU-equipped node pool with a
                device plugin provisioned; not verified against real GPU
                hardware in this codebase.
        """
        body: dict[str, Any] = {}
        if label:
            body["label"] = label
        if size is not None:
            body["size"] = size
        if storage_gb is not None:
            body["storage_gb"] = storage_gb
        if lifetime_minutes is not None:
            body["lifetime_minutes"] = lifetime_minutes
        if count is not None:
            body["count"] = count
        if secret_names is not None:
            body["secret_names"] = secret_names
        if image_id is not None:
            body["image_id"] = image_id
        if mcp_connection_names is not None:
            body["mcp_connection_names"] = mcp_connection_names
        if volume_mounts is not None:
            body["volume_mounts"] = volume_mounts
        if gpu_count is not None:
            body["gpu_count"] = gpu_count
        return await self._request("POST", "/v1/sandboxes", json=body)

    async def get_sandbox(self, session_id: str) -> dict:
        return await self._request("GET", f"/v1/sandboxes/{session_id}")

    async def list_sandboxes(self, *, active_only: bool = False) -> list[dict]:
        result = await self._request("GET", "/v1/sandboxes", params={"active_only": str(active_only).lower()})
        return result or []

    async def destroy_sandbox(self, session_id: str) -> None:
        await self._request("DELETE", f"/v1/sandboxes/{session_id}")

    async def create_image(
        self,
        *,
        label: str | None = None,
        base: str = "boxkite-default",
        python_packages: list[str] | None = None,
        apt_packages: list[str] | None = None,
        npm_packages: list[str] | None = None,
    ) -> dict:
        """POST /v1/images -- build a custom sandbox image.

        Args:
            label: Optional human-readable label for the image.
            base: Base image to build from -- one of "boxkite-default",
                "boxkite-minimal", "boxkite-node", "boxkite-go",
                "boxkite-nextjs", "boxkite-rust". Defaults to
                "boxkite-default". "boxkite-node" drops Python entirely (no
                python_packages installable, only apt_packages/npm_packages).
                "boxkite-go" and "boxkite-rust" both drop Python and Node
                entirely (no python_packages or npm_packages installable,
                only apt_packages). "boxkite-nextjs" is the same Node-only
                runtime as "boxkite-node" plus a pre-installed Next.js App
                Router starter vendored at /opt/nextjs-template (same
                python_packages restriction as "boxkite-node").
            python_packages: Exact-version-pinned packages ("name==version",
                no ranges) to install into the image.
            apt_packages: Exact-version-pinned apt packages ("name==version",
                no ranges) to install into the image.
            npm_packages: Exact-version-pinned npm packages ("name==version",
                no ranges), e.g. "typescript==5.6.0", to install into the
                image. Not supported on base="boxkite-go" or
                base="boxkite-rust".
        """
        body: dict[str, Any] = {}
        if label:
            body["label"] = label
        if base is not None:
            body["base"] = base
        if python_packages is not None:
            body["python_packages"] = python_packages
        if apt_packages is not None:
            body["apt_packages"] = apt_packages
        if npm_packages is not None:
            body["npm_packages"] = npm_packages
        return await self._request("POST", "/v1/images", json=body)

    async def get_image(self, image_id: str) -> dict:
        return await self._request("GET", f"/v1/images/{image_id}")

    async def list_images(self) -> list[dict]:
        result = await self._request("GET", "/v1/images")
        return result or []

    async def delete_image(self, image_id: str) -> None:
        await self._request("DELETE", f"/v1/images/{image_id}")

    async def create_volume(self, *, label: str | None = None, size_gb: float) -> dict:
        """POST /v1/volumes -- async counterpart of
        `BoxkiteClient.create_volume`. See that docstring."""
        body: dict[str, Any] = {"size_gb": size_gb}
        if label:
            body["label"] = label
        return await self._request("POST", "/v1/volumes", json=body)

    async def get_volume(self, volume_id: str) -> dict:
        return await self._request("GET", f"/v1/volumes/{volume_id}")

    async def list_volumes(self) -> list[dict]:
        result = await self._request("GET", "/v1/volumes")
        return result or []

    async def delete_volume(self, volume_id: str) -> None:
        await self._request("DELETE", f"/v1/volumes/{volume_id}")

    async def create_webhook(
        self,
        *,
        url: str,
        event_types: list[str],
        description: str | None = None,
    ) -> dict:
        """POST /v1/webhooks -- async counterpart of
        `BoxkiteClient.create_webhook`. See that docstring."""
        body: dict[str, Any] = {"url": url, "event_types": event_types}
        if description is not None:
            body["description"] = description
        return await self._request("POST", "/v1/webhooks", json=body)

    async def list_webhooks(self) -> list[dict]:
        """GET /v1/webhooks -- webhook subscriptions for this account. The
        signing secret is never returned here."""
        result = await self._request("GET", "/v1/webhooks")
        return result or []

    async def delete_webhook(self, subscription_id: str) -> None:
        """DELETE /v1/webhooks/{id} -- delete a webhook subscription owned
        by this account. 404s if already gone or never owned by this
        account."""
        await self._request("DELETE", f"/v1/webhooks/{subscription_id}")

    async def list_webhook_deliveries(
        self, subscription_id: str, *, limit: int | None = None, offset: int | None = None
    ) -> list[dict]:
        """GET /v1/webhooks/{id}/deliveries -- async counterpart of
        `BoxkiteClient.list_webhook_deliveries`. See that docstring."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        result = await self._request("GET", f"/v1/webhooks/{subscription_id}/deliveries", params=params)
        return result or []

    async def create_mcp_connection(self, *, label: str, catalog_id: str) -> dict:
        """POST /v1/mcp-connections -- async counterpart of
        `BoxkiteClient.create_mcp_connection`. See that docstring."""
        return await self._request(
            "POST", "/v1/mcp-connections", json={"label": label, "catalog_id": catalog_id}
        )

    async def list_mcp_connections(self) -> list[dict]:
        """GET /v1/mcp-connections -- outbound-MCP connection grants for
        this account."""
        result = await self._request("GET", "/v1/mcp-connections")
        return result or []

    async def delete_mcp_connection(self, connection_id: str) -> None:
        """DELETE /v1/mcp-connections/{id} -- delete an outbound-MCP
        connection grant owned by this account. 404s if already gone or
        never owned by this account."""
        await self._request("DELETE", f"/v1/mcp-connections/{connection_id}")

    async def create_secret(
        self,
        *,
        name: str,
        value: str,
        allowed_hosts: list[str],
        trust_tier: str | None = None,
    ) -> dict:
        """POST /v1/secrets -- async counterpart of
        `BoxkiteClient.create_secret`. See that docstring."""
        body: dict[str, Any] = {"name": name, "value": value, "allowed_hosts": allowed_hosts}
        if trust_tier is not None:
            body["trust_tier"] = trust_tier
        return await self._request("POST", "/v1/secrets", json=body)

    async def list_secrets(self) -> list[dict]:
        """GET /v1/secrets -- secrets registered for this account. Raw
        values are never returned here."""
        result = await self._request("GET", "/v1/secrets")
        return result or []

    async def delete_secret(self, secret_id: str) -> None:
        """DELETE /v1/secrets/{id} -- delete a secret owned by this
        account. 404s if already gone or never owned by this account."""
        await self._request("DELETE", f"/v1/secrets/{secret_id}")

    async def exec(
        self, session_id: str, command: str, *, timeout: int | None = None, description: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"command": command}
        if timeout is not None:
            body["timeout"] = timeout
        if description is not None:
            body["description"] = description
        kwargs: dict[str, Any] = {"json": body}
        if timeout is not None:
            kwargs["timeout"] = timeout + EXEC_TIMEOUT_HEADROOM
        return await self._request("POST", f"/v1/sandboxes/{session_id}/exec", **kwargs)

    async def http_request(
        self,
        session_id: str,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout: int | None = None,
    ) -> dict:
        """POST /v1/sandboxes/{id}/http-request -- the secrets-broker HTTP
        request (docs/SECRETS-DESIGN.md). `headers`/`body` may contain a
        literal `{{secret:name}}` reference for a secret granted to this
        session via `create_sandbox(secret_names=[...])`; the sidecar
        substitutes the real value in-process, this SDK/client never sees it.
        """
        payload: dict[str, Any] = {"method": method, "url": url}
        if headers is not None:
            payload["headers"] = headers
        if body is not None:
            payload["body"] = body
        if timeout is not None:
            payload["timeout"] = timeout
        kwargs: dict[str, Any] = {"json": payload}
        if timeout is not None:
            kwargs["timeout"] = timeout + EXEC_TIMEOUT_HEADROOM
        return await self._request("POST", f"/v1/sandboxes/{session_id}/http-request", **kwargs)

    async def file_create(self, session_id: str, path: str, content: str, *, description: str | None = None) -> dict:
        body: dict[str, Any] = {"path": path, "content": content}
        if description is not None:
            body["description"] = description
        return await self._request("POST", f"/v1/sandboxes/{session_id}/files", json=body)

    async def lsp_start(self, session_id: str, language: str) -> dict:
        """POST /v1/sandboxes/{id}/lsp/start -- starts a persistent language
        server (pyright for "python", typescript-language-server for
        "typescript"/"javascript"). Returns a dict with `lsp_id`, an opaque
        handle to pass to lsp_open/lsp_completion/lsp_stop."""
        return await self._request("POST", f"/v1/sandboxes/{session_id}/lsp/start", json={"language": language})

    async def lsp_open(self, session_id: str, lsp_id: str, path: str, content: str) -> dict:
        """POST /v1/sandboxes/{id}/lsp/{lsp_id}/open -- opens (or
        full-document-replaces) a document on a running language server."""
        return await self._request(
            "POST", f"/v1/sandboxes/{session_id}/lsp/{lsp_id}/open", json={"path": path, "content": content}
        )

    async def lsp_completion(self, session_id: str, lsp_id: str, path: str, line: int, character: int) -> dict:
        """POST /v1/sandboxes/{id}/lsp/{lsp_id}/completion -- requests
        completions at a 0-indexed (line, character) position. `path` must
        already be open on this handle (see lsp_open)."""
        return await self._request(
            "POST",
            f"/v1/sandboxes/{session_id}/lsp/{lsp_id}/completion",
            json={"path": path, "line": line, "character": character},
        )

    async def lsp_stop(self, session_id: str, lsp_id: str) -> dict:
        """POST /v1/sandboxes/{id}/lsp/{lsp_id}/stop -- gracefully shuts
        down a running language server."""
        return await self._request("POST", f"/v1/sandboxes/{session_id}/lsp/{lsp_id}/stop")

    async def view(
        self, session_id: str, path: str, *, view_range: list[int] | None = None, description: str | None = None
    ) -> dict:
        body: dict[str, Any] = {"path": path}
        if view_range is not None:
            body["view_range"] = view_range
        if description is not None:
            body["description"] = description
        return await self._request("POST", f"/v1/sandboxes/{session_id}/files/view", json=body)

    async def str_replace(
        self,
        session_id: str,
        path: str,
        old_str: str,
        new_str: str,
        *,
        replace_all: bool = False,
        description: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "path": path,
            "old_str": old_str,
            "new_str": new_str,
            "replace_all": replace_all,
        }
        if description is not None:
            body["description"] = description
        return await self._request("POST", f"/v1/sandboxes/{session_id}/files/str-replace", json=body)

    async def ls(self, session_id: str, *, path: str = "/") -> dict:
        body: dict[str, Any] = {"path": path}
        return await self._request("POST", f"/v1/sandboxes/{session_id}/files/ls", json=body)

    async def glob(self, session_id: str, pattern: str, *, path: str = "/") -> dict:
        body: dict[str, Any] = {"pattern": pattern, "path": path}
        return await self._request("POST", f"/v1/sandboxes/{session_id}/files/glob", json=body)

    async def grep(
        self,
        session_id: str,
        pattern: str,
        *,
        path: str = "/",
        glob: str | None = None,
        max_matches: int = 500,
    ) -> dict:
        body: dict[str, Any] = {"pattern": pattern, "path": path, "max_matches": max_matches}
        if glob is not None:
            body["glob"] = glob
        return await self._request("POST", f"/v1/sandboxes/{session_id}/files/grep", json=body)

    async def get_log(self, session_id: str, *, limit: int | None = None, offset: int | None = None) -> dict:
        """GET /v1/sandboxes/{session_id}/log -- paginated exec/file-op audit
        history (`docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 3)."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return await self._request("GET", f"/v1/sandboxes/{session_id}/log", params=params)

    async def start_process(
        self,
        session_id: str,
        command: str,
        *,
        description: str | None = None,
        max_runtime_seconds: int = 3600,
    ) -> dict:
        """POST /v1/sandboxes/{session_id}/processes -- async counterpart of
        `BoxkiteClient.start_process`. See that docstring."""
        body: dict[str, Any] = {"command": command, "max_runtime_seconds": max_runtime_seconds}
        if description is not None:
            body["description"] = description
        return await self._request("POST", f"/v1/sandboxes/{session_id}/processes", json=body)

    async def list_processes(self, session_id: str) -> dict:
        """GET /v1/sandboxes/{session_id}/processes -- every background
        process currently tracked for this session."""
        return await self._request("GET", f"/v1/sandboxes/{session_id}/processes")

    async def get_process_output(self, session_id: str, process_id: str, *, since_offset: int = 0) -> dict:
        """GET /v1/sandboxes/{session_id}/processes/{process_id}/output --
        async counterpart of `BoxkiteClient.get_process_output`."""
        params: dict[str, Any] = {"since_offset": since_offset}
        return await self._request(
            "GET", f"/v1/sandboxes/{session_id}/processes/{process_id}/output", params=params
        )

    async def send_process_input(self, session_id: str, process_id: str, data: str) -> dict:
        """POST /v1/sandboxes/{session_id}/processes/{process_id}/input --
        write to a tracked background process's stdin pipe."""
        return await self._request(
            "POST",
            f"/v1/sandboxes/{session_id}/processes/{process_id}/input",
            json={"data": data},
        )

    async def stop_process(self, session_id: str, process_id: str) -> dict:
        """POST /v1/sandboxes/{session_id}/processes/{process_id}/stop --
        stop a tracked background process."""
        return await self._request("POST", f"/v1/sandboxes/{session_id}/processes/{process_id}/stop")

    async def stream_process_output(
        self, session_id: str, process_id: str, *, since_offset: int = 0
    ) -> AsyncIterator[dict]:
        """GET /v1/sandboxes/{session_id}/processes/{process_id}/stream -- async
        counterpart of `BoxkiteClient.stream_process_output`. See that
        docstring for the event contract."""
        params: dict[str, Any] = {"since_offset": since_offset}
        async with self._http.stream(
            "GET",
            f"/v1/sandboxes/{session_id}/processes/{process_id}/stream",
            params=params,
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                _raise_for_error(resp)
            async for event in _aiter_sse_events(resp.aiter_lines()):
                yield event

    async def watch(self, session_id: str) -> AsyncIterator[dict]:
        """GET /v1/sandboxes/{session_id}/watch -- async counterpart of
        `BoxkiteClient.watch`. See that docstring for the SSE contract."""
        async with self._http.stream("GET", f"/v1/sandboxes/{session_id}/watch") as resp:
            if resp.status_code >= 400:
                await resp.aread()
                _raise_for_error(resp)
            async for entry in _aiter_sse_events(resp.aiter_lines()):
                yield entry

    async def takeover(self, session_id: str) -> Any:
        """WS /v1/sandboxes/{session_id}/takeover -- async counterpart of
        `BoxkiteClient.takeover`. See that docstring for the raw-byte
        duplex-stream contract and the 4401/4404 close-code semantics.
        Returns an (already-connected) `websockets.asyncio.client.ClientConnection`.
        """
        url = _to_ws_url(self._base_url, f"/v1/sandboxes/{session_id}/takeover")
        return await self._ws_connect(url, additional_headers={"Authorization": f"Bearer {self._api_key}"})

    async def desktop_takeover(self, session_id: str) -> Any:
        """WS /v1/sandboxes/{session_id}/desktop -- async counterpart of
        `BoxkiteClient.desktop_takeover`. See that docstring for the RBAC
        reuse, 4401/4403/4404 close-code semantics, and lack of a
        `read_only` variant. Returns an (already-connected)
        `websockets.asyncio.client.ClientConnection`.
        """
        url = _to_ws_url(self._base_url, f"/v1/sandboxes/{session_id}/desktop")
        return await self._ws_connect(url, additional_headers={"Authorization": f"Bearer {self._api_key}"})

    async def get_allowed_commands(self) -> dict:
        """GET /v1/account/allowed-commands -- current per-account command allowlist.

        Returns the raw ``{"rules": [...]}`` body. Each rule is either a plain
        command string (e.g. ``"git"``) or an object of the form
        ``{"command": str, "args_allow": [regex, ...]?, "args_deny": [regex, ...]?}``,
        where ``args_allow``/``args_deny`` are regexes matched against the
        command's argument string.

        This allowlist is an opt-in guardrail for narrowing what a sandbox's
        agent is permitted to run -- it is not a sandbox-escape boundary and
        does not substitute for the sandbox's own isolation.
        """
        return await self._request("GET", "/v1/account/allowed-commands")

    async def set_allowed_commands(self, rules: list) -> dict:
        """PUT /v1/account/allowed-commands -- replace the per-account command allowlist.

        Args:
            rules: List of rules, each either a plain command string or an
                object ``{"command": str, "args_allow": [regex, ...]?, "args_deny": [regex, ...]?}``.
                Replaces the existing rule set entirely.

        This allowlist is an opt-in guardrail, not a sandbox-escape boundary.
        """
        return await self._request("PUT", "/v1/account/allowed-commands", json={"rules": rules})

    async def clear_allowed_commands(self) -> None:
        """DELETE /v1/account/allowed-commands -- remove the per-account command allowlist."""
        await self._request("DELETE", "/v1/account/allowed-commands")

    async def create_preview_url(self, session_id: str, port: int, *, ttl_seconds: int | None = None) -> dict:
        """POST /v1/sandboxes/{session_id}/preview/{port} -- async counterpart
        of `BoxkiteClient.create_preview_url`. See that docstring."""
        body: dict[str, Any] = {}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return await self._request("POST", f"/v1/sandboxes/{session_id}/preview/{port}", json=body)

    async def revoke_preview_url(self, session_id: str, port: int, token_id: str) -> dict:
        """POST /v1/sandboxes/{session_id}/preview/{port}/revoke -- async
        counterpart of `BoxkiteClient.revoke_preview_url`. See that
        docstring."""
        return await self._request(
            "POST", f"/v1/sandboxes/{session_id}/preview/{port}/revoke", json={"token_id": token_id}
        )

    def sandbox(
        self,
        *,
        label: str | None = None,
        secret_names: list[str] | None = None,
        mcp_connection_names: list[str] | None = None,
    ) -> AsyncSandboxSession:
        return AsyncSandboxSession(
            self, label=label, secret_names=secret_names, mcp_connection_names=mcp_connection_names
        )


class AsyncSandboxSession:
    def __init__(
        self,
        client: AsyncBoxkiteClient,
        *,
        label: str | None = None,
        secret_names: list[str] | None = None,
        mcp_connection_names: list[str] | None = None,
    ) -> None:
        self._client = client
        self._label = label
        self._secret_names = secret_names
        self._mcp_connection_names = mcp_connection_names
        self.id: str | None = None

    async def __aenter__(self) -> AsyncSandboxSession:
        result = await self._client.create_sandbox(
            label=self._label,
            secret_names=self._secret_names,
            mcp_connection_names=self._mcp_connection_names,
        )
        self.id = result["id"]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self.id is not None:
            try:
                await self._client.destroy_sandbox(self.id)
            except BoxkiteApiError:
                pass

    async def exec(self, command: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.exec(self.id, command, **kwargs)

    async def http_request(self, method: str, url: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.http_request(self.id, method, url, **kwargs)

    async def file_create(self, path: str, content: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.file_create(self.id, path, content, **kwargs)

    async def lsp_start(self, language: str) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.lsp_start(self.id, language)

    async def lsp_open(self, lsp_id: str, path: str, content: str) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.lsp_open(self.id, lsp_id, path, content)

    async def lsp_completion(self, lsp_id: str, path: str, line: int, character: int) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.lsp_completion(self.id, lsp_id, path, line, character)

    async def lsp_stop(self, lsp_id: str) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.lsp_stop(self.id, lsp_id)

    async def view(self, path: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.view(self.id, path, **kwargs)

    async def str_replace(self, path: str, old_str: str, new_str: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.str_replace(self.id, path, old_str, new_str, **kwargs)

    async def ls(self, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.ls(self.id, **kwargs)

    async def glob(self, pattern: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.glob(self.id, pattern, **kwargs)

    async def grep(self, pattern: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.grep(self.id, pattern, **kwargs)

    async def get_log(self, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.get_log(self.id, **kwargs)

    def watch(self) -> AsyncIterator[dict]:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return self._client.watch(self.id)

    async def start_process(self, command: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.start_process(self.id, command, **kwargs)

    async def list_processes(self) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.list_processes(self.id)

    async def get_process_output(self, process_id: str, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.get_process_output(self.id, process_id, **kwargs)

    async def send_process_input(self, process_id: str, data: str) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.send_process_input(self.id, process_id, data)

    async def stop_process(self, process_id: str) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.stop_process(self.id, process_id)

    async def takeover(self) -> Any:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.takeover(self.id)

    async def desktop_takeover(self) -> Any:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.desktop_takeover(self.id)

    async def create_preview_url(self, port: int, **kwargs: Any) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.create_preview_url(self.id, port, **kwargs)

    async def revoke_preview_url(self, port: int, token_id: str) -> dict:
        assert self.id is not None, "AsyncSandboxSession must be used as a context manager"
        return await self._client.revoke_preview_url(self.id, port, token_id)
