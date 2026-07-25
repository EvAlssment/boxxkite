# Native Mistral function-calling example

Closes a real, previously-undocumented gap noted in `docs/E2B-COMPARISON.md`
§4.1: E2B has a narrated "connect an LLM provider" quickstart per provider
(OpenAI, Gemini, Mistral, Groq); boxkite had the equivalent underlying
capability (`boxkite.tools.adapters.to_openai_functions`, pure stdlib, no
provider SDK dependency) but no runnable example showing it for Mistral.
This is that example.

## What it does

Same task as `../openai_function_calling`: ask the agent to write a small
Python script to a file and run it, then report exactly what it printed.
Mistral's chat + tool-calling API is close to OpenAI's shape --
`chat.complete(model=..., messages=..., tools=...)` returning a
`choices[0].message` with a `tool_calls` list -- so `to_openai_functions()`'s
output is used as-is for the `tools=` argument (verified against the
installed `mistralai` 2.6.0 package's own `Tool`/`Function` models: a `Tool`
wraps a `Function(name, description, parameters: Dict[str, Any])`, the same
shape `to_openai_functions()` already produces).

Two real differences from the OpenAI example, both verified against the
installed package rather than assumed from memory:

1. The importable client class lives at `mistralai.client.Mistral`, not
   `mistralai.Mistral` -- this package version's top-level `mistralai` is a
   thin namespace covering `mistralai.client` (this SDK), `mistralai.azure`,
   and `mistralai.gcp` variants, and the package's own bundled README
   documents `from mistralai.client import Mistral`.
2. A returned tool call's `function.arguments` is typed as `Dict[str, Any]
   | str` (`mistralai.client.models.functioncall.Arguments`), not always a
   JSON string like OpenAI's -- `agent.py` handles both cases.

No LangChain/LangGraph or other framework agent class is used here either;
it's the same plain tool-calling loop as the OpenAI example, dispatching
each `tool_call` back to the matching `ToolSpec.handler()` by name.

## Prerequisites

1. A running boxkite stack: `boxkite up` from the repo root (see the main
   README's "Quickstart: docker-compose" section).
2. `pip install -e ../..` (boxkite itself -- no extra needed) then
   `pip install -r requirements.txt` (just the `mistralai` package).
3. `MISTRAL_API_KEY` set.

## Run

```bash
boxkite up
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080
export MISTRAL_API_KEY=...

python agent.py
```

Expect output resembling:

```
Creating sandbox session <uuid> ...
Tools wired: ['bash_tool', 'file_create']
Running agent...
------------------------------------------------------------
The script printed:
hello from boxkite
2026-07-11
Destroying session <uuid> ...
```

## What's verified vs. what needs a live run

Imports and tool wiring were verified against the actual installed
`boxkite`/`mistralai` 2.6.0 package versions in this environment: the
`mistralai.client.Mistral` import path, `Mistral(api_key=...)` and
`client.chat.complete_async`'s real async signature, and the
`Tool`/`Function`/`ToolCall`/`FunctionCall` model field sets (confirming the
tool schema shape and the `arguments` field's `Dict | str` union) were all
inspected directly (via `inspect.signature` and `model_fields`) against the
installed package -- no import errors, no signature mismatches. The
end-to-end agent run against a live Mistral model was **not** exercised in
this environment (no API key available); please verify with your own key
before relying on it.
