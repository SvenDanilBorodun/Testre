# EduBotics on Orange Pi 5 Pro — Approved Deployment Plan

> **Status: APPROVED PLAN, implementation pending** (decisions locked
> 2026-07-11). Every file:line reference below was verified against the
> code at v2.12.2 on 2026-07-11. This document is the durable spec for the
> Orange-Pi workstream; per-phase throwaway plans still go to `docs/plans/`
> (gitignored) as usual. When implementation lands, fold the durable
> invariants into `CLAUDE.md` and convert this file into a runbook in the
> style of `docs/JETSON_DEPLOY.md`.

## 1. Goal

Ship the **full student EduBotics experience on an Orange Pi 5 Pro (8 GB)**:
the student plugs leader + follower arms and both USB cameras into the Pi,
does the normal hardware setup (the same wizard the Windows GUI has today,
re-implemented as a web window), and runs recording, teleop with the
collision e-stop, Roboter Studio, dataset management and cloud training —
all served from the Pi and used from a browser. The Pi replaces the
student's Windows PC + WSL2 on the robot side entirely.

### Locked decisions

| Decision | Choice |
|---|---|
| Discovery of many Pis on one school LAN | **mDNS hostnames + printed labels** (`edubotics-NN.local` via avahi, QR/label on the case). No cloud registry work for Pis. |
| Student-PC access / control security | **Open LAN binding, no auth.** Ports 80/8080/9090 bound to the LAN. Mitigations: single env switch back to loopback, and deployment docs that require a dedicated robotics VLAN/SSID (see §8). |
| Inference | **Classroom Jetson only.** The inference code path is not touched (`inference_manager.py` stays byte-identical; Rule §2 untouched). The Pi records and trains; the existing Jetson connect flow executes policies. |

## 2. Hardware & OS

**Orange Pi 5 Pro**: Rockchip RK3588S (4×Cortex-A76 @2.4 GHz + 4×A55),
8 GB LPDDR5, Mali-G610 GPU (**no CUDA — ever**), 6-TOPS NPU (INT8/RKNN —
unusable by LeRobot, explicitly a non-goal), M.2 NVMe + eMMC socket,
GbE, Wi-Fi 5. USB: **1× USB3.1 Gen1 + 3× USB2.0 Type-A, two of which sit
behind an internal USB2 hub**. Port budget: 2 arms (serial, negligible
bandwidth → hub ports) + 2 cameras (see §9 for the bandwidth plan).

**OS**: Armbian (recommended — actively maintained for the 5 Pro) or the
official Orange Pi Ubuntu 22.04 BSP image. The formerly popular
Joshua-Riek `ubuntu-rockchip` project was **archived April 2026** — do not
build on it. Host requirements are thin: pinned Docker + compose, udev,
avahi-daemon, zram, and the stock `cdc_acm`/`uvcvideo` modules.

## 3. Architecture

```
Student PC (browser only)                Orange Pi 5 Pro  (edubotics-NN.local)
┌──────────────────────────┐       ┌─────────────────────────────────────────┐
│ http://edubotics-07.local │──:80─▶│ physical_ai_manager (nginx, arm64)      │
│  React app (host-relative │       │   └─ /api/system → pi-agent (same-origin)
│  URLs already)            │─:9090▶│ rosbridge  (physical-ai-server-opi)     │
│                           │─:8080▶│ web_video_server                        │
└──────────────────────────┘       │ pi-agent   (native python, systemd)     │
        │                          │ open_manipulator-opi ── /dev/ttyACM*    │
        │ Inferenz tab only        │                      ── usb_cam (v4l2)  │
        ▼                          └─────────────────────────────────────────┘
Classroom Jetson :9091 (JWT, unchanged)     Cloud: Railway/Modal/Supabase/HF
```

Key facts the architecture leans on (all verified):

- **The React app is already host-relative.** `StudentApp.js:88` seeds
  `rosHost` from `window.location.hostname`; `rosSlice.js:31-34` derives
  `ws://<host>:9090`; camera stream URLs derive from the same `rosHost`
  (`CameraFeedOverlay.jsx:51`). A browser on `http://<pi>/` reaches
  rosbridge and camera streams with **zero frontend URL changes** once the
  compose ports are LAN-bound.
- **The native-Linux camera path already exists.** `entrypoint_omx.sh:103-135`
  detects non-WSL kernels (`uname -r` grep `microsoft`) → `EDUBOTICS_CAMERA_SOURCE=usb_cam`
  with the `yuyv` default (the `mjpeg2rgb` flip at `:147-149` is WSL2-only).
  The Windows capture bridge (:5557), usbipd, and the WSL keepalive all
  simply do not exist on the Pi.
- **Teleop + collision e-stop work unchanged** — both arms plug into the
  Pi; the 100 Hz ros2_control loop, mixed-servo effort fractions,
  leader-gate and two-step recovery are architecture-neutral.
- **`ROS_DOMAIN_ID`** is derived per-machine from `/etc/machine-id`
  (hash mod 233) exactly as `jetson_agent/setup.sh:183-184` does — 30 Pis
  on one LAN cannot DDS-cross-talk.
- **Inference**: the Inferenz tab's existing Jetson flow
  (`useJetsonConnection.js`, `PROXY_PORT = 9091` at `:37`, JWT first-frame
  auth in `rosConnectionManager.js`) is used as-is. While connected to a
  Jetson, `jetsonIncompatible` tabs (Aufnahme/Daten/Roboter Studio,
  `StudentApp.js:269-276`) hide — as today; disconnecting restores them.
  The CUDA gate at `inference_manager.py:141` (constructed `device='cuda'`
  at `physical_ai_server.py:744`) is **deliberately left in place** on the
  Pi: local inference refuses, which matches the decision.

## 4. Workstream 1 — the `-opi` image flavor

The existing arm64 images are Jetson-only: `Dockerfile.arm64:1` builds
`FROM robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0` (L4T CUDA) with
the Jetson AI Lab cu126 pip index (`Dockerfile.arm64:52-53`) — unusable on
Rockchip. Work items:

1. **New base** `physical_ai_tools/physical_ai_server/Dockerfile.arm64cpu`:
   - `FROM ros:jazzy-ros-base` (stock arm64), plain PyPI (aarch64 torch
     wheels on PyPI are CPU-only — no `+cpu` local-tag dance, no SLIM_CUDA
     step needed because there is no CUDA to strip).
   - Same pins as the sibling Dockerfiles: `lerobot[pi,smolvla,peft]==0.5.1`,
     `numpy==1.26.4` force-reinstall after lerobot, `scipy>=1.14.0,<1.18`,
     `ros-jazzy-control-msgs` apt (collision e-stop fail-open guard),
     s6-overlay-aarch64. **Rule §5's LeRobot pin lockstep grows to a
     fourth site** — update the CLAUDE.md list in the same PR.
   - The open_manipulator arm64 base has no torch; assess reuse first,
     fork only if the L4T base leaks in.
2. **Thin overlays: no new files.** Both
   `robotis_ai_setup/docker/{physical_ai_server,open_manipulator}/Dockerfile`
   are `ARG BASE_IMAGE`-parameterized and arch-neutral; the 7-file overlay
   chain, COPY-wholesale staging, forbidden-file asserts and all four
   build-time smoke gates apply as-is (Rule §3 intact).
3. **`build-images.sh`**: add a `PLATFORM=opi` case beside
   amd64/arm64 (`build-images.sh:52-94`) → bases
   `*-opi-base`, output repos `open-manipulator-opi` /
   `physical-ai-server-opi`. Run `flatten_amd64_image`'s logic for this
   flavor too (rename accordingly) — unlike the Jetson image this one
   *should* be slim (~5-6 GB target).
4. **Manager for arm64**: `build-images.sh:253` currently skips the
   manager on arm64. The manager Dockerfile is `node:22` + nginx — both
   multi-arch official images — so an arm64 build is trivial. Publish as a
   multi-arch manifest on the existing `physical-ai-manager` repo (or an
   `-opi` twin; decide at implementation, thread through retag either way).
5. **CI (`docker-publish.yml`)**: third matrix entry (`platform: opi`,
   `runner: ubuntu-24.04-arm`) in both `build` (`:134-139`) and
   `smoke-test` (`:495-503`); extend `AMD64_REPOS`/`ARM64_REPOS`
   (`:274-275`) with the opi repos in `retag`; add an **opi size gate**
   (the 11 GB amd64 gate at `:549` exists to catch un-slimmed CUDA — the
   opi image needs its own ceiling, ~7 GB); run
   `image_source_parity.sh` for the flavor.
6. **Write the missing `docs/arm64_base/README.md`** — referenced five
   times by `build-images.sh` (e.g. `:44`, `:382`, `:511`) but absent from
   the tree; document both the Jetson and the opi base builds.

## 5. Workstream 2 — pi-agent + `docker-compose.opi.yml`

A native Python systemd service `robotis_ai_setup/pi_agent/` — the Jetson
agent's skeleton (systemd unit, scrubbed-env compose driver, arm64
digest-checked auto-pull with GHCR→Hub fallback) merged with the GUI's
platform-neutral brain. Ports/marks from the verified GUI inventory:

**Ported nearly verbatim** (from `robotis_ai_setup/gui/app/`):
`config_generator.py` (managed `.env` model — `MANAGED_KEYS` at `:24`,
prefixes `:55`, atomic writes, `HF_TOKEN` deliberately unmanaged with
`upsert_env_var` as sole writer `:249`; env file moves to
`~/.config/edubotics/.env`), `docker_manager`'s pull/update/digest logic
(**flip the digest pre-check from `linux/amd64` — `docker_manager.py:336-365`
— to arm64**; the Jetson agent already has the arm64 variant),
`factory_reset` (volume-suffix rm of `ai_workspace`/`huggingface_cache`/
`edubotics_calib`), `ensure_environment_stopped` (still required: the
Dynamixel bus must be free before every arm scan),
`roboter_studio_control.py`'s endpoint contract, `phone_camera.py`
(pure-stdlib HTTPS receiver; cert minted with `openssl` instead of
PowerShell), `update_checker`'s cloud `/version` + SHA-256 gate,
`identify_arm.py` (runs in a throwaway
`docker run --privileged -v /dev:/dev` scanner container, same as today),
and the v4l2 enumeration + identical-serial dedup logic from
`wsl_bridge.list_video_devices` (run natively, no `wsl -d` wrapping).

**Deleted on the Pi** (Windows/WSL artifacts): `usbipd_resolver.py` and
all usbipd attach/bind, `wsl_bridge`'s distro tunneling, keepalive,
`win_camera.py` (MSMF), `camera_bridge.py` (:5557), `webview_window.py`
(WebView2), all `_elevate_and_wait`/PowerShell/UAC repair flows, the
camera-privacy registry check. Guided repairs collapse to udev-rule +
`dialout`/`video` group checks.

**Management API**, reverse-proxied **same-origin** by the manager's
nginx at `/api/system/` (works identically on-device and over LAN, no
CORS/Origin dance): status, scan-arms, camera scan/roles/MJPEG-previews,
phone-camera toggle, HF token, start/stop environment, update
(image pull + agent self-update from a SHA-256-verified release tarball),
factory reset (double-confirm), Protokoll (SSE, with the existing secret
redaction), and the Roboter-Studio endpoints preserving the exact
`roboter_studio_control.py` JSON contract (`/status`, `/leader-enable`,
`/leader-disable`, busy/ready guards, `.env` rollback on failed restart).

**Lifecycle model: the GUI-owner model, not the Jetson model.** The Jetson
is a cloud-lock-driven shared appliance (`restart: unless-stopped`); the
Pi is a personal rig — the stack comes up only on „Umgebung starten" in
the System window, compose services stay `restart: "no"`, and the agent
runs `ensure_environment_stopped` before every arm scan (the serial-bus
lesson from `docker-compose.yml:6` applies unchanged).

**`docker-compose.opi.yml`** — derived from the Jetson compose minus its
NVIDIA lines (`docker-compose.jetson.yml:24`, `:83`), plus the manager as
a third service:

- Images `${REGISTRY}/…-opi:${IMAGE_TAG}`; same `${REGISTRY}`/fallback
  interpolation and `.env` interface as today.
- **mem_limit 1.5g / 5g / 512m** (today's student file is 2g/6g/512m
  = 8.5 GB and the Jetson file 3g/6g = 9 GB — both over-commit an 8 GB
  board). **memlock cut from 8428281856 (~7.85 GB, present in both
  existing composes) to 2 GB**; keep `SYS_NICE` + rtprio for the 100 Hz
  loop. zram swap is mandatory at provisioning.
- Healthchecks, tzdata ro-mounts, `.s6-keep` mount, `ros_net`, and the
  ~50-entry `EDUBOTICS_*` `environment:` forwarding lists carried over —
  every new env var must join those lists (`env-forwarding-guard`).
- **Ports**: `80`, `8080`, `9090` bound to the LAN per the locked
  decision, behind one managed env switch (see §8). `5557` does not exist
  (no capture bridge); `8769` is replaced by the same-origin `/api/system`.

**React fix required**: `LeaderToggle.jsx:47` and `RunControls.jsx:47`
hardcode `RS_CONTROL_BASE = 'http://localhost:8769'` — in Pi mode these
route to the host-relative `/api/system/roboter-studio/...` path instead
(from a remote browser, `localhost:8769` resolves to the student's own PC
and the toggle silently self-hides today).

## 6. Workstream 3 — the React „System"-Fenster (the GUI, in the browser)

A new window/tab, visible only in Pi mode (runtime-gated like `?cloud=1` /
the Jetson connection state; mechanism decided at implementation — URL
param the manager's Pi nginx config appends, mirroring how the GUI
appends `?cloud=1` today, or a runtime probe of `/api/system/status`).
Feature-for-feature parity with `EduBotics.exe`:

| GUI today | System window |
|---|---|
| Modus (cloud-only checkbox) | kept (compose up of manager only) |
| Schritt A/B „Arme scannen" + guided repair | scan via scanner container + `identify_arm.py`; repairs = udev/group checks; leader/follower ports persisted as managed keys; fast-rehydrate on revisit |
| Schritt C Kameras: Scan, Rollen (Greifer/Szene), Vorschau | v4l2 by-id/by-path enumeration incl. identical-serial dedup; MJPEG `<img>` previews from the agent; previews stop before the stack claims devices |
| Handy als 3. Kamera (:8444) | ported as-is (`0.0.0.0:8444` HTTPS, openssl cert) |
| Schritt D HF-Token | same upsert, same „✓ Token gespeichert" semantics |
| „Umgebung starten"/„Stoppen" + start-gate | same gating (prerequisites ∧ both arms identified ∨ cloud-only) |
| Update-Gate | cloud `/version` check; image pulls via digest pre-check + agent tarball self-update replace the `.exe` download |
| „Web-Oberfläche öffnen" | not needed — the user is already in the browser |
| „Daten zurücksetzen" | identical volume wipe, double-confirm |
| Protokoll | SSE log panel, secret redaction preserved |

All student-facing strings in German with literal umlauts (Rule §1;
`german-strings-lint` covers `[FEHLER]`/`[WARNUNG]`/`[STOPP]` lines).

## 7. Workstream 4 — provisioning & fleet

- **Phase 1: `pi_agent/setup.sh`**, mirroring `jetson_agent/setup.sh`
  minus its NVIDIA hard-checks (`setup.sh:40-46`): install pinned Docker,
  udev rules (ROBOTIS VID `2F5D` symlinks — reuse
  `jetson_agent/udev/99-edubotics-robotis.rules`), avahi + hostname
  assignment (`edubotics-NN`), zram, agent + systemd unit, image pull,
  print the label/QR for the case.
- **Phase 2: golden eMMC/NVMe image** („flash → boot → ready"):
  bench-provision one unit, capture, per-unit first boot regenerates
  machine-id (which re-derives `ROS_DOMAIN_ID`), hostname, and secrets.
  This is the `.exe`-installer equivalent for fleet rollout.
- **Updates**: images via the digest-checked auto-pull; agent via
  SHA-256-verified release tarball (reusing the `update_checker` logic);
  OS via unattended-upgrades. `release.yml` gains the opi images in W4
  and the agent tarball as a W5-adjacent release asset.

## 8. Security posture (deliberate, decided)

With open LAN binding and no auth, **anyone on the same network segment
can drive any arm, watch every camera, and call the management API
(including Stoppen and Daten zurücksetzen) on any rig**. This was an
explicit product decision (2026-07-11). Two mitigations ship anyway
because they are nearly free:

1. **One env switch**: `EDUBOTICS_LAN_OPEN` (managed key, default `1` on
   the Pi). `0` rebinds all published ports to `127.0.0.1` (kiosk-style
   use with a local monitor still works fully). The compose keeps the
   bind host in a variable so this is a `.env` regenerate, not a file
   edit.
2. **Deployment docs (German, for teachers)** require a **dedicated
   robotics VLAN/SSID** — which also serves mDNS reliability: `.local`
   resolution across VLANs or client-isolated Wi-Fi is exactly where mDNS
   fails, so the isolation requirement carries both the safety and the
   discovery story. Wired ethernet for the Pis is recommended.

The Jetson JWT proxy (`rosbridge_proxy.py`, `0.0.0.0:9091`, alg-pinned /
issuer-pinned / owner-matched) remains a proven drop-in if this posture
is ever revisited; nothing in this plan forecloses it.

## 9. Memory & USB budget

- **RAM**: 1.5 + 5 + 0.5 GB caps = 7 GB, ~1 GB for the OS; zram absorbs
  transient spikes. No local policy loads (Jetson-only inference) keeps
  the server container's peak well under its cap during recording
  (streaming h264 encode, ~2-3 of 8 cores for 2×640×480@30).
- **USB**: one camera on the USB3 port, one on the standalone USB2 port,
  arms on the hub ports. Native default stays **YUYV** —
  `entrypoint_omx.sh`'s own comments (`:79-91`) note `mjpeg2rgb` burns
  ~60 % of an ARM core per stream, so it is the *fallback* (existing
  `EDUBOTICS_CAMERA_PIXEL_FORMAT` env knob), not the default, if a rig's
  port layout forces both cameras onto one USB2 bus (2×YUYV@640×480@30
  ≈ 35 MB/s would saturate it).

## 10. Phases & acceptance criteria

| Phase | Scope | Done when |
|---|---|---|
| **P1 — Images** | `Dockerfile.arm64cpu`, `PLATFORM=opi`, arm64 manager, CI matrix/retag/parity/size-gate, `docs/arm64_base/README.md` | CI publishes `*-opi:latest` to GHCR+Hub; smoke-test passes arch/size/parity gates; images boot on a bench Pi |
| **P2 — pi-agent + compose** | Agent port, management API, `docker-compose.opi.yml`, nginx `/api/system` proxy, `EDUBOTICS_LAN_OPEN` | Full wizard→start→record cycle driven purely via `curl` against the API on a bench Pi |
| **P3 — System window** | Pi-mode gating, wizard UI, Start/Stopp/Update/Reset/Protokoll, camera previews, host-relative LeaderToggle/RunControls | A student can go from freshly flashed Pi to a recorded + uploaded dataset using only a browser |
| **P4 — Provisioning + pilot** | `setup.sh` → golden image, labels/QR, teacher docs (VLAN), rig pilot | Pilot checklist in §12 fully green on ≥2 units |

## 11. Non-goals (this round)

- **Local inference on the Pi** (CPU or NPU/RKNN). Trigger to revisit:
  schools without a Jetson demanding on-Pi execution, or LeRobot gaining
  a viable quantized/compiled ACT path (~2.5-6 Hz effective was the
  CPU estimate — demo-grade only).
- **Cloud pairing/registry for Pis** (Jetson-style `device_type`
  generalization, teacher-dashboard rig overview). Trigger: fleets where
  label-based discovery breaks down, or the security posture is revisited.
- **Any auth on the Pi's LAN surface** (JWT proxy, claim lock, PIN).
  Trigger: an incident, or a school that cannot provide an isolated VLAN.
- **Kiosk mode as a shipped feature** (works implicitly via
  `EDUBOTICS_LAN_OPEN=0` + a local browser, but no packaged
  autostart/kiosk config this round).
- **Phone-camera recording integration** (unchanged from the existing
  parked item — `camera_topic_list` rework).
- **Offline installs** — the Pi path is online-only, like the product.

## 12. Risks & rig-validation checklist (pilot gates)

1. **USB bandwidth**: 2 cameras + 2 arms across the 5 Pro's port layout;
   verify no `VIDIOC_DQBUF` timeouts at YUYV 640×480@30 and no Dynamixel
   jitter at 100 Hz under camera load.
2. **Thermals**: sustained 2-stream h264 encode + teleop on RK3588S wants
   a heatsink/fan; verify no throttling over a 20-min recording session.
3. **BSP kernel quirks**: uvcvideo timestamping and cdc_acm stability on
   the chosen Armbian/BSP kernel; pin the known-good OS image version.
4. **mDNS in the field**: `.local` resolution from managed Windows
   student PCs on the school's actual network (the VLAN requirement in §8
   is the fallback).
5. **8 GB headroom**: record a long episode while the manager serves a
   second browser; no OOM kills (watch `pids_limit`/`mem_limit` events).
6. **Jetson interop**: Pi-served frontend → classroom Jetson `:9091`
   connect/claim/inference end-to-end (plain-HTTP origin keeps `ws://`
   legal — this is why the Pi serves the app over HTTP, and why serving
   the student SPA from an HTTPS cloud origin is off the table).
7. **CI cost**: arm64 runner minutes for a third flavor; acceptable on
   the current plan, monitor after the first releases.

## 13. Repo-rule compliance notes

- **Rule §1**: all new student/teacher-facing strings German (umlauts
  literal); agent/internal logs English.
- **Rule §2**: no inference-path or safety-envelope changes; collision
  e-stop, torque-disable trap, xacro limits, sync tolerances untouched.
- **Rule §3**: COPY-wholesale + overlay chain unchanged; the opi flavor
  only adds a base and build wiring; `image_source_parity.sh` extended to
  cover it.
- **Rule §5**: LeRobot `0.5.1` pin gains a fourth site
  (`Dockerfile.arm64cpu`) — CLAUDE.md pin list updated in the same PR;
  numpy/scipy caps replicated.
- **Rule §6**: all deploys via the existing workflows; opi images ride W4;
  no new manual deploy surfaces.
- **CI guards to extend**: `env-forwarding-guard` (new `EDUBOTICS_*` vars
  in compose `environment:` lists), `compose-validate` (validate
  `docker-compose.opi.yml` + its `.s6-keep` mount), `german-strings-lint`
  (pi-agent strings), `shell-lint` (`pi_agent/setup.sh`).
