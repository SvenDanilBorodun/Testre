## EduBotics 2.12.2

### Was ist neu
- **Bearbeitete oder erweiterte Datensätze landen jetzt zuverlässig im Training.** Wer einen Datensatz nachträglich ändert (Episoden löschen) oder um weitere Aufnahmen ergänzt und erneut hochlädt, dessen Training nutzt ab sofort wirklich die aktualisierte Fassung. Bisher konnte das Training in solchen Fällen still die alte Version verwenden.
- **Klare Rückmeldung beim Bearbeiten von Datensätzen.** Nach dem Löschen von Episoden erinnert EduBotics jetzt in verständlichem Deutsch daran, den Datensatz vor dem nächsten Training erneut hochzuladen – Bearbeitungen bleiben zunächst nur lokal.
- **Die Inferenz startet nur noch mit sinnvollen Einstellungen.** Passt die eingestellte Taktrate nicht zum trainierten Modell oder fehlt eine benötigte Angabe, erklärt EduBotics das jetzt verständlich vor dem Start – statt ohne erkennbaren Grund abzubrechen.
- **Robuster bei Störungen während der Aufnahme.** Tritt beim Aufnehmen ein Problem auf (zum Beispiel voller Speicher), stoppt EduBotics die Aufnahme mit einer verständlichen Meldung, statt die Verbindung zum Roboter zu verlieren.
- **Roboter Studio: benannte Objekte funktionieren auf jedem Klassen-PC gleich.** Der Objektkatalog (zum Beispiel „Würfel") ist jetzt auf allen Rechnern identisch. Ein gespeichertes Programm mit benannten Objekten läuft dadurch an jedem Platz gleich zuverlässig.

### Upgrade
Bestehende Installationen aktualisieren sich beim nächsten Start von EduBotics automatisch. Es ist keine Neuinstallation nötig.
