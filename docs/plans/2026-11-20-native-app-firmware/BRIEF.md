# EduBotics long-term architecture: V1 only, native app + arm firmware — final idea (2026-11-24)

Status: **final idea, not yet a spec.** Each phase in §8 becomes its own `/deepwork` run.
Written 2026-11-20, consolidated 2026-11-24 with the V1-only direction, the container
replacement, the HAT (A) check and the LeRobot-on-Windows checks. Builds on, and partly
supersedes, `docs/BROWSER_ONLY_DEPLOY_PLAN.md` (2026-07-12): its „browser + cloud session
service" target is replaced by „native app + arm firmware" (owner decision 2026-11-20: ship
software, no extra box per robot). Its OMX servo contract (§3.2–3.5) is no longer needed once
the OMX is retired.

**The idea in one paragraph.** One arm, the EduBotics **V1**, as a follower plus a V1 **leader**,
both on one Waveshare **Bus Servo Driver HAT (A)** running **our firmware** (loop, limits,
watchdog, teleop, collision stop). One per-user **Windows app** (no admin, no WSL, no Docker, no
ROS) shows the **unchanged React UI** and runs a **Python engine** that reuses today's recorder,
Daten, Roboter Studio and cloud-inference code. The cloud (training, inference, accounts, hours)
is unchanged. The OMX and the whole WSL/Docker/ROS stack are removed at the end, after the V1 can
record, train and run on its own.

## 1. Why

A school PC today runs: tkinter GUI → WSL2 VM → Docker → 4 containers (ROS 2 arm bringup,
`physical_ai_server`, `code_runner`, nginx + React). Consequences seen in the field:

- Admin rights for install, every update (Inno `PrivilegesRequired=admin`) and USB (`usbipd bind`).
- Multi-GB downloads (rootfs + images), reboot for the WSL feature.
- A whole failure class that exists only because a VM must be kept alive. Field case 2026-11-20:
  „WSL2 konnte eine virtuelle Festplatte nicht einbinden (ERROR_FILE_NOT_FOUND)" — the distro's
  `ext4.vhdx` was gone. Root-cause candidates: WSL's own `system.vhd` broken by a WSL update, or
  our `import_edubotics_wsl.ps1` deleting **another Windows account's** `ext4.vhdx` after a failed
  import into the shared `ProgramData\EduBotics\wsl` folder (`Remove-Item … ext4.vhdx` in the
  import-failure branch; WSL refuses to import over an existing disk with ERROR_FILE_EXISTS).
- The real-time arm loop runs on the student PC; a stalled PC directly affects the arm. The
  Feetech V1 servos have **no host watchdog** (`feetech_arm_node.py` header): a dead PC leaves
  the V1 energised.
- Two arm families (OMX Dynamixel + V1 Feetech) double the code, tests and support: three robot
  registries, the overlay chain, two IK solvers, two catalogs, two HOME families, two scans.

Industry pattern for desktop/edu arms (Dobot Magician + DobotStudio, myCobot + myStudio): a
**native desktop app over USB serial**, with the **motion loop and safety on a controller in the
robot**. LeRobot's SO-101 is the closest analogue to the V1: the same STS3215 servos, leader +
follower, an app over USB. Nobody ships a Linux VM + Docker to students.

## 2. Target architecture

```
Student PC (Windows; later macOS/Linux)          Arm kit
┌──────────────────────────────────────┐         ┌──────────────────────────────────────┐
│ EduBotics.exe  (per-user, no admin)  │  USB    │ Bus Servo Driver HAT (A), ESP32,      │
│  ├─ WebView2: the unchanged React UI │ serial  │ OUR firmware                          │
│  ├─ Engine (Python 3.13+)            │◄──────►│  ├─ 100 Hz servo loop                 │
│  │   recorder · Daten · Studio ·     │ framed  │  ├─ trajectory buffer + interpolation │
│  │   cloud-inference client ·        │ binary  │  ├─ joint/edge limits (enforced)      │
│  │   rosbridge-compatible server     │ proto   │  ├─ link watchdog                     │
│  ├─ Camera capture (MSMF, existing)  │         │  ├─ teleop leader → follower          │
│  └─ Code sandbox (AppContainer)      │         │  ├─ collision stop (Present_Load)     │
└──────────────────────────────────────┘         │  ├─ boot-time EEPROM check            │
          │ HTTPS (unchanged)                    │  └─ timestamped state stream          │
          ▼                                      └──────────┬───────────────────────────┘
 Cloud API · RunPod training · Modal inference              │ one TTL servo bus, 12 V
 · Supabase · Hugging Face   (all unchanged)        V1 follower ids 1–6 · V1 leader ids 11–16
```

Principle: **the PC sends targets and trajectories, never per-tick setpoints.** Real-time and
safety live in the firmware; PC jitter only delays the next target.

## 3. V1 only: what the V1 must gain first

Measured 2026-11-24 in `robot_profiles.py`: `v1_studio` is **Roboter Studio only** —
`recordable=False, editable=False, trainable=False, inferable=False, has_leader=False`,
`collision_enabled=False`, `camera_roles=('scene',)`. Keeping only the V1 as it is would remove
record → train → run, the product's purpose. So before the OMX goes, the V1 needs:

| Missing | How | Why |
|---|---|---|
| **Bench validation** | Rig gates VR1–VR16 (`docs/KNOWN-ISSUES.md`, `docs/v1-hardware-bringup.md`): servo order, signs, EEPROM provisioning, HOME, grasp numbers, joint4 ±110°, smoothness, claw gear ratio | Every V1 number is derived from CAD; nothing is hardware-validated |
| **Leader arm** | A second V1, or a cheaper 6-servo copy with the same kinematics, torque off, read at 100 Hz | ACT needs human demos; leader + follower is the standard (ALOHA, SO-101). Hand-guiding the follower puts hands in the images |
| **Teleop** | Firmware: leader q → follower goal on the same board; start-up becomes home → sync → teleop (today the V1 stops after home) | Low, PC-independent delay |
| **Collision stop** | Detection from `Present_Load` (already published as `JointState.effort`, consumed by nothing) on the board; student-paced recovery in the engine as today | The V1 has no teleop e-stop today. **Rule §2 change: owner sign-off** |
| **Pinch force** | Measure `Max_Torque` 800 (arm) / 150 (claw) on the bench | Inherited, unmeasured (VR4/VR5) |
| **Record / train / run** | Profile flags true, `has_leader=True`, config YAML with leader topics, dataset features (action = leader 6 dims) | Today the Aufnahme and Inferenz tabs refuse the V1 |
| **Second camera** (decision D9) | A wrist camera; `camera_roles` gains `gripper` | ACT works with one camera; grasps are clearly better with a wrist camera |
| **Training quality proof** | One full trial: record → RunPod ACT → Cloud-Inferenz on the V1, success rate measured | Unknown today (one camera, rotating claw, jaw friction unmeasured; a rubber pad helps, VR5) |

Order is load-bearing: **the OMX is removed only after this table is done.**

## 4. Component A — arm firmware

### 4.1 Board: Waveshare Bus Servo Driver HAT (A)
Checked 2026-11-22 against the product page (SKU 27577) and the wiki:

| Fact | Value |
|---|---|
| MCU | ESP32-WROOM-32 (2 cores, 240 MHz, Wi-Fi + Bluetooth) |
| Power | 9–25 V on XT60 or 5.5×2.5 DC jack, input = servo voltage (V1: 12 V); 5 V buck for a Pi |
| Interfaces | UART, 2× USB-C (one to the ESP32, one direct to the servo bus), a UART control switch picks the mode; TTL servo headers + RS485 header; Raspberry Pi 40-pin header |
| Flashing | Espressif flash tool / esptool over USB, up to 921 600 baud; Arduino and ESP-IDF |
| Factory firmware | JSON commands + **an open Wi-Fi hotspot at power-on** (our firmware removes it) |
| Compatible list | ST3215, ST3215-HS, ST3025, ST3020, RSBL series |
| Price | $18.99; $17.97 from 4 |

**Open checks before buying in volume (decision D2):**
1. **STS3250** (V1 joints 2/3) is not on Waveshare's list. Same protocol, register-identical to
   the STS3215 (`feetech_bus.STS_ACCEPTED_MODELS`); our firmware talks to the servo → bench test.
2. **Which USB bridge chip feeds the ESP32, and does Windows need an admin-installed driver?**
   Decides the no-admin install. The schematic
   (`https://files.waveshare.com/wiki/Bus-Servo-Driver-HAT-(A)/Bus_Servo_Driver_HAT_(A)_Sch.pdf`)
   was not read (it downloads, it does not open). Read it, and test on a clean school PC.
3. **Current rating** is stated nowhere. Six servos incl. two STS3250 under load: size the supply,
   measure.
4. One bus for 12 servos: estimated 2–3 ms per cycle at 1 Mbit/s (sync-read 12, sync-write 6),
   so 100 Hz fits — **measure**.

What the board unlocks: arm safety without the PC, smooth motion independent of Windows timing,
leader + follower on one board, board-timestamped recordings, app-driven firmware updates, a
one-board Pi stack (40-pin header + 5 V buck), wireless/tablet control later (off by default),
24 V RSBL servos / RS485 for a bigger arm later, and Chrome Web Serial for a possible
browser-only variant (Chromebooks) later.

### 4.2 Firmware responsibilities
1. **EEPROM check at boot** (replaces the driver's boot probe): ping ids 1..6 (and 11..16 for the
   leader), accepted models, `Min/Max_Position_Limit` = the designed windows
   (`feetech_bus.position_limit_window`), `Operating_Mode` 0, multi-turn bit refused. Refuse to
   energise on any mismatch and report a fault code (German text lives on the PC). Provisioning
   itself stays a tool (`tools/feetech_provision.py`, through passthrough mode).
2. **Servo loop** at a fixed 100 Hz: sync-read state (follower + leader), run the active command
   source, sync-write goals. One core for the bus, one for USB.
3. **Command sources, priority order:** fault/stop hold → trajectory buffer → teleop (leader on
   the same bus) → hold position.
4. **Trajectory buffer:** time-stamped points; quintic for moves, linear for replays (port of
   `trajectory_builder.py`: velocity floor 0.6 × 4.72 rad/s for the V1, 15/8 peak factor;
   `build_linear_segment`). A new chunk replaces the queued tail (the semantics the stack relies on).
5. **Limits enforced on the board**, not trusted from the PC: joint window → refuse the chunk (never
   clamp silently), velocity cap, the V1 edge rules from `feetech_arm_node.py`
   (`EDGE_REFUSE_MARGIN_TICKS` 40, post-energise abort 2048 ticks, commanded-tick clamp 128 ≤ 130),
   `Goal = Present` seed before every torque-on, register 40 never ≠ 0/1
   (`assert_safe_torque_enable`).
6. **Link watchdog** (the V1's only one — Feetech servos have none): no valid frame for `T_link`
   (proposal 300 ms moving, 1–2 s idle) → stop and hold; after `T_off` → hold or torque off
   (decision D7). ESP32 task watchdog → reset → boots torque-off.
7. **Collision stop**: `Present_Load` per joint ÷ 1000 against per-joint thresholds, low velocity,
   debounce (today's detector logic, `collision_detector.py`); trip → relax in place, report.
   Thresholds measured on the bench.
8. **State stream** at loop rate: q, dq, load, error flags, torque state, active source, leader q,
   **board µs timestamp**. The PC maps it with the existing `ClockMap`.
9. **Passthrough mode** (provisioning tool still works) and **firmware update** over the same USB,
   driven by the app after asking, never by the student. Wi-Fi/BT off.

### 4.3 Board protocol (draft)
USB serial, framed binary: `COBS(frame)` + `0x00` delimiter; frame = `type u8 | seq u16 | len u16 |
payload | crc32`. Little-endian, SI units as float32 (rad, rad/s, s).

| Dir | Type | Payload |
|---|---|---|
| PC→B | `HELLO` | protocol version, host nonce |
| B→PC | `HELLO_ACK` | protocol version, firmware version + git sha, board kind, joint count, capabilities bitmask, servo ids/models (follower + leader) |
| PC→B | `CONFIG` | joint signs, limits, loop rate, watchdog times, collision thresholds (the board refuses values outside its compiled ceilings — tighten, never widen, like `cloud_schedule.clamp_limits`) |
| PC→B | `TORQUE` | on/off (on = seed Goal = Present first) |
| PC→B | `TRAJ` | chunk id, start mode (now / after current), points `[t, q0..q4, grip]`, flags (linear/quintic) |
| PC→B | `MODE` | teleop on/off, hand-guide (limp), hold |
| PC→B | `STOP` | stop and hold (Stopp key) |
| PC→B | `HEARTBEAT` | keeps the link watchdog fed when idle |
| B→PC | `STATE` | board µs, q, dq, load, err flags, torque, source, leader q, traj progress |
| B→PC | `EVENT` | traj done/refused (reason), limit hit, collision, watchdog fired, EEPROM fault |
| both | `PASSTHROUGH` / `DFU` | raw bus mode / firmware update |

Every value is validated on the board (range, finiteness, monotone time) — the „every frame is
hostile" stance of `code_rpc.py`. Python and C share encode/decode test vectors (the repo's
`*-cases.json` pattern).

## 5. Component B — the engine (Python, no ROS)

### 5.1 The four containers and what replaces them

| Container today | What it does | Replaced by | Lost |
|---|---|---|---|
| `open_manipulator` | ROS 2 bringup, `feetech_arm_node.py`, `activation_agent.py`, arm scan, camera ingest | **Firmware** (loop, limits, watchdog, teleop, stop) + a **board client in the engine**; cameras read directly by the engine (no TCP bridge into WSL) | nothing |
| `physical_ai_server` | ROS node: recording, Daten, HF up/downloads, Roboter Studio, cloud inference, token slot; rosbridge; web_video_server | **The engine**, one Python process plus its helper processes as today (Daten sidecar, download, HF, edit workers); a **rosbridge-v2-protocol WebSocket server** so React is unchanged; video = an MJPEG endpoint | ROS tools (`ros2 topic echo`, rviz) and the rosbag recording mode (the Aufnahme page already never uses it) |
| `code_runner` | Student Python/Java: no network, uid 10001, limits | **Child processes in an AppContainer + Job Object** with bundled CPython and a JRE; the two sockets become AF_UNIX (Windows 10+) or named pipes | nothing functional; the sandbox must be re-proven (Rule §2) |
| `physical_ai_manager` | nginx serves React on :80; Origin/Host checks | **The engine serves the React build**; WebView2 loads it; the nginx checks re-implemented in the engine's HTTP server | nothing |

| Container mechanism | After |
|---|---|
| s6 respawns a crashed node | the app supervises and restarts its processes |
| `restart: "no"`, the GUI owns start/stop | same: „Umgebung starten" starts the engine |
| `mem_limit`/`pids_limit` | Job Object per process |
| „image == repo" | one signed installer per version |
| tmpfs token file | a file in the user's folder, encrypted with DPAPI |
| `ros_net` | local communication on one machine |

The Orange Pi keeps its containers until the end (decision D11), then runs the same engine on Linux.

### 5.2 What is reused, replaced, deleted
Measured 2026-11-20: `physical_ai_server/` has 95 `.py` files; only **6** import rclpy
(`physical_ai_server.py` 8 908 lines, `timer/timer_manager.py`, `utils/parameter_utils.py`,
`safety/collision_monitor.py`, `communication/communicator.py`,
`communication/multi_subscriber.py`), 9 import ROS message types.

| Area | Today | Target |
|---|---|---|
| Recorder (`data_processing/`, `capture_timeline.py` slot sampler, LeRobot writer) | ROS-free core | **reuse**; sources from board `STATE` and camera callbacks |
| Daten 2.0 (`daten/`) | ROS-free processes | **reuse** as is |
| Roboter Studio (`workflow/`) | ROS-free, ~14 injected callables | **reuse**; rebind callables to the board client (`ctx.publisher` → `TRAJ`) |
| Cloud inference (`inference/cloud_*`, `chunk_engine`) | ROS-free client | **reuse**; tick drives `TRAJ` |
| `physical_ai_server.py` (services, timers, mode arbiter, locks) | rclpy node | **rewrite as an application core**: same invariants (`on_manual`, lock order, motion-lock policy), plain threads + an event loop |
| V1 driver `feetech_arm_node.py` (2 358 lines) | ROS node, PC loop | its rules move into firmware; the file is the reference spec |
| Cameras | MSMF bridge → TCP → container ingest | engine opens them directly (`camera_bridge.py` code) |
| HF token | tmpfs file + ECDH envelope | in-process memory + DPAPI; the envelope stays only for the Pi |

**Deleted with the OMX** (after §3 is done): ROBOTIS `open_manipulator/` (148 files, ~18 000
lines), the 7-file overlay chain and its sha256 check, the Dynamixel xacro limits,
`collision_monitor.py`/`collision_detector.py` OMX parts (1 700 lines, logic reused for the V1),
OMX IK `ik_solver.py` (500 lines), `omx_full`/`omx_follower` profiles, the OMX catalog,
`public/omx-urdf/`, the OpenRB-150 scan (VID 2F5D), the robot-type choice (three registries), the
arm-family conflict checks, the `:8769` follower-only flip, the second HOME family. About 78 React
and 25 server files mention OMX/leader/collision and need edits (leader logic stays, rebound to
the V1 leader). **Kept readable:** old OMX datasets, models and trajectory rows (as the retired
types are today).

**Deleted with WSL** (Phase 7): `wsl_rootfs/`, the WSL installer scripts, usbipd handling,
Docker composes for Windows, the amd64 images, the GUI's `wsl … docker` layer. Estimate: 30 000+
lines removed, ~80 % of the Python logic kept, 10 000–15 000 new lines (firmware + engine).

### 5.3 UI contract — keep React unchanged first
React talks rosbridge (roslib) to 40 `.srv` services, 10 `.msg` types and topics such as
`/task/status`, `/joint_states`, `/heartbeat`, `/workflow/status`, `/workflow/sensors`,
`/huggingface/status`, `/edubotics/signal_status`, `/edubotics/hf_token_state`,
`/edubotics/activation_state`, `/edubotics/daten_state`, `/edubotics/cloud_inference_state`
(~43 non-test files). The engine serves the **rosbridge-v2 protocol** at `ws://localhost/rosbridge`
with the same names and message shapes; `/video/stream?topic=…` becomes MJPEG; `/daten-api/`
stays. **Later:** a typed client replaces roslib. Enum wire ints and `ci.yml::enum-parity` hold.

### 5.4 LeRobot on native Windows
LeRobot is **not lost**: it stays `lerobot==0.5.1` from PyPI, public API only, for recording,
Daten, training (RunPod, unchanged) and Cloud-Inferenz (Modal, unchanged); datasets stay v3.0.
What can differ on Windows and must be proven:

| Point | Risk | Answer |
|---|---|---|
| Clock | `time.monotonic()` on Windows ticks every 15.6 ms before Python 3.13 | **Python 3.13+** in the app |
| Video encoding | PyAV/ffmpeg Windows builds may lack or slow a codec | measure the recorder's codec |
| Frame-loss detection | relies on LeRobot's warning timing | `frame_drop_rerecord` smoke scenario |
| Open files | Windows cannot rename/delete an open file (Daten swap, finalize) | Daten smoke on Windows |
| Long paths | 260-character limit unless enabled | short data root or long-path manifest |
| Shutdown finalize | no SIGINT/SIGTERM chain | an explicit finalize on „Beenden" and app close |
| torch | LeRobot's dataset module may import it (+ ~200 MB) | measure; decides installer size |

Gate: `record_fsm_smoke.py` and `daten_smoke.py` pass on a Windows CI runner.

### 5.5 Code sandbox (Rule §2, owner sign-off)
Today: container, no network, uid 10001, supervisor with SETUID/SETGID/KILL. Target, no admin:
student process in an **AppContainer** (no network capability, no file access beyond its temp dir)
inside a **Job Object** (memory, CPU rate, process count, kill-on-close). Unchanged: the two-socket
split, server-side budgets and validation in `code_rpc.py`, out-of-band stop. Spike early: CPython
+ the JDK inside an AppContainer with our stub library; `ctypes`/`subprocess` stay contained.

### 5.6 Packaging, install, update
- One per-user installer (`%LOCALAPPDATA%\Programs\EduBotics`), `PrivilegesRequired=lowest` (or
  MSIX): embedded CPython 3.13, the engine, the React build, a JRE, PyAV. Target ≤ 600 MB.
- Update: the existing `/version` channel + SHA-256, per user, background download, apply on next
  start. Firmware shipped inside the app, applied by it after asking.
- Driver: depends on the HAT (A) USB chip (§4.1 check 2).

## 6. Safety mapping (Rule §2)

| Today | After |
|---|---|
| V1 EEPROM fingerprint, edge refusal, post-energise abort, tick clamp | firmware |
| Torque-off on SIGTERM / atexit (none on a dead host for the V1) | firmware link watchdog + ESP32 task watchdog — **stronger** |
| Boot motion gate (`EDUBOTICS_REQUIRE_ACTIVATION`) | engine sequence home → sync → teleop; the board never moves on its own |
| Trajectory tolerances / arrival check | firmware buffer + `EVENT traj done/refused`; arrival check in the engine |
| Teleop collision e-stop (OMX only today) | **new for the V1**: detection on the board, recovery FSM in the engine (owner sign-off) |
| Manual-mode arbiter, motion-lock policy | engine, unchanged invariants |
| Floors, IK reach, no-go zones, velocity floor (`workflow/`) | engine, unchanged; board limits as a floor below them |
| Code-runner four conditions | AppContainer + Job Object + unchanged `code_rpc` budgets |
| Cloud-Inferenz ceilings (stall end, blind cap, leader refusal) | engine, unchanged; board watchdog adds a hardware floor |

Every row needs a bench re-proof before WSL is retired.

## 7. Migration and coexistence
1. Field fixes to the WSL product first (Phase 0); it stays the product for months.
2. The native app installs **beside** it. First start offers „Daten übernehmen": datasets
   (`ai_workspace`), calibration (`edubotics_calib`), HF cache optional, out of the distro via
   `wsl -d EduBotics -- tar …`; upload-first prompt for unuploaded datasets.
3. Schools with OMX arms: decide how long the OMX is supported (decision D10); their datasets and
   models stay readable in the cloud but do not run on a V1.
4. After one release cycle without blockers: no new WSL installs; „Alte Umgebung entfernen"
   unregisters the distro (own account only) and removes usbipd (IT, once).
5. Orange Pi: Docker until the engine runs on Linux, then the same engine natively.

## 8. Phases with go/no-go gates (~5 months; hardware first)

**Phase 0 — field fix in today's product (1–2 weeks)**
- `import_edubotics_wsl.ps1`: delete only a disk this run created; refuse to import over another
  account's disk; per-account folder `ProgramData\EduBotics\wsl\<SID>` for new imports.
- Import un-elevated as the GUI user; app updates without admin.
- GUI: name the missing file; separate remedies (`wsl --update` for WSL's `system.vhd`, „neu
  einrichten" for our disk, IT text for a blocked disk).
- Gate: executed pwsh tests for every disk state (missing, other owner, locked, compressed).

**Phase 1 — V1 bench + HAT checks (1–2 weeks, in parallel with Phase 0)**
- VR1–VR16 on the follower; the four HAT (A) checks (§4.1).
- Gate: every VR gate passed or a decided fix; board confirmed (D2).

**Phase 2 — V1 firmware (2–3 weeks)**
- Protocol v1, EEPROM check, 100 Hz loop, trajectory buffer, limits, link watchdog; a tiny
  Python test client on Windows.
- Gates: smooth motion with injected 500 ms PC stalls; D7 behaviour within `T_off` of USB unplug
  / process kill; EEPROM refusal on a wrong arm; jitter histogram.

**Phase 3 — V1 leader, teleop, collision stop (2–3 weeks)**
- Leader on ids 11–16, teleop on the board, `Present_Load` stop, pinch-force measurement.
- Gates: teleop delay ≤ 20 ms p95; stop within today's ~150 ms; owner sign-off on the stop.

**Phase 4 — engine skeleton + rosbridge emulation (3–4 weeks)**
- App core without rclpy; rosbridge-protocol server; `/joint_states`, `/task/status`,
  `/heartbeat`, activation; cameras + MJPEG.
- Gate: the unchanged React build connects, shows the twin and cameras, activates the arm.

**Phase 5 — recording, Daten, cloud inference on the V1 (3–4 weeks)**
- V1 profile recordable/trainable/inferable; slot sampler on board timestamps; LeRobot writer;
  upload; Daten; cloud tick.
- Gates: `record_fsm_smoke.py` + `daten_smoke.py` pass on Windows; a V1 dataset trains on RunPod
  and runs in Cloud-Inferenz with a measured success rate.

**Phase 6 — Roboter Studio + sandbox (4–6 weeks)**
- Rebind `workflow/` callables; manual modes, Vormachen, calibration; AppContainer runner.
- Gates: `code_e2e_smoke.py` matrix against the native runner; Rule §2 four conditions re-proven;
  owner sign-off.

**Phase 7 — installer, updater, migration; retire OMX and WSL**
- Per-user installer, `/version` channel, firmware updater, data import from the distro; remove
  the OMX code (§5.2) and the WSL stack.
- Gate: install + first lesson on a clean, locked-down school PC with a standard account.

CI: Windows runners test the engine directly (impossible with WSL2 nested virt); firmware builds
in CI (ESP-IDF or Arduino CLI); shared protocol test vectors between Python and C.

## 9. How the work runs

Each phase is one `/deepwork` run: spec (~1 day, reviewed by a second model) → owner gate
(~30 min) → test-first coders per work package in a worktree, verifiers per package (1–3 weeks)
→ **bench session** (owner, ~½ day, scripted checklist printing pass/fail with numbers) → PR, CI,
release on the owner's go. Phases 0 and 1 share no files and run in parallel; Phase 4 can start
once the protocol is frozen in Phase 2.

| Owner | Claude |
|---|---|
| decisions (§10, once), one approval per phase | specs, code, tests, CI, firmware build |
| buying hardware, flashing, bench runs | bench checklists and measurement scripts |
| testing on a locked-down school PC | analysing failures and fixing |
| the release go | PRs, changelog, CLAUDE.md |

Hardware before Phase 1: 2–3 HAT (A) boards, a second V1 (leader) or its parts, a spare V1 or
permission to reflash, a cheap slow Windows laptop (timing reference), a USB power meter;
optionally a logic analyzer.

## 10. Decisions for the owner
- **D1** Order: hardware first (Phases 1–3 before 4) — recommended.
- **D2** Board: HAT (A) for the V1 — confirm after the four checks (§4.1).
- **D3** V1 only: retire the OMX after Phase 5 — the direction of this brief; confirm.
- **D4** Drop ROS completely on Windows (no navigation/MoveIt planned).
- **D5** Collision detection on the board (recommended) or in the engine.
- **D6** Teleop on one board (leader + follower, same bus) — recommended; or two boards.
- **D7** Link loss: hold position (arm stays up, energised) vs torque off (arm falls).
- **D8** rosbridge emulation first (React unchanged) vs a typed API now.
- **D9** Second (wrist) camera on the V1.
- **D10** How long existing OMX schools are supported, and what they are offered.
- **D11** Pi: keep Docker until Phase 7, then the engine.
- **D12** Code sandbox on AppContainer as the replacement for the container (Rule §2 cond. 4).
- **D13** Platforms: Windows first; macOS/Linux later from the same engine?
- **D14** Leader hardware: a full second V1 or a cheaper copy with the same geometry.

## 11. Limitations, risks and what changes

### 11.1 Limitations of the target
- **No ROS:** no ROS tooling; navigation, MoveIt or mobile robots would be rebuilt (D4).
- **One arm family:** the V1 only; a new arm means a new firmware spec row and profile.
- **Windows first** (D13).
- **The HAT has no current sensor:** the stop relies on the servos' own load reading.
- **Feetech servos have no watchdog:** the firmware watchdog is the only one.
- **Code sandbox** is a new design, weaker-proven than the container.
- **Offline:** recording works; training and inference need the internet (as today).

### 11.2 Risks
| Risk | Severity | Mitigation |
|---|---|---|
| The only arm is the unproven one (VR1–VR16 open) | High | Phase 1 before anything is deleted |
| No V1 teleop e-stop until Phase 3; pinch force unmeasured | High | board stop, force measured, owner sign-off |
| PC crash leaves the V1 energised (today) | High | firmware watchdog (Phase 2) |
| HAT (A): STS3250, USB driver needing admin, current rating | Medium | the four checks before volume purchase |
| ACT quality on the V1 unknown | Medium | training trial in Phase 5; wrist camera (D9); rubber pad |
| LeRobot behaves differently on Windows | Medium | Python 3.13+, Windows smoke runs (§5.4) |
| Safety regression in the rewrite | Medium | §6 table, bench re-proof per row, owner sign-off |
| AppContainer breaks the runner or the JDK | Medium | spike early |
| Schools with OMX arms lose their arm's support | Medium (business) | D10, data stays readable |
| Data loss at the move off WSL | Medium | export with upload-first prompt |
| Firmware bugs / failed field updates | Medium | versioned `HELLO`, shared test vectors, verify + rollback |
| Single supplier (Feetech, Waveshare) | Low | cheap parts, keep spares |
| Two products in parallel for months | Low | Phase 0 keeps WSL stable; strict scope per phase |

### 11.3 What changes right after Phase 0
- The installer never deletes another account's disk; new imports get a per-account folder.
- Setup runs as the logged-in user; app updates need no admin.
- The start-failure dialog names the missing file and offers the right fix.
- Everything else unchanged (WSL, Docker, usbipd, both arms).

### 11.4 Today vs after all phases
| | Today | After |
|---|---|---|
| Arms | OMX (Dynamixel) + V1 (Feetech), V1 Studio-only | V1 only, leader + follower, all features |
| Install | admin, reboot, WSL feature, multi-GB images | one per-user installer, ≤ 0.6 GB, no admin |
| Updates | admin installer + image pulls | automatic per user; firmware by the app |
| USB | usbipd driver, bind, attach | a plain COM port |
| Arm control | 100 Hz loop on the PC inside a VM | loop on the arm's board; PC sends targets |
| PC freezes/crashes | V1 stays energised | the board stops the arm (watchdog, D7) |
| V1 collision stop | none | on the board |
| Typical field errors | VM won't start, missing disk, dockerd, usbipd, wrong account | ordinary app errors |
| Stack | ROS 2 + Docker + WSL + nginx + s6, 4 containers | Python engine + firmware, one app |
| V1 electronics | Bus Servo Adapter (A) | Bus Servo Driver HAT (A), ~$19 |
| Code size | — | ~30 000+ lines removed, 10–15 000 new |

**Unchanged throughout:** the React UI, LeRobot (0.5.1, v3.0 datasets), recording logic, Daten,
Roboter Studio, training, Cloud-Inferenz, the teacher dashboard, accounts and training hours.

## 12. Pointers
- V1 driver = firmware reference: `robotis_ai_setup/docker/open_manipulator/feetech_arm_node.py`,
  `feetech_bus.py`, `feetech_geometry.py`, `tools/feetech_provision.py`.
- V1 profile: `physical_ai_tools/physical_ai_server/physical_ai_server/robot_profiles.py` (`_V1_STUDIO`).
- V1 bench: `docs/v1-hardware-bringup.md`, `docs/KNOWN-ISSUES.md` (VR1–VR16, VG1/VG5).
- ROS seam to cut: the 6 rclpy files (§5.2); `workflow/` injection point in `physical_ai_server.py`.
- Recording proofs to run on Windows: `.github/scripts/record_fsm_smoke.py`, `.github/scripts/daten_smoke.py`.
- Phase 0 code: `robotis_ai_setup/installer/scripts/import_edubotics_wsl.ps1` (import-failure
  cleanup), `gui/app/gui_app.py::_report_distro_cannot_start`, `_distro_start_remedy`,
  `gui/app/wsl_bridge.py::classify_wsl_failure`.
- Prior plan: `docs/BROWSER_ONLY_DEPLOY_PLAN.md`.
- External: HAT (A) product page https://www.waveshare.com/bus-servo-driver-hat-a.htm ; wiki
  https://www.waveshare.com/wiki/Bus_Servo_Driver_HAT_(A) ; Bus Servo Adapter (A) FAQ
  https://docs.waveshare.com/Bus_Servo_Adapter_A/FAQ ; LeRobot SO-101
  https://huggingface.co/docs/lerobot/so101

Next session: walk §10 decisions, then `/deepwork` on Phase 0 (field fix); Phase 1 (bench + HAT
checks) runs beside it.
