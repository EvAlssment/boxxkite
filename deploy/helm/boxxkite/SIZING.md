# Resource sizing guide

The chart does not deploy the control-plane or the session pods itself. The
control-plane creates those pods at runtime, so the files in this directory
are capacity inputs for the cluster that will run them.

## Presets

Each preset is a valid Helm values overlay:

| Preset | Concurrent sandboxes | Warm pool | Starting cluster shape |
|---|---:|---:|---|
| `values-small.yaml` | about 10 | 2 | 3 small general-purpose nodes |
| `values-medium.yaml` | about 50 | 6 | 3 medium general-purpose nodes |
| `values-large.yaml` | about 200 | 20 | 6 large general-purpose nodes |

The concurrency figures are starting points, not guarantees. The sidecar
container is the enforced per-session budget; command mix, image-pull time,
filesystem activity, and the selected node size change the practical limit.
Measure representative workloads before committing a production capacity
decision.

## Install

```bash
helm install boxxkite ./deploy/helm/boxxkite \
  --namespace boxxkite --create-namespace \
  --set namespace=boxxkite \
  -f ./deploy/helm/boxxkite/values-medium.yaml
```

The overlays intentionally change only `sandboxConfig.warmPoolSize`; the
resource requests and limits remain the runtime defaults guarded by
`tests/test_pod_template_parity.py`. If your nodes have different CPU or
memory shapes, override `resources` in a deployment-specific values file and
rerun the capacity test.

## Provider estimates

Cloud-provider rates and regional taxes change independently of this chart.
Use the node counts and container requests above as inputs to your provider's
current calculator rather than embedding stale currency figures in a release.
This keeps the repository's guidance reproducible while still making the
infrastructure assumptions explicit.
