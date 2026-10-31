from dataclasses import dataclass

import pytest

from boxxkite_rewards import FileExistsCheck, RewardFn, UnitTestPassRate


@dataclass
class Session:
    files: set[str]
    test_results: list[bool]


@pytest.mark.parametrize(
    ("files", "path", "expected"),
    [({"out.txt"}, "out.txt", 1.0), ({"out.txt"}, "missing.txt", 0.0), (set(), "out.txt", 0.0)],
)
def test_file_exists_check(files: set[str], path: str, expected: float) -> None:
    scorer: RewardFn = FileExistsCheck(path)

    assert scorer(Session(files, []), []) == expected


@pytest.mark.parametrize(
    ("results", "expected"),
    [([], 0.0), ([True, True], 1.0), ([True, False, True, False], 0.5), ([False], 0.0)],
)
def test_unit_test_pass_rate(results: list[bool], expected: float) -> None:
    scorer: RewardFn = UnitTestPassRate()

    assert scorer(Session(set(), results), []) == expected
