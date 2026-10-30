# Boxxkite Enhancement Proposals

Boxxkite Enhancement Proposals (BEPs) are design records for changes whose
impact should remain discoverable after the implementation PR is closed. A
BEP captures the problem, the proposed decision, the trade-offs, and the
status of the decision. Issues remain the place for questions and
discussion; PRs remain the place for implementation.

The first four BEPs below are retrospective worked examples. They document
decisions that are already present in the repository; they do not claim that
those historical changes went through this process.

## When a BEP is required

Open a BEP before implementation for:

- a new SDK-visible API, wire-format contract, or compatibility promise;
- an isolation, runtime, or security-model change;
- a control-plane authentication, account, or session-boundary change; or
- a change to the licensing or entitlement model.

A BEP is also appropriate when a change crosses package boundaries and its
design cannot be reviewed clearly in the implementation PR alone. A bug fix,
documentation correction, test-only change, dependency update, or internal
refactor with no externally observable behavior normally does not need one.
When the scope is unclear, open an issue and ask before choosing a number.

Security vulnerabilities must follow the private reporting path in
[`SECURITY.md`](../SECURITY.md), not a public BEP.

## Lifecycle

| Status | Meaning |
| --- | --- |
| **Draft** | The author is shaping the proposal. It is not a project commitment. |
| **Discussion** | The proposal is open for technical feedback in its PR and linked issue. The author may revise it. |
| **Accepted** | The maintainer has approved the direction. Acceptance is not a promise that implementation will happen immediately. |
| **Rejected** | The maintainer has decided not to adopt the proposal. Keep the record and rationale; do not reuse its number. |
| **Implemented** | The accepted decision has shipped. Link the implementation PR, commit, or release from the BEP. |

The normal paths are `Draft → Discussion → Accepted → Implemented` and
`Draft → Discussion → Rejected`. A proposal may be withdrawn during Draft or
Discussion; record that in the document rather than reusing its number. An
implementation PR should update an Accepted BEP to Implemented, or explain
why the decision changed and link a follow-up BEP.

## Numbering and filenames

Use the next unused four-digit number, starting at `0001`, and never reuse a
number. Name the file `NNNN-short-slug.md` and use the matching identifier in
the title:

```text
beps/0005-add-example.md
# BEP-0005: Add example
```

Copy [`bep-template.md`](bep-template.md), keep the status and reference
links current, and add the proposal to the index below. A BEP is one decision
record; split unrelated decisions into separate proposals.

## How to contribute

1. Check this index, [`ROADMAP.md`](../ROADMAP.md), and open issues for
   existing context.
2. Open an issue when the direction needs early discussion, then create a
   numbered BEP from the template with status `Draft`.
3. Open the BEP PR early. Move it to `Discussion` when feedback is ready,
   and record the accepted or rejected decision in the document.
4. Link the BEP from the implementation PR. If the implementation differs
   materially, update the BEP before or alongside that PR.
5. For an implemented decision, add links to the shipped code, documentation,
   release, or PR so the record can be checked against the repository.

The maintainer owns the final status decision under the current governance
model. The process records technical reasoning; it does not change review,
security-reporting, or release authority.

## Index

These retrospective examples are the initial BEP set:

| BEP | Status | Decision |
| --- | --- | --- |
| [BEP-0001](0001-filesystem-snapshots-and-state-boundaries.md) | Implemented | Filesystem snapshots are the portable restore boundary; full-state checkpoints remain experimental. |
| [BEP-0002](0002-fleet-capacity-and-warm-pool-observability.md) | Implemented | Warm-pool capacity is operator-configurable and its demand signals are observable. |
| [BEP-0003](0003-cross-sdk-error-taxonomy.md) | Implemented | SDKs share a machine-readable error taxonomy checked for parity. |
| [BEP-0004](0004-default-deny-network-boundary.md) | Implemented | Sandbox network access is denied by default, with explicit deployment-scoped exceptions. |
