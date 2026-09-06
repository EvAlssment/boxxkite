# Multi-tenancy for self-hosted boxxkite

If one deployment serves several business units or customers, first decide
which boundary you need. The control-plane supports account-scoped resources
inside one deployment. Stronger infrastructure separation is the supported
one-deployment-per-tenant model described below.

## What is isolated today

Each sandbox session gets its own pod. The runtime configures the sandbox
container as non-root, drops its capabilities, uses a read-only root
filesystem and `seccompProfile: RuntimeDefault`, and disables automatic
service-account-token mounting. The sidecar has a separate security context
because it must use `nsenter` to operate on the sandbox process namespace.

The default network policy denies sandbox ingress and egress. The sidecar also
supports a fresh network namespace for each `/exec` call; disabling that
option leaves the Kubernetes NetworkPolicy as the remaining network
backstop. Verify that the cluster's CNI actually enforces NetworkPolicy.

Warm-pool recycling kills tracked processes and resets the workspace before a
pod is claimed by another session. Control-plane lookups are account-scoped,
so a resource owned by another account is returned as not found.

## What is not tenant-aware

Sandbox pods share the configured namespace and do not carry a tenant label
that can be used by a per-tenant policy. As a result, one shared namespace
cannot currently provide separate per-tenant `NetworkPolicy`, `ResourceQuota`,
or `LimitRange` controls. Global concurrency settings also apply to the whole
deployment rather than allocating capacity per account.

## Supported deployment model

For independent blast radius and namespace-scoped controls, give each tenant
their own namespace, control-plane, and database. Apply the reference
manifests in [`deploy/multi-tenancy/`](../../deploy/multi-tenancy/) with the
tenant-specific values; they are templates and are not intended to be applied
unchanged.

This model separates namespace-scoped resources and control-plane data. Pods
still share the cluster's nodes and therefore the node kernel unless the
operator deliberately selects an experimental alternate runtime. Read the
[isolation model](../architecture/isolation-model.md) before choosing that
boundary.

For the full account-scoping and checklist details retained from the original
document, see [the compatibility page](../SELF-HOSTED-MULTI-TENANCY.md).
