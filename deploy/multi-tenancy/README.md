# Per-tenant reference manifests

Templates for the namespace-per-tenant model described in
[docs/SELF-HOSTED-MULTI-TENANCY.md](../../docs/SELF-HOSTED-MULTI-TENANCY.md).
Read that first — it explains *why* the namespace is the tenant boundary here,
and what these manifests can and cannot enforce.

Short version: sandbox pods carry no tenant label (`app: sandbox` plus
pool/status/size, nothing identifying the account), so a per-tenant
`ResourceQuota` or `NetworkPolicy` inside a shared namespace has nothing to
select on. Giving each tenant its own namespace is what makes these
namespace-scoped controls work.

## Files

| File | What it does |
| --- | --- |
| `namespace.yaml` | The tenant's namespace, labelled for Pod Security Admission `restricted` |
| `resource-quota.yaml` | Aggregate CPU/memory/pod/storage ceiling for the tenant |
| `limit-range.yaml` | Per-pod floor and ceiling inside that namespace |
| `network-policy-tenant.yaml` | Denies traffic to/from other namespaces (tenants) |

These are **templates, not defaults**. Nothing in the Helm chart applies them,
and the numbers are a starting point, not a recommendation for your workload.

## Applying them

Replace the `TENANT` placeholder, then apply:

```bash
mkdir -p /tmp/boxxkite-tenant
for f in deploy/multi-tenancy/*.yaml; do
  sed 's/TENANT/acme/g' "$f" > "/tmp/boxxkite-tenant/$(basename "$f")"
done

# Always dry-run first -- a quota sized below what your sandbox size presets
# request surfaces as pods stuck Pending, not as a clear error.
kubectl apply --dry-run=server -f /tmp/boxxkite-tenant/
kubectl apply -f /tmp/boxxkite-tenant/
```

Then point that tenant's control-plane at the namespace:

```bash
SANDBOX_NAMESPACE=boxxkite-acme
```

## Still required per tenant

These manifests only cover the Kubernetes-side boundary. Each tenant also
needs its own control-plane deployment with its own database and its own
`SIDECAR_AUTH_TOKEN`, and `deploy/network-policy.yaml` still has to be applied
inside the namespace — that's the per-sandbox boundary, and this directory's
policy is the per-tenant one. They are not substitutes for each other.

## Sizing

`resource-quota.yaml` is sized for roughly 10 concurrent `small` sandboxes.
Check `src/boxxkite/resource_config.py`'s `SANDBOX_SIZE_PRESETS` for the actual
per-pod requests and limits of the sizes your tenants can request, and scale
from there — including headroom for the warm pool, whose pods count against
`count/pods` like any other.
