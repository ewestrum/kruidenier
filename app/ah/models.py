"""Pydantic models for AH responses.

Status: PROVISIONAL. Shapes are taken from gwillem/appie-go and must be confirmed
against fixtures recorded by `make probe` (see docs/ah-api.md). Only fields we act
on are required; everything else is optional and unknown fields are kept
(`extra="allow"`) so recorded fixtures stay inspectable.
"""

from datetime import UTC, date, datetime, timedelta

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class AhModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


# --- auth -------------------------------------------------------------------


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    access_token: str
    refresh_token: str | None = None
    expires_in: int
    member_id: str | int | None = None


class Tokens(BaseModel):
    """What we persist (encrypted) per AhAccount."""

    access_token: str
    refresh_token: str
    expires_at: datetime

    @classmethod
    def from_response(cls, resp: TokenResponse, *, now: datetime | None = None) -> "Tokens":
        if not resp.refresh_token:
            raise ValueError("token response without refresh_token")
        issued = now or datetime.now(UTC)
        return cls(
            access_token=resp.access_token,
            refresh_token=resp.refresh_token,
            expires_at=issued + timedelta(seconds=resp.expires_in),
        )

    def expires_within(self, seconds: float, *, now: datetime | None = None) -> bool:
        return self.expires_at - (now or datetime.now(UTC)) < timedelta(seconds=seconds)


# --- products ---------------------------------------------------------------


class Product(AhModel):
    webshop_id: int
    title: str
    brand: str | None = None
    sales_unit_size: str | None = None
    unit_price_description: str | None = None
    current_price: float | None = None
    price_before_bonus: float | None = None
    is_bonus: bool = False
    bonus_mechanism: str | None = None
    main_category: str | None = None
    sub_category: str | None = None
    available_online: bool | None = None
    is_orderable: bool | None = None


class SearchPage(AhModel):
    number: int
    size: int
    total_elements: int
    total_pages: int


class SearchResponse(AhModel):
    products: list[Product]
    page: SearchPage | None = None


class ProductDetailResponse(AhModel):
    product_id: int
    product_card: Product


# --- orders -----------------------------------------------------------------


class OrderedProductRef(AhModel):
    """Product inside an order. Prices reflect the catalogue now, not what was paid."""

    webshop_id: int
    hq_id: int | None = None
    title: str
    brand: str | None = None
    sales_unit_size: str | None = None
    unit_price_description: str | None = None
    current_price: float | None = None
    price_before_bonus: float | None = None
    is_bonus: bool = False
    bonus_mechanism: str | None = None
    main_category: str | None = None
    sub_category: str | None = None
    # Guaranteed minimum freshness on delivery; only set for fresh products (fase 0).
    min_best_before_days: int | None = None


class OrderedProduct(AhModel):
    quantity: int
    amount: int | None = None
    # Present on some delivered lines only; equal to `quantity` in all fase-0 samples.
    allocated_quantity: int | None = None
    product: OrderedProductRef

    @property
    def delivered_quantity(self) -> int:
        return self.allocated_quantity if self.allocated_quantity is not None else self.quantity


class OrderTotalPrice(AhModel):
    price_before_discount: float | None = None
    price_after_discount: float | None = None
    price_discount: float | None = None
    price_total_payable: float | None = None


class DeliveryInformation(AhModel):
    delivery_date: date | None = None
    delivery_start_time: str | None = None
    delivery_end_time: str | None = None


class ActiveOrderSummary(AhModel):
    id: int
    state: str
    shopping_type: str | None = None
    total_price: OrderTotalPrice | None = None
    delivery_information: DeliveryInformation | None = None
    ordered_products: list[OrderedProduct] = Field(default_factory=list)


class TaxonomyGroup(AhModel):
    taxonomy_name: str | None = None
    ordered_products: list[OrderedProduct]


class DeliveryTimePeriod(AhModel):
    start_date_time: datetime | None = None
    end_date_time: datetime | None = None
    start_time_utc: datetime | None = None
    end_time_utc: datetime | None = None


class OrderDetails(AhModel):
    """Seen in fase 0: orderState CONFIRMED (upcoming) / DELIVERED; orderMethod PLANSERVICE
    (recurring slot) or APPIE_WEB_OLD."""

    order_id: int
    order_state: str
    delivery_date: date | None = None
    delivery_type: str | None = None
    delivery_time_period: DeliveryTimePeriod | None = None
    # The cutoff: last moment the order can be changed (UTC).
    closing_time: datetime | None = None
    reopenable: bool | None = None
    cancellable: bool | None = None
    order_method: str | None = None
    grouped_products: list[TaxonomyGroup] = Field(
        validation_alias=AliasChoices("groupedProductsInTaxonomy", "grouped_products")
    )

    def lines(self) -> list[OrderedProduct]:
        return [p for g in self.grouped_products for p in g.ordered_products]


class LineItem(BaseModel):
    """A line we want on the order. `quantity` is absolute, not a delta."""

    product_id: int
    quantity: int = Field(ge=0)


# --- graphql: fulfillments --------------------------------------------------


class Amount(AhModel):
    amount: float


class FulfillmentPrice(AhModel):
    total_price: Amount | None = None


class Slot(AhModel):
    slot_date: date | None = Field(default=None, alias="date")
    date_display: str | None = None
    time_display: str | None = None
    start_time: str | None = None
    end_time: str | None = None


class FulfillmentDelivery(AhModel):
    status: str | None = None
    method: str | None = None
    slot: Slot | None = None


class Fulfillment(AhModel):
    """One order in `orderFulfillments`.

    Seen in fase 0: statusCode 10 "Order is bevestigd" (delivery SUBMITTED),
    60 "Geïncasseerd" (DELIVERED), 99 "Geannuleerd" (CANCELLED). `modifiable` and
    `transactionCompleted` are true for (nearly) everything and carry no signal.
    """

    order_id: int
    status_code: int | str | None = None
    status_description: str | None = None
    shopping_type: str | None = None
    transaction_completed: bool | None = None
    modifiable: bool | None = None
    total_price: FulfillmentPrice | None = None
    delivery: FulfillmentDelivery | None = None

    @property
    def delivery_date(self) -> date | None:
        if self.delivery is None or self.delivery.slot is None:
            return None
        return self.delivery.slot.slot_date


class FulfillmentList(AhModel):
    result: list[Fulfillment]


class FulfillmentsData(AhModel):
    order_fulfillments: FulfillmentList


class MutationResult(AhModel):
    status: str
    error_message: str | None = None


class OrderReopenData(AhModel):
    order_reopen: MutationResult


class OrderRevertData(AhModel):
    order_revert: MutationResult


class UpcomingOrder(BaseModel):
    """Adapter-level view of the next modifiable order. Not an AH response shape."""

    order_id: int
    delivery_date: date | None
    slot_start: str | None
    slot_end: str | None
    delivery_status: str | None = None
    # From order details `closingTime` (UTC); not part of orderFulfillments.
    cutoff: datetime | None = None
    reopenable: bool | None = None


# --- bonus ------------------------------------------------------------------


class BonusPeriod(AhModel):
    bonus_start_date: date
    bonus_end_date: date


class BonusMetadata(AhModel):
    periods: list[BonusPeriod]
