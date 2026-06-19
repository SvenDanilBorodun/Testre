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
| AprilTag-Bogen (tag36h11, IDs 0–19) | `tools/generate_apriltags.py` → PDF | 1 |
| Farbige Würfel (rot/grün/blau/gelb), 25–35 mm | Bastelladen oder 3D-Druck | je 2 |
| Kleine Greif-Objekte (Spielzeug-Banane, Stifte …) | optional | je 1 |
| Eimer/Schüsseln zum Sortieren | Klassenraum | 2–3 |

> **Stand 2026-06-18 — wichtige Änderungen:**
> - Roboter Studio läuft **nur mit dem Follower-Arm**. In der Web-Oberfläche
>   schaltest du den **Leader-Arm ab** („Leader abschalten") — der Arm-Container
>   startet kurz neu (~15–20 s), dann steuert Roboter Studio den Follower allein.
>   Zum Aufnehmen/Teleoperieren später „Leader verbinden".
> - Die **Tischhöhe wird gemessen** (Schritt „Tisch vermessen"), nicht
>   angenommen — der Arm tippt selbst auf den Tisch.
> - Die **Kamera-Innenwerte** sind voreingestellt; der intrinsische Schritt ist
>   optional (nur zum Verfeinern).
> - Es wird **nur die Szenen-Kamera** verwendet (kein Greifer-ChArUco-Adapter).

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

### 4. Beleuchtung

Kalibrierung und Farbprofil sind beleuchtungsempfindlich:

- **Diffuses Licht**, KEIN direktes Sonnen-/Spotlicht (Reflexionen auf der
  Tafel werden sonst als Marker fehlerkannt).
- Beim Farbprofil: dasselbe Licht wie später beim Spielen. Ändert sich das
  Licht, das Farbprofil neu erfassen.

### 5. Erst-Test mit der Lehrkraft

Bevor die Klasse startet:

1. Umgebung starten, Web-Oberfläche öffnen, **Roboter-Studio-Tab**.
2. **„Leader abschalten"** klicken — der Arm-Container startet kurz neu.
3. Die Kalibrier-Schritte durchspielen:
   - **(Intrinsisch)** — voreingestellt, kann übersprungen werden (nur zum
     Verfeinern: Tafel aus verschiedenen Winkeln, Reprojektionsfehler < 1 px).
   - **Extrinsik** — die Tafel in den L-Jig legen, **ein Bild** erfassen →
     „Berechnen & speichern". Der Arm bewegt sich nicht.
   - **Tisch vermessen** — „Starten" (Arm wird weich), Greifer von Hand an
     **≥ 3 verteilten Stellen** auf den Tisch tippen + „Punkt erfassen",
     dann „Berechnen & speichern" (Arm wird wieder fest).
   - **Farbprofil** — je einen Würfel jeder Farbe mittig erfassen.
4. **Greif-Ring prüfen:** ein Ziel an den Innen- und Außenrand des markierten
   Rings legen und greifen lassen; falls „nicht erreichbar", den Ring anpassen.
   Einen bekannten Punkt pinnen und mit dem Maßband prüfen, dass der Arm
   wirklich dorthin fährt (sonst Extrinsik/Tisch-Schritt wiederholen).
5. Mini-Workflow `erkenne Farbe rot → nimm auf → lege ab bei A` testen.

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
- **Würfel werden an der falschen Stelle gegriffen**: meist die Kamera wurde
  bewegt (Extrinsik ungültig) ODER die Tafel lag nicht im Jig. Extrinsik +
  Tisch vermessen wiederholen. Steilere Kameraposition hilft.
- **Würfel werden nicht erkannt** trotz Farbprofil: Licht hat sich geändert —
  Farbprofil neu erfassen.
- **„Objekt-/Marker-Erkennung ist nicht verfügbar"**: das Erkennungsmodell ist
  im Image nicht vorhanden — Farb-Erkennung verwenden oder Image neu bauen.

Issues über das EduBotics-Repository melden.
