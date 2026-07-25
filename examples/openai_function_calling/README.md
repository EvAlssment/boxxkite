# Native OpenAI function-calling example

Closes a real, previously-undocumented gap noted in `docs/E2B-COMPARISON.md`
§4.1: E2B has a narrated "connect an LLM provider" quickstart per provider
(OpenAI, Anthropic, Mistral, Groq); boxkite had the equivalent underlying
capability (`boxkite.tools.adapters.to_openai_functions`, pure stdlib, no
`openai` package dependency) but no runnable example showing it. This is
that example.

## What it does

Same task as `../langchain_tool_calling`: ask the agent to write a small
Python script to a file and run it, then report exactly what it printed.
The difference is the wiring -- this example talks to the `openai` SDK
directly, with no LangChain/LangGraph in the loop at all. `to_openai_functions()`
converts boxkite's two `ToolSpec`s (`bash_tool`, `file_create`) into the
`{"type": "function", "function": {...}}` schema OpenAI's `tools=` parameter
expects; each returned `tool_call` is dispatched back to the matching
`ToolSpec.handler()` by name in a plain loop -- no framework-specific
tool-calling agent class involved.

## Prerequisites

1. A running boxkite stack: `boxkite up` from the repo root (see the main
   README's "Quickstart: docker-compose" section).
2. `pip install -e ../..` (boxkite itself -- no extra needed, unlike the
   LangChain examples) then `pip install -r requirements.txt` (just the
   `openai` package).
3. `OPENAI_API_KEY` set.

## Run

```bash
boxkite up
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080
export OPENAI_API_KEY=sk-...

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

Imports and tool wiring (`to_openai_functions`, `create_bash_tool_spec`,
`create_file_create_tool_spec`, the `openai` SDK's `chat.completions.create`
call shape) were verified against the actual installed `boxkite`/`openai`
package versions in this environment -- no import errors, no signature
mismatches, and the schema `to_openai_functions()` produces was inspected
directly. The end-to-end agent run against a live OpenAI model was **not**
exercised in this environment (no API key available); please verify with
your own key before relying on it.
