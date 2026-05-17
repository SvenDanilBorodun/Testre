#!/usr/bin/env bash
# Jetson Orin Nano EduBotics agent — one-shot installer.
#
# Run as root on a fresh JetPack 6 install:
#     curl -fsSL https://<cloud-api>/jetson/setup.sh | sudo bash
# or download + run locally:
#     sudo ./setup.sh
#
# Steps (idempotent — safe to re-run):
#   1. Verify JetPack 6 + NVIDIA Container Toolkit + Docker present.
#   2. Install Python 3 + pip + the agent's pip requirements.
#   3. Discover the connected ROBOTIS arm + 2 USB cameras, write udev
#      rules so they get stable /dev/edubotics-* symlinks.
#   4. Register the Jetson with the EduBotics Cloud API and receive
#      the agent_token + pairing_code + HF token + Supabase config.
#   5. Write /etc/edubotics/jetson.env (mode 600).
#   6. Install + enable the edubotics-jetson systemd unit.
#   7. Print the 6-digit pairing code prominently so the teacher can
#      enter it in the admin dashboard to bind this device to their
#      classroom.

set -euo pipefail

# ─── Config (override via env) ────────────────────────────────────────────
EDUBOTICS_CLOUD_API_URL="${EDUBOTICS_CLOUD_API_URL:-https://scintillating-empathy-production-9efd.up.railway.app}"
INSTALL_DIR="${EDUBOTICS_INSTALL_DIR:-/opt/edubotics}"
ENV_DIR="${EDUBOTICS_ENV_DIR:-/etc/edubotics}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Helpers ──────────────────────────────────────────────────────────────
log() { printf "\033[1;36m[setup]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[setup ERROR]\033[0m %s\n" "$*" >&2; }
die() { err "$*"; exit 1; }

[ "$(id -u)" = "0" ] || die "Bitte als root oder via sudo ausführen."

# ─── Step 1: Verify JetPack 6 + NVIDIA Container Toolkit + Docker ─────────
log "Überprüfe System-Voraussetzungen..."

if ! command -v nvidia-smi >/dev/null 2>&1; then
    die "nvidia-smi fehlt. Ist das wirklich ein Jetson mit JetPack 6?"
fi

if ! dpkg -l | grep -q nvidia-container-toolkit; then
    die "nvidia-container-toolkit fehlt. Bitte vor dem Setup mit JetPack 6 installieren."
fi

if ! command -v docker >/dev/null 2>&1; then
    log "Docker fehlt — installiere docker.io ..."
    apt-get update -qq
    apt-get install -y -qq docker.io
fi

if ! docker info >/dev/null 2>&1; then
    die "Docker-Daemon läuft nicht. Bitte 'systemctl start docker' ausführen und Setup wiederholen."
fi

if ! command -v jq >/dev/null 2>&1; then
    log "Installiere jq für JSON-Parsing..."
    apt-get install -y -qq jq
fi

if ! command -v qrencode >/dev/null 2>&1; then
    apt-get install -y -qq qrencode || log "qrencode konnte nicht installiert werden — Pairing-Code wird ohne QR ausgegeben."
fi

# ─── Step 2: Python + pip deps ────────────────────────────────────────────
log "Installiere Python-Abhängigkeiten..."
apt-get install -y -qq python3 python3-pip python3-venv v4l-utils
pip3 install --no-cache-dir -r "${SCRIPT_DIR}/requirements.txt"

# ─── Step 3: Discover hardware ────────────────────────────────────────────
log "Suche ROBOTIS-Arm und USB-Kameras..."
mkdir -p /etc/udev/rules.d

# Follower arm — ROBOTIS VID 2F5D, first matching tty.
FOLLOWER_DEVICE=$(ls /dev/serial/by-id/ 2>/dev/null | grep -i robotis | head -n1 || true)
if [ -z "$FOLLOWER_DEVICE" ]; then
    log "WARNUNG: Kein ROBOTIS-Arm an USB erkannt. Bitte später anschließen und Agent neu starten."
fi

# Cameras — list every video device with v4l2-ctl --info, pick the first
# two with USB serials. Teacher confirms order via stdout below.
declare -a CAMERA_DEVICES=()
declare -a CAMERA_SERIALS=()
for dev in $(ls /dev/video* 2>/dev/null | sort); do
    info=$(v4l2-ctl --device "$dev" --info 2>/dev/null || true)
    serial=$(echo "$info" | grep -oP 'Serial : \K\S+' | head -n1 || true)
    # Dedupe by serial. Use [[ ]] (regex actually works there — `=~` in
    # single brackets is a literal string match in bash, which made the
    # check a no-op and let identical cameras be added twice).
    if [ -n "$serial" ] && [[ ! " ${CAMERA_SERIALS[*]:-} " =~ " $serial " ]]; then
        CAMERA_DEVICES+=("$dev")
        CAMERA_SERIALS+=("$serial")
    fi
done

# Write udev rules. The ROBOTIS rule keys on VID 2F5D regardless of
# product ID (matches both 0103 and 2202 OpenRB firmware). Camera rules
# key on the discovered serials.
cat > /etc/udev/rules.d/99-edubotics-robotis.rules <<'EOF'
# EduBotics — ROBOTIS OpenRB-150 stable name (any product ID under VID 2F5D)
SUBSYSTEM=="tty", ATTRS{idVendor}=="2f5d", ATTRS{idProduct}=="0103", SYMLINK+="edubotics-follower"
SUBSYSTEM=="tty", ATTRS{idVendor}=="2f5d", ATTRS{idProduct}=="2202", SYMLINK+="edubotics-follower"
EOF

if [ "${#CAMERA_SERIALS[@]}" -ge 1 ]; then
    {
        echo "# EduBotics — USB cameras (auto-discovered by setup.sh)"
        echo "SUBSYSTEM==\"video4linux\", ATTRS{serial}==\"${CAMERA_SERIALS[0]}\", SYMLINK+=\"edubotics-gripper-cam\""
        if [ "${#CAMERA_SERIALS[@]}" -ge 2 ]; then
            echo "SUBSYSTEM==\"video4linux\", ATTRS{serial}==\"${CAMERA_SERIALS[1]}\", SYMLINK+=\"edubotics-scene-cam\""
        fi
    } > /etc/udev/rules.d/98-edubotics-cameras.rules
    log "Erkannte Kameras:"
    log "  Greifer-Kamera: ${CAMERA_DEVICES[0]} (Serial ${CAMERA_SERIALS[0]})"
    if [ "${#CAMERA_SERIALS[@]}" -ge 2 ]; then
        log "  Szenen-Kamera:  ${CAMERA_DEVICES[1]} (Serial ${CAMERA_SERIALS[1]})"
    fi
else
    log "WARNUNG: Keine USB-Kameras gefunden — Setup läuft trotzdem weiter."
fi

udevadm control --reload-rules
udevadm trigger

# ─── Step 4: Register with EduBotics Cloud API ────────────────────────────
log "Registriere Jetson bei der EduBotics Cloud API..."
LAN_IP=$(ip -4 route get 8.8.8.8 2>/dev/null | awk 'NR==1 {for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' || echo "")

REGISTER_BODY=$(jq -n \
    --arg lan_ip "$LAN_IP" \
    --arg ver "v1.0.0" \
    '{lan_ip: $lan_ip, agent_version: $ver}')

REGISTER_RESP=$(curl -fsS \
    -X POST \
    -H "Content-Type: application/json" \
    -d "$REGISTER_BODY" \
    "${EDUBOTICS_CLOUD_API_URL%/}/jetson/register" \
    || die "Registrierung fehlgeschlagen — bitte EDUBOTICS_CLOUD_API_URL prüfen und Setup wiederholen.")

JETSON_ID=$(echo "$REGISTER_RESP" | jq -r '.jetson_id')
AGENT_TOKEN=$(echo "$REGISTER_RESP" | jq -r '.agent_token')
PAIRING_CODE=$(echo "$REGISTER_RESP" | jq -r '.pairing_code')
HF_TOKEN=$(echo "$REGISTER_RESP" | jq -r '.hf_token')
SUPABASE_URL=$(echo "$REGISTER_RESP" | jq -r '.supabase_url // empty')
JWT_ALG=$(echo "$REGISTER_RESP" | jq -r '.supabase_jwt_algorithm // "ES256"')
# v2.3.0: HS256 path — Cloud API forwards the symmetric JWT secret so
# the agent's rosbridge_proxy can verify student JWTs locally without
# needing a JWKS endpoint. ES256/RS256 projects leave this empty
# (proxy uses JWKS at SUPABASE_URL/auth/v1/.well-known/jwks.json).
# The Cloud API hard-fails /jetson/register with 503 if HS256 is
# configured on Railway but the secret env var is missing, so reaching
# this line guarantees the secret is present when needed.
JWT_SECRET=$(echo "$REGISTER_RESP" | jq -r '.supabase_jwt_secret // empty')

[ "$JETSON_ID" != "null" ] || die "Cloud API gab keine jetson_id zurück."
[ "$AGENT_TOKEN" != "null" ] || die "Cloud API gab kein agent_token zurück."
# Defensive: refuse to write the env file if the algorithm advertised
# is HS256 but the secret is empty. This shouldn't happen (Cloud API
# refuses to register in that case) but a future Cloud API regression
# would silently produce broken Jetsons; better to fail loudly here.
if [ "$JWT_ALG" = "HS256" ] && [ -z "$JWT_SECRET" ]; then
    die "Cloud API meldet HS256, hat aber kein supabase_jwt_secret gesendet. Bitte SUPABASE_JWT_SECRET auf Railway setzen und Setup wiederholen."
fi

# ─── Step 5: Write /etc/edubotics/jetson.env ──────────────────────────────
log "Schreibe Konfiguration nach ${ENV_DIR}/jetson.env ..."
mkdir -p "$ENV_DIR"
ENV_FILE="${ENV_DIR}/jetson.env"

# Derive a per-machine ROS_DOMAIN_ID like the GUI's _resolve_ros_domain_id:
# /etc/machine-id hash mod 233 (avoids two Jetsons in the same school
# Wi-Fi sharing domain 30 and cross-talking).
MACHINE_ID="$(cat /etc/machine-id 2>/dev/null || hostname)"
ROS_DOMAIN_ID=$(printf '%s' "$MACHINE_ID" | sha256sum | head -c 8 | python3 -c 'import sys; print(int(sys.stdin.read(),16) % 233)')

cat > "$ENV_FILE" <<EOF
# EduBotics Jetson agent config — auto-generated by setup.sh
# DO NOT edit by hand; re-run setup.sh to refresh.
EDUBOTICS_JETSON_ID="$JETSON_ID"
EDUBOTICS_AGENT_TOKEN="$AGENT_TOKEN"
EDUBOTICS_CLOUD_API_URL="$EDUBOTICS_CLOUD_API_URL"
EDUBOTICS_HF_TOKEN="$HF_TOKEN"
EDUBOTICS_SUPABASE_URL="$SUPABASE_URL"
EDUBOTICS_SUPABASE_JWT_ALGORITHM="$JWT_ALG"
EDUBOTICS_SUPABASE_JWT_SECRET="$JWT_SECRET"
EDUBOTICS_AGENT_VERSION="v1.0.0"
ROS_DOMAIN_ID="$ROS_DOMAIN_ID"
REGISTRY="${REGISTRY:-nettername}"
EOF
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

# ─── Step 6: Install agent + compose + systemd unit ──────────────────────
log "Installiere Agent-Dateien nach ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
install -m 0755 "${SCRIPT_DIR}/agent.py" "${INSTALL_DIR}/agent.py"
install -m 0755 "${SCRIPT_DIR}/rosbridge_proxy.py" "${INSTALL_DIR}/rosbridge_proxy.py"
install -m 0644 "${SCRIPT_DIR}/docker-compose.jetson.yml" "${INSTALL_DIR}/docker-compose.jetson.yml"
install -m 0644 "${SCRIPT_DIR}/.s6-keep" "${INSTALL_DIR}/.s6-keep"

install -m 0644 "${SCRIPT_DIR}/systemd/edubotics-jetson.service" /etc/systemd/system/edubotics-jetson.service

systemctl daemon-reload
systemctl enable edubotics-jetson.service
systemctl restart edubotics-jetson.service

# Verify the service actually came up before printing the pairing code.
# Otherwise a teacher would enter the code in the admin dashboard but
# the agent isn't running, the heartbeat never arrives, and the Jetson
# silently stays offline.
sleep 5
if ! systemctl is-active --quiet edubotics-jetson.service; then
    err "Agent-Dienst startet nicht."
    err "Logs: journalctl -u edubotics-jetson -n 50"
    systemctl status edubotics-jetson.service --no-pager || true
    exit 1
fi

# ─── Step 7: Print pairing code prominently ──────────────────────────────
printf "\n"
printf "============================================================\n"
printf "  EduBotics Jetson — bereit zum Pairen\n"
printf "============================================================\n"
printf "\n"
printf "  Pairing-Code:  \033[1;33m%s\033[0m\n" "$PAIRING_CODE"
printf "\n"
printf "  Bitte diesen Code im Admin-Dashboard eingeben, um den\n"
printf "  Jetson einem Klassenzimmer zuzuweisen. Der Code läuft\n"
printf "  in 30 Minuten ab.\n"
printf "\n"
if command -v qrencode >/dev/null 2>&1; then
    printf "  QR-Code (für mobile Eingabe):\n"
    qrencode -t ANSIUTF8 "${EDUBOTICS_CLOUD_API_URL%/}/admin/jetson/pair?code=${PAIRING_CODE}" || true
fi
printf "\n"
printf "  Status:  systemctl status edubotics-jetson\n"
printf "  Logs:    journalctl -u edubotics-jetson -f\n"
printf "============================================================\n"
