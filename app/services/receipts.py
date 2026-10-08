"""Import in-store AH receipts as purchases, so consumption includes shopping in the store.

Off by default per household (Instellingen → "Winkelaankopen meetellen") until a probe has
confirmed the receipt endpoints (docs/ah-api.md). Receipts list POS product ids; we map
them to webshop ids, first via the catalogue's `hq_id`, otherwise via AH's
`productConvertId`, and cache the result so each id is asked at most once.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ah.models import PosReceiptDetails, PosReceiptSummary
from app.ah.models import Product as AhProduct
from app.db.models import Household, PosProductMap, Product, Purchase
from app.services.catalog import upsert_product

log = logging.getLogger(__name__)

PAGE_SIZE = 20
MAX_PAGES = 5  # at most 100 receipts per run
SETTING = "receipts_enabled"


class ReceiptSource(Protocol):
    async def list_receipts(
        self, *, offset: int = 0, limit: int = 20
    ) -> list[PosReceiptSummary]: ...
    async def get_receipt(self, receipt_id: str) -> PosReceiptDetails: ...
    async def convert_pos_id(self, pos_id: int) -> int | None: ...
    async def get_products(self, product_ids: list[int]) -> list[AhProduct]: ...


@dataclass
class ReceiptImport:
    receipts: int = 0
    lines: int = 0
    unmapped: list[str] = field(default_factory=list)
    conversions: int = 0  # ids asked at AH (cache misses)


def receipts_enabled(household: Household) -> bool:
    s: dict[str, Any] = household.settings_json or {}
    return bool(s.get(SETTING, False))


async def _webshop_id(
    client: ReceiptSource, session: Session, pos_id: int, result: ReceiptImport
) -> int | None:
    cached = session.get(PosProductMap, pos_id)
    if cached is not None:
        return cached.ah_product_id
    by_hq = session.scalar(select(Product.ah_id).where(Product.hq_id == pos_id).limit(1))
    webshop = by_hq if by_hq is not None else await client.convert_pos_id(pos_id)
    if by_hq is None:
        result.conversions += 1
    session.add(PosProductMap(pos_id=pos_id, ah_product_id=webshop, checked_at=datetime.now(UTC)))
    session.flush()
    return webshop


async def import_receipts(
    client: ReceiptSource, session: Session, *, household_id: int, since: date
) -> ReceiptImport:
    """Idempotent: receipts that already have purchases are skipped."""
    result = ReceiptImport()
    known = set(
        session.scalars(
            select(Purchase.receipt_id)
            .where(Purchase.household_id == household_id, Purchase.receipt_id.is_not(None))
            .distinct()
        )
    )
    todo: list[PosReceiptSummary] = []
    for page in range(MAX_PAGES):
        batch = await client.list_receipts(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
        todo += [r for r in batch if r.date_time.date() >= since and r.id not in known]
        if len(batch) < PAGE_SIZE or any(r.date_time.date() < since for r in batch):
            break

    for summary in todo:
        details = await client.get_receipt(summary.id)
        lines: dict[int, int] = {}
        names: dict[int, str] = {}
        for item in details.products:
            if item.id is None or item.quantity <= 0:
                continue
            webshop = await _webshop_id(client, session, item.id, result)
            if webshop is None:
                result.unmapped.append(item.name)
                continue
            lines[webshop] = lines.get(webshop, 0) + max(1, round(item.quantity))
            names[webshop] = item.name
        missing = [pid for pid in lines if session.get(Product, pid) is None]
        if missing:
            for p in await client.get_products(missing):
                upsert_product(session, p)
            session.flush()
        for pid, qty in lines.items():
            if session.get(Product, pid) is None:
                result.unmapped.append(names[pid])
                continue
            session.add(
                Purchase(
                    household_id=household_id,
                    receipt_id=summary.id,
                    ah_order_id=None,
                    ah_product_id=pid,
                    qty=qty,
                    delivered_at=summary.date_time.date(),
                    source="store",
                )
            )
            result.lines += 1
        session.flush()
        result.receipts += 1
    if result.unmapped:
        log.info("receipt lines without webshop product: %s", result.unmapped[:10])
    return result
