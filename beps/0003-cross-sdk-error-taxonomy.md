# BEP-0003: Cross-SDK error taxonomy

| Field | Value |
| --- | --- |
| Status | Implemented (retrospective) |
| Type | SDK-visible API contract |
| Related issue(s) | [#94](https://github.com/EvAlssment/boxxkite/issues/94), [#99](https://github.com/EvAlssment/boxxkite/issues/99) |
| Related implementation | [0b6f231](https://github.com/EvAlssment/boxxkite/commit/0b6f231), [ef1b080](https://github.com/EvAlssment/boxxkite/commit/ef1b080) |

## Summary

The four SDKs classify API failures against one machine-readable taxonomy and
shared error envelope. The specification and prose documentation are kept
together, and a parity test detects drift between SDK implementations.

## Motivation

An application using more than one SDK should not need a different error
classification model for the same control-plane response. Stable machine-
readable codes, retryability metadata, and remediation text make failures
usable by both callers and tooling.

## Decision

[`specs/error-taxonomy.json`](../specs/error-taxonomy.json) is the
machine-readable source of truth, with [`docs/ERROR-TAXONOMY.md`](../docs/ERROR-TAXONOMY.md)
as its prose counterpart. The shared envelope includes `code`, `message`,
`retryable`, `remediation`, and optional `details` fields. The taxonomy
defines named classes including quota, egress, capability, read-only
filesystem, readiness, crash, and service-unavailable failures.

`tests/test_sdk_error_parity.py` checks the Python, JavaScript, Go, and Rust
SDK classifications against the specification. The documentation also keeps
known contract gaps explicit rather than presenting them as solved.

## Documented trade-offs

The taxonomy allows SDKs to classify some future or endpoint-specific codes
without waiting for a new SDK release, but not every named class is emitted by
the control-plane today. The documented fallback behavior and known gaps are
part of the current contract.

## Evidence

- [Machine-readable taxonomy](../specs/error-taxonomy.json)
- [Prose taxonomy](../docs/ERROR-TAXONOMY.md)
- [Parity test](../tests/test_sdk_error_parity.py)
- [Initial cross-SDK specification and parity check](https://github.com/EvAlssment/boxxkite/commit/0b6f231)
- [Rust SDK classification follow-up](https://github.com/EvAlssment/boxxkite/commit/ef1b080)

This BEP was written after the implementation shipped. It records the
repository's current decision and does not claim that the historical changes
followed the BEP lifecycle.
