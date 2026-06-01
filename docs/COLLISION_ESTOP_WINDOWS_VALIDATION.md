# Teleop Collision E‑Stop — Windows Hardware Validation Runbook

**Read this first — you are (probably) a fresh Claude Code session on a Windows 11 PC.**
This document is fully self‑contained: it assumes you have **no memory** of how this feature was
built. You have the **Testre** repo cloned (VS Code) and the **EduBotics** WSL2 distro + the
EduBotics GUI installed, with both OMX arms (leader + follower) physically connected. Your job is
to **calibrate** the per‑joint force thresholds on this real rig and **validate** the teleop
collision e‑stop end‑to‑end, then **report back** so the defaults can be finalized.

The calibration and the live dry‑run **cannot be done on a Mac** (Docker Desktop for macOS can't
pass the USB serial bus into a container, and the stack is Windows‑WSL2 / Jetson only) — that's
why this runs here.

---

## 1. What the feature does (and the invariants you must NOT break)

During teleoperation the student moves the back‑drivable **leader** arm by hand; the **follower**
mirrors it over the ROS topic `/leader/joint_trajectory`. The follower arm joints run position
control with **no current limit**, so forcing the arm into an object winds the motors toward stall
current and can damage them. This feature detects that and stops safely.

**Flow:** detect high motor force (per‑joint `Present Current`, velocity‑gated, debounced) →
publish `/collision_flag=true` (freezes the leader→follower stream) → glide the follower to a safe
home pose → discard the in‑progress recording episode → show a big German blocking modal → student
brings the leader near the home pose and clicks **„Teleoperation neu starten"** → server resyncs
follower→leader and clears the flag.

**Load‑bearing invariants — do not "fix" these away:**
1. **Glide, never freeze.** On collision the follower is commanded to a safe home pose (gripper
   held). It must NOT just hold the contact pose (a position‑mode joint would keep pushing) and
   must NOT torque‑disable mid‑air (the arm would slump under gravity).
2. **Flag before home.** `/collision_flag=true` is published *before* the safe‑home trajectory,
   else the 100 Hz leader broadcaster overwrites the home setpoint.
3. **Teleop‑only.** The guard is gated OFF during inference (`mode_is_inference`), and the leader
   broadcaster (the only `/collision_flag` consumer) isn't in the inference path. It must never
   reshape the recorded/replayed action distribution (Rule §2).
4. **Discard, don't record.** On a trip mid‑recording the in‑progress episode is discarded via
   `re_record()`; the safe‑home glide is never recorded. Prior saved episodes are kept.
5. **Gripper (dxl16) is excluded** from current monitoring (it's an XL430‑class servo that may
   lack a `Present Current` register, and it's already current‑limited at 350 mA). Do NOT add
   `Present Current` to dxl16.

## 2. Files that implement it (orientation)

- **Expose force (open_manipulator):**
  `robotis_ai_setup/docker/open_manipulator/overlays/omx_f.ros2_control.xacro` (adds
  `Present Current` + `Hardware Error Status` state interfaces to dxl11–15),
  `…/overlays/omx_f_hardware_controller_manager.yaml` (adds `gpio_command_controller`),
  `…/overlays/omx_f_follower_ai.launch.py` (spawns it).
- **Detection + orchestration (physical_ai_server):**
  `physical_ai_tools/physical_ai_server/physical_ai_server/safety/collision_detector.py` (pure
  logic), `…/safety/collision_monitor.py` (ROS wiring mixin),
  `…/physical_ai_server.py` (constructs it + the `RESUME_TELEOP` service branch),
  `…/package.xml` (adds `control_msgs`).
- **Interfaces:** `physical_ai_tools/physical_ai_interfaces/srv/SendCommand.srv`
  (`RESUME_TELEOP = 8`), `…/msg/TaskStatus.msg` (`COLLISION = 7`).
- **React (student):** `…/physical_ai_manager/src/components/CollisionModal.js`,
  `…/hooks/useRosTopicSubscription.js`, `…/hooks/useRosServiceCaller.js`,
  `…/features/tasks/taskSlice.js`, `…/constants/taskPhases.js`, `…/constants/taskCommand.js`,
  `…/StudentApp.js`.
- **Calibration helper:** `robotis_ai_setup/docker/open_manipulator/calibrate_collision_currents.py`.
- **Config:** `robotis_ai_setup/docker/docker-compose.yml` (the `EDUBOTICS_COLLISION_*` env block
  on the `physical_ai_server` service).

## 3. Tunables (defaults; refine `CURRENT_J*` in Step 3)

| Env var | Default | Meaning |
|---|---|---|
| `EDUBOTICS_COLLISION_ENABLED` | `1` | Master switch / one‑variable rollback (`0` = fully off) |
| `EDUBOTICS_COLLISION_CURRENT_J1..J5` | `1.5,1.5,1.2,1.0,1.0` | Per‑joint trip current (Amps), joint1..joint5 |
| `EDUBOTICS_COLLISION_VELOCITY_GATE` | `0.05` | rad/s; below this the joint counts as "not moving" |
| `EDUBOTICS_COLLISION_DEBOUNCE_MS` | `150` | Sustained‑over time before tripping |
| `EDUBOTICS_COLLISION_RESUME_TOL_RAD` | `0.30` | Max leader↔home gap allowed to resume |
| `EDUBOTICS_COLLISION_GPIO_TOPIC` | `/gpio_command_controller/gpio_states` | Force topic to subscribe |
| `EDUBOTICS_COLLISION_USE_OVERLOAD_BIT` | `1` | Honor the firmware Overload bit as a hard‑trip backstop |

These live in the EduBotics `.env` (next to `docker-compose.yml` in the installed stack — typically
under the install dir, mounted into WSL). All are already forwarded in `docker-compose.yml`.

---

## 4. Procedure

### Helpers
Run ROS commands inside the containers (container names: `open_manipulator`, `physical_ai_server`).
Define a shell helper in your terminal:
```bash
omx() { wsl -d EduBotics -- docker exec open_manipulator bash -lc \
  "source /opt/ros/jazzy/setup.bash && source /root/ros2_ws/install/setup.bash && $*"; }
pas() { wsl -d EduBotics -- docker exec physical_ai_server bash -lc \
  "source /opt/ros/jazzy/setup.bash && source /root/ros2_ws/install/setup.bash && $*"; }
```

### Step 0 — Get the feature images onto this PC
The student GUI pulls `nettername/*:latest` (or a pinned `IMAGE_TAG`). The collision feature ships
in the `open-manipulator` and `physical-ai-server` images.
- **Preferred:** the branch `feat/teleop-collision-estop` is merged to `main` → `docker-publish.yml`
  builds + pushes → the GUI pulls on launch / environment start. Confirm the running images are
  recent (`wsl -d EduBotics -- docker image ls | findstr physical-ai-server`).
- **Pre‑merge local build (advanced):** from a clean Linux build host only, per CLAUDE.md
  `build-images.sh` is CI‑only — do not build from the Windows box. If you must test before merge,
  build on the CI branch and pull the `<sha>` tag by setting `IMAGE_TAG` in `.env`.

### Step 1 — Bring up the stack
1. Open the EduBotics GUI. Scan both arms, then click **„Umgebung starten"**.
2. Confirm the follower's USB is attached to the distro:
   `wsl -d EduBotics -- usbipd list` (the follower's 2 servos should be attached; cameras are
   native on the WSL2 path and are NOT usbipd‑attached).
3. Wait for healthy: `wsl -d EduBotics -- docker ps` shows `open_manipulator` and
   `physical_ai_server` healthy.

### Step 2 — Preflight checks (catch the bench‑validation unknowns)
```bash
omx "ros2 topic list | grep -E 'collision_flag|gpio'"      # expect /collision_flag and a gpio_states topic
omx "ros2 topic echo --once /gpio_command_controller/gpio_states"   # confirm it publishes
omx "ros2 interface show control_msgs/msg/DynamicJointState"
```
- **Confirm the gpio topic name.** If it's `/dynamic_joint_states` (or other), set
  `EDUBOTICS_COLLISION_GPIO_TOPIC` in `.env` accordingly and restart.
- **Confirm `Present Current` + `Hardware Error Status` are present** in the echoed
  `interface_values` for dxl11–15 (the message lists per‑joint `interface_names`). If
  `Hardware Error Status` is rejected at hardware init (check `omx "ros2 ..."` / container logs for
  a `CANNOT_FIND_CONTROL_ITEM`‑style error and the open_manipulator container failing to start),
  remove `Hardware Error Status` from the xacro + the yaml and set
  `EDUBOTICS_COLLISION_USE_OVERLOAD_BIT=0` (current‑threshold detection still works fully).
- **Confirm `Present Current` units.** Echo it while the arm holds a load; the monitor converts raw
  counts × `0.00269` → Amps (`PRESENT_CURRENT_A_PER_LSB` in
  `safety/collision_detector.py`). A joint holding against gravity should read on the order of
  tenths of an Amp. If the echoed numbers are already in Amps (≈0.1–0.5) rather than raw counts
  (≈40–200), the model unit scale differs — adjust the conversion constant and re‑note it.
- **Confirm dxl16 model** (gripper) — it must be excluded; if it's actually an XM430 the exclusion
  is still safe (we simply don't monitor it).
- **Bus stability (critical).** Teleoperate normally for ~2 min and watch the open_manipulator logs
  for `SYNC_READ_FAIL` / `BULK_READ_FAIL` / `error_timeout_ms`:
  `wsl -d EduBotics -- docker logs -f open_manipulator`. The two extra reads per joint must not
  destabilize the 100 Hz bus. **Mitigation if it does:** drop `Present Velocity` from the
  `gpio_command_controller` `state_interfaces` block in
  `omx_f_hardware_controller_manager.yaml` (the monitor uses `/joint_states` velocity, not the gpio
  one) and/or accept a slightly higher debounce.

### Step 3 — Calibrate the per‑joint thresholds
```bash
wsl -d EduBotics -- docker exec -it open_manipulator \
  python3 /usr/local/bin/calibrate_collision_currents.py --duration 30
```
Follow the German prompts: with the follower powered and **no object contact**, move the arm slowly
through its full normal workspace for 30 s. It prints `EDUBOTICS_COLLISION_CURRENT_J1..J5=…` lines.
Paste them into the EduBotics `.env`, then restart the stack (GUI „Umgebung neu starten" or
`wsl -d EduBotics -- docker compose ... restart physical_ai_server`). Re‑teleoperate the full
workspace + actuate the gripper and confirm **no false trips**.

### Step 4 — End‑to‑end dry run (use a SOFT obstacle, e.g. a foam block)
Start teleop (don't start inference). Begin recording an episode, then gently push the follower into
the obstacle. Verify, in order:
1. `omx "ros2 topic echo /collision_flag"` shows `data: true` — and it appears **before** the
   safe‑home trajectory on `omx "ros2 topic echo /leader/joint_trajectory"`.
2. The follower **glides to the home pose and holds** (no slump, no continued pushing).
3. The React student UI shows the big German blocking modal („STOPP — Kollision erkannt").
4. The in‑progress episode is **gone but prior episodes remain** — inspect the dataset dir under the
   `huggingface_cache` volume; episode count is unchanged and there is no partial/home‑glide episode.
5. Click resume with the leader still far from home → the modal stays and shows the „zu weit entfernt"
   re‑prompt (service returns `success=false`).
6. Bring the leader near the home pose, click **„Teleoperation neu starten"** → the follower resyncs
   to the leader (smooth ~3 s) → `/collision_flag` goes `false` → the leader drives the follower again.
7. Re‑start recording and confirm a normal episode records cleanly.

### Step 5 — Rollback + inference safety
- Set `EDUBOTICS_COLLISION_ENABLED=0` in `.env`, restart, repeat the push → **no trip** (rollback works).
  Re‑enable (`=1`) afterwards.
- Start **inference** mode, then force the follower against the obstacle → it must **NOT trip**
  (mode‑gating), and inference must behave exactly as before.

### Step 6 — (Rare) firmware‑latch path
The software guard normally trips well before the firmware Overload latches. If you can force a real
firmware Overload (sustained hard press), confirm resume recovers it — the resume path makes a
best‑effort `reboot_dxl` + re‑enable‑torque call. If `dynamixel_interfaces` isn't available in the
`physical_ai_server` image, the resume logs a warning and the arm may not move; in that case the
student restarts the environment. Note whether reboot worked for the report.

---

## 5. Report back (paste into the PR / hand to the next session)
- Measured `EDUBOTICS_COLLISION_CURRENT_J1..J5` (the calibrated values) and the chosen velocity
  gate / debounce.
- Any false trips during normal teleop (which joint, what motion).
- Bus stability result: any `SYNC_READ_FAIL` with the extra reads? Did you need the
  `Present Velocity`‑drop mitigation?
- Gpio topic name + whether it was `/gpio_command_controller/gpio_states` or different.
- `Present Current` units sanity (raw counts vs Amps) and whether the 0.00269 constant held.
- Whether `Hardware Error Status` was accepted by the hardware interface (or you set
  `USE_OVERLOAD_BIT=0`).
- Screenshot of the collision modal + a note that resume + episode‑discard worked.
- Firmware‑latch / reboot result (if tested).

These feed a small follow‑up PR that bakes the calibrated per‑joint defaults into
`collision_detector.DEFAULT_CURRENT_THRESHOLDS_A` and the compose defaults.
