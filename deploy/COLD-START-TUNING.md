# Cold-start tuning

The dominant cost of a *cold* sandbox start is pulling the sandbox container
image, not scheduling or sidecar boot. This guide documents the levers for it.
Addresses issue #233 (follow-up to the #178 cold-start profiling).

## The measurement

`#178`'s profiling on a real fresh GKE node, corroborated by
`docs/BENCHMARKS.md`:

| Scenario | time-to-usable |
|---|---|
| Cold start on a **fresh node** (full image pull) | **~74.7 s** |
| — pulling `boxkite-sandbox` (~1.32 GB) | 47.7–62.9 s |
| — pulling `boxkite-sidecar` (~123 MB) | ~10 s |
| Cold start, node **already has the image cached** | 0.85–1.16 s |
| Warm-pool claim (pod pre-pulled + pre-started) | ~1.0 s |

So the ~1.32 GB `sandbox` image pull is the lever: it dominates a fresh-node
cold start and gates how fast the warm pool can replenish after a spike.
Everything below reduces or hides that pull.

## Lever 1 — GKE Image Streaming (biggest, non-breaking)

[Image Streaming](https://cloud.google.com/kubernetes-engine/docs/how-to/image-streaming)
(`gcfs`) lets a pod start before the full image is local — layers are streamed
on demand — turning a multi-second pull into a near-instant start. It requires
images stored in Artifact Registry.

Enable it on the cluster (existing cluster):

```bash
gcloud container clusters update <CLUSTER> \
  --location <LOCATION> \
  --enable-image-streaming
```

Or at creation time with `--enable-image-streaming`. No application change is
needed; it is purely a node-runtime feature. This is the recommended first
move for any GKE deployment.

## Lever 2 — pick a smaller sandbox image via `SANDBOX_IMAGE`

The sandbox image is selected by the `SANDBOX_IMAGE` env var (read by both the
manager and the warm pool; defaults to `boxkite-sandbox:latest`). The bulk of
the default image is Chrome-for-Testing, pandoc, and the bundled language
runtimes. Workloads that don't drive a browser (the `browser_*` tools) or
render documents can run a much smaller image:

```bash
# Build/publish the minimal variant (already in this repo):
#   deploy/sandbox-minimal.Dockerfile
export SANDBOX_IMAGE=<your-registry>/boxkite-sandbox-minimal:<tag>
```

Prebuilt variants in `deploy/` for common stacks (each smaller than the
kitchen-sink default): `sandbox-minimal`, `sandbox-node`, `sandbox-go`,
`sandbox-rust`, `sandbox-nextjs`, `sandbox-lsp`. Pick the smallest one that
carries the tools your agents actually use.

**Tradeoff:** `sandbox-minimal` has no browser and no pandoc — the
`browser_navigate`/`browser_exec`/`screenshot` tools and document rendering
won't work under it. Switching the *default* is therefore a deployment
decision, not made here; this documents the knob and the tradeoff.

## Lever 3 — size the warm pool to your concurrency

A warm pool pre-pays the pull before the request arrives, collapsing
fresh-node ~74.7 s to ~1 s (~70×). Set `WARM_POOL_SIZE` to at least your
expected steady-state concurrency so real users rarely land on a cold pull,
and remember the pool can only replenish as fast as the image pulls — so
Levers 1 and 2 also make the pool recover faster after a spike.

## Follow-up — shrink `sandbox.Dockerfile` itself

The measurable next step (not done here because it needs an image build to
verify size + that nothing breaks): multi-stage prune, `--no-install-recommends`,
dropping build toolchains from the final layer, and/or splitting rarely-used
tooling into opt-in variants. Track under #233.
