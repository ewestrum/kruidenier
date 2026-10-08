"""Daily price logger (SPEC §7). Price history exists nowhere else, so this runs from day 1."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ah.models import Product as AhProduct
from app.db.models import FamilyMember, PriceObservation
from app.services.catalog import upsert_product

log = logging.getLogger(__name__)

BATCH_SIZE = 30


class PriceSource(Protocol):
    async def get_products(self, product_ids: list[int]) -> list[AhProduct]: ...


@dataclass
class PriceLogResult:
    requested: int = 0
    observed: int = 0
    missing: list[int] = field(default_factory=list)


def tracked_product_ids(session: Session) -> list[int]:
    """Every SKU in any household's families (bonus products follow in fase 3)."""
    return sorted(set(session.scalars(select(FamilyMember.ah_product_id))))


def record_observation(session: Session, p: AhProduct, *, on: date) -> None:
    upsert_product(session, p)
    regular = p.price_before_bonus if p.price_before_bonus is not None else p.current_price
    price = p.current_price if p.current_price is not None else p.price_before_bonus
    obs = session.get(PriceObservation, (p.webshop_id, on)) or PriceObservation(
        ah_product_id=p.webshop_id, observed_on=on
    )
    obs.price = price
    obs.regular_price = regular
    obs.is_bonus = p.is_bonus
    obs.bonus_mechanism = p.bonus_mechanism
    obs.available = p.is_orderable if p.is_orderable is not None else p.available_online
    session.add(obs)


async def log_prices(
    client: PriceSource, session: Session, *, on: date, product_ids: Sequence[int] | None = None
) -> PriceLogResult:
    """One observation per product per day; re-running the same day overwrites it."""
    ids = list(product_ids) if product_ids is not None else tracked_product_ids(session)
    result = PriceLogResult(requested=len(ids))
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start : start + BATCH_SIZE]
        products = await client.get_products(batch)  # sequential: one request per batch
        wanted = set(batch)
        seen = set()
        for p in products:
            if p.webshop_id not in wanted or p.webshop_id in seen:
                continue  # never record what we didn't ask for
            record_observation(session, p, on=on)
            seen.add(p.webshop_id)
            result.observed += 1
        result.missing.extend(pid for pid in batch if pid not in seen)
        session.flush()
    if result.missing:
        log.warning("no price for %d products: %s", len(result.missing), result.missing[:20])
    return result
