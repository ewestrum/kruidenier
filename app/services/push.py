"""Put the draft into the real AH order, and undo that (SPEC fase 2).

Safety rules, learned in fase 0 (docs/ah-api.md question 8):
- never call orderRevert: it discards everyone's unsaved edits;
- only reopen an order that is CONFIRMED; a REOPENED order is being edited (by a person in
  the AH app, or by us earlier) and we just add to it, like the app does;
- only raise quantities, never lower what is already in the order;
- refuse close to the cutoff, when the delivery changed, or above the amount limit;
- every write gets an action_log row with an undo payload.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ah.models import ActiveOrderSummary, LineItem, OrderDetails, UpcomingOrder
from app.db.models import ActionLog, Household, PlanLine, PriceObservation, Product
from app.db.models import Plan as PlanRow
from app.domain.order_changes import Change, changes_to_apply, changes_to_undo
from app.services.week import latest_plan

log = logging.getLogger(__name__)

CUTOFF_MARGIN = timedelta(minutes=15)
DEFAULT_MAX_AMOUNT = 150.0
PUSH = "ah.push"
UNDO = "ah.push.undo"


class OrderWriter(Protocol):
    async def get_upcoming_order(self, *, today: date | None = None) -> UpcomingOrder | None: ...
    async def get_order(self, order_id: int) -> OrderDetails: ...
    async def reopen_order(self, order_id: int) -> None: ...
    async def add_to_order(
        self, order_id: int, items: list[LineItem]
    ) -> ActiveOrderSummary | None: ...


class PushError(Exception):
    """Refused or failed; the message is shown to the user as-is (Dutch)."""


@dataclass
class PushResult:
    action_id: int | None
    order_id: int
    changed: list[str] = field(default_factory=list)  # human-readable lines
    not_taken: list[str] = field(default_factory=list)
    amount: float = 0.0
    lines_marked: int = 0


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _quantities(lines: list[Any]) -> dict[int, int]:
    out: dict[int, int] = {}
    for line in lines:
        pid = line.product.webshop_id
        out[pid] = out.get(pid, 0) + line.quantity
    return out


def _price(session: Session, product_id: int) -> float:
    obs = session.scalar(
        select(PriceObservation)
        .where(PriceObservation.ah_product_id == product_id)
        .order_by(PriceObservation.observed_on.desc())
        .limit(1)
    )
    return float(obs.price) if obs and obs.price is not None else 0.0


def _title(session: Session, product_id: int) -> str:
    product = session.get(Product, product_id)
    return product.title if product else f"product {product_id}"


def max_amount(household: Household) -> float:
    return float((household.settings_json or {}).get("max_push_amount", DEFAULT_MAX_AMOUNT))


async def _open_for_editing(client: OrderWriter, order_id: int) -> tuple[OrderDetails, bool]:
    """Make sure the order is REOPENED. Returns the fresh details and whether we reopened it."""
    details = await client.get_order(order_id)
    if details.order_state == "REOPENED":
        return details, False
    if details.order_state != "CONFIRMED":
        raise PushError(f"Deze AH-bestelling heeft status {details.order_state} en kan niet meer.")
    if details.reopenable is False:
        raise PushError("AH staat niet toe dat deze bestelling nog gewijzigd wordt.")
    await client.reopen_order(order_id)
    return await client.get_order(order_id), True


async def _check_order(
    client: OrderWriter, plan: PlanRow, order_id: int | None, now: datetime
) -> UpcomingOrder:
    upcoming = await client.get_upcoming_order(today=now.date())
    if upcoming is None or order_id is None or upcoming.order_id != order_id:
        raise PushError(
            "De eerstvolgende AH-levering is niet meer die van dit voorstel. "
            "Kies 'Opnieuw berekenen' en probeer het dan nog eens."
        )
    cutoff = upcoming.cutoff or plan.cutoff
    if cutoff is None:
        raise PushError("Onbekend tot wanneer deze bestelling kan worden aangepast; niets gedaan.")
    if now >= _utc(cutoff) - CUTOFF_MARGIN:
        raise PushError(
            "Deze bestelling sluit binnen een kwartier of is al gesloten; niets gedaan."
        )
    return upcoming


async def push_plan(
    client: OrderWriter,
    session: Session,
    *,
    household_id: int,
    actor: str,
    now: datetime,
    only_tier: str | None = None,
) -> PushResult:
    """Send the not-yet-applied lines of the latest plan to the AH order."""
    household = session.get(Household, household_id)
    plan = latest_plan(session, household_id)
    if household is None or plan is None:
        raise PushError("Er is nog geen voorstel om te versturen.")
    lines: list[PlanLine] = [
        line
        for line in plan.lines
        if not line.applied and (only_tier is None or line.tier == only_tier)
    ]
    if not lines:
        raise PushError("Er staat niets (meer) in het voorstel om te versturen.")

    order_id = plan.ah_order_id
    await _check_order(client, plan, order_id, now)
    assert order_id is not None

    wanted: dict[int, int] = {}
    for line in lines:
        wanted[line.ah_product_id] = max(wanted.get(line.ah_product_id, 0), line.qty)

    # Read first (no AH write yet) so the amount limit is checked before reopening.
    current = _quantities((await client.get_order(order_id)).lines())
    changes = changes_to_apply(current, wanted)
    amount = round(sum((c.after - c.before) * _price(session, c.product_id) for c in changes), 2)
    limit = max_amount(household)
    if amount > limit:
        raise PushError(
            f"Dit zou ongeveer € {amount:.2f} toevoegen, meer dan het maximum van € {limit:.0f} "
            "per keer (Instellingen). Er is niets verstuurd."
        )

    result = PushResult(action_id=None, order_id=order_id, amount=amount)
    reopened = False
    if changes:
        details, reopened = await _open_for_editing(client, order_id)
        current = _quantities(details.lines())
        changes = changes_to_apply(current, wanted)  # recheck on the fresh state
    if changes:
        summary = await client.add_to_order(
            order_id, [LineItem(product_id=c.product_id, quantity=c.after) for c in changes]
        )
        after = (
            _quantities(summary.ordered_products)
            if summary is not None
            else _quantities((await client.get_order(order_id)).lines())
        )
        for c in changes:
            title = _title(session, c.product_id)
            if after.get(c.product_id, 0) >= c.after:
                result.changed.append(f"{title}: {c.before} → {c.after}")
            else:
                result.not_taken.append(title)

    entry = ActionLog(
        household_id=household_id,
        at=now,
        action=PUSH,
        payload_json={
            "plan_id": plan.id,
            "order_id": order_id,
            "actor": actor,
            "reopened": reopened,
            "amount": amount,
            "changed": result.changed,
            "not_taken": result.not_taken,
            "line_ids": [line.id for line in lines],
        },
        undo_payload_json={
            "order_id": order_id,
            "changes": [
                {"product_id": c.product_id, "before": c.before, "after": c.after} for c in changes
            ],
        },
    )
    session.add(entry)
    for line in lines:
        line.applied = True
    plan.status = "applied"
    session.flush()
    result.action_id = entry.id
    result.lines_marked = len(lines)
    log.info("pushed plan %s to order %s by %s: %s", plan.id, order_id, actor, result.changed)
    return result


@dataclass
class UndoResult:
    reverted: list[str] = field(default_factory=list)
    left_alone: list[str] = field(default_factory=list)


def push_actions(session: Session, household_id: int, plan_id: int) -> list[ActionLog]:
    """Pushes for this plan that have not been undone, newest first."""
    rows = session.scalars(
        select(ActionLog)
        .where(ActionLog.household_id == household_id, ActionLog.action == PUSH)
        .order_by(ActionLog.at.desc(), ActionLog.id.desc())
    ).all()
    return [
        r
        for r in rows
        if r.payload_json.get("plan_id") == plan_id and not r.payload_json.get("undone_at")
    ]


async def undo_push(
    client: OrderWriter, session: Session, *, household_id: int, action_id: int, now: datetime
) -> UndoResult:
    entry = session.get(ActionLog, action_id)
    if entry is None or entry.household_id != household_id or entry.action != PUSH:
        raise PushError("Deze actie bestaat niet (meer).")
    if entry.payload_json.get("undone_at"):
        raise PushError("Deze actie is al teruggedraaid.")
    plan = session.get(PlanRow, entry.payload_json.get("plan_id"))
    order_id = int(entry.undo_payload_json["order_id"])
    if plan is not None:
        await _check_order(client, plan, order_id, now)

    applied = [Change(**c) for c in entry.undo_payload_json.get("changes", [])]
    current = _quantities((await client.get_order(order_id)).lines())
    undo = changes_to_undo(applied, current)
    result = UndoResult(left_alone=[_title(session, pid) for pid in undo.untouched])
    if undo.revert:
        await _open_for_editing(client, order_id)
        await client.add_to_order(
            order_id, [LineItem(product_id=c.product_id, quantity=c.after) for c in undo.revert]
        )
        result.reverted = [
            f"{_title(session, c.product_id)}: {c.before} → {c.after}" for c in undo.revert
        ]

    entry.payload_json = {**entry.payload_json, "undone_at": now.isoformat()}
    session.add(
        ActionLog(
            household_id=household_id,
            at=now,
            action=UNDO,
            payload_json={
                "push_id": entry.id,
                "reverted": result.reverted,
                "left_alone": result.left_alone,
            },
            undo_payload_json={},
        )
    )
    if plan is not None:
        line_ids = set(entry.payload_json.get("line_ids", []))
        for line in plan.lines:
            if line.id in line_ids:
                line.applied = False
        if not any(line.applied for line in plan.lines):
            plan.status = "draft"
    session.flush()
    return result
