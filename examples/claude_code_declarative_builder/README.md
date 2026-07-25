# Declarative builder: a Claude-Code-ready image without a Dockerfile

Shows the **hosted-control-plane, runtime-composed** alternative to
`deploy/sandbox-claude-code.Dockerfile`: instead of a maintainer
hand-writing and rebuilding a Dockerfile every time the Claude Code version
bumps, a caller of the hosted control-plane API can compose an equivalent
image at request time via `POST /v1/images` (the "declarative builder",
`docs/DECLARATIVE-BUILDER-DESIGN.md`).

This is **not** a replacement for `deploy/sandbox-claude-code.Dockerfile` in
general — that Dockerfile still exists and is still what a self-hoster
running `SandboxManager` directly (no control-plane) uses. This example is
for the other deployment shape: someone calling a *hosted* control-plane
instance who has no access to rebuild base images themselves, and wants a
Claude-Code-capable sandbox anyway.

## Why this is possible now, and wasn't before

`deploy/sandbox-claude-code.Dockerfile`'s own header comment explains why it
had to be hand-maintained out-of-band: `SandboxImageBuildRequest` originally
only had `python_packages`/`apt_packages` — no `npm_packages` field — so an
npm-global-install use case (installing `@anthropic-ai/claude-code`) could
not be self-served through `POST /v1/images`. `npm_packages` was added to
`SandboxImageBuildRequest` after `docs/E2B-COMPARISON.md` flagged that gap
(see `control-plane/src/control_plane/schemas.py`'s
`_NPM_PINNED_PACKAGE_RE` and `image_builder.py`'s `render_dockerfile`). That
closes the gap for this specific use case — this example is that closed
gap, exercised concretely.

## What the request looks like

```jsonc
// POST /v1/images
{
  "label": "claude-code-declarative",
  "base": "boxkite-minimal",
  "apt_packages": ["git==2.54.0-r0", "openssh-client==10.0_p1-r2"],
  "npm_packages": ["@anthropic-ai/claude-code==2.0.1"]
}
```

This is the declarative equivalent of
`deploy/sandbox-claude-code.Dockerfile`'s content: `boxkite-minimal` is the
same lean python+node base `deploy/sandbox-minimal.Dockerfile` builds (no
preinstalled data-science/document/browser stack), `git` +
`openssh-client` are the two apt/apk packages that Dockerfile adds on top of
that base, and `@anthropic-ai/claude-code==2.0.1` is the exact Claude Code
version already verified working in that Dockerfile, installed the same way
(global npm install, npm then stripped from the final image —
`image_builder.py`'s `render_dockerfile` does this in one `RUN` layer, same
security posture: no package manager survives into the runtime image).

Every package spec must satisfy `schemas.py`'s exact-version-pin regexes
(`_PINNED_PACKAGE_RE` for apt, `_NPM_PINNED_PACKAGE_RE` for npm, which
additionally allows a leading `@scope/` segment) — no ranges, no `latest`.

## Prerequisites

- A control-plane instance (self-deployed; there is no public hosted
  boxkite service — see the main README) with
  **`BOXKITE_IMAGE_BUILDER_ENABLED=true`**. This route family 404s on every
  deployment that hasn't explicitly opted in.
- `pip install httpx`

## Running it

```bash
export CONTROL_PLANE_URL=http://localhost:8090
python build_claude_code_image.py
```

The script (`build_claude_code_image.py`):

1. `POST /v1/auth/signup` + `POST /v1/api-keys` — same pattern as
   `../hosted_control_plane/hosted_flow.py`.
2. `POST /v1/images` with the base + pinned `apt_packages`/`npm_packages`
   above. Always async — returns `202` with `status="queued"` immediately.
3. Polls `GET /v1/images/{id}` until `status` reaches a terminal value
   (`completed`, `failed`, or `rejected`).
4. `POST /v1/sandboxes` with `image_id` set to the built image's id.
5. `POST /v1/sandboxes/{id}/exec` running `claude --version` to prove the
   resulting sandbox actually has Claude Code installed.
6. Tears the session down.

## Read this before treating the build step as production-ready

`docs/DECLARATIVE-BUILDER-DESIGN.md`'s own status note is explicit about
what's implemented and what isn't, and this example does not oversell past
that:

- The declarative builder is **off by default**
  (`BOXKITE_IMAGE_BUILDER_ENABLED=false`) and, per that doc, **has not had a
  dedicated security review** for untrusted multi-tenant traffic yet. Don't
  point this at a real, internet-facing, multi-tenant deployment without
  doing that review first.
- The real build executor for `RUNTIME_MODE=k8s`
  (`KanikoJobBuildRunner.run_build` in
  `control-plane/src/control_plane/image_builder.py`) is **not implemented**
  — it raises `NotImplementedError` if a `RUNTIME_MODE=k8s` deployment
  actually tries to build. Only its Kubernetes `Job` spec construction
  (`build_job_spec`) is unit-tested; there is no live-cluster build path in
  this codebase yet.
- Every other `RUNTIME_MODE` (local dev, `compose`, and the test suite)
  gets `FakeImageBuildRunner` instead — a deterministic in-process stand-in
  that fabricates a `sha256` digest and runs the real scan-gate policy
  logic, without ever invoking a container build. That's what actually ran
  in the verification below.

## What was verified for real, and what wasn't

**Verified for real, against a locally-running control-plane instance in
this environment** (SQLite backend, `BOXKITE_IMAGE_BUILDER_ENABLED=true`,
`FakeImageBuildRunner`, no mocking of the HTTP layer):

- `POST /v1/auth/signup` → `POST /v1/api-keys` → `POST /v1/images` all
  succeeded with real HTTP responses matching the real Pydantic schemas
  (`SandboxImageBuildAccepted`, then `SandboxImageOut` on poll).
- The build request was accepted as `queued`, and polling
  `GET /v1/images/{id}` showed it transition all the way to
  `status: "completed"` with a real (fake-runner-generated) `digest` and
  `registry_ref`, e.g.:

  ```
  Image built: digest=sha256:cac4d4f99d3c27a7257453715697dba93108b72fc90aad134226f5a9a9d82a01
  registry_ref=registry.internal/boxkite-images/<account_id>/<image_id>@sha256:cac4d...
  ```

- This confirms the request/response shapes in this README and script are
  exactly what the real control-plane accepts and returns today — not a
  guess at the schema.

**Not verified — could not be exercised in this environment, and the
script's final steps will fail if run as-is against anything but a real
Kubernetes cluster with a finished `KanikoJobBuildRunner`:**

- The actual container **build** (Kaniko running, package installation,
  vulnerability scan against a real image) — `FakeImageBuildRunner` never
  builds anything; it only fabricates a digest.
- Creating a sandbox session from that `image_id` and running
  `claude --version` inside it. Run end-to-end here (same environment,
  reachable Kubernetes API), `POST /v1/sandboxes` reached real
  `create_namespaced_pod` call and failed with a real, informative error —
  not a hang or a silent skip:

  ```
  kubernetes_asyncio.client.exceptions.ApiException: (422)
  ...pods "sandbox-<id>" is forbidden: ValidatingAdmissionPolicy
  'sandbox-pod-creator-restriction' ... denied request: Sandbox pods can
  only be created by in-cluster service accounts. Local/external pod
  creation is not allowed.
  ```

  This is expected and correct: this environment's cluster (reasonably)
  refuses pod creation from outside the cluster, and even if it hadn't, the
  fake digest from `FakeImageBuildRunner` was never actually pushed to any
  registry, so a real `kubelet` image pull would fail regardless. Neither
  failure mode is specific to this example's code — they're both downstream
  of `KanikoJobBuildRunner.run_build` not being implemented yet. Exercising
  this step for real requires a Kubernetes cluster with that build runner
  finished, a real container registry, and in-cluster credentials to create
  pods — none of which exist in this development environment.

## Pinned package versions used, and how they were confirmed real

`git` and `openssh-client` versions were confirmed installable against the
live Wolfi package repo `boxkite-minimal`'s base
(`cgr.dev/chainguard/wolfi-base:latest`) is built from, by actually running:

```bash
docker run --rm cgr.dev/chainguard/wolfi-base:latest sh -c \
  "apk update && apk add --no-cache git=2.54.0-r0 openssh-client=10.0_p1-r2 && git --version && ssh -V"
```

which succeeded and printed `git version 2.54.0` /
`OpenSSH_10.0p2, OpenSSL 3.6.3`. Both specs
(`git==2.54.0-r0`, `openssh-client==10.0_p1-r2`) were also checked directly
against `schemas.py`'s `_PINNED_PACKAGE_RE` in Python and matched.

`@anthropic-ai/claude-code==2.0.1` is the same version already pinned and
verified working in `deploy/sandbox-claude-code.Dockerfile`'s own
`ARG CLAUDE_CODE_VERSION=2.0.1`. Checked against `_NPM_PINNED_PACKAGE_RE`
and matched (the regex's `@scope/name==version` form is exactly what
allows this scoped package through).

**Caveat:** Wolfi is a rolling-release distribution — old package builds
get pruned from the repo over time. A pin that resolves today is not
guaranteed to still resolve months from now; re-verify with the same
`docker run ... apk add --no-cache <name>=<version>` command above before
relying on these exact pins in a real build.
