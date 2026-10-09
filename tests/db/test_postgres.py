"""Tests that only mean something on real Postgres (SQLite behaves differently).

Run locally with a throwaway database:
    docker run -d --rm --name kr-pg -e POSTGRES_USER=k -e POSTGRES_PASSWORD=k \
        -e POSTGRES_DB=k -p 55440:5432 postgres:16-alpine
    KRUIDENIER_TEST_PG=postgresql+psycopg://k:k@localhost:55440/k uv run pytest tests/db

CI starts a Postgres service and sets KRUIDENIER_TEST_PG (.github/workflows/image.yml).
"""

import os
from collections.abc import Iterator
from datetime import date

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.migrate import alembic_config
from app.db.models import Household, PriceObservation, Product, Purchase
from app.services.prices import log_prices
from tests.services.conftest import FakeAh

PG_URL = os.environ.get("KRUIDENIER_TEST_PG")
pytestmark = pytest.mark.skipif(not PG_URL, reason="KRUIDENIER_TEST_PG not set")


def migrate(engine: Engine, target: str, direction: str = "upgrade") -> None:
    cfg = alembic_config()
    cfg.attributes["configure_logger"] = False
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, direction)(cfg, target)


@pytest.fixture
def pg() -> Iterator[Engine]:
    assert PG_URL is not None
    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_migrations_up_down_up_with_data(pg: Engine) -> None:
    migrate(pg, "0001")
    with pg.begin() as c:
        c.execute(
            text(
                "INSERT INTO household (name, settings_json, created_at) "
                "VALUES ('Thuis', '{}', now())"
            )
        )
        c.execute(text("INSERT INTO product (ah_id, title, updated_at) VALUES (1, 'Melk', now())"))
        c.execute(
            text(
                "INSERT INTO purchase (household_id, ah_order_id, ah_product_id, qty, "
                "delivered_at, source) VALUES (1, 10, 1, 2, '2026-10-04', 'staple')"
            )
        )
    migrate(pg, "head")
    migrate(pg, "base", "downgrade")
    migrate(pg, "head")
    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM household")).scalar() == 0  # base wiped


async def test_price_logger_stores_new_product_before_its_price(pg: Engine) -> None:
    """Regression: the unit of work does not order inserts by plain foreign keys."""
    migrate(pg, "head")
    sessions = sessionmaker(pg, expire_on_commit=False)
    with sessions.begin() as s:
        result = await log_prices(FakeAh(), s, on=date(2026, 10, 7), product_ids=[609672])
    assert result.observed == 1
    with sessions() as s:
        assert s.get(Product, 609672) is not None
        assert s.get(PriceObservation, (609672, date(2026, 10, 7))) is not None


def test_store_and_online_purchases_coexist_and_stay_unique(pg: Engine) -> None:
    migrate(pg, "head")
    sessions = sessionmaker(pg, expire_on_commit=False)
    with sessions.begin() as s:
        h = Household(name="Thuis", settings_json={})
        s.add(h)
        s.add(Product(ah_id=1, title="Melk"))
        s.flush()
        s.add(
            Purchase(
                household_id=h.id,
                ah_order_id=10,
                ah_product_id=1,
                qty=1,
                delivered_at=date(2026, 10, 4),
            )
        )
        s.add(
            Purchase(
                household_id=h.id,
                receipt_id="r1",
                ah_order_id=None,
                ah_product_id=1,
                qty=1,
                delivered_at=date(2026, 10, 3),
                source="store",
            )
        )
        s.add(
            Purchase(
                household_id=h.id,
                receipt_id="r2",
                ah_order_id=None,
                ah_product_id=1,
                qty=1,
                delivered_at=date(2026, 10, 5),
                source="store",
            )
        )
        hid = h.id
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(Purchase)) == 3
    with pytest.raises(IntegrityError), sessions.begin() as s:
        s.add(
            Purchase(
                household_id=hid,
                receipt_id="r1",
                ah_order_id=None,
                ah_product_id=1,
                qty=5,
                delivered_at=date(2026, 10, 3),
                source="store",
            )
        )
