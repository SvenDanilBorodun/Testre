# Servo-Station – Anleitung für den V1

Die Servo-Station kalibriert **jeden Servo des V1 einmal**, bevor er in einen Arm eingebaut wird: Sie
schreibt das Soll-Abbild des EEPROM (Nullpunkt, Positionsgrenzen, Schutzwerte) aus der einen
Servo-Spezifikation (`robotis_ai_setup/docker/open_manipulator/feetech_servo_spec.py`), beweist einen echten
Neustart des Servos, speichert einen Datensatz und lässt den Servo mit seiner Gelenkfarbe markieren. Der
Arm-Treiber prüft beim Start jedes Servos genau dieses Abbild; ein nicht kalibrierter oder verstellter Servo
startet nicht.

Bedient wird die Station immer von einem **Admin-Konto** (nur ein Admin darf Datensätze hochladen). Sie
läuft auf einem Windows-PC am Prüfplatz, mit einem Waveshare-Adapter (CH343, USB `1A86:55D3`), einem
12-V-Netzteil, der Kalibrierlehre und einem Handscanner.

> **Solange die Servo-Spezifikation nicht festgelegt ist** (vor P0 und dem Festlegen der Werte, Abschnitt 5),
> bietet die Station nur „Referenz lesen (P0)" an. Kein V1-Servo wird vorher kalibriert.

---

## 1. Einrichten (einmal pro PC)

**Python.** Python 3.12 von python.org installieren, dabei „tcl/tk and IDLE" angehakt lassen (die Station
braucht Tk für ihr Fenster). Dann in einer Eingabeaufforderung im Repository-Ordner:

```bat
py -3.12 -m pip install -r tools\servo_station\requirements-station.txt
```

Das installiert die einzige Abhängigkeit, `pyserial==3.5`.

**Adapter-Treiber.** Der Waveshare-Adapter (CH343) sollte mit dem Windows-eigenen CDC-Treiber als COM-Port
erscheinen; sonst den Treiber CH343SER von WCH installieren. *(ANNAHME bis P0: auf dem Prüf-PC noch nicht
ausprobiert.)*

**Scanner.** Den Handscanner auf das **deutsche Tastaturlayout** einstellen. Ein auf US-Layout eingestellter
Scanner tippt auf einem deutschen Windows `ß` statt `-` und vertauscht Y und Z: Die Station liest `ß` als `-`,
und die Kappen-Etiketten verwenden kein Y und kein Z – trotzdem gehört der Scanner auf das deutsche Layout.

**Cloud.** Über „Anmelden …" einmal die Supabase-Adresse (`https://<projekt>.supabase.co`) und den
öffentlichen Schlüssel (anon) eintragen; beides steht danach in `station.json`. Bei jeder Sitzung meldet sich
der Admin mit Benutzername und Passwort an. **Das Passwort wird nie gespeichert**, das Anmelde-Token nur im
Arbeitsspeicher; ein Dienstrollen-Schlüssel gehört nie auf den Prüf-PC.

## 2. Starten

```bat
tools\servo_station\Servo-Station.cmd
tools\servo_station\Servo-Station.cmd --data-dir D:\Station-Daten
```

Die Daten liegen sonst in `%LOCALAPPDATA%\EduBotics\Servo-Station`:

| Datei | Inhalt |
|---|---|
| `records\<Seriennummer>.json` | der Kalibrier-Datensatz – **erst er macht einen Servo „kalibriert"** |
| `records\history\` | frühere Datensätze eines neu kalibrierten Servos (nichts wird gelöscht) |
| `incoming\<Seriennummer>.json` | das Ergebnis des Wareneingangs |
| `reference\…` | P0-Referenzen, `p0_report.json`, `p0_pin_snippet.txt` |
| `calibrations.csv`, `incoming.csv`, `gauge_checks.csv` | fortlaufende Listen (öffnen per Doppelklick in Excel) |
| `station.json`, `station.log` | Cloud-Adresse, öffentlicher Schlüssel, Meister-Servos, letzter Port; Protokoll |

Nach dem Start: Anschluss (COM-Port) wählen, „Verbinden" drücken, Betriebsart wählen. **Der Scan ist der
Klick:** Eine gescannte Seriennummer startet den Ablauf der gewählten Betriebsart.

## 3. Etiketten

| Etikett | Form | Beispiel |
|---|---|---|
| Servo | `V1S-` und sechs Ziffern | `V1S-000123` |
| Kappe der Lehre | `KAPPE-J15-…` (Gelenke 1–5) oder `KAPPE-GR-…` (Greifer), danach 1–4 Zeichen aus A–X und 0–9 | `KAPPE-J15-A`, `KAPPE-GR-A` |

Jeder Servo gehört zu einem Gelenk und hat dessen Farbe:

| Gelenk | Servo-ID | Modell | Kappe | Farbe |
|---|---|---|---|---|
| Gelenk 1 – Basis | 1 | STS3215 | J15 | weiß |
| Gelenk 2 – Schulter | 2 | STS3250 | J15 | gelb |
| Gelenk 3 – Ellbogen | 3 | STS3250 | J15 | orange |
| Gelenk 4 – Handgelenk | 4 | STS3215 | J15 | grün |
| Gelenk 5 – Drehung | 5 | STS3215 | J15 | blau |
| Greifer | 6 | STS3215 | GR | rot |

## 4. Die Kalibrierlehre

**Aufbau.** Der Servo liegt mit dem Horn nach oben in einer **Wiege**: unten auf drei Auflagepunkten,
seitlich an zwei Anlagepunkten und am kurzen Gehäuseende an einem Anlagepunkt; zwei federnde Zungen drücken
ihn dagegen. Die Kabel gehen nach unten durch die Öffnung der Grundplatte. Auf das Horn kommt eine
**schwimmende Kappe**: Zwei Stifte greifen in zwei gegenüberliegende Hornlöcher (nie in das markierte Loch),
der Schlitz am langen Arm umfasst einen Stahlstift (Ø3 h6) in der Wiege und hält die Drehung fest. Der
Markierarm hat einen Schlitz, durch den **ein einziger Pinselstrich** Horn-Oberseite, Hornrand und den
Gehäusestreifen markiert. Die Kappe `J15` dient Gelenk 1–5, die Kappe `GR` dem Greifer.

**Erzeugen.** Die Teile werden mit OCP erzeugt, nicht gezeichnet. Einmal eine CAD-Umgebung anlegen
(Python 3.12 mit `cadquery-ocp==8.0.1.1.0` und `numpy`), dann im Repository:

```bash
python tools/servo_gauge/build_gauge.py --self-check --out tools/servo_gauge/out
```

Das schreibt `cradle.step/.stl`, `cap_J15.step/.stl`, `cap_GR.step/.stl` und `gauge_manifest.json` nach
`tools/servo_gauge/out/` (nicht im Repository). `--self-check` prüft vor dem Schreiben unter anderem, dass der
Schlitz Horn und Gehäusestreifen frei erreicht, dass die Kappe das Gehäuse nirgends berührt, dass die Stifte
auf dem Lochkreis sitzen und das markierte Loch frei bleibt und dass die Wiege das Gehäuse nur an ihren
Anlagepunkten berührt; bei einem Fehler wird nichts geschrieben. Den Stahlstift (Ø3 h6 × 28 mm) bis zum
Grund der Bohrung im Pfosten einpressen. **Vor einer Serien-Lehre misst P1** Gehäuse, Horn und Achslage am
echten Servo (die Maße stammen aus dem CAD).

**Täglich: „Lehre prüfen".** Für jede Kappe gibt es einen **Meister-Servo** (einen pro Kappentyp). Vor dem
Kalibrieren an jedem Tag: Betriebsart „Lehre prüfen", das Kappen-Etikett scannen, den Meister-Servo in die
Lehre setzen und seine Seriennummer scannen. Liest der Meister seinen Nullpunkt, ist die Lehre in Ordnung;
sonst „Lehre … verstellt" – dann nicht kalibrieren. Ohne bestandene Prüfung dieser Kappe am selben Tag
verweigert „Kalibrieren" den Start. Den Meister legt die erste „Lehre prüfen" mit einem bereits kalibrierten
Servo fest; solange noch keiner festgelegt ist, kalibriert die Station mit einem Hinweis.

## 5. P0 – Referenz lesen (vor dem ersten Kalibrieren)

P0 legt fest, was nur am echten Servo messbar ist: die Werkswerte, die Drehrichtung der Positionswerte und
die Fahrzeit, **je Modell** (STS3215 und STS3250). Dafür **mindestens drei fabrikneue Servos je Modell**
(ID 1, Positionsgrenzen 0/4095; jeder andere wird mit „Kein fabrikneuer Servo …" abgelehnt).

Betriebsart „Referenz lesen (P0)", pro Servo:

1. Servo allein anschließen, **„Referenz lesen"** – liest EEPROM und RAM, schreibt nichts.
2. Servo **in die Wiege** legen, Horn nach oben, ohne Kappe, **„Drehrichtung messen"** und das Horn langsam
   in Richtung des Pfeils auf der Wiege (gegen den Uhrzeigersinn, von oben gesehen) etwa eine Vierteldrehung
   drehen.
3. Servo **aus der Wiege** nehmen (frei drehbar), **„Fahrzeit messen"** – der Servo fährt 45° hin und zurück;
   nur der Arbeitsspeicher wird beschrieben, der Servo bleibt fabrikneu.

Nach allen Servos **„P0-Bericht"**: Er schreibt `reference/p0_report.json` und `reference/p0_pin_snippet.txt`.
Stimmen die Referenzen eines Modells nicht überein (Werkswerte, Drehrichtung) oder wurde ein Register einzeln
statt im Block gelesen, schlägt der Bericht nichts vor.

**Am Prüfstand außerdem (Eigentümer, je Modell, an einem Servo, der keine Referenz mehr wird):** Bleibt die
Position nach einem Nullpunkt, der über die Naht 0/4095 geht, innerhalb von 0–4095? Dazu mit dem
Hersteller-Programm FD einen Homing-Offset schreiben, der die gelesene Position über 4095 (oder unter 0)
schieben würde, und die Position lesen. Liest der Servo den Wert umgebrochen (0–4095), ist alles in Ordnung.
Liest er ihn **nicht** umgebrochen (über 4095 oder negativ), verweigert „Kalibrieren" später jeden Servo, dessen
Nullpunkt über die Naht geht (`OFFSET_CHECK`; bei Gelenk 2 und 3 etwa jeder vierte, beim Greifer etwa jeder
achte, bei Gelenk 1, 4 und 5 keiner), weil der Arm-Treiber einen solchen Servo beim Start ablehnen würde – das
vor dem Festlegen der Werte klären.

**Werte festlegen (Eigentümer).** Der Ausschnitt aus `p0_pin_snippet.txt` kommt in einen eigenen Commit auf
einem Zweig vom Release (nie gemergt, kein PR, kein Tag): `TICK_DIRECTION`, `FACTORY`,
`MOTION_TEST_WINDOW_MS` in `feetech_servo_spec.py`, `PROVISIONAL_NAMES` leer, die zwei Stolperdrähte in
`test_feetech_servo_spec.py` angepasst. Mit diesem Zweig läuft der Pilot-Arm (`docs/v1-hardware-bringup.md`,
„Pilot"); erst nach den Prüfungen dort (P1–P6, Vorzeichen, Max-Drehmoment, Überstrom) werden die Werte
endgültig festgelegt und veröffentlicht. **Danach sind sie eingefroren**: Jede Änderung ließe jeden
kalibrierten Servo beim Start scheitern.

## 6. Wareneingang (freiwillig)

Betriebsart „Wareneingang": Servo ohne Lehre, frei drehbar, anschließen und die Seriennummer scannen. Die
Station prüft Modell, Spannung, Temperatur und Fehlerbits und fährt einen Bewegungstest (45° hin und zurück,
Fahrzeit im Fenster des Modells). Steht die Welle zu nah am Rand des Messbereichs, verweigert sie den Test
(„Die Abtriebswelle steht zu nah an der Grenze …" – Welle ein Stück weiterdrehen und neu starten). Einen Servo,
der nicht im Positionsmodus steht, bewegt sie nicht („… nicht im Positionsmodus …"); „Kalibrieren" stellt den
Positionsmodus ein.

Der Wareneingang ist **freiwillig**: Ohne ihn kalibriert die Station mit einem Hinweis, der Datensatz vermerkt
`incoming_test: null`. **Ein nicht bestandener Wareneingang sperrt das Kalibrieren dieses Servos**, bis ein
neuer Wareneingang besteht. Gehört der Wareneingang einer Seriennummer zu einem anderen Servo (anderes Modell
oder andere Firmware als der Servo in der Lehre), verweigert „Kalibrieren" den Start, bevor es etwas schreibt:
Etikett neu scannen oder den Wareneingang wiederholen.

## 7. Kalibrieren

**Charge vorbereiten.** Betriebsart „Kalibrieren", die **Charge** (das Gelenk) wählen, den Namen des
Bedieners eintragen und das **Etikett der Kappe** scannen. Eine Kappe des falschen Typs für die Charge wird
abgelehnt (ein Greifer-Servo mit der Kappe J15 wäre um 8° falsch markiert).

**Pro Servo:**

1. Horn aufsetzen, Servo in die Wiege legen, Kappe aufsetzen (Stifte in die Hornlöcher, Schlitz über den
   Stahlstift), Servo-Kabel anstecken.
2. Seriennummer scannen. Die Station arbeitet die Schritte ab und zeigt jeden an: A „Servo suchen" (fragt jede
   Baudrate ab, damit ein zweiter Servo am Bus auffällt), B „Servo
   erkennen" (Modell der Charge, 12-V-Variante, Phase-Byte), C „Schreibschutz öffnen", C2 „Baudrate setzen",
   D „ID setzen", E „Abbildungs-Register", F „Werkswerte schreiben", G „Ruhe prüfen", H „Nullpunkt schreiben",
   I „Positionsgrenzen", J „Sperren und Abbild prüfen".
3. **K „Neustart-Probe"**: Die Station fordert auf, den **12-V-Stecker am Adapter** zu ziehen und wieder
   einzustecken (nicht das USB-Kabel). Sie erkennt einen echten Neustart daran, dass ein Register, das sie vorher
   verstellt hat, danach wieder den Wert aus dem EEPROM trägt; erst dann gilt das Abbild als gespeichert.
4. L „Datensatz speichern" – jetzt ist der Servo kalibriert.

   **Neu kalibrieren:** Ein vorhandener Datensatz dieser Seriennummer wandert direkt vor dem ersten
   Schreibzugriff auf den Servo nach `records\history\`. Bricht der Lauf danach ab, gilt der Servo als **nicht
   kalibriert** (er trägt womöglich schon den neuen Nullpunkt) – erneut kalibrieren. Bricht er vorher ab (etwa
   „zu warm"), bleibt der alte Datensatz gültig.
5. M „Markieren": **ein Pinselstrich in der Gelenkfarbe durch den Schlitz der Kappe**. Danach den nächsten
   Servo scannen oder Enter drücken.

## 8. Prüfen

Betriebsart „Prüfen": Servo in die Lehre setzen, Seriennummer scannen. Die Station vergleicht Position und
EEPROM-Abbild mit dem Datensatz („Prüfung bestanden" oder die Abweichungen). Gedacht für Rückläufer und
Stichproben.

## 9. Hochladen

Jeder Datensatz wird nach dem Speichern hochgeladen, wenn ein Admin angemeldet ist; sonst wartet er
(„… Datensätze warten auf das Hochladen") bis „Jetzt hochladen". Die Datenbank-Funktion
(`submit_servo_calibration_record`, Migration 052) nimmt nur Admin-Anmeldungen an und überschreibt nie einen
neueren Datensatz. Ein abgelehnter Datensatz wird nicht wieder gesendet und erscheint als „abgelehnt".
**Maßgeblich ist immer die Datei auf dem Prüf-PC**, die Cloud hält eine Kopie. Nach einer abgebrochenen
Neu-Kalibrierung zeigt die Cloud noch die frühere Kalibrierung, bis der nächste erfolgreiche Datensatz dieses
Servos hochgeladen ist.

## 10. Meldungen und was zu tun ist

| Meldung (Code) | Bedeutung | Was tun |
|---|---|---|
| Spezifikation nicht festgelegt (`SPEC_NOT_PINNED`) | vor P0 | nur „Referenz lesen" |
| Kein Servo gefunden (`NO_SERVO`) / mehr als ein Servo (`MULTIPLE`) | Bus leer oder nicht allein | Stecker und 12 V prüfen; nur EINEN Servo anschließen |
| Port lässt sich nicht öffnen (`PORT_OPEN_FAILED`) | COM-Port belegt oder weg (Einzelheiten in `station.log`) | anderes Programm schließen, Port neu wählen |
| Servo antwortet nicht mehr richtig (`BUS_ERROR`) | Störung mitten im Ablauf | Kabel und 12 V prüfen, neu starten |
| USB-Verbindung weg (`USB_LOST`) | USB statt 12 V gezogen | nur den 12-V-Stecker ziehen |
| Falsches Modell / 7,4-V-Servo / Max-Spannung / Phase-Byte | nicht der Servo der Charge | Servo aussortieren |
| Spannung, Temperatur, Fehlerbits | Netzteil oder Servo | Netzteil prüfen; abkühlen lassen |
| Servo bewegt sich (`UNSTABLE`) | Kappe sitzt nicht | Lehre richtig aufsetzen |
| Horn einen Zahn versetzt (`OFFSET_EDGE`) | Horn ungünstig verzahnt | Horn einen Zahn versetzt aufsetzen, neu starten |
| Nullpunkt stimmt nach dem Schreiben nicht (`OFFSET_CHECK`) | Horn verrutscht, oder der Servo liest die Position außerhalb von 0–4095 (P0) | Horn prüfen, neu kalibrieren; passiert es wieder, melden |
| Kein Neustart erkannt (`REPLUG_TIMEOUT`, `NO_REAL_RESTART`) | Strom nicht wirklich weg | 12-V-Stecker ziehen, 3 s warten, einstecken |
| Neustart nicht eindeutig (`RESTART_UNCLEAR`) | Register 48 zeigt keinen der erwarteten Werte | noch einmal; bleibt es so, notieren und melden (Prüfstand P2) – der Servo ist deswegen nicht defekt |
| EEPROM hat nicht gespeichert (`NOT_PERSISTED`) | Schreibschutz; der Servo meldet sich nach dem Neustart mit alter ID oder alter Baudrate | neu kalibrieren; passiert es wieder, aussortieren |
| Welle zu nah an der Grenze (`EDGE_PARKED`) | Bewegungstest würde den falschen Weg fahren | Welle ein Stück weiterdrehen |
| Nicht im Positionsmodus (`MOTION_MODE`) | Bewegungstest würde den Servo ohne Ende drehen | kein Bewegungstest; „Kalibrieren" stellt den Positionsmodus ein |
| Kein fabrikneuer Servo (`P0_NOT_FRESH`) | für P0 untauglich | nur fabrikneue Servos für die Referenz |
| Lehre nicht geprüft / verstellt | „Lehre prüfen" fehlt oder misslang | Meister prüfen; bei „verstellt" nicht kalibrieren |
| Wareneingang nicht bestanden (`INCOMING_FAILED`) | gesperrt | Servo aussortieren oder Wareneingang wiederholen |
| Wareneingang gehört zu einem anderen Servo (`INCOMING_MISMATCH`) | anderes Modell oder andere Firmware als der Servo in der Lehre | Etikett neu scannen oder Wareneingang wiederholen |

## 11. Die markierten Arm-Teile

Die Pfeile und Farbpunkte auf den gedruckten Teilen werden **erzeugt**, nicht in SolidWorks gezeichnet: ein
Pfeil (0,5 mm tief) auf dem Teil, das ein Gelenk dreht, zeigt auf das markierte Hornloch; ein Farbpunkt
(Ø4 × 0,4 mm) markiert die Tasche, in die der Servo seiner Farbe gehört.

```bash
python tools/v1_marked_parts/build_marked_parts.py --step <Baugruppe>.STEP --self-check --out tools/v1_marked_parts/out
```

Die STEP-Datei ist die Baugruppe des Eigentümers, **in der URDF-Nullstellung exportiert**; sie wird nur
gelesen. Der Lauf dauert etwa zwei Minuten und schreibt pro Teil `<teil>.step`, `<teil>.stl`,
`<teil>_marks.png` (jede Markierung von vorn) und `<teil>_iso.png` sowie `marked_parts_manifest.json` nach
`tools/v1_marked_parts/out/` (nicht im Repository; nach jedem neuen CAD-Export neu erzeugen).

- **„STEP nicht in der Nullstellung"** heißt: Die Baugruppe wurde mit verdrehtem Gelenk exportiert. Die
  Markierungen wären dann an falschen Hornlöchern – das Modell in der URDF-Nullstellung neu exportieren.
  Diese Prüfung lässt sich durch nichts übergehen.
- **„Markierung … weicht von EXPECTED ab"** heißt: Nach einer CAD-Änderung landet eine Markierung woanders.
  Erst **jedes Vorschaubild ansehen**, dann mit `--accept-placements` erneut laufen lassen und den
  ausgegebenen `EXPECTED`-Block in `tools/v1_marked_parts/marks_params.py` eintragen.
- Nach dem Druck die Vertiefungen in der Gelenkfarbe ausmalen (Pfeile und Punkte).

**Wo die Markierungen sitzen** (Stand der aktuellen Baugruppe; das markierte Hornloch liegt bei θ = 0 zum
langen Gehäuseende, beim Greifer bei +81,97°):

| Gelenk (Farbe) | Pfeil auf | Punkt auf | Gehäusemarke sichtbar |
|---|---|---|---|
| 1 – Basis (weiß) | `base_lid` und `link1_motorpart_new` | `Base_part` (Vorderwand) | unter dem Deckel – der Pfeil auf `base_lid` ersetzt sie |
| 2 – Schulter (gelb) | `link2part` | `link1_motorpart_new` | r ≈ 11,8–18,3 mm |
| 3 – Ellbogen (orange) | `link3_part` | `link2_motor_moutingpart` | r ≈ 11,8–17,3 mm |
| 4 – Handgelenk (grün) | `link5_part` – auf das Loch, das **leer bleibt** | `link4_part` | r ≈ 9,8–22,3 mm |
| 5 – Drehung (blau) | `last_link` | `link5_part` (Stützfläche neben Servo 5) | ganze Seite, bis `last_link` montiert ist |
| Greifer (rot) | Zahnrad 18T (gedruckt) | `last_link` | r ≈ 9,8–12,3 mm |

Genutzte Löcher der Kind-Teile: `link1_motorpart_new` 4 von 4, `link2part` 3 von 4, `link3_part` 3 von 4,
`link5_part` 3 von 4 (das markierte Loch bleibt frei), `last_link` 4 von 4, Zahnrad 2 von 4.

**Zusammenbau-Regel (für jedes Gelenk gleich):** den Servo mit seiner Farbe in die Tasche mit dem gleichen
Punkt setzen; das Horn drehen, bis seine Marke die Gehäusemarke trifft (Gelenk 1: den Pfeil auf dem Deckel);
das nächste Teil so aufsetzen, dass sein Pfeil auf das markierte Hornloch zeigt (Gelenk 4: auf das Loch, das
leer bleibt); festschrauben.
