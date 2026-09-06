# boxxkite examples

Every example has its own README with a copy-pasteable run command and a
one-line statement of what it demonstrates. Start with the category that
matches your use case:

| Category | Use it for |
|---|---|
| [`basic/`](basic/) | Direct HTTP, hosted control-plane, and interpreter workflows |
| [`agent-integrations/`](agent-integrations/) | LangGraph, LangChain, SDK, and provider tool calling |
| [`browser-desktop/`](browser-desktop/) | Claude Code, browser/desktop, and Foundry workflows |
| [`storage/`](storage/) | Declarative image and storage-oriented workflows |

## Basic

| Example | Demonstrates |
|---|---|
| [`raw_api/`](basic/raw_api/) | Calling the sidecar with curl or `requests` |
| [`hosted_control_plane/`](basic/hosted_control_plane/) | Signup, API key, sandbox, execution, and teardown |
| [`stateful_interpreters/`](basic/stateful_interpreters/) | Persistent Python and Node interpreter state |

## Agent integrations

| Example | Demonstrates |
|---|---|
| [`langgraph_agent/`](agent-integrations/langgraph_agent/) | A LangGraph coding-agent loop with sandbox tools |
| [`langchain_tool_calling/`](agent-integrations/langchain_tool_calling/) | Minimal LangChain tool calling |
| [`llamaindex_agent/`](agent-integrations/llamaindex_agent/) | LlamaIndex `ReActAgent` integration |
| [`openai_agents_sdk/`](agent-integrations/openai_agents_sdk/) | OpenAI Agents SDK `Agent`/`Runner` integration |
| [`openai_function_calling/`](agent-integrations/openai_function_calling/) | Native OpenAI-compatible function calling |
| [`google-adk/`](agent-integrations/google-adk/) | Google ADK function tools |
| [`gemini_function_calling/`](agent-integrations/gemini_function_calling/) | Gemini native function calling |
| [`mistral_function_calling/`](agent-integrations/mistral_function_calling/) | Mistral native function calling |
| [`groq_function_calling/`](agent-integrations/groq_function_calling/) | Groq's OpenAI-compatible function calling |

## Browser and desktop

| Example | Demonstrates |
|---|---|
| [`claude_code_sandbox/`](browser-desktop/claude_code_sandbox/) | Running Claude Code headlessly in a sandbox |
| [`claude_code_declarative_builder/`](browser-desktop/claude_code_declarative_builder/) | Building a Claude Code image through the image builder |
| [`foundry_audit_sandbox/`](browser-desktop/foundry_audit_sandbox/) | A deterministic Foundry/Anvil audit sandbox |

## Storage and custom images

| Example | Demonstrates |
|---|---|
| [`quant_research_declarative_builder/`](storage/quant_research_declarative_builder/) | Building and smoke-testing a quant-research image |

## Common prerequisites

Most examples need a running local stack:

```bash
boxxkite up
```

The example README gives the exact environment variables and optional
provider dependency for that workflow. Examples that call an LLM also need a
provider key; the direct HTTP examples do not.
