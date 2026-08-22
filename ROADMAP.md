# Roadmap

What's planned, what's only proposed, and what isn't being worked on. The
point of this file is that you can tell those apart before you spend time
building something we're already doing, or open an issue for something we've
already decided against.

**Nothing here has a committed date.** boxxkite is a small, indie-run project
(see [CONTRIBUTING.md](CONTRIBUTING.md)); "Proposed" means there's a written-up
issue and we think it's the right direction, not that anyone has started or
promised it. Treat the linked issue as the source of truth — this table is an
index, and an index can go stale between reviews.

**Already shipped** lives in [CHANGELOG.md](CHANGELOG.md), not here. Completed
items get removed from this file rather than accumulating, so a long roadmap
means a lot of open work, not a lot of finished work.

## Status vocabulary

| Status | Means |
| --- | --- |
| **In review** | An open pull request exists. Linked below. |
| **Proposed** | An issue is written up and we agree with the direction. Nobody is necessarily working on it. |
| **Not planned** | A real gap we've decided not to commit to. Explained in the last section, so you don't have to guess. |

Anything not listed here is simply un-triaged — the [open issues](https://github.com/EvAlssment/boxxkite/issues)
are broader than this file.

## Isolation & runtime

The isolation model is the product, so this section moves the most carefully.

| Item | Status | Tracking |
| --- | --- | --- |
| Cluster preflight checker (`boxxkite doctor`) — verify a cluster can actually enforce NetworkPolicy/PSA/seccomp before install | In review | [#119](https://github.com/EvAlssment/boxxkite/issues/119) |
| Content-hash pinning / cosign-verify admission gate for custom sandbox images | Proposed | [#92](https://github.com/EvAlssment/boxxkite/issues/92) |
| Signed, attested GHCR releases (finish the cosign/SBOM pipeline) | Proposed | [#76](https://github.com/EvAlssment/boxxkite/issues/76), [#117](https://github.com/EvAlssment/boxxkite/issues/117) |
| Snapshot immutability enforced at the storage layer, not just app-level | Proposed | [#28](https://github.com/EvAlssment/boxxkite/issues/28) |
| Multi-arch (arm64) image builds | Proposed | [#50](https://github.com/EvAlssment/boxxkite/issues/50) |
| Explicit multi-tenancy isolation boundary for self-hosted installs | Proposed | [#102](https://github.com/EvAlssment/boxxkite/issues/102) |

## Agent-facing tool surface

Tools an agent calls inside a session. New tools land opt-in by default so an
agent that doesn't need one doesn't pay for it in context.

| Item | Status | Tracking |
| --- | --- | --- |
| `scratch_memory` — session-scoped bookkeeping that isn't the workspace filesystem | In review | [#74](https://github.com/EvAlssment/boxxkite/issues/74) |
| `explain_last_failure` — last failed command, full stderr, and the files it touched, in one call | Proposed | [#77](https://github.com/EvAlssment/boxxkite/issues/77) |
| `workspace_diff` — structured file-tree/content delta since the last checkpoint | Proposed | [#71](https://github.com/EvAlssment/boxxkite/issues/71) |
| `process_tree` — running processes correlated to what spawned them | Proposed | [#73](https://github.com/EvAlssment/boxxkite/issues/73) |
| `semantic_search` over an embeddings index of the sandbox filesystem | Proposed | [#72](https://github.com/EvAlssment/boxxkite/issues/72) |
| Streaming output for exec and code execution instead of synchronous request/response | Proposed | [#67](https://github.com/EvAlssment/boxxkite/issues/67) |
| Persistent, variable-preserving execution context (stateful kernel) | Proposed | [#70](https://github.com/EvAlssment/boxxkite/issues/70) |
| That context over the real Jupyter kernel wire protocol | Proposed | [#69](https://github.com/EvAlssment/boxxkite/issues/69) |
| DOM/accessibility-tree grounded action API for GUI agents | Proposed | [#45](https://github.com/EvAlssment/boxxkite/issues/45) |

## SDKs & adapters

Four SDKs today (Python/JS/Go/Rust), hand-written against the REST API with no
codegen — which is exactly why parity enforcement is on this list.

| Item | Status | Tracking |
| --- | --- | --- |
| Cross-SDK parity/consistency CI check | Proposed | [#99](https://github.com/EvAlssment/boxxkite/issues/99) |
| Actionable error taxonomy standardized across SDKs | Proposed | [#94](https://github.com/EvAlssment/boxxkite/issues/94) |
| Java SDK | Proposed | [#57](https://github.com/EvAlssment/boxxkite/issues/57), [#81](https://github.com/EvAlssment/boxxkite/issues/81) |
| Kotlin SDK (coroutine wrapper over Java) | Proposed | [#58](https://github.com/EvAlssment/boxxkite/issues/58) |
| C#/.NET SDK | Proposed | [#59](https://github.com/EvAlssment/boxxkite/issues/59), [#82](https://github.com/EvAlssment/boxxkite/issues/82) |
| Maven Central + NuGet publishing for the above | Proposed | [#64](https://github.com/EvAlssment/boxxkite/issues/64) |
| LangGraph / Vercel AI SDK / Google ADK adapters | Proposed | [#66](https://github.com/EvAlssment/boxxkite/issues/66), [#84](https://github.com/EvAlssment/boxxkite/issues/84) |
| Snapshot create/restore exposed in the SDKs and CLI | Proposed | [#25](https://github.com/EvAlssment/boxxkite/issues/25) |
| Dedicated code-interpreter SDK package | Proposed | [#63](https://github.com/EvAlssment/boxxkite/issues/63), [#83](https://github.com/EvAlssment/boxxkite/issues/83) |
| `create-boxxkite-app` scaffolding command | Proposed | [#93](https://github.com/EvAlssment/boxxkite/issues/93) |

## Evals, RL & telemetry

The largest cluster of proposals, and the least settled — most of it is
downstream of decisions not made yet, so read these as a direction rather than
a plan.

| Item | Status | Tracking |
| --- | --- | --- |
| `boxxkite-eval` native evaluation harness SDK | Proposed | [#30](https://github.com/EvAlssment/boxxkite/issues/30) |
| Gymnasium-compatible RL environment interface | Proposed | [#31](https://github.com/EvAlssment/boxxkite/issues/31) |
| Snapshot-based fast episode reset instead of pod recreation | Proposed | [#38](https://github.com/EvAlssment/boxxkite/issues/38) |
| Parallel rollout orchestration for fan-out trials | Proposed | [#37](https://github.com/EvAlssment/boxxkite/issues/37) |
| Structured tool-call telemetry (JSONL traces) | Proposed | [#33](https://github.com/EvAlssment/boxxkite/issues/33) |
| Per-tool-call token/cost/latency metrics | Proposed | [#49](https://github.com/EvAlssment/boxxkite/issues/49) |
| Session-handoff recordings as reproducible eval seeds | Proposed | [#48](https://github.com/EvAlssment/boxxkite/issues/48) |
| Standard task spec + SWE-bench/WebArena/OSWorld/GAIA import adapters | Proposed | [#40](https://github.com/EvAlssment/boxxkite/issues/40) |
| LLM-as-judge grading for open-ended tasks | Proposed | [#39](https://github.com/EvAlssment/boxxkite/issues/39) |
| Session replay viewer over the existing audit log | Proposed | [#35](https://github.com/EvAlssment/boxxkite/issues/35) |

## Self-hosting & operations

| Item | Status | Tracking |
| --- | --- | --- |
| Production-hardened docker-compose profile | Proposed | [#115](https://github.com/EvAlssment/boxxkite/issues/115) |
| Helm values JSON schema + CI lint/dry-run gate | Proposed | [#114](https://github.com/EvAlssment/boxxkite/issues/114) |
| Helm upgrade compatibility matrix / breaking-change checker | Proposed | [#116](https://github.com/EvAlssment/boxxkite/issues/116) |
| In-place upgrade/migration CLI | Proposed | [#110](https://github.com/EvAlssment/boxxkite/issues/110) |
| Backup/restore CLI for self-hosted Postgres and volumes | Proposed | [#118](https://github.com/EvAlssment/boxxkite/issues/118) |
| Kustomize/GitOps overlays (ArgoCD, Flux) | Proposed | [#52](https://github.com/EvAlssment/boxxkite/issues/52) |
| Terraform module for one-command cloud provisioning | Proposed | [#103](https://github.com/EvAlssment/boxxkite/issues/103) |
| Air-gapped bundle generator + offline registry mirror | Proposed | [#112](https://github.com/EvAlssment/boxxkite/issues/112) |
| Self-hosted admin UI for cluster/fleet health | Proposed | [#111](https://github.com/EvAlssment/boxxkite/issues/111) |
| Resource-sizing presets and a cost-estimation guide | Proposed | [#54](https://github.com/EvAlssment/boxxkite/issues/54) |

## Fleet & scale

| Item | Status | Tracking |
| --- | --- | --- |
| Fleet-level declarative capacity config | Proposed | [#55](https://github.com/EvAlssment/boxxkite/issues/55) |
| Bin-packing / pool-utilization reporting for stranded warm capacity | Proposed | [#56](https://github.com/EvAlssment/boxxkite/issues/56) |
| Fleet-status admin/CLI view | Proposed | [#78](https://github.com/EvAlssment/boxxkite/issues/78) |
| Helm support for registering additional clusters | Proposed | [#68](https://github.com/EvAlssment/boxxkite/issues/68) |

## Docs, distribution & process

| Item | Status | Tracking |
| --- | --- | --- |
| Enhancement-proposal (BEP) process for major changes | Proposed | [#79](https://github.com/EvAlssment/boxxkite/issues/79) |
| Docs-site restructure (getting-started / guides / reference / architecture) | Proposed | [#100](https://github.com/EvAlssment/boxxkite/issues/100) |
| `examples/` reorganized into a discoverable taxonomy | Proposed | [#101](https://github.com/EvAlssment/boxxkite/issues/101) |
| "Try boxxkite in 60 seconds" single-command trial | Proposed | [#113](https://github.com/EvAlssment/boxxkite/issues/113) |
| `troubleshoot-sandbox` Skill for Claude Code / Cursor | Proposed | [#89](https://github.com/EvAlssment/boxxkite/issues/89) |
| One-click deploys beyond the existing Render button (Fly.io, Railway) | Proposed | [#104](https://github.com/EvAlssment/boxxkite/issues/104), [#105](https://github.com/EvAlssment/boxxkite/issues/105) |
| Cloud marketplace listings (AWS, GCP, Azure, DigitalOcean) | Proposed | [#106](https://github.com/EvAlssment/boxxkite/issues/106)–[#109](https://github.com/EvAlssment/boxxkite/issues/109) |

## Not currently planned

Real gaps. Listed so you don't have to open an issue to find out we know.

- **VM-level isolation: gVisor and Firecracker.** boxxkite's boundary today is
  a Kubernetes pod: non-root, all capabilities dropped, read-only root
  filesystem, seccomp `RuntimeDefault`, network egress denied by default. That
  is a container boundary sharing the node's kernel, not a hypervisor one.
  Neither gVisor nor Firecracker is planned. If a shared kernel is
  unacceptable for your threat model, boxxkite is the wrong layer today. See
  [SECURITY.md](SECURITY.md) for what is and isn't in scope for a report.

  **Kata Containers is the partial exception, and it is experimental.** It
  ships today behind `BOXXKITE_KATA_RUNTIME_CLASS_ENABLED` (off by default),
  but it is implemented against the Kubernetes `runtimeClassName` API shape and
  has never been exercised against a live Kata-enabled cluster. One concrete
  risk is still unverified: Kata's documented block-backed `emptyDir` modes do
  not honor `emptyDir.sizeLimit`, so if the default backend behaves the same
  way, the per-sandbox storage caps silently stop applying the moment the flag
  is on. Enabling it is an opt-in onto an experimental configuration, not a
  supported one. See the comment on the flag in
  [`src/boxxkite/resource_config.py`](src/boxxkite/resource_config.py).
- **Multi-region scheduling.** A single control-plane can address multiple
  clusters, but there's no region-aware placement, no cross-region failover,
  and no data-residency routing.
- **A stable v1 API guarantee.** Pre-1.0, the REST API and SDK surfaces can
  change between minor versions. The [CHANGELOG](CHANGELOG.md) records what
  changed; there is no deprecation-window promise yet, and claiming one we
  don't enforce would be worse than saying this.
- **Windows sandbox containers.** Linux only, and there's no work planned to
  change that.

If one of these blocks you, say so on an issue or in
[Discord](https://discord.gg/JntfAx7cg5) — "not planned" is a statement about
current priorities, not a permanent decision, and a concrete use case is what
would move it.

## Keeping this honest

Reviewed roughly quarterly, and whenever a linked issue closes. If you find a
row here that contradicts the code, that's a bug in this file — please open an
issue, the same as you would for anything else that's wrong.
