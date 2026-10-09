from datetime import date, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    FamilyMember,
    Household,
    PriceObservation,
    Product,
    ProductFamily,
    Purchase,
)
from app.services.planner import build_draft_plan, load_family, recompute_stats
from app.services.price_history import PricePoint, build_chart
from app.services.throttle import MAX_PER_ACCOUNT, WINDOW_SECONDS, LoginThrottle

# --- login throttle -------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_throttle_blocks_after_limit_and_expires() -> None:
    clock = Clock()
    t = LoginThrottle(clock=clock)
    for _ in range(MAX_PER_ACCOUNT):
        assert t.wait_seconds("a@b.nl", "1.2.3.4") == 0
        t.failed("A@b.nl ", "1.2.3.4")  # same account, case and spaces ignored
    assert t.wait_seconds("a@b.nl", "1.2.3.4") > 0
    assert t.wait_seconds("ander@b.nl", "1.2.3.4") == 0  # other account still allowed
    clock.now += WINDOW_SECONDS + 1
    assert t.wait_seconds("a@b.nl", "1.2.3.4") == 0


def test_success_resets_account_counter() -> None:
    t = LoginThrottle(clock=Clock())
    for _ in range(MAX_PER_ACCOUNT - 1):
        t.failed("a@b.nl", "x")
    t.succeeded("a@b.nl", "x")
    t.failed("a@b.nl", "x")
    assert t.wait_seconds("a@b.nl", "x") == 0


def test_one_address_cannot_try_many_accounts() -> None:
    t = LoginThrottle(clock=Clock())
    for i in range(20):
        t.failed(f"user{i}@b.nl", "9.9.9.9")
    assert t.wait_seconds("nieuw@b.nl", "9.9.9.9") > 0


# --- price chart geometry --------------------------------------------------------------

D = date(2026, 10, 1)


def pts(*prices: float, bonus_at: int | None = None) -> list[PricePoint]:
    return [
        PricePoint(
            D + timedelta(days=i), p, 1.49, i == bonus_at, "25% korting" if i == bonus_at else None
        )
        for i, p in enumerate(prices)
    ]


def test_chart_needs_two_points() -> None:
    assert build_chart(pts(1.49)) is None
    assert build_chart([]) is None


def test_chart_geometry_within_plot_area() -> None:
    chart = build_chart(pts(1.49, 1.49, 1.12, 1.49), width=640, height=200)
    assert chart is not None
    xs = [c.x for c in chart.points]
    ys = [c.y for c in chart.points]
    assert xs[0] == chart.left and xs[-1] == chart.right and xs == sorted(xs)
    assert all(chart.top <= y <= chart.bottom for y in ys)
    assert ys[2] > ys[0]  # the cheaper day is lower on screen
    assert chart.price_path.startswith("M") and chart.price_path.count("L") == 3
    assert chart.regular_path  # dashed regular-price line
    assert chart.x_labels[0][1] == D and chart.x_labels[-1][1] == D + timedelta(days=3)


def test_flat_price_still_has_a_scale() -> None:
    chart = build_chart(pts(2.0, 2.0, 2.0))
    assert chart is not None and len({round(c.y) for c in chart.points}) == 1
    assert chart.y_ticks[0][1] < 2.0 < chart.y_ticks[-1][1]


# --- unavailable preferred product -> family alternative ---------------------------------

TODAY = date(2026, 10, 8)


def test_unavailable_preferred_is_replaced_by_most_bought_available(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        for pid, title in [
            (1, "AH Halfvolle melk"),
            (2, "Campina Halfvolle melk"),
            (3, "Melkunie Halfvolle melk"),
        ]:
            s.add(Product(ah_id=pid, title=title, unit_amount=1000, unit="ml"))
        fam = ProductFamily(household_id=household.id, name="Halfvolle melk", base_unit="ml")
        s.add(fam)
        s.flush()
        s.add_all(
            [
                FamilyMember(family_id=fam.id, ah_product_id=1, preferred=True),
                FamilyMember(family_id=fam.id, ah_product_id=2),
                FamilyMember(family_id=fam.id, ah_product_id=3),
            ]
        )
        for i in range(6):
            s.add(
                Purchase(
                    household_id=household.id,
                    ah_order_id=10 + i,
                    ah_product_id=1,
                    qty=2,
                    delivered_at=TODAY - timedelta(weeks=6 - i),
                )
            )
        s.add(
            Purchase(
                household_id=household.id,
                ah_order_id=30,
                ah_product_id=2,
                qty=1,
                delivered_at=TODAY - timedelta(weeks=8),
            )
        )
        s.add(
            Purchase(
                household_id=household.id,
                ah_order_id=31,
                ah_product_id=2,
                qty=1,
                delivered_at=TODAY - timedelta(weeks=9),
            )
        )
        s.add(
            Purchase(
                household_id=household.id,
                ah_order_id=32,
                ah_product_id=3,
                qty=1,
                delivered_at=TODAY - timedelta(weeks=10),
            )
        )
        s.add(PriceObservation(ah_product_id=1, observed_on=TODAY, price=1.19, available=False))
        s.add(PriceObservation(ah_product_id=3, observed_on=TODAY, price=1.29, available=True))
        s.flush()

        data = load_family(s, fam)
        assert data.replaced is not None and data.replaced.ah_id == 1
        assert data.order_product is not None and data.order_product.ah_id == 2  # bought most

        h = s.get(Household, household.id)
        assert h is not None
        recompute_stats(s, h, today=TODAY)
        plan = build_draft_plan(s, h, delivery_date=TODAY + timedelta(days=3), today=TODAY)
        [line] = plan.lines
        assert line.ah_product_id == 2
        assert "In plaats van AH Halfvolle melk (niet leverbaar)" in line.reason_text


def test_no_alternative_keeps_preferred(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        s.add(Product(ah_id=1, title="Saffraan", unit="st", unit_amount=1))
        fam = ProductFamily(household_id=household.id, name="Saffraan", base_unit="st")
        s.add(fam)
        s.flush()
        s.add(FamilyMember(family_id=fam.id, ah_product_id=1, preferred=True))
        s.add(PriceObservation(ah_product_id=1, observed_on=TODAY, price=4.0, available=False))
        s.flush()
        data = load_family(s, fam)
        assert data.order_product is not None and data.order_product.ah_id == 1
        assert data.replaced is None
