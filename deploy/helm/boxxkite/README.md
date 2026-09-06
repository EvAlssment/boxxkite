# boxxkite Helm chart

Packages the cluster-level manifests self-hosters need for a real
Kubernetes deployment: `../rbac.yaml`, `../network-policy.yaml`,
`../pod-security-policy.yaml`, and (opt-in) `../image-builder-rbac.yaml` +
`../image-builder-network-policy.yaml`.

**What this chart does NOT do**: deploy the control-plane API, build/push
the `boxxkite-sandbox`/`boxxkite-sidecar` images, or create the per-session
sandbox pod itself. `src/boxxkite/manager.py` builds that pod spec
programmatically at session-creation time -- `../pod-template.yaml` is a
reference doc for it, not something applied once via `helm install`, and
this chart follows the same split. See the top-level README's "Quickstart:
real Kubernetes, via kind" section and `../local-kind/README.md` for a
full local walkthrough of what a real deployment looks like end-to-end.

## Install

```bash
helm install boxxkite ./deploy/helm/boxxkite \
  --namespace boxxkite --create-namespace \
  --set namespace=boxxkite
```

Review `values.yaml` first -- in particular:

- [`SIZING.md`](SIZING.md) lists small, medium, and large capacity overlays.
  Choose one as a starting point, then measure your representative workload;
  these overlays change only the adaptive warm-pool target.

- `networkPolicy.storageEgress.mode` defaults to `none` (no storage egress
  rule at all, fails closed). Set it to `inCluster`, `ipBlock`, or `fqdn`
  and fill in the matching real value before the sidecar's S3/Azure/MinIO
  sync will work over the network this policy governs.
- `storageCredentials.accessKeyId` / `secretAccessKey` render a plain
  Kubernetes Secret if left set (base64, not encrypted at rest unless your
  cluster has envelope encryption enabled) -- set
  `storageCredentials.manage=false` and manage that Secret out-of-band
  (IRSA/Workload Identity, external-secrets, sealed-secrets) for anything
  beyond a quick local test.
- `podSecurityPolicy.enabled` requires the `ValidatingAdmissionPolicy` API
  (GA in Kubernetes 1.30, beta since 1.28) -- set to `false` on older
  clusters rather than let `helm install` fail on an unrecognized API.
- `imageBuilder.enabled` is `false` by default, matching
  `BOXXKITE_IMAGE_BUILDER_ENABLED=false`. Its NetworkPolicy CIDRs default to
  a non-routable RFC 5737 placeholder (same as `../image-builder-network-policy.yaml`)
  -- fill in your real package-registry/container-registry CIDRs before
  enabling it for real.

Before moving between chart releases, inspect the machine-readable
compatibility matrix:

```bash
TARGET_CHART_VERSION=0.1.1  # choose an entry present in COMPATIBILITY.json
boxxkite upgrade check --from 0.1.0 --to "$TARGET_CHART_VERSION" \
  --matrix ./deploy/helm/boxxkite/COMPATIBILITY.json
```

The command prints every intermediate migration entry and exits non-zero if
one is marked breaking. Review its migration note before rerunning with
`--allow-breaking`.

## Lint / dry-run

```bash
helm lint ./deploy/helm/boxxkite
helm template boxxkite ./deploy/helm/boxxkite --set namespace=boxxkite
```

Both commands enforce `values.schema.json`: it rejects unknown values, wrong
types, unsupported egress modes, and plainly malformed IPv4/IPv6 CIDR strings
before rendering. `.github/workflows/helm-chart.yml` runs these same checks
plus `tests/test_helm_smoke.py` whenever the chart changes. It ships enabled,
so a fork or a downstream copy of this repo gets it automatically; it is
switched off in this repo specifically (see CONTRIBUTING.md), same as the
other workflows here. Schema checks intentionally do not contact a Kubernetes API
server and do not validate Kubernetes resource-quantity semantics; use the
manual kind smoke test below to check a real cluster.

## Smoke test against a local kind cluster

```bash
kind create cluster --name boxxkite-helm-smoke
helm install boxxkite ./deploy/helm/boxxkite \
  --namespace boxxkite --create-namespace \
  --set namespace=boxxkite \
  --kube-context kind-boxxkite-helm-smoke
kubectl get role,rolebinding,networkpolicy,configmap,secret -n boxxkite
kind delete cluster --name boxxkite-helm-smoke
```

`tests/test_helm_smoke.py` in the root test suite automates a lighter
version of this (`helm lint` + `helm template`), gated on `helm`
being present on `PATH`, and skips outright otherwise.

## Uninstall

```bash
helm uninstall boxxkite --namespace boxxkite
```

## Cold-start latency

The dominant cold-start cost is pulling the ~1.32 GB sandbox image. See
[`../COLD-START-TUNING.md`](../COLD-START-TUNING.md) for the levers: GKE Image
Streaming, choosing a smaller `SANDBOX_IMAGE`, and warm-pool sizing.

## Parity with the runtime code

`tests/test_pod_template_parity.py::test_helm_values_defaults_match_resource_config_defaults`
asserts `values.yaml`'s `resources`/`volumeSizeLimits` defaults stay
byte-identical to `src/boxxkite/resource_config.py`'s
`DEFAULT_SANDBOX_CONTAINER_*`/`DEFAULT_SANDBOX_SIDECAR_*`/
`DEFAULT_SANDBOX_*_VOLUME_SIZE_LIMIT` constants -- the same drift class that
previously caused a real ~4-13x mismatch in `../pod-template.yaml`. If you
change one, change the other in the same commit.

## Admin fleet status

The control-plane exposes a read-only, admin-key-only operational snapshot at
`GET /v1/admin/fleet/status`. It reports the current runtime's warm-pool target
and actual counts by size, plus the recent claim rate from the existing
warm-pool status source:

```bash
curl -H "Authorization: Bearer bxk_live_..." \
  https://control-plane.example/v1/admin/fleet/status
```

The current architecture has one runtime record rather than a multi-cluster
registry; its `cluster_id` comes from `BOXXKITE_CLUSTER_ID` and defaults to
`default`. Signals that are not available from the runtime status source (such
as claim fall-throughs, node pressure, pending pod phases, or placement
classes) are returned as `supported: false` with a reason; consumers should
not treat those fields as zero.
