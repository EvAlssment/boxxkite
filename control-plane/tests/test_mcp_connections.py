"""Tests for the org-scoped outbound-MCP connection-grant CRUD API
(GitHub issues #116/#117, docs/OUTBOUND-MCP-DESIGN.md §3) -- the
mechanical-reuse pass only: curated catalog resolution, connection CRUD,
mcp_connection_names resolution on sandbox create (404 not 400, matching
secret_names's existing precedent), and the resolved allowed_hosts reaching
SandboxManager.create_session's mcp_connection_grants (unioned with
secret_grants into the existing per-session NetworkPolicy at the manager
layer, tested separately in tests/test_manager_mcp_connections_network_policy.py).

Explicitly NOT covered here (out of scope for this pass, per
docs/OUTBOUND-MCP-DESIGN.md §6/§7): any MCP-proxy transport, and any
third-party OAuth credential handling.
"""

from __future__ import annotations

import httpx

from conftest import create_api_key, signup
from control_plane.config import settings


async def _account_with_key(client: httpx.AsyncClient, email: str) -> str:
    token_response = await signup(client, email)
    created = await create_api_key(client, token_response["access_token"], name="ci key")
    return created["key"]


async def test_create_mcp_connection_resolves_catalog_host(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "mcp-create@example.com")

    resp = await client.post(
        "/v1/mcp-connections",
        json={"label": "team-slack", "catalog_id": "slack"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["label"] == "team-slack"
    assert body["catalog_id"] == "slack"
    assert body["host"] == "mcp.slack.com"


async def test_create_mcp_connection_rejects_unknown_catalog_id(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "mcp-unknown-catalog@example.com")

    resp = await client.post(
        "/v1/mcp-connections",
        json={"label": "whatever", "catalog_id": "not-a-real-provider"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    # Pydantic Literal validation -- a 422, not a 404, since this is
    # request-shape validation, not an account-scoped name lookup.
    assert resp.status_code == 422


async def test_list_mcp_connections(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "mcp-list@example.com")
    await client.post(
        "/v1/mcp-connections",
        json={"label": "c1", "catalog_id": "notion"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    resp = await client.get("/v1/mcp-connections", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    assert resp.json()[0]["label"] == "c1"
    assert resp.json()[0]["host"] == "mcp.notion.com"


async def test_duplicate_mcp_connection_label_within_account_is_409(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "mcp-dup@example.com")
    body = {"label": "dup", "catalog_id": "linear"}
    first = await client.post("/v1/mcp-connections", json=body, headers={"Authorization": f"Bearer {api_key}"})
    assert first.status_code == 201

    second = await client.post("/v1/mcp-connections", json=body, headers={"Authorization": f"Bearer {api_key}"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "mcp_connection_label_taken"


async def test_cannot_delete_another_accounts_mcp_connection(client: httpx.AsyncClient):
    api_key_a = await _account_with_key(client, "mcp-owner-a@example.com")
    api_key_b = await _account_with_key(client, "mcp-owner-b@example.com")

    created = await client.post(
        "/v1/mcp-connections",
        json={"label": "a-connection", "catalog_id": "github"},
        headers={"Authorization": f"Bearer {api_key_a}"},
    )
    connection_id = created.json()["id"]

    resp = await client.delete(
        f"/v1/mcp-connections/{connection_id}", headers={"Authorization": f"Bearer {api_key_b}"}
    )
    assert resp.status_code == 404


async def test_delete_mcp_connection(client: httpx.AsyncClient):
    api_key = await _account_with_key(client, "mcp-delete@example.com")
    created = await client.post(
        "/v1/mcp-connections",
        json={"label": "to-delete", "catalog_id": "slack"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    connection_id = created.json()["id"]

    resp = await client.delete(
        f"/v1/mcp-connections/{connection_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp.status_code == 204

    # Deleting again -- already gone -- 404s the same as a foreign id.
    resp2 = await client.delete(
        f"/v1/mcp-connections/{connection_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert resp2.status_code == 404


async def test_mcp_connection_names_on_sandbox_create_grants_allowed_hosts(
    client: httpx.AsyncClient, fake_manager
):
    api_key = await _account_with_key(client, "mcp-grant@example.com")
    await client.post(
        "/v1/mcp-connections",
        json={"label": "team-slack", "catalog_id": "slack"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    resp = await client.post(
        "/v1/sandboxes",
        json={"mcp_connection_names": ["team-slack"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    created = fake_manager.created[session_id]
    assert created["mcp_connection_grants"] == [
        {"name": "team-slack", "allowed_hosts": ["mcp.slack.com"]}
    ]


async def test_mcp_connection_names_unknown_name_is_404_and_creates_no_session(
    client: httpx.AsyncClient, fake_manager
):
    api_key = await _account_with_key(client, "mcp-unknown-name@example.com")

    resp = await client.post(
        "/v1/sandboxes",
        json={"mcp_connection_names": ["does-not-exist"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "mcp_connection_not_found"
    assert fake_manager.created == {}


async def test_mcp_connection_names_never_leaks_across_accounts(
    client: httpx.AsyncClient, fake_manager
):
    """A connection created by account A must 404, not resolve, when
    another account (B) names it in mcp_connection_names -- same
    cross-tenant precedent as secret_names."""
    api_key_a = await _account_with_key(client, "mcp-cross-a@example.com")
    api_key_b = await _account_with_key(client, "mcp-cross-b@example.com")

    await client.post(
        "/v1/mcp-connections",
        json={"label": "shared-name", "catalog_id": "slack"},
        headers={"Authorization": f"Bearer {api_key_a}"},
    )

    resp = await client.post(
        "/v1/sandboxes",
        json={"mcp_connection_names": ["shared-name"]},
        headers={"Authorization": f"Bearer {api_key_b}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "mcp_connection_not_found"


async def test_sandbox_create_with_both_secret_and_mcp_connection_names(
    client: httpx.AsyncClient, fake_manager
):
    """secret_grants and mcp_connection_grants are resolved independently
    and both reach SandboxManager.create_session -- the manager layer (not
    this router/policy layer) is responsible for unioning their
    allowed_hosts into one NetworkPolicy."""
    original_url = settings.SECRETS_CONTROL_PLANE_URL
    settings.SECRETS_CONTROL_PLANE_URL = "https://control-plane.internal.example"
    try:
        api_key = await _account_with_key(client, "mcp-and-secret@example.com")
        await client.post(
            "/v1/secrets",
            json={"name": "api-key", "value": "v", "allowed_hosts": ["api.example.com"]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        await client.post(
            "/v1/mcp-connections",
            json={"label": "team-linear", "catalog_id": "linear"},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        resp = await client.post(
            "/v1/sandboxes",
            json={"secret_names": ["api-key"], "mcp_connection_names": ["team-linear"]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, resp.text
        created = fake_manager.created[resp.json()["id"]]
        assert created["secret_grants"] == [{"name": "api-key", "allowed_hosts": ["api.example.com"]}]
        assert created["mcp_connection_grants"] == [
            {"name": "team-linear", "allowed_hosts": ["mcp.linear.app"]}
        ]
    finally:
        settings.SECRETS_CONTROL_PLANE_URL = original_url


async def test_sandbox_create_with_colliding_secret_and_mcp_connection_name(
    client: httpx.AsyncClient, fake_manager
):
    """A Secret and an McpConnection are each unique only per-account
    within their own table (models_orm.py's Secret.name / McpConnection.label
    unique constraints are separate) -- nothing stops an account from
    having both named "shared". That used to crash create_session with an
    unhandled ValueError out of assert_policy_invariants (#155's
    cross-mechanism invariant check): this is a real, reachable account
    configuration, not malicious input, so the request must still succeed
    (phase 1 is observability/invariant-checking only, not enforcement)."""
    original_url = settings.SECRETS_CONTROL_PLANE_URL
    settings.SECRETS_CONTROL_PLANE_URL = "https://control-plane.internal.example"
    try:
        api_key = await _account_with_key(client, "colliding-name@example.com")
        await client.post(
            "/v1/secrets",
            json={"name": "shared", "value": "v", "allowed_hosts": ["api.example.com"]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        await client.post(
            "/v1/mcp-connections",
            json={"label": "shared", "catalog_id": "linear"},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        resp = await client.post(
            "/v1/sandboxes",
            json={"secret_names": ["shared"], "mcp_connection_names": ["shared"]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, resp.text
    finally:
        settings.SECRETS_CONTROL_PLANE_URL = original_url
