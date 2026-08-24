from __future__ import annotations

import asyncio

import pytest

from boxxkite.memory_context import (
    ContextBudget,
    assemble_memory_context,
    assemble_prompt_context,
    estimate_token_count,
)


def test_prompt_context_deduplicates_and_keeps_provenance_ids():
    context = assemble_prompt_context(
        {
            "static": [
                {
                    "id": "memory-1",
                    "kind": "preference",
                    "content": "Use dark mode.",
                    "source_session_id": "session-1",
                }
            ],
            "dynamic": [
                {"id": "memory-2", "kind": "goal", "content": "Ship the release."}
            ],
        },
        {
            "memories": [
                {
                    "id": "memory-1",
                    "kind": "preference",
                    "content": "Use dark mode.",
                    "score": 0.95,
                    "source_session_id": "session-1",
                },
                {"id": "memory-3", "kind": "fact", "content": "The API is async."},
            ]
        },
    )

    assert [entry.memory_id for entry in context.entries] == [
        "memory-1",
        "memory-2",
        "memory-3",
    ]
    assert context.memory_ids == ("memory-1", "memory-2", "memory-3")
    assert context.provenance_ids == ("memory-1", "session-1", "memory-2", "memory-3")
    assert context.entries[0].origins == ("profile_static", "recall")
    assert context.entries[0].score == 0.95
    assert context.text.count('memory_id="memory-1"') == 1
    assert 'source_session_id="session-1"' in context.text


def test_prompt_context_is_deterministically_bounded_by_chars_tokens_and_items():
    payload = {
        "static": [
            {"id": "first", "content": "alpha " * 40},
            {"id": "second", "content": "beta " * 40},
            {"id": "third", "content": "gamma " * 40},
        ]
    }
    budget = ContextBudget(max_chars=180, max_tokens=30, max_items=2)

    first = assemble_prompt_context(payload, {}, budget=budget)
    second = assemble_prompt_context(payload, {}, budget=budget)

    assert first == second
    assert first.character_count <= budget.max_chars
    assert first.estimated_tokens <= budget.max_tokens
    assert len(first.entries) <= budget.max_items
    assert first.truncated is True
    assert first.memory_ids
    assert all(identifier in first.text for identifier in first.memory_ids)


def test_prompt_context_deduplicates_records_without_ids_by_normalized_content():
    context = assemble_prompt_context(
        {"static": [{"kind": "fact", "content": "Same   durable fact"}]},
        {"memories": [{"kind": "fact", "content": " same durable fact ", "score": 0.7}]},
    )

    assert len(context.entries) == 1
    assert context.entries[0].origins == ("profile_static", "recall")
    assert context.entries[0].score == 0.7


def test_query_context_leads_with_matching_recalled_evidence():
    context = assemble_prompt_context(
        {"static": [{"id": "profile", "content": "Use dark mode."}]},
        {
            "memories": [
                {"id": "unrelated", "content": "The API is async.", "score": 0.99},
                {
                    "id": "release",
                    "content": "The release is scheduled for Tuesday.",
                    "score": 0.72,
                },
            ]
        },
        query="When is the release scheduled?",
    )

    assert [entry.memory_id for entry in context.entries] == [
        "release",
        "unrelated",
        "profile",
    ]


@pytest.mark.asyncio
async def test_async_helper_fetches_profile_and_recall_concurrently():
    started: set[str] = set()
    both_started = asyncio.Event()

    class FakeMemoryClient:
        async def profile(self, *, scope=None, limit=20):
            started.add("profile")
            if started == {"profile", "recall"}:
                both_started.set()
            await both_started.wait()
            return {"static": [{"id": "profile-1", "content": "Use Python."}], "dynamic": []}

        async def recall(self, *, query, scope=None, limit=10):
            started.add("recall")
            if started == {"profile", "recall"}:
                both_started.set()
            await both_started.wait()
            return {"memories": [{"id": "recall-1", "content": f"Query: {query}"}]}

    context = await assemble_memory_context(
        FakeMemoryClient(),
        query="runtime",
        scope="agent",
        budget=ContextBudget(max_chars=500, max_tokens=100, max_items=10),
    )

    assert started == {"profile", "recall"}
    assert context.memory_ids == ("recall-1", "profile-1")
    assert estimate_token_count(context.text) == context.estimated_tokens


@pytest.mark.asyncio
async def test_async_helper_can_skip_profile_for_low_latency_recall_only():
    class FakeMemoryClient:
        def __init__(self):
            self.profile_calls = 0

        async def profile(self, *, scope=None, limit=20):
            self.profile_calls += 1
            return {"static": [], "dynamic": []}

        async def recall(self, *, query, scope=None, limit=10):
            return {"memories": [{"id": "recall-1", "content": query}]}

    client = FakeMemoryClient()
    context = await assemble_memory_context(client, query="fast path", include_profile=False)

    assert client.profile_calls == 0
    assert context.memory_ids == ("recall-1",)
