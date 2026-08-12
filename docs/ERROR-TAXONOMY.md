# Boxxkite error taxonomy

Every handled control-plane error uses this envelope:

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
