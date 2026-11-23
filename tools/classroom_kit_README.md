# Klassen-Kit für Roboter Studio

Material und Anleitung für die Lehrkraft, um eine Station (OMX-F oder V1) für das
Roboter-Studio-Modul vorzubereiten. Aufbauzeit: ~15 Minuten.

## Inhalt des Kits

| Komponente | Quelle | Anzahl |
|---|---|---|
| **Turm-Kit** (Kameraturm mit Turm-Kamera, Szenen-Matte, Kalibrier-Ziel, Turm-Marker) | fertig geliefert (Druckdateien: Abschnitt „Turm-Kit“ unten) | 1 |
| AprilTag-Bogen (tag36h11, **exakt die Katalog-IDs**, 24 mm) | `tools/generate_apriltags.py` → PDF (liest den festen Objekt-Katalog) | 1 |
| Katalog-Objekte (aktuell: Würfel ~30 mm, je 1 Tag oben aufgeklebt) | Bastelladen oder 3D-Druck | je Tag-ID 1 |
| Eimer/Schüsseln zum Sortieren | Klassenraum | 2–3 |

> **Wichtig:**
> - Roboter Studio läuft **nur mit dem Follower-Arm**. In der Web-Oberfläche
>   schaltest du den **Leader-Arm ab** („Leader abschalten") — der Arm-Container
>   startet kurz neu (~15–20 s), dann steuert Roboter Studio den Follower allein.
>   Zum Aufnehmen/Teleoperieren später „Leader verbinden".
> - Kalibriert wird **mit dem Turm-Kit**: Beim Öffnen von Roboter Studio prüft
>   der Roboter die Kalibrierung von selbst, den **Arm-Check** startet man einmal
>   pro Kit (Roboter Studio → Kalibrierung). Die ChArUco-Tafel, die alten
>   Arbeits-Matten und der Schritt „Tisch vermessen“ sind entfallen.
> - Es wird **nur die Szenen-Kamera** verwendet (die Turm-Kamera).
> - Objekte werden **über AprilTags erkannt** (fester Objekt-Katalog) — es gibt
>   **kein Farbprofil** mehr; farbige Würfel ohne Tag werden nicht erkannt.

## Aufbau

### 1. Turm-Kit aufbauen und kalibrieren

Die ausführliche Anleitung mit Reihenfolge, USB-Anschlüssen und Fehlerbehebung
steht in [`docs/TURM-KIT-AUFBAU.md`](../docs/TURM-KIT-AUFBAU.md). Kurz:

- **Erst das Update, dann der Umbau:** Das Kit kommt vor dem Update. Bis der
  Roboter Version 2.41.0 hat, bleibt die alte Szenen-Kamera montiert und nimmt
  wie bisher auf.
- Roboter, Kameraturm und Szenen-Matte aufbauen (die alte Szenen-Kamera samt Fuß
  abbauen, der fertig montierte Turm kommt mit seinem Fuß an ihre Stelle an die
  Grundplatte, die beiden Laschen-Schrauben fest anziehen): die Matte bis
  zum Anschlag an die Grundplatte schieben, glatt auflegen und die Ecken mit
  den vier Klebepunkten fixieren. Die Turm-Kamera an einen eigenen USB-Anschluss, nie gemeinsam mit
  der Greifer-Kamera an einen Hub.
- In der Einrichtung die Turm-Kamera als Szenen-Kamera auswählen und
  **„Turm-Kamera (Turm-Kit)“** ankreuzen (am Orange Pi im System-Fenster).
- Roboter Studio öffnen: Die Kalibrierung wird von selbst geprüft. Beim ersten Mal
  **„Arm-Check starten“**: das Kalibrier-Ziel auf den Greifer klipsen, den Tisch
  frei räumen, 30 cm hinter dem Roboter frei lassen und Abstand halten — der Arm
  fährt seine Posen selbst an. Danach das Kalibrier-Ziel abnehmen und bestätigen.
- Wurde die Kamera verdreht, verlangt diese Prüfung einen neuen Arm-Check.
- Die Ecke der Greifzone neben dem Kameraturm ist auf der Matte ausgespart:
  dort keine Objekte hinlegen.
- **Nach dem Umbau neue Datensätze aufnehmen und neu trainieren:** Die
  Turm-Kamera filmt aus einem anderen Blickwinkel, ein Datensatz der alten Kamera
  lässt sich mit ihr nicht fortsetzen, und wie gut Modelle mit diesem Blickwinkel
  lernen, ist noch nicht belegt.

### 2. AprilTags drucken und aufkleben

```bash
cd Testre
python tools/generate_apriltags.py --out classroom_kit/apriltags.pdf
```

Der Bogen enthält **genau die Tag-IDs des festen Objekt-Katalogs** in der
richtigen Größe (Standard **24 mm**; per `EDUBOTICS_TAG_SIZE_M` änderbar —
Bogen und Rig-Einstellung müssen übereinstimmen). Mit „Tatsächliche Größe"
drucken und **nachmessen**.

Die Tag-IDs **400 bis 586** gehören der Kalibrierung (Matten, Turm-Marker,
Kalibrier-Ziele); `generate_apriltags.py` verweigert sie in jedem Modus, auch mit
`--allow-reserved`. Ihre Druckvorlagen stehen im Abschnitt „Turm-Kit:
Druckdateien“ unten.

- Beim Ausschneiden den **weißen Rand um das schwarze Quadrat stehen lassen**
  (die Erkennung braucht ihn).
- Jeden Tag **flach auf die OBERSEITE** des passenden Objekts kleben (nicht
  seitlich, nicht gewölbt).
- Bei nicht-runden/nicht-würfelförmigen Objekten: den Tag auf **jeder Kopie
  gleich ausgerichtet** aufkleben — der Greifer richtet sich nach dem Tag aus.

### 3. Beleuchtung

Kalibrierung und Marker-Erkennung sind beleuchtungsempfindlich:

- **Diffuses Licht**, KEIN direktes Sonnen-/Spotlicht (Reflexionen auf der
  Matte oder den Tags stören die Erkennung).

### 4. Erst-Test mit der Lehrkraft

Bevor die Klasse startet:

1. Umgebung starten, Web-Oberfläche öffnen, **Roboter-Studio-Tab**.
2. **„Leader abschalten"** klicken — der Arm-Container startet kurz neu.
3. Die Kalibrierung prüfen: Roboter Studio meldet „Kalibrierung in Ordnung.“ —
   sonst den Arm-Check starten (Schritt 1).
4. **Greifzone prüfen:** ein Katalog-Objekt an den Innen- und Außenrand der
   markierten Greifzone legen und greifen lassen. Einen bekannten Punkt pinnen
   und mit dem Maßband prüfen, dass der Arm wirklich dorthin fährt (sonst den
   Arm-Check wiederholen).
5. Mini-Workflow `Greife Würfel → lege ab bei A` testen — dabei einmal den
   Würfel **gedreht** hinlegen und prüfen, dass der Greifer die Drehung
   mitmacht.

Wenn etwas hakt, vor der Stunde lösen — nicht während 24 Schülerinnen zuschauen.

## Bei Problemen

- **„… nicht erreichbar / zu nah / zu weit"**: Objekt außerhalb der Greifzone.
  In die markierte Greifzone der Matte legen.
- **Objekte werden an der falschen Stelle gegriffen**: meist wurde die Kamera
  oder der Turm bewegt. Den Arm-Check wiederholen.
- **Objekte werden nicht erkannt**: Tag verdeckt/spiegelnd, weißer Rand beim
  Ausschneiden entfernt, oder falsche Tag-ID (der Bogen muss die Katalog-IDs
  enthalten — Bogen mit `tools/generate_apriltags.py` neu drucken).
- **„Ausrichtung konnte nicht bestimmt werden"**: Tag nicht flach auf der
  Oberseite, gewölbt, oder die gedruckte Tag-Größe passt nicht zur
  Rig-Einstellung (`EDUBOTICS_TAG_SIZE_M`) — Tag neu drucken/aufkleben.

Issues über das EduBotics-Repository melden.

## Turm-Kit: Druckdateien und Bestelltext (für die Kit-Herstellung)

Die Turm-Kits werden mit fertig gedruckten Matten und Etiketten ausgeliefert;
Schulen drucken davon nichts selbst, und ein gekachelter Druck auf
Büro-Druckern ist kein vorgesehener Weg. Alle Maße kommen aus dem
Fixture-Vertrag (`physical_ai_server/workflow/scene_fixture.py`); die Dateien
liegen in `classroom_kit/`:

| Datei | Inhalt | erzeugt mit |
|---|---|---|
| `scene_mat_v1.pdf` | Szenen-Matte V1, Seite = Blatt 470 × 830 mm | `python3 tools/generate_scene_mat.py --arm v1` |
| `scene_mat_omx.pdf` | Szenen-Matte OMX, Seite = Blatt 380 × 660 mm | `python3 tools/generate_scene_mat.py --arm omx` |
| `calibration_target_v1.pdf` | Etikett des Kalibrier-Ziels V1 (80 × 80 mm, Tags 500–508), A4 | `python3 tools/generate_calibration_target.py --arm v1` |
| `calibration_target_omx.pdf` | Etikett des Kalibrier-Ziels OMX (80 × 80 mm, Tags 520–528), A4 | `python3 tools/generate_calibration_target.py --arm omx` |
| `fixture_tags.pdf` | Turm-Marker 480 und 481 (je 35 × 35 mm, Maß der Taschen im Turm-Fuß), A4 | `python3 tools/generate_calibration_target.py --fixture-tags` |

Jedes Blatt nennt die Kit-Kennung `tower_v1` und die ersten 8 Zeichen der
Prüfsumme des Vertrags. Ändert sich der Vertrag (zum Beispiel, wenn der
gemessene OMX-Fingerspitzen-Versatz eingetragen wird), alle fünf Dateien neu
erzeugen. Die Matten werden erst nach der Freigabe der Probedrucke (Rig-Gate
RG-MAT) in Auftrag gegeben: Der ausgesparte Turm-Bereich der Arbeitsfläche darf
bis dahin nur noch wachsen.

**Bestelltext für die Druckerei – Matten** (je Kit eine Matte des passenden Arms):

> Plott der beiliegenden PDF-Datei im Maßstab 100 % – die Seitengröße ist das
> Endformat (V1 470 × 830 mm, OMX 380 × 660 mm); nicht skalieren, nicht
> kacheln. Papier 160–200 g/m², beidseitig matt laminiert, 2 × 125 µm
> (Gesamtdicke 0,45 mm ± 0,05 mm). Auf die Außenkontur zuschneiden,
> einschließlich des rechteckigen Ausschnitts für die Grundplatte entlang der
> gedruckten Schnittlinie. Vor der Auslieferung die beiden 100-mm-Marken (eine
> waagerecht, eine senkrecht) mit dem Lineal nachmessen: je 100 mm ± 0,3 mm.
> Flach lagern und versenden, nicht rollen.

**Bestelltext für die Druckerei – Etiketten** (je Kit ein Kalibrier-Ziel des
passenden Arms und ein Satz Turm-Marker):

> Druck der beiliegenden PDF-Dateien im Maßstab 100 % auf mattes
> Etikettenmaterial (nicht glänzend), etwa 0,3 mm dick (die Taschen im
> Turm-Fuß sind 0,3 mm tief). Entlang der gedruckten Schnittlinien
> ausschneiden. Die beiden 100-mm-Marken je Blatt mit dem Lineal nachmessen:
> je 100 mm ± 0,3 mm.

Beim Zusammenbau: das Ziel-Etikett so auf die Platte kleben, dass die Kante mit
dem kurzen Balken zur Klammer zeigt; die Turm-Marker so in ihre Taschen kleben,
dass der kurze Balken nach vorn zeigt (in die Blickrichtung des Roboters, zur
Matte) – Marker 481 in die Tasche näher am Turm-Profil.
