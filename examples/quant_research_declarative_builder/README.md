# Declarative builder: a quant-research image (vectorbt, backtrader, TA-Lib, QuantLib, quantstats)

Builds on [GitHub issue #135](https://github.com/EvAlssment/boxkite/issues/135):
boxkite's default sandbox image already ships `pandas`/`numpy`/`polars`/
`scikit-learn` (see the blog post this follows on from,
[`site/app/blog/self-hosted-quant-research-agent-for-banks/page.tsx`](../../site/app/blog/self-hosted-quant-research-agent-for-banks/page.tsx)),
but a real quant research desk leans on more specialized libraries the
default image doesn't carry:

- **`vectorbt`** — vectorized, NumPy/Numba-accelerated backtesting; fast
  iteration over many parameter combinations at once.
- **`backtrader`** — event-driven backtesting with broker-realistic order
  fills; a genuinely different execution model than vectorbt's vectorized
  one (see "Why both vectorbt and backtrader" below).
- **`TA-Lib`** — the standard C-based technical-indicator library (moving
  averages, oscillators, pattern recognition).
- **`QuantLib`** — derivatives and fixed-income pricing (bonds, curves,
  swaps).
- **`quantstats`** — portfolio tear sheets and risk/return statistics
  (Sharpe, drawdown, etc.) from a strategy's return stream.

None of these need a new base image or a hand-maintained Dockerfile: they're
exact-version-pinned `python_packages` layered on top of `"boxkite-default"`
through the existing declarative builder (`POST /v1/images`, see
[`docs/DECLARATIVE-BUILDER-DESIGN.md`](../../docs/DECLARATIVE-BUILDER-DESIGN.md)),
the same mechanism [`../claude_code_declarative_builder`](../claude_code_declarative_builder)
uses for a Claude-Code-capable image.

## Why both vectorbt and backtrader

They're not redundant — they answer different questions:

- **vectorbt** computes an entire universe of entries/exits as vectorized
  NumPy operations across the whole price series at once. It's fast (good
  for scanning many parameter combinations) but works at the level of
  "given these signal arrays, what's the resulting equity curve," not a
  simulated order-by-order fill.
- **backtrader** runs an event loop, bar by bar, through a `Strategy`
  object with real broker/position/order semantics (`self.buy()`,
  `self.position`, commission, slippage). Slower, but the fills and
  bookkeping are closer to what a live/paper-trading engine would actually
  do.

A desk iterating on a strategy idea often wants the fast vectorized pass
first (vectorbt, sweep parameters, keep loaded data in memory across
calls — see "the `python_interpreter` differentiator" below) and the
event-driven pass second, as a realism check before it goes anywhere near
production. This example runs the *same* simple SMA-crossover strategy
through both, on the same synthetic price series, to make that contrast
concrete rather than asserted.

## What the request looks like

```jsonc
// POST /v1/images
{
  "label": "quant-research",
  "base": "boxkite-default",
  "python_packages": [
    "vectorbt==0.28.5",
    "backtrader==1.9.78.123",
    "TA-Lib==0.7.0",
    "QuantLib==1.42.1",
    "quantstats==0.0.81"
  ]
}
```

`base` is `"boxkite-default"`, not `"boxkite-minimal"` — a quant workload
wants the existing pandas/numpy/scikit-learn stack the default image
already ships (see the blog post) *plus* these five, not a leaner base with
that stack re-declared. Every entry must satisfy `schemas.py`'s
`_PINNED_PACKAGE_RE` (exact `name==version`, no ranges, no `latest`) — see
"How these pins were confirmed real" below for how each was checked.

## Prerequisites

- A control-plane instance (self-deployed; there is no public hosted
  boxkite service — see the main README) with
  **`BOXKITE_IMAGE_BUILDER_ENABLED=true`**. This route family 404s on every
  deployment that hasn't explicitly opted in.
- `pip install httpx`

## Running it

```bash
export CONTROL_PLANE_URL=http://localhost:8090
python build_quant_research_image.py
```

The script (`build_quant_research_image.py`):

1. `POST /v1/auth/signup` + `POST /v1/api-keys` — same pattern as
   [`../hosted_control_plane/hosted_flow.py`](../hosted_control_plane/hosted_flow.py).
2. `POST /v1/images` with `base="boxkite-default"` and the five pinned
   `python_packages` above. Always async — returns `202` with
   `status="queued"` immediately.
3. Polls `GET /v1/images/{id}` until `status` reaches a terminal value
   (`completed`, `failed`, or `rejected`).
4. `POST /v1/sandboxes` with `image_id` set to the built image's id.
5. `POST /v1/sandboxes/{id}/files` writes `quant_smoke_test.py` (this
   directory's other file) into the sandbox, then
   `POST /v1/sandboxes/{id}/exec` runs it — importing all five packages,
   running a synthetic-data SMA-crossover backtest through both vectorbt
   and backtrader, pricing a bond with QuantLib, and computing Sharpe/
   drawdown with quantstats. No network egress needed: the price series is
   generated in-process with `numpy`, matching the sandbox's default-deny
   network posture the blog post describes.
6. Tears the session down.

## The `python_interpreter` differentiator (issue #135's framing)

This example's `exec` step runs the smoke-test script as one subprocess —
simplest to demonstrate over the *hosted control-plane's* HTTP API, which
only exposes one-shot `/exec`, not the kept-alive
`python_interpreter`/`node_interpreter` tools (those are wired at the
agent-framework layer via `boxkite.tools.create_sandbox_tool_specs`, see
[`../stateful_interpreters`](../stateful_interpreters)). The real payoff
issue #135 calls out — "load once, tweak, rerun" iteration without
re-loading a dataset every call — is `python_interpreter`'s statefulness,
demonstrated on its own in `../stateful_interpreters/interpreters_demo.py`.
This image build is what makes that loop useful for *this* vertical
specifically: once a researcher's agent has vectorbt/backtrader/TA-Lib/
QuantLib/quantstats available in a kept-alive interpreter, they can load a
dataset once and then vary the strategy, re-run, inspect, adjust — all
inside one session — instead of re-importing and re-loading on every call
the way a fresh-subprocess tool (`bash_tool`, or this example's one-shot
`/exec`) would force.

## How these pins were confirmed real

Every version below was checked directly against the live PyPI JSON API
(`https://pypi.org/pypi/<name>/<version>/json`) — real releases, not
guessed:

| Package | Pin | Confirmed |
|---|---|---|
| `vectorbt` | `0.28.5` | Real PyPI release (2026-03-26); `requires_python>=3.10`, `pandas<3.0,>=2.0` — compatible with the base image's Python 3.11. (A newer `1.1.0` also exists on PyPI — a July-2026 rewrite requiring `pandas>=3.0.3` / `numpy>=2.4.6` — but `0.28.5` was picked deliberately for broader dependency compatibility; see the code comment in `build_quant_research_image.py`.) |
| `backtrader` | `1.9.78.123` | Real, latest PyPI release; pure-Python wheel, no version-specific Python constraint. |
| `TA-Lib` | `0.7.0` | Real, latest PyPI release; ships `manylinux`/`musllinux` wheels for `cp311` — installs from a prebuilt wheel, no C compiler needed inside the build layer (confirmed against the actual PyPI file list, not assumed). |
| `QuantLib` | `1.42.1` | Real, latest PyPI release. Note: the issue's suggested `QuantLib-Python` is a **legacy meta-package** (last released `1.18`, described on PyPI as a "backward-compatible meta-package for the QuantLib module") — the current, actively-released project is plain `QuantLib`. Ships `cp38-abi3` `manylinux`/`musllinux` wheels, so `cp311` resolves against it too. |
| `quantstats` | `0.0.81` | Real, latest PyPI release; `requires_python>=3.10`. |

**Why prebuilt wheels matter here specifically:** `image_builder.py`'s
`render_dockerfile` installs `python_packages` with a plain
`python -m pip install --break-system-packages --no-cache-dir ...` — it
does **not** install a C compiler/build toolchain into the build layer
first (same as `deploy/sandbox.Dockerfile`'s own base). `TA-Lib` and
`QuantLib` are C/C++-backed packages; if a pin only had an sdist for the
base image's platform, the build would fail with a compiler error, not
silently succeed. Both were confirmed to publish `manylinux`/`musllinux`
wheels for `cp311` (the base image's Python version — see
`deploy/sandbox.Dockerfile`'s `PYTHON_VERSION=3.11`) before being pinned
here. The base image (`cgr.dev/chainguard/wolfi-base`) is **glibc**-based,
not musl, so the `manylinux` wheel variant is what actually resolves.

## What was verified for real, and what wasn't

**Verified for real in this environment:**

- The exact request above (`base="boxkite-default"` + these five pins) was
  submitted through the real `SandboxImageBuildRequest`/`SandboxImageOut`
  Pydantic schemas via the control-plane's FastAPI app (in-process, real
  HTTP request/response cycle through `httpx`'s ASGI transport — the same
  harness `control-plane/tests/` uses), with `BOXKITE_IMAGE_BUILDER_ENABLED`
  turned on. The build was accepted as `queued` and polling
  `GET /v1/images/{id}` showed it reach `status: "completed"` with a real
  (`FakeImageBuildRunner`-fabricated) digest and `registry_ref`, e.g.:

  ```
  digest=sha256:d410abb95e9381f630a00e7d7726b58625f52eef88c0c1e8948a212725cdef58
  registry_ref=registry.internal/boxkite-images/<account_id>/<image_id>@sha256:d410a...
  scan_result={'critical': 0, 'high': 0, 'policy': 'trivy-equivalent'}
  ```

  This confirms the exact package list and request shape in this README
  and script are accepted by the real schema, not a guess.

- **`quant_smoke_test.py` itself was actually run**, end to end, in a
  throwaway Python 3.11 virtualenv with these exact five pinned versions
  `pip install`-ed (not a different/nearby version, not simulated) — this
  is the file this example writes into the sandbox and executes there.
  Running it for real caught and fixed one genuine bug before this was
  documented as working: `quantstats.stats.max_drawdown` raised a
  `TypeError` (`unsupported operand type(s) for -: 'int' and 'Timedelta'`)
  when given a return series with a plain integer index instead of a
  `DatetimeIndex` — fixed by giving the synthetic price series a real
  `pd.date_range` index. Real output from that run:

  ```
  vectorbt 0.28.5
  backtrader 1.9.78.123
  QuantLib 1.42.1
  quantstats 0.0.81
  vectorbt total return: -0.1016
  quantstats Sharpe: -0.5172
  quantstats max drawdown: -0.1469
  QuantLib bond NPV: 103.8701
  backtrader ending cash: 99991.41
  OK: all five quant-research packages installed via the declarative builder, imported, and ran.
  ```

**Not verified — could not be exercised in this environment, for the same
disclosed reason [`../claude_code_declarative_builder`](../claude_code_declarative_builder)'s
README already documents:**

- The actual container **build** (Kaniko running `pip install` against
  these five packages inside a real build Job, then a real vulnerability
  scan) — `FakeImageBuildRunner` never builds anything; it only fabricates
  a digest. `KanikoJobBuildRunner.run_build` has not been exercised
  against a live/kind/k3d Kubernetes cluster (see
  `docs/DECLARATIVE-BUILDER-DESIGN.md`'s own status note).
- Creating a sandbox session from the built `image_id` and running
  `quant_smoke_test.py` **inside an actual boxkite sandbox pod**. Two
  independent reasons this step specifically can't be exercised in a local
  dev setup, not just "wasn't tried":
  - In `RUNTIME_MODE=compose` (the fast local-dev path), `image_ref` is a
    no-op — `src/boxkite/manager.py`'s compose branch always execs into
    the one already-running, statically-built `sandbox` container; it
    never reads the caller-supplied `image_ref` at all (that parameter is
    only consulted in `_create_pod`, the `RUNTIME_MODE=k8s` path). Running
    this script against a local `boxkite up` stack would create the
    sandbox session fine, but silently execute
    `quant_smoke_test.py` against the *default* image (no vectorbt/
    backtrader/TA-Lib/QuantLib/quantstats installed) rather than the
    custom one — a real gap in local-only verification, not a bug in this
    example's request shape.
  - In `RUNTIME_MODE=k8s`, `image_ref` is threaded into the pod spec for
    real, but requires the Kaniko build to have actually run and pushed a
    real, pullable digest first (see the previous bullet) — the fabricated
    `FakeImageBuildRunner` digest was never pushed anywhere, so a real
    `kubelet` image pull would fail regardless of pod-creation
    permissions.

  Exercising this step for real requires a Kubernetes cluster with
  `KanikoJobBuildRunner` finished and live-cluster-verified, a real
  container registry, and in-cluster credentials to create pods — none of
  which exist in this development environment, same gap
  `../claude_code_declarative_builder`'s README already discloses for its
  own final step.

If you run this against a real deployment with the K8s build path
finished, please file an issue with the exact output (or failure) so this
README's verification status can be updated with a real end-to-end result
instead of this documented gap.
