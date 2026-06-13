## EduBotics 2.8.2

### Was ist neu
- **Zuverlässigere Image-Downloads im Klassenzimmer.** Die Docker-Images werden jetzt von der GitHub Container Registry (GHCR) geladen statt von Docker Hub. Damit entfällt die Docker-Hub-Sperre („429 – Too Many Requests"), die auftrat, wenn viele Schüler-PCs im selben Schulnetz gleichzeitig aktualisierten. Ist GHCR einmal nicht erreichbar, wird automatisch auf Docker Hub zurückgegriffen.
- **Nur noch Online-Installation.** Der frühere Offline-Installer (zwei Dateien: `EduBotics_Setup_Full.exe` + `.dat`) entfällt. Bitte den normalen Online-Installer **`EduBotics_Setup.exe`** verwenden. Für die Erstinstallation wird eine Internetverbindung benötigt.

### Hinweis für die Schul-IT
Bitte sicherstellen, dass die Firewall diese Hosts erlaubt:
`ghcr.io`, `pkg-containers.githubusercontent.com`, `*.githubusercontent.com`.
(`pkg-containers.githubusercontent.com` liefert die Image-Schichten – ohne diesen Host bleibt der Download hängen.)

### Upgrade
Bestehende Installationen aktualisieren sich beim nächsten Start von EduBotics automatisch. Es ist keine Neuinstallation nötig.
