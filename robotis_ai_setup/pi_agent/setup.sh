#!/usr/bin/env bash
# EduBotics on Orange Pi 5 Pro — one-shot provisioning installer (deploy plan §7).
#
# Mirrors robotis_ai_setup/jetson_agent/setup.sh MINUS the NVIDIA hard-checks:
# the Pi has no CUDA and never runs local inference (Jetson-only, plan §3/§11).
# Run as root on a fresh Armbian / Orange Pi Ubuntu image with both OpenRB-150
# arm boards + both USB cameras plugged in:
#
#     sudo ./setup.sh
#
# Or capture a golden image after one bench install:
#
#     sudo ./setup.sh --prepare-golden    # strip per-unit identity, then image
#
# Steps (idempotent — safe to re-run):
#   1. Install pinned Docker Engine + compose plugin.
#   2. Install jq/qrencode/v4l-utils/avahi/python + the agent's pip deps.
#   3. Install the ROBOTIS udev rule (verbatim) + reload/trigger.
#   4. Enable zram swap (mandatory on the 8 GB board, plan §9).
#   5. Assign the mDNS hostname edubotics-NN (derived from /etc/machine-id).
#   6. Lay out the agent tree under /opt/edubotics (+ compose file + .s6-keep).
#   7. Choose a free ros_net subnet (ip-route overlap check, plan §5) and seed
#      the managed /etc/edubotics/.env.
#   8. Install + enable the edubotics-pi + edubotics-pi-firstboot systemd units.
#   9. Pull the -opi images (GHCR primary, Hub fallback).
#  10. Print the case label/QR: hostname + wired NIC MAC + a BLANK IP field.
#
# Operator/maintainer tool → log lines are ENGLISH (and deliberately avoid the
# [FEHLER]/[WARNUNG]/[STOPP] German marker prefixes german-strings-lint scans
# for). The printed PHYSICAL LABEL text is German with literal umlauts.

set -euo pipefail

# ─── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$(cd "${SCRIPT_DIR}/../docker" 2>/dev/null && pwd || true)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." 2>/dev/null && pwd || true)"

INSTALL_DIR="${EDUBOTICS_INSTALL_DIR:-/opt/edubotics}"
ENV_DIR="${EDUBOTICS_ENV_DIR:-/etc/edubotics}"
STATE_DIR="${EDUBOTICS_STATE_DIR:-/var/lib/edubotics}"
ENV_FILE="${ENV_DIR}/.env"

# Docker pin — kept in lockstep with the WSL rootfs pin (CLAUDE.md): Docker 29.x
# ships the containerd snapshotter, which corrupts multi-layer pulls; 27.5.1 is
# the last pre-snapshotter line we validate.
DOCKER_VERSION_PIN="${EDUBOTICS_DOCKER_VERSION:-5:27.5.1-1~ubuntu.22.04~jammy}"
CONTAINERD_VERSION_PIN="${EDUBOTICS_CONTAINERD_VERSION:-1.7.27-1}"

# ─── Helpers ──────────────────────────────────────────────────────────────────
log()  { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[setup WARN]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[setup ERROR]\033[0m %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

require_root() { [ "$(id -u)" = "0" ] || die "Please run as root (sudo ./setup.sh)."; }

# ─── --prepare-golden: strip per-unit identity before imaging (plan §7 Ph.2) ──
prepare_golden() {
    require_root
    log "Preparing this unit as a GOLDEN IMAGE source (removing per-unit identity)."
    # Stop the agent so nothing rewrites state while we strip it.
    systemctl stop edubotics-pi.service 2>/dev/null || true
    # Empty machine-id → systemd regenerates a fresh one early on every clone,
    # which the first-boot unit hashes into a unique hostname + ROS_DOMAIN_ID.
    if [ -f /etc/machine-id ]; then
        : > /etc/machine-id
    fi
    rm -f /var/lib/dbus/machine-id 2>/dev/null || true
    # Re-arm the first-boot one-shot + drop clone-unsafe state/secrets.
    rm -f "${INSTALL_DIR}/.first-boot-done" 2>/dev/null || true
    # Regenerate a CLEAN token-free cloud-only .env instead of deleting it: the
    # provisioned ros_net subnet must survive into the golden image (the clone's
    # first boot carries it forward), while the bench HF token + arm ports are
    # dropped. A DELETED .env would risk bricking the clone (the agent unit's
    # EnvironmentFile is NON-optional); first-boot still recreates it, but a valid
    # golden .env is the belt-and-suspenders. Order matters: this re-persists the
    # ROS_DOMAIN_ID, so the .ros_domain_id removal below MUST follow it.
    local golden_subnet=""
    if [ -f "${ENV_FILE}" ] && [ -d "${INSTALL_DIR}/pi_agent" ]; then
        golden_subnet="$(cd "$INSTALL_DIR" && python3 -c \
            'from pi_agent import config_generator as c; print(c.read_env_var("EDUBOTICS_ROS_NET_SUBNET") or "")' \
            2>/dev/null || true)"
    fi
    rm -f "${ENV_FILE}" 2>/dev/null || true
    if [ -d "${INSTALL_DIR}/pi_agent" ]; then
        if ( cd "$INSTALL_DIR" && SUBNET="$golden_subnet" python3 -c \
                'import os; from pi_agent import config_generator as c; c.generate_cloud_only_env(ros_net_subnet=(os.environ.get("SUBNET") or None))' \
                2>/dev/null ); then
            chmod 600 "${ENV_FILE}" 2>/dev/null || true
            chown root:root "${ENV_FILE}" 2>/dev/null || true
        else
            warn "Could not regenerate a clean golden .env — the clone's first boot will recreate one."
        fi
    fi
    rm -rf "${ENV_DIR}/phone-cert" 2>/dev/null || true
    rm -f "${STATE_DIR}/.ros_domain_id" "${STATE_DIR}/.last_image_pull.json" 2>/dev/null || true
    log "Done. Power off now and capture the eMMC/NVMe image."
    log "Each clone will self-personalize (hostname edubotics-NN + secrets) on first boot."
    exit 0
}

if [ "${1:-}" = "--prepare-golden" ]; then
    prepare_golden
fi

require_root

# ─── Step 1: pinned Docker Engine + compose plugin ────────────────────────────
install_docker() {
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        log "Docker already installed and running — skipping install."
    else
        log "Installing Docker Engine (pinned ${DOCKER_VERSION_PIN})..."
        apt-get update -qq
        apt-get install -y -qq ca-certificates curl gnupg
        install -m 0755 -d /etc/apt/keyrings
        if [ ! -s /etc/apt/keyrings/docker.gpg ]; then
            curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
                | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null \
                || warn "Could not fetch the Docker GPG key — is egress to download.docker.com open?"
            chmod a+r /etc/apt/keyrings/docker.gpg 2>/dev/null || true
        fi
        # Codename: Armbian/Orange-Pi Ubuntu images set UBUNTU_CODENAME; Debian
        # bases set VERSION_CODENAME. Fall back to jammy (the pin's codename).
        local codename
        codename="$(. /etc/os-release 2>/dev/null && printf '%s' "${UBUNTU_CODENAME:-${VERSION_CODENAME:-jammy}}")"
        printf 'deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' \
            "$codename" > /etc/apt/sources.list.d/docker.list
        apt-get update -qq || warn "apt-get update failed for the Docker repo."
        # Pin the exact version when the repo carries it; otherwise install the
        # best available docker-ce and warn (a school Armbian codename may not
        # publish the jammy pin string).
        if apt-cache madison docker-ce 2>/dev/null | grep -q "$DOCKER_VERSION_PIN"; then
            apt-get install -y -qq \
                "docker-ce=${DOCKER_VERSION_PIN}" \
                "docker-ce-cli=${DOCKER_VERSION_PIN}" \
                "containerd.io=${CONTAINERD_VERSION_PIN}" \
                docker-buildx-plugin docker-compose-plugin \
                || die "Docker install failed (pinned)."
        else
            warn "Pin ${DOCKER_VERSION_PIN} not in this repo — installing the newest docker-ce."
            apt-get install -y -qq \
                docker-ce docker-ce-cli containerd.io \
                docker-buildx-plugin docker-compose-plugin \
                || apt-get install -y -qq docker.io docker-compose-plugin \
                || die "Docker install failed."
        fi
        # Hold so an unattended apt upgrade can't drag Docker onto the 29.x
        # snapshotter line (matches the WSL rootfs `apt-mark hold`).
        apt-mark hold docker-ce docker-ce-cli containerd.io >/dev/null 2>&1 || true
        systemctl enable --now docker >/dev/null 2>&1 || true
    fi

    # Disable the containerd snapshotter defensively (parity with the rootfs
    # daemon.json; harmless on the pinned 27.5.1 line, protective if unpinned).
    mkdir -p /etc/docker
    if [ ! -f /etc/docker/daemon.json ]; then
        printf '{\n  "features": { "containerd-snapshotter": false }\n}\n' > /etc/docker/daemon.json
        systemctl restart docker >/dev/null 2>&1 || true
    fi

    docker info >/dev/null 2>&1 || die "Docker daemon not reachable after install."
    docker compose version >/dev/null 2>&1 || die "docker compose plugin missing."
    log "Docker OK: $(docker --version 2>/dev/null)"
}

# ─── Step 2: system packages + agent pip deps ─────────────────────────────────
install_packages() {
    log "Installing system packages (jq, qrencode, v4l-utils, avahi, python3, rsync)..."
    apt-get install -y -qq \
        jq qrencode v4l-utils avahi-daemon avahi-utils \
        python3 python3-pip rsync openssl curl ca-certificates \
        || warn "Some apt packages failed to install — continuing."
    systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

    log "Installing agent pip dependencies..."
    # PEP 668 (externally-managed) needs --break-system-packages on newer
    # distros; try the plain install first, then the override.
    if [ -f "${SCRIPT_DIR}/requirements.txt" ]; then
        pip3 install --no-cache-dir -r "${SCRIPT_DIR}/requirements.txt" 2>/dev/null \
            || pip3 install --no-cache-dir --break-system-packages -r "${SCRIPT_DIR}/requirements.txt" \
            || warn "pip install failed — the camera-preview endpoint (opencv) will be degraded."
    fi
}

# ─── Step 3: udev rule (verbatim) ─────────────────────────────────────────────
install_udev() {
    log "Installing the ROBOTIS udev rule (verbatim, permission floor only)..."
    mkdir -p /etc/udev/rules.d
    install -m 0644 "${SCRIPT_DIR}/udev/99-edubotics-robotis.rules" \
        /etc/udev/rules.d/99-edubotics-robotis.rules
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
}

# ─── Step 4: zram swap (mandatory, plan §9) ───────────────────────────────────
enable_zram() {
    if swapon --show 2>/dev/null | grep -qi zram; then
        log "zram swap already active."
        return
    fi
    log "Enabling zram swap (mandatory on the 8 GB board)..."
    if apt-get install -y -qq zram-tools 2>/dev/null; then
        # zram-tools: ~50% of RAM, zstd — configure /etc/default/zramswap.
        {
            echo "ALGO=zstd"
            echo "PERCENT=50"
        } > /etc/default/zramswap
        systemctl enable --now zramswap.service >/dev/null 2>&1 \
            || systemctl restart zramswap.service >/dev/null 2>&1 || true
    elif apt-get install -y -qq systemd-zram-generator 2>/dev/null \
         || apt-get install -y -qq zram-generator 2>/dev/null; then
        cat > /etc/systemd/zram-generator.conf <<'EOF'
[zram0]
zram-size = min(ram / 2, 4096)
compression-algorithm = zstd
EOF
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl start systemd-zram-setup@zram0.service >/dev/null 2>&1 || true
    else
        warn "Neither zram-tools nor zram-generator installed — enable zram manually (plan §9)."
        return
    fi
    if swapon --show 2>/dev/null | grep -qi zram; then
        log "zram active: $(swapon --show=NAME,SIZE 2>/dev/null | grep -i zram | head -n1)"
    else
        warn "zram did not come up — verify manually."
    fi
}

# ─── Step 5: mDNS hostname edubotics-NN (derived from machine-id) ─────────────
derive_nn() {
    # 8-digit decimal from the first 24 bits of sha256(machine-id) — the SAME
    # formula the first-boot unit uses, so bench + fleet names agree. 24 bits
    # (16.7M space) keeps a fleet of ~100 rigs collision-free (16 bits was ~7%).
    local mid
    mid="$(cat /etc/machine-id 2>/dev/null || hostname)"
    printf '%s' "$mid" | sha256sum | head -c6 \
        | python3 -c 'import sys; print("%08d" % (int(sys.stdin.read(), 16)))' 2>/dev/null \
        || echo "00000000"
}

assign_hostname() {
    local new_host
    if [ -n "${EDUBOTICS_HOSTNAME:-}" ]; then
        new_host="${EDUBOTICS_HOSTNAME}"
    else
        new_host="edubotics-$(derive_nn)"
    fi
    log "Assigning mDNS hostname: ${new_host}"
    hostnamectl set-hostname "$new_host" 2>/dev/null || printf '%s\n' "$new_host" > /etc/hostname
    if grep -qE '^\s*127\.0\.1\.1' /etc/hosts 2>/dev/null; then
        sed -i "s/^\s*127\.0\.1\.1.*/127.0.1.1\t${new_host}/" /etc/hosts || true
    else
        printf '127.0.1.1\t%s\n' "$new_host" >> /etc/hosts || true
    fi
    systemctl restart avahi-daemon >/dev/null 2>&1 || true
    HOSTNAME_ASSIGNED="$new_host"
}

# ─── Step 6: lay out the agent tree under /opt/edubotics ──────────────────────
install_agent_tree() {
    log "Installing the agent under ${INSTALL_DIR}..."
    mkdir -p "${INSTALL_DIR}/pi_agent" "${INSTALL_DIR}/physical_ai_server" \
             "${ENV_DIR}" "${STATE_DIR}"

    # The agent runs as `python3 -m pi_agent.agent` from WorkingDirectory
    # /opt/edubotics, so the package must live at /opt/edubotics/pi_agent/.
    # Exclude tests + bytecode (they never ship — matches the release tarball).
    rsync -a --delete \
        --exclude='tests' --exclude='__pycache__' --exclude='*.pyc' \
        "${SCRIPT_DIR}/" "${INSTALL_DIR}/pi_agent/"
    chmod 0755 "${INSTALL_DIR}/pi_agent/systemd/edubotics-pi-firstboot.sh" 2>/dev/null || true

    # Compose file + the s6-keep marker it bind-mounts. The compose references
    # ./physical_ai_server/.s6-keep RELATIVE to its own directory, so both land
    # under /opt/edubotics (constants.COMPOSE_FILE = /opt/edubotics/docker-compose.opi.yml).
    [ -n "$DOCKER_DIR" ] || die "Cannot locate the docker/ dir (expected ${SCRIPT_DIR}/../docker)."
    install -m 0644 "${DOCKER_DIR}/docker-compose.opi.yml" \
        "${INSTALL_DIR}/docker-compose.opi.yml"
    install -m 0644 "${DOCKER_DIR}/physical_ai_server/.s6-keep" \
        "${INSTALL_DIR}/physical_ai_server/.s6-keep"

    # VERSION so constants._read_version_file resolves the product version even
    # when the agent tarball is deployed without the source tree beside it.
    if [ -n "$REPO_ROOT" ] && [ -f "${REPO_ROOT}/VERSION" ]; then
        install -m 0644 "${REPO_ROOT}/VERSION" "${INSTALL_DIR}/VERSION"
    fi
}

# ─── Step 7: choose a free ros_net subnet + seed the managed .env ─────────────
choose_ros_net_subnet() {
    # Pick the first candidate /24 that does NOT overlap any existing route
    # (plan §5: 172.16.0.0/12 is common institutional space and an overlap
    # blackholes container→LAN routing). Prints the chosen CIDR on stdout.
    python3 - <<'PY'
import ipaddress, subprocess, sys
candidates = [
    "172.28.0.0/24", "172.29.0.0/24", "172.30.0.0/24", "172.31.0.0/24",
    "10.201.7.0/24", "192.168.221.0/24",
]
existing = []
try:
    out = subprocess.run(["ip", "-4", "route"], capture_output=True,
                         text=True, timeout=5).stdout
    for line in out.splitlines():
        tok = line.split()
        if not tok:
            continue
        net = tok[0]
        if net in ("default", "broadcast", "unicast", "local", "multicast"):
            continue
        try:
            existing.append(ipaddress.ip_network(net, strict=False))
        except ValueError:
            continue
except Exception:
    pass  # no routes readable -> assume nothing overlaps

for cand in candidates:
    c = ipaddress.ip_network(cand)
    if not any(c.overlaps(e) for e in existing):
        print(cand)
        break
else:
    print(candidates[0])  # every candidate overlaps -> fall back to the default
PY
}

seed_env() {
    local subnet
    subnet="$(choose_ros_net_subnet 2>/dev/null || echo '172.28.0.0/24')"
    log "Selected ros_net subnet: ${subnet} (overlap-checked against ip route)."
    if [ -f "$ENV_FILE" ]; then
        log ".env already present at ${ENV_FILE} — leaving it (re-run is non-destructive)."
        return
    fi
    log "Seeding the managed .env at ${ENV_FILE}..."
    ( cd "$INSTALL_DIR" && SUBNET="$subnet" python3 -c \
        'import os; from pi_agent import config_generator as c; c.generate_cloud_only_env(ros_net_subnet=os.environ["SUBNET"])' ) \
        || die "Could not seed ${ENV_FILE} (config_generator failed)."
    chmod 600 "$ENV_FILE"
    chown root:root "$ENV_FILE"
}

# ─── Step 8: systemd units ────────────────────────────────────────────────────
install_units() {
    log "Installing systemd units..."
    install -m 0644 "${SCRIPT_DIR}/systemd/edubotics-pi.service" \
        /etc/systemd/system/edubotics-pi.service
    install -m 0644 "${SCRIPT_DIR}/systemd/edubotics-pi-firstboot.service" \
        /etc/systemd/system/edubotics-pi-firstboot.service
    systemctl daemon-reload
    # The first-boot unit is ENABLED but self-gates on the .first-boot-done
    # marker — on a bench unit it is already satisfied (we touch it below), so it
    # only ever fires on a golden clone after --prepare-golden strips the marker.
    systemctl enable edubotics-pi-firstboot.service >/dev/null 2>&1 || true
    : > "${INSTALL_DIR}/.first-boot-done"
    systemctl enable edubotics-pi.service >/dev/null 2>&1 || true
    systemctl restart edubotics-pi.service
}

# ─── Step 9: pull the -opi images (best-effort, GHCR→Hub fallback) ────────────
pull_images() {
    log "Pulling the -opi container images (GHCR primary, Docker Hub fallback)..."
    if ( cd "$INSTALL_DIR" && python3 -c \
        'import sys; from pi_agent import docker_manager as d; sys.exit(0 if d.pull_images(log=lambda m: print("  " + m)) else 1)' ); then
        log "Images pulled."
    else
        warn "Image pull incomplete — the agent's /update path will retry (needs egress to GHCR/Hub)."
    fi
}

# ─── Step 10: case label (hostname + wired MAC + BLANK IP) ────────────────────
detect_wired_mac() {
    local iface mac
    for iface in /sys/class/net/*; do
        iface="$(basename "$iface")"
        case "$iface" in
            en*|eth*) ;;
            *) continue ;;
        esac
        # A real NIC has a /device symlink (docker0/veth/br do not).
        [ -e "/sys/class/net/${iface}/device" ] || continue
        mac="$(cat "/sys/class/net/${iface}/address" 2>/dev/null || true)"
        if [ -n "$mac" ] && [ "$mac" != "00:00:00:00:00:00" ]; then
            printf '%s' "$mac"
            return 0
        fi
    done
    return 1
}

print_label() {
    local host mac
    host="${HOSTNAME_ASSIGNED:-$(hostname)}"
    mac="$(detect_wired_mac 2>/dev/null || echo '??:??:??:??:??:??')"

    printf '\n'
    printf '============================================================\n'
    printf '  EduBotics Orange Pi — Etikett/QR für das Gehäuse\n'
    printf '============================================================\n'
    printf '\n'
    printf '  Hostname:  \033[1;33m%s.local\033[0m\n' "$host"
    printf '  MAC (LAN): \033[1;33m%s\033[0m\n' "$mac"
    printf '  IP:        ________________   (von der IT reserviert eintragen)\n'
    printf '\n'
    printf '  Aufruf im Browser:  http://%s.local/   (oder http://<IP>/)\n' "$host"
    printf '\n'
    printf '  Hinweis für die IT: Hostname + MAC genügen für eine\n'
    printf '  DHCP-Reservierung und die NAC/802.1X-Freigabe — siehe die\n'
    printf '  Netzwerk-Anleitung (docs/ORANGE_PI_IT_NETZWERK.md).\n'
    printf '\n'
    if command -v qrencode >/dev/null 2>&1; then
        printf '  QR-Code (Wizard-URL):\n'
        qrencode -t ANSIUTF8 "http://${host}.local/" 2>/dev/null || true
    fi
    printf '============================================================\n'
}

# ─── Orchestration ────────────────────────────────────────────────────────────
HOSTNAME_ASSIGNED=""

install_docker
install_packages
install_udev
enable_zram
assign_hostname
install_agent_tree
seed_env
install_units
pull_images

# Verify the agent actually came up before we print the label — otherwise a
# teacher labels a rig whose wizard never serves.
sleep 5
if ! systemctl is-active --quiet edubotics-pi.service; then
    err "The edubotics-pi agent service did not start."
    err "Logs: journalctl -u edubotics-pi -n 50"
    systemctl status edubotics-pi.service --no-pager 2>/dev/null || true
    exit 1
fi

log "Agent running. Web wizard: http://$(hostname).local/"
print_label
