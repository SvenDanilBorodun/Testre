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

**Inferenz** (das Ausführen trainierter Modelle) läuft **„In der Cloud"**: Das
Modell rechnet auf einer Cloud-GPU in einem EU-Rechenzentrum, der Arm bleibt am
Pi und führt die Bewegungen aus. Der Pi selbst rechnet keine Modelle (er hat
keine GPU) und es wird nichts heruntergeladen.

## Vor dem Start — Netzwerk klären

Schulnetze sind der häufigste Stolperstein. **Bitte zuerst die
[Netzwerk-Anleitung für die IT](ORANGE_PI_IT_NETZWERK.md) an die IT geben** —
sie ist als fertiges Ticket formuliert (VLAN, Port 80, Egress-Liste,
TLS-/Proxy-Ausnahmen, MAC-Registrierung).

> Für den **Browser** genügt **Port 80**: Web-Oberfläche, rosbridge und
> Videostrom laufen am Pi über denselben Proxy. Die Ports 8080 und 9090 sind
> nur noch auf dem Pi selbst erreichbar (für die Fehlersuche per SSH) und
> müssen **nicht** freigegeben werden.

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
| Roboterarm(e) — **je nach Robotertyp** | „OMX – Voll": 1× OpenMANIPULATOR-X **Leader** + 1× **Follower** (beide OpenRB-150-Boards). „OMX – Roboter Studio": **nur der Follower**. „V1": **ein** Arm, und zwar ein **Feetech**-Arm am Waveshare-Adapter — **kein** OpenRB-150. |
| USB-Kameras — **je nach Robotertyp** | „OMX – Voll": 2× (Greifer + Szene). „OMX – Roboter Studio": 1× (Szene) genügt, 2× möglich. „V1": **1× (Szene)** — dieser Typ kennt nur die Szenen-Kamera. |
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
> provisionieren, dann `sudo /opt/edubotics/pi_agent/setup.sh --prepare-golden`
> ausführen und das Image abziehen. Jeder Klon vergibt sich beim **ersten Boot** automatisch eine
> **eindeutige** Kennung (Hostname `edubotics-NN`, ROS-Domain, frische
> Schlüssel) — doppelte `.local`-Namen sind das Einzige, was mDNS nicht
> übersteht, deshalb wird `NN` **abgeleitet, nie von Hand vergeben**.
> Jeder Klon startet außerdem **nicht eingerichtet** und erzeugt seinen
> **eigenen Einrichtungscode** (siehe „Schritt 4 — Roboter einrichten"); den
> Code eines Klons zeigt der Befehl `sudo sh -c 'cd /opt/edubotics && python3 -m pi_agent.setup_code'` im Terminal des Klons.

## Schritt 2 — Agent installieren (provisionieren)

Der Agent kommt **ausschließlich als geprüftes Paket aus dem öffentlichen
Download-Kanal** — dieselbe Datei, mit der sich jeder Pi später selbst
aktualisiert. Welche Fassung gerade freigegeben ist und wo sie liegt, sagt die
EduBotics-Cloud unter `/version` (Felder `pi_agent_download_url` und
`pi_agent_sha256`). Ein Zugang zum Quellcode wird **nicht** gebraucht.

Mit eingesteckten Armen + Kameras, im Terminal des Pi. Den Block **als Ganzes**
einfügen, von der Zeile `(` bis zur Zeile `)`:

```bash
(
  set -euo pipefail
  abbruch() { echo "ABBRUCH: $*" >&2; exit 1; }

  # 1) Ein leeres Arbeitsverzeichnis anlegen (nicht im Home-Verzeichnis selbst
  #    entpacken: das Skript sieht sich neben dem entpackten Ordner um).
  mkdir -p ~/edubotics-install || abbruch "Arbeitsverzeichnis nicht angelegt."
  cd ~/edubotics-install || abbruch "Arbeitsverzeichnis nicht erreichbar."
  rm -rf version.json edubotics-pi-agent.tar.gz pi_agent

  # 2) Die aktuelle Freigabe abfragen: Download-Adresse und SHA-256-Prüfsumme.
  curl -fsS -o version.json \
    https://scintillating-empathy-production-1068.up.railway.app/version \
    || abbruch "Die EduBotics-Cloud ist nicht erreichbar."
  URL=$(python3 -c 'import json; print(json.load(open("version.json")).get("pi_agent_download_url") or "")') \
    || abbruch "Die Antwort der Cloud ist nicht lesbar."
  SHA=$(python3 -c 'import json; print(json.load(open("version.json")).get("pi_agent_sha256") or "")') \
    || abbruch "Die Antwort der Cloud ist nicht lesbar."
  echo "Paket:      $URL"
  echo "Prüfsumme:  $SHA"
  if [ -z "$URL" ] || [ -z "$SHA" ]; then
    abbruch "Gerade gibt es keine geprüfte Freigabe (ein Feld ist leer). Später erneut versuchen."
  fi

  # 3) Herunterladen und die Prüfsumme vergleichen.
  curl -fL -o edubotics-pi-agent.tar.gz "$URL" \
    || abbruch "Das Paket konnte nicht heruntergeladen werden."
  echo "$SHA  edubotics-pi-agent.tar.gz" | sha256sum -c - \
    || { rm -f edubotics-pi-agent.tar.gz
         abbruch "Die Prüfsumme stimmt nicht. Die Datei ist gelöscht, nichts wurde entpackt."; }

  # 4) Erst nach „edubotics-pi-agent.tar.gz: OK": entpacken und als root provisionieren.
  tar xzf edubotics-pi-agent.tar.gz
  sudo ./pi_agent/setup.sh
)
```

Der Block **hält von selbst an**, bevor etwas entpackt oder als root
ausgeführt wird, und meldet `ABBRUCH: …`, wenn die Cloud nicht antwortet, ein
Feld leer ist, der Download scheitert oder die Prüfsumme nicht stimmt. Danach
**nichts von Hand nachholen** (kein `tar`, kein `sudo …/setup.sh` einzeln
eintippen):

- **Ist eines der beiden Felder leer**, liegt gerade keine geprüfte Freigabe
  vor (die Veröffentlichung ist unterwegs oder fehlgeschlagen): später den
  ganzen Block erneut einfügen. Nie ein Paket ohne Prüfsumme installieren.
- **Meldet `sha256sum` „FAILED"**, ist die Datei beschädigt oder unterwegs
  verändert worden (oft eine TLS-Inspektion, siehe
  [Netzwerk-Anleitung](ORANGE_PI_IT_NETZWERK.md)). Der Block hat sie bereits
  gelöscht; erst die Ursache klären, dann den Block erneut einfügen.
- Das Paket bringt seine feste Image-Version mit (`pi_agent/docker/versions.env`)
  und die Lizenztexte (`pi_agent/LICENSE.txt`, `pi_agent/THIRD_PARTY_NOTICES.txt`).

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
10. druckt am Ende das **Gehäuse-Etikett**,
11. und darunter den **Einrichtungscode** (z. B. `ABCD-EFGH`). Er wird nur
    jetzt angezeigt — bitte notieren (oder aufs Etikett schreiben); er wird
    in Schritt 4 einmal gebraucht.

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

## Schritt 4 — Roboter einrichten (Einrichtungscode)

Seit dem Update vom November 2026 braucht **jeder Zugriff auf den Roboter**
(Robotersteuerung, Kamerabild, Datensätze und das System-Fenster) eine
**Anmeldung mit einem EduBotics-Konto**. Ein frisch installierter Pi ist
außerdem **noch nicht eingerichtet**: bis eine Lehrkraft oder ein Admin ihn
einmal mit dem **Einrichtungscode** einrichtet, nimmt er gar keine Anmeldung an.

1. `http://edubotics-NN.local/` (oder `http://<IP>/`) öffnen. Es erscheint
   „Diesen Roboter einrichten".
2. Mit einem **Lehrkräfte- oder Admin-Konto** anmelden (ein Schülerkonto kann
   nicht einrichten).
3. Den **Einrichtungscode** eingeben (Groß-/Kleinschreibung und Bindestrich
   egal) und „Roboter einrichten" drücken. Danach öffnet sich das
   System-Fenster.
4. Ab jetzt kann sich **jedes EduBotics-Konto** an diesem Pi anmelden. Der
   Code ist damit verbraucht.

**Wo ist der Code?**

- am Ende der Installation (`setup.sh`, unter dem Etikett);
- beim Start auf einem **angeschlossenen Bildschirm** (HDMI-Textkonsole), wenn
  der Pi noch keinen Code hatte — auf einem Desktop-Image oft nicht sichtbar;
- nach einem **„Daten zurücksetzen"** einmal im Browser, der zurückgesetzt hat.

**Code verloren oder gesperrt?** (Nach 20 Fehlversuchen sperrt sich ein Code.)
Im Terminal des Pi (per Tastatur/Bildschirm oder SSH):

```bash
sudo sh -c 'cd /opt/edubotics && python3 -m pi_agent.setup_code'
```

Der Befehl ersetzt den alten Code durch einen neuen und zeigt ihn an. Er
funktioniert nur, solange der Pi **nicht** eingerichtet ist; ein eingerichteter
Pi bekommt einen neuen Code nur über „Daten zurücksetzen".

> **Bereits installierte Pis nach dem Update:** Ein Pi, der vor diesem Update
> eingerichtet war, gilt danach als **nicht eingerichtet** — Roboter, Kamera,
> Daten und System-Fenster sind gesperrt, bis er eingerichtet ist. Für **jeden**
> Pi einmal im Terminal den Befehl oben ausführen und den Code wie in den
> Schritten 1–3 eingeben.

## Schritt 5 — Im Browser verbinden

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

## Schritt 6 — Einrichtungsassistent (System-Fenster)

Derselbe Ablauf wie in der Windows-App, jetzt im Browser. Das System-Fenster
braucht — wie jeder Tab — eine Anmeldung; jedes EduBotics-Konto darf es
benutzen (Lehrkräfte und Admins sehen am Pi **nur** das System-Fenster):

| Schritt | Aktion |
|---|---|
| **Modus — Robotertyp** | Auswählen, **welcher Roboter** an diesem Pi hängt: „OMX – Voll" (beide Arme), „OMX – Roboter Studio (nur Follower)" oder „V1 – Roboter Studio". Diese Wahl bestimmt alles Weitere — nach welcher Arm-Familie der Scan sucht, ob es überhaupt einen Leader-Arm gibt, welche Kamera-Rollen angeboten werden und was „Umgebung starten" verlangt. **Zuerst** den Robotertyp wählen, **dann** scannen: ein Wechsel über die Arm-Familien-Grenze (OMX ↔ V1) macht einen vorhandenen Scan ungültig (siehe Fehlerbehebung). Das Auswahlfeld ist gesperrt, solange die **Roboter-Umgebung läuft**, im **Cloud-Modus** (dort spielt der Robotertyp keine Rolle), während die Auswahl gerade **gespeichert** wird und während ein **Update läuft**. |
| **A/B — Arm(e) scannen** | „OMX – Voll": **beide** Arme scannen, Leader/Follower werden per Servo-ID erkannt. Bei den beiden Follower-only-Typen heißt der Schritt „**Arm scannen**" und es gibt **keine Leader-Kachel** — ein Arm genügt. Die Ports werden in jedem Fall stabil gespeichert. |
| **C — Kameras** | Kameras scannen, Rollen zuweisen, Vorschau prüfen. **Welche Rollen zur Auswahl stehen, hängt am Robotertyp**: bei den OMX-Typen **Greifer** und **Szene**, bei „V1" **nur Szene** — dieser Typ kennt serverseitig keine Greifer-Kamera, und eine so benannte Kamera würde ein Thema veröffentlichen, das niemand liest (der Start meldete trotzdem Erfolg). Auf einem Roboter-Studio-Kit mit **nur einer** Kamera ist **Szene** die richtige Rolle (die Perzeption hängt am Rollen-Namen). Der Pi rät hier nichts: eine Kamera **ohne** zugewiesene Rolle wird nicht gespeichert. |
| **D — HF-Token** | Entfällt auf dem Pi: Jede Schülerin und jeder Schüler trägt das Hugging-Face-Token einmal auf der Startseite der Web-App ein. Es wird mit dem Konto gespeichert und beim Abmelden vom Roboter entfernt. |
| **Umgebung starten** | Bringt die Roboter-Container hoch (der Manager/die Web-Oberfläche läuft **immer**). |

> **Zwei-Tier-Lebenszyklus:** Die Web-Oberfläche (Manager) ist **immer an** —
> sonst gäbe es auf einem frisch gebooteten Pi keine Seite mit dem
> „Umgebung starten"-Knopf. Die **Roboter-Container** kommen erst mit
> „Umgebung starten" hoch (der Dynamixel-Bus muss vorher frei sein).

## Schritt 7 — Aufnehmen & trainieren

> Dieser Schritt gilt für **„OMX – Voll"**. Auf den Follower-only-Typen blendet
> die Web-Oberfläche **Aufnahme, Daten und Training** aus (es gibt keinen
> Leader-Arm zum Teleoperieren) — dort bleiben **Roboter Studio** und, bei
> „OMX – Roboter Studio", **Inferenz**. Der **V1**-Typ kann
> ausschließlich Roboter Studio: der Inferenz-Tab ist sichtbar, ein Start wird
> aber auf Deutsch abgelehnt.
>
> **Inferenz läuft am Pi „In der Cloud".** Der Pi hat keine GPU; das Modell
> rechnet auf einer Cloud-GPU, der Arm bleibt am Pi. Dafür braucht die Station
> ausgehendes HTTPS zur Cloud-Inferenz (siehe
> [Netzwerk-Anleitung](ORANGE_PI_IT_NETZWERK.md)) und die Schülerin bzw. der
> Schüler ein fertig trainiertes ACT-Modell.

1. **Aufnahme**-Tab: Demos mit Leader→Follower-Teleop aufnehmen (inkl.
   Kollisions-Nothalt, unverändert).
2. **Roboter Studio** (optional): Blockly-Programme, AprilTag-Perzeption,
   manuelle Armsteuerung.
3. **Training**: Datensatz in die Cloud hochladen und ein Modell trainieren
   (bestehender Ablauf, unverändert).
4. **Inferenz**: im Inferenz-Tab ein trainiertes Modell wählen und
   „In der Cloud starten". Arm und Kameras bleiben am Pi; der Leader-Arm muss
   dafür in den Follower-Modus geschaltet sein (die Seite sagt es).

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
| Nach einem Wechsel des **Robotertyps**: „Die gescannten Arme gehören zu einem anderen Robotertyp." | **Kein Kabelproblem.** Ein Wechsel über die Arm-Familien-Grenze (OMX ↔ V1) macht den vorhandenen Scan ungültig — dieselbe Meldung steht auch im Protokoll. Einfach mit dem neuen Typ **neu scannen**. |
| Arme werden nicht erkannt | **Zuerst:** Passt der oben gewählte **Robotertyp** zum angesteckten Arm? Der Scan sucht ausschließlich nach der Arm-Familie dieses Typs, findet den falschen also gar nicht — dann nennt die Meldung den Robotertyp, nicht das Kabel. Sonst: Arm(e) eingesteckt? Läuft bereits eine Umgebung (belegt den seriellen Bus)? Erst „Stoppen", dann neu scannen. |
| Unterspannungs-Resets in der Aufnahme | Offizielles 5 V/5 A-Netzteil verwenden; Kernel-Log auf `undervoltage`/`reset` prüfen. |
| Agent-Dienst startet nicht | `journalctl -u edubotics-pi -n 50`. |
| „Diesen Roboter einrichten" statt der Anmeldung | Der Pi ist (noch oder nach „Daten zurücksetzen"/einem Update) nicht eingerichtet — Schritt 4. |
| „Der Einrichtungscode ist nach zu vielen Fehlversuchen gesperrt" / „Für diesen Pi gibt es noch keinen Einrichtungscode" | Im Terminal des Pi `sudo sh -c 'cd /opt/edubotics && python3 -m pi_agent.setup_code'` ausführen; er zeigt einen neuen Code. |
| „Der Roboter erreicht die EduBotics-Cloud gerade nicht und kann die Anmeldung deshalb nicht prüfen" | Der **Pi** (nicht der Browser) braucht ausgehendes HTTPS zur Cloud-API; TLS-Inspektion bricht es. „Netzwerk-Check" im System-Fenster, sonst [Netzwerk-Anleitung](ORANGE_PI_IT_NETZWERK.md). Ohne Internet bleibt der Roboter gesperrt. |
| „Die Pi-Software ist älter als diese Weboberfläche" | Im System-Fenster „Aktualisieren" ausführen. |
| „Die Weboberfläche dieses Pis ist älter als die Pi-Software" / „Umgebung starten" wird abgelehnt | Direkt nach einem Update normal: der Pi lädt die neue Weboberfläche selbst (einige Minuten), danach die Seite neu laden. Bleibt es dabei: Pi neu starten. |

## Sicherheitshinweis

Port 80 (Web-Oberfläche, Robotersteuerung, Kamerabild, Datensätze und
System-Fenster über denselben Proxy) verlangt seit dem Update vom November 2026
eine **Anmeldung mit einem EduBotics-Konto**. Ein Gerät im LAN **ohne** Konto
kann den Arm nicht mehr steuern, keine Kamera sehen, keine Daten lesen und den
Pi nicht zurücksetzen. Jedes EduBotics-Konto darf jeden eingerichteten Pi
benutzen, den es erreicht.

Die Verbindung zum Pi ist aber **unverschlüsselt (HTTP)**. Wer im selben
Netzsegment mitlesen kann, kann eine laufende Anmeldung abgreifen (das
Anmelde-Cookie des Roboters und für kurze Zeit die Anmeldung des Kontos).
Deshalb bleibt das **eigene Robotik-VLAN** (oder der eigene Pilot-Router)
**Voraussetzung für den Betrieb**, nicht Kür. Zusätzlich: der
**`EDUBOTICS_LAN_OPEN=0`**-Schalter (Kiosk: alle Ports zurück auf `127.0.0.1`)
und die **Host/Origin-Prüfung** auf zustandsändernden Endpunkten. „Daten
zurücksetzen" verlangt das eingetippte Wort **ZURÜCKSETZEN** und macht den Pi
wieder „nicht eingerichtet".
