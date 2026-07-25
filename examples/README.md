# boxkite cookbook

Runnable examples of using boxkite from different angles: a full LangGraph
agent, a minimal LangChain agent, raw HTTP calls with no framework at all,
and the hosted multi-tenant control-plane API. Every example targets
boxkite's actual, current APIs (`SandboxManager`, `create_sandbox_tools`,
the sidecar's HTTP routes, the `boxkite` CLI, and the `control-plane/`
router contracts) -- not illustrative pseudocode.

## Which example should I run?

| I want to... | Start here |
|---|---|
| See boxkite's full 5-tool surface handed to a real agent, doing a concrete multi-step task | [`langgraph_agent/`](langgraph_agent/) |
| See the fastest possible "it works" with the smallest amount of code | [`langchain_tool_calling/`](langchain_tool_calling/) |
| Wire boxkite directly into the OpenAI SDK's native function-calling, no agent framework at all | [`openai_function_calling/`](openai_function_calling/) |
| Use boxkite from a LlamaIndex `ReActAgent` | [`llamaindex_agent/`](llamaindex_agent/) |
| Use boxkite tools from the OpenAI Agents SDK's `Agent`/`Runner` | [`openai_agents_sdk/`](openai_agents_sdk/) |
| Run Claude Code itself, headlessly, inside a sandbox | [`claude_code_sandbox/`](claude_code_sandbox/) |
| Build a Claude-Code-capable image via the declarative builder API instead of a hand-maintained Dockerfile | [`claude_code_declarative_builder/`](claude_code_declarative_builder/) |
| Build a quant-research image (vectorbt, backtrader, TA-Lib, QuantLib, quantstats) via the declarative builder | [`quant_research_declarative_builder/`](quant_research_declarative_builder/) |
| Run a smart-contract-audit agent (Foundry/Anvil) against a deterministic, network-dark local chain | [`foundry_audit_sandbox/`](foundry_audit_sandbox/) |
| Wire boxkite into Gemini's native function-calling | [`gemini_function_calling/`](gemini_function_calling/) |
| Wire boxkite into Mistral's native function-calling | [`mistral_function_calling/`](mistral_function_calling/) |
| Wire boxkite into Groq's (OpenAI-compatible) function-calling | [`groq_function_calling/`](groq_function_calling/) |
| Integrate with a framework that isn't LangChain/LlamaIndex, or write my own tool-calling layer | [`raw_api/`](raw_api/) |
| Understand the hosted, multi-tenant control-plane API (accounts, API keys, sessions) | [`hosted_control_plane/`](hosted_control_plane/) |
| See `python_interpreter`/`node_interpreter`'s persistent, kept-alive statefulness across calls (vs. `bash_tool`'s fresh-subprocess-per-call) | [`stateful_interpreters/`](stateful_interpreters/) |

## Prerequisites (all examples)

Every example needs a running boxkite **sandbox runtime** -- either:

- **Local docker-compose** (`boxkite up`, or `docker compose -f
  ../deploy/docker-compose.yml up -d --build` from the repo root) -- the
  fastest path, no Kubernetes required. This is what all four examples'
  own READMEs assume by default.
- **A real Kubernetes cluster** (see the main [README](../README.md)'s
  "Quickstart: real Kubernetes, via kind" section) -- swap `RUNTIME_MODE=k8s`
  and the relevant `SANDBOX_IMAGE`/`SIDECAR_IMAGE`/proxy env vars into any
  example below; `SandboxManager` picks the runtime up from the
  environment, the example code itself doesn't change.

The two LangChain/LangGraph examples additionally need an LLM API key
(Anthropic by default) -- `raw_api/` and `hosted_control_plane/` do not,
since they call the sidecar/control-plane HTTP APIs directly with no LLM
in the loop.

## Directory layout

```
examples/
├── langgraph_agent/         # Full 5-tool LangGraph agent (the headline example)
│   ├── agent.py
│   ├── requirements.txt
│   └── README.md
├── langchain_tool_calling/  # Minimal 2-tool LangChain agent
│   ├── agent.py
│   ├── requirements.txt
│   └── README.md
├── openai_function_calling/ # Native OpenAI tool-calling, no agent framework
│   ├── agent.py
│   ├── requirements.txt
│   └── README.md
├── llamaindex_agent/         # LlamaIndex ReActAgent via to_llamaindex_tools
│   ├── agent.py
│   ├── requirements.txt
│   └── README.md
├── openai_agents_sdk/         # OpenAI Agents SDK Agent/Runner via to_openai_agents_tools
│   ├── agent.py
│   ├── requirements.txt
│   └── README.md
├── claude_code_sandbox/      # Claude Code CLI, headless, via bash_tool + git_tools
│   ├── run_claude_code.py
│   └── README.md
├── claude_code_declarative_builder/  # Claude Code image via POST /v1/images instead of a Dockerfile
│   ├── build_claude_code_image.py
│   └── README.md
├── quant_research_declarative_builder/  # Quant-research image (vectorbt/backtrader/TA-Lib/QuantLib/quantstats)
│   ├── build_quant_research_image.py
│   ├── quant_smoke_test.py
│   └── README.md
├── foundry_audit_sandbox/    # Foundry/Anvil smart-contract-audit sandbox, network-dark by default
│   ├── Dockerfile
│   ├── docker-compose.override.yml
│   ├── contracts/
│   ├── scripts/deploy_and_exploit.sh
│   ├── run_audit_sandbox.py
│   └── README.md
├── gemini_function_calling/  # Native Gemini (google-genai) tool-calling, no agent framework
│   ├── agent.py
│   ├── requirements.txt
│   └── README.md
├── mistral_function_calling/ # Native Mistral tool-calling, no agent framework
│   ├── agent.py
│   ├── requirements.txt
│   └── README.md
├── groq_function_calling/    # Native Groq (OpenAI-compatible) tool-calling, no agent framework
│   ├── agent.py
│   ├── requirements.txt
│   └── README.md
├── raw_api/                 # Plain curl/requests against the sidecar, no framework
│   ├── curl_examples.sh
│   ├── requests_example.py
│   └── README.md
├── hosted_control_plane/    # Hosted multi-tenant API: signup -> sandbox -> exec -> teardown
│   ├── hosted_flow.py
│   └── README.md
└── stateful_interpreters/   # python_interpreter + node_interpreter statefulness, no framework
    ├── interpreters_demo.py
    ├── requirements.txt
    └── README.md
```

## What's actually verified here

Being blunt about this, because it matters more than looking finished:

- **`raw_api/`** was run end-to-end against a real local docker-compose
  sidecar in this environment (both `curl_examples.sh` and
  `requests_example.py`). Its `README.md` says so.
- **`hosted_control_plane/`** was also run end-to-end for real: a local
  control-plane instance (SQLite-backed) in front of the same local
  docker-compose sidecar, exercising the full signup -> API key -> create
  sandbox -> exec -> file create/view -> list -> teardown flow.
- **`langgraph_agent/`** and **`langchain_tool_calling/`**: the original 5
  tools in the sandbox tool layer they depend on (`create_sandbox_tools` /
  `create_bash_tool` / `create_file_create_tool` -- bash_tool, file_create,
  view, str_replace, present_files) were independently exercised against a
  live sandbox pod in this environment and confirmed working. The 3 newer
  tools (`ls`, `glob`, `grep`) are covered by unit tests with a mocked
  `SandboxManager` (see `tests/test_search_tools.py`) but have **not**
  themselves been exercised against a live pod as part of this cookbook --
  neither example currently calls them. The other piece **not** exercised
  is the LLM reasoning loop itself (`create_react_agent`/`create_agent`
  actually calling a model) -- no `ANTHROPIC_API_KEY`/other provider key
  was available in this environment. Imports and tool wiring were verified
  against the actual installed `boxkite`/`langgraph`/`langchain`/
  `langchain-anthropic` package versions (no import errors, no signature
  mismatches). If you have a key, treat your first run as the final
  verification step; please file an issue with the exact traceback if
  something doesn't work.

- **`openai_function_calling/`**, **`llamaindex_agent/`**, and
  **`openai_agents_sdk/`**: tool wiring (`to_openai_functions`/
  `to_llamaindex_tools`/`to_openai_agents_tools`, the generated schemas, and
  the underlying `bash_tool`/`file_create` handlers) was exercised against
  a fake sandbox manager in this environment and confirmed correct;
  `llamaindex_agent/`'s `ReActAgent` construction and `openai_agents_sdk/`'s
  `Agent` construction were also verified against the actual installed
  `llama-index-core`/`llama-index-llms-openai`/`openai-agents` package
  versions. None of the three examples' end-to-end runs against a live LLM
  were exercised (no API key available in this environment).
- **`claude_code_sandbox/`**: `deploy/sandbox-claude-code.Dockerfile` was
  built and run locally in this environment -- `claude --version` works as
  the non-root `sandbox` user post-build, and `npm`/`npx` are confirmed
  absent, same as `sandbox`/`sandbox-minimal`. The example script's tool
  wiring was verified against a fake sandbox manager. The end-to-end run
  against a real pod and a live Claude Code invocation was **not**
  exercised (no live sidecar + Anthropic API key available together in
  this environment).

- **`gemini_function_calling/`**, **`mistral_function_calling/`**, and
  **`groq_function_calling/`**: each provider's package (`google-genai`
  2.11.0, `mistralai` 2.6.0, `groq` 1.5.0) was installed into this repo's
  `.venv` and its real client/schema classes were inspected directly
  (`inspect.signature`/`model_fields`) before writing the agent, rather
  than assuming the shape from training-data memory -- Gemini's genuinely
  different `Content`/`Part`/`FunctionDeclaration` shape, Mistral's actual
  `mistralai.client.Mistral` import path, and Groq's confirmed
  OpenAI-compatible `chat.completions.create` are all verified against the
  installed packages, not guessed. Each `agent.py` passed a syntax check
  and a tool-wiring check (constructing the real ToolSpecs against a fake
  sandbox manager, building each provider's actual schema/tool objects, and
  instantiating each client with a dummy key) with no import errors or
  signature mismatches. No live LLM call was exercised (no API keys
  available in this environment).
- **`claude_code_declarative_builder/`**: verified for real, not just
  syntax-checked -- a local control-plane instance (SQLite,
  `BOXKITE_IMAGE_BUILDER_ENABLED=true`) was actually run, and the full
  signup -> API key -> `POST /v1/images` -> poll -> `status: "completed"`
  flow was exercised against it end to end via real HTTP calls (through
  the non-mocked `FakeImageBuildRunner` code path). The final step --
  creating a sandbox from the built image and running `claude --version`
  -- was also attempted against this environment's reachable Kubernetes
  API and failed with a real, expected error (a `ValidatingAdmissionPolicy`
  denying external pod creation), which is documented honestly in that
  example's own README rather than glossed over. The apt/npm package
  version pins used were confirmed against the live Wolfi package repo via
  `docker run`, not guessed.

- **`quant_research_declarative_builder/`**: the exact `POST /v1/images`
  request (`base="boxkite-default"` + the five pinned `python_packages`)
  was submitted through the real `SandboxImageBuildRequest`/`SandboxImageOut`
  schemas via the control-plane's FastAPI app in-process
  (`BOXKITE_IMAGE_BUILDER_ENABLED=true`) and reached `status: "completed"`
  through the real (non-mocked) `FakeImageBuildRunner` path. Separately,
  the smoke-test script this example writes into the sandbox
  (`quant_smoke_test.py`) was actually run end to end in a throwaway
  Python 3.11 virtualenv with the same five exact pinned versions
  installed -- this caught and fixed one real bug (`quantstats.stats.
  max_drawdown` on a non-`DatetimeIndex` return series) before being
  documented as working. Every package version pin was checked against the
  live PyPI JSON API and, for the two C-extension packages (`TA-Lib`,
  `QuantLib`), confirmed to publish `manylinux`/`musllinux` wheels for the
  base image's Python 3.11 (no compiler needed in the build layer). Not
  verified, for the same disclosed reason as
  `claude_code_declarative_builder/`'s final step: the real Kaniko build
  and creating a sandbox from the built image and running the smoke test
  *inside* an actual pod -- `RUNTIME_MODE=compose` doesn't even thread a
  custom `image_ref` through at all (see that example's own README for the
  specific mechanism), and `RUNTIME_MODE=k8s` needs a live-cluster-verified
  `KanikoJobBuildRunner` this repo doesn't have yet.

- **`stateful_interpreters/`**: tool wiring (`create_sandbox_tool_specs(enable_node_interpreter=True)`,
  both handlers' call signatures and return shapes) was exercised against
  fake sandbox managers in this environment and confirmed correct, and the
  underlying persistence/redeclaration/error behavior it demonstrates is
  covered by real tests elsewhere (`tests/test_sidecar_node_interpreter.py`,
  `tests/test_node_interpreter_tool.py`, `tests/test_python_interpreter_tool.py`)
  including a real local `node` binary. This script itself was **not** run
  against a live `boxkite up` docker-compose stack with
  `BOXKITE_NODE_INTERPRETER_ENABLED=true` set -- same scope boundary already
  disclosed in `docs/NODE-INTERPRETER-DESIGN.md`'s status header (tested
  against a real local `node` binary; not yet exercised inside an actual
  Kubernetes pod or the `deploy/sandbox.Dockerfile` image).

- **`foundry_audit_sandbox/`**: the image build and the entire Solidity/
  chain workflow were verified for real, end to end, against the exact
  `Dockerfile` and scripts checked in here -- not a fake manager, not a
  guess. `docker build` succeeded from this directory; the full flow (seed
  `/workspace` from the baked template, `forge build`, `forge test` proving
  the exploit drains `Vault.sol` but not `VaultFixed.sol`, then
  `scripts/deploy_and_exploit.sh` deploying to a live, deterministic local
  Anvil chain and actually draining it: reentrancy attacker walks away with
  11 ETH, vault balance 0) was run inside a container with
  `--read-only --cap-drop ALL --user 1001:1001 --network none` -- i.e. the
  same read-only-rootfs/non-root/capability-dropped/zero-egress posture
  `deploy/pod-template.yaml` gives a real sandbox pod, not a looser stand-in
  -- and it passed with genuinely zero network egress available (a `curl`
  to a live host from inside that same container failed with "Could not
  resolve host"). Foundry's install method (`curl -L
  https://foundry.paradigm.xyz | bash` then `foundryup --install v1.0.0`)
  was checked directly against getfoundry.sh's current docs before writing
  the Dockerfile, not assumed from training data -- including confirming
  Foundry publishes no apt/apk/npm/pip package at all. The
  `docker-compose.override.yml` merge (`docker compose -f
  deploy/docker-compose.yml -f
  examples/foundry_audit_sandbox/docker-compose.override.yml build
  sandbox`) was also run for real and produced a working image. **Not**
  verified: `run_audit_sandbox.py`'s use of a real `SandboxManager` against
  a live compose stack (its `bash_tool` wiring was checked against a fake
  `SandboxManager` only, same tier as `openai_function_calling/`) -- no
  full local boxkite stack (sidecar + Vault/MinIO) was stood up in this
  pass. See the example's own README for the full disclosure, including
  why K8s-mode reachability between a backgrounded Anvil process and later
  `bash_tool` calls needs an explicit, disclosed operator tradeoff that
  compose mode does not.

Each example's own README repeats its specific verification status so you
don't have to cross-reference this file.
