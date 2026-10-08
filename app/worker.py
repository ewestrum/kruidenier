"""Scheduler entrypoint (`python -m app.worker`).

Jobs: heartbeat every minute, prices daily, history/families/stats/draft plan daily.
On start it catches up on today's price log, because a NAS reboot must not leave a gap
in the price history (SPEC §7).
"""

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from app.ah.token_crypto import TokenCipher
from app.config import get_settings
from app.db.models import PriceObservation, WorkerHeartbeat
from app.db.session import session_factory
from app.services.autopilot import run_autopilot
from app.services.notify import WebhookNotifier
from app.services.sync import daily_prices, daily_sync

log = logging.getLogger("kruidenier.worker")


def beat(detail: str = "") -> None:
    with session_factory().begin() as s:
        row = s.get(WorkerHeartbeat, "worker") or WorkerHeartbeat(name="worker")
        row.beat_at = datetime.now(UTC)
        if detail:
            row.detail = detail
        s.add(row)


async def prices_job() -> None:
    settings = get_settings()
    n = await daily_prices(
        session_factory(),
        settings=settings,
        cipher=TokenCipher(settings.fernet_key),
        notifier=WebhookNotifier(settings),
        today=date.today(),
    )
    beat(f"prices: {n} op {date.today()}")


async def sync_job() -> None:
    settings = get_settings()
    report = await daily_sync(
        session_factory(),
        settings=settings,
        cipher=TokenCipher(settings.fernet_key),
        notifier=WebhookNotifier(settings),
        today=date.today(),
    )
    beat(f"sync: {report.orders_imported} orders, {'; '.join(report.plans)}")
    log.info("sync done: %s", report)


async def autopilot_job() -> None:
    """Every 30 minutes; only acts for households that switched it on, inside their window."""
    settings = get_settings()
    report = await run_autopilot(
        session_factory(),
        settings=settings,
        cipher=TokenCipher(settings.fernet_key),
        notifier=WebhookNotifier(settings),
        now=datetime.now(UTC),
    )
    if report:
        beat(f"autopilot: {'; '.join(report)}"[:500])


def prices_logged_today() -> bool:
    with session_factory()() as s:
        n = s.scalar(select(func.count()).where(PriceObservation.observed_on == date.today()))
    return bool(n)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    TokenCipher(settings.fernet_key)  # fail fast on a missing key

    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(beat, "interval", minutes=1, id="heartbeat", next_run_time=datetime.now(UTC))
    scheduler.add_job(
        prices_job, CronTrigger(hour=6, minute=15), id="prices", coalesce=True, max_instances=1
    )
    scheduler.add_job(
        sync_job, CronTrigger(hour=6, minute=45), id="sync", coalesce=True, max_instances=1
    )
    scheduler.add_job(
        autopilot_job, "interval", minutes=30, id="autopilot", coalesce=True, max_instances=1
    )
    scheduler.start()
    log.info("worker started (tz=%s)", settings.tz)

    if not prices_logged_today():
        log.info("no prices logged today yet: catching up")
        scheduler.add_job(prices_job, id="prices-catchup")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows dev
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
