from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.consumption import (
    Confidence,
    Pause,
    Purchase,
    active_days,
    add_active_days,
    due_date,
    estimate_consumption,
    merge_same_day,
    stock_on,
)

D0 = date(2026, 1, 4)  # a Sunday


def weekly(n: int, amount: float = 1000.0, start: date = D0, every: int = 7) -> list[Purchase]:
    return [Purchase(start + timedelta(days=every * i), amount) for i in range(n)]


def test_steady_weekly_milk() -> None:
    purchases = weekly(6, 2000)  # 2 l per week
    today = purchases[-1].day + timedelta(days=2)
    est = estimate_consumption(purchases, today=today)
    assert est.rate_per_day == pytest.approx(2000 / 7)
    assert est.confidence is Confidence.HIGH
    assert est.cv_interval == pytest.approx(0.0)
    assert due_date(est) == purchases[-1].day + timedelta(days=7)


def test_single_purchase_has_no_rate() -> None:
    est = estimate_consumption([Purchase(D0, 500)], today=D0 + timedelta(days=3))
    assert est.rate_per_day is None
    assert est.confidence is Confidence.LOW
    assert est.n_purchases == 1
    assert due_date(est) is None


def test_no_purchases() -> None:
    est = estimate_consumption([], today=D0)
    assert est.rate_per_day is None
    assert est.last_purchase is None
    assert stock_on(est, D0) == 0.0


def test_bulk_purchase_does_not_distort_rate() -> None:
    """Six packs every six weeks consume the same as one pack per week (SPEC §1)."""
    bulk = weekly(4, amount=6 * 1000, every=42)
    single = weekly(19, amount=1000)
    today = D0 + timedelta(days=130)
    r_bulk = estimate_consumption(bulk, today=today).rate_per_day
    r_single = estimate_consumption(single, today=today).rate_per_day
    assert r_bulk == pytest.approx(r_single)


def test_one_bulk_purchase_in_weekly_pattern_keeps_rate_close() -> None:
    purchases = weekly(5, 1000)
    # a bulk buy of 4 packs, then nothing for 4 weeks
    bulk_day = purchases[-1].day + timedelta(days=7)
    purchases.append(Purchase(bulk_day, 4000))
    purchases.append(Purchase(bulk_day + timedelta(days=28), 1000))
    est = estimate_consumption(purchases, today=bulk_day + timedelta(days=30))
    assert est.rate_per_day == pytest.approx(1000 / 7, rel=0.01)


def test_holiday_days_do_not_count() -> None:
    # bought 1000 on D0, two-week holiday, next purchase after 3 weeks
    pause = Pause(D0 + timedelta(days=3), D0 + timedelta(days=16))
    purchases = [Purchase(D0, 1000), Purchase(D0 + timedelta(days=21), 1000)]
    est = estimate_consumption(purchases, today=D0 + timedelta(days=22), pauses=[pause])
    assert est.rate_per_day == pytest.approx(1000 / 7)


def test_due_date_skips_holiday() -> None:
    purchases = weekly(4, 700)  # 100/day
    last = purchases[-1].day
    pause = Pause(last + timedelta(days=2), last + timedelta(days=5))  # 4 days away
    est = estimate_consumption(purchases, today=last, pauses=[pause])
    assert due_date(est, pauses=[pause]) == last + timedelta(days=11)


def test_outlier_interval_is_filtered() -> None:
    purchases = weekly(8, 1000)
    # one week a huge amount was bought for a party and finished in the same week
    party = purchases[4].day
    purchases[4] = Purchase(party, 9000)
    est = estimate_consumption(purchases, today=purchases[-1].day)
    assert est.rate_per_day == pytest.approx(1000 / 7)


def test_same_day_purchases_merge() -> None:
    merged = merge_same_day([Purchase(D0, 1), Purchase(D0, 2), Purchase(D0 + timedelta(1), 3)])
    assert merged == [Purchase(D0, 3), Purchase(D0 + timedelta(1), 3)]


def test_zero_amounts_ignored() -> None:
    assert merge_same_day([Purchase(D0, 0)]) == []


def test_recent_purchases_weigh_more() -> None:
    old = weekly(5, 700, start=D0)  # 100/day
    new_start = old[-1].day + timedelta(days=7)
    new = weekly(5, 1400, start=new_start)  # 200/day
    today = new[-1].day
    est = estimate_consumption(old + new, today=today)
    assert est.rate_per_day is not None
    assert 150 < est.rate_per_day < 200


def test_correction_factor_scales_rate() -> None:
    purchases = weekly(5, 700)
    today = purchases[-1].day
    base = estimate_consumption(purchases, today=today).rate_per_day
    more = estimate_consumption(purchases, today=today, correction=1.2).rate_per_day
    assert base is not None and more == pytest.approx(base * 1.2)


def test_stale_family_is_not_high_confidence() -> None:
    purchases = weekly(6, 1000)
    est = estimate_consumption(purchases, today=purchases[-1].day + timedelta(days=60))
    assert est.confidence is Confidence.LOW


def test_irregular_intervals_are_medium() -> None:
    days = [0, 3, 15, 18, 35]
    purchases = [Purchase(D0 + timedelta(days=d), 500) for d in days]
    est = estimate_consumption(purchases, today=D0 + timedelta(days=36))
    assert est.cv_interval is not None and est.cv_interval >= 0.35
    assert est.confidence is Confidence.MEDIUM


def test_carryover_pushes_due_date() -> None:
    purchases = weekly(4, 700)
    est = estimate_consumption(purchases, today=purchases[-1].day)
    assert due_date(est, carryover=700) == purchases[-1].day + timedelta(days=14)


def test_stock_on_never_negative() -> None:
    purchases = weekly(4, 700)
    est = estimate_consumption(purchases, today=purchases[-1].day)
    assert stock_on(est, purchases[-1].day + timedelta(days=3)) == pytest.approx(400)
    assert stock_on(est, purchases[-1].day + timedelta(days=30)) == 0.0


def test_active_days_and_add_active_days() -> None:
    pause = Pause(D0 + timedelta(days=1), D0 + timedelta(days=2))
    assert active_days(D0, D0 + timedelta(days=5), [pause]) == 3
    assert active_days(D0, D0) == 0
    assert add_active_days(D0, 3, [pause]) == D0 + timedelta(days=5)
    assert add_active_days(D0, 2.9) == D0 + timedelta(days=2)


@given(
    amount=st.floats(min_value=1, max_value=10_000),
    every=st.integers(min_value=1, max_value=60),
    n=st.integers(min_value=2, max_value=20),
)
def test_regular_pattern_rate_is_amount_over_interval(amount: float, every: int, n: int) -> None:
    purchases = weekly(n, amount, every=every)
    est = estimate_consumption(purchases, today=purchases[-1].day)
    assert est.rate_per_day == pytest.approx(amount / every)


@given(
    days=st.lists(st.integers(min_value=0, max_value=365), min_size=2, max_size=25, unique=True),
    amounts=st.lists(st.floats(min_value=1, max_value=5000), min_size=25, max_size=25),
    scale=st.floats(min_value=0.1, max_value=10),
)
def test_rate_scales_linearly_with_amounts(
    days: list[int], amounts: list[float], scale: float
) -> None:
    purchases = [
        Purchase(D0 + timedelta(days=d), a) for d, a in zip(sorted(days), amounts, strict=False)
    ]
    scaled = [Purchase(p.day, p.amount * scale) for p in purchases]
    today = D0 + timedelta(days=400)
    r1 = estimate_consumption(purchases, today=today).rate_per_day
    r2 = estimate_consumption(scaled, today=today).rate_per_day
    assert r1 is not None and r2 is not None
    assert r2 == pytest.approx(r1 * scale, rel=1e-6)
    assert r1 > 0
