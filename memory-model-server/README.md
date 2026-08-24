# Boxkite Memory Model Server

Standalone Hugging Face inference for Boxkite MemoryBase. It keeps PyTorch and
the Qwen weights out of the control-plane process while exposing the exact
local HTTP contracts used by the control-plane embedding and reranking
adapters.

## Local M4 setup

```bash
cd public/memory-model-server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
BOXKITE_DEVICE=auto \
BOXKITE_EAGER_LOAD=true \
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

The first request downloads `microsoft/harrier-oss-v1-0.6b` and
`Qwen/Qwen3-Reranker-0.6B` from Hugging Face into the normal Transformers
cache. On Apple Silicon, `BOXKITE_DEVICE=auto` selects MPS when available.
Set `BOXKITE_API_KEY` if the service should require a bearer token.

The control-plane defaults already target this service:

```text
BOXXKITE_MEMORY_EMBEDDINGS_ENABLED=true
BOXXKITE_MEMORY_EMBEDDING_BASE_URL=http://127.0.0.1:8000/v1
BOXXKITE_MEMORY_RERANKING_ENABLED=true
BOXXKITE_MEMORY_RERANKER_BASE_URL=http://127.0.0.1:8000/v1
```

The server emits 1024-dimensional Harrier vectors by default. Embedding batches are length-bucketed
and use an 8,192-token serving window by default; override it with
`BOXKITE_EMBEDDING_MAX_LENGTH` when a deployment has a tighter latency budget. `POST /v1/embeddings`
also accepts a `dimensions` value from 32 through 1024. If the control-plane uses an API key,
set the matching `BOXXKITE_MEMORY_EMBEDDING_API_KEY` and
`BOXXKITE_MEMORY_RERANKER_API_KEY`.

Reranking is bounded to 2,048 tokens per pair and eight pairs per inference batch
by default, preventing long evidence windows or large candidate pools from
creating an unsafe padded tensor. Tune `BOXKITE_RERANKER_MAX_LENGTH` and
`BOXKITE_RERANKER_BATCH_SIZE` only after measuring memory and latency.

## API checks

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/embeddings \
  -H 'content-type: application/json' \
  -d '{"model":"microsoft/harrier-oss-v1-0.6b","input":"remember this"}'
curl http://127.0.0.1:8000/v1/rerank \
  -H 'content-type: application/json' \
  -d '{"model":"Qwen/Qwen3-Reranker-0.6B","query":"language","documents":["Python","Java"]}'
```

Run the service tests without downloading model weights:

```bash
../../.venv/bin/pytest -q tests
```
