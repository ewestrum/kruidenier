"""Import delivered AH orders as purchases (SPEC §13 fase 1: import van de historie)."""

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ah.models import Fulfillment, OrderDetails
from app.db.models import Purchase
from app.services.catalog import upsert_product

log = logging.getLogger(__name__)


class HistorySource(Protocol):
    async def list_orders(self, since: date) -> list[Fulfillment]: ...
    async def get_order(self, order_id: int) -> OrderDetails: ...


@dataclass
class ImportResult:
    orders_seen: int = 0
    orders_imported: list[int] = field(default_factory=list)
    purchases_added: int = 0


async def import_history(
    client: HistorySource, session: Session, *, household_id: int, since: date
) -> ImportResult:
    """Idempotent: orders that already have purchases are skipped, so a re-run adds nothing."""
    result = ImportResult()
    orders = await client.list_orders(since)
    result.orders_seen = len(orders)
    known = set(
        session.scalars(
            select(Purchase.ah_order_id).where(Purchase.household_id == household_id).distinct()
        )
    )
    for summary in orders:
        if summary.order_id in known or summary.delivery_date is None:
            continue
        details = await client.get_order(summary.order_id)
        if details.order_state != "DELIVERED":
            log.info("skip order %s in state %s", details.order_id, details.order_state)
            continue
        delivered = details.delivery_date or summary.delivery_date
        qty_by_product: dict[int, int] = {}
        for line in details.lines():
            product = upsert_product(
                session, line.product, min_best_before_days=line.product.min_best_before_days
            )
            qty = line.delivered_quantity
            if qty > 0:
                qty_by_product[product.ah_id] = qty_by_product.get(product.ah_id, 0) + qty
        session.flush()
        for product_id, qty in qty_by_product.items():
            session.add(
                Purchase(
                    household_id=household_id,
                    ah_order_id=details.order_id,
                    ah_product_id=product_id,
                    qty=qty,
                    delivered_at=delivered,
                    source="staple",
                )
            )
            result.purchases_added += 1
        session.flush()
        result.orders_imported.append(details.order_id)
    return result
