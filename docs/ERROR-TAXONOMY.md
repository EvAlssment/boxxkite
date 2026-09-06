# Boxxkite error taxonomy

> The machine-readable copy of this contract lives in
> [`specs/error-taxonomy.json`](../specs/error-taxonomy.json), and
> `tests/test_sdk_error_parity.py` fails the build when an SDK drifts from it.
> Change the spec and this document together: a code added to one and not the
> other is the drift both are meant to prevent.


Every error raised as `ApiError`, plus `HTTPException` (the rate limiter's
429s), uses this envelope. Two paths do not yet: `idempotency.py` and
`hosted_mcp.py` still emit the older two-field `{"code", "message"}` shape.
Treat those as known gaps rather than as the contract.

```json
{
  "error": {
    "code": "egress_denied",
    "message": "The sandbox cannot reach that destination.",
    "retryable": false,
    "remediation": "Allow the destination in the sandbox egress policy or use an approved proxy.",
    "details": {}
  }
}
```

`code` is stable and machine-readable. `message` is safe to show to a user.
`retryable` tells an SDK whether an automatic retry can help. `remediation`
is an actionable next step and may link to deployment documentation in a
future release. `details` remains endpoint-specific and optional.

Core named failure classes are `quota_exceeded` (the SDK maps all quota and
capacity codes to this class), `egress_denied`, `capability_denied`,
`readonly_filesystem`, `sandbox_not_ready`, `sandbox_crashed`, and
`service_unavailable`. Existing endpoint-specific codes remain valid and are
classified by their metadata.

Not every class above is currently emitted by the control-plane. The SDKs
define all of them so a code introduced later is classified without an SDK
release, but today `capability_denied` and `service_unavailable` exist only as
SDK-side classifications of other codes, and `sandbox_crashed` appears as a
diagnostics field rather than an HTTP error code. Only a handful of the
emitted codes have explicit `ERROR_TAXONOMY` entries; the rest fall back to
the default metadata, which is why `retryable` is derived from the status code
when an entry is absent.
