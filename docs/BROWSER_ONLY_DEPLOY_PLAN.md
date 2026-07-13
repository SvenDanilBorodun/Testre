# EduBotics Browser-Only Deployment — Deep Architecture Plan (rev 2, 2026-07-13)

Status: PROPOSAL (research-grade, code-audited). Successor question to the Orange-Pi/edge plans:
**can we deploy EduBotics "somewhere else" — containers and all — so a student needs NOTHING
on their PC except a current Chromium browser?** No `.exe`, no WSL2, no Docker Desktop-less
Docker, no usbipd, no Pi/edge box on the desk. The arms and cameras stay on the student's desk
and plug into the student's PC over USB, because Physical AI without physical hardware is not
the product.

This plan was derived from a fresh full-source audit (file:line cites throughout) plus web
research (URL cites at the bottom). It deliberately does NOT build on the earlier
`ORANGE_PI_DEPLOY_PLAN.md` / `INSTALL_SPLIT_PLAN.md` documents — but it DOES reconcile with
the **parked 2026-06-07 browser-migration study** archived in `docs/CLAUDE-CHANGELOG.md`
(rev 1 ignored it and re-derived several of its findings, sometimes wrongly).

## Rev 2 changelog (what a line-by-line re-verification against repo HEAD fixed)

Every `file:line` cite below was re-checked against the code on 2026-07-13. Corrections:

1. **Web Serial re-attach was unsound.** `SerialPort.getInfo()` exposes ONLY
   `usbVendorId`/`usbProductId` — no serial number. Leader and follower are the SAME board
   (OpenRB-150, VID `2F5D`; the two known PIDs `0103`/`2202` are board/firmware variants, and
   the udev rules map BOTH to `edubotics-follower` — `jetson_agent/udev/99-edubotics-robotis.rules:17-18`).
   Today the GUI tells the arms apart by their distinct **USB serial numbers**, which the
   browser cannot see. → The ping-sweep role identification (IDs 1–6 vs 11–16,
   `identify_arm.py:16-18`) runs on **every** connect; persisted `getInfo()` only pre-filters
   the port picker. (§3.1)
2. **`Drive Mode 4` was missing from the servo init table.** Every follower servo is
   configured `Drive Mode 4` = **time-based profile**, which changes the meaning of
   `Profile Velocity 50 / Profile Acceleration 25` from velocity units to **milliseconds**
   (`omx_f.ros2_control.xacro:94,112,132,151,170,187`). The leader uses `Drive Mode 0`. A
   runtime that omits this writes completely different motion profiles. (§3.2)
3. **The leader trigger is NOT "held at 50 Hz".** The launch runs
   `ros2 topic pub -r 50 -t 50 -p 50 … 'data: [-0.7]'` — 50 messages over ~1 s, then the
   process EXITS (`omx_l_leader_ai.launch.py:148-158`). Goal Position persists in the
   register; the 300 mA current cap does the spring feel. The browser needs ONE init write. (§3.2)
4. **HOME-pose conflation fixed.** The entrypoint boot/workflow HOME is
   `[0, −π/2, π/2, 0, 0, 0.8]` (`entrypoint_omx.sh:461`, `workflow/handlers/motion.py:67` matches), but
   the **collision-recovery** home is `SAFE_HOME_ARM = (0, −π/4, π/4, 0, 0)` — arm joints
   only, gripper held (`collision_monitor.py:108`, deliberately matching
   `jetson_agent/agent.py` `SAFE_HOME_JOINTS`). These are two different poses; rev 1 (and
   CLAUDE.md's "four lockstep sites" line) conflated them. (§3.3, §3.4; CLAUDE.md fixed in
   this change.)
5. **"Classroom Jetson path unaffected" was wrong.** A cloud-HTTPS SPA cannot open
   `ws://<jetson-LAN-ip>:9091` — mixed content blocks it outright and Chrome ≥147
   additionally permission-gates WebSockets to private IPs (Local Network Access). Today it
   works only because the SPA is served from `http://localhost`. Browser-only students lose
   the Jetson tab unless it moves to WebRTC DataChannel (the archived study's conclusion) —
   declared a non-goal for round 1, with the trigger condition stated. (§4.6)
6. **`inference_manager.predict()` does not return a 15-action chunk.** It returns ONE
   action per call via lerobot's `predict_action` → `policy.select_action`; the chunk lives
   in the policy's internal action queue (`inference_manager.py:23-28,214-287`). The Modal
   endpoint therefore needs a thin chunk-drain wrapper (lerobot 0.5.1's
   `predict_action_chunk` path), not `predict()` "as-is". Also `n_action_steps=15` is an
   EduBotics-injected, **user-overridable** ACT default (`training_handler.py:595-612`) —
   the executor must read the chunk length from the model config, never assume 15. (§4.3)
7. **Prior art was oversold.** lerobot.js/bambot are **Feetech-only at ~10–30 Hz teleop**;
   there is no known Dynamixel-Protocol-2.0-over-WebSerial implementation anywhere. The only
   direct evidence is our own parked spike (single-board reads benched 503–713 Hz; the
   archived gate G0 was "mirror ≥60 Hz sustained, target 100, p99 < 1 cycle"). (§1c, §8)
8. **§3.4 now carries the full e-stop contract** (12 behaviors rev 1 omitted: torque is
   never disabled at trip; relax = 3 sends total; trip-sequence ordering; 5 Hz flag
   watchdog incl. idle-False self-heal; leader-gate trip DROP; settle-window semantics;
   FORCE_RESUME; phase wire ints; env knobs; fail-open rules; incomplete-pose guard;
   resume cache-clearing).
9. **Bus Watchdog semantics verified against the e-manual** and made precise: on trigger the
   servo stops in place with **torque still enabled** and REJECTS further Goal writes until
   the error is cleared by writing 0 — the recovery path must clear it. (§3.5)
10. Smaller fixes: leader trigger gains are P=1000/**D=1500** (no I, no Profile params,
    `omx_l.ros2_control.xacro:142-144`); leader arm servos are `Operating Mode 0` (current
    mode) + `Torque Enable 0`; hardware-interface params `error_timeout_ms 500` /
    `disable_torque_at_init true` / follower `is_async="true"` join the contract; ingest
    JPEG estimate is ~30–60 KB (`camera_ingest_node.py:63`), quality 80 via
    `EDUBOTICS_CAMERA_JPEG_QUALITY` (`gui/app/constants.py:190`); the SPA's rosbridge surface
    is ~35 services (CLAUDE.md's executor figure), not 38; `dynamixel_hardware_interface` is
    NOT vendored — it is cloned from ROBOTIS-GIT at **unpinned `main`** at image build
    (`open_manipulator_ci.repos`), so parts of today's contract live outside the repo and
    must be bench-verified, not just code-read; the concrete per-tick bus-transaction layout
    is now specified (§3.3); the dataset-upload bandwidth risk is upgraded and the video
    fidelity trade-off is stated honestly (§4.2).

---

## 0. TL;DR

The three student-side Docker containers do not have to run *near the student* at all — they
have to run *near their responsibilities*, and those split cleanly:

| Today (student PC, WSL2) | Responsibility | Browser-only home |
|---|---|---|
| `open_manipulator` (ROS 2 + ros2_control + Dynamixel) | 100 Hz hard-realtime servo I/O, boot sync, torque safety | **The Chrome tab itself** — Web Serial API straight to the OpenRB-150 boards (factory firmware is already a Protocol 2.0 USB passthrough) |
| `physical_ai_server` (recorder, inference, Roboter Studio, safety FSM) | 30 Hz soft-realtime dataset/inference/workflow logic | **Cloud session service** (CPU) + **Modal GPU inference endpoint**; the collision detector and trajectory pacing move into the browser |
| `physical_ai_manager` (nginx + React) | Serving the SPA | **Static hosting** (same Railway pattern as the existing teacher-web build) |

Three load-bearing facts make this real and not a fantasy:

1. **The OpenRB-150's factory-default firmware is `usb_to_dynamixel`** — a transparent
   USB-CDC ↔ DYNAMIXEL Protocol 2.0 bridge at 1 Mbps bus speed. The C++
   `dynamixel_hardware_interface` is *already* just a PC-side Protocol 2.0 speaker; Chrome's
   Web Serial API can be the same speaker. Our own parked spike benched WebSerial reads off
   an OpenRB-150 at **503–713 Hz single-board**; the still-unproven part is the dual-board
   full mirror loop (archived gate G0: ≥60 Hz sustained, target 100 — §8/P0).
2. **Every EduBotics software layer above the servo bus is already network-shaped.** Teleop is
   a local leader→follower position mirror (no network in the loop at all). Everything else on
   the command rail is *trajectories*, not per-tick setpoints (`/leader/joint_trajectory` carries
   single-point teleop/inference msgs and 50-point quintic moves —
   `om_joint_trajectory_command_broadcaster/src/joint_trajectory_command_broadcaster.cpp:283-342`,
   `entrypoint_omx.sh:311-333`, `data_converter.py:206-245`). Chunked commands tolerate WAN latency.
3. **The Python that matters is already ROS-free.** `workflow/` (the whole Roboter Studio
   runtime) imports zero rclpy — its hardware boundary is **13 injected callables**
   (`physical_ai_server.py:4090-4116`, enumerated in §4.4). `inference_manager.py` imports only
   torch/lerobot/numpy + one pure util (`inference_manager.py:19-28`). The LeRobot dataset
   wrapper is pure (`lerobot_dataset_wrapper.py`). The cloud API is plain FastAPI+JWT. These
   lift into cloud services with small shims, not rewrites.

The one genuinely new artifact is a **browser hardware runtime** (`@edubotics/dxl-web`, the
name the archived study already coined: a TypeScript re-implementation of the Dynamixel layer
+ collision e-stop + trajectory pacing, running in a dedicated Worker). Its full contract is
extracted in §3 — every register, constant, and FSM state it must replicate is enumerated and
cited. One honest caveat: the exact read/write composition of today's loop lives in the
**unpinned upstream** `dynamixel_hardware_interface` (cloned from ROBOTIS-GIT `main` at image
build, `open_manipulator_ci.repos` — not vendored), so the contract below is xacro + observed
behavior, and P0 must bench against the real firmware, not against a code-reading.

---

## 1. Why NOT the two "obvious" alternatives

### 1a. Lift the containers to the cloud unchanged, tunnel USB
Rejected. The ros2_control loop performs a **synchronous sync-read + sync-write serial
transaction cycle every 10 ms** (update_rate 100, `omx_f_hardware_controller_manager.yaml:4`).
Tunneling the serial byte stream over a WAN puts 20–80 ms of RTT *inside* each 10 ms cycle —
the loop collapses, the JTC watchdogs fire, and the boot-sync/verify phases
(`entrypoint_omx.sh:267-408`) time out. USB/IP over WAN also has no browser story. The
hard-realtime endpoint must terminate on the same machine the USB cable plugs into — and the
only runtime a zero-install student PC offers is the browser.

### 1b. Keep a per-desk edge box (Pi/Orange Pi/Jetson)
Explicitly out of scope for this plan — the whole point is removing that box. (The classroom
Jetson remains a separate, already-shipped inference target — but see §4.6: it is NOT
reachable from a browser-only student page as-is.)

### 1c. Why the browser CAN be the realtime endpoint
- Web Serial API is stable in Chrome/Edge ≥ 89 and on **ChromeOS** (managed school Chromebooks
  work), HTTPS + one user gesture per port; **schools can pre-grant access with the
  `SerialAllowUsbDevicesForUrls` / `SerialAllowAllPortsForUrls` enterprise policies keyed to our
  origin + VID `0x2F5D`** — zero prompts on managed devices.
- Prior art, stated honestly: HuggingFace-ecosystem **lerobot.js** proves browser-WebSerial
  arm control end-to-end, but for **Feetech STS3215** servos at **~10–30 Hz teleop** — there
  is no known DYNAMIXEL-Protocol-2.0-over-WebSerial implementation anywhere. The strongest
  evidence is **our own parked spike**: single-board OpenRB-150 reads at 503–713 Hz (100 Hz
  solid) through the CDC bridge (no FTDI 16 ms latency timer — the arms are CDC-ACM, not
  U2D2). The dual-board mirror loop is exactly what P0 re-proves.
- The 100 Hz loop lives in a **dedicated Web Worker** — workers are exempt from the
  background-tab `setTimeout` clamping (incl. "intensive throttling") that would kill a
  main-thread loop; the leader→follower mirror has zero network dependency, so Wi-Fi jitter
  cannot make the arm stutter during teleop. What CAN stop a worker is tab freeze/discard and
  laptop sleep — mitigated by the firmware Bus Watchdog (§3.5), a Screen Wake Lock during
  active sessions, and P0 measuring Chrome's freeze behavior with an open serial port +
  active getUserMedia (both are strong "do not discard" signals to Chrome; verify, don't
  assume).

Firefox/Safari have no WebSerial — hence the product requirement the user already accepts:
**"the correct Chrome browser"** (Chrome/Edge/Chromium, or a managed Chromebook).

---

## 2. Target architecture

```
 Student desk                                   Cloud (all existing infra kept)
┌──────────────────────────────┐
│  Chrome tab (HTTPS SPA)      │   WSS (JWT)   ┌──────────────────────────────────┐
│ ┌──────────────────────────┐ │◄─────────────►│ Session service (CPU, FastAPI,   │
│ │ Hardware Worker          │ │  obs 1-5 Hz   │  EU region)                      │
│ │  · WebSerial ×2 boards   │ │  cmd chunks   │  · workflow/ engine (as-is)      │
│ │  · P2.0 sync R/W @100Hz  │ │               │  · perception+calibration (cv2)  │
│ │  · teleop mirror (local) │ │               │  · recorder→LeRobot packager     │
│ │  · collision e-stop FSM  │ │               │  · heartbeat/status              │
│ │  · quintic/JTC pacing    │ │               └───────────┬──────────────────────┘
│ │  · Bus Watchdog arming   │ │                           │ spawns/queries
│ └──────────────────────────┘ │               ┌───────────▼──────────────────────┐
│  getUserMedia ×2 cameras     │               │ Modal GPU endpoint (L4, WSS)     │
│  (timestamps → 15 ms sync)   │               │  · loads student policy          │
│  OPFS episode buffer         │               │  · obs → action CHUNK (n from    │
│  (persist() + crash journal) │               │    model config, ACT default 15) │
└──────────────────────────────┘               └──────────────────────────────────┘
      USB: 2× OpenRB-150 (VID 2F5D)            cloud_training_api / Supabase /
                                               Modal training / HF Hub: UNCHANGED
```

**What stays byte-identical:** `cloud_training_api` (all routes are already browser-callable
JSON+JWT — `auth.py:10-48`, Supabase `auth.get_user` verification), Supabase
(auth/RLS/realtime), Modal training (`modal_training/`), the HF dataset/model flow, the
teacher web deploy, and the existing `.exe` product (which remains the offline/fallback SKU —
see §9). The classroom Jetson keeps working for `.exe` students; for browser-only students it
is a declared round-1 non-goal (§4.6).

**What is deleted from the student PC:** installer, WSL2 rootfs, Docker, usbipd, the tkinter
GUI, the camera MSMF bridge, the WebView2 child process, the `.env` machinery, per-machine
`ROS_DOMAIN_ID` (no DDS graph exists anymore in this path).

---

## 3. The browser hardware runtime (the new artifact)

This is the WebSerial re-implementation of what `dynamixel_hardware_interface` + the xacro
configs + the entrypoint + `collision_monitor` do today. Everything below is the extracted,
citable contract. (Reminder: the C++ side is unpinned upstream — bench, don't just port.)

### 3.1 Bus layer
- **Protocol 2.0 at 1,000,000 baud** (`identify_arm.py:14-15`, `omx_f.ros2_control.xacro:20`,
  `omx_l.ros2_control.xacro:20`). baudRate is nominal for native-USB CDC (SAMD51), but must
  still be passed through so the OpenRB-150 bridge clocks its DXL-side UART correctly.
- Port discovery: `navigator.serial.requestPort({filters:[{usbVendorId: 0x2F5D}]})`.
  **Role identification runs on EVERY connect** — `getInfo()` exposes only VID/PID, both
  boards are OpenRB-150s sharing the same VID/PID set (PIDs `0103`/`2202` are variants, both
  udev-mapped to the follower symlink — `99-edubotics-robotis.rules:17-18`), and the USB
  serial numbers the GUI uses today (`fast_rehydrate_arms`) are invisible to Web Serial. The
  existing ping-sweep is the identifier: ping IDs 1–6 vs 11–16, majority wins
  (`identify_arm.py:16-18` + `identify()`); it costs <100 ms per port. Persisted
  `navigator.serial.getPorts()` grants only remove the picker prompt, never the sweep.
- Packet layer to port to TypeScript: P2.0 framing (0xFFFFFD header, byte-stuffing), CRC-16,
  Ping (0x01), Read (0x02), Write (0x03), **Sync Read (0x82) / Sync Write (0x83)**, Reboot (0x08).
  ~600 LOC, table-driven, fully unit-testable against golden packets.
- Transient-error tolerance: the C++ interface tolerates up to `error_timeout_ms` **500 ms**
  of consecutive failed transactions before declaring a hardware error
  (`omx_f.ros2_control.xacro:21`) — the Worker adopts the same window before tripping its
  own bus-fault state.

### 3.2 Servo init writes (follower IDs 11–16, leader IDs 1–6)
Replicates the xacro `<param>` blocks exactly (`omx_f.ros2_control.xacro:75-202`,
`omx_l.ros2_control.xacro:72-145`). `disable_torque_at_init: true` on both arms — init
writes happen torque-off, then torque-on (EEPROM-class registers demand it anyway):

| ID | Model | Drive Mode | Op Mode | Limits / currents | Per-cycle reads | Writes |
|---|---|---|---|---|---|---|
| 11 | XL430-W250 | **4 (time-based profile)** | 4 (ext. pos) | — | PresPos, PresVel, **PresLoad**, HWErr | GoalPos |
| 12 | XL430-W250 | 4 | 3 | pos 830–3129 | same | GoalPos |
| 13 | XL430-W250 | 4 | 3 | pos 1024–3140 | same | GoalPos |
| 14/15 | XL330-M288 | 4 | 3 | pos 0–4095 | PresPos, PresVel, **PresCurrent**, HWErr | GoalPos |
| 16 | XL330 (gripper) | 4 | **5 (current-pos)** | CurrLimit **350**, GoalCurrent **350**, Shutdown **21** | PresPos, PresVel (no HWErr — gripper excluded from e-stop) | GoalPos |
| 1–5 (leader) | XL430/XL330 mix | 0 | 0 (current), **Torque OFF** (limp) | — | PresPos, PresVel | — |
| 6 (leader trigger) | XL330 | 0 | 5 | CurrLimit **300**, Pos P **1000** / D **1500** | PresPos, PresVel | GoalPos = −0.7 **once at init** (today: `ros2 topic pub -r 50 -t 50` — 50 msgs over ~1 s, then exits; the register holds — `omx_l_leader_ai.launch.py:148-158`) |

Common firmware params: Return Delay 0 everywhere; follower Pos P/I/D = 1000/0/1000,
Profile Velocity 50 / Acc 25 — **in Drive Mode 4 these are time values (ms), not
velocities** (`omx_f.ros2_control.xacro:94-101`). **Model-awareness is mandatory** — asking
an XL430 for Present Current aborts init on real hardware (documented scar,
`omx_f.ros2_control.xacro:81-91`); the effort source per joint is Load (÷1000) for J1–3 and
Current (÷1750) for J4–5, exactly as `collision_detector.py:47-60` encodes.

### 3.3 The 100 Hz loop (dedicated Worker)
Concrete bus schedule per 10 ms tick — the X-series control table makes this cheap because
Present Current/Load (126), Present Velocity (128) and Present Position (132) are one
contiguous 10-byte block:

1. Follower **Sync Read** addr 126 len 10, IDs 11–16 → PresLoad/Current + PresVel + PresPos.
2. Leader **Sync Read** addr 126 len 10, IDs 1–6 (only 128–135 consumed).
3. Follower **Sync Write** Goal Position (116, 4 bytes), IDs 11–16.
4. Hardware Error Status (addr 70, IDs 11–15) polled on a slow lane (~2–5 Hz) — it only
   matters for the Overload latch, and dropping it from the hot loop keeps the tick at
   3 transactions. (Today's stack reads it per-cycle via the gpio controller; the detector's
   only use is the 0x20 bit — `collision_detector.py:41`. If P0 shows headroom, move it back
   into the hot loop for exact parity.)

Command sources, in priority order (this replaces the ros2_control controller graph + the
`/arm_controller/joint_trajectory → /leader/joint_trajectory` remap,
`omx_f_follower_ai.launch.py:161`):

1. **Collision FSM override** (freeze / relax-in-place / quintic safe-home / resync) — §3.4.
2. **Trajectory executor** — plays server-sent or locally generated `(q, t_s[, v])` chunks with
   quintic interpolation; replaces the stock JointTrajectoryController. Quintic profile is the
   entrypoint's exact math: `s = 10t³ − 15t⁴ + 6t⁵` with matching `s_dot`/`s_ddot` terms —
   the boot sync publishes 50 points with explicit velocities AND accelerations, zero at both
   endpoints (`entrypoint_omx.sh:311-333`; duplicated `collision_monitor.py:712-714`).
   Velocity floor 2.88 rad/s peak (4.8 × 0.6) with quintic peak factor 15/8 ports from
   `trajectory_builder.py:40-44` (`_velocity_safe_duration`).
3. **Teleop mirror** — leader PresPos → follower GoalPos with the gripper **sign flip**
   (`reverse_joints: [gripper_joint_1]`, `omx_l_leader_ai_hardware_controller_manager.yaml:33-36`).
   Entirely local; the recorded "action" (leader pose) and "state" (follower readback) are
   sampled from this loop at the dataset fps. (Today the mirror is the
   `joint_trajectory_command_broadcaster` publishing at the 100 Hz controller rate with a
   collision-flag skip — `joint_trajectory_command_broadcaster.cpp:283-291`.)
4. Idle (hold — Goal Position persists; no re-write needed).

Boot sequence ports from `entrypoint_omx.sh`: leader up → follower up → **3 s quintic sync
leader-pose→follower** → verify (starts at DURATION+0.5 s, 2 s deadline, 0.10 s ticks;
0.30 rad tol, arm joints only, ≥50 % of commanded delta traversed to distinguish dropout from
finishing lag; soft-fail `[WARN]` — `entrypoint_omx.sh:267-408`) → cameras. The verifier
snapshots the pose at sync-publish time so a stale readback can't pass vacuously
(`entrypoint_omx.sh:305-315`). Follower-only mode homes to `[0, −π/2, π/2, 0, 0, 0.8]`
(`entrypoint_omx.sh:461`, matching `workflow/handlers/motion.py::HOME_JOINTS_RAD`) — note this is the
BOOT home; the collision-recovery home is a different pose (§3.4).

### 3.4 Collision e-stop (full port — pure logic already)
`collision_detector.py` is deliberately pure (imports only logging/dataclasses/typing,
`collision_detector.py:34-36`) with a full unit suite
(`robotis_ai_setup/tests/test_collision_detector.py` + 3 contract suites) and ports 1:1:

- Trip: `effort_fraction ≥ threshold AND |vel| ≤ 0.05 rad/s` debounced — **tick-counted, not
  time-based**: 150 ms × update rate → 15 consecutive samples at 100 Hz
  (`collision_detector.py:106-118, 192-218`; conversion in `build_detector_from_env:271`).
  If the Worker's actual read rate differs from 100 Hz it must recompute the tick count, or
  the debounce duration silently drifts. Thresholds `(0.30, 0.65, 0.40, 0.30, 0.30)` J1–J5;
  firmware Overload bit `0x20` = immediate trip regardless of debounce (`:209-212`).
  Signed-16 unwrap of unsigned-published values (`:94-96`). Missing effort resets the counter
  (a dropout can't trip); missing velocity defaults 0.0 (fails toward protection). Gripper
  excluded.
- Gating semantics preserved: OFF during inference lives INSIDE the detector
  (`collision_detector.py:184-186`); OFF during workflow + manual is the monitor's combined
  gate which also keeps the settle window armed while gated
  (`collision_monitor.py:409-416`). `COLLISION_SETTLE_WINDOW_S = 0.5` re-arms on every
  re-torque (`:170`). The leader-alive gate (2 s `/leader/joint_states` freshness, `:161,
  345-367`) becomes trivial in the browser — the Worker knows leader port state directly —
  but its semantic must survive: **a trip is DROPPED, not queued, when the leader is not
  live** (`:463-465`).
- FSM behaviors rev 1 omitted, all load-bearing (`collision_monitor.py`):
  - **Torque is NEVER disabled at trip.** "Freeze" = stop the mirror (today: publish
    `/collision_flag=true`; the C++ broadcaster skip-publishes — in the browser this is an
    `if` on the mirror source). Relax-in-place exists precisely because the servos stay
    torqued: commanding the MEASURED pose zeroes the position error so the arm stops
    pressing (`:565-585`). Relax = **3 sends total** (first delayed 0.15 s, then ≤2 re-sends
    every 0.35 s — `RELAX_DELAY_S:138`, `RELAX_RESEND_EVERY_S:139`, `RELAX_SENDS:140`), each
    re-capturing the current pose, and a send is SKIPPED (retried next cycle) if the pose
    readback is incomplete — never command 0.0 defaults (`:572-575`).
  - **Trip sequence order is load-bearing**: flag → relax → recording-discard → status, each
    step independently exception-guarded so one failure can't skip the next (`:489-535`);
    the recording timer stops BEFORE the episode discard (`:517-525`).
  - Two-step student-paced recovery: quintic **safe-home glide 2.5 s** to
    `SAFE_HOME_ARM = (0, −π/4, π/4, 0, 0)` — arm joints only, gripper held at its current
    position (`:108, 626-631`) — with verify/re-send loop (verify after 1.0 s, ≤3 sends,
    progress ε 0.05 rad, arrival tol 0.10 rad, `:146-151, 658-685`); then RESUME strictly
    gated: homed first, leader proximity ≤ 0.30 rad (`DEFAULT_RESUME_TOL_RAD:155`,
    env-overridable), 3 s quintic resync (`RESYNC_DURATION_S:153`), freeze clears only after
    resync + 0.5 s grace (`:820, 875-892`). HOME/RESUME refused while a resync is in flight.
  - **Overload recovery**: latched servos get a P2.0 Reboot first (today via
    `/dynamixel_hardware_interface/reboot_dxl`, `:122, 949-978`, best-effort), then
    re-torque via the set-torque path — the ONLY place torque is toggled.
  - **Flag/watchdog hygiene**: 5 Hz watchdog (`WATCHDOG_PERIOD_S 0.2`, `:154, 754-778`)
    re-asserts the flag AND the collision TaskStatus while active, and re-asserts **False
    when idle** — the self-heal that un-wedges a stale latch after a runtime restart. The
    browser port keeps both halves even though the "topic" becomes internal state shared
    with the session service.
  - **Recording semantics**: a trip DISCARDS the in-flight episode (buffer dropped, no
    filtering/clamping, episode count NOT decremented — `data_manager.re_record():604-613`,
    `_episode_reset():811-823`) and a mid-recording collision resumes the SAME dataset,
    re-recording the interrupted episode after resync (`:901-947`), clearing stale camera
    sync rings and restarting the topic-timeout window on resume.
  - **Escape hatches**: `FORCE_RESUME_TELEOP` (wire int 10) skips the homed requirement,
    proximity-gates against the follower's CURRENT pose, and never auto-resumes a recording
    (`:823-873`). Phase wire ints COLLISION=7 / COLLISION_HOMING=8 / COLLISION_HOMED=9 carry
    over unchanged. Env knobs port as session config: `EDUBOTICS_COLLISION_ENABLED` (master
    kill-switch), `_VELOCITY_GATE`, `_DEBOUNCE_MS`, `_USE_OVERLOAD_BIT`, `_EFFORT_J1..J5`,
    `_RESUME_TOL_RAD`.

This is Rule-§2-sensitive: the port changes *where* the sanctioned e-stop runs, not what it
does — same constants, same student-paced recovery, same discard-episode semantics. **Explicit
user sign-off required before implementation**, per CLAUDE.md.

### 3.5 Safety when the browser dies (different, and on balance better)
Today's guarantee is the entrypoint's `trap`-based torque disable
(`entrypoint_omx.sh:15-62`) — the arm goes LIMP on container death. A crashed tab can't run a
trap, but the X-series **Bus Watchdog register** (addr 98, 1 byte, unit 20 ms, range 1–127,
on both XL430-W250 and XL330-M288) gives a firmware-level backstop with precisely verified
semantics: if no instruction packet arrives within the window while torque is enabled, the
servo **stops in place with torque still enabled**, resets profile velocity/acceleration, and
**rejects all further Goal writes** (Data Range Error) until the error is cleared by writing
0 to the register. Consequences the design embraces:

- A killed tab / closed CDC port halts motion within the watchdog window (e.g. value 10 =
  200 ms; the 100 Hz loop feeds it continuously in normal operation).
- The arm HOLDS rather than drops — for a raised arm carrying an object this is safer than
  today's limp-drop; the trade-off is the motor keeps drawing holding current until
  re-connect or power-off. The gripper current cap (350 mA) still bounds grasp force.
- Every reconnect path must **write 0 to Bus Watchdog first** (clear the error), then
  re-arm — otherwise all Goal writes bounce and the runtime looks mysteriously dead.
- The leader arm joints are torque-off, so the watchdog is irrelevant there by definition.

Layered on top: `beforeunload`/`pagehide` best-effort torque-off, torque-off on WebSerial
`disconnect` events, and a Screen Wake Lock during active sessions. Net: the failure story
*improves* over the current stack (which relies on a SIGTERM actually reaching the
entrypoint). P0 validates the watchdog on both servo models with a literal tab-kill.

### 3.6 Cameras — getUserMedia replaces the entire native bridge
- 2× `getUserMedia({video: {deviceId, width: 640, height: 480, frameRate: 30}})`. This deletes
  the MSMF/DSHOW backend dance, usbipd, vhci_hcd Hz-capping, and the `:5557` TCP bridge in one
  move — Chrome's capture pipeline is the thing the GUI was hand-rebuilding.
- **Identical-serial problem persists as a label problem**: both Innomaker cams (`0c45:6367`,
  serial "SN0001") may enumerate with colliding labels, and Chrome's per-origin `deviceId`
  stability for two identical-serial devices is NOT guaranteed across replug/port changes.
  Mitigation = the existing product answer, moved into the SPA: student assigns roles from
  live previews once; persist `deviceId` per rig as a HINT; **re-verify with live previews on
  every session start** (one glance: "ist das die Greifer-Kamera?"). The `.env`
  `gripper`/`scene` role contract (`generate_env_file` validation) becomes a session-config
  contract.
- **Frame timestamps**: `requestVideoFrameCallback` (or `MediaStreamTrackProcessor` +
  `VideoFrame.timestamp` in a Worker) supplies capture timestamps; both cameras are
  normalized to one monotonic clock so the recorder keeps the **15 ms cross-camera pairing**
  semantics (`communicator.py:91-92` — `_CAMERA_SYNC_SLOP_NS = 15 ms`,
  `_CAMERA_SYNC_HISTORY = 8`; pairing walk at `:544-609`) instead of degrading to
  latest-wins. Absolute epoch is not required — pairing is relative; dataset timestamps are
  frame-index/fps-based.
- Today's wire produces JPEG q80 (`EDUBOTICS_CAMERA_JPEG_QUALITY`, `gui/app/constants.py:190`),
  ~30–60 KB per 640×480 frame (`camera_ingest_node.py:63`) — the browser matches those
  parameters for pipeline parity.
- Phone-as-3rd-camera: replaced by a QR-pairing page that streams into the session service
  (server-relayed), no self-signed-cert `:8444` hack needed. Non-goal for round 1 (same
  `camera_topic_list` blockers as today, CLAUDE.md "Phone-camera non-goals").

---

## 4. Cloud session service (replaces the non-realtime half of `physical_ai_server`)

One per-student **session** (WSS, Supabase-JWT-authenticated on the first frame — the exact
pattern the SPA already implements for the Jetson proxy: auth op as first raw WS frame,
`rosConnectionManager.js:127-140`). Hosted initially as one Railway service handling N
sessions with asyncio — **deployed in Railway's EU region** (students are in Germany; the
workflow perception loop round-trips camera frames, so 15–30 ms RTT beats 120 ms). The
documented `--workers 1` single-process constraints apply here too and cap a single
instance's classroom count; WSS sessions are sticky by construction.

### 4.1 Protocol (replaces rosbridge for the student path)
CBOR/msgpack frames over one WSS, three lanes:
- **obs lane (up)**: `{cam_id, capture_ns, jpeg}` at a *mode-dependent* rate (workflow
  perception needs only on-demand/1–5 Hz; recording uploads happen per-episode, not live —
  §4.2) + `{state[6], action[6], t_ns}` at the tick rate during active tasks.
- **cmd lane (down)**: trajectory chunks `[(q6, t_s, v6?), …]`, torque/jog/home directives,
  workflow status, calibration prompts.
- **ctl lane (both)**: heartbeat (≥1 Hz — the SPA's watchdog flips to `timeout` at 3 s,
  `useHeartbeatWatchdog.js:42`), task/phase status (the `TaskStatus`/`WorkflowStatus`
  payloads become JSON — the SPA's ~35-service rosbridge surface shrinks to an RPC map over
  this lane; wire-int enums in `taskCommand.js`/`taskPhases.js` carry over unchanged).

Do **not** adopt LeRobot's own async-inference gRPC server as-is: it deserializes pickles off
the wire and has a published unauthenticated RCE (CVE-2026-25874, CVSS 9.3, still unpatched
as of 2026-04). Same architecture, our own typed protocol.

### 4.2 Recorder → LeRobot packager
Recording must produce byte-faithful LeRobot v3.0 datasets (schema extracted:
`observation.state`/`action` float32×6, per-camera `video` features 480×640×3, fps 30, h264,
`streaming_encoding=True`, concatenated `videos/<key>/chunk-NNN/file-NNN.mp4` layout —
`data_manager.py:906-936`, `lerobot_dataset_wrapper.py:36-61,42`). Two viable splits:

- **(chosen) Server-side packaging, JPEG-frame input.** Browser buffers each episode in OPFS
  (JPEG frames + joint samples + timestamps; `navigator.storage.persist()` requested; a
  crash-journal entry per in-flight episode ≙ today's `.session.json` marker), uploads
  per-episode over HTTPS; the session service replays them through the *existing* ROS-free
  pipeline — `LeRobotDatasetWrapper`, `create_frame`, `_finalize_dataset` (idempotent,
  upload-skipping on failure, `data_manager.py:446`), `_verify_saved_video_files`, and the HF
  upload with the two load-bearing hub-maintenance steps (orphan sweep + v3.0 tag re-point,
  `data_manager.py:1466-1539`) — after a mechanical split of `data_manager.py`'s ROS-msg
  decoding from its pure core (the ROS coupling is the top-level msg imports at `:31,45-47,
  59-60` and `convert_msgs_to_raw_datas` at `:737`; everything below is
  numpy/cv2/huggingface_hub). **JPEG-frame input is deliberate**: today's pipeline input IS
  JPEG (camera bridge → decode → LeRobot h264 encoder), so fidelity and the encode path are
  byte-similar to the shipped product — zero new video variables in the training data.
- (deferred, P4) In-browser **WebCodecs H.264** per-episode encoding to cut upload 5–10×.
  Two honest costs rev 1 glossed over: (a) if the server re-encodes into the LeRobot layout
  it adds a second lossy generation (vs today's one); avoiding that means stream-copying the
  browser's MP4 into the concatenated layout, which demands pinned codec params
  (bt709/limited color, fixed GOP, one resolution) and a golden-dataset **train-quality**
  A/B, not just a byte-diff — the archived study flagged the WebCodecs color-space shift
  explicitly. (b) It re-implements a slice of the video path in JS, exactly the drift class
  Rule §3 exists to kill. Do it only when bandwidth data (P0.4) proves it necessary.
- (rejected) Full browser-side dataset building (WebCodecs + parquet-wasm +
  `@huggingface/hub` direct upload). Technically plausible — the archived study even chose
  it — but re-implementing LeRobot's parquet/stats/episode-metadata layout in JS is a
  silent-drift machine; this plan keeps Python authoritative for ALL dataset artifacts.

Bandwidth reality (upgraded from rev 1): a 60 s 2-camera episode ≈ 160–210 MB of q80 JPEG;
typical 10–20 s episodes ≈ 30–70 MB. Upload happens between episodes (reset window), so it is
never in the teleop path — but **30 students sharing one school uplink is the real
constraint** (30 × 50 MB per round ≈ 1.5 GB per episode wave). Mitigations, in order:
per-classroom upload queue with visible per-student progress (the reset window is
student-paced anyway), episode-length caps, off-peak/deferred upload mode ("Hochladen am
Stundenende"), and only then the P4 in-browser H.264 path. HF token stays **server-side**
(per-student, encrypted in Supabase) — strictly better than today's plaintext host `.env`.

### 4.3 GPU inference (Modal, chunked)
- `inference_manager.py` (already ROS-free: torch/lerobot/numpy + one pure util,
  `inference_manager.py:19-28`) runs inside a Modal ASGI/WebSocket function — **plus a thin
  chunk wrapper**: `predict()` returns ONE action per call (lerobot `predict_action` →
  `select_action` pops the policy's internal action queue, `:214-287`), so the endpoint
  drains the queue / uses lerobot 0.5.1's chunk API to return the whole chunk per
  observation. **Chunk length comes from the model config** — `n_action_steps=15` is an
  EduBotics ACT default injected at training time and user-overridable
  (`training_handler.py:595-612`); never hard-code 15.
- **Chunking absorbs the WAN.** ACT semantics: `chunk_size=100`, EduBotics default
  `n_action_steps=15` → one obs → ~0.5 s of actions at 30 fps. Session flow: browser sends
  one obs → GPU returns the chunk → the browser's trajectory executor paces it at fps
  locally → next obs sent while the current chunk plays (the LeRobot async-inference
  architecture, minus its pickle wire). Preflights port intact and stay server-side:
  camera-contract per tick, fps-vs-`edubotics_model_meta.json`, language-instruction checks,
  `record_inference_mode` refusal (`physical_ai_server.py:1213-1300` semantics); `predict()`
  re-checks the camera contract every tick.
- Modal supports WebSocket endpoints; per-second GPU billing; keep-warm during class hours;
  EU region selection exists (verify plan tier + region latency at implementation).
  Cost order-of-magnitude: one warm L4 (~$0.80/h) time-slices a classroom's inference turns;
  training economics unchanged.

### 4.4 Roboter Studio — the audit's decisive finding
The entire `workflow/` package (interpreter 1538 LOC, motion/trajectory/perception-blocks,
IK 421 LOC, path guard, sim) **imports no rclpy anywhere** — its world is **13 injected
callables** (`physical_ai_server.py:4090-4116`): `publisher`, `ik_factory`,
`perception_factory`, `load_destinations`, `load_calibration`, `emit_status`, `on_finished`,
`get_scene_frame`, `get_gripper_frame`, `get_scene_frame_age`, `get_current_pose_xyz`,
`get_follower_joints`, `load_object_catalog`. The port is a transport rebinding, not a
rewrite:

| Injected callable | Cloud binding |
|---|---|
| `publisher(points)` (sole command path — `_trajectory_publisher`, `physical_ai_server.py:4039-4080`, publishes one JointTrajectory per chunk and caches the last commanded vector for segment chaining) | send trajectory chunk down the cmd lane → browser executor (keep the last-commanded cache server-side — workflow chaining depends on it) |
| `get_follower_joints()` | session-side cache of the browser's state stream |
| `get_scene_frame()` / `get_gripper_frame()` / frame age | latest browser JPEG (decode server-side); workflow perception needs only on-demand/1–5 Hz frames |
| `load_calibration` / `emit_status` / `on_finished` / `load_object_catalog` / `load_destinations` / `get_current_pose_xyz` (FK) / `ik_factory` / `perception_factory` | unchanged (server-local) |

Perception (`pupil_apriltags`, C, thread-locked) and calibration (`cv2.aruco` ChArUco +
`SOLVEPNP_SQPNP` + `solvePnPRefineLM` — absent from stock OpenCV.js, confirmed) **stay
server-side**, consuming browser frames — which also keeps the mandatory per-rig
intrinsics/extrinsic/touch-off flow intact (calibration YAMLs move to per-rig rows in
Supabase). The `/workflow/start` caps port with the engine: `MAX_WORKFLOW_JSON_BYTES`
(256 KiB) and `MAX_HAT_HANDLERS=16`, plus the `_ik_precheck` warning pass. The four
manual-mode services (jog/hand-guide/record/replay) re-bind the same way: torque toggles and
~25 Hz Contract-B sampling execute in the browser runtime; the arbiter invariant
(`on_manual == persistent ∨ transient>0`, `_mode_lock` ordering, exit-generation snapshots,
30 s idle watchdog) lives in the session service. Replay's velocity-floor resegmentation
(`handlers/trajectory.py:138-352` — speed clamp [0.25, 3.0], floor-checked synthetic lead-in,
per-waypoint `point_floor_check`, central-difference velocities) runs server-side; the
browser only ever executes floor-checked chunks — preserving the "untrusted payload can't
drive below the table" property. Defense in depth: the browser executor ALSO enforces the
velocity floor on whatever it is handed (both sides validate; neither trusts the other).

The IK solver (pure NumPy closed-form, 421 LOC, reach annulus `_REACH_MIN/_MAX` =
0.0415/0.2825 m) is the one module worth *additionally* porting to TS later for instant
client-side reachability hints; not required for correctness.

### 4.5 What the SPA needs (mostly deletion)
The audit shows the SPA is already dual-personality (`cloud=1`, Jetson mode, `hardwareOnly`/
`jetsonIncompatible` tab gating, base64-frames-over-WS fallback in `ImageGridCell.js:137-168`).
The browser-only mode is a third personality: rosbridge/web_video_server/`:8769` bridge calls
replaced by (a) the session WSS client and (b) **direct local rendering** — camera `<video>`
elements and joint states come from the same tab, so the `http://host:8080/stream` MJPEG hack
(mixed-content, unauthenticated — `ImageGridCell.js:172`) dies entirely. The UrdfTwin, Blockly
editor, calibration wizard, dock, tutorials are untouched. LeaderToggle's container-restart
choreography (90 s readiness gates) collapses to a local mode-flip in the hardware Worker.

### 4.6 Classroom Jetson — NOT unaffected (rev 1 error)
Today the Inferenz tab re-points rosbridge at `ws://<jetson-ip>:9091`, which works only
because the SPA is served from `http://localhost`. A browser-only SPA is served from a public
HTTPS origin: `ws://` to a LAN IP is **blocked as mixed content**, and Chrome ≥147
additionally permission-gates WebSockets to private addresses (Local Network Access). The
archived study's answer stands: the Jetson needs a **WebRTC DataChannel bridge**
(rosbridge-JSON-over-DC, cloud signaling, JWT + owner-check preserved, TURN fallback for
AP-isolated school Wi-Fi; `LocalNetworkAccessAllowedForUrls` MDM policy as belt-and-braces).
**Round-1 non-goal**: browser-SKU students run inference via the Modal endpoint (§4.3);
Jetson classrooms keep the `.exe` SKU. Trigger to revisit: a browser-SKU classroom that owns
a Jetson and wants local inference latency/cost.

---

## 5. Chrome requirements & school deployability

- **Chrome/Edge/Chromium ≥ 89 on Win/mac/Linux/ChromeOS.** WebSerial does not exist in
  Firefox/Safari — this is the product's stated "correct Chrome browser" constraint. ChromeOS
  support means **managed school Chromebooks become first-class student machines** (they could
  never run WSL2) — arguably the biggest win of the whole plan. (Android Chrome has no Web
  Serial — tablets are out.)
- **Permission-free fleet rollout**: `SerialAllowUsbDevicesForUrls` (VID `0x2F5D` scoped to our
  origin) + camera-allow policies via Google Admin / group policy. Unmanaged home PCs see one
  serial-port picker + one camera prompt, once per origin.
- **Local Network Access is a non-issue for this architecture** (and a landmine for any
  LAN-server alternative): Chrome 142 gated fetch/XHR to private/loopback addresses behind
  the LNA permission; **Chrome 147 (stable since 2026-04) extended it to WebSockets**.
  Browser→cloud is public; browser→USB is WebSerial. Nothing touches a private IP. (This is
  also the reason §4.6 exists, and a fresh argument *against* resurrecting classroom-LAN
  servers for the student UI.)
- One operational footgun inherited from the hybrid fleet: a usbipd-**attached** arm is
  invisible to Windows and therefore to WebSerial (archived spike README) — mixed
  `.exe`/browser classrooms need the "Umgebung stoppen / detach" step documented.
- Requires genuinely working school internet for cloud phases (recording upload between
  episodes, inference chunks). Classrooms without it stay on the `.exe` SKU (§9).

---

## 6. Security posture (net improvement)

| Today | Browser-only |
|---|---|
| rosbridge `:9090` unauthenticated (loopback-bound as mitigation) — anyone reaching it drives the arm | No rosbridge. WSS with Supabase JWT first-frame; per-user session isolation server-side (existing IDOR discipline / ownership asserts extend to session + calibration rows) |
| HF token in plaintext host `.env` | Server-side encrypted per-user token; browser never sees it |
| Phone cam receiver `0.0.0.0:8444` unauthenticated | Server-relayed, authenticated |
| Arm safety = entrypoint trap (limp on death) | Same FSM + firmware Bus Watchdog backstop (holds in place on death, §3.5) |
| `:8769` GUI bridge origin-checks | Gone |

New surface to defend: the session service is an arm-command channel — JWT + per-session
binding + the server-side floor/velocity validation on every outbound chunk (already the
`resegment_trajectory` posture) are mandatory from day one, and the browser executor
re-validates independently (§4.4).

---

## 7. What maps to the Six Rules

1. **German UI / English code** — unchanged; the new runtime's student-facing strings are German.
2. **Hardware safety in xacro+entrypoint** — the xacro register values and entrypoint semantics
   *are* the browser runtime's init table and boot FSM (§3.2–3.5). Same floor: gripper current
   limits, Shutdown 21, position limits, Drive Mode 4 profiles. The collision e-stop port
   needs explicit sign-off.
3. **Image == repo** — becomes "runtime == repo": SPA buildId gating (`useVersionCheck` already
   exists), the session service is COPY-wholesale-equivalent by being deployed from repo HEAD via
   the same CI identity-gated pattern (`/health` commit == sha). The `@edubotics/dxl-web`
   contract constants (§3.2–3.4) get an enum-parity-style CI cross-check against the xacros
   and `collision_detector.py` so the two implementations cannot drift silently.
4. **Service-role / ownership asserts** — session service adopts the same assert-or-IDOR rules
   for session, calibration, trajectory, episode-upload resources.
5. **LeRobot pin lockstep** — the packager and Modal inference images join the `0.5.1` pin list
   (one more site in the one-PR-multi-site bump).
6. **CI/CD** — two new deploy surfaces (session service, Modal inference endpoint) slot into the
   existing golden order; both get identity health gates.

---

## 8. Phased migration (each phase independently shippable)

**P0 — spikes (1–2 weeks, gate everything on these).** Rebuild the spike harness (the old
`tools/browser-spike/` is gone from the tree; its README/gates are archived in
`docs/CLAUDE-CHANGELOG.md`) and re-run with the archived thresholds:
1. TS Protocol 2.0 + **dual-board** bench: the §3.3 transaction schedule at 100 Hz in a
   Worker on a low-end laptop + a managed Chromebook; jitter histogram. **Gate (archived
   G0): mirror ≥60 Hz sustained, target 100, p99 < 1 cycle + teleop feel sign-off.**
   Below 60 Hz ⇒ STOP and re-architect (the detector's tick-counted debounce and the
   recorded-dataset equivalence both assume ~100 Hz).
2. Bus Watchdog validation on XL430 + XL330 (tab-kill → motion halt; clear-then-re-arm
   reconnect path; §3.5).
3. getUserMedia dual-Innomaker role stability across replug/reboot + capture-timestamp
   quality vs the 15 ms pairing budget (`requestVideoFrameCallback` vs
   `MediaStreamTrackProcessor`).
4. OPFS episode write/upload throughput on school-grade hardware + a realistic
   30-clients-one-uplink upload-wave simulation (drives the §4.2 queue design and whether
   P4's H.264 gets pulled forward).
5. Tab-lifecycle audit: freeze/discard behavior with an open serial port + live capture,
   Wake Lock effectiveness, worker timer stability when the window is occluded.

**P1 — "Aufnahme im Browser" (replaces the .exe for record→train):** hardware runtime (teleop,
boot sync, collision e-stop — after Rule-§2 sign-off), OPFS episode capture + crash journal,
session-service packager + HF upload (golden-dataset byte-diff in CI against a reference
`.exe` recording), existing cloud training untouched. Success = a student records, trains,
and sees the dataset in Daten with zero installs.

**P2 — cloud inference:** Modal WSS endpoint + chunk wrapper + chunked local execution +
preflight ports (§4.3).

**P3 — Roboter Studio remote:** workflow engine in the session service over the 13-callable
rebinding; calibration wizard server-side; manual-mode services; sim path (pure server,
trivial).

**P4 — long tail:** `/dataset/edit` (subprocess re-encode) moves server-side next to the
packager where the data already lives; optional in-browser H.264 episode compression (only
if P0.4 data demands it, with the §4.2 codec-pinning + train-quality A/B); phone camera;
Jetson WebRTC bridge (§4.6) if a real classroom triggers it; retire the installer for
browser-capable classrooms.

Per the repo's own convention, each phase that touches >1 layer gets its dated one-pager in
`docs/plans/` before code.

---

## 9. Honest risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Dual-board 100 Hz Worker loop on weakest school PCs (single-board reads proved; the mirror is not) | HIGH (P0 gate) | Worker loop + adaptive tick; archived gate G0 (≥60 Hz floor, feel sign-off); teleop degrades gracefully (recording samples at 30 Hz regardless); detector debounce recomputed from the real tick rate |
| Tab lifecycle (sleep, tab discard, accidental close) mid-torque | HIGH | Bus Watchdog (firmware; clear-then-re-arm on reconnect), `pagehide` torque-off, Wake Lock during active sessions, session-service dead-man alarm; P0.5 measures Chrome's actual freeze/discard behavior with open port + live capture |
| School upstream bandwidth for episode upload (30 students share one uplink) | HIGH (upgraded) | Upload in reset windows via a per-classroom queue with visible progress; episode caps; deferred "Stundenende" mode; P4 H.264 only if data demands it |
| Dataset byte-parity vs current recorder | MED | Server-side packager reuses the exact Python pipeline on the same JPEG-frame input as today; golden-dataset diff test in CI |
| Two-cam identical-serial role swap (deviceId stability not guaranteed for identical-serial devices) | MED | One-time role assignment + persisted deviceIds as hints + mandatory per-session live-preview confirm |
| Chrome-only exclusion (Firefox/Safari households, Android tablets) | ACCEPTED | Stated product constraint; `.exe` SKU remains |
| Session service = remote arm-command channel | MED | JWT binding, server-side chunk validation + independent browser-side floor re-validation, rate limits, kill-switch parity with `EDUBOTICS_COLLISION_ENABLED` |
| Contract drift between `@edubotics/dxl-web` and the Python/xacro truth | MED | Enum-parity-style CI cross-check of shared constants (§7.3); golden-packet unit tests; upstream `dynamixel_hardware_interface` is unpinned `main` — bench against firmware, not code-reading |
| Jetson classrooms on the browser SKU | ACCEPTED (round 1) | `.exe` SKU covers them; WebRTC DataChannel bridge specced (§4.6) with a trigger condition |
| Offline classrooms | ACCEPTED | Browser path requires internet by construction; `.exe` remains the offline SKU |
| Rule-§2 scope (e-stop relocation) | PROCESS | Explicit user sign-off before P1 implementation |

---

## 10. Sources

Codebase: all `file:line` cites re-verified against repo HEAD on 2026-07-13 (branch
`claude/browser-deploy-plan-review-turgzy`); the parked 2026-06-07 browser-migration study +
spike results are archived in `docs/CLAUDE-CHANGELOG.md:143-167`.

Web:
- Web Serial enterprise policies: [SerialAllowUsbDevicesForUrls](https://chromeenterprise.google/intl/en_us/policies/serial-allow-usb-devices-for-urls/), [SerialAllowAllPortsForUrls](https://chromeenterprise.google/intl/en_uk/policies/serial-allow-all-ports-for-urls/)
- Browser arm-control prior art (Feetech-only, ~10–30 Hz): [LeRobot.js (HF blog)](https://huggingface.co/blog/NERDDISCO/lerobotjs), [lerobot GitHub](https://github.com/huggingface/lerobot)
- OpenRB-150 `usb_to_dynamixel` factory firmware: [ROBOTIS e-Manual OpenRB-150](https://emanual.robotis.com/docs/en/parts/controller/openrb-150/), [DYNAMIXEL Protocol 2.0](https://emanual.robotis.com/docs/en/dxl/protocol2/)
- Bus Watchdog semantics (addr 98, stop-in-place torque-on, Goal writes rejected until cleared): [XL330-M288 e-Manual](https://emanual.robotis.com/docs/en/dxl/x/xl330-m288/), [XL430-W250 e-Manual](https://emanual.robotis.com/docs/en/dxl/x/xl430-w250/)
- Chrome Local Network Access (142 fetch/XHR, 147 WebSocket — verified 2026-07): [Chrome dev blog](https://developer.chrome.com/blog/local-network-access), [Chrome 147 LNA + WebSockets](https://myconnectionserver.visualware.com/support/v11/userguide/chrome-lna-websocket), [WICG explainer](https://github.com/WICG/local-network-access/blob/main/explainer.md)
- HF Hub uploads from JS/browser (P4 reference only): [@huggingface/hub (npm)](https://www.npmjs.com/package/@huggingface/hub)
- LeRobot async inference (architecture precedent + CVE warning): [async inference docs](https://huggingface.co/docs/lerobot/async), [CVE-2026-25874 write-up](https://chocapikk.com/posts/2026/lerobot-pickle-rce/), [The Hacker News coverage](https://thehackernews.com/2026/04/critical-cve-2026-25874-leaves-hugging.html)
- Modal web/WebSocket endpoints, cold start, pricing: [Modal web functions](https://modal.com/docs/guide/webhooks), [Modal cold start](https://modal.com/docs/guide/cold-start)
