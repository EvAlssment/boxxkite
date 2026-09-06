# boxxkite error taxonomy

Every error raised as `ApiError`, plus the rate limiter's `HTTPException`
429s, uses this envelope. `idempotency.py` and `hosted_mcp.py` still emit the
older two-field `{"code", "message"}` shape; treat those as known contract
gaps.

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
is an actionable next step, and `details` is endpoint-specific and optional.

Named failure classes include `quota_exceeded`, `egress_denied`,
`capability_denied`, `readonly_filesystem`, `sandbox_not_ready`,
`sandbox_crashed`, and `service_unavailable`. The SDKs define these classes
so a code introduced later can be classified without an SDK release. Not
every class is emitted by the control-plane today: for example,
`capability_denied` and `service_unavailable` can be SDK-side classifications,
while `sandbox_crashed` is currently a diagnostics field rather than an HTTP
error code.

Only some emitted codes have explicit `ERROR_TAXONOMY` entries. When an entry
is absent, the default metadata derives retryability from the HTTP status.
