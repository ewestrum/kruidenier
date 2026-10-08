"""Admin commands (`python -m app.cli ...`), also usable inside the container.

link-account  koppel een AH-account (plak de code uit de browser)
sync          historie importeren, families, statistiek en concept nu draaien
log-prices    prijzen van vandaag nu loggen
migrate       database-migraties draaien
"""

import argparse
import asyncio
import sys
from datetime import date

from app.ah.client import HttpAhClient
from app.ah.errors import AhHttpError
from app.ah.token_crypto import TokenCipher
from app.config import get_settings
from app.db.migrate import upgrade_head
from app.db.session import session_factory
from app.services.accounts import get_or_create_household, save_account, shared_limiter
from app.services.notify import WebhookNotifier
from app.services.sync import daily_prices, daily_sync


async def link_account(household_name: str, label: str) -> int:
    settings = get_settings()
    cipher = TokenCipher(settings.fernet_key)
    async with HttpAhClient(
        client_id=settings.ah_client_id,
        client_version=settings.ah_client_version,
        limiter=shared_limiter(settings),
    ) as client:
        print(
            "\n1. Open deze URL in een browser met DevTools (F12, Network-tab, 'Preserve log'):\n"
        )
        print(f"   {await client.login_url()}\n")
        print("2. Log in. Zoek daarna in de Network-tab de rode regel 'login-exit?code=...'.")
        print("3. Rechtsklik > Copy > Copy URL en plak hem hier.\n")
        for _ in range(3):
            code = (
                HttpAhClient.extract_code(await asyncio.to_thread(input, "redirect-URL of code: "))
                or ""
            )
            try:
                tokens = await client.exchange_code(code)
                break
            except AhHttpError as e:
                print(f"Mislukt ({e.status_code}). Een code werkt maar één keer; log opnieuw in.")
        else:
            return 1
    with session_factory().begin() as s:
        household = get_or_create_household(s, household_name)
        account = save_account(s, household=household, label=label, tokens=tokens, cipher=cipher)
        print(f"Gekoppeld: '{account.label}' in huishouden '{household.name}'.")
    return 0


async def run_sync() -> int:
    settings = get_settings()
    report = await daily_sync(
        session_factory(),
        settings=settings,
        cipher=TokenCipher(settings.fernet_key),
        notifier=WebhookNotifier(settings),
        today=date.today(),
    )
    print(f"Huishoudens: {report.households}, orders geïmporteerd: {report.orders_imported}")
    for line in report.plans:
        print(f"Concept: {line}")
    for err in report.errors:
        print(f"FOUT: {err}")
    return 1 if report.errors else 0


async def run_prices() -> int:
    settings = get_settings()
    n = await daily_prices(
        session_factory(),
        settings=settings,
        cipher=TokenCipher(settings.fernet_key),
        notifier=WebhookNotifier(settings),
        today=date.today(),
    )
    print(f"Prijzen gelogd: {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kruidenier", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    link = sub.add_parser("link-account")
    link.add_argument("--household", default="Thuis")
    link.add_argument("--label", default="AH")
    sub.add_parser("sync")
    sub.add_parser("log-prices")
    sub.add_parser("migrate")
    args = ap.parse_args(argv)

    if args.cmd == "migrate":
        upgrade_head()
        return 0
    if args.cmd == "link-account":
        return asyncio.run(link_account(args.household, args.label))
    if args.cmd == "sync":
        return asyncio.run(run_sync())
    if args.cmd == "log-prices":
        return asyncio.run(run_prices())
    return 2


if __name__ == "__main__":
    sys.exit(main())
