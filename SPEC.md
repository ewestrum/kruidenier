# Kruidenier — slimme AH-boodschappen op de NAS

Naam: **Kruidenier**, je eigen kruidenier die weet wat er op raakt. Self-hosted webapp die op basis van je bestelhistorie bij Albert Heijn zelf je online bestelling vult, aanbiedingen op echtheid toetst en bulkinkoop adviseert. Later komt daar maaltijdplanning bij.

---

## 1. Doel en uitgangspunten

**Probleem:** elke week handmatig de AH-app vullen met grotendeels dezelfde producten.

**Doel:** de bestelling staat vóór de sluitingstijd vanzelf klaar. De gebruiker hoeft alleen nog te checken en zelf af te rekenen.

**Vastgestelde keuzes**

| Onderwerp | Keuze |
|---|---|
| Kanaal | Alleen **online bestellen** (bezorgen/ophalen) |
| Autonomie | App mag **zelfstandig items toevoegen** aan mandje/order. **Afrekenen en betalen doet altijd de mens.** |
| Gebruikers | **Meerdere accounts** (zie §3) |
| Voorraad- en bulkgrenzen | **Instelbaar** per huishouden, categorie en product |
| Maaltijdplanning | Komt later. Het datamodel houdt er nu al rekening mee (§12). |

**Kernprincipe:** we modelleren het **verbruik** (eenheden per dag), niet de aankoopfrequentie. Daardoor verstoren bulkaankopen het model niet: zes pakken wasmiddel betekent simpelweg zes keer zo lang voorraad.

---

## 2. Architectuur

```
┌──────────── Synology (Docker Compose) ────────────┐
│  web     FastAPI + Jinja/HTMX (PWA, mobiel eerst)  │
│  worker  APScheduler: syncs, model, autopilot      │
│  db      PostgreSQL 16                             │
│  (opt.)  reverse proxy / Tailscale, níet publiek   │
└────────────────────────────────────────────────────┘
        │ AH-adapter (losse module, vervangbaar)
        ▼
   api.ah.nl (onofficiële mobiele API)
```

- **Taal:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, httpx (async), pydantic.
- **UI:** server-rendered met HTMX. Dat is eenvoudig en past bij een NAS. Een Flutter-web-UI kan later via dezelfde JSON-API.
- **Notificaties:** webhook naar Home Assistant, met optioneel ntfy als fallback.
- **Secrets:** AH refresh tokens versleuteld in de database (Fernet). De sleutel staat in de env/Docker-secret, niet in de repo.

---

## 3. Gebruikers en accounts

Verbruik is een eigenschap van een **huishouden**, niet van een persoon. Daarom deze structuur:

- **Household**: de eenheid waarvoor het model draait.
- **User**: een app-login (lokaal, wachtwoord + sessie). Rollen: `admin` (instellingen, AH-koppeling, autonomie) en `member` (wensen toevoegen, concept bekijken, suggesties goed- of afkeuren).
- **AhAccount**: een gekoppeld AH-account (OAuth-tokens). Een huishouden heeft er meestal één. Meerdere is mogelijk, met één account als "bestel-account" en de andere alleen als bron van historie.
- Meerdere huishoudens op één instantie wordt ondersteund, zodat je het bijvoorbeeld ook voor familie kunt draaien.

> Aanname, nog te bevestigen: "meerdere accounts" betekent meerdere gezinsleden met eigen login, plus eventueel meerdere AH-accounts waarvan de historie wordt samengevoegd.

---

## 4. AH-adapter

De AH-koppeling is de grootste bron van breekbaarheid, dus alles loopt via één interface:

```python
class AhClient(Protocol):
    async def login_url(self) -> str: ...
    async def exchange_code(self, code: str) -> Tokens: ...
    async def refresh(self, tokens: Tokens) -> Tokens: ...
    async def list_orders(self, since: date) -> list[OrderSummary]: ...
    async def get_order(self, order_id: str) -> OrderDetail: ...
    async def get_product(self, product_id: int) -> Product: ...  # prijs, bonus, inhoud
    async def search_products(self, query: str) -> list[Product]: ...
    async def get_bonus(self, week: str = "current") -> list[BonusOffer]: ...
    async def get_upcoming_order(self) -> UpcomingOrder | None: ...  # slot + cutoff
    async def add_to_order(self, order_id: str, items: list[LineItem]) -> None: ...
    async def add_to_basket(self, items: list[LineItem]) -> None: ...  # fallback
```

**Eisen**
- **Nooit** endpoints aanroepen voor afrekenen, betalen of het indienen van een nieuwe order. Dit wordt in code afgedwongen met een allowlist van endpoints.
- Rate limiting: maximaal ~1 request per seconde, met backoff. Volledige sync maximaal 1× per dag, prijzen 1× per dag.
- Elke response wordt gevalideerd tegen pydantic-modellen. Bij een schemafout volgt een "adapter kapot"-notificatie en geen acties.
- Tests draaien op **opgenomen fixtures**, nooit live. Een losse `make probe` draait handmatig een rooktest tegen de echte API.
- Spike vooraf (fase 0): de endpoints verifiëren met een echt account. Referenties: `python-appie` (PyPI) en `ah-mcp` (GitHub). Die laatste toont het heropenen van een order en het bijwerken van items.

---

## 5. Datamodel (kern)

```
household(id, name, settings_json)
user(id, household_id, email, pw_hash, role)
ah_account(id, household_id, label, tokens_enc, is_order_account)

product(ah_id PK, title, brand, unit_size, unit (g|ml|st), category, shelf_life_days?)
product_family(id, household_id, name, base_unit, pinned, excluded)
family_member(family_id, ah_product_id, preferred)        -- "halfvolle melk" = meerdere SKU's

purchase(id, household_id, ah_order_id, ah_product_id, qty, delivered_at)
price_observation(ah_product_id, observed_on, price, regular_price, bonus_label, bonus_mechanism)
bonus_offer(id, week, ah_product_id, mechanism, raw_json)

family_stats(family_id, rate_per_day, cv_interval, n_purchases, last_purchase_at,
             est_stock, due_date, confidence, bonus_interval_days, computed_at)

feedback(id, family_id, user_id, kind (enough_stock|not_anymore|more|less|ok), at)
plan(id, household_id, delivery_date, status (draft|applied|reverted))
plan_line(plan_id, family_id, ah_product_id, qty, reason_code, reason_text, tier, applied)
action_log(id, household_id, at, action, payload_json, undo_payload_json)

-- later (§12)
recipe(...), recipe_ingredient(recipe_id, family_id, amount, unit), meal_plan(...)
```

---

## 6. Verbruiksmodel

Per `product_family` worden alle hoeveelheden omgerekend naar de basiseenheid (liter, kg of stuks).

1. **Intervalverbruik:** voor elke opeenvolgende aankoop i → i+1 geldt `r_i = q_i / (d_{i+1} − d_i)`.
2. **Verbruikssnelheid** `r` = exponentieel gewogen gemiddelde van `r_i` (halfwaardetijd instelbaar, standaard 60 dagen), na het wegfilteren van uitschieters (buiten 1.5×IQR).
3. **Dekking laatste aankoop:** `due_date = d_last + (q_last + carryover) / r`. De carryover is standaard 0 en wordt bijgesteld via feedback.
4. **Opnemen in plan** als `due_date < volgende_levering + cadans + veiligheidsmarge`.
5. **Hoeveelheid** = `ceil((r × (cadans + marge) − voorraad_bij_levering) / unit_size)`, afgerond op hele verpakkingen.
6. **Betrouwbaarheid:** `high` bij ≥ 4 aankopen, een variatiecoëfficiënt van de intervallen < 0.35 en een laatste aankoop binnen 3× het verwachte interval. `medium` of `low` anders.
7. **Productfamilies:** eenmalig voorstel via LLM-clustering of heuristiek (categorie + titel-similariteit), daarna handmatig te corrigeren in de UI. Binnen een familie gaat de voorkeurs-SKU voor, of de goedkoopste per eenheid als de gebruiker dat instelt.
8. **Feedback** stuurt bij:
   - `enough_stock` → carryover omhoog en de due date schuift op.
   - `not_anymore` → familie wordt `excluded`.
   - `more` / `less` → correctiefactor op `r`.

   *Fase 1, uitgewerkt:* `more`/`less` vermenigvuldigen de correctie met 1,15 of delen erdoor (begrensd tussen 0,25 en 4), en passen de conceptregel met één verpakking aan. `enough_stock` verhoogt de carryover tot de familie tot na de volgende planhorizon gedekt is (levering + cadans + marge + 1 dag), en minstens met de conceptregel zelf. Die carryover vervalt zodra de familie opnieuw gekocht is. Een product dat iemand zelf aan het concept toevoegt, blijft staan als het concept elke ochtend opnieuw wordt berekend.
9. **Pauzes:** een vakantieperiode per huishouden. Die dagen tellen niet mee in het verbruik en er wordt geen plan gemaakt.
10. **Ruis:** families met minder dan 3 aankopen in 180 dagen komen niet in het automatische plan, tenzij ze zijn vastgepind.

---

## 7. Bonusbeoordeling

Vanaf dag 1 logt de worker **dagelijks** de prijzen van alle SKU's in de families van het huishouden en van alle bonusproducten. Prijshistorie bestaat nergens anders, dus deze logger is de basis voor alles in deze sectie.

> *Stand fase 1:* de logger volgt alle SKU's in families (06:15, in batches van 30 via `product/search/v2/products`, en bij het starten van de worker een inhaalslag als er die dag nog niets gelogd is). Alle bonusproducten volgen zodra de vorm van de bonuspagina-endpoints met de probe is vastgesteld (docs/ah-api.md vraag 6).

1. **Effectieve stukprijs per basiseenheid** per mechanisme:
   - `x% korting` → prijs × (1 − x)
   - `1+1 gratis` → prijs / 2 per stuk, maar alleen als 2 stuks verbruikt worden binnen de houdbaarheid
   - `2e halve prijs` → 0,75 × prijs
   - `n voor €y` → y / n
   - `2+1`, `bundel`, enzovoort → generiek als (totaalprijs / aantal stuks)
2. **Referentieprijs** = mediaan van de niet-bonusprijzen over 90 dagen, met als fallback de goedkoopste reguliere eenheidsprijs binnen de familie.
3. **Echte korting** = `1 − effectief / referentie`.
4. **Rode vlaggen:**
   - De reguliere prijs is in de 21 dagen vóór de bonus met meer dan 10% gestegen.
   - Een grotere verpakking zonder bonus is goedkoper per eenheid.
   - De bonus vereist een hoeveelheid die niet binnen de houdbaarheid of het verbruik op gaat.
5. **Suggestie** als de echte korting ≥ de drempel is (standaard 15%, instelbaar) en de familie relevant is (gekocht of vastgepind). Optioneel ook "nieuw in jouw categorieën", maar dat wordt nooit autonoom toegevoegd.

---

## 8. Bulkoptimalisatie

Uit de prijshistorie leert het model per familie het **bonusinterval** (hoe vaak er een actie op zit, bijvoorbeeld wasmiddel ~6 weken).

```
bulk_qty = min( r × verwachte_dagen_tot_volgende_bonus,
                r × shelf_life_days × 0.8,
                opslaglimiet_familie_of_categorie )
           − verwachte_voorraad_bij_levering
```

- Alleen bij een echte korting ≥ de bulkdrempel (standaard 25%).
- Begrensd door het **bulkbudget** per week (instelbaar).
- Opslaglimieten zijn instelbaar per familie en per categorie, bijvoorbeeld "vriezer", "drank" of "schoonmaak".

---

## 9. Autonomie en vangrails

Elke planregel krijgt een **tier**:

| Tier | Wat | Actie |
|---|---|---|
| `auto` | Vaste producten met `high` betrouwbaarheid, normale hoeveelheid | Wordt automatisch in de order/het mandje gezet |
| `auto_bonus` | Een vast product dat nu echt in de aanbieding is, binnen de normale hoeveelheid | Automatisch (via een instelling uit te zetten) |
| `propose` | Bulk, `medium`/`low` betrouwbaarheid, nieuwe producten, alles boven de budgetgrens | Alleen voorgesteld, de gebruiker keurt goed |

**Vangrails**
- Nooit afrekenen of betalen. Wordt afgedwongen in de adapter (§4).
- Een maximaal weekbedrag voor autonome toevoegingen, plus een maximale afwijking ten opzichte van het gemiddelde van de laatste 8 weken.
- Een maximaal aantal stuks per regel (sanity check tegen bugs).
- **Timing:** de autopilot draait X uur voor de cutoff (standaard 24 uur), zodat er tijd is om bij te sturen.
- **Idempotent:** bij een tweede run wordt niets dubbel toegevoegd. Er wordt vergeleken met wat al in de order staat.
- *Fase 2, gebouwd (v0.3.0):* een knop "Zet in mijn AH-bestelling" op Deze week (alle niet-verstuurde regels), plus de autopilot (standaard uit, aan te zetten bij Instellingen) die elk half uur kijkt of een huishouden binnen `autopilot_hours_before` vóór de cutoff zit en dan alleen `auto`-regels verstuurt. Tier `auto` = reden `due`, betrouwbaarheid `high` en hoogstens 1,5× de gebruikelijke hoeveelheid per aankoop (`app/domain/tiers.py`); `auto_bonus` volgt in fase 3. Hoeveelheden worden alleen verhoogd (`max(in order, voorstel)`), nooit verlaagd. Vangrails: maximaal bedrag per keer (`max_push_amount`, standaard €150), niet binnen 15 minuten voor de cutoff, alleen als de eerstvolgende order nog die van het voorstel is. Terugdraaien zet alleen producten terug die sindsdien niet door iemand anders zijn aangepast. Nog niet gebouwd: de grens "maximale afwijking t.o.v. het gemiddelde van de laatste 8 weken".
- **Nooit `orderRevert`:** een revert gooit alle niet-opgeslagen wijzigingen weg, ook die van een huisgenoot in de AH-app (fase 0, docs/ah-api.md vraag 8). De autopilot heropent alleen een `CONFIRMED` order, laat hem daarna `REOPENED` staan (zoals de app zelf doet) en draait terug door de vorige hoeveelheden terug te zetten. Producten die al in de order staan en niet door de autopilot zijn toegevoegd, raakt hij nooit aan.
- **Logging en ongedaan maken:** elke actie komt in `action_log` met een undo-payload. In de UI staat een knop "draai autopilot terug".
- **Notificatie** na elke run: "X items toegevoegd (€Y), Z voorstellen wachten", met een link naar het concept.

---

## 10. UI (mobiel eerst)

- **Deze week:** de order met per regel de tier, de reden ("voorraad op do", "−32% t.o.v. normaal, genoeg tot volgende bonus") en knoppen voor ok, meer, minder, nog genoeg en niet meer.
- **Voorstellen:** goedkeuren of afwijzen, ook in bulk.
- **Wensen:** vrije items van huisgenoten, die bij de volgende run worden gematcht en toegevoegd.
- *Gebouwd (v0.4.0):* **Vaak gekocht** (families die niet in het voorstel staan, gesorteerd op aantal aankopen, met Toevoegen) en **Bonus**. De bonustab toont per huishouden eerst eigen producten in de bonus, daarna vergelijkbare producten (gevonden door elke ochtend om 06:30 met `product.search` te zoeken op de namen van de 25 meest gekochte families), gesorteerd op hoe vaak je het product of de familie koopt. Gevonden bonusproducten gaan ook de prijslog in. De bonuspagina-endpoints van AH zijn nog niet geverifieerd en worden daarom nog niet gebruikt. De korting is AH's eigen tekst; de echte-kortingtoets volgt in fase 3.
- **Families:** SKU's samenvoegen of splitsen, vastpinnen, uitsluiten, voorkeurs-SKU kiezen.
- **Prijzen:** prijsgrafiek per familie met de bonusmomenten erin.
- **Instellingen** (admin):
  - drempels en budgetten
  - opslaglimieten
  - bezorgcadans en marge
  - vakanties
  - autonomie per tier
  - gebruikers
  - AH-koppeling

---

## 11. Instelbaar (overzicht)

| Niveau | Instellingen |
|---|---|
| Huishouden | cadans, veiligheidsmarge, autopilot-tijdstip, weekbudget auto/bulk, kortingsdrempels, halfwaardetijd, vakanties, notificatiekanaal |
| Categorie | opslaglimiet, bulk aan/uit |
| Familie | vastgepind/uitgesloten, voorkeurs-SKU, min/max per bestelling, houdbaarheid-override, opslaglimiet, autonomie-override |

---

## 12. Later: maaltijdplanning (nu al rekening mee houden)

- Recepten verwijzen naar **product_family**, niet naar SKU's. Daardoor kunnen ingrediënten later gewoon als extra vraag bovenop het verbruik komen.
- Een weekmenu genereert een tijdelijke vraag. Het model moet die **niet** meetellen als structureel verbruik (de purchase krijgt een `source`-tag: `staple | meal | manual`).
- Ideeën:
  - Menu afstemmen op de bonus van de week.
  - Restjes-planning (halve pakken opmaken).
  - Allerhande-recepten als bron, mits via de API beschikbaar.

---

## 13. Fasering

**Fase 0: spike (½–1 dag)**
- AH-login, orders ophalen, één order heropenen of items toevoegen aan een testmandje.
- Fixtures opnemen. Vaststellen of de upcoming-order/cutoff beschikbaar is.

**Fase 1: MVP**
- Docker-package voor Synology volgens §15 (image, compose, backup, installatiehandleiding), auth, huishoudens en gebruikers, AH-koppeling.
- Import van de volledige historie, families (heuristiek + handmatig), verbruiksmodel.
- Plan "Deze week" alleen als **concept** (nog niet autonoom).
- De **prijslogger draait vanaf nu dagelijks.**
- *Klaar als:* het concept voor 2 opeenvolgende weken ≥ 80% overlapt met wat je zelf zou bestellen.

**Fase 2: autopilot**
- Tiers, vangrails, toevoegen aan de order, actielog en terugdraaien, HA-notificatie.

**Fase 3: bonus**
- Normalisatie van mechanismes, referentieprijs, rode vlaggen, `auto_bonus`.
- Vereist ≥ 4–6 weken prijsdata.

**Fase 4: bulk**
- Bonusinterval leren, bulkformule, opslag en budget.

**Fase 5: maaltijdplanning**

---

## 14. Risico's en open punten

- **Onofficiële API:** kan zonder aankondiging breken. Daarom de adapter-isolatie, schemavalidatie, fixtures en "veilig falen" (geen acties bij twijfel). Gebruik is persoonlijk en met lage frequentie.
- **Order vs mandje:** nog te bepalen of je eerst een slot/order moet hebben voordat items in een order kunnen, of dat het mandje volstaat. Dit volgt uit fase 0. *Fase 0 (deels):* het testaccount heeft wekelijks vooraf bevestigde, lege orders (`PLANSERVICE`, `reopenable`). De autopilot vult die via heropenen + `PUT items`. De write-test moet dit nog bevestigen.
- **Cutoff:** *fase 0:* staat per order als `closingTime` in de orderdetails en verschilt per order. De autopilot leest hem altijd uit de API, het is geen instelling.
- **Houdbaarheid:** grotendeels niet uit de API te halen. *Fase 0:* versproducten hebben `minBestBeforeDays` (minimale houdbaarheid bij levering), te gebruiken als ondergrens. Verder categorie-defaults plus een override.
- **Historie:** *fase 0:* de API geeft voorlopig maximaal de 10 laatste orders. Zonder paginering begint het verbruiksmodel met ~10 weken data en groeit het door de wekelijkse import. Historische orders bevatten geen betaalde prijs, alleen de huidige catalogusprijs.
- **Fuzzy families:** een LLM is optioneel. Als het wordt gebruikt, alleen voor eenmalige clustering en uitleg, nooit in het autopilotpad.

---

## 15. Database en uitrol op Synology

### Lokale database

- **PostgreSQL 16** draait als eigen container naast de app. Alle data blijft op de NAS: historie, prijzen, instellingen en versleutelde tokens. Er gaat niets naar de cloud, behalve de calls naar AH.
- Waarom niet SQLite: web en worker schrijven tegelijk, en de prijshistorie groeit snel (dagelijks honderden SKU's). Postgres is daarvoor robuuster en heeft goede tooling voor backups.
- De data staat in een **bind mount** op de NAS, niet in een anoniem Docker-volume. Zo zie je de data in File Station en neemt Hyper Backup het mee.

### Docker-package

- **Eén image** (`ghcr.io/<user>/kruidenier`) voor zowel `web` als `worker`, met een ander startcommando. Multi-stage Dockerfile: een build-stage met uv, en een slanke runtime op `python:3.12-slim` die als non-root user draait.
- **Multi-arch:** `linux/amd64` + `linux/arm64` via `docker buildx`. Synology-modellen verschillen per processor (Intel/AMD of ARM), zo werkt het op allemaal.
- **CI:** GitHub Actions bouwt het image bij elke tag `v*` en pusht naar GHCR. Lokaal kan het ook met `make image`, met een tar-export voor import via Container Manager als je niet via een registry wilt werken.
- **Migraties** draaien automatisch bij het starten van de container (`alembic upgrade head` in de entrypoint van `web`, met een advisory lock zodat ze nooit dubbel lopen). *Fase 1:* ook de `worker` draait ze bij het starten, zodat hij nooit tegen een lege database begint. Dankzij dezelfde advisory lock is dat veilig.
- **Healthchecks:**
  - `web` heeft `/healthz` (DB bereikbaar, laatste worker-heartbeat < 10 min).
  - `db` gebruikt `pg_isready`.
  - `worker` schrijft een heartbeat naar de DB.

### `docker-compose.yml` (Container Manager → Project)

```yaml
name: kruidenier
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: kruidenier
      POSTGRES_USER: kruidenier
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      TZ: Europe/Amsterdam
    volumes:
      - /volume1/docker/kruidenier/db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U kruidenier"]
      interval: 10s
      retries: 5
    restart: unless-stopped

  web:
    image: ghcr.io/<user>/kruidenier:${TAG:-latest}
    command: web
    env_file: .env
    ports:
      - "8085:8000"
    depends_on:
      db: { condition: service_healthy }
    restart: unless-stopped

  worker:
    image: ghcr.io/<user>/kruidenier:${TAG:-latest}
    command: worker
    env_file: .env
    depends_on:
      db: { condition: service_healthy }
    restart: unless-stopped

  backup:
    image: postgres:16-alpine
    entrypoint: /scripts/backup.sh       # nachtelijke pg_dump, 14 dagen bewaren
    environment:
      PGPASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - /volume1/docker/kruidenier/backups:/backups
      - ./scripts:/scripts:ro
    depends_on:
      db: { condition: service_healthy }
    restart: unless-stopped
```

> *Fase 1, afwijking van het voorbeeld hierboven:* in de echte `docker-compose.yml` zijn het datapad (`${DATA_DIR}`), de image-naam (`${IMAGE}`) en de poort (`${WEB_PORT}`) variabelen uit `.env`, zodat er geen paden hardgecodeerd zijn. `DATABASE_URL` wordt in de compose-file samengesteld uit `POSTGRES_PASSWORD`, omdat niet elke Compose-versie variabelen binnen een `env_file` invult. Het AH-account koppel je voorlopig via `python -m app.cli link-account` in de container (docs/install-synology.md), tot de web-UI dat overneemt.

### Configuratie (`.env.example` in de repo, `.env` alleen op de NAS)

```
TZ=Europe/Amsterdam
POSTGRES_PASSWORD=
DATABASE_URL=postgresql+psycopg://kruidenier:${POSTGRES_PASSWORD}@db:5432/kruidenier
SECRET_KEY=          # sessies
FERNET_KEY=          # versleuteling AH-tokens
BASE_URL=            # bijv. https://kruidenier.<jouwdomein> (voor links in notificaties)
HA_WEBHOOK_URL=      # optioneel
NTFY_URL=            # optioneel
```

### Toegang en beveiliging

- De app hoort **niet** direct op internet. Toegang loopt via het LAN, Tailscale of de **DSM reverse proxy** met een Let's Encrypt-certificaat, eventueel achter Synology-firewallregels.
- `.env` krijgt rechten 600. Als de `FERNET_KEY` kwijtraakt, moeten de AH-accounts opnieuw gekoppeld worden. Dat is acceptabel, maar wordt wel gedocumenteerd.

### Backups en updates

- Nachtelijke `pg_dump` naar `/volume1/docker/kruidenier/backups`. **Hyper Backup** neemt die map mee naar extern of cloud.
- Herstellen: `docker exec -i kruidenier-db-1 psql -U kruidenier < dump.sql`. Dit wordt beschreven in `docs/install-synology.md`.
- Updaten: in Container Manager het project stoppen, het image pullen en weer starten. Migraties lopen dan vanzelf. Pin in productie een versie-tag (`TAG=v0.3.0`) en gebruik niet `latest`.

### Lokaal ontwikkelen

`docker-compose.override.yml` (alleen dev): de broncode als volume mounten, `uvicorn --reload`, de db-poort 5432 openzetten en een `mailpit`/`ntfy`-container erbij voor het testen van notificaties.

### Opleveren

De repo bevat:
- `Dockerfile` en `docker-compose.yml`
- `docker-compose.override.yml`
- `.env.example`
- `scripts/backup.sh` en `scripts/entrypoint.sh`
- `.github/workflows/image.yml`
- `docs/install-synology.md`: stap-voor-stap met screenshots-placeholders. Mappen aanmaken in File Station, `.env` invullen, project aanmaken in Container Manager, de reverse proxy instellen en het eerste AH-account koppelen.
