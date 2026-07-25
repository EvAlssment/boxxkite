# Contributing to boxkite

Thanks for considering a contribution. boxkite's own code is small and
focused, so this doc stays short — but its dependency tree (LangChain,
LangGraph, and cloud SDKs for Kubernetes/AWS/Azure) is substantial. Expect a
cold `pip install -e '.[dev]'` to take a few minutes, not seconds.

## One repo, several packages

This repo holds the core sandbox library, a self-hostable control-plane,
four SDKs, and an MCP server — each with its own dependencies, tests, and
(eventual) release/versioning, kept in one repo rather than split across
several. That's a deliberate choice at this stage: these pieces are still
young enough that most changes span more than one of them at once (an API
change in `control-plane/` usually needs a matching SDK update in the same
PR), and splitting now would mean coordinating versions and CI across repos
that don't yet have independent contributor traffic to justify it. If a
given piece — the JS SDK, say — later needs its own release cadence
independent of everything else here, it can be extracted with full git
history via `git filter-repo` at that point.

Practically, this means: **check which package's `tests/` a change touches**,
and follow that package's own setup below — installing the root package does
not install `control-plane/`, `sdk-python/`, `sdk-js/`, `sdk-go/`,
`sdk-rust/`, or `mcp-server/`; each is independent.

## Before you start

For anything beyond a small fix (a new tool, a change to pod security
context, a new storage backend), please open an issue first to discuss the
approach. This project touches code-execution isolation; changes to
`deploy/pod-template.yaml`, `deploy/network-policy.yaml`,
`sidecar/main.py`'s path/permission handling, or `src/boxkite/manager.py`'s
security context construction get extra scrutiny before merge.

## Developer Certificate of Origin (DCO), not a CLA

We use the [Developer Certificate of Origin](https://developercertificate.org/)
instead of a Contributor License Agreement. It's a lightweight way to certify
that you wrote the code (or otherwise have the right to submit it) — no
paperwork, no assignment of copyright.

Sign off every commit with `git commit -s`, which appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

If you forgot to sign off, fix it on your last commit with:

```bash
git commit --amend -s
```

or for a range of commits:

```bash
git rebase --signoff HEAD~<n>
```

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# Some tests import sidecar/main.py directly (fastapi/pydantic/aiofiles),
# which aren't declared in pyproject.toml since they're the sidecar's own
# dependencies, not the boxkite package's — install them too:
pip install -r sidecar/requirements.txt
pytest tests/
```

For the sidecar and sandbox containers, see the top-level README's
quickstart (`deploy/docker-compose.yml`) or `deploy/local-kind/` for a real
Kubernetes dev loop.

### control-plane/ (separate package, separate setup)

`control-plane/` is a fully separate Python package — its own
`pyproject.toml`, its own `[dev]` extras, its own `src/control_plane`, and
its own `tests/` (`ci.yml`'s dedicated `test-control-plane` job exercises
this when CI is enabled — see [Fork, branch, PR](#fork-branch-pr) for its
current status). Setting up the root package above does **not** set this up; it needs its own
install steps, run from the repo root:

```bash
# from the repo root, with the root .venv already active
pip install -e .                        # control-plane depends on boxkite as a local sibling
cd control-plane
pip install -e ".[dev]"
pytest tests/
```

The control-plane suite runs against `aiosqlite` instead of a real Postgres
(see `control-plane/pyproject.toml`'s `dev` extras), so no external database
is needed to run it locally.

### sdk-python/ (separate package, separate setup)

```bash
cd sdk-python
pip install -e ".[dev,langchain]"
pytest tests/
```

Tests mock the control-plane with `httpx.MockTransport` — no real deployment
needed.

### sdk-js/ (separate package, separate setup)

```bash
cd sdk-js
npm install
npm test   # builds via tsc, then runs node's built-in test runner
```

### mcp-server/ (separate package, separate setup)

Depends on `sdk-python` as a local sibling, same as `control-plane/` depends
on the root package:

```bash
pip install -e ./sdk-python
cd mcp-server
pip install -e ".[dev]"
pytest tests/
```

Unit tests mock the control-plane the same way `sdk-python/tests/` does.
`mcp-server/tests/live_smoke.py` is a separate, non-CI manual script that
exercises a real running control-plane — don't run it as part of a normal
test pass.

## Fork, branch, PR

`main` is a protected branch — nobody, including maintainers, pushes to it
directly. Every change lands through a pull request from your own fork:

```bash
gh repo fork EvAlssment/boxkite --clone
cd boxkite
git checkout -b my-change
# ... make your change ...
git commit -s
git push -u origin my-change
gh pr create --repo EvAlssment/boxkite
```

(Or the non-`gh` equivalent: fork via the GitHub UI, clone your fork, add
`EvAlssment/boxkite` as an `upstream` remote to pull in future changes, and
open the PR from your fork's branch against `EvAlssment/boxkite:main`.)

Every PR needs at least one approving review before it can merge — branch
protection enforces this, it isn't just a norm. This repo's CI workflows
(`ci.yml`, `publish-images.yml`, `publish-pypi.yml`,
`benchmark-warm-pool.yml`) are currently disabled, so nothing runs
automatically against your PR — run the checks below yourself before
requesting review, since a reviewer is the only gate that's actually active
right now.

## Pull requests

- Keep PRs focused — one change, one PR.
- Add or update tests for behavior changes, in whichever package's own
  `tests/` your change touches.
- Run that package's own test command from the setup sections above before
  opening the PR — this matters most for `sdk-python/`, `sdk-js/`,
  `sdk-go/`, `sdk-rust/`, and `mcp-server/`.
- Run the same lint and dependency-audit checks CI would otherwise run, so
  a reviewer isn't the first to catch them:
  ```bash
  ruff check src/ tests/ control-plane/src/ control-plane/tests/
  pip-audit --progress-spinner=off
  ```
  Both `ruff` and `pip-audit` are installed by the root `pip install -e ".[dev]"`
  from the setup above.
- Describe what changed and why in the PR description; link the issue it
  addresses if one exists.

## Security issues

Do **not** open a public issue for a security vulnerability. See
[SECURITY.md](SECURITY.md) for the private reporting path.

## Code of conduct

Be respectful, assume good faith, and keep discussion focused on the
technical merits. Maintainers may close or lock discussions that don't meet
this bar. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for the full policy
and how to report a violation.
