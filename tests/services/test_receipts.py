from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ah.models import PosReceiptDetails, PosReceiptSummary
from app.ah.models import Product as AhProduct
from app.db.models import Household, PosProductMap, Product, Purchase
from app.services.families import assign_families
from app.services.planner import load_family
from app.services.receipts import import_receipts

MILK, PEANUT = 1525, 2000
POS_MILK, POS_PEANUT, POS_UNKNOWN = 12345, 67890, 55555
TODAY = date(2026, 10, 8)


class FakeReceipts:
    def __init__(self) -> None:
        self.receipts = [
            PosReceiptSummary(id="r1", date_time=datetime(2026, 10, 3, 14, 30)),
            PosReceiptSummary(id="r2", date_time=datetime(2026, 9, 26, 9, 15)),
            PosReceiptSummary(id="old", date_time=datetime(2025, 1, 1, 9, 0)),
        ]
        self.details = {
            "r1": [
                (POS_MILK, 2, "AH Halfvolle melk"),
                (POS_PEANUT, 1, "AH Pindakaas"),
                (None, 1, "STATIEGELD"),
            ],
            "r2": [(POS_MILK, 1, "AH Halfvolle melk"), (POS_UNKNOWN, 1, "Bloemen")],
        }
        self.converted: list[int] = []
        self.fetched: list[str] = []

    async def list_receipts(self, *, offset: int = 0, limit: int = 20) -> list[PosReceiptSummary]:
        return self.receipts[offset : offset + limit]

    async def get_receipt(self, receipt_id: str) -> PosReceiptDetails:
        self.fetched.append(receipt_id)
        return PosReceiptDetails.model_validate(
            {
                "id": receipt_id,
                "products": [
                    {"id": i, "quantity": q, "name": n} for i, q, n in self.details[receipt_id]
                ],
            }
        )

    async def convert_pos_id(self, pos_id: int) -> int | None:
        self.converted.append(pos_id)
        return {POS_PEANUT: PEANUT}.get(pos_id)

    async def get_products(self, product_ids: list[int]) -> list[AhProduct]:
        return [
            AhProduct(webshop_id=pid, title="AH Pindakaas", brand="AH", sales_unit_size="350 g")
            for pid in product_ids
            if pid == PEANUT
        ]


def seed_catalog(s: Session) -> None:
    # milk is known from online orders, including its internal (hq) id
    s.add(
        Product(
            ah_id=MILK,
            hq_id=POS_MILK,
            title="AH Halfvolle melk",
            brand="AH",
            unit_size_text="1 l",
            unit_amount=1000,
            unit="ml",
        )
    )
    s.flush()


async def test_import_maps_via_hq_id_then_convert_and_caches(
    sessions: sessionmaker[Session], household: Household
) -> None:
    fake = FakeReceipts()
    with sessions.begin() as s:
        seed_catalog(s)
        result = await import_receipts(fake, s, household_id=household.id, since=date(2026, 1, 1))
    assert result.receipts == 2 and result.lines == 3
    assert fake.converted == [POS_PEANUT, POS_UNKNOWN]  # milk via hq_id: no request
    assert result.unmapped == ["Bloemen"]
    assert "old" not in fake.fetched
    with sessions() as s:
        rows = s.scalars(select(Purchase).order_by(Purchase.delivered_at)).all()
        assert {(r.receipt_id, r.ah_product_id, r.qty, r.source) for r in rows} == {
            ("r2", MILK, 1, "store"),
            ("r1", MILK, 2, "store"),
            ("r1", PEANUT, 1, "store"),
        }
        assert all(r.ah_order_id is None for r in rows)
        assert s.get(PosProductMap, POS_UNKNOWN).ah_product_id is None  # type: ignore[union-attr]
        assert s.get(Product, PEANUT) is not None  # fetched into the catalogue


async def test_import_is_idempotent_and_uses_cache(
    sessions: sessionmaker[Session], household: Household
) -> None:
    fake = FakeReceipts()
    with sessions.begin() as s:
        seed_catalog(s)
        await import_receipts(fake, s, household_id=household.id, since=date(2026, 1, 1))
    fake.converted.clear()
    fake.fetched.clear()
    with sessions.begin() as s:
        again = await import_receipts(fake, s, household_id=household.id, since=date(2026, 1, 1))
    assert again.lines == 0 and fake.converted == []
    assert fake.fetched == []  # both receipts already have purchases


async def test_store_purchases_count_as_consumption(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        seed_catalog(s)
        for week in range(1, 4):  # online every week
            s.add(
                Purchase(
                    household_id=household.id,
                    ah_order_id=week,
                    ah_product_id=MILK,
                    qty=1,
                    delivered_at=TODAY - timedelta(weeks=week),
                )
            )
        s.add(
            Purchase(
                household_id=household.id,
                receipt_id="r9",
                ah_product_id=MILK,
                qty=1,
                delivered_at=TODAY - timedelta(days=10),
                source="store",
            )
        )
        s.add(
            Purchase(
                household_id=household.id,
                ah_order_id=99,
                ah_product_id=MILK,
                qty=5,
                delivered_at=TODAY - timedelta(days=9),
                source="meal",
            )
        )
        s.flush()
        [family] = assign_families(s, household.id)
        amounts = [p.amount for p in load_family(s, family).purchases]
    assert len(amounts) == 4  # 3 online + 1 store; the meal purchase does not count
