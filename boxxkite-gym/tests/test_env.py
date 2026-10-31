from __future__ import annotations

import pytest

gymnasium = pytest.importorskip("gymnasium")

from boxxkite_gym import BoxxkiteEnv


class FakeClient:
    def __init__(self):
        self.calls = []

    def create_sandbox(self, **kwargs):
        self.calls.append(("create_sandbox", kwargs))
        return {"id": "session-1"}

    def create_snapshot(self, session_id, **kwargs):
        self.calls.append(("create_snapshot", session_id, kwargs))
        return {"id": "snapshot-1"}

    def restore_snapshot(self, snapshot_id, **kwargs):
        self.calls.append(("restore_snapshot", snapshot_id, kwargs))
        return {"id": "session-2"}

    def exec(self, session_id, action):
        self.calls.append(("exec", session_id, action))
        return {"stdout": "observed 😀", "exit_code": 0}

    def destroy_sandbox(self, session_id):
        self.calls.append(("destroy_sandbox", session_id))

    def delete_snapshot(self, snapshot_id):
        self.calls.append(("delete_snapshot", snapshot_id))


def test_reset_step_and_truncation_follow_gymnasium_contract():
    client = FakeClient()
    env = BoxxkiteEnv(client, max_episode_steps=1, reward_fn=lambda action, result: result["exit_code"] + 0.5)

    observation, info = env.reset(seed=5)
    assert observation == ""
    assert info == {"session_id": "session-1", "snapshot_id": "snapshot-1"}
    assert env.observation_space.contains(observation)

    observation, reward, terminated, truncated, info = env.step("true")
    assert (observation, reward, terminated, truncated) == (r"observed \U0001f600", 0.5, False, True)
    assert info["exit_code"] == 0
    assert env.observation_space.contains(observation)
    with pytest.raises(RuntimeError, match="call reset"):
        env.step("true")

    env.reset()
    assert ("destroy_sandbox", "session-1") in client.calls
    assert ("restore_snapshot", "snapshot-1", {"label": "boxxkite-gym-episode"}) in client.calls
    assert env.step("true")[4]["session_id"] == "session-2"
    env.close()
    env.close()
    assert client.calls.count(("destroy_sandbox", "session-2")) == 1
    assert client.calls.count(("delete_snapshot", "snapshot-1")) == 1


def test_step_requires_reset_and_rejects_invalid_action():
    env = BoxxkiteEnv(FakeClient())
    with pytest.raises(RuntimeError, match="reset"):
        env.step("true")
    env.reset()
    with pytest.raises(ValueError, match="action"):
        env.step(12)
    env.close()


def test_creation_failure_destroys_session():
    client = FakeClient()
    client.create_snapshot = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("snapshot failed"))
    env = BoxxkiteEnv(client)
    with pytest.raises(RuntimeError, match="snapshot failed"):
        env.reset()
    assert ("destroy_sandbox", "session-1") in client.calls


def test_restore_failure_keeps_baseline_for_retry():
    client = FakeClient()
    env = BoxxkiteEnv(client)
    env.reset()
    env.reset()
    client.restore_snapshot = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("restore failed"))
    with pytest.raises(RuntimeError, match="restore failed"):
        env.reset()
    client.restore_snapshot = FakeClient().restore_snapshot
    env.reset()
    assert client.calls.count(("create_snapshot", "session-1", {"label": "boxxkite-gym-baseline"})) == 1
    env.close()


def test_gymnasium_checker():
    from gymnasium.utils.env_checker import check_env

    env = BoxxkiteEnv(FakeClient())
    check_env(env)
    env.close()
