# Hoe Kruidenier rekent

Dit document legt stap voor stap uit hoe Kruidenier bepaalt wat er in het voorstel komt en
hoeveel. Alle regels staan als pure functies in `app/domain/`. Daar zijn ze los te testen,
zonder database of internet. De getallen hieronder zijn de standaardwaarden; de meeste kun
je aanpassen bij Instellingen.

## Het idee in één zin

Kruidenier rekent uit **hoeveel je per dag opmaakt**, niet hoe vaak je iets koopt.

Waarom dat uitmaakt: koop je één keer zes flessen wasmiddel in de aanbieding, dan zou een
systeem dat naar "hoe vaak koop je dit" kijkt in de war raken. Kruidenier denkt: zes flessen
betekent gewoon zes keer zo lang voorraad. Een bulkaankoop verstoort het model dus niet.

## Stap 1: producten groeperen in families

Voor jou zijn "AH Andijvie fijngesneden grootverpakking" en "AH Andijvie fijngesneden
kleinverpakking" hetzelfde. Kruidenier rekent daarom per **familie**.

De eerste indeling maakt Kruidenier zelf (`domain/families.py`): het haalt het merk, "AH",
verpakkingsgroottes en woorden als "grootverpakking" uit de titel. Producten met dezelfde
overgebleven naam én dezelfde eenheid (gram, milliliter of stuks) komen samen. Dat is
bewust voorzichtig: samenvoegen kun je met één klik in het scherm Families, terwijl een
verkeerde samenvoeging het verbruik ongemerkt zou vertekenen. Wat je zelf aanpast, laat
Kruidenier daarna met rust.

## Stap 2: alles in dezelfde eenheid

AH schrijft verpakkingen op als "0,58 l", "10 x 25 g", "6 stuks" of "ca. 110 g".
`domain/units.py` maakt daar grammen, milliliters of stuks van. Lukt dat niet (bijvoorbeeld
"Tros"), dan telt het product als 1 stuk.

## Stap 3: verbruik per dag

Voor elke twee opeenvolgende aankopen van een familie:

> **verbruik in die periode = gekochte hoeveelheid ÷ aantal dagen tot de volgende aankoop**

Daarna:

- **Vakantiedagen tellen niet mee.** Ben je twee weken weg, dan maak je in die tijd niets op.
- **Uitschieters gaan eruit.** Met minstens 4 periodes worden waarden die ver buiten het
  normale vallen weggelaten (de "1,5 × IQR"-regel), bijvoorbeeld die ene week met een feestje.
- **Recent telt zwaarder.** Een aankoop van 60 dagen geleden telt half zo zwaar als een van
  vandaag (instelbaar: "Geheugen van het verbruik").
- **Jouw feedback telt mee.** "Meer" en "Minder" passen een correctiefactor toe (zie stap 8).

## Stap 4: wanneer is het op?

> **op-datum = laatste aankoop + (laatst gekochte hoeveelheid + extra voorraad) ÷ verbruik per dag**

"Extra voorraad" is 0, tenzij je "Nog genoeg" hebt gezegd (zie stap 8). Vakantiedagen worden
weer overgeslagen.

## Stap 5: moet het in het voorstel?

Kruidenier kijkt vooruit tot **de volgende levering + de bezorgcadans + een
veiligheidsmarge**. Met de standaardwaarden is dat de levering plus 7 + 2 = 9 dagen. Raakt
het product binnen die horizon op, dan komt het in het voorstel.

Het komt er **niet** in als:

- je de familie hebt uitgesloten ("Niet meer" of "Nooit voorstellen");
- de leverdag in een vakantie valt;
- je het in de laatste 180 dagen minder dan 3 keer kocht (te weinig om iets over te zeggen),
  tenzij je de familie hebt vastgepind;
- het pas na de horizon op is;
- je bij levering nog genoeg hebt voor de hele horizon.

## Stap 6: hoeveel?

> **aantal = afronden naar boven ( (verbruik × 9 dagen − voorraad bij levering) ÷ inhoud van één verpakking )**

Kruidenier bestelt het **voorkeursproduct** van de familie (instelbaar bij Families), of
anders het laatst gekochte. Er komen nooit meer dan 12 verpakkingen op één regel; dat is een
vangnet tegen rekenfouten.

## Rekenvoorbeeld: melk

Je koopt elke zondag 2 pakken AH Halfvolle melk van 1 liter. De laatste was zondag 4
oktober. Het is nu donderdag 8 oktober en je volgende levering is zondag 11 oktober.

| | Berekening | Uitkomst |
|---|---|---|
| Verbruik | 2000 ml ÷ 7 dagen, elke week hetzelfde | **286 ml per dag** |
| Op-datum | 4 okt + 2000 ÷ 286 = 4 okt + 7 dagen | **zondag 11 okt** |
| Horizon | 11 okt + 7 + 2 dagen | 20 okt |
| In het voorstel? | 11 okt valt vóór 20 okt | **ja** |
| Voorraad bij levering | 2000 − 286 × 7 | 0 ml |
| Nodig | 286 × 9 − 0 = 2571 ml, gedeeld door 1000 ml | 2,57, **dus 3 pakken** |

In het scherm staat: *AH Halfvolle melk, 3 stuks, "Voorraad op rond zo 11-10"*.

## Stap 7: hoe zeker is het?

| Betrouwbaarheid | Wanneer |
|---|---|
| **zeker** (high) | minstens 4 aankopen, de tijd tussen aankopen is regelmatig (variatie onder 35%), en je laatste aankoop is niet ouder dan 3× de gewone tussentijd |
| **redelijk zeker** (medium) | minstens 3 aankopen en niet te lang geleden |
| **nog onzeker** (low) | al het andere |

De melk uit het voorbeeld is "zeker": 6 aankopen, steeds precies 7 dagen ertussen.

Daaruit volgt de **tier** van een regel (`domain/tiers.py`):

| Tier | Wanneer | Wat ermee gebeurt |
|---|---|---|
| `auto` | gewoon aan de beurt, "zeker", en hoogstens 1,5× de gebruikelijke hoeveelheid per aankoop | mag de autopilot zelf in je bestelling zetten |
| `propose` | al het andere: onzeker, vastgepind maar zelden gekocht, zelf toegevoegd, of ongewoon veel | alleen een voorstel; jij beslist |

De melk: gebruikelijk 2 pakken, 1,5 × 2 = 3, en het voorstel is 3. Dus `auto`. Zou het
voorstel 4 pakken zijn, dan wordt het `propose`: dat is ongewoon veel, dus jij beslist.

## Stap 8: wat de knoppen doen

| Knop | Wat Kruidenier ermee doet |
|---|---|
| **+ (meer)** | De regel krijgt 1 verpakking meer, en het verbruik wordt voortaan 15% hoger geschat. |
| **− (minder)** | De regel krijgt 1 verpakking minder (bij 0 verdwijnt hij), en het verbruik wordt 15% lager geschat. Plus en min heffen elkaar precies op. |
| **Klopt** | Wordt vastgelegd; verder verandert er niets. |
| **Nog genoeg** | Kruidenier telt zoveel extra voorraad bij dat je tot na de volgende planhorizon gedekt bent, en de regel verdwijnt. Die extra voorraad vervalt zodra je het product weer koopt. |
| **Niet meer** | De familie wordt uitgesloten en komt niet meer in voorstellen. Terugdraaien kan bij Families. |

De correctie van "meer" en "minder" blijft tussen een kwart en het viervoudige van het
berekende verbruik.

## Stap 9: in je AH-bestelling zetten

Als je op "Zet in mijn AH-bestelling" drukt, of als de autopilot dat doet
(`domain/order_changes.py` en `services/push.py`):

- per product wordt het aantal **het hoogste van** wat al in je bestelling staat en wat het
  voorstel zegt. Kruidenier verlaagt nooit iets;
- staat alles er al in, dan verandert er niets. Twee keer drukken doet dus niets dubbel;
- **terugdraaien** zet alleen de producten terug die Kruidenier wijzigde, en alleen als
  niemand ze daarna nog aanpaste;
- niet binnen 15 minuten voor de sluitingstijd, niet boven het maximumbedrag per keer
  (standaard €150), en alleen als de eerstvolgende AH-levering nog dezelfde is.

## Stap 10: bonus

De bonustab (`services/bonus.py`) toont aanbiedingen in deze volgorde:

1. producten die je **zelf** koopt en die in de bonus zijn, het vaakst gekochte eerst;
2. **vergelijkbare** producten: Kruidenier zoekt elke ochtend op de naam van je 25 meest
   gekochte families ("Halfvolle melk") en bewaart wat daarvan in de bonus is, gesorteerd op
   hoe vaak je die familie koopt.

De korting is voorlopig de tekst van AH zelf ("25% korting"). Of een aanbieding écht
voordelig is, kan Kruidenier pas beoordelen met een paar weken eigen prijshistorie. Dat is
fase 3 in [SPEC.md](../SPEC.md).

## Waar staat wat?

| Regel | Bestand | Tests |
|---|---|---|
| Verpakkingen lezen | `app/domain/units.py` | `tests/domain/test_units.py` |
| Verbruik, op-datum, voorraad | `app/domain/consumption.py` | `tests/domain/test_consumption.py` |
| In voorstel en hoeveel | `app/domain/planning.py` | `tests/domain/test_planning.py` |
| Tiers | `app/domain/tiers.py` | `tests/domain/test_tiers.py` |
| Knoppen | `app/domain/feedback.py` | `tests/domain/test_feedback.py` |
| Naar AH en terug | `app/domain/order_changes.py` | `tests/domain/test_order_changes.py` |
| Families voorstellen | `app/domain/families.py` | `tests/domain/test_families.py` |
