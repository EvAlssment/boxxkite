# Guides

Guides answer “how do I do this?” and point to runnable material where one is
available.

## Run locally

- [Local Docker Compose quickstart](../getting-started/) — install, start a
  sandbox, and make a first tool call.
- [Raw sidecar HTTP API](raw-api.md) — use `curl` or Python without the
  `boxxkite` package or an agent framework. The companion code is in
  [`examples/basic/raw_api/`](../../examples/basic/raw_api/).
- [Stateful interpreters](../../examples/basic/stateful_interpreters/) — keep Python
  or Node interpreter state between calls.

## Run on Kubernetes or behind an API

- [Local kind cluster](local-kind.md) — build the images, create a kind
  cluster, apply the runtime resources, and verify warm pods.
- [Hosted control-plane](hosted-control-plane.md) — run the account, API-key,
  sandbox, exec, file, list, and teardown flow. The runnable walkthrough is
  [`examples/basic/hosted_control_plane/`](../../examples/basic/hosted_control_plane/).
- [Self-hosted multi-tenancy](self-hosted-multi-tenancy.md) — understand what
  is scoped per account today and the supported deployment-per-tenant model.

## Integrate with agent tools

- [Handoff adapters](handoff-adapters.md) — move a resumable local coding-agent
  session into a fresh sandbox using its portable credential.
- [Framework and provider examples](../../examples/) — LangChain, LangGraph,
  LlamaIndex, OpenAI Agents, Google ADK, and native function-calling examples.
- [Declarative image builders](declarative-image-builders.md) — request a
  Claude-Code-capable or quant-research image through the control-plane image
  builder; read each example's verification limits before relying on it.
- [Claude Code sandbox](claude-code-sandbox.md) — build and run the dedicated
  image, then invoke Claude Code headlessly through `bash_tool`.

## Operations and security

- [Architecture and isolation](../architecture/) explains the default pod
  boundary, network controls, state reset, and experimental runtime options.
- [Security policy](../../SECURITY.md) is the source of truth for reporting
  and the complete known limitation list.
