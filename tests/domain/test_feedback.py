import pytest

from app.domain.feedback import (
    MAX_CORRECTION,
    MIN_CORRECTION,
    FamilyState,
    FeedbackKind,
    apply_feedback,
)

BASE = FamilyState(correction=1.0, carryover=0.0, excluded=False)


def test_ok_changes_nothing() -> None:
    eff = apply_feedback(FeedbackKind.OK, BASE, line_qty=2, pack_size=1000)
    assert eff.family == BASE and eff.line_qty == 2


def test_more_then_less_is_neutral() -> None:
    more = apply_feedback(FeedbackKind.MORE, BASE, line_qty=2, pack_size=1000)
    assert more.line_qty == 3 and more.family.correction > 1
    back = apply_feedback(FeedbackKind.LESS, more.family, line_qty=3, pack_size=1000)
    assert back.line_qty == 2
    assert back.family.correction == pytest.approx(1.0)


def test_less_on_last_pack_removes_line() -> None:
    eff = apply_feedback(FeedbackKind.LESS, BASE, line_qty=1, pack_size=1000)
    assert eff.line_qty == 0


def test_correction_is_bounded() -> None:
    state = BASE
    for _ in range(50):
        state = apply_feedback(FeedbackKind.MORE, state, line_qty=1, pack_size=1).family
    assert state.correction == MAX_CORRECTION
    for _ in range(100):
        state = apply_feedback(FeedbackKind.LESS, state, line_qty=1, pack_size=1).family
    assert state.correction == MIN_CORRECTION


def test_enough_stock_adds_the_line_as_carryover() -> None:
    eff = apply_feedback(FeedbackKind.ENOUGH_STOCK, BASE, line_qty=3, pack_size=500)
    assert eff.line_qty == 0 and eff.family.carryover == 1500


def test_enough_stock_uses_model_need_when_larger() -> None:
    eff = apply_feedback(
        FeedbackKind.ENOUGH_STOCK, BASE, line_qty=3, pack_size=500, enough_stock_extra=4200
    )
    assert eff.family.carryover == 4200
    small = apply_feedback(
        FeedbackKind.ENOUGH_STOCK, BASE, line_qty=3, pack_size=500, enough_stock_extra=10
    )
    assert small.family.carryover == 1500


def test_not_anymore_excludes() -> None:
    eff = apply_feedback(FeedbackKind.NOT_ANYMORE, BASE, line_qty=2, pack_size=1)
    assert eff.family.excluded and eff.line_qty == 0
