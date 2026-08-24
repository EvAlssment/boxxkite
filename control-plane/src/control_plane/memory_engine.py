"""Model-independent extraction and ranking primitives for MemoryBase."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import exp
from typing import Iterable


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_'-]*", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(?:20\d{2})[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])\b")
_NATURAL_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?[\s,]+(20\d{2})\b",
    re.IGNORECASE,
)
_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9'’.-]*)(?:\s+(?:[A-Z][A-Za-z0-9'’.-]*|of|the|and|de|van|von)){1,5}\b"
)
_SINGLE_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9'’.-]{2,}\b")
_ENTITY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "after",
        "and",
        "as",
        "at",
        "before",
        "but",
        "by",
        "ceo",
        "city",
        "continent",
        "country",
        "current",
        "does",
        "how",
        "is",
        "of",
        "on",
        "or",
        "only",
        "the",
        "then",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "while",
    }
)
_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "that", "the", "this",
    "to", "use", "we", "with", "you", "your",
}
_UPDATE_MARKERS = (
    " now ",
    " as of ",
    " currently ",
    " instead",
    " no longer",
    " changed",
    " used to ",
    " rescheduled",
    " replaced",
    " superseded",
    " retired",
)
_STATEMENT_SEPARATORS = (
    " is ",
    " are ",
    " was ",
    " were ",
    " died ",
    " was born ",
    " was created ",
    " was founded ",
    " is a citizen of ",
    " is affiliated with ",
    " is married to ",
    " is employed by ",
    " is located in ",
    " works in ",
    " uses ",
    " used ",
    " prefers ",
    " runs ",
    " is scheduled ",
    " plays the ",
    " is associated with ",
    " is affiliated with ",
    " works in ",
)
_GENERIC_SUBJECT_TOKENS = frozenset(
    {
        "capital",
        "chief",
        "city",
        "company",
        "country",
        "current",
        "government",
        "head",
        "name",
        "organization",
        "state",
        "university",
    }
)


@dataclass(frozen=True)
class ExtractedMemory:
    content: str
    kind: str
    event_dates: list[str]
    importance: float


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def meaningful_tokens(text: str) -> set[str]:
    return {token for token in tokenize(text) if len(token) > 1 and token not in _STOPWORDS}


def infer_kind(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("prefer", "favorite", "favourite", "like to", "dislike")):
        return "preference"
    if any(marker in lowered for marker in ("need to", "plan to", "goal", "will ", "want to")):
        return "goal"
    if any(marker in lowered for marker in ("must ", "always ", "never ", "do not ", "don't ")):
        return "instruction"
    if any(marker in lowered for marker in ("summary:", "in summary", "overall ")):
        return "summary"
    return "fact"


def extract_memories(
    content: str,
    *,
    requested_kind: str | None = None,
    max_items: int = 50,
    document_date: datetime | date | None = None,
) -> list[ExtractedMemory]:
    """Extract bounded atomic candidates without requiring a model provider.

    This fallback deliberately keeps the original text intact as evidence and
    splits only at sentence/line boundaries. A future LLM extractor can return
    the same dataclass without changing persistence or retrieval contracts.
    """
    candidates: list[ExtractedMemory] = []
    seen: set[str] = set()
    for raw in _SPLIT_RE.split(content.strip()):
        sentence = " ".join(raw.split())
        if len(meaningful_tokens(sentence)) < 3:
            continue
        normalized = sentence.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        dates = _extract_event_dates(sentence, document_date=document_date)
        importance = 0.75 if dates else 0.5
        if any(marker in sentence.lower() for marker in ("important", "must", "critical", "favorite")):
            importance = min(1.0, importance + 0.2)
        candidates.append(
            ExtractedMemory(
                content=sentence[:64 * 1024],
                kind=requested_kind or infer_kind(sentence),
                event_dates=dates,
                importance=importance,
            )
        )
        if len(candidates) >= max_items:
            break
    if not candidates and content.strip():
        candidates.append(
            ExtractedMemory(
                content=content.strip()[:64 * 1024],
                kind=requested_kind or infer_kind(content),
                event_dates=_extract_event_dates(content, document_date=document_date),
                importance=0.5,
            )
        )
    return candidates


def _extract_event_dates(
    text: str, *, document_date: datetime | date | None = None
) -> list[str]:
    dates = list(_DATE_RE.findall(text))
    for month, day, year in _NATURAL_DATE_RE.findall(text):
        try:
            normalized = datetime.strptime(
                f"{month} {day} {year}", "%B %d %Y"
            ).date().isoformat()
        except ValueError:
            continue
        if normalized not in dates:
            dates.append(normalized)
    base_date = (
        document_date.date()
        if isinstance(document_date, datetime)
        else document_date
    )
    if base_date is not None:
        dates.extend(_relative_event_dates(text, base_date))
    return list(dict.fromkeys(dates))


_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def resolve_relative_dates(text: str, base_date: date) -> list[date]:
    """Resolve relative date/period language against a reference date.

    Shared by ingest-time event-date extraction and query-time temporal
    scoring so "last Sunday" resolves identically on both sides of recall.
    """

    lowered = text.casefold()
    offsets = {
        "yesterday": -1,
        "today": 0,
        "tomorrow": 1,
    }
    dates = [
        base_date + timedelta(days=offset)
        for marker, offset in offsets.items()
        if re.search(rf"\b{marker}\b", lowered)
    ]
    for match in re.finditer(
        r"\b(last|this|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        lowered,
    ):
        direction, weekday = match.groups()
        target = _WEEKDAY_NAMES.index(weekday)
        distance = (target - base_date.weekday()) % 7
        if direction == "last":
            distance -= 7
        elif direction == "next":
            distance = distance or 7
        dates.append(base_date + timedelta(days=distance))
    for match in re.finditer(r"\b(\d+)\s+days?\s+ago\b", lowered):
        dates.append(base_date - timedelta(days=int(match.group(1))))
    for match in re.finditer(r"\b(?:in|within)\s+(\d+)\s+days?\b", lowered):
        dates.append(base_date + timedelta(days=int(match.group(1))))
    for match in re.finditer(r"\b(\d+)\s+weeks?\s+ago\b", lowered):
        dates.append(base_date - timedelta(weeks=int(match.group(1))))
    for match in re.finditer(r"\b(?:in|within)\s+(\d+)\s+weeks?\b", lowered):
        dates.append(base_date + timedelta(weeks=int(match.group(1))))
    return dates


def _relative_event_dates(text: str, base_date: date) -> list[str]:
    return [value.isoformat() for value in resolve_relative_dates(text, base_date)]


def lexical_score(query: str, content: str) -> float:
    query_tokens = meaningful_tokens(query)
    content_tokens = meaningful_tokens(content)
    if not query_tokens or not content_tokens:
        return 0.0
    overlap = len(query_tokens & content_tokens) / len(query_tokens)
    phrase = " ".join(query.lower().split())
    normalized_content = " ".join(content.lower().split())
    phrase_boost = 0.35 if phrase and phrase in normalized_content else 0.0
    return min(1.0, overlap + phrase_boost)


def recency_score(updated_at: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - updated_at).total_seconds() / 86400)
    return exp(-age_days / 90.0)


def rank_score(*, query: str, content: str, updated_at: datetime, importance: float, now: datetime | None = None) -> float:
    lexical = lexical_score(query, content)
    if lexical == 0.0:
        return 0.0
    return (0.72 * lexical) + (0.18 * recency_score(updated_at, now)) + (0.10 * max(0.0, min(1.0, importance)))


def jaccard_similarity(left: str, right: str) -> float:
    a = meaningful_tokens(left)
    b = meaningful_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def named_entity_phrases(text: str) -> set[str]:
    """Return bounded, case-folded proper-name phrases for graph linking."""

    entities: set[str] = set()
    for match in _ENTITY_RE.findall(text):
        normalized = " ".join(match.casefold().split())
        if normalized.startswith("the "):
            normalized = normalized[4:]
        if normalized and len(normalized) <= 128 and not all(
            token in _ENTITY_STOPWORDS for token in normalized.split()
        ):
            entities.add(normalized)
    for match in _SINGLE_ENTITY_RE.findall(text):
        normalized = match.casefold()
        if normalized not in _ENTITY_STOPWORDS:
            entities.add(normalized)
    return entities


def relation_type(new_content: str, old_content: str) -> str:
    lowered = f" {new_content.lower()} "
    if any(marker in lowered for marker in _UPDATE_MARKERS):
        return "updates"
    if _same_statement_subject(new_content, old_content):
        return "updates"
    if len(meaningful_tokens(new_content)) > len(meaningful_tokens(old_content)):
        return "extends"
    return "related"


def _same_statement_subject(left: str, right: str) -> bool:
    """Detect a new value for the same compact subject without an update verb."""

    def subject(text: str) -> set[str]:
        lowered = f" {text.casefold()} "
        positions = [lowered.find(separator) for separator in _STATEMENT_SEPARATORS]
        positions = [position for position in positions if position >= 0]
        if not positions:
            return set()
        return meaningful_tokens(lowered[: min(positions)])

    left_subject = set(subject(left))
    right_subject = set(subject(right))
    if not left_subject or not right_subject:
        return False
    left_entities = named_entity_phrases(left)
    right_entities = named_entity_phrases(right)
    subject_overlap = len(left_subject & right_subject) / max(
        len(left_subject), len(right_subject)
    )
    generic_subject = bool(
        (left_subject | right_subject) & _GENERIC_SUBJECT_TOKENS
    )
    if (
        left_entities
        and right_entities
        and not left_entities & right_entities
        and (subject_overlap < 0.75 or generic_subject)
    ):
        return False
    left_tokens = meaningful_tokens(left)
    right_tokens = meaningful_tokens(right)
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return False
    return subject_overlap >= 0.75


def dedupe_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.casefold()
        if normalized not in seen:
            seen.add(normalized)
            result.append(value)
    return result
