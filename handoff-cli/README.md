# boxkite-handoff

Move an in-progress Claude Code, Codex CLI, opencode, or Cursor session from
your local machine into a fresh [boxkite](https://github.com/EvAlssment/boxkite)
sandbox — full conversation history included — and keep interacting with it
from there.

```bash
pip install boxkite-handoff
BOXKITE_API_KEY=... BOXKITE_BASE_URL=... boxkite-handoff claude-code
```

This provisions a fresh sandbox, pushes your local session's on-disk state
into it, and opens the same takeover terminal boxkite already uses for
human operator sessions — with the resume command already typed and
running. See [`docs/handoff-adapters.md`](../docs/handoff-adapters.md) for
the full architecture and the adapter contract for adding support for
another tool.

## Supported tools

| `boxkite-handoff <name>` | Tool |
|---|---|
| `claude-code` | Claude Code |
| `codex` | Codex CLI |
| `opencode` | opencode |
| `cursor` | Cursor (`cursor-agent`) |

## Development

```bash
pip install -e ".[dev]"
pytest
```
