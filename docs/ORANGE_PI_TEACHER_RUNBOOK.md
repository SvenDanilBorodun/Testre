# EduBotics Orange Pi 5 Pro — Lehrkräfte-Handbuch

> **Für Lehrkräfte und die Schul-IT.** Diese Anleitung bringt eine
> EduBotics-Roboterstation auf dem Orange Pi 5 Pro von „ausgepackt" bis
> „Datensatz aufgenommen und hochgeladen" — komplett **im Browser**, ohne
> Windows-PC und ohne WSL2 auf der Roboterseite. Zeit pro Station nach der
> ersten Einrichtung: **ca. 10 Minuten**.

## Was das ergibt

Eine vollständige Schüler-Roboterstation: Leader- + Follower-Arm und **beide
USB-Kameras** stecken direkt am Pi. Der Schüler öffnet im Browser
`http://edubotics-NN.local/`, durchläuft denselben Einrichtungsassistenten wie
die Windows-App (jetzt als **System-Fenster** im Browser), nimmt Demos auf,
trainiert in der Cloud und nutzt optional Roboter Studio. Der Pi ersetzt den
Windows-PC + WSL2 auf der Roboterseite vollständig.

**Inferenz** (das Ausführen trainierter Modelle) läuft weiterhin **nur auf dem
Klassen-Jetson** — die Inferenz-Tab-Verbindung ist unverändert (siehe
[`JETSON_DEPLOY.md`](JETSON_DEPLOY.md)). Der Pi **nimmt auf und trainiert**;
der Jetson **führt aus**.

## Vor dem Start — Netzwerk klären

Schulnetze sind der häufigste Stolperstein. **Bitte zuerst die
[Netzwerk-Anleitung für die IT](ORANGE_PI_IT_NETZWERK.md) an die IT geben** —
sie ist als fertiges Ticket formuliert (VLAN, Ports 80/8080/9090/9091,
Egress-Liste, TLS-/Proxy-Ausnahmen, MAC-Registrierung).

> Für den **Browser** genügt inzwischen **Port 80** allein: Web-Oberfläche,
> rosbridge und Videostrom laufen am Pi über denselben Proxy. 8080 und 9090
> stehen nur noch für Diagnose/Rückfallweg offen, 9091 gehört zum
> Klassenraum-Jetson. Bleibt im Ticket also nur 80 übrig, funktioniert der
> Unterricht trotzdem vollständig.

Für den **Pilot** genügt oft **Tier 2** aus jener Anleitung: ein eigener,
mitgebrachter Router/Access-Point. Damit läuft alles sofort, unabhängig von
der Ticket-Warteschlange.

## Hardware-Checkliste

| Teil | Hinweise |
|---|---|
| Orange Pi 5 Pro **8 GB** | Die 4-GB-Variante ist zu klein. |
| Offizielles Netzteil (5 V / 5 A USB-C) | Ein schwaches Netzteil führt zu Unterspannungs-Resets mitten in der Aufnahme. |
| Aktive Kühlung (Kühlkörper + Lüfter) | Für dauerhaften 2-Kamera-Encode nötig (Throttling sonst). |
| eMMC-Modul **oder** NVMe-SSD | Schneller + zuverlässiger als microSD. |
| Roboterarm(e) — **je nach Robotertyp** | „OMX – Voll": 1× OpenMANIPULATOR-X **Leader** + 1× **Follower** (beide OpenRB-150-Boards). „OMX – Roboter Studio": **nur der Follower**. „EduBotics 6-Achs": **ein** Arm, und zwar ein **Feetech**-Arm am Waveshare-Adapter — **kein** OpenRB-150. |
| USB-Kameras — **je nach Robotertyp** | „OMX – Voll": 2× (Greifer + Szene). „OMX – Roboter Studio": 1× (Szene) genügt, 2× möglich. „EduBotics 6-Achs": **1× (Szene)** — dieser Typ kennt nur die Szenen-Kamera. |
| Kabelgebundenes Ethernet | Empfohlen — stabiler als WLAN, und `.local` funktioniert zuverlässiger. |

**USB-Aufteilung** (Bandbreite): eine Kamera an den **USB3**-Port, eine an den
**eigenständigen USB2**-Port, den/die Arm(e) an die **Hub-Ports**.

## Schritt 1 — OS flashen

1. Ein **gepinntes, bekannt-gutes** Armbian-Image für den Orange Pi 5 Pro (oder
   das offizielle Orange-Pi-Ubuntu-22.04-BSP-Image) auf eMMC/NVMe schreiben
   (z. B. mit `balenaEtcher` oder `dd`). *Das archivierte
   `Joshua-Riek/ubuntu-rockchip`-Projekt nicht verwenden.*
2. Pi einmal booten, Grundeinrichtung (Sprache, Benutzer) durchlaufen,
   Internetzugang sicherstellen.

> **Golden Image für die Flotte:** Eine einzige Station wie unten
> provisionieren, dann `sudo ./setup.sh --prepare-golden` ausführen und das
> Image abziehen. Jeder Klon vergibt sich beim **ersten Boot** automatisch eine
> **eindeutige** Kennung (Hostname `edubotics-NN`, ROS-Domain, frische
> Schlüssel) — doppelte `.local`-Namen sind das Einzige, was mDNS nicht
> übersteht, deshalb wird `NN` **abgeleitet, nie von Hand vergeben**.

## Schritt 2 — Agent installieren (provisionieren)

Mit eingesteckten Armen + Kameras, als root:

```bash
# Optional: abweichende Cloud-API-URL setzen (sonst Produktions-Standard).
export EDUBOTICS_UPDATE_API_URL="https://scintillating-empathy-production-1068.up.railway.app"

# Provisionierung als root.
sudo ./robotis_ai_setup/pi_agent/setup.sh
```

Das Skript:

1. installiert gepinntes **Docker** + Compose-Plugin,
2. installiert `jq` / `qrencode` / `v4l-utils` / **avahi** / Python + die
   Agent-Abhängigkeiten,
3. legt die **udev-Regel** für die ROBOTIS-Boards an (Rechte-Grundlage — die
   Leader/Follower-Rolle wird beim Scannen im Assistenten bestimmt, nicht per
   Symlink),
4. aktiviert **zram**-Swap (auf dem 8-GB-Board Pflicht),
5. vergibt den mDNS-Hostnamen **`edubotics-NN`** (aus der Maschinen-ID
   abgeleitet),
6. legt den Agenten unter `/opt/edubotics` ab (inkl. Compose-Datei),
7. wählt ein **freies `ros_net`-Subnetz** (prüft `ip route` auf Überlappung)
   und schreibt die verwaltete `/etc/edubotics/.env`,
8. installiert + startet die systemd-Dienste `edubotics-pi` und
   `edubotics-pi-firstboot`,
9. zieht die **`-opi`-Container-Images** (GHCR zuerst, Docker Hub als
   Ausweichweg),
10. druckt am Ende das **Gehäuse-Etikett**.

## Schritt 3 — Etikett aufs Gehäuse

Das Skript druckt drei Felder plus QR-Code:

```
============================================================
  EduBotics Orange Pi — Etikett/QR für das Gehäuse
============================================================

  Hostname:  edubotics-04823.local
  MAC (LAN): dc:a6:32:11:22:33
  IP:        ________________   (von der IT reserviert eintragen)

  Aufruf im Browser:  http://edubotics-04823.local/   (oder http://<IP>/)
============================================================
```

- **Hostname** + **MAC** aufs Etikett kleben. Beides genügt der IT für eine
  **DHCP-Reservierung** und die **NAC/802.1X-Freigabe** — ganz ohne den Pi
  anzufassen (siehe [Netzwerk-Anleitung](ORANGE_PI_IT_NETZWERK.md)).
- Das **IP-Feld bleibt zunächst leer**. Sobald die IT eine feste IP reserviert
  hat, liest sie die Lehrkraft in der **Pi-IP-Anzeige** des System-Fensters ab
  und trägt sie ins Etikett ein. Danach gilt `http://<IP>/` dauerhaft.

## Schritt 4 — Im Browser verbinden

Vom Schüler-PC im selben (Robotik-)Netz:

1. `http://edubotics-NN.local/` öffnen.
   - Findet der PC den Namen nicht (verwaltete PCs haben oft `EnableMDNS=0`
     oder eine VLAN-Grenze): stattdessen **`http://<IP>/`** vom Etikett
     verwenden. Der **IP-Weg funktioniert immer**, wo das Netz routet.
2. Der **„Netzwerk-Check"** im System-Fenster prüft von der Station aus die
   typischen Schulnetz-Fallen. Es sind **fünf** Zeilen: „Cloud-Dienst
   erreichbar", „Container-Registry erreichbar", „Hugging Face erreichbar",
   „Zertifikate echt (keine TLS-Inspektion)" und „Systemuhr synchron (NTP)".
   Grün zeigt nur diesen Namen; **schlägt eine Zeile fehl, erscheint an ihrer
   Stelle der ausführliche Hinweis mit der Ursache und dem, was die IT
   freigeben muss** — es gibt keine kurze rote Fehlermeldung. Die TLS-Zeile
   meldet dann „TLS-Inspektion erkannt (Zertifikat neu signiert) — bricht
   Pulls/Uploads/Updater." → siehe Netzwerk-Anleitung, Ausnahme.

## Schritt 5 — Einrichtungsassistent (System-Fenster)

Derselbe Ablauf wie in der Windows-App, jetzt im Browser:

| Schritt | Aktion |
|---|---|
| **Modus — Robotertyp** | Auswählen, **welcher Roboter** an diesem Pi hängt: „OMX – Voll" (beide Arme), „OMX – Roboter Studio (nur Follower)" oder „EduBotics 6-Achs – Roboter Studio". Diese Wahl bestimmt alles Weitere — nach welcher Arm-Familie der Scan sucht, ob es überhaupt einen Leader-Arm gibt, welche Kamera-Rollen angeboten werden und was „Umgebung starten" verlangt. **Zuerst** den Robotertyp wählen, **dann** scannen: ein Wechsel über die Arm-Familien-Grenze (OMX ↔ EduBotics 6-Achs) macht einen vorhandenen Scan ungültig (siehe Fehlerbehebung). Das Auswahlfeld ist gesperrt, solange die **Roboter-Umgebung läuft**, im **Cloud-Modus** (dort spielt der Robotertyp keine Rolle), während die Auswahl gerade **gespeichert** wird und während ein **Update läuft**. |
| **A/B — Arm(e) scannen** | „OMX – Voll": **beide** Arme scannen, Leader/Follower werden per Servo-ID erkannt. Bei den beiden Follower-only-Typen heißt der Schritt „**Arm scannen**" und es gibt **keine Leader-Kachel** — ein Arm genügt. Die Ports werden in jedem Fall stabil gespeichert. |
| **C — Kameras** | Kameras scannen, Rollen zuweisen, Vorschau prüfen. **Welche Rollen zur Auswahl stehen, hängt am Robotertyp**: bei den OMX-Typen **Greifer** und **Szene**, bei „EduBotics 6-Achs" **nur Szene** — dieser Typ kennt serverseitig keine Greifer-Kamera, und eine so benannte Kamera würde ein Thema veröffentlichen, das niemand liest (der Start meldete trotzdem Erfolg). Auf einem Roboter-Studio-Kit mit **nur einer** Kamera ist **Szene** die richtige Rolle (die Perzeption hängt am Rollen-Namen). Der Pi rät hier nichts: eine Kamera **ohne** zugewiesene Rolle wird nicht gespeichert. |
| **D — HF-Token** | Hugging-Face-Token einmal eintragen (`✓ Token gespeichert`). Überlebt Regenerate + „Daten zurücksetzen". |
| **Umgebung starten** | Bringt die Roboter-Container hoch (der Manager/die Web-Oberfläche läuft **immer**). |

> **Zwei-Tier-Lebenszyklus:** Die Web-Oberfläche (Manager) ist **immer an** —
> sonst gäbe es auf einem frisch gebooteten Pi keine Seite mit dem
> „Umgebung starten"-Knopf. Die **Roboter-Container** kommen erst mit
> „Umgebung starten" hoch (der Dynamixel-Bus muss vorher frei sein).

## Schritt 6 — Aufnehmen & trainieren

> Dieser Schritt gilt für **„OMX – Voll"**. Auf den Follower-only-Typen blendet
> die Web-Oberfläche **Aufnahme, Daten und Training** aus (es gibt keinen
> Leader-Arm zum Teleoperieren) — dort bleiben **Roboter Studio** und, bei
> „OMX – Roboter Studio", **Inferenz**. Der **EduBotics 6-Achs**-Typ kann
> ausschließlich Roboter Studio: der Inferenz-Tab ist sichtbar, ein Start wird
> aber auf Deutsch abgelehnt.
>
> **Inferenz braucht am Pi grundsätzlich einen Klassenraum-Jetson.** Ein
> lokaler Start scheitert an der GPU — der Orange Pi hat keine. Die Absage im
> Protokoll ist für Windows geschrieben und nennt NVIDIA-Treiber, `nvidia-smi`
> in der WSL2-Distro und `docker-compose.gpu.yml`: **diese drei Hinweise am Pi
> ignorieren**, es gibt dort nichts davon. Der Weg zur Inferenz führt über den
> Jetson, den der Inferenz-Tab genau deshalb immer anbietet.

1. **Aufnahme**-Tab: Demos mit Leader→Follower-Teleop aufnehmen (inkl.
   Kollisions-Nothalt, unverändert).
2. **Roboter Studio** (optional): Blockly-Programme, AprilTag-Perzeption,
   manuelle Armsteuerung.
3. **Training**: Datensatz in die Cloud hochladen und ein Modell trainieren
   (bestehender Ablauf, unverändert).
4. **Inferenz**: den **Follower-Arm + 2 Kameras** an den **Klassen-Jetson**
   umstecken und über den Inferenz-Tab verbinden (siehe
   [`JETSON_DEPLOY.md`](JETSON_DEPLOY.md)). Der Pi selbst führt keine Modelle
   aus.

## Updates

- **Container-Images**: über den digest-geprüften Auto-Pull im „Update"-Gate
  des System-Fensters (GHCR zuerst, Hub als Ausweichweg). Die Web-Oberfläche
  lädt danach kurz neu und heilt sich selbst.
- **Agent**: über ein **SHA-256-geprüftes** Release-Tarball
  (`edubotics-pi-agent.tar.gz`), das die Cloud über `/version` bekanntgibt
  (Felder `pi_agent_download_url` / `pi_agent_sha256`).
- **OS**: über `unattended-upgrades`.

## Protokoll & Status (auf dem Pi)

```bash
sudo systemctl status edubotics-pi
sudo journalctl -u edubotics-pi -f            # Live-Agent-Log
docker compose -f /opt/edubotics/docker-compose.opi.yml ps
cat /var/lib/edubotics/.last_image_pull.json  # letzter Image-Update
```

## Fehlerbehebung

| Symptom | Wo nachsehen |
|---|---|
| `edubotics-NN.local` nicht erreichbar | `http://<IP>/` vom Etikett verwenden. mDNS ist auf verwalteten PCs oft deaktiviert; siehe [Netzwerk-Anleitung](ORANGE_PI_IT_NETZWERK.md). |
| Web-App lädt, aber der Roboter bleibt „Getrennt" | Die Oberfläche zeigt dazu „Verbindung zum Roboter blockiert? Netzwerk-Anleitung prüfen". **Am Pi läuft alles über Port 80** — Web-Oberfläche, rosbridge und Videostrom gehen durch denselben Proxy; 9090 und 8080 muss die IT **nicht** freigeben. Zuerst prüfen: Läuft die Roboter-Umgebung überhaupt („Umgebung starten")? Startet sie noch? Erst danach ans Netz denken — bleibt es dabei, bricht eine Middlebox die WebSocket-Verbindung (Netzwerk-Anleitung). |
| „Updates schlagen mit Zertifikatfehler fehl" | TLS-Inspektion → Ausnahme nötig; der „Netzwerk-Check" bestätigt es. |
| Kamerabild schwarz, UI sonst da | Kamera in Schritt C **ohne Rolle** gespeichert, oder die Roboter-Umgebung läuft nicht. **Nicht** die Ports: der Videostrom läuft am Pi über Port 80, nicht über 8080. |
| Nach einem Wechsel des **Robotertyps**: „Die gescannten Arme gehören zu einem anderen Robotertyp." | **Kein Kabelproblem.** Ein Wechsel über die Arm-Familien-Grenze (OMX ↔ EduBotics 6-Achs) macht den vorhandenen Scan ungültig — dieselbe Meldung steht auch im Protokoll. Einfach mit dem neuen Typ **neu scannen**. |
| Arme werden nicht erkannt | **Zuerst:** Passt der oben gewählte **Robotertyp** zum angesteckten Arm? Der Scan sucht ausschließlich nach der Arm-Familie dieses Typs, findet den falschen also gar nicht — dann nennt die Meldung den Robotertyp, nicht das Kabel. Sonst: Arm(e) eingesteckt? Läuft bereits eine Umgebung (belegt den seriellen Bus)? Erst „Stoppen", dann neu scannen. |
| Unterspannungs-Resets in der Aufnahme | Offizielles 5 V/5 A-Netzteil verwenden; Kernel-Log auf `undervoltage`/`reset` prüfen. |
| Agent-Dienst startet nicht | `journalctl -u edubotics-pi -n 50`. |

## Sicherheitshinweis (bewusste Entscheidung)

Die Steuer-Ports (80/8080/9090) sind **offen im LAN, ohne Authentifizierung** —
wer im selben Netzsegment ist, kann jeden Arm steuern und jede Kamera sehen.
Deshalb ist das **eigene Robotik-VLAN** (oder der eigene Pilot-Router) keine
Kür, sondern die Sicherheitsgrenze. Mildernde Maßnahmen, die trotzdem
mitkommen: der **`EDUBOTICS_LAN_OPEN=0`**-Schalter (Kiosk: alle Ports zurück
auf `127.0.0.1`) und die **Host/Origin-Prüfung** auf zustandsändernden
Management-Endpunkten (blockt bösartige Web-Seiten, lässt LAN-Nachbarn — das
akzeptierte Restrisiko — unberührt).
