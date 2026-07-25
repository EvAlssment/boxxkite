"""Fault-injection coverage for GitHub issue #151, scenario 3: does a
control-plane process restart lose or corrupt anything for a sandbox session
that was already running before the restart?

Per this repo's own design (see `boxkite/manager.py`'s module docstring --
"K8s pod labels/annotations as session store (survives backend restarts)"),
a `SandboxManager`'s real state lives in the Kubernetes cluster, not the
control-plane process -- restarting the control plane doesn't touch a
running pod. This control-plane service's OWN bookkeeping (the
`sandbox_sessions` table) is the other half of that durability story: it's
already a real Postgres/SQLite row, not an in-memory dict, specifically so
a restart doesn't lose track of which sessions exist.

What this test actually demonstrates, rather than assumes:
1. A session row created before a simulated restart is still readable and
   usable (GET + exec) through a freshly rebuilt DB engine/session factory
   afterward -- durable state genuinely survives.
2. `rate_limit.py`'s in-memory sliding-window counter (`_hits`, a plain
   module-level `OrderedDict`, explicitly documented in that module's own
   docstring as per-process-only) does NOT survive -- a caller who was
   429'd right before the restart is let back in immediately after. This is
   a known, accepted limitation (not a bug this task is asking to fix, and
   not something to silently paper over) -- flagged explicitly by this test
   rather than left as an unstated assumption.

A "restart" here means what it would take for a *new* Python process to
pick up where the old one left off against the SAME persistent database:
the DB engine/session factory are disposed and rebuilt fresh (mirroring
`db.py`'s own lazy, module-level singleton pattern that a new process
would populate from scratch), and the in-memory-only module globals
(`rate_limit._hits`, `usage_policy._create_session_lock`) are reset to the
same fresh values a new process's module import would produce -- reusing
the exact same `reset_rate_limits_for_tests`/
`reset_create_session_lock_for_tests` helpers this suite's `conftest.py`
already calls between tests for the identical reason (an `asyncio.Lock`/
in-memory dict bound to a stale event loop or carrying stale counts). No
real process is killed and restarted -- there is no fake/mock
infrastructure in this repo for that, and simulating it at the level of
"what module-level state resets, and does durable data survive" is the
faithful, meaningful thing to test without standing up a real second
process.
"""

from __future__ import annotations

import httpx

from control_plane import db as db_module
from control_plane import rate_limit as rate_limit_module
from control_plane.config import settings
from control_plane.usage_policy import reset_create_session_lock_for_tests
from conftest import FakeSandboxManager, signup_and_get_api_key


async def _simulate_control_plane_restart() -> None:
    # Durable state: dispose and rebuild the DB engine/session factory --
    # exactly what a fresh process does on its first call to
    # get_engine()/get_session_factory(), reconnecting to the SAME
    # settings.DATABASE_URL a real restart would still point at (this is
    # the one thing this simulation deliberately does NOT change).
    await db_module.dispose_engine()
    db_module.get_engine()
    db_module.get_session_factory()

    # In-memory-only state: re-initialize to the same fresh values a new
    # process's module import would produce.
    rate_limit_module._hits.clear()
    reset_create_session_lock_for_tests()


async def test_existing_session_survives_a_control_plane_restart(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "restart-survival@example.com")
    create_resp = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"}
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["id"]

    await _simulate_control_plane_restart()

    # Durable: the pre-existing session row still resolves correctly
    # through the freshly rebuilt DB connection.
    get_resp = await client.get(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == session_id
    assert get_resp.json()["status"] == "active"

    # And a real operation against that pre-existing session still works.
    # (FakeSandboxManager itself models the fact that a real SandboxManager's
    # state is the K8s cluster, not this process, so it's deliberately left
    # untouched by the "restart" above -- see this module's own docstring.)
    exec_resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert exec_resp.status_code == 200
    assert session_id in fake_manager.created


async def test_in_memory_rate_limit_counter_does_not_survive_a_restart(
    client: httpx.AsyncClient, monkeypatch
):
    """Explicitly disclosed limitation, not a regression this test is
    guarding against: `rate_limit.py`'s in-memory sliding window is
    documented (in that module's own docstring) as per-process-only. This
    test exists so that fact is demonstrated and visible in the suite,
    rather than silently assumed -- a caller blocked with a 429 right
    before a control-plane restart is let back in immediately after,
    identically to how a multi-replica deployment already isn't protected
    by this backend either (the module's own docstring calls that out too;
    BOXKITE_RATE_LIMIT_BACKEND=postgres is the fix for both cases, not
    exercised here since it has its own dedicated test file,
    test_rate_limit_postgres_backend.py).
    """
    monkeypatch.setattr(settings, "BOXKITE_AUTH_RATE_LIMIT_PER_MINUTE", 1)

    first = await client.post(
        "/v1/auth/login", json={"email": "restart-rate-limit@example.com", "password": "wrong"}
    )
    assert first.status_code != 429  # first hit within the limit=1 budget

    blocked = await client.post(
        "/v1/auth/login", json={"email": "restart-rate-limit@example.com", "password": "wrong"}
    )
    assert blocked.status_code == 429

    await _simulate_control_plane_restart()

    # Known, accepted gap: the counter reset, so the very next request from
    # the same client is let through again instead of still being blocked.
    retried_after_restart = await client.post(
        "/v1/auth/login", json={"email": "restart-rate-limit@example.com", "password": "wrong"}
    )
    assert retried_after_restart.status_code != 429
