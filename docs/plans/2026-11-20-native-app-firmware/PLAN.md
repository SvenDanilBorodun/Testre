# EduBotics native app + arm firmware: the plan (2026-11-24)

Status: **plan with the owner's decisions of 2026-11-24 folded in; not yet a build spec.** Each phase
in §6 becomes its own `/deepwork` run. Evidence for every technical claim is in `FINDINGS.md` and the
four reports under `research/`; this file does not repeat it. Where this plan and `BRIEF.md` or
`FINDINGS.md` disagree, this plan wins (the owner changed several points, marked **[owner]**).

Proof status, stated once: nothing here has run on Windows, on an iPad or on a robot. Every such
claim has a named test (17 Windows spikes, 13 bench scripts). The owner allowed Windows spikes on
GitHub Actions under a standard account on spike branches only, and has a provisioned V1, parts for a
second V1, a HAT (A) and a locked-down Windows PC for bench work.

---

## 1. Target in one paragraph

One arm family, the EduBotics **V1**: a follower and a **lighter leader copy** [owner], **each on its
own controller board running the same firmware** [owner], both on USB to the PC. A **native Windows 11
app** [owner] (no WSL, no Docker, no ROS) shows the unchanged React interface in a WebView2 window
hosted by Python (pywebview) [owner] and runs a ROS-free Python engine that reuses today's recorder,
Daten, Roboter Studio and Cloud-Inferenz code. Teleop goes **leader board → PC → follower board**
[owner]; all arm safety lives on the follower's board. Wi-Fi is not used at all [owner]; Bluetooth is
built in from the start and switched on once its proofs and the legal check pass [owner]. The cloud is
unchanged. The OMX and the WSL/Docker/ROS stack are retired at the end.

```
Student PC (Windows 11 x64)                               Arm kit
┌──────────────────────────────────────────────┐   USB    ┌──────────────────────────────────────┐
│ Host (Python, pywebview): window, supervisor, │◄───────►│ FOLLOWER board (ids 1–6), our firmware│
│  self-updater, sandbox start                  │         │  boot read-only, keep holding         │
│  ├─ WebView2: unchanged React UI              │         │  100 Hz loop, setpoint/waypoint       │
│  ├─ Engine (CPython 3.13, ROS-free)           │         │   executor (integer ticks, µs)        │
│  │   rosbridge subset + HTTP, login-protected │         │  every driver guard + EEPROM check    │
│  │   recorder · Daten · Studio · cloud client │         │  tracking-error guard (new)           │
│  ├─ Arm link process: both COM ports,         │         │  gravity-compensated collision stop   │
│  │   teleop relay ~100 Hz, state to engine    │         │  link watchdog, health stream         │
│  ├─ Capture process: tower + wrist camera     │   USB    ├──────────────────────────────────────┤
│  ├─ Daten sidecar + workers                   │◄───────►│ LEADER board (ids 11–16), same firmware│
│  └─ Student code: AppContainer + Job Object   │         │  read-only role: never writes a goal  │
└──────────────────────────────────────────────┘         └──────────────────────────────────────┘
   │ HTTPS (unchanged)            ▲ optional, IT-enabled: iPad browser as the station's screen
   ▼                              │ Bluetooth (later): identify, status, setup, updates, iPad remote
 Cloud API · RunPod · Modal · Supabase · Hugging Face
```

---

## 2. Decisions

### 2.1 Hardware
| Topic | Decision |
|---|---|
| Board | **The Waveshare Bus Servo Driver HAT (A) is the final follower board** (owner, 2026-11-24, replacing the earlier "custom ESP32-S3 later"). No custom board track. Its weak points are therefore permanent and need fixed mitigations: the bus-bypass slide switch covered or glued, an inline fuse and XT60-only supply, firmware that keeps holding when opening the COM port resets the chip. Bluetooth stays 4.2. The rows below that say "custom board" (stop button, secure boot) need a new owner decision. [owner] |
| Boards per kit | Two: one per arm, same board type, same firmware; the role comes from the servo ids found (1–6 follower, 11–16 leader). [owner] |
| Leader arm | A lighter copy with the same geometry and easier-turning servos; claw blades limp, moved by hand. [owner] |
| Teleop path | Through the PC, about 100 times a second. A bench test on a slow laptop must confirm the lag. [owner] |
| Stop button | Only on the custom board. [owner] |
| Power supply | Sized after the bench power test. [owner] |
| HOME / joint 3 | Decided after measuring rig gate RG-H1. [owner] |
| Cameras | Tower camera plus a wrist camera. [owner] |
| Kit cost | No hard ceiling. [owner] |
| Rejected | The Waveshare "Servo Driver with ESP32" (CP2102 driver, 5 A barrel-jack supply) except as an optional second test target. |

### 2.2 Radios, legal
| Topic | Decision |
|---|---|
| Wi-Fi | Not used; compiled out. [owner] |
| Bluetooth | Designed and bench-measured from the start; shipped when its safety proofs and the legal check pass. [owner] |
| Bluetooth uses | Identify and status broadcast; setup and firmware update; a native iPad remote (jog, hand-guide, stop). [owner] |
| Compliance | A consultant reviews radio, CE and Cyber Resilience Act duties before Bluetooth or the custom board ships. [owner] |
| Legal entity | Expected within 3 months; Windows signing as an EU organisation and the Apple organisation account wait for it. [owner] |
| Firmware authenticity | Signed updates with rollback now; secure boot on the custom board at the factory step. [owner] |

### 2.3 iPad and other platforms
| Topic | Decision |
|---|---|
| iPad alone at a station | Dropped. [owner] |
| iPad for cloud work | Yes (Safari; needs a small "cloud host" build mode of the student app). |
| iPad as the screen of a Windows station | Yes, in the first release, as an IT-enabled network mode (firewall rule from the per-machine installer, login-protected page). Lightning iPads included. [owner] |
| iPad remote app | Native iPad app over Bluetooth, after the Windows app. [owner] |
| Touch work | Later, no standing rule now. [owner] |
| Orange Pi | Frozen: existing Pis keep today's software; no V1 engine on the Pi. [owner] |
| Next platform | None planned; the engine avoids Windows-only choices where it is free. [owner] |
| OMX | Retired; today's WSL product gets fixes only until the end of the school year in which the native app ships. [owner] |

### 2.4 Safety (Rule §2 sign-offs given in principle; each needs its bench result)
| Topic | Decision |
|---|---|
| Boot | Read-only probe, keep holding torqued servos, report the restart reason. [owner] |
| Link loss in a program | Stop and hold; after a long idle hold a slow release in the servos' damping mode, only if the bench shows a controlled settle. [owner] |
| Link loss in teleop | Stop and hold after about 1 s. [owner] |
| New firmware guard | Continuous tracking-error and wrong-way watchdog, thresholds from bench data. [owner] |
| Collision stop | Gravity-compensated residual on the follower's board; recovery stays the two-step flow. [owner] |
| Damping mode | May be written on the bench only. [owner] |
| Models from another robot | Refused at start (robot type and joint names). [owner] |
| Teleop on + program/inference/manual start | Refused, as today. [owner] |
| Student-code sandbox | AppContainer + Job Object, provided the standard-user spikes pass. [owner] |
| Firmware scope | Execution-and-safety kernel only; planning, IK, floor and tower checks stay in the engine. |
| Limp servos | The firmware never writes a goal to a servo that should be limp; the leader role writes nothing at all. |

### 2.5 Windows app and engine
| Topic | Decision |
|---|---|
| Windows versions | Windows 11 only, 64-bit. [owner] |
| Install | Both channels from one signed build: per-user (self-install, self-update) and per-machine (IT). [owner] |
| Updates on IT-installed PCs | IT rolls them out; the app tolerates being one release behind. [owner] |
| Window host | Python (pywebview); the engine is a supervised child process. [owner] |
| Engine API | The rosbridge subset React uses today, login-protected, no client publishing; a typed interface later. [owner] |
| Migration off ROS | Compatibility layer first (tested against real ROS), then module-by-module clean-up. [owner] |
| Inference | Exactly like LeRobot: one action per control tick; no board-side buffering. [owner] |
| Recorded action | The goal sent to the follower. [owner] |
| Dataset type name | Stays `v1_studio`. [owner] |
| HF token | In memory only. [owner] |
| Java | A trimmed JDK with the compiler. [owner] |
| Removed | Rosbag recording mode, local GPU inference, dead service types, the old dataset-edit service, the phone camera. [owner] |
| Runtime | CPython 3.13, numpy 2.x, a curated hash-locked dependency set; camera capture and the arm link each in their own process; named pipes for student code; a stable per-user loopback port with a per-session secret. |
| Phase 0 | The stronger fix (never delete a disk, refuse a foreign one, import without admin per user); starts once this plan is final. [owner] |

### 2.6 Context the owner gave
No school depends on the product yet (pilot only), school PCs are mostly open, there is no fixed date.

---

## 3. What the owner's choices change against `FINDINGS.md`

1. **Two boards instead of one.** Teleop is relayed by the PC, so it pauses when the PC stalls. In
   exchange the PC sees the exact action it records, "is teleop on?" is a plain engine state, the two
   arms share neither power nor bus, and each bus carries 6 servos (cycle about 1.3–2.3 ms by the
   firmware report's arithmetic, not yet measured). The relay runs in the arm-link process, not in
   the engine, so a busy engine does not slow it.
2. **pywebview instead of a native host.** Self-update, supervision and sandbox start are written in
   Python; the window does not survive a crash of its own Python process. The engine is a separate
   child so that an engine crash leaves the window and its German error message standing.
3. **Windows 11 only** removes the Windows 10 test matrix.
4. **iPad as a screen in the first release** adds a network mode to the Windows app: the Orange Pi's
   robot login (one-time tickets from the cloud, session cookie, encrypted token hand-over) is the
   pattern to reuse.
5. **Pilot-only fleet** makes migration off WSL a small step rather than a rollout.

---

## 4. Open points that still need an answer or a measurement

| # | Point | Why it is open | Settled by |
|---|---|---|---|
| O1 | iPad remote jog and the camera-tower guard | Today every jog is judged by the tower guard in the engine. A tablet that jogs the board directly would bypass it. | Owner (Rule §2) when the remote is specified: route jog through a PC engine, or limit the remote to hand-guide, stop and identify. |
| O2 | Touch gaps and iPad-as-screen | Touch work was deferred, but the iPad screen mode ships in the first release; some controls are unusable by touch today. | The iPad test checklist; then the owner decides which fixes are in. |
| O3 | Plain HTTP on the school network for the iPad screen mode | The session cookie travels unencrypted, as on the Pi today. | Owner: accept as on the Pi, or require an encrypted transport. |
| O4 | Lighter leader design | Servo type, gear ratio, joint offsets and how its angles map to the follower's. | Mechanical design + bench. |
| O5 | Wrist camera | Model, mount on the V1, cable route, effect on the claw's reach and the tower guard's arm model. | Design + bench. |
| O6 | Custom board maker and schedule | Not decided. | Gate after the firmware phase. |
| O7 | Power supply size, HOME pose | Wait for the bench. | Bench power test, RG-H1. |
| O8 | Teleop lag through the PC | Unmeasured on a slow Windows laptop. | Bench teleop-lag test; fallback is a direct cable between the boards on the custom board. |
| O9 | Cloud-Inferenz timing | "Exactly like LeRobot" fixes one action per tick; the cloud path's own chunk blending (an earlier owner decision) is kept unless the owner says otherwise. | Owner confirms when Phase 5 is specified. |

---

## 5. Proof obligations (gates, not opinions)

| Gate | Proves | Before |
|---|---|---|
| RG-H1 + bench power, register dump, port-open reset, goal-write auto-torque, bus timing | The V1 hardware can hold HOME, the supply size, the board boots safely, 100 Hz fits | Any firmware design freeze |
| Windows spikes S1–S5, S-J | CPython and the JVM run inside an AppContainer as a standard user; every escape attempt is refused | Sandbox work is planned in detail |
| Spikes S7, S8, S16 | The dependency set imports and records on Windows; installed size; camera capture | The engine skeleton |
| Spike S10 | `daten_smoke.py` on Windows with a concurrent reader | Daten moves |
| Spikes S11, S13 | Timer jitter; the HAT's USB chip without admin and without resets | Teleop and recording |
| Firmware: shared test vectors, simulator, decoder fuzzing, hardware-in-the-loop | Every ported guard behaves like the Python driver | Any firmware release |
| Link-loss, stall-smoothness and teleop-lag benches | Stop within the bound; smooth motion through PC stalls; acceptable teleop | Leader and recording work |
| Collision bench (load maps, contact force, 30 min without a false trip) | The residual detector works | Owner sign-off of the collision stop |
| Compatibility layer differential test against real ROS | None of the 19 ROS-held behaviours is lost | The engine replaces the node |
| `record_fsm_smoke.py`, `daten_smoke.py`, `code_e2e_smoke.py` on Windows as a standard user | Recording, Daten and student code work natively | Release |
| One full trial: record → train → Cloud-Inferenz on the V1, success rate measured | The V1 with tower + wrist camera learns | The OMX is removed |
| Radio jitter and wrong-arm protection tests; compliance review | Bluetooth is safe and lawful to switch on | Bluetooth ships |

---

## 6. Phases

**Phase 0 — field fix of today's installer** (starts when this plan is final). Never delete a disk,
refuse a foreign one, import without admin per user, name the situation in German. Gate: executed
PowerShell tests for every disk state.

**Phase 1 — bench and Windows spikes** (in parallel).
Bench: RG-H1, VR gates on the follower, power, register dump, port-open reset, goal-write
auto-torque, bus timing. Windows: spikes S0–S16 on CI as a standard user and on the locked-down PC.
Gate: every result recorded; a decided fix for every failure.

**Phase 2 — firmware kernel on the HAT (A).** Protocol (integer ticks, microseconds, generated
structs, shared test vectors), read-only boot, role detection, 100 Hz loop, setpoint/waypoint
executor with a stop generator, every driver guard and the full EEPROM check, link watchdog, health
stream, signed update with rollback, a Python soft board and a bus simulator. Bluetooth: transport
and security layer designed, loop jitter with the radio on measured. Gates: link-loss, stall
smoothness, guard vectors, fuzzing. **Board decision gate** (custom board: who, when; consultant).

**Phase 3 — leader, teleop, new guards.** Lighter leader designed and built (a second V1 stands in on
the bench meanwhile); leader role in firmware; teleop relay in the arm-link process; tracking-error
guard; gravity-compensated collision stop; damping-mode bench. Gates: teleop lag, collision bench,
owner sign-off per guard.

**Phase 4 — engine step 1.** Compatibility layer, generated message classes, arm-link client in
place of the arm container, rosbridge subset with a session secret, capture process (tower split,
wrist camera), the whole 2.41.0 tower kit carried over, pywebview host with supervision. Gate: the
unchanged React build connects, shows twin and cameras, activates the arm, runs the tower calibration.

**Phase 5 — recording, Daten, Cloud-Inferenz on the V1.** Profile flags, leader gates re-keyed on the
engine's teleop state, action = goal sent, cross-robot model refusal, Daten's Windows portability
layer, token in memory. Gates: the smokes on Windows; the full train-and-run trial.

**Phase 6 — Roboter Studio and the sandbox.** Workflow callables rebound, manual modes, Vormachen
with the leader, AppContainer runner over named pipes, trimmed JDK. Gates: the code end-to-end matrix
against the native runner; Rule §2's four conditions re-proven; owner sign-off.

**Phase 7 — product.** Both installers, signing (legal entity), self-updater, the IT-enabled network
mode for iPad-as-screen, the cloud-host build for iPad Safari, data import from WSL, removal of the
OMX code and the WSL stack, engine step 2 (compatibility layer replaced). Gate: install and first
lesson on a clean Windows 11 PC with a standard account; iPad test checklist.

**Tracks beside the phases**
- Custom ESP32-S3 board (after the Phase 2 gate; two USB boards per kit, stop button, protected power).
- Bluetooth release: identify and status, setup, firmware update (after proofs and the legal check).
- Native iPad remote (after the Windows app; open point O1 first).

No date is fixed. A time estimate follows once Phase 1's results are in, because they decide the
size of Phases 2, 3 and 6.

---

## 7. Pointers
- Evidence: `FINDINGS.md`; `research/01-engine-seam.md` (19 ROS-held behaviours, parity matrix),
  `research/02-firmware-board-transports.md` (driver spec, bench scripts B1–B13),
  `research/03-windows-native.md` (spikes S0–S16, Phase 0 analysis),
  `research/04-ipad-crossplatform.md` (iPad facts, test checklist).
- Original idea: `BRIEF.md` (superseded where this plan differs).
