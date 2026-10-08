"""Should a family be on next delivery's order, and how many packs? (SPEC §6.4, §6.5, §6.10)"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from app.domain.consumption import (
    ConsumptionEstimate,
    ModelParams,
    Pause,
    Purchase,
    due_date,
    estimate_consumption,
    stock_on,
)


class Reason(StrEnum):
    DUE = "due"
    PINNED_DUE = "pinned_due"
    NOT_DUE = "not_due"
    EXCLUDED = "excluded"
    TOO_FEW_PURCHASES = "too_few_purchases"
    NO_RATE = "no_rate"
    PAUSED = "paused"
    ENOUGH_STOCK = "enough_stock"


@dataclass(frozen=True)
class PlanSettings:
    cadence_days: int = 7
    margin_days: int = 2
    noise_window_days: int = 180
    noise_min_purchases: int = 3
    max_packs_per_line: int = 12
    model: ModelParams = field(default_factory=ModelParams)


@dataclass(frozen=True)
class FamilyInput:
    purchases: Sequence[Purchase]
    pack_size: float  # base units in one pack of the SKU we would order
    carryover: float = 0.0
    correction: float = 1.0
    pinned: bool = False
    excluded: bool = False


@dataclass(frozen=True)
class PlanDecision:
    include: bool
    packs: int
    reason: Reason
    estimate: ConsumptionEstimate
    due: date | None
    stock_at_delivery: float


def decide(
    family: FamilyInput,
    *,
    next_delivery: date,
    today: date,
    pauses: Sequence[Pause] = (),
    settings: PlanSettings | None = None,
) -> PlanDecision:
    settings = settings or PlanSettings()
    est = estimate_consumption(
        family.purchases,
        today=today,
        pauses=pauses,
        params=settings.model,
        correction=family.correction,
    )

    def no(reason: Reason, due: date | None = None, stock: float = 0.0) -> PlanDecision:
        return PlanDecision(False, 0, reason, est, due, stock)

    if family.excluded:
        return no(Reason.EXCLUDED)
    if any(p.contains(next_delivery) for p in pauses):
        return no(Reason.PAUSED)

    window_start = today - timedelta(days=settings.noise_window_days)
    recent = sum(1 for p in family.purchases if p.day >= window_start and p.amount > 0)
    if recent < settings.noise_min_purchases and not family.pinned:
        return no(Reason.TOO_FEW_PURCHASES)
    if est.rate_per_day is None or est.rate_per_day <= 0:
        return no(Reason.NO_RATE)

    due = due_date(est, carryover=family.carryover, pauses=pauses)
    stock = stock_on(est, next_delivery, carryover=family.carryover, pauses=pauses)
    horizon_days = settings.cadence_days + settings.margin_days
    horizon = next_delivery + timedelta(days=horizon_days)
    if due is None or due >= horizon:
        return no(Reason.NOT_DUE, due, stock)

    need = est.rate_per_day * horizon_days - stock
    if need <= 0 or family.pack_size <= 0:
        return no(Reason.ENOUGH_STOCK, due, stock)
    packs = min(math.ceil(need / family.pack_size - 1e-9), settings.max_packs_per_line)
    pinned_only = family.pinned and recent < settings.noise_min_purchases
    reason = Reason.PINNED_DUE if pinned_only else Reason.DUE
    return PlanDecision(True, max(packs, 1), reason, est, due, stock)
