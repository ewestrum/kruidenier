from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import FamilyMember, Household, Product, ProductFamily, Purchase
from app.services.families import assign_families
from app.services.history import import_history
from tests.services.conftest import FakeAh


async def test_import_recorded_history(
    sessions: sessionmaker[Session], household: Household
) -> None:
    fake = FakeAh()
    with sessions.begin() as s:
        result = await import_history(fake, s, household_id=household.id, since=date(2026, 1, 1))
    assert sorted(result.orders_imported) == sorted(fake.details)
    with sessions() as s:
        n = s.scalar(select(func.count()).select_from(Purchase))
        assert n == result.purchases_added > 40
        milk = s.scalars(select(Product).where(Product.title == "AH Houdbare halfvolle melk")).one()
        assert (milk.unit, milk.unit_amount) == ("ml", 1000)
        p = s.scalars(select(Purchase).where(Purchase.ah_product_id == milk.ah_id)).one()
        assert p.qty == 4 and p.delivered_at == date(2026, 9, 28)


async def test_import_is_idempotent(sessions: sessionmaker[Session], household: Household) -> None:
    fake = FakeAh()
    with sessions.begin() as s:
        await import_history(fake, s, household_id=household.id, since=date(2026, 1, 1))
    fake.calls.clear()
    with sessions.begin() as s:
        again = await import_history(fake, s, household_id=household.id, since=date(2026, 1, 1))
    assert again.orders_imported == [] and again.purchases_added == 0
    assert fake.calls == ["list_orders"]  # no detail calls for known orders


async def test_families_group_pack_variants(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        await import_history(FakeAh(), s, household_id=household.id, since=date(2026, 1, 1))
        created = assign_families(s, household.id)
    assert created
    with sessions() as s:
        andijvie = s.scalars(
            select(ProductFamily).where(ProductFamily.name == "Andijvie fijngesneden")
        ).one()
        members = s.scalars(select(FamilyMember).where(FamilyMember.family_id == andijvie.id)).all()
        assert len(members) == 2
        assert sum(m.preferred for m in members) == 1
        n_products = s.scalar(select(func.count(func.distinct(Purchase.ah_product_id))))
        n_members = s.scalar(select(func.count()).select_from(FamilyMember))
        assert n_members == n_products


async def test_assign_families_keeps_manual_changes(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        await import_history(FakeAh(), s, household_id=household.id, since=date(2026, 1, 1))
        assign_families(s, household.id)
        fam = s.scalars(select(ProductFamily).order_by(ProductFamily.id)).first()
        assert fam is not None
        fam.name = "Mijn eigen naam"
        fam.pinned = True
    with sessions.begin() as s:
        assert assign_families(s, household.id) == []
        fam2 = s.get(ProductFamily, fam.id)
        assert fam2 is not None and fam2.name == "Mijn eigen naam" and fam2.pinned
