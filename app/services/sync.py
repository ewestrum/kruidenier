"""Daily jobs: history import, families, stats, draft plan and price logging.

Safe failure (CLAUDE.md rule 4): if AH answers with something that doesn't validate,
or auth is broken, the job stops for that account, logs, and notifies. No actions.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ah.errors import AhAuthError, AhError, AhSchemaError
from app.ah.token_crypto import TokenCipher
from app.config import Settings
from app.db.models import AhAccount, Household
from app.services.accounts import client_for
from app.services.families import assign_families
from app.services.history import import_history
from app.services.notify import Message, Notifier
from app.services.planner import build_draft_plan, recompute_stats
from app.services.prices import log_prices
from app.services.week import reset_carryover_after_purchase

log = logging.getLogger(__name__)

HISTORY_LOOKBACK_DAYS = 400


@dataclass
class SyncReport:
    households: int = 0
    orders_imported: int = 0
    plans: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def report_ah_failure(notifier: Notifier, account: AhAccount, err: AhError) -> str:
    if isinstance(err, AhSchemaError):
        title = "Kruidenier: AH-koppeling kapot"
        body = (
            f"AH gaf een onverwacht antwoord ({err.endpoint}). Er is niets aangepast. "
            "Waarschijnlijk is de AH-app veranderd; de adapter moet bijgewerkt worden."
        )
    elif isinstance(err, AhAuthError):
        title = "Kruidenier: AH-account opnieuw koppelen"
        body = f"De inlog van '{account.label}' werkt niet meer. Koppel het account opnieuw."
    else:
        title = "Kruidenier: AH niet bereikbaar"
        body = f"Synchroniseren van '{account.label}' is mislukt: {err}"[:300]
    await notifier.send(Message(title, body))
    log.error("%s: %s", title, err)
    return f"{account.label}: {type(err).__name__}: {err}"[:300]


async def daily_sync(
    sessions: sessionmaker[Session],
    *,
    settings: Settings,
    cipher: TokenCipher,
    notifier: Notifier,
    today: date,
) -> SyncReport:
    report = SyncReport()
    with sessions() as s:
        household_ids = list(s.scalars(select(Household.id)))
    for household_id in household_ids:
        report.households += 1
        with sessions() as s:
            accounts = list(
                s.scalars(select(AhAccount).where(AhAccount.household_id == household_id))
            )
        upcoming = None
        for account in accounts:
            try:
                async with client_for(
                    account, settings=settings, cipher=cipher, sessions=sessions
                ) as client:
                    with sessions.begin() as s:
                        result = await import_history(
                            client,
                            s,
                            household_id=household_id,
                            since=today - timedelta(days=HISTORY_LOOKBACK_DAYS),
                        )
                    report.orders_imported += len(result.orders_imported)
                    if account.is_order_account:
                        upcoming = await client.get_upcoming_order(today=today)
            except AhError as e:
                report.errors.append(await report_ah_failure(notifier, account, e))

        with sessions.begin() as s:
            household = s.get(Household, household_id)
            assert household is not None
            assign_families(s, household_id)
            recompute_stats(s, household, today=today)
            if reset_carryover_after_purchase(s, household_id):
                recompute_stats(s, household, today=today)
            if upcoming is not None and upcoming.delivery_date is not None:
                plan = build_draft_plan(
                    s,
                    household,
                    delivery_date=upcoming.delivery_date,
                    today=today,
                    ah_order_id=upcoming.order_id,
                    cutoff=upcoming.cutoff,
                )
                report.plans.append(
                    f"{household.name}: {len(plan.lines)} regels voor {plan.delivery_date}"
                )
    return report


async def daily_prices(
    sessions: sessionmaker[Session],
    *,
    settings: Settings,
    cipher: TokenCipher,
    notifier: Notifier,
    today: date,
) -> int:
    """Log prices once, through any working account (prices are not per household)."""
    with sessions() as s:
        accounts = list(s.scalars(select(AhAccount).order_by(AhAccount.id)))
    for account in accounts:
        try:
            async with client_for(
                account, settings=settings, cipher=cipher, sessions=sessions
            ) as client:
                with sessions.begin() as s:
                    result = await log_prices(client, s, on=today)
                log.info("logged %d/%d prices", result.observed, result.requested)
                return result.observed
        except AhError as e:
            await report_ah_failure(notifier, account, e)
    return 0
