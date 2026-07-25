"""Idempotency-Key support for creating (POST) endpoints — the Stripe pattern.

A client that sends `Idempotency-Key: <opaque>` on a POST can safely retry it
after a network blip: the first request's response is stored and replayed for
every subsequent request carrying the same key, so a retry never creates a
duplicate sandbox/image/volume/etc.

Implemented as a pure-ASGI middleware that is a strict no-op unless the request
is a POST *and* carries the header — ordinary traffic never reads the body,
never touches the database, and is completely unaffected.

Semantics (per key, scoped to caller identity + method + path):
- first request:        processed normally; response cached (status < 500 only)
- retry, completed:     the cached status/body is replayed verbatim
- retry, still running: 409 (the original is in flight; try again shortly)
- same key, different request body: 422 (a key must identify one request)
- original failed (>=500) or raised: the row is dropped so a retry re-runs

Only the status, body, and content-type are cached (not arbitrary response
headers). That covers this API's JSON responses; a response whose meaning lives
in an extra header would need that header added here before relying on replay.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.exc import IntegrityError

from .db import get_session_factory
from .models_orm import IdempotencyKey

_HEADER = b"idempotency-key"
# Responses at/above this status are not cached, so a client can retry through
# a transient server-side failure.
_UNCACHEABLE_STATUS = 500


def _header(scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value
    return None


async def _send_json_error(send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _replay(send, row: IdempotencyKey) -> None:
    body = row.response_body or b""
    media_type = (row.response_media_type or "application/json").encode()
    await send(
        {
            "type": "http.response.start",
            "status": row.response_status,
            "headers": [
                (b"content-type", media_type),
                (b"content-length", str(len(body)).encode()),
                (b"idempotent-replayed", b"true"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class IdempotencyMiddleware:
    """See module docstring. Add via `app.add_middleware(IdempotencyMiddleware)`."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        key = _header(scope, _HEADER)
        if not key:
            return await self.app(scope, receive, send)

        body = await _read_body(receive)
        auth = _header(scope, b"authorization") or b""
        scope_hash = hashlib.sha256(
            b"\0".join([key, auth, scope["method"].encode(), scope["path"].encode()])
        ).hexdigest()
        request_fingerprint = hashlib.sha256(body).hexdigest()

        session_factory = get_session_factory()

        # Claim the key: insert a pending row, or discover an existing one.
        async with session_factory() as db:
            existing = await db.get(IdempotencyKey, scope_hash)
            if existing is None:
                db.add(
                    IdempotencyKey(
                        scope_hash=scope_hash, request_fingerprint=request_fingerprint
                    )
                )
                try:
                    await db.commit()
                except IntegrityError:
                    # A concurrent request won the race and inserted first.
                    await db.rollback()
                    existing = await db.get(IdempotencyKey, scope_hash)

            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    return await _send_json_error(
                        send,
                        422,
                        "idempotency_key_reuse",
                        "This Idempotency-Key was already used with a different request body.",
                    )
                if existing.response_status is None:
                    return await _send_json_error(
                        send,
                        409,
                        "idempotency_key_in_progress",
                        "A request with this Idempotency-Key is still being processed.",
                    )
                return await _replay(send, existing)

        # We own the pending row: run the request, capturing the response.
        captured: dict = {"status": None, "media_type": None, "body": bytearray()}

        async def send_capture(message) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                for hk, hv in message.get("headers", []):
                    if hk.lower() == b"content-type":
                        captured["media_type"] = hv.decode()
            elif message["type"] == "http.response.body":
                captured["body"].extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, _replayable_receive(body), send_capture)
        except BaseException:
            await self._drop(session_factory, scope_hash)
            raise

        await self._finalize(session_factory, scope_hash, captured)

    async def _finalize(self, session_factory, scope_hash: str, captured: dict) -> None:
        status = captured["status"]
        if status is None or status >= _UNCACHEABLE_STATUS:
            await self._drop(session_factory, scope_hash)
            return
        async with session_factory() as db:
            row = await db.get(IdempotencyKey, scope_hash)
            if row is not None:
                row.response_status = status
                row.response_body = bytes(captured["body"])
                row.response_media_type = captured["media_type"]
                await db.commit()

    async def _drop(self, session_factory, scope_hash: str) -> None:
        async with session_factory() as db:
            row = await db.get(IdempotencyKey, scope_hash)
            if row is not None:
                await db.delete(row)
                await db.commit()


async def _read_body(receive) -> bytes:
    chunks = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.extend(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return bytes(chunks)


def _replayable_receive(body: bytes):
    """Return a receive() that hands the buffered body to the downstream app
    once, then reports disconnect (the body was already consumed upstream)."""
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive
