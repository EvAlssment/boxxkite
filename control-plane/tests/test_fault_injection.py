"""Chaos / fault-injection tests for a control-plane restart with live
sessions -- GitHub issue #151's third scenario (the other two, warm-pool
exhaustion and pod eviction mid-session, live in
tests/test_fault_injection.py in the root package, since they exercise
SandboxManager directly rather than this FastAPI app/DB).

Before writing these, the actual persistence story had to be checked rather
than assumed (per the issue's own framing -- "do webhook deliveries /
audit-log writes / rate-limit state silently drop"):

- Webhook deliveries (`WebhookDelivery` rows) are pure DB rows --
  `webhooks.enqueue_event` only ever writes a row, and `webhook_delivery.
  run_webhook_delivery_loop` is a stateless poller that re-reads "due"
  deliveries from the DB every cycle. A restart loses no delivery: a fresh
  process's poller finds exactly the same pending/retry-due rows a
  continuously-running one would have.
- Audit log entries (`ExecLogEntry` rows, docs/TAMPER-EVIDENT-AUDIT-DESIGN.md)
  are written synchronously in the same request that produced them
  (repository.py's `ExecLogEntryRepository.create`), hash-chained at write
  time -- also pure DB state, nothing in-memory to lose.
- Rate-limit state is the one exception, and deliberately so per
  rate_limit.py's own module docstring: the default "memory" backend is an
  in-process `OrderedDict`, explicitly documented as NOT shared across
  replicas or surviving a restart -- single-instance/local-dev only. The
  "postgres" backend (`BOXKITE_RATE_LIMIT_BACKEND=postgres`) exists
  specifically to make this durable/cross-replica when it matters.

So this file has one test confirming durability where it's actually meant
to exist (webhooks, audit log) and one test confirming the memory rate
limiter's KNOWN, DESIGNED gap is exactly what it claims to be -- documented
expected behavior, not a silently-introduced bug -- alongside the postgres
backend actually closing that gap. All three simulate "control-plane
restart" the same way: drop the module-level engine/session-factory
singletons in `db.py` (mirroring what a fresh process's global state would
be) without touching the underlying SQLite file, so any data still on disk
represents what would have survived a real pod restart against a
continuously-running Cloud SQL/Postgres instance.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from conftest import FakeSandboxManager, signup_and_get_api_key
from control_plane import db as db_module
from control_plane.audit_chain import verify_exec_log_chain
from control_plane.models_orm import Base, ExecLogEntry, WebhookDelivery
from control_plane.rate_limit import (
    PostgresRateLimiter,
    _hits,
    reset_rate_limits_for_tests,
)
from control_plane import webhook_delivery as webhook_delivery_module
from control_plane.webhook_delivery import _deliver_once, set_http_client_for_tests

pytestmark = pytest.mark.asyncio

_SAFE_DNS_IP = "93.184.216.34"


async def _register_webhook(client: httpx.AsyncClient, api_key: str, *, url: str) -> dict:
    resp = await client.post(
        "/v1/webhooks",
        json={"url": url, "event_types": ["sandbox.created", "sandbox.destroyed"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_sandbox(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _exec(client: httpx.AsyncClient, api_key: str, session_id: str, command: str) -> None:
    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": command},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200, resp.text


def _simulate_control_plane_restart() -> None:
    """Drop the process-local engine/session-factory singletons the same
    way a fresh process would start with none -- WITHOUT touching the
    underlying SQLite file, mirroring a pod restart against an
    always-on Postgres/Cloud SQL instance that survives the restart.
    Deliberately does not call `db_module.dispose_engine()` (that awaits
    engine.dispose(), a real async teardown of in-flight connections) --
    a hard pod kill wouldn't get to run that either; a fresh process
    simply never had a handle to the old engine to begin with."""
    db_module._engine = None
    db_module._session_factory = None


@pytest.fixture(autouse=True)
def _reset_webhook_http_client():
    set_http_client_for_tests(None)
    yield
    set_http_client_for_tests(None)


@pytest.fixture(autouse=True)
def _patch_safe_webhook_dns(monkeypatch):
    """This file's webhook uses an example.com-style hostname that doesn't
    actually resolve -- default to a safe public IP so the request-time
    re-validation added for GitHub issue #148 doesn't turn this restart test
    into a DNS-dependent one, matching test_webhook_delivery.py's own
    pattern for the same reason."""

    async def _fake_resolve(hostname: str) -> str:
        return _SAFE_DNS_IP

    monkeypatch.setattr(webhook_delivery_module, "resolve_and_validate_destination_ip", _fake_resolve)


# ---------------------------------------------------------------------------
# Webhook deliveries: DB-persisted, must survive a restart with zero loss
# ---------------------------------------------------------------------------


async def test_webhook_delivery_survives_control_plane_restart_and_still_delivers(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "restart-webhook@example.com")
    created = await _register_webhook(client, key, url="https://receiver.example.com/hooks")
    raw_secret = created["secret"]

    await _create_sandbox(client, key)

    # Sanity: the delivery is enqueued (pending) before any "restart".
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row_before] = result.scalars().all()
    assert row_before.status == "pending"
    delivery_id = row_before.id

    _simulate_control_plane_restart()

    # A fresh process reading the same on-disk database must see the exact
    # same pending delivery -- nothing silently dropped by the restart.
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row_after] = result.scalars().all()
    assert row_after.id == delivery_id
    assert row_after.status == "pending"

    # And the (also freshly "restarted") delivery worker can still complete
    # it, proving this isn't just a row surviving inertly but the actual
    # feature working end-to-end after the simulated restart.
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={"ok": True})

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await _deliver_once()

    assert len(received) == 1
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row_final] = result.scalars().all()
    assert row_final.status == "delivered"
    assert row_final.id == delivery_id
    assert raw_secret  # sanity: signup flow actually returned a real secret


async def test_webhook_pending_retry_backoff_survives_restart(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """A delivery already mid-retry (failed once, scheduled for a future
    attempt) must keep its exact retry schedule across a restart -- a
    fresh process re-reading the row must not treat it as brand new (which
    would reset backoff) nor drop it (which would abandon the retry
    entirely)."""
    key = await signup_and_get_api_key(client, "restart-webhook-retry@example.com")
    await _register_webhook(client, key, url="https://receiver.example.com/hooks")
    await _create_sandbox(client, key)

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    set_http_client_for_tests(httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)))
    await _deliver_once()

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row_before] = result.scalars().all()
    assert row_before.status == "pending"
    assert row_before.attempt_count == 1
    assert row_before.next_attempt_at is not None
    next_attempt_before = row_before.next_attempt_at

    _simulate_control_plane_restart()

    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(WebhookDelivery))
        [row_after] = result.scalars().all()
    assert row_after.attempt_count == 1
    assert row_after.next_attempt_at == next_attempt_before


# ---------------------------------------------------------------------------
# Audit log (ExecLogEntry): DB-persisted hash chain, must survive a restart
# ---------------------------------------------------------------------------


async def test_audit_log_entries_and_hash_chain_survive_control_plane_restart(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "restart-audit@example.com")
    session_id = await _create_sandbox(client, key)
    await _exec(client, key, session_id, "echo one")
    await _exec(client, key, session_id, "echo two")

    async with db_module.get_session_factory()() as db:
        result = await db.execute(
            select(ExecLogEntry).where(ExecLogEntry.session_id == session_id).order_by(ExecLogEntry.started_at)
        )
        entries_before = list(result.scalars().all())
    assert len(entries_before) == 2
    row_hashes_before = [entry.row_hash for entry in entries_before]

    _simulate_control_plane_restart()

    # No silent data loss: the fresh process reads back the exact same rows.
    async with db_module.get_session_factory()() as db:
        result = await db.execute(
            select(ExecLogEntry).where(ExecLogEntry.session_id == session_id).order_by(ExecLogEntry.started_at)
        )
        entries_after = list(result.scalars().all())
    assert [entry.row_hash for entry in entries_after] == row_hashes_before

    # A third exec AFTER the simulated restart must chain from the last
    # pre-restart row, not restart its own chain from genesis -- proving
    # the hash chain itself (not just the raw rows) survives intact.
    await _exec(client, key, session_id, "echo three")

    async with db_module.get_session_factory()() as db:
        db_after_restart = db
        result = await db.execute(
            select(ExecLogEntry).where(ExecLogEntry.session_id == session_id).order_by(ExecLogEntry.started_at)
        )
        all_entries = list(result.scalars().all())
        assert len(all_entries) == 3
        assert all_entries[2].prev_hash == row_hashes_before[-1]

        chain_result = await verify_exec_log_chain(db_after_restart, session_id=session_id)
    assert chain_result.ok is True
    assert chain_result.rows_checked == 3
    assert chain_result.first_break_at_row_id is None


# ---------------------------------------------------------------------------
# Rate-limit state: in-memory backend is a KNOWN, documented gap; postgres
# backend is the durable alternative -- both get pinned here.
# ---------------------------------------------------------------------------


async def test_in_memory_rate_limit_state_is_lost_on_restart_by_design(client: httpx.AsyncClient):
    """Documents rate_limit.py's own disclosed tradeoff rather than a bug:
    the default in-memory limiter is explicitly single-process/local-dev
    only. A "restart" (module state gone) must reset the counter to zero --
    if this ever stopped happening, the in-memory backend would either be
    leaking real cross-restart persistence it never claimed to have, or
    (the actual risk on a real rolling restart) an operator relying on it
    across multiple replicas/restarts would get a false sense of durability
    it was never designed to provide.
    """
    key = "rate-limit-restart-test-bucket:some-account"
    for _ in range(5):
        _hits.setdefault(key, __import__("collections").deque()).append(0.0)
    assert len(_hits[key]) == 5

    # A restarted process starts this module fresh -- there is no on-disk
    # state to reload, unlike the DB-backed features above. reset_rate_
    # limits_for_tests() clearing the in-memory dict IS the accurate
    # simulation of "this state simply never existed in a new process."
    reset_rate_limits_for_tests()

    assert key not in _hits


async def test_postgres_rate_limit_backend_persists_across_restart(tmp_path):
    """The durable alternative actually closes the gap the test above
    documents: a fresh PostgresRateLimiter (standing in for a restarted
    process's brand new in-memory limiter object) reading the SAME
    database must see the prior window's count and keep incrementing it,
    not restart from zero."""
    db_path = tmp_path / f"rl_restart_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    pre_restart_limiter = PostgresRateLimiter(factory)
    counts_before = [await pre_restart_limiter.hit_and_count("bucket:restart-key") for _ in range(3)]
    assert counts_before == [1, 2, 3]

    # Simulate the control-plane restarting: dispose this "replica's"
    # engine/session-factory and construct a brand new limiter (a fresh
    # process would construct a brand new PostgresRateLimiter() at import
    # time, same as `_postgres_limiter` in rate_limit.py) against the same
    # underlying database file.
    await engine.dispose()
    restarted_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False})
    restarted_factory = async_sessionmaker(bind=restarted_engine, expire_on_commit=False)
    post_restart_limiter = PostgresRateLimiter(restarted_factory)

    count_after = await post_restart_limiter.hit_and_count("bucket:restart-key")

    assert count_after == 4, "rate-limit count must continue from where it left off, not reset to 1"
    await restarted_engine.dispose()
