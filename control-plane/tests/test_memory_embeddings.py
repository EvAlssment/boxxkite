from __future__ import annotations

import httpx

from conftest import signup_and_get_api_key
from control_plane.memory_embeddings import GeminiEmbeddingProvider, OpenAICompatibleEmbeddingProvider
from control_plane.routers import memory as memory_router


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        vectors = []
        for text in texts:
            vector = [0.0] * 1024
            if "spaceship" in text or text == "rocket engine":
                vector[0] = 1.0
            else:
                vector[1] = 1.0
            vectors.append(vector)
        return vectors


class FakeRerankerProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    async def rerank(self, query, documents):
        self.calls.append((query, tuple(documents)))
        return {
            memory_id: (1.0 if "Gardening" in content else 0.0)
            for memory_id, content in documents
        }


async def test_qwen_query_instruction_is_only_added_to_query_embeddings():
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:8000/v1",
        model="Qwen/Qwen3-Embedding-0.6B",
        dimensions=768,
        query_instruction="retrieve durable memory",
    )
    captured: list[tuple[str, ...]] = []

    async def fake_request(texts):
        captured.append(tuple(texts))
        return [[0.0] * 1024]

    provider._request = fake_request
    await provider.embed(["stored memory"])
    await provider.embed_query("what should I remember?")

    assert captured == [
        ("stored memory",),
        ("Instruct: retrieve durable memory\n Query:what should I remember?",),
    ]


async def test_gemini_uses_asymmetric_retrieval_prompts():
    provider = GeminiEmbeddingProvider(
        model="gemini-embedding-2",
        dimensions=768,
        api_key="test-key",
    )
    captured: list[tuple[str, ...]] = []

    async def fake_request(texts):
        captured.append(tuple(texts))
        return [[0.0] * 768]

    provider._request = fake_request
    await provider.embed(["stored memory"])
    await provider.embed_query("what should I remember?")

    assert captured == [
        ("title: none | text: stored memory",),
        ("task: search result | query: what should I remember?",),
    ]


def test_gemini_requires_the_current_model_and_key():
    try:
        GeminiEmbeddingProvider(model="gemini-embedding-001", dimensions=768, api_key="test-key")
    except ValueError as exc:
        assert "gemini-embedding-2" in str(exc)
    else:
        raise AssertionError("the Gemini provider must reject incompatible model spaces")

    try:
        GeminiEmbeddingProvider(model="gemini-embedding-2", dimensions=768, api_key=None)
    except ValueError as exc:
        assert "API key" in str(exc)
    else:
        raise AssertionError("the Gemini provider must reject a missing API key")


def test_reranker_rejects_non_local_plain_http():
    from control_plane.memory_embeddings import OpenAICompatibleReranker

    try:
        OpenAICompatibleReranker(
            base_url="http://memory-reranker.example",
            model="Qwen/Qwen3-Reranker-0.6B",
        )
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("non-local plain HTTP reranker must be rejected")


async def test_pgvector_semantic_channel_recalls_without_lexical_overlap(
    client: httpx.AsyncClient, monkeypatch
):
    provider = FakeEmbeddingProvider()
    monkeypatch.setattr(memory_router, "get_memory_embedding_provider", lambda: provider)
    key = await signup_and_get_api_key(client, "memory-vector@example.com")
    headers = {"Authorization": f"Bearer {key}"}

    imported = await client.post(
        "/v1/memory/import",
        json={
            "memories": [
                {"content": "Spaceship propulsion notes", "kind": "note"},
                {"content": "Gardening schedule", "kind": "note"},
            ]
        },
        headers=headers,
    )
    assert imported.status_code == 201, imported.text
    assert len(provider.calls) == 1
    assert len(provider.calls[0]) == 2

    recalled = await client.get(
        "/v1/memory/search",
        params={"q": "rocket engine"},
        headers=headers,
    )
    assert recalled.status_code == 200, recalled.text
    assert recalled.json()["memories"][0]["content"] == "Spaceship propulsion notes"
    assert len(provider.calls) == 2
    assert provider.calls[1] == ("rocket engine",)

    metrics = await client.get("/v1/memory/metrics", headers=headers)
    assert metrics.json()["embedded_memories"] == 2
    assert metrics.json()["embedding_coverage"] == 1.0


async def test_unchanged_memory_skips_embedding_regeneration(client: httpx.AsyncClient, monkeypatch):
    provider = FakeEmbeddingProvider()
    monkeypatch.setattr(memory_router, "get_memory_embedding_provider", lambda: provider)
    key = await signup_and_get_api_key(client, "memory-vector-cache@example.com")
    headers = {"Authorization": f"Bearer {key}"}
    payload = {"content": "A durable memory whose text does not change", "kind": "note"}

    first = await client.post("/v1/memory", json=payload, headers=headers)
    second = await client.post("/v1/memory", json=payload, headers=headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert len(provider.calls) == 1


async def test_embedding_failure_does_not_lose_durable_memory(
    client: httpx.AsyncClient, monkeypatch
):
    class BrokenProvider:
        async def embed(self, texts):
            raise RuntimeError("embedding service unavailable")

    monkeypatch.setattr(
        memory_router,
        "get_memory_embedding_provider",
        lambda: BrokenProvider(),
    )
    key = await signup_and_get_api_key(client, "memory-vector-fallback@example.com")
    headers = {"Authorization": f"Bearer {key}"}

    created = await client.post(
        "/v1/memory",
        json={"content": "Lexical fallback stays durable"},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    recalled = await client.get(
        "/v1/memory/search",
        params={"q": "lexical fallback"},
        headers=headers,
    )
    assert recalled.status_code == 200, recalled.text
    assert recalled.json()["memories"][0]["id"] == created.json()["id"]


async def test_optional_qwen_reranker_reorders_hybrid_candidates(
    client: httpx.AsyncClient, monkeypatch
):
    embedding_provider = FakeEmbeddingProvider()
    reranker_provider = FakeRerankerProvider()
    monkeypatch.setattr(
        memory_router,
        "get_memory_embedding_provider",
        lambda: embedding_provider,
    )
    monkeypatch.setattr(
        memory_router,
        "get_memory_reranker",
        lambda: reranker_provider,
    )
    key = await signup_and_get_api_key(client, "memory-reranker@example.com")
    headers = {"Authorization": f"Bearer {key}"}

    imported = await client.post(
        "/v1/memory/import",
        json={
            "memories": [
                {"content": "Spaceship propulsion notes", "kind": "note"},
                {"content": "Gardening schedule", "kind": "note"},
            ]
        },
        headers=headers,
    )
    assert imported.status_code == 201, imported.text

    recalled = await client.get(
        "/v1/memory/search",
        params={"q": "rocket engine"},
        headers=headers,
    )
    assert recalled.status_code == 200, recalled.text
    assert recalled.json()["memories"][0]["content"] == "Gardening schedule"
    assert reranker_provider.calls
    assert reranker_provider.calls[0][0] == "rocket engine"


async def test_reranker_is_skipped_for_graph_intent_queries(
    client: httpx.AsyncClient, monkeypatch
):
    embedding_provider = FakeEmbeddingProvider()
    reranker_provider = FakeRerankerProvider()
    monkeypatch.setattr(memory_router, "get_memory_embedding_provider", lambda: embedding_provider)
    monkeypatch.setattr(memory_router, "get_memory_reranker", lambda: reranker_provider)
    key = await signup_and_get_api_key(client, "memory-reranker-graph@example.com")
    headers = {"Authorization": f"Bearer {key}"}

    imported = await client.post(
        "/v1/memory/import",
        json={
            "memories": [
                {"content": "The author is Charles Darwin.", "kind": "fact"},
                {"content": "Charles Darwin is married to Amala Paul.", "kind": "fact"},
            ]
        },
        headers=headers,
    )
    assert imported.status_code == 201, imported.text

    recalled = await client.get(
        "/v1/memory/search",
        params={"q": "What is the citizenship of the spouse of the author?"},
        headers=headers,
    )
    assert recalled.status_code == 200, recalled.text
    assert reranker_provider.calls == []
