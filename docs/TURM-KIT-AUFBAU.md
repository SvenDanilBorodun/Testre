# Turm-Kit — Aufbau und Inbetriebnahme

> **Für Lehrkräfte.** Diese Anleitung gilt ab **EduBotics 2.41.0** für den
> Windows-PC und den Orange Pi. Das Turm-Kit bringt die neue **Turm-Kamera**
> auf einem Kameraturm hinter dem Roboter, eine gedruckte **Szenen-Matte** und
> ein **Kalibrier-Ziel** zum Aufklipsen. Damit kalibriert sich Roboter Studio
> selbst: Es gibt kein „Tisch vermessen" und keine ChArUco-Tafel mehr.
> Zeitbedarf pro Station: etwa 20 Minuten beim ersten Mal.

## Inhalt des Kits

| Teil | Hinweis |
|---|---|
| Kameraturm, fertig montiert (Fuß, Aluminium-Profil, Kopf mit Turm-Kamera) | Der Fuß wird an die vorhandene Grundplatte des Roboters geschraubt; am Turm selbst ist nichts zu schrauben. Im Fuß stecken zwei Marker (Nummer 480 und 481) – nicht abziehen, nicht überkleben. |
| Zwei Schrauben M3 × 10 und ein Innensechskantschlüssel 2,5 mm | Für die beiden Laschen des Turm-Fußes. |
| Turm-Kamera (Innomaker U20CAM-1080P-S1) mit USB-Verlängerung | Die Verlängerung gehört nur an die Turm-Kamera. |
| Szenen-Matte (laminiert, passend zum Arm: V1 oder OMX) | Flach lagern, nicht rollen. |
| Vier ablösbare Klebepunkte | Für die Ecken der Matte. |
| Kalibrier-Ziel zum Aufklipsen (passend zum Arm) | Nur für den Arm-Check; danach wieder abnehmen. |

## Wann das Kit montiert wird — die Reihenfolge ist wichtig

1. **Das Kit kommt vor dem Update.** Bitte zunächst **nichts umbauen**: Bis der
   Roboter die neue Version zeigt, bleibt die **alte Szenen-Kamera montiert und
   nimmt wie bisher auf**.
2. **Zuerst das Update.** Den Turm erst montieren, wenn der Roboter **2.41.0
   oder neuer** hat. Woran man das erkennt: am Windows-PC steht im
   EduBotics-Fenster „Version 2.41.0" (oder höher), und in der Einrichtung gibt
   es das Kästchen „Turm-Kamera (Turm-Kit)"; am Orange Pi zeigt das
   System-Fenster (mit einem Lehrkräfte-Konto) bei der Kamera das Kästchen
   „Turm-Kamera (Turm-Kit)" – ältere Versionen haben es nicht. Zwischen Update und
   Umbau läuft Roboter Studio am echten Roboter nicht („Roboter Studio am
   echten Roboter braucht die Turm-Kamera (Turm-Kit) …"); der Simulator,
   Aufnahme, Training und Inferenz funktionieren wie gewohnt.
3. **Dann umbauen** (nächster Abschnitt) und einmal den **Arm-Check** fahren.

## Umbau

1. **Umgebung stoppen.**
2. **Die alte Szenen-Kamera und ihren Fuß abbauen.** Der Fuß des Kameraturms
   sitzt an derselben Stelle.
3. **Den Turm an die Grundplatte schrauben:** Der Turm kommt fertig montiert.
   Seinen Fuß mit den beiden Laschen in die Zähne am Rand der Grundplatte
   setzen und die beiden Schrauben M3 × 10 durch die Platte in die Laschen
   **fest anziehen**. Der Fuß liegt flach auf dem Tisch und wackelt nicht. Den
   Turm danach nicht mehr verschieben, an den Schrauben des Turms nichts lösen
   und **das Objektiv der Kamera nicht verdrehen** – es ist scharf gestellt und
   mit einer Markierung gesichert.
4. **Die Turm-Kamera an einen eigenen USB-Anschluss stecken** (siehe
   „USB-Anschlüsse" unten), mit der mitgelieferten Verlängerung.
5. **Die Szenen-Matte auflegen:** bis zum Anschlag an die Grundplatte
   schieben (der Ausschnitt der Matte umschließt die Platte), glatt auflegen
   und die Ecken mit den vier Klebepunkten fixieren. Die Matte darf keine
   Wellen werfen.
6. **„Turm-Kamera" auswählen:**
   - **Windows:** in der Einrichtung der EduBotics-App die Szenen-Kamera
     auswählen und **„Turm-Kamera (Turm-Kit)"** ankreuzen. Die App prüft die
     Kamera dabei kurz (1920 × 1080 Bildpunkte); passt sie nicht, wird das
     Häkchen wieder entfernt und die App sagt, warum.
   - **Orange Pi:** im System-Fenster bei der Kamera **„Turm-Kamera (Turm-Kit)"**
     ankreuzen. Das dürfen nur Lehrkräfte und Admins. Kann die Kamera das
     Turm-Format nicht liefern, lehnt „Umgebung starten" mit einer Meldung ab,
     die die Turm-Kamera nennt.
   - Die Einstellung gilt ab dem nächsten **„Umgebung starten"**.
7. **Umgebung starten**, die Web-Oberfläche öffnen, **Roboter Studio** wählen.
8. **Den Arm-Check fahren** (nächster Abschnitt).

## Der Arm-Check (einmal pro Kit)

Beim Öffnen von Roboter Studio prüft der Roboter die Kalibrierung von selbst
(die **Matten-Prüfung**, ohne Bewegung, bei jedem Start). Beim ersten Mal meldet
er „Dieser Roboter ist noch nicht eingemessen. Bitte den Arm-Check starten."

Vor dem Arm-Check:

- **Zuerst den Arm in die Grundstellung fahren:** in der Kalibrierung
  **„Arm in die Grundstellung fahren"** drücken (der Arm fährt sofort los).
  Der Arm-Check startet nur aus der Grundstellung.
- **Erst dann das Kalibrier-Ziel auf den Greifer aufklipsen** – nie vorher.
  Ein Ziel, das schon in einer anderen Haltung am Greifer steckt, kann auf dem
  Weg in die Grundstellung den Kameraturm streifen: Der Roboter fährt dann
  nicht los, solange die Kamera das Ziel sieht („Am Greifer steckt noch das
  Kalibrier-Ziel. Bitte abnehmen.").
- **Tisch frei räumen.**
- **30 cm hinter dem Roboter frei lassen.**
- **Abstand halten – der Arm bewegt sich von selbst.**

Dann **„Arm-Check starten"**. Nach einem Countdown fährt der Arm seine
Messposen selbst an; „Stopp" bricht jederzeit ab (dann wird nichts
gespeichert). Am Ende fährt er in die Grundstellung und bittet:
„Bitte das Kalibrier-Ziel jetzt vom Greifer abnehmen." Das Ziel abnehmen und
**„Kalibrier-Ziel ist abgenommen"** drücken. Danach zeigt die Kalibrierung
„Kalibrierung in Ordnung." und der Editor öffnet sich.

Einen neuen Arm-Check braucht es nur, wenn der Roboter danach fragt – zum
Beispiel nachdem die Kamera oder der Turm verdreht wurde. Meldet der Roboter,
dass die Kamera nicht mehr an ihrem Platz sitzt, zuerst den Turm wieder richtig
hinstellen: Solange er verschoben ist, bewegt Roboter Studio den echten Arm
nicht.

## Die Arbeitsfläche

- **Objekte nur in die markierte Greifzone der Matte legen.** Die Ecke neben
  dem Kameraturm ist **ausgespart**: Dort käme der Arm dem Turm zu nahe, und
  der Roboter würde das Greifen ablehnen.
- **30 cm hinter dem Roboter frei lassen**: Zwischen zwei Durchgängen einer
  „Solange sichtbar"-Schleife schwenkt der Arm in eine Beobachtungshaltung
  hinter sich, damit er der Kamera nicht im Bild steht.
- Fährt der Arm zu nah an den Turm, lehnt Roboter Studio die Bewegung auf
  Deutsch ab („Diese Bewegung käme dem Kameraturm zu nahe …"). Das ist
  gewollt. Beim OMX dreht sich das erste Gelenk in Roboter Studio nur noch bis
  100° nach jeder Seite.

## Nur V1: den Arm nach vorne legen

Die Grundstellung des V1 ist ab dieser Version **nach vorne geneigt** (wie beim
OMX). Liegt der Arm beim Start nach hinten zum Turm, würde ihn die Fahrt in die
Grundstellung zu nah an den Turm bringen. Dann zeigt die Startseite statt
„Roboter aktivieren" eine kurze Animation und den Hinweis:
„Der Arm liegt so, dass er beim Aktivieren dem Kameraturm zu nahe käme.
Lockere ihn, lege ihn nach vorne und setze ihn wieder fest – dann geht's los."

So geht's: **„Arm lockern"** → **„Ich halte ihn"** (der Arm wird weich – mit
einer Hand halten) → den Arm **nach vorne legen** → **„Arm festsetzen"** → jetzt
**„Roboter aktivieren"**. Den V1 immer nach vorne legen, nie senkrecht
aufrichten.

**Beim Stoppen** wird der Arm weich. Würde der V1 dabei nach hinten zum Turm
fallen, fragt „Stoppen" vorher nach (am Windows-PC und im System-Fenster des
Pi): den Arm zuerst in die Grundstellung fahren (Roboter Studio → Steuern →
Grundstellung) oder ihn beim Stoppen festhalten, dann „Trotzdem stoppen". Am
Windows-PC fragen ebenso das Schließen des EduBotics-Fensters und
„Arme scannen" vorher nach, denn beide stoppen die Umgebung.

## Nur OMX mit Leader-Arm: wo der Leader steht

Die Turm-Kamera sieht mehr vom Tisch als die alte Szenen-Kamera, auch die
rechte Tischseite. Damit der Leader-Arm und die Hand nicht mit aufgenommen
werden, steht der **Leader-Arm rechts vom Roboter, weit genug weg**: Sein Fuß
steht – vom Drehpunkt des Follower-Arms aus gemessen – **40 cm dahinter und
60 cm rechts daneben** oder **80 cm direkt rechts daneben**, oder noch weiter
weg. Wer den Leader führt, steht **hinter oder neben dem Leader**, nicht vor dem
Tisch.

## USB-Anschlüsse

- **Turm-Kamera und Greifer-Kamera an getrennte USB-Anschlüsse direkt am
  Gerät**, die zu verschiedenen USB-Controllern gehören – **nie gemeinsam an
  einen USB-Hub**. Zwei Kameras an einem Hub können sich gegenseitig die
  Bandbreite nehmen.
- **Orange Pi:** prüfen mit
  ```bash
  lsusb -t
  ```
  Die beiden Kameras müssen an **verschiedenen Bussen** hängen (verschiedene
  „Bus"-Zeilen).
- Die USB-Verlängerung des Kits gehört an die Turm-Kamera, nirgends sonst.

## Belichtung

Die Belichtung und der Weißabgleich der Kamera bleiben **automatisch**. Bitte
nichts an der Kamera einstellen. Für gleichmäßiges Licht sorgen: diffuses Licht,
kein direktes Sonnen- oder Spotlicht auf Matte und Markern.

## Aufnahmen und Training nach dem Umbau

- **Nach dem Umbau neue Datensätze aufnehmen und neu trainieren.** Die
  Turm-Kamera filmt den Tisch aus einem anderen Blickwinkel; ein Modell, das
  mit Aufnahmen der alten Szenen-Kamera trainiert wurde, sieht bei der Inferenz
  ein anderes Bild.
- **Ein Datensatz der alten Kamera lässt sich mit der Turm-Kamera nicht
  fortsetzen.** Der Roboter lehnt das ab und nennt den Grund; bitte einen
  **neuen Datensatz** (neuer Aufgabenname) beginnen. Auch im Tab „Daten"
  lassen sich Datensätze der beiden Kameras nicht zusammenführen.
- **Wie gut Modelle mit dem Blickwinkel der Turm-Kamera lernen, ist noch nicht
  belegt.** Eine Vergleichsstudie (alte Kamera gegen Turm-Kamera) folgt nach
  dieser Version.

## Zurück zur alten Kamera

> Wird die Turm-Kamera abgeschaltet, während der Turm noch montiert ist, nimmt der Roboter weiter mit der Turm-Kamera auf – nur ohne Kennzeichnung. Dann einen NEUEN Datensatz beginnen und keinen Datensatz fortsetzen, der vor dem Turm aufgenommen wurde – oder die alte Szenen-Kamera wieder anbauen.

„Turm-Kamera" wird abgeschaltet, indem man das Häkchen bei
„Turm-Kamera (Turm-Kit)" entfernt (Windows: Einrichtung; Orange Pi:
System-Fenster). Dasselbe gilt, wenn der Roboter auf die Version vor 2.41.0
zurückgesetzt wird.

## Bei Problemen

| Meldung | Was tun |
|---|---|
| „Die Turm-Kamera liefert kein Bild. Bitte das USB-Kabel der Kamera prüfen." | Kabel und Anschluss prüfen (eigener Anschluss, kein Hub), dann „Erneut prüfen". |
| „Das Kamerabild ist zu dunkel. Bitte das Licht einschalten." | Licht an, dann „Erneut prüfen". |
| „Am Greifer steckt noch das Kalibrier-Ziel. Bitte abnehmen." | Ziel abnehmen, dann „Erneut prüfen" (vor dem Arm-Check: erneut „Arm in die Grundstellung fahren"). |
| „Der Arm-Check startet nur aus der Grundstellung. …" | Ein schon aufgeklipstes Ziel abnehmen, „Arm in die Grundstellung fahren" drücken, dann das Ziel aufklipsen und „Arm-Check starten". |
| „Die Kamera wurde verdreht. Bitte den Arm-Check starten." | Kamera und Turm in Ruhe lassen und den Arm-Check neu fahren. |
| „Die Kamera sitzt nicht mehr an ihrem Platz am Turm oder ihr Objektiv wurde verdreht …" | Turm und Kopf prüfen und zurechtrücken; hilft das nicht, den Arm-Check fahren. |
| „Die beiden Marker neben dem Kameraturm liegen nicht wie vorgesehen …" | Prüfen, ob die Marker 480 und 481 sauber in ihren Taschen im Turm-Fuß kleben. |
| „Am Greifer steckt das Kalibrier-Ziel des anderen Roboters." | Das Ziel des richtigen Arms (V1 oder OMX) verwenden. |
| „Roboter Studio am echten Roboter braucht die Turm-Kamera (Turm-Kit) …" | „Turm-Kamera (Turm-Kit)" ankreuzen (nach dem Umbau) und die Umgebung neu starten. |
| Objekte werden nicht erkannt | Liegt das Objekt in der Greifzone? Ist der Marker oben gut sichtbar und nicht spiegelnd? |

Mehr zum Klassen-Kit (AprilTag-Bogen, Objekte, Erst-Test):
[`tools/classroom_kit_README.md`](../tools/classroom_kit_README.md).
Orange Pi: [`ORANGE_PI_TEACHER_RUNBOOK.md`](ORANGE_PI_TEACHER_RUNBOOK.md).
