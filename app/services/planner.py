"""Family statistics and the draft plan for the next delivery (fase 1: concept only)."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (
    FamilyMember,
    FamilyStats,
    Household,
    Pause,
    Plan,
    PlanLine,
    Product,
    ProductFamily,
    Purchase,
)
from app.domain import consumption as cm
from app.domain.consumption import ModelParams, estimate_consumption, stock_on
from app.domain.planning import FamilyInput, PlanSettings, Reason, decide
from app.domain.tiers import assign_tier, usual_packs

WEEKDAYS_NL = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MANUAL = "manual"  # reason_code of lines added by a person; kept across rebuilds


def plan_settings(household: Household) -> PlanSettings:
    s: dict[str, Any] = household.settings_json or {}
    return PlanSettings(
        cadence_days=int(s.get("cadence_days", 7)),
        margin_days=int(s.get("margin_days", 2)),
        model=ModelParams(half_life_days=float(s.get("half_life_days", 60))),
    )


def pauses_for(session: Session, household_id: int) -> list[cm.Pause]:
    rows = session.scalars(select(Pause).where(Pause.household_id == household_id))
    return [cm.Pause(r.start, r.end) for r in rows]


def _base_amount(product: Product, base_unit: str) -> float | None:
    """Base units in one pack, or None if the product can't be expressed in this unit."""
    if product.unit == base_unit and product.unit_amount:
        return product.unit_amount
    if base_unit == "st" and product.unit is None:
        return 1.0
    return None


@dataclass(frozen=True)
class FamilyData:
    family: ProductFamily
    purchases: list[cm.Purchase]
    order_product: Product | None
    pack_size: float


def load_family(session: Session, family: ProductFamily) -> FamilyData:
    rows = session.execute(
        select(Purchase, Product)
        .join(Product, Product.ah_id == Purchase.ah_product_id)
        .join(FamilyMember, FamilyMember.ah_product_id == Purchase.ah_product_id)
        .where(
            FamilyMember.family_id == family.id,
            Purchase.household_id == family.household_id,
            Purchase.source == "staple",
        )
        .order_by(Purchase.delivered_at)
    ).all()
    purchases: list[cm.Purchase] = []
    last_bought: Product | None = None
    for purchase, product in rows:
        per_pack = _base_amount(product, family.base_unit)
        if per_pack is None:
            continue
        purchases.append(cm.Purchase(purchase.delivered_at, purchase.qty * per_pack))
        last_bought = product

    preferred = session.scalar(
        select(Product)
        .join(FamilyMember, FamilyMember.ah_product_id == Product.ah_id)
        .where(FamilyMember.family_id == family.id, FamilyMember.preferred.is_(True))
    )
    order_product = preferred or last_bought
    pack = _base_amount(order_product, family.base_unit) if order_product else None
    return FamilyData(family, purchases, order_product, pack or 1.0)


def recompute_stats(session: Session, household: Household, *, today: date) -> int:
    settings = plan_settings(household)
    pauses = pauses_for(session, household.id)
    families = session.scalars(
        select(ProductFamily).where(ProductFamily.household_id == household.id)
    ).all()
    for family in families:
        data = load_family(session, family)
        est = estimate_consumption(
            data.purchases,
            today=today,
            pauses=pauses,
            params=settings.model,
            correction=family.correction,
        )
        stats = session.get(FamilyStats, family.id) or FamilyStats(family_id=family.id)
        stats.rate_per_day = est.rate_per_day
        stats.cv_interval = est.cv_interval
        stats.n_purchases = est.n_purchases
        stats.last_purchase_at = est.last_purchase
        stats.est_stock = stock_on(est, today, carryover=family.carryover, pauses=pauses)
        stats.due_date = cm.due_date(est, carryover=family.carryover, pauses=pauses)
        stats.confidence = est.confidence.value
        stats.computed_at = datetime.now(UTC)
        session.add(stats)
    session.flush()
    return len(families)


def reason_text(reason: Reason, due: date | None, today: date | None = None) -> str:
    if due is None:
        return "Vastgepind" if reason is Reason.PINNED_DUE else "Voorraad raakt op"
    when = f"{WEEKDAYS_NL[due.weekday()]} {due.day}-{due.month}"
    stock = (
        f"Waarschijnlijk al op sinds {when}"
        if today is not None and due < today
        else f"Voorraad op rond {when}"
    )
    match reason:
        case Reason.DUE:
            return stock
        case Reason.PINNED_DUE:
            return f"Vastgepind. {stock}"
        case _:
            return ""


def build_draft_plan(
    session: Session,
    household: Household,
    *,
    delivery_date: date,
    today: date,
    ah_order_id: int | None = None,
    cutoff: datetime | None = None,
) -> Plan:
    """(Re)build the draft for this delivery. Applied plans are never touched.

    Lines a person added by hand (reason "manual") survive a rebuild.
    """
    existing = session.scalar(
        select(Plan).where(Plan.household_id == household.id, Plan.delivery_date == delivery_date)
    )
    if existing is not None and existing.status != "draft":
        return existing
    manual_families: set[int] = set()
    if existing is not None:
        manual_families = set(
            session.scalars(
                select(PlanLine.family_id).where(
                    PlanLine.plan_id == existing.id, PlanLine.reason_code == MANUAL
                )
            )
        )
        session.execute(
            delete(PlanLine).where(PlanLine.plan_id == existing.id, PlanLine.reason_code != MANUAL)
        )
        session.expire(existing, ["lines"])
        plan = existing
    else:
        plan = Plan(household_id=household.id, delivery_date=delivery_date, status="draft")
        session.add(plan)
    plan.ah_order_id = ah_order_id
    plan.cutoff = cutoff
    session.flush()

    settings = plan_settings(household)
    pauses = pauses_for(session, household.id)
    families = session.scalars(
        select(ProductFamily).where(ProductFamily.household_id == household.id)
    ).all()
    for family in families:
        if family.id in manual_families:
            continue
        data = load_family(session, family)
        if data.order_product is None:
            continue
        decision = decide(
            FamilyInput(
                purchases=data.purchases,
                pack_size=data.pack_size,
                carryover=family.carryover,
                correction=family.correction,
                pinned=family.pinned,
                excluded=family.excluded,
            ),
            next_delivery=delivery_date,
            today=today,
            pauses=pauses,
            settings=settings,
        )
        if not decision.include:
            continue
        tier = assign_tier(
            reason=decision.reason,
            confidence=decision.estimate.confidence,
            packs=decision.packs,
            usual=usual_packs([p.amount for p in data.purchases], data.pack_size),
        )
        session.add(
            PlanLine(
                plan_id=plan.id,
                family_id=family.id,
                ah_product_id=data.order_product.ah_id,
                qty=decision.packs,
                reason_code=decision.reason.value,
                reason_text=reason_text(decision.reason, decision.due, today),
                tier=tier.value,
            )
        )
    session.flush()
    return plan
