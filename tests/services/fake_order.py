"""In-memory AH order that behaves like the fase-0 findings (docs/ah-api.md)."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.ah.errors import AhError
from app.ah.models import ActiveOrderSummary, LineItem, OrderDetails, UpcomingOrder


class FakeOrder:
    def __init__(
        self,
        order_id: int = 555,
        *,
        delivery: date | None = None,
        cutoff: datetime | None = None,
        state: str = "CONFIRMED",
        items: dict[int, int] | None = None,
        refuse: set[int] | None = None,
    ) -> None:
        self.order_id = order_id
        self.delivery = delivery or date.today() + timedelta(days=3)
        self.cutoff = cutoff or datetime.now(UTC) + timedelta(days=2)
        self.state = state
        self.items: dict[int, int] = dict(items or {})
        self.refuse = refuse or set()  # products AH silently does not take
        self.calls: list[str] = []
        self.writes = 0

    async def get_upcoming_order(self, *, today: date | None = None) -> UpcomingOrder | None:
        self.calls.append("upcoming")
        return UpcomingOrder(
            order_id=self.order_id,
            delivery_date=self.delivery,
            slot_start="20:00",
            slot_end="22:00",
            delivery_status="SUBMITTED",
            cutoff=self.cutoff,
            reopenable=True,
        )

    def _lines(self) -> list[dict[str, Any]]:
        return [
            {"quantity": q, "product": {"webshopId": pid, "title": f"P{pid}"}}
            for pid, q in sorted(self.items.items())
        ]

    async def get_order(self, order_id: int) -> OrderDetails:
        self.calls.append("details")
        assert order_id == self.order_id
        return OrderDetails.model_validate(
            {
                "orderId": order_id,
                "orderState": self.state,
                "reopenable": self.state == "CONFIRMED",
                "groupedProductsInTaxonomy": [{"orderedProducts": self._lines()}],
            }
        )

    async def reopen_order(self, order_id: int) -> None:
        self.calls.append("reopen")
        if self.state != "CONFIRMED":
            raise AhError(f"cannot reopen in state {self.state}")
        self.state = "REOPENED"

    async def revert_order(self, order_id: int) -> None:
        raise AssertionError("Kruidenier must never call orderRevert (docs/ah-api.md q8)")

    async def add_to_order(self, order_id: int, items: list[LineItem]) -> ActiveOrderSummary:
        self.calls.append("put")
        if self.state != "REOPENED":
            raise AhError("Required header 'Appie-Current-Order-Id' / order not active")
        self.writes += 1
        for item in items:
            if item.product_id in self.refuse:
                continue
            if item.quantity == 0:
                self.items.pop(item.product_id, None)
            else:
                self.items[item.product_id] = item.quantity
        return ActiveOrderSummary.model_validate(
            {"id": order_id, "state": self.state, "orderedProducts": self._lines()}
        )
