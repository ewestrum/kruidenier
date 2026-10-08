"""Compare each past draft with the delivered order (read model + notification)."""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ActionLog, FamilyMember, Plan, ProductFamily, Purchase
from app.domain.overlap import Overlap, compare, ready_for_autopilot

KNOWN_MIN_PURCHASES = 3
KNOWN_WINDOW_DAYS = 180
NOTIFIED = "evaluation.notified"


@dataclass(frozen=True)
class Evaluation:
    plan_id: int
    delivery_date: date
    overlap: Overlap
    names: dict[int, str]  # family id -> name

    @property
    def score(self) -> float | None:
        return self.overlap.score

    def titled(self, ids: frozenset[int]) -> list[str]:
        return sorted(self.names.get(i, "?") for i in ids)


def _family_of(session: Session, household_id: int) -> dict[int, int]:
    return {
        m.ah_product_id: m.family_id
        for m in session.scalars(
            select(FamilyMember)
            .join(ProductFamily)
            .where(ProductFamily.household_id == household_id)
        )
    }


def evaluate(session: Session, plan: Plan) -> Evaluation | None:
    """None until the order of this plan has been delivered and imported."""
    if plan.ah_order_id is None:
        return None
    family_of = _family_of(session, plan.household_id)
    delivered_rows = session.scalars(
        select(Purchase).where(
            Purchase.household_id == plan.household_id,
            Purchase.ah_order_id == plan.ah_order_id,
        )
    ).all()
    if not delivered_rows:
        return None
    delivered = {family_of[p.ah_product_id] for p in delivered_rows if p.ah_product_id in family_of}

    since = plan.delivery_date - timedelta(days=KNOWN_WINDOW_DAYS)
    counts: dict[int, int] = {}
    for p in session.scalars(
        select(Purchase).where(
            Purchase.household_id == plan.household_id,
            Purchase.delivered_at < plan.delivery_date,
            Purchase.delivered_at >= since,
        )
    ):
        fam = family_of.get(p.ah_product_id)
        if fam is not None:
            counts[fam] = counts.get(fam, 0) + 1
    families = {
        f.id: f
        for f in session.scalars(
            select(ProductFamily).where(ProductFamily.household_id == plan.household_id)
        )
    }
    known = {
        fid
        for fid, n in counts.items()
        if n >= KNOWN_MIN_PURCHASES and fid in families and not families[fid].excluded
    }
    planned = {line.family_id for line in plan.lines}
    overlap = compare(planned, delivered, known)
    return Evaluation(
        plan.id, plan.delivery_date, overlap, {i: f.name for i, f in families.items()}
    )


def recent_evaluations(
    session: Session, household_id: int, *, today: date, limit: int = 6
) -> list[Evaluation]:
    """Newest first; only deliveries that have been imported."""
    plans = session.scalars(
        select(Plan)
        .where(Plan.household_id == household_id, Plan.delivery_date <= today)
        .order_by(Plan.delivery_date.desc())
        .limit(limit * 2)
    ).all()
    out = [e for e in (evaluate(session, p) for p in plans) if e is not None]
    return out[:limit]


def autopilot_ready(evaluations: list[Evaluation]) -> bool:
    return ready_for_autopilot([e.score for e in evaluations])


def summary(e: Evaluation) -> str:
    if e.score is None:
        return "Nog niets te vergelijken."
    text = (
        f"{round(e.score * 100)}% raak: {len(e.overlap.hits)} goed, "
        f"{len(e.overlap.missed)} gemist, {len(e.overlap.extra)} te veel."
    )
    if e.overlap.missed:
        text += f" Gemist: {', '.join(e.titled(e.overlap.missed)[:5])}."
    return text


def unnotified(session: Session, household_id: int, *, today: date) -> list[Evaluation]:
    """Evaluations not yet announced; marks them as announced."""
    done = {
        a.payload_json.get("plan_id")
        for a in session.scalars(
            select(ActionLog).where(
                ActionLog.household_id == household_id, ActionLog.action == NOTIFIED
            )
        )
    }
    fresh = [
        e
        for e in recent_evaluations(session, household_id, today=today, limit=2)
        if e.plan_id not in done
    ]
    for e in fresh:
        session.add(
            ActionLog(
                household_id=household_id,
                action=NOTIFIED,
                payload_json={"plan_id": e.plan_id, "score": e.score},
                undo_payload_json={},
            )
        )
    session.flush()
    return fresh
