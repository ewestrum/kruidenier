"""Consumption model per product family (SPEC §6).

We model consumption (base units per day), not purchase frequency, so a bulk purchase
simply means a longer interval until the next one.
"""

import itertools
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

MAX_PROJECTION_DAYS = 3650


@dataclass(frozen=True)
class Purchase:
    day: date
    amount: float  # in the family's base unit


@dataclass(frozen=True)
class Pause:
    """A holiday: consumption stops on these days (inclusive)."""

    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class ModelParams:
    half_life_days: float = 60.0
    iqr_factor: float = 1.5
    high_min_purchases: int = 4
    high_max_cv: float = 0.35
    recency_factor: float = 3.0


@dataclass(frozen=True)
class ConsumptionEstimate:
    rate_per_day: float | None
    n_purchases: int
    mean_interval_days: float | None
    cv_interval: float | None
    last_purchase: date | None
    last_amount: float
    confidence: Confidence


def _paused(day: date, pauses: Iterable[Pause]) -> bool:
    return any(p.contains(day) for p in pauses)


def active_days(start: date, end: date, pauses: Sequence[Pause] = ()) -> int:
    """Days in [start, end) that are not in a pause."""
    if end <= start:
        return 0
    total = (end - start).days
    if not pauses:
        return total
    return sum(1 for i in range(total) if not _paused(start + timedelta(days=i), pauses))


def add_active_days(start: date, days: float, pauses: Sequence[Pause] = ()) -> date:
    """The date on which `days` active (non-paused) days after `start` have passed."""
    # The epsilon absorbs float noise such as 2000 / (2000 / 7) == 6.999...
    whole = min(math.floor(days + 1e-9), MAX_PROJECTION_DAYS)
    current = start
    remaining = whole
    guard = 0
    while remaining > 0 and guard < MAX_PROJECTION_DAYS * 2:
        current += timedelta(days=1)
        guard += 1
        if not _paused(current, pauses):
            remaining -= 1
    return current


def merge_same_day(purchases: Iterable[Purchase]) -> list[Purchase]:
    """Purchases on the same day (e.g. two SKUs of one family) count as one."""
    by_day: dict[date, float] = {}
    for p in purchases:
        if p.amount > 0:
            by_day[p.day] = by_day.get(p.day, 0.0) + p.amount
    return [Purchase(d, a) for d, a in sorted(by_day.items())]


def _drop_outliers(values: list[float], factor: float) -> list[bool]:
    """Keep-mask: False for values outside [Q1 - f*IQR, Q3 + f*IQR] (needs >= 4 values)."""
    if len(values) < 4:
        return [True] * len(values)
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    iqr = q3 - q1
    lo, hi = q1 - factor * iqr, q3 + factor * iqr
    return [lo <= v <= hi for v in values]


def estimate_consumption(
    purchases: Iterable[Purchase],
    *,
    today: date,
    pauses: Sequence[Pause] = (),
    params: ModelParams | None = None,
    correction: float = 1.0,
) -> ConsumptionEstimate:
    """SPEC §6.1, §6.2, §6.6 and §6.9.

    `correction` is the multiplicative feedback factor ("more"/"less", §6.8).
    """
    params = params or ModelParams()
    merged = merge_same_day(purchases)
    n = len(merged)
    last = merged[-1] if merged else None
    if n < 2:
        return ConsumptionEstimate(
            rate_per_day=None,
            n_purchases=n,
            mean_interval_days=None,
            cv_interval=None,
            last_purchase=last.day if last else None,
            last_amount=last.amount if last else 0.0,
            confidence=Confidence.LOW,
        )
    assert last is not None

    rates: list[float] = []
    intervals: list[int] = []
    ends: list[date] = []
    for cur, nxt in itertools.pairwise(merged):
        days = active_days(cur.day, nxt.day, pauses)
        if days <= 0:
            continue
        rates.append(cur.amount / days)
        intervals.append(days)
        ends.append(nxt.day)

    if not rates:
        return ConsumptionEstimate(None, n, None, None, last.day, last.amount, Confidence.LOW)

    keep = _drop_outliers(rates, params.iqr_factor)
    weighted = 0.0
    weight_sum = 0.0
    for r, end, k in zip(rates, ends, keep, strict=True):
        if not k:
            continue
        age = max((today - end).days, 0)
        w = 0.5 ** (age / params.half_life_days)
        weighted += w * r
        weight_sum += w
    rate = (weighted / weight_sum) * correction if weight_sum > 0 else None

    mean_interval = statistics.fmean(intervals)
    cv = statistics.pstdev(intervals) / mean_interval if len(intervals) >= 2 else None

    since_last = active_days(last.day, today, pauses)
    recent = since_last <= params.recency_factor * mean_interval
    if n >= params.high_min_purchases and cv is not None and cv < params.high_max_cv and recent:
        confidence = Confidence.HIGH
    elif n >= 3 and recent:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW

    return ConsumptionEstimate(
        rate_per_day=rate,
        n_purchases=n,
        mean_interval_days=mean_interval,
        cv_interval=cv,
        last_purchase=last.day,
        last_amount=last.amount,
        confidence=confidence,
    )


def due_date(
    est: ConsumptionEstimate, *, carryover: float = 0.0, pauses: Sequence[Pause] = ()
) -> date | None:
    """SPEC §6.3: d_last + (q_last + carryover) / r, skipping paused days."""
    if est.rate_per_day is None or est.rate_per_day <= 0 or est.last_purchase is None:
        return None
    cover_days = (est.last_amount + carryover) / est.rate_per_day
    return add_active_days(est.last_purchase, cover_days, pauses)


def stock_on(
    est: ConsumptionEstimate,
    on: date,
    *,
    carryover: float = 0.0,
    pauses: Sequence[Pause] = (),
) -> float:
    """Expected stock left on `on`, assuming the last purchase started from empty."""
    if est.last_purchase is None:
        return 0.0
    start = est.last_amount + carryover
    if est.rate_per_day is None:
        return start
    used = est.rate_per_day * active_days(est.last_purchase, on, pauses)
    return max(0.0, start - used)
