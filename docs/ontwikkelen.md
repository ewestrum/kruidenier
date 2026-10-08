# Zelf aan Kruidenier werken

Dit document is voor wie de code wil aanpassen. Het beschrijft hoe je alles opzet, test en
uitrolt. Lees eerst [architectuur.md](architectuur.md) voor het overzicht en
[keuzes.md](keuzes.md) voor het waarom.

## 1. Wat je nodig hebt

| Tool | Waarvoor | Installeren |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python-versie en pakketten beheren | `pip install uv` of de installer van astral.sh |
| Python 3.12 | De taal | `uv python install 3.12` |
| Docker Desktop | Het image bouwen, Postgres voor tests | docker.com |
| Git | Versiebeheer | git-scm.com |

## 2. Opzetten

```sh
git clone https://github.com/ewestrum/kruidenier.git
cd kruidenier
uv sync --python 3.12          # maakt .venv en installeert alles
cp .env.example .env           # vul minstens FERNET_KEY in (commando staat erin)
```

> **Windows met de code op een netwerkschijf (zoals H: naar de NAS):** Python is dan erg
> traag (seconden per import). Zet de virtuele omgeving op een lokale schijf:
>
> ```powershell
> $env:UV_PROJECT_ENVIRONMENT = "$env:USERPROFILE\.venvs\kruidenier"
> uv sync --python 3.12
> & "$env:USERPROFILE\.venvs\kruidenier\Scripts\python.exe" -m pytest
> ```
>
> Docker Desktop kan op Windows ook geen mappen van een netwerkschijf koppelen. Draai
> `docker compose` voor ontwikkelen dus vanuit een lokale kloon.

## 3. Dagelijkse commando's

| Wat | Commando | Zonder `make` |
|---|---|---|
| Tests | `make test` | `uv run pytest` |
| Lint en types | `make lint` | `uv run ruff check . && uv run ruff format --check . && uv run mypy app` |
| Code netjes maken | | `uv run ruff check --fix . && uv run ruff format .` |
| Alles lokaal in Docker | `make dev` | `docker compose up --build` (met `docker-compose.override.yml`: hot reload, Postgres op 5432, ntfy op 8090) |
| Migraties draaien | `make migrate` | `uv run python -m app.db.migrate` |
| Image bouwen (beide processors) | `make image` | `docker buildx build --platform linux/amd64,linux/arm64 .` |
| Commando's in de app | | `uv run python -m app.cli --help` (koppelen, sync, prijzen, bonus) |

**Klaar betekent** (uit [CLAUDE.md](../CLAUDE.md)): tests groen, lint schoon, migratie
aanwezig als het datamodel veranderde, en SPEC bijgewerkt als het gedrag afwijkt.

## 4. Tests

| Map | Wat | Soort |
|---|---|---|
| `tests/domain/` | De rekenregels | tabeltests en *property-based* tests (Hypothesis) |
| `tests/ah/` | De AH-adapter | tegen opgenomen fixtures, met respx als nep-internet |
| `tests/services/` | Import, planner, prijzen, bonus, versturen, autopilot | SQLite in het geheugen, nep-AH (`fake_order.py`) |
| `tests/web/` | De schermen en knoppen, van inloggen tot versturen | FastAPI TestClient |
| `tests/db/` | Migraties: heen, terug, en gelijk aan de modellen | SQLite-bestand |

Twee vangnetten om te kennen:

- `tests/conftest.py` zet voor **elke** test een nep-internet voor `api.ah.nl` klaar. Een
  verzoek dat niet expliciet is nagebootst, laat de test falen. Zo kan een test nooit echt
  bij AH uitkomen.
- `tests/services/fake_order.py` gedraagt zich als een echte AH-bestelling (heropenen, alleen
  wijzigen als hij open staat, 0 betekent verwijderen) en laat elke test falen die
  `orderRevert` aanroept.

**Tegen echte Postgres testen** (doe dit bij elke migratie):

```sh
docker run -d --rm --name kr-pg -e POSTGRES_USER=kruidenier -e POSTGRES_PASSWORD=t \
  -e POSTGRES_DB=kruidenier -p 55433:5432 postgres:16-alpine
DATABASE_URL=postgresql+psycopg://kruidenier:t@localhost:55433/kruidenier uv run alembic upgrade head
DATABASE_URL=... uv run alembic downgrade -1 && DATABASE_URL=... uv run alembic upgrade head
docker stop kr-pg
```

## 5. Werken met de AH-API

Lees eerst [ah-api.md](ah-api.md): daar staat wat er bekend is en wat nog open is.

- **Tests gebruiken alleen fixtures** in `tests/fixtures/ah/`. Die maak je met de probe:
  ```sh
  uv run python scripts/probe.py                       # alleen lezen
  uv run python scripts/probe.py --write-test <id>     # wijzigt je echte bestelling en zet hem terug
  ```
  De probe draait **alleen een mens**, met een eigen account. Hij haalt persoonlijke gegevens
  uit de fixtures; lees ze toch na voordat je ze commit. Ruwe antwoorden gaan naar
  `.probe-raw/` (staat in `.gitignore`).
- **Schrijftests nooit draaien terwijl iemand de bestelling in de AH-app bewerkt.** De probe
  weigert als de bestelling al open staat (zie de les in [keuzes.md](keuzes.md#lessen-uit-de-praktijk)).
- **Een nieuw AH-verzoek toevoegen** gaat zo:
  1. eerst onderzoeken met de probe en vastleggen in `docs/ah-api.md`;
  2. **akkoord vragen** aan de eigenaar: de allowlist wordt niet zomaar uitgebreid;
  3. toevoegen aan `app/ah/allowlist.py`, met een pydantic-model in `models.py`;
  4. een contracttest op de fixture (`tests/ah/test_contract_fixtures.py`).
- **Nooit** iets toevoegen dat afrekent, betaalt of een bestelling plaatst. De allowlist
  weigert die woorden sowieso.

## 6. Het datamodel veranderen

1. Pas `app/db/models.py` aan.
2. Laat Alembic de migratie maken:
   ```sh
   DATABASE_URL=sqlite:///tmp.db uv run alembic upgrade head
   DATABASE_URL=sqlite:///tmp.db uv run alembic revision --autogenerate --rev-id 0003 -m "korte omschrijving"
   ```
3. **Lees de migratie na.** Let op verplichte kolommen in tabellen die al gegevens hebben
   (geef een standaardwaarde mee), en zorg dat `downgrade()` alles netjes terugzet.
4. `tests/db/test_migrations.py` controleert dat modellen en migraties gelijk zijn en dat
   terug kan. Test daarnaast tegen echte Postgres (§4).

## 7. Een nieuwe versie uitbrengen

1. Alles groen: `make test` en `make lint`.
2. Commit en push naar `main`. GitHub draait de tests opnieuw.
3. Zet een tag en push die:
   ```sh
   git tag -a v0.5.0 -m "v0.5.0: wat er nieuw is"
   git push origin v0.5.0
   ```
4. GitHub Actions bouwt het image voor amd64 en arm64 en zet het op
   `ghcr.io/ewestrum/kruidenier` met de tags `0.5.0`, `v0.5.0`, `0.5` en `latest`
   (±10–15 minuten).
5. Op de NAS: `sudo sh /volume2/docker/Kruidenier-NAS/scripts/update.sh 0.5.0`.

Versienummers: het eerste getal voor grote veranderingen, het tweede voor nieuwe functies,
het derde voor reparaties.

## 8. Afspraken in de code

- Code en namen in het **Engels**, alles wat de gebruiker ziet in het **Nederlands**.
- `app/domain` bevat alleen pure functies; geen database, geen netwerk, geen `datetime.now()`.
  Geef "vandaag" mee als argument, zodat tests vaste datums kunnen gebruiken.
- `mypy --strict` op `app/domain` en `app/ah`.
- Elke query in de web-laag filtert op het huishouden van de ingelogde gebruiker.
- Elke schrijfactie naar AH krijgt een regel in `action_log` met hoe hij terug kan.
- Teksten in de UI: kort, actief, zeg wat er gebeurt ("Zet in mijn AH-bestelling", niet
  "Verzenden"). Een foutmelding zegt wat er misging en wat je kunt doen.

## 9. Lokaal de schermen bekijken

`make dev` start alles in Docker met hot reload op http://localhost:8085. De eerste keer
maak je via `/setup` een account. Zonder gekoppeld AH-account zie je lege schermen.
Testgegevens kun je toevoegen met de helpers uit `tests/services/` en `tests/web/test_ui.py`.
