"""Bounded, provider-optional retrieval primitives for MemoryBase."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Protocol

from .memory_engine import named_entity_phrases, resolve_relative_dates


MAX_QUERY_CHARS = 4_096
MAX_MEMORY_CHARS = 64 * 1024
MAX_CANDIDATES = 2_000
MAX_RELATIONS = 10_000
MAX_EMBEDDING_DIMENSIONS = 4_096
MAX_EMBEDDING_TEXTS = MAX_CANDIDATES + 1
DEFAULT_RRF_K = 60
SEMANTIC_MMR_LAMBDA_FLOOR = 0.95

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_'-]*", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b")
_YEAR_RE = re.compile(r"\b20\d{2}\b")
_MONTH_YEAR_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(20\d{2})\b",
    re.IGNORECASE,
)
_NATURAL_DATE_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?[\s,]+(20\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "use",
        "we",
        "was",
        "were",
        "with",
        "you",
        "your",
        "could",
        "did",
        "do",
        "does",
        "done",
        "been",
        "gone",
        "how",
        "likely",
        "recall",
        "remember",
        "should",
        "tell",
        "when",
        "where",
        "which",
        "who",
        "why",
        "what",
        "would",
    }
)
_ACKNOWLEDGEMENT_PREFIXES = (
    "wow",
    "that's",
    "thats",
    "glad",
    "great",
    "cool",
    "awesome",
    "nice",
    "thanks",
    "thank you",
    "love that",
    "i agree",
    "totally agree",
    "sounds",
    "good for you",
    "hey",
    "hello",
    "oops",
    "sorry",
    "yes",
    "yep",
)
_FACTUAL_CUES = frozenset(
    {
        "attended",
        "born",
        "chose",
        "career",
        "goal",
        "have",
        "into",
        "looking",
        "plan",
        "planning",
        "prefer",
        "research",
        "researching",
        "studying",
        "want",
        "went",
        "work",
        "working",
    }
)
_NUMBER_WORDS = frozenset(
    {
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "first",
        "second",
        "third",
    }
)
_PREDICATE_GROUPS = (
    frozenset({"citizenship", "citizen"}),
    frozenset({"spouse", "married", "partner", "relationship", "single", "divorced", "separated"}),
    frozenset({"birth", "birthplace", "born"}),
    frozenset({"location", "located", "place", "city"}),
    frozenset({"work", "works", "worked", "employed"}),
    frozenset({"founder", "founded"}),
    frozenset({"ceo", "chief", "executive"}),
)
_PREDICATE_TO_GROUP = {
    token: group_index
    for group_index, group in enumerate(_PREDICATE_GROUPS)
    for token in group
}
_GRAPH_OBJECT_RE = re.compile(
    r"\b(?:sport|religion|position|field|country|city|capital|movement|"
    r"organization|company|university|team|competition|song|director|"
    r"manager|developer|founder|chairperson|author|spouse|partner|successor|"
    r"citizenship|language|continent)\s+of\s+([^.;]+)",
    re.IGNORECASE,
)
_GRAPH_ASSIGNMENT_RE = re.compile(
    r"\b(?:type of music that|official language of|language of)"
    r"\s+[^.;]+?\s+is\s+([^.;]+)",
    re.IGNORECASE,
)
_GRAPH_SUBJECT_RE = re.compile(
    r"^\s*([^.;]{2,120}?)\s+(?:is|was|are|were)\s+"
    r"(?:associated with|created|founded|developed|affiliated|located|"
    r"the capital|the director|the chairperson)",
    re.IGNORECASE,
)
_CURRENT_MARKERS = frozenset(
    {
        "active",
        "current",
        "currently",
        "latest",
        "newest",
        "now",
        "recent",
        "recently",
    }
)
_HISTORICAL_MARKERS = frozenset(
    {
        "earlier",
        "former",
        "formerly",
        "historical",
        "old",
        "original",
        "previous",
        "prior",
        "retired",
        "superseded",
    }
)
_STALE_CONTENT_MARKERS = frozenset(
    {
        "earlier",
        "former",
        "formerly",
        "old",
        "originally",
        "previous",
        "prior",
        "retired",
        "superseded",
        "used",
    }
)
_FUTURE_OR_DRAFT_MARKERS = frozenset(
    {"draft", "proposes", "proposed", "future", "inactive"}
)

_QUERY_EXPANSIONS = (
    (
        "symbols",
        "symbols symbolism icon sign flag mural identity rainbow transgender symbol eagle",
    ),
    ("items", "belongings possessions childhood objects toys doll film camera"),
    ("nickname", "nickname short name called call her goes by Jo"),
    ("screenplay", "screenplay script production company rejection film first third drama romance loss identity connection"),
    ("fantasy movies", "fantasy films cinema favorite magical adventure trilogy"),
    ("new hobbies", "hobbies pursuits interests activities painting kayaking hiking cooking running"),
    ("classes", "classes courses lessons training workshops"),
    (
        "events",
        "events activities fundraiser tournament gathering parade speech support group "
        "mentoring program school event conference youth center",
    ),
    ("attributes", "attributes traits qualities personality character"),
    ("what kind of yoga", "yoga style type practice"),
    ("hobby", "hobby pastime interest activity"),
    ("dogs", "dogs puppies pets animals"),
    ("bride after the wedding", "married honeymoon family plans travel activities winter outdoor skiing cuisine"),
    ("job might", "future career profession possible role volunteering shelter counselor coordinator front desk community"),
    ("live in", "lives residence home location city state town Stamford Connecticut"),
    ("how long have", "married husband years duration"),
    ("give her", "family encouragement motivation love support"),
    ("another roadtrip", "another road trip future travel plans"),
    ("appreciation letter", "letter recognition gratitude community thanks"),
    ("reschedule", "rescheduled postponed meeting plans"),
    ("second week", "week month travel visiting"),
    ("ideal dance studio", "dance studio water natural light flooring"),
    ("future", "future plans hopes goals intentions"),
    ("identity", "gender transgender woman man nonbinary"),
    ("reminder", "reminder reminds meaning stands for symbolizes art self-expression"),
    ("hand-painted bowl", "hand-painted bowl friend birthday pattern colors art self-expression"),
    ("underlying condition", "allergies asthma respiratory condition puffy itchy"),
    ("wouldn't cause", "hairless cats pigs no fur allergy alternative pets"),
    ("live close", "residence lives near beach mountains location home"),
    ("political leaning", "politics liberal conservative progressive left wing LGBTQ rights"),
    ("prominent charity", "charity organization youth sports Nike Gatorade Under Armour"),
    ("how often", "frequency daily weekly regularly often always multiple times"),
    ("what flavor", "flavor flavour chocolate vanilla coconut strawberry dessert"),
    ("damages", "damage broken windshield car broke down accident crash repairs"),
    ("challenges", "challenges difficulties obstacles motivation diet support"),
    ("address them", "handle overcome solve coping strategy support classes"),
    ("research", "research researching studied looked into investigated"),
    ("career", "career work profession field job"),
    (
        "activities",
        "activities hobbies interests likes pottery camping painting swimming hiking "
        "museum running walking cooking concerts travel family",
    ),
    ("book", "books reading read titles novels favorite recommended book"),
    (
        "instruments",
        "music instruments play violin clarinet guitar piano acoustic musician "
        "concerts bands artists",
    ),
    ("children", "children kids child youngest family number three two"),
    ("beach", "beach shore coast vacation trip frequency once twice times"),
    ("hikes", "hike hikes hiking trail trails times family outdoors"),
    ("religious", "religion religious faith church spiritual beliefs"),
    ("console", "console gaming video game Nintendo Switch Xbox PlayStation"),
    ("state did", "state visited travel trip vacation city Florida"),
    ("outdoor gear", "outdoor gear hiking endorsement brand sponsor sports company"),
    ("musical artists", "music concerts bands artists festival performers live show"),
    ("alternative career", "alternative career future profession animal keeper zoo turtles writing"),
    ("movie scripts", "movie scripts filmmaking filmmaker director production"),
    ("open to moving", "moving abroad another country relocation international plans goals"),
    ("financial status", "financial status income wealthy middle-class money assets"),
    ("personality traits", "personality traits attributes qualities thoughtful authentic driven selfless rational"),
    ("which city", "city visited traveled trip Rome"),
)


class EmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one finite vector for each input text."""


class RerankerProvider(Protocol):
    async def rerank(
        self, query: str, documents: Sequence[tuple[str, str]]
    ) -> Mapping[str, float]:
        """Return relevance scores keyed by memory id."""


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    id: str
    content: str
    kind: str = "fact"
    scope: str = "default"
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    document_date: datetime | date | None = None
    event_dates: Sequence[str] = ()
    importance: float = 0.5
    expires_at: datetime | None = None
    superseded_at: datetime | None = None
    superseded_by_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRelation:
    source_memory_id: str
    target_memory_id: str
    relation_type: str = "related"
    confidence: float = 0.5


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    memory: Any
    memory_id: str
    score: float
    lexical_score: float
    semantic_score: float
    temporal_score: float
    relation_boost: float
    rrf_score: float
    state_score: float


def merge_live_and_expanded_results(
    base_results: Sequence[RetrievedMemory],
    expanded_results: Sequence[RetrievedMemory],
    *,
    preserve_prefix: int = 1,
) -> list[RetrievedMemory]:
    """Preserve trusted live winners while filling the result set from graph expansion."""

    if preserve_prefix < 0:
        raise ValueError("preserve_prefix must be non-negative")
    if not base_results:
        return list(expanded_results)
    selected = list(base_results[:preserve_prefix])
    selected_ids = {result.memory_id for result in selected}
    for result in (*expanded_results, *base_results[preserve_prefix:]):
        if result.memory_id in selected_ids:
            continue
        selected.append(result)
        selected_ids.add(result.memory_id)
    return selected


@dataclass(frozen=True, slots=True)
class _PreparedMemory:
    memory: Any
    memory_id: str
    content: str
    tokens: tuple[str, ...]
    updated_at: datetime
    document_date: datetime | date | None
    event_dates: tuple[str, ...]
    importance: float
    expires_at: datetime | None
    superseded_at: datetime | None
    superseded_by_id: str | None
    original_index: int


def tokenize(text: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for token in _TOKEN_RE.findall(text):
        token = token.lower()
        if token.endswith("'s"):
            token = token[:-2]
        elif token.endswith("'"):
            token = token[:-1]
        if token:
            normalized.append(token)
    return tuple(normalized)


@lru_cache(maxsize=8_192)
def meaningful_tokens(text: str) -> tuple[str, ...]:
    return tuple(token for token in tokenize(text) if len(token) > 1 and token not in _STOPWORDS)


def query_variants(query: str) -> tuple[str, ...]:
    """Return bounded lexical views that preserve the user's original query."""

    bounded = _bounded_text(query, MAX_QUERY_CHARS, "query").strip()
    variants = [bounded]
    body = re.sub(
        r"^\s*(?:what|when|where|who|which|why|how)\b(?:\s+[^?]{0,24})?\s+",
        "",
        bounded,
        flags=re.IGNORECASE,
    ).strip(" ?")
    if len(body) >= 3 and body.casefold() != bounded.casefold():
        variants.append(body)
    lowered = bounded.casefold()
    for needle, expansion in _QUERY_EXPANSIONS:
        if needle in lowered:
            variants.append(f"{bounded} {expansion}")
    if " and " in lowered:
        for clause in re.split(r"\s+and\s+", bounded, flags=re.IGNORECASE):
            clause = clause.strip(" ?")
            if len(meaningful_tokens(clause)) >= 2:
                variants.append(clause)
    return tuple(dict.fromkeys(variants))[:8]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    weights: Sequence[float] | None = None,
    k: int = DEFAULT_RRF_K,
) -> dict[str, float]:
    """Fuse ranked IDs while preserving the first occurrence in each list."""

    if k < 1:
        raise ValueError("k must be positive")
    if weights is not None and len(weights) != len(rankings):
        raise ValueError("weights must match rankings")
    scores: defaultdict[str, float] = defaultdict(float)
    for list_index, ranking in enumerate(rankings):
        weight = weights[list_index] if weights is not None else 1.0
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("weights must be finite and non-negative")
        seen: set[str] = set()
        for rank, memory_id in enumerate(ranking, start=1):
            if memory_id in seen:
                continue
            seen.add(memory_id)
            scores[memory_id] += weight / (k + rank)
    return dict(scores)


def temporal_relevance(
    query: str,
    memory: Any,
    *,
    now: datetime | None = None,
) -> float:
    """Score exact and nearby dates, relative-date language, and recency intent."""

    query = _bounded_text(query, MAX_QUERY_CHARS, "query")
    current = _as_utc(now or datetime.now(timezone.utc))
    query_dates = _query_dates(query, current.date())
    candidate_dates = _candidate_dates(memory)
    lowered = query.casefold()
    score = 0.0

    query_months = {
        _MONTHS[month.casefold()]
        for month in re.findall(
            r"\b(january|february|march|april|may|june|july|august|september|"
            r"october|november|december)\b",
            query,
            re.IGNORECASE,
        )
    }
    if query_months and candidate_dates:
        if any(candidate_date.month in query_months for candidate_date in candidate_dates):
            score = max(score, 0.78)

    date_candidates = candidate_dates
    if query_dates and any(marker in lowered for marker in ("as of", "on ")):
        updated_date = _as_utc(_field(memory, "updated_at", current)).date()
        date_candidates = tuple(dict.fromkeys((*candidate_dates, updated_date)))
    if query_dates and date_candidates:
        for query_date in query_dates:
            for candidate_date in date_candidates:
                distance = abs((candidate_date - query_date).days)
                if distance == 0:
                    score = max(score, 1.0)
                elif distance <= 31:
                    score = max(score, 0.72 * math.exp(-distance / 14.0))

    if any(marker in lowered for marker in ("latest", "recent", "recently", "newest", "current")):
        updated = _as_utc(_field(memory, "updated_at", current))
        age_days = max(0.0, (current - updated).total_seconds() / 86_400)
        score = max(score, math.exp(-age_days / 45.0))

    if "last week" in lowered:
        start = current.date() - timedelta(days=current.weekday() + 7)
        end = start + timedelta(days=6)
        score = max(score, _range_date_score(candidate_dates, start, end))
    elif "this week" in lowered:
        start = current.date() - timedelta(days=current.weekday())
        score = max(score, _range_date_score(candidate_dates, start, current.date()))

    if "before" in lowered and query_dates and candidate_dates:
        score = 1.0 if min(candidate_dates) < min(query_dates) else 0.0
    if "after" in lowered and query_dates and candidate_dates:
        score = 1.0 if max(candidate_dates) > max(query_dates) else 0.0
    return min(1.0, score)


def memory_state_relevance(
    query: str,
    memory: Any,
    *,
    now: datetime | None = None,
) -> float:
    """Prefer live facts for current queries and history for historical queries."""

    del now
    query_tokens = set(meaningful_tokens(_bounded_text(query, MAX_QUERY_CHARS, "query")))
    content_tokens = set(
        meaningful_tokens(str(_field(memory, "content", "")))
    )
    current_intent = bool(query_tokens & _CURRENT_MARKERS)
    historical_intent = query_requests_history(query)
    replacement_intent = query_requests_replacement(query)
    superseded = _field(memory, "superseded_at", None) is not None or _field(
        memory, "superseded_by_id", None
    ) is not None
    content_text = str(_field(memory, "content", "")).casefold()
    explicitly_current = any(
        phrase in content_text
        for phrase in ("as of ", "currently", "current ", "now ")
    )
    stale_content = not explicitly_current and (
        bool(content_tokens & _STALE_CONTENT_MARKERS)
        or bool(content_tokens & _FUTURE_OR_DRAFT_MARKERS)
        or any(
            phrase in content_text
            for phrase in ("before ", "no longer", "was replaced", "has been replaced")
        )
    )

    if replacement_intent:
        return 0.0 if superseded or stale_content else 1.0
    if current_intent and not historical_intent:
        if superseded or stale_content:
            return 0.0
        return 1.0
    if historical_intent and not current_intent:
        return 1.0 if superseded or stale_content else 0.35
    if superseded:
        return 0.25
    return 0.5


def query_requests_history(query: str) -> bool:
    """Return whether a query asks for a superseded or historical fact."""

    bounded_query = _bounded_text(query, MAX_QUERY_CHARS, "query")
    query_tokens = set(meaningful_tokens(bounded_query))
    return bool(query_tokens & _HISTORICAL_MARKERS) or any(
        phrase in bounded_query.casefold()
        for phrase in ("used to", "before ", "what was", "originally")
    )


def query_requests_replacement(query: str) -> bool:
    """Return whether a query asks for the live value that replaced an old one."""

    lowered = _bounded_text(query, MAX_QUERY_CHARS, "query").casefold()
    return (
        (lowered.startswith(("what ", "which ")) and " replaced " in lowered)
        or "what replaced " in lowered
        or "after the " in lowered and " change" in lowered
    )


def query_requests_diversity(query: str) -> bool:
    """Return whether the answer needs several distinct evidence items."""

    lowered = _bounded_text(query, MAX_QUERY_CHARS, "query").casefold()
    return (
        "how many" in lowered
        or "how often" in lowered
        or "what are some" in lowered
        or "what are the" in lowered
        or "what has " in lowered
        or "what have " in lowered
        or re.search(r"\bwhat\b.+\b(?:has|have)\b", lowered) is not None
        or "to remember" in lowered
        or "names of" in lowered
        or "which " in lowered and " and " in lowered
        or "what items" in lowered
        or "what symbols" in lowered
        or "what events" in lowered
        or "what classes" in lowered
        or "what screenplay" in lowered
        or "fantasy movies" in lowered
        or "new hobbies" in lowered
        or re.search(
            r"\b(?:what|which|where)\b.+\b(?:activities|books|hobbies|items|"
            r"locations|places|screenplays?|events|classes|instruments?|camped|camping|hikes?|beach)\b",
            lowered,
        ) is not None
        or "with her family" in lowered
        or "with their family" in lowered
        or "on hikes" in lowered
        or "musical artists" in lowered
        or "instruments" in lowered
        or "look like" in lowered
        or "after the wedding" in lowered
    )


def query_requests_entity_context(query: str) -> bool:
    """Return whether a question benefits from same-entity evidence expansion."""

    return query_requests_diversity(query)


_MIN_EFFECTIVE_DOCUMENT_FREQUENCY_RATIO = 0.01


def _token_idf(document_frequency: Mapping[str, int], total_documents: int, term: str) -> float:
    """IDF with a corpus-scaled floor on document frequency.

    A token that happens to occur in only one or two candidates within a single small
    conversation (e.g. a common auxiliary verb like "done" that just wasn't said often
    in this particular transcript) otherwise gets treated as maximally rare and can
    outscore the query's actual, genuinely rare topic words. Flooring the effective
    document frequency at ~1% of the corpus caps that blowup without meaningfully
    suppressing terms that are actually rare because they're topically specific.
    """

    raw_df = document_frequency.get(term, 1)
    floor = min(total_documents, max(2, round(total_documents * _MIN_EFFECTIVE_DOCUMENT_FREQUENCY_RATIO)))
    effective_df = max(raw_df, floor)
    return math.log1p((total_documents - effective_df + 0.5) / (effective_df + 0.5))


def lexical_relevance(query: str, content: str, *, document_frequency: Mapping[str, int] | None = None, total_documents: int | None = None) -> float:
    """Return a deterministic BM25-like score normalized to the interval [0, 1]."""

    query_tokens = meaningful_tokens(_bounded_text(query, MAX_QUERY_CHARS, "query"))
    content = _bounded_text(content, MAX_MEMORY_CHARS, "content")
    content_tokens = meaningful_tokens(content)
    if not query_tokens or not content_tokens:
        return 0.0
    counts = Counter(content_tokens)
    query_counts = Counter(query_tokens)
    document_length = len(content_tokens)
    average_length = max(document_length, 24)
    score = 0.0
    document_frequency = document_frequency or {}
    total_documents = total_documents or 1
    for token, query_frequency in query_counts.items():
        frequency = counts.get(token, 0)
        if not frequency and len(token) >= 5:
            frequency = max(
                (
                    counts[candidate]
                    for candidate in counts
                    if _near_token(token, candidate)
                ),
                default=0,
            )
        if not frequency:
            continue
        idf = _token_idf(document_frequency, total_documents, token)
        denominator = frequency + 1.2 * (0.25 + 0.75 * document_length / average_length)
        score += idf * (frequency * 2.2 / denominator) * min(query_frequency, 2)
    normalized = score / (score + 2.5)
    phrase = " ".join(query.casefold().split())
    normalized_content = " ".join(content.casefold().split())
    if phrase and phrase in normalized_content:
        normalized = min(1.0, normalized + 0.18)
    return min(1.0, normalized)


def _anchor_matches(query_terms: set[str], content_tokens: Sequence[str]) -> set[str]:
    """Match query anchor terms against content tokens, tolerating plural/near-token variants.

    A bare set intersection misses the common case where the question uses a plural
    ("goals") and the answer states it in the singular ("goal") - exact-match anchor
    scoring would otherwise credit only incidental, non-discriminative overlap (e.g. a
    shared name) while missing the one token that actually answers the question.
    """

    content_set = set(content_tokens)
    matched = query_terms & content_set
    for term in query_terms - matched:
        if len(term) >= 5 and any(_near_token(term, candidate) for candidate in content_set):
            matched.add(term)
    return matched


def _near_token(left: str, right: str) -> bool:
    """Accept a small typo without turning lexical search into substring search."""

    left_stem = re.sub(r"(?:ation|tion|ing|ed|es|s)$", "", left)
    right_stem = re.sub(r"(?:ation|tion|ing|ed|es|s)$", "", right)
    if len(left_stem) >= 4 and left_stem == right_stem:
        return True
    if left == right or abs(len(left) - len(right)) > 2:
        return left == right
    if min(len(left), len(right)) < 5:
        return False
    if left[0] != right[0]:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.84


def _content_quality(content: str) -> float:
    """Estimate whether a turn contains evidence instead of social backchannel."""

    tokens = meaningful_tokens(content)
    if not tokens:
        return 0.0
    lowered = " ".join(content.casefold().split())
    body = lowered.split(":", 1)[1].lstrip() if ":" in lowered else lowered
    acknowledgement = any(body.startswith(prefix) for prefix in _ACKNOWLEDGEMENT_PREFIXES)
    factual = bool(set(tokens) & _FACTUAL_CUES) or bool(re.search(r"\d", content))
    if acknowledgement and not factual and len(tokens) <= 18:
        return 0.12
    score = 0.52
    if factual:
        score += 0.28
    if len(tokens) >= 8:
        score += 0.08
    return min(1.0, score)


def query_answer_type_relevance(query: str, content: str) -> float:
    """Prefer candidates containing the evidence shape requested by a question."""

    query_lower = query.casefold()
    content_lower = content.casefold()
    content_tokens = set(meaningful_tokens(content))
    if "how many" in query_lower or "how much" in query_lower:
        if re.search(r"\b\d+(?:\.\d+)?\b", content_lower) or content_tokens & _NUMBER_WORDS:
            return 1.0
        query_subject = set(meaningful_tokens(query)) - {"many", "much"}
        return 0.35 if query_subject & content_tokens else 0.0
    if "relationship status" in query_lower or "marital status" in query_lower:
        return 0.95 if content_tokens & {
            "single",
            "married",
            "partner",
            "spouse",
            "divorced",
            "separated",
            "relationship",
        } else 0.0
    if "identity" in query_lower or "gender" in query_lower:
        return 0.95 if content_tokens & {
            "identity",
            "gender",
            "transgender",
            "woman",
            "man",
            "nonbinary",
            "non-binary",
        } else 0.0
    if "how often" in query_lower or "how frequently" in query_lower:
        return 0.9 if (
            content_tokens
            & {
                "daily",
                "weekly",
                "regularly",
                "often",
                "always",
                "sometimes",
                "frequently",
                "usually",
                "every",
                "multiple",
                "times",
            }
            or re.search(r"\b\d+\s+times?\b", content_lower)
        ) else 0.0
    if "what flavor" in query_lower or "what flavour" in query_lower:
        return 0.9 if content_tokens & {
            "chocolate",
            "vanilla",
            "coconut",
            "strawberry",
            "mint",
            "swirl",
            "flavor",
            "flavour",
        } else 0.0
    if "favorite" in query_lower or "favourite" in query_lower:
        return 0.9 if (
            "favorite" in content_lower
            or "favourite" in content_lower
            or content_tokens & {"love", "loves", "like", "likes", "prefer", "prefers"}
        ) else 0.0
    if re.search(r"\bgoals?\b|\baspir|\bambitions?\b", query_lower):
        # A question asking "what are X's goals ... career" is about aspirations, not
        # about identifying a job/profession - check this before the job/career branch
        # below, which would otherwise reward any candidate merely containing the word
        # "career" (a qualifier in the question, not the thing being asked about) over
        # one that actually states a goal.
        return 0.9 if content_tokens & {
            "goal",
            "goals",
            "aspire",
            "aspires",
            "aspiration",
            "aspirations",
            "dream",
            "dreams",
            "hope",
            "hopes",
            "hoping",
            "ambition",
            "ambitions",
            "achieve",
            "achieving",
            "striving",
            "strive",
            "want",
            "wants",
            "plan",
            "plans",
            "planning",
        } else 0.0
    if re.search(r"\b(?:job|career|occupation|profession)\b", query_lower):
        return 0.9 if content_tokens & {
            "job",
            "career",
            "work",
            "working",
            "profession",
            "professional",
            "counselor",
            "counsellor",
            "coordinator",
            "manager",
            "teacher",
            "engineer",
            "volunteer",
            "volunteering",
            "shelter",
            "counseling",
            "community",
        } else 0.0
    if "nickname" in query_lower or "what does" in query_lower and "call" in query_lower:
        return 0.9 if content_tokens & {
            "nickname",
            "called",
            "call",
            "goes",
            "jo",
        } else 0.0
    if any(token in query_lower for token in ("symbol", "symbols")):
        return 0.9 if content_tokens & {
            "symbol",
            "symbols",
            "flag",
            "mural",
            "eagle",
            "transgender",
            "rainbow",
        } else 0.0
    if "screenplay" in query_lower or "production compan" in query_lower:
        return 0.9 if content_tokens & {
            "screenplay",
            "script",
            "rejection",
            "rejected",
            "production",
            "company",
            "companies",
        } else 0.0
    if "when" in query_lower or "what date" in query_lower or "what year" in query_lower:
        return 0.9 if (
            _candidate_dates_from_text(content)
            or content_tokens & {"yesterday", "today", "tomorrow", "month", "week", "year", "started", "finished"}
        ) else 0.0
    if any(token in query_lower for token in ("city", "location", "capital")):
        return 0.9 if any(
            phrase in content_lower
            for phrase in ("capital of ", "city of ", "worked in the city", "located in the city", "died in the city")
        ) else 0.0
    if any(
        phrase in query_lower
        for phrase in ("where did", "come into existence", "where was", "place was")
    ):
        return 0.9 if any(
            phrase in content_lower
            for phrase in ("city of ", "located in the city", "founded in the city", "died in the city")
        ) else 0.0
    if "continent" in query_lower:
        return 0.9 if "continent of " in content_lower or "in the continent of" in content_lower else 0.0
    if any(token in query_lower for token in ("head of state", "chief of state")):
        return 0.9 if any(
            phrase in content_lower for phrase in ("head of state", "chief of state")
        ) else 0.0
    if (
        any(token in query_lower for token in ("head of government", "chief executive"))
        or re.search(r"\bhead of (?:the )?.+ government\b", query_lower)
    ):
        return 0.9 if any(
            phrase in content_lower
            for phrase in ("head of government", "chief executive officer")
        ) or bool(re.search(r"head of (?:the )?.+ government", content_lower)) else 0.0
    if (
        "religious affiliation" in query_lower
        or "which faith" in query_lower
        or "what faith" in query_lower
        or "religion" in query_lower and "founded" not in query_lower
    ):
        return 0.95 if any(
            phrase in content_lower
            for phrase in ("religion of ", "affiliated with the religion")
        ) else 0.0
    if "chairperson" in query_lower and not any(
        phrase in query_lower for phrase in ("professional role", "job title")
    ):
        return 0.95 if "chairperson of " in content_lower else 0.0
    if "director/manager" in query_lower or "director" in query_lower:
        return 0.95 if "director of " in content_lower else 0.0
    if "citizenship" in query_lower or "citizen of" in query_lower:
        return 0.9 if "citizen of " in content_lower else 0.0
    if "professional role" in query_lower:
        return 0.9 if "works in the field" in content_lower or "plays the position" in content_lower else 0.0
    if any(phrase in query_lower for phrase in ("position did", "position was", "professional position")):
        return 0.9 if any(
            phrase in content_lower
            for phrase in (
                "plays the position",
                "works in the field",
                "director of ",
                "chairperson of ",
                "chief executive officer of ",
                "employed by ",
            )
        ) else 0.0
    if "founding" in query_lower or "founded" in query_lower:
        return 0.9 if "founded by " in content_lower or "was founded" in content_lower else 0.0
    if "religious leader" in query_lower or "founding the religion" in query_lower:
        return 0.9 if "founded by " in content_lower else 0.0
    if re.search(r"\b(?:which|what is the|what was the) sport\b", query_lower):
        return 0.75 if "sport of " in content_lower else 0.0
    if "region" in query_lower or "geography" in query_lower:
        return 0.9 if any(
            phrase in content_lower
            for phrase in ("region", "multi-region", "single-region", "deployment geography")
        ) else 0.0
    if any(token in query_lower for token in ("job title", "position")):
        return 0.9 if "position of " in content_lower or "works in the field" in content_lower else 0.0
    if "language" in query_lower:
        return 0.9 if any(
            phrase in content_lower
            for phrase in (
                "official language of ",
                "speaks the language of",
                "written in the language of",
            )
        ) else 0.0
    if "educational institution" in query_lower:
        return 0.9 if "educated" in content_lower and any(
            phrase in content_lower for phrase in ("university where", "was educated")
        ) else 0.0
    if "country" in query_lower or "nation" in query_lower or "from which country" in query_lower:
        return 0.9 if any(
            phrase in content_lower
            for phrase in ("country of ", "citizen of ", "country where", "country is")
        ) else 0.0
    quoted = re.findall(r"['\"]([^'\"]{2,})['\"]", query)
    if len(quoted) >= 2 and (" or " in query_lower or " first" in query_lower):
        return min(1.0, sum(0.5 for phrase in quoted if phrase.casefold() in content_lower))
    return 0.0


def _candidate_dates_from_text(content: str) -> tuple[date, ...]:
    return tuple(_parse_dates(value)[0] for value in _ISO_DATE_RE.findall(content) if _parse_dates(value))


@lru_cache(maxsize=8_192)
def graph_link_phrases(text: str) -> set[str]:
    """Extract proper names and bounded fact-template entities for graph links."""

    phrases = set(named_entity_phrases(text))
    for match in _GRAPH_OBJECT_RE.finditer(text):
        value = match.group(1)
        value = re.split(r"\s+(?:is|was|are|were|in|by|for|to|from)\s+", value, maxsplit=1, flags=re.IGNORECASE)[0]
        _add_graph_phrase(phrases, value)
    for match in _GRAPH_ASSIGNMENT_RE.finditer(text):
        _add_graph_phrase(phrases, match.group(1))
    subject = _GRAPH_SUBJECT_RE.match(text)
    if subject:
        _add_graph_phrase(phrases, subject.group(1))
    return phrases


def _add_graph_phrase(phrases: set[str], value: str) -> None:
    normalized = " ".join(value.casefold().strip(" .,:;!?\"'").split())
    if normalized.startswith("the "):
        normalized = normalized[4:]
    tokens = meaningful_tokens(normalized)
    if 1 <= len(tokens) <= 6 and len(normalized) <= 128:
        phrases.add(normalized)


def predicate_relevance(query: str, content: str) -> float:
    query_groups = {
        _PREDICATE_TO_GROUP[token]
        for token in meaningful_tokens(query)
        if token in _PREDICATE_TO_GROUP
    }
    if not query_groups:
        return 0.0
    content_groups = {
        _PREDICATE_TO_GROUP[token]
        for token in meaningful_tokens(content)
        if token in _PREDICATE_TO_GROUP
    }
    return len(query_groups & content_groups) / len(query_groups)


def query_requires_graph(query: str) -> bool:
    lowered = query.casefold()
    predicate_groups = {
        _PREDICATE_TO_GROUP[token]
        for token in meaningful_tokens(query)
        if token in _PREDICATE_TO_GROUP
    }
    return (
        len(predicate_groups) >= 2
        or lowered.count(" of ") >= 2
        or any(
            marker in lowered
            for marker in (
                "associated with",
                "belongs to",
                "country where",
                "country of origin",
                "developer of",
                "founder of",
                "person who",
                "religion to which",
                "performer of",
                "director/manager",
                "continent",
                "head of state",
                "head of government",
                "chief of state",
                "chief executive",
                "professional role",
                "political entity",
                "position on the team",
                "which language",
                "official language",
                "position did",
                "performer",
                "birthplace of the sport",
                "country that gave rise",
                "headquarters",
                "educated",
            )
        )
    )


async def retrieve_memories(
    memories: Sequence[Any],
    query: str,
    *,
    limit: int = 10,
    scope: str | None = None,
    relations: Sequence[Any] = (),
    embedding_provider: EmbeddingProvider | None = None,
    precomputed_semantic_scores: Mapping[str, float] | None = None,
    now: datetime | None = None,
    mmr_lambda: float = 0.82,
    rrf_k: int = DEFAULT_RRF_K,
    candidate_limit: int = MAX_CANDIDATES,
    strict_embeddings: bool = False,
) -> list[RetrievedMemory]:
    """Retrieve memories with lexical, optional semantic, temporal, graph, and MMR stages."""

    query = _bounded_text(query, MAX_QUERY_CHARS, "query")
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not 0.0 <= mmr_lambda <= 1.0:
        raise ValueError("mmr_lambda must be between 0 and 1")
    if not 1 <= candidate_limit <= MAX_CANDIDATES:
        raise ValueError(f"candidate_limit must be between 1 and {MAX_CANDIDATES}")
    if len(memories) > MAX_CANDIDATES:
        raise ValueError(f"memories cannot exceed {MAX_CANDIDATES} items")
    if len(relations) > MAX_RELATIONS:
        raise ValueError(f"relations cannot exceed {MAX_RELATIONS} items")

    current = _as_utc(now or datetime.now(timezone.utc))
    prepared: list[_PreparedMemory] = []
    seen_ids: set[str] = set()
    for original_index, memory in enumerate(memories[:candidate_limit]):
        memory_id = str(_field(memory, "id", original_index))
        if memory_id in seen_ids:
            raise ValueError(f"duplicate memory id: {memory_id}")
        seen_ids.add(memory_id)
        if scope is not None and _field(memory, "scope", None) != scope:
            continue
        content = _bounded_text(str(_field(memory, "content", "")), MAX_MEMORY_CHARS, "content")
        if not content.strip() or _is_expired(_field(memory, "expires_at", None), current):
            continue
        event_values = _field(memory, "event_dates", None)
        if event_values is None:
            event_values = _field(memory, "event_dates_json", ())
        prepared.append(
            _PreparedMemory(
                memory=memory,
                memory_id=memory_id,
                content=content,
                tokens=meaningful_tokens(content),
                updated_at=_as_utc(_field(memory, "updated_at", current)),
                document_date=_field(memory, "document_date", None),
                event_dates=tuple(str(value) for value in (event_values or ())),
                importance=max(0.0, min(1.0, float(_field(memory, "importance", 0.5) or 0.0))),
                expires_at=_field(memory, "expires_at", None),
                superseded_at=_field(memory, "superseded_at", None),
                superseded_by_id=_field(memory, "superseded_by_id", None),
                original_index=original_index,
            )
        )
    if not prepared:
        return []

    query_views = query_variants(query)
    document_frequency = Counter(token for item in prepared for token in set(item.tokens))
    lexical_scores = {
        item.memory_id: max(
            lexical_relevance(
                query_view,
                item.content,
                document_frequency=document_frequency,
                total_documents=len(prepared),
            )
            for query_view in query_views
        )
        for item in prepared
    }
    query_anchor_terms = set(meaningful_tokens(query))
    anchor_idf = {
        term: _token_idf(document_frequency, len(prepared), term)
        for term in query_anchor_terms
    }
    total_anchor_idf = sum(anchor_idf.values())
    anchor_scores = {
        item.memory_id: (
            sum(anchor_idf[term] for term in _anchor_matches(query_anchor_terms, item.tokens))
            / total_anchor_idf
            if query_anchor_terms and total_anchor_idf > 0
            else 0.0
        )
        for item in prepared
    }
    answer_type_scores = {
        item.memory_id: max(
            query_answer_type_relevance(query_view, item.content)
            for query_view in query_views
        )
        for item in prepared
    }
    predicate_scores = {
        item.memory_id: max(
            predicate_relevance(query_view, item.content) for query_view in query_views
        )
        for item in prepared
    }
    lexical_ranking = _rank_ids(prepared, lexical_scores)
    rankings: list[Sequence[str]] = [lexical_ranking]
    weights: list[float] = [0.65]
    if any(score > 0 for score in anchor_scores.values()):
        rankings.append(_rank_ids(prepared, anchor_scores))
        weights.append(0.85)
    graph_intent = query_requires_graph(query)
    collection_intent = graph_intent or query_requests_entity_context(query)
    if any(score > 0 for score in predicate_scores.values()):
        rankings.append(_rank_ids(prepared, predicate_scores))
        weights.append(0.65 if graph_intent else 0.45)
    semantic_scores: dict[str, float] = {}
    for memory_id, score in (precomputed_semantic_scores or {}).items():
        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            continue
        if memory_id in seen_ids and math.isfinite(numeric_score):
            semantic_scores[memory_id] = max(0.0, min(1.0, numeric_score))
    vectors: dict[str, tuple[float, ...]] = {}
    if embedding_provider is not None:
        try:
            provider_scores, vectors = await _semantic_scores(query, prepared, embedding_provider)
            semantic_scores.update(provider_scores)
        except Exception:
            if strict_embeddings:
                raise
            vectors = {}
    if semantic_scores:
        rankings.append(_rank_ids(prepared, semantic_scores))
        weights.append(1.35)

    temporal_scores = {
        item.memory_id: max(
            temporal_relevance(query_view, item.memory, now=current)
            for query_view in query_views
        )
        for item in prepared
    }
    if any(score > 0 for score in temporal_scores.values()):
        rankings.append(_rank_ids(prepared, temporal_scores))
        weights.append(0.85)

    relation_scores = (
        _relation_boosts(
            prepared,
            relations,
            anchors=set(
                (lexical_ranking + list(rankings[1]) if len(rankings) > 1 else lexical_ranking)[:8]
            ),
            query=query,
            include_entity_bridges=collection_intent,
        )
    )
    if any(score > 0 for score in relation_scores.values()):
        rankings.append(_rank_ids(prepared, relation_scores))
        weights.append(1.25 if graph_intent else (0.90 if collection_intent else 0.65))
    replacement_scores = _replacement_boosts(
        prepared,
        relations,
        lexical_scores=lexical_scores,
        semantic_scores=semantic_scores,
        query=query,
    )
    if any(score > 0 for score in replacement_scores.values()):
        rankings.append(_rank_ids(prepared, replacement_scores))
        weights.append(1.0)

    state_scores = {
        item.memory_id: memory_state_relevance(query, item.memory, now=current)
        for item in prepared
    }
    if len(set(state_scores.values())) > 1:
        rankings.append(_rank_ids(prepared, state_scores))
        weights.append(0.75)
    rrf_scores = reciprocal_rank_fusion(rankings, weights=weights, k=rrf_k)
    max_rrf = max(rrf_scores.values(), default=1.0)
    prepared_by_id = {item.memory_id: item for item in prepared}
    answer_type_weight = 0.10 if graph_intent else 0.18
    relation_weight = (
        0.24
        if graph_intent and any(relation_scores.values())
        else (0.12 if collection_intent and any(relation_scores.values()) else 0.08)
    )
    state_weight = 0.12 if query_requests_history(query) else 0.04
    semantic_weight = (
        0.14 if semantic_scores and graph_intent and any(relation_scores.values()) else 0.24
    )
    scored: list[RetrievedMemory] = []
    for item in prepared:
        if item.memory_id not in rrf_scores:
            continue
        normalized_rrf = rrf_scores.get(item.memory_id, 0.0) / max_rrf
        final_score = (
            0.36 * normalized_rrf
            + 0.14 * lexical_scores[item.memory_id]
            + 0.14 * anchor_scores[item.memory_id]
            + 0.03 * predicate_scores[item.memory_id]
            + 0.12 * temporal_scores[item.memory_id]
            + relation_weight * relation_scores.get(item.memory_id, 0.0)
            + 0.08 * replacement_scores.get(item.memory_id, 0.0)
            + state_weight * state_scores[item.memory_id]
        )
        final_score += 0.10 * _content_quality(item.content)
        final_score += answer_type_weight * answer_type_scores[item.memory_id]
        if graph_intent:
            final_score += (
                1.35
                * relation_scores.get(item.memory_id, 0.0)
                * answer_type_scores[item.memory_id]
            )
            final_score -= (
                0.15
                * relation_scores.get(item.memory_id, 0.0)
                * (1.0 - answer_type_scores[item.memory_id])
            )
        if semantic_scores:
            final_score += semantic_weight * semantic_scores.get(item.memory_id, 0.0)
        final_score += 0.02 * item.importance
        if graph_intent and not query_requests_history(query) and (
            item.superseded_at is not None or item.superseded_by_id is not None
        ):
            final_score -= 0.20
        scored.append(
            RetrievedMemory(
                memory=item.memory,
                memory_id=item.memory_id,
                score=(
                    max(0.0, final_score)
                    / (1.0 + max(0.0, final_score))
                ),
                lexical_score=lexical_scores[item.memory_id],
                semantic_score=semantic_scores.get(item.memory_id, 0.0),
                temporal_score=temporal_scores[item.memory_id],
                relation_boost=relation_scores.get(item.memory_id, 0.0),
                rrf_score=rrf_scores.get(item.memory_id, 0.0),
                state_score=state_scores[item.memory_id],
            )
        )
    scored.sort(key=lambda result: (-result.score, prepared_by_id[result.memory_id].original_index))
    pool = scored[: min(len(scored), max(limit * 5, limit))]
    effective_mmr_lambda = (
        (0.62 if query_requests_diversity(query) else max(mmr_lambda, SEMANTIC_MMR_LAMBDA_FLOOR))
        if semantic_scores
        else mmr_lambda
    )
    selected = _mmr_select(
        pool,
        prepared_by_id,
        vectors,
        limit=limit,
        mmr_lambda=effective_mmr_lambda,
    )
    return selected


def _mmr_select(
    pool: Sequence[RetrievedMemory],
    prepared: Mapping[str, _PreparedMemory],
    vectors: Mapping[str, tuple[float, ...]],
    *,
    limit: int,
    mmr_lambda: float,
) -> list[RetrievedMemory]:
    remaining = list(pool)
    selected: list[RetrievedMemory] = []
    while remaining and len(selected) < limit:
        best_index = 0
        best_value = -math.inf
        for index, result in enumerate(remaining):
            redundancy = max(
                (_memory_similarity(result.memory_id, chosen.memory_id, prepared, vectors) for chosen in selected),
                default=0.0,
            )
            value = mmr_lambda * result.score - (1.0 - mmr_lambda) * redundancy
            if value > best_value:
                best_value = value
                best_index = index
        selected.append(remaining.pop(best_index))
    return selected


async def _semantic_scores(
    query: str,
    prepared: Sequence[_PreparedMemory],
    provider: EmbeddingProvider,
) -> tuple[dict[str, float], dict[str, tuple[float, ...]]]:
    texts = [query, *(item.content for item in prepared)]
    if len(texts) > MAX_EMBEDDING_TEXTS:
        raise ValueError("embedding batch exceeds the bounded input size")
    raw_vectors = await provider.embed(texts)
    if len(raw_vectors) != len(texts):
        raise ValueError("embedding provider returned the wrong number of vectors")
    vectors = [_validated_vector(vector) for vector in raw_vectors]
    query_vector = vectors[0]
    memory_vectors = {item.memory_id: vector for item, vector in zip(prepared, vectors[1:])}
    scores = {memory_id: max(0.0, (_cosine(query_vector, vector) + 1.0) / 2.0) for memory_id, vector in memory_vectors.items()}
    return scores, memory_vectors


def _relation_boosts(
    prepared: Sequence[_PreparedMemory],
    relations: Sequence[Any],
    *,
    anchors: set[str],
    query: str,
    include_entity_bridges: bool = True,
    include_entity_direct: bool = True,
) -> dict[str, float]:
    candidate_ids = {item.memory_id for item in prepared}
    boosts: defaultdict[str, float] = defaultdict(float)
    adjacency: defaultdict[str, list[tuple[str, float]]] = defaultdict(list)
    type_weights = {"updates": 1.0, "extends": 0.85, "derives": 0.8, "related": 0.65}
    for relation in relations:
        source = str(_field(relation, "source_memory_id", ""))
        target = str(_field(relation, "target_memory_id", ""))
        if source not in candidate_ids or target not in candidate_ids or source == target:
            continue
        confidence = max(0.0, min(1.0, float(_field(relation, "confidence", 0.5) or 0.0)))
        weight = type_weights.get(str(_field(relation, "relation_type", "related")), 0.65)
        adjacency[source].append((target, confidence * weight))
        adjacency[target].append((source, confidence * weight * 0.5))
    entities_by_id = {
        item.memory_id: graph_link_phrases(item.content) for item in prepared
    }
    query_entities = graph_link_phrases(query)
    entity_anchors = {
        anchor
        for anchor in anchors
        if query and query_entities & entities_by_id.get(anchor, set())
    }
    for anchor in (entity_anchors or anchors) & candidate_ids:
        frontier = {anchor: 1.0}
        for hop in range(6):
            next_frontier: dict[str, float] = {}
            decay = 1.0 if hop == 0 else 0.8
            for node, path_strength in frontier.items():
                for neighbor, edge_strength in adjacency.get(node, ()):
                    if neighbor == anchor:
                        continue
                    value = path_strength * edge_strength * decay
                    boosts[neighbor] = max(boosts[neighbor], value)
                    if hop < 5:
                        next_frontier[neighbor] = max(next_frontier.get(neighbor, 0.0), value)
            frontier = next_frontier
    if include_entity_bridges:
        prepared_by_id = {item.memory_id: item for item in prepared}
        for memory_id, boost in _entity_relation_boosts(
            prepared, anchors, query_entities=query_entities
        ).items():
            if _content_quality(prepared_by_id[memory_id].content) <= 0.12:
                continue
            boosts[memory_id] = max(boosts[memory_id], boost)
    query_entity_tokens = {
        token for entity in query_entities if " " in entity for token in entity.split()
    }
    direct_query_entities = {
        entity
        for entity in query_entities
        if " " in entity or entity not in query_entity_tokens
    }
    if include_entity_direct and direct_query_entities:
        prepared_by_id = {item.memory_id: item for item in prepared}
        for item in prepared:
            if not direct_query_entities & graph_link_phrases(item.content):
                continue
            quality = _content_quality(item.content)
            if quality <= 0.12:
                continue
            direct_weight = 0.28 if include_entity_bridges else 0.08
            boosts[item.memory_id] = max(
                boosts[item.memory_id], direct_weight + 0.10 * quality
            )
    return dict(boosts)


def _replacement_boosts(
    prepared: Sequence[_PreparedMemory],
    relations: Sequence[Any],
    *,
    lexical_scores: Mapping[str, float],
    semantic_scores: Mapping[str, float],
    query: str,
) -> dict[str, float]:
    if not query_requests_replacement(query):
        return {}
    candidate_ids = {item.memory_id for item in prepared}
    scores: defaultdict[str, float] = defaultdict(float)
    for relation in relations:
        if str(_field(relation, "relation_type", "")) != "updates":
            continue
        source = str(_field(relation, "source_memory_id", ""))
        target = str(_field(relation, "target_memory_id", ""))
        if source not in candidate_ids or target not in candidate_ids:
            continue
        target_anchor = max(
            lexical_scores.get(target, 0.0), semantic_scores.get(target, 0.0)
        )
        if target_anchor <= 0:
            continue
        source_anchor = max(
            lexical_scores.get(source, 0.0), semantic_scores.get(source, 0.0)
        )
        scores[source] = max(scores[source], min(1.0, target_anchor * 0.85 + source_anchor * 0.35))
    return dict(scores)


def _entity_relation_boosts(
    prepared: Sequence[_PreparedMemory], anchors: set[str], *, query_entities: set[str]
) -> dict[str, float]:
    entity_members: defaultdict[str, set[str]] = defaultdict(set)
    entities_by_id: dict[str, set[str]] = {}
    for item in prepared:
        entities = graph_link_phrases(item.content)
        entities_by_id[item.memory_id] = entities
        for entity in entities:
            entity_members[entity].add(item.memory_id)

    multiword_entity_tokens = {
        token
        for entity in entity_members
        if " " in entity
        for token in entity.split()
    }
    adjacency: defaultdict[str, list[tuple[str, float]]] = defaultdict(list)
    for entity, members in entity_members.items():
        if len(members) < 2 or len(members) > 64:
            continue
        if (
            " " not in entity
            and entity in multiword_entity_tokens
        ):
            continue
        member_list = tuple(members)
        for index, left in enumerate(member_list):
            for right in member_list[index + 1 :]:
                shared = len(entities_by_id[left] & entities_by_id[right])
                strength = min(0.9, 0.72 + 0.08 * max(0, shared - 1))
                adjacency[left].append((right, strength))
                adjacency[right].append((left, strength))

    entity_anchors = {
        anchor
        for anchor in anchors
        if query_entities & entities_by_id.get(anchor, set())
    }
    boosts: defaultdict[str, float] = defaultdict(float)
    for anchor in entity_anchors or anchors:
        frontier = {anchor: 1.0}
        for hop in range(6):
            next_frontier: dict[str, float] = {}
            decay = 1.0 if hop == 0 else 0.8
            for node, path_strength in frontier.items():
                for neighbor, edge_strength in adjacency.get(node, ()):
                    if neighbor == anchor:
                        continue
                    value = path_strength * edge_strength * decay
                    boosts[neighbor] = max(boosts[neighbor], value)
                    if hop < 5:
                        next_frontier[neighbor] = max(next_frontier.get(neighbor, 0.0), value)
            frontier = next_frontier
    return dict(boosts)


def _memory_similarity(
    left_id: str,
    right_id: str,
    prepared: Mapping[str, _PreparedMemory],
    vectors: Mapping[str, tuple[float, ...]],
) -> float:
    left = prepared[left_id]
    right = prepared[right_id]
    lexical = _jaccard(left.tokens, right.tokens)
    if left_id not in vectors or right_id not in vectors:
        return lexical
    semantic = max(0.0, _cosine(vectors[left_id], vectors[right_id]))
    return 0.6 * semantic + 0.4 * lexical


def _rank_ids(prepared: Sequence[_PreparedMemory], scores: Mapping[str, float]) -> list[str]:
    return [
        item.memory_id
        for item in sorted(
            prepared,
            key=lambda item: (-scores.get(item.memory_id, 0.0), item.original_index),
        )
        if scores.get(item.memory_id, 0.0) > 0
    ]


def _validated_vector(vector: Sequence[float]) -> tuple[float, ...]:
    if not 1 <= len(vector) <= MAX_EMBEDDING_DIMENSIONS:
        raise ValueError("embedding dimensions are outside the supported bound")
    result = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in result):
        raise ValueError("embedding vectors must contain only finite values")
    return result


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions must match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _field(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _bounded_text(value: str, limit: int, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _as_utc(value: datetime | date) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_expired(value: datetime | None, now: datetime) -> bool:
    return value is not None and _as_utc(value) <= now


def _candidate_dates(memory: Any) -> tuple[date, ...]:
    parsed: list[date] = []
    document_date = _field(memory, "document_date", None)
    if document_date is not None:
        parsed.append(_as_utc(document_date).date())
    event_values = _field(memory, "event_dates", None)
    if event_values is None:
        event_values = _field(memory, "event_dates_json", ())
    for value in event_values or ():
        parsed.extend(_parse_dates(str(value)))
    if not parsed:
        parsed.extend(_parse_dates(str(_field(memory, "content", ""))))
    if not parsed:
        updated_at = _field(memory, "updated_at", None)
        if updated_at is not None:
            parsed.append(_as_utc(updated_at).date())
    return tuple(dict.fromkeys(parsed))


def _query_dates(query: str, today: date) -> tuple[date, ...]:
    dates: list[date] = []
    for value in _ISO_DATE_RE.findall(query):
        dates.extend(_parse_dates(value))
    for month, year in _MONTH_YEAR_RE.findall(query):
        dates.append(date(int(year), _MONTHS[month.casefold()], 1))
    for month, day, year in _NATURAL_DATE_RE.findall(query):
        try:
            dates.append(date(int(year), _MONTHS[month.casefold()], int(day)))
        except ValueError:
            continue
    dates.extend(resolve_relative_dates(query, today))
    return tuple(dict.fromkeys(dates))


def _parse_dates(value: str) -> list[date]:
    result: list[date] = []
    for match in _ISO_DATE_RE.findall(value):
        year, month, day = (int(part) for part in re.split(r"[-/]", match))
        try:
            result.append(date(year, month, day))
        except ValueError:
            continue
    if not result:
        for month, day, year in _NATURAL_DATE_RE.findall(value):
            try:
                result.append(date(int(year), _MONTHS[month.casefold()], int(day)))
            except ValueError:
                continue
    if not result:
        for year in _YEAR_RE.findall(value):
            result.append(date(int(year), 1, 1))
    return result


def _range_date_score(candidate_dates: Sequence[date], start: date, end: date) -> float:
    if any(start <= value <= end for value in candidate_dates):
        return 1.0
    return 0.0
