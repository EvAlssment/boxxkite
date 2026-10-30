# BEP-0002: Fleet capacity and warm-pool observability

| Field | Value |
| --- | --- |
| Status | Implemented (retrospective) |
| Type | Runtime configuration and operations |
| Related implementation | [4748b65](https://github.com/EvAlssment/boxxkite/commit/4748b65), [64d3ca3](https://github.com/EvAlssment/boxxkite/commit/64d3ca3) |

## Summary

Warm-pool capacity is an operator-owned configuration, and the runtime
exposes the live and demand signals needed to inspect it. The configuration
and observability surfaces do not introduce cross-cluster request routing.

## Motivation

Warm capacity differs by cluster and sandbox size. Operators need a reviewed
source for per-cluster targets and ceilings, while troubleshooting also needs
to distinguish available warm pods, served claims, and requests that fell
through to fresh pod creation.

## Decision

When `BOXXKITE_FLEET_CONFIG` is enabled, each selected cluster entry can set
`warmPool.targets` for the `small`, `medium`, and `large` size classes and an
inclusive `warmPool.maxSize`. Optional image and scheduling settings apply to
both cold-created and warm-pool pods. The sum of targets cannot exceed the
cluster ceiling.

When the fleet file is unset, the existing warm-pool environment variables
remain the source of truth. The current process still uses one Kubernetes
client/provider per process; selecting multiple entries means running one
configured process per cluster.

The warm-pool admin views report the target, actual ready warm count, claims in
the rolling window, and cold fall-throughs by size class. Demand counters are
in-memory and per process, reset on restart, and are not aggregated across
replicas.

## Documented trade-offs

The configuration is explicit and reviewable, but it is not a routing layer.
The operational counters are useful for live diagnosis, but they are not a
durable time series. Requests that require custom resources or configuration
are not counted as ordinary warm-pool fall-throughs because they cannot use a
pre-warmed pod.

## Evidence

- [Fleet capacity configuration](../docs/FLEET-CAPACITY.md)
- [Warm-pool utilization reporting](../docs/WARM-POOL-UTILIZATION.md)
- [Declarative fleet capacity implementation](https://github.com/EvAlssment/boxxkite/commit/4748b65)
- [Warm-pool utilization implementation](https://github.com/EvAlssment/boxxkite/commit/64d3ca3)

This BEP was written after the implementation shipped. It records the
repository's current decision and does not claim that the historical changes
followed the BEP lifecycle.
