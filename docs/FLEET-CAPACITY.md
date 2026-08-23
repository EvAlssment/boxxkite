# Fleet capacity configuration

`deploy/fleet.yaml` is an optional, reviewable capacity source for operators
whose deployment has more than one Kubernetes cluster. Each process selects
one entry from the file; this config surface does not perform cross-cluster
routing.

## Enable it

```bash
export BOXXKITE_FLEET_CONFIG=/etc/boxxkite/fleet.yaml
export BOXXKITE_CLUSTER_NAME=us-east
```

`BOXXKITE_CLUSTER_NAME` may be omitted when the file contains exactly one
cluster. It is required for a multi-cluster file. The file is loaded and
validated during runtime configuration loading, so an unknown cluster,
duplicate name, malformed field, or capacity overflow stops startup with a
specific error.

Each cluster declares:

- `warmPool.targets` for the `small`, `medium`, and `large` size classes;
- `warmPool.maxSize`, an inclusive per-cluster ceiling. The sum of all targets
  must not exceed it;
- optional `images.sandbox` and `images.sidecar` references;
- optional Kubernetes `nodeSelector` labels and `tolerations`.

The selected images and scheduling constraints are used for both cold-created
and warm-pool pods in that process. The config is operator-owned input; it is
not populated from session or agent requests. The existing pod security
context, service-account policy, network isolation, and per-size resource
limits remain unchanged.

## Single-cluster compatibility

When `BOXXKITE_FLEET_CONFIG` is unset, the existing env-var path remains the
source of truth:

```bash
WARM_POOL_SIZE=3
WARM_POOL_SIZE_SMALL=3
WARM_POOL_SIZE_MEDIUM=0
WARM_POOL_SIZE_LARGE=0
WARM_POOL_MAX=15
```

Those variables are ignored for the selected cluster's warm-pool targets and
ceiling once a fleet file is enabled. This makes switching from one cluster
to a reviewed multi-cluster file explicit rather than merging two competing
capacity sources.

The current checkout still has one Kubernetes client/provider per process.
Selecting multiple entries therefore means running one configured process per
cluster; a future provider registry can consume the same validated model for
request routing.
