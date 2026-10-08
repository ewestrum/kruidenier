from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    ActionLog,
    AhAccount,
    Household,
    Plan,
    PlanLine,
    PriceObservation,
    Product,
    Purchase,
)
from app.services.autopilot import in_window, run_autopilot
from app.services.families import assign_families
from app.services.planner import build_draft_plan, recompute_stats
from app.services.push import PushError, push_actions, push_plan, undo_push
from tests.services.conftest import CollectingNotifier
from tests.services.fake_order import FakeOrder

MILK, BREAD, RARE = 1525, 4401, 7777
NOW = datetime.now(UTC)
TODAY = NOW.date()


def seed(s: Session, household_id: int, order: FakeOrder, *, settings: dict | None = None) -> Plan:
    """Weekly milk and bread (sure: auto) + a pinned, rarely bought item (propose)."""
    h = s.get(Household, household_id)
    assert h is not None
    h.settings_json = settings or {}
    for pid, title, size, amount, unit in [
        (MILK, "AH Halfvolle melk", "1 l", 1000, "ml"),
        (BREAD, "AH Volkoren brood", "800 g", 800, "g"),
        (RARE, "Saffraan", "1 stuk", 1, "st"),
    ]:
        s.add(Product(ah_id=pid, title=title, unit_size_text=size, unit_amount=amount, unit=unit))
        s.flush()
        s.add(PriceObservation(ah_product_id=pid, observed_on=TODAY, price=2.0, regular_price=2.0))
    for i in range(6):
        s.add(
            Purchase(
                household_id=household_id,
                ah_order_id=100 + i,
                ah_product_id=MILK,
                qty=2,
                delivered_at=TODAY - timedelta(days=7 * (6 - i)),
            )
        )
        s.add(
            Purchase(
                household_id=household_id,
                ah_order_id=200 + i,
                ah_product_id=BREAD,
                qty=2,
                delivered_at=TODAY - timedelta(days=7 * (6 - i)),
            )
        )
    for i, days_ago in enumerate((60, 30)):  # two purchases: has a rate, below noise level
        s.add(
            Purchase(
                household_id=household_id,
                ah_order_id=300 + i,
                ah_product_id=RARE,
                qty=1,
                delivered_at=TODAY - timedelta(days=days_ago),
            )
        )
    s.flush()
    for fam in assign_families(s, household_id):
        if fam.name == "Saffraan":
            fam.pinned = True
    recompute_stats(s, h, today=TODAY)
    plan = build_draft_plan(
        s,
        h,
        delivery_date=order.delivery,
        today=TODAY,
        ah_order_id=order.order_id,
        cutoff=order.cutoff,
    )
    assert {line.ah_product_id for line in plan.lines} == {MILK, BREAD, RARE}
    return plan


def tiers(plan: Plan) -> dict[int, str]:
    return {line.ah_product_id: line.tier for line in plan.lines}


async def test_push_from_confirmed_reopens_sets_and_logs(
    sessions: sessionmaker[Session], household: Household
) -> None:
    order = FakeOrder(items={9999: 1})  # something the person already ordered
    with sessions.begin() as s:
        plan = seed(s, household.id, order)
        assert tiers(plan) == {MILK: "auto", BREAD: "auto", RARE: "propose"}
        wanted = {line.ah_product_id: line.qty for line in plan.lines}
        result = await push_plan(order, s, household_id=household.id, actor="user:a@b.nl", now=NOW)
    assert order.calls[:3] == ["upcoming", "details", "details"]
    assert "reopen" in order.calls and order.state == "REOPENED"
    assert order.items == {9999: 1, **wanted}
    assert len(result.changed) == 3 and result.not_taken == []
    with sessions() as s:
        plan = s.scalars(select(Plan)).one()
        assert plan.status == "applied" and all(line.applied for line in plan.lines)
        entry = s.get(ActionLog, result.action_id)
        assert entry is not None and entry.payload_json["reopened"] is True
        assert {c["product_id"] for c in entry.undo_payload_json["changes"]} == set(wanted)


async def test_push_into_order_being_edited_never_lowers(
    sessions: sessionmaker[Session], household: Household
) -> None:
    order = FakeOrder(state="REOPENED", items={MILK: 9})
    with sessions.begin() as s:
        seed(s, household.id, order)
        await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
    assert "reopen" not in order.calls  # someone is editing: just add, like the app
    assert order.items[MILK] == 9  # the person's 9 stays
    assert order.items[BREAD] >= 1 and order.items[RARE] == 1


async def test_second_push_has_nothing_left(
    sessions: sessionmaker[Session], household: Household
) -> None:
    order = FakeOrder()
    with sessions.begin() as s:
        seed(s, household.id, order)
        await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
        writes = order.writes
        with pytest.raises(PushError, match="niets"):
            await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
    assert order.writes == writes


async def test_amount_limit_refuses_before_any_write(
    sessions: sessionmaker[Session], household: Household
) -> None:
    order = FakeOrder()
    with sessions.begin() as s:
        seed(s, household.id, order, settings={"max_push_amount": 3})
        with pytest.raises(PushError, match="maximum"):
            await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
    assert "reopen" not in order.calls and order.writes == 0 and order.state == "CONFIRMED"


async def test_too_close_to_cutoff(sessions: sessionmaker[Session], household: Household) -> None:
    order = FakeOrder(cutoff=NOW + timedelta(minutes=10))
    with sessions.begin() as s:
        seed(s, household.id, order)
        with pytest.raises(PushError, match="sluit"):
            await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
    assert order.writes == 0


async def test_other_delivery_is_refused(
    sessions: sessionmaker[Session], household: Household
) -> None:
    order = FakeOrder()
    with sessions.begin() as s:
        seed(s, household.id, order)
    order.order_id = 777  # the upcoming order changed since the draft
    with sessions.begin() as s, pytest.raises(PushError, match="Opnieuw berekenen"):
        await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
    assert order.writes == 0


async def test_product_ah_does_not_take_is_reported(
    sessions: sessionmaker[Session], household: Household
) -> None:
    order = FakeOrder(refuse={RARE})
    with sessions.begin() as s:
        seed(s, household.id, order)
        result = await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
    assert result.not_taken == ["Saffraan"]


async def test_undo_reverts_ours_and_leaves_changed_products(
    sessions: sessionmaker[Session], household: Household
) -> None:
    order = FakeOrder(items={MILK: 1})
    with sessions.begin() as s:
        seed(s, household.id, order)
        result = await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
    order.items[BREAD] = 5  # the person changed bread in the AH app afterwards
    with sessions.begin() as s:
        assert result.action_id is not None
        undo = await undo_push(
            order, s, household_id=household.id, action_id=result.action_id, now=NOW
        )
    assert order.items.get(MILK) == 1  # back to what the person had
    assert RARE not in order.items
    assert order.items[BREAD] == 5
    assert undo.left_alone == ["AH Volkoren brood"]
    with sessions() as s:
        plan = s.scalars(select(Plan)).one()
        assert plan.status == "draft" and not any(line.applied for line in plan.lines)
        assert push_actions(s, household.id, plan.id) == []
    with sessions.begin() as s, pytest.raises(PushError, match="al teruggedraaid"):
        await undo_push(order, s, household_id=household.id, action_id=result.action_id, now=NOW)


async def test_undo_of_other_household_is_refused(
    sessions: sessionmaker[Session], household: Household
) -> None:
    order = FakeOrder()
    with sessions.begin() as s:
        seed(s, household.id, order)
        result = await push_plan(order, s, household_id=household.id, actor="user:x", now=NOW)
        other = Household(name="Buren", settings_json={})
        s.add(other)
        s.flush()
        assert result.action_id is not None
        with pytest.raises(PushError, match="bestaat niet"):
            await undo_push(order, s, household_id=other.id, action_id=result.action_id, now=NOW)


def test_in_window() -> None:
    cutoff = NOW + timedelta(hours=10)
    assert in_window(cutoff, NOW, hours_before=24)
    assert not in_window(cutoff, NOW, hours_before=5)
    assert not in_window(NOW + timedelta(minutes=5), NOW, hours_before=24)
    assert in_window(cutoff.replace(tzinfo=None), NOW, hours_before=24)


async def test_autopilot_pushes_only_sure_lines_and_notifies(
    sessions: sessionmaker[Session], household: Household, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = FakeOrder(cutoff=NOW + timedelta(hours=10))
    with sessions.begin() as s:
        seed(s, household.id, order, settings={"autopilot_enabled": True})
        s.add(AhAccount(household_id=household.id, label="AH", tokens_enc=b"x"))

    class _Ctx:
        async def __aenter__(self) -> FakeOrder:
            return order

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr("app.services.autopilot.client_for", lambda *a, **k: _Ctx())
    notifier = CollectingNotifier()
    report = await run_autopilot(
        sessions,
        settings=Settings(base_url="http://nas:8085"),
        cipher=None,
        notifier=notifier,
        now=NOW,
    )  # type: ignore[arg-type]
    assert report and set(order.items) == {MILK, BREAD}  # not the pinned 'propose' line
    [msg] = notifier.sent
    assert "2 producten" in msg.body and "1 voorstellen wachten" in msg.body
    assert msg.url == "http://nas:8085/week"
    with sessions() as s:
        lines = {line.ah_product_id: line.applied for line in s.scalars(select(PlanLine))}
        assert lines == {MILK: True, BREAD: True, RARE: False}

    # second pass: nothing auto left, no new notification
    await run_autopilot(
        sessions,
        settings=Settings(),
        cipher=None,
        notifier=notifier,  # type: ignore[arg-type]
        now=NOW,
    )
    assert len(notifier.sent) == 1


async def test_autopilot_off_or_outside_window_does_nothing(
    sessions: sessionmaker[Session], household: Household, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = FakeOrder(cutoff=NOW + timedelta(hours=40))
    with sessions.begin() as s:
        seed(
            s,
            household.id,
            order,
            settings={"autopilot_enabled": True, "autopilot_hours_before": 24},
        )
        s.add(AhAccount(household_id=household.id, label="AH", tokens_enc=b"x"))
    monkeypatch.setattr(
        "app.services.autopilot.client_for",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AH call")),
    )
    notifier = CollectingNotifier()
    assert (
        await run_autopilot(
            sessions,
            settings=Settings(),
            cipher=None,  # type: ignore[arg-type]
            notifier=notifier,
            now=NOW,
        )
        == []
    )
    with sessions.begin() as s:
        h = s.get(Household, household.id)
        assert h is not None
        h.settings_json = {"autopilot_enabled": False}
    assert (
        await run_autopilot(
            sessions,
            settings=Settings(),
            cipher=None,  # type: ignore[arg-type]
            notifier=notifier,
            now=NOW + timedelta(hours=30),
        )
        == []
    )
    assert order.calls == [] and notifier.sent == []
