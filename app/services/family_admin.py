"""Manual family corrections from the UI (SPEC §10 "Families"). Always household-scoped."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FamilyMember, FamilyStats, PlanLine, Product, ProductFamily, Purchase


class FamilyError(ValueError):
    """Shown to the user as-is (Dutch)."""


@dataclass(frozen=True)
class MemberView:
    product_id: int
    title: str
    unit_size: str | None
    preferred: bool
    times_bought: int


@dataclass(frozen=True)
class FamilyView:
    id: int
    name: str
    base_unit: str
    pinned: bool
    excluded: bool
    confidence: str
    n_purchases: int
    last_purchase: date | None
    due: date | None
    members: list[MemberView]


def _family(session: Session, household_id: int, family_id: int) -> ProductFamily:
    family = session.get(ProductFamily, family_id)
    if family is None or family.household_id != household_id:
        raise FamilyError("Deze familie bestaat niet (meer).")
    return family


def family_view(session: Session, family: ProductFamily) -> FamilyView:
    stats = session.get(FamilyStats, family.id)
    members = []
    for member in sorted(family.members, key=lambda m: (not m.preferred, m.product.title)):
        bought = len(
            session.scalars(
                select(Purchase.id).where(
                    Purchase.household_id == family.household_id,
                    Purchase.ah_product_id == member.ah_product_id,
                )
            ).all()
        )
        members.append(
            MemberView(
                member.ah_product_id,
                member.product.title,
                member.product.unit_size_text,
                member.preferred,
                bought,
            )
        )
    return FamilyView(
        id=family.id,
        name=family.name,
        base_unit=family.base_unit,
        pinned=family.pinned,
        excluded=family.excluded,
        confidence=stats.confidence if stats else "low",
        n_purchases=stats.n_purchases if stats else 0,
        last_purchase=stats.last_purchase_at if stats else None,
        due=stats.due_date if stats else None,
        members=members,
    )


def list_families(session: Session, household_id: int, query: str = "") -> list[FamilyView]:
    stmt = select(ProductFamily).where(ProductFamily.household_id == household_id)
    if query.strip():
        stmt = stmt.where(ProductFamily.name.ilike(f"%{query.strip()}%"))
    families = session.scalars(stmt.order_by(ProductFamily.name)).all()
    views = [family_view(session, f) for f in families]
    return sorted(views, key=lambda v: (v.excluded, -v.n_purchases, v.name.lower()))


def get_family(session: Session, household_id: int, family_id: int) -> FamilyView:
    return family_view(session, _family(session, household_id, family_id))


def rename(session: Session, household_id: int, family_id: int, name: str) -> None:
    name = " ".join(name.split())
    if not name:
        raise FamilyError("Een familie heeft een naam nodig.")
    _family(session, household_id, family_id).name = name[:200]


def set_flags(
    session: Session,
    household_id: int,
    family_id: int,
    *,
    pinned: bool | None = None,
    excluded: bool | None = None,
) -> None:
    family = _family(session, household_id, family_id)
    if pinned is not None:
        family.pinned = pinned
        if pinned:
            family.excluded = False
    if excluded is not None:
        family.excluded = excluded
        if excluded:
            family.pinned = False


def set_preferred(session: Session, household_id: int, family_id: int, product_id: int) -> None:
    family = _family(session, household_id, family_id)
    if product_id not in {m.ah_product_id for m in family.members}:
        raise FamilyError("Dat product hoort niet bij deze familie.")
    for member in family.members:
        member.preferred = member.ah_product_id == product_id


def merge(session: Session, household_id: int, target_id: int, source_ids: list[int]) -> int:
    """Move all products of the sources into the target and delete the sources."""
    target = _family(session, household_id, target_id)
    sources = [_family(session, household_id, i) for i in set(source_ids) if i != target_id]
    if not sources:
        raise FamilyError("Kies minstens één andere familie om samen te voegen.")
    target_units = {target.base_unit}
    for source in sources:
        if source.base_unit not in target_units:
            raise FamilyError(
                f"'{source.name}' telt in {source.base_unit} en '{target.name}' in "
                f"{target.base_unit}; die kun je niet samenvoegen."
            )
    moved = 0
    existing = {m.ah_product_id for m in target.members}
    for source in sources:
        for member in list(source.members):
            if member.ah_product_id not in existing:
                target.members.append(
                    FamilyMember(ah_product_id=member.ah_product_id, preferred=False)
                )
                existing.add(member.ah_product_id)
                moved += 1
        target.pinned = target.pinned or source.pinned
        for line in session.scalars(select(PlanLine).where(PlanLine.family_id == source.id)):
            session.delete(line)
        stats = session.get(FamilyStats, source.id)
        if stats is not None:
            session.delete(stats)
        session.delete(source)
    session.flush()
    return moved


def split(session: Session, household_id: int, family_id: int, product_id: int) -> ProductFamily:
    """Take one product out of a family into its own new family."""
    family = _family(session, household_id, family_id)
    member = next((m for m in family.members if m.ah_product_id == product_id), None)
    if member is None:
        raise FamilyError("Dat product hoort niet bij deze familie.")
    if len(family.members) == 1:
        raise FamilyError("Dit is het enige product in de familie.")
    product = session.get(Product, product_id)
    assert product is not None
    was_preferred = member.preferred
    family.members.remove(member)
    session.flush()
    if was_preferred and family.members:
        family.members[0].preferred = True
    new = ProductFamily(household_id=household_id, name=product.title, base_unit=family.base_unit)
    new.members.append(FamilyMember(ah_product_id=product_id, preferred=True))
    session.add(new)
    session.flush()
    return new
