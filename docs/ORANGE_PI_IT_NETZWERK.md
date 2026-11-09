# EduBotics Orange Pi — Netzwerk-Anleitung für die IT

> **Für die Schul-IT / Netzwerkadministration.** Diese Seite ist als
> **Arbeitsauftrag (Ticket)** gedacht: sie enthält alles, was zum Freischalten
> der EduBotics-Roboterstationen (Orange Pi 5 Pro) nötig ist — Porttabelle,
> Ziel-Adressliste (Egress), Ausnahmen für TLS-Inspektion und Proxy sowie die
> MAC-/NAC-Registrierung. Bearbeitungszeit
> ca. 20–30 Minuten. Kein Rückruf nötig — bei Fragen genügt der Absatz
> „Warum diese Ausnahmen?" am Ende.

## Kurzfassung (TL;DR für das Ticket)

1. **Eigenes Robotik-VLAN** (z. B. `ROBOTIK`) mit **abgeschalteter
   Client-Isolation**. Die Pis gehören **kabelgebunden** hinein, die
   Schüler-PCs erreichen es über eine passende SSID **oder** über die
   Firewall-Freigabe in Schritt 3.
2. **DHCP-Reservierungen** für jeden Pi anhand der **MAC-Adresse auf dem
   Gehäuse-Etikett**. Die reservierte IP kommt zurück aufs Etikett.
3. **Eingehend** (Schüler-PC → Robotik-VLAN): nur TCP **80**.
4. **Ausgehend** (Robotik-VLAN → Internet): TCP **443** zur Adressliste unten
   plus **NTP (UDP 123)**.
5. **Zwei Ausnahmen:** **keine TLS-Inspektion** für dieses VLAN und
   **Proxy-/PAC-Bypass** für das Robotik-Subnetz und `*.local`.
6. Bei **NAC / 802.1X**: die MAC-Adressen der Pis als **Headless-Geräte**
   freigeben.

Ist ein eigenes VLAN kurzfristig nicht möglich, siehe **Tier 2** (eigener
Robotik-Router) weiter unten — der Pilot kann sofort und unabhängig von der
Ticket-Warteschlange starten.

---

## Warum überhaupt ein eigenes Netzsegment?

Die EduBotics-Roboterstation bindet ihren Steuer-Port 80 (Web-Oberfläche,
Robotersteuerung und Kamerabild über denselben Proxy) **offen ins LAN, ohne
Authentifizierung** — das ist eine bewusste
Produktentscheidung für den Klasseneinsatz. Das eigene Robotik-VLAN ist
daher **gleichzeitig die Sicherheitsgrenze und die Lösung für die
mDNS-Erreichbarkeit** (`.local`-Namen scheitern zuverlässig genau dort, wo
VLAN-Grenzen oder Client-Isolation im Weg stehen). Beides kommt mit
demselben Schritt.

---

## Tier 1 — dediziertes Robotik-VLAN (empfohlener Dauerbetrieb)

Der einzige Aufbau, der **alle** Fehlerklassen behebt und zugleich die
Sicherheitsgrenze bildet. Konkreter Arbeitsauftrag:

### 1. VLAN anlegen

- Neues VLAN, z. B. `ROBOTIK`. Kabelports für **jeden Pi**.
- Passende **SSID** in dasselbe VLAN gemappt für die Schüler-PCs — **oder**
  die Schüler-PCs bleiben in ihrem VLAN und erhalten die Firewall-Freigabe aus
  Schritt 3.
- **Client-/AP-Isolation im Robotik-VLAN: AUS** (sonst erreichen die
  Schüler-PCs die Pis nicht).

### 2. DHCP-Reservierungen

Für **jeden Pi** eine feste IP per DHCP-Reservierung,
gebunden an die **MAC-Adresse vom Gehäuse-Etikett** (`setup.sh` druckt
Hostname + MAC beim Provisionieren). Die reservierte IP bitte **auf das
Etikett in das freie IP-Feld** eintragen — danach gilt `http://<IP>/`
dauerhaft, auch wenn mDNS auf gehärteten Schul-PCs deaktiviert ist.

### 3. Eingehende Freigaben (nur falls die Schüler-PCs in einem anderen VLAN sind)

Nur dieser Port, sonst nichts eingehend:

| Freigeben | Port | Zweck |
|---|---|---|
| TCP | **80** | Web-App (Einrichtungsassistent + Bedienoberfläche), Robotersteuerung (rosbridge) und Kamerabild — alles über denselben Proxy |

Richtung: **Schüler-PC → Robotik-VLAN**. Keine Freigabe in die Gegenrichtung
nötig. Die Ports **8080** und **9090** sind am Pi nur noch lokal
(`127.0.0.1`) gebunden und **nicht** freizugeben.

### 4. Ausgehende Freigaben (Robotik-VLAN → Internet)

**TCP 443** zu den folgenden Zielen, plus **NTP (UDP 123)**. „Pi" heißt: die
Station selbst baut die Verbindung auf; „Browser" heißt: der Browser auf dem
Schüler- oder Lehrkräfte-PC (nur nötig, wenn diese PCs ebenfalls im
Robotik-VLAN hängen — sonst gilt die Zeile für ihr eigenes Netz).

**Laufender Betrieb**

| Zweck | Wer | Ziel-Hosts (FQDN) |
|---|---|---|
| EduBotics Cloud-API (Update-Abfrage `/version`, Training, Modelle) | Pi, Browser | `scintillating-empathy-production-1068.up.railway.app` |
| Agent-Update (das geprüfte Paket aus dem Download-Kanal) | Pi | `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` (zusammen: `github.com`, `*.githubusercontent.com`) |
| Container-Images (GHCR) | Pi | `ghcr.io`, `pkg-containers.githubusercontent.com` |
| Container-Images, Ausweichweg (Docker Hub) | Pi | `registry-1.docker.io`, `auth.docker.io`, `production.cloudfront.docker.com`, `production.cloudflare.docker.com` (Docker wechselt das Download-Netz; beide freigeben) |
| Datensätze / Modelle (Hugging Face) | Pi | `huggingface.co`, `*.huggingface.co`, `*.hf.co` (Downloads laufen über `*.cdn.hf.co` und `cas-server.xethub.hf.co`) |
| Cloud-Inferenz (Modell auf einer Cloud-GPU in der EU) | Pi | `svendanilborodun--edubotics-inference-cloudinference.eu-west.modal.direct` (zusammen: `*.modal.direct`) |
| Anmeldung / Datenbank (Supabase, auch WebSocket) | Browser | `fnnbysrjkfugsqzwcksd.supabase.co` |
| Lehrer-Web-Dashboard (Railway) | Browser | `teacher-web-production.up.railway.app` |
| OS-Sicherheitsupdates (`unattended-upgrades`) | Pi | `ports.ubuntu.com`, `*.armbian.com` |
| Zeitsynchronisation | Pi | **UDP 123** (NTP) — beliebiger erreichbarer NTP-Server |

**Nur bei der Einrichtung** (`setup.sh`; danach nicht mehr nötig)

| Zweck | Ziel-Hosts (FQDN) |
|---|---|
| Docker Engine (Paketquelle) | `download.docker.com` |
| System-Pakete (`apt-get`) | `ports.ubuntu.com` bzw. die Paketquellen des Images (`*.armbian.com`) |
| Python-Pakete des Agenten (`pip3`) | `pypi.org`, `files.pythonhosted.org` |
| Agent-Paket und `/version` | wie oben (Cloud-API, `github.com`, `*.githubusercontent.com`) |

Gemessen am 9. November 2026 (Weiterleitungen beim Download von GitHub, GHCR,
Docker Hub und Hugging Face). Die Anbieter können ihre Download-Netze ändern;
schlägt der „Netzwerk-Check" oder ein Image-Pull trotz Freigabe fehl, zeigt das
Protokoll des Pi den blockierten Namen.

> Wo nur IP-Freigaben möglich sind: Die Railway-/Supabase-/GHCR-/HF-Hosts
> liegen hinter CDNs mit wechselnden IPs — bitte **FQDN-basiert** freigeben.
> Ist das nicht möglich, ist **Tier 2** (eigener Router) der einfachere Weg.

### 5. Zwei Ausnahmen (wichtig — sonst schlägt der Betrieb still fehl)

- **Keine TLS-Inspektion für dieses VLAN.** Ein Middlebox, das
  Zertifikate neu signiert, bricht `docker pull`, Hugging-Face-Uploads und
  den SHA-256-geprüften Agent-Updater **prinzipbedingt**. Der eingebaute
  **„Netzwerk-Check"** im Einrichtungsassistenten erkennt genau das und meldet
  „Zertifikat nicht echt".
- **Proxy-/PAC-Bypass** auf den Schüler-PCs für das **Robotik-Subnetz** und
  **`*.local`**. Sonst schickt der Browser die lokale `http://edubotics-NN.local/`-
  Anfrage an den Proxy, der sie nicht auflösen kann.

### 6. NAC / 802.1X

Falls Portsicherheit oder 802.1X aktiv ist: die **MAC-Adressen** der Pis (vom
Etikett) als **Headless-Geräte** registrieren. Die Pis
authentifizieren sich nicht per Supplicant.

### 7. Optional: mDNS-Reflektor

Ein mDNS-Reflektor/Repeater zwischen Schüler- und Robotik-VLAN (auf UniFi/
Aruba/Cisco meist ein Häkchen) lässt `.local`-Namen VLAN-übergreifend
auflösen. **Verzichtbar** — die IP-Etiketten decken die Erreichbarkeit ab.

---

## Tier 2 — eigener Robotik-Router (empfohlener Pilot, Rückfalllösung)

Ein von der Lehrkraft mitgebrachter Router/Access-Point:

- Pis **kabelgebunden** an die LAN-Ports, Schüler-PCs auf dessen SSID.
- WAN-Port in eine beliebige Wanddose der Schule (oder an einen **4G/5G-
  Hotspot** — dann entfällt sogar die Egress-Freigabe).

Im eigenen Netz gibt es **keine GPO, keine Isolation, keine ACL, keinen
Proxy** — alles funktioniert sofort. Aus Sicht der Schul-IT ist die ganze
Klasse **ein einziger ausgehender HTTPS-Client** (eine MAC zu registrieren,
eine TLS-Inspektions-Ausnahme). **Der P4-Pilot sollte auf diesem Tier laufen**,
damit er unabhängig von der Ticket-Warteschlange ist, und danach auf Tier 1
migrieren.

> Hinweis: Verwaltete Schüler-PCs können lokal weiterhin `EnableMDNS=0` haben —
> die **IP-Etiketten bleiben die universelle Rückfalllösung** (DHCP-
> Reservierungen auf dem eigenen Router sind trivial).

---

## Tier 3 — ganz ohne Netzfreigaben (Notbetrieb)

- **(a) Direktes Ethernet-Kabel PC ↔ Pi.** Link-Local-Adressierung + avahi
  funktionieren auf einer Direktverbindung ohne jede Konfiguration; Aufnahme,
  Teleop und Roboter Studio laufen, die Cloud-Schritte warten auf eine
  spätere Internetverbindung.
- **(b) Kiosk-Modus.** `EDUBOTICS_LAN_OPEN=0` + Monitor/Tastatur direkt am Pi —
  exponiert nichts ins Netz, braucht nur ausgehendes HTTPS für die
  Cloud-Anteile.

---

## Fehlerbilder → Ursache (für das IT-Ticket)

| Symptom (Meldung im Assistenten) | Ursache | Behebung |
|---|---|---|
| „Updates schlagen mit Zertifikatfehler fehl" / „Zertifikat nicht echt" | TLS-Inspektion signiert die Verbindung neu | Ausnahme Schritt 5 |
| „Anmeldung läuft ständig ab" | TLS-Inspektion / Supabase-Host blockiert | Egress Schritt 4 + Ausnahme Schritt 5 |
| `edubotics-NN.local` nicht auffindbar | mDNS über VLAN/Isolation gefiltert oder `EnableMDNS=0` | `http://<IP>/` vom Etikett verwenden; optional Reflektor Schritt 7 |
| Web-App lädt, aber der Roboter bleibt „Getrennt" („Verbindung zum Roboter blockiert?") | Eine Middlebox bricht die WebSocket-Verbindung auf Port 80 (`/rosbridge`) ab, oder die Roboter-Umgebung läuft nicht | Umgebung starten; sonst WebSocket auf Port 80 zulassen, Schritt 3 |
| Cloud-Inferenz bricht ab mit „Die Cloud ist nicht erreichbar. Bitte die Internetverbindung des Roboters prüfen." | `*.modal.direct` blockiert oder per TLS-Inspektion neu signiert | Egress Schritt 4 + Ausnahme Schritt 5 |
| Bild lädt gar nicht auf dem Schüler-PC | Client-/AP-Isolation im Robotik-VLAN aktiv | Isolation aus, Schritt 1 |

## Warum diese Ausnahmen? (Ein-Satz-Begründungen)

- **80** = der eine Roboter-Port (Bedienung, Kamera und Steuerung laufen über
  denselben Proxy).
- **Egress 443** = Container-Images, KI-Datensätze, Cloud-Training, Anmeldung.
- **Keine TLS-Inspektion** = Image-Pulls und der Updater prüfen echte
  Zertifikate; ein Re-Signieren bricht sie **absichtlich**.
- **Proxy-Bypass für `*.local`** = lokale Gerätenamen dürfen nicht an den
  Internet-Proxy gehen.
- **NTP** = ohne synchrone Uhr scheitern TLS-Handshakes und die Anmeldung.
