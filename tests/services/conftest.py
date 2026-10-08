import json
from datetime import date
from typing import Any

import pytest
from pydantic import TypeAdapter
from sqlalchemy.orm import Session, sessionmaker

from app.ah.models import Fulfillment, FulfillmentsData, OrderDetails, Product
from app.db.models import Household
from app.services.notify import Message
from tests.conftest import FIXTURES


@pytest.fixture
def household(sessions: sessionmaker[Session]) -> Household:
    with sessions.begin() as s:
        h = Household(name="Thuis", settings_json={})
        s.add(h)
    return h


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def recorded_order_details() -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for p in sorted(FIXTURES.glob("order.details*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if d["orderState"] == "DELIVERED":
            out[d["orderId"]] = d
    return out


class FakeAh:
    """AhClient stand-in built from the fase-0 recordings."""

    def __init__(self) -> None:
        self.details = recorded_order_details()
        self.calls: list[str] = []

    async def list_orders(self, since: date) -> list[Fulfillment]:
        self.calls.append("list_orders")
        data = FulfillmentsData.model_validate(recorded("graphql.OrderFulfillmentsClosed")["data"])
        return [
            f
            for f in data.order_fulfillments.result
            if f.order_id in self.details and f.delivery_date and f.delivery_date >= since
        ]

    async def get_order(self, order_id: int) -> OrderDetails:
        self.calls.append(f"get_order:{order_id}")
        return OrderDetails.model_validate(self.details[order_id])

    async def get_products(self, product_ids: list[int]) -> list[Product]:
        self.calls.append(f"get_products:{len(product_ids)}")
        known = TypeAdapter(list[Product]).validate_python(recorded("product.by_ids"))
        by_id = {p.webshop_id: p for p in known}
        return [by_id[i] for i in product_ids if i in by_id]


class CollectingNotifier:
    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> bool:
        self.sent.append(message)
        return True
