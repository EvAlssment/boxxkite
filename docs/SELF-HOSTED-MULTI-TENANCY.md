# Multi-tenancy for self-hosted boxxkite

If you're a platform team offering boxxkite internally to several business
units, or an operator running one install for more than one customer, the
question is: **what actually separates one tenant from another, and what
doesn't?**

This document answers that against the code as it exists today, including the
parts that aren't supported. Where a protection isn't there, it says so
plainly rather than leaving you to infer it from a manifest.

Related reading: [SECURITY.md](../SECURITY.md) (what's in scope for a report),
[deploy/network-policy.yaml](../deploy/network-policy.yaml) (the isolation
backstop and its CNI caveats), and
[deploy/multi-tenancy/](../deploy/multi-tenancy/) (reference manifests for the
model recommended below).

## What "tenant" means here

Two different things get called multi-tenancy, and they have different answers:

1. **Accounts inside one control-plane.** The hosted control-plane already has
   accounts, API keys, and per-account scoping of every resource. Sandboxes,
   secrets, snapshots, volumes, and images are all looked up with an
   `account_id`-scoped query — a cross-account id 404s identically to a
   nonexistent one, so a caller can't even probe existence.
2. **Separate organizations that must not share infrastructure.** Different
   budgets, different blast radius, possibly different compliance answers.

boxxkite supports (1) out of the box. For (2), the honest answer is **one
deployment per tenant** — and the rest of this document explains why the
shared-namespace alternative doesn't hold up today.

## What isolates one sandbox from another today

Independent of tenancy, every sandbox session already gets:

- **Its own pod.** One session per pod, non-root, all capabilities dropped,
  read-only root filesystem, `seccompProfile: RuntimeDefault` set at runtime on
  both containers, and `automountServiceAccountToken: false`.
- **No network path to another sandbox.** `deploy/network-policy.yaml` selects
  `app: sandbox` with default-deny ingress and egress; nothing in it permits
  sandbox-to-sandbox traffic. Independently, each `/exec` runs in a freshly
  created empty network namespace, so an exec'd process has no interfaces at
  all -- that one is on by default and disappears if you set
  `SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED=false`, at which point the
  NetworkPolicy is the only backstop.
- **Storage scoped by organization.** Session files live under a prefix keyed
  by the organization id the control-plane passes down, so one tenant's objects
  are not addressable from another's prefix.
- **A wiped pod between sessions.** Warm-pool recycling kills every tracked
  process, interpreter, browser, desktop, and LSP server, and wipes the
  workspace, before a pod is handed to the next session.

That is a real per-session boundary, and it applies whether or not you care
about tenancy.

## What does *not* separate tenants today

This is the part worth reading twice.

**Sandbox pods carry no tenant identity.** Every sandbox pod is created in a
single namespace (`SANDBOX_NAMESPACE`, default `default`) with the labels
`app: sandbox`, `pool`, `sandbox.boxxkite.dev/status`, and a size label. There
is no account, organization, or tenant label on the pod.

The consequences follow directly from that:

- **You cannot write a per-tenant `NetworkPolicy`.** A policy can select
  `app: sandbox`; it cannot select "tenant A's sandboxes".
- **You cannot write a per-tenant `ResourceQuota` or `LimitRange`.** Both are
  namespace-scoped objects, and all tenants share one namespace, so a quota
  applies to everyone at once.
- **You cannot give one tenant a bigger allowance than another.**
  `BOXXKITE_MAX_CONCURRENT_SANDBOXES` and
  `BOXXKITE_FREE_MONTHLY_SANDBOX_HOURS` are process-wide settings applied
  identically to every account. There is no per-account override in the data
  model.
- **One tenant can exhaust shared capacity.**
  `BOXXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES` caps the whole install, so a
  tenant running at their per-account limit consumes global slots other tenants
  then can't get. The cap prevents cluster exhaustion; it does not allocate
  fairly between tenants.
- **Node-level isolation is not tenant-aware.** Pods share nodes, and therefore
  a kernel. There is no per-tenant node pool, taint, or affinity wired up, and
  boxxkite's boundary is a container boundary rather than a hypervisor one --
  there is no VM-isolated runtime tier.

None of this is a bug — it's what a single-organization deployment target
implies. It only becomes a problem if you assume otherwise, which is exactly
what this document exists to prevent.

## Supported model: one deployment per tenant

Give each tenant their own namespace, their own control-plane, and their own
database. Then every namespace-scoped Kubernetes primitive works normally,
because the namespace *is* the tenant.

| Concern | How it's handled |
| --- | --- |
| Compute/memory ceiling | `ResourceQuota` per tenant namespace |
| Per-pod sizing floor/ceiling | `LimitRange` per tenant namespace |
| Cross-tenant network traffic | Namespaced NetworkPolicy denying other namespaces |
| Noisy-neighbour blast radius | Separate control-plane process and database |
| Differentiated allowances | Different `BOXXKITE_*` env values per deployment |
| Deleting a tenant | Delete the namespace |

Reference manifests for exactly this are in
[deploy/multi-tenancy/](../deploy/multi-tenancy/) — a namespace with the PSA
labels boxxkite expects, a `ResourceQuota`, a `LimitRange`, and a
tenant-scoped `NetworkPolicy`. They're templates with a `TENANT` placeholder,
not something to apply unmodified.

**Cost:** one control-plane process and one database per tenant. For a handful
of business units that's usually acceptable. For hundreds of small tenants it
isn't, and the honest answer is that boxxkite doesn't have a good story for
that shape yet.

## Unsupported model: shared namespace, per-tenant policy

Tempting, and it doesn't work today — a per-tenant `NetworkPolicy` or
`ResourceQuota` needs a tenant label on the pod to select, and there isn't one
(see above). Adding that label is a plausible future change; if you need it,
[open an issue](https://github.com/EvAlssment/boxxkite/issues) describing the
shape you need rather than patching a label in locally and discovering later
that warm-pool claim logic doesn't preserve it.

## Middle ground: shared cluster, namespace per tenant

One Kubernetes cluster, one namespace per tenant, one control-plane deployment
per tenant pointed at its own namespace via `SANDBOX_NAMESPACE`. You keep a
single cluster to operate while every namespace-scoped control still works
per-tenant.

What you're accepting: a shared control plane at the *Kubernetes* level (one
API server, one etcd, shared nodes and therefore a shared kernel), so this
separates resource usage and network reachability, not the node itself.

## Checklist before you offer this to another team

- [ ] Each tenant has its own namespace, with
      `deploy/multi-tenancy/pod-hardening-policy.yaml` bound to it. Do **not**
      label these namespaces `pod-security.kubernetes.io/enforce=restricted`
      or `=baseline`; both reject the sidecar's documented root + SYS_ADMIN /
      SYS_PTRACE grant, so every sandbox pod would fail admission.
- [ ] `ResourceQuota` and `LimitRange` applied per tenant namespace.
- [ ] A namespaced NetworkPolicy denying traffic from other tenant namespaces,
      **and** you have verified your CNI actually enforces NetworkPolicy —
      several managed setups serve the API without enforcing it, in which case
      the policy silently fails open (see deploy/network-policy.yaml's own
      notes on which setups).
- [ ] Each tenant's control-plane has its own database and its own
      `SIDECAR_AUTH_TOKEN`.
- [ ] You've decided what happens when one tenant hits
      `BOXXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES`, because it affects the others.
- [ ] Your tenants know the boundary is a container, not a VM.
