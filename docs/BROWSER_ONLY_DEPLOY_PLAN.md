# EduBotics Browser-Only Deployment — Deep Architecture Plan (2026-07-12)

Status: PROPOSAL (research-grade, code-audited). Successor question to the Orange-Pi/edge plans:
**can we deploy EduBotics "somewhere else" — containers and all — so a student needs NOTHING
on their PC except a current Chromium browser?** No `.exe`, no WSL2, no Docker Desktop-less
Docker, no usbipd, no Pi/edge box on the desk. The arms and cameras stay on the student's desk
and plug into the student's PC over USB, because Physical AI without physical hardware is not
the product.

This plan was derived from a fresh full-source audit (file:line cites throughout) plus web
research (URL cites at the bottom). It deliberately does NOT build on the earlier
`ORANGE_PI_DEPLOY_PLAN.md` / `INSTALL_SPLIT_PLAN.md` documents.

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
   USB-CDC ↔ DYNAMIXEL Protocol 2.0 bridge at up to 1 Mbps. The C++ `dynamixel_hardware_interface`
   is *already* just a PC-side Protocol 2.0 speaker; Chrome's Web Serial API can be the same
   speaker. Our own earlier ground-truth spike benched WebSerial reads off an OpenRB-150 at
   503–713 Hz (100 Hz solid).
2. **Every EduBotics software layer above the servo bus is already network-shaped.** Teleop is
   a local leader→follower position mirror (no network in the loop at all). Everything else on
   the command rail is *trajectories*, not per-tick setpoints (`/leader/joint_trajectory` carries
   single-point teleop/inference msgs and 50-point quintic moves —
   `open_manipulator/ros2_controller/om_joint_trajectory_command_broadcaster/src/joint_trajectory_command_broadcaster.cpp:283-342`,
   `entrypoint_omx.sh:319-331`, `data_converter.py:206-245`). Chunked commands tolerate WAN latency.
3. **The Python that matters is already ROS-free.** `workflow/` (the whole Roboter Studio
   runtime) imports zero rclpy — its hardware boundary is ~14 injected callables
   (`physical_ai_server.py:4090-4116`). `inference_manager.py` imports only torch/lerobot/numpy
   (`inference_manager.py:19-29`). The LeRobot dataset wrapper is pure
   (`lerobot_dataset_wrapper.py`). The cloud API is plain FastAPI+JWT. These lift into cloud
   services with small shims, not rewrites.

The one genuinely new artifact is a **browser hardware runtime** (~a TypeScript re-implementation
of the Dynamixel layer + collision e-stop + trajectory pacing, running in a dedicated Worker).
Its full contract is extracted in §3 — every register, constant, and FSM state it must replicate
is enumerated and cited.

---

## 1. Why NOT the two "obvious" alternatives

### 1a. Lift the containers to the cloud unchanged, tunnel USB
Rejected. The ros2_control loop performs a **synchronous sync-read + sync-write serial
transaction every 10 ms** (update_rate 100, `omx_f_hardware_controller_manager.yaml:4`).
Tunneling the serial byte stream over a WAN puts 20–80 ms of RTT *inside* each 10 ms cycle —
the loop collapses, the JTC watchdogs fire, and the boot-sync/verify phases
(`entrypoint_omx.sh:342-408`) time out. USB/IP over WAN also has no browser story. The
hard-realtime endpoint must terminate on the same machine the USB cable plugs into — and the
only runtime a zero-install student PC offers is the browser.

### 1b. Keep a per-desk edge box (Pi/Orange Pi/Jetson)
Explicitly out of scope for this plan — the whole point is removing that box. (The classroom
Jetson remains a separate, already-shipped inference target and is unaffected.)

### 1c. Why the browser CAN be the realtime endpoint
- Web Serial API is stable in Chrome/Edge ≥ 89 and on **ChromeOS** (managed school Chromebooks
  work), HTTPS + one user gesture per port; **schools can pre-grant access with the
  `SerialAllowUsbDevicesForUrls` / `SerialAllowAllPortsForUrls` enterprise policies keyed to our
  origin + VID `0x2F5D`** — zero prompts on managed devices.
- Prior art: HuggingFace-ecosystem **lerobot.js** already calibrates and teleoperates SO-100/SO-101
  arms (Feetech STS3215 bus servos) entirely over WebSerial in the browser.
- The 100 Hz loop lives in a **dedicated Web Worker** — workers are exempt from the background-tab
  `setTimeout` clamping that would kill a main-thread loop; the leader→follower mirror has zero
  network dependency, so Wi-Fi jitter cannot make the arm stutter during teleop.

Firefox/Safari have no WebSerial — hence the product requirement the user already accepts:
**"the correct Chrome browser"** (Chrome/Edge/Chromium, or a managed Chromebook).

---

## 2. Target architecture

```
 Student desk                                   Cloud (all existing infra kept)
┌──────────────────────────────┐
│  Chrome tab (HTTPS SPA)      │   WSS (JWT)   ┌──────────────────────────────────┐
│ ┌──────────────────────────┐ │◄─────────────►│ Session service (CPU, FastAPI)   │
│ │ Hardware Worker          │ │  obs 2-30 Hz  │  · workflow/ engine (as-is)      │
│ │  · WebSerial ×2 boards   │ │  cmd chunks   │  · perception+calibration (cv2)  │
│ │  · P2.0 sync R/W @100Hz  │ │               │  · recorder→LeRobot packager     │
│ │  · teleop mirror (local) │ │               │  · heartbeat/status              │
│ │  · collision e-stop FSM  │ │               └───────────┬──────────────────────┘
│ │  · quintic/JTC pacing    │ │                           │ spawns/queries
│ │  · Bus Watchdog arming   │ │               ┌───────────▼──────────────────────┐
│ └──────────────────────────┘ │               │ Modal GPU endpoint (L4, WSS)     │
│  getUserMedia ×2 cameras     │               │  · loads student policy           │
│  (timestamps → 15 ms sync)   │               │  · obs → 15-action chunk          │
│  OPFS episode buffer         │               └──────────────────────────────────┘
└──────────────────────────────┘               cloud_training_api / Supabase /
      USB: 2× OpenRB-150 (VID 2F5D)            Modal training / HF Hub: UNCHANGED
```

**What stays byte-identical:** `cloud_training_api` (all routes are already browser-callable
JSON+JWT — `auth.py:10-48`), Supabase (auth/RLS/realtime), Modal training
(`modal_training/`), the HF dataset/model flow, the teacher web deploy, the classroom Jetson
path, and the existing `.exe` product (which remains the offline/fallback SKU — see §9).

**What is deleted from the student PC:** installer, WSL2 rootfs, Docker, usbipd, the tkinter
GUI, the camera MSMF bridge, the WebView2 child process, the `.env` machinery, per-machine
`ROS_DOMAIN_ID` (no DDS graph exists anymore in this path).

---

## 3. The browser hardware runtime (the new artifact)

This is the WebSerial re-implementation of what `dynamixel_hardware_interface` + the xacro
configs + the entrypoint + `collision_monitor` do today. Everything below is the extracted,
citable contract.

### 3.1 Bus layer
- **Protocol 2.0 at 1,000,000 baud** (`identify_arm.py:14-15`, `omx_f.ros2_control.xacro:20`).
  baudRate is nominal for native-USB CDC (SAMD51), but must still be passed through so the
  OpenRB-150 bridge clocks its DXL-side UART correctly.
- Port discovery: `navigator.serial.requestPort({filters:[{usbVendorId: 0x2F5D}]})`
  (OpenRB-150 VID `2f5d`, follower PIDs `0103`/`2202` — `jetson_agent/udev/99-edubotics-robotis.rules:17-18`).
  Role identification = the existing ping-sweep algorithm: ping IDs 1–6 vs 11–16, majority wins
  (`identify_arm.py:21-50`). Persist `SerialPort.getInfo()` + role for silent re-attach via
  `navigator.serial.getPorts()`.
- Packet layer to port to TypeScript: P2.0 framing (0xFFFFFD header, byte-stuffing), CRC-16,
  Ping (0x01), Read (0x02), Write (0x03), **Sync Read (0x82) / Sync Write (0x83)**, Reboot (0x08).
  ~600 LOC, table-driven, fully unit-testable against golden packets.

### 3.2 Servo init writes (follower IDs 11–16, leader IDs 1–6)
Replicates the xacro `<param>` blocks exactly (`omx_f.ros2_control.xacro:75-202`,
`omx_l.ros2_control.xacro:72-145`):

| ID | Model | Op Mode | Limits / currents | Per-cycle reads | Writes |
|---|---|---|---|---|---|
| 11 | XL430-W250 | 4 (ext. pos) | — | PresPos, PresVel, **PresLoad**, HWErr | GoalPos |
| 12 | XL430-W250 | 3 | pos 830–3129 | same | GoalPos |
| 13 | XL430-W250 | 3 | pos 1024–3140 | same | GoalPos |
| 14/15 | XL330-M288 | 3 | pos 0–4095 | PresPos, PresVel, **PresCurrent**, HWErr | GoalPos |
| 16 | XL330 (gripper) | **5 (current-pos)** | CurrLimit **350**, GoalCurrent **350**, Shutdown **21** | PresPos, PresVel | GoalPos |
| 1–5 (leader) | mixed | 0, **Torque OFF** (limp) | — | PresPos, PresVel | — |
| 6 (leader trigger) | XL330 | 5 | CurrLimit **300** | PresPos | GoalPos = −0.7 held @50 Hz |

Common firmware params: Return Delay 0, Pos P/I/D = 1000/0/1000, Profile Vel 50 / Acc 25
(`omx_f.ros2_control.xacro:94-101`). **Model-awareness is mandatory** — asking an XL430 for
Present Current aborts init on real hardware (documented scar, `omx_f.ros2_control.xacro:84-90`);
the effort source per joint is Load (÷1000) for J1–3 and Current (÷1750) for J4–5, exactly as
`collision_detector.py:47-60` encodes.

### 3.3 The 100 Hz loop (dedicated Worker)
Per tick: Sync Read follower {PresPos, PresVel, effort-signal, HWErr} + leader {PresPos, PresVel};
apply the active command source; Sync Write follower Goal Positions. Command sources, in
priority order (this replaces the ros2_control controller graph + the
`/arm_controller/joint_trajectory → /leader/joint_trajectory` remap,
`omx_f_follower_ai.launch.py:161`):

1. **Collision FSM override** (freeze / relax-in-place / quintic home / resync) — §3.4.
2. **Trajectory executor** — plays server-sent or locally generated `(q, t_s[, v])` chunks with
   quintic interpolation; replaces the stock JointTrajectoryController. Quintic profile is the
   entrypoint's exact math: `s = 10t³ − 15t⁴ + 6t⁵` (`entrypoint_omx.sh:319-331`,
   duplicated `collision_monitor.py:712-714`). Velocity floor 2.88 rad/s peak with quintic
   factor 15/8 ports from `trajectory_builder._velocity_safe_duration` (`trajectory_builder.py`).
3. **Teleop mirror** — leader PresPos → follower GoalPos with the gripper **sign flip**
   (`reverse_joints: [gripper_joint_1]`, `omx_l_leader_ai_hardware_controller_manager.yaml:33-36`).
   Entirely local; the recorded "action" (leader pose) and "state" (follower readback) are
   sampled from this loop at the dataset fps.
4. Idle (hold).

Boot sequence ports from `entrypoint_omx.sh`: leader up → follower up → **3 s quintic sync
leader-pose→follower** → verify (0.30 rad tol, arm joints only, ≥50 % delta traversed, soft-fail
warn — `entrypoint_omx.sh:342-437`) → cameras. Follower-only mode homes to
`[0, −π/2, π/2, 0, 0, 0.8]` (`entrypoint_omx.sh:461` — the same pose constant that lives in 4
lockstep sites today).

### 3.4 Collision e-stop (full port — pure logic already)
`collision_detector.py` is deliberately pure/unit-tested and ports 1:1:
- Trip: `effort_fraction ≥ threshold AND |vel| ≤ 0.05 rad/s` for **15 consecutive 10 ms ticks**
  (150 ms debounce), thresholds `(0.30, 0.65, 0.40, 0.30, 0.30)` J1–J5; firmware Overload bit
  `0x20` in HW Error Status = immediate trip (`collision_detector.py:106-118, 192-218`).
  Signed-16 unwrap of unsigned-published values (`:94-97`). Gripper excluded.
- FSM (`collision_monitor.py`): freeze teleop (the browser simply stops the mirror — no
  `/collision_flag` topic needed, the C++ broadcaster's skip-publish becomes an `if`), relax to
  measured pose after 0.15 s (≤3 sends), student-paced two-step recovery: quintic home 2.5 s
  verified glide → resume STRICTLY gated on leader proximity ≤ 0.30 rad → 3 s quintic resync.
  Settle window 0.5 s after re-torque; leader-alive gate (2 s freshness) becomes trivial (the
  browser knows port state directly). All constants at `collision_monitor.py:138-170`.
- Gating semantics preserved: OFF during inference/workflow/manual (`collision_detector.py:184-186`).
- Reboot of Overload-latched servos via P2.0 Reboot instruction (today:
  `/dynamixel_hardware_interface/reboot_dxl`, `collision_monitor.py:122-123`).

This is Rule-§2-sensitive: the port changes *where* the sanctioned e-stop runs, not what it
does — same constants, same student-paced recovery, same discard-episode semantics. **Explicit
user sign-off required before implementation**, per CLAUDE.md.

### 3.5 Safety when the browser dies (better than today)
Today's guarantee is the entrypoint's `trap`-based torque disable (`entrypoint_omx.sh:62`).
A crashed tab can't run a trap — but the servos offer something stronger: the X-series
**Bus Watchdog register** halts motion when the bus goes silent for a configured interval.
The runtime arms it at init; a killed tab/OS-closed CDC port then stops the arm at firmware
level within the watchdog window. Additionally: `beforeunload`/`pagehide` best-effort torque-off,
and torque-off on WebSerial `disconnect` events. Net: the failure story *improves* over the
current stack (which relies on a SIGTERM reaching the entrypoint).

### 3.6 Cameras — getUserMedia replaces the entire native bridge
- 2× `getUserMedia({video: {deviceId, width: 640, height: 480, frameRate: 30}})`. This deletes
  the MSMF/DSHOW backend dance, usbipd, vhci_hcd Hz-capping, and the `:5557` TCP bridge in one
  move — Chrome's capture pipeline is the thing the GUI was hand-rebuilding.
- **Identical-serial problem persists as a label problem**: both Innomaker cams (`0c45:6367`,
  serial "SN0001") may enumerate with colliding labels. Mitigation = the existing product answer,
  moved into the SPA: student assigns roles from live previews once; persist `deviceId` per rig;
  re-verify on session start (deviceIds are origin-stable). The `.env` `gripper`/`scene` role
  contract (`generate_env_file` validation) becomes a session-config contract.
- **Frame timestamps**: `requestVideoFrameCallback` supplies capture timestamps; these ride with
  every frame so the recorder keeps the **15 ms cross-camera pairing** semantics
  (`communicator.py:91-92, 544-609` — slop 15 ms, ring depth 8) instead of degrading to
  latest-wins.
- Phone-as-3rd-camera: replaced by a QR-pairing page that streams into the session service
  (server-relayed), no self-signed-cert `:8444` hack needed. Non-goal for round 1.

---

## 4. Cloud session service (replaces the non-realtime half of `physical_ai_server`)

One per-student **session** (WSS, Supabase-JWT-authenticated on the first frame — the exact
pattern the SPA already implements for the Jetson proxy: auth op as first raw WS frame,
`rosConnectionManager.js:118-142`). Hosted initially as one Railway service handling N sessions
with asyncio (same platform as `cloud_training_api`; note its documented `--workers 1`
single-process constraints apply here too and cap a single instance's classroom count).

### 4.1 Protocol (replaces rosbridge for the student path)
CBOR/msgpack frames over one WSS, three lanes:
- **obs lane (up)**: `{cam_id, capture_ns, jpeg}` at a *mode-dependent* rate + `{state[6], action[6], t_ns}`
  at the tick rate. JPEG ~30–80 KB at q80 (`camera_ingest_node.py:64`, `constants.py:190`).
- **cmd lane (down)**: trajectory chunks `[(q6, t_s, v6?), …]`, torque/jog/home directives,
  workflow status, calibration prompts.
- **ctl lane (both)**: heartbeat (≥1 Hz — the SPA's watchdog flips to `timeout` at 3 s,
  `useHeartbeatWatchdog.js:40-44`), task/phase status (the `TaskStatus`/`WorkflowStatus`
  payloads become JSON — the SPA's 38-service rosbridge surface shrinks to an RPC map over
  this lane; wire-int enums in `taskCommand.js`/`taskPhases.js` carry over unchanged).

Do **not** adopt LeRobot's own async-inference gRPC server as-is: it deserializes pickles off
the wire and has a published RCE (CVE-2026-25874). Same architecture, our own typed protocol.

### 4.2 Recorder → LeRobot packager
Recording must produce byte-faithful LeRobot v3.0 datasets (schema extracted:
`observation.state`/`action` float32×6, per-camera `video` features 480×640×3, fps 30, h264,
`streaming_encoding=True`, concatenated `videos/<key>/chunk-NNN/file-NNN.mp4` layout —
`data_manager.py:906-936`, `lerobot_dataset_wrapper.py:36-61`). Two viable splits:

- **(chosen) Server-side packaging.** Browser buffers each episode in OPFS (JPEG frames +
  joint samples + timestamps), uploads per-episode over the WSS/HTTPS; the session service
  replays them through the *existing* ROS-free pipeline — `LeRobotDatasetWrapper`,
  `create_frame`, `_finalize_dataset`, the HF upload with the two load-bearing hub-maintenance
  steps (orphan sweep + v3.0 tag re-point, `data_manager.py:1465-1539`) — after a mechanical
  split of `data_manager.py`'s ROS-msg decoding from its pure core (the audit confirms the
  core is ROS-free; only `convert_msgs_to_raw_datas` and the top-level msg imports bind it,
  `data_manager.py:31-60`). Upload happens between episodes (reset window), so live upstream
  bandwidth is NOT in the teleop path.
- (rejected for r1) Browser-side dataset building via WebCodecs H264 + parquet-wasm +
  `@huggingface/hub` direct upload. Technically plausible (HF's JS client uploads Blobs with
  LFS from the browser), but re-implementing LeRobot's parquet/stats/episode-metadata layout in
  JS is exactly the class of silent-drift risk Rule §3 exists to kill. Keep Python authoritative.

Bandwidth math (per §e of the pipeline audit): raw JPEG obs streaming would be ~24 Mbit/s per
rig — unacceptable live; per-episode OPFS upload amortizes it (a 60 s episode ≈ 180 MB raw JPEG;
optionally WebCodecs-H264 the episode in the browser before upload → ~15–30 MB). HF token stays
**server-side** (per-student, encrypted in Supabase) — strictly better than today's plaintext
host `.env`.

### 4.3 GPU inference (Modal, chunked)
- `inference_manager.py` runs as-is in a Modal ASGI/WebSocket function (it is already ROS-free;
  `predict()` takes `{cam→ndarray}, state[6]` and returns `action[6]` — `inference_manager.py:214-287`).
- **Chunking absorbs the WAN.** ACT is trained with `chunk_size=100, n_action_steps=15`
  (`training_handler.py:602-620`) — the policy semantically emits 15-action, 0.5 s chunks. The
  session flow: browser sends one obs → GPU returns 15 actions → browser's trajectory executor
  paces them at fps locally → next obs sent while the current chunk plays (the
  LeRobot async-inference architecture, sub-100 ms RTT reported, minus its wire format).
  Preflights port intact: camera-contract per tick, fps-vs-`edubotics_model_meta.json`,
  language-instruction checks (`physical_ai_server.py:1213-1300` semantics).
- Modal supports WebSocket endpoints; per-second GPU billing; keep-warm during class hours.
  Cost order-of-magnitude: one warm L4 (~$0.80/h) time-slices a classroom's inference turns;
  training economics unchanged.

### 4.4 Roboter Studio — the audit's decisive finding
The entire `workflow/` package (interpreter 1538 LOC, motion/trajectory/perception-blocks,
IK, path guard, sim) **imports no ROS anywhere** — its world is ~14 injected callables
(`physical_ai_server.py:4090-4116`). The port is a transport rebinding, not a rewrite:

| Injected callable | Cloud binding |
|---|---|
| `publisher(points)` (sole command path, `physical_ai_server.py:4039-4080`) | send trajectory chunk down the cmd lane → browser executor |
| `get_follower_joints()` | session-side cache of the browser's state stream |
| `get_scene_frame()` / frame age | latest browser JPEG (decode server-side); workflow perception needs only on-demand/1–5 Hz frames |
| `load_calibration()` / `emit_status` / `on_finished` / catalog / FK pose | unchanged (server-local) |

Perception (`pupil_apriltags`, C) and calibration (`cv2.aruco` ChArUco + `SOLVEPNP_SQPNP` —
absent from stock OpenCV.js, confirmed) **stay server-side**, consuming browser frames — which
also keeps the mandatory per-rig intrinsics/extrinsic/touch-off flow intact (calibration YAMLs
move to per-rig rows in Supabase). The four manual-mode services (jog/hand-guide/record/replay)
re-bind the same way: torque toggles and ~25 Hz Contract-B sampling execute in the browser
runtime; the arbiter invariant (`on_manual == persistent ∨ transient>0`, `_mode_lock` ordering)
lives in the session service. Replay's velocity-floor resegmentation
(`handlers/trajectory.py:138-352`) runs server-side; the browser only ever executes floor-checked
chunks — preserving the "untrusted payload can't drive below the table" property.

The IK solver (pure NumPy closed-form, 421 LOC) is the one module worth *additionally* porting
to TS later for instant client-side reachability hints; not required for correctness.

### 4.5 What the SPA needs (mostly deletion)
The audit shows the SPA is already dual-personality (`cloud=1`, Jetson mode, `hardwareOnly`/
`jetsonIncompatible` tab gating, base64-frames-over-WS fallback in `ImageGridCell.js:137-168`).
The browser-only mode is a third personality: rosbridge/web_video_server/`:8769` bridge calls
replaced by (a) the session WSS client and (b) **direct local rendering** — camera `<video>`
elements and joint states come from the same tab, so the `http://host:8080/stream` MJPEG hack
(mixed-content, unauthenticated — `ImageGridCell.js:172`) and its Jetson half-workaround die
entirely. The UrdfTwin, Blockly editor, calibration wizard, dock, tutorials are untouched.
LeaderToggle's container-restart choreography (90 s readiness gates) collapses to a local
mode-flip in the hardware Worker.

---

## 5. Chrome requirements & school deployability

- **Chrome/Edge/Chromium ≥ 89 on Win/mac/Linux/ChromeOS.** WebSerial does not exist in
  Firefox/Safari — this is the product's stated "correct Chrome browser" constraint. ChromeOS
  support means **managed school Chromebooks become first-class student machines** (they could
  never run WSL2) — arguably the biggest win of the whole plan.
- **Permission-free fleet rollout**: `SerialAllowUsbDevicesForUrls` (VID `0x2F5D` scoped to our
  origin) + camera-allow policies via Google Admin / group policy. Unmanaged home PCs see one
  serial-port picker + one camera prompt, once per origin.
- **Local Network Access is a non-issue for this architecture** (and a landmine for any
  LAN-server alternative): since Chrome 142/147, a public HTTPS site fetching or web-socketing
  to private/loopback addresses triggers the LNA permission gate. Browser→cloud is public;
  browser→USB is WebSerial. Nothing touches a private IP. (This is also a fresh argument
  *against* resurrecting classroom-LAN servers for the student UI.)
- Requires genuinely working school internet for cloud phases (recording upload between
  episodes, inference chunks). Classrooms without it stay on the `.exe` SKU (§9).

---

## 6. Security posture (net improvement)

| Today | Browser-only |
|---|---|
| rosbridge `:9090` unauthenticated (loopback-bound as mitigation) — anyone reaching it drives the arm | No rosbridge. WSS with Supabase JWT first-frame; per-user session isolation server-side (existing IDOR discipline / ownership asserts extend to session + calibration rows) |
| HF token in plaintext host `.env` | Server-side encrypted per-user token; browser never sees it |
| Phone cam receiver `0.0.0.0:8444` unauthenticated | Server-relayed, authenticated |
| Arm safety = entrypoint trap | Same FSM + firmware Bus Watchdog backstop |
| `:8769` GUI bridge origin-checks | Gone |

New surface to defend: the session service is an arm-command channel — JWT + per-session
binding + the server-side floor/velocity validation on every outbound chunk (already the
`resegment_trajectory` posture) are mandatory from day one.

---

## 7. What maps to the Six Rules

1. **German UI / English code** — unchanged; the new runtime's student-facing strings are German.
2. **Hardware safety in xacro+entrypoint** — the xacro register values and entrypoint semantics
   *are* the browser runtime's init table and boot FSM (§3.2–3.5). Same floor: gripper current
   limits, Shutdown 21, position limits. The collision e-stop port needs explicit sign-off.
3. **Image == repo** — becomes "runtime == repo": SPA buildId gating (`useVersionCheck` already
   exists), the session service is COPY-wholesale-equivalent by being deployed from repo HEAD via
   the same CI identity-gated pattern (`/health` commit == sha).
4. **Service-role / ownership asserts** — session service adopts the same assert-or-IDOR rules
   for session, calibration, trajectory, episode-upload resources.
5. **LeRobot pin lockstep** — the packager and Modal inference images join the `0.5.1` pin list
   (one more site in the one-PR-multi-site bump).
6. **CI/CD** — two new deploy surfaces (session service, Modal inference endpoint) slot into the
   existing golden order; both get identity health gates.

---

## 8. Phased migration (each phase independently shippable)

**P0 — spikes (1–2 weeks, gate everything on these):**
1. TS Protocol 2.0 + dual-board bench: 100 Hz leader-read/follower-write + effort reads in a
   Worker on a low-end laptop + a managed Chromebook; measure jitter histogram. (Prior single-board
   spike: 503–713 Hz.)
2. Bus Watchdog validation on XL430/XL330 (tab-kill → motion halt).
3. getUserMedia dual-Innomaker role stability + `requestVideoFrameCallback` timestamp quality
   vs the 15 ms pairing budget.
4. OPFS episode write/upload throughput on school-grade hardware.

**P1 — "Aufnahme im Browser" (replaces the .exe for record→train):** hardware runtime (teleop,
boot sync, collision e-stop), OPFS episode capture, session-service packager + HF upload,
existing cloud training. Success = a student records, trains, and sees the dataset in Daten
with zero installs.

**P2 — cloud inference:** Modal WSS endpoint + chunked execution + preflight ports.

**P3 — Roboter Studio remote:** workflow engine in the session service over the 14-callable
rebinding; calibration wizard server-side; manual-mode services; sim path (pure server, trivial).

**P4 — dataset editing + long tail:** `/dataset/edit` (subprocess re-encode) moves server-side
next to the packager where the data already lives; retire installer for browser-capable
classrooms.

Per the repo's own convention, each phase that touches >1 layer gets its dated one-pager in
`docs/plans/` before code.

---

## 9. Honest risk register

| Risk | Severity | Mitigation |
|---|---|---|
| 100 Hz Worker jitter on weakest school PCs | HIGH (P0 gate) | Worker loop + adaptive tick; teleop degrades gracefully (mirror at 50 Hz is still usable — recording samples at 30 Hz regardless); hard data from P0.1 |
| Tab lifecycle (sleep, tab discard, accidental close) mid-torque | HIGH | Bus Watchdog (firmware), `pagehide` torque-off, wake-lock API during active sessions, session-service dead-man alarm |
| School upstream bandwidth for episode upload | MED | Upload in reset windows; optional in-browser H264 (WebCodecs) before upload; episode-size caps |
| Dataset byte-parity vs current recorder | MED | Server-side packager reuses the exact Python pipeline; golden-dataset diff test in CI |
| Two-cam identical-serial role swap | MED | One-time role assignment UI + persisted deviceIds + per-session preview confirm (same defense-in-depth philosophy as today's 3 guards) |
| Chrome-only exclusion (Firefox/Safari households) | ACCEPTED | Stated product constraint; `.exe` SKU remains |
| Session service = remote arm-command channel | MED | JWT binding, server-side chunk validation, rate limits, kill-switch parity with `EDUBOTICS_COLLISION_ENABLED` |
| Offline classrooms | ACCEPTED | Browser path requires internet by construction; `.exe` remains the offline SKU |
| Rule-§2 scope (e-stop relocation) | PROCESS | Explicit user sign-off before P1 implementation |

---

## 10. Sources

Codebase: all `file:line` cites above, verified against repo HEAD on this branch's base
(2026-07-12, 12-agent audit fan-out).

Web:
- Web Serial enterprise policies: [SerialAllowUsbDevicesForUrls](https://chromeenterprise.google/intl/en_us/policies/serial-allow-usb-devices-for-urls/), [SerialAllowAllPortsForUrls](https://chromeenterprise.google/intl/en_uk/policies/serial-allow-all-ports-for-urls/)
- Browser arm-control prior art: [LeRobot.js (HF blog)](https://huggingface.co/blog/NERDDISCO/lerobotjs), [lerobot GitHub](https://github.com/huggingface/lerobot)
- OpenRB-150 `usb_to_dynamixel` factory firmware & 1 Mbps: [ROBOTIS e-Manual OpenRB-150](https://emanual.robotis.com/docs/en/parts/controller/openrb-150/), [DYNAMIXEL Protocol 2.0](https://emanual.robotis.com/docs/en/dxl/protocol2/)
- Chrome Local Network Access (142 fetch/XHR, 147 WebSocket): [Chrome dev blog](https://developer.chrome.com/blog/local-network-access), [LNA + WebSockets note](https://myconnectionserver.visualware.com/support/v11/userguide/chrome-lna-websocket), [WICG explainer](https://github.com/WICG/local-network-access/blob/main/explainer.md)
- HF Hub uploads from JS/browser: [@huggingface/hub (npm)](https://www.npmjs.com/package/@huggingface/hub), [huggingface.js hub docs](https://huggingface.co/docs/huggingface.js/hub/README)
- LeRobot async inference (architecture precedent + CVE warning): [async inference docs](https://huggingface.co/docs/lerobot/async), [HF blog](https://huggingface.co/blog/async-robot-inference), [CVE-2026-25874 write-up](https://chocapikk.com/posts/2026/lerobot-pickle-rce/)
- Modal web/WebSocket endpoints, cold start, pricing: [Modal web functions](https://modal.com/docs/guide/webhooks), [Modal cold start](https://modal.com/docs/guide/cold-start)
