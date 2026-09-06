# Raw sidecar HTTP API

For a runnable walkthrough, use [`examples/basic/raw_api/`](../../examples/basic/raw_api/).
That example calls the local Compose sidecar directly, without the Python
package, LangChain, or another agent framework.

The example documents the current routes and request/response shapes from
`sidecar/main.py`. Every route except `/health` requires the
`X-Sidecar-Auth-Token` header. The local `boxxkite up` command writes the
token to `~/.boxxkite/local.env`.

This path is a single local sidecar. For account-scoped sessions and API-key
authentication, use [the hosted control-plane guide](hosted-control-plane.md).
