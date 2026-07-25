"""Shared orchestration: every adapter's LocatedSession goes through this
same, single path to become a live sandbox session. No sidecar/control-plane
code is touched by this package -- it composes boxkite_client.BoxkiteClient
primitives that already exist: create_sandbox, file_create, and takeover()
(the same PTY channel behind the human-takeover terminal; see
docs/AGENT-PTY-DESIGN.md and docs/SANDBOX-OBSERVABILITY-DESIGN.md, and
GitHub issues #130/#144 for why that channel is isolated from the sandboxed
workload's own namespace).

Credential handling -- CORRECTED after a security review found the first
version of this module unsafe (see docs/handoff-adapters.md's "Credential
handling" section for the full incident writeup): typing the credential's
raw value as PTY input is NOT safe, because `takeover()` connects through
the control-plane, not directly to the sidecar, and the control-plane
mirrors every byte typed on that channel into the `exec_log_entries` audit
table (`_relay_client_to_sidecar`/`_periodic_typed_snapshot_flush` in
control-plane/src/control_plane/routers/sandboxes.py) -- durably, in
plaintext, with no redaction on that path, readable later by any API key on
the account via `GET /v1/sandboxes/{id}/log` and fanned out to any
configured audit-log webhook. That logging is itself correct, intentional,
already-reviewed behavior for its actual purpose (human-takeover
accountability) -- it's just squarely incompatible with also typing a real
credential through the same channel.

The fix: the credential's raw value is pushed via `file_create` instead
(see `create_handoff_sandbox` below) -- control-plane's own audit entry for
`file_create` records only `{"path": ...}`, never `content`
(routers/sandboxes.py's `create_file_in_sandbox`), so the value itself
never reaches the audit log this way. Only a `cat <path>; rm -f <path>`
reference is typed into the PTY -- a path, never the secret -- so the
takeover-channel logging problem doesn't apply to it either (note: `;`, not
`&&` -- `rm -f` must run even if `cat` fails, or the credential file would
outlive the brief window it's meant to and sit on disk for the sandbox's
full lifetime instead). The file is written under /tmp (not /workspace,
which syncs to durable storage) and removed by the same typed line that
consumes it, so it only exists sandbox-side for the brief window between
the file_create call and the `cat`. `secrets.token_hex(16)` (128 bits of
CSPRNG output) makes that path unguessable. That window is a smaller,
already-accepted risk relative to durable, cross-account-readable audit
logging -- though it's worth being precise about what it's smaller than:
`deploy/pod-template.yaml` mounts this same /tmp `emptyDir` into both the
sandbox container (uid 1001) and the sidecar container (uid 0, holding
CAP_SYS_ADMIN/SETUID/SETGID/SYS_PTRACE), so the file is technically
visible to the sidecar's root the moment it's written -- not a new hole
(a compromised sidecar is already documented in SECURITY.md as equivalent
to full root within the pod), but distinct from the narrower "another
same-UID sandbox process" framing alone.

This still deliberately does NOT go through create_sandbox(secret_names=
...)/create_secret() -- that mechanism brokers a third-party API key to a
semi-trusted, prompt-injectable agent workload without it ever holding the
raw value, a different trust boundary than this, where the CLI itself is
the trusted operator's own tool and must hold the raw value to authenticate
at all, same as it would running locally. `HISTFILE` is still redirected
before the export so the token also never lands in shell history, on top
of (not instead of) the file-based fix above.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from boxkite_client import BoxkiteClient

from .core import LocatedSession

logger = logging.getLogger("boxkite_handoff")

DEFAULT_LIFETIME_MINUTES = 120
CREDENTIAL_FILE_TEMPLATE = "/tmp/.boxkite-handoff-credential-{suffix}"


@dataclass(frozen=True)
class HandoffResult:
    sandbox_id: str
    takeover_ws: Any


def create_handoff_sandbox(
    client: BoxkiteClient,
    session: LocatedSession,
    *,
    label: str | None = None,
    lifetime_minutes: int = DEFAULT_LIFETIME_MINUTES,
) -> HandoffResult:
    """Provision a fresh sandbox, push every file this session needs (plus
    the credential file -- see module docstring for why that's pushed as a
    file rather than typed directly), and open the takeover PTY with the
    resume command already typed and running. Returns the still-open
    websocket -- the caller (cli.py, or a test) owns its lifecycle from
    here."""
    sandbox = client.create_sandbox(
        label=label or f"handoff-{session.tool}-{session.session_id}",
        lifetime_minutes=lifetime_minutes,
    )
    sandbox_id = sandbox["session_id"]
    logger.info("Provisioned sandbox %s for %s session %s", sandbox_id, session.tool, session.session_id)

    credential_path = CREDENTIAL_FILE_TEMPLATE.format(suffix=secrets.token_hex(16))
    try:
        _push_session_files(client, sandbox_id, session)
        client.file_create(sandbox_id, credential_path, session.credential.value)
    finally:
        if session.cleanup is not None:
            session.cleanup()

    ws = client.takeover(sandbox_id)
    _start_resume(ws, session, credential_path)
    return HandoffResult(sandbox_id=sandbox_id, takeover_ws=ws)


def _push_session_files(client: BoxkiteClient, sandbox_id: str, session: LocatedSession) -> None:
    for f in session.files:
        content = f.local_path.read_text(encoding="utf-8")
        client.file_create(sandbox_id, f.sandbox_path, content)


def _start_resume(ws: Any, session: LocatedSession, credential_path: str) -> None:
    """Type the cwd change and resume command into the takeover shell,
    plus a credential export that reads the value from the file
    `create_handoff_sandbox` already pushed and deletes it immediately
    after -- never the literal value itself (see module docstring)."""
    _send_line(ws, "unset HISTFILE")
    _send_line(
        ws,
        f"export {session.credential.env_var}=\"$(cat {_shell_quote(credential_path)})\"; "
        f"rm -f {_shell_quote(credential_path)}",
    )
    _send_line(ws, f"cd {_shell_quote(session.workdir)}")
    _send_line(ws, session.resume_command)


def _send_line(ws: Any, line: str) -> None:
    ws.send((line + "\n").encode("utf-8"))


def _shell_quote(value: str) -> str:
    """Single-quote for POSIX sh/bash -- the only shell the takeover PTY
    ever spawns (see sidecar_pty.py's PTY_SHELL). Not a general-purpose
    shlex.quote substitute; deliberately minimal since this only ever
    quotes a boxkite-handoff-generated path, never arbitrary user input."""
    return "'" + value.replace("'", "'\\''") + "'"
