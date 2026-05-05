# Klassen-Kit für Roboter Studio

Material und Anleitung für die Lehrkraft, um eine OMX-F-Station für das
Roboter-Studio-Modul vorzubereiten. Aufbauzeit: ~15 Minuten.

## Inhalt des Kits

| Komponente | Quelle | Anzahl |
|---|---|---|
| ChArUco-Tafel (7×5, DICT_5X5_250) | `tools/generate_charuco.py` → PDF, ausdrucken | 1 |
| Foam-Board / starker Karton, mind. A4 | Schreibwarenladen | 1 |
| Schwarzer Sprühlack ODER schwarzer Filzstift | Hardware-Store | 1 |
| AprilTag-Bogen (tag36h11, IDs 0–19) | `tools/generate_apriltags.py` → PDF | 1 |
| Greifer-ChArUco-Adapter (3D-Druck) | `tools/gripper_charuco_adapter.stl` (im Repo) | 1 |
| Farbige Würfel (rot/grün/blau/gelb), 25–35 mm | Bastelladen oder 3D-Druck | je 2 |
| Eimer/Schüsseln zum Sortieren | Klassenraum | 2–3 |

## Aufbau

### 1. ChArUco-Tafel

```bash
cd Testre
python tools/generate_charuco.py --out classroom_kit/charuco.pdf
```

PDF im Adobe Reader öffnen → Drucken **mit „Tatsächliche Größe"** (KEIN
„An Seite anpassen"!). Die schwarz-weißen Quadrate müssen exakt 30 mm
breit sein — mit Lineal nachmessen, sonst geht jede spätere Längen-
messung schief.

Foam-Board zuschneiden, ChArUco-Bogen mit Sprühkleber faltenfrei
aufkleben. Über Nacht trocknen lassen. Eine in den Ständer geklemmte
Tafel funktioniert besser als eine Hand-getragene.

### 2. AprilTags

```bash
python tools/generate_apriltags.py --out classroom_kit/apriltags.pdf
```

20 Tags auf einem A4-Bogen, je 30 × 30 mm. Ausschneiden, jeden Tag auf
ein Stück Pappe (Schuhkarton-Stärke) kleben, sodass die Schülerinnen
und Schüler ihn umdrehen oder aufstellen können.

### 3. Greifer-Adapter

Das 3D-druckbare Modell `gripper_charuco_adapter.stl` befestigt einen
ChArUco-Patch (~3×3 Felder) am Greifer; die Szenen-Kamera benötigt
das, um die Eye-to-Base-Kalibrierung durchzuführen. Druck-Empfehlung:
PLA, 0.2 mm Layer, 20 % Infill. Der Adapter wird nach Abschluss der
Kalibrierung entfernt.

### 4. Beleuchtung

Kalibrierung und Farbprofil-Schritt sind beleuchtungsempfindlich.
Faustregel:

- **Diffuses Licht**, KEIN direktes Sonnen- oder Spotlicht (sonst werden
  Reflexionen auf der Tafel als zusätzliche Marker erkannt).
- Beim Farbprofil-Schritt: gleiches Licht, das später beim Sortier-Spiel
  herrschen wird. Wenn die Lampen wechseln, muss das Profil neu erfasst
  werden.

### 5. Erst-Test mit der Lehrkraft

Bevor die Klasse das Modul zum ersten Mal benutzt:

1. WebApp öffnen (`http://localhost:3000`), Roboter-Studio-Tab anklicken.
2. Schritt 1 (Greifer-Kamera intrinsisch) bis Schritt 5 (Farbprofil)
   einmal komplett durchspielen. Reprojektionsfehler sollten unter 0.5 px
   liegen. PARK ↔ TSAI-Abweichung sollte unter 2° sein.
3. Im Editor einen Mini-Workflow `Heimposition → warte 1 Sekunde →
   Greifer öffnen` testen.

Wenn etwas hakt, vor der Stunde lösen — nicht während 24 Schülerinnen
zuschauen.

## Bei Problemen

- **„Pose der Tafel konnte nicht bestimmt werden"**: ChArUco-Tafel ist
  nicht vollständig sichtbar oder gewölbt. Foam-Board prüfen.
- **„Aktion verletzt Gelenklimits"** beim Auto-Anfahren: TRAC-IK hat keine
  erreichbare Lösung gefunden. Tafel weiter weg vom Roboter platzieren.
- **Würfel werden nicht erkannt** trotz Farbprofil: das Licht hat sich
  geändert — Farbprofil-Schritt erneut durchlaufen.
- **„Workflow wurde gestoppt"** mitten in einer Aufgabe: Stopp-Knopf
  wurde gedrückt; Roboter fährt automatisch zurück zur Heimposition.

Tracking-Issues über GitHub melden:
<https://github.com/anthropics/claude-code/issues> (für allgemeine
Claude-Code-Themen) bzw. das EduBotics-Repository.
