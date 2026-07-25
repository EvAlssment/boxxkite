# Native Gemini function-calling example

Closes a real, previously-undocumented gap noted in `docs/E2B-COMPARISON.md`
§4.1: E2B has a narrated "connect an LLM provider" quickstart per provider
(OpenAI, Gemini, Mistral, Groq); boxkite had the equivalent underlying
capability (`boxkite.tools.adapters.to_openai_functions`, pure stdlib, no
provider SDK dependency) but no runnable example showing it for Gemini.
This is that example.

## What it does

Same task as `../openai_function_calling`: ask the agent to write a small
Python script to a file and run it, then report exactly what it printed.
The wiring is necessarily different from the OpenAI example, though --
Gemini's function-calling shape is not a `messages` + `tool_calls` list.
It's a `Content`/`Part` conversation: each turn is a
`Content(role=..., parts=[Part(...)])`, a model's function call comes back
as a `Part.function_call` (a `FunctionCall(name=..., args=...)`), and the
tool result goes back as a new `Content` holding a
`Part.from_function_response(name=..., response={...})`.

This example still starts from `to_openai_functions()` for the actual
schema construction (name/description/JSON-schema parameters), then unwraps
each entry into a `google.genai.types.FunctionDeclaration` via
`parameters_json_schema` (which accepts a raw JSON-schema dict directly --
verified against the installed `google-genai` 2.11.0 package). No
LangChain/LangGraph or other framework agent class is used; the tool-calling
loop is a plain `while`/`for` loop dispatching each `FunctionCall` back to
the matching `ToolSpec.handler()` by name.

## Prerequisites

1. A running boxkite stack: `boxkite up` from the repo root (see the main
   README's "Quickstart: docker-compose" section).
2. `pip install -e ../..` (boxkite itself -- no extra needed) then
   `pip install -r requirements.txt` (just the `google-genai` package).
3. `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) set -- `genai.Client()` picks
   either up from the environment with no explicit `api_key=` argument
   needed.

## Run

```bash
boxkite up
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080
export GEMINI_API_KEY=...

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
`boxkite`/`google-genai` 2.11.0 package versions in this environment:
`genai.Client(api_key=...)` construction, `types.FunctionDeclaration`'s real
field set (confirming `parameters_json_schema` takes a raw dict, unlike
`parameters` which wants a `Schema` object), `types.Content`/`types.Part`'s
field sets, and `client.aio.models.generate_content`'s async signature were
all inspected directly (via `inspect.signature` and `model_fields`) against
the installed package -- no import errors, no signature mismatches. The
end-to-end agent run against a live Gemini model was **not** exercised in
this environment (no API key available); please verify with your own key
before relying on it.
