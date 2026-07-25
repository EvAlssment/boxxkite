"""opencode adapter -- see docs/handoff-adapters.md for the shared contract.

opencode has a genuine client/server split (`opencode serve` for a headless
HTTP backend, `opencode attach <url>` to point a TUI client at one already
running -- verified directly against opencode's own docs,
packages/web/src/content/docs/{cli,server}.mdx in
github.com/anomalyco/opencode @ dev, 2026-07-20). This adapter deliberately
does NOT use `--attach`: attaching only helps if the local process stays
running and network-reachable, which defeats the point of a *handoff* into
an independent, freshly provisioned sandbox that should keep working after
the laptop closes.

It also does NOT copy opencode's raw on-disk session storage the way the
Claude Code/Codex adapters copy JSONL files. As of the version this was
verified against, opencode's session data lives in a SQL(ite) database
(`SessionTable`/`MessageTable`/`PartTable`,
packages/opencode/src/storage/schema.ts -- `opencode db path` prints its
location) rather than flat files, so dropping a raw DB file into a sandbox
running a different opencode build risks a schema mismatch the tool has no
defense against.

Instead, this adapter uses opencode's own supported portability boundary:
`opencode export <sessionID>` prints the full `{info, messages}` JSON for a
session to stdout (packages/opencode/src/cli/cmd/export.ts), and `opencode
import <file>` reads that JSON back and upserts it into whatever local
database it's run against (packages/opencode/src/cli/cmd/import.ts). That
JSON becomes the `SessionFile` pushed into the sandbox, and the resume
command is `opencode import <path> && opencode --session <id>` -- the same
"push a file, type a resume command" shape every other adapter uses, just
built on opencode's own export format instead of a raw internal one. Like
Codex's `-c experimental_resume=<path>`, `opencode import` takes an
arbitrary path, so no cwd-encoded path mapping is needed.

Credential: opencode supports supplying its entire auth.json content via one
env var, `OPENCODE_AUTH_CONTENT` (packages/opencode/src/auth/index.ts's
`Auth.all()` reads this before falling back to the on-disk file). This
adapter reads the session's own transcript to find which provider it
actually used -- opencode records `providerID`/`modelID` per *message*, not
once per session, since a conversation can switch models mid-stream -- and
takes the most recent assistant message's provider as the one to keep
using on resume. It looks that provider up in
`~/.local/share/opencode/auth.json` and exports *only* that provider's
entry as `OPENCODE_AUTH_CONTENT` (scoped to what this session needs, not
the user's whole credential file).

Only `type: "api"` auth.json entries (a real, portable, revocable API key)
are supported, matching docs/handoff-adapters.md's credential table. If the
session's provider is only configured via opencode's OAuth device-flow
login (e.g. a "Claude Pro/Max" or GitHub Copilot login), this adapter
raises HandoffError rather than shipping a device-bound refresh/access
token that was never meant to leave the machine it was issued on --
opencode has no `setup-token`-equivalent portable export for its OAuth
providers as of this writing.

Honest limitation: this was verified by reading opencode's own source
(github.com/anomalyco/opencode, dev branch) rather than by running a real
opencode binary in this environment, since opencode is not installed here.
The export/import JSON shape could still drift between opencode releases;
this adapter treats `opencode export`'s stdout as mostly opaque (it only
reads `messages[].info.providerID` out of it) to stay resilient to most
such changes, but a future CLI-flag or command removal would need a
matching update here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..core import Credential, HandoffError, LocatedSession, SessionFile, validate_identifier

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "opencode"
SANDBOX_HANDOFF_DIR = "/workspace/.opencode-handoff"
SANDBOX_WORKDIR = "/workspace"
COMMAND_TIMEOUT_SECONDS = 30

CommandRunner = Callable[[Sequence[str]], str]


def _run_opencode(argv: Sequence[str]) -> str:
    """Default runner: shell out to the real opencode CLI. Tests inject a
    fake runner instead of exercising this, since opencode is not installed
    in this package's test environment."""
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise HandoffError("opencode CLI not found on PATH -- is opencode installed?") from exc
    except subprocess.TimeoutExpired as exc:
        raise HandoffError(f"opencode command timed out: {' '.join(argv)}") from exc
    if result.returncode != 0:
        raise HandoffError(f"opencode command failed ({' '.join(argv)}): {result.stderr.strip()}")
    return result.stdout


class OpencodeAdapter:
    """See this module's docstring for the export/import continuation
    approach and credential-scoping rationale."""

    name: str = "opencode"

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        opencode_bin: str = "opencode",
        runner: CommandRunner | None = None,
        export_dir: Path | None = None,
    ) -> None:
        self._data_dir = data_dir if data_dir is not None else DEFAULT_DATA_DIR
        self._opencode_bin = opencode_bin
        self._runner: CommandRunner = runner if runner is not None else _run_opencode
        self._export_dir = export_dir

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        session_id = session_ref if session_ref is not None else self._find_most_recent_session_id()
        session_id = validate_identifier(session_id, what="session id")

        raw_export = self._export_session(session_id)
        export_data = self._parse_export(raw_export, session_id)
        provider_id = self._provider_for_session(export_data, session_id)
        credential = self._credential_for_provider(provider_id)
        export_file, cleanup = self._write_export_file(session_id, raw_export)

        sandbox_path = f"{SANDBOX_HANDOFF_DIR}/{session_id}.json"
        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(SessionFile(local_path=export_file, sandbox_path=sandbox_path),),
            credential=credential,
            resume_command=f"opencode import {sandbox_path} && opencode --session {session_id}",
            workdir=SANDBOX_WORKDIR,
            cleanup=cleanup,
        )

    def _find_most_recent_session_id(self) -> str:
        raw = self._runner([self._opencode_bin, "session", "list", "--format", "json"])
        try:
            sessions = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HandoffError("opencode session list did not return valid JSON") from exc
        if not sessions:
            raise HandoffError("no local opencode sessions found")
        most_recent = max(sessions, key=lambda session: session.get("updated", 0))
        session_id = most_recent.get("id")
        if not session_id:
            raise HandoffError("opencode session list entry is missing an id")
        return session_id

    def _export_session(self, session_id: str) -> str:
        raw = self._runner([self._opencode_bin, "export", session_id])
        if not raw.strip():
            raise HandoffError(f"opencode export returned no data for session {session_id}")
        return raw

    def _parse_export(self, raw: str, session_id: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HandoffError(
                f"opencode export for session {session_id} was not valid JSON"
            ) from exc
        if "info" not in data or "messages" not in data:
            raise HandoffError(f"opencode export for session {session_id} is missing info/messages")
        return data

    def _provider_for_session(self, export_data: dict[str, Any], session_id: str) -> str:
        """The provider the session most recently used -- opencode records
        providerID/modelID per message, so a conversation can switch models
        mid-stream; the last assistant message wins."""
        for message in reversed(export_data.get("messages", [])):
            provider_id = message.get("info", {}).get("providerID")
            if provider_id:
                return provider_id
        raise HandoffError(
            f"could not determine which provider session {session_id} used -- no message carries a providerID"
        )

    def _credential_for_provider(self, provider_id: str) -> Credential:
        auth_path = self._data_dir / "auth.json"
        if not auth_path.exists():
            raise HandoffError(f"no opencode credentials found at {auth_path}")
        try:
            auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HandoffError(f"{auth_path} is not valid JSON") from exc
        entry = auth_data.get(provider_id)
        if entry is None:
            raise HandoffError(f"no opencode credential configured for provider '{provider_id}'")
        if entry.get("type") != "api":
            raise HandoffError(
                f"provider '{provider_id}' is only configured via opencode's OAuth login "
                "(no portable, revocable API key) -- handoff requires a real API key entry"
            )
        scoped_auth_content = json.dumps({provider_id: entry})
        return Credential(env_var="OPENCODE_AUTH_CONTENT", value=scoped_auth_content)

    def _write_export_file(self, session_id: str, raw: str) -> tuple[Path, Callable[[], None] | None]:
        """Write the exported session JSON to a temp file the orchestrator
        will read once and push into the sandbox. Returns a `cleanup`
        callback that removes the file it just created -- but only when
        this call made its own temp directory (`export_dir` unset); a
        caller-provided `export_dir` (tests, or a future caller with its
        own retention policy) is left alone, since this adapter didn't
        create it and has no business deleting it."""
        owns_export_dir = self._export_dir is None
        export_dir = self._export_dir if self._export_dir is not None else Path(tempfile.mkdtemp(prefix="boxkite-handoff-opencode-"))
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"{session_id}.json"
        export_path.write_text(raw, encoding="utf-8")

        cleanup = (lambda: shutil.rmtree(export_dir, ignore_errors=True)) if owns_export_dir else None
        return export_path, cleanup
