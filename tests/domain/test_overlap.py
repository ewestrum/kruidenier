import pytest

from app.domain.overlap import compare, ready_for_autopilot


def test_hits_missed_extra() -> None:
    o = compare(planned={1, 2, 3}, delivered={2, 3, 4, 5}, known={1, 2, 3, 4})
    assert o.hits == {2, 3}
    assert o.missed == {4}  # 5 is new to the model: not a miss
    assert o.extra == {1}
    assert o.score == pytest.approx(2 / 4)


def test_perfect_and_empty() -> None:
    assert compare({1, 2}, {1, 2}, {1, 2}).score == 1.0
    assert compare(set(), {9}, set()).score is None  # nothing the model could know


def test_ready_needs_two_good_weeks_in_a_row() -> None:
    assert ready_for_autopilot([0.9, 0.85])
    assert ready_for_autopilot([0.8, 0.95, 0.1])  # only the last two count
    assert not ready_for_autopilot([0.9, 0.7])
    assert not ready_for_autopilot([0.9])
    assert ready_for_autopilot([None, 0.9, 0.9])  # unevaluable deliveries are skipped
