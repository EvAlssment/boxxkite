# BEP-0004: Default-deny network boundary

| Field | Value |
| --- | --- |
| Status | Implemented (retrospective) |
| Type | Security model |
| Related documentation | [Isolation model](../docs/architecture/isolation-model.md), [network policy](../deploy/network-policy.yaml) |

## Summary

The default Kubernetes network policy denies sandbox ingress and egress, with
only explicitly configured destinations allowed. In the normal Kubernetes
execution path, commands can also run in a fresh empty network namespace;
these are independent layers and both depend on correct operator and cluster
configuration.

## Motivation

Agent-generated code should not receive ambient network access from the
sandbox pod. A default-deny policy makes every required destination an
explicit deployment decision and leaves the per-exec network namespace as a
separate defense layer.

## Decision

The reference policy selects live sandbox pods, declares both `Ingress` and
`Egress` policy types, permits only the documented sidecar ingress sources and
DNS by default, and leaves storage or other workflow destinations to explicit
operator configuration. The manifest rejects a blanket unrestricted egress
fallback in its guidance.

The isolation documentation states the boundary and its limits: NetworkPolicy
enforcement is CNI-dependent, the per-exec network namespace is configurable,
and the sidecar's authentication and other controls remain separate layers.

## Documented trade-offs

The boundary is restrictive by default, so workflows that need storage,
secrets, browser, or source-control access require separately scoped
configuration. The policy is not sufficient by itself: operators must verify
CNI enforcement and keep the sidecar authentication layer enabled.

## Evidence

- [Isolation model: network boundary](../docs/architecture/isolation-model.md#network-boundary)
- [Reference NetworkPolicy](../deploy/network-policy.yaml)
- [Self-hosted multi-tenancy guide](../docs/guides/self-hosted-multi-tenancy.md)
- [Security model](../SECURITY.md)

This BEP was written after the implementation and security documentation
shipped. It records the repository's current decision and does not claim that
the historical changes followed the BEP lifecycle.
