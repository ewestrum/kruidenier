"""Bonus offers relevant to a household: its own products on bonus, and similar products.

Only verified endpoints are used (docs/ah-api.md): `bonus.metadata` for the period and
`product.search` with each family's name to find similar products on bonus. The bonus
section endpoints stay unused until a probe has confirmed their shape.

Real-discount assessment (SPEC §7) is fase 3; here AH's own mechanism text is shown.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ah.models import BonusMetadata
from app.ah.models import Product as AhProduct
from app.db.models import (
    BonusOffer,
    FamilyMember,
    FamilyStats,
    PlanLine,
    PriceObservation,
    Product,
    ProductFamily,
    Purchase,
)
from app.services.planner import MANUAL
from app.services.prices import record_observation
from app.services.week import NotFound, latest_plan

log = logging.getLogger(__name__)

MAX_FAMILIES_SEARCHED = 25  # ~25 requests at 1/s per household per day
SEARCH_SIZE = 30


class BonusSource(Protocol):
    async def get_bonus_metadata(self) -> BonusMetadata: ...
    async def search_products(self, query: str, *, size: int = 30) -> list[AhProduct]: ...


@dataclass
class BonusRefresh:
    week: str
    own: int = 0
    similar: int = 0
    searches: int = 0


def _current_period(meta: BonusMetadata, today: date) -> tuple[date, date] | None:
    for period in meta.periods:
        if period.bonus_start_date <= today <= period.bonus_end_date:
            return period.bonus_start_date, period.bonus_end_date
    return None


def _raw(p: AhProduct) -> dict[str, object]:
    return {
        "title": p.title,
        "unit_size": p.sales_unit_size,
        "price": p.current_price,
        "price_before": p.price_before_bonus,
        "category": p.main_category,
    }


async def refresh_bonus(
    client: BonusSource, session: Session, *, household_id: int, today: date
) -> BonusRefresh:
    """Rebuild this household's offers for the current bonus period (sequential requests)."""
    period = _current_period(await client.get_bonus_metadata(), today)
    if period is None:
        log.info("no current bonus period for %s", today)
        return BonusRefresh(week="")
    start, end = period
    week = start.isoformat()
    result = BonusRefresh(week=week)
    session.execute(
        delete(BonusOffer).where(BonusOffer.household_id == household_id, BonusOffer.week == week)
    )

    families = session.execute(
        select(ProductFamily, FamilyStats)
        .join(FamilyStats, FamilyStats.family_id == ProductFamily.id)
        .where(ProductFamily.household_id == household_id, ProductFamily.excluded.is_(False))
        .order_by(FamilyStats.n_purchases.desc(), ProductFamily.name)
    ).all()
    member_family = {
        m.ah_product_id: m.family_id
        for m in session.scalars(
            select(FamilyMember)
            .join(ProductFamily)
            .where(ProductFamily.household_id == household_id)
        )
    }
    seen: set[int] = set()

    def add(p: AhProduct, family_id: int) -> None:
        if not p.is_bonus or not p.bonus_mechanism or p.webshop_id in seen:
            return
        record_observation(session, p, on=today)  # also feeds the price history (SPEC §7)
        own = p.webshop_id in member_family
        session.add(
            BonusOffer(
                household_id=household_id,
                family_id=member_family.get(p.webshop_id, family_id),
                source="own" if own else "similar",
                week=week,
                period_end=end,
                ah_product_id=p.webshop_id,
                mechanism=p.bonus_mechanism[:100],
                raw_json=_raw(p),
            )
        )
        seen.add(p.webshop_id)
        if own:
            result.own += 1
        else:
            result.similar += 1

    for family, _stats in families[:MAX_FAMILIES_SEARCHED]:
        found = await client.search_products(family.name, size=SEARCH_SIZE)
        result.searches += 1
        for p in found:
            add(p, family.id)
        session.flush()

    # Own products on bonus that the name search did not surface: from today's price log.
    own_today = session.execute(
        select(PriceObservation, Product)
        .join(Product, Product.ah_id == PriceObservation.ah_product_id)
        .where(PriceObservation.observed_on == today, PriceObservation.is_bonus.is_(True))
    ).all()
    for obs, product in own_today:
        family_id = member_family.get(product.ah_id)
        if family_id is None or product.ah_id in seen or not obs.bonus_mechanism:
            continue
        session.add(
            BonusOffer(
                household_id=household_id,
                family_id=family_id,
                source="own",
                week=week,
                period_end=end,
                ah_product_id=product.ah_id,
                mechanism=obs.bonus_mechanism[:100],
                raw_json={
                    "title": product.title,
                    "unit_size": product.unit_size_text,
                    "price": obs.price,
                    "price_before": obs.regular_price,
                },
            )
        )
        seen.add(product.ah_id)
        result.own += 1
    session.flush()
    return result


def _text(value: object) -> str | None:
    return str(value) if value else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


@dataclass(frozen=True)
class OfferView:
    id: int
    title: str
    unit_size: str | None
    mechanism: str
    price: float | None
    price_before: float | None
    own: bool
    family_id: int | None
    family_name: str | None
    family_purchases: int
    times_bought: int
    in_plan: bool


@dataclass
class BonusView:
    week: str | None
    period_end: date | None
    offers: list[OfferView] = field(default_factory=list)


def bonus_view(session: Session, household_id: int, *, today: date) -> BonusView:
    current = session.scalar(
        select(BonusOffer.week)
        .where(BonusOffer.household_id == household_id)
        .where((BonusOffer.period_end.is_(None)) | (BonusOffer.period_end >= today))
        .order_by(BonusOffer.week.desc())
        .limit(1)
    )
    if current is None:
        return BonusView(None, None)
    rows: Sequence[BonusOffer] = session.scalars(
        select(BonusOffer).where(
            BonusOffer.household_id == household_id, BonusOffer.week == current
        )
    ).all()
    plan = latest_plan(session, household_id)
    in_plan_products = {line.ah_product_id for line in plan.lines} if plan else set()
    families = {
        f.id: (f, s)
        for f, s in session.execute(
            select(ProductFamily, FamilyStats)
            .join(FamilyStats, FamilyStats.family_id == ProductFamily.id, isouter=True)
            .where(ProductFamily.household_id == household_id)
        ).all()
    }
    bought: dict[int, int] = {}
    for pid in session.scalars(
        select(Purchase.ah_product_id).where(Purchase.household_id == household_id)
    ):
        bought[pid] = bought.get(pid, 0) + 1

    offers = []
    for r in rows:
        fam, stats = families.get(r.family_id or -1, (None, None))
        raw = r.raw_json or {}
        offers.append(
            OfferView(
                id=r.id,
                title=str(raw.get("title") or r.ah_product_id),
                unit_size=_text(raw.get("unit_size")),
                mechanism=r.mechanism,
                price=_number(raw.get("price")),
                price_before=_number(raw.get("price_before")),
                own=r.source == "own",
                family_id=r.family_id,
                family_name=fam.name if fam else None,
                family_purchases=stats.n_purchases if stats else 0,
                times_bought=bought.get(r.ah_product_id, 0),
                in_plan=r.ah_product_id in in_plan_products,
            )
        )
    # Products you buy first, then by how often you buy the family, then by name.
    offers.sort(key=lambda o: (not o.own, -o.times_bought, -o.family_purchases, o.title))
    period_end = rows[0].period_end if rows else None
    return BonusView(current, period_end, offers)


def add_offer_to_plan(session: Session, *, household_id: int, offer_id: int) -> PlanLine:
    """Put this bonus product in the draft, as a manual line of its family."""
    offer = session.get(BonusOffer, offer_id)
    plan = latest_plan(session, household_id)
    if offer is None or offer.household_id != household_id or offer.family_id is None:
        raise NotFound(offer_id)
    if plan is None:
        raise NotFound(offer_id)
    existing = session.scalar(
        select(PlanLine).where(
            PlanLine.plan_id == plan.id, PlanLine.ah_product_id == offer.ah_product_id
        )
    )
    if existing is not None:
        return existing
    line = PlanLine(
        plan_id=plan.id,
        family_id=offer.family_id,
        ah_product_id=offer.ah_product_id,
        qty=1,
        reason_code=MANUAL,
        reason_text=f"Bonus: {offer.mechanism}",
        tier="propose",
    )
    session.add(line)
    session.flush()
    return line
