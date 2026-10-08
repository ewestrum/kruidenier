"""Fase-0 spike: smoke test against the REAL AH API and record fixtures.

HANDMATIG — alleen een mens draait dit (CLAUDE.md rule 2). Usage:

    uv run python scripts/probe.py                    # read-only probes
    uv run python scripts/probe.py --write-test 12345 # + reopen/edit/restore/revert test

Raw responses go to .probe-raw/ (gitignored). Scrubbed copies go to
tests/fixtures/ah/ and are meant to be reviewed and committed.
Never calls checkout/payment: the adapter allowlist makes that impossible.
"""

import argparse
import asyncio
import json
import re
import sys
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from app.ah.client import HttpAhClient
from app.ah.errors import AhHttpError
from app.ah.models import LineItem, Tokens
from app.ah.ratelimit import RateLimiter
from app.ah.token_crypto import TokenCipher
from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / ".probe-raw"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ah"
TOKEN_FILE = ROOT / ".probe-tokens"

SENSITIVE_KEY = re.compile(
    r"(token|address|street|house|zip|postal|city|name$|firstname|lastname|email|phone|"
    r"member|^iban$|hash|customer|card|loyalty|birth|invoice)",
    re.IGNORECASE,
)
# Keys that match the pattern above but are product data, not personal data.
KEEP_KEYS = {"taxonomyName", "brandName", "productName", "segmentDescription", "shopName"}


def scrub(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if key and key not in KEEP_KEYS and SENSITIVE_KEY.search(key) and value is not None:
        return "REDACTED"
    return value


class Recorder:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        RAW_DIR.mkdir(exist_ok=True)
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    def __call__(self, name: str, data: Any) -> None:
        self.counts[name] += 1
        n = self.counts[name]
        stem = name if n == 1 else f"{name}.{n}"
        (RAW_DIR / f"{stem}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if not name.startswith("auth."):
            (FIXTURE_DIR / f"{stem}.json").write_text(
                json.dumps(scrub(data), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )


results: list[tuple[str, str, str]] = []


async def step(label: str, fn: Callable[[], Awaitable[Any]]) -> Any:
    try:
        value = await fn()
    except NotImplementedError as e:
        results.append((label, "SKIP", str(e)))
        return None
    except Exception as e:
        results.append((label, "FAIL", f"{type(e).__name__}: {e}"[:400]))
        traceback.print_exc(limit=1)
        return None
    summary = repr(value)
    results.append((label, "OK", summary[:200]))
    return value


def extract_code(text: str) -> str:
    m = re.search(r"[?&]code=([^&\s]+)", text)
    return m.group(1) if m else text.strip()


async def load_tokens(client: HttpAhClient, cipher: TokenCipher | None) -> Tokens:
    if cipher and TOKEN_FILE.exists():
        tokens = cipher.decrypt(TOKEN_FILE.read_bytes())
        print("Bestaande tokens gevonden in .probe-tokens.")
        return tokens
    url = await client.login_url()
    print("\n1. Open deze URL in een browser met DevTools (Network-tab) open:\n")
    print(f"   {url}\n")
    print("2. Log in. De browser probeert daarna 'appie://login-exit?code=...' te openen.")
    print("3. Kopieer die URL (of alleen de code) uit de Network-tab en plak hem hier.\n")
    for attempt in range(3):
        code = extract_code(await asyncio.to_thread(input, "code of redirect-URL: "))
        print(f"(code ontvangen: {code[:8]}…, lengte {len(code)})")
        try:
            return await client.exchange_code(code)
        except AhHttpError as e:
            print(f"Inwisselen mislukt: {e}")
            if attempt < 2:
                print("Een code is maar één keer en kort geldig. Log opnieuw in via de URL")
                print("hierboven (of ververs die pagina) en plak de nieuwe code.\n")
    raise SystemExit("Inloggen niet gelukt.")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--write-test",
        type=int,
        metavar="PRODUCT_ID",
        help="heropen de open order, zet PRODUCT_ID +1, herstel, en revert",
    )
    ap.add_argument(
        "--revert-test",
        type=int,
        metavar="PRODUCT_ID",
        help="heropen, zet PRODUCT_ID +1 en revert ZONDER terugzetten: blijft het product staan?",
    )
    ap.add_argument(
        "--receipts",
        action="store_true",
        help="kassabonnen ophalen (alleen lezen) en controleren hoe kassa-ID's te vertalen zijn",
    )
    args = ap.parse_args()

    settings = get_settings()
    cipher = TokenCipher(settings.fernet_key) if settings.fernet_key else None
    if cipher is None:
        print("Let op: FERNET_KEY niet gezet; tokens worden niet bewaard tussen runs.")

    async def save_tokens(tokens: Tokens) -> None:
        if cipher:
            TOKEN_FILE.write_bytes(cipher.encrypt(tokens))

    recorder = Recorder()
    client = HttpAhClient(
        client_id=settings.ah_client_id,
        client_version=settings.ah_client_version,
        limiter=RateLimiter(max(settings.ah_min_request_interval, 1.0)),
        on_tokens_refreshed=save_tokens,
        on_raw_response=recorder,
    )

    async with client:
        tokens = await load_tokens(client, cipher)
        client.use_tokens(tokens)
        await save_tokens(tokens)

        await step("bonus.metadata", client.get_bonus_metadata)
        found = await step("product.search", lambda: client.search_products("halfvolle melk"))
        if found:
            pid = found[0].webshop_id
            await step("product.detail", lambda: client.get_product(pid))
            await step(
                "product.by_ids", lambda: client.get_products([p.webshop_id for p in found[:3]])
            )

        upcoming = await step("graphql.OrderFulfillments", client.get_upcoming_order)
        await step("graphql.OrderFulfillmentsAll", lambda: client._graphql("OrderFulfillmentsAll"))
        history = await step("list_orders", lambda: client.list_orders(date(2000, 1, 1)))
        active = await step("order.active_summary", client.get_active_order)
        if active:
            await step("order.details(active)", lambda: client.get_order(active.id))
        if upcoming and (not active or upcoming.order_id != active.id):
            await step("order.details(upcoming)", lambda: client.get_order(upcoming.order_id))
        # Past deliveries are the source of the consumption model (fase 1).
        for past in (history or [])[:2]:
            await step(
                f"order.details(delivered {past.delivery_date})",
                lambda past=past: client.get_order(past.order_id),
            )

        if args.write_test:
            await write_test(client, upcoming, args.write_test)
        if args.revert_test:
            await revert_test(client, upcoming, args.revert_test)
        if args.receipts:
            await receipts_probe(client)

    print("\n=== Resultaat ===")
    for label, status, detail in results:
        print(f"[{status:4}] {label}: {detail}")
    print(f"\nRuwe responses: {RAW_DIR}\nFixtures (gescrubd, eerst nalezen!): {FIXTURE_DIR}")
    print("Werk docs/ah-api.md bij met wat je hierboven ziet.")
    return 0 if all(s != "FAIL" for _, s, _ in results) else 1


async def write_test(client: HttpAhClient, upcoming: Any, product_id: int) -> None:
    if upcoming is None:
        results.append(("write-test", "SKIP", "geen open, wijzigbare order gevonden"))
        return
    order_id = upcoming.order_id
    if not await order_is_untouched(client, order_id, "write-test"):
        return
    print(f"\nWRITE-TEST op order {order_id} (levering {upcoming.delivery_date}).")
    print("Stappen: heropenen -> product +1 -> terugzetten -> revert. Er wordt NIET afgerekend.")
    if (await asyncio.to_thread(input, "Typ JA om door te gaan: ")).strip() != "JA":
        results.append(("write-test", "SKIP", "niet bevestigd"))
        return

    await step("graphql.OrderReopen", lambda: client.reopen_order(order_id))
    if results[-1][1] != "OK":
        return
    try:
        before = await step("order.details(reopened)", lambda: client.get_order(order_id))
        if before is None:
            return
        current = next(
            (ln.quantity for ln in before.lines() if ln.product.webshop_id == product_id), 0
        )
        await step(
            "order.set_items(+1)",
            lambda: client.add_to_order(
                order_id, [LineItem(product_id=product_id, quantity=current + 1)]
            ),
        )
        after = await step("order.details(after +1)", lambda: client.get_order(order_id))
        if after:
            new = next(
                (ln.quantity for ln in after.lines() if ln.product.webshop_id == product_id), 0
            )
            results.append(
                (
                    "write-test verify",
                    "OK" if new == current + 1 else "FAIL",
                    f"was {current}, nu {new}, verwacht {current + 1}",
                )
            )
        await step(
            "order.set_items(restore)",
            lambda: client.add_to_order(
                order_id, [LineItem(product_id=product_id, quantity=current)]
            ),
        )
    finally:
        await step("graphql.OrderRevert", lambda: client.revert_order(order_id))
    print("Controleer in de AH-app dat de order weer in de oorspronkelijke staat is.")


async def order_is_untouched(client: HttpAhClient, order_id: int, label: str) -> bool:
    """Refuse write tests on an order that is already REOPENED.

    The AH app keeps an order REOPENED while a person edits it, and orderRevert throws
    those unsaved edits away (fase 0: a revert-test wiped 6 items added in the app).
    """
    details = await step(f"{label} precheck", lambda: client.get_order(order_id))
    if details is None:
        return False
    if details.order_state != "CONFIRMED":
        results.append(
            (
                label,
                "SKIP",
                f"order staat op {details.order_state}: iemand is hem aan het wijzigen; "
                "een revert zou die wijzigingen weggooien",
            )
        )
        return False
    return True


async def receipts_probe(client: HttpAhClient) -> None:
    """Read-only check of the store-receipt endpoints (docs/ah-api.md, kassabonnen)."""
    receipts = await step("graphql.PosReceipts", lambda: client.list_receipts(limit=5))
    if not receipts:
        results.append(("kassabonnen", "SKIP", "geen kassabonnen gevonden (bonuskaart gekoppeld?)"))
        return
    details = await step("graphql.PosReceipt", lambda: client.get_receipt(receipts[0].id))
    if details is None:
        return
    for item in [i for i in details.products if i.id is not None][:3]:
        pos_id = item.id
        assert pos_id is not None
        webshop = await step(
            f"convert {item.name}", lambda pos_id=pos_id: client.convert_pos_id(pos_id)
        )
        found = await step(
            f"search {item.name}", lambda name=item.name: client.search_products(name, size=5)
        )
        hq_match = any(p.hq_id == pos_id for p in (found or []))
        results.append(
            (
                f"kassa-ID {pos_id}",
                "OK",
                f"{item.name}: webshop {webshop}, gelijk aan hqId van zoekresultaat: {hq_match}",
            )
        )


def _qty(details: Any, product_id: int) -> int:
    return next((ln.quantity for ln in details.lines() if ln.product.webshop_id == product_id), 0)


async def revert_test(client: HttpAhClient, upcoming: Any, product_id: int) -> None:
    """Answers docs/ah-api.md question 8: does orderRevert keep or discard item changes?"""
    if upcoming is None:
        results.append(("revert-test", "SKIP", "geen komende order gevonden"))
        return
    order_id = upcoming.order_id
    if not await order_is_untouched(client, order_id, "revert-test"):
        return
    print(f"\nREVERT-TEST op order {order_id} (levering {upcoming.delivery_date}).")
    print("Stappen: heropenen -> product +1 -> revert, ZONDER terugzetten.")
    print("Blijft het product staan, haal het dan zelf weg in de AH-app (met de min-knop)")
    print(f"vóór de sluitingstijd ({upcoming.cutoff}). Er wordt NIET afgerekend.")
    if (await asyncio.to_thread(input, "Typ JA om door te gaan: ")).strip() != "JA":
        results.append(("revert-test", "SKIP", "niet bevestigd"))
        return

    await step("graphql.OrderReopen", lambda: client.reopen_order(order_id))
    if results[-1][1] != "OK":
        return
    try:
        before = await step("order.details(reopened)", lambda: client.get_order(order_id))
        if before is None:
            return
        current = _qty(before, product_id)
        await step(
            "order.set_items(+1)",
            lambda: client.add_to_order(
                order_id, [LineItem(product_id=product_id, quantity=current + 1)]
            ),
        )
    finally:
        await step("graphql.OrderRevert", lambda: client.revert_order(order_id))

    after = await step("order.details(after revert)", lambda: client.get_order(order_id))
    if after is not None:
        kept = _qty(after, product_id)
        verdict = (
            "BEHOUDEN: revert = wijzigen afsluiten en opslaan"
            if kept == current + 1
            else "WEGGEGOOID: revert draait itemwijzigingen terug"
            if kept == current
            else f"onverwacht: {kept}"
        )
        results.append(
            ("revert-test", "OK", f"state={after.order_state}, was {current}, nu {kept}: {verdict}")
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
