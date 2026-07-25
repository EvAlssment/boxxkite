# Local Kind Cluster Quickstart

This is the "real Kubernetes, but on your laptop" path — a [kind](https://kind.sigs.k8s.io/)
cluster running the actual pod-per-session sandbox, warm pool, and RBAC/network-policy
shape described in the top-level README. Use `../docker-compose.yml` instead if you just
want to try the sandbox quickly without a Kubernetes cluster.

## Prerequisites

- Docker Desktop (or another local Docker daemon)
- [`kind`](https://kind.sigs.k8s.io/) (the setup script installs it via `brew` if missing)
- `kubectl`

## Known limitations

**The `boxkite-sandbox` image cannot be built natively on Apple Silicon / arm64
hosts.** `./deploy/local-kind/setup.sh` (and `setup.sh reload`) build
`boxkite-sandbox` from `../sandbox.Dockerfile`, which intentionally `exit 1`s on
`arm64` during the Chrome-for-Testing install step
(`deploy/sandbox.Dockerfile` lines ~112-122 and ~132-138). This is **not a
bug and not a stale version pin** — it's a deliberate security control, and it
should not be "fixed" by relaxing it:

- The sandbox's Chromium is replaced with a pinned Chrome-for-Testing build
  specifically to clear known-vulnerability-scanner findings against
  Playwright's older bundled Chromium (see the comment block right above the
  `case "$(uname -m)"` in `sandbox.Dockerfile` for the full rationale).
- Chrome for Testing's own published manifest
  (`googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json`)
  publishes `linux64`, `mac-arm64`, `mac-x64`, `win32`, and `win64` artifacts —
  **no `linux-arm64` build exists for any version**. There is no version to
  pin that would fix this for Linux/arm64; it's a permanent upstream gap, not
  something a newer pin will resolve.
- Silently falling back to Playwright's bundled Chromium on arm64 would ship
  the older, vulnerable browser the pin exists to avoid — so the Dockerfile
  fails the build instead, on purpose.

If you're on an Apple Silicon Mac (or any arm64 host), `setup.sh` will fail
when it tries to build `boxkite-sandbox`. Workarounds, in order of
preference:

1. **Build the `boxkite-sandbox` image on an amd64 CI runner or an amd64
   cloud/remote Docker host**, then push it to a registry your kind cluster
   can pull from (`docker buildx build --platform linux/amd64 ...` from that
   amd64 host, `docker push`, then point `setup.sh`/`k8s-resources.yaml` at
   the pushed tag instead of building locally).
2. **Develop/test `local-kind` on an actual amd64 machine** — a cloud dev box
   or CI runner, for example — instead of your local laptop.

This does **not** mean changing the architecture your local Docker/colima VM
runs as (e.g. `colima start --arch x86_64`) — that changes the daemon for
everything else running under it on this machine too, and isn't something to
do just to unblock this one image build. Use one of the two workarounds above
instead.

Once pre-built images are published to GHCR (see the top-level README's
"Self-hosting" section), this will mostly go away for local-kind users — you
won't need to build `boxkite-sandbox` locally at all on any architecture.

## Setup

```bash
./deploy/local-kind/setup.sh
```

This creates a `boxkite-dev` kind cluster, builds the `boxkite-sandbox` and
`boxkite-sidecar` images from `../sandbox.Dockerfile` / `../sidecar.Dockerfile`,
loads them into the cluster, and applies `k8s-resources.yaml` (ServiceAccount,
ConfigMap, Secret, RBAC, PriorityClasses).

After it finishes, start a `kubectl proxy` in the background — pod IPs inside a
kind cluster's Docker network aren't reachable directly from the host on macOS,
so the sandbox package routes sidecar HTTP through the K8s API proxy instead:

```bash
kubectl proxy --context kind-boxkite-dev --reject-paths='' &
```

`--reject-paths=''` is required because the default kubectl proxy blocks any URL
matching `/exec` or `/attach` — the sidecar's `/exec` endpoint URL matches that
pattern by coincidence.

Then, wherever you're using `boxkite.SandboxManager` (see the top-level README's
quickstart), set:

```bash
export SANDBOX_IMAGE=boxkite-sandbox:local
export SIDECAR_IMAGE=boxkite-sidecar:local
export SANDBOX_USE_K8S_PROXY=true
export RUNTIME_MODE=k8s
```

Warm pool pods should appear shortly after: `kubectl get pods -l app=sandbox --context kind-boxkite-dev`.

Unlike `../docker-compose.yml`, you do **not** need to generate or set a
`SIDECAR_AUTH_TOKEN` yourself here — in K8s mode, `SandboxManager`/
`WarmPoolManager` generate a fresh random secret per pod automatically at
pod-creation time and both inject it into the sidecar container and record
it on the pod's own annotation, so the same process (or a different one,
after a restart) can recover it. See `src/boxkite/sidecar_auth.py` and the
top-level README's "Security" section.

## Reload after code/image changes

```bash
./deploy/local-kind/setup.sh reload
```

Rebuilds both images and reloads them into the existing cluster without
recreating it.

## Verify it works

```bash
# Should list one or more warm pods once the pool replenishes:
kubectl get pods -l app=sandbox,pool=warm --context kind-boxkite-dev
```

See the top-level README's quickstart for driving `bash_tool`/`file_create`
against a claimed pod once the pool is warm.

## Teardown

```bash
./deploy/local-kind/teardown.sh
```

Deletes the kind cluster and (optionally, on prompt) the two local images.

## What's in `k8s-resources.yaml`

- `ServiceAccount` for sandbox pods, with `automountServiceAccountToken: false`
- `ConfigMap` for pool size, image references, and priority class names
- `Secret` for storage credentials (empty by default — see the CHANGEME note
  inline; the sidecar runs fine with no storage backend configured, it just
  skips S3/Azure sync)
- `Role`/`RoleBinding` scoping pod get/list/watch/create/delete/patch to
  whatever identity your `SandboxManager` process runs as (in kind, that's
  usually just your default kubeconfig identity, which already has
  cluster-admin — the RBAC here exists for parity with a real cluster, not
  because kind enforces it). Kubernetes RBAC can't restrict a Role's pod verbs
  to label-matched pods — that restriction only happens in application code
  (see the comment in `../rbac.yaml`). **In a real cluster, run the sandbox
  manager's ServiceAccount in a dedicated namespace containing only sandbox
  pods**, so "any pod in the namespace" and "any sandbox pod" are the same
  set and a compromised manager credential can't reach unrelated workloads.
- Two `PriorityClass` resources so claimed (active-session) pods are harder to
  evict than idle warm-pool pods under node pressure

This file intentionally skips `NetworkPolicy` (kind's default CNI doesn't
enforce it) and an image prepuller (not needed for a single-node local
cluster). See `../network-policy.yaml` and `../rbac.yaml` for the
production-cluster equivalents.

## Alternative: install the production manifests via Helm

`k8s-resources.yaml` above is deliberately a stripped-down, kind-friendly
subset. For a real cluster (or to smoke-test the production shape locally,
NetworkPolicy included, on a kind cluster whose CNI actually enforces it),
`../helm/boxkite/` packages `../rbac.yaml`, `../network-policy.yaml`,
`../pod-security-policy.yaml`, and the opt-in image-builder RBAC/
NetworkPolicy as a Helm chart instead of a hand-applied manifest:

```bash
helm install boxkite ../helm/boxkite --namespace boxkite --create-namespace
```

See `../helm/boxkite/README.md` for the full set of values worth reviewing
before installing (storage egress mode, storage credentials, image-builder
opt-in). This does not replace `setup.sh` above -- it's the RBAC/
NetworkPolicy layer only, not the sandbox/sidecar images or a running
control plane.
