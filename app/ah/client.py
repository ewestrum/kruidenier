"""HTTP implementation of AhClient against the unofficial AH mobile API.

Every request goes through `_send`, which enforces, in order:
allowlist -> rate limit (serialised, ~1 req/s) -> retry with backoff -> schema validation.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, Final, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.ah import allowlist
from app.ah.errors import (
    AhAuthError,
    AhError,
    AhGraphqlError,
    AhHttpError,
    AhSchemaError,
)
from app.ah.models import (
    ActiveOrderSummary,
    BonusMetadata,
    Fulfillment,
    FulfillmentsData,
    LineItem,
    OrderDetails,
    OrderReopenData,
    OrderRevertData,
    Product,
    ProductDetailResponse,
    SearchResponse,
    TokenResponse,
    Tokens,
    UpcomingOrder,
)
from app.ah.ratelimit import RateLimiter

log = logging.getLogger(__name__)

BASE_URL: Final = f"https://{allowlist.API_HOST}"
LOGIN_URL: Final = "https://login.ah.nl/login"
REDIRECT_URI: Final = "appie://login-exit"

RETRYABLE_STATUS: Final = frozenset({429, 502, 503, 504})
REFRESH_MARGIN_SECONDS: Final = 120
MAX_LINE_QUANTITY: Final = 24  # sanity cap per line (SPEC §9), enforced again in the autopilot

_FINISHED_DELIVERY_STATUSES: Final = frozenset({"DELIVERED", "CANCELLED"})
_PRODUCT_LIST: Final = TypeAdapter(list[Product])

M = TypeVar("M", bound=BaseModel)
TokenCallback = Callable[[Tokens], Awaitable[None]]


class HttpAhClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_version: str,
        tokens: Tokens | None = None,
        on_tokens_refreshed: TokenCallback | None = None,
        limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        on_raw_response: Callable[[str, Any], None] | None = None,
    ) -> None:
        self._client_id = client_id
        self._tokens = tokens
        self._on_tokens_refreshed = on_tokens_refreshed
        self._limiter = limiter or RateLimiter(1.0)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._on_raw_response = on_raw_response
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            transport=transport,
            timeout=httpx.Timeout(20.0),
            headers={
                "User-Agent": f"Appie/{client_version}",
                "x-client-name": client_id,
                "x-client-version": client_version,
                "x-application": "AHWEBSHOP",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "HttpAhClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def tokens(self) -> Tokens | None:
        return self._tokens

    def use_tokens(self, tokens: Tokens) -> None:
        self._tokens = tokens

    # --- transport ----------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json: Any = None,
        authenticated: bool = True,
        label: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        endpoint = allowlist.check_rest(method, allowlist.API_HOST, path)
        name = label or endpoint.name
        headers: dict[str, str] = dict(extra_headers or {})
        if authenticated:
            headers["Authorization"] = f"Bearer {await self._access_token()}"

        attempt = 0
        async with self._limiter:
            while True:
                try:
                    resp = await self._http.request(
                        method, path, params=params, json=json, headers=headers
                    )
                except httpx.TransportError as e:
                    # A write whose outcome is unknown must not be blindly repeated.
                    if endpoint.writes or attempt >= self._max_retries:
                        raise AhError(f"{name}: transport error: {e}") from e
                    attempt += 1
                    await self._limiter.pause(self._backoff_base**attempt)
                    continue

                retryable = resp.status_code in RETRYABLE_STATUS and (
                    resp.status_code == 429 or not endpoint.writes
                )
                if retryable and attempt < self._max_retries:
                    attempt += 1
                    await self._limiter.pause(self._retry_delay(resp, attempt))
                    continue
                break

        log.info("ah %s %s -> %s", method, path, resp.status_code)
        if resp.status_code in (401, 403):
            raise AhAuthError(f"{name}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise AhHttpError(name, resp.status_code, resp.text)
        if not resp.content:
            return None
        try:
            data = resp.json()
        except ValueError as e:
            raise AhSchemaError(name, "response is not JSON") from e
        if self._on_raw_response is not None:
            self._on_raw_response(name, data)
        return data

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 300.0)
        return float(self._backoff_base**attempt)

    async def _graphql(self, operation: str, variables: dict[str, Any] | None = None) -> Any:
        doc = allowlist.graphql_document(operation)
        body = {"operationName": doc.operation, "query": doc.query, "variables": variables or {}}
        payload = await self._send("POST", "/graphql", json=body, label=f"graphql.{operation}")
        if not isinstance(payload, dict):
            raise AhSchemaError(f"graphql.{operation}", "payload is not an object")
        if payload.get("errors"):
            raise AhGraphqlError(operation, payload["errors"])
        return payload.get("data")

    @staticmethod
    def _parse(name: str, model: type[M], data: Any) -> M:
        try:
            return model.model_validate(data)
        except ValidationError as e:
            raise AhSchemaError(name, str(e)) from e

    async def _access_token(self) -> str:
        if self._tokens is None:
            raise AhAuthError("no tokens: link an AH account first")
        if self._tokens.expires_within(REFRESH_MARGIN_SECONDS):
            self._tokens = await self.refresh(self._tokens)
            if self._on_tokens_refreshed is not None:
                await self._on_tokens_refreshed(self._tokens)
        return self._tokens.access_token

    # --- auth ---------------------------------------------------------------

    async def login_url(self) -> str:
        return self.build_login_url(self._client_id)

    @staticmethod
    def build_login_url(client_id: str, redirect_uri: str = REDIRECT_URI) -> str:
        return str(
            httpx.URL(
                LOGIN_URL,
                params={
                    "client_id": client_id,
                    "response_type": "code",
                    "redirect_uri": redirect_uri,
                },
            )
        )

    @staticmethod
    def extract_code(text: str) -> str | None:
        """The code from a pasted 'appie://login-exit?code=…' URL, or a bare code."""
        text = text.strip()
        if m := re.search(r"[?&]code=([^&\s]+)", text):
            return m.group(1)
        return text if re.fullmatch(r"[A-Za-z0-9._~-]{8,}", text) else None

    async def exchange_code(self, code: str) -> Tokens:
        data = await self._send(
            "POST",
            "/mobile-auth/v1/auth/token",
            json={"clientId": self._client_id, "code": code},
            authenticated=False,
        )
        tokens = Tokens.from_response(self._parse("auth.exchange_code", TokenResponse, data))
        self._tokens = tokens
        return tokens

    async def refresh(self, tokens: Tokens) -> Tokens:
        data = await self._send(
            "POST",
            "/mobile-auth/v1/auth/token/refresh",
            json={"clientId": self._client_id, "refreshToken": tokens.refresh_token},
            authenticated=False,
        )
        resp = self._parse("auth.refresh", TokenResponse, data)
        if not resp.refresh_token:  # some token servers don't rotate refresh tokens
            resp.refresh_token = tokens.refresh_token
        return Tokens.from_response(resp)

    # --- products -----------------------------------------------------------

    async def search_products(self, query: str, *, size: int = 30) -> list[Product]:
        data = await self._send(
            "GET",
            "/mobile-services/product/search/v2",
            params={"query": query, "page": 0, "size": size, "sortOn": "RELEVANCE"},
        )
        return self._parse("product.search", SearchResponse, data).products

    async def get_products(self, product_ids: list[int]) -> list[Product]:
        params: list[tuple[str, Any]] = [("ids", pid) for pid in product_ids]
        params.append(("sortOn", "INPUT_PRODUCT_IDS"))
        data = await self._send("GET", "/mobile-services/product/search/v2/products", params=params)
        # Unlike search, this endpoint returns a bare JSON array (verified in fase 0).
        try:
            return _PRODUCT_LIST.validate_python(data)
        except ValidationError as e:
            raise AhSchemaError("product.by_ids", str(e)) from e

    async def get_product(self, product_id: int) -> Product:
        data = await self._send("GET", f"/mobile-services/product/detail/v4/fir/{int(product_id)}")
        return self._parse("product.detail", ProductDetailResponse, data).product_card

    # --- bonus --------------------------------------------------------------

    async def get_bonus_metadata(self) -> BonusMetadata:
        data = await self._send("GET", "/mobile-services/bonuspage/v3/metadata")
        return self._parse("bonus.metadata", BonusMetadata, data)

    # --- orders -------------------------------------------------------------

    async def list_orders(self, since: date) -> list[Fulfillment]:
        """Delivered orders since `since`, newest first.

        AH returns at most the 10 most recent closed orders and paging is not yet known
        (docs/ah-api.md), so older history is silently missing for now.
        """
        data = await self._graphql("OrderFulfillmentsClosed")
        result = self._parse("graphql.OrderFulfillmentsClosed", FulfillmentsData, data)
        delivered = [
            f
            for f in result.order_fulfillments.result
            if f.delivery is not None
            and f.delivery.status == "DELIVERED"
            and f.delivery_date is not None
            and f.delivery_date >= since
        ]
        return sorted(delivered, key=lambda f: f.delivery_date or date.min, reverse=True)

    async def get_order(self, order_id: int) -> OrderDetails:
        data = await self._send(
            "GET", f"/mobile-services/order/v1/{int(order_id)}/details-grouped-by-taxonomy"
        )
        return self._parse("order.details", OrderDetails, data)

    async def get_active_order(self) -> ActiveOrderSummary | None:
        try:
            data = await self._send(
                "GET", "/mobile-services/order/v1/summaries/active", params={"sortBy": "DEFAULT"}
            )
        except AhHttpError as e:
            # AH answers 404 "Order does not exist" when no order is active (verified in fase 0).
            if e.status_code == 404:
                return None
            raise
        if data is None:
            return None
        return self._parse("order.active_summary", ActiveOrderSummary, data)

    async def list_upcoming_orders(self, *, today: date | None = None) -> list[UpcomingOrder]:
        """Submitted, not yet delivered orders from today on, earliest first.

        `modifiable` and `transactionCompleted` are true for every order AH returns,
        including delivered ones (fase 0), so we select on delivery status and date.
        """
        today = today or date.today()
        data = await self._graphql("OrderFulfillments")
        result = self._parse("graphql.OrderFulfillments", FulfillmentsData, data)
        upcoming = [
            f
            for f in result.order_fulfillments.result
            if f.delivery is not None
            and f.delivery.status not in _FINISHED_DELIVERY_STATUSES
            and f.delivery_date is not None
            and f.delivery_date >= today
        ]
        upcoming.sort(key=lambda f: f.delivery_date or date.max)
        return [
            UpcomingOrder(
                order_id=f.order_id,
                delivery_date=f.delivery_date,
                slot_start=f.delivery.slot.start_time if f.delivery and f.delivery.slot else None,
                slot_end=f.delivery.slot.end_time if f.delivery and f.delivery.slot else None,
                delivery_status=f.delivery.status if f.delivery else None,
            )
            for f in upcoming
        ]

    async def get_upcoming_order(self, *, today: date | None = None) -> UpcomingOrder | None:
        """The next order, enriched with cutoff and reopenable from its details."""
        orders = await self.list_upcoming_orders(today=today)
        if not orders:
            return None
        nxt = orders[0]
        details = await self.get_order(nxt.order_id)
        return nxt.model_copy(
            update={"cutoff": details.closing_time, "reopenable": details.reopenable}
        )

    async def reopen_order(self, order_id: int) -> None:
        data = await self._graphql("OrderReopen", {"id": int(order_id)})
        res = self._parse("graphql.OrderReopen", OrderReopenData, data).order_reopen
        if res.error_message:
            raise AhError(f"orderReopen {order_id}: {res.status}: {res.error_message}")

    async def revert_order(self, order_id: int) -> None:
        data = await self._graphql("OrderRevert", {"id": int(order_id)})
        res = self._parse("graphql.OrderRevert", OrderRevertData, data).order_revert
        if res.error_message:
            raise AhError(f"orderRevert {order_id}: {res.status}: {res.error_message}")

    async def add_to_order(self, order_id: int, items: list[LineItem]) -> ActiveOrderSummary | None:
        """Set absolute quantities on the active order and return the updated order.

        Quantity 0 removes the line (verified in fase 0). Refuses if `order_id` isn't active.
        """
        if not items:
            return None
        for item in items:
            if item.quantity > MAX_LINE_QUANTITY:
                raise AhError(f"quantity {item.quantity} for {item.product_id} exceeds cap")
        active = await self.get_active_order()
        if active is None or active.id != order_id:
            raise AhError(
                f"order {order_id} is not the active order "
                f"(active: {active.id if active else None}); reopen it first"
            )
        data = await self._send(
            "PUT",
            "/mobile-services/order/v1/items",
            params={"sortBy": "DEFAULT"},
            # Required by AH (fase-0 write-test: 400 without it).
            extra_headers={"Appie-Current-Order-Id": str(order_id)},
            json={
                "items": [
                    {
                        "productId": i.product_id,
                        "quantity": i.quantity,
                        "originCode": "PRD",
                        "description": "",
                        "strikethrough": False,
                    }
                    for i in items
                ]
            },
        )
        if data is None:
            return None
        return self._parse("order.set_items", ActiveOrderSummary, data)
