from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .protocol import RewardSession


@dataclass(frozen=True)
class FileExistsCheck:
    path: str

    def __call__(
        self,
        session: RewardSession,
        action_log: Sequence[Mapping[str, object]],
    ) -> float:
        return float(self.path in session.files)


@dataclass(frozen=True)
class UnitTestPassRate:
    def __call__(
        self,
        session: RewardSession,
        action_log: Sequence[Mapping[str, object]],
    ) -> float:
        if not session.test_results:
            return 0.0
        return sum(session.test_results) / len(session.test_results)
