# Kruidenier installeren op een Synology NAS

Deze handleiding gaat uit van DSM 7.2 met **Container Manager**. Werkt ook op ARM-modellen:
het image is multi-arch (amd64 + arm64).

> Kruidenier hoort **niet** direct op internet. Gebruik je LAN, Tailscale of de DSM reverse
> proxy met HTTPS (stap 6).

## 1. Mappen aanmaken (File Station)

Maak onder de gedeelde map `docker` deze structuur:

```text
docker/kruidenier/
├── db/            ← database (Postgres), wordt automatisch gevuld
├── backups/       ← nachtelijke dumps, 14 dagen bewaard
└── project/       ← docker-compose.yml, .env en scripts/
    └── scripts/
```

`[screenshot: File Station met de mappen]`

> Maak `db/` en `backups/` echt aan: Container Manager maakt ontbrekende mappen voor
> bind mounts **niet** zelf ("Bind mount failed: ... does not exist"). Postgres zet zijn data
> in de submap `db/pgdata`, die het zelf aanmaakt met de juiste rechten.

Zet in `project/` uit de repository:

- `docker-compose.yml`
- `.env.example` → hernoemen naar `.env`
- de map `scripts/` (met `backup.sh` en `entrypoint.sh`)

`docker-compose.override.yml` hoort **niet** op de NAS, die is alleen voor ontwikkelen.

## 2. `.env` invullen

Open `project/.env` (bijv. met de Text Editor-app) en vul in:

| Variabele | Waarde |
|---|---|
| `IMAGE` | `ghcr.io/<jouw-github-naam>/kruidenier` |
| `TAG` | een vaste versie, bijv. `0.1.0` (niet `latest`) |
| `DATA_DIR` | `/volume1/docker/kruidenier` (pas aan als je volume anders heet) |
| `POSTGRES_PASSWORD` | een lang willekeurig wachtwoord |
| `SECRET_KEY` | zie het commando in `.env.example` |
| `FERNET_KEY` | zie het commando in `.env.example`. **Bewaar deze sleutel ook in je wachtwoordmanager.** |
| `BASE_URL` | het adres waarop je Kruidenier opent, bijv. `https://kruidenier.thuis.nl` |
| `HA_WEBHOOK_URL` / `NTFY_URL` | optioneel, voor meldingen |

De sleutels genereer je op elke computer met Python, of via SSH op de NAS met
`docker run --rm python:3.12-slim python -c "..."`.

**Rechten:** zet `.env` op alleen-lezen voor jezelf. Via SSH: `chmod 600 .env`.

> **FERNET_KEY kwijt?** Dan kunnen de opgeslagen AH-tokens niet meer ontsleuteld worden. Je
> data blijft bewaard, maar je moet de AH-accounts opnieuw koppelen (stap 5).

## 3. Image beschikbaar maken

Kies één van de twee:

- **Via GHCR (aanbevolen):** een tag `v*` pushen bouwt het image automatisch
  (`.github/workflows/image.yml`). Zet het package op GitHub op *public*, of log in op de NAS:
  Container Manager → Register → Instellingen → toevoegen `ghcr.io` met een GitHub-token
  (scope `read:packages`).
- **Zonder registry:** bouw lokaal `make image-tar ARCH=amd64` (of `arm64` voor ARM-NAS'en) en
  importeer de `.tar` via Container Manager → Image → Toevoegen → Importeren uit bestand.
  Zet dan in `.env`: `IMAGE=kruidenier` en `TAG=local`.

`[screenshot: Container Manager, register-instellingen]`

## 4. Project aanmaken (Container Manager)

1. Container Manager → **Project** → **Aanmaken**.
2. Projectnaam: `kruidenier`. Pad: `docker/kruidenier/project`.
3. Bron: *Bestaande docker-compose.yml gebruiken*.
4. Web portal-instellingen overslaan → **Gereed**.

Container Manager start vier containers: `db`, `web`, `worker` en `backup`. De `web`-container
draait bij elke start automatisch de databasemigraties.

`[screenshot: project met vier draaiende containers]`

Controle: open `http://<nas-ip>:8085/healthz`. Na een minuut moet daar `"status": "ok"` staan.
Zie je `no heartbeat yet`, wacht dan even: de worker schrijft elke minuut een heartbeat.

## 5. Eerste AH-account koppelen

Zolang de web-UI er nog niet is, koppel je het account via de terminal van de container:

1. Container Manager → **Container** → `kruidenier-web-1` → **Details** → **Terminal** →
   **Aanmaken** → bij *Opdracht* invullen:
   `python -m app.cli link-account --household Thuis --label "AH"`
   (Of via SSH: `sudo docker exec -it kruidenier-web-1 python -m app.cli link-account`.)
2. De terminal toont een login-URL. Open die op je computer in Chrome of Edge, **met DevTools
   open** (F12 → tab *Network* → vinkje *Preserve log*).
3. Log in bij AH. De pagina lijkt daarna te blijven hangen; dat hoort zo.
4. Typ `login-exit` in het filter van de Network-tab. Rechtsklik de (rode) regel →
   **Copy → Copy URL** en plak die in de terminal.
5. Draai daarna één keer handmatig de eerste import:
   `python -m app.cli sync` en `python -m app.cli log-prices`.

Daarna gaat alles vanzelf: de worker logt elke dag om 06:15 de prijzen en synchroniseert om
06:45 je bestelhistorie en het concept voor de volgende levering.

`[screenshot: terminal met 'Gekoppeld: AH in huishouden Thuis']`

## 6. Reverse proxy met HTTPS (optioneel)

1. DSM → Configuratiescherm → **Aanmeldingsportaal** → Geavanceerd → **Reverse proxy** →
   Aanmaken.
2. Bron: `HTTPS`, hostnaam `kruidenier.<jouwdomein>`, poort 443.
3. Doel: `HTTP`, `localhost`, poort `8085`.
4. Certificaat: Configuratiescherm → Beveiliging → Certificaat → Let's Encrypt, en koppel het
   aan deze reverse-proxyregel.
5. Beperk de toegang tot je LAN/Tailscale via de Synology-firewall, of zet niets open in je
   router.

`[screenshot: reverse-proxyregel]`

## Backups

- Elke nacht om 03:00 komt er een `pg_dump` in `docker/kruidenier/backups/`
  (`kruidenier-JJJJ-MM-DD_UUMM.sql.gz`). Na 14 dagen worden ze opgeruimd.
- Neem die map mee in **Hyper Backup** naar een externe schijf of de cloud.

**Terugzetten** (via SSH, in de map met de dump):

```sh
gunzip -c kruidenier-2026-10-08_0300.sql.gz | sudo docker exec -i kruidenier-db-1 psql -U kruidenier kruidenier
```

Stop eerst `web` en `worker` (in Container Manager), zet terug, en start ze daarna weer.

**Direct een backup maken** (bijv. vóór een update): Container Manager → Container →
`kruidenier-backup-1` → Terminal → Aanmaken → opdracht `sh /scripts/backup.sh now`.
Of via SSH: `sudo docker exec kruidenier-backup-1 sh /scripts/backup.sh now`.

## Updaten

1. Maak een backup met `backup.sh now` (zie hierboven).
2. Zet in `.env` de nieuwe versie: `TAG=0.2.0`.
3. Container Manager → Project `kruidenier` → **Stoppen** → **Bouwen** (haalt het nieuwe image
   op) → **Starten**.

Migraties lopen vanzelf bij het starten. Wil je terug naar de vorige versie, zet dan de oude
`TAG` terug én de backup van stap 1: de nieuwe versie kan de database al gemigreerd hebben.

## Problemen

| Symptoom | Oplossing |
|---|---|
| `/healthz` geeft `db: unreachable` | Kijk in de log van `db`; klopt `POSTGRES_PASSWORD` nog met de bestaande database? |
| `/healthz` geeft `worker: last heartbeat ...s ago` | Bekijk de log van `worker`. Meestal ontbreekt `FERNET_KEY`. |
| Melding "AH-koppeling kapot" | AH heeft de app-API veranderd. Er wordt niets aangepast; wacht op een update. |
| Melding "AH-account opnieuw koppelen" | Herhaal stap 5. |
