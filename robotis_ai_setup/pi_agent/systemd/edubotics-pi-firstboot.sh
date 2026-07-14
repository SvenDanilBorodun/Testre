#!/usr/bin/env bash
# EduBotics Orange Pi — golden-image first-boot personalization (run ONCE).
#
# This is the fleet-rollout analogue of the Windows .exe installer's per-machine
# setup (deploy plan §7, Phase 2): a single golden eMMC/NVMe image is captured
# from one bench-provisioned unit and cloned onto every Pi. On the FIRST boot of
# each clone this script makes the unit unique so it can coexist on one school
# LAN — the one thing mDNS cannot survive is two rigs answering to the same
# `edubotics-NN.local`, so uniqueness is DERIVED from the machine-id, never
# hand-assigned.
#
# Invoked by edubotics-pi-firstboot.service, ordered BEFORE the agent + avahi,
# and gated so it runs exactly once (ConditionPathExists=!/opt/edubotics/.first-boot-done).
#
# Prep the golden image with `setup.sh --prepare-golden` before capture: that
# truncates /etc/machine-id (so systemd regenerates a fresh one early on every
# clone) and removes the .first-boot-done marker (so this unit fires again).
#
# English by intent: a provisioning/maintainer tool, not a student surface.

set -euo pipefail

log() { printf '[firstboot] %s\n' "$*"; }

INSTALL_DIR="/opt/edubotics"
ENV_DIR="/etc/edubotics"
STATE_DIR="/var/lib/edubotics"
DONE_MARKER="${INSTALL_DIR}/.first-boot-done"

# ── 1. Ensure a UNIQUE machine-id exists ─────────────────────────────────────
# When the golden image was captured with an empty /etc/machine-id, systemd has
# already regenerated it in early boot (systemd-machine-id-setup). This block is
# a defensive fallback for images captured WITHOUT that prep — it re-seeds a
# fresh id so clones don't all share the bench unit's identity.
if [ ! -s /etc/machine-id ]; then
    log "machine-id empty/missing — regenerating"
    rm -f /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
    systemd-machine-id-setup >/dev/null 2>&1 || true
fi
MACHINE_ID="$(cat /etc/machine-id 2>/dev/null || true)"
if [ -z "$MACHINE_ID" ]; then
    MACHINE_ID="$(hostname 2>/dev/null || echo edubotics)"
fi

# ── 2. Derive + assign the unique hostname edubotics-NN ──────────────────────
# NN = 8-digit decimal from the first 24 bits of sha256(machine-id). The label
# printed by setup.sh carries the .local name; the teacher reads the reserved IP
# off the System window, so a wide-but-readable numeric suffix is the tradeoff.
# (24 bits — 16.7M space — keeps a ~100-rig fleet collision-free where 16 bits
# was ~7%; the bench operator can still override with EDUBOTICS_HOSTNAME set.)
NN="$(printf '%s' "$MACHINE_ID" | sha256sum | head -c6 \
      | python3 -c 'import sys; print("%08d" % (int(sys.stdin.read(), 16)))' 2>/dev/null || echo "00000000")"
NEW_HOST="edubotics-${NN}"
log "assigning hostname ${NEW_HOST}"
if command -v hostnamectl >/dev/null 2>&1; then
    hostnamectl set-hostname "$NEW_HOST" || printf '%s\n' "$NEW_HOST" > /etc/hostname
else
    printf '%s\n' "$NEW_HOST" > /etc/hostname
    hostname "$NEW_HOST" 2>/dev/null || true
fi
# Keep /etc/hosts' 127.0.1.1 line in sync so local name resolution matches.
if grep -qE '^\s*127\.0\.1\.1' /etc/hosts 2>/dev/null; then
    sed -i "s/^\s*127\.0\.1\.1.*/127.0.1.1\t${NEW_HOST}/" /etc/hosts || true
else
    printf '127.0.1.1\t%s\n' "$NEW_HOST" >> /etc/hosts || true
fi

# ── 3. Regenerate per-clone secrets + drop clone-unsafe state ─────────────────
# Fresh SSH host keys so cloned units don't share a private key.
if [ -d /etc/ssh ]; then
    log "regenerating SSH host keys"
    rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
    ssh-keygen -A >/dev/null 2>&1 || true
fi
# The phone-camera self-signed cert is re-minted on demand by the agent; a
# per-clone cert (not the bench one) is minted lazily on first use.
rm -rf "${ENV_DIR}/phone-cert" 2>/dev/null || true
# Persisted per-machine state that must re-derive from the NEW machine-id.
rm -f "${STATE_DIR}/.ros_domain_id" "${STATE_DIR}/.last_image_pull.json" 2>/dev/null || true

# ── 4. Regenerate the managed .env (drop the bench HF token + stale arm ports) ─
# The agent unit's EnvironmentFile=/etc/edubotics/.env is NON-optional, so the
# file MUST exist before the agent starts — the agent CANNOT self-seed it (a
# missing EnvironmentFile fails the unit outright). We therefore regenerate it
# UNCONDITIONALLY here (whether or not a golden .env survived --prepare-golden).
# generate_cloud_only_env is atomic (temp file + os.replace), so a failed regen
# leaves any existing file intact — we never `rm` first. The provisioned
# ros_net subnet (chosen at bench time to dodge a LAN overlap) is carried
# forward; the bench HF token is dropped explicitly and the arm ports + stale
# ROS_DOMAIN_ID are dropped (a fresh domain derives from the new machine-id). If
# regen fails AND no file survives, a minimal valid fallback .env is written by
# hand — the agent must never be left with no EnvironmentFile.
SUBNET=""
if [ -f "${ENV_DIR}/.env" ] && [ -d "${INSTALL_DIR}/pi_agent" ]; then
    SUBNET="$(cd "$INSTALL_DIR" && python3 -c \
        'from pi_agent import config_generator as c; print(c.read_env_var("EDUBOTICS_ROS_NET_SUBNET") or "")' \
        2>/dev/null || true)"
fi
log "regenerating ${ENV_DIR}/.env (cloud-only; bench token + arm ports dropped, subnet kept)"
if [ -d "${INSTALL_DIR}/pi_agent" ] && (cd "$INSTALL_DIR" && SUBNET="$SUBNET" python3 -c \
        'import os; from pi_agent import config_generator as c
if os.path.isfile(c.ENV_FILE): c.upsert_env_var("HF_TOKEN", "")
c.generate_cloud_only_env(ros_net_subnet=(os.environ.get("SUBNET") or None))' \
        2>/dev/null); then
    :
elif [ -f "${ENV_DIR}/.env" ]; then
    log "WARNING: could not regenerate .env — keeping the existing file (agent cannot self-seed)"
else
    log "WARNING: could not regenerate .env — writing a minimal fallback (agent cannot self-seed)"
    SUB="${SUBNET:-172.28.0.0/24}"
    GW="$(printf '%s' "$SUB" | python3 -c \
        'import sys, ipaddress; print(ipaddress.ip_network(sys.stdin.read().strip(), strict=False).network_address + 1)' \
        2>/dev/null || echo "172.28.0.1")"
    # Registry pins mirror pi_agent/constants.py defaults (emergency path only —
    # the wizard regenerates the .env properly on the first hardware scan).
    cat > "${ENV_DIR}/.env" <<EOF
# Cloud-only / manager-only mode — first-boot fallback (generator unavailable).
FOLLOWER_PORT=""
LEADER_PORT=""
EDUBOTICS_FOLLOWER_ONLY=0
CAMERA_DEVICE_1=""
CAMERA_NAME_1="gripper"
CAMERA_DEVICE_2=""
CAMERA_NAME_2="scene"
ROS_DOMAIN_ID=30
REGISTRY=ghcr.io/svendanilborodun
REGISTRY_FALLBACK=nettername
IMAGE_TAG=latest
EDUBOTICS_CAMERA_SOURCE=usb_cam
EDUBOTICS_LAN_OPEN=1
EDUBOTICS_BIND_HOST=0.0.0.0
EDUBOTICS_ROS_NET_SUBNET=${SUB}
EDUBOTICS_ROS_NET_GATEWAY=${GW}
EOF
fi
chmod 600 "${ENV_DIR}/.env" 2>/dev/null || true
chown root:root "${ENV_DIR}/.env" 2>/dev/null || true

# ── 5. Re-announce over mDNS with the new hostname ───────────────────────────
systemctl restart avahi-daemon 2>/dev/null || true

# ── 6. Mark done so this one-shot never re-runs on THIS clone ────────────────
mkdir -p "$INSTALL_DIR"
: > "$DONE_MARKER"
log "first-boot personalization complete: ${NEW_HOST}"
