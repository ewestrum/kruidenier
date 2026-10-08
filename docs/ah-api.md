# AH-API — bevindingen fase 0

Status: **eerste probe gedraaid op 2026-10-07** (alleen lezen, nog geen write-test). De
uitgangspunten komen uit [`gwillem/appie-go`](https://github.com/gwillem/appie-go) (waar
[`ah-mcp`](https://github.com/mrserzhan/ah-mcp) op leunt). Opgenomen, gescrubde responses staan in
`tests/fixtures/ah/` en worden door de contracttests gevalideerd.

## Verbinding

| | |
|---|---|
| API-host | `https://api.ah.nl` |
| Login | `https://login.ah.nl/login?client_id=appie-ios&response_type=code&redirect_uri=appie://login-exit` |
| Client-ID | `appie-ios` (instelbaar via `AH_CLIENT_ID`) |
| Headers | `User-Agent: Appie/9.28`, `x-client-name: appie-ios`, `x-client-version: 9.28`, `x-application: AHWEBSHOP` |
| Auth | `Authorization: Bearer <access_token>` |

**Login-flow:** de browser stuurt na het inloggen door naar `appie://login-exit?code=…`. Een
gewone browser kan dat niet openen; de code staat dan in de DevTools Network-tab. Die code wissel
je in via `POST /mobile-auth/v1/auth/token` met `{"clientId", "code"}`. Het antwoord is snake_case:
`access_token`, `refresh_token`, `expires_in` en `member_id`. Verversen gaat via
`POST /mobile-auth/v1/auth/token/refresh` met `{"clientId", "refreshToken"}`.

> Voor de web-UI (fase 1) moet het koppelen van een account dus een stap "plak hier de redirect-URL"
> krijgen, tenzij de spike een betere route vindt.

## Endpoints (allowlist: `app/ah/allowlist.py`)

| Naam | Methode + pad | Schrijft | Geverifieerd |
|---|---|---|---|
| auth.exchange_code | `POST /mobile-auth/v1/auth/token` | – | ✅ |
| auth.refresh | `POST /mobile-auth/v1/auth/token/refresh` | – | ☐ |
| product.search | `GET /mobile-services/product/search/v2?query&page&size&sortOn=RELEVANCE` | – | ✅ |
| product.by_ids | `GET /mobile-services/product/search/v2/products?ids=…&sortOn=INPUT_PRODUCT_IDS` → **kale lijst** | – | ✅ |
| product.detail | `GET /mobile-services/product/detail/v4/fir/{id}` → `{productId, productCard}` | – | ✅ |
| bonus.metadata | `GET /mobile-services/bonuspage/v3/metadata` | – | ✅ |
| bonus.section | `GET /mobile-services/bonuspage/v2/section?application&date&promotionType&category` | – | ☐ |
| order.active_summary | `GET /mobile-services/order/v1/summaries/active?sortBy=DEFAULT` → **404** als er geen actieve order is | – | ✅ |
| order.details | `GET /mobile-services/order/v1/{id}/details-grouped-by-taxonomy` | – | ✅ |
| order.set_items | `PUT /mobile-services/order/v1/items` | **ja** | ☐ |
| graphql `OrderFulfillments` | `orderFulfillments(status: OPEN)` → komende orders | – | ✅ |
| graphql `OrderFulfillmentsClosed` | `orderFulfillments(status: CLOSED)` → historie (max. 10) | – | ✅ |
| graphql `OrderFulfillmentsAll` | `orderFulfillments` zonder filter → 10 meest recente | – | ✅ |
| graphql `OrderReopen` | `orderReopen(id)` → `{status: "SUCCESS", errorMessage: null}` | **ja** | ✅ |
| graphql `OrderRevert` | `orderRevert(id)` → `{status: "SUCCESS", errorMessage: null}` | **ja** | ✅ |

### Write-test (2026-10-07, eerste poging)

- `orderReopen` op de lege PLANSERVICE-order → `orderState` werd `REOPENED` en
  `summaries/active` gaf die order terug (met `orderMethod`, `lastChangedOrderTime` en
  `filteredShoppingListItems`: de items van het AH-boodschappenlijstje, bruikbaar voor "Wensen").
- `PUT order/v1/items` → **400 "Required header 'Appie-Current-Order-Id' is not present."**
  De adapter stuurt die header nu mee (waarde = order-id). Een `…-Order-Hash` header, die appie-go
  noemt, kwam in geen enkele response voor.
- `orderRevert` → `SUCCESS`.

### Write-test (2026-10-07, tweede poging, geslaagd)

- `orderReopen` → `PUT items` (+1 melk, met header) → details tonen 1 regel → `PUT items`
  (quantity 0) → `orderRevert`. Alles `OK`.
- **`PUT items` geeft de complete bijgewerkte order terug** (zelfde vorm als
  `summaries/active`: regels, `totalPrice` incl. bonuskorting). De adapter valideert en
  retourneert die, zodat de autopilot zijn eigen resultaat kan controleren en loggen.
- **`quantity: 0` verwijdert de regel** (vraag 5 ✅). Undo = de vorige hoeveelheid terugzetten.
- Nog niet getest: of `orderRevert` zelf itemwijzigingen terugdraait (we zetten eerst handmatig
  terug), en wat er gebeurt met een order die `REOPENED` blijft staan (zie vraag 8).

### Wat de probe liet zien

- **Statussen in `orderFulfillments`:** `statusCode` 10 "Order is bevestigd" (delivery `SUBMITTED`),
  60 "Geïncasseerd" (`DELIVERED`) en 99 "Geannuleerd" (`CANCELLED`). Bezorgmethode: `HOME_DELIVERY`.
- **`modifiable` en `transactionCompleted` zeggen niets:** ze staan op `true` bij vrijwel alle
  orders, ook bij bezorgde en geannuleerde. De adapter selecteert daarom op `delivery.status` en
  datum: komend = niet `DELIVERED`/`CANCELLED` én leverdatum ≥ vandaag.
- **Komende orders:** dit account had vier bevestigde orders voor de komende zondagen (wekelijks,
  20:00–22:00), allemaal met totaalbedrag €0. Dat zijn kennelijk gereserveerde, lege orders, dus precies
  wat Kruidenier moet vullen.
- **Historie:** zowel `CLOSED` als zonder filter geven er **precies 10** terug (CLOSED: 16 aug – 4 okt).
  Er is dus een limiet of paginering, maar de argumenten daarvoor zijn nog onbekend.
- **Cutoff: gevonden** in `order.details` als `closingTime` (UTC). De komende PLANSERVICE-order
  voor zondag 11 okt sluit op za 10 okt 10:00Z (12:00 NL). Een oudere, losse order (`APPIE_WEB_OLD`)
  sloot de avond ervoor om 21:59Z (23:59 NL). De cutoff verschilt dus per order. Altijd uit de
  details lezen, nooit aannemen.
- **Orderdetails** bevatten verder `reopenable` (true voor de komende order, false na bezorging),
  `cancellable`, `orderMethod` (`PLANSERVICE` = vast bezorgmoment, `APPIE_WEB_OLD`),
  `deliveryTimePeriod` (lokaal en UTC), een `address` (wordt gescrubd) en `invoiceId` (gescrubd:
  het prefix lijkt een klantnummer).
- **Orderregels:** `quantity`, `amount` en soms `allocatedQuantity` (overal gelijk in de samples;
  de adapter gebruikt `allocatedQuantity` als die er is, als "geleverd"). Per product o.a.
  `salesUnitSize` ("0,58 l", "10 x 25 g", "2 stuks", "Tros", "ca. 110 g"), `mainCategory`/
  `subCategory`, `bonusMechanism` ("3 voor 5.00", "15% KORTING", "10% KORTING").
- **Houdbaarheid:** `minBestBeforeDays` (1–7) staat bij versproducten. Dat is de minimale
  houdbaarheid bij levering: bruikbaar als ondergrens, niet als volledige houdbaarheid.
- **Prijzen in historische orders** zijn de huidige catalogusprijzen (`currentPrice` is vaak
  `null`), niet wat er toen betaald is. Prijshistorie moet dus echt uit onze eigen dagelijkse logger
  komen (SPEC §7).

**`order.set_items`** zet een **absolute** hoeveelheid per product op de *actieve* order, met als
body `{"items":[{"productId","quantity","originCode":"PRD","description":"","strikethrough":false}]}`.
Dat is idempotent, dus een tweede run voegt niets dubbel toe. De undo-payload is simpelweg de vorige hoeveelheid.

**Actieve order:** na `orderReopen` wordt die order de "actieve" order op de server. Volgens
appie-go moet je daarna `orderRevert` aanroepen, anders blijft het account in die staat hangen. De adapter
weigert `add_to_order` als de doelorder niet de actieve order is.

## Veiligheid

- Alleen de endpoints hierboven kunnen worden aangeroepen; elk ander pad, andere host of methode
  geeft `ForbiddenEndpointError`. Pad-fragmenten zoals `checkout`, `payment`, `submit`, `confirm`
  en `place` worden daarnaast altijd geweigerd.
- GraphQL kan alleen met vooraf geregistreerde documenten. Vrije queries zijn onmogelijk.
- Eén request tegelijk, minimaal 1 s ertussen. Bij 429 en 5xx volgt backoff (`Retry-After` wordt
  gerespecteerd). Schrijfacties worden na een 5xx of een netwerkfout **niet** herhaald (uitkomst
  onbekend), alleen na een 429.

## Open vragen (te beantwoorden met de probe)

1. **Orderhistorie:** ~~welk endpoint?~~ `orderFulfillments(status: CLOSED)` werkt, maar geeft
   max. 10 orders. **Open:** hoe haal je oudere orders op? Kandidaten: argumenten als
   `offset`/`limit`/`page` op `orderFulfillments` (te vinden via GraphQL-introspectie), of de REST-
   route die de AH-app voor "Mijn bestellingen" gebruikt. Zonder paginering start het model met
   ~10 weken historie en groeit het daarna vanzelf, omdat we elke week importeren.
2. ~~**Cutoff**~~ → `closingTime` in `order.details` (zie hierboven).
3. **Order vs mandje (SPEC §14):** werkt `PUT order/v1/items` zonder heropenen, op het mandje
   van een nieuwe, nog niet ingediende order? Zo ja, dan is dat de fallback als er geen open order is.
4. **Heropenen:** verandert `orderReopen` iets aan de bezorgslot-reservering of de prijs?
   Draait `orderRevert` ook itemwijzigingen terug, of alleen de status?
5. ~~**Hoeveelheid 0**~~ → ja, verwijdert de regel.
6. **Bonus:** welke secties en velden van de bonuspagina geven per product het mechanisme
   (`bonusMechanism`-teksten verzamelen voor de normalisatie in fase 3)?
7. **Rate limits:** nog geen 429 gezien bij 1 req/s (drie probe-runs).
8. **Opslaan na heropenen.** *Revert-test 2026-10-07:* `orderRevert` **gooit itemwijzigingen weg**
   en zet de order terug op `CONFIRMED` met de inhoud van vóór het heropenen. **Incident:** de
   order stond al `REOPENED` omdat de gebruiker hem in de app aan het vullen was (6 producten,
   `lastChangedOrderTime` 18:37Z). De revert van de test gooide die ook weg. De gebruiker heeft ze
   handmatig teruggezet. Sindsdien weigeren de write-tests als een order niet `CONFIRMED` is.
   **Gevolg voor het ontwerp:** de AH-app heeft geen bevestigknop. Wie in de app wijzigt, laat de
   order `REOPENED` staan. Dat is dus de normale bewerkstatus. De autopilot doet daarom hetzelfde:
   heropenen (alleen als hij `CONFIRMED` is), hoeveelheden zetten, en de order `REOPENED` laten.
   **De autopilot roept `orderRevert` nooit aan**, want dat kan andermans wijzigingen wissen.
   Ongedaan maken gaat via de oude hoeveelheden terugzetten. Nog te bevestigen: dat AH een
   `REOPENED` order bij `closingTime` gewoon levert (de app-ervaring van de gebruiker wijst daarop).

## Zo draai je de spike

```
cp .env.example .env            # vul FERNET_KEY in (zie commentaar in .env.example)
uv sync
uv run python scripts/probe.py                       # alleen lezen
uv run python scripts/probe.py --write-test <productId>   # met heropenen/aanpassen/revert
```

Lees daarna de gescrubde fixtures in `tests/fixtures/ah/` na (geen adres, naam of tokens) en draai
`uv run pytest`. De contracttests valideren de opgenomen fixtures tegen de modellen.
