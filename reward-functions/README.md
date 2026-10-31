# Boxxkite reward functions

`boxxkite-reward-functions` is a small, standalone Python package for scoring
already-captured sandbox state. It has no runtime dependencies and makes no
model, network, filesystem, or subprocess calls.

The `RewardFn` protocol accepts a `RewardSession` snapshot and an action log,
and returns a float. The supplied scorers are deterministic and use only the
snapshot's `files` and `test_results` fields:

```python
from boxxkite_rewards import FileExistsCheck, UnitTestPassRate

file_reward = FileExistsCheck("artifacts/result.json")
test_reward = UnitTestPassRate()
score = file_reward(session_snapshot, action_log)
```

`FileExistsCheck` returns `1.0` when its exact path is in `session.files`,
otherwise `0.0`. `UnitTestPassRate` returns the fraction of true values in
`session.test_results`; an empty result sequence scores `0.0`. The caller is
responsible for creating the snapshot and recording test outcomes. These
scorers do not run commands or inspect a live sandbox.

Install for local development and run the focused tests:

```bash
python -m pip install -e '.[test]'
pytest
```

This package is currently standalone. It does not provide `boxxkite-eval` or
`boxxkite-gym` adapters, and does not include reference-diff or model-judgment
scorers.
