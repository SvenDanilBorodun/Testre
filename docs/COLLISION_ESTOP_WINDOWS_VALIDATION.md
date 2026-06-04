# Teleop Collision E‑Stop — Windows Hardware Validation Runbook

**Read this first — you are (probably) a fresh Claude Code session on a Windows 11 PC.**
This document is fully self‑contained: it assumes you have **no memory** of how this feature was
built. You have the **Testre** repo cloned (VS Code) and the **EduBotics** WSL2 distro + the
EduBotics GUI installed, with both OMX arms (leader + follower) physically connected. Your job is
to validate the teleop collision e‑stop's **two‑step recovery** end‑to‑end on this real rig, then
**report back** so the feature can ship through the normal CI pipeline.

The live dry‑run **cannot be done on a Mac** (Docker Desktop for macOS can't pass the USB serial
bus into a container, and the stack is Windows‑WSL2 / Jetson only) — that's why this runs here.

**History (why several things look the way they do):** the first shipped version (commit
`96f5ee0`, in `:latest`) (a) declared `Present Current` on ALL follower arm joints — but dxl11‑13
are XL430‑W250, which have NO such register, so the follower's hardware init aborted and the arm
was **bricked** on every standard rig; (b) was missing `ros-jazzy-control-msgs` in the server
image, so detection silently fail‑opened; (c) subscribed with the wrong message type
(`DynamicJointState` vs Jazzy's `DynamicInterfaceGroupValues`) — silently zero data; (d) decoded
the signed 16‑bit force registers as unsigned (−5 → 65531 → instant false trip); and (e)
auto‑glided the follower home at trip time, which raced the frozen broadcaster's in‑flight
messages (arm kept pressing) and would drag the arm through a still‑present obstacle. All five
are fixed in the current working tree; the recovery is now **student‑paced and two‑step**.

---

## 1. What the feature does (and the invariants you must NOT break)

During teleoperation the student moves the back‑drivable **leader** arm by hand; the **follower**
mirrors it over the ROS topic `/leader/joint_trajectory`. The follower arm joints run position
control with **no current limit**, so forcing the arm into an object winds the motors toward
stall and can damage them. This feature detects that and stops safely.

**Flow (two‑step, student‑paced):**
1. **Detect** high motor force (per‑joint normalized effort fraction, velocity‑gated, debounced)
   → publish `/collision_flag=true` (the upstream `om_joint_trajectory_command_broadcaster`
   skips publishing while the flag is set → leader stream frozen).
2. **Relax in place**: the server commands the follower's *current measured pose* (delayed
   ~0.15 s so the broadcaster's in‑flight messages drain, then re‑sent 3×). Position error → 0,
   the motors stop pressing, the arm stays put. **It does NOT auto‑home.**
3. If recording: the in‑progress episode is **discarded** (`re_record()`) and capture halts.
   Prior saved episodes are kept.
4. React shows the blocking modal, **Schritt 1**: „STOPP — Kollision erkannt … Entferne zuerst
   das Hindernis und klicke dann auf ‚Follower in Grundstellung fahren'."
   (`/task/status` phase=COLLISION=7).
5. Student removes the obstacle, clicks **„Follower in Grundstellung fahren"** →
   `HOME_FOLLOWER` (=9) → verified quintic safe‑home glide (progress‑checked every 1 s,
   stalled glide re‑sent up to 3×, then reported failed back to Schritt 1 with a retry hint).
   phase=COLLISION_HOMING=8 while gliding; on verified arrival phase=COLLISION_HOMED=9 →
   modal **Schritt 2**.
6. Student brings the leader near the home pose, clicks **„Teleoperation fortsetzen"** →
   `RESUME_TELEOP` (=8) → STRICTLY refused until homed; leader proximity‑checked (0.30 rad);
   then quintic resync follower→leader, and the flag clears only after the resync completes.

**Load‑bearing invariants — do not "fix" these away:**
1. **Flag before motion.** `/collision_flag=true` is published *before* any trajectory, else the
   100 Hz leader broadcaster overwrites it.
2. **Relax, never press — and never auto‑home.** On collision the follower must stop pressing
   (hold its measured pose) but must NOT move home on its own: the student must be able to
   remove the obstacle before the arm moves again, and a frozen position‑mode joint holding its
   last (in‑obstacle) goal keeps pushing at full PWM. It must also NOT torque‑disable mid‑air
   (the arm would slump).
3. **Strict two‑step order.** `RESUME_TELEOP` is refused until the follower verifiably reached
   home (`HOME_ARRIVED_TOL_RAD`, 0.10 rad). One mental model for students.
4. **Teleop‑only.** The guard is gated OFF during inference (`mode_is_inference`), and the
   leader broadcaster (the only `/collision_flag` consumer) isn't in the inference path. It must
   never reshape the recorded/replayed action distribution (Rule §2).
5. **Discard, don't record.** On a trip mid‑recording the in‑progress episode is discarded via
   `re_record()`; nothing after the trip is recorded. Prior saved episodes are kept.
6. **Model‑aware force signals.** dxl11‑13 (XL430‑W250) expose `Present Load` (NO
   `Present Current` register — declaring it bricks hardware init); dxl14‑15 (XL330‑M288) expose
   `Present Current`. Both are normalized to a 0..1 effort fraction. The gripper (dxl16) is
   excluded entirely (already current‑limited at 350 mA in Op Mode 5).
7. **Signed registers arrive unsigned** on the gpio topic: values ≥ 32768 must be mapped back
   to int16 before taking the magnitude (`effort_fraction_from_values()`).

## 2. Files that implement it (orientation)

- **Expose force (open_manipulator):**
  `robotis_ai_setup/docker/open_manipulator/overlays/omx_f.ros2_control.xacro`
  (`Present Load` on dxl11‑13, `Present Current` on dxl14‑15, `Hardware Error Status` on all 5),
  `…/overlays/omx_f_hardware_controller_manager.yaml` (read‑only `gpio_command_controller`),
  `…/overlays/omx_f_follower_ai.launch.py` (spawns it).
- **Detection + orchestration (physical_ai_server):**
  `physical_ai_tools/physical_ai_server/physical_ai_server/safety/collision_detector.py`
  (pure logic + `effort_fraction_from_values`), `…/safety/collision_monitor.py` (ROS wiring,
  relax/home/resume state machine), `…/physical_ai_server.py` (`HOME_FOLLOWER` +
  `RESUME_TELEOP` service branches), `…/package.xml` (declares `control_msgs`).
- **Interfaces:** `physical_ai_tools/physical_ai_interfaces/srv/SendCommand.srv`
  (`RESUME_TELEOP=8`, `HOME_FOLLOWER=9`), `…/msg/TaskStatus.msg` (`COLLISION=7`,
  `COLLISION_HOMING=8`, `COLLISION_HOMED=9`). The server uses getattr fallbacks so a container
  with pre‑rebuild compiled interfaces still speaks the right wire values.
- **React (student):** `…/physical_ai_manager/src/components/CollisionModal.js` (two‑step),
  `…/hooks/useRosTopicSubscription.js`, `…/hooks/useRosServiceCaller.js` (`homeFollower` +
  `resumeTeleop`), `…/features/tasks/taskSlice.js` (`collision.stage`),
  `…/constants/taskPhases.js`, `…/constants/taskCommand.js`, `…/StudentApp.js`.
- **Calibration helper:** `robotis_ai_setup/docker/open_manipulator/calibrate_collision_currents.py`
  (emits `EDUBOTICS_COLLISION_EFFORT_J*` fractions).
- **Config:** `robotis_ai_setup/docker/docker-compose.yml` (the `EDUBOTICS_COLLISION_*` env
  block on the `physical_ai_server` service).
- **Image gate:** `robotis_ai_setup/docker/physical_ai_server/Dockerfile` installs
  `ros-jazzy-control-msgs` + asserts `DynamicInterfaceGroupValues` imports at build time.
- **Tests:** `robotis_ai_setup/tests/test_collision_detector.py`,
  `…/test_collision_monitor_contract.py`, `…/test_collision_discard_contract.py`.

## 3. Tunables (defaults = calibrated on the reference rig 2026‑06‑03)

| Env var | Default | Meaning |
|---|---|---|
| `EDUBOTICS_COLLISION_ENABLED` | `1` | Master switch / one‑variable rollback (`0` = fully off) |
| `EDUBOTICS_COLLISION_EFFORT_J1..J5` | `0.30,0.65,0.40,0.30,0.30` | Per‑joint trip threshold as **effort fraction 0..1** (J1‑3 = \|Present Load\|/1000, J4‑5 = \|Present Current\|/1750) |
| `EDUBOTICS_COLLISION_VELOCITY_GATE` | `0.05` | rad/s; below this the joint counts as "not moving" |
| `EDUBOTICS_COLLISION_DEBOUNCE_MS` | `150` | Sustained‑over time before tripping |
| `EDUBOTICS_COLLISION_RESUME_TOL_RAD` | `0.30` | Max leader↔home gap allowed to resume |
| `EDUBOTICS_COLLISION_GPIO_TOPIC` | `/gpio_command_controller/gpio_states` | Force topic to subscribe |
| `EDUBOTICS_COLLISION_USE_OVERLOAD_BIT` | `1` | Honor the firmware Overload bit as a hard‑trip backstop |

These live in the EduBotics `.env` (`%LOCALAPPDATA%\EduBotics\.env`). All are forwarded in
`docker-compose.yml`. The defaults were measured with a 35 s no‑contact full‑workspace sweep
(p95 envelope J1=0.08, J2=0.44, J3=0.26, J4=0.04, J5=0.02; threshold = clamp(p95×1.5, 0.30,
0.95)) — servos and friction are identical across classroom rigs, so they generalize.

---

## 4. Validation state: **COMPLETE (2026‑06‑04)** — this section is now a re‑validation guide

Validated live on the reference rig (sessions 2026‑06‑03 + 2026‑06‑04):
- Follower init with the model‑aware xacro/yaml: **OK** (`OMXFSystem` init success, 100 Hz
  `/joint_states`, gpio stream 100 Hz with per‑model signals, `[KOLLISION] … guard armed`).
- Step 3 calibration: **DONE**, values baked as the shipped defaults (table above). No false
  trips at rest / during normal full‑workspace teleop.
- **7 complete live collision cycles** (trips on joint1 + joint3, efforts 0.30–0.78): trip →
  relax‑in‑place (servos stop pressing, arm holds) → modal Schritt 1 → student‑triggered
  verified home glide → Schritt 2 → strict resume → teleop restored. Includes a mid‑recording
  trip with episode discard (prior episodes kept, clean re‑record afterwards). UI fully live
  (no reloads) after the `rosConnectionManager` empty‑URL fix + StrictMode removal.
- NOT exercised live (unit‑tested only): the stalled‑glide retry/failure path (foam compresses
  out of the way; would need holding the arm) and the firmware‑Overload reboot path.
- Two incidental rig findings, unrelated to the feature: the WSL2 `vhci_hcd` USB/IP bridge
  reset once mid‑session and dropped BOTH arm attachments (recovery: re‑attach via usbipd +
  restart `open_manipulator`); the leader's `-3002` read storm was the degrading USB link —
  after a physical re‑plug the leader runs at 99 Hz clean.

For a future re‑validation (new rig / changed thresholds), the working setup was:
- Images: local‑only `nettername/*:collision-validate` retags (obsolete once v2.6.0 images are
  published — use the normal pinned tags instead).
- `.env`: `EDUBOTICS_COLLISION_ENABLED=1`; per‑rig `EFFORT_J*` only if re‑calibrating.
- Dev GUI: `cd Testre\robotis_ai_setup\gui` then
  `$env:EDUBOTICS_IMAGE_TAG='<tag>'; $env:EDUBOTICS_SKIP_AUTO_PULL='1';
  Start-Process python main.py`.
- Arm USB detaches on container stop / distro restart: `usbipd attach --wsl EduBotics
  --busid <busid>` for BOTH arms before scanning (busids shift between reboots — identify via
  `usbipd list`, the two `2f5d:2202` serial devices).
- React dev server (`npm start` in `physical_ai_manager`, browser at `localhost:3000`)
  connects to the rig's rosbridge — useful for UI iteration without an image rebuild.

## 5. Procedure

### Helpers
```bash
omx() { wsl -d EduBotics -- docker exec open_manipulator bash -lc \
  "source /opt/ros/jazzy/setup.bash && source /root/ros2_ws/install/setup.bash && $*"; }
pas() { wsl -d EduBotics -- docker exec physical_ai_server bash -lc \
  "source /opt/ros/jazzy/setup.bash && source /root/ros2_ws/install/setup.bash && $*"; }
```
The physical_ai_server node log is at `/var/log/physical_ai_server/current` inside the
container (s6‑log) — docker stdout is drowned by a harmless `talos.agent.s6_agent` crash‑loop.

### Step 1 — Bring up the stack
Open the dev GUI (above). Attach follower USB if needed. Scan both arms, click
**„Umgebung starten"**, wait for healthy containers, confirm
`pas "grep KOLLISION /var/log/physical_ai_server/current | tail -5"` shows „guard armed".

### Step 2 — Preflight
```bash
omx "ros2 topic echo --once /gpio_command_controller/gpio_states"  # interface_groups + per-model signals
omx "ros2 topic echo --once /collision_flag"                       # exists (may be silent until trip)
```
Confirm dxl11‑13 rows carry `Present Load` and dxl14‑15 carry `Present Current`, each with
`Hardware Error Status`. Values at rest are small (|fraction| ≪ threshold) after the unsigned →
int16 mapping.

### Step 3 — (Only if re‑calibrating) per‑joint thresholds
```bash
wsl -d EduBotics -- docker exec -it open_manipulator \
  python3 /usr/local/bin/calibrate_collision_currents.py --duration 30
```
Move the follower slowly through its full workspace, no contact. Paste the printed
`EDUBOTICS_COLLISION_EFFORT_J*` lines into `.env`, restart the stack, confirm no false trips.

### Step 4 — End‑to‑end two‑step dry run (use a SOFT obstacle, e.g. a foam block)
Start teleop; begin recording an episode; gently push the follower into the obstacle. Verify,
in order:
1. `omx "ros2 topic echo /collision_flag"` flips to `data: true`; the follower **stops pressing
   and holds in place** (no auto‑home, no slump, no continued force — listen: the servos stop
   straining).
2. The React modal (dev server) shows **Schritt 1** „STOPP — Kollision erkannt" with the
   „Follower in Grundstellung fahren" button; the leader no longer drives the follower.
3. The in‑progress episode is **gone, prior episodes remain** (dataset dir under the
   `huggingface_cache` volume — no partial/post‑trip episode).
4. Click „Follower in Grundstellung fahren" **with the obstacle still in the way** → the glide
   stalls, re‑sends ~3×, then the modal returns to Schritt 1 with the German retry hint
   („… konnte die Grundstellung nicht erreichen …"). The flag stays `true`.
5. Remove the obstacle, click the button again → the follower glides home (~2.5 s quintic),
   modal advances to **Schritt 2** („Follower in Grundstellung").
6. Click „Teleoperation fortsetzen" with the leader still far away → refused, German hint
   („zu weit entfernt"), modal stays.
7. Bring the leader near the home pose, click again → smooth ~3 s resync, `/collision_flag`
   goes `false`, modal closes, the leader drives the follower again.
8. Re‑start recording and confirm a normal episode records cleanly.
9. Also verify a trip in **free teleop** (not recording): same two‑step flow, no dataset side
   effects.

### Step 5 — Rollback + inference safety
- `EDUBOTICS_COLLISION_ENABLED=0` in `.env`, restart, push → **no trip**. Re‑enable afterwards.
- Start **inference**, force the follower against the obstacle → must **NOT** trip
  (mode‑gating), inference behaves exactly as before.

### Step 6 — (Rare) firmware‑latch path
If a sustained hard press latches the firmware Overload bit, the trip records the joint and the
**HOME_FOLLOWER** step makes a best‑effort `reboot_dxl` + torque re‑enable *before* the glide
(a latched joint cannot move). If `dynamixel_interfaces` isn't importable in the server image,
the log warns and the student restarts the environment. Note whether reboot worked.

---

## 6. Validation report (2026‑06‑04, reference rig)
- Relax‑in‑place reliably stops the pressing on every trip (7/7); arm holds, no slump.
- Two‑step recovery end‑to‑end live in the UI, zero reloads (after the rosConnectionManager
  empty‑URL fix); resume strictly refused until homed; leader‑proximity re‑prompt works.
- Mid‑recording trip: in‑progress episode discarded, prior episodes kept, clean re‑record.
- No false trips during normal teleop with the calibrated defaults.
- gpio reads at 100 Hz did not destabilize the bus (the `-3002` storm observed was a failing
  USB link on the leader, fixed by re‑plug — present before and independent of the gpio reads).
- Stalled‑glide + firmware‑Overload paths: unit‑tested, not reproduced on hardware.

**Shipped in v2.6.0:** validation bind‑mounts stripped from compose, CLAUDE.md updated
(Rule §2 sanctioned‑exception note + "Critical architectural choices" entry), server
Dockerfiles install + build‑gate `ros-jazzy-control-msgs`. Rig cleanup after the release:
remove `IMAGE_TAG=collision-validate` + `EDUBOTICS_SKIP_AUTO_PULL` from the rig `.env`,
delete the local `:collision-validate` images.
