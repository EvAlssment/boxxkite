# Isolation model and state boundaries

boxxkite's normal Kubernetes path creates one pod per sandbox session. The
pod contains:

- a non-root `sandbox` container with all capabilities dropped, a read-only
  root filesystem, `allowPrivilegeEscalation: false`, and
  `seccompProfile: RuntimeDefault`;
- an authenticated `sidecar` container that performs filesystem operations,
  storage synchronization, and `nsenter`-based command execution; and
- a shared process namespace so the sidecar can enter the sandbox process and
  mount namespaces before dropping to the sandbox UID for agent code.

The sidecar's extra capabilities are a deliberate requirement of that
`nsenter` design. They are not inherited by agent code after the sidecar
enters the target namespaces and drops privileges. Sidecar authentication,
TLS configuration, path containment, and the network policy are independent
layers; none should be treated as a replacement for the others.

## Network boundary

The default Kubernetes NetworkPolicy denies sandbox ingress and egress. In
addition, the manager can run each `/exec` command in a fresh network
namespace. That per-exec option is configurable, so an operator must verify
the effective settings and that the cluster CNI enforces NetworkPolicy. A
policy manifest that the CNI ignores is not an isolation boundary.

## Filesystem snapshots are not process checkpoints

The implemented snapshot API flushes the session's workspace/output
filesystem state to blob storage and restores it into a *fresh* pod. It does
not preserve running processes, open connections, or in-memory interpreter
state. This boundary matches the existing pod lifecycle and storage backends:
the portable state is the filesystem, while a running process tree is tied to
the kernel, container runtime, node, and transient network state of its old
pod.

The project therefore uses filesystem snapshots for durable restore rather
than pretending that a pod can be resumed as a full VM or process image. A
separate full-state checkpoint method exists behind
`BOXXKITE_FULL_STATE_SNAPSHOT_ENABLED`, but it is an experimental,
Kubernetes-only forensic path. It requires an additional RBAC opt-in, returns
paths on the node's local disk, and is explicitly not a pause/resume or
restore mechanism.

## Runtime choices

The default pod spec omits `runtimeClassName`, which means the cluster's
ordinary Kubernetes container runtime (normally runc/containerd) remains the
default. boxxkite does not implement gVisor or Firecracker runtimes.

Kata Containers is a separate, opt-in experimental path selected with
`BOXXKITE_KATA_RUNTIME_CLASS_ENABLED=true` and a pre-existing `RuntimeClass`.
It is not the default and should not be described as a generally supported
replacement for the ordinary pod boundary. Review the comments in
[`deploy/pod-template-kata.yaml`](../../deploy/pod-template-kata.yaml) and
the [roadmap's limitation note](../../ROADMAP.md#not-currently-planned) for
its current verification status and trade-offs.

## What this model does not promise

Pods share the node kernel unless the operator selects an alternate runtime.
NetworkPolicy depends on the cluster CNI. The sidecar is a privileged helper
within the pod because its execution design requires namespace entry. These
are deliberate, documented boundaries; if a threat model requires a
hypervisor boundary by default, this runtime is not that system.
