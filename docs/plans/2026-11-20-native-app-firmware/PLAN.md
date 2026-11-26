# EduBotics native app + arm firmware: the plan (2026-11-24, owner answers folded in)

Status: **plan with every owner decision of 2026-11-24 folded in, including the Q&A that followed
`VERIFICATION.md`; ready for the architect.** It is split into three specs (§8). Evidence for every
technical claim is in `FINDINGS.md`, the four reports under `research/` and `VERIFICATION.md` (which
corrects some of them); this file does not repeat it. Where this plan and `BRIEF.md`, `FINDINGS.md` or a
report disagree, this plan wins. **[owner]** marks an owner decision.

**Release numbers amended 2026-11-26 (owner, `SPEC-DECISIONS.md` Q1-A):** the arm-check stillness fix shipped alone as **2.41.1**; Phase 0 ships as **2.41.2**, and `release/2.41` is cut from `v2.41.2`. Every „2.41.1" below that names Phase 0's release or the cut reads „2.41.2".

Proof status, stated once: nothing here has run on Windows, on an iPad or on a robot. Every such claim
has a named test (16 Windows spikes: S0–S5, S7–S14, S16, S-J in `research/03` §15; 13 bench scripts
B1–B13 in `research/02` §10). The owner allowed Windows spikes on GitHub Actions under a standard account
on spike branches only, and has a provisioned V1, parts for a second V1, a HAT (A) and a locked-down
Windows PC for bench work.

---

## 1. Target in one paragraph

One arm family, the EduBotics **V1**: a follower and a **lighter leader copy** [owner]. The **follower**
sits on a **Waveshare Bus Servo Driver HAT (A) running our firmware**; the **leader** sits on a **plain
Waveshare Bus Servo Adapter (A)**, has no firmware and is read by the PC [owner]. Both are on USB to the
PC, each with its own 12 V supply [owner]. A **native Windows 11 app** [owner] (no WSL, no Docker, no ROS)
shows the React interface in a WebView2 window hosted by Python (pywebview) [owner] and runs a ROS-free
Python engine that reuses today's recorder, Daten, Roboter Studio and Cloud-Inferenz code. Teleop goes
**leader adapter → PC → follower board** [owner]. All **real-time** arm safety (limits, watchdogs,
collision stop) lives on the follower's board; planning, IK, floor and tower checks stay in the engine.
Wi-Fi is not used at all [owner]; Bluetooth is built in from the start and switched on once its proofs
and the legal check pass [owner]. The cloud is unchanged except where a decision below names a change.
The OMX and the WSL/Docker/ROS stack are retired at the end.

```
Student PC (Windows 11 x64)                               Arm kit
┌──────────────────────────────────────────────┐   USB    ┌──────────────────────────────────────────┐
│ Host (Python, pywebview): window, supervisor, │◄───────►│ FOLLOWER: HAT (A), our firmware, ids 1–6  │
│  self-updater, sandbox start                  │         │  boot read-only, keep holding             │
│  ├─ WebView2: React UI (named changes only)   │         │  100 Hz loop, setpoint/waypoint executor  │
│  ├─ Engine (CPython 3.13, ROS-free)           │         │   (integer ticks, µs), stop generator     │
│  │   rosbridge subset + HTTP, login-protected │         │  every driver guard + full EEPROM check   │
│  │   recorder · Daten · Studio · cloud client │         │  tracking-error / wrong-way watchdog      │
│  │   System page (/api/system contract)       │         │  gravity-compensated collision stop       │
│  ├─ Arm link process: both COM ports,         │         │  link watchdog, health stream             │
│  │   teleop relay ~100 Hz, state to engine    │   USB    ├──────────────────────────────────────────┤
│  ├─ Capture process: tower + wrist camera     │◄───────►│ LEADER: plain Bus Servo Adapter (A),      │
│  ├─ Daten sidecar + workers                   │         │  ids 11–16, no firmware; read by the PC,  │
│  └─ Student code: AppContainer + Job Object   │         │  never written (except by the EduBotics   │
└──────────────────────────────────────────────┘         │  provisioning tool)                       │
   │ HTTPS (unchanged)            ▲ optional, IT-enabled: └──────────────────────────────────────────┘
   ▼                              │ iPad browser as the station's screen
 Cloud API · RunPod · Modal · Supabase · Hugging Face      Bluetooth (later, follower only): identify,
                                                           status, setup, updates, iPad remote
```

---

## 2. Decisions

### 2.1 Hardware
| Topic | Decision |
|---|---|
| Follower board | **The Waveshare Bus Servo Driver HAT (A) is the final follower board. There is no custom-board track.** [owner] Its weak points are permanent and get fixed mitigations: the bus-bypass slide switch SW1 glued to the ESP32 position; an inline fuse and an XT60-only supply (never the barrel jack); firmware that keeps holding when opening the COM port resets the chip. Bluetooth stays 4.2. |
| Leader board | **A plain Waveshare Bus Servo Adapter (A)** (CH343, today's V1 adapter), no MCU, no firmware. The arm-link process reads the leader bus directly; it never writes to it. Only the EduBotics provisioning tool writes there. [owner] |
| Roles | Follower ids 1–6 on the HAT; leader ids 11–16 on the adapter. The arm link tells the ports apart by protocol (the HAT answers our HELLO; the adapter's servos answer a Feetech ping of 11–16), never by COM number. A HAT that finds ids 11–16 on its bus runs teleop on the board only in the O8 fallback (§4). |
| Leader arm | A lighter copy with the same geometry and easier-turning **12 V** servos; claw blades limp, moved by hand. [owner] Servo model, gear ratio, joint offsets and the angle mapping to the follower: O4. |
| Power | **One 12 V XT60 supply per board** (follower HAT, leader adapter); the follower's size after the bench power test. [owner] |
| Teleop path | Through the PC, about 100 times a second, in the arm-link process. A bench test on a slow laptop must confirm the lag (numbers: §5). [owner] |
| Stop button | **None.** The on-screen stop and the firmware link watchdog. [owner] |
| Flashing | **EduBotics flashes every HAT before shipping**; the app updates it later over our own protocol. [owner] |
| Provisioning | Follower: a **gated firmware passthrough**, used only by an EduBotics provisioning tool (never the student app). Leader: the same tool, directly on the adapter's bus (`--role leader`, ids 11–16, `Return_Delay` 0). [owner] |
| Soft board | Our board protocol in Python over a plain adapter: **development and CI only, never shipped.** Today's pilot V1 followers are converted to a HAT. [owner] |
| HOME / joint 3 | Decided after measuring rig gate RG-H1. [owner] |
| Cameras | Tower camera plus a wrist camera (role `gripper`). [owner] |
| Kit cost | No hard ceiling. [owner] |
| Rejected | The Waveshare "Servo Driver with ESP32" (CP2102 driver, 5 A barrel-jack supply) except as an optional second test target; a custom ESP32-S3 board. |

### 2.2 Radios, legal, signing
| Topic | Decision |
|---|---|
| Wi-Fi | Not used; compiled out. [owner] |
| Bluetooth | Follower board only. Designed and bench-measured from the start; shipped when its safety proofs and the legal check pass. [owner] |
| Bluetooth uses | Identify and status broadcast; setup and firmware update; a native iPad remote (jog, hand-guide, stop). [owner] |
| Compliance | A consultant reviews radio, CE and Cyber Resilience Act duties **before Bluetooth is switched on or anything is sold**. Pilot kits with our firmware may go to pilot schools before that. [owner] |
| Firmware authenticity | **Signed updates with rollback; no eFuses are burned on the HAT** (no secure boot). [owner] |
| Legal entity | Expected within 3 months; Windows signing as an EU organisation and the Apple organisation account wait for it. [owner] |
| Signing before the entity | **Pilot builds ship unsigned to pilot PCs** (mostly open; SmartScreen warnings accepted for the pilot only). [owner] |

### 2.3 iPad and other platforms
| Topic | Decision |
|---|---|
| iPad alone at a station | Dropped. [owner] |
| iPad for cloud work | Yes (Safari; needs a small "cloud host" build mode of the student app). |
| iPad as the screen of a Windows station | **In the first release**, as an IT-enabled network mode. Lightning iPads included. [owner] Details: switched on **only by IT policy** (the per-machine installer, which also adds the inbound firewall rule a standard user cannot add); access with the Pi's robot login (one-time cloud ticket → session cookie) **for any role, without a teacher claim step**; the PC's own window and an iPad may be used **at the same time** (one robot token slot, the Pi's take-over handling); the PC shows a URL and a QR code on a **fixed machine-wide port**; **plain HTTP accepted** as on the Pi, stated in the IT sheet. [owner] |
| iPad remote app | Native iPad app over Bluetooth, after the Windows app. [owner] |
| Touch work | Later, no standing rule now. [owner] |
| Orange Pi | Frozen: existing Pis keep today's software; no V1 engine on the Pi. [owner] |
| Next platform | None planned; the engine avoids Windows-only choices where it is free. [owner] |
| OMX | Retired; today's WSL product gets fixes only until the end of the school year in which the native app ships. [owner] |

### 2.4 Safety (Rule §2 sign-offs given in principle; each needs its bench result)
| Topic | Decision |
|---|---|
| Boot | Read-only probe, keep holding torqued servos, report the restart reason. [owner] |
| Energise | Only at an explicit „Umgebung starten“; nothing moves before activation (as today). [owner] |
| Link loss in a program | Stop and hold; after a long idle hold a slow release in the servos' damping mode, only if the bench shows a controlled settle. [owner] |
| Link loss in teleop | Stop and hold after about 1 s; teleop resumes **only by a student action and a sync glide**. [owner] |
| A goal far from the last one (after a PC stall; teleop and inference) | **Executed as is**, shaped only by the servos' speed and acceleration registers (as the OMX and LeRobot do). The collision stop and the tracking watchdog are the floor. [owner] |
| New firmware guard | Continuous watchdog, thresholds from bench data: a **wrong-way runaway** (error over half a turn the long way) **torques that joint off** (as the post-energise abort today); a **tracking error while moving stops and holds** the arm. [owner] |
| Collision stop | Gravity-compensated residual on the follower's board; recovery stays the two-step flow, step 1 glides to the **V1 HOME**. [owner] **Scope (teleop/recording only, or also programs, manual moves, Cloud-Inferenz) is open: O10.** |
| Tower and table in teleop and Cloud-Inferenz | **Unguarded, as Rule §2 says for the OMX** (collision stop only). The **sync glide** at teleop start is planned by the engine through the floor ladder and the tower guard, like the home glide. [owner] |
| Orderly end (app close, „Stoppen“) | The engine **glides the arm to a rest pose on the table, away from the tower, then torques off**; if that glide is refused: the tower question, then torque off. The rest pose joins the fixture contract. [owner] |
| Damping mode | May be written on the bench only. [owner] |
| Models from another robot | Refused at start (robot type and joint names). Needs the training dataset's `info.json` at reserve time (the model meta carries neither); Daten's other-robot refusal is the pattern. [owner] |
| Teleop on blocks … | Exactly today's leader-gated set, re-keyed on "teleop is on": a real program start; a Cloud-Inferenz start (a running run is ended); jog and the manual home glide; hand-guide on; manual record start; replay. React keeps refusing simulator previews. Leader-Vormachen requires teleop on. Not gated (as today): simulator runs on the server, hand-guide off, capture pose, mark destination. [owner] |
| Student-code sandbox | AppContainer + Job Object, provided the standard-user spikes pass. If they fail, the failure is confirmed first and then decided (O11). [owner] |
| Firmware scope | Execution-and-safety kernel only; planning, IK, floor and tower checks stay in the engine. |
| Limp servos | The firmware never writes a goal to a servo that should be limp (hand-guided follower). The PC never writes to the leader bus. |

### 2.5 Windows app and engine
| Topic | Decision |
|---|---|
| Windows versions | Windows 11 only, 64-bit. [owner] |
| Install | Both channels from one build: per-user (self-install, self-update) and per-machine (IT). [owner] |
| Compatibility | The cloud and the firmware protocol stay compatible with **app N and N−1**; app N−2 shows a blocking German notice for robot work and keeps the cloud pages. [owner] |
| Firmware versions | The app **never downgrades** firmware; firmware speaks protocol N and N−1; an older app that meets newer firmware says „App aktualisieren“. [owner] |
| Updates on IT-installed PCs | IT rolls them out; the app tolerates being one release behind. [owner] |
| Window host | Python (pywebview); the engine is a supervised child process. [owner] |
| Setup UI | Today's tkinter flows (arm connection, camera roles, tower camera check, cloud-only choice, start/stop, factory reset, Protokoll, update prompt, legal links) move to a **React System page served by the engine with the Pi agent's `/api/system` contract**, the same page an iPad screen sees. [owner] |
| React | **Unchanged by default; small changes are allowed when a spec names each one** (known: V1 phase pictures, the hard-coded `localhost:8769` control bridge, Pi-mode detection for the screen mode, the System page). [owner] |
| Engine API | The rosbridge subset React uses today, login-protected, no client publishing; a typed interface later. Its behaviour, error texts included („does not exist“ marks an older image), is the contract. [owner] |
| Migration off ROS | Compatibility layer first (tested against real ROS), then module-by-module clean-up. [owner] |
| Inference | One action per control tick; no board-side buffering. **Cloud-Inferenz keeps its chunk blending** (0.3·old + 0.7·new = LeRobot async inference's default `weighted_average`). [owner] |
| Recorded action | **The leader reading mapped into the follower's joint space, before any PC or firmware clamp** — exactly what LeRobot's record loop saves (it records the teleop action, not the clamped sent action). Recording needs teleop on. [owner] |
| Dataset type name | Stays `v1_studio`. [owner] |
| HF token | In memory only. [owner] |
| Java | A trimmed JDK with the compiler. [owner] |
| Removed | Rosbag recording mode, local GPU inference, the seven unserved calibration `.srv` types, the old dataset-edit service, the phone camera, **the seven dead services** (`/training/status`, `/training/command`, `/training/get_training_info`, `/browse_file`, `/dataset/get_info`, `/get_saved_policies`, `/get_policy_list`), **MyModels' download to the robot**, **the OMX leader joystick buttons**. [owner] Kept: the Training page's robot-side lists. What the cloud path shares with local inference (timer name, `operation_mode`, `on_inference`, `_preflight_contract`, `INFERENCE_LOADING`) stays; the `record_rosbag2` field stays in `TaskInfo` while React sends it. |
| Runtime | CPython 3.13, numpy 2.x, a curated hash-locked dependency set; camera capture and the arm link each in their own process; named pipes for student code; a stable per-user loopback port with a per-session secret (the iPad network mode adds the fixed machine-wide LAN port). |
| Phase 0 | The stronger fix (never delete a disk, refuse a foreign one, import without admin per user); per-user distros and images (several GB per Windows account) accepted for the pilot; ships as **patch 2.41.1 through the normal pipeline** (tag → installer → hardware test → W6). It concerns only today's WSL product. [owner] |
| Branches | After 2.41.1, a **`release/2.41` maintenance branch** carries WSL-product fixes; **main carries the native work**; the cloud stays on main and serves both products. [owner] |

### 2.6 Context the owner gave
No school depends on the product yet (pilot only), school PCs are mostly open, there is no fixed date.

---

## 3. What the owner's choices change against `FINDINGS.md` and the reports

1. **Two separate buses, and the leader has no firmware.** Teleop is relayed by the PC, so it pauses
   when the PC stalls (the follower holds its last goal; a far goal after the stall is executed as is).
   In exchange the PC sees the exact action it records, "is teleop on?" is a plain engine state, and each
   arm has its own bus and its own supply. The follower bus (6 servos, our firmware) needs about
   1.6–2.6 ms per cycle by report 02's arithmetic (§3.2; t_turn 20–100 µs, full health reads at the upper
   end), unmeasured (B1). The leader is read by the PC over USB, so its cycle depends on the adapter and
   Windows (S13c, the teleop-lag bench). The relay runs in the arm-link process, not in the engine.
2. **Two clock domains.** Leader samples carry the PC's arrival time; follower samples carry the board's
   µs. Each source gets its own `ClockMap`; the recorded action's stamp is the leader sample's.
3. **The firmware's teleop mode changes meaning.** The follower board never sees the leader. The 0.30 rad
   proximity check at teleop start moves to the PC; the board keeps a teleop mode so the collision stop
   knows when it is armed.
4. **pywebview instead of a native host.** Self-update, supervision and sandbox start are written in
   Python; the window does not survive a crash of its own Python process. The engine is a separate child
   so that an engine crash leaves the window and its German error message standing.
5. **Windows 11 only** removes the Windows 10 test matrix.
6. **iPad as a screen in the first release.** A page from a non-loopback host is a Pi to the SPA
   (`utils/piMode.js`), so the engine serves the Pi agent's `/api/system` contract (login, robot key,
   update check, status, System page). The Orange Pi's robot login (one-time tickets, session cookie,
   encrypted token hand-over) is reused without the claim step.
7. **Pilot-only fleet** makes migration off WSL a small step rather than a rollout.

---

## 4. Open points

| # | Point | Why it is open | Settled by |
|---|---|---|---|
| O1 | iPad remote jog and the camera-tower guard | A tablet that jogs the board directly would bypass the engine's tower guard. | Owner (Rule §2) when the remote is specified: route jog through a PC engine, or limit the remote to hand-guide, stop and identify. |
| O2 | Touch gaps and iPad-as-screen | Touch work was deferred, but the screen mode ships in the first release. | The iPad test checklist; then the owner decides which fixes are in. |
| O4 | Lighter leader design | Servo model (12 V), gear ratio, joint offsets, angle mapping to the follower, claw mapping. | Mechanical design + bench. |
| O5 | Wrist camera | Model, mount, cable route, effect on the claw's reach, the tower guard's arm model and the clip-on calibration target. | Design + bench. |
| O7 | Follower supply size, HOME pose | Wait for the bench. | Bench power test, RG-H1. |
| O8 | Teleop lag through the PC | Unmeasured on a slow Windows laptop. | Teleop-lag bench. **Fallback decided: the leader on the follower HAT's bus (ids 11–16), teleop on the board.** [owner] |
| O10 | Collision-stop scope | Teleop and recording only (as the OMX), or also programs, manual moves, Cloud-Inferenz. | Owner, with the collision bench's data (left open on 2026-11-24). |
| O11 | Sandbox fallback | Only if S1–S5 fail. | Confirm the failure first, then the owner decides (Blockly only, WASM, …). |

Settled since `VERIFICATION.md`: O3 (plain HTTP accepted), O6 (no custom board), O9 (chunk blending kept).

---

## 5. Proof obligations (gates, not opinions)

**Pass bars.** Where a gate needs a number (teleop lag, the train-and-run success rate, the collision
trip force), it is measured the same way on today's OMX where possible; the V1 must match that baseline;
the owner approves each number at its gate. [owner]

| Gate | Proves | Before |
|---|---|---|
| RG-H1 + bench power, register dump, port-open reset, goal-write auto-torque, bus timing | The V1 hardware can hold HOME, the supply size, the board boots safely, 100 Hz fits | Any firmware design freeze |
| Windows spikes S1–S5, S-J | CPython and the JVM run inside an AppContainer as a standard user; every escape attempt is refused | Sandbox work is planned in detail |
| Spikes S7, S8, S16 | The dependency set imports and records on Windows; installed size; camera capture | The engine skeleton |
| Spike S10 | `daten_smoke.py` on Windows with a concurrent reader | Daten moves |
| Spikes S11, S13 | Timer jitter; the HAT's and the adapter's USB chips without admin and without resets | Teleop and recording |
| Firmware: shared test vectors, simulator, decoder fuzzing, hardware-in-the-loop | Every ported guard behaves like the Python driver | Any firmware release |
| Link-loss, stall-smoothness and teleop-lag benches | Stop within the bound; smooth motion through PC stalls; teleop lag within the OMX baseline | Leader and recording work |
| Collision bench (load maps, contact force, 30 min without a false trip) | The residual detector works | Owner sign-off of the collision stop and its scope (O10) |
| Compatibility layer differential test against real ROS | None of the 19 ROS-held behaviours is lost | The engine replaces the node |
| `record_fsm_smoke.py`, `daten_smoke.py`, `code_e2e_smoke.py` on Windows as a standard user | Recording, Daten and student code work natively | Release |
| One full trial: record → train → Cloud-Inferenz on the V1, success rate measured against the OMX baseline | The V1 with tower + wrist camera learns | The OMX is removed |
| Radio jitter and wrong-arm protection tests; compliance review | Bluetooth is safe and lawful to switch on | Bluetooth ships |

---

## 6. Phases

**Phase 0 — field fix of today's installer** (WSL product only). Never delete a disk, refuse a foreign
one, import without admin per user, name the situation in German. Gate: executed PowerShell tests for
every disk state (S0 on a real PC). Ships as 2.41.1; then `release/2.41` is cut.

**Phase 1 — bench and Windows spikes** (in parallel). Bench: RG-H1, VR gates on the follower, power,
register dump, port-open reset, goal-write auto-torque, bus timing (a minimal diagnostic firmware where a
bench script needs one), OMX baselines (teleop lag, success rate). Windows: spikes S1–S5, S7–S14, S16 and
S-J on CI as a standard user and on the locked-down PC. Gate: every result recorded; a decided fix for
every failure.

**Phase 2 — firmware kernel on the HAT (A).** Protocol (integer ticks, microseconds, generated structs,
shared test vectors), read-only boot, role check, 100 Hz loop, setpoint/waypoint executor with a stop
generator, every driver guard and the full EEPROM check, link watchdog, health stream, signed update with
rollback (no eFuses), the gated provisioning passthrough, a Python soft board and a bus simulator.
Bluetooth: transport and security layer designed, loop jitter with the radio on measured. Gates:
link-loss, stall smoothness, guard vectors, fuzzing.

**Phase 3 — leader, teleop, new guards.** Lighter leader designed and built (a second V1 on a plain
adapter stands in meanwhile); the arm-link process with the leader reader and the teleop relay; the sync
glide; tracking-error / wrong-way watchdog; gravity-compensated collision stop; damping-mode bench.
Gates: teleop lag, collision bench, owner sign-off per guard and of O10.

**Phase 4 — engine step 1.** Compatibility layer, generated message classes, the arm-link client in place
of the arm container, rosbridge subset with a session secret, capture process (tower split, wrist
camera), the whole 2.41.0 tower kit carried over (the full list is in `VERIFICATION.md` §6 C1), the
System page contract, pywebview host with supervision. Gate: the React build connects, shows twin and
cameras, activates the arm, runs the tower calibration.

**Phase 5 — recording, Daten, Cloud-Inferenz on the V1.** Profile flags, the teleop gates (§2.4), the
recorded action (§2.5), cross-robot model refusal, Daten's Windows portability layer, token in memory.
Gates: the smokes on Windows; the full train-and-run trial.

**Phase 6 — Roboter Studio and the sandbox.** Workflow callables rebound, manual modes, Vormachen with the
leader, AppContainer runner over named pipes, trimmed JDK. Gates: the code end-to-end matrix against the
native runner; Rule §2's four conditions re-proven; owner sign-off.

**Phase 7 — product.** Both installers, signing (unsigned for the pilot; the legal entity's signature
afterwards), self-updater, the IT-enabled network mode for iPad-as-screen, the cloud-host build for iPad
Safari, data import from WSL, removal of the OMX code and the WSL stack, engine step 2 (compatibility
layer replaced). Gate: install and first lesson on a clean Windows 11 PC with a standard account; iPad
test checklist.

**Tracks beside the phases**
- Bluetooth release on the follower board: identify and status, setup, firmware update (after proofs and
  the legal check).
- Native iPad remote (after the Windows app; O1 first).

No date is fixed. A time estimate follows once Phase 1's results are in.

---

## 7. Pointers
- Evidence: `FINDINGS.md`; `research/01-engine-seam.md` (19 ROS-held behaviours, parity matrix),
  `research/02-firmware-board-transports.md` (driver spec, bench scripts B1–B13),
  `research/03-windows-native.md` (spikes, Phase 0 analysis), `research/04-ipad-crossplatform.md`
  (iPad facts, test checklist).
- Corrections and code gaps: `VERIFICATION.md` (§3 factual corrections, §6 the independent code check).
- Original idea: `BRIEF.md` (superseded where this plan differs).

---

## 8. The three specs (owner: at most three; division decided by Claude, 2026-11-24)

The seam is the **board protocol and the arm-link process**: everything below it is proven and built in
spec B; everything above it is built in spec C, which is written only once spec B's numbers exist.

| Spec | Covers | Why this cut | Starts |
|---|---|---|---|
| **A — Phase 0: WSL field fix** | Phase 0 only: `import_edubotics_wsl.ps1`, the finalize path, the GUI's remedies, executed PowerShell tests (no test covers the delete branch today), spike S0, release 2.41.1, cutting `release/2.41`. | A live data-loss bug in the product schools run today; a different product, release train and test surface from everything else. | Now |
| **B — Proofs, firmware and the arm link** | Phases 1–3: all bench gates and scripts, the OMX baselines, the 16 Windows spikes, the firmware kernel on the HAT (A), the protocol and its generated codec and vectors, the soft board and the bus simulator, provisioning (passthrough + leader), the arm-link process (both COM ports, leader reader, teleop relay, sync glide hook), the new guards and their benches. | Everything here is hardware- or platform-bound and must produce numbers before the app is designed; its output (a frozen protocol, a tested arm-link package, the spike results) is spec C's input. | Now, in parallel with A (no shared files) |
| **C — The native app** | Phases 4–7: engine step 1 and 2, capture, tower kit, System page, recording/Daten/Cloud-Inferenz on the V1, Roboter Studio and the sandbox, installers, updater, signing hooks, iPad screen mode, cloud-host build, data import, OMX/WSL removal. | One software product on one seam; its design depends on spike results (sandbox, timers, MSMF, Daten on Windows) and the protocol freeze. | After spec B's Phase 1 results and the protocol freeze |
