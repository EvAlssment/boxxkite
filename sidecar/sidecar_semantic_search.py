"""Bounded, sandbox-local lexical search for natural-language code queries.

This is deliberately a lexical fallback rather than an embeddings model.  It
keeps the feature deterministic and dependency-free, and never creates an
outbound network path or downloads model data.  The index is process-local,
invalidated on sidecar file writes, refreshed from file signatures on lookup,
and cleared when a warm pod is configured for a new session.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")
router = APIRouter()


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


MAX_INDEXED_FILES = _bounded_env_int("SIDECAR_SEMANTIC_SEARCH_MAX_FILES", 2000, 1, 20000)
MAX_INDEXED_BYTES = _bounded_env_int(
    "SIDECAR_SEMANTIC_SEARCH_MAX_BYTES", 32 * 1024 * 1024, 1024, 256 * 1024 * 1024
)
MAX_FILE_BYTES = _bounded_env_int(
    "SIDECAR_SEMANTIC_SEARCH_MAX_FILE_BYTES", 512 * 1024, 1024, 16 * 1024 * 1024
)
MAX_SCANNED_DIRECTORIES = _bounded_env_int(
    "SIDECAR_SEMANTIC_SEARCH_MAX_DIRECTORIES", 10000, 1, 100000
)
SEARCH_TIMEOUT_SECONDS = _bounded_env_int("SIDECAR_SEMANTIC_SEARCH_TIMEOUT_SECONDS", 10, 1, 60)
MAX_LINE_CHARS = 1000

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "use",
        "where", "which", "with",
    }
)


def _normalize_token(token: str) -> str:
    token = token.lower()
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> tuple[str, ...]:
    parts: list[str] = []
    for chunk in _CAMEL_BOUNDARY_RE.sub(" ", text).replace("_", " ").split():
        parts.extend(_TOKEN_RE.findall(chunk))
    return tuple(
        token
        for raw in parts
        if (token := _normalize_token(raw)) not in _STOPWORDS and len(token) >= 2
    )


@dataclass(frozen=True)
class _IndexedFile:
    signature: tuple[int, int]
    size_bytes: int
    lines: tuple[str, ...]
    line_tokens: tuple[tuple[str, ...], ...]
    filename_tokens: tuple[str, ...]
    tokens: frozenset[str]


class _SemanticSearchIndex:
    def __init__(self) -> None:
        self._documents: dict[str, _IndexedFile] = {}
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            self._documents.clear()

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._documents.pop(os.path.realpath(path), None)

    @staticmethod
    def _candidate_files(base_path: str) -> tuple[list[str], bool]:
        if os.path.isfile(base_path):
            return [base_path], False
        if not os.path.isdir(base_path):
            return [], False

        roots = main._typed_allowed_roots()
        candidates: list[str] = []
        scanned_directories = 0
        truncated = False
        for root, dirs, filenames in os.walk(base_path, topdown=True, followlinks=False):
            scanned_directories += 1
            if scanned_directories > MAX_SCANNED_DIRECTORIES:
                truncated = True
                break
            dirs[:] = [
                name
                for name in dirs
                if main._is_path_contained(os.path.join(root, name), roots)
            ]
            for filename in filenames:
                path = os.path.join(root, filename)
                if main._is_path_contained(path, roots):
                    candidates.append(os.path.realpath(path))
                    if len(candidates) > MAX_INDEXED_FILES:
                        truncated = True
                        break
            if truncated:
                break
        return sorted(set(candidates))[: MAX_INDEXED_FILES + 1], truncated

    @staticmethod
    def _read_file(path: str, stat_result: os.stat_result) -> Optional[_IndexedFile]:
        if stat_result.st_size > MAX_FILE_BYTES:
            return None
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
        except (OSError, PermissionError):
            return None
        if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        lines = tuple(text.splitlines())
        line_tokens = tuple(_tokens(line) for line in lines)
        filename_tokens = _tokens(os.path.basename(path))
        return _IndexedFile(
            signature=(int(stat_result.st_size), int(stat_result.st_mtime_ns)),
            size_bytes=len(raw),
            lines=lines,
            line_tokens=line_tokens,
            filename_tokens=filename_tokens,
            tokens=frozenset(token for line in line_tokens for token in line),
        )

    def _refresh(self, base_path: str) -> tuple[list[tuple[str, _IndexedFile]], bool]:
        candidates, truncated = self._candidate_files(base_path)
        documents: dict[str, _IndexedFile] = {}
        indexed_bytes = 0

        for path in candidates:
            if len(documents) >= MAX_INDEXED_FILES:
                truncated = True
                break
            if not main._is_path_contained(path, main._typed_allowed_roots()):
                continue
            try:
                stat_result = os.stat(path)
            except (OSError, PermissionError):
                continue
            if stat_result.st_size > MAX_FILE_BYTES:
                truncated = True
                continue

            signature = (int(stat_result.st_size), int(stat_result.st_mtime_ns))
            previous = self._documents.get(path)
            if previous is not None and previous.signature == signature:
                document = previous
            else:
                document = self._read_file(path, stat_result)
            if document is None:
                continue
            if indexed_bytes + document.size_bytes > MAX_INDEXED_BYTES:
                truncated = True
                continue
            documents[path] = document
            indexed_bytes += document.size_bytes

        base_real = os.path.realpath(base_path)
        for path in list(self._documents):
            if main._is_under_root(path, base_real) and path not in documents:
                self._documents.pop(path, None)
        self._documents.update(documents)
        return list(documents.items()), truncated

    def search(self, base_path: str, query: str, max_results: int) -> dict:
        with self._lock:
            query_tokens = _tokens(query)
            if not query_tokens:
                raise ValueError("query must contain at least one searchable word")

            documents, truncated = self._refresh(base_path)
            document_frequency = Counter(
                token for _, document in documents for token in document.tokens
            )
            query_set = set(query_tokens)
            matches: list[dict] = []
            for path, document in documents:
                filename_overlap = query_set.intersection(document.filename_tokens)
                filename_bonus = 0.35 * len(filename_overlap)
                for line_number, (line, line_tokens) in enumerate(
                    zip(document.lines, document.line_tokens), start=1
                ):
                    overlap = query_set.intersection(line_tokens)
                    if not overlap:
                        continue
                    score = filename_bonus
                    counts = Counter(line_tokens)
                    for token in overlap:
                        idf = math.log((len(documents) + 1) / (document_frequency[token] + 1)) + 1.0
                        score += idf * min(counts[token], 3)
                    if query_set.issubset(set(line_tokens)):
                        score += 0.5
                    matches.append(
                        {
                            "path": path,
                            "line": line_number,
                            "text": line[:MAX_LINE_CHARS],
                            "score": round(score, 6),
                        }
                    )

            matches.sort(key=lambda item: (-item["score"], item["path"], item["line"]))
            if len(matches) > max_results:
                truncated = True
                matches = matches[:max_results]
            return {
                "matches": matches,
                "truncated": truncated,
                "indexed_files": len(documents),
                "indexed_bytes": sum(document.size_bytes for _, document in documents),
            }


_INDEX = _SemanticSearchIndex()


def reset_semantic_search_index() -> None:
    _INDEX.reset()


def invalidate_semantic_search_path(path: str) -> None:
    _INDEX.invalidate(path)


@router.post("/semantic-search", response_model=main.SemanticSearchResponse)
async def semantic_search(req: main.SemanticSearchRequest):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    try:
        _, base_path = main._resolve_virtual_path(req.path or "/")
    except HTTPException:
        raise

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_INDEX.search, base_path, query, req.max_results),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except asyncio.TimeoutError:
        return main.SemanticSearchResponse(
            matches=[],
            truncated=True,
            indexed_files=0,
            indexed_bytes=0,
            notes=[
                f"Search timed out after {SEARCH_TIMEOUT_SECONDS}s; narrow the path or query."
            ],
        )

    matches = []
    for match in result["matches"]:
        if not main._is_path_contained(match["path"], main._typed_allowed_roots()):
            continue
        matches.append(
            main.SemanticSearchMatch(
                path=main._to_virtual_path(match["path"]),
                line=match["line"],
                text=match["text"],
                score=match["score"],
            )
        )
    notes = [
        "Deterministic lexical ranking is used; no embedding model or network access is involved."
    ]
    if result["truncated"]:
        notes.append("Index or result limits were reached; narrow the path or query for more results.")
    return main.SemanticSearchResponse(
        matches=matches,
        truncated=result["truncated"],
        indexed_files=result["indexed_files"],
        indexed_bytes=result["indexed_bytes"],
        notes=notes,
    )
