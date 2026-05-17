# Classroom Jetson Orin Nano — Deploy & Pair Runbook

> **For teachers + admins** setting up a shared classroom Jetson for the
> EduBotics inference pipeline. Run-time per Jetson: ~15 min after the
> hardware is on the bench. Repeat per classroom.

## What this gets you

One shared `linux/arm64` inference rig per classroom, so students no
longer need their own follower arm at home to run a trained policy.
Each student moves only the **follower arm + 2 USB cameras** to the
Jetson; the leader arm stays at their desk for recording.

**Scope is narrow** — only the **Inferenz** tab in EduBotics connects
to the Jetson. Roboter Studio (Workshop), Calibration, and Recording
all stay on the student PC. Classrooms without a Jetson see no UI
change.

## Hardware checklist

| Item | Notes |
|---|---|
| NVIDIA Jetson Orin Nano 8 GB Dev Kit | The 4 GB variant is too small for ACT inference at 25 Hz. |
| JetPack 6 (Ubuntu 22.04, L4T r36+) | JetPack 5 is not supported — no `runtime: nvidia` for compose. |
| 64 GB NVMe (or SD card, slower) | Wipe-on-disconnect resets ~5 GB of Docker volumes per session. |
| Classroom Wi-Fi reachable from student PCs | Same subnet, no NAT — the React app connects directly. |
| 1× ROBOTIS OpenMANIPULATOR-X follower arm | Stays at the Jetson permanently. |
| 2× USB cameras (gripper + scene) | Same models as the student-desk setup. |
| 4 free USB-A ports on the Jetson | follower + 2 cameras + 1 free. |

## Pre-flight on a fresh JetPack 6 install

```bash
# Confirm GPU works (any output is fine):
nvidia-smi

# Confirm NVIDIA container runtime is present (ships with JetPack 6):
docker info | grep -i nvidia

# Confirm Docker is running:
sudo systemctl status docker | head -3
```

If any of these fail, fix the JetPack install first — the agent setup
script will refuse to continue otherwise.

## One-time arm64 image build (maintainer only — students don't do this)

Before any teacher can pair their Jetson, the maintainer must push the
arm64 image set to Docker Hub **once**:

```bash
# On a maintainer host with Docker buildx + Docker Hub login.
docker login

# First time: build the arm64 base images (~30-40 min on Mac QEMU).
BUILD_BASE_ARM64=1 PLATFORM=arm64 ./robotis_ai_setup/docker/build-images.sh

# Subsequent updates: pulls existing bases, rebuilds + pushes thin layers.
PLATFORM=arm64 ./robotis_ai_setup/docker/build-images.sh
```

See [docs/arm64_base/README.md](arm64_base/README.md) for the full
walkthrough, including the buildx + QEMU setup and the multi-arch
manifest verification step.

## Teacher walkthrough — install the agent

On the Jetson, with the follower arm + 2 cameras plugged in:

```bash
# 1. Set the Cloud API URL (skip if using the default production deploy).
export EDUBOTICS_CLOUD_API_URL="https://scintillating-empathy-production-9efd.up.railway.app"

# 2. Run the setup script as root.
sudo bash /path/to/robotis_ai_setup/jetson_agent/setup.sh
```

The script will:

1. Verify JetPack 6 + NVIDIA Container Toolkit + Docker.
2. Install Python deps (`websockets`, `python-jose`, `jq`, `qrencode`).
3. Auto-discover the USB cameras + write udev rules so the follower
   gets a stable `/dev/edubotics-follower` symlink.
4. Register the Jetson with the Cloud API → receive an agent_token,
   a 6-digit pairing code, and the shared read-only HF token.
5. Write `/etc/edubotics/jetson.env` (mode 600).
6. Install + start the `edubotics-jetson.service` systemd unit.
7. Print the 6-digit pairing code prominently (plus a QR code for
   mobile teachers).

```
============================================================
  EduBotics Jetson — bereit zum Pairen
============================================================

  Pairing-Code:  423918

  Bitte diesen Code im Admin-Dashboard eingeben, um den
  Jetson einem Klassenzimmer zuzuweisen. Der Code läuft
  in 30 Minuten ab.
============================================================
```

## Teacher walkthrough — pair to a classroom

1. Log in to the EduBotics teacher web app at
   `https://teacher-web-production.up.railway.app` as a **teacher**.
2. Open the classroom you want to bind this Jetson to.
3. The new **Klassen-Jetson** card at the top of the classroom detail
   shows "Kein Jetson gepaart" plus a **Jetson hinzufügen** button.
4. Click **Jetson hinzufügen** → enter the 6-digit code from the
   Jetson's setup.sh stdout → submit.
5. The card now shows the Jetson's `mdns_name`, `lan_ip`,
   `agent_version`, last heartbeat age, and a green **Bereit** pill.
   When a student claims it, the pill turns blue with **Belegt von:
   {ihrer Name} · seit X min**, and a **Lock freigeben** button
   appears for emergency unlock.

Once paired, the **Verbinde mit Klassen-Jetson** button appears on
every classroom student's **Inferenz** tab.

### Other teacher actions on the card

- **Pairing-Code erneuern** — generate a fresh 6-digit code without
  SSHing back to the Jetson. Useful when the prior code expired (30-min
  lifetime) or you want to re-pair the device to a different classroom.
- **Lock freigeben** (only visible when the Jetson is busy) — emergency
  force-release of the current student's session. Used between
  consecutive class periods when 5 minutes (the sweeper window) is too
  long to wait. The student's browser receives the auto-disconnect
  toast within 30 s of the next heartbeat.
- **Vom Klassenzimmer trennen** — unbind the Jetson from this
  classroom. Also force-releases any active owner. The `agent_token`
  on the Jetson is preserved so the same physical device can be
  paired to another classroom without re-running setup.sh.

## Daily classroom flow

1. Student records demos on their **own PC** (leader + follower at the
   desk) as today.
2. Student trains an ACT policy in the cloud (existing flow, untouched).
3. Student moves the **follower arm + 2 USB cameras** to the Jetson.
4. From the school PC's React UI:
   - Open the **Inferenz** tab.
   - The chip shows **Jetson frei** (green).
   - Click **Verbinde mit Klassen-Jetson**.
   - The Aufnahme + Roboter Studio tabs disappear from the sidebar
     while connected (they need the local rosbridge).
   - Click **Start Inference** as usual.
   - Trained policy auto-downloads on the Jetson (~30-60 s first time).
   - Follower moves on the Jetson at ~25 Hz.
5. When done, click **Trennen**. The chip flips to **Jetson wird
   vorbereitet…**; the Jetson wipes itself (~20-30 s) and becomes
   **Jetson frei** for the next student.

## Lock + abandonment recovery

- **Explicit disconnect (Trennen)**: primary release path. Wipes the
  Jetson immediately for the next student.
- **5-minute heartbeat timeout**: if the student's browser crashes,
  PC reboots, or they walk away without clicking Trennen, the
  Cloud API's sweeper auto-releases after 5 minutes of silence.
- **Teacher force-release**: emergency unlock from the admin UI for
  the rare case where 5 minutes is too long.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| `Pairing-Code ungültig oder abgelaufen` | Codes expire after 30 min. Either re-run `sudo bash setup.sh` on the Jetson, or (for an already-paired device) click **Pairing-Code erneuern** in the teacher dashboard. |
| Chip stuck on `Jetson offline` | `systemctl status edubotics-jetson` on the Jetson. Check `journalctl -u edubotics-jetson -f` for heartbeat errors. Network down? Cloud API reachable? |
| Chip stuck on `Jetson belegt von …` and the named user left the room | 5-min sweeper will free it. Teacher can force-release immediately via admin UI. |
| `Verbindung fehlgeschlagen: Lock verloren` mid-inference | The lock changed hands server-side (sweep or teacher force). Click **Verbinde** again. |
| Pull error in agent log | `EDUBOTICS_SKIP_AUTO_PULL=1 systemctl restart edubotics-jetson` falls back to whatever images are cached locally. |
| arm64 base image not found | The maintainer hasn't pushed yet — see [docs/arm64_base/README.md](arm64_base/README.md). Images are at `nettername/{open-manipulator,physical-ai-server}-jetson{,-base}` (v2.3.0 renamed off the `arm64-*` tag suffix). |
| Setup aborts with "Cloud API meldet HS256, hat aber kein supabase_jwt_secret" | The Cloud API on Railway is configured for HS256 auth but `SUPABASE_JWT_SECRET` isn't set on Railway. Operator must add it (Supabase Dashboard → Settings → API → JWT Secret) and restart the Cloud API. Without it the rosbridge proxy cannot verify student JWTs and every WS connection would close with code 4401. |

## Logs + status

```bash
# On the Jetson:
sudo systemctl status edubotics-jetson
sudo journalctl -u edubotics-jetson -f          # live agent logs
docker compose -f /opt/edubotics/docker-compose.jetson.yml ps
docker compose -f /opt/edubotics/docker-compose.jetson.yml logs -f physical_ai_server

# Read the persisted last-image-pull info:
cat /var/lib/edubotics/.last_image_pull.json
```

## Security caveats (v1 — read me)

- **No TLS**: the React app connects to the Jetson via plain `ws://`.
  The JWT auth-op on the agent's proxy gates ROS traffic per student,
  but the WS payload itself (camera streams, JointTrajectory commands)
  is visible to anyone with packet capture on the classroom LAN.
- **Recommendation**: put the Jetson on a separate classroom Wi-Fi
  VLAN (or even a wired-only segment) if security-sensitive. The
  loopback bind on `127.0.0.1:9090` ensures the unauthenticated
  rosbridge itself never reaches the LAN — only the JWT-gated `:9091`
  does.
- **HF token**: the shared read-only `EduBotics-Solutions/*` HF token
  lives in `/etc/edubotics/jetson.env` (mode 600, root-only). If a
  Jetson is compromised, rotate the token via the Cloud API's
  `EDUBOTICS_JETSON_HF_TOKEN` Railway env var.
- **TLS in v2**: tracked as a follow-up. Either self-signed CA
  (installer ships the cert) or Let's Encrypt + per-classroom
  subdomain.
