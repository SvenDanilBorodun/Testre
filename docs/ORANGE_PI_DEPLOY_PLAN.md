# EduBotics on Orange Pi 5 Pro — Approved Deployment Plan

> **Status: APPROVED PLAN, implementation pending** (decisions locked
> 2026-07-11). Every file:line reference below was verified against the
> code at v2.12.2 on 2026-07-11. **Rev. 2 (2026-07-11)**: amended after a
> full-code + web review — two-tier lifecycle carve-out for the manager
> (§5), decided `/api/system` proxy mechanics (§5), decided Pi-mode
> gating + `physical-ai-manager-opi` twin (§4/§6), explicit torch pin +
> flatten/CI corrections (§4), agent-update cloud fields (§7), and
> factual fixes throughout. **Rev. 3 (2026-07-12)**: second full
> verification pass (code + PyPI metadata + web, all rev.-2 claims
> re-checked) — StartupGate carve-out (§6, rev. 2's lifecycle fix was
> incomplete without it), Pi-mode gating hardened to a baked static
> marker + JSON-guarded probe (§6), the compose recipe restated as a
> three-way merge checklist (§5 — the Jetson file is follower-only and
> would silently drop leader support, the calib volume and the whole
> recording env surface), `ros_net` subnet made configurable + a
> provisioning overlap check (§5/§7), SSE/MJPEG proxy-buffering
> mechanics (§5), update in-flight semantics (§5/§6), agent runtime
> identity pinned to `/etc/edubotics` (§5/§7), Host/Origin allowlist on
> mutating endpoints (§8), IP-literal mDNS fallback (§8),
> `REACT_APP_BUILD_ID` stamping for the opi manager (§4), digest-check /
> `nginx.web.conf.template` / CUDA-gate citation fixes. The
> **phone-camera-on-usb_cam question REMAINS OPEN** (owner decision
> pending) — §6 now marks it as such instead of claiming a straight
> port. **Rev. 4 (2026-07-12)**: school-network permissions worked
> out — §8 gains the three-tier network model (IT-configured VLAN /
> own robotics router / kiosk) and the exact IT work order (port
> table, egress FQDN allowlist, TLS-inspection + proxy exemptions,
> NAC); product-side: MAC on the case label + blank IP field (§7),
> LAN IP shown in the System window (§6), a wizard „Netzwerk-Check"
> agent endpoint (§5/§6), network-aware Pi-mode disconnect wording
> (§6), and the German IT one-pager as a named P4 deliverable
> (§7/§10). This document is the durable spec for the
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
| Student-PC access / control security | **Open LAN binding, no auth.** Ports 80/8080/9090 bound to the LAN. Mitigations: single env switch back to loopback, Host/Origin allowlist on mutating management endpoints, and the §8 three-tier network guide (dedicated VLAN / own robotics router / kiosk) with its IT work order. |
| Inference | **Classroom Jetson only.** The inference code path is not touched (`inference_manager.py` stays byte-identical; Rule §2 untouched). The Pi records and trains; the existing Jetson connect flow executes policies. |

## 2. Hardware & OS

**Orange Pi 5 Pro**: Rockchip RK3588S (4×Cortex-A76 @2.4 GHz + 4×A55),
8 GB LPDDR5, Mali-G610 GPU (**no CUDA — ever**), 6-TOPS NPU (INT8/RKNN —
unusable by LeRobot, explicitly a non-goal), M.2 NVMe + eMMC socket,
GbE, Wi-Fi 5. USB: **1× USB3.1 Gen1 + 3× USB2.0 Type-A, two of which sit
behind an internal USB2 hub**. Port budget: 2 arms (serial, negligible
bandwidth → hub ports) + 2 cameras (see §9 for the bandwidth plan).

**OS**: Armbian (recommended — **community-maintained** builds for the
5 Pro, two kernel lines: vendor 6.1.x BSP and a mainline-based "current"
line. Pin ONE exact known-good image per §12.3 — the vendor 6.1.x line
has a known PCIe regression in Armbian releases >24.8.1, which matters
if the golden image boots from NVMe; the mainline line is the
workaround) or the
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
  (`CameraFeedOverlay.jsx:86` and `ImageGridCell.js:172` — the only two
  stream-URL construction sites in the app, covering Roboter Studio,
  Aufnahme and Inferenz). A browser on `http://<pi>/` reaches
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
  (hash mod 233) exactly as `jetson_agent/setup.sh:183-184` does. Honest
  rationale: Pis cannot DDS-cross-talk on the LAN **regardless** —
  `ros_net` is a docker **bridge** network in every DEPLOYMENT compose
  (student, gpu overlay, Jetson), so DDS multicast never leaves the
  host. Caveat for precision: the two VENDORED upstream ROBOTIS composes
  (`open_manipulator/docker/docker-compose.yml:16`,
  `physical_ai_tools/docker/docker-compose.yml:8,:21`) DO use
  `network_mode: host` and amd64-tagged images — a Pi bring-up must
  never start from those files. The
  derivation is kept for fleet-convention consistency and host-side ROS
  tooling, NOT as the isolation mechanism (with 30 rigs mod 233 a
  birthday collision between two Pis is likely anyway — and harmless).
- **Inference**: the Inferenz tab's existing Jetson flow
  (`useJetsonConnection.js`, `PROXY_PORT = 9091` at `:37`, JWT first-frame
  auth in `rosConnectionManager.js`) is used as-is. While connected to a
  Jetson, `jetsonIncompatible` tabs (Aufnahme/Daten/Roboter Studio,
  `StudentApp.js:269-276`) hide — as today; disconnecting restores them.
  The CUDA gate at `inference_manager.py:141` (it lives in
  `load_policy()`, and the `'cuda'` comes from the DEFAULT parameter at
  `inference_manager.py:35` — `physical_ai_server.py:744` constructs
  `InferenceManager()` bare) is **deliberately left in place** on the
  Pi: local inference refuses, which matches the decision. Side note:
  `get_available_policies()` also excludes SmolVLA on any aarch64 box
  (`inference_manager.py:385-386`, written for the Jetson but keyed on
  `platform.machine()`), which is moot here since inference refuses
  anyway.

## 4. Workstream 1 — the `-opi` image flavor

The existing arm64 images are Jetson-only: `Dockerfile.arm64:1` builds
`FROM robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0` (L4T CUDA) with
the Jetson AI Lab cu126 pip index (`Dockerfile.arm64:52-53`) — unusable on
Rockchip. Work items:

1. **New base** `physical_ai_tools/physical_ai_server/Dockerfile.arm64cpu`:
   - `FROM ros:jazzy-ros-base` (stock arm64), plain PyPI. **Pin
     `torch==2.7.0` explicitly** (plus its matching torchvision) — the
     sibling Dockerfiles inherit torch from their BASE images, so "same
     pins as the siblings" does NOT cover it, and `lerobot==0.5.1`
     allows `torch<2.11`: an unpinned resolve installs 2.10.x and
     silently breaks the "one PyTorch surface (torch 2.7.x)" invariant.
     torch 2.7.0's PyPI aarch64 wheel is genuinely CPU-only (~99 MB,
     zero `nvidia-*` deps — verified against PyPI metadata; the
     CUDA-by-default flip for aarch64 PyPI wheels happened in torch
     2.11.0), so no `+cpu` local-tag dance and no SLIM_CUDA step are
     needed **at this pin**. Any future torch bump must switch to the
     `download.pytorch.org/whl/cpu` index (it carries aarch64 `+cpu`
     wheels) or the image silently grows the full CUDA/SBSA payload.
   - Video decode: LeRobot 0.5.1 excludes `torchcodec` on Linux aarch64
     via an environment marker and falls back to PyAV (`av` is an
     unconditional dep with manylinux aarch64 wheels) — the install
     works out of the box on plain PyPI; decode-speed caveat in §9.
   - Same pins as the sibling Dockerfiles: `lerobot[pi,smolvla,peft]==0.5.1`,
     `numpy==1.26.4` force-reinstall after lerobot, `scipy>=1.14.0,<1.18`,
     `ros-jazzy-control-msgs` apt (collision e-stop fail-open guard),
     s6-overlay-aarch64. **Rule §5's LeRobot pin lockstep grows to a
     fourth site** — update the CLAUDE.md list in the same PR.
   - The open_manipulator arm64 base has no torch; assess reuse first,
     fork only if the L4T base leaks in.
2. **Thin overlays: no new files.** Both
   `robotis_ai_setup/docker/{physical_ai_server,open_manipulator}/Dockerfile`
   are `ARG BASE_IMAGE`-parameterized. The open_manipulator one is
   arch-neutral; the physical_ai_server one is arch-**safe** rather than
   arch-neutral — it carries the amd64-only `SLIM_CUDA` torch-swap block
   (`:263-287`, a no-op at the default `SLIM_CUDA=0`, which is what the
   opi build passes) and Jetson-base ENV scrubs
   (`TWINE_*`/`SCP_UPLOAD_*`, `:442-449`, harmless no-ops on other
   bases). The 7-file overlay chain, COPY-wholesale staging,
   forbidden-file asserts and all four build-time smoke gates apply
   as-is (Rule §3 intact; none of the four gates touches CUDA — they
   run identically on CPU torch).
3. **`build-images.sh`**: add a `PLATFORM=opi` case beside
   amd64/arm64 (`build-images.sh:52-94`) → bases
   `*-opi-base`, output repos `open-manipulator-opi` /
   `physical-ai-server-opi`. Reuse `flatten_amd64_image`'s logic for
   this flavor, but a rename is NOT enough: the function hardcodes
   `--platform linux/amd64` on BOTH `docker create` and `docker import`
   (`build-images.sh:161-162`) — generalize it to take the target
   platform as a parameter (keep the `ROS_DISTRO=jazzy` drift assert
   and the `--change` config preservation), otherwise the re-imported
   opi rootfs is mislabeled amd64 — the exact hazard the function's own
   comment warns about. Unlike the Jetson image this one *should* be
   slim (~5-6 GB target).
4. **Manager for the Pi — DECIDED: a `physical-ai-manager-opi` twin,
   not a multi-arch manifest.** `build-images.sh:253` currently skips
   the manager on arm64; the build itself is trivial (`node:22` + nginx,
   both multi-arch official images). A shared multi-arch manifest is off
   the table because the Pi image's CONTENT differs, not just its arch:
   nginx.conf is baked at `/etc/nginx/conf.d/default.conf`
   (`Dockerfile:48`; the manager service declares no volumes), and the
   Pi needs a `/api/system` reverse-proxy location (§5). Maintaining
   that as a compose bind-mounted full-file override would create an
   out-of-image duplicate of the whole SPA config (a drift pair).
   Instead follow the EXISTING dual-nginx precedent (`nginx.conf` vs
   `nginx.web.conf.template` for Railway — note the Railway file is an
   envsubst TEMPLATE for `$PORT`; the opi twin mirrors the STATIC
   `nginx.conf` form, a fixed `:80` needs no template): add
   `nginx.opi.conf` + a thin `Dockerfile.opi` in
   `physical_ai_manager/` — student/web/opi becomes a triple of the
   established pattern. Thread the new repo through retag and the
   smoke-test. **Two load-bearing details the twin must carry**:
   (a) stamp a real `REACT_APP_BUILD_ID` — `useVersionCheck`
   self-disables when the baked buildId is `dev`
   (`useVersionCheck.js:28,49`) and only `build-images.sh:278-301`
   injects a real one today; without the same `--build-arg` on the opi
   build, §5's "SPA self-heals via `/version.json` after an update"
   story silently never fires; (b) keep the `/version.json` healthcheck
   (the manager's container health keys on it,
   `docker-compose.yml:365`).
5. **CI (`docker-publish.yml`)**: third matrix entry (`platform: opi`,
   `runner: ubuntu-24.04-arm`) in both `build` (`:134-139`) and
   `smoke-test` (`:495-507`); extend `AMD64_REPOS`/`ARM64_REPOS`
   (`:274-275`) with the opi repos in `retag` AND the dual-push
   integrity `REPOS` list (`:366`) — two hardcoded repo lists, not one;
   add an **opi size gate** as a NEW step (the existing 11 GB gate —
   step at `:549`, literal at `:567` — is guarded
   `if: matrix.platform == 'amd64'` at `:550`, so the opi ceiling,
   ~7 GB, is its own gate rather than a threshold tweak); run
   `image_source_parity.sh` for the flavor (verified flavor-neutral —
   parameterized purely by `<kind> <image-ref>`, no edits needed).
6. **Write the missing `docs/arm64_base/README.md`** — referenced 3×
   by `build-images.sh` (`:44`, `:382`, `:511`) and 8× repo-wide (also
   both thin Dockerfiles at `:4`, `docs/JETSON_DEPLOY.md` ×2, and
   `docs/deploy/DEPLOY.md:235`) but absent from the tree; document both
   the Jetson and the opi base builds.

## 5. Workstream 2 — pi-agent + `docker-compose.opi.yml`

A native Python systemd service `robotis_ai_setup/pi_agent/` — the Jetson
agent's skeleton (systemd unit, scrubbed-env compose driver, arm64
digest-checked auto-pull with GHCR→Hub fallback) merged with the GUI's
platform-neutral brain. Ports/marks from the verified GUI inventory:

**Ported nearly verbatim** (from `robotis_ai_setup/gui/app/`):
`config_generator.py` (managed `.env` model — `MANAGED_KEYS` at `:24`,
prefixes `:55`, atomic writes, `HF_TOKEN` deliberately unmanaged with
`upsert_env_var` as sole writer, def `:245` / sole-writer docstring
`:249`; env file moves to **`/etc/edubotics/.env`** — the Jetson
precedent (`systemd/edubotics-jetson.service` runs as root with
`EnvironmentFile=/etc/edubotics/jetson.env`), NOT `~/.config`: for a
root systemd unit `~` is `/root`, and the agent needs the docker
socket + `/dev` anyway. Pin the identity model with it: the agent runs
as root like the Jetson agent; the `dialout`/`video` group checks in
the guided repairs are for interactive debug shells, not the agent.
The module itself
is platform-neutral — the Windows bits it must shed live in
`constants.py`: `%LOCALAPPDATA%` path defaults at `:158/:284/:340`, the
`sys.platform == "win32"` camera fallback at `:206`, and the
`sys.executable`-relative `versions.env` walk — the port swaps the
constants module, not the generator), `docker_manager`'s pull/update/digest logic
(**flip the digest pre-check from `linux/amd64` to arm64 — BOTH code
paths**: `docker_manager.py:336-380` is only the LEGACY single-digest
fallback probe; the set-membership machinery is
`_parse_digest_candidates`/`_get_remote_digest_candidates` at
`:388-455` with the membership tests at `:560`/`:702`.
`jetson_agent/agent.py:210-283` already carries the arm64 twins of
both — reuse those, don't re-port from the GUI),
`factory_reset` (volume-suffix rm of `ai_workspace`/`huggingface_cache`/
`edubotics_calib`), `ensure_environment_stopped` (**ported TARGETED,
not verbatim** — robot tier only, see the lifecycle model below; the
Dynamixel bus must still be free before every arm scan),
`roboter_studio_control.py`'s endpoint contract (the actual paths carry
a `/roboter-studio/` prefix: `GET /roboter-studio/status` `:190`,
`POST /roboter-studio/leader-disable` `:232` / `-enable` `:234` — which
maps 1:1 onto the proposed `/api/system/roboter-studio/…`; note the
`.env`-rollback-on-failed-restart logic is NOT in that module, it lives
in the injected callback `gui_app.py::_rs_set_leader_mode` `:2534-2589`
and must be ported alongside it), `phone_camera.py`
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
CORS/Origin dance): status (INCLUDING the Pi's LAN IP — reuse the
Jetson agent's LAN-IP detection, `agent.py:504`; the System window
displays it, §6), scan-arms, camera scan/roles/MJPEG-previews,
phone-camera toggle, HF token, start/stop environment, update
(image pull + agent self-update from a SHA-256-verified release tarball),
factory reset (double-confirm), Protokoll (SSE, with the existing secret
redaction), a **„Netzwerk-Check"** (tests FROM the Pi the things that
fail invisibly on school networks: cloud API reachable, GHCR/HF
reachable, presented TLS certificate chain genuine — a fingerprint
mismatch means the school's TLS-inspection middlebox is re-signing,
which breaks pulls/uploads/updater by design — and clock sane/NTP
synced; each reported as a green/red German line in the wizard, §6.
This converts the two most baffling field failures — "Updates schlagen
mit Zertifikatfehler fehl", "Anmeldung läuft ständig ab" — into
first-glance diagnoses), and the Roboter-Studio endpoints preserving the exact
`roboter_studio_control.py` JSON contract (`/roboter-studio/status`,
`/roboter-studio/leader-enable`, `/roboter-studio/leader-disable`,
busy/ready guards, `.env` rollback on failed restart).

**Proxy mechanics (decided).** The agent binds `127.0.0.1:8769` AND the
compose network's gateway IP — never the LAN NIC. `docker-compose.opi.yml`
pins `ros_net`'s IPAM subnet/gateway (default `172.28.0.0/24`, gateway
`172.28.0.1`) so `nginx.opi.conf` can `proxy_pass http://172.28.0.1:8769/`
deterministically, with no `extra_hosts: host-gateway` indirection.
**The subnet is a managed `.env` key** (`${EDUBOTICS_ROS_NET_SUBNET}`
+ derived gateway), not a hardcoded literal: `172.16.0.0/12` is common
institutional address space, and a school LAN overlapping the pinned
subnet blackholes container→LAN routing (cloud API unreachable from
the server container, with baffling symptoms). `setup.sh` (§7) checks
`ip route` for an overlap at provisioning and records a free range
into the `.env`. **nginx location details**: the `/api/system/`
location must set `proxy_buffering off` + a long `proxy_read_timeout`
— the Protokoll SSE stream and the MJPEG camera previews both ride
this proxy, and nginx's defaults (buffering on, 60 s read timeout)
stall the former and freeze the latter.
Boot-ordering caveat: the gateway interface only exists once compose has
created `ros_net` — the agent binds its gateway listener AFTER its
boot-time manager `up` (or retries the bind), not at process start.
The same listener dies whenever `ros_net` is DELETED, so two agent
rules follow: (a) the agent NEVER runs `compose down` — stop + `rm -f`
only, for every tier (the robot-tier carve-out below generalizes; the
network must survive every lifecycle operation, including factory
reset: stop robot tier → stop+rm manager → volume rm → recreate
manager, nothing `down`s); (b) interface-gone is a rebind trigger,
not a fatal error. Two
properties fall out: (1) the agent API is reachable from the browser
ONLY through the manager's same-origin `/api/system` proxy, so with
`EDUBOTICS_LAN_OPEN=0` the management surface shrinks with the rest of
the stack instead of leaking past it; (2) port 8769 keeps its documented
meaning — the agent's API supersedes and extends the Roboter-Studio
control server rather than adding a second port.

**Lifecycle model: two tiers — an always-on manager, a student-owned
robot tier.** The pure GUI-owner model cannot be ported verbatim: on
Windows the wizard lives in a NATIVE app that exists before any
container does, but on the Pi the wizard IS the React app served by the
manager container. With everything `restart: "no"` and "stack comes up
only on „Umgebung starten"", a freshly booted Pi serves nothing on :80
and the student can never reach the start button — a chicken-and-egg.
Resolution:

- **Manager tier (always-on)**: `physical_ai_manager` gets
  `restart: unless-stopped` in `docker-compose.opi.yml` and is
  additionally brought up by the pi-agent at boot (the
  `up -d --force-recreate --no-deps physical_ai_manager` pattern from
  `docker_manager.start_cloud_only`, `:1163-1190`, invocation `:1175`). This is a
  deliberate, documented exception to the `restart: "no"` invariant —
  same category as the Jetson's sanctioned `unless-stopped` — and the
  opi compose therefore DROPS the manager's `depends_on:
  physical_ai_server`: the manager must serve the wizard while the
  server is down. NOTE (rev. 3): serving is only half the story —
  `StartupGate` currently full-screen-BLOCKS the served SPA until
  rosbridge connects, so the §6 StartupGate carve-out is a REQUIRED
  companion to this lifecycle model, not an optional polish item.
  Graduate this exception into CLAUDE.md's
  lifecycle bullet when the feature lands (see §13).
- **Robot tier (student-owned)**: `open_manipulator` +
  `physical_ai_server` stay `restart: "no"` and come up only on
  „Umgebung starten" — the GUI-owner lesson survives where it matters:
  the Dynamixel serial bus (`docker-compose.yml:6`).
- **`ensure_environment_stopped` is ported TARGETED, not verbatim.**
  The GUI version is a full `compose down` (`docker_manager.py:1262-1296`)
  — on the Pi that would kill the manager serving the very page the
  student is clicking in, mid-wizard. The agent's version stops/removes
  ONLY the two robot-tier containers, using the `stop` + `rm -f`
  pattern from `stop_cloud_only` (`:1223-1243`, which deliberately
  avoids `down` so the network survives), preserving the
  graceful-SIGTERM path so the entrypoint's torque-disable trap still
  runs. The same carve-out applies to „Stoppen" and the pre-arm-scan
  teardown.
- **„Umgebung starten"** = `up -d --force-recreate --no-deps
  open_manipulator physical_ai_server` — both services named explicitly
  so the health-gated `depends_on` between THEM still applies, while
  `--no-deps` keeps compose's dependency resolution away from the
  running manager.
- **Updates recreate the manager LAST**, after the robot tier is down
  and images are pulled. Recreating the manager drops the student's SPA
  for a few seconds — expected and self-healing: `useVersionCheck`
  polls `/version.json` and reloads on the new buildId (which is why
  §4's `REACT_APP_BUILD_ID` stamping is load-bearing). Document the
  blip, don't fight it. **In-flight semantics**: the update POST and
  its SSE progress themselves ride THROUGH the manager being
  recreated (and the agent's own self-update restart 502s the proxy
  for a moment) — so the update endpoint ACKs early with a job id and
  runs asynchronously, and the System window re-attaches by polling
  job status; a long-lived in-flight response would be severed
  mid-update and read as a failure.

**`docker-compose.opi.yml`** — a **three-way merge, NOT a Jetson
derivative**. The Jetson file is an inference-only, follower-only
stack; taken literally, "Jetson minus NVIDIA" silently drops leader
support, calibration persistence and the whole recording/Roboter-Studio
env surface. Explicit merge checklist:

- **From the Jetson compose**: the udev-symlink device style
  (`/dev/edubotics-*`), the `CMD`/bash healthcheck shape — it is
  already the pure-`usb_cam` model, whereas the student file's
  healthcheck inline-branches on the WSL2 `uname`, which a native-Linux
  Pi never wants. DROP `runtime: nvidia`
  (`docker-compose.jetson.yml:24`, `:83`) — and note NVIDIA wiring
  lives in TWO places: those inline lines AND the separate
  `docker-compose.gpu.yml` overlay, which must simply never be layered
  on the Pi.
- **From the student compose**: the ~50-entry `EDUBOTICS_*`
  `environment:` forwarding lists (the Jetson server forwards ONLY
  `ROS_DOMAIN_ID` + `HF_TOKEN` — recording/calibration/collision/
  workflow vars all fail-open without the student lists); the volume
  set `ai_workspace`/`huggingface_cache`/**`edubotics_calib`** (the
  Jetson file has `jetson_*` names and NO calib volume — miss it and
  every container recreate wipes Roboter-Studio calibration, and the
  factory-reset volume-suffix rm above mismatches); the `.env`-driven
  reference style (`${ROS_DOMAIN_ID:-30}`, `HF_TOKEN=${HF_TOKEN:-}` —
  the Jetson file's bare `- ROS_DOMAIN_ID` host-passthrough and its
  `HF_TOKEN=${EDUBOTICS_HF_TOKEN}` form would silently detach the
  compose from the agent-managed `.env`; the ported `config_generator`
  writes the key `HF_TOKEN`); **leader support**:
  `LEADER_PORT=${LEADER_PORT}` re-added and `EDUBOTICS_FOLLOWER_ONLY`
  `.env`-driven, never hardcoded — the Jetson file pins `=1` and has
  no leader device at all, and the Roboter-Studio leader toggle
  DEPENDS on regenerating exactly that key; tzdata ro-mounts,
  `.s6-keep` mount, `pids_limit`, `SYS_NICE`/rtprio.
- **New in this file**: the manager as a third service (per the
  lifecycle model above), the `${EDUBOTICS_BIND_HOST:-…}` port
  binding, the pinned-but-configurable `ros_net` IPAM block (proxy
  mechanics above), and `EDUBOTICS_CAMERA_SOURCE=usb_cam` set
  EXPLICITLY (don't lean on the entrypoint's non-WSL auto-detect
  alone) plus the camera tuning vars
  (`EDUBOTICS_CAMERA_PIXEL_FORMAT/_WIDTH/_HEIGHT/_FRAMERATE` — absent
  from the Jetson file).

Remaining parameters:

- Images `${REGISTRY}/…-opi:${IMAGE_TAG}`; same `${REGISTRY}`/fallback
  interpolation and `.env` interface as today.
- **mem_limit 1.5g / 5g / 512m** (today's student file is 2g/6g/512m
  = 8.5 GB and the Jetson file 3g/6g = 9 GB — both over-commit an 8 GB
  board). **memlock cut from 8428281856 (~7.85 GB, present in both
  existing composes) to 2 GB**; keep `SYS_NICE` + rtprio for the 100 Hz
  loop. zram swap is mandatory at provisioning.
- Every new env var must join the `environment:` forwarding lists
  (`env-forwarding-guard` — and the opi compose joins the guard's
  hardcoded compose-file list, §13).
- **Ports**: `80`, `8080`, `9090` bound to the LAN per the locked
  decision, behind one managed env switch (see §8). Note today's compose
  hardcodes literal `127.0.0.1:` on every `ports:` line
  (`:42/:166-167/:352`) — no bind-host variable exists yet; the opi
  compose introduces it (`${EDUBOTICS_BIND_HOST:-…}:80:80` style, with
  the agent's `.env` regenerate mapping `EDUBOTICS_LAN_OPEN` onto it).
  `5557` does not exist (no capture bridge); the browser reaches the
  agent only via the same-origin `/api/system` proxy — the agent's
  `:8769` binds loopback + the pinned docker gateway, never the LAN NIC
  (see proxy mechanics above). Also pin `ros_net`'s IPAM
  subnet/gateway here (the proxy depends on it).

**React fix required**: `LeaderToggle.jsx:47` and `RunControls.jsx:47`
hardcode `RS_CONTROL_BASE = 'http://localhost:8769'` — in Pi mode these
route to the host-relative `/api/system/roboter-studio/...` path instead
(from a remote browser, `localhost:8769` resolves to the student's own PC
and the toggle silently self-hides today).

## 6. Workstream 3 — the React „System"-Fenster (the GUI, in the browser)

A new window/tab, visible only in Pi mode. **Gating mechanism DECIDED
(hardened in rev. 3): a baked static marker + a live agent probe.**
The "nginx appends a URL param, mirroring `?cloud=1`" idea is dropped
as unsound: today the NATIVE GUI builds the `?cloud=1&_v=…` query
string client-side before navigation (`gui_app.py:2437-2440`) — a
server cannot "append a param" to an SPA load without a 302 redirect
(loop-guard, address-bar churn) or HTML injection; there is nothing to
mirror. Rev. 2's "one-shot probe of `/api/system/status`" had two
traps: (1) on a NON-Pi rig the probe does NOT cleanly fail —
`nginx.conf:32-34`'s SPA catch-all (`try_files $uri /index.html`)
answers `GET /api/system/status` with **200 + index.html**, so a bare
`res.ok` check would flip Pi mode on every Windows rig; any probe must
require valid JSON (the exact defensive pattern
`useVersionCheck.js:16-23` already uses against the same
fallthrough). (2) The agent binds its gateway listener only AFTER its
boot-time manager `up` (§5) — a one-shot probe racing that window
502s, and the System tab would never appear until a manual reload.
Resolution: `nginx.opi.conf` serves a **static `/pi-mode.json` baked
into the opi manager image** — the image IS the Pi flavor, so
"is this a Pi" is deterministic and independent of agent health — and
that flag gates the System tab (`piOnly: true`); the live
`/api/system/status` probe (retried with backoff, never one-shot)
feeds agent-health/readiness INSIDE the System window. Tabs are
already gated on async Redux state (`jetsonConnected`,
`StudentApp.js:275-276`) — the same `.filter()` chain hides the tab
until the marker resolves (progressive reveal, exactly like the
Jetson-gated tabs).

**StartupGate carve-out (REQUIRED — rev. 2's lifecycle fix is
incomplete without it).** `StartupGate.js:80-207` renders a
full-screen blocking overlay (`fixed inset-0 z-50`) until rosbridge
connects; the only bypass today is `isCloudOnlyMode()`. On a freshly
booted Pi — manager up, robot tier down BY DESIGN (§5) — the student
gets a spinner, then the 90 s "check Docker" screen, and can never
reach the System tab to press „Umgebung starten": the §5
chicken-and-egg survives one layer up. In Pi mode StartupGate must not
block globally — let the System tab render and gate only the
robot-dependent tabs (or lift the overlay whenever Pi mode is set and
the robot tier is intentionally down). Ordering constraint: the
Pi-mode flag must resolve BEFORE StartupGate decides to block — the
static `/pi-mode.json` marker above makes that cheap and race-free.

Feature-for-feature parity with `EduBotics.exe`:

| GUI today | System window |
|---|---|
| Modus (cloud-only checkbox) | kept — on the Pi the manager is ALWAYS up (§5 lifecycle), so cloud-only reduces to "skip the robot tier + the hardware gate" |
| Schritt A/B „Arme scannen" + guided repair | scan via scanner container + `identify_arm.py`; repairs = udev/group checks; leader/follower ports persisted as managed keys; fast-rehydrate on revisit |
| Schritt C Kameras: Scan, Rollen (Greifer/Szene), Vorschau | v4l2 by-id/by-path enumeration incl. identical-serial dedup; MJPEG `<img>` previews from the agent; previews stop before the stack claims devices |
| Handy als 3. Kamera (:8444) | **⚠ OFFEN — owner decision pending, do NOT implement in P3 until decided.** The receiver itself ports cleanly (`0.0.0.0:8444` HTTPS, openssl cert), but on the Pi's `usb_cam` path there is NO consumer: `entrypoint_omx.sh` starts `camera_ingest_node.py` only in the `native_bridge` branch (`:528/:535`), so phone frames reach no ROS topic. The ingest node is a TCP SERVER the Windows GUI dials into at `127.0.0.1:5557` — a working Pi port would need an agent-side sender AND the ingest node running beside `usb_cam`. Note the phone is preview-only even on Windows (recording/inference out of scope per `phone_camera.py`'s own docstring). Options when decided: drop from Pi v1, or run the ingest node alongside `usb_cam` with an agent-side sender. |
| Schritt D HF-Token | same upsert, same „✓ Token gespeichert" semantics |
| „Umgebung starten"/„Stoppen" + start-gate | same gating (prerequisites ∧ both arms identified ∨ cloud-only) |
| Update-Gate | cloud `/version` check (needs the new `pi_agent_*` fields, §7); image pulls via digest pre-check + agent tarball self-update replace the `.exe` download; manager recreated last — brief SPA reload, `useVersionCheck` self-heals (requires the §4 buildId stamp); the update POST/SSE ride through the manager being recreated — agent ACKs early with a job id, UI re-attaches by polling (§5) |
| „Web-Oberfläche öffnen" | not needed — the user is already in the browser |
| „Daten zurücksetzen" | identical volume wipe, double-confirm |
| Protokoll | SSE log panel, secret redaction preserved |
| — (new, no GUI equivalent) | **Pi-IP-Anzeige**: the System window shows the Pi's current LAN IP prominently (from the status endpoint, §5) — the teacher reads it off the screen during first setup/kiosk and writes it onto the case label's IP field (§7); `http://<ip>/` is the universal fallback on GPO-hardened school PCs |
| — (new, no GUI equivalent) | **„Netzwerk-Check"**: one button, runs the §5 agent checks, renders green/red German lines (Cloud erreichbar / Registry erreichbar / Zertifikat echt / Uhrzeit synchron) with a one-line German hint per failure (e.g. TLS-Inspektion → „IT: Ausnahme für das Robotik-VLAN nötig, siehe Netzwerk-Anleitung") |

**Network-aware disconnect wording (Pi mode).** The frontend can
detect one failure signature precisely: robot tier REPORTED RUNNING by
the agent, but rosbridge (`:9090`) unreachable from the browser —
that is the port-filtering/VLAN-ACL case, not a crashed stack. In Pi
mode the StartupGate timeout screen and the heartbeat „Getrennt" state
must say so („Verbindung zu Port 9090 blockiert? Netzwerk-Anleitung
prüfen") instead of the Windows-era „Docker prüfen" text, which is
actively misleading on a Pi.

All student-facing strings in German with literal umlauts (Rule §1;
`german-strings-lint` covers `[FEHLER]`/`[WARNUNG]`/`[STOPP]` lines).

## 7. Workstream 4 — provisioning & fleet

- **Phase 1: `pi_agent/setup.sh`**, mirroring `jetson_agent/setup.sh`
  minus its NVIDIA hard-checks (`setup.sh:40-46`): install pinned Docker,
  udev rules (ROBOTIS VID `2F5D` symlinks — reuse
  `jetson_agent/udev/99-edubotics-robotis.rules` **extended with a
  leader symlink**: the Jetson rules map only `edubotics-follower`,
  the Pi has both arms), avahi + hostname assignment (`edubotics-NN`),
  zram, agent + systemd unit (root, `EnvironmentFile=/etc/edubotics/`
  per §5 — the Jetson precedent), the `ip route` overlap check for the
  `ros_net` subnet (§5, records a free range into the `.env`), image
  pull, print the label/QR for the case. **The label carries three
  fields**: the derived hostname (`edubotics-NN.local`), the wired
  NIC's **MAC address**, and a **blank IP field**. The MAC is the
  keystone of the IT handoff: with hostname + MAC on the case, the
  school admin can do DHCP reservations and NAC/802.1X registration
  (§8) without ever touching a Pi; the teacher fills in the reserved
  IP once (read off the System window's Pi-IP-Anzeige, §6), and
  `http://<ip>/` stays true forever.
- **Phase 2: golden eMMC/NVMe image** („flash → boot → ready"):
  bench-provision one unit, capture, per-unit first boot regenerates
  machine-id (which re-derives `ROS_DOMAIN_ID`), hostname, and secrets
  via a one-shot systemd unit. The `NN` in `edubotics-NN` is DERIVED
  (from the SoC serial / fresh machine-id), never hand-assigned —
  duplicate `.local` hostnames are the one failure mDNS cannot survive,
  so uniqueness must be generated, and the derived name is what gets
  printed on the label/QR. This is the `.exe`-installer equivalent for
  fleet rollout.
- **Updates**: images via the digest-checked auto-pull; agent via
  SHA-256-verified release tarball (reusing `update_checker`'s download
  gates — HEAD-precheck, Content-Length truncation reject, SHA-256
  verify); OS via unattended-upgrades. **This needs cloud-side work the
  `.exe` flow does not cover**: `/version` returns exactly
  `{version, download_url, installer_sha256, commit}`, ALL hard-wired
  to `EduBotics_Setup.exe` (`routes/version.py:37, :80-83`) — reusing
  them would point the Pi at the Windows installer. Add OPTIONAL,
  additive fields `pi_agent_download_url` + `pi_agent_sha256` (old GUIs
  ignore them), derived the same way as the `.exe` pair (release repo +
  version + fixed asset name `edubotics-pi-agent.tar.gz`). `release.yml`
  gains the opi images in W4, the agent tarball as a W5-adjacent
  release asset, and a W6 extension that hashes the exact attached
  tarball into a new `PI_AGENT_SHA256` Railway var BEFORE the final
  `GUI_VERSION` flip — preserving W6's attach-asset-first /
  advertise-last race-safety and its empty-on-failure (never stale)
  semantics.
- **The German IT one-pager is a NAMED P4 deliverable** (not a
  footnote of the teacher docs): the exact §8 work order — three-tier
  model, port table, egress FQDN allowlist, TLS-inspection + proxy
  exemptions, NAC/MAC registration — formatted so a school admin can
  execute it as a ticket without a phone call. §8's "docs require a
  VLAN" is a requirement; this page is the INSTRUCTIONS, and the
  difference is what makes the ticket succeed on the first pass.

## 8. Security posture (deliberate, decided)

With open LAN binding and no auth, **anyone on the same network segment
can drive any arm, watch every camera, and call the management API
(including Stoppen and Daten zurücksetzen) on any rig**. This was an
explicit product decision (2026-07-11). Three mitigations ship anyway
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
   discovery story. Wired ethernet for the Pis is recommended. Because
   the app is fully host-relative (§3), **`http://<ip-address>/` works
   as the discovery fallback** wherever mDNS is GPO-disabled
   (`EnableMDNS=0` is common Windows hardening on managed school PCs)
   — the teacher docs print the IP-literal path next to the `.local`
   name.
3. **Host/Origin allowlist on MUTATING `/api/system` endpoints**
   (does NOT touch the open-LAN decision): with no auth, any web page
   a student's browser visits — anywhere on the internet — can fire a
   drive-by `POST http://edubotics-NN.local/api/system/factory-reset`
   as a no-cors cross-site request from inside the LAN. Rejecting
   state-changing requests whose `Host`/`Origin` is not the Pi itself
   stops hostile WEB PAGES while leaving LAN peers (the accepted risk)
   untouched — the same exact-host check
   `roboter_studio_control.py:209-224` already implements; port it,
   adapted to the mDNS hostname + IP literals (never a `startswith`
   match, per that module's own comment).

### Network setup — three tiers + the exact IT work order

School networks break this deployment in predictable ways (GPO-disabled
mDNS, Wi-Fi client isolation, inter-VLAN ACLs filtering 8080/9090,
proxy/PAC interception, NAC/802.1X, TLS inspection, `.local` AD-domain
collisions). The German IT one-pager (§7, P4 deliverable) ships THREE
tiers; a pilot must never stall on a slow IT ticket:

**Tier 1 — dedicated robotics VLAN (the recommended permanent
install).** It is the only variant that fixes every failure class AND
it is the security boundary the locked no-auth decision already
requires — the permission fixes come along for free. The literal work
order for the school admin:

1. New VLAN (e.g. `ROBOTIK`): wired ports for every Pi + the Jetson;
   a matching SSID mapped into the same VLAN for student PCs (or the
   ACL of step 3 from the existing student VLAN). **Client/AP
   isolation OFF** inside it.
2. **DHCP reservations** for every Pi and the Jetson, keyed to the MAC
   printed on each case label (§7). The reserved IPs go onto the
   labels' IP fields.
3. Inbound (student PCs → robotics VLAN), only if PCs stay on another
   VLAN — nothing else inbound:

   | Allow | Port | Purpose |
   |---|---|---|
   | TCP | 80 | Web app (wizard + UI) |
   | TCP | 8080 | Camera streams (web_video_server) |
   | TCP | 9090 | Robot control (rosbridge) |
   | TCP | 9091 | Jetson inference proxy |

4. Outbound (robotics VLAN → internet), TCP 443 to: `ghcr.io` +
   `*.githubusercontent.com` + `github.com` (images, agent tarball);
   `registry-1.docker.io`, `auth.docker.io`,
   `production.cloudflare.docker.com` (registry fallback);
   `huggingface.co`, `*.hf.co` (dataset CDN); the Railway cloud-API
   host; the Supabase project host; OS mirrors (`ports.ubuntu.com` /
   `armbian.com`) for unattended-upgrades. Plus **NTP** (UDP 123).
5. **Two exemptions**: NO TLS inspection for this VLAN (re-signed
   certificates break `docker pull`, HF uploads and the SHA-256
   updater BY DESIGN — the §5 Netzwerk-Check detects and names this);
   proxy/PAC bypass on student PCs for the robotics subnet + `*.local`.
6. If NAC/802.1X is in use: register the Pis'/Jetson's MACs (from the
   labels) as headless devices.
7. Optional: mDNS reflector between student VLAN and robotics VLAN
   (a controller checkbox on UniFi/Aruba/Cisco) so `.local` resolves
   cross-VLAN — skippable, the IP labels cover discovery.

**Tier 2 — own robotics router (the recommended PILOT setup, and the
fallback for uncooperative IT).** A dedicated teacher-owned router/AP:
Pis wired into its LAN ports, student PCs on its SSID, WAN port into
any school wall jack. On the owned network there is no GPO, no
isolation, no ACL, no proxy — everything works out of the box, and
from IT's perspective the entire classroom is ONE outbound-HTTPS
client (one MAC to register, one TLS-inspection exemption; a 4G/5G
hotspot on the WAN port removes even that). Caveat: managed student
PCs may still have `EnableMDNS=0` locally — the IP labels remain the
universal fallback (trivial DHCP reservations on the owned router).
P4's pilot SHOULD run on this tier so it is independent of the
school's ticket queue, then migrate to Tier 1.

**Tier 3 — no network permissions at all (documented emergency
modes).** (a) Direct ethernet cable PC↔Pi: link-local addressing +
avahi work on a direct link with zero configuration; recording,
teleop and Roboter Studio all work, cloud steps wait for an uplink.
(b) Kiosk: `EDUBOTICS_LAN_OPEN=0` + monitor/keyboard on the Pi —
exposes nothing to any network, needs only outbound HTTPS for the
cloud parts (§11's implicit kiosk mode).

What we deliberately do NOT do: fight mDNS on managed student PCs.
The IP-literal path is first-class (host-relative app, §3; label IP
field, §7; Pi-IP-Anzeige, §6) and works on every network that routes.

The Jetson JWT proxy (`rosbridge_proxy.py`, `0.0.0.0:9091`, alg-pinned /
issuer-pinned / owner-matched) remains a proven drop-in if this posture
is ever revisited; nothing in this plan forecloses it.

## 9. Memory & USB budget

- **RAM**: 1.5 + 5 + 0.5 GB caps = 7 GB, ~1 GB for the OS; zram absorbs
  transient spikes. No local policy loads (Jetson-only inference) keeps
  the server container's peak well under its cap during recording
  (streaming h264 encode, ~2-3 of 8 cores for 2×640×480@30).
- **Video decode is PyAV, not torchcodec** (LeRobot 0.5.1 excludes
  torchcodec on Linux aarch64 by environment marker, §4): recording
  (encode) is unaffected; dataset EDITS (`delete_episodes` re-encode)
  run noticeably slower on the Pi — the existing nice'd
  `edit_worker.py` subprocess design absorbs it (the dashboard stays
  responsive), edits just take longer.
- **USB**: one camera on the USB3 port, one on the standalone USB2 port,
  arms on the hub ports. Native default stays **YUYV** —
  `entrypoint_omx.sh`'s own comments (`:79-91`) note `mjpeg2rgb` burns
  ~60 % of an ARM core, so it is the *fallback* (existing
  `EDUBOTICS_CAMERA_PIXEL_FORMAT` env knob), not the default, if a rig's
  port layout forces both cameras onto one USB2 bus (2×YUYV@640×480@30
  ≈ 35 MB/s would saturate it).
- **h264 encode is CPU x264 — by constraint, not choice.** Mainline
  ffmpeg has NO rkmpp H.264 *encoder* (only RK3588 *decode* was
  mainlined, 2026-02); hardware encode exists solely via the
  out-of-tree `nyanmisaka/ffmpeg-rockchip` fork, which needs the BSP
  kernel plus `/dev/mpp_service`/`/dev/rga` passthrough into the
  container. That fork is the documented escape hatch if pilot gate
  §12.2 (thermals under sustained 2-stream encode) fails — not part
  of this round.

## 10. Phases & acceptance criteria

| Phase | Scope | Done when |
|---|---|---|
| **P1 — Images** | `Dockerfile.arm64cpu` (explicit `torch==2.7.0` pin), `PLATFORM=opi` + platform-parameterized flatten, `physical-ai-manager-opi` twin (`nginx.opi.conf` + `Dockerfile.opi`, static `/pi-mode.json`, `REACT_APP_BUILD_ID` stamped), CI matrix/retag+REPOS/parity/new size-gate, `docs/arm64_base/README.md` | CI publishes `*-opi:latest` to GHCR+Hub; smoke-test passes arch/size/parity gates; images boot on a bench Pi |
| **P2 — pi-agent + compose** | Agent port, management API (ACK-early update job, Host/Origin allowlist on mutating endpoints, LAN IP in status, „Netzwerk-Check" endpoint), two-tier lifecycle (always-on manager, targeted stop, no-`down` rule), `docker-compose.opi.yml` (three-way merge checklist, configurable IPAM gateway, bind-host var), nginx `/api/system` proxy (`proxy_buffering off` for SSE/MJPEG), `EDUBOTICS_LAN_OPEN` | Full wizard→start→record cycle driven purely via `curl` against the API on a bench Pi — incl. reboot → manager auto-serves the wizard with the robot tier down |
| **P3 — System window** | Pi-mode gating (static marker + JSON-guarded probe), **StartupGate carve-out**, wizard UI, Start/Stopp/Update/Reset/Protokoll, camera previews, Pi-IP-Anzeige + „Netzwerk-Check" panel, network-aware disconnect wording, host-relative LeaderToggle/RunControls | A student can go from freshly flashed Pi to a recorded + uploaded dataset using only a browser — incl. fresh boot: SPA loads, System tab visible, no StartupGate deadlock with the robot tier down |
| **P4 — Provisioning + pilot** | `setup.sh` → golden image, labels/QR (hostname + MAC + IP field), teacher docs + the German **IT one-pager** (§8 three tiers + work order), rig pilot (run on Tier-2 own-router per §8, then migrate to Tier 1) | Pilot checklist in §12 fully green on ≥2 units |

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
  parked item — `camera_topic_list` rework). Distinct from this
  non-goal, the phone-PREVIEW-on-Pi question (§6 table) is **OPEN,
  not decided** — neither committed nor ruled out this round.
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
   student PCs on the school's actual network — GPO-disabled mDNS
   (`EnableMDNS=0`), client isolation and cross-VLAN are the known
   failure modes (the §8 tier model + printed IP-literal fallback are
   the mitigations; verify BOTH the `.local` and the `http://<ip>/`
   path on a managed school PC).
5. **8 GB headroom**: record a long episode while the manager serves a
   second browser; no OOM kills (watch `pids_limit`/`mem_limit` events).
6. **Jetson interop**: Pi-served frontend → classroom Jetson `:9091`
   connect/claim/inference end-to-end (plain-HTTP origin keeps `ws://`
   legal — this is why the Pi serves the app over HTTP, and why serving
   the student SPA from an HTTPS cloud origin is off the table).
7. **CI cost**: arm64 runner minutes for a third flavor; acceptable on
   the current plan, monitor after the first releases.
8. **Power budget**: the 5 Pro is 5 V/5 A USB-C; two UVC cameras + two
   OpenRB serial boards on a marginal PSU is a classic brown-out source
   (undervoltage resets mid-recording). Pilot with the official PSU and
   watch the kernel undervolt/reset logs; arm servo power stays on the
   arms' own 12 V supplies as today.
9. **Egress on the school's real uplink**: „Netzwerk-Check" (§5/§6)
   fully green from inside the school network — cloud API, registries,
   HF CDN reachable; certificate fingerprints genuine (TLS inspection
   is the silent killer of pulls/uploads/updates); NTP synced. Run it
   on BOTH the Tier-2 pilot router uplink and the final Tier-1 VLAN.

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
- **Lifecycle invariant amendment (sanctioned)**: the always-on manager
  (`restart: unless-stopped` on `physical_ai_manager` in the **opi
  compose only**, §5) is a deliberate exception in the same category as
  the Jetson's — the manager IS the GUI on the Pi. Document it in
  CLAUDE.md's lifecycle bullet in the landing PR; the robot tier stays
  `restart: "no"` everywhere, and the student compose is untouched.
- **CI guards to extend** (all four are HARDCODED path lists, verified
  — none picks up new dirs/files automatically): `env-forwarding-guard`
  (add `docker-compose.opi.yml` to its compose-file union; new
  `EDUBOTICS_*` vars join the `environment:` lists),
  `compose-validate` (validate `docker-compose.opi.yml` + its
  `.s6-keep` mount), `german-strings-lint` (add `robotis_ai_setup/pi_agent/`
  to its 3-directory grep list), `shell-lint` (add `pi_agent/setup.sh`
  to its 6-script list).
