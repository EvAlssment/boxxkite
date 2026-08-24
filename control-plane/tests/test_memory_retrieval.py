from __future__ import annotations

from datetime import datetime, timezone

import pytest
from types import SimpleNamespace

from control_plane.memory_engine import relation_type
from control_plane.memory_retrieval import (
    MAX_CANDIDATES,
    MemoryCandidate,
    MemoryRelation,
    graph_link_phrases,
    lexical_relevance,
    merge_live_and_expanded_results,
    reciprocal_rank_fusion,
    query_answer_type_relevance,
    query_requests_entity_context,
    query_requests_diversity,
    query_variants,
    retrieve_memories,
)


NOW = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)


def memory(memory_id: str, content: str, **kwargs) -> MemoryCandidate:
    return MemoryCandidate(id=memory_id, content=content, updated_at=NOW, **kwargs)


async def test_lexical_fallback_is_deterministic_and_does_not_need_a_provider():
    memories = [
        memory("exact", "Blue-green deployment uses a canary rollout."),
        memory("other", "The support team keeps a weekly incident review."),
    ]

    first = await retrieve_memories(memories, "blue-green canary rollout", now=NOW)
    second = await retrieve_memories(memories, "blue-green canary rollout", now=NOW)

    assert [result.memory_id for result in first] == ["exact"]
    assert [result.memory_id for result in second] == ["exact"]
    assert first[0].semantic_score == 0


async def test_factual_turn_beats_short_social_backchannel():
    results = await retrieve_memories(
        [
            memory("backchannel", "Melanie: Wow, that's awesome, Caroline!"),
            memory("fact", "Caroline: Researching adoption agencies for a future family."),
        ],
        "What did Caroline research?",
        now=NOW,
    )

    assert results[0].memory_id == "fact"


async def test_relationship_status_queries_surface_single_parent_evidence():
    results = await retrieve_memories(
        [
            memory("generic", "Melanie: Agreed, Caroline."),
            memory("status", "Caroline: It will be tough as a single parent, but I am up for the challenge."),
        ],
        "What is Caroline's relationship status?",
        now=NOW,
    )

    assert results[0].memory_id == "status"


async def test_entity_bridges_do_not_promote_social_backchannels_for_simple_facts():
    results = await retrieve_memories(
        [
            memory("backchannel", "Melanie: Agreed, Caroline."),
            memory("fact", "Caroline: It will be tough as a single parent, but I am up for the challenge."),
        ],
        "What is Caroline's relationship status?",
        relations=[
            MemoryRelation("backchannel", "fact", "related", 1.0),
        ],
        now=NOW,
    )

    assert results[0].memory_id == "fact"


async def test_collection_queries_can_follow_entity_related_evidence():
    results = await retrieve_memories(
        [
            memory("first", "Maria: I took a creative writing class."),
            memory("second", "Maria: I took a poetry class."),
            memory("backchannel", "John: That's great, Maria!"),
        ],
        "What writing classes has Maria taken?",
        relations=[MemoryRelation("first", "second", "related", 1.0)],
        now=NOW,
    )

    assert {result.memory_id for result in results[:2]} == {"first", "second"}


async def test_collection_queries_cover_many_mentions_of_the_same_entity():
    memories = [
        memory("painting", "Sam: I was thinking about trying painting."),
        memory("kayaking", "Sam: Kayaking sounds fun; I am considering it."),
        memory("hiking", "Sam: I might try hiking."),
        memory("cooking", "Sam: I want to try cooking."),
        memory("running", "Sam: I am considering running."),
    ]

    results = await retrieve_memories(
        memories,
        "What new hobbies did Sam consider trying?",
        limit=5,
        now=NOW,
    )

    assert {result.memory_id for result in results} == {
        "painting",
        "kayaking",
        "hiking",
        "cooking",
        "running",
    }


@pytest.mark.parametrize(
    "query",
    (
        "What activities does Melanie partake in?",
        "What books has Melanie read?",
        "Where has Melanie camped?",
        "Which of Joanna's screenplays were rejected?",
    ),
)
def test_collection_question_shapes_request_diverse_evidence(query: str):
    assert query_requests_diversity(query)


async def test_lexical_relevance_handles_a_single_typo():
    results = await retrieve_memories(
        [memory("education", "Caroline: I plan to continue my education and pursue counseling.")],
        "What fields would Caroline pursue in her educaton?",
        now=NOW,
    )

    assert results[0].memory_id == "education"


async def test_lexical_relevance_matches_inflected_fact_terms():
    results = await retrieve_memories(
        [
            memory("career", "Caroline: I have been looking into counseling as a career."),
            memory("research", "Caroline: Researching adoption agencies for a future family."),
        ],
        "What did Caroline research?",
        now=NOW,
    )

    assert results[0].memory_id == "research"


def test_meaningful_tokens_normalize_possessive_subjects():
    from control_plane.memory_retrieval import meaningful_tokens

    assert meaningful_tokens("Melanie's daughter's birthday") == (
        "melanie",
        "daughter",
        "birthday",
    )
    assert meaningful_tokens("Melanie talked about her kids'")[-1] == "kids"


async def test_possessive_question_keeps_named_subject_evidence_first():
    results = await retrieve_memories(
        [
            memory(
                "birthday",
                "Melanie: Last night we celebrated my daughter's birthday with a concert.",
            ),
            memory("noise", "Caroline: The concert was wonderful and full of music."),
        ],
        "When is Melanie's daughter's birthday?",
        now=NOW,
    )

    assert results[0].memory_id == "birthday"


async def test_literal_anchor_coverage_beats_semantic_neighbor():
    results = await retrieve_memories(
        [
            memory(
                "council",
                "Caroline: Last Friday I went to a council meeting for adoption.",
            ),
            memory(
                "neighbor",
                "Caroline: I am excited about the adoption process and helping children.",
            ),
        ],
        "When did Caroline go to the adoption meeting?",
        now=NOW,
    )

    assert results[0].memory_id == "council"


async def test_query_variants_recover_identity_and_frequency_language():
    results = await retrieve_memories(
        [
            memory("generic", "Caroline: Glad you agree with me."),
            memory("identity", "Caroline: I am a transgender woman."),
            memory("frequency", "Audrey: I take my dogs out multiple times a day."),
        ],
        "What is Caroline's identity?",
        now=NOW,
    )

    assert results[0].memory_id == "identity"
    assert "transgender" in " ".join(query_variants("What is Caroline's identity?") )
    frequency = await retrieve_memories(
        [
            memory("vague", "Audrey: We go on adventures very often."),
            memory("explicit", "Audrey: I take my dogs out multiple times a day."),
        ],
        "How often does Audrey walk her dogs?",
        now=NOW,
    )
    assert frequency[0].memory_id == "explicit"


def test_query_variants_cover_paraphrased_lists_and_temporal_inference():
    variants = " ".join(
        query_variants("What fantasy movies does Tim like?")
        + query_variants("What items did John mention having as a child?")
        + query_variants("Would Melanie go on another roadtrip soon?")
    ).casefold()

    assert "fantasy films" in variants
    assert "childhood objects" in variants
    assert "future travel plans" in variants

    activity_variants = " ".join(query_variants("What activities does Melanie partake in?")).casefold()
    assert "swimming" in activity_variants
    assert "painting" in activity_variants
    assert "family" in activity_variants

    focused_variants = " ".join(
        query_variants("What does this reminder stand for?")
        + query_variants("What book did Melanie recommend?")
        + query_variants("What underlying condition might explain this?")
    ).casefold()
    assert "symbolizes" in focused_variants
    assert "recommended" in focused_variants
    assert "asthma" in focused_variants


def test_answer_type_relevance_preserves_count_and_comparison_evidence():
    assert query_answer_type_relevance("How many weddings did I attend?", "I attended three weddings.") == 1.0
    assert query_answer_type_relevance(
        "Which show did I start first, 'The Crown' or 'Game of Thrones'?",
        "I just finished watching The Crown.",
    ) == 0.5
    assert query_answer_type_relevance(
        "Who is the chief of state of the country?",
        "The name of the current head of state in the country is Alex Smith.",
    ) == 0.9
    assert query_answer_type_relevance(
        "Who is the chief of state of the country?",
        "The name of the current head of the country government is Jamie Smith.",
    ) == 0.0
    assert query_requests_diversity("How many hikes has Joanna been on?")
    assert query_requests_diversity("What are some foods Audrey likes?")
    assert not query_requests_entity_context("What nickname does Nate use for Joanna?")
    assert query_answer_type_relevance(
        "Which individual is responsible for founding the organization?",
        "The organization was founded by Alex Smith.",
    ) == 0.9
    assert query_answer_type_relevance(
        "In which position did the person serve?",
        "The director of the company is Alex Smith.",
    ) == 0.9


def test_graph_link_phrases_include_lowercase_fact_entities():
    assert "association football" in graph_link_phrases(
        "association football was created in the country of Italy."
    )
    assert "as of" not in graph_link_phrases(
        "As of 2026-08-01, production uses canary releases."
    )


def test_graph_result_merge_preserves_live_prefix_and_fills_from_expansion():
    base = [
        SimpleNamespace(memory_id="live-1"),
        SimpleNamespace(memory_id="live-2"),
    ]
    expanded = [
        SimpleNamespace(memory_id="history-1"),
        SimpleNamespace(memory_id="live-1"),
    ]

    merged = merge_live_and_expanded_results(base, expanded)

    assert [result.memory_id for result in merged] == [
        "live-1",
        "history-1",
        "live-2",
    ]
    with pytest.raises(ValueError):
        merge_live_and_expanded_results(base, expanded, preserve_prefix=-1)


def test_generic_named_subjects_do_not_cross_supersede():
    assert relation_type(
        "The name of the Italy government is Giuseppe Conte.",
        "The name of the Tucson government is Jonathan Rothschild.",
    ) != "updates"
    assert relation_type(
        "point guard is associated with the sport of cricket.",
        "point guard is associated with the sport of basketball.",
    ) == "updates"


def test_answer_type_precedence_prefers_requested_endpoint_shape():
    assert query_answer_type_relevance(
        "What is the religious affiliation of the person who founded the movement?",
        "A person is affiliated with the religion of atheism.",
    ) == 0.95
    assert query_answer_type_relevance(
        "Who holds the position of chairperson in the organization?",
        "The chairperson of the organization is Alex Smith.",
    ) == 0.95


def test_answer_type_relevance_covers_long_chain_endpoint_phrases():
    assert query_answer_type_relevance(
        "What is the language of the work that was created by the author?",
        "Shahnameh was written in the language of Persian.",
    ) == 0.9
    assert query_answer_type_relevance(
        "From which educational institution did the author receive their education?",
        "The university where Samuel Beckett was educated is University of Southern California.",
    ) == 0.9
    assert query_answer_type_relevance(
        "What was the sport that Hines Ward competed in professionally?",
        "cornerback is associated with the sport of field hockey.",
    ) == 0.75


def test_same_subject_fact_templates_are_updates_even_when_values_differ():
    assert relation_type(
        "Olga of Kiev died in the city of Rodez.",
        "Olga of Kiev died in the city of Kyiv.",
    ) == "updates"
    assert relation_type(
        "association football was created in the country of Italy.",
        "association football was created in the country of England.",
    ) == "updates"


def test_reciprocal_rank_fusion_applies_weights_and_deduplicates_each_list():
    fused = reciprocal_rank_fusion(
        [["a", "b", "b"], ["b", "a"]], weights=[1.0, 2.0], k=10
    )

    assert fused["b"] > fused["a"]
    assert set(fused) == {"a", "b"}


async def test_temporal_query_prefers_matching_event_date_over_newer_memory():
    memories = [
        memory(
            "exact-date",
            "Migration incident details",
            event_dates=("2026-08-20",),
        ),
        memory(
            "near-date",
            "Migration incident details",
            event_dates=("2026-08-21",),
        ),
    ]

    results = await retrieve_memories(memories, "migration incident on 2026-08-20", now=NOW)

    assert results[0].memory_id == "exact-date"
    assert results[0].temporal_score == 1


async def test_current_query_prefers_non_superseded_memory():
    memories = [
        memory(
            "old",
            "The deployment uses blue-green rollout.",
            superseded_at=NOW,
            superseded_by_id="current",
        ),
        memory("current", "The deployment uses canary rollout now."),
    ]

    results = await retrieve_memories(memories, "What is the current deployment strategy?", now=NOW)

    assert results[0].memory_id == "current"
    assert results[0].state_score == 1.0


async def test_replacement_query_prefers_live_replacement():
    memories = [
        memory(
            "old",
            "Before July, backups ran weekly on Sundays.",
            superseded_at=NOW,
            superseded_by_id="current",
        ),
        memory("current", "Backups now run daily at 02:00 UTC."),
    ]

    results = await retrieve_memories(
        memories, "What schedule replaced weekly Sunday backups?", now=NOW
    )

    assert results[0].memory_id == "current"


async def test_dates_in_content_participate_in_temporal_relevance():
    memories = [
        memory("exact", "The review happens on 2026-08-20."),
        memory("near", "The review happens on 2026-08-21."),
    ]

    results = await retrieve_memories(memories, "review on 2026-08-20", now=NOW)

    assert results[0].memory_id == "exact"
    assert results[0].temporal_score == 1.0


async def test_natural_language_dates_and_months_participate_in_temporal_relevance():
    memories = [
        memory("january", "Deborah received an appreciation letter on January 26, 2023."),
        MemoryCandidate(
            id="november",
            content="Tim planned a trip to the UK in the second week of November.",
            updated_at=datetime(2023, 11, 8, tzinfo=timezone.utc),
            document_date=datetime(2023, 11, 8, tzinfo=timezone.utc),
        ),
    ]

    exact = await retrieve_memories(
        memories,
        "When did Deborah receive the letter on January 26, 2023?",
        now=NOW,
    )
    assert exact[0].memory_id == "january"
    assert exact[0].temporal_score == 1.0

    month = await retrieve_memories(
        memories,
        "Which country was Tim visiting in the second week of November?",
        now=NOW,
    )
    assert month[0].memory_id == "november"


async def test_as_of_query_matches_memory_update_date():
    memories = [
        MemoryCandidate(
            id="review",
            content="The architecture review is scheduled for 2026-10-01.",
            event_dates=("2026-10-01",),
            updated_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        ),
        memory(
            "decoy",
            "A database review is scheduled for 2026-08-12.",
            event_dates=("2026-08-12",),
        ),
    ]

    results = await retrieve_memories(
        memories, "As of 2026-08-12, when is the architecture review?", now=NOW
    )

    assert results[0].memory_id == "review"


async def test_relation_boost_can_surface_a_connected_memory():
    memories = [
        memory("anchor", "Python build tooling is standardized."),
        memory("connected", "The deployment automation runbook."),
        memory("unrelated", "A quarterly hiring review."),
    ]
    relations = [
        MemoryRelation(
            source_memory_id="anchor",
            target_memory_id="connected",
            relation_type="extends",
            confidence=1.0,
        )
    ]

    results = await retrieve_memories(
        memories,
        "Python build tooling",
        relations=relations,
        limit=2,
        now=NOW,
    )

    connected = next(result for result in results if result.memory_id == "connected")
    assert connected.relation_boost == 0.85


async def test_ambiguous_singleton_names_do_not_create_graph_bridges():
    memories = [
        memory("anchor", "Charles Darwin is associated with natural history."),
        memory("related", "Charles Darwin is married to Emma Darwin."),
        memory("ambiguous", "Charles Messier is a citizen of France."),
        memory("ambiguous-2", "Charles Dickens was born in Portsmouth."),
    ]

    results = await retrieve_memories(
        memories,
        "What is the country of citizenship of the spouse of Charles Darwin?",
        limit=3,
        now=NOW,
    )

    ambiguous = next(result for result in results if result.memory_id == "ambiguous")
    assert ambiguous.relation_boost == 0.0


async def test_mmr_reduces_near_duplicate_results():
    memories = [
        memory("primary", "Python tooling for API services and deployment."),
        memory("duplicate", "Python tooling for API services and testing."),
        memory("diverse", "Python tooling for notebooks and data analysis."),
    ]

    results = await retrieve_memories(
        memories,
        "Python tooling",
        limit=2,
        mmr_lambda=0.35,
        now=NOW,
    )

    assert results[0].memory_id == "primary"
    assert results[1].memory_id == "diverse"


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        vectors = []
        for text in texts:
            if text == "semantic query":
                vectors.append((1.0, 0.0))
            elif "semantic" in text:
                vectors.append((0.98, 0.02))
            else:
                vectors.append((0.0, 1.0))
        return vectors


async def test_async_embedding_provider_is_one_optional_ranked_channel():
    provider = FakeEmbeddingProvider()
    results = await retrieve_memories(
        [
            memory("lexical", "semantic query is documented here."),
            memory("semantic", "A conceptually similar architecture note."),
        ],
        "semantic query",
        embedding_provider=provider,
        now=NOW,
    )

    assert len(provider.calls) == 1
    assert len(provider.calls[0]) == 3
    assert all(result.semantic_score >= 0 for result in results)
    assert any(result.semantic_score > 0.9 for result in results)


async def test_precomputed_semantic_scores_are_a_ranked_channel():
    results = await retrieve_memories(
        [
            memory("lexical", "rocket engine documentation"),
            memory("semantic", "spaceship propulsion notes"),
        ],
        "rocket engine",
        precomputed_semantic_scores={"semantic": 1.0, "unknown": 1.0},
        now=NOW,
    )

    semantic = next(result for result in results if result.memory_id == "semantic")
    assert semantic.semantic_score == 1.0


async def test_embedding_failure_falls_back_unless_strict_mode_is_requested():
    class BrokenProvider:
        async def embed(self, texts):
            raise AssertionError("provider failure")

    memories = [memory("one", "deterministic fallback result")]
    fallback = await retrieve_memories(
        memories, "fallback result", embedding_provider=BrokenProvider(), now=NOW
    )
    assert fallback[0].memory_id == "one"

    with pytest.raises(AssertionError, match="provider failure"):
        await retrieve_memories(
            memories,
            "fallback result",
            embedding_provider=BrokenProvider(),
            strict_embeddings=True,
            now=NOW,
        )


async def test_inputs_are_bounded_and_duplicate_ids_are_rejected():
    with pytest.raises(ValueError, match="query exceeds"):
        await retrieve_memories([memory("one", "content")], "x" * 4_097, now=NOW)

    too_many = [memory(str(index), "bounded content") for index in range(MAX_CANDIDATES + 1)]
    with pytest.raises(ValueError, match="cannot exceed"):
        await retrieve_memories(too_many, "bounded", now=NOW)

    with pytest.raises(ValueError, match="duplicate memory id"):
        await retrieve_memories(
            [memory("same", "first"), memory("same", "second")], "first", now=NOW
        )


async def test_relative_weekday_query_resolves_against_the_reference_date():
    memories = [
        memory(
            "correct-date",
            "Attended a support group meeting.",
            event_dates=("2026-08-14",),
        ),
        memory(
            "near-date",
            "Attended a support group meeting.",
            event_dates=("2026-08-07",),
        ),
    ]

    results = await retrieve_memories(memories, "What did I do last Friday?", now=NOW)

    assert results[0].memory_id == "correct-date"
    assert results[0].temporal_score == 1.0
    assert results[1].temporal_score < 1.0


async def test_relative_day_count_query_resolves_against_the_reference_date():
    memories = [
        memory("exact", "Filed the incident report.", event_dates=("2026-08-13",)),
        memory("near", "Filed the incident report.", event_dates=("2026-08-12",)),
    ]

    results = await retrieve_memories(memories, "What happened 3 days ago?", now=NOW)

    assert results[0].memory_id == "exact"
    assert results[0].temporal_score == 1.0


async def test_anchor_scores_prefer_rare_specific_terms_over_common_topic_overlap():
    decoys = [
        memory(f"decoy-{i}", f"John: I love talking about my basketball career, session {i}.")
        for i in range(6)
    ]
    target = memory("target", "John: My real ambition is to win a championship this season.")

    results = await retrieve_memories(
        [*decoys, target],
        "What is John's basketball career goal?",
        now=NOW,
    )

    assert results[0].memory_id == "target"


async def test_anchor_scores_tolerate_singular_plural_mismatch():
    results = await retrieve_memories(
        [
            memory("generic", "John: Basketball has been a big part of my life since I was a kid."),
            memory("goal", "John: My goal is to improve my shooting percentage this year."),
        ],
        "What are John's goals?",
        now=NOW,
    )

    assert results[0].memory_id == "goal"


def test_goal_questions_are_not_captured_by_the_career_job_bucket():
    """A "goals ... career" question is about aspirations, not John's job - regression
    test for the answer-type bucket that used to reward any mention of "career" over an
    actual stated goal, because the job/career branch fired before a goal-specific one
    existed."""

    query = "What are John's goals with regards to his basketball career?"

    goal_score = query_answer_type_relevance(query, "Winning a championship is my number one goal.")
    career_mention_score = query_answer_type_relevance(query, "I've had some amazing moments in my career as a player.")

    assert goal_score > career_mention_score


def test_common_auxiliary_verb_does_not_outrank_the_real_topic_match():
    """A word like "done" can be genuinely rare within one small conversation's corpus
    purely by coincidence, which used to give it an inflated idf and let an unrelated
    message that happens to contain it outscore the one that actually answers the
    question, since "martial"/"arts" don't match it at all - regression test for
    flooring the effective document frequency (found via a real 2,000-message LoCoMo
    conversation where this exact query surfaced an unrelated veterans-volunteering
    message above the actual kickboxing/taekwondo answer)."""

    query = "What martial arts has John done?"
    unrelated_but_matches_done = (
        "John: I've always been passionate about veterans, and wanted to show what "
        "I've done for them through volunteering."
    )
    target = "John: I'm doing kickboxing and it's giving me so much energy."
    document_frequency = {"done": 2, "john": 800, "kickboxing": 3}

    unrelated_score = lexical_relevance(
        query, unrelated_but_matches_done, document_frequency=document_frequency, total_documents=2000
    )
    target_score = lexical_relevance(
        query, target, document_frequency=document_frequency, total_documents=2000
    )

    assert target_score >= unrelated_score


def test_token_idf_stays_bounded_for_tiny_corpora():
    """The document-frequency floor used to elevate the effective df above the total
    document count for small corpora, driving idf negative and silently dropping exact
    matches from a one- or two-memory account - the case every unit test with a single
    seeded memory exercises."""

    from control_plane.memory_retrieval import _token_idf

    assert _token_idf({"rollout": 1}, 1, "rollout") > 0.0
    assert _token_idf({"rollout": 1}, 2, "rollout") > 0.0
