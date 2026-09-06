# BEP-0001: Filesystem snapshots and state boundaries

| Field | Value |
| --- | --- |
| Status | Implemented (retrospective) |
| Type | Runtime and API |
| Related issue(s) | [#25](https://github.com/EvAlssment/boxxkite/issues/25) |
| Related implementation | [5dc6a91](https://github.com/EvAlssment/boxxkite/commit/5dc6a91) |

## Summary

The durable snapshot boundary is the session's workspace and output
filesystem. Restoring a snapshot creates a fresh pod; it does not resume the
old process tree or its in-memory state.

## Motivation

Filesystem state is the portable part of a session across pod, node, runtime,
and network lifecycles. Running processes, open connections, and interpreter
state are tied to the old pod and its kernel context. Treating a filesystem
snapshot as a process checkpoint would promise a restore behavior the normal
pod lifecycle cannot provide.

## Decision

The implemented snapshot API flushes workspace/output state to blob storage
and restores it into a fresh pod. Snapshot create, list, get, restore, and
delete are exposed through the Python SDK and the `boxxkite snapshots` CLI.

Full-state checkpointing is a separate, opt-in experimental Kubernetes path.
It requires additional RBAC, returns paths on node-local storage, and is not a
pause/resume or restore mechanism.

## Documented trade-off

This design favors a portable filesystem restore boundary over preserving
process execution state. The repository documents the full-state path as an
experimental forensic capability rather than making it the default snapshot
contract.

## Evidence

- [Isolation model: filesystem snapshots are not process checkpoints](../docs/architecture/isolation-model.md#filesystem-snapshots-are-not-process-checkpoints)
- [Snapshot SDK and CLI implementation](https://github.com/EvAlssment/boxxkite/commit/5dc6a91)
- [Snapshot command](../src/boxxkite/cli/cmd_snapshots.py)

This BEP was written after the implementation shipped. It records the
repository's current decision and does not claim that the historical change
followed the BEP lifecycle.
