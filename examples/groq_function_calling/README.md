# Native Groq function-calling example

Closes a real, previously-undocumented gap noted in `docs/E2B-COMPARISON.md`
§4.1: E2B has a narrated "connect an LLM provider" quickstart per provider
(OpenAI, Gemini, Mistral, Groq); boxkite had the equivalent underlying
capability (`boxkite.tools.adapters.to_openai_functions`, pure stdlib, no
provider SDK dependency) but no runnable example showing it for Groq. This
is that example.

## What it does

Same task as `../openai_function_calling`: ask the agent to write a small
Python script to a file and run it, then report exactly what it printed.
Groq's SDK is deliberately OpenAI-compatible -- a drop-in
`chat.completions.create(model=..., messages=..., tools=...)` shape
returning a pydantic `ChatCompletionMessage` with the same `tool_calls` /
`model_dump()` behavior (verified against the installed `groq` 1.5.0
package) -- so this example is the closest mirror of
`../openai_function_calling` of the three new provider examples: the loop
body is unchanged, only the client class and default model name differ.

No LangChain/LangGraph or other framework agent class is used; the
tool-calling loop is the same plain loop dispatching each `tool_call` back
to the matching `ToolSpec.handler()` by name.

## Prerequisites

1. A running boxkite stack: `boxkite up` from the repo root (see the main
   README's "Quickstart: docker-compose" section).
2. `pip install -e ../..` (boxkite itself -- no extra needed) then
   `pip install -r requirements.txt` (just the `groq` package).
3. `GROQ_API_KEY` set.

## Run

```bash
boxkite up
# SandboxManager auto-loads the sidecar token + URL from ~/.boxkite/local.env
# (written by `boxkite up`), so no manual SIDECAR_AUTH_TOKEN export is needed.
export RUNTIME_MODE=compose
export GROQ_API_KEY=...

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
`boxkite`/`groq` 1.5.0 package versions in this environment:
`AsyncGroq(api_key=...)` construction, `client.chat.completions.create`'s
real async signature, and `ChatCompletionMessage`'s field set (confirming
the same `tool_calls`/`model_dump()` shape as OpenAI's client) were all
inspected directly (via `inspect.signature` and `model_fields`) against the
installed package -- no import errors, no signature mismatches. The
end-to-end agent run against a live Groq model was **not** exercised in
this environment (no API key available); please verify with your own key
before relying on it.
