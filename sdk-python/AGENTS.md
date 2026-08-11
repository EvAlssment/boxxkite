# AGENTS.md — sdk-python

Guide for an AI coding agent working in `sdk-python/`. For DCO sign-off
and the fork/PR flow, see the repo root's
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Setup

```bash
cd sdk-python
pip install -e ".[dev,langchain]"
```

## Test

```bash
pytest tests/
```

Tests mock the control-plane via `httpx.MockTransport` — no real
deployment (hosted or self-hosted) needed to run the suite.

## Conventions

- This SDK, `sdk-go`, and `sdk-rust` are hand-written and independently
  maintained against the control-plane's REST API — there is no OpenAPI
  spec or codegen anywhere in this repo. A control-plane API change needs
  a matching hand-edit here, and it's easy for the SDKs to drift out of
  shape from each other (see `sdk-rust/README.md`'s own notes on a prior
  drift incident from hand-typing response models). If you're adding a
  new endpoint wrapper, check whether `sdk-go`/`sdk-rust` need the
  equivalent change in the same PR, or file a follow-up issue if not.
- Response models are hand-typed to mirror the control-plane's Pydantic
  schemas — check `control-plane/src/control_plane/schemas.py` for the
  actual current shape (nullable fields, enum values) rather than
  assuming an older SDK method's shape still matches.
