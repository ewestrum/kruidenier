from hypothesis import given
from hypothesis import strategies as st

from app.domain.order_changes import Change, changes_to_apply, changes_to_undo


def test_adds_missing_and_raises_lower() -> None:
    changes = changes_to_apply({1: 1, 2: 5}, {1: 3, 2: 2, 3: 4})
    assert changes == [Change(1, 1, 3), Change(3, 0, 4)]  # product 2 already has more


def test_second_run_changes_nothing() -> None:
    first = changes_to_apply({}, {1: 2, 2: 1})
    after = {c.product_id: c.after for c in first}
    assert changes_to_apply(after, {1: 2, 2: 1}) == []


def test_undo_restores_previous() -> None:
    applied = [Change(1, 1, 3), Change(3, 0, 4)]
    plan = changes_to_undo(applied, {1: 3, 3: 4})
    assert plan.revert == [Change(1, 3, 1), Change(3, 4, 0)]
    assert plan.untouched == []


def test_undo_leaves_products_someone_changed() -> None:
    applied = [Change(1, 1, 3), Change(3, 0, 4)]
    plan = changes_to_undo(applied, {1: 5, 3: 4})
    assert plan.revert == [Change(3, 4, 0)]
    assert plan.untouched == [1]


def test_undo_skips_already_reverted() -> None:
    plan = changes_to_undo([Change(1, 1, 3)], {1: 1})
    assert plan.revert == [] and plan.untouched == []


quantities = st.dictionaries(st.integers(1, 30), st.integers(0, 12), max_size=15)


@given(current=quantities, wanted=quantities)
def test_never_lowers_and_is_idempotent(current: dict[int, int], wanted: dict[int, int]) -> None:
    changes = changes_to_apply(current, wanted)
    for c in changes:
        assert c.after > c.before == current.get(c.product_id, 0)
    result = dict(current) | {c.product_id: c.after for c in changes}
    assert changes_to_apply(result, wanted) == []
    undo = changes_to_undo(changes, result)
    restored = result | {c.product_id: c.after for c in undo.revert}
    assert all(restored.get(p, 0) == current.get(p, 0) for p in wanted)
