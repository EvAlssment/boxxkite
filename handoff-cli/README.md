# boxxkite-handoff

Move an in-progress Claude Code, Codex CLI, opencode, or Cursor session from
your local machine into a fresh [boxxkite](https://github.com/EvAlssment/boxxkite)
sandbox — full conversation history included — and keep interacting with it
from there.

```bash
pip install boxxkite-handoff
BOXXKITE_API_KEY=... BOXXKITE_BASE_URL=... boxxkite-handoff claude-code
```

This provisions a fresh sandbox, pushes your local session's on-disk state
into it, and opens the same takeover terminal boxxkite already uses for
human operator sessions — with the resume command already typed and
running. See [`docs/handoff-adapters.md`](../docs/handoff-adapters.md) for
the full architecture and the adapter contract for adding support for
another tool.

## Supported tools

| `boxxkite-handoff <name>` | Tool |
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
