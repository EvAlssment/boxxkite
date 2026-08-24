"""Account-scoped durability and isolation tests for the owned MemoryBase."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from conftest import signup, signup_and_get_api_key
from control_plane.memory_engine import extract_memories


async def test_memory_round_trip_search_update_and_forget(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-round-trip@example.com")
    headers = {"Authorization": f"Bearer {key}"}

    created = await client.post(
        "/v1/memory",
        json={
            "content": "The deployment uses a blue-green rollout.",
            "scope": "project-alpha",
            "kind": "fact",
            "metadata": {"source": "test"},
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    memory = created.json()
    assert memory["scope"] == "project-alpha"

    recalled = await client.get(
        "/v1/memory/search",
        params={"q": "blue-green rollout", "scope": "project-alpha"},
        headers=headers,
    )
    assert recalled.status_code == 200
    assert recalled.json()["memories"][0]["id"] == memory["id"]
    assert recalled.json()["memories"][0]["score"] > 0

    duplicate = await client.post(
        "/v1/memory",
        json={
            "content": "The deployment uses a blue-green rollout.",
            "scope": "project-alpha",
            "metadata": {"source": "updated"},
        },
        headers=headers,
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == memory["id"]
    assert duplicate.json()["metadata"] == {"source": "updated"}

    deleted = await client.delete(f"/v1/memory/{memory['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/v1/memory/{memory['id']}", headers=headers)).status_code == 404


async def test_memory_isolation_is_enforced_on_get_and_search(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "memory-a@example.com")
    key_b = await signup_and_get_api_key(client, "memory-b@example.com")
    created = await client.post(
        "/v1/memory",
        json={"content": "Account A private deployment detail"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    memory_id = created.json()["id"]

    get_other = await client.get(
        f"/v1/memory/{memory_id}", headers={"Authorization": f"Bearer {key_b}"}
    )
    search_other = await client.get(
        "/v1/memory/search",
        params={"q": "private deployment detail"},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert get_other.status_code == 404
    assert search_other.status_code == 200
    assert search_other.json()["memories"] == []


async def test_expired_memory_is_not_recalled_or_listed(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-expiry@example.com")
    headers = {"Authorization": f"Bearer {key}"}
    created = await client.post(
        "/v1/memory",
        json={
            "content": "This is temporary context",
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert (await client.get("/v1/memory", headers=headers)).json() == []
    assert (
        await client.get(
            "/v1/memory/search", params={"q": "temporary context"}, headers=headers
        )
    ).json()["memories"] == []
    assert (await client.delete(f"/v1/memory/{created.json()['id']}", headers=headers)).status_code == 204


async def test_memory_requires_api_key_not_dashboard_token(client: httpx.AsyncClient):
    signup_response = await signup(client, "memory-auth@example.com")
    response = await client.post(
        "/v1/memory",
        json={"content": "should not be stored"},
        headers={"Authorization": f"Bearer {signup_response['access_token']}"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "wrong_credential_type"


async def test_memory_ingest_extracts_provenance_profile_and_relations(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-ingest@example.com")
    headers = {"Authorization": f"Bearer {key}"}
    ingested = await client.post(
        "/v1/memory/ingest",
        json={
            "content": (
                "I prefer dark mode. My goal is to ship on 2026-08-20. "
                "The deployment uses blue-green rollout. "
                "The review is on January 26, 2023."
            ),
            "scope": "project-alpha",
            "source_session_id": "session-ingest",
            "metadata": {"source": "conversation"},
        },
        headers=headers,
    )
    assert ingested.status_code == 201, ingested.text
    memories = ingested.json()["memories"]
    assert {memory["kind"] for memory in memories} == {"preference", "goal", "fact"}
    assert all(memory["source_content"] for memory in memories)
    assert any("2026-08-20" in memory["event_dates"] for memory in memories)
    assert any("2023-01-26" in memory["event_dates"] for memory in memories)
    assert any(
        memory["content"].startswith("I prefer dark mode.")
        and "blue-green rollout" in memory["content"]
        for memory in memories
    )

    profile = await client.get(
        "/v1/memory/profile", params={"scope": "project-alpha"}, headers=headers
    )
    assert profile.status_code == 200
    assert {memory["kind"] for memory in profile.json()["static"]} >= {"preference", "fact"}
    assert {memory["kind"] for memory in profile.json()["dynamic"]} >= {"goal"}

    first_id = memories[0]["id"]
    related = await client.get(f"/v1/memory/{first_id}/relations", headers=headers)
    assert related.status_code == 200
    assert all(edge["source_memory_id"] == first_id for edge in related.json()["relations"])


def test_memory_ingest_normalizes_relative_event_dates_from_document_date():
    memories = extract_memories(
        "Caroline went to a support group yesterday and a council meeting last Friday.",
        document_date=datetime(2023, 5, 8, tzinfo=timezone.utc),
    )
    dates = {date for memory in memories for date in memory.event_dates}
    assert "2023-05-07" in dates
    assert "2023-05-05" in dates


async def test_memory_export_import_and_scope_isolation(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-transfer@example.com")
    headers = {"Authorization": f"Bearer {key}"}
    created = await client.post(
        "/v1/memory",
        json={"content": "Exportable account memory", "scope": "transfer"},
        headers=headers,
    )
    assert created.status_code == 201
    exported = await client.get("/v1/memory/export", params={"scope": "transfer"}, headers=headers)
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["memories"][0]["content"] == "Exportable account memory"
    metrics = await client.get("/v1/memory/metrics", params={"scope": "transfer"}, headers=headers)
    assert metrics.json()["total_memories"] == 1

    imported = await client.post(
        "/v1/memory/import",
        json={
            "memories": [
                {
                    "content": "Imported account memory",
                    "scope": "imported",
                    "kind": "note",
                }
            ],
            "relations": payload["relations"],
        },
        headers=headers,
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["memories"][0]["kind"] == "note"
    assert (await client.get("/v1/memory", params={"scope": "transfer"}, headers=headers)).json()
    assert (await client.get("/v1/memory", params={"scope": "imported"}, headers=headers)).json()


async def test_memory_update_supersedes_old_fact_but_preserves_history(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-versioning@example.com")
    headers = {"Authorization": f"Bearer {key}"}
    old = await client.post(
        "/v1/memory",
        json={"content": "My favorite color is blue.", "kind": "preference"},
        headers=headers,
    )
    new = await client.post(
        "/v1/memory",
        json={"content": "My favorite color is now green.", "kind": "preference"},
        headers=headers,
    )
    assert old.status_code == 201
    assert new.status_code == 201

    recalled = await client.get(
        "/v1/memory/search", params={"q": "favorite color"}, headers=headers
    )
    assert [row["id"] for row in recalled.json()["memories"]] == [new.json()["id"]]
    historical_recalled = await client.get(
        "/v1/memory/search",
        params={"q": "What was my favorite color before green?"},
        headers=headers,
    )
    assert historical_recalled.status_code == 200
    assert historical_recalled.json()["memories"][0]["id"] == old.json()["id"]
    historical = await client.get(f"/v1/memory/{old.json()['id']}", headers=headers)
    assert historical.json()["superseded_by_id"] == new.json()["id"]
    versions = await client.get(
        "/v1/memory", params={"include_superseded": True}, headers=headers
    )
    assert {row["id"] for row in versions.json()} == {old.json()["id"], new.json()["id"]}
    relations = await client.get(f"/v1/memory/{new.json()['id']}/relations", headers=headers)
    assert relations.json()["relations"][0]["relation_type"] == "updates"


async def test_same_subject_value_change_supersedes_without_update_marker(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-subject-update@example.com")
    headers = {"Authorization": f"Bearer {key}"}
    old = await client.post(
        "/v1/memory",
        json={"content": "The deployment uses blue-green rollout."},
        headers=headers,
    )
    new = await client.post(
        "/v1/memory",
        json={"content": "The deployment uses canary rollout."},
        headers=headers,
    )

    assert old.status_code == 201
    assert new.status_code == 201
    historical = await client.get(f"/v1/memory/{old.json()['id']}", headers=headers)
    assert historical.json()["superseded_by_id"] == new.json()["id"]


async def test_named_entity_bridge_creates_related_memory_edge(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "memory-entity-bridge@example.com")
    headers = {"Authorization": f"Bearer {key}"}
    first = await client.post(
        "/v1/memory",
        json={"content": "Charles Darwin is married to Amala Paul."},
        headers=headers,
    )
    second = await client.post(
        "/v1/memory",
        json={"content": "Amala Paul is a citizen of Belgium."},
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    relations = await client.get(
        f"/v1/memory/{second.json()['id']}/relations", headers=headers
    )
    assert relations.status_code == 200
    assert any(
        edge["target_memory_id"] == first.json()["id"]
        and edge["relation_type"] == "related"
        for edge in relations.json()["relations"]
    )
