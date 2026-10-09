"""Price history of a product from the daily price log, and the geometry of its chart."""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PriceObservation


@dataclass(frozen=True)
class PricePoint:
    day: date
    price: float
    regular: float | None
    bonus: bool
    mechanism: str | None


def price_series(
    session: Session, product_id: int, *, today: date, days: int = 120
) -> list[PricePoint]:
    rows = session.scalars(
        select(PriceObservation)
        .where(
            PriceObservation.ah_product_id == product_id,
            PriceObservation.observed_on >= today - timedelta(days=days),
            PriceObservation.price.is_not(None),
        )
        .order_by(PriceObservation.observed_on)
    )
    return [
        PricePoint(r.observed_on, float(r.price), r.regular_price, r.is_bonus, r.bonus_mechanism)
        for r in rows
        if r.price is not None
    ]


@dataclass(frozen=True)
class ChartPoint:
    x: float
    y: float
    point: PricePoint


@dataclass(frozen=True)
class PriceChart:
    width: int
    height: int
    left: int
    right: int
    top: int
    bottom: int
    price_path: str
    regular_path: str
    points: list[ChartPoint]
    y_ticks: list[tuple[float, float]]  # (y, value)
    x_labels: list[tuple[float, date]]


def build_chart(
    points: list[PricePoint], *, width: int = 360, height: int = 170
) -> PriceChart | None:
    """Pixel geometry for a line chart; None with fewer than two observations."""
    if len(points) < 2:
        return None
    # Drawn at phone size so 12px text stays 12px; wide screens cap the width in CSS.
    left, right, top, bottom = 46, width - 50, 10, height - 24
    values = [p.price for p in points] + [p.regular for p in points if p.regular is not None]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.15 or max(hi * 0.1, 0.1)
    lo, hi = max(lo - pad, 0.0), hi + pad
    first, last = points[0].day, points[-1].day
    span = max((last - first).days, 1)

    def x(d: date) -> float:
        return round(left + (d - first).days / span * (right - left), 1)

    def y(v: float) -> float:
        return round(bottom - (v - lo) / (hi - lo) * (bottom - top), 1)

    def path(pairs: list[tuple[date, float]]) -> str:
        return " ".join(f"{'M' if i == 0 else 'L'}{x(d)},{y(v)}" for i, (d, v) in enumerate(pairs))

    regular = [(p.day, p.regular) for p in points if p.regular is not None]
    ticks = [lo + (hi - lo) * i / 3 for i in range(4)]
    middle = first + timedelta(days=span // 2)
    return PriceChart(
        width=width,
        height=height,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        price_path=path([(p.day, p.price) for p in points]),
        regular_path=path(regular) if len(regular) >= 2 else "",
        points=[ChartPoint(x(p.day), y(p.price), p) for p in points],
        y_ticks=[(y(t), round(t, 2)) for t in ticks],
        x_labels=[(x(first), first), (x(middle), middle), (x(last), last)],
    )
