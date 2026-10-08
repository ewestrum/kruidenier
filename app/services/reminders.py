"""Reminder before the AH cutoff when draft lines are not in the order yet (SPEC §9).

One reminder per draft, `reminder_hours` before the cutoff (0 switches it off). Only sent
when the household has a notification channel; nothing touches AH here.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import ActionLog, Household
from app.services.notify import Message, NotifierFor
from app.services.planner import WEEKDAYS_NL
from app.services.week import latest_plan

log = logging.getLogger(__name__)

DEFAULT_REMINDER_HOURS = 3
REMINDED = "reminder.sent"


def reminder_hours(household: Household) -> float:
    s: dict[str, Any] = household.settings_json or {}
    return float(s.get("reminder_hours", DEFAULT_REMINDER_HOURS))


def _already_reminded(session: Session, household_id: int, plan_id: int) -> bool:
    return any(
        a.payload_json.get("plan_id") == plan_id
        for a in session.scalars(
            select(ActionLog).where(
                ActionLog.household_id == household_id, ActionLog.action == REMINDED
            )
        )
    )


async def run_reminders(
    sessions: sessionmaker[Session], *, settings: Settings, notifiers: NotifierFor, now: datetime
) -> list[str]:
    sent: list[str] = []
    with sessions() as s:
        households = [(h.id, reminder_hours(h)) for h in s.scalars(select(Household))]
    for household_id, hours in households:
        if hours <= 0:
            continue
        with sessions.begin() as s:
            plan = latest_plan(s, household_id)
            if plan is None or plan.cutoff is None:
                continue
            cutoff = plan.cutoff if plan.cutoff.tzinfo else plan.cutoff.replace(tzinfo=UTC)
            if not cutoff - timedelta(hours=hours) <= now < cutoff:
                continue
            waiting = sum(1 for line in plan.lines if not line.applied)
            if not waiting or _already_reminded(s, household_id, plan.id):
                continue
            local = cutoff.astimezone(ZoneInfo(settings.tz))
            day = plan.delivery_date
            body = (
                f"Je AH-bestelling voor {WEEKDAYS_NL[day.weekday()]} {day.day}-{day.month} "
                f"sluit om {local:%H:%M}. "
                f"{waiting} {'product' if waiting == 1 else 'producten'} uit het voorstel "
                f"{'staat' if waiting == 1 else 'staan'} er nog niet in."
            )
            message = Message(
                "Kruidenier: bestelling sluit bijna",
                body,
                url=f"{settings.base_url.rstrip('/')}/week",
            )
            if await notifiers(household_id).send(message):
                s.add(
                    ActionLog(
                        household_id=household_id,
                        at=now,
                        action=REMINDED,
                        payload_json={"plan_id": plan.id, "waiting": waiting},
                        undo_payload_json={},
                    )
                )
                sent.append(f"huishouden {household_id}: {body}")
    return sent
