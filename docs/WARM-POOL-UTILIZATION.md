# Warm-pool utilization

`GET /v1/admin/warm-pool` reports the signals needed to tune the warm pool by
size class. Dashboard sessions can use the equivalent
`GET /v1/account/admin/warm-pool` route.

Each entry in `sizes` contains:

- `target`: the current reconciler target for that size class, including the
  adaptive target when adaptive sizing is enabled.
- `actual_warm_count`: Running, ready pods currently labeled as warm and young
  enough to claim. This is a live Kubernetes scan, not a reservation count.
- `claims_last_window`: warm-pod claims served in the rolling window.
- `cold_fallthroughs_last_window`: requests that attempted a normal warm-pool
  claim for this size but had to create a fresh pod instead.

The window is reported as `window_seconds`. These demand counters are
in-memory, per control-plane process, and reset on process restart; they are
not aggregated across replicas. The endpoint returns `available: false` when
the runtime has no configured warm pool or its status cannot be read, rather
than filling the response with inferred values.

Requests that require a fresh pod by design are not cold fall-throughs. This
includes per-session storage or lifetime overrides, custom images, volume
mounts, and GPU requests, because those pods cannot use the pre-warmed pool.

The reconciler also emits the same per-size report at its normal background
reconciliation interval. The log line is a snapshot of the live warm count
and the rolling demand counters; it is intended for operator diagnostics, not
as a durable time series.
