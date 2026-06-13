## EduBotics 2.8.3

### Was ist neu
- **Schärfere Kamera-Vorschau bei der Rollenzuweisung.** Die Live-Vorschau im Einrichtungsfenster (Schritt C – Greifer-/Szenen-Kamera) wird wieder sauber und in der richtigen Größe dargestellt: Beide Kameras sind gleichzeitig nebeneinander sichtbar, sodass sich Greifer- und Szenen-Kamera weiterhin zuverlässig unterscheiden lassen.
- **Keine fälschlich erkannten Geräte mehr in der Kamera-Liste.** Netzwerk-Drucker und Scanner (z. B. ein HP-LaserJet-MFP im Schulnetz) tauchen nicht mehr in der Kamera-Auswahl auf und können die Zuordnung der beiden baugleichen Innomaker-Kameras nicht mehr durcheinanderbringen.
- **Schluss mit der zufälligen Trennung („Getrennt"), die eine erneute Roboter-Auswahl erzwang.** Die Verbindung zwischen Bedienoberfläche und Roboter bleibt jetzt stabil; nach einer kurzen Störung verbindet sich die App selbstständig wieder und merkt sich den ausgewählten Roboter – ein manuelles Neu-Auswählen auf der Startseite entfällt.
- **Aufnahme läuft nach einem Kollisions-Stopp nahtlos weiter.** Wenn die Sicherheits­abschaltung während einer Aufnahme auslöst, muss der Datensatz nicht mehr von vorne begonnen werden: Nach dem zweistufigen „Hindernis entfernen → Teleoperation fortsetzen" wird dieselbe Aufnahme automatisch fortgeführt; bereits aufgenommene Episoden bleiben erhalten.
- **Daten-Tab bleibt beim Bearbeiten großer Datensätze bedienbar.** Das Löschen/Zusammenführen von Datensätzen (insbesondere älterer Videos, die neu kodiert werden müssen) läuft jetzt im Hintergrund. Die Bedienoberfläche „friert" nicht mehr ein und bleibt während der Bearbeitung erreichbar.

### Upgrade
Bestehende Installationen aktualisieren sich beim nächsten Start von EduBotics automatisch. Es ist keine Neuinstallation nötig.
