# boxxkite-gym

A small Gymnasium `Env` adapter for one shell command per action in a Boxxkite sandbox.

Install the package with its optional Gymnasium dependency:

```sh
pip install '.[gymnasium]'
```

Pass an existing `BoxxkiteClient` to `BoxxkiteEnv`. `reset()` creates an initial session and filesystem baseline snapshot; subsequent resets destroy the episode session and restore that snapshot into a new session. `step(command)` executes one command, returns stdout as the observation, applies the supplied `reward_fn(action, result)` (zero by default), and returns Gymnasium's five-value step tuple. There is no task-specific terminal condition; `max_episode_steps` produces `truncated=True`.

Snapshots contain filesystem state only. Reset does not rewind processes, network state, or other runtime state. Closing the environment destroys its active session and deletes its baseline snapshot, but does not close the caller-owned client. Call `close()` or use an environment-management wrapper when finished.

Actions use Gymnasium's printable-text space; non-ASCII stdout is represented with Python-style escapes so observations remain inside their declared space. This package does not integrate an external trainer and has not been validated against a live control-plane or benchmark. Tests use a fake client and require no cloud credentials.
