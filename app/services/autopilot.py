"""Autopilot (SPEC §9): before the cutoff, put the sure lines (tier `auto`) in the AH order.

Off by default; a household admin switches it on in Instellingen. No LLM anywhere here.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ah.errors import AhError
from app.ah.token_crypto import TokenCipher
from app.config import Settings
from app.db.models import Household
from app.domain.tiers import Tier
from app.services.accounts import client_for, order_account
from app.services.notify import Message, NotifierFor
from app.services.push import CUTOFF_MARGIN, PushError, push_plan
from app.services.sync import report_ah_failure
from app.services.week import latest_plan

log = logging.getLogger(__name__)

DEFAULT_HOURS_BEFORE = 24


@dataclass(frozen=True)
class AutopilotSettings:
    enabled: bool
    hours_before: float


def autopilot_settings(household: Household) -> AutopilotSettings:
    s: dict[str, Any] = household.settings_json or {}
    return AutopilotSettings(
        enabled=bool(s.get("autopilot_enabled", False)),
        hours_before=float(s.get("autopilot_hours_before", DEFAULT_HOURS_BEFORE)),
    )


def in_window(cutoff: datetime, now: datetime, hours_before: float) -> bool:
    cutoff = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)
    return cutoff - timedelta(hours=hours_before) <= now < cutoff - CUTOFF_MARGIN


async def run_autopilot(
    sessions: sessionmaker[Session],
    *,
    settings: Settings,
    cipher: TokenCipher,
    notifiers: NotifierFor,
    now: datetime,
) -> list[str]:
    """One pass over all households. Returns a short report per household that acted."""
    report: list[str] = []
    with sessions() as s:
        households = [h.id for h in s.scalars(select(Household)) if autopilot_settings(h).enabled]
    for household_id in households:
        with sessions() as s:
            household = s.get(Household, household_id)
            plan = latest_plan(s, household_id)
            account = order_account(s, household_id)
            if household is None or plan is None or account is None or plan.cutoff is None:
                continue
            if not in_window(plan.cutoff, now, autopilot_settings(household).hours_before):
                continue
            if not any(line.tier == Tier.AUTO.value and not line.applied for line in plan.lines):
                continue
        try:
            async with client_for(
                account, settings=settings, cipher=cipher, sessions=sessions
            ) as client:
                with sessions.begin() as s:
                    result = await push_plan(
                        client,
                        s,
                        household_id=household_id,
                        actor="autopilot",
                        now=now,
                        only_tier=Tier.AUTO.value,
                    )
                    current = latest_plan(s, household_id)
                    waiting = sum(1 for ln in current.lines if not ln.applied) if current else 0
        except PushError as e:
            log.info("autopilot household %s: %s", household_id, e)
            continue
        except AhError as e:
            report.append(await report_ah_failure(notifiers(household_id), account, e))
            continue
        body = (
            f"{len(result.changed)} producten in je AH-bestelling gezet "
            f"(ongeveer € {result.amount:.2f})."
        )
        if result.not_taken:
            body += f" Niet gelukt: {', '.join(result.not_taken)}."
        if waiting:
            body += f" {waiting} voorstellen wachten op jou."
        await notifiers(household_id).send(
            Message(
                "Kruidenier: bestelling aangevuld",
                body,
                url=f"{settings.base_url.rstrip('/')}/week",
            )
        )
        report.append(f"huishouden {household_id}: {body}")
    return report
