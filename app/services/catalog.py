"""Keep the global `product` table in sync with what AH tells us."""

from typing import Protocol

from sqlalchemy.orm import Session

from app.db.models import Product
from app.domain.units import parse_unit_size


class ProductLike(Protocol):
    @property
    def webshop_id(self) -> int: ...
    @property
    def title(self) -> str: ...
    @property
    def brand(self) -> str | None: ...
    @property
    def sales_unit_size(self) -> str | None: ...
    @property
    def main_category(self) -> str | None: ...
    @property
    def sub_category(self) -> str | None: ...


def upsert_product(
    session: Session, p: ProductLike, *, min_best_before_days: int | None = None
) -> Product:
    row = session.get(Product, p.webshop_id)
    if row is None:
        row = Product(ah_id=p.webshop_id, title=p.title)
        session.add(row)
    row.title = p.title
    row.brand = p.brand
    if p.sales_unit_size:
        row.unit_size_text = p.sales_unit_size
        size = parse_unit_size(p.sales_unit_size)
        row.unit_amount = size.amount if size else None
        row.unit = size.unit.value if size else None
    row.category = p.main_category or row.category
    row.sub_category = p.sub_category or row.sub_category
    if min_best_before_days is not None:
        row.min_best_before_days = min_best_before_days
    return row
