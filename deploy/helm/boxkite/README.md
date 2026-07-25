# boxkite Helm chart

Packages the cluster-level manifests self-hosters need for a real
Kubernetes deployment: `../rbac.yaml`, `../network-policy.yaml`,
`../pod-security-policy.yaml`, and (opt-in) `../image-builder-rbac.yaml` +
`../image-builder-network-policy.yaml`.

**What this chart does NOT do**: deploy the control-plane API, build/push
the `boxkite-sandbox`/`boxkite-sidecar` images, or create the per-session
sandbox pod itself. `src/boxkite/manager.py` builds that pod spec
programmatically at session-creation time -- `../pod-template.yaml` is a
reference doc for it, not something applied once via `helm install`, and
this chart follows the same split. See the top-level README's "Quickstart:
real Kubernetes, via kind" section and `../local-kind/README.md` for a
full local walkthrough of what a real deployment looks like end-to-end.

## Install

```bash
helm install boxkite ./deploy/helm/boxkite \
  --namespace boxkite --create-namespace \
  --set namespace=boxkite
```

Review `values.yaml` first -- in particular:

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
  `BOXKITE_IMAGE_BUILDER_ENABLED=false`. Its NetworkPolicy CIDRs default to
  a non-routable RFC 5737 placeholder (same as `../image-builder-network-policy.yaml`)
  -- fill in your real package-registry/container-registry CIDRs before
  enabling it for real.

## Lint / dry-run

```bash
helm lint ./deploy/helm/boxkite
helm template boxkite ./deploy/helm/boxkite --set namespace=boxkite
```

## Smoke test against a local kind cluster

```bash
kind create cluster --name boxkite-helm-smoke
helm install boxkite ./deploy/helm/boxkite \
  --namespace boxkite --create-namespace \
  --set namespace=boxkite \
  --kube-context kind-boxkite-helm-smoke
kubectl get role,rolebinding,networkpolicy,configmap,secret -n boxkite
kind delete cluster --name boxkite-helm-smoke
```

`tests/test_helm_smoke.py` in the root test suite automates a lighter
version of this (`helm lint` + `helm template --dry-run`), gated on `helm`
being present on `PATH`, and skips outright otherwise.

## Uninstall

```bash
helm uninstall boxkite --namespace boxkite
```

## Cold-start latency

The dominant cold-start cost is pulling the ~1.32 GB sandbox image. See
[`../COLD-START-TUNING.md`](../COLD-START-TUNING.md) for the levers: GKE Image
Streaming, choosing a smaller `SANDBOX_IMAGE`, and warm-pool sizing.

## Parity with the runtime code

`tests/test_pod_template_parity.py::test_helm_values_defaults_match_resource_config_defaults`
asserts `values.yaml`'s `resources`/`volumeSizeLimits` defaults stay
byte-identical to `src/boxkite/resource_config.py`'s
`DEFAULT_SANDBOX_CONTAINER_*`/`DEFAULT_SANDBOX_SIDECAR_*`/
`DEFAULT_SANDBOX_*_VOLUME_SIZE_LIMIT` constants -- the same drift class that
previously caused a real ~4-13x mismatch in `../pod-template.yaml`. If you
change one, change the other in the same commit.
