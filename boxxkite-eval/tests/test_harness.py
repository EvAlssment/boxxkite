from boxxkite_eval import Task, run_suite, run_task


class FakeClient:
    def __init__(self, exit_codes=()):
        self.exit_codes = iter(exit_codes)
        self.calls = []

    def create_sandbox(self, *, label=None):
        self.calls.append(("create", label))
        return {"id": f"session-{len([call for call in self.calls if call[0] == 'create'])}"}

    def exec(self, session_id, command, *, timeout=None):
        self.calls.append(("exec", session_id, command, timeout))
        code = next(self.exit_codes, 0)
        return {"exit_code": code, "stdout": "ok", "stderr": ""}

    def destroy_sandbox(self, session_id):
        self.calls.append(("destroy", session_id))


def test_runs_phases_in_sandbox_and_cleans_up():
    client = FakeClient()
    task = Task("sample", "python agent.py", "python verify.py", "prepare", timeout_seconds=12)

    result = run_task(client, task)

    assert result.passed
    assert [call[2] for call in client.calls if call[0] == "exec"] == [
        "prepare",
        "python agent.py",
        "python verify.py",
    ]
    assert all(
        call[1] == "session-1" and call[3] == 12 for call in client.calls if call[0] == "exec"
    )
    assert client.calls[-1] == ("destroy", "session-1")


def test_stops_after_failed_phase_and_still_cleans_up():
    client = FakeClient([0, 7])
    task = Task("sample", "python agent.py", "python verify.py", "prepare")

    result = run_task(client, task)

    assert not result.passed
    assert result.failed_phase == "agent"
    assert result.exit_code == 7
    assert [call[2] for call in client.calls if call[0] == "exec"] == ["prepare", "python agent.py"]
    assert client.calls[-1] == ("destroy", "session-1")


def test_suite_is_sequential_and_aggregates_trials():
    client = FakeClient([0, 1, 0])
    task = Task("sample", "run-agent", "check-result")

    result = run_suite(client, [task], trials_per_task=2)

    assert (result.passed, result.failed) == (1, 1)
    assert [call[0] for call in client.calls if call[0] in {"create", "destroy"}] == [
        "create",
        "destroy",
        "create",
        "destroy",
    ]
