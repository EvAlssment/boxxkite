"""Thin HTTP helpers for both modes.

Hosted responses use the control-plane's `{"error": {code, message}}`
envelope (see control-plane/src/control_plane/errors.py); local sidecar
responses use plain FastAPI `HTTPException` `{"detail": "..."}` bodies. Both
get translated into a `CliError` with a readable message rather than a raw
httpx exception or a JSON dump.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx

from .context import Context
from .errors import CliError

DEFAULT_TIMEOUT = 30.0


def _hosted_error_message(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return f"{err['message']} [{err.get('code', 'error')}]"
    return f"HTTP {resp.status_code}"


def _local_error_message(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return f"HTTP {resp.status_code}"


def hosted_request(
    ctx: Context,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict | list | None:
    url = f"{ctx.base_url}{path}"
    try:
        resp = httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {ctx.api_key}"},
            json=json,
            params=params,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise CliError(f"Could not reach {ctx.base_url}: {exc}") from exc

    if resp.status_code >= 400:
        raise CliError(_hosted_error_message(resp))
    return resp.json() if resp.content else None


def local_request(
    ctx: Context,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    url = f"{ctx.sidecar_url}{path}"
    try:
        resp = httpx.request(
            method,
            url,
            headers={"X-Sidecar-Auth-Token": ctx.sidecar_token},
            json=json,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise CliError(f"Could not reach local sidecar at {ctx.sidecar_url}: {exc}") from exc

    if resp.status_code >= 400:
        raise CliError(_local_error_message(resp))
    return resp.json()


def _iter_sse_events(lines: Iterator[str]) -> Iterator[dict]:
    """Parse a text/event-stream body into decoded JSON payloads. Only the
    `data:` field is used -- `boxkite watch` doesn't need `event:`/`id:`
    framing, just the ExecLogEntry payload each event carries. Mirrors
    sdk-python's `_iter_sse_events` (same wire format, same parser)."""
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


def hosted_stream_events(ctx: Context, method: str, path: str) -> Iterator[dict]:
    """Stream Server-Sent Events from a hosted control-plane endpoint (e.g.
    `.../watch`), yielding one decoded JSON payload per `data:` event.

    Deliberately no request timeout, unlike `hosted_request` -- a watch
    stream is expected to sit open and idle between events (the
    control-plane sends nothing while there's nothing new to report; see
    `docs/SANDBOX-OBSERVABILITY-DESIGN.md` section 2), so a finite read
    timeout would spuriously kill a healthy, quiet stream.
    """
    url = f"{ctx.base_url}{path}"
    try:
        with httpx.stream(method, url, headers={"Authorization": f"Bearer {ctx.api_key}"}, timeout=None) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise CliError(_hosted_error_message(resp))
            yield from _iter_sse_events(resp.iter_lines())
    except httpx.HTTPError as exc:
        raise CliError(f"Could not reach {ctx.base_url}: {exc}") from exc


def list_active_sandboxes(ctx: Context) -> list[dict]:
    result = hosted_request(ctx, "GET", "/v1/sandboxes", params={"active_only": "true"})
    return result or []  # type: ignore[return-value]


def resolve_session_id(ctx: Context, explicit: str | None) -> str:
    """Pick a session_id for `exec`/`files` commands in hosted mode.

    Never guesses across multiple active sessions — either the caller
    passes --session, or exactly one active session must exist.
    """
    if explicit:
        return explicit

    sessions = list_active_sandboxes(ctx)
    if len(sessions) == 1:
        return sessions[0]["id"]
    if not sessions:
        raise CliError("No active sandbox sessions. Run `boxkite session create` first, or pass --session <id>.")

    ids = ", ".join(session["id"] for session in sessions)
    raise CliError(f"Multiple active sandbox sessions ({ids}). Pass --session <id> to pick one.")
