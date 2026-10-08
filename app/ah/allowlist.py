"""Explicit allowlist of AH endpoints (CLAUDE.md rule 1, SPEC §4).

Every request the adapter sends passes through `check_rest` or `graphql_document`.
Anything not listed here cannot be called. Checkout, payment and order submission
are deliberately absent, and a deny-list of path fragments is checked on top as a
second line of defence.

DO NOT add endpoints here without the human explicitly asking for it.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.ah.errors import ForbiddenEndpointError

API_HOST: Final = "api.ah.nl"


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"


@dataclass(frozen=True)
class RestEndpoint:
    name: str
    method: Method
    path_pattern: str
    writes: bool = False

    def matches(self, method: str, path: str) -> bool:
        return method == self.method and re.fullmatch(self.path_pattern, path) is not None


_ID = r"[0-9]+"

REST_ENDPOINTS: Final[tuple[RestEndpoint, ...]] = (
    # auth
    RestEndpoint("auth.anonymous", Method.POST, r"/mobile-auth/v1/auth/token/anonymous"),
    RestEndpoint("auth.exchange_code", Method.POST, r"/mobile-auth/v1/auth/token"),
    RestEndpoint("auth.refresh", Method.POST, r"/mobile-auth/v1/auth/token/refresh"),
    # products
    RestEndpoint("product.search", Method.GET, r"/mobile-services/product/search/v2"),
    RestEndpoint("product.by_ids", Method.GET, r"/mobile-services/product/search/v2/products"),
    RestEndpoint("product.detail", Method.GET, rf"/mobile-services/product/detail/v4/fir/{_ID}"),
    # bonus
    RestEndpoint("bonus.metadata", Method.GET, r"/mobile-services/bonuspage/v3/metadata"),
    RestEndpoint("bonus.section", Method.GET, r"/mobile-services/bonuspage/v2/section"),
    RestEndpoint("bonus.spotlight", Method.GET, r"/mobile-services/bonuspage/v2/section/spotlight"),
    # orders (read)
    RestEndpoint("order.active_summary", Method.GET, r"/mobile-services/order/v1/summaries/active"),
    RestEndpoint(
        "order.details",
        Method.GET,
        rf"/mobile-services/order/v1/{_ID}/details-grouped-by-taxonomy",
    ),
    # orders (write): sets an absolute quantity per product on the active order
    RestEndpoint("order.set_items", Method.PUT, r"/mobile-services/order/v1/items", writes=True),
    # graphql: the body is restricted to the registered documents below
    RestEndpoint("graphql", Method.POST, r"/graphql"),
)

# Defence in depth: even a future mistake in the list above cannot reach these.
FORBIDDEN_PATH_FRAGMENTS: Final = (
    "checkout",
    "payment",
    "pay/",
    "submit",
    "confirm",
    "place",
)


@dataclass(frozen=True)
class GraphqlDocument:
    operation: str
    query: str
    writes: bool = False


def _doc(operation: str, query: str, *, writes: bool = False) -> GraphqlDocument:
    return GraphqlDocument(operation, " ".join(query.split()), writes)


_FULFILLMENT_FIELDS = """
  result {
    orderId statusCode statusDescription shoppingType transactionCompleted modifiable
    totalPrice { totalPrice { amount } }
    delivery { status method slot { date dateDisplay timeDisplay startTime endTime } }
  }
"""

GRAPHQL_DOCUMENTS: Final[dict[str, GraphqlDocument]] = {
    d.operation: d
    for d in (
        _doc(
            "OrderFulfillments",
            "query OrderFulfillments { orderFulfillments(status: OPEN) {"
            + _FULFILLMENT_FIELDS
            + "} }",
        ),
        # Spike candidates for order history; the enum value is unverified (docs/ah-api.md).
        _doc(
            "OrderFulfillmentsClosed",
            "query OrderFulfillmentsClosed { orderFulfillments(status: CLOSED) {"
            + _FULFILLMENT_FIELDS
            + "} }",
        ),
        _doc(
            "OrderFulfillmentsAll",
            "query OrderFulfillmentsAll { orderFulfillments {" + _FULFILLMENT_FIELDS + "} }",
        ),
        # In-store receipts (read-only; approved by the owner 2026-10-08). Shapes come from
        # gwillem/appie-go and are unverified until a probe run (docs/ah-api.md). We ask for
        # the minimum: no memberId, no payments.
        _doc(
            "PosReceipts",
            "query PosReceipts($offset: Int!, $limit: Int!) {"
            " posReceiptsPage(pagination: {offset: $offset, limit: $limit}) {"
            " posReceipts { id dateTime totalAmount { amount } } } }",
        ),
        _doc(
            "PosReceipt",
            "query PosReceipt($id: String!) { posReceiptDetails(id: $id) {"
            " id products { id quantity name price { amount } amount { amount } } } }",
        ),
        _doc(
            "ProductConvertId",
            "query ProductConvertId($id: Int!) { productConvertId(sourceId: $id) }",
        ),
        _doc(
            "OrderReopen",
            "mutation OrderReopen($id: Int!) { orderReopen(id: $id) { status errorMessage } }",
            writes=True,
        ),
        _doc(
            "OrderRevert",
            "mutation OrderRevert($id: Int!) { orderRevert(id: $id) { status errorMessage } }",
            writes=True,
        ),
    )
}


def check_rest(method: str, host: str, path: str) -> RestEndpoint:
    """Return the allowlisted endpoint for this call, or raise ForbiddenEndpointError."""
    if host != API_HOST:
        raise ForbiddenEndpointError(f"host not allowed: {host}")
    lowered = path.lower()
    for fragment in FORBIDDEN_PATH_FRAGMENTS:
        if fragment in lowered:
            raise ForbiddenEndpointError(f"forbidden path fragment {fragment!r}: {path}")
    for endpoint in REST_ENDPOINTS:
        if endpoint.matches(method.upper(), path):
            return endpoint
    raise ForbiddenEndpointError(f"endpoint not on allowlist: {method} {path}")


def graphql_document(operation: str) -> GraphqlDocument:
    """Only registered documents can be sent; free-form GraphQL is impossible."""
    try:
        return GRAPHQL_DOCUMENTS[operation]
    except KeyError:
        raise ForbiddenEndpointError(f"graphql operation not on allowlist: {operation}") from None
