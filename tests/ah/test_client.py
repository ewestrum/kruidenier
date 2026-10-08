import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.ah.client import HttpAhClient
from app.ah.errors import (
    AhAuthError,
    AhError,
    AhGraphqlError,
    AhHttpError,
    AhSchemaError,
    ForbiddenEndpointError,
)
from app.ah.models import LineItem, Tokens
from app.ah.ratelimit import RateLimiter
from tests.conftest import FIXTURES, load_fixture


async def _no_sleep(_: float) -> None:
    return None


def fresh_tokens(**kw: object) -> Tokens:
    data: dict[str, object] = {
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    data.update(kw)
    return Tokens.model_validate(data)


@pytest.fixture
async def client() -> AsyncIterator[HttpAhClient]:
    c = HttpAhClient(
        client_id="appie-ios",
        client_version="9.28",
        tokens=fresh_tokens(),
        limiter=RateLimiter(0.0, sleep=_no_sleep),
        backoff_base=0.0,
    )
    async with c:
        yield c


async def test_search_parses_products(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    route = ah_api.get("/mobile-services/product/search/v2").respond(
        json=load_fixture("product.search")
    )
    products = await client.search_products("melk")
    assert [p.webshop_id for p in products] == [1525, 200486]
    assert products[1].bonus_mechanism == "25% korting"
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer acc"
    assert req.headers["x-client-name"] == "appie-ios"
    assert req.url.params["query"] == "melk"


async def test_product_detail(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.get("/mobile-services/product/detail/v4/fir/1525").respond(
        json=load_fixture("product.detail")
    )
    product = await client.get_product(1525)
    assert product.title == "AH Halfvolle melk"


async def test_schema_error_on_invalid_response(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.get("/mobile-services/product/search/v2").respond(json={"products": [{"title": 1}]})
    with pytest.raises(AhSchemaError) as e:
        await client.search_products("melk")
    assert e.value.endpoint == "product.search"


async def test_non_json_is_schema_error(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.get("/mobile-services/bonuspage/v3/metadata").respond(text="<html>")
    with pytest.raises(AhSchemaError):
        await client.get_bonus_metadata()


async def test_order_details_lines(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.get("/mobile-services/order/v1/228193311/details-grouped-by-taxonomy").respond(
        json=load_fixture("order.details")
    )
    order = await client.get_order(228193311)
    assert {(ln.product.webshop_id, ln.quantity) for ln in order.lines()} == {(1525, 2), (4401, 1)}


async def test_upcoming_order_is_earliest_future_undelivered(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    route = ah_api.post("/graphql").respond(json=load_fixture("graphql.OrderFulfillments"))
    ah_api.get("/mobile-services/order/v1/228193311/details-grouped-by-taxonomy").respond(
        json=load_fixture("order.details")
    )
    upcoming = await client.get_upcoming_order(today=date(2026, 10, 7))
    assert upcoming is not None
    assert upcoming.order_id == 228193311
    assert upcoming.delivery_date == date(2026, 10, 10)
    assert upcoming.delivery_status == "SUBMITTED"
    assert upcoming.cutoff is None  # synthetic details have no closingTime
    body = json.loads(route.calls.last.request.content)
    assert body["operationName"] == "OrderFulfillments"


async def test_upcoming_order_cutoff_from_recorded_details(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.post("/graphql").respond(json=load_fixture("graphql.OrderFulfillments", synthetic=False))
    # Probe runs rewrite fixtures, so take whichever recorded details exist for that order.
    details = next(d for d in _recorded("order.details*") if d["orderId"] == 150638209)
    ah_api.get("/mobile-services/order/v1/150638209/details-grouped-by-taxonomy").respond(
        json=details
    )
    upcoming = await client.get_upcoming_order(today=date(2026, 10, 7))
    assert upcoming is not None
    assert upcoming.order_id == 150638209
    assert upcoming.cutoff == datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    assert upcoming.reopenable == details["reopenable"]


async def test_no_upcoming_order_skips_details(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.post("/graphql").respond(json={"data": {"orderFulfillments": {"result": []}}})
    assert await client.get_upcoming_order(today=date(2026, 10, 7)) is None


async def test_delivered_order_lines_from_recorded_fixture(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    # Probe runs renumber order.details.N fixtures, so select by content.
    candidates = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(FIXTURES.glob("order.details*.json"))
    ]
    data = next(d for d in candidates if d["orderState"] == "DELIVERED")
    ah_api.get(f"/mobile-services/order/v1/{data['orderId']}/details-grouped-by-taxonomy").respond(
        json=data
    )
    order = await client.get_order(data["orderId"])
    assert order.order_state == "DELIVERED"
    assert order.reopenable is False
    lines = order.lines()
    assert lines and all(ln.delivered_quantity >= 1 for ln in lines)
    assert any(ln.product.min_best_before_days for ln in lines)


async def test_upcoming_orders_from_recorded_fixture(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.post("/graphql").respond(json=load_fixture("graphql.OrderFulfillments", synthetic=False))
    orders = await client.list_upcoming_orders(today=date(2026, 10, 7))
    assert [o.delivery_date for o in orders] == [
        date(2026, 10, 11),
        date(2026, 10, 18),
        date(2026, 10, 25),
        date(2026, 11, 1),
    ]
    assert orders[0].order_id == 150638209


async def test_list_orders_returns_delivered_only_newest_first(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    route = ah_api.post("/graphql").respond(
        json=load_fixture("graphql.OrderFulfillmentsClosed", synthetic=False)
    )
    orders = await client.list_orders(since=date(2026, 1, 1))
    assert orders, "recorded history should contain delivered orders"
    assert all(o.delivery and o.delivery.status == "DELIVERED" for o in orders)
    dates = [o.delivery_date for o in orders]
    assert dates == sorted(dates, reverse=True)
    assert json.loads(route.calls.last.request.content)["operationName"] == (
        "OrderFulfillmentsClosed"
    )


async def test_list_orders_respects_since(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.post("/graphql").respond(
        json=load_fixture("graphql.OrderFulfillmentsClosed", synthetic=False)
    )
    orders = await client.list_orders(since=date(2026, 9, 25))
    assert all(o.delivery_date and o.delivery_date >= date(2026, 9, 25) for o in orders)


async def test_products_by_ids_is_a_bare_list(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.get("/mobile-services/product/search/v2/products").respond(
        json=load_fixture("product.by_ids", synthetic=False)
    )
    products = await client.get_products([609672])
    assert products[0].webshop_id == 609672


async def test_no_active_order_is_none(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(
        status_code=404, json={"status": 404, "message": "Order does not exist"}
    )
    assert await client.get_active_order() is None


async def test_add_to_order_refuses_without_active_order(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(status_code=404)
    put = ah_api.put("/mobile-services/order/v1/items")
    with pytest.raises(AhError, match="not the active order"):
        await client.add_to_order(1, [LineItem(product_id=1525, quantity=1)])
    assert not put.called


async def test_graphql_errors_raise(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.post("/graphql").respond(json={"errors": [{"message": "nope"}], "data": None})
    with pytest.raises(AhGraphqlError):
        await client.get_upcoming_order()


async def test_unregistered_graphql_is_forbidden(client: HttpAhClient) -> None:
    with pytest.raises(ForbiddenEndpointError):
        await client._graphql("CheckoutOrder")


async def test_add_to_order_sets_absolute_quantities(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(
        json=load_fixture("order.active_summary")
    )
    put = ah_api.put("/mobile-services/order/v1/items").respond(
        json=load_fixture("order.active_summary")
    )
    await client.add_to_order(228193311, [LineItem(product_id=1525, quantity=3)])
    assert put.calls.last.request.headers["Appie-Current-Order-Id"] == "228193311"
    body = json.loads(put.calls.last.request.content)
    assert body == {
        "items": [
            {
                "productId": 1525,
                "quantity": 3,
                "originCode": "PRD",
                "description": "",
                "strikethrough": False,
            }
        ]
    }


def _recorded(pattern: str) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob(f"{pattern}.json"))
    ]


def _recorded_put_with_lines() -> dict[str, Any]:
    return next(d for d in _recorded("order.set_items*") if d["orderedProducts"])


async def test_add_to_order_returns_updated_order(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    after_put = _recorded_put_with_lines()
    order_id = int(after_put["id"])
    first = after_put["orderedProducts"][0]
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(json=after_put)
    ah_api.put("/mobile-services/order/v1/items").respond(json=after_put)
    updated = await client.add_to_order(
        order_id,
        [LineItem(product_id=first["product"]["webshopId"], quantity=first["quantity"])],
    )
    assert updated is not None
    assert [(p.product.webshop_id, p.quantity) for p in updated.ordered_products] == [
        (p["product"]["webshopId"], p["quantity"]) for p in after_put["orderedProducts"]
    ]
    assert updated.total_price is not None
    assert updated.total_price.price_total_payable == after_put["totalPrice"]["priceTotalPayable"]


async def test_quantity_zero_removes_line(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    before = _recorded_put_with_lines()
    removed = before["orderedProducts"][0]["product"]["webshopId"]
    after = {
        **before,
        "orderedProducts": [
            p for p in before["orderedProducts"] if p["product"]["webshopId"] != removed
        ],
    }
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(json=before)
    put = ah_api.put("/mobile-services/order/v1/items").respond(json=after)
    updated = await client.add_to_order(
        int(before["id"]), [LineItem(product_id=removed, quantity=0)]
    )
    assert updated is not None
    assert removed not in {p.product.webshop_id for p in updated.ordered_products}
    assert json.loads(put.calls.last.request.content)["items"][0]["quantity"] == 0


async def test_add_to_order_invalid_response_is_schema_error(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(
        json=load_fixture("order.active_summary")
    )
    ah_api.put("/mobile-services/order/v1/items").respond(json={"unexpected": True})
    with pytest.raises(AhSchemaError):
        await client.add_to_order(228193311, [LineItem(product_id=1525, quantity=1)])


async def test_add_to_order_refuses_when_order_not_active(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(
        json=load_fixture("order.active_summary")
    )
    put = ah_api.put("/mobile-services/order/v1/items")
    with pytest.raises(AhError, match="not the active order"):
        await client.add_to_order(999, [LineItem(product_id=1525, quantity=1)])
    assert not put.called


async def test_add_to_order_quantity_cap(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    active = ah_api.get("/mobile-services/order/v1/summaries/active")
    with pytest.raises(AhError, match="exceeds cap"):
        await client.add_to_order(228193311, [LineItem(product_id=1, quantity=500)])
    assert not active.called


async def test_retries_read_on_503(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    route = ah_api.get("/mobile-services/bonuspage/v3/metadata")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=load_fixture("bonus.metadata")),
    ]
    meta = await client.get_bonus_metadata()
    assert meta.periods[0].bonus_start_date == date(2026, 10, 5)
    assert route.call_count == 2


async def test_gives_up_after_max_retries(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    route = ah_api.get("/mobile-services/bonuspage/v3/metadata").respond(status_code=503)
    with pytest.raises(AhHttpError):
        await client.get_bonus_metadata()
    assert route.call_count == 4  # 1 + max_retries


async def test_write_not_retried_on_5xx(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(
        json=load_fixture("order.active_summary")
    )
    put = ah_api.put("/mobile-services/order/v1/items").respond(status_code=503)
    with pytest.raises(AhHttpError):
        await client.add_to_order(228193311, [LineItem(product_id=1525, quantity=1)])
    assert put.call_count == 1


async def test_write_not_retried_on_transport_error(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(
        json=load_fixture("order.active_summary")
    )
    put = ah_api.put("/mobile-services/order/v1/items").mock(
        side_effect=httpx.ReadTimeout("timeout")
    )
    with pytest.raises(AhError):
        await client.add_to_order(228193311, [LineItem(product_id=1525, quantity=1)])
    assert put.call_count == 1


async def test_write_retried_on_429(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.get("/mobile-services/order/v1/summaries/active").respond(
        json=load_fixture("order.active_summary")
    )
    put = ah_api.put("/mobile-services/order/v1/items")
    put.side_effect = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200)]
    await client.add_to_order(228193311, [LineItem(product_id=1525, quantity=1)])
    assert put.call_count == 2


async def test_401_raises_auth_error(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    ah_api.get("/mobile-services/bonuspage/v3/metadata").respond(status_code=401)
    with pytest.raises(AhAuthError):
        await client.get_bonus_metadata()


async def test_refreshes_expiring_token_and_reports_it(ah_api: respx.MockRouter) -> None:
    saved: list[Tokens] = []

    async def on_refresh(t: Tokens) -> None:
        saved.append(t)

    refresh = ah_api.post("/mobile-auth/v1/auth/token/refresh").respond(
        json={"access_token": "new", "refresh_token": "ref2", "expires_in": 3600}
    )
    meta = ah_api.get("/mobile-services/bonuspage/v3/metadata").respond(
        json=load_fixture("bonus.metadata")
    )
    async with HttpAhClient(
        client_id="appie-ios",
        client_version="9.28",
        tokens=fresh_tokens(expires_at=datetime.now(UTC) + timedelta(seconds=10)),
        on_tokens_refreshed=on_refresh,
        limiter=RateLimiter(0.0, sleep=_no_sleep),
    ) as c:
        await c.get_bonus_metadata()

    assert json.loads(refresh.calls.last.request.content) == {
        "clientId": "appie-ios",
        "refreshToken": "ref",
    }
    assert meta.calls.last.request.headers["Authorization"] == "Bearer new"
    assert [t.refresh_token for t in saved] == ["ref2"]


async def test_refresh_keeps_old_refresh_token_if_not_rotated(ah_api: respx.MockRouter) -> None:
    ah_api.post("/mobile-auth/v1/auth/token/refresh").respond(
        json={"access_token": "new", "expires_in": 3600}
    )
    async with HttpAhClient(client_id="appie-ios", client_version="9.28") as c:
        tokens = await c.refresh(fresh_tokens())
    assert tokens.refresh_token == "ref"


async def test_no_tokens_is_auth_error() -> None:
    async with HttpAhClient(client_id="appie-ios", client_version="9.28") as c:
        with pytest.raises(AhAuthError):
            await c.get_bonus_metadata()


async def test_login_url() -> None:
    async with HttpAhClient(client_id="appie-ios", client_version="9.28") as c:
        url = httpx.URL(await c.login_url())
    assert url.host == "login.ah.nl"
    assert url.params["redirect_uri"] == "appie://login-exit"
    assert url.params["client_id"] == "appie-ios"


# --- in-store receipts (synthetic fixtures until the probe has run) ---------------------


async def test_list_and_get_receipt(client: HttpAhClient, ah_api: respx.MockRouter) -> None:
    def graphql(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "memberId" not in body["query"] and "payments" not in body["query"]
        name = {
            "PosReceipts": "graphql.PosReceipts",
            "PosReceipt": "graphql.PosReceipt",
            "ProductConvertId": "graphql.ProductConvertId",
        }[body["operationName"]]
        return httpx.Response(200, json=load_fixture(name))

    route = ah_api.post("/graphql").mock(side_effect=graphql)
    receipts = await client.list_receipts(limit=5)
    assert [r.id for r in receipts] == ["txn-001", "txn-002"]
    assert json.loads(route.calls.last.request.content)["variables"] == {"offset": 0, "limit": 5}
    details = await client.get_receipt("txn-001")
    assert [(p.id, p.quantity) for p in details.products] == [(12345, 2), (67890, 1), (None, 1)]
    assert await client.convert_pos_id(12345) == 1525


async def test_receipt_schema_change_is_safe_failure(
    client: HttpAhClient, ah_api: respx.MockRouter
) -> None:
    ah_api.post("/graphql").respond(json={"data": {"posReceiptsPage": {"receipts": []}}})
    with pytest.raises(AhSchemaError):
        await client.list_receipts()
