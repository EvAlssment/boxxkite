# boxxkite-eval

A small sequential evaluation harness built on `boxxkite-client`. Each trial
creates one sandbox, runs its optional setup command, agent command, and
verifier command in that sandbox, then destroys it. `timeout_seconds` is one
monotonic deadline for the entire trial, including sandbox creation. Commands
are sent only to the sandbox client; this package does not run them on the
machine invoking it.

```python
from boxxkite_client import BoxxkiteClient
from boxxkite_eval import Task, run_suite

task = Task(
    name="hello",
    setup_command="mkdir -p /workspace",
    agent_command="python /workspace/agent.py",
    verifier_command="python /workspace/verify.py",
    timeout_seconds=120,
)

with BoxxkiteClient(api_key="...", base_url="https://your-control-plane") as client:
    result = run_suite(client, [task], trials_per_task=3)
    print(result.passed, result.failed)
```

This first slice accepts commands already available inside the sandbox image.
It does not transfer local files, fan trials out concurrently, create task
images, persist reports, or provide a command-line interface. The verifier's
zero exit status determines a pass.
