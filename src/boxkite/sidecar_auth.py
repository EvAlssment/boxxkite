"""Shared-secret authentication between SandboxManager/WarmPoolManager and the sidecar.

SECURITY CONTEXT (see SECURITY.md and README "Security" section):

The sidecar's HTTP API (`sidecar/main.py`) has no authentication of its own —
historically it relied entirely on network isolation (a K8s NetworkPolicy, or
the docker-compose internal network) to keep it unreachable by anything other
than the manager. That assumption does not hold universally:

- NetworkPolicy *enforcement* is CNI-dependent. Several common managed K8s
  setups (GKE Autopilot without Dataplane V2 explicitly enabled, EKS's
  default VPC CNI) do not enforce NetworkPolicy at all unless additional
  configuration is applied.
- Even where NetworkPolicy is enforced, an egress rule scoped too broadly on
  the sandbox pod (e.g. allow-all-443 for storage) also governs what can
  reach the sidecar's own ingress port, because both containers in a pod
  share one network namespace / one set of NetworkPolicy egress rules.
- `deploy/local-kind/k8s-resources.yaml` ships with **no** NetworkPolicy at
  all (kind doesn't enforce one), so a local dev cluster has zero network
  isolation for the sidecar unless the operator layers
  `deploy/network-policy.yaml` on top themselves.

This module provides a second, independent layer (defense in depth, not a
replacement for NetworkPolicy): a random secret generated **per pod** at
pod-creation time — never a static, repo-wide value — that is:

1. Stored in a per-pod Kubernetes Secret (name derived deterministically from
   the pod name via `sidecar_auth_secret_name()` below), and injected into
   the sidecar container as the `SIDECAR_AUTH_TOKEN` env var via
   `valueFrom.secretKeyRef` — never as a literal env value.
2. Recoverable by any SandboxManager/WarmPoolManager process (including ones
   that didn't create the pod themselves — e.g. after a backend restart, or
   a different worker process claiming a warm pod) via `read_namespaced_secret`
   on that deterministic name, using dedicated `secrets` RBAC
   (`get`/`create`/`delete`, deliberately NOT `list`/`watch` — see
   deploy/rbac.yaml).
3. Sent back by the manager on every HTTP call to that pod's sidecar via the
   `X-Sidecar-Auth-Token` header.

SECURITY NOTE (fixes a real finding, not a hypothetical): this token used to
live as a **plaintext pod annotation**
(`sandbox.boxkite.dev/sidecar-auth-token`), readable by anything holding mere
`pods: get/list` RBAC — the same permission the manager already needs for
routine pod lifecycle management, and a much lower bar than a credential
compromise should need to clear to read every live tenant's sidecar secret
and mount a cross-tenant sandbox takeover. A Kubernetes Secret requires a
*separate* `secrets` RBAC grant the manager's ServiceAccount now holds
narrowly (no `list`/`watch`, since the deterministic naming means the
manager never needs to enumerate secrets, only fetch one it already knows
the name of) — so a credential leak scoped to `pods` RBAC alone (e.g. a
monitoring tool, or a narrower future role) can no longer recover tokens.
This does not eliminate the control-plane's own ServiceAccount as a target
— the remaining, larger-scope follow-up is Workload Identity Federation or
mTLS instead of any shared-secret-recoverable-via-RBAC design at all — it
narrows the blast radius of a *different, lower-privilege* credential
leaking, which the annotation-based design didn't distinguish at all.

The sidecar (`sidecar/main.py`) is a separately deployed service (its own
Dockerfile/requirements, no dependency on this package), so it intentionally
does NOT import this module — it re-declares the same env var name and header
name as local constants. `tests/test_sidecar_auth_parity.py` asserts the two
definitions never drift apart.
"""

from __future__ import annotations

import secrets

# Name of the env var the sidecar reads its own secret from. Must match the
# SIDECAR_AUTH_TOKEN_ENV constant duplicated in sidecar/main.py.
SIDECAR_AUTH_TOKEN_ENV = "SIDECAR_AUTH_TOKEN"

# HTTP header the manager sends on every sidecar request. Must match the
# SIDECAR_AUTH_HEADER constant duplicated in sidecar/main.py.
SIDECAR_AUTH_HEADER = "X-Sidecar-Auth-Token"

# Key within the per-pod Secret's `data`/`string_data` the token is stored
# under (the Secret's own name is derived from the pod name -- see
# sidecar_auth_secret_name() below).
SIDECAR_AUTH_SECRET_KEY = "token"


def sidecar_auth_secret_name(pod_name: str) -> str:
    """Deterministic Secret name for a pod's sidecar auth token.

    Must be a pure function of `pod_name` alone (no session_id, no
    manager-instance-local state) -- token recovery routinely happens from a
    process that never created the pod (WarmPoolManager creating it,
    SandboxManager later claiming it; or any manager process after a
    restart), and in every one of those paths the only thing reliably in
    hand is the pod's own name from a list/read_namespaced_pod response.
    """
    return f"{pod_name}-sidecar-auth"

# deploy/pod-template.yaml's literal SIDECAR_AUTH_TOKEN value -- that file is
# a static reference manifest, never read by SandboxManager/WarmPoolManager
# (which set a real per-pod token programmatically instead), but a
# self-hoster who copies it verbatim would otherwise get a plausible-looking
# value that "just works" as a shared, guessable, effectively-no-auth
# secret across every pod created from the unmodified template. The sidecar
# treats this exact string the same as an unset token (fail-closed 503) --
# see sidecar/main.py's duplicated constant and enforce_sidecar_auth().
# Must match tests/test_pod_template_parity.py and
# tests/test_sidecar_auth_parity.py.
SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER = "CHANGEME-generate-a-random-per-pod-secret-see-comment-above"


def generate_sidecar_auth_token() -> str:
    """Generate a fresh, unguessable per-pod secret (256 bits of entropy)."""
    return secrets.token_urlsafe(32)
