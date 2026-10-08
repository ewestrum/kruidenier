"""End-to-end daily sync through the real HttpAhClient, with AH mocked by respx."""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import respx
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.ah.models import Tokens
from app.ah.token_crypto import TokenCipher
from app.config import Settings
from app.db.models import (
    AhAccount,
    FamilyMember,
    Household,
    Plan,
    PriceObservation,
    Product,
    ProductFamily,
    Purchase,
)
from app.services import accounts
from app.services.accounts import save_account
from app.services.sync import daily_prices, daily_sync
from tests.conftest import FIXTURES
from tests.services.conftest import CollectingNotifier, recorded, recorded_order_details

TODAY = date(2026, 10, 7)


def setup_account(sessions: sessionmaker[Session], household: Household) -> TokenCipher:
    cipher = TokenCipher(Fernet.generate_key().decode())
    tokens = Tokens(
        access_token="acc", refresh_token="ref", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    with sessions.begin() as s:
        h = s.get(Household, household.id)
        assert h is not None
        save_account(s, household=h, label="AH", tokens=tokens, cipher=cipher)
    accounts._LIMITER = None  # fresh limiter per test
    return cipher


def settings() -> Settings:
    return Settings(ah_min_request_interval=0.0)


def mock_ah(router: respx.MockRouter) -> dict[int, dict[str, object]]:
    """Recorded AH, with history limited to the orders whose details were recorded."""
    delivered = recorded_order_details()
    closed = recorded("graphql.OrderFulfillmentsClosed")
    result = closed["data"]["orderFulfillments"]["result"]
    closed["data"]["orderFulfillments"]["result"] = [f for f in result if f["orderId"] in delivered]
    responses = {
        "OrderFulfillments": recorded("graphql.OrderFulfillments"),
        "OrderFulfillmentsClosed": closed,
    }

    def graphql(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[json.loads(request.content)["operationName"]])

    router.post("/graphql").mock(side_effect=graphql)
    details = dict(delivered)
    upcoming_ids = {
        f["orderId"] for f in responses["OrderFulfillments"]["data"]["orderFulfillments"]["result"]
    }
    for p in sorted(FIXTURES.glob("order.details*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if d["orderId"] in upcoming_ids:
            details[d["orderId"]] = d  # whatever state the last probe run recorded
    for order_id, data in details.items():
        router.get(f"/mobile-services/order/v1/{order_id}/details-grouped-by-taxonomy").respond(
            json=data
        )
    return delivered


async def test_daily_sync_end_to_end(
    sessions: sessionmaker[Session], household: Household, ah_api: respx.MockRouter
) -> None:
    cipher = setup_account(sessions, household)
    delivered = mock_ah(ah_api)
    notifier = CollectingNotifier()
    report = await daily_sync(
        sessions, settings=settings(), cipher=cipher, notifiers=lambda _: notifier, today=TODAY
    )
    assert report.errors == [] and notifier.sent == []
    assert report.orders_imported == len(delivered) == 2
    with sessions() as s:
        plan = s.scalars(select(Plan)).one()
        assert plan.delivery_date == date(2026, 10, 11)
        assert plan.ah_order_id == 150638209
        assert plan.cutoff is not None
        assert plan.status == "draft"
        assert s.scalar(select(func.count()).select_from(Purchase)) > 40

    # second run on the same day: nothing new, same single draft
    report2 = await daily_sync(
        sessions, settings=settings(), cipher=cipher, notifiers=lambda _: notifier, today=TODAY
    )
    assert report2.orders_imported == 0
    with sessions() as s:
        assert len(s.scalars(select(Plan)).all()) == 1


async def test_schema_error_notifies_and_does_nothing(
    sessions: sessionmaker[Session], household: Household, ah_api: respx.MockRouter
) -> None:
    cipher = setup_account(sessions, household)
    ah_api.post("/graphql").respond(json={"data": {"orderFulfillments": {"oops": []}}})
    notifier = CollectingNotifier()
    report = await daily_sync(
        sessions, settings=settings(), cipher=cipher, notifiers=lambda _: notifier, today=TODAY
    )
    assert report.errors and "AhSchemaError" in report.errors[0]
    assert [m.title for m in notifier.sent] == ["Kruidenier: AH-koppeling kapot"]
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(Purchase)) == 0
        assert s.scalars(select(Plan)).all() == []


async def test_auth_error_asks_to_relink(
    sessions: sessionmaker[Session], household: Household, ah_api: respx.MockRouter
) -> None:
    cipher = setup_account(sessions, household)
    ah_api.post("/graphql").respond(status_code=401)
    notifier = CollectingNotifier()
    await daily_sync(
        sessions, settings=settings(), cipher=cipher, notifiers=lambda _: notifier, today=TODAY
    )
    assert notifier.sent[0].title == "Kruidenier: AH-account opnieuw koppelen"


async def test_refreshed_tokens_are_persisted_encrypted(
    sessions: sessionmaker[Session], household: Household, ah_api: respx.MockRouter
) -> None:
    cipher = setup_account(sessions, household)
    track_one_product(sessions, household)
    with sessions.begin() as s:
        acc = s.scalars(select(AhAccount)).one()
        acc.tokens_enc = cipher.encrypt(
            Tokens(access_token="old", refresh_token="ref", expires_at=datetime.now(UTC))
        )
    refresh = ah_api.post("/mobile-auth/v1/auth/token/refresh").respond(
        json={"access_token": "new", "refresh_token": "ref2", "expires_in": 3600}
    )
    prices = ah_api.get("/mobile-services/product/search/v2/products").respond(json=[])
    await daily_prices(
        sessions,
        settings=settings(),
        cipher=cipher,
        notifiers=lambda _: CollectingNotifier(),
        today=TODAY,
    )
    assert refresh.called
    assert prices.calls.last.request.headers["Authorization"] == "Bearer new"
    with sessions() as s:
        acc = s.scalars(select(AhAccount)).one()
        assert b"ref2" not in acc.tokens_enc
        assert cipher.decrypt(acc.tokens_enc).refresh_token == "ref2"


def track_one_product(sessions: sessionmaker[Session], household: Household) -> None:
    with sessions.begin() as s:
        s.add(Product(ah_id=609672, title="Campina melk"))
        fam = ProductFamily(household_id=household.id, name="Melk", base_unit="st")
        s.add(fam)
        s.flush()
        s.add(FamilyMember(family_id=fam.id, ah_product_id=609672, preferred=True))


async def test_daily_prices_logs_family_products(
    sessions: sessionmaker[Session], household: Household, ah_api: respx.MockRouter
) -> None:
    cipher = setup_account(sessions, household)
    track_one_product(sessions, household)
    ah_api.get("/mobile-services/product/search/v2/products").respond(
        json=recorded("product.by_ids")
    )
    n = await daily_prices(
        sessions,
        settings=settings(),
        cipher=cipher,
        notifiers=lambda _: CollectingNotifier(),
        today=TODAY,
    )
    assert n == 1
    with sessions() as s:
        assert s.get(PriceObservation, (609672, TODAY)) is not None
