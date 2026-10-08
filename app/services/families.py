"""Put every purchased product into a family (heuristic first pass, SPEC §6.7)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FamilyMember, Product, ProductFamily, Purchase
from app.domain.families import family_key, family_name


def assign_families(session: Session, household_id: int) -> list[ProductFamily]:
    """Give products without a family in this household one. Returns created families.

    Existing (possibly hand-corrected) families are never changed.
    """
    families = session.scalars(
        select(ProductFamily).where(ProductFamily.household_id == household_id)
    ).all()
    member_ids = set(
        session.scalars(
            select(FamilyMember.ah_product_id)
            .join(ProductFamily)
            .where(ProductFamily.household_id == household_id)
        )
    )
    by_key = {(f.name.lower(), f.base_unit): f for f in families}

    purchased = session.scalars(
        select(Product)
        .join(Purchase, Purchase.ah_product_id == Product.ah_id)
        .where(Purchase.household_id == household_id)
        .distinct()
        .order_by(Product.ah_id)
    ).all()

    created: list[ProductFamily] = []
    for product in purchased:
        if product.ah_id in member_ids:
            continue
        key = family_key(product.title, product.brand)
        base_unit = product.unit or "st"
        family = by_key.get((family_name(key).lower(), base_unit))
        is_new = family is None
        if family is None:
            family = ProductFamily(
                household_id=household_id, name=family_name(key), base_unit=base_unit
            )
            session.add(family)
            session.flush()
            by_key[(family.name.lower(), base_unit)] = family
            created.append(family)
        session.add(
            FamilyMember(family_id=family.id, ah_product_id=product.ah_id, preferred=is_new)
        )
        member_ids.add(product.ah_id)
    session.flush()
    return created
