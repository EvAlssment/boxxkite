"""Local Hugging Face Qwen model server for Boxkite MemoryBase."""

from __future__ import annotations

import hmac
import math
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOXKITE_", case_sensitive=False)

    embedding_model: str = "microsoft/harrier-oss-v1-0.6b"
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    device: Literal["auto", "cpu", "mps", "cuda"] = "auto"
    dtype: Literal["auto", "float32", "float16", "bfloat16"] = "auto"
    embedding_dimensions: int = 1024
    max_batch_size: int = 128
    max_input_chars: int = 1_048_576
    max_document_chars: int = 64 * 1024
    embedding_max_length: int = 8_192
    max_length: int = 8_192
    reranker_max_length: int = 2_048
    reranker_batch_size: int = 8
    max_reranker_candidates: int = 100
    eager_load: bool = False
    api_key: str | None = None

    @field_validator("embedding_dimensions")
    @classmethod
    def validate_embedding_dimensions(cls, value: int) -> int:
        if not 32 <= value <= 1_024:
            raise ValueError("embedding dimensions must be between 32 and 1024")
        return value

    @field_validator("max_batch_size")
    @classmethod
    def validate_max_batch_size(cls, value: int) -> int:
        if not 1 <= value <= 128:
            raise ValueError("max batch size must be between 1 and 128")
        return value

    @field_validator(
        "max_input_chars",
        "max_document_chars",
        "embedding_max_length",
        "max_length",
        "reranker_max_length",
    )
    @classmethod
    def validate_positive_bounds(cls, value: int) -> int:
        if value < 1:
            raise ValueError("model-server bounds must be positive")
        return value

    @field_validator("max_reranker_candidates")
    @classmethod
    def validate_reranker_candidates(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("reranker candidates must be between 1 and 100")
        return value

    @field_validator("reranker_batch_size")
    @classmethod
    def validate_reranker_batch_size(cls, value: int) -> int:
        if not 1 <= value <= 32:
            raise ValueError("reranker batch size must be between 1 and 32")
        return value


settings = Settings()


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input: str | list[str]
    dimensions: int | None = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: str | list[str]) -> str | list[str]:
        values = [value] if isinstance(value, str) else value
        if not values or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("input must contain non-empty text")
        if any(len(item) > settings.max_document_chars for item in values):
            raise ValueError("embedding input exceeds the supported text bound")
        if len(values) > settings.max_batch_size:
            raise ValueError("embedding batch exceeds the supported batch bound")
        if sum(len(item) for item in values) > settings.max_input_chars:
            raise ValueError("embedding request exceeds the supported input bound")
        return value

    @field_validator("dimensions")
    @classmethod
    def validate_dimensions(cls, value: int | None) -> int | None:
        if value is not None and not 32 <= value <= 1_024:
            raise ValueError("dimensions must be between 32 and 1024")
        return value


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    query: str
    documents: list[str]
    top_n: int | None = None

    @field_validator("model", "query")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model and query must not be empty")
        return value

    @field_validator("query")
    @classmethod
    def validate_query_size(cls, value: str) -> str:
        if len(value) > settings.max_document_chars:
            raise ValueError("reranker query exceeds the supported text bound")
        return value

    @field_validator("documents")
    @classmethod
    def validate_documents(cls, value: list[str]) -> list[str]:
        if not value or len(value) > settings.max_reranker_candidates:
            raise ValueError("reranker document count is outside the supported bound")
        if any(not item.strip() for item in value):
            raise ValueError("reranker documents must not be empty")
        if any(len(item) > settings.max_document_chars for item in value):
            raise ValueError("reranker document exceeds the supported text bound")
        if sum(len(item) for item in value) > settings.max_input_chars:
            raise ValueError("reranker request exceeds the supported input bound")
        return value

    @model_validator(mode="after")
    def validate_top_n(self) -> "RerankRequest":
        if self.top_n is not None and not 1 <= self.top_n <= len(self.documents):
            raise ValueError("top_n must be between 1 and the document count")
        return self


def _device(torch):
    if settings.device == "cpu":
        return torch.device("cpu")
    if settings.device == "mps":
        if not torch.backends.mps.is_available():
            raise RuntimeError("MPS was requested but is unavailable")
        return torch.device("mps")
    if settings.device == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but is unavailable")
        return torch.device("cuda")
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _dtype(torch, device):
    if settings.dtype == "float32":
        return torch.float32
    if settings.dtype == "float16":
        return torch.float16
    if settings.dtype == "bfloat16":
        return torch.bfloat16
    return torch.float16 if device.type in {"mps", "cuda"} else torch.float32


def _last_token_pool(last_hidden_state, attention_mask):
    left_padding = bool(attention_mask[:, -1].sum() == attention_mask.shape[0])
    if left_padding:
        return last_hidden_state[:, -1]
    sequence_lengths = attention_mask.sum(dim=1) - 1
    batch_size = last_hidden_state.shape[0]
    return last_hidden_state[
        range(batch_size), sequence_lengths
    ]


def _finite_vectors(vectors: list[list[float]], dimensions: int) -> list[list[float]]:
    result: list[list[float]] = []
    for vector in vectors:
        if len(vector) != dimensions or not all(math.isfinite(value) for value in vector):
            raise RuntimeError("model returned an invalid embedding vector")
        result.append(vector)
    return result


class QwenModelService:
    def __init__(self) -> None:
        self._embedding_tokenizer = None
        self._embedding_model = None
        self._reranker_tokenizer = None
        self._reranker_model = None
        self._torch = None
        self._device = None
        self._dtype = None
        self._lock = threading.RLock()

    @property
    def embedding_loaded(self) -> bool:
        return self._embedding_model is not None

    @property
    def reranker_loaded(self) -> bool:
        return self._reranker_model is not None

    def _load_common(self) -> None:
        if self._torch is not None:
            return
        import torch

        self._torch = torch
        self._device = _device(torch)
        self._dtype = _dtype(torch, self._device)

    def load_embedding(self) -> None:
        with self._lock:
            if self.embedding_loaded:
                return
            self._load_common()
            from transformers import AutoModel, AutoTokenizer

            self._embedding_tokenizer = AutoTokenizer.from_pretrained(
                settings.embedding_model,
                padding_side="left",
            )
            self._embedding_model = AutoModel.from_pretrained(
                settings.embedding_model,
                dtype=self._dtype,
            ).to(self._device)
            self._embedding_model.eval()

    def load_reranker(self) -> None:
        with self._lock:
            if self.reranker_loaded:
                return
            self._load_common()
            from transformers import AutoModelForCausalLM, AutoTokenizer

            self._reranker_tokenizer = AutoTokenizer.from_pretrained(
                settings.reranker_model,
                padding_side="left",
            )
            if self._reranker_tokenizer.pad_token is None:
                self._reranker_tokenizer.pad_token = self._reranker_tokenizer.eos_token
            self._reranker_model = AutoModelForCausalLM.from_pretrained(
                settings.reranker_model,
                dtype=self._dtype,
            ).to(self._device)
            self._reranker_model.eval()

    def load_all(self) -> None:
        self.load_embedding()
        self.load_reranker()

    def embed(self, texts: list[str], dimensions: int | None = None) -> list[list[float]]:
        self.load_embedding()
        output_dimensions = dimensions or settings.embedding_dimensions
        order = sorted(range(len(texts)), key=lambda index: len(texts[index]))
        ordered_vectors: list[list[float]] = []
        with self._lock, self._torch.inference_mode():
            for start in range(0, len(order), settings.max_batch_size):
                batch_texts = [
                    texts[index]
                    for index in order[start : start + settings.max_batch_size]
                ]
                batch = self._embedding_tokenizer(
                    batch_texts,
                    padding=True,
                    truncation=True,
                    max_length=settings.embedding_max_length,
                    return_tensors="pt",
                )
                batch = {key: value.to(self._device) for key, value in batch.items()}
                outputs = self._embedding_model(**batch)
                embeddings = _last_token_pool(outputs.last_hidden_state, batch["attention_mask"])
                embeddings = self._torch.nn.functional.normalize(embeddings, p=2, dim=1)
                if output_dimensions > embeddings.shape[1]:
                    raise RuntimeError("requested dimensions exceed the model output size")
                if output_dimensions != embeddings.shape[1]:
                    embeddings = embeddings[:, :output_dimensions]
                    embeddings = self._torch.nn.functional.normalize(embeddings, p=2, dim=1)
                ordered_vectors.extend(embeddings.float().cpu().tolist())
        vectors: list[list[float] | None] = [None] * len(texts)
        for index, vector in zip(order, ordered_vectors):
            vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise RuntimeError("model returned an incomplete embedding batch")
        return _finite_vectors([vector for vector in vectors if vector is not None], output_dimensions)

    def rerank(self, query: str, documents: list[str], top_n: int | None) -> list[dict]:
        self.load_reranker()
        tokenizer = self._reranker_tokenizer
        prefix = (
            "<|im_start|>system\n"
            "Judge whether the Document meets the requirements based on the Query "
            "and the Instruct provided. Note that the answer can only be \"yes\" or \"no\"."
            "<|im_end|>\n<|im_start|>user\n"
        )
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        instruction = "Retrieve passages relevant to the user's memory query."
        pairs = [
            f"<Instruct>: {instruction}\n\n<Query>: {query}\n\n<Document>: {document}"
            for document in documents
        ]
        prefix_tokens = tokenizer.encode(prefix, add_special_tokens=False)
        suffix_tokens = tokenizer.encode(suffix, add_special_tokens=False)
        max_pair_length = max(
            1,
            settings.reranker_max_length
            - len(prefix_tokens)
            - len(suffix_tokens),
        )
        values: list[float] = []
        true_id = tokenizer("yes", add_special_tokens=False).input_ids[0]
        false_id = tokenizer("no", add_special_tokens=False).input_ids[0]
        for start in range(0, len(pairs), settings.reranker_batch_size):
            encoded = tokenizer(
                pairs[start : start + settings.reranker_batch_size],
                padding=False,
                truncation="longest_first",
                max_length=max_pair_length,
                return_attention_mask=False,
            )
            input_ids = [
                prefix_tokens + tokens + suffix_tokens for tokens in encoded["input_ids"]
            ]
            with self._lock, self._torch.inference_mode():
                batch = tokenizer.pad(
                    {"input_ids": input_ids},
                    padding=True,
                    return_tensors="pt",
                )
                batch = {key: value.to(self._device) for key, value in batch.items()}
                logits = self._reranker_model(**batch).logits[:, -1, :]
                scores = self._torch.softmax(logits[:, [false_id, true_id]], dim=1)[:, 1]
                values.extend(scores.float().cpu().tolist())
        results = [
            {"index": index, "relevance_score": max(0.0, min(1.0, float(score)))}
            for index, score in enumerate(values)
        ]
        results.sort(key=lambda item: (-item["relevance_score"], item["index"]))
        return results[: top_n or len(results)]


service = QwenModelService()


def _authorize(authorization: str | None) -> None:
    if not settings.api_key:
        return
    expected = f"Bearer {settings.api_key}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid model-server credentials")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.eager_load:
        service.load_all()
    yield


app = FastAPI(title="Boxkite Memory Model Server", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "embedding_loaded": service.embedding_loaded, "reranker_loaded": service.reranker_loaded}


@app.get("/health/ready")
def ready() -> dict:
    if not service.embedding_loaded:
        raise HTTPException(status_code=503, detail="embedding model is not loaded")
    return {"status": "ready", "embedding_loaded": True, "reranker_loaded": service.reranker_loaded}


@app.post("/v1/embeddings")
def embeddings(body: EmbeddingRequest, authorization: str | None = Header(default=None)) -> dict:
    _authorize(authorization)
    if body.model != settings.embedding_model:
        raise HTTPException(status_code=400, detail="unknown embedding model")
    texts = [body.input] if isinstance(body.input, str) else body.input
    try:
        vectors = service.embed(texts, body.dimensions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="embedding model unavailable") from exc
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vector, "index": index}
            for index, vector in enumerate(vectors)
        ],
        "model": body.model,
    }


@app.post("/v1/rerank")
def rerank(body: RerankRequest, authorization: str | None = Header(default=None)) -> dict:
    _authorize(authorization)
    if body.model != settings.reranker_model:
        raise HTTPException(status_code=400, detail="unknown reranker model")
    try:
        results = service.rerank(body.query, body.documents, body.top_n)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="reranker model unavailable") from exc
    return {"model": body.model, "results": results}
