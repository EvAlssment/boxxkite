# Hosted control-plane deployment path

The control-plane is an optional, separate API service in front of
`SandboxManager`. It provides account and API-key authentication, session
ownership, and the HTTP surface used by the SDKs. It does not remove the
runtime requirement: actual sandbox execution still needs a configured
Compose or Kubernetes runtime.

For a complete local walkthrough:

1. Start the local sandbox with `boxxkite up`.
2. Set up the control-plane's SQLite-backed development environment.
3. Run the signup → API key → sandbox → exec → file → list → teardown flow.

The commands and environment variables are maintained in
[`examples/basic/hosted_control_plane/README.md`](../../examples/basic/hosted_control_plane/README.md).
Keep that example's distinction in mind: a local control-plane can exercise
HTTP and authentication without a Kubernetes cluster, but it cannot create
real Kubernetes sandbox pods until a cluster is configured.

For clients, see the package-level references for [Python](../../sdk-python/),
[JavaScript/TypeScript](../../sdk-js/), [Go](../../sdk-go/), and
[Rust](../../sdk-rust/).
