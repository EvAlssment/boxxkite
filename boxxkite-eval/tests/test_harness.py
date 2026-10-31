import pytest
from boxxkite_client import BoxxkiteConnectionError

from boxxkite_eval import Task, run_suite, run_task


class FakeClient:
    def __init__(self, exit_codes=(), *, error=None):
        self.exit_codes = iter(exit_codes)
        self.calls = []
        self.error = error

    def create_sandbox(self, *, label=None):
        self.calls.append(("create", label))
        return {"id": f"session-{len([call for call in self.calls if call[0] == 'create'])}"}

    def exec(self, session_id, command, *, timeout=None):
        self.calls.append(("exec", session_id, command, timeout))
        if self.error is not None:
            raise self.error
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
    phase_calls = [call for call in client.calls if call[0] == "exec"]
    assert all(call[1] == "session-1" and 1 <= call[3] <= 12 for call in phase_calls)
    assert [call[3] for call in phase_calls] == sorted(
        (call[3] for call in phase_calls), reverse=True
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


def test_phases_share_one_trial_deadline(monkeypatch):
    times = iter([0, 1, 4, 10, 11])
    monkeypatch.setattr("boxxkite_eval.harness.monotonic", lambda: next(times))
    client = FakeClient()
    task = Task("sample", "run-agent", "check-result", "prepare", timeout_seconds=12)

    result = run_task(client, task)

    assert result.passed
    assert [call[3] for call in client.calls if call[0] == "exec"] == [11, 8, 2]


def test_deadline_expiry_stops_later_phases_and_cleans_up(monkeypatch):
    times = iter([0, 1, 12.5, 13])
    monkeypatch.setattr("boxxkite_eval.harness.monotonic", lambda: next(times))
    client = FakeClient()
    task = Task("sample", "run-agent", "check-result", "prepare", timeout_seconds=12)

    result = run_task(client, task)

    assert not result.passed
    assert result.failed_phase == "agent"
    assert result.stderr == "trial deadline exceeded"
    assert [call[2] for call in client.calls if call[0] == "exec"] == ["prepare"]
    assert client.calls[-1] == ("destroy", "session-1")


def test_sdk_connection_timeout_is_a_failed_trial_and_cleans_up():
    client = FakeClient(error=BoxxkiteConnectionError("request timed out"))
    task = Task("sample", "run-agent", "check-result")

    result = run_task(client, task)

    assert not result.passed
    assert result.failed_phase == "agent"
    assert result.exit_code is None
    assert result.stderr == "request timed out"
    assert client.calls[-1] == ("destroy", "session-1")


@pytest.mark.parametrize("timeout_seconds", [True, False])
def test_task_rejects_boolean_timeout(timeout_seconds):
    with pytest.raises(ValueError, match="timeout_seconds"):
        Task("sample", "run-agent", "check-result", timeout_seconds=timeout_seconds)


def test_suite_rejects_boolean_trial_count():
    with pytest.raises(ValueError, match="trials_per_task"):
        run_suite(FakeClient(), [Task("sample", "run-agent", "check-result")], trials_per_task=True)
