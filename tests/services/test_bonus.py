from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ah.models import BonusMetadata, BonusPeriod
from app.ah.models import Product as AhProduct
from app.db.models import BonusOffer, Household, PlanLine, PriceObservation, Product, Purchase
from app.services import bonus
from app.services.families import assign_families
from app.services.planner import build_draft_plan, recompute_stats

TODAY = date(2026, 10, 8)
MILK, MILK_OTHER, CHIPS, KNAKS = 1525, 1600, 1700, 1800


def ah(pid: int, title: str, *, bonus_text: str | None = None, price: float = 1.0) -> AhProduct:
    return AhProduct.model_validate(
        {
            "webshopId": pid,
            "title": title,
            "salesUnitSize": "1 l",
            "currentPrice": price * 0.75 if bonus_text else price,
            "priceBeforeBonus": price,
            "isBonus": bool(bonus_text),
            "bonusMechanism": bonus_text,
        }
    )


class FakeBonusAh:
    def __init__(self, results: dict[str, list[AhProduct]], *, in_period: bool = True) -> None:
        self.results = results
        self.in_period = in_period
        self.queries: list[str] = []

    async def get_bonus_metadata(self) -> BonusMetadata:
        start = TODAY - timedelta(days=3) if self.in_period else TODAY + timedelta(days=10)
        return BonusMetadata(
            periods=[BonusPeriod(bonus_start_date=start, bonus_end_date=start + timedelta(days=6))]
        )

    async def search_products(self, query: str, *, size: int = 30) -> list[AhProduct]:
        self.queries.append(query)
        return self.results.get(query, [])


def seed(s: Session, household_id: int) -> None:
    """Milk bought weekly (6x), knaks twice; both in families."""
    for pid, title, brand in [(MILK, "AH Halfvolle melk", "AH"), (KNAKS, "Unox Knaks", "Unox")]:
        s.add(
            Product(
                ah_id=pid,
                title=title,
                brand=brand,
                unit_size_text="1 l",
                unit_amount=1000,
                unit="ml",
            )
        )
    s.flush()
    for i in range(6):
        s.add(
            Purchase(
                household_id=household_id,
                ah_order_id=10 + i,
                ah_product_id=MILK,
                qty=2,
                delivered_at=TODAY - timedelta(days=7 * (6 - i)),
            )
        )
    for i in range(2):
        s.add(
            Purchase(
                household_id=household_id,
                ah_order_id=10 + i,
                ah_product_id=KNAKS,
                qty=1,
                delivered_at=TODAY - timedelta(days=14 * (2 - i)),
            )
        )
    s.flush()
    assign_families(s, household_id)
    h = s.get(Household, household_id)
    assert h is not None
    recompute_stats(s, h, today=TODAY)
    build_draft_plan(s, h, delivery_date=TODAY + timedelta(days=3), today=TODAY)


async def test_refresh_stores_own_and_similar_bonus_sorted(
    sessions: sessionmaker[Session], household: Household
) -> None:
    fake = FakeBonusAh(
        {
            "Halfvolle melk": [
                ah(MILK, "AH Halfvolle melk", bonus_text="2e halve prijs"),
                ah(MILK_OTHER, "Campina Halfvolle melk", bonus_text="25% korting"),
                ah(CHIPS, "Lays chips"),  # not on bonus: ignored
            ],
            "Knaks": [ah(KNAKS + 1, "Zwan knakworst", bonus_text="1+1 gratis")],
        }
    )
    with sessions.begin() as s:
        seed(s, household.id)
        result = await bonus.refresh_bonus(fake, s, household_id=household.id, today=TODAY)
    assert result.own == 1 and result.similar == 2 and result.searches == 2
    assert fake.queries == ["Halfvolle melk", "Knaks"]  # most bought family first
    with sessions() as s:
        view = bonus.bonus_view(s, household.id, today=TODAY)
        titles = [o.title for o in view.offers]
        assert titles == ["AH Halfvolle melk", "Campina Halfvolle melk", "Zwan knakworst"]
        assert view.offers[0].own and view.offers[0].times_bought == 6
        assert view.offers[1].family_name == "Halfvolle melk"
        # bonus products also feed the price history
        obs = s.get(PriceObservation, (MILK_OTHER, TODAY))
        assert obs is not None and obs.is_bonus


async def test_refresh_replaces_previous_run(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        seed(s, household.id)
        await bonus.refresh_bonus(
            FakeBonusAh({"Halfvolle melk": [ah(MILK_OTHER, "Campina melk", bonus_text="x")]}),
            s,
            household_id=household.id,
            today=TODAY,
        )
        await bonus.refresh_bonus(FakeBonusAh({}), s, household_id=household.id, today=TODAY)
    with sessions() as s:
        assert s.scalars(select(BonusOffer)).all() == []


async def test_no_current_period_does_nothing(
    sessions: sessionmaker[Session], household: Household
) -> None:
    fake = FakeBonusAh({}, in_period=False)
    with sessions.begin() as s:
        seed(s, household.id)
        result = await bonus.refresh_bonus(fake, s, household_id=household.id, today=TODAY)
    assert result.week == "" and fake.queries == []


async def test_search_count_is_capped(
    sessions: sessionmaker[Session], household: Household, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bonus, "MAX_FAMILIES_SEARCHED", 1)
    fake = FakeBonusAh({})
    with sessions.begin() as s:
        seed(s, household.id)
        await bonus.refresh_bonus(fake, s, household_id=household.id, today=TODAY)
    assert fake.queries == ["Halfvolle melk"]


async def test_add_bonus_product_to_plan_as_manual_line(
    sessions: sessionmaker[Session], household: Household
) -> None:
    fake = FakeBonusAh(
        {"Halfvolle melk": [ah(MILK_OTHER, "Campina Halfvolle melk", bonus_text="25% korting")]}
    )
    with sessions.begin() as s:
        seed(s, household.id)
        await bonus.refresh_bonus(fake, s, household_id=household.id, today=TODAY)
        offer = s.scalars(select(BonusOffer)).one()
        line = bonus.add_offer_to_plan(s, household_id=household.id, offer_id=offer.id)
        again = bonus.add_offer_to_plan(s, household_id=household.id, offer_id=offer.id)
        assert again.id == line.id
        assert line.ah_product_id == MILK_OTHER and line.reason_text == "Bonus: 25% korting"
        assert s.scalars(select(PlanLine).where(PlanLine.reason_code == "manual")).one()
        assert bonus.bonus_view(s, household.id, today=TODAY).offers[0].in_plan
