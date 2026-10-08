from datetime import date, timedelta

import pytest

from app.domain.consumption import Pause, Purchase
from app.domain.planning import FamilyInput, PlanSettings, Reason, decide

D0 = date(2026, 1, 4)


def weekly(n: int, amount: float, every: int = 7) -> list[Purchase]:
    return [Purchase(D0 + timedelta(days=every * i), amount) for i in range(n)]


def test_weekly_staple_is_due_next_delivery() -> None:
    purchases = weekly(6, 2000)  # 2 l milk per week, packs of 1 l
    last = purchases[-1].day
    d = decide(
        FamilyInput(purchases, pack_size=1000),
        next_delivery=last + timedelta(days=7),
        today=last + timedelta(days=5),
    )
    assert d.include
    assert d.reason is Reason.DUE
    # need for cadence+margin (9 days) = 2571, stock at delivery = 0 -> 3 packs
    assert d.packs == 3
    assert d.stock_at_delivery == pytest.approx(0)


def test_monthly_item_not_due_yet() -> None:
    purchases = weekly(5, 500, every=30)
    last = purchases[-1].day
    d = decide(
        FamilyInput(purchases, pack_size=500),
        next_delivery=last + timedelta(days=7),
        today=last + timedelta(days=2),
    )
    assert not d.include
    assert d.reason is Reason.NOT_DUE
    assert d.due == last + timedelta(days=30)


def test_due_within_horizon_counts_remaining_stock() -> None:
    purchases = weekly(5, 1000, every=10)  # 100/day, packs of 1000
    last = purchases[-1].day
    d = decide(
        FamilyInput(purchases, pack_size=1000),
        next_delivery=last + timedelta(days=6),  # 400 left on delivery
        today=last + timedelta(days=3),
    )
    assert d.include
    assert d.stock_at_delivery == pytest.approx(400)
    assert d.packs == 1  # need 900 - 400 = 500 -> 1 pack


def test_too_few_purchases_is_noise_unless_pinned() -> None:
    purchases = weekly(2, 1000)
    last = purchases[-1].day
    kwargs = {"next_delivery": last + timedelta(days=7), "today": last + timedelta(days=5)}
    assert decide(FamilyInput(purchases, 1000), **kwargs).reason is Reason.TOO_FEW_PURCHASES  # type: ignore[arg-type]
    pinned = decide(FamilyInput(purchases, 1000, pinned=True), **kwargs)  # type: ignore[arg-type]
    assert pinned.include
    assert pinned.reason is Reason.PINNED_DUE


def test_old_purchases_do_not_count_for_noise_window() -> None:
    purchases = weekly(5, 1000)
    today = purchases[-1].day + timedelta(days=200)
    d = decide(FamilyInput(purchases, 1000), next_delivery=today + timedelta(1), today=today)
    assert d.reason is Reason.TOO_FEW_PURCHASES


def test_excluded_family() -> None:
    purchases = weekly(6, 1000)
    last = purchases[-1].day
    d = decide(
        FamilyInput(purchases, 1000, excluded=True),
        next_delivery=last + timedelta(days=7),
        today=last,
    )
    assert d.reason is Reason.EXCLUDED and not d.include


def test_no_plan_when_delivery_is_in_holiday() -> None:
    purchases = weekly(6, 1000)
    last = purchases[-1].day
    delivery = last + timedelta(days=7)
    d = decide(
        FamilyInput(purchases, 1000),
        next_delivery=delivery,
        today=last,
        pauses=[Pause(delivery - timedelta(days=1), delivery + timedelta(days=7))],
    )
    assert d.reason is Reason.PAUSED and not d.include


def test_enough_stock_feedback_skips_this_week() -> None:
    purchases = weekly(6, 700)
    last = purchases[-1].day
    d = decide(
        FamilyInput(purchases, pack_size=700, carryover=2000),
        next_delivery=last + timedelta(days=7),
        today=last + timedelta(days=5),
    )
    assert not d.include
    assert d.reason is Reason.NOT_DUE


def test_pack_cap_guards_against_bugs() -> None:
    purchases = weekly(6, 10_000)
    last = purchases[-1].day
    d = decide(
        FamilyInput(purchases, pack_size=10),
        next_delivery=last + timedelta(days=7),
        today=last,
        settings=PlanSettings(max_packs_per_line=12),
    )
    assert d.include and d.packs == 12
