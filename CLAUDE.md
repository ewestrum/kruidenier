# CLAUDE.md — Kruidenier

Self-hosted webapp (Synology, Docker) die de online AH-bestelling automatisch vult op basis van verbruik, aanbiedingen op echtheid toetst en bulk adviseert. **Lees eerst `SPEC.md`**: dat is de bron van waarheid. Daarna `docs/keuzes.md` (waarom het zo gebouwd is, en de lessen uit de praktijk; o.a. nooit `orderRevert`), `docs/architectuur.md` en `docs/ah-api.md` (wat er over de AH-API bekend is).

## Harde regels

1. **Nooit afrekenen, betalen of een nieuwe order indienen.** De AH-adapter heeft een expliciete allowlist van endpoints. Voeg daar niets aan toe zonder dat expliciet te vragen.
2. **Tests raken de echte AH-API nooit.** Gebruik opgenomen fixtures in `tests/fixtures/ah/`. Live calls alleen via `make probe`, en die draait de mens zelf.
3. **Respecteer rate limits:** maximaal ~1 request per seconde naar AH, met backoff. Geen parallelle fan-out naar AH.
4. **Veilig falen:** valideert een AH-response niet tegen het pydantic-model, dan wordt er geen actie uitgevoerd. Log de fout en stuur een notificatie.
5. **Geen secrets in de repo.** Tokens worden versleuteld opgeslagen (Fernet), de sleutel komt uit de env. Zorg dat `.env` in `.gitignore` staat.
6. **Autopilot-acties zijn idempotent en terug te draaien.** Elke schrijfactie naar AH krijgt een `action_log`-regel met undo-payload.
7. **De autopilot gebruikt geen LLM.** Een LLM mag alleen voor de clustering van productfamilies en voor uitlegteksten.

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2, Alembic, httpx (async), pydantic v2, APScheduler
- PostgreSQL 16 als lokale container, data in een bind mount op de NAS (zie SPEC §15)
- Docker: één multi-arch image (amd64 + arm64) voor `web` en `worker`, publicatie naar GHCR via GitHub Actions
- UI: Jinja2 + HTMX, mobiel eerst, PWA-manifest
- Tests: pytest, pytest-asyncio, respx (HTTP-mocks)
- Tooling: uv, ruff, mypy (strict op `app/domain` en `app/ah`)

## Structuur

```
app/
  ah/          # adapter: client, modellen, allowlist, auth (enige plek die AH kent)
  domain/      # puur: verbruiksmodel, bonusbeoordeling, bulk, tiers (geen I/O)
  services/    # orkestratie: sync, planner, autopilot, notificaties
  db/          # modellen, sessies, migraties (alembic/)
  web/         # routes, templates, htmx-partials
  worker.py    # scheduler-entrypoint
tests/
  domain/      # property- en tabeltests op formules
  ah/          # contracttests op fixtures
  fixtures/ah/
Dockerfile
docker-compose.yml           # productie: Synology Container Manager-project
docker-compose.override.yml  # alleen dev
.env.example
scripts/                     # entrypoint.sh, backup.sh
docs/install-synology.md
.github/workflows/image.yml
```

`app/domain` bevat pure functies: data erin, beslissing eruit. Alle formules uit SPEC §6–§9 komen daar te staan, met unit tests en randgevallen (bulkaankoop, vakantie, één aankoop, uitschieters).

## Commando's

```
make dev        # compose up met hot reload
make test       # pytest
make lint       # ruff + mypy
make migrate    # alembic upgrade head
make image      # multi-arch image bouwen (buildx), optioneel als tar exporteren
make probe      # HANDMATIG: rooktest tegen echte AH-API, neemt fixtures op
```

## Werkwijze

- Werk per fase uit SPEC §13. Begin met **fase 0 (spike)**: verifieer de endpoints, neem fixtures op en documenteer de bevindingen in `docs/ah-api.md`. Pas daarna bouw je verder.
- De prijslogger (SPEC §7) moet zo vroeg mogelijk dagelijks draaien. Zonder historie werkt fase 3 niet.
- Kies bij twijfel tussen autonoom en voorstellen altijd voor **voorstellen**.
- Elke migratie heeft een downgrade. Migraties draaien automatisch bij de start van `web`.
- Het image moet zonder aanpassingen draaien op Synology Container Manager: geen hardcoded paden, alle configuratie via `.env`, en draaien als non-root user.
- **Klaar betekent:** tests groen, lint schoon, migratie aanwezig, en SPEC bijgewerkt als het gedrag afwijkt.

## Domeinbegrippen

- **Familie:** een groep uitwisselbare SKU's ("halfvolle melk").
- **Verbruik `r`:** basiseenheden per dag.
- **Tier:** `auto` / `auto_bonus` / `propose`.
- **Cutoff:** het laatste moment waarop de AH-order nog gewijzigd kan worden.
- **Huishouden:** de eenheid van het model. Gebruikers horen bij een huishouden.

Taal: code en identifiers in het Engels, UI-teksten in het Nederlands.
