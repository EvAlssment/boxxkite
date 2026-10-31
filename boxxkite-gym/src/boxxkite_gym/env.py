from __future__ import annotations

from collections.abc import Callable
from string import printable
from typing import Any

from gymnasium import Env, spaces


class BoxxkiteEnv(Env[str, str]):
    """Run one shell command per action in an isolated sandbox episode.

    The first reset creates a session and records a filesystem snapshot.
    Later resets destroy the episode session and restore that snapshot into
    a fresh session. Only workspace filesystem state is restored; process,
    network, and other runtime state are not part of a snapshot.
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        client: Any,
        *,
        reward_fn: Callable[[str, dict[str, Any]], float] | None = None,
        max_episode_steps: int = 100,
        action_max_length: int = 4096,
        observation_max_length: int = 32768,
        sandbox_options: dict[str, Any] | None = None,
    ) -> None:
        if max_episode_steps < 1:
            raise ValueError("max_episode_steps must be positive")
        if action_max_length < 1 or observation_max_length < 1:
            raise ValueError("space maximum lengths must be positive")
        self.client = client
        self.reward_fn = reward_fn or (lambda _action, _result: 0.0)
        self.max_episode_steps = max_episode_steps
        self.observation_space = spaces.Text(
            min_length=0, max_length=observation_max_length, charset=printable
        )
        self.action_space = spaces.Text(min_length=1, max_length=action_max_length, charset=printable)
        self._sandbox_options = dict(sandbox_options or {})
        self._session_id: str | None = None
        self._snapshot_id: str | None = None
        self._steps = 0
        self._episode_done = True
        self._closed = False

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        super().reset(seed=seed)
        if self._closed:
            raise RuntimeError("environment is closed")
        if options:
            raise ValueError("reset options are not supported")

        if self._snapshot_id is None:
            created = self.client.create_sandbox(**self._sandbox_options)
            self._session_id = created["id"]
            try:
                snapshot = self.client.create_snapshot(self._session_id, label="boxxkite-gym-baseline")
                self._snapshot_id = snapshot["id"]
            except BaseException:
                self._destroy_session()
                raise
        else:
            if self._session_id is not None:
                self._destroy_session()
            restored = self.client.restore_snapshot(self._snapshot_id, label="boxxkite-gym-episode")
            self._session_id = restored["id"]

        self._steps = 0
        self._episode_done = False
        return "", {"session_id": self._session_id, "snapshot_id": self._snapshot_id}

    def step(self, action: str) -> tuple[str, float, bool, bool, dict[str, Any]]:
        if self._closed:
            raise RuntimeError("environment is closed")
        if self._session_id is None:
            raise RuntimeError("call reset() before step()")
        if self._episode_done:
            raise RuntimeError("episode ended; call reset() before step()")
        if not isinstance(action, str) or not self.action_space.contains(action):
            raise ValueError("action must be a non-empty command within action_space")

        result = self.client.exec(self._session_id, action)
        observation = str(result.get("stdout", "")).encode("ascii", "backslashreplace").decode("ascii")
        observation = observation[: self.observation_space.max_length]
        reward = float(self.reward_fn(action, result))
        self._steps += 1
        truncated = self._steps >= self.max_episode_steps
        self._episode_done = truncated
        info = {"exit_code": result.get("exit_code"), "session_id": self._session_id}
        return observation, reward, False, truncated, info

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._destroy_session()
        finally:
            try:
                if self._snapshot_id is not None:
                    self.client.delete_snapshot(self._snapshot_id)
            finally:
                self._snapshot_id = None
                self._closed = True

    def _destroy_session(self) -> None:
        session_id, self._session_id = self._session_id, None
        if session_id is not None:
            self.client.destroy_sandbox(session_id)
