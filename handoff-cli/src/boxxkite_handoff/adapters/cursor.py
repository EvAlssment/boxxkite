"""Cursor adapter -- currently a documented, honest non-implementation.

See docs/handoff-adapters.md's "Adding a new adapter" step 1: a tool only
qualifies for this contract if it persists a locally-resumable session that
an adapter can actually locate and copy. Verification record for Cursor's
`cursor-agent` CLI (checked directly against the tool, not secondhand
sources, 2026-07):

Confirmed, directly against `cursor-agent`'s own docs and its actual
shipped binary (downloaded and inspected, not assumed):
- `cursor-agent`/`agent` genuinely has a local resume mechanism:
  `--resume [chatId]`, bare `agent resume` (latest), `--continue` (alias
  for `--resume=-1`), and an interactive `agent ls` picker.
- `CURSOR_API_KEY` (or `--api-key <key>`) is documented as sufficient for
  headless, non-interactive auth -- no browser/device-flow step is
  described for this path.

NOT confirmed, despite a real attempt (downloading the actual `cursor-agent`
package and reading its bundled source, not just its docs):
- The on-disk artifact that actually backs `--resume` could not be pinned
  down as a single, portable, per-session file the way Claude Code's JSONL
  transcript or Codex's rollout file are. The only concrete local-storage
  code path found reads a VS Code-style `state.vscdb` SQLite `ItemTable`
  under the *editor's* own user-data directory
  (`~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/...`
  on macOS, `~/.config/cursor/...` on Linux) -- i.e. workspace-hash-keyed,
  IDE-shared state, not an obviously portable single file an adapter could
  copy into a fresh sandbox with confidence it'd resume correctly there.
- Whether a machine that only ever runs `cursor-agent` headlessly (never
  opens the Cursor desktop app) even populates that path the same way.
- The workspace-hash algorithm needed to locate the right store for an
  arbitrary project directory, and the row/key layout inside it needed to
  extract one specific chat.
- A live, authenticated round-trip (create a chat locally, copy whatever
  file(s) into a fresh install, confirm `--resume` reconstructs it) --  not
  achievable here (no `CURSOR_API_KEY` available to test with, and
  `agent ls`/interactive resume require a real TTY).

Per the "degrade honestly, don't fake it" rule this doc lays out, this
adapter does not guess at that mapping. `locate_session` always raises
`HandoffError` rather than fabricate a `local_path`/`sandbox_path` that was
never confirmed to actually work. If a future contributor can confirm the
real backing artifact (e.g. by tracing what `state.vscdb` write happens
during a real interactive `agent` session), this adapter should be
implemented for real at that point -- see the two-outcome guidance in
docs/handoff-adapters.md.
"""

from __future__ import annotations

from ..core import HandoffError, LocatedSession

_UNVERIFIED_MESSAGE = (
    "Full-conversation handoff for Cursor is not currently supported. "
    "cursor-agent does have a local resume mechanism (--resume [chatId], "
    "agent resume, agent ls) and CURSOR_API_KEY is confirmed sufficient for "
    "headless auth -- but the on-disk session artifact that --resume "
    "actually reads could not be confirmed as a portable, copyable file: "
    "the only local-storage code path found in the shipped cursor-agent "
    "binary is a VS Code-style state.vscdb SQLite store under the editor's "
    "own workspace-hash-keyed user-data directory, not a self-contained "
    "per-session file the way Claude Code's or Codex's transcripts are. "
    "Faking a session-file mapping here would violate this project's "
    "'degrade honestly, don't fake it' rule for handoff adapters -- see "
    "docs/handoff-adapters.md and this module's docstring for the full "
    "verification record."
)


class CursorAdapter:
    """See this module's docstring for why `locate_session` always raises."""

    name = "cursor"

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        raise HandoffError(_UNVERIFIED_MESSAGE)
