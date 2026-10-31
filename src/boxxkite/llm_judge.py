"""Standalone rubric-based grading with an injected async model client."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any


ModelClient = Callable[[str], str | dict[str, Any] | Awaitable[str | dict[str, Any]]]


@dataclass(frozen=True)
class Grade:
    score: float
    rationale: str
    transcript_hash: str


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("grading inputs must be finite JSON-compatible values") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class LLMJudge:
    def __init__(
        self,
        rubric: str,
        client: ModelClient,
        *,
        model: str,
        max_score: float = 1.0,
        cache: MutableMapping[str, Grade] | None = None,
    ) -> None:
        if not isinstance(rubric, str) or not rubric.strip():
            raise ValueError("rubric must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty cache namespace")
        if (
            isinstance(max_score, bool)
            or not isinstance(max_score, (int, float))
            or not math.isfinite(max_score)
            or max_score <= 0
        ):
            raise ValueError("max_score must be a finite positive number")
        if not callable(client):
            raise ValueError("client must be callable")
        self.rubric = rubric.strip()
        self.client = client
        self.model = model.strip()
        self.max_score = float(max_score)
        self.cache = cache if cache is not None else {}

    async def grade(self, transcript: Any, final_state: Any = None) -> Grade:
        transcript_hash = _digest(transcript)
        request = {
            "rubric": self.rubric,
            "transcript": transcript,
            "final_state": final_state,
            "max_score": self.max_score,
        }
        cache_key = _digest({"model": self.model, "request": request})
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        prompt = (
            "Grade the submission using the rubric. Return only a JSON object with exactly "
            'the fields "score" (number) and "rationale" (non-empty string). '
            f"Score must be between 0 and {self.max_score}.\n"
            f"{_canonical_json(request)}"
        )
        response = self.client(prompt)
        if inspect.isawaitable(response):
            response = await response
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except json.JSONDecodeError as exc:
                raise ValueError("model response must be a JSON object") from exc
        if not isinstance(response, dict) or set(response) != {"score", "rationale"}:
            raise ValueError('model response must contain exactly "score" and "rationale"')
        score = response["score"]
        rationale = response["rationale"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("score must be a finite number")
        if not float("-inf") < float(score) < float("inf") or not 0 <= score <= self.max_score:
            raise ValueError(f"score must be between 0 and {self.max_score}")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("rationale must be a non-empty string")
        result = Grade(float(score), rationale.strip(), transcript_hash)
        self.cache[cache_key] = result
        return result
