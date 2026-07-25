# Claude Code, headless, inside a boxkite sandbox

Closes the gap `docs/E2B-COMPARISON.md` §4.2 named directly: E2B ships a
dedicated `claude` sandbox template and docs page; this is boxkite's
equivalent quickstart. Full write-up, including why this needs a custom
image and the security caveat on the API key, lives in
[`docs/CLAUDE-CODE-SANDBOX-QUICKSTART.md`](../../docs/CLAUDE-CODE-SANDBOX-QUICKSTART.md)
— read that first.

## What it does

Clones a small public repo (`octocat/Hello-World`) into `/workspace/repo`
using `git_clone` (no raw credential in a shell string — this repo is
public so no `token=`/`ssh_key=` is even needed here, but the same call
accepts them), then runs Claude Code headlessly (`claude -p ... --output-format
json --dangerously-skip-permissions`) via `bash_tool` to summarize the repo.

## Prerequisites

1. Build the Claude-Code-enabled sandbox image:
   ```bash
   docker build -f ../../deploy/sandbox-claude-code.Dockerfile -t boxkite-sandbox-claude-code ../..
   ```
2. A running boxkite stack pointed at that image: `SANDBOX_IMAGE=boxkite-sandbox-claude-code
   boxkite up` (or the equivalent `docker-compose`/Kubernetes env var, see
   `docs/CONFIGURATION.md`).
3. `pip install -e ../..` (boxkite itself -- no extra needed).
4. `ANTHROPIC_API_KEY` set in the process running this script (not baked
   into the image -- see the quickstart doc's security note on exactly how
   and why it's passed).

## Run

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080

python run_claude_code.py
```

## What's verified vs. what needs a live run

`deploy/sandbox-claude-code.Dockerfile` was built and run locally in this
environment: `claude --version` reports `2.0.1 (Claude Code)` both during
the image build and when invoked as the non-root `sandbox` user afterward,
and `npm`/`npx` were confirmed absent post-build (same hardening posture as
every other boxkite base image). This script's own tool wiring
(`create_bash_tool_spec`, `create_git_tool_specs`, the `git_clone` handler's
parameter names) was verified against a fake sandbox manager -- no import
errors, no signature mismatches. The end-to-end run against a real sandbox
pod and a live Claude Code invocation was **not** exercised in this
environment (no live Kubernetes/docker-compose sidecar or Anthropic API key
available together); please verify with your own setup before relying on
it, and treat the security note in the quickstart doc as load-bearing, not
optional reading.
