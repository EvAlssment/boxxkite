# First-party task benchmark

`boxxkite-bench-v1` is an initial, first-party task dataset for exercising
agent work in a filesystem workspace. The dataset lives outside the runtime
package under [`boxxkite-bench-v1/`](boxxkite-bench-v1/) and is licensed under
Apache-2.0; these tasks were authored for this repository. This is a small
starter slice, not the full coverage proposed in [issue #42](https://github.com/EvAlssment/boxxkite/issues/42).

The versioned `tasks.json` has a top-level `format`, `license`, and `tasks`
array. Each task has a unique kebab-case `id`, display `title`, broad
`category`, agent-facing `prompt`, UTF-8 `setup_files` map, and deterministic
`checks`. The first format version supports `file_contains` checks only.
Paths must be relative and may not traverse parent directories. Unknown or
missing task fields are rejected so format changes are explicit.

Validate the checked-in dataset with Python 3.11 or newer:

```sh
python3 benchmarks/runner.py validate
python3 -m unittest discover -s benchmarks -p 'test_*.py'
```

Run a command against every task, or select one by id:

```sh
python3 benchmarks/runner.py run -- python3 -c \
  "from pathlib import Path; Path('stats.py').write_text('def summarize(values): return {}')"
python3 benchmarks/runner.py run --task py-stats-001 --timeout 60 -- <agent-command> [arguments...]
```

The runner creates a fresh temporary workspace per task, writes its starter
files, invokes the supplied command with that workspace as its current
directory, then checks the declared outputs and exit status. The command is
executed as supplied and the runner does not provide operating-system
isolation, network restrictions, an agent adapter, or hidden semantic tests;
use an appropriate isolated environment for untrusted commands. Output checks
are intentionally basic and are not a model-quality score. This release has
three authored tasks, no reference-model measurements, and no browser-specific
task yet. It reports only whether the command exited successfully and met the
declared file checks.

The runner and task data have no extra third-party runtime dependencies.
