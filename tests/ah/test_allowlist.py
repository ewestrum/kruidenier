import pytest

from app.ah import allowlist
from app.ah.errors import ForbiddenEndpointError

HOST = allowlist.API_HOST


@pytest.mark.parametrize(
    ("method", "path", "name"),
    [
        ("GET", "/mobile-services/product/search/v2", "product.search"),
        ("GET", "/mobile-services/product/detail/v4/fir/1525", "product.detail"),
        ("GET", "/mobile-services/order/v1/summaries/active", "order.active_summary"),
        ("GET", "/mobile-services/order/v1/42/details-grouped-by-taxonomy", "order.details"),
        ("PUT", "/mobile-services/order/v1/items", "order.set_items"),
        ("POST", "/graphql", "graphql"),
        ("post", "/mobile-auth/v1/auth/token/refresh", "auth.refresh"),
    ],
)
def test_allowed(method: str, path: str, name: str) -> None:
    assert allowlist.check_rest(method, HOST, path).name == name


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/mobile-services/order/v1/checkout"),
        ("POST", "/mobile-services/payment/v1/start"),
        ("POST", "/mobile-services/order/v1/42/submit"),
        ("POST", "/mobile-services/order/v1/42/confirm"),
        ("POST", "/mobile-services/order/v1/place"),
        ("DELETE", "/mobile-services/order/v1/items"),
        ("GET", "/mobile-services/order/v1/items"),  # wrong method for a listed path
        ("GET", "/mobile-services/product/detail/v4/fir/1525/../../checkout"),
        ("GET", "/mobile-services/product/detail/v4/fir/abc"),
        ("POST", "/mobile-services/order/v1/items"),
        ("GET", "/mobile-services/something/new"),
    ],
)
def test_forbidden(method: str, path: str) -> None:
    with pytest.raises(ForbiddenEndpointError):
        allowlist.check_rest(method, HOST, path)


def test_other_host_forbidden() -> None:
    with pytest.raises(ForbiddenEndpointError):
        allowlist.check_rest("GET", "evil.example", "/mobile-services/product/search/v2")


def test_only_registered_graphql_documents() -> None:
    assert allowlist.graphql_document("OrderReopen").writes
    with pytest.raises(ForbiddenEndpointError):
        allowlist.graphql_document("OrderCheckout")


def test_no_listed_endpoint_mentions_checkout_or_payment() -> None:
    texts = [e.path_pattern for e in allowlist.REST_ENDPOINTS] + [
        d.query for d in allowlist.GRAPHQL_DOCUMENTS.values()
    ]
    for text in texts:
        lowered = text.lower()
        for word in ("checkout", "payment", "submit", "orderconfirm", "placeorder"):
            assert word not in lowered, text


def test_only_expected_write_endpoints() -> None:
    writes = {e.name for e in allowlist.REST_ENDPOINTS if e.writes} | {
        d.operation for d in allowlist.GRAPHQL_DOCUMENTS.values() if d.writes
    }
    assert writes == {"order.set_items", "OrderReopen", "OrderRevert"}
