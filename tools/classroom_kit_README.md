# Klassen-Kit für Roboter Studio

Material und Anleitung für die Lehrkraft, um eine OMX-F-Station für das
Roboter-Studio-Modul vorzubereiten. Aufbauzeit: ~15 Minuten.

## Inhalt des Kits

| Komponente | Quelle | Anzahl |
|---|---|---|
| ChArUco-Tafel (7×5, DICT_5X5_250) | `tools/generate_charuco.py` → PDF, ausdrucken | 1 |
| Foam-Board / starker Karton, mind. A4 | Schreibwarenladen | 1 |
| **Ausricht-Winkel (L-Jig)** für die Tafel | 3D-Druck (`tools/` STL) **oder** Papp-Winkel | 1 |
| **Arbeits-Matte** mit Greifbereich + Tafel-Platz | ausdrucken/zeichnen (siehe unten) | 1 |
| AprilTag-Bogen (tag36h11, **exakt die Katalog-IDs**, 24 mm) | `tools/generate_apriltags.py` → PDF (liest den festen Objekt-Katalog) | 1 |
| Katalog-Objekte (aktuell: Würfel ~30 mm, je 1 Tag oben aufgeklebt) | Bastelladen oder 3D-Druck | je Tag-ID 1 |
| Eimer/Schüsseln zum Sortieren | Klassenraum | 2–3 |

> **Stand 2026-07-10 — wichtige Änderungen:**
> - Roboter Studio läuft **nur mit dem Follower-Arm**. In der Web-Oberfläche
>   schaltest du den **Leader-Arm ab** („Leader abschalten") — der Arm-Container
>   startet kurz neu (~15–20 s), dann steuert Roboter Studio den Follower allein.
>   Zum Aufnehmen/Teleoperieren später „Leader verbinden".
> - Die **Tischhöhe wird gemessen** (Schritt „Tisch vermessen"), nicht
>   angenommen — der Arm tippt selbst auf den Tisch.
> - Der **intrinsische Schritt ist Pflicht** (20 Bilder pro Rig) — es gibt
>   keine voreingestellten Kamera-Innenwerte mehr.
> - Es wird **nur die Szenen-Kamera** verwendet (kein Greifer-ChArUco-Adapter).
> - Objekte werden **über AprilTags erkannt** (fester Objekt-Katalog) — es gibt
>   **kein Farbprofil** mehr; farbige Würfel ohne Tag werden nicht erkannt.

## Aufbau

### 1. Szenen-Kamera montieren (wichtig!)

Die Szenen-Kamera muss **fest** und **steil von oben** auf den Arbeitsbereich
schauen:

- Auf einem Stativ/Klemmarm montieren, Blickwinkel **40–70° von der Waagerechten**
  (steiler = besser), ca. **30–60 cm** über dem Tisch.
- **Fest mit derselben Grundplatte wie der Roboter** — die Kamera darf sich
  nach der Kalibrierung **nicht mehr relativ zum Arm bewegen** (ein Stoß macht
  die Kalibrierung ungültig).
- Das Sichtfeld muss den **Greifbereich** (siehe Matte) **und den Tafel-Platz**
  abdecken.

> Warum steil von oben? Roboter Studio greift Objekte senkrecht von oben. Je
> steiler die Kamera schaut, desto kleiner der Versatz zwischen Objekt-Oberseite
> und Greifpunkt — desto genauer der Griff.

### 2. ChArUco-Tafel + Ausricht-Winkel

```bash
cd Testre
python tools/generate_charuco.py --out classroom_kit/charuco.pdf
```

PDF im Adobe Reader öffnen → Drucken **mit „Tatsächliche Größe"** (KEIN
„An Seite anpassen"!). Die Quadrate müssen exakt **30 mm** breit sein — mit
Lineal nachmessen. Bogen faltenfrei auf das Foam-Board kleben.

Der **L-Jig** (ein rechtwinkliger Anschlag) sorgt dafür, dass die Tafel
**immer gleich, gerade und am selben Platz** liegt — das ist der ganze
Genauigkeits-Trick: nicht „ungefähr mit Klebeband", sondern **mechanisch
festgelegt**. Den Jig am markierten Tafel-Platz fixieren (siehe Matte). Ohne
3D-Drucker: einen stabilen Papp-/Holz-Winkel verwenden, der die Tafel an zwei
Kanten anschlägt.

### 3. Arbeits-Matte (Greifbereich + Tafel-Platz)

Auf eine abwischbare Matte (oder direkt auf den Tisch mit Klebeband):

- **Greif-Ring markieren:** ein Ring etwa **10–28 cm vor/um die Roboterbasis**
  (genaue Werte beim Erst-Test prüfen, siehe unten). Objekte **nur in diesen
  Ring** legen — außerhalb meldet der Roboter „nicht erreichbar".
- **Tafel-Platz markieren:** der Anschlag des L-Jigs, standardmäßig **ca. 18 cm
  vor der Roboterbasis**, **mittig** auf der Vorwärts-Mittellinie. Die Tafel
  liegt flach, bedruckte Seite nach oben, so dass die **lange Kante (7 Felder)
  vom Roboter weg verläuft** (vorne–hinten) und die **kurze Kante (5 Felder)
  quer liegt** (links–rechts). Die **Ursprungs-Ecke (Marker 0)** zeigt nach
  **vorne links** — dem Roboter am nächsten, auf seiner linken Seite. Über
  `EDUBOTICS_BOARD_ORIGIN_X_M`, `EDUBOTICS_BOARD_ORIGIN_Y_M` und (falls der Jig
  die Tafel gedreht hält) `EDUBOTICS_BOARD_YAW_DEG` anpassbar.

#### 3b. Arbeits-Matte für den **EduBotics 6-Achs** (edu6_studio)

> ⚠️ **Der OMX-Greif-Ring aus Schritt 3 ist für diesen Arm FALSCH.** Der 6-Achs-Arm
> dreht sich am Gelenk 1 nur **±90°**, sein Arbeitsbereich ist also ein **Halbkreis
> vor dem Arm**, kein Ring — und er ist deutlich kleiner. Eine Ring-Matte lädt dazu
> ein, Objekte hinter den Arm zu legen, wo er sie nie erreicht.

Matte drucken (maßstabsgetreu, alle Maße kommen direkt aus dem Code):

```bash
cd Testre
python tools/generate_edu6_mat.py --out /tmp/edu6_mat.pdf
```

Das Werkzeug gibt die Maße aus und prüft die gedruckte Außengrenze gegen den
echten IK-Löser, bevor es zeichnet. Für Arm EDU6-0001:

| Größe | Wert | warum |
|---|---|---|
| Fächer | **180°** (±90°) | Grenze von Gelenk 1 |
| grün — **Greifen** bis | **20,8 cm** | letzter Radius, den der Löser auf **Greifhöhe** (1,5 cm über dem Tisch, bei einem 3-cm-Würfel) noch löst |
| orange (gestrichelt) — **Ablegen** bis | **17,4 cm** | „lege ab" öffnet den Greifer **5 cm über** dem Tisch (damit die Backen über einen Behälterrand kommen), und der Arbeitsbereich wird mit der Höhe kleiner |
| rot — innere Grenze | **9,0 cm** | **Sperrbereich** — hier rechnet der Löser noch, aber der Arm faltet sich auf sich selbst (Eigenkollision) |
| Blattgröße | **43,6 × 21,8 cm** | **passt NICHT auf A3** (A3 quer ist nur 42,0 cm breit) — auf **A2** drucken, oder auf zwei A4/A3-Blättern und an der Mittellinie zusammenkleben |
| Mittelpunkt des Fächers | **21,3 mm** vor dem Basis-Ursprung | die **Drehachse von Gelenk 1**, nicht die Mitte des Basis-Klotzes |

- Den Arm so stellen, dass seine Drehachse auf dem **schwarzen Punkt an der
  geraden Kante** steht.
- Objekte zum **Greifen** nur zwischen dem **roten und dem grünen** Bogen ablegen.
- Ziele zum **Ablegen** („lege ab bei …", „Position merken") müssen **innerhalb
  des orangenen** Bogens liegen. Zwischen orange und grün gilt: **aufnehmen ja,
  ablegen nein** — der Roboter meldet dort „Position außerhalb des
  Arbeitsbereichs". Das sind die äußersten ~3,4 cm des Greifbereichs.
- **Im 100-%-Maßstab drucken** („Tatsächliche Größe", nie „an Seite anpassen") und
  danach die aufgedruckte **100-mm-Marke mit einem Lineal nachmessen**. Ein
  skalierter Druck verschiebt jeden Radius und der Sperrbereich passt nicht mehr.

Die **ChArUco-Tafel** liegt wie in Schritt 2/3 beschrieben; ihre Ursprungs-Ecke
ist auf der Matte als `ChArUco-Ecke` markiert. Die Tafel selbst ist 21 × 15 cm
und reicht damit **über den Rand der Matte hinaus** — das ist richtig, sie ist
eine Referenz für die **Kamera**, kein Greifziel.

> **Noch offen (Rig-Test):** die Standard-Tafelposition (`18 cm` vor der Basis)
> stammt vom OMX. Beim 6-Achs-Arm ist der Arbeitsbereich kleiner, die Tafel liegt
> also relativ weiter außen. Falls die Szenen-Kamera nicht **beide** gut im Bild
> hat (Arbeitsbereich *und* Tafel), `EDUBOTICS_BOARD_ORIGIN_X_M` /
> `EDUBOTICS_BOARD_ORIGIN_Y_M` einmal pro Klassenraum anpassen — kein Code-Umbau.

### 4. AprilTags drucken und aufkleben

```bash
cd Testre
python tools/generate_apriltags.py --out classroom_kit/apriltags.pdf
```

Der Bogen enthält **genau die Tag-IDs des festen Objekt-Katalogs** in der
richtigen Größe (Standard **24 mm**; per `EDUBOTICS_TAG_SIZE_M` änderbar —
Bogen und Rig-Einstellung müssen übereinstimmen). Mit „Tatsächliche Größe"
drucken und **nachmessen**.

- Beim Ausschneiden den **weißen Rand um das schwarze Quadrat stehen lassen**
  (die Erkennung braucht ihn).
- Jeden Tag **flach auf die OBERSEITE** des passenden Objekts kleben (nicht
  seitlich, nicht gewölbt).
- Bei nicht-runden/nicht-würfelförmigen Objekten: den Tag auf **jeder Kopie
  gleich ausgerichtet** aufkleben — der Greifer richtet sich nach dem Tag aus.

### 5. Beleuchtung

Kalibrierung und Marker-Erkennung sind beleuchtungsempfindlich:

- **Diffuses Licht**, KEIN direktes Sonnen-/Spotlicht (Reflexionen auf der
  Tafel oder den Tags stören die Erkennung).

### 6. Erst-Test mit der Lehrkraft

Bevor die Klasse startet:

1. Umgebung starten, Web-Oberfläche öffnen, **Roboter-Studio-Tab**.
2. **„Leader abschalten"** klicken — der Arm-Container startet kurz neu.
3. Die Kalibrier-Schritte durchspielen:
   - **Intrinsisch** — Pflicht (20 Bilder, Tafel aus verschiedenen Winkeln,
     Reprojektionsfehler < 1 px).
   - **Extrinsik** — die Tafel in den L-Jig legen, **ein Bild** erfassen →
     „Berechnen & speichern". Der Arm bewegt sich nicht.
   - **Tisch vermessen** — „Starten" (Arm wird weich), Greifer von Hand an
     **≥ 3 verteilten Stellen** auf den Tisch tippen + „Punkt erfassen",
     dann „Berechnen & speichern" (Arm wird wieder fest).
   - **(Optional) Genauigkeit prüfen** — bekannte Punkte auflegen und die
     XY-/Dreh-Korrektur speichern.
4. **Greif-Ring prüfen:** ein Katalog-Objekt an den Innen- und Außenrand des
   markierten Rings legen und greifen lassen; falls „nicht erreichbar", den
   Ring anpassen. Einen bekannten Punkt pinnen und mit dem Maßband prüfen,
   dass der Arm wirklich dorthin fährt (sonst Extrinsik/Tisch-Schritt
   wiederholen).
5. Mini-Workflow `Greife Würfel → lege ab bei A` testen — dabei einmal den
   Würfel **gedreht** hinlegen und prüfen, dass der Greifer die Drehung
   mitmacht.

Wenn etwas hakt, vor der Stunde lösen — nicht während 24 Schülerinnen zuschauen.

## Bei Problemen

- **„Pose der Tafel konnte nicht bestimmt werden" / „Reprojektionsfehler zu
  hoch"**: Tafel nicht vollständig sichtbar, gewölbt oder schräg. Foam-Board
  prüfen, Tafel flach in den Jig, ganze Tafel ins Bild.
- **„Die Kamera scheint nicht über dem Tisch zu liegen"**: Tafel liegt nicht
  flach mit der bedruckten Seite nach oben am Jig. Korrekt hinlegen, erneut.
- **„Gemessene Tischhöhe passt nicht zur Kamerakalibrierung"**: Extrinsik und
  Tisch-Messung widersprechen sich — die Extrinsik (Schritt davor) wiederholen.
- **„… nicht erreichbar / zu nah / zu weit"**: Objekt außerhalb des Greif-Rings.
  In den markierten Ring legen.
- **Objekte werden an der falschen Stelle gegriffen**: meist die Kamera wurde
  bewegt (Extrinsik ungültig) ODER die Tafel lag nicht im Jig. Extrinsik +
  Tisch vermessen wiederholen. Steilere Kameraposition hilft.
- **Objekte werden nicht erkannt**: Tag verdeckt/spiegelnd, weißer Rand beim
  Ausschneiden entfernt, oder falsche Tag-ID (der Bogen muss die Katalog-IDs
  enthalten — Bogen mit `tools/generate_apriltags.py` neu drucken).
- **„Ausrichtung konnte nicht bestimmt werden"**: Tag nicht flach auf der
  Oberseite, gewölbt, oder die gedruckte Tag-Größe passt nicht zur
  Rig-Einstellung (`EDUBOTICS_TAG_SIZE_M`) — Tag neu drucken/aufkleben.

Issues über das EduBotics-Repository melden.
