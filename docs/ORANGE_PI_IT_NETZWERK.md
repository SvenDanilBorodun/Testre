# EduBotics Orange Pi — Netzwerk-Anleitung für die IT

> **Für die Schul-IT / Netzwerkadministration.** Diese Seite ist als
> **Arbeitsauftrag (Ticket)** gedacht: sie enthält alles, was zum Freischalten
> der EduBotics-Roboterstationen (Orange Pi 5 Pro) und des Klassen-Jetson
> nötig ist — Porttabelle, Ziel-Adressliste (Egress), Ausnahmen für
> TLS-Inspektion und Proxy sowie die MAC-/NAC-Registrierung. Bearbeitungszeit
> ca. 20–30 Minuten. Kein Rückruf nötig — bei Fragen genügt der Absatz
> „Warum diese Ausnahmen?" am Ende.

## Kurzfassung (TL;DR für das Ticket)

1. **Eigenes Robotik-VLAN** (z. B. `ROBOTIK`) mit **abgeschalteter
   Client-Isolation**. Die Pis und der Jetson gehören **kabelgebunden** hinein,
   die Schüler-PCs erreichen es über eine passende SSID **oder** über die
   Firewall-Freigabe in Schritt 3.
2. **DHCP-Reservierungen** für jeden Pi und den Jetson anhand der **MAC-Adresse
   auf dem Gehäuse-Etikett**. Die reservierte IP kommt zurück aufs Etikett.
3. **Eingehend** (Schüler-PC → Robotik-VLAN): nur TCP **80 / 8080 / 9090**
   (Pi) und TCP **9091** (Jetson).
4. **Ausgehend** (Robotik-VLAN → Internet): TCP **443** zur Adressliste unten
   plus **NTP (UDP 123)**.
5. **Zwei Ausnahmen:** **keine TLS-Inspektion** für dieses VLAN und
   **Proxy-/PAC-Bypass** für das Robotik-Subnetz und `*.local`.
6. Bei **NAC / 802.1X**: die MAC-Adressen der Pis/des Jetson als **Headless-
   Geräte** freigeben.

Ist ein eigenes VLAN kurzfristig nicht möglich, siehe **Tier 2** (eigener
Robotik-Router) weiter unten — der Pilot kann sofort und unabhängig von der
Ticket-Warteschlange starten.

---

## Warum überhaupt ein eigenes Netzsegment?

Die EduBotics-Roboterstation bindet ihre Steuer-Ports (80/8080/9090)
**offen ins LAN, ohne Authentifizierung** — das ist eine bewusste
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

- Neues VLAN, z. B. `ROBOTIK`. Kabelports für **jeden Pi** und **den Jetson**.
- Passende **SSID** in dasselbe VLAN gemappt für die Schüler-PCs — **oder**
  die Schüler-PCs bleiben in ihrem VLAN und erhalten die Firewall-Freigabe aus
  Schritt 3.
- **Client-/AP-Isolation im Robotik-VLAN: AUS** (sonst erreichen die
  Schüler-PCs die Pis nicht).

### 2. DHCP-Reservierungen

Für **jeden Pi und den Jetson** eine feste IP per DHCP-Reservierung,
gebunden an die **MAC-Adresse vom Gehäuse-Etikett** (`setup.sh` druckt
Hostname + MAC beim Provisionieren). Die reservierte IP bitte **auf das
Etikett in das freie IP-Feld** eintragen — danach gilt `http://<IP>/`
dauerhaft, auch wenn mDNS auf gehärteten Schul-PCs deaktiviert ist.

### 3. Eingehende Freigaben (nur falls die Schüler-PCs in einem anderen VLAN sind)

Nur diese Ports, sonst nichts eingehend:

| Freigeben | Port | Zweck |
|---|---|---|
| TCP | **80** | Web-App (Einrichtungsassistent + Bedienoberfläche) |
| TCP | **8080** | Kamera-Streams (web_video_server) |
| TCP | **9090** | Robotersteuerung (rosbridge) |
| TCP | **9091** | Jetson-Inferenz-Proxy (nur der Jetson) |

Richtung: **Schüler-PC → Robotik-VLAN**. Keine Freigabe in die Gegenrichtung
nötig.

### 4. Ausgehende Freigaben (Robotik-VLAN → Internet)

**TCP 443** zu den folgenden Zielen, plus **NTP (UDP 123)**:

| Zweck | Ziel-Hosts (FQDN) |
|---|---|
| Container-Images + Agent-Update | `ghcr.io`, `*.githubusercontent.com`, `github.com` |
| Registry-Ausweichweg (Docker Hub) | `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com` |
| Datensätze / Modelle (Hugging Face) | `huggingface.co`, `*.hf.co` |
| EduBotics Cloud-API (Railway) | `scintillating-empathy-production-1068.up.railway.app` |
| Lehrer-Web-Dashboard (Railway) | `teacher-web-production.up.railway.app` |
| Anmeldung / Datenbank (Supabase) | `fnnbysrjkfugsqzwcksd.supabase.co`, `*.supabase.co` |
| OS-Sicherheitsupdates | `ports.ubuntu.com`, `*.armbian.com` |
| Zeitsynchronisation | **UDP 123** (NTP) — beliebiger erreichbarer NTP-Server |

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

Falls Portsicherheit oder 802.1X aktiv ist: die **MAC-Adressen** der Pis und
des Jetson (vom Etikett) als **Headless-Geräte** registrieren. Die Pis
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
| Web-App lädt, aber „Verbindung zu Port 9090 blockiert" | Inter-VLAN-ACL filtert 9090 | Portfreigabe Schritt 3 |
| Kamerabild schwarz, UI sonst da | 8080 gefiltert | Portfreigabe Schritt 3 |
| Bild lädt gar nicht auf dem Schüler-PC | Client-/AP-Isolation im Robotik-VLAN aktiv | Isolation aus, Schritt 1 |

## Warum diese Ausnahmen? (Ein-Satz-Begründungen)

- **80/8080/9090** = die drei Roboter-Ports (Bedienung, Kamera, Steuerung);
  **9091** ist der JWT-gesicherte Jetson-Proxy.
- **Egress 443** = Container-Images, KI-Datensätze, Cloud-Training, Anmeldung.
- **Keine TLS-Inspektion** = Image-Pulls und der Updater prüfen echte
  Zertifikate; ein Re-Signieren bricht sie **absichtlich**.
- **Proxy-Bypass für `*.local`** = lokale Gerätenamen dürfen nicht an den
  Internet-Proxy gehen.
- **NTP** = ohne synchrone Uhr scheitern TLS-Handshakes und die Anmeldung.
