"""Optional pgvector persistence and OpenAI-compatible embedding provider."""

from __future__ import annotations

import json
import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Mapping, Sequence
from urllib.parse import urlparse

import httpx
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .memory_retrieval import EmbeddingProvider, RerankerProvider
from .models_orm import MemoryEmbedding, MemoryRecord


MAX_PROVIDER_BATCH_SIZE = 128
MAX_PROVIDER_INPUT_CHARS = 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validated_vector(vector: Sequence[float], dimensions: int) -> list[float]:
    values = [float(value) for value in vector]
    if len(values) != dimensions:
        raise ValueError(f"embedding provider returned {len(values)} dimensions, expected {dimensions}")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding provider returned non-finite values")
    return values


def _embedding_batches(
    memories: Sequence[tuple[str, str]],
) -> list[Sequence[tuple[str, str]]]:
    batches: list[Sequence[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_chars = 0
    for memory in memories:
        content_chars = len(memory[1])
        if content_chars > 64 * 1024:
            raise ValueError("memory content exceeds the embedding input bound")
        if current and (
            len(current) >= MAX_PROVIDER_BATCH_SIZE
            or current_chars + content_chars > MAX_PROVIDER_INPUT_CHARS
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(memory)
        current_chars += content_chars
    if current:
        batches.append(current)
    return batches


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dimensions: int,
        query_instruction: str,
        api_key: str | None = None,
        timeout: float = 10.0,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("embedding base URL must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("embedding base URL must use HTTPS outside localhost")
        if not model.strip() or not 1 <= dimensions <= 4_096 or timeout <= 0:
            raise ValueError("invalid embedding provider configuration")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self.query_instruction = query_instruction
        self.api_key = api_key
        self.timeout = timeout

    async def _request(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts or len(texts) > MAX_PROVIDER_BATCH_SIZE:
            raise ValueError("embedding batch is outside supported bounds")
        if any(not isinstance(text, str) or len(text) > 64 * 1024 for text in texts):
            raise ValueError("embedding input contains an invalid or oversized text")
        if sum(len(text) for text in texts) > MAX_PROVIDER_INPUT_CHARS:
            raise ValueError("embedding batch exceeds the total input bound")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": list(texts)},
            ) as response:
                response.raise_for_status()
                response_parts: list[bytes] = []
                response_size = 0
                async for chunk in response.aiter_bytes():
                    response_size += len(chunk)
                    if response_size > MAX_PROVIDER_RESPONSE_BYTES:
                        raise ValueError("embedding provider response exceeds the supported bound")
                    response_parts.append(chunk)
        response_body = b"".join(response_parts)
        payload = json.loads(response_body)
        data = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
        if len(data) != len(texts):
            raise ValueError("embedding provider returned the wrong number of vectors")
        return [_validated_vector(item["embedding"], self.dimensions) for item in data]

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return await self._request(texts)

    async def embed_query(self, query: str) -> Sequence[Sequence[float]]:
        instructed_query = f"Instruct: {self.query_instruction}\n Query:{query}"
        return await self._request([instructed_query])


class GeminiEmbeddingProvider:
    """Direct adapter for Google's Gemini Embedding 2 batch endpoint."""

    API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
    MAX_BATCH_SIZE = 32

    def __init__(
        self,
        *,
        model: str,
        dimensions: int,
        api_key: str | None,
        timeout: float = 30.0,
        query_task: str = "search result",
    ):
        if model != "gemini-embedding-2":
            raise ValueError("Gemini provider requires the gemini-embedding-2 model")
        if not api_key or not api_key.strip():
            raise ValueError("Gemini provider requires an API key")
        if not 128 <= dimensions <= 3_072 or timeout <= 0:
            raise ValueError("invalid Gemini embedding provider configuration")
        self.model = model
        self.dimensions = dimensions
        self.api_key = api_key
        self.timeout = timeout
        self.query_task = query_task.strip() or "search result"

    @staticmethod
    def _document_text(content: str) -> str:
        return f"title: none | text: {content}"

    def _query_text(self, query: str) -> str:
        return f"task: {self.query_task} | query: {query}"

    async def _request(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts or len(texts) > self.MAX_BATCH_SIZE:
            raise ValueError("Gemini embedding batch is outside supported bounds")
        if any(not isinstance(text, str) or len(text) > 64 * 1024 for text in texts):
            raise ValueError("Gemini embedding input contains an invalid or oversized text")
        if sum(len(text) for text in texts) > MAX_PROVIDER_INPUT_CHARS:
            raise ValueError("Gemini embedding batch exceeds the total input bound")
        requests = [
            {
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": self.dimensions,
            }
            for text in texts
        ]
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"x-goog-api-key": self.api_key},
        ) as client:
            response = await client.post(
                f"{self.API_BASE_URL}/models/{self.model}:batchEmbedContents",
                json={"requests": requests},
            )
        response.raise_for_status()
        payload = response.json()
        embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ValueError("Gemini embedding provider returned the wrong number of vectors")
        vectors: list[Sequence[float]] = []
        for item in embeddings:
            if not isinstance(item, dict) or "values" not in item:
                raise ValueError("Gemini embedding provider returned an invalid vector")
            vectors.append(_validated_vector(item["values"], self.dimensions))
        return vectors

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        vectors: list[Sequence[float]] = []
        for start in range(0, len(texts), self.MAX_BATCH_SIZE):
            batch = texts[start : start + self.MAX_BATCH_SIZE]
            vectors.extend(await self._request([self._document_text(text) for text in batch]))
        return vectors

    async def embed_query(self, query: str) -> Sequence[Sequence[float]]:
        return await self._request([self._query_text(query)])


class OpenAICompatibleReranker:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 10.0,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("reranker base URL must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("reranker base URL must use HTTPS outside localhost")
        if not model.strip() or timeout <= 0:
            raise ValueError("invalid reranker configuration")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    async def rerank(
        self, query: str, documents: Sequence[tuple[str, str]]
    ) -> Mapping[str, float]:
        if not query or not documents or len(documents) > 100:
            raise ValueError("reranker input is outside supported bounds")
        if any(len(content) > 64 * 1024 for _memory_id, content in documents):
            raise ValueError("reranker document exceeds the supported bound")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.post(
                f"{self.base_url}/rerank",
                json={
                    "model": self.model,
                    "query": query,
                    "documents": [content for _memory_id, content in documents],
                    "top_n": len(documents),
                },
            )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", payload) if isinstance(payload, dict) else payload
        if not isinstance(results, list):
            raise ValueError("reranker returned an invalid result shape")
        scores: dict[str, float] = {}
        for result in results:
            if not isinstance(result, dict):
                continue
            index = result.get("index")
            raw_score = result.get("relevance_score", result.get("score"))
            if not isinstance(index, int) or not 0 <= index < len(documents):
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if math.isfinite(score):
                scores[documents[index][0]] = score
        if not scores:
            raise ValueError("reranker returned no finite scores")
        minimum = min(scores.values())
        maximum = max(scores.values())
        if minimum < 0.0 or maximum > 1.0:
            spread = maximum - minimum
            scores = {
                memory_id: (score - minimum) / spread if spread else 1.0
                for memory_id, score in scores.items()
            }
        return {memory_id: max(0.0, min(1.0, score)) for memory_id, score in scores.items()}


@dataclass(frozen=True)
class VectorSearchHit:
    memory: MemoryRecord
    score: float


class MemoryVectorStore:
    def __init__(
        self,
        db: AsyncSession,
        provider: EmbeddingProvider,
        *,
        model_id: str,
        dimensions: int,
    ):
        self.db = db
        self.provider = provider
        self.model_id = model_id
        self.dimensions = dimensions

    async def upsert_many(
        self, *, account_id: str, memories: Sequence[tuple[str, str]]
    ) -> None:
        if not memories:
            return
        if len(memories) > 2_000:
            raise ValueError("embedding batch exceeds 2000 memories")
        unique_memories = list(dict(memories).items())
        memory_ids = [memory_id for memory_id, _content in unique_memories]
        existing_result = await self.db.execute(
            select(MemoryEmbedding.memory_id, MemoryEmbedding.content_hash).where(
                MemoryEmbedding.account_id == account_id,
                MemoryEmbedding.memory_id.in_(memory_ids),
                MemoryEmbedding.model_id == self.model_id,
                MemoryEmbedding.dimensions == self.dimensions,
            )
        )
        existing_hashes = {memory_id: content_hash for memory_id, content_hash in existing_result.all()}
        content_hashes = {
            memory_id: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for memory_id, content in unique_memories
        }
        pending = [
            (memory_id, content)
            for memory_id, content in unique_memories
            if existing_hashes.get(memory_id) != content_hashes[memory_id]
        ]
        if not pending:
            return

        vectors: list[Sequence[float]] = []
        for batch in _embedding_batches(pending):
            batch_vectors = await self.provider.embed(
                [content for _memory_id, content in batch]
            )
            if len(batch_vectors) != len(batch):
                raise ValueError("embedding provider returned the wrong number of vectors")
            vectors.extend(batch_vectors)
        now = _utcnow()
        values = [
            {
                "id": str(uuid.uuid4()),
                "account_id": account_id,
                "memory_id": memory_id,
                "model_id": self.model_id,
                "dimensions": self.dimensions,
                "embedding": _validated_vector(vector, self.dimensions),
                "created_at": now,
                "updated_at": now,
            }
            for (memory_id, content), vector in zip(pending, vectors)
        ]
        for value in values:
            value["content_hash"] = content_hashes[value["memory_id"]]
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect == "postgresql":
            statement = postgresql_insert(MemoryEmbedding).values(values)
            statement = statement.on_conflict_do_update(
                constraint="uq_memory_embeddings_memory_model",
                set_={
                    "account_id": statement.excluded.account_id,
                    "dimensions": statement.excluded.dimensions,
                    "content_hash": statement.excluded.content_hash,
                    "embedding": statement.excluded.embedding,
                    "updated_at": statement.excluded.updated_at,
                },
            )
            await self.db.execute(statement)
        elif dialect == "sqlite":
            statement = sqlite_insert(MemoryEmbedding).values(values)
            statement = statement.on_conflict_do_update(
                index_elements=["memory_id", "model_id"],
                set_={
                    "account_id": statement.excluded.account_id,
                    "dimensions": statement.excluded.dimensions,
                    "content_hash": statement.excluded.content_hash,
                    "embedding": statement.excluded.embedding,
                    "updated_at": statement.excluded.updated_at,
                },
            )
            await self.db.execute(statement)
        else:
            for value in values:
                result = await self.db.execute(
                    select(MemoryEmbedding).where(
                        MemoryEmbedding.memory_id == value["memory_id"],
                        MemoryEmbedding.model_id == self.model_id,
                    )
                )
                row = result.scalar_one_or_none()
                if row is None:
                    self.db.add(MemoryEmbedding(**value))
                else:
                    row.embedding = value["embedding"]
                    row.dimensions = self.dimensions
                    row.content_hash = value["content_hash"]
                    row.updated_at = now
        await self.db.flush()

    async def upsert(self, *, account_id: str, memory_id: str, content: str) -> None:
        await self.upsert_many(account_id=account_id, memories=[(memory_id, content)])

    async def search(
        self,
        *,
        account_id: str,
        query_text: str,
        scope: str | None,
        limit: int,
        include_superseded: bool = False,
    ) -> list[VectorSearchHit]:
        embed_query = getattr(self.provider, "embed_query", None)
        vectors = (
            await embed_query(query_text)
            if embed_query is not None
            else await self.provider.embed([query_text])
        )
        query_vector = _validated_vector(vectors[0], self.dimensions)
        filters = [
            MemoryEmbedding.account_id == account_id,
            MemoryEmbedding.model_id == self.model_id,
            MemoryEmbedding.dimensions == self.dimensions,
            MemoryRecord.account_id == account_id,
            (MemoryRecord.expires_at.is_(None)) | (MemoryRecord.expires_at > _utcnow()),
        ]
        if not include_superseded:
            filters.append(MemoryRecord.superseded_at.is_(None))
        if scope is not None:
            filters.append(MemoryRecord.scope == scope)
        query = select(MemoryRecord, MemoryEmbedding.embedding).join(
            MemoryEmbedding, MemoryEmbedding.memory_id == MemoryRecord.id
        ).where(*filters)
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect == "postgresql":
            await self.db.execute(
                select(
                    func.set_config(
                        "hnsw.ef_search",
                        str(max(1, min(1_000, settings.BOXXKITE_MEMORY_HNSW_EF_SEARCH))),
                        True,
                    ),
                    func.set_config("hnsw.iterative_scan", "relaxed_order", True),
                    func.set_config(
                        "hnsw.max_scan_tuples",
                        str(settings.BOXXKITE_MEMORY_HNSW_MAX_SCAN_TUPLES),
                        True,
                    ),
                )
            )
            distance = cast(MemoryEmbedding.embedding, VECTOR(self.dimensions)).cosine_distance(
                query_vector
            )
            result = await self.db.execute(
                select(MemoryRecord, distance.label("distance"))
                .join(MemoryEmbedding, MemoryEmbedding.memory_id == MemoryRecord.id)
                .where(*filters)
                .order_by(distance)
                .limit(limit)
            )
            return [
                VectorSearchHit(
                    memory=row,
                    score=max(0.0, min(1.0, 1.0 - float(distance) / 2.0)),
                )
                for row, distance in result.all()
            ]

        result = await self.db.execute(query.limit(2_000))
        hits = [
            VectorSearchHit(memory=row, score=max(0.0, (_cosine(query_vector, embedding) + 1.0) / 2.0))
            for row, embedding in result.all()
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


@lru_cache(maxsize=1)
def get_memory_embedding_provider() -> EmbeddingProvider | None:
    if not settings.BOXXKITE_MEMORY_EMBEDDINGS_ENABLED:
        return None
    if settings.BOXXKITE_MEMORY_EMBEDDING_PROVIDER == "gemini":
        return GeminiEmbeddingProvider(
            model=settings.BOXXKITE_MEMORY_EMBEDDING_MODEL,
            dimensions=settings.BOXXKITE_MEMORY_EMBEDDING_DIMENSIONS,
            api_key=settings.BOXXKITE_MEMORY_EMBEDDING_API_KEY,
            timeout=settings.BOXXKITE_MEMORY_EMBEDDING_TIMEOUT_SECONDS,
        )
    return OpenAICompatibleEmbeddingProvider(
        base_url=settings.BOXXKITE_MEMORY_EMBEDDING_BASE_URL,
        model=settings.BOXXKITE_MEMORY_EMBEDDING_MODEL,
        dimensions=settings.BOXXKITE_MEMORY_EMBEDDING_DIMENSIONS,
        query_instruction=settings.BOXXKITE_MEMORY_EMBEDDING_QUERY_INSTRUCTION,
        api_key=settings.BOXXKITE_MEMORY_EMBEDDING_API_KEY,
        timeout=settings.BOXXKITE_MEMORY_EMBEDDING_TIMEOUT_SECONDS,
    )


@lru_cache(maxsize=1)
def get_memory_reranker() -> RerankerProvider | None:
    if not settings.BOXXKITE_MEMORY_RERANKING_ENABLED:
        return None
    return OpenAICompatibleReranker(
        base_url=settings.BOXXKITE_MEMORY_RERANKER_BASE_URL,
        model=settings.BOXXKITE_MEMORY_RERANKER_MODEL,
        api_key=settings.BOXXKITE_MEMORY_RERANKER_API_KEY,
        timeout=settings.BOXXKITE_MEMORY_RERANKER_TIMEOUT_SECONDS,
    )
