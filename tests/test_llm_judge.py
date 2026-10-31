import json

import pytest

from boxxkite.llm_judge import LLMJudge


@pytest.mark.asyncio
async def test_grade_injects_client_validates_and_caches_identical_request():
    calls = []

    async def client(prompt):
        calls.append(prompt)
        return '{"score": 0.75, "rationale": "Meets most criteria."}'

    judge = LLMJudge("Correctness", client, model="test-model")
    first = await judge.grade([{"tool": "bash", "output": "ok"}], {"tests": "passed"})
    second = await judge.grade([{"output": "ok", "tool": "bash"}], {"tests": "passed"})

    assert first == second
    assert first.score == 0.75
    assert first.rationale == "Meets most criteria."
    assert len(first.transcript_hash) == 64
    assert len(calls) == 1
    assert json.loads(calls[0].splitlines()[-1])["rubric"] == "Correctness"


@pytest.mark.asyncio
async def test_cache_separates_rubric_model_and_final_state():
    cache = {}
    calls = []

    async def client(_):
        calls.append(1)
        return {"score": 1, "rationale": "Complete."}

    transcript = [{"action": "done"}]
    await LLMJudge("A", client, model="m", cache=cache).grade(transcript, {"x": 1})
    await LLMJudge("A", client, model="m", cache=cache).grade(transcript, {"x": 2})
    await LLMJudge("B", client, model="m", cache=cache).grade(transcript, {"x": 1})
    await LLMJudge("A", client, model="other", cache=cache).grade(transcript, {"x": 1})

    assert len(calls) == 4


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        {"score": True, "rationale": "No."},
        {"score": 1.1, "rationale": "Too high."},
        {"score": float("nan"), "rationale": "Invalid."},
        {"score": 0.5, "rationale": "  "},
        {"score": 0.5, "rationale": "ok", "extra": "field"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_model_outputs_are_rejected_and_not_cached(response):
    async def client(_):
        return response

    judge = LLMJudge("Rubric", client, model="test")
    with pytest.raises(ValueError):
        await judge.grade("transcript")
    assert judge.cache == {}


def test_invalid_configuration_is_rejected():
    async def client(_):
        return {"score": 0, "rationale": "ok"}

    with pytest.raises(ValueError):
        LLMJudge(" ", client, model="test")
    with pytest.raises(ValueError):
        LLMJudge("rubric", client, model="test", max_score=0)
