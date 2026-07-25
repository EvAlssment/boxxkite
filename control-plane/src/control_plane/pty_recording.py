"""Full-duplex PTY session recording for human takeover (GitHub issue #133).

`docs/SANDBOX-OBSERVABILITY-DESIGN.md`'s original takeover audit trail
(`routers/sandboxes.py`'s `_flush_typed_snapshot`/`TAKEOVER_TYPED_SNAPSHOT_MAX_LENGTH`)
only ever captured *typed input*, periodically, into the `exec_log_entries`
table. This module adds a full-duplex, timestamped, replayable recording of
both directions of PTY traffic (what the human typed AND what the shell
printed back), serialized in the asciicast v2 format
(https://docs.asciinema.org/manual/asciicast/v2/) used by asciinema/ttyd-style
terminal-session players, so a completed recording can be handed straight to
an existing player rather than needing a bespoke one. Both halves of #133
are now implemented: this module is the "recording" half; the "replay"
half is `routers/sandboxes.py`'s `GET .../takeover-recordings/{entry_id}`
(reads `storage.download_bytes`, streams the asciicast bytes back out) plus
the dashboard's `RecordingPlayer`/`TakeoverRecordingsPanel` components
(`site/components/dashboard/`), which hand the fetched recording straight
to the real `asciinema-player` npm library -- the "existing player" this
module's format choice was made for.

Recording *ownership* is scoped per `session_id`, not per WebSocket
connection (`routers/sandboxes.py`'s `_acquire_takeover_recording`/
`_release_takeover_recording`) -- see `docs/SHARED-TAKEOVER-SESSIONS-DESIGN.md`
section 6 for why a naive per-connection `PtyRecordingBuffer` produced N
redundant recordings once concurrent multi-operator attach (issue #132) is
considered: tmux already lets multiple WS connections attach to one
session, so without this fix, N concurrently-attached connections would
each instantiate and upload their own overlapping recording.

**Why a separate blob, not a bigger `exec_log_entries` row:** a full-duplex
byte stream over a human-length terminal session is routinely megabytes,
nothing like the bounded, already-capped `output_truncated` column
(`SANDBOX_FILE_CONTENT_MAX_LENGTH`) every other audit row uses. Per the
issue's own scoping, the recording is uploaded to the control plane's
existing object-storage abstraction (`storage_client.py`'s
`SnapshotStorageClient`, the same S3/Azure client `docs/SNAPSHOT-DESIGN.md`
already uses for filesystem snapshots) under its own
`takeover-recordings/{account_id}/{session_id}/...` prefix -- a sibling use
of that client, not a fork of it. Only a small pointer (the storage key,
byte count, and whether it was truncated) is written into the existing
`exec_log_entries` row's `detail` JSON (see `takeover_end`'s handling in
`routers/sandboxes.py`), keeping the DB-side audit trail exactly as bounded
as it already was.

**Redaction -- read this before assuming it's a complete mitigation:**
`docs/SECRETS-DESIGN.md`'s `_scrub_secret_values` (sidecar/sidecar_secrets.py)
is an EXACT-VALUE scrub: the sidecar knows the literal secret values it just
substituted into one specific `/http-request` call and can strip those exact
strings back out. That mechanism is not reachable from this code path for two
structural reasons, not just a wiring gap:
  1. It lives in the sidecar process (inside the sandbox pod), not the
     control plane -- this recorder runs in the control-plane process, which
     never sees which secret grants/values exist for a session at all (by
     design; see SECURITY.md's "New trust boundary: the secrets broker").
  2. Even if it were reachable, it only knows about secrets substituted via
     the `/http-request` broker. A human-takeover PTY session doesn't go
     through that broker at all -- a human can `cat .env`, `echo
     $SOME_TOKEN`, or type a credential directly into a prompt, none of
     which the sidecar's secret-substitution bookkeeping ever sees.
  Given that, this module reuses `boxkite.tools.bash_tool.sanitize_output`'s
  SHAPE-based heuristic patterns instead (the same "high false-negative
  rate by design" patterns `bash_tool.py`'s own comments already disclose
  for ordinary command output) -- pattern-matching common credential shapes
  (AWS keys, JWTs, GitHub/GitLab tokens, `password=`/`api_key=`-style
  assignments, PEM private keys) rather than exact known values. This is a
  strictly weaker guarantee than an exact-value scrub and is disclosed as
  such, not papered over:
    - A secret in a shape none of these patterns recognize (e.g. an
      internal, non-vendor-prefixed opaque token) will NOT be redacted.
    - Redaction is applied independently to each captured chunk (each
      WebSocket frame / each PTY read), so a secret whose bytes happen to
      be split across two chunks by network fragmentation will not be
      caught either -- this mirrors the same "one call/response at a time"
      scope `_scrub_secret_values` itself has, just without that
      function's exact-value precision within that scope.
  Treat this as the same class of defense-in-depth backstop
  `SENSITIVE_OUTPUT_PATTERNS` already is for bash_tool output, not a
  guarantee that no secret can ever reach the durable recording.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from boxkite.tools.bash_tool import sanitize_output

logger = logging.getLogger(__name__)

ASCIICAST_VERSION = 2

# Object-storage key prefix for recordings, namespaced by account_id first
# (same "account-scoped prefix, not just a DB-layer filter" discipline
# snapshots.py's `_snapshot_storage_prefix` already follows) so a bug in the
# DB-layer authorization check isn't the only thing standing between two
# tenants' recorded PTY sessions.
TAKEOVER_RECORDING_STORAGE_PREFIX = "takeover-recordings"

# Same size-cap philosophy as TAKEOVER_TYPED_SNAPSHOT_MAX_LENGTH -- a
# recording is still caller-controlled-duration data ending up in durable
# storage. Bounds worst case to a single-digit-MB blob per session rather
# than growing unbounded for a takeover session left open for hours.
TAKEOVER_RECORDING_MAX_BYTES = 8 * 1024 * 1024

_TRUNCATION_NOTICE = "\r\n\x1b[31m[boxkite: recording truncated -- max size reached]\x1b[0m\r\n"


def redact_pty_bytes(data: bytes) -> str:
    """Decode one chunk of raw PTY bytes and apply the shape-based
    heuristic redaction pass described in this module's docstring.

    `errors="replace"` rather than a stricter decode: PTY bytes are not
    guaranteed to be valid UTF-8 at an arbitrary chunk boundary (a
    multi-byte UTF-8 character split across two reads, raw terminal control
    sequences, etc.) -- the recording is a best-effort human-readable
    replay artifact, not a byte-exact retransmission channel, so a lossy
    decode here is an accepted tradeoff, not a bug.
    """
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    return sanitize_output(text)


class PtyRecordingBuffer:
    """Accumulates one takeover session's full-duplex PTY events in
    asciicast v2 shape, in memory, for later serialization + upload.

    Timestamps are wall-clock-relative seconds since this buffer was
    constructed (`time.monotonic()`-based, so immune to system clock
    adjustments mid-session), matching asciicast v2's own `[time, "o"|"i",
    data]` event shape.
    """

    def __init__(self, *, max_bytes: int = TAKEOVER_RECORDING_MAX_BYTES) -> None:
        self._start_monotonic = time.monotonic()
        self.started_at_epoch_ms = int(time.time() * 1000)
        self._max_bytes = max_bytes
        self._events: list[tuple[float, str, str]] = []
        self._recorded_bytes = 0
        self.truncated = False

    @property
    def event_count(self) -> int:
        return len(self._events)

    def record(self, direction: str, data: bytes) -> None:
        """Record one chunk of raw bytes flowing in `direction`
        ("o" for sidecar->human output, "i" for human->sidecar input).

        A no-op once `truncated` is already set, or for an empty chunk --
        mirrors `_flush_typed_snapshot`'s "nothing new, nothing to do"
        short-circuit.
        """
        if direction not in ("o", "i"):
            raise ValueError(f"direction must be 'o' or 'i', got {direction!r}")
        if not data or self.truncated:
            return

        text = redact_pty_bytes(data)
        encoded_len = len(text.encode("utf-8"))
        if self._recorded_bytes + encoded_len > self._max_bytes:
            self.truncated = True
            return

        self._recorded_bytes += encoded_len
        elapsed = time.monotonic() - self._start_monotonic
        self._events.append((elapsed, direction, text))

    def serialize(self, *, session_id: str) -> bytes:
        """Render the accumulated events as an asciicast v2 file: one JSON
        header line, then one `[time, direction, data]` JSON array per
        line. https://docs.asciinema.org/manual/asciicast/v2/
        """
        header = {
            "version": ASCIICAST_VERSION,
            "width": 80,
            "height": 24,
            "timestamp": self.started_at_epoch_ms // 1000,
            "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
            "title": f"boxkite takeover session {session_id}",
        }
        lines = [json.dumps(header, separators=(",", ":"))]
        for elapsed, direction, text in self._events:
            lines.append(json.dumps([round(elapsed, 6), direction, text], separators=(",", ":")))
        if self.truncated:
            final_elapsed = self._events[-1][0] if self._events else 0.0
            lines.append(json.dumps([round(final_elapsed, 6), "o", _TRUNCATION_NOTICE], separators=(",", ":")))
        return ("\n".join(lines) + "\n").encode("utf-8")


class _RecordingStorage(Protocol):
    async def upload_bytes(self, *, key: str, data: bytes, content_type: str = ...) -> None: ...


def takeover_recording_storage_key(*, account_id: str, session_id: str, recording: PtyRecordingBuffer) -> str:
    return f"{TAKEOVER_RECORDING_STORAGE_PREFIX}/{account_id}/{session_id}/{recording.started_at_epoch_ms}.cast"


async def finalize_takeover_recording(
    recording: PtyRecordingBuffer,
    *,
    storage: _RecordingStorage,
    account_id: str,
    session_id: str,
) -> dict | None:
    """Serialize and upload one completed takeover session's recording.

    Returns `{"storage_key", "bytes", "truncated"}` on success, for the
    caller to fold into the `takeover_end` audit row's `detail`. Returns
    `None` if there was nothing recorded, or if the upload itself failed --
    best-effort, same "never fail the caller's own teardown over this"
    posture as `_fire_audit_log_webhook_event`, since a storage outage must
    not prevent a takeover session from closing cleanly.

    Uploads even with zero *events* if `recording.truncated` is set -- that
    combination means the very first chunk recorded already exceeded
    `max_bytes` on its own, which is still worth surfacing (a truncated,
    near-empty recording) rather than silently treated the same as "nothing
    was ever typed or printed."
    """
    if recording.event_count == 0 and not recording.truncated:
        return None

    blob = recording.serialize(session_id=session_id)
    key = takeover_recording_storage_key(account_id=account_id, session_id=session_id, recording=recording)
    try:
        await storage.upload_bytes(key=key, data=blob, content_type="application/x-asciicast")
    except Exception as exc:
        logger.error(
            "[takeover-recording] Failed to upload recording for session %s: %s", session_id, exc
        )
        return None

    return {"storage_key": key, "bytes": len(blob), "truncated": recording.truncated}
