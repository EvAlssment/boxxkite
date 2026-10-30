# BEP-0003: Cross-SDK error taxonomy

| Field | Value |
| --- | --- |
| Status | Implemented (retrospective) |
| Type | SDK-visible API contract |
| Related issue(s) | [#94](https://github.com/EvAlssment/boxxkite/issues/94), [#99](https://github.com/EvAlssment/boxxkite/issues/99) |
| Related implementation | [0b6f231](https://github.com/EvAlssment/boxxkite/commit/0b6f231), [ef1b080](https://github.com/EvAlssment/boxxkite/commit/ef1b080) |

## Summary

The four SDKs expose classifications based on one machine-readable taxonomy
and shared error envelope. The specification and prose documentation are
kept together. A parity test checks Python behavior and sets a static
coverage floor for the other SDKs; it does not establish behavioral parity
across all four implementations.

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

[`tests/test_sdk_error_parity.py`](../tests/test_sdk_error_parity.py) loads
and exercises the Python classifier against each specified code, suffix
rule, and the unknown-code 5xx fallback. For JavaScript, Go, and Rust, it
statically checks that their source mentions every specified code and suffix
rule and contains a 5xx fallback. Those checks catch missing coverage but do
not execute those classifiers or prove their behavior matches the
specification. Existing behavioral SDK tests include
[`sdk-python/tests/test_taxonomy_errors.py`](../sdk-python/tests/test_taxonomy_errors.py),
[`sdk-go/errors_test.go`](../sdk-go/errors_test.go), and
[`sdk-rust/tests/error_taxonomy_test.rs`](../sdk-rust/tests/error_taxonomy_test.rs).
No dedicated JavaScript error-taxonomy behavioral test is linked here. The
documentation also keeps known contract gaps explicit rather than presenting
them as solved.

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
