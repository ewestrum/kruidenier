from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    FamilyStats,
    Household,
    Pause,
    Plan,
    PriceObservation,
    Product,
    ProductFamily,
    Purchase,
)
from app.services.families import assign_families
from app.services.history import import_history
from app.services.planner import build_draft_plan, recompute_stats
from app.services.prices import log_prices
from tests.services.conftest import FakeAh

MILK = 1525
D0 = date(2026, 8, 2)


def seed_weekly_milk(s: Session, household_id: int, weeks: int = 6, qty: int = 2) -> None:
    s.add(
        Product(
            ah_id=MILK,
            title="AH Halfvolle melk",
            brand="AH",
            unit_size_text="1 l",
            unit_amount=1000,
            unit="ml",
        )
    )
    for i in range(weeks):
        s.add(
            Purchase(
                household_id=household_id,
                ah_order_id=100 + i,
                ah_product_id=MILK,
                qty=qty,
                delivered_at=D0 + timedelta(weeks=i),
            )
        )
    s.flush()
    assign_families(s, household_id)


def test_draft_plan_contains_due_staple(
    sessions: sessionmaker[Session], household: Household
) -> None:
    last = D0 + timedelta(weeks=5)
    delivery = last + timedelta(days=7)
    with sessions.begin() as s:
        seed_weekly_milk(s, household.id)
        h = s.get(Household, household.id)
        assert h is not None
        recompute_stats(s, h, today=last + timedelta(days=5))
        plan = build_draft_plan(
            s, h, delivery_date=delivery, today=last + timedelta(days=5), ah_order_id=999
        )
        assert plan.status == "draft" and plan.ah_order_id == 999
        [line] = plan.lines
        assert line.ah_product_id == MILK
        assert line.qty == 3  # 2 l/week, 9-day horizon, 1 l packs
        assert line.tier == "propose"
        assert line.reason_text.startswith("Voorraad op rond")
        stats = s.get(FamilyStats, line.family_id)
        assert stats is not None and stats.confidence == "high"
        assert stats.rate_per_day is not None and abs(stats.rate_per_day - 2000 / 7) < 1e-6


def test_rebuilding_draft_replaces_lines(
    sessions: sessionmaker[Session], household: Household
) -> None:
    last = D0 + timedelta(weeks=5)
    with sessions.begin() as s:
        seed_weekly_milk(s, household.id)
        h = s.get(Household, household.id)
        assert h is not None
        for _ in range(2):
            build_draft_plan(s, h, delivery_date=last + timedelta(days=7), today=last)
        plans = s.scalars(select(Plan)).all()
        assert len(plans) == 1 and len(plans[0].lines) == 1


def test_applied_plan_is_never_rebuilt(
    sessions: sessionmaker[Session], household: Household
) -> None:
    last = D0 + timedelta(weeks=5)
    with sessions.begin() as s:
        seed_weekly_milk(s, household.id)
        h = s.get(Household, household.id)
        assert h is not None
        plan = build_draft_plan(s, h, delivery_date=last + timedelta(days=7), today=last)
        plan.status = "applied"
        plan.lines[0].qty = 7
        again = build_draft_plan(s, h, delivery_date=last + timedelta(days=7), today=last)
        assert again.id == plan.id and again.lines[0].qty == 7


def test_excluded_and_holiday(sessions: sessionmaker[Session], household: Household) -> None:
    last = D0 + timedelta(weeks=5)
    delivery = last + timedelta(days=7)
    with sessions.begin() as s:
        seed_weekly_milk(s, household.id)
        h = s.get(Household, household.id)
        assert h is not None
        s.add(Pause(household_id=h.id, start=delivery - timedelta(days=2), end=delivery))
        s.flush()
        assert build_draft_plan(s, h, delivery_date=delivery, today=last).lines == []


def test_household_settings_change_horizon(
    sessions: sessionmaker[Session], household: Household
) -> None:
    last = D0 + timedelta(weeks=5)
    with sessions.begin() as s:
        seed_weekly_milk(s, household.id)
        h = s.get(Household, household.id)
        assert h is not None
        h.settings_json = {"cadence_days": 14, "margin_days": 0}
        plan = build_draft_plan(s, h, delivery_date=last + timedelta(days=7), today=last)
        assert plan.lines[0].qty == 4  # 2 l/week over 14 days


async def test_recorded_history_gives_no_lines_below_noise_threshold(
    sessions: sessionmaker[Session], household: Household
) -> None:
    """Two recorded orders: no family reaches 3 purchases (SPEC §6.10), unless pinned."""
    with sessions.begin() as s:
        await import_history(FakeAh(), s, household_id=household.id, since=date(2026, 1, 1))
        assign_families(s, household.id)
        h = s.get(Household, household.id)
        assert h is not None
        plan = build_draft_plan(s, h, delivery_date=date(2026, 10, 11), today=date(2026, 10, 7))
        assert plan.lines == []
        assert s.scalars(select(ProductFamily)).first() is not None


async def test_price_logger_records_and_overwrites_same_day(
    sessions: sessionmaker[Session],
) -> None:
    fake = FakeAh()
    today = date(2026, 10, 7)
    with sessions.begin() as s:
        result = await log_prices(fake, s, on=today, product_ids=[609672, 123])
    assert result.observed == 1 and result.missing == [123]
    with sessions.begin() as s:
        obs = s.get(PriceObservation, (609672, today))
        assert obs is not None
        assert obs.price == 5.1 and obs.regular_price == 5.37
        product = s.get(Product, 609672)
        assert product is not None and product.unit == "st" and product.unit_amount == 3
        obs.price = 0.0
    with sessions.begin() as s:
        await log_prices(fake, s, on=today, product_ids=[609672])
    with sessions() as s:
        obs = s.get(PriceObservation, (609672, today))
        assert obs is not None and obs.price == 5.1
        assert len(s.scalars(select(PriceObservation)).all()) == 1


async def test_price_logger_batches_requests(sessions: sessionmaker[Session]) -> None:
    fake = FakeAh()
    with sessions.begin() as s:
        await log_prices(fake, s, on=date(2026, 10, 7), product_ids=list(range(1, 66)))
    assert fake.calls == ["get_products:30", "get_products:30", "get_products:5"]
