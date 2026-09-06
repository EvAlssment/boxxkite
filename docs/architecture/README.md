# Architecture

These pages describe the implementation in this repository, including the
limits that matter when choosing a threat model.

- [Isolation model](isolation-model.md) — pod boundary, sidecar role,
  network controls, state reset, snapshots, and alternate runtimes.
- [Security policy](../../SECURITY.md) — reporting path, in-scope classes,
  and the complete list of known limitations.
- [Self-hosted multi-tenancy](../guides/self-hosted-multi-tenancy.md) —
  account scoping versus infrastructure tenancy.

The default execution boundary is a Kubernetes pod with a sandbox container
and an authenticated sidecar. It is not a claim that arbitrary code becomes
safe in every operator configuration; cluster policy and CNI enforcement are
part of the deployment's security posture.
