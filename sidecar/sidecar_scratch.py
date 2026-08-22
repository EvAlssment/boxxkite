"""Session-scoped agent scratch memory (``/scratch/*``) -- GitHub issue #74.

A small key/value store for an agent's own bookkeeping (a running todo list,
paths already reviewed, a plan outline) that deliberately is **not** the
workspace filesystem:

- It never touches ``WORKSPACE_DIR``/``OUTPUTS_DIR``, so scratch keys never
  show up in ``file_glob``/``file_grep`` results, never get flushed to the
  session's storage prefix by ``sidecar_sync``, and never end up in a
  snapshot. Agent bookkeeping stops polluting the artifacts a human (or a
  downstream process) actually cares about.
- It is process-local memory, so it dies with the sidecar. There is no
  persistence layer to reason about, back up, or leak.

SECURITY: the store is wiped on ``/configure``, in the same
kill-everything-first block that resets processes/interpreters/browser/LSP
(``sidecar_sync.configure``). A recycled pod handing a new tenant the
previous tenant's scratch keys would be exactly the cross-tenant leak
docs/PROCESS-SESSIONS-DESIGN.md section 2(b) describes for kept-alive
background processes -- same requirement, same reason. Shutdown needs no
equivalent hook: the store is in-process, so it cannot outlive the process
the way a spawned child (or a tmux session) can.

The limits below exist so this stays a scratchpad rather than becoming a
second database product: values are opaque strings, there is no query
language, and the whole store is bounded so a looping agent can't grow the
sidecar's memory without bound.
"""

import logging

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()

# Bounded on three axes so no single dimension can be abused: many tiny keys,
# one enormous value, or many medium values that only blow up in aggregate.
SCRATCH_MAX_KEYS = 256
SCRATCH_MAX_KEY_LENGTH = 256
SCRATCH_MAX_VALUE_BYTES = 64 * 1024
SCRATCH_MAX_TOTAL_BYTES = 1024 * 1024

_scratch: dict[str, str] = {}


def clear_scratch_memory() -> int:
    """Drop every key. Called from ``/configure`` before a pod is handed to
    the next tenant -- see this module's docstring for why that's mandatory
    rather than merely tidy. Returns the number of keys dropped, so the
    caller can log a non-zero wipe."""
    count = len(_scratch)
    _scratch.clear()
    return count


def _total_bytes() -> int:
    return sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in _scratch.items())


@router.post("/scratch/set", response_model=main.ScratchSetResponse)
async def scratch_set(req: main.ScratchSetRequest):
    """Store `value` under `key`, replacing any existing value."""
    key = req.key
    if not key.strip():
        raise HTTPException(status_code=400, detail="key must not be empty")
    if len(key) > SCRATCH_MAX_KEY_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"key exceeds {SCRATCH_MAX_KEY_LENGTH} characters",
        )

    value_bytes = len(req.value.encode("utf-8"))
    if value_bytes > SCRATCH_MAX_VALUE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"value exceeds {SCRATCH_MAX_VALUE_BYTES} bytes ({value_bytes} given)",
        )

    if key not in _scratch and len(_scratch) >= SCRATCH_MAX_KEYS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"scratch memory holds the maximum {SCRATCH_MAX_KEYS} keys; "
                "delete a key before setting a new one"
            ),
        )

    # Measured against the store as it would be AFTER this write (excluding
    # the value being replaced), not before -- otherwise overwriting a large
    # key with a small one could be rejected by the size the old value used.
    projected = _total_bytes() - len(_scratch.get(key, "").encode("utf-8")) + value_bytes
    if key not in _scratch:
        projected += len(key.encode("utf-8"))
    if projected > SCRATCH_MAX_TOTAL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"scratch memory would exceed its {SCRATCH_MAX_TOTAL_BYTES}-byte total "
                "budget; delete keys to free space"
            ),
        )

    _scratch[key] = req.value
    return main.ScratchSetResponse(key=key, bytes_stored=value_bytes, keys=len(_scratch))


@router.get("/scratch/get", response_model=main.ScratchGetResponse)
async def scratch_get(key: str):
    """Read one key. A missing key is `found=false`, not a 404 -- an agent
    checking whether it has stored something yet is an expected, non-error
    control flow, and surfacing it as an HTTP error would make every
    first-run lookup look like a failure in logs and tool output."""
    if key in _scratch:
        return main.ScratchGetResponse(key=key, value=_scratch[key], found=True)
    return main.ScratchGetResponse(key=key, value=None, found=False)


@router.post("/scratch/delete", response_model=main.ScratchDeleteResponse)
async def scratch_delete(req: main.ScratchDeleteRequest):
    """Delete one key. Deleting a key that isn't there is a no-op, for the
    same reason `/scratch/get` doesn't 404."""
    existed = _scratch.pop(req.key, None) is not None
    return main.ScratchDeleteResponse(key=req.key, deleted=existed, keys=len(_scratch))


@router.get("/scratch/list", response_model=main.ScratchListResponse)
async def scratch_list():
    """List keys and their sizes -- deliberately not their values, so an
    agent (or an operator debugging one) can see the shape of what's stored
    without pulling the whole store back through the model's context."""
    entries = [
        main.ScratchEntry(key=k, bytes=len(v.encode("utf-8")))
        for k in sorted(_scratch)
        for v in (_scratch[k],)
    ]
    return main.ScratchListResponse(
        entries=entries,
        keys=len(entries),
        total_bytes=_total_bytes(),
        max_keys=SCRATCH_MAX_KEYS,
        max_total_bytes=SCRATCH_MAX_TOTAL_BYTES,
    )
