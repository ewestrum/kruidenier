# Keuzes en waarom

Dit document legt uit **waarom** Kruidenier is gebouwd zoals het is. Bij elke keuze staat
wat we kozen, waarom, en wat het alternatief was en wat het kost. Twijfel je of iets anders
moet, lees dan eerst hier waarom het nu zo is.

De keuzes zijn per onderwerp gegroepeerd. Onderaan staan de [lessen uit de
praktijk](#lessen-uit-de-praktijk): dingen die misgingen en wat we ervan leerden.

---

## Wat Kruidenier doet

### Verbruik per dag, niet "hoe vaak koop je het"

- **Keuze:** het model rekent uit hoeveel je per dag opmaakt (bijvoorbeeld 286 ml melk).
- **Waarom:** dan verstoort een bulkaankoop niets. Zes flessen wasmiddel in de aanbieding
  betekent zes keer zo lang voorraad. Een model op aankoopfrequentie zou denken dat je ineens
  zes keer zoveel wasmiddel gebruikt.
- **Prijs:** het model heeft verpakkingsgroottes nodig (`domain/units.py`), en die schrijft
  AH op tientallen manieren op.

### Bij twijfel voorstellen, niet zelf doen

- **Keuze:** standaard is alles een voorstel. In de AH-bestelling zetten gebeurt pas als je op
  de knop drukt. De autopilot staat uit, en mag als hij aan staat alleen "zekere" regels doen
  (tier `auto`, zie [hoe-kruidenier-rekent.md](hoe-kruidenier-rekent.md#stap-7-hoe-zeker-is-het)).
- **Waarom:** een verkeerd voorstel kost je een tik; een verkeerde automatische bestelling
  kost geld en vertrouwen. Het model moet eerst een paar weken laten zien dat het klopt. Dat
  is ook de lat uit de SPEC: twee weken achter elkaar minstens 80% overlap met wat je zelf
  zou bestellen.
- **Alternatief:** meteen volledig automatisch, wat sneller handig is maar riskant bij zo
  weinig historie.

### Geen AI in het bestelpad

- **Keuze:** het voorstel komt uit gewone rekenregels, geen taalmodel.
- **Waarom:** rekenregels zijn voorspelbaar, uitlegbaar ("voorraad op rond vr 9-10") en
  testbaar. Een taalmodel kan per keer iets anders zeggen en is niet te controleren.
- **Ruimte voor later:** een taalmodel mag eventueel helpen met families groeperen of met
  uitlegteksten, nooit met wat er besteld wordt.

### Families voorzichtig groeperen

- **Keuze:** Kruidenier voegt alleen producten samen die na het weghalen van merk en
  verpakkingswoorden precies dezelfde naam hebben. De rest doe je zelf bij Families.
- **Waarom:** samenvoegen is één klik; een verkeerde samenvoeging vertekent het verbruik
  ongemerkt. Wat jij aanpast, wordt nooit meer overschreven.
- **Prijs:** in het begin staan sommige families nog los die eigenlijk bij elkaar horen.

### De prijslogger vanaf dag één

- **Keuze:** elke dag de prijs van elk product in je families bewaren, en bij het opstarten
  een gemiste dag inhalen.
- **Waarom:** AH bewaart geen prijshistorie. Om later te zien of "25% korting" écht korting
  is (was de prijs vlak ervoor verhoogd?), heb je weken eigen data nodig. Elke dag zonder
  logger is voorgoed weg.

---

## Veilig omgaan met Albert Heijn

### Eén adapter voor alles wat met AH te maken heeft

- **Keuze:** alleen `app/ah/` weet hoe AH werkt. De rest van de code gebruikt die adapter.
- **Waarom:** de AH-API is onofficieel en kan zonder aankondiging veranderen. Dan hoeft er
  maar op één plek iets aangepast te worden.

### Een allowlist, en afrekenen staat er niet op

- **Keuze:** Kruidenier kan alleen de AH-verzoeken doen die op een vaste lijst staan
  (`app/ah/allowlist.py`). Paden met `checkout`, `payment`, `submit` of `confirm` worden altijd
  geweigerd, ook als ze per ongeluk op de lijst zouden komen. GraphQL kan alleen met vooraf
  vastgelegde teksten.
- **Waarom:** de belangrijkste belofte van Kruidenier is dat het **nooit** afrekent of een
  bestelling plaatst. Dat moet door de code onmogelijk gemaakt worden, niet alleen beloofd.
- **Regel:** nieuwe verzoeken komen er alleen bij na een onderzoek met de probe én akkoord
  van de eigenaar.

### Eerst onderzoeken, dan bouwen (fase 0)

- **Keuze:** voordat er iets gebouwd werd, hebben we met een echt account onderzocht welke
  AH-verzoeken werken en hoe de antwoorden eruitzien (`scripts/probe.py`, verslag in
  [ah-api.md](ah-api.md)).
- **Waarom:** de voorbeelden op internet waren deels verouderd. Zo bleek een order wijzigen
  een extra header `Appie-Current-Order-Id` te vereisen, en bleek de sluitingstijd wél
  beschikbaar te zijn (`closingTime`). Zonder spike hadden we op aannames gebouwd.

### Tests praten nooit met de echte AH

- **Keuze:** tests gebruiken opgenomen antwoorden (`tests/fixtures/ah/`). Een vangnet in
  `tests/conftest.py` laat elke test falen die toch naar AH probeert te gaan. Alleen een mens
  draait de probe tegen de echte API.
- **Waarom:** tests moeten snel en herhaalbaar zijn, en mogen nooit per ongeluk je echte
  bestelling aanpassen.
- **Hoe de fixtures veilig blijven:** de probe haalt adres, naam, tokens, klantnummer en
  factuurnummer uit de opgenomen antwoorden voordat ze in git komen.

### Rustig aan: één verzoek per seconde, nooit tegelijk

- **Keuze:** alle verzoeken naar AH gaan door één gedeelde wachtrij: maximaal één tegelijk,
  minstens een seconde ertussen. Bij "te veel verzoeken" wordt langer gewacht.
- **Waarom:** we gebruiken AH's app-API als gast. Netjes gedrag verkleint de kans dat AH het
  blokkeert, en voorkomt dat Kruidenier ooit als belasting opvalt.
- **Prijs:** de bonusjob (25 zoekopdrachten) duurt ongeveer een halve minuut. Dat maakt 's
  ochtends om 06:30 niet uit.

### Bij twijfel niets doen, en het laten weten

- **Keuze:** elk antwoord van AH wordt gecontroleerd tegen een model. Klopt het niet, dan
  stopt de taak, verandert er niets en krijg je een melding ("AH-koppeling kapot").
- **Waarom:** een veranderd antwoord kan betekenen dat we iets verkeerd lezen. Doorgaan zou
  verkeerde bestellingen kunnen opleveren.

### Nooit `orderRevert`

- **Keuze:** Kruidenier heropent alleen een *bevestigde* bestelling, laat hem daarna open
  staan en roept nooit `orderRevert` aan. Terugdraaien gaat door de oude aantallen terug te
  zetten.
- **Waarom:** in fase 0 bleek dat `orderRevert` **alle** niet-opgeslagen wijzigingen weggooit,
  ook die van iemand die op dat moment in de AH-app zijn bestelling vult. Dat gebeurde echt:
  zie de [lessen uit de praktijk](#lessen-uit-de-praktijk). De AH-app zelf laat een bestelling
  ook gewoon "heropend" staan als je hem wijzigt.

### Alleen verhogen, nooit verlagen

- **Keuze:** per product wordt het aantal het hoogste van "wat er al in staat" en "wat het
  voorstel zegt". AH zet absolute aantallen, dus twee keer drukken doet niets dubbel.
- **Waarom:** wat jij of een huisgenoot zelf in de bestelling zet, gaat altijd voor.
  Kruidenier vult aan; het haalt nooit weg.

### Vangrails bij het versturen

- **Keuze:** niet binnen 15 minuten voor de sluitingstijd, niet boven een maximumbedrag per
  keer (standaard €150), en alleen als de eerstvolgende levering nog die van het voorstel is.
  Elke wijziging komt in het actielog, met hoe hij terug kan.
- **Waarom:** de laatste minuten zijn te krap om nog bij te sturen; het bedrag is een vangnet
  tegen rekenfouten; en een voorstel voor zondag hoort niet in de bestelling van woensdag.

### De sluitingstijd komt van AH, niet uit een instelling

- **Keuze:** Kruidenier leest de sluitingstijd per bestelling uit de API (`closingTime`).
- **Waarom:** die verschilt per bestelling. Een vaste levering op zondag sloot zaterdag om
  12:00, een losse bestelling de avond ervoor om 23:59. Een instelling zou soms verkeerd zijn.

### Kassabonnen: minimaal, en pas aan na een probe

- **Keuze:** Kruidenier haalt van een kassabon alleen product, aantal en datum op, niet je
  lidnummer of hoe je betaalde. Importeren staat per huishouden uit totdat een probe de vorm van
  het antwoord heeft bevestigd. Kassa-ID's worden eerst via het interne AH-ID (`hqId`) vertaald en
  pas daarna, als het moet, via AH's `productConvertId`; elke vertaling wordt bewaard.
- **Waarom:** winkelaankopen maken het verbruik kloppend, maar het zijn persoonlijke gegevens en
  de endpoints zijn nog niet onderzocht. Minder ophalen is minder risico, en de cache voorkomt dat
  dezelfde vraag elke dag opnieuw naar AH gaat.

### Eerst meten, dan automatiseren

- **Keuze:** na elke levering vergelijkt Kruidenier het voorstel met de echte bestelling en toont
  het percentage "raak". Instellingen adviseert pas automatisch aanvullen na twee keer 80%.
- **Waarom:** "het voorstel klopt" moet een getal zijn, geen gevoel. Een product dat je voor het
  eerst koopt, telt niet als fout van het model.

### Meldingen per huishouden, ingesteld in de app

- **Keuze:** het ntfy-adres en de Home Assistant-webhook stel je in bij Instellingen; `.env` is
  alleen de terugval. Een herinnering wordt één keer per voorstel gestuurd, en alleen als hij
  aankwam.
- **Waarom:** meldingen instellen mag geen herstart van de containers vragen, en elk huishouden
  kan zijn eigen kanaal hebben.

### Bonus via de zoekfunctie, niet via de bonuspagina

- **Keuze:** vergelijkbare bonusproducten worden gevonden door te zoeken op de naam van je
  families, met de zoekfunctie die in fase 0 bewezen werkte.
- **Waarom:** de bonuspagina-verzoeken van AH zijn nog niet onderzocht. Liever een bewezen
  route met wat minder volledige resultaten dan een gok op een onbekend antwoord.
- **Later:** zodra een probe de bonuspagina heeft onderzocht, kan die erbij.

---

## Techniek

### Python, FastAPI en HTML van de server (met htmx)

- **Keuze:** Python 3.12 en FastAPI; de schermen zijn gewone HTML-templates (Jinja2), en
  [htmx](https://htmx.org) ververst alleen het stukje dat verandert.
- **Waarom:** één taal voor alles, geen apart JavaScript-project om te bouwen, en snelle
  pagina's op een telefoon. htmx staat in de app zelf, dus het werkt ook zonder internet op
  je LAN.
- **Alternatief:** een losse app in React of Flutter. Dat geeft meer mogelijkheden, maar ook
  twee projecten om te onderhouden.

### PostgreSQL in productie, SQLite in de tests

- **Keuze:** Postgres op de NAS; de meeste tests gebruiken een SQLite-database in het
  geheugen.
- **Waarom Postgres:** `web` en `worker` schrijven tegelijk, de prijshistorie groeit elke dag,
  en Postgres heeft goede backup-tools.
- **Waarom SQLite in tests:** honderden tests in een minuut, zonder Docker.
- **Prijs (en les):** SQLite gedraagt zich niet overal als Postgres. Daarom testen we
  migraties en risicovolle paden ook tegen een echte Postgres in Docker (zie
  [lessen](#lessen-uit-de-praktijk)).

### Pure rekenregels apart van de rest

- **Keuze:** alle formules staan in `app/domain/`: geen database, geen internet, alleen
  gegevens erin en een beslissing eruit.
- **Waarom:** zo zijn ze los en uitputtend te testen, ook met willekeurige invoer
  ("property-based testing" met Hypothesis). Bijvoorbeeld: "dubbel zoveel kopen geeft dubbel
  zoveel verbruik", of "versturen verlaagt nooit iets en twee keer versturen doet niets extra".

### Een rem op inloggen, in het geheugen

- **Keuze:** na 5 mislukte pogingen voor hetzelfde account (of 20 vanaf hetzelfde adres) binnen
  15 minuten moet je wachten. De tellers staan in het geheugen van de web-container.
- **Waarom:** de app hoort niet op internet, maar een eenvoudige rem kost bijna niets. Er is één
  web-proces, dus geheugen volstaat; een herstart die de tellers vergeet, is geen probleem.

### Wachtwoorden met scrypt, tokens met Fernet

- **Keuze:** wachtwoorden worden met scrypt gehasht (zit in Python zelf). AH-tokens worden met
  Fernet versleuteld; de sleutel `FERNET_KEY` staat alleen in `.env`.
- **Waarom:** geen extra afhankelijkheden voor iets wat de standaardbibliotheek goed kan. En
  wie de database ziet, ziet je AH-toegang niet.
- **Prijs:** raak je `FERNET_KEY` kwijt, dan moet je het AH-account opnieuw koppelen. Je
  gegevens blijven wel bewaard.

### Inloggen bij AH door een URL te plakken

- **Keuze:** je logt in bij AH in je browser en plakt daarna de regel `appie://login-exit?code=…`
  uit de ontwikkelaarstools.
- **Waarom:** AH stuurt na het inloggen door naar de AH-app (`appie://`), niet naar een
  website. Een gewone browser kan die stap niet afmaken; de code moet je dus overnemen.
- **Prijs:** iets omslachtig, maar je hoeft het maar zelden te doen.

---

## Uitrol op de NAS

### Eén image voor web en worker, voor elke processor

- **Keuze:** één Docker-image dat als `web` en als `worker` draait, gebouwd voor Intel/AMD
  (amd64) en ARM (arm64), en gepubliceerd op GitHub Container Registry.
- **Waarom:** één ding om te bouwen en bij te werken, en het werkt op elk Synology-model.

### Code privé, image publiek

- **Keuze:** de GitHub-repository is privé; het image op ghcr.io is publiek.
- **Waarom:** de repository bevat in de testfixtures wat je bij AH koopt, met ordernummers en
  je bezorgmoment. Het image bevat alleen de app-code (tests en fixtures gaan er bewust niet
  in, zie `.dockerignore`), dus de NAS kan het zonder inloggen ophalen.

### Alles via `.env`, geen vaste paden

- **Keuze:** datapad, poort, versie en geheimen staan in `.env`. `DATABASE_URL` wordt in
  `docker-compose.yml` samengesteld uit het databasewachtwoord.
- **Waarom:** het moet op elke NAS werken (bij jou bijvoorbeeld `/volume2`). En niet elke
  versie van Compose vult variabelen in binnen een `env_file`.

### Migraties automatisch bij het opstarten

- **Keuze:** `web` en `worker` draaien bij het starten `alembic upgrade head`, met een
  Postgres-*advisory lock* zodat dat nooit dubbel gebeurt. Elke migratie kan ook terug.
- **Waarom:** bijwerken moet één stap zijn. De worker migreert ook, zodat hij nooit tegen
  een lege database begint.

### Bijwerken met één commando via SSH

- **Keuze:** `scripts/update.sh <versie>` zet de versie, haalt het image op, herstart en wacht
  tot de app gezond is.
- **Waarom:** klikken in Container Manager is omslachtig. SSH zit in DSM en is versleuteld;
  telnet niet, en dat zou je wachtwoord onversleuteld over het netwerk sturen.

---

## Lessen uit de praktijk

Dingen die misgingen of verrasten, en wat er daarna is veranderd. Ze staan hier zodat
niemand dezelfde fout opnieuw maakt.

| Wat er gebeurde | Les en maatregel |
|---|---|
| Een testscript riep `orderRevert` aan terwijl de gebruiker tegelijk zijn bestelling in de AH-app vulde. Zes producten verdwenen uit de bestelling en moesten met de hand terug. | `orderRevert` wordt nooit meer gebruikt. Schrijftests weigeren als de bestelling al open staat. De nep-AH in de tests laat elke test falen die `orderRevert` aanroept. |
| De prijslogger kon een **nieuw** product niet opslaan in Postgres: de prijs werd vóór het product weggeschreven. In SQLite viel dat niet op. | Volgorde expliciet gemaakt. Kritieke paden en migraties worden ook tegen een echte Postgres getest. |
| `backup.sh` meldde een mislukte backup als geslaagd: in een shell-pijp (`pg_dump \| gzip`) telt alleen de laatste stap. | Dump en compressie apart, elk gecontroleerd. Getest met een fout wachtwoord. |
| Container Manager op Synology maakt ontbrekende mappen niet aan, en Postgres kan de rechten van een Synology-map niet aanpassen. | De mappen `db` en `backups` worden vooraf aangemaakt; Postgres schrijft in de submap `db/pgdata`. |
| Tag `v0.1.0` leverde een image `0.1.0` op, terwijl de handleiding `v0.1.0` noemde. | De workflow maakt nu beide namen; de handleiding gebruikt de versie zonder `v`. |
| De `.env` in de ontwikkelmap werd aangepast in plaats van die op de NAS, waardoor de oude versie bleef draaien. | `update.sh` zet de versie zelf in de juiste `.env`. |
| De prijsgrafiek was getekend op 640 px en werd op een telefoon verkleind, waardoor de aslabels onleesbaar klein werden. | Getekend op telefoonmaat (360 px) en op grote schermen begrensd; gecontroleerd met een screenshot. |
| Python was traag vanaf de netwerkschijf (H:). | De virtuele omgeving staat op C: (zie [ontwikkelen.md](ontwikkelen.md)). |
| "Nog genoeg" haalde een regel niet altijd weg: de extra voorraad werd opgeslokt door een tekort dat het model tot nul had afgerond. | De extra voorraad wordt nu uitgerekend tot na de volgende planhorizon. |
| Een volle gele balk betekende "op", wat als "veel voorraad" werd gelezen. | Bij de compacte lijst vervangen door een duidelijke tekst met een geel bolletje. |
