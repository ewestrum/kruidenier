# Kruidenier gebruiken

Open Kruidenier op je telefoon of computer via het adres van je NAS, bijvoorbeeld
`http://192.168.1.10:8085`. Op een telefoon kun je het via "Toevoegen aan beginscherm" als
app op je startscherm zetten.

## De eerste keer

1. Je komt op **Maak je account**. Dat wordt de beheerder van het huishouden.
2. Ga naar **Instellingen → AH-account** en volg de stappen om je AH-account te koppelen.
3. De volgende ochtend om 06:45 importeert Kruidenier je bestellingen en staat er een voorstel
   klaar. Huisgenoten voeg je toe bij **Instellingen → Mensen**.

## Deze week

Het voorstel voor je eerstvolgende AH-levering, met bovenaan tot wanneer je de bestelling nog
kunt aanpassen.

Per regel zie je het product, de verpakking, de prijs van vandaag, een eventuele bonus en de
**reden** ("Voorraad op rond vr 9-10"). Een geel bolletje betekent: waarschijnlijk al op, of
op vóór de levering.

| Knop | Wat het doet |
|---|---|
| **−** en **+** | Eén verpakking minder of meer. Kruidenier onthoudt dat je iets meer of minder gebruikt dan het dacht. |
| **⋯ → Klopt** | Bevestigt dat het voorstel goed is. |
| **⋯ → Nog genoeg** | Je hebt nog genoeg in huis; de regel verdwijnt en komt pas terug als het echt op is. |
| **⋯ → Niet meer** | Je koopt dit niet meer; het komt niet meer in voorstellen. Terugdraaien kan bij Families. |
| **Zet in mijn AH-bestelling** | Zet de regels in je echte AH-bestelling. Wat er al in staat, wordt nooit minder. |
| **Terugdraaien** | Haalt weg wat Kruidenier toevoegde, behalve producten die iemand daarna zelf aanpaste. |
| **Opnieuw berekenen** | Rekent het voorstel nu opnieuw uit; gebeurt ook elke ochtend vanzelf. |

**Afrekenen doe je altijd zelf in de AH-app.** Controleer daar ook even of alles erin staat.

**Hoe goed was het voorstel?** Onderaan staat per eerdere levering hoeveel van het voorstel
raak was, wat er gemist werd en wat er te veel in stond. Alleen producten die Kruidenier kón
kennen (minstens 3 keer gekocht) tellen als gemist. Zijn twee leveringen achter elkaar 80% of
meer raak, dan kun je automatisch aanvullen overwegen.

## Vaak gekocht

Producten die je regelmatig koopt maar die niet in het voorstel staan, met wanneer ze
ongeveer op zijn. Met **Toevoegen** zet je ze in het voorstel. Wat je zelf toevoegt, blijft
staan als het voorstel 's ochtends opnieuw wordt berekend.

## Bonus

De aanbiedingen van deze week die bij jou passen:

- **Wat je zelf koopt:** jouw producten die in de bonus zijn, het vaakst gekochte eerst;
- **Vergelijkbaar met wat je koopt:** andere merken of varianten van je families.

Met **Toevoegen** komt een aanbieding in je voorstel. De korting is wat AH zelf zegt; of het
echt voordelig is, beoordeelt Kruidenier later met je eigen prijshistorie.

## Families

Een familie is een groep producten die voor jou hetzelfde zijn. Klik op een familie om:

- **Vast te pinnen:** altijd voorstellen als het op raakt, ook als je het zelden koopt;
- **Nooit voor te stellen:** het tegenovergestelde;
- **Een voorkeursproduct** te kiezen: dat product komt in je bestelling;
- **Samen te voegen** met een andere familie, als je ze door elkaar koopt;
- **Een product apart te zetten**, als het er niet bij hoort;
- **De naam** te veranderen.

Bovenaan staat een **prijsgrafiek** van het product dat Kruidenier bestelt: de lijn is wat je
betaalt, de stippellijn de normale prijs, en een stip betekent een dag met bonus. Onder **Als tabel**
staan dezelfde gegevens als getallen. Is het voorkeursproduct volgens de prijslog niet leverbaar,
dan stelt Kruidenier het meest gekochte leverbare product uit dezelfde familie voor, en zegt dat
er ook bij ("In plaats van … (niet leverbaar)").

## Instellingen (alleen beheerders)

| Onderdeel | Wat je instelt |
|---|---|
| **Bezorgen** | Dagen tussen leveringen (meestal 7), veiligheidsmarge in dagen, en hoe lang oude aankopen meetellen. |
| **Bestellen bij AH** | Het maximumbedrag per keer, en of zekere producten automatisch in je bestelling mogen (en hoeveel uur voor de sluitingstijd). Zet automatisch pas aan als het voorstel een paar weken klopt. |
| **Vakanties** | Periodes waarin je niets verbruikt; dan komt er ook geen voorstel. |
| **Mensen** | Huisgenoten toevoegen of verwijderen. Huisgenoten zien alles behalve Instellingen. |
| **Meldingen** | Een ntfy-adres en/of Home Assistant-webhook, hoeveel uur voor de sluitingstijd je een herinnering krijgt, en een knop voor een testmelding. |
| **AH-account** | Koppelen of opnieuw koppelen, en **winkelaankopen meetellen** (je kassabonnen uit de winkel; standaard uit). |

## Je account

Onderaan elke pagina staat **Wachtwoord wijzigen** en het versienummer van Kruidenier. Na 5
mislukte inlogpogingen voor hetzelfde account moet je een kwartier wachten; dat beschermt tegen
het raden van wachtwoorden.

## Meldingen

Stel bij **Instellingen → Meldingen** een ntfy-adres of Home Assistant-webhook in (of bij de
installatie `NTFY_URL` / `HA_WEBHOOK_URL` in `.env`). Je krijgt dan een melding als:

- je bestelling binnenkort sluit en er nog producten uit het voorstel niet in staan (standaard 3
  uur van tevoren);
- er een levering is vergeleken met het voorstel ("85% raak, gemist: kwark");

- de automatische modus iets in je bestelling zette;
- AH een onverwacht antwoord gaf ("AH-koppeling kapot"). Kruidenier doet dan niets tot er een
  update is;
- je AH-account opnieuw gekoppeld moet worden.

## Veelgestelde vragen

**Waarom staat iets niet in het voorstel dat ik wel koop?** Kruidenier stelt pas iets voor als
je het in de laatste 180 dagen minstens 3 keer kocht, of als je de familie vastpint. Voeg het
toe via Vaak gekocht; met elke aankoop leert het model bij.

**Waarom staan er twee soorten melk los?** De automatische indeling is voorzichtig. Voeg ze
samen bij Families.

**Kan Kruidenier per ongeluk iets bestellen of betalen?** Nee. Het kan alleen producten
toevoegen aan een bestelling die jij al hebt ingepland; afrekenen en een bestelling plaatsen
kan het niet. Zie [keuzes.md](keuzes.md#veilig-omgaan-met-albert-heijn).
