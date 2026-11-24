# EduBotics native app + arm firmware: verified findings and reconciled design (2026-11-24)

Status: **findings, not yet the final plan.** The final plan is written once the owner has answered §9.
Basis: the idea brief (`BRIEF.md`, same folder) and four independent research reports in `research/`
(Opus 5.5, max effort, read-only), each with its own evidence, scripts and sources:

| Report | Area |
|---|---|
| `research/01-engine-seam.md` | Server wire surface, the ROS seam, functional parity, V1 record/train/infer gaps |
| `research/02-firmware-board-transports.md` | V1 driver as a spec, bus timing, HAT (A) schematic, boards, radios, safety |
| `research/03-windows-native.md` | No-admin install, signing, drivers, sandbox, LeRobot on Windows, WSL migration |
| `research/04-ipad-crossplatform.md` | iPadOS facts, where each function can run, architecture alternatives |

How to read the tags: **EXECUTED** = proven by a run on this macOS host; **CODE** = read in the
repository at HEAD dba3ac81 (2.41.0); **SOURCE** = primary documentation, schematic or vendor source;
**NEEDS-WINDOWS / NEEDS-BENCH / NEEDS-DEVICE** = not provable at a desk, with the deciding test named
in the report. Nothing in this study ran on Windows, on an iPad or on a robot.

Claims I re-checked in the code myself after the agents reported: the V1's collision wiring returns
before any leader subscription exists; the browser never publishes over rosbridge; cloud inference
publishes one action per tick; `import_edubotics_wsl.ps1` deletes `ext4.vhdx` after any failed import
with no ownership check; the V1 driver replaces the whole trajectory on every message; the provisioning
tool's safety registers; `program_start_gate` on every real program start; the Windows nginx host
check; KNOWN-ISSUES RG-H1. All held.

---

## 1. The result in short

The brief's direction survives: **a controller board with our firmware in the arm, a native Windows
app with a ROS-free Python engine, the React UI unchanged, the cloud unchanged, the OMX retired.**
No alternative (ROS on Windows, an engine rewrite in TypeScript/Rust, an engine in the cloud) is
better on reliability, cleanliness and "no function lost". But the brief is wrong or silent in
places that change the design:

1. **Firmware must be a small execution-and-safety kernel**, not a port of the driver's maths. Integer
   ticks and microseconds on the wire, a time-stamped waypoint executor with an explicit splice rule,
   the driver's guards, teleop, a stop generator. Planning, IK, floor and tower checks, recovery
   state machines and all German text stay in the engine.
2. **"Reset → boots torque-off" is dangerous.** An ESP32 reset does not touch the servos; a boot-time
   torque-off drops the arm. The board must boot read-only and adopt the hold.
3. **The HAT (A) is a good development board and a weak product board** (bus-bypass slide switch,
   unprotected servo rail, a module Espressif no longer recommends, Bluetooth 4.2 only, COM-open can
   reset the MCU). A production board is an open decision.
4. **A fixed-threshold collision stop cannot work on the V1's shoulder and elbow** (gravity alone uses
   up to ~83 % of joint 2's stall torque against an 80 % cap). It needs a gravity-compensated residual.
5. **Joint 3 at the V1's HOME sits at about its over-current trip** (already rig gate RG-H1). This is a
   hardware risk for a V1-only product regardless of architecture and must be measured first.
6. **19 behaviours of the server hold today only because of ROS machinery** (which callbacks are
   serialised, latching, timer semantics, the SIGINT finalize chain, respawn). They are the real risk
   of a ROS-free engine, not the line count (only 2.7 % of the node's code lines touch ROS).
7. **Leader + follower on one board invalidates every "is a leader live?" gate**, and the recorded
   "action" (today a ROS message the PC sees) no longer exists. Both need a defined replacement.
8. **Per-user install alone does not reach locked-down school PCs** (AppLocker default rules, frozen
   PCs, one copy per student account), and **code signing needs the legal entity first**.
9. **The current product can delete another Windows account's data** (Phase 0, confirmed in code).
10. **iPad: three different things.** Cloud pages on any iPad: cheap. iPad as the screen of a station
    whose PC/Pi runs the engine: works today against the Pi. iPad alone at a station: needs a native
    app, USB-C cameras, and Python somewhere else (LeRobot cannot run on iPadOS); unproven.
11. **Radios have a price.** Technically BLE is the dependable tablet link and Wi-Fi station mode is
    optional; a per-arm hotspot does not work in a classroom. Legally, enabled radios bring the EU
    radio cybersecurity rules (EN 18031) and the Cyber Resilience Act into scope (needs a consultant).

---

## 2. Reconciled target design

```
Student PC (Windows 10 22H2 / 11, x64)                    Arm kit
┌───────────────────────────────────────────────┐         ┌─────────────────────────────────────────┐
│ Signed host (window, supervisor, updater,      │  USB    │ Controller board, OUR firmware           │
│  sandbox broker)                               │  CDC    │  boot: read-only probe → adopt the hold  │
│  ├─ WebView2: the unchanged React UI           │◄──────►│  100 Hz loop: sync-read 12, sync-write 6 │
│  ├─ Engine (CPython 3.13, ROS-free)            │ framed  │  waypoint executor (int ticks, µs,       │
│  │   rosbridge-v2 SUBSET + HTTP on loopback,   │ binary  │   splice at board time, stop generator)  │
│  │   per-session secret, no client publish     │ proto,  │  every driver guard + full EEPROM check  │
│  │   recorder · Daten · Studio · cloud client  │ same on │  teleop leader→follower (never writes    │
│  │   board client (replaces the arm container) │ USB/BLE │   goals to a limp servo)                 │
│  ├─ Capture process (MSMF, tower split)        │ /Wi-Fi  │  link watchdog (buffer underrun + 1 s)   │
│  ├─ Daten sidecar + workers (portability layer)│         │  collision residual, health stream       │
│  └─ Student code: AppContainer + Job Object,   │         │  signed updates with rollback            │
│      named pipes, jlinked JDK                  │         │  radios: off until decided (§9 B)        │
└───────────────────────────────────────────────┘         └──────────┬──────────────────────────────┘
        │ HTTPS, unchanged                                            │ TTL bus(es), 12 V
        ▼                                                  V1 follower ids 1–6 · V1 leader ids 11–16
 Cloud API · RunPod · Modal · Supabase · Hugging Face
```

Engine migration in two steps (report 01 §10): **step 1** runs today's node almost unchanged on a
small runtime that reproduces rclpy's semantics (executors, callback groups, timers, guard condition,
latching), with message classes generated from the existing `.msg`/`.srv` files, and is
differential-tested against real rclpy in today's Linux image. **Step 2** replaces that layer with
explicit lanes module by module under recorded traces. A Python "soft board" (the same protocol over
a plain adapter) exists for development, CI and as a fallback with weaker guarantees.

---

## 3. Corrections to the brief (consolidated)

| # | Brief | Finding | Tag |
|---|---|---|---|
| 1 | 95 files, 6 rclpy importers, node 8 908 lines, driver 2 358 | 111 files; 8 rclpy importers (10 files touch ROS); 8 542; 2 643 | EXECUTED |
| 2 | React uses 40 services | 29 services, 20 topics; only `subscribe`/`unsubscribe`/`call_service`; never publishes | CODE + EXECUTED (172-line server passes with the app's own roslib 1.4.1) |
| 3 | "PC never sends per-tick setpoints" | Inference sends one action per tick (30 Hz) | CODE |
| 4 | TWDT reset → boots torque-off | Servos keep holding; torque-off at boot drops the arm | CODE + SOURCE |
| 5 | Chunk "replaces the queued tail" | V1 driver replaces the whole trajectory and jumps to its first point | CODE |
| 6 | Port `trajectory_builder.py` to C, float32 SI units | Not bit-exact with Python (±1 tick, time resolution, rounding); engine already sends dense waypoints | EXECUTED |
| 7 | Link watchdog 300 ms | Contradicts the brief's own "smooth through 500 ms stalls" gate | reasoning |
| 8 | Collision stop = today's thresholds on `Present_Load` | Gravity uses 45–83 % of stall on j3/j2; needs a model-based residual | EXECUTED estimate from URDF masses; NEEDS-BENCH |
| 9 | STS3250 compatibility is the open check | It is register-identical; the real questions are power and protection headroom | SOURCE |
| 10 | USB bridge chip unknown | Two CH343P (CDC, inbox `usbser.sys` expected); a slide switch hands the live bus to a raw USB port; no fuse/reverse protection on the servo rail | SOURCE (schematic); NEEDS-WINDOWS |
| 11 | Sandbox sockets become AF_UNIX | CPython has no AF_UNIX on Windows; named pipes | SOURCE; NEEDS-WINDOWS |
| 12 | "A JRE" | `javac` needs a JDK (a jlinked runtime with `jdk.compiler`) | SOURCE |
| 13 | ≤ 600 MB | 565 MB of wheels compressed; 1.2–1.8 GB on disk estimated; recorder needs torch | EXECUTED (resolution); NEEDS-WINDOWS (size) |
| 14 | Python 3.13, numpy unchanged | numpy must move to 2.x (LeRobot needs it; 1.26.4 has no 3.13 wheel; the cap existed only for cv_bridge); `pupil-apriltags` 1.0.4 → 1.0.4.post11 | EXECUTED (resolution) |
| 15 | HF token in a DPAPI file | DPAPI does not separate students on a shared Windows account | SOURCE |
| 16 | Per-user install | Blocked by AppLocker default rules; wiped on frozen PCs; N copies | SOURCE; NEEDS-WINDOWS |
| 17 | Daten "reuse as is" | `fcntl.flock`, `os.killpg`, `nice`, `/run`, renames of open files: a Windows portability layer is needed | CODE |
| 18 | Leader gates and recording carry over | Leader-liveness gates and the recorded action must be redefined (§1 item 7) | CODE |
| 19 | OMX models "do not run on a V1" | Nothing enforces it; cameras and sizes match | CODE |
| 20 | Tower kit (2.41.0) | Not mentioned; all of it must move into the engine (third node, calibration, guards, tower state, placement hint, stamp provider) | CODE |
| 21 | "Wireless/tablet control later" | No iPad browser has Web Serial/Bluetooth/USB; an iPad needs a native app or an engine host on the LAN | SOURCE |
| 22 | Phase 0 root cause | Confirmed: unconditional `Remove-Item … ext4.vhdx` after a failed import | CODE |

---

## 4. Decisions taken on the evidence (object if you disagree)

These have one clearly better answer, so I do not ask them in §9.

1. **Loop and safety on a controller board with our firmware**, plus a Python soft board for dev/CI.
2. **Firmware scope = execution-and-safety kernel**; no IK, geometry or planning on the board; the
   tower check stays in the engine (33 ms median in Python; no reason to move it).
3. **Wire format**: integer ticks, microsecond board time, generated fixed structs, CRC, command ids,
   flow control, one framing over every transport, shared test vectors Python ↔ C.
4. **Boot = read-only probe, adopt and hold**; the full provisioned safety EEPROM is verified at boot
   (today's probe does not check torque and current limits).
5. **Link watchdog on buffer underrun + link silence ≥ ~1 s**, tuned from a Windows stall histogram.
6. **No goal write to a servo that should be limp**, structurally (leader, hand-guided follower).
7. **Leader ids 11–16**, provisioned (return delay too).
8. **ROS-free Python engine; rosbridge-v2 subset first** (React unchanged), authenticated from day one
   and refusing every client publish; a typed API later, enabled by the generated contract.
9. **Two-step engine migration** (compat runtime, then explicit lanes) with characterisation traces.
10. **Leader gates re-keyed** on "the board's command source is teleop"; a program, inference or manual
    move is refused while teleop is on, as today.
11. **Recorded action = the goal the board actually wrote to the follower** (what inference will
    command); recording is refused while teleop is off.
12. **Inference keeps one setpoint per tick first**; a buffered-chunk mode is measured later.
13. **Python 3.13, numpy 2.x, curated hash-locked dependency set without unused extras.**
14. **Camera capture in its own process** until GIL contention is measured on a slow laptop.
15. **Student-code channel = named pipes**; sandbox contract kept transport-neutral.
16. **Loopback server on a stable per-user port with a per-session secret**, never port 80.
17. **HF token in memory only**, served to helper processes through one accessor (no file at rest).
18. **Dataset type name stays `v1_studio`.**
19. **Supported Windows**: 11 and 10 22H2, x64.
20. **Phase 0 goes first and stronger than the brief**: never delete a disk, refuse a foreign one,
    import un-elevated per user.

---

## 5. Where the reports disagreed, and the resolution

| Topic | Positions | Resolution |
|---|---|---|
| HF token at rest | 01: per-user file wiped at sign-out; 03: memory only | Memory only (decision 17): school PCs often share one Windows account, and a crash would leave a readable file. |
| Radios | 02: off in v1; 04: BLE is the tablet link; owner: full use | Protocol and firmware are transport-agnostic from day one; which radio roles ship, and when, is an owner decision with a legal dependency (§9 B). |
| Board | 02: HAT (A) now, custom ESP32-S3 for production; 04: S3 if wireless is first-class | Develop on the HAT (A) behind a hardware abstraction; the production board is an owner decision (§9 A). |
| BLE channel | 02: L2CAP preferred; 04: GATT baseline (Chrome speaks GATT only) | GATT byte pipe as the baseline; L2CAP optional later. |
| Raw-episode container | 04: introduce now to keep an iPad-only tier open | Not now. The recorder already takes timestamped histories; the container is only needed if the iPad-alone tier is chosen (§9 C). |

---

## 6. What is proven, and what is not

| Proven by execution here | Proven on paper only | Not provable at a desk |
|---|---|---|
| Brief counts; 41 of 43 node-method test files pass without ROS (1 210 tests) | HAT (A) schematic facts | Everything on Windows: AppContainer with CPython/JVM, pipes, timers, installer size, CH343 driver as a standard user, Daten file semantics (17 spikes, report 03 §15) |
| rosbridge subset server + IDL generator against the app's roslib | Bus cycle 2.5–4.5 ms (one servo parameter unmeasured) | Everything on the robot: bus timing, power, joint-3 load, collision maps, link loss, goal-write auto-torque, damping mode (13 bench scripts, report 02 §10) |
| Windows wheel resolution for Python 3.12/3.13 (97 packages) | iPadOS capability table (primary sources) | Everything on an iPad (7 gates, report 04 §9.1) |
| Tower check timing; float32 vs Python differences; gravity headroom estimate | Regulatory picture (summaries, not legal advice) | Locked-down school PC behaviour (AppLocker, frozen PCs) |

"Verified to the finest detail" therefore needs three things from the owner: Windows CI runs as a
standard user, bench sessions with a V1 and a HAT (A), and (only if an iPad tier is chosen) device tests.

---

## 7. Top risks (consolidated)

| # | Risk | Severity | First proof |
|---|---|---|---|
| 1 | Current product deletes another account's WSL disk | Critical, live | Phase 0; check field logs for `RegisterDistro/ERROR_FILE_EXISTS` |
| 2 | Joint 3 at HOME near its over-current trip: forearm drops | High | RG-H1 with current/temperature logging |
| 3 | One of the 19 ROS-held behaviours silently disappears in the engine | High | Compat runtime differential-tested against rclpy |
| 4 | Collision detector cannot separate contact from gravity | High | Bench load maps; owner sign-off |
| 5 | Firmware safety regression against the reviewed Python driver | High | Shared vectors, simulator, fuzzing, hardware-in-the-loop |
| 6 | Unsigned build blocked on school PCs; signing blocked by the missing legal entity | High | Owner decision |
| 7 | AppContainer sandbox fails for CPython/JVM on real school PCs | High | Spikes S1–S5 as a standard user |
| 8 | Behaviour drift from numpy 2 / newer OpenCV / Python 3.13 | High | Full server suite + smokes on Windows CI |
| 9 | Windows file locking corrupts Daten swaps | High | `daten_smoke.py` on Windows with a concurrent reader |
| 10 | HAT (A) slide switch, unprotected rail, COM-open reset | Medium–High | Mitigations or a custom board |
| 11 | Recorded action defined differently from what inference commands | Medium | Decision 11 + one full train-and-run trial |
| 12 | Radio regulation and classroom RF density | Medium (business) | Consultant before any radio ships |

---

## 8. Phase outline (to be finalised after §9)

0. **Field fix now** (stronger Phase 0), independent of everything else.
1. **Bench first**: RG-H1 and VR gates on the follower, the HAT (A) checks, the 13 bench scripts'
   first half (bus timing, power, port-open reset, goal-write auto-torque, register dump).
   In parallel: **Windows spikes** S0–S16 on CI as a standard user.
2. **Firmware kernel** on the HAT (A) + protocol + soft board + simulator; link-loss and stall gates.
3. **Leader, teleop, collision residual, health stream**; owner sign-off on each new guard.
4. **Engine step 1**: compat runtime, generated messages, board client, rosbridge subset, capture
   process, tower kit carried over; the unchanged React build runs end to end.
5. **Recording, Daten, cloud inference on the V1**; one measured train-and-run trial.
6. **Roboter Studio + sandbox**; Rule §2 four conditions re-proven.
7. **Installer channels, signing, updater, migration off WSL; retire OMX and WSL**; engine step 2.
Optional tracks decided in §9: production board, radio roles, iPad tiers.

---

## 9. Open decisions for the owner

See the question list in the conversation of 2026-11-24 (groups A–F). The final plan replaces this
section with the answers.
