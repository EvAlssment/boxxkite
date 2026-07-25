# Security Policy

boxkite executes arbitrary, agent-generated code. A security bug here is not
a normal bug — it's a potential sandbox escape or credential leak for every
self-hosted deployment. **We treat security reports as the highest-priority
category of issue in this project**, and we ask you to help us keep it that
way by reporting privately, not in a public issue.

## Reporting a vulnerability

Please use **GitHub's private vulnerability reporting** for this repository:

1. Go to the **Security** tab of this repository.
2. Click **Report a vulnerability**.
3. Describe the issue, affected version(s), and — if you have one — a
   reproduction (a minimal `bash_tool`/`file_create` payload, a pod spec
   diff, a network policy bypass, etc.).

This opens a private advisory visible only to maintainers and you, with its
own discussion thread, so we can coordinate a fix and disclosure timeline
without exposing the issue while it's live.

If you cannot use GitHub's private reporting for some reason, open a regular
issue that says only "security issue, please contact me privately" with no
technical details, and a maintainer will reach out for a private channel.

## What's in scope

- Sidecar HTTP API authentication bypass: anything that lets a request reach
  `/exec`, `/file-create`, `/str-replace`, `/configure`, `/tool-call`,
  `/process/*`, or any other sidecar route without a valid
  `X-Sidecar-Auth-Token` (see `src/boxkite/sidecar_auth.py`), or that
  recovers/predicts another pod's token.
- Cross-tenant leakage of a background process (`/process/*`) across pod
  recycling: a process (or its buffered output) started by one tenant's
  session being observable, or still running, once the pod is claimed by a
  different tenant. The mandatory mitigation is `_kill_all_processes()`,
  called both from `/configure` (sidecar-side, on every call) and from
  `SandboxManager.destroy_session()`/`_recycle_pod_via_k8s()` (manager-side,
  before the `/configure` wipe) — a gap in either path that still lets a
  process or its output cross a tenant boundary is in scope.
- Session/tenant impersonation via `/tool-call`'s `session_id` handling
  (see `sidecar/main.py` — the field must always be ignored in favor of the
  sidecar's own `current_session["session_id"]`).
- Sandbox escape: anything that lets code running in the `sandbox` container
  read/write outside its intended mounts, escalate privileges, or reach the
  `sidecar` container's credentials.
- Network isolation bypass: anything that lets sandboxed code reach the
  internet, the Kubernetes API, cloud metadata endpoints (IMDS), or other
  pods despite `deploy/network-policy.yaml`.
- Secret exposure: sidecar storage credentials (S3/Azure) leaking into
  sandboxed process environments, logs, or tool output (see the redaction
  patterns in `src/boxkite/tools/bash_tool.py` — gaps there are in scope).
- RBAC over-permissioning in `deploy/rbac.yaml` beyond what
  `SandboxManager`/`WarmPoolManager` actually need.
- Path traversal in the sidecar's file endpoints (`/file-create`, `/view`,
  `/str-replace`, `/present-files`, `/ls`, `/glob`, `/grep`).
- Command whitelist bypass in `src/boxkite/command_whitelist.py` for agents
  configured with `sandbox_allowed_commands`.
- Manager-to-sidecar TLS bypass: anything that lets the manager's HTTP
  client accept a cert other than the one pinned for that specific pod
  (`src/boxkite/tls.py`'s `build_pinned_ssl_context`), or that lets a pod's
  sidecar serve plaintext HTTP while `SIDECAR_TLS_DISABLED` is unset/false.

## What's out of scope

- Vulnerabilities that require the operator to have already misconfigured
  RBAC to grant the sandbox pods themselves K8s API access (the manifests
  ship with `automountServiceAccountToken: false` specifically to prevent
  this — reports that this protection is absent if you've removed it
  yourself aren't actionable).
- Denial-of-service from a user intentionally exhausting their own sandbox's
  resource limits (`deploy/pod-template.yaml` requests/limits) — that's the
  isolation working as intended, not a vulnerability.
- Vulnerabilities in upstream dependencies without a boxkite-specific
  exploitation path — please report those upstream, though we'll still want
  to know if a boxkite default makes an upstream CVE reachable.

## Verifying released images

`.github/workflows/publish-images.yml` (GitHub issue #227) generates an SPDX
SBOM and signs both the image and its SBOM keylessly via
[cosign](https://github.com/sigstore/cosign)/Sigstore for every image it
publishes, starting with the first release built after this landed
(`v0.1.0`'s four GHCR images predate it and are unsigned). Keyless signing
means Fulcio issues a short-lived certificate bound to that specific
workflow run's GitHub Actions OIDC identity — no long-lived private key is
generated, stored, or exposed to rotate — and Rekor logs the signature to a
public, append-only transparency log.

```bash
# Verify the image signature
cosign verify \
  --certificate-identity-regexp "^https://github\.com/EvAlssment/boxkite/\.github/workflows/publish-images\.yml@refs/tags/.+$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/evalssment/boxkite-sandbox:<tag>

# Verify the attached SBOM attestation and print the SBOM itself
cosign verify-attestation \
  --type spdxjson \
  --certificate-identity-regexp "^https://github\.com/EvAlssment/boxkite/\.github/workflows/publish-images\.yml@refs/tags/.+$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/evalssment/boxkite-sandbox:<tag>
```

Swap `boxkite-sandbox` for `boxkite-sandbox-minimal`, `boxkite-sidecar`, or
`boxkite-control-plane` for the other three images. What a successful
`cosign verify` does and does not protect against:

- **Does protect against**: a compromised GHCR account, or a
  man-in-the-middle/registry-mirror substitution, serving you an image that
  was not actually built by this repo's own `publish-images.yml` workflow
  from the source at the tagged release.
- **Does not protect against**: a compromise of the GitHub Actions runner
  environment itself while the real workflow is executing (a signed
  malicious build would still verify — signing attests to *provenance*, not
  to the absence of vulnerabilities in what was built), or vulnerabilities
  disclosed by the SBOM itself (the SBOM is a dependency inventory for your
  own scanning, not a guarantee of a clean scan).

## Known, currently-unmitigated limitations

Disclosed here rather than left for you to discover:

- **`deploy/sandbox.Dockerfile`'s pandoc and Chrome-for-Testing downloads**
  verify `sha256sum -c` against pinned digests, and those pinned digests
  have been independently cross-checked against a second source (2026-07-12,
  closing issue #75; see `deploy/pinned-checksums-verification.json` for the
  dated record and `scripts/verify-pinned-checksums.sh` for the repeatable
  check) — pandoc's cross-check is GitHub's own server-computed release
  digest, Chrome for Testing's is GCS's server-reported md5 at upload time,
  both independent of whoever originally self-computed the Dockerfile pin.
  This narrows, but does not eliminate, the residual risk: a same-algorithm,
  upstream-signed checksum manifest still does not exist for either
  dependency, so re-derive the cross-check every time
  `PANDOC_VERSION`/`CHROME_FOR_TESTING_VERSION` is bumped.
- Command-name allowlisting (`src/boxkite/command_whitelist.py`) is a
  guardrail against accidental/unexpected commands, not a sandbox-escape
  boundary — allowing a general-purpose interpreter (`python3`, `bash`,
  `node`) through it still permits arbitrary code to run once it starts.

## Response

We aim to acknowledge reports within 5 business days and to have a fix or
mitigation plan within 30 days for confirmed issues, faster for anything
actively exploitable. Given this project's size, please be patient — but
security reports get priority over everything else in the backlog.
