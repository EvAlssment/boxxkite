"""Security and concurrency tests for the owned MemoryBase API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import update

from conftest import signup, signup_and_get_api_key
from control_plane import db as db_module
from control_plane.models_orm import MemoryRecord


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _create_related_pair(client: httpx.AsyncClient, key: str, scope: str = "security") -> tuple[str, str]:
    headers = _headers(key)
    first = await client.post(
        "/v1/memory",
        json={
            "content": "The user prefers Python for analysis.",
            "scope": scope,
            "kind": "fact",
        },
        headers=headers,
    )
    second = await client.post(
        "/v1/memory",
        json={
            "content": "The user prefers Python for analysis in notebooks.",
            "scope": scope,
            "kind": "fact",
        },
        headers=headers,
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    return first.json()["id"], second.json()["id"]


async def test_every_read_and_mutation_endpoint_is_account_scoped(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "memory-security-a@example.com")
    key_b = await signup_and_get_api_key(client, "memory-security-b@example.com")
    first_id, second_id = await _create_related_pair(client, key_a)
    headers_b = _headers(key_b)

    list_response = await client.get("/v1/memory", headers=headers_b)
    search_response = await client.get(
        "/v1/memory/search", params={"q": "prefers Python analysis"}, headers=headers_b
    )
    profile_response = await client.get("/v1/memory/profile", headers=headers_b)
    export_response = await client.get("/v1/memory/export", headers=headers_b)
    metrics_response = await client.get("/v1/memory/metrics", headers=headers_b)

    assert list_response.status_code == 200
    assert list_response.json() == []
    assert search_response.status_code == 200
    assert search_response.json()["memories"] == []
    assert profile_response.status_code == 200
    assert profile_response.json() == {"static": [], "dynamic": []}
    assert export_response.status_code == 200
    assert export_response.json() == {"memories": [], "relations": []}
    assert metrics_response.status_code == 200
    assert metrics_response.json() == {
        "total_memories": 0,
        "by_kind": {},
        "embedded_memories": 0,
        "embedding_coverage": 0.0,
        "embedding_model_id": "harrier-oss-v1-0.6b-1024-v1",
    }

    for path in (f"/v1/memory/{first_id}", f"/v1/memory/{first_id}/relations"):
        response = await client.get(path, headers=headers_b)
        assert response.status_code == 404, (path, response.text)

    deleted = await client.delete(f"/v1/memory/{second_id}", headers=headers_b)
    assert deleted.status_code == 404
    assert (
        await client.get(f"/v1/memory/{second_id}", headers=_headers(key_a))
    ).status_code == 200


async def test_scope_filters_are_applied_consistently(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-scope-security@example.com")
    headers = _headers(key)
    for scope in ("alpha", "beta"):
        first_id, second_id = await _create_related_pair(client, key, scope=scope)
        assert first_id != second_id

    for scope, other_scope in (("alpha", "beta"), ("beta", "alpha")):
        listed = await client.get("/v1/memory", params={"scope": scope}, headers=headers)
        searched = await client.get(
            "/v1/memory/search",
            params={"q": "prefers Python analysis", "scope": scope},
            headers=headers,
        )
        profiled = await client.get("/v1/memory/profile", params={"scope": scope}, headers=headers)
        exported = await client.get("/v1/memory/export", params={"scope": scope}, headers=headers)
        metrics = await client.get("/v1/memory/metrics", params={"scope": scope}, headers=headers)

        assert listed.status_code == 200
        assert len(listed.json()) == 2
        assert {row["scope"] for row in listed.json()} == {scope}
        assert searched.status_code == 200
        assert {row["scope"] for row in searched.json()["memories"]} == {scope}
        assert profiled.status_code == 200
        assert {row["scope"] for group in profiled.json().values() for row in group} == {scope}
        assert exported.status_code == 200
        exported_ids = {row["id"] for row in exported.json()["memories"]}
        assert len(exported_ids) == 2
        assert all(
            relation["source_memory_id"] in exported_ids
            and relation["target_memory_id"] in exported_ids
            for relation in exported.json()["relations"]
        )
        assert metrics.status_code == 200
        assert metrics.json()["total_memories"] == 2

        unscoped = await client.get("/v1/memory", params={"scope": other_scope}, headers=headers)
        assert {row["scope"] for row in unscoped.json()} == {other_scope}


async def test_expired_relation_target_is_not_traversable(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-expired-relation@example.com")
    headers = _headers(key)
    target_id, source_id = await _create_related_pair(client, key)

    before = await client.get(f"/v1/memory/{source_id}/relations", headers=headers)
    assert before.status_code == 200, before.text
    assert any(edge["target_memory_id"] == target_id for edge in before.json()["relations"])

    async with db_module.get_session_factory()() as db:
        await db.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == target_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await db.commit()

    after = await client.get(f"/v1/memory/{source_id}/relations", headers=headers)
    assert after.status_code == 200
    assert after.json()["relations"] == []
    assert (await client.get(f"/v1/memory/{target_id}", headers=headers)).status_code == 404


async def test_memory_payload_limits_and_invalid_values_are_rejected(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-payload-security@example.com")
    headers = _headers(key)

    invalid_requests = [
        ("post", "/v1/memory", {"content": "x" * (64 * 1024 + 1)}),
        ("post", "/v1/memory/ingest", {"content": "x" * (256 * 1024 + 1)}),
        ("post", "/v1/memory", {"content": "x", "metadata": {"blob": "x" * (32 * 1024)}}),
        ("post", "/v1/memory", {"content": "x", "importance": 1.01}),
        ("post", "/v1/memory", {"content": "x", "scope": "x" * 129}),
        ("post", "/v1/memory", {"content": "x", "event_dates": ["2026-01-01"] * 17}),
        ("get", "/v1/memory/search?q=", None),
        ("get", "/v1/memory?limit=0", None),
        (
            "post",
            "/v1/memory/import",
            {
                "memories": [{"content": "x"}],
                "relations": [{"source_memory_id": "a", "target_memory_id": "b", "relation_type": "nope"}],
            },
        ),
        (
            "post",
            "/v1/memory/import",
            {
                "memories": [{"content": "x"}],
                "relations": [{
                    "source_memory_id": "a",
                    "target_memory_id": "b",
                    "relation_type": "related",
                    "confidence": 1.01,
                }],
            },
        ),
    ]
    for method, path, payload in invalid_requests:
        response = await client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 422, (method, path, response.text)


async def test_import_cannot_attach_relations_to_foreign_memory_ids(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "memory-import-a@example.com")
    key_b = await signup_and_get_api_key(client, "memory-import-b@example.com")
    foreign_source, foreign_target = await _create_related_pair(client, key_a, scope="foreign")

    imported = await client.post(
        "/v1/memory/import",
        json={
            "memories": [
                {"content": "Account B imported record", "scope": "local", "id": "b-local"},
            ],
            "relations": [
                {
                    "source_memory_id": foreign_source,
                    "target_memory_id": foreign_target,
                    "relation_type": "related",
                },
            ],
        },
        headers=_headers(key_b),
    )
    assert imported.status_code == 201, imported.text
    local_id = imported.json()["memories"][0]["id"]
    relations = await client.get(f"/v1/memory/{local_id}/relations", headers=_headers(key_b))
    assert relations.status_code == 200
    assert relations.json()["relations"] == []

    foreign_relations = await client.get(
        f"/v1/memory/{foreign_target}/relations", headers=_headers(key_a)
    )
    assert foreign_relations.status_code == 200
    assert any(edge["target_memory_id"] == foreign_source for edge in foreign_relations.json()["relations"])


async def test_all_memory_endpoints_reject_dashboard_jwt(client: httpx.AsyncClient):
    session = await signup(client, "memory-jwt-security@example.com")
    headers = _headers(session["access_token"])
    calls = [
        ("post", "/v1/memory", {"content": "x"}),
        ("post", "/v1/memory/ingest", {"content": "x"}),
        ("post", "/v1/memory/import", {"memories": [{"content": "x"}]}),
        ("get", "/v1/memory", None),
        ("get", "/v1/memory/search?q=x", None),
        ("get", "/v1/memory/profile", None),
        ("get", "/v1/memory/export", None),
        ("get", "/v1/memory/metrics", None),
        ("get", "/v1/memory/not-a-memory", None),
        ("get", "/v1/memory/not-a-memory/relations", None),
        ("delete", "/v1/memory/not-a-memory", None),
    ]
    for method, path, payload in calls:
        response = await client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 401, (method, path, response.text)
        assert response.json()["error"]["code"] == "wrong_credential_type"


async def test_concurrent_duplicate_writes_are_idempotent(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-concurrency-security@example.com")
    headers = _headers(key)
    payload = {"content": "One logical concurrent memory", "scope": "concurrency", "kind": "fact"}

    outcomes = await asyncio.gather(
        *(client.post("/v1/memory", json=payload, headers=headers) for _ in range(2)),
        return_exceptions=True,
    )
    assert all(isinstance(outcome, httpx.Response) for outcome in outcomes), outcomes
    responses = [outcome for outcome in outcomes if isinstance(outcome, httpx.Response)]
    assert all(response.status_code == 201 for response in responses), [response.text for response in responses]
    assert len({response.json()["id"] for response in responses}) == 1

    listed = await client.get("/v1/memory", params={"scope": "concurrency"}, headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1
