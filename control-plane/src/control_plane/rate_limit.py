"""Rate limiting for the control-plane's auth, sandbox, image, and snapshot
routes, to blunt brute-force, account-enumeration, and abuse-volume attempts.

Two backends, selected by `settings.BOXKITE_RATE_LIMIT_BACKEND`:

- "memory" (default): an in-memory, per-process sliding-window limiter (an
  `OrderedDict` of timestamps per key). Correct and fully self-contained for
  a SINGLE control-plane replica, but its state is NOT shared across
  replicas -- a multi-instance deployment silently enforces `limit *
  replica_count` cluster-wide instead of `limit`, since each replica
  enforces its own independent copy. Fine, and the simplest option, for
  single-instance/local-dev deployments.
- "postgres": a shared, cross-replica fixed-window counter backed by this
  service's own database (`models_orm.RateLimitWindow`) -- reuses
  DATABASE_URL, no new infra (e.g. Redis) to run. Any deployment running
  more than one control-plane replica MUST set
  `BOXKITE_RATE_LIMIT_BACKEND=postgres`; it is not auto-detected, since a
  single process has no way to know how many replicas exist.

Both backends are exposed through the same `enforce_rate_limit` entry point
used by every route -- callers don't need to know which backend is active.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque

from fastapi import HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy import text

from .config import settings
from .db import get_session_factory

_WINDOW_SECONDS = 60.0
_MAX_TRACKED_KEYS = 10_000

_hits: OrderedDict[str, deque[float]] = OrderedDict()


def _client_key(request: Request, *, bucket: str, subject: str | None = None) -> str:
    """Build the sliding-window key for this bucket.

    `subject` (e.g. an account id) is preferred when available — auth's
    signup/login are unauthenticated at the point they're called, so those
    fall back to the caller's IP; already-authenticated routes (e.g. sandbox
    exec/file ops) pass their account id so the limit tracks the account
    rather than a possibly-shared/NATed IP.
    """
    if subject is not None:
        return f"{bucket}:{subject}"
    client_host = request.client.host if request.client else "unknown"
    return f"{bucket}:{client_host}"


def _prune_and_count_in_memory(key: str, *, now: float) -> int:
    window = _hits.setdefault(key, deque())
    _hits.move_to_end(key)
    while window and now - window[0] > _WINDOW_SECONDS:
        window.popleft()
    while len(_hits) > _MAX_TRACKED_KEYS:
        _hits.popitem(last=False)
    return len(window)


class PostgresRateLimiter:
    """Shared, cross-replica rate limiter backed by the control-plane's own
    database (any dialect `db.py` supports -- Postgres in production,
    SQLite in tests/local dev).

    Uses a fixed window per key (`floor(now / window_seconds)`), a coarser
    granularity than the in-memory limiter's exact sliding window: a caller
    right at a window boundary can briefly see up to ~2x the configured
    limit. This is the standard, accepted tradeoff of fixed-window counters
    -- it defends against sustained brute-force/abuse volume across
    replicas, not exact-window precision, which is what this feature needs.
    """

    def __init__(self, session_factory: async_sessionmaker | None = None) -> None:
        self._session_factory = session_factory

    def _factory(self) -> async_sessionmaker:
        return self._session_factory or get_session_factory()

    async def hit_and_count(self, key: str, *, window_seconds: float = _WINDOW_SECONDS) -> int:
        """Record one hit for `key` in the current window and return the
        window's total count (including this hit)."""
        window_start = int(time.time() // window_seconds) * int(window_seconds)
        factory = self._factory()

        async with factory() as session:
            result = await session.execute(
                text(
                    "UPDATE rate_limit_windows SET count = count + 1 "
                    "WHERE key = :key AND window_start = :window_start "
                    "RETURNING count"
                ),
                {"key": key, "window_start": window_start},
            )
            row = result.first()
            if row is not None:
                await session.commit()
                return row[0]

        # No row for this (key, window_start) yet -- try to create it in a
        # fresh transaction so a concurrent insert from another replica
        # surfaces as an IntegrityError we can recover from, rather than
        # failing the request.
        async with factory() as session:
            try:
                await session.execute(
                    text(
                        "INSERT INTO rate_limit_windows (key, window_start, count) "
                        "VALUES (:key, :window_start, 1)"
                    ),
                    {"key": key, "window_start": window_start},
                )
                await session.commit()
                return 1
            except IntegrityError:
                await session.rollback()

        # Lost the insert race to another replica -- its row now exists, so
        # the increment succeeds this time.
        async with factory() as session:
            result = await session.execute(
                text(
                    "UPDATE rate_limit_windows SET count = count + 1 "
                    "WHERE key = :key AND window_start = :window_start "
                    "RETURNING count"
                ),
                {"key": key, "window_start": window_start},
            )
            row = result.first()
            await session.commit()
            return row[0] if row is not None else 1


_postgres_limiter = PostgresRateLimiter()


async def enforce_rate_limit(
    request: Request,
    *,
    bucket: str,
    subject: str | None = None,
    limit: int | None = None,
    response: Response | None = None,
) -> None:
    """Raise 429 if the caller has exceeded `limit` (default
    BOXKITE_AUTH_RATE_LIMIT_PER_MINUTE) requests to this bucket in the last
    60 seconds; otherwise record this request.

    Dispatches to the in-memory or Postgres-backed limiter per
    `settings.BOXKITE_RATE_LIMIT_BACKEND` -- see module docstring.

    When `response` is given, sets X-RateLimit-Limit/-Remaining on it so
    callers can back off intelligently instead of parsing 429 bodies. A 429
    also gets Retry-After, via HTTPException's own `headers` param since
    there's no successful Response to mutate at that point."""
    key = _client_key(request, bucket=bucket, subject=subject)
    max_allowed = limit if limit is not None else settings.BOXKITE_AUTH_RATE_LIMIT_PER_MINUTE

    if settings.BOXKITE_RATE_LIMIT_BACKEND == "postgres":
        count = await _postgres_limiter.hit_and_count(key)
        # The Postgres backend increments unconditionally (it can't cheaply
        # "peek" without an extra round trip), so a request that turns out
        # to be over the limit still recorded a hit -- consistent with the
        # in-memory backend's "record this request" happening after the
        # limit check succeeds, since either way the caller is being
        # rate-limited and the over-limit request itself counts against them.
        if count > max_allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests. Please wait a moment and try again.",
                    }
                },
                headers={
                    "X-RateLimit-Limit": str(max_allowed),
                    "X-RateLimit-Remaining": "0",
                    "Retry-After": str(int(_WINDOW_SECONDS)),
                },
            )
        if response is not None:
            remaining = max(0, max_allowed - count)
            response.headers["X-RateLimit-Limit"] = str(max_allowed)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        return

    now = time.monotonic()
    count = _prune_and_count_in_memory(key, now=now)
    if count >= max_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": {
                    "code": "rate_limited",
                    "message": "Too many requests. Please wait a moment and try again.",
                }
            },
            headers={
                "X-RateLimit-Limit": str(max_allowed),
                "X-RateLimit-Remaining": "0",
                "Retry-After": str(int(_WINDOW_SECONDS)),
            },
        )
    _hits[key].append(now)
    if response is not None:
        remaining = max(0, max_allowed - count - 1)
        response.headers["X-RateLimit-Limit"] = str(max_allowed)
        response.headers["X-RateLimit-Remaining"] = str(remaining)


def reset_rate_limits_for_tests() -> None:
    """Test-only helper to avoid cross-test bleed of the in-memory limiter state.

    The Postgres-backed limiter doesn't need this: each test already gets
    its own fresh SQLite file via the `db` fixture in conftest.py, so its
    `rate_limit_windows` table starts empty per test.
    """
    _hits.clear()
