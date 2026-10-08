"""Read model and actions for the "Deze week" screen."""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    ActionLog,
    FamilyStats,
    Feedback,
    Household,
    PlanLine,
    PriceObservation,
    Product,
    ProductFamily,
)
from app.db.models import Plan as PlanRow
from app.domain import consumption as cm
from app.domain.feedback import FamilyState, FeedbackKind, apply_feedback
from app.services.planner import MANUAL, load_family, pauses_for, plan_settings


@dataclass(frozen=True)
class LineView:
    id: int
    family_id: int
    family_name: str
    title: str
    unit_size: str | None
    qty: int
    reason: str
    reason_code: str
    tier: str
    price: float | None
    is_bonus: bool
    bonus_mechanism: str | None
    confidence: str
    n_purchases: int
    due: date | None
    stock_days: float | None  # days of stock left on delivery day

    @property
    def line_total(self) -> float | None:
        return round(self.price * self.qty, 2) if self.price is not None else None

    @property
    def stock_fill(self) -> int:
        """Width (%) of the stock line: a full week of stock fills it; empty is empty."""
        if self.stock_days is None or self.stock_days <= 0:
            return 0
        return max(4, min(100, round(self.stock_days / 7 * 100)))


@dataclass(frozen=True)
class SuggestionView:
    family_id: int
    name: str
    n_purchases: int
    due: date | None
    confidence: str


@dataclass(frozen=True)
class WeekView:
    plan: PlanRow | None
    lines: list[LineView]
    suggestions: list[SuggestionView]

    @property
    def total(self) -> float:
        return round(sum(line.line_total or 0 for line in self.lines), 2)


def latest_plan(session: Session, household_id: int) -> PlanRow | None:
    return session.scalar(
        select(PlanRow)
        .where(PlanRow.household_id == household_id)
        .order_by(PlanRow.delivery_date.desc())
        .limit(1)
    )


def _latest_price(session: Session, product_id: int) -> PriceObservation | None:
    return session.scalar(
        select(PriceObservation)
        .where(PriceObservation.ah_product_id == product_id)
        .order_by(PriceObservation.observed_on.desc())
        .limit(1)
    )


def line_view(session: Session, line: PlanLine, delivery: date) -> LineView:
    product = session.get(Product, line.ah_product_id)
    family = session.get(ProductFamily, line.family_id)
    stats = session.get(FamilyStats, line.family_id)
    assert product is not None and family is not None
    price = _latest_price(session, product.ah_id)
    stock_days = None
    if stats and stats.rate_per_day and stats.due_date:
        stock_days = (stats.due_date - delivery).days
    return LineView(
        id=line.id,
        family_id=family.id,
        family_name=family.name,
        title=product.title,
        unit_size=product.unit_size_text,
        qty=line.qty,
        reason=line.reason_text,
        reason_code=line.reason_code,
        tier=line.tier,
        price=price.price if price else None,
        is_bonus=bool(price and price.is_bonus),
        bonus_mechanism=price.bonus_mechanism if price else None,
        confidence=stats.confidence if stats else "low",
        n_purchases=stats.n_purchases if stats else 0,
        due=stats.due_date if stats else None,
        stock_days=stock_days,
    )


def week_view(session: Session, household_id: int, *, max_suggestions: int = 12) -> WeekView:
    plan = latest_plan(session, household_id)
    if plan is None:
        return WeekView(None, [], [])
    lines = sorted(
        (line_view(session, line, plan.delivery_date) for line in plan.lines),
        key=lambda v: (v.due or date.max, v.title),
    )
    in_plan = {line.family_id for line in plan.lines}
    rows = session.execute(
        select(ProductFamily, FamilyStats)
        .join(FamilyStats, FamilyStats.family_id == ProductFamily.id)
        .where(
            ProductFamily.household_id == household_id,
            ProductFamily.excluded.is_(False),
            FamilyStats.n_purchases >= 2,
        )
        .order_by(FamilyStats.n_purchases.desc(), ProductFamily.name)
    ).all()
    suggestions = [
        SuggestionView(f.id, f.name, s.n_purchases, s.due_date, s.confidence)
        for f, s in rows
        if f.id not in in_plan
    ][:max_suggestions]
    return WeekView(plan, lines, suggestions)


class NotFound(LookupError):
    pass


def _line_for_household(session: Session, line_id: int, household_id: int) -> PlanLine:
    line = session.get(PlanLine, line_id)
    if line is None or line.plan.household_id != household_id:
        raise NotFound(line_id)
    return line


def give_feedback(
    session: Session, *, household_id: int, user_id: int, line_id: int, kind: FeedbackKind
) -> PlanLine | None:
    """Apply a feedback button. Returns the line, or None if it left the draft."""
    line = _line_for_household(session, line_id, household_id)
    family = session.get(ProductFamily, line.family_id)
    assert family is not None
    data = load_family(session, family)
    extra = None
    if kind is FeedbackKind.ENOUGH_STOCK:
        extra = _stock_to_skip_delivery(session, family, data.purchases, line.plan.delivery_date)
    effect = apply_feedback(
        kind,
        FamilyState(family.correction, family.carryover, family.excluded),
        line_qty=line.qty,
        pack_size=data.pack_size,
        enough_stock_extra=extra,
    )
    family.correction = effect.family.correction
    family.carryover = effect.family.carryover
    family.excluded = effect.family.excluded
    session.add(Feedback(family_id=family.id, user_id=user_id, kind=kind.value))
    session.add(
        ActionLog(
            household_id=household_id,
            action=f"feedback.{kind.value}",
            payload_json={"line_id": line.id, "family_id": family.id, "qty": effect.line_qty},
            undo_payload_json={"family_id": family.id, "qty": line.qty},
        )
    )
    if effect.line_qty <= 0:
        session.delete(line)
        session.flush()
        return None
    line.qty = effect.line_qty
    session.flush()
    return line


def _stock_to_skip_delivery(
    session: Session,
    family: ProductFamily,
    purchases: list[cm.Purchase],
    delivery: date,
) -> float | None:
    """Extra carryover so the family is covered until after the next planning horizon.

    Covers consumption from the last purchase up to delivery plus cadence and margin, plus
    one day so rounding never pulls it back into the plan.
    """
    household = session.get(Household, family.household_id)
    assert household is not None
    settings = plan_settings(household)
    pauses = pauses_for(session, household.id)
    est = cm.estimate_consumption(
        purchases,
        today=date.today(),
        pauses=pauses,
        params=settings.model,
        correction=family.correction,
    )
    if not est.rate_per_day or est.last_purchase is None:
        return None
    days = (
        cm.active_days(est.last_purchase, delivery, pauses)
        + settings.cadence_days
        + settings.margin_days
        + 1
    )
    return max(est.rate_per_day * days - est.last_amount - family.carryover, 0.0)


def add_family_to_plan(
    session: Session, *, household_id: int, family_id: int, qty: int = 1
) -> PlanLine:
    """Manual addition to the draft (counts as a proposal, never automatic)."""
    plan = latest_plan(session, household_id)
    family = session.get(ProductFamily, family_id)
    if plan is None or family is None or family.household_id != household_id:
        raise NotFound(family_id)
    existing = session.scalar(
        select(PlanLine).where(PlanLine.plan_id == plan.id, PlanLine.family_id == family_id)
    )
    if existing is not None:
        return existing
    data = load_family(session, family)
    if data.order_product is None:
        raise NotFound(family_id)
    line = PlanLine(
        plan_id=plan.id,
        family_id=family_id,
        ah_product_id=data.order_product.ah_id,
        qty=max(qty, 1),
        reason_code=MANUAL,
        reason_text="Zelf toegevoegd",
        tier="propose",
    )
    session.add(line)
    session.flush()
    return line


def reset_carryover_after_purchase(session: Session, household_id: int) -> int:
    """A real purchase replaces "nog genoeg": drop carryover once the family is bought again."""
    last_enough = (
        select(Feedback.family_id, func.max(Feedback.at).label("at"))
        .where(Feedback.kind == FeedbackKind.ENOUGH_STOCK.value)
        .group_by(Feedback.family_id)
        .subquery()
    )
    rows = session.execute(
        select(ProductFamily, FamilyStats, last_enough.c.at)
        .join(FamilyStats, FamilyStats.family_id == ProductFamily.id)
        .join(last_enough, last_enough.c.family_id == ProductFamily.id, isouter=True)
        .where(ProductFamily.household_id == household_id, ProductFamily.carryover > 0)
    ).all()
    reset = 0
    for family, stats, at in rows:
        given = at.date() if isinstance(at, datetime) else None
        if stats.last_purchase_at and (given is None or stats.last_purchase_at > given):
            family.carryover = 0.0
            reset += 1
    session.flush()
    return reset
