from collections.abc import Collection, Mapping, Sequence
from typing import Protocol


class RewardSession(Protocol):
    files: Collection[str]
    test_results: Sequence[bool]


class RewardFn(Protocol):
    def __call__(
        self,
        session: RewardSession,
        action_log: Sequence[Mapping[str, object]],
    ) -> float: ...
