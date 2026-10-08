from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import ActionLog, Household, Plan, PlanLine, Product, ProductFamily, Purchase
from app.services import evaluation
from app.services.families import assign_families
from app.services.notify import Message, notifier_for
from app.services.reminders import run_reminders
from tests.services.conftest import CollectingNotifier

DELIVERY = date(2026, 10, 11)
ORDER = 777
MILK, BREAD, CHEESE, NEW = 1, 2, 3, 4


def seed_history(s: Session, household_id: int) -> dict[str, int]:
    """Milk, bread and cheese bought weekly for 5 weeks before the delivery."""
    for pid, title in [(MILK, "Melk"), (BREAD, "Brood"), (CHEESE, "Kaas"), (NEW, "Saffraan")]:
        s.add(Product(ah_id=pid, title=title, unit="st", unit_amount=1))
    s.flush()
    for week in range(1, 6):
        for pid in (MILK, BREAD, CHEESE):
            s.add(
                Purchase(
                    household_id=household_id,
                    ah_order_id=100 + week,
                    ah_product_id=pid,
                    qty=1,
                    delivered_at=DELIVERY - timedelta(weeks=week),
                )
            )
    # saffraan once, long ago: has a family, but too rare for the model to "know"
    s.add(
        Purchase(
            household_id=household_id,
            ah_order_id=99,
            ah_product_id=NEW,
            qty=1,
            delivered_at=DELIVERY - timedelta(weeks=12),
        )
    )
    s.flush()
    assign_families(s, household_id)
    return {f.name: f.id for f in s.scalars(select(ProductFamily))}


def deliver(s: Session, household_id: int, products: list[int]) -> None:
    for pid in products:
        s.add(
            Purchase(
                household_id=household_id,
                ah_order_id=ORDER,
                ah_product_id=pid,
                qty=1,
                delivered_at=DELIVERY,
            )
        )
    s.flush()
    assign_families(s, household_id)


def plan_with(s: Session, household_id: int, fams: dict[str, int], names: list[str]) -> Plan:
    plan = Plan(
        household_id=household_id, delivery_date=DELIVERY, ah_order_id=ORDER, status="applied"
    )
    s.add(plan)
    s.flush()
    for n in names:
        s.add(
            PlanLine(plan_id=plan.id, family_id=fams[n], ah_product_id=1, qty=1, reason_code="due")
        )
    s.flush()
    return plan


def test_evaluation_counts_hits_missed_extra(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        fams = seed_history(s, household.id)
        plan = plan_with(s, household.id, fams, ["Melk", "Brood", "Saffraan"])
        assert evaluation.evaluate(s, plan) is None  # not delivered yet
        deliver(s, household.id, [MILK, BREAD, CHEESE])
        e = evaluation.evaluate(s, plan)
        assert e is not None
        assert e.titled(e.overlap.hits) == ["Brood", "Melk"]
        assert e.titled(e.overlap.missed) == ["Kaas"]  # known: bought 5x before
        assert e.titled(e.overlap.extra) == ["Saffraan"]  # proposed, not delivered
        assert e.score == pytest.approx(2 / 4)
        text = evaluation.summary(e)
        assert "50% raak" in text and "Gemist: Kaas" in text


def test_first_time_purchase_is_not_a_miss(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        fams = seed_history(s, household.id)
        plan = plan_with(s, household.id, fams, ["Melk", "Brood", "Kaas"])
        deliver(s, household.id, [MILK, BREAD, CHEESE, NEW])  # saffraan: bought only once before
        e = evaluation.evaluate(s, plan)
        assert e is not None and e.overlap.missed == frozenset() and e.score == 1.0


def test_unnotified_only_once(sessions: sessionmaker[Session], household: Household) -> None:
    with sessions.begin() as s:
        fams = seed_history(s, household.id)
        plan_with(s, household.id, fams, ["Melk", "Brood", "Kaas"])
        deliver(s, household.id, [MILK, BREAD, CHEESE])
        first = evaluation.unnotified(s, household.id, today=DELIVERY)
        assert len(first) == 1 and first[0].score == 1.0
        assert evaluation.unnotified(s, household.id, today=DELIVERY) == []


def test_autopilot_ready_after_two_good_deliveries(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        fams = seed_history(s, household.id)
        plan_with(s, household.id, fams, ["Melk", "Brood", "Kaas"])
        deliver(s, household.id, [MILK, BREAD, CHEESE])
        evals = evaluation.recent_evaluations(s, household.id, today=DELIVERY)
        assert not evaluation.autopilot_ready(evals)  # only one week so far


# --- reminders ---------------------------------------------------------------------

NOW = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)


def draft(s: Session, household_id: int, *, cutoff: datetime, applied: bool = False) -> Plan:
    s.add(Product(ah_id=50, title="Melk"))
    fam = ProductFamily(household_id=household_id, name="Melk", base_unit="st")
    s.add(fam)
    s.flush()
    plan = Plan(household_id=household_id, delivery_date=DELIVERY, ah_order_id=ORDER, cutoff=cutoff)
    s.add(plan)
    s.flush()
    s.add(
        PlanLine(
            plan_id=plan.id,
            family_id=fam.id,
            ah_product_id=50,
            qty=2,
            reason_code="due",
            applied=applied,
        )
    )
    s.flush()
    return plan


async def test_reminder_sent_once_inside_window(
    sessions: sessionmaker[Session], household: Household
) -> None:
    with sessions.begin() as s:
        draft(s, household.id, cutoff=NOW + timedelta(hours=2))
    notifier = CollectingNotifier()
    settings = Settings(base_url="http://nas:8085")
    sent = await run_reminders(sessions, settings=settings, notifiers=lambda _: notifier, now=NOW)
    assert len(sent) == 1
    [msg] = notifier.sent
    assert "sluit om 12:00" in msg.body  # 10:00 UTC = 12:00 in Amsterdam
    assert "1 product uit het voorstel staat er nog niet in" in msg.body
    assert msg.url == "http://nas:8085/week"
    await run_reminders(
        sessions, settings=settings, notifiers=lambda _: notifier, now=NOW + timedelta(minutes=15)
    )
    assert len(notifier.sent) == 1


@pytest.mark.parametrize(
    ("cutoff_in", "applied", "hours", "expected"),
    [
        (timedelta(hours=5), False, 3, 0),  # too early
        (timedelta(minutes=-1), False, 3, 0),  # already closed
        (timedelta(hours=2), True, 3, 0),  # everything already in the order
        (timedelta(hours=2), False, 0, 0),  # switched off
        (timedelta(hours=5), False, 6, 1),  # larger window set by the household
    ],
)
async def test_reminder_conditions(
    sessions: sessionmaker[Session],
    household: Household,
    cutoff_in: timedelta,
    applied: bool,
    hours: int,
    expected: int,
) -> None:
    with sessions.begin() as s:
        h = s.get(Household, household.id)
        assert h is not None
        h.settings_json = {"reminder_hours": hours}
        draft(s, household.id, cutoff=NOW + cutoff_in, applied=applied)
    notifier = CollectingNotifier()
    await run_reminders(sessions, settings=Settings(), notifiers=lambda _: notifier, now=NOW)
    assert len(notifier.sent) == expected


async def test_failed_send_is_retried_next_tick(
    sessions: sessionmaker[Session], household: Household
) -> None:
    class Failing:
        async def send(self, message: Message) -> bool:
            return False

    with sessions.begin() as s:
        draft(s, household.id, cutoff=NOW + timedelta(hours=2))
    await run_reminders(sessions, settings=Settings(), notifiers=lambda _: Failing(), now=NOW)
    with sessions() as s:
        assert s.scalars(select(ActionLog)).all() == []
    ok = CollectingNotifier()
    await run_reminders(sessions, settings=Settings(), notifiers=lambda _: ok, now=NOW)
    assert len(ok.sent) == 1


# --- notifier per household ------------------------------------------------------------


async def test_household_channel_wins_over_env() -> None:
    settings = Settings(ntfy_url="https://ntfy.example/env-topic")
    with respx.mock(assert_all_mocked=True) as router:
        own = router.post("https://ntfy.example/own-topic").respond(200)
        env = router.post("https://ntfy.example/env-topic").respond(200)
        h = Household(name="Thuis", settings_json={"ntfy_url": "https://ntfy.example/own-topic"})
        assert await notifier_for(h, settings).send(Message("Titel", "Tekst"))
        assert own.called and not env.called
        assert await notifier_for(Household(name="X", settings_json={}), settings).send(
            Message("Titel", "Tekst")
        )
        assert env.called
    assert not notifier_for(None, Settings()).configured


async def test_ntfy_title_with_non_ascii_is_safe() -> None:
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post("https://ntfy.example/t").respond(200)
        h = Household(name="Thuis", settings_json={"ntfy_url": "https://ntfy.example/t"})
        await notifier_for(h, Settings()).send(Message("Kruidenier: geïmporteerd", "Tekst é"))
        req: httpx.Request = route.calls.last.request
        assert req.content.decode() == "Tekst é"
