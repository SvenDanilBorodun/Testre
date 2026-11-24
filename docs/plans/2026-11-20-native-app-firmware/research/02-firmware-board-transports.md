# 02 — Arm firmware, controller board, servo bus and transports

Research architect 2 of 4. Date: 2026-11-24. Repo: HEAD dba3ac81 (release 2.41.0). Read-only study; no tracked file was changed.

**Tags.** **VERIFIED** = read in code (`file::symbol`), computed by a script I ran, or read at a cited URL. **REFUTED** = the brief says X, the evidence says Y. **UNVERIFIED-BENCH** = needs hardware (a measurement and a script outline is given). **UNVERIFIED-WINDOWS** = needs a real Windows PC. **UNVERIFIED** (plain) = from background knowledge or a secondary source I could not confirm in this session. **ESTIMATE** = my arithmetic on stated assumptions.

**Scratch evidence** (all under `/private/tmp/claude-501/-Users-svenborodun-Desktop-eduboticscode-Testre/dfe0e2ca-98df-46f4-9a43-9af94cb59472/scratchpad/agent2/`):
`time_geometry.py`, `count_geometry.py` (driver geometry cost), `bus_timing.py` (bus arithmetic), `float32_check.py` (float32/rounding), `gravity_headroom.py` (collision headroom), `dl_hat_sch/` (the HAT (A) schematic PDF rendered at 500 dpi, crops `c_*.png`, `pdftotext` output), `dl_dobot/` (Dobot protocol PDF text).

---

## 0. Executive findings

1. **The HAT (A) schematic is now read** (VERIFIED, Altium export dated 2024-04-27, `Bus_Servo_Driver_HAT_(A)_Sch.pdf`). Both USB-C ports use a **WCH CH343P** (CDC-capable); one bridges the ESP32's UART0 with the classic DTR/RTS auto-program transistors, the other is a **raw USB-to-servo-bus port**. A **DPDT slide switch (SW1)** gives the servo bus either to the ESP32 (GPIO19 TX / GPIO18 RX) or to that raw USB bridge. The TTL bus uses an **auto-direction circuit** (PNP MMBT3906 + SN74LVC1G126/125, 10 kΩ pull-up), so no direction GPIO is needed. The servo supply ("24V" net: XT60, DC005 jack, screw terminal) goes **straight to the servo headers with no fuse, no reverse-polarity FET and no switch**; only a SMAJ26CA TVS. A 5 A MP9447 buck (≈5.38 V) feeds a Raspberry Pi through an ideal-diode PMOS; the Pi's UART lands on the ESP32's UART0 in parallel with the USB bridge.
2. **The no-admin question is answered on paper**: CH343 in CDC mode uses Windows' inbox `usbser.sys` (Waveshare: "For most of the PC, the driver for CH343 is CDC by default"). The CH343P's default pair is presumably `1A86:55D3`, the same as today's adapter; the schematic does not show it and the CH343 allows a custom VID/PID (UNVERIFIED-BENCH). What remains is a locked-down-PC bench and the chance that Windows Update later swaps in WCH's vendor driver (UNVERIFIED-WINDOWS).
3. **Firmware on a board is the better design** than a PC-side driver on today's adapter, but only as a **small real-time execution-and-safety kernel**. It should hold no IK, no geometry and no planning, and keep **integer ticks and microseconds** on the wire. The engine keeps everything it does today (planning, floor/tower guards, IK, recovery state machines, German text).
4. **The brief's "TWDT → reset → boots torque-off" is wrong and would be dangerous** (REFUTED). An ESP32 reset does not touch the servos: Feetech STS servos hold their last goal while energised, and they power up torque-off only when *they* lose power. A firmware that torques off at boot drops the arm (a V1 at HOME tips forward). The boot must **adopt and hold**.
5. **Bus timing (VERIFIED by computation, one unknown parameter).** One 1 Mbit/s bus with follower ids 1–6 and leader ids 11–16 needs **2.5–4.5 ms per cycle**, depending on the per-reply turnaround (20–100 µs, UNVERIFIED-BENCH) and the fields read. That is 25–45 % bus duty at 100 Hz, so 100 Hz fits. 200 Hz fits only with lean reads or two buses. With servos still at the factory return delay (500 µs, LeRobot's own comment), a 12-servo read takes 8.2 ms, so the **leader must be provisioned too**.
6. **A collision stop from `Present_Load` with fixed thresholds will not work on the V1's shoulder and elbow** (ESTIMATE from the driver's own URDF masses). Gravity alone uses ~45 % of joint 3's stall torque at HOME and up to ~83 % of joint 2's at stretched poses, while `Max_Torque_Limit` caps output at 80 %. The detector needs a **gravity-compensated residual**: the same idea as uFactory's "current and dynamic model based" detection, plus bench data.
7. **Joint 3 at HOME sits at its over-current trip** (ESTIMATE ≈2.04 A against `Protection_Current` 310 × 6.5 mA = 2.015 A; the repo's own estimate is "about 94 %", KNOWN-ISSUES RG-H1). The firmware must stream current, temperature and status at ≥ 10 Hz. A servo's own over-current unload drops the forearm.
8. **Two independent reports say a `Goal_Position` write switches torque ON by itself** (LeRobot #3585, May and Nov 2026; unconfirmed by maintainers). The firmware must **structurally never write a goal to a servo that should be limp** (the hand-guided follower and the leader).
9. **The tower box check is the only heavy computation** (VERIFIED on this Mac, CPython): median 33 ms, p95 84 ms, max 109 ms over the 807 fixture glides, 605 of them refused (matches the fixture). A float32 C port on an ESP32 is ESTIMATED at 0.25–0.5 s per clear glide. The ESP32 *could* run it off-loop, but there is **no reason to**: the firmware never plans a glide, and the engine has the calibrated version.
10. **A float32 port of the trajectory maths is not bit-exact with Python** (VERIFIED): it differs by up to 1 tick, float32 seconds-since-boot lose 244 µs resolution after one hour, and Python's `round()` (half-even) disagrees with C `lround` on half of all exact ties. Keep the safety path in **integers** and the **quintic generation in the engine**, which already emits dense waypoints. Do not port `trajectory_builder.py` to C.
11. **iPad**: Safari supports neither Web Serial nor Web Bluetooth nor WebUSB (VERIFIED). An iPad reaches the arm only through a **native app over BLE**, or as a browser client of an **engine host on the LAN** (today's Orange-Pi pattern). The board cannot host the React UI.
12. **Radios have a real cost**: EN 18031 (RED 3(3)(d/e/f), applicable since 2025-08-01, harmonised with restrictions such as no default passwords), and the **CRA, whose reporting duties already apply since 2026-09-11**. Thirty SoftAPs in one room would take an estimated 10–64 % of one 2.4 GHz channel for beacons alone. **Ship v1 USB-only, radios off.** Each radio role is a separately decided, separately assessed feature.
13. **Board choice**: **develop on the HAT (A) now** (cheap, proven bus circuit, inbox CDC driver). Its slide switch, unprotected servo rail, NRND ESP32-WROOM-32 module, DTR/RTS reset path and single shared bus make it a weak production controller. The **production target** should be a small custom **ESP32-S3** controller: native USB, two independent servo buses, a switched and current-monitored servo rail with fuse and reverse-polarity protection, and a stop button. Decide at the Phase-2 gate.

---

## 1. Corrections to the brief

| # | Brief says | Evidence | Tag | Consequence |
|---|---|---|---|---|
| R1 | `feetech_arm_node.py` 2 358 lines (§5.2) | 2 643 lines at HEAD (`wc -l`) | REFUTED (stale) | Re-measure before sizing work. |
| R2 | "ESP32 task watchdog → reset → boots torque-off" (§4.2.6) | Servos hold the last goal energised ("the servo has NO host watchdog — it holds its last goal energised forever", `feetech_arm_node.py` module docstring). `Torque_Enable` (40) is SRAM, default 0 only at *servo* power-up (Waveshare ST memory map). An MCU reset changes nothing on the bus; a boot-time torque-off write would drop the arm. | REFUTED | Boot = read-only probe → adopt (hold) → report reset reason. |
| R3 | "STS3250 is not on Waveshare's list → bench test" (§4.1 check 1) | With our firmware the board is protocol-transparent. LeRobot maps `sts3250` (2825) to the same control table as `sts3215` (`tables.py` v0.5.1, VERIFIED). The real STS3250 questions are **power** (4.2 A stall at 12 V) and **protection-current headroom** (finding 7). | REFUTED (framing) | Replace check 1 with the power and RG-H1 benches. |
| R4 | USB bridge chip and admin driver unknown (§4.1 check 2) | CH343P ×2 (schematic). CDC by default (Waveshare CH343 wiki). WCH also publishes a vendor driver on Windows Update (Microsoft Update Catalog search "ch343"). | Answered (paper); UNVERIFIED-WINDOWS | Remaining bench: standard user, locked-down PC, online and offline. |
| R5 | "A new chunk replaces the queued tail (the semantics the stack relies on)" (§4.2.4) | The V1 driver replaces the **whole** trajectory and restarts its clock at receipt (`_trajectory_cb` → `_replace_trajectory(points, time.monotonic())`). `interpolate_trajectory` returns the first point for `t <= points[0][1]`, so a chunk's first point is applied at once (the KNOWN-ISSUES chunk-boundary bump). The OMX JTC splices instead. | REFUTED | Firmware needs an explicit **splice-at-board-time** rule, and the engine must stamp chunks. |
| R6 | Protocol units "SI as float32 (rad, rad/s, s)" (§4.3) | float32 vs float64 differ by ≤ 1 tick; float32 time has a 244 µs ulp at 1 h and 3.9 ms at 10 h; half-even vs half-away rounding disagree on 2 048 of 4 096 exact ties (`float32_check.py`) | REFUTED (design) | Wire and firmware use int16 ticks and µs time. Radians and signs live only in the engine. |
| R7 | Trajectory buffer with quintic for moves, a port of `trajectory_builder.py` (§4.2.4) | The engine already samples every motion densely (`build_segment` / `build_linear_segment`, 30 fps; inference streams 1-point messages per tick, `data_converter.py::tensor_array2joint_msgs`) | REFUTED (unnecessary) | Firmware = time-stamped waypoint executor with linear interpolation + a stop generator. |
| R8 | 12 servos "2–3 ms per cycle" (§4.1 check 4) | 2.48–4.52 ms depending on read width and per-reply turnaround; 8.24 ms with factory return delay (`bus_timing.py`) | Partly VERIFIED | 100 Hz fits; provision the leader's return delay too. |
| R9 | Factory firmware opens an **open** hotspot (§4.1) | Waveshare: "Will Automatically Establish A Hotspot after Powering on". Whether it has a password is not stated. | UNVERIFIED-BENCH | Irrelevant once reflashed; check before the first classroom power-up of an unflashed board. |
| R10 | Board unlocks "wireless/tablet control", "Chrome Web Serial for … Chromebooks" (§4.1) | Safari/iPadOS: no Web Serial, no Web Bluetooth, no WebUSB (caniuse; WHATWG Web Bluetooth status) | REFUTED for iPad | iPad = native BLE app, or a browser on an engine host. Chromebook Web Serial stays plausible. |
| R11 | Collision stop: "`Present_Load` ÷ 1000 against per-joint thresholds (today's detector logic)" (§4.2.7) | Static gravity duty: j3 45 % at HOME, j2 up to 83 % (`gravity_headroom.py`); output capped at 80 % (`Max_Torque_Limit` 800) | REFUTED (insufficient) | Gravity-compensated residual + bench thresholds. |
| R12 | Link watchdog `T_link` 300 ms while moving (§4.2.6) | The brief's own Phase-2 gate wants smooth motion through 500 ms PC stalls. A 300 ms raw-silence stop trips on exactly those stalls. | REFUTED (inconsistent) | Watchdog on **buffer underrun + link silence ≥ 1 s** (§7.2); tune from a Windows stall histogram. |
| R13 | "a UART control switch picks the mode" (§4.1) | It is a DPDT **slide switch** that hands the energised servo bus to a raw USB port (SW1) | VERIFIED + hazard | A flipped switch cuts the firmware off from energised servos and exposes an unguarded bus. Lock it. |
| R14 | Board "Compatible list" etc. | Product page: ST3215, ST3215-HS, ST3025, ST3020, RSBL45-24/85-12/85-24/120-24; $18.99, tiers down to $17.97 | VERIFIED | — |
| R15 | Industry pattern: "motion loop and safety on a controller in the robot" (§1) | True for Dobot Magician (queued PTP/JOG executed by the controller, same protocol over USB, TTL, Wi-Fi/UDP and BLE: protocol PDF V1.1.5), myCobot (arm-side ESP32 firmware), Niryo (RPi 4 + ROS inside), uFactory (controller-side, model-based collision detection). **False** for LeRobot SO-101 (host loop, 60 fps default). | Partly REFUTED | See §8. The products sold to non-experts embed the loop. |
| R16 | §4.2.1 EEPROM boot check list | Today's `probe_bus` verifies model, mode, Phase bit 4 and the limit windows only. **Not** verified: `Max_Torque_Limit`, `Protection_Current`, `Overload_Torque`, `Return_Delay`. | Gap (VERIFIED) | Firmware verifies the full provisioned safety set. |
| R17 | Gate "teleop delay ≤ 20 ms p95" (§8 Phase 3) | Leader read → goal on the wire is 3.4–13.4 ms at 100 Hz (computed). The follower's position lag is dominated by the servo acceleration register (`WRITE_ACCELERATION` 50 ≈ 7.7 rad/s², which KNOWN-ISSUES already flags as lagging brisk replays). | Partly REFUTED (metric) | Measure **position lag by cross-correlation**, not wire latency. Teleop needs a higher acceleration setting. |
| R18 | Firmware update "over the same USB" (§4.2.9) | ESP-IDF: during flash writes the caches are off and "all other tasks are suspended" except IRAM-safe ISRs (spi_flash concurrency doc) | Addition | Update only when idle and limp or holding; never while moving. |

---

## 2. The V1 driver as a behaviour specification

### 2.1 What runs today (VERIFIED, `feetech_arm_node.py`)

* Two bus threads at `LOOP_HZ = 50`, one plain `threading.Lock` for the bus. The docstring explains the rate: "50 Hz, not 100: the usbipd/WSL2 tunnel jitters 100 Hz".
* **Read**: `_read_loop` → `_read_tick`: `sync_read(56, 6, ids 1..6)` (position, speed, load). It publishes `/joint_states` only when all 6 replied. A missing reply opens a **wall-clock** miss window; after `READ_FAIL_STOP_S = 1.0` it latches `_bus_fault` and aborts the trajectory. The latch clears only after a full read that also published.
* **Write**: `_write_loop` interpolates the active trajectory (`interpolate_trajectory`, linear). It writes the 7-byte block at register 41 (`Acceleration 50`, goal tick via `rad_to_command_tick`, `Goal_Time 0`, `Goal_Speed 2840`) **only while** a trajectory exists, torque is on and the bus is healthy. The buffer is dropped 1 s after its last point; when idle nothing is written and the servos hold.
* **Bus layer** (`feetech_bus.py::FeetechBus`): 1 Mbit/s, reply timeout 20 ms, `write_timeout 0.1 s`, `reset_input_buffer` before every write, sync-read deadline `timeout × (1 + n)` (140 ms for 6 servos with one absent), breaking at the first missing reply.

### 2.2 Rule-by-rule extraction and its future home

FW = firmware, ENG = engine (PC/Pi), BOTH = engine plans and judges, firmware enforces a floor.

| Rule (symbol) | Constants | Today | Future home | Notes |
|---|---|---|---|---|
| Boot probe: ping all, model ∈ {777, 2825} (`probe_bus`) | `SERVO_IDS` 1..6; `STS_ACCEPTED_MODELS` | 5 s retry loop before any ROS entity | **FW** | Also probe leader ids 11..16. Report per-id result; German text in ENG. |
| Provisioning fingerprint: `Operating_Mode` 0, Phase bit 4 clear, `Min/Max_Position_Limit` = `position_limit_window` ±1 | windows: 1024–3072, 2048–4095, 2048–4095, 796–3300, 1024–3072, 2048–3072 | refuse boot | **FW** | **Extend** to `Max_Torque_Limit` (800/150), `Protection_Current` 310, `Overload_Torque` 80, `Return_Delay` 0, `Response_Status_Level` 1, `Lock` 1 (R16). |
| Boot plausibility band (`BOOT_POSITION_TOLERANCE_TICKS`) | 400 ticks (env; ≥ 4096 disables) | refuse boot | **FW** | Compiled default; engine may tighten. |
| Angular resolution ≠ 1, non-uniform or suspect firmware (`angular_resolution_note`, `firmware_fingerprint_notes`) | `FIRMWARE_SUSPECT_VERSIONS {(3,9)}` | warn only | FW reads, ENG words | **Defect risk:** the uniformity warning compares *all* servos, but the V1 mixes STS3215 and STS3250 by design. If the two models ship different firmware, every healthy V1 warns (UNVERIFIED-BENCH B9). LeRobot even hard-raises (`_assert_same_firmware`). Make uniformity **per model**. |
| Torque-on edge refusal (`edge_parked_joints`), only on an OFF→ON transition or while the bus is faulted | `EDGE_REFUSE_MARGIN_TICKS` 40 (0 disables; ≥ 2048 → default) | refuse torque-on | **FW** | Pure integer rule; share test vectors. |
| Seed `Goal = Present` (7-byte block, `SEED_SPEED_STEPS` 200, acc 50) before `Torque_Enable` (`_seed_goal_from_present_locked`) | 200 steps/s | in the single torque choke point `set_torque` | **FW** | Exempt from the goal-edge clamp (must not create motion). |
| Post-energise wrong-way abort (`wrong_way_joints`, `_verify_after_energise_locked`) | `TORQUE_ABORT_ERROR_TICKS` 2048, strict `>`, plus "wrapped < naive"; `POST_ENERGISE_READ_ATTEMPTS` 2 | torque off at once | **FW** | Prebuilt payload, no allocation between verdict and write — natural in C. |
| Commanded-tick clamp (`rad_to_command_tick`) | `GOAL_EDGE_MARGIN_TICKS` 128, ceiling 130 | every goal write | **FW** | In tick space: `clamp(tick, m, 4095−m)`. The engine also clamps (so planning sees the same reach). |
| Register-40 guard (`assert_safe_torque_enable`), span test on writes and sync-writes | allowlist {0, 1} | `ValueError` | **FW** (codec level) | Value 2 = damping mode exists in the vendor table: an owner decision to widen (§7.2). |
| Trajectory refused while limp, while bus-faulted, or containing any non-finite value (`_trajectory_cb`) | — | drop + rate-limited warning | **FW** (integers: range checks) + ENG (finite floats) | Joints missing from a message are filled with the **measured** positions. Keep that, or make the wire always carry all joints (recommended). |
| Joint radian clamp to `JOINT_LIMITS_RAD` (`_write_targets`) | ±1.5708, [0, 3.1416] ×2, ±1.9198, ±1.5708, [0, 1.5708] | clamp | **BOTH** | FW: refuse a waypoint outside the EEPROM window by more than 0 ticks (an engine bug); keep the sanctioned edge clamp. |
| Second speed limit `Goal_Speed` 2840 + `Acceleration` 50 | 2840 steps/s ≈ 4.36 rad/s; 50 × 8.7 °/s² | written every cycle | **FW** (per mode) | Teleop needs a higher acceleration (R17); bench VR12. |
| Velocity floor (planning) | 0.6 × 4.72 rad/s; quintic peak 15/8 | `trajectory_builder` | **ENG** + FW step cap | FW refuses a chunk whose per-cycle step exceeds the compiled cap. Never clamp silently. |
| Read-fail latch | 1.0 s wall clock | latch, stop writes | **FW** | Per-reply timeout ≈ 0.5 ms instead of 20 ms, so a dead servo no longer stretches the cycle to 140 ms. |
| Status-bit telemetry (`describe_error_bits`) | 0x01 voltage, 0x02 sensor, 0x04 overheat, 0x08 overcurrent, 0x20 overload; unknown bits by value | warning 1/5 s/servo | FW reports, ENG words | — |
| Boot energise only (`boot_energise`), torque-on retries | `BOOT_TORQUE_ON_ATTEMPTS` 3, 1 s | at container start | **ENG** commands it, FW executes | The board never energises on its own (§6.8). |
| Activation glide to HOME: floor check (`boot_home_floor_decision`) and tower check (`boot_home_tower_decision`), refuse-only, arm stays torqued | `BOOT_HOME_FLOOR_TOL_M` 0.020; tower allowance and residual 26 mm | in `run_home` | **ENG** (`home_planner` + `fixture_guard`) | FW never generates a glide. The engine's planner is stronger (via search, calibrated tower). |
| Pull-in wait before judging (`_reading_after_pull_in`) | 0.25 s slack, 20 ms poll | tower only | **ENG** (reads STATE) | — |
| Boot-home arrival verify + one re-send (`_boot_home_verify`) | 0.30 rad, settle 0.7 s, `MAX_SENDS` 1 | own thread | **ENG** | FW sends `TRAJ_DONE`, so the "timed active claim" in `activation_agent._home_feetech` (3.5 s sleep) becomes an exact event. |
| Limp-fall direction (`limp_fall_direction`) | `LIMP_FALL_THRESHOLD_NM` 0.15 | every tower state | **ENG** | 38 µs in CPython per call (VERIFIED). |
| Tower state topic | 0.5 s period, re-judge 2 ticks | own thread | **ENG** | — |
| Shutdown torque-off (SIGTERM / atexit; entrypoint trap) | — | arm falls | **ENG** ("lay down, then release") + FW link-loss policy | §7.2. |
| Feetech activation = home only (`activation_agent.activation_steps`) | `FEETECH_HOME_WAIT_S` 20 | — | **ENG** | Becomes home → sync → teleop for the V1 with a leader. |
| Arm scan (`identify_arm.py::identify_feetech`) | contiguous ids from 1 to 7; tokens `feetech_silent`, `partial:K`, `bus_too_long:K`, `unknown` | in container | **FW** (HELLO_ACK lists ids, models, fingerprint) | The PC can no longer probe the raw bus. The board must report *why* (12 V off, switch on USB, partial chain). |
| Provisioning (`tools/feetech_provision.py::provision`) | 800/150/310/80/0; jig ±4 ticks; EEPROM reply timeout 0.2 s | bench tool on the raw port | **Tool** via the raw port (HAT USB-C1) or a gated firmware passthrough | Add `--role leader` (ids 11..16). |

### 2.3 State machines worth porting as-is

* **Torque choke point** (`set_torque`): read → edge guard (on transition) → seed write → `Torque_Enable 1` → re-read → wrong-way/missing ⇒ torque off and belief false. On an exception after the seed: try torque off, else name the 12 V supply. One function, in FW.
* **Trajectory rail**: a single writer (`_replace_trajectory`) with a generation counter, so long tasks can tell they lost ownership. The natural expiry does not bump the generation. Port the generation idea as **command ids** in the protocol.
* **Fault latch**: set by the read path only; cleared only by a complete, published read.

### 2.4 Computational weight (VERIFIED on this host, CPython 3.13, `time_geometry.py`, `count_geometry.py`)

| Function | Median | p95 | Max | Hot path (worst clear glide) |
|---|---|---|---|---|
| `tower_glide_decision` (807 fixture glides; 605 refused, matching the fixture) | 33.3 ms | 84.3 ms | 108.9 ms | ≤ 1 217 samples, ≈ 51 k sphere bounds, 2.5 k pre-checks, 2 k 15-axis SAT tests, 6.3 k box placements |
| … clear glides only (202) | 58.4 ms | — | 112.2 ms | — |
| `swept_lowest_z` (floor, same starts) | 9.8 ms | 10.2 ms | 12.1 ms | ≤ 256 samples |
| `lowest_z`, one pose | 38 µs | | | |
| `limp_fall_direction` | 38 µs | | | |

ESTIMATE for a float32 C port on an ESP32 (LX6, 240 MHz, single-precision FPU): ~15 Mflop + ~12 k trig + ~80 k sqrt for the worst clear glide, about **0.25–0.5 s**; in double precision (software-emulated on every ESP32) ~10× more. Espressif's own benchmark shows the cost: ESP32-S3 `cos` 121 cycles in hardware vs 2 377 on the FPU-less C3 (developer.espressif.com, "cores with FPU"). **Verdict: the engine must keep this.** The firmware has no use for it once it no longer plans glides.

### 2.5 Servo behaviours the firmware must assume

| F | Behaviour | Tag / source |
|---|---|---|
| F1 | `Torque_Enable` default 0 at servo power-up (SRAM). 2 = damping mode ("torque limit 48 sets damping force"). 128 = re-datum to 2048. | VERIFIED: Waveshare ST memory map; aifitlab STS table |
| F2 | No communication watchdog: the last goal is held while energised | VERIFIED: driver docstring; no such register in the vendor tables |
| F3 | A `Goal_Position` write may set `Torque_Enable = 1` by itself (with and without motion) | UNVERIFIED-BENCH: LeRobot #3585 (May 2026, out-of-window goal; Nov 2026, in-window goal, C001) |
| F4 | The position loop is not wrap-aware (naive `goal − present`) | VERIFIED: driver, bench datum 2026-07-26 |
| F5 | Built-in protections: overload (load > `Overload_Torque` 80 % for `Protection_Time` 2 s → output limited to `Protective_Torque` 20 %); over-current (> `Protection_Current` for `Overcurrent_Time` addr 38, default 2 s) → unload; overheat (`Max_Temperature` 70 °C) → unload | VERIFIED (defaults: Waveshare/aifitlab tables); unload bits from `Unload_Condition` default 44 per the driver comment in `feetech_bus.py` |
| F6 | A 12 V brown-out resets the servos → torque off → the arm falls | follows from F1 |
| F7 | Under load, transient sync-read corruption "typically when several joints move at once"; LeRobot's fix is bounded immediate retries | VERIFIED: LeRobot PR #3888 (superseded by #4207, merged) |
| F8 | Factory `Return_Delay` is 500 µs (register value 250) | VERIFIED: LeRobot v0.5.1 `feetech.py` comment |
| F9 | Non-default limit windows plus homing offsets can produce wrong-way moves (SO-101, C018, firmware 3.10) | UNVERIFIED-BENCH: LeRobot #3585 follow-up, Sep 2026 |
| F10 | `Protection_Current` factory default reads 511 (≈3.3 A) in the Waveshare/aifitlab tables, while `feetech_provision.py` calls the factory value "~2 A (310)" | CONFLICT → bench B9 |

---

## 3. Bus timing, computed

### 3.1 Wire arithmetic (VERIFIED from `feetech_bus.py` framing; `bus_timing.py`)

1 Mbit/s 8N1 = 10 µs per byte.
SYNC_READ (0x82) request = `8 + n` bytes (14 B for 6 ids, 20 B for 12).
Each status reply = `6 + L` bytes (12 B for L = 6, 21 B for L = 15).
SYNC_WRITE (0x83) = `8 + n(L+1)` bytes (56 B for 6 servos × 7 B; 26 B for goal only).

Unknowns: the per-reply turnaround **t_turn** (servo processing; `Return_Delay` 0 is the documented minimum, real latency unmeasured), and the host gap **t_gap** (assumed 20 µs on an MCU).

Fields the product needs:
* every cycle: position, speed and load (56–61, L = 6);
* health: voltage 62, temperature 63, status 65, current 69–70. Reading 56–70 contiguously (L = 15) gets everything in one transaction.

### 3.2 Cycle time (ms, t_gap = 20 µs)

| Configuration | t_turn 20 µs | 50 µs | 100 µs | 150 µs | 500 µs (factory) |
|---|---|---|---|---|---|
| Follower 6, L=6 + write 7 B | 1.58 | 1.76 | 2.06 | 2.36 | 4.46 |
| Follower 6, L=15 (all health) + write 7 B | 2.12 | 2.30 | 2.60 | 2.90 | 5.00 |
| Follower 6, L=15 + goal-only write | 1.82 | 2.00 | 2.30 | 2.60 | 4.70 |
| F6 + L6 in one SR of 12, L=6 + write F6 | 2.48 | 2.84 | 3.44 | 4.04 | 8.24 |
| F6 L=15 + L6 L=6 (two SRs) + write F6 | 3.12 | 3.48 | 4.08 | 4.68 | 8.88 |
| F6 + L6 one SR, L=15 + write F6 | 3.56 | 3.92 | 4.52 | 5.12 | 9.32 |

* Teleop on-board, 100 Hz: leader sample → follower goal on the wire **3.4–13.4 ms** (best/worst phase). At 200 Hz: 3.4–8.4 ms. The servo's acceleration profile comes on top.
* STATE stream (follower 6 with health, leader 6 positions, header): ≈ 108 B/frame → **10.8 kB/s at 100 Hz** (86 kbit/s).

### 3.3 Verdict

* **100 Hz on one bus with 12 servos is feasible with margin**: 34–45 % duty at t_turn 50–100 µs with full health reads.
* **200 Hz** needs lean reads (pos/vel/load only, health round-robin) or **two buses**.
* A servo that never answers must cost ≤ 0.5 ms, not today's 20 ms per-reply timeout × (1 + n).
* Unprovisioned servos (factory 500 µs return delay) double the cycle. The boot check must verify `Return_Delay` = 0 on **both** arms.

### 3.4 What others achieve, and the documented problems

* LeRobot teleop: host loop, `fps: int = 60` default, leader `get_action` + follower `send_action` + observation read every iteration (`lerobot_teleoperate.py` v0.5.1, VERIFIED). Recording defaults to 30 fps (UNVERIFIED in this session).
* Host-side sync-read timings found: 0.83 ms for 2 servos (feetech-cli PR #3, firmware 3.10) and 1.5 ms for 5 servos + a relay board (actuo PR #6). Both include adapter and host overhead (secondary sources).
* LeRobot problems that matter here:
  * load-dependent sync-read corruption → retries (#3888 → #4207);
  * factory return delay 500 µs → set 0;
  * a PyPI SDK packet-timeout bug patched by LeRobot (`patch_setPacketTimeout`);
  * firmware uniformity hard-raise;
  * P coefficient lowered 32 → 16 "to avoid shakiness";
  * gripper `Max_Torque_Limit` 500, `Protection_Current` 250, `Overload_Torque` 25 "to avoid burnout" (`so_follower.py` v0.5.1);
  * goal-write auto-torque (#3585, F3).
* The "FW 3.9 corrupts SYNC_READ" claim stays unproven (the driver's own review of LeRobot #1010).

### 3.5 STS3250 vs STS3215 (12 V)

| | STS3215-C018 | STS3250-C001 |
|---|---|---|
| Stall torque | 30 kgf·cm ≈ 2.94 N·m | 50 kgf·cm ≈ 4.90 N·m |
| Stall current | 2.7 A | 4.2 A |
| No-load current | 180 mA | 280 mA |
| No-load speed | 0.222 s/60° ≈ 4.72 rad/s | 0.133 s/60° ≈ 7.87 rad/s |
| Control table | STS/SMS | same (LeRobot `MODEL_CONTROL_TABLE`) |

Sources: vendor listings (robotshop/openelab), secondary. The speeds match `robot_profiles._V1_STUDIO` ("STS3215 at 4.72 rad/s while joint2/3 are STS3250 at 7.87").

What matters:
* Over-current trip 2.015 A = 48 % of the 3250's stall current vs 75 % for the 3215. The 3250 joints trip relatively early (finding 7).
* Possible per-model firmware versions (§2.2).
* There is no protocol or timing difference documented.

### 3.6 Bench to close the unknowns: B1 `bench_bus_timing`

A firmware diagnostic command runs 10 000 cycles per configuration of §3.2 at 100 and 200 Hz, arm still and arm moving all joints. It reports:
* per-transaction duration (µs, `esp_timer`) and per-reply first-byte offset (UART RX timeout events);
* missed and corrupt replies;
* a logic-analyser capture of the DATA line for 50 cycles: t_turn per servo, rise times with 12 servos and 2 m of cable (the 10 kΩ pull-up R11 is the slow edge).

Pass:
* p99 cycle ≤ 5 ms with 12 servos and full health;
* corrupt replies < 10⁻⁴;
* t_turn reported.

---

## 4. Board facts and choice

### 4.1 Waveshare Bus Servo Driver HAT (A) — from the schematic (VERIFIED unless tagged)

| Block | Finding |
|---|---|
| MCU | "ESP-WROOM-32" (product page: ESP32-WROOM-32). Flash size not stated (WROOM-32 modules are commonly 4 MB, UNVERIFIED). Espressif marks ESP32-WROOM-32 **NRND** (datasheet v3.6); Secure Boot V2 / the RSA app-signing scheme need **chip rev ≥ v3.0** (ESP-IDF Secure Boot V2 doc). The HAT's chip revision is unknown → read it on the bench (B9). |
| ESP32 USB-C (C2) | **CH343P** U2 ↔ UART0 (TXD/RXD via 22 Ω). DTR/RTS → two S8050 auto-program transistors (EN/IO0). Table on the sheet: DTR,RTS = 1,0 → EN 0 (reset); 0,1 → IO0 0. |
| Servo USB-C (C1) | **CH343P** U1 → `USB_TXD_485/USB_RXD_485` → SW1 position B → servo bus (raw, no MCU) |
| SW1 | DPDT slide: `Bus_TXD` ↔ IO19 \| USB_TXD_485; `Bus_RXD` ↔ IO18 \| USB_RXD_485. The ESP32 and the raw USB port cannot collide electrically, but either can be cut off by a flip. |
| TTL bus | `Bus_TXD` = U1TXD drives both the TTL and the RS485 transmitter. TTL: Q1 MMBT3906 makes TXEN = NOT(TX); U4 SN74LVC1G126 (OE = TXEN) drives DATA low; U6 SN74LVC1G125 (OE# = TXEN) passes DATA to RX only while we are not transmitting. R11 10 kΩ pull-up to 3.3 V. TVS (LTVS16H5.0ET5G) on DATA. Two 3-pin headers H2/H3 (DATA, VIN, GND) on the same bus. **No direction GPIO, no echo.** |
| RS485 | SP3485EN, own PNP auto-direction, fixed 120 Ω termination, 4.7 kΩ bias, PTC fuses F1/F2, SM712 + SMAJ TVS, 4-pin P1 (A, B, VIN, GND), 2 × 220 µF/35 V on VIN. `Bus_RXD` = AND(TTL RX, RS485 RX) (U8 SN74LVC1G08). |
| Servo power | XT60PW-M (H1), DC005 5.5×2.5 jack (DC1) and screw terminal DG128-2P (J1, an output) all on net "24V". SMAJ26CA TVS. LL4148 across the rail. 10 µF + 0.1 µF at the headers. **No fuse, no reverse-polarity FET, no switch, no current sense.** |
| 5 V for a Pi | MP9447 sync buck (MPS: 5 A, 36 V) from VIN, FB 0.815 V with 56 k/10 k ⇒ ≈ 5.38 V; L4 10 µH "8A"; AO4407A PMOS + 2× MMBT3906 ideal diode → Pi header pins 2/4 |
| Logic power | VBUS of **both** USB-C (tied together) and the buck output, diode-ORed (D1/D2) → SGM2212-3.3 LDO → 3V3. **The ESP32 runs on USB alone** (servos unpowered); a 12 V loss with USB attached does not reset the ESP32. |
| Pi header | Pi 3V3 pins not connected; Pi TXD/RXD ↔ ESP32 UART0, **in parallel with CH343 U2** (contention if a Pi and the PC both talk) |
| Input range | 9–25 V; "input = servo voltage" (wiki, product page) |
| Mechanical | 65 × 57 mm, holes 3 mm on 58 × 49 mm, 82 g |
| Package / price | HAT, 2×20 header, USB-A to C cable, screws; $18.99 (tiers to $17.97) |

### 4.2 HAT (A) hazards for a firmware that owns safety

| H | Hazard | Severity | Mitigation on the HAT |
|---|---|---|---|
| H1 | SW1 flipped while energised: the firmware loses the bus (cannot torque off). In "USB" position the bus is raw: any program on C1 bypasses every guard. | High | Fix the switch (adhesive cover / glue). FW names "Schalter auf der Platine?" when all replies vanish. Document C1 as bench-only (provisioning). |
| H2 | Port open/close toggles DTR/RTS → possible ESP32 reset or download mode (classic auto-program circuit). The CH343 stays enumerated, so the COM port survives the MCU reset. | Medium | Engine opens with DTR/RTS deasserted before open; FW is reset-tolerant (adopt and hold) and reports the reset reason. Bench B3. |
| H3 | No fuse or reverse-polarity protection on the servo rail; DC005 jack of unknown rating | Medium | Ship an XT60 12 V supply only; never the barrel jack. |
| H4 | Single shared bus for leader and follower: one shorted cable kills both; the leader hot-plug inrush can dip the rail and reset the follower (the arm falls) | Medium | "Leader only with power off" until bench B2(d) proves otherwise. Ids 11..16 for the leader. |
| H5 | NRND module; chip revision unknown (Secure Boot V2 needs rev ≥ 3.0) | Low (now) / Medium (production) | App-level signature checks work on any revision (§5.5). |
| H6 | UART0 shared by USB-C2, the Pi header and the ROM boot log (115200 at reset) | Low | Engine resyncs on COBS delimiters; FW routes logs into protocol frames. |
| H7 | No current sense on the rail: a shorted chain or a sagging supply is visible only through per-servo `Present_Voltage` / `Present_Current` | Low | Read health ≥ 10 Hz. |

### 4.3 Power budget (ESTIMATE from vendor numbers)

* Theoretical simultaneous stall at the provisioned 80 % PWM: 2 × (0.8 × 4.2) + 3 × (0.8 × 2.7) + 0.15 × 2.7 ≈ **13.6 A**.
* Over-current protection (2.015 A per servo after 2 s) bounds sustained draw to ≈ 12 A.
* Leader arm limp: ≈ 6 × 24–30 mA idle.
* Waveshare's own sibling board states "5A maximum for long-term operation" and that five ST3215 work "if they don't stall at the same time" (General Driver for Robots wiki). That is a vendor's own sizing hint at a lower load.
* Recommendation: a **12 V supply of at least 8 A (≈ 100 W) on XT60**.
* Daisy-chained 3-pin cables carry the whole downstream current through the first connector. Measure the per-servo voltage drop under stress (B2), and split the chain across H2/H3 if needed (UNVERIFIED-BENCH).

### 4.4 Alternatives

| Board | MCU | USB path → Windows driver | Radios | Bus driver | Power path | Protections | Price / availability | Verdict |
|---|---|---|---|---|---|---|---|---|
| Waveshare Bus Servo Adapter (A) (today) | none | CH343 `1A86:55D3` → CDC/usbser | — | TTL + RS485 (circuit not read) | 12 V pass-through | — | in use | Only for a PC-side driver (§4.5). |
| **Waveshare Bus Servo Driver HAT (A)** | ESP32-WROOM-32 (NRND), 2 cores, FPU | 2 × CH343P → inbox CDC (vendor) | Wi-Fi 4, BT 4.2 (BLE 1M PHY only) | auto-dir TTL + RS485, one bus | 9–25 V XT60 / DC005; 5 V/5 A for a Pi | none on the servo rail | $18.99, in stock | **Prototype now**; production only with H1–H4 mitigations. |
| Waveshare General Driver for Robots | ESP32-WROOM-32UE (rev-3 class) | 2 × CP2102 → Silicon Labs VCP (not inbox; Windows Update, UNVERIFIED-WINDOWS) | Wi-Fi 4, BT | ST bus | 7–13 V on XH2.54; servo output "5A maximum", switched | INA219 V/I, servo power switch | listed | No: 5 A servo path, CP2102 driver risk, motor drivers unused. |
| Seeed XIAO Bus Servo Adapter | XIAO ESP32-C3 (1 core, no FPU) | native USB-Serial-JTAG CDC → inbox | Wi-Fi 4, BLE 5 | not documented | 5–12 V, 5.5×2.1 jack | not documented | listed | No: single core shared with radios, no FPU, weak power path. |
| Seeed Bus Servo Driver Board + XIAO ESP32-S3 | ESP32-S3 (2 cores, FPU) | native CDC → inbox | Wi-Fi 4, BLE 5 | not documented | 5–12 V jack | not documented | listed | Possible quick S3 bring-up; not a product. |
| **Custom EduBotics controller (ESP32-S3-WROOM-1)** | S3, 2 × LX7 240 MHz, FPU, 512 KB SRAM | native USB-C → inbox CDC; no DTR/RTS transistors | Wi-Fi 4, BLE 5 (2M PHY, L2CAP CoC), module RF-certified | **2 independent buses** (3 UARTs on the S3) with TVS + series R | XT60/XT30, ideal-diode reverse protection, eFuse/load switch per bus port with current limit and soft-start, INA228-class V/I monitor | servo-power cut, stop button input, status LED | ESTIMATE €12–25 at 100–500 pcs + EMC test cost | **Production target.** |
| Custom with RP2350 (+ CYW43439 module) | 2 × M33 with FPU, PIO | native USB | Wi-Fi 4, BLE 5.2 (separate radio chip) | PIO half-duplex | as above | as above | — | Viable, but a new ecosystem for no decisive gain over the S3 (UNVERIFIED details). |

Chips **without** an FPU: ESP32-S2, C2, C3, C5, C6, C61, H2. **With** one: ESP32, S3, H4, P4 (Espressif blog, VERIFIED). The C6's single core and soft-float make it the wrong host for a 100 Hz loop next to radio stacks.

### 4.5 Recommendation

**Firmware on a controller board, not a PC-side driver.**

The PC-side alternative (today's adapter, engine loop on Windows) would work functionally; LeRobot proves 60 Hz host loops. But it leaves four things the product needs to the mercy of Windows scheduling, GIL contention in a busy engine, endpoint protection and USB suspend:
* smooth execution;
* the collision stop latency;
* teleop latency;
* any stop at all when the PC freezes.

Keep a **"soft board"**: the same protocol implemented in Python over a plain adapter, as a dev, CI and fallback path with documented weaker guarantees.

**Board**:
* HAT (A) for Phases 1–3 (2–3 units), firmware written against a HAL.
* Decide the production board at the Phase-2 gate: custom S3 (recommended) or HAT (A) with H1–H4 mitigations if time to market dominates.

---

## 5. Transports: roles, figures, security

### 5.1 Per transport

| Transport | Latency / jitter | Throughput | Can carry | Should carry |
|---|---|---|---|---|
| USB CDC (CH343 at 921 600–2 000 000 baud, or S3 native full-speed) | ~1 ms USB frame + driver; host jitter dominates (Windows) | 90–200 kB/s (bridge); native S3 more | everything | **Primary for all roles in v1**: commands and trajectories, STATE 100 Hz (10.8 kB/s), events, firmware update (1.5 MB in ~8–17 s), provisioning passthrough (gated) |
| BLE GATT (ESP32 classic = BLE 4.2, 1M PHY) | iOS connection interval ≥ 15 ms (Silicon Labs, citing Apple ADG R21 §49.6); ±15 ms jitter plus retransmits; supervision timeout seconds | ~20–60 kB/s realistic (forum reports: ~46–102 kB/s on S3 with 2M PHY and DLE) | STATE ≤ 30 Hz, commands/trajectories, identify, provisioning | identify/blink, Wi-Fi credential provisioning, an authenticated iPad client (later) |
| BLE L2CAP CoC | same link timing; stream semantics, credit flow control | similar or better than GATT; iOS 11+ `CBL2CAPChannel`; Espressif lists ESP32/C3/S3 as CoC targets, MTU up to 512 | as GATT, cleaner framing | the iPad app's data channel (preferred over GATT) |
| Wi-Fi STA (school WLAN) | LAN ms, spikes 100 ms+ | Mbit/s | everything | **not primary in schools**: client isolation, 802.1X, captive portals, mDNS across VLANs. Optional for labs/home. |
| Wi-Fi SoftAP | ms | Mbit/s | everything | not default: an iPad on the arm's AP loses internet; beacon load (§5.3) |
| ESP-NOW | ~1–6 ms per hobbyist measurements; heavy loss in crowded 2.4 GHz (forum and Hackaday reports) | small frames | wireless leader→follower | demo only, never default; loss means stutter and watchdog stops |

### 5.2 iPad reality

* Safari has no Web Serial, no Web Bluetooth, no WebUSB (caniuse; WHATWG implementation status), so a web page on an iPad cannot reach the board directly.
* iPadOS offers apps no public USB-CDC API without MFi or a DriverKit entitlement (UNVERIFIED in this session).
* An https-served cloud page cannot open `ws://` to a LAN device (mixed content, UNVERIFIED for every iPadOS version).
* The board cannot serve the React UI: the V1 meshes alone are 3.6 MB per CLAUDE.md, and the ESP32 has 4–8 MB of flash.
* **Options**: (a) a native iPad app (Swift/CoreBluetooth or a WKWebView wrapper with a native BLE plugin) for direct arm work; (b) the iPad as a browser client of an engine host (PC/Pi) on the LAN, which already works today in Pi mode.
* Recording, training and cloud inference need the Python engine (LeRobot, PyAV) and cannot realistically run on iPadOS. That belongs to the engine architect.
* **Firmware implication**: keep the protocol transport-agnostic. Never assume a BLE MTU above 20 bytes (an iPad forum report shows exactly that). iOS negotiates the MTU itself (typically 185/247; `maximumWriteValueLength` = MTU − 3).

### 5.3 Classroom density (ESTIMATES)

| Scenario | Arithmetic | Result |
|---|---|---|
| 30 SoftAPs, beacons at 1 Mbit/s DSSS | ≈ 250 B beacon ≈ 2.19 ms; 30 × 9.77/s × 2.19 ms | ≈ **64 %** of one channel |
| … beacons at 6 Mbit/s OFDM | ≈ 0.35 ms each | ≈ 10 % |
| 30 boards advertising BLE every 200 ms | ADV_IND ≈ 376 µs; 30 × 5/s × 0.376 ms per channel | ≈ 5.6 % per advertising channel |

(ESP32's default SoftAP beacon rate is UNVERIFIED.) SoftAP-per-arm is incompatible with a school WLAN; BLE advertising is fine.

### 5.4 Radio vs the 100 Hz loop on ESP32 / S3

* Interrupts are allocated on the core that allocates them ("Allocating an external interrupt will always allocate it on the core that does the allocation", ESP-IDF intr_alloc).
* Layout: bus task and UART ISR on core 1; Wi-Fi task, BT controller, NimBLE host and the link task on core 0. Radio interrupts then stay on core 0.
* The real disturbers:
  1. **Flash writes** (OTA, NVS, core dumps) disable the caches and suspend all non-IRAM tasks on both cores ("all other tasks are suspended … The other core will be polling in a busy loop"). The bus loop stalls for every sector erase.
  2. Shared flash-cache misses: µs-level.
  3. Any allocation inside the loop (forbid it).
* The rule follows: **no flash writes while energised and moving**, and the loop must not allocate.
* Radio coexistence (Wi-Fi and BT time-sharing one radio) affects radio throughput, not UART timing.
* Proof obligation: a board-side jitter histogram with radios active (B11).

### 5.5 Security design (needed the day any radio is enabled; partly for USB too)

* **Trust boundary on USB**: physical. COM ports are exclusive-open; a session id in HELLO keeps a stale process from steering the board. No crypto needed.
* **Radio pairing and mis-association** (30 identical arms in one room):
  * a per-device static key pair generated at provisioning, its fingerprint and a short id printed as a QR label on the kit;
  * "identify" (LED blink plus a buzz from a 1-tick servo nudge — owner decision) before control is granted;
  * one controlling session at a time.
* **Authenticated encryption on every radio transport**: an app-layer Noise handshake (NK/XX with the QR-known static key; ChaCha20-Poly1305 or AES-GCM) with 64-bit counters as nonces, which also gives **replay protection**. The same inner frames as USB. BLE LE Secure Connections alone (Just Works) does not stop mis-association.
* **Teacher stop/fleet view**: read-only status in BLE advertisements. A "stop all" broadcast signed with a classroom key and a counter (otherwise any phone can mount a DoS). Never sold as an e-stop; it is a convenience.
* **No open or default-password hotspot**: EN 18031 was harmonised *with restrictions* on default passwords (EU Implementing Decision 2025/138, per SGS and Intertek summaries).
* **Firmware updates**:
  * signed images; ESP-IDF supports verifying signed apps without hardware secure boot (`CONFIG_SECURE_SIGNED_APPS_NO_SECURE_BOOT`, RSA scheme needs rev ≥ 3.0). An app-level Ed25519/ECDSA check in our updater works on any revision;
  * **app rollback** (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`: a new image boots `PENDING_VERIFY`; a reset before `esp_ota_mark_app_valid_cancel_rollback()` falls back);
  * eFuse anti-rollback has only **32** steps on ESP32 — use a soft monotonic version in NVS first.
* **Anti-brick**: the ROM serial bootloader stays reachable through the bridge (HAT) or USB-Serial-JTAG (S3), so a failed update is always recoverable by the app.
* **Secure boot and flash encryption trade-off**:
  * Without Secure Boot V2 plus secure download mode, anyone with esptool and the kit can flash firmware that drops every limit. Physical access already lets them move the arm, but not make it *run away under program control*.
  * With them, the signing key becomes a fleet-critical secret, and eFuse burning on a third-party board is irreversible.
  * Recommendation: no eFuses on HAT prototypes; Secure Boot V2 + secure download mode on the production board, at the factory step; flash encryption only if keys on the board must stay secret.
* **Licences**: esptool is GPL-2.0-or-later (Espressif docs). For in-app flashing use Espressif's C `esp-serial-flasher` (licence UNVERIFIED — check the repo LICENSE) or `espflash` (MIT/Apache-2.0), or bundle esptool with its source offer. The firmware also brings ESP-IDF, FreeRTOS, mbedTLS, NimBLE and lwIP notices into `THIRD_PARTY_NOTICES`.

### 5.6 Regulatory (UNVERIFIED-legal; needs a compliance consultant)

* **RED (2014/53/EU)**. Reusing a pre-certified module's RF reports covers Art. 3.2 if the module's integration conditions are kept (antenna, PHY settings). The final kit still needs its own conformity assessment: 3.1(a) safety, 3.1(b) EMC, CE marking under our name. Reflashing a Waveshare board and selling it in our kit makes us the manufacturer of that product.
* **Delegated Regulation (EU) 2022/30** (RED 3(3)(d) network protection, (e) personal data, (f) fraud) applies from **2025-08-01**. EN 18031-1/-2/-3 are harmonised with restrictions, so self-assessment is only available where the restricted clauses are not relied on (SGS, Intertek summaries). Whether a board whose firmware never initialises the radio is still "internet-connected radio equipment" is a legal question.
* **Cyber Resilience Act (EU) 2024/2847**: vulnerability and incident **reporting duties apply since 2026-09-11**, also for products already on the market; the main obligations from **2027-12-11** (CMS, Kirkland, Freshfields summaries). This concerns the whole EduBotics product with digital elements, not only the radio. Design now for signed updates, a support period and vulnerability handling.
* The **Machinery Regulation (EU) 2023/1230**, applicable from January 2027, may scope a robot arm (UNVERIFIED).

---

## 6. Firmware architecture

### 6.1 ESP-IDF, not Arduino

ESP-IDF (pinned v5.x LTS, built in CI from a digest-pinned `espressif/idf` image) gives:
* explicit core pinning and priorities;
* the OTA/rollback/partition machinery;
* NimBLE with L2CAP CoC;
* secure boot and NVS encryption;
* menuconfig-reproducible builds.

Arduino-ESP32 sits on ESP-IDF but hides exactly these controls.

Language: C11 for the portable core (§6.9); C++ optional at the edges.

### 6.2 Responsibility split

**Firmware = a real-time execution and safety kernel:**
* bus I/O at a fixed rate;
* full provisioning check at boot (both roles);
* adopt-and-hold boot;
* energise (edge guard → seed → torque on → post-energise check);
* time-stamped waypoint executor with splice semantics and a stop generator;
* window, edge, step and velocity guards (refuse, plus the sanctioned edge clamp);
* tracking-error/wrong-way watchdog (new, owner decision);
* link and underrun watchdog;
* teleop (proximity-gated enable, step limit, collision detector);
* health monitor;
* STATE and EVENT stream with board timestamps;
* signed update with rollback;
* optional radio roles.

**Engine = authority and planner**, everything else:
* kinematics, IK, every geometric guard (floor ladder, tower guard, no-go zones);
* activation sequence (home planned by `home_planner`, the sync glide, teleop enable);
* recovery state machines;
* German text;
* recorder on board timestamps;
* cloud schedule pushing chunks with splice times;
* Roboter Studio;
* provisioning tool;
* update orchestration.

### 6.3 Tasks (dual core)

| Core | Task | Priority | Duty |
|---|---|---|---|
| 1 | `bus_task` (timer-notified every 10 ms) | highest | sync-read → decode → guards → source → goals → sync-write → publish a STATE snapshot (lock-free double buffer). No allocation, no logging, no flash. |
| 1 | UART ISR (servo bus) | ISR | installed from core 1 |
| 0 | `link_task` | high | frame decode, validation, command queue to `bus_task`, STATE/EVENT TX (non-blocking; **drop, never block**, when the host stops reading) |
| 0 | radio tasks (off by default) | per IDF | Wi-Fi, BT controller, NimBLE host |
| 0 | `svc_task` | low | health aggregation, OTA writes (only in UPDATE state), NVS |

Task watchdog on `bus_task` at 100–200 ms → panic → reset → adopt-and-hold.

Memory: a 512-waypoint ring of int16 × 6 + µs time ≈ 8 KB, static. Wi-Fi + NimBLE need ~100–150 KB of the ESP32's ~320 KB usable DRAM (UNVERIFIED budget figure).

### 6.4 Half-duplex driver

* HAT: plain `UART_MODE_UART`; direction is automatic in hardware.
* Custom board: `UART_MODE_RS485_HALF_DUPLEX` with RTS as DE, or the same auto-direction circuit per bus.
* Per transaction: flush RX → write → `uart_wait_tx_done` → parse replies incrementally against a per-cycle deadline.
* Missing ids get one immediate per-id READ retry in the same cycle (LeRobot's lesson, F7), then they are stale.
* Wall-clock fault latch as today.
* Start with the IDF driver and measure (B1). Write a lean ISR/DMA driver only if p99 misses.

### 6.5 Waypoint executor (integer)

* **Waypoint** = (board time µs, int16 ticks[6]).
* **Interpolation**: linear in ticks with int64 intermediates. Bit-exact with Python, so shared vectors need no tolerance.
* **Splice**: `TRAJ{cmd_id, splice_at_us (0 = now), points…}`. Points at or after the splice time replace the queued tail. Before the first new point the executor interpolates **from the current commanded goal**, not "jump to the first point". That removes today's chunk-boundary bump; it is a behaviour change, so the engine's pacing stays and VR12 measures it.
* **Validation, whole chunk refused on any failure**:
  * time strictly increasing;
  * every tick inside the EEPROM window;
  * per-10-ms step ≤ cap (compiled from 0.6 × the servo's rated speed);
  * buffer capacity respected.
* **Stop generator**: on STOP or watchdog, decelerate each joint from its commanded velocity at the compiled deceleration (`d = v²/2a` in ticks), then hold. The driver's own comment shows why: today a truncated trajectory pins the goal ~57 ticks ahead of a joint whose stopping ramp is ≈ 807 ticks.
* **Why integers** (`float32_check.py`): float32 rad maths is ≤ 1 tick off (fine for motion, fatal for shared vectors); float32 seconds-since-boot lose resolution (244 µs ulp at 1 h); uint32 µs wraps every 71.6 min (use int64 internally, uint32 deltas on the wire); rounding rules differ at ties.

### 6.6 STATE and time

* Every cycle: board µs at read completion; mode and source; torque mask; fault flags; buffer fill and horizon; last executed `cmd_id`; follower pos/vel/load; leader pos; health (voltage, temperature, current, status) for all servos at ≥ 10 Hz or round-robin.
* The engine maps board time with the existing `capture_timeline.py::ClockMap`, which already works for any stamp domain: `t = stamp + min(arrival − stamp)` over 2 s, clamped to `[arrival − 0.5 s, arrival]`. Leader and follower samples of one cycle are then within ~2–3 ms of each other: better action/state alignment than today's arrival stamps.

### 6.7 Protocol

**The brief's draft** (`COBS + type | seq | len | payload | crc32`) is a good skeleton. Missing pieces:

| Gap | Fix |
|---|---|
| No version per frame | version/flags byte |
| `len` redundant with COBS | keep as a cross-check |
| No ack or command identity | per-command `cmd_id` (u32), acked in STATE/EVENT; duplicates re-acked, never re-applied (last 16 ids kept) |
| Float SI units | int16 ticks + µs |
| No flow control | STATE advertises free slots and horizon; TRAJ beyond capacity refused whole |
| No splice semantics | §6.5 |
| Capabilities as a fixed bitmask | TLVs: buses, ids/models per role, compiled ceilings, chip revision, reset reason, rollback state |
| No replay protection | needed only on radio: the AEAD session layer (§5.5) |
| Fragmentation | COBS stream chunked to MTU − 3 on GATT; one frame per SDU on L2CAP CoC |

**CRC**: CRC-32/ISO-HDLC, the same as Python's `zlib.crc32` and the ESP ROM's `esp_rom_crc32_le` (ROM function name UNVERIFIED), so the vectors are trivial.

Proposed frame inside COBS (+ `0x00`), little-endian:
`ver_flags u8 | type u8 | seq u16 | len u16 | payload[len] | crc32 u32`

Messages:
* host → board: `HELLO`, `CONFIG` (tighten-only against compiled ceilings), `ENERGISE` / `TORQUE_OFF` / `DAMP` (if approved), `TRAJ`, `STREAM` (1-point setpoint with duration, for inference), `MODE` (HOLD / TELEOP_ON with proximity check / TELEOP_OFF / HANDGUIDE), `STOP`, `HEARTBEAT`, `PASSTHROUGH` (torque off + explicit), `DFU_*` (offset-acked, sha256, signature);
* board → host: `HELLO_ACK`, `STATE`, `EVENT` (traj_done/refused (reason), collision, watchdog, bus_fault (ids), provisioning_fault (id, register), status_bits, boot (reset reason)), `LOG` (English, rate-limited).

**Codec choice**:
* Fixed little-endian structs, **generated from one schema table** in the repo, in the style of `workflow/robot_api.py` (one table → C header + Python codec + JSON test vectors, later TypeScript/Swift), append-only evolution. Easiest to bound, fuzz and audit.
* nanopb/protobuf is the mature alternative if a Swift app becomes real (codegen for every language, fuzzed decoders) at the cost of variable sizes.
* CBOR (zcbor) is flexible but weakest at schema enforcement.
* Recommendation: generated fixed structs now; protobuf only if the iPad app is approved.

### 6.8 Firmware state machine

```
BOOT ──read-only probe──▶ PROBED{limp | adopted-hold | FAULT_PROVISION | FAULT_BUS}
                              │ HELLO
                              ▼
LIMP ──ENERGISE (edge→seed→on→verify)──▶ HOLD ◀──────────────── STOPPING ◀── STOP / underrun / link loss
  ▲                                       │ TRAJ/STREAM ──▶ MOVING ──┘
  │                                       │ MODE TELEOP_ON (|leader−follower| ≤ 0.30 rad) ──▶ TELEOP ──trip──▶ COLLISION_HOLD
  └──TORQUE_OFF / wrong-way / post-energise fault ◀── any       │ MODE HANDGUIDE ──▶ LIMP_FOLLOWER (no goal writes, F3)
UPDATE: only from LIMP or idle HOLD with an engine command; flash writes; reboot → PENDING_VERIFY → self-test → valid | rollback
```

Rules:
* Nothing moves before ENERGISE + a motion command.
* The board never energises on its own; the engine sends ENERGISE right after connect, which preserves today's "torque comes up with the environment, nothing moves".
* No goal write to any servo outside the torque-on set.

### 6.9 Testability without hardware

* `firmware/core/` is pure C with no ESP-IDF includes: codec, executor, guards, collision detector, teleop mapper, FSM, health logic. Built natively in CI with gcc/clang, UBSan/ASan, a libFuzzer target on the frame decoder (≥ 10⁸ execs per release) and unit tests.
* **Shared vectors** (`*-cases.json`, the repo's `code-insert-cases.json` pattern): Python generates cases from today's pure functions. Port into ticks: `edge_parked_joints`, `wrong_way_joints`, the tick part of `rad_to_command_tick`, the probe fingerprint, interpolation, splice and stop outcomes. The C runner must match bit for bit.
* **Servo-bus simulator** (C, linked through the HAL):
  * register map with EEPROM/RAM and `Lock`;
  * goal clamp to the window; naive wrap-unaware P-loop; F3 auto-torque on goal write;
  * sync-read reply order and turnaround;
  * fault injection: drop, corrupt, brown-out reset, overload bit.
* **Espressif QEMU** boots the real image (ESP32 and C3 documented; S3 support UNVERIFIED), with the bus UART on a socket to the simulator.
* **HIL per release**: a bench arm + board + logic analyser, running the scripted checklist (§10).

### 6.10 Update flow, app-driven

1. HELLO reports version, git sha, partition and rollback state, plus chip revision.
2. The engine compares with the firmware shipped inside the app; firmware hashes are allowlisted, the same discipline as the trainer image digest pin.
3. The engine asks in German, requires the arm laid down and limp, outside recording and teleop.
4. It streams the signed image in offset-acked chunks. The firmware verifies the signature and hash, then switches the boot partition.
5. Reboot → `PENDING_VERIFY` → self-test (bus probe OK, HELLO from the engine within N s) → mark valid; otherwise automatic rollback.
6. Never while moving (§5.4).

---

## 7. Safety design

### 7.1 Guard map (Rule §2 rows that concern the V1)

| Guard today | New home |
|---|---|
| EEPROM fingerprint, plausibility band, edge refusal, seed, post-energise abort, goal-edge clamp, register-40 guard, refuse-while-limp/faulted, read-fail latch | **FW**, same constants, shared vectors |
| Provisioned safety EEPROM not verified at boot | **FW verifies it (new)** |
| Torque-off on SIGTERM/atexit (the arm falls) | **ENG** "Arm ablegen" (a planned move to a resting pose, then release) + FW link-loss policy (§7.2) |
| Activation gate (`EDUBOTICS_REQUIRE_ACTIVATION`) | **ENG** decides; FW refuses motion before ENERGISE + command |
| Activation-glide floor and tower checks | **ENG** (`home_planner`, `fixture_guard`); FW never plans |
| Teleop collision e-stop (OMX only today) | **FW** detector (new for the V1, owner sign-off) + **ENG** student-paced recovery |
| Velocity floor | **ENG** plans; **FW** refuses over-fast chunks |
| Workflow guards, motion-lock policy, manual arbiter, code-runner conditions, Cloud-Inferenz rules | **ENG**, unchanged; FW adds a hardware floor (buffer horizon, validation, watchdog) |
| New: continuous tracking-error / wrong-way watchdog (the "assessed, not implemented" residual in `_verify_after_energise_locked`) | **FW**, owner decision: `|goal − present|` naive error > 2048 the long way ⇒ torque off that joint; a tracking error over a bound for > N ms while moving ⇒ stop + report |
| New: health thresholds (temperature, current near trip, voltage sag) | **FW** detects, **ENG** words |
| New: stop button (HAT: repurpose BOOT/IO0 at runtime; custom: a proper input) | **FW** |

### 7.2 Hard cases and the staged response

| Event | What physics and the servos do | Firmware response |
|---|---|---|
| ESP32 reset (TWDT, panic, BOD, DTR/RTS glitch) | servos keep holding the last goal (F2) | Boot read-only; adopt the hold (no writes to limp servos); EVENT boot(reason); the engine fails the running program in German. **No torque-off at boot** (R2). |
| 12 V brown-out | servos reset → limp → the arm falls (F1, F6); the ESP32 survives on USB power (HAT) | Detect (replies vanish, then reappear with `Torque_Enable` 0 / low voltage) → FAULT, no automatic re-energise; the engine guides recovery. |
| USB unplug / app crash / PC freeze | — | Moving: execute validated waypoints until **link silence ≥ T_link** (proposal 1.0 s, above the 500 ms stall the brief must tolerate and equal to today's ~1 s chunk overshoot), or until underrun → decelerate → HOLD, latched (a new ENERGISE/command is needed to move). Teleop: continue up to 2 s, then HOLD (owner decision). |
| Long unattended HOLD (thermal: j3 at HOME ≈ the trip, §0 finding 7) | the servo's own over-temperature/over-current unload drops the arm at an arbitrary moment | After `T_idle` (proposal 120 s) or a temperature warning: **damping release** (`Torque_Enable 2`, the vendor's damping mode) if bench B8 shows a slow, controlled settle; otherwise hold and let the engine's "Ablegen" be the normal path. Owner decision (D7 refined). |
| Radio link loss (if enabled) | — | Same as USB link loss; BLE supervision timeouts are not the watchdog. |
| Firmware hang | servos hold the last goal (≤ one 10 ms step ahead) | TWDT → reset → adopt. |
| Wrong goals (engine bug) | — | Chunk refused whole (window, step, time order). |
| Wrap-around runaway (F4) | the joint drives the long way | Post-energise abort (as today) + the new continuous watchdog. |
| Collision in teleop | the follower presses at up to 80 % PWM; the servo's overload protection lowers it to 20 % after 2 s (F5) | FW trip → relax in place (goal = present) → latch → EVENT; the engine runs Schritt 1/2 as today. |

"Hold vs torque off" is decidable per case: never automatically torque off except for a runaway, a post-energise fault or an explicit command. The arm falls when limp, and from many poses toward the camera tower (`limp_fall_direction`).

### 7.3 EEPROM floor independent of any MCU

* Exists (F5): `Max_Torque_Limit`, the overload pair (`Overload_Torque` / `Protection_Time` → `Protective_Torque`), `Protection_Current` + overcurrent time, `Max_Temperature`, voltage limits, position windows (goal clamp only, not the path), `Operating_Mode`.
* `feetech_provision.py` sets: `Max_Torque_Limit` 800 (arm) / 150 (claw), `Protection_Current` 310, `Overload_Torque` 80, `Operating_Mode` 0, `Return_Delay` 0, the windows, `Homing_Offset`, Phase bit 4 clear, `Angular_Resolution` 1, `Response_Status_Level` 1, `Lock` 1.
* Left at factory: `Protective_Torque`, `Protection_Time`, the overcurrent time, `Max_Temperature`, voltage limits, `Unload_Condition`, LED alarm, PID gains, dead zones.
* The firmware boot check should cover **all** provisioned values. Values left at factory should be recorded in the provisioning record, so a later firmware can verify them too.

### 7.4 Collision stop on the V1 (ESTIMATE, `gravity_headroom.py`, URDF masses from `feetech_geometry.V1_LINK_MASSES`)

* Gravity torques at HOME: j2 0.934, j3 −2.211, j4 −0.413 N·m. Static duty ≈ 19 % / 45 % / 14 % of stall.
* Over random above-table poses: j2 up to 83 % (≥ 50 % in 18 % of samples), j3 45 %, j4 14 %.
* With output capped at 80 %, a fixed j2/j3 threshold set above gravity has almost no headroom left. This is the OMX method (p95 × 1.5), and it fails here.
* Design: residual = measured load − predicted gravity load (a compiled-in 5-link gravity model: forward kinematics plus five masses, ESTIMATE ~1–2 kflop ≈ 50 µs per cycle, ≈ 0.5 % of a core at 100 Hz) − a velocity/friction term from bench data. Threshold the residual; keep the 150 ms debounce and the low-speed gate.
* Bench B6 measures:
  * per-joint load vs pose and speed maps;
  * noise σ;
  * contact force at trip with a spring gauge;
  * false trips over 30 min of free teleop.
* Today's OMX stop is ~150 ms; LeRobot has no collision detection; uFactory uses current + a dynamic model.

### 7.5 Leader on the bus

* **Ids**: leader **11..16** in every topology. They are the only machine-readable role mark; they prevent collisions on a shared bus and catch a swapped cable on separate buses.
* **Provisioning**: `feetech_provision.py --role leader`, which writes the ID first with only that arm connected, and records the role in the record.
* **Bus time**: +0.86 ms + 6 t_turn per cycle (pos/vel/load) or less with a 2-byte position read. A limp leader still needs a read every cycle.
* **Power**: ≈ 0.2 A idle. Hot-plugging can dip a shared rail (B2d).
* **Never write goals to leader servos** (F3), except a deliberately torqued trigger servo if D14 wants a spring feel.
* **Teleop enable** only within 0.30 rad per joint (today's resume rule), so the engine glides the follower to the leader first.
* **One board, two buses** is cleaner than two boards: two boards bring two links and a board-to-board teleop path; ESP-NOW is lossy in classrooms. On the HAT (A), one shared bus is acceptable with H4 mitigations.

---

## 8. Industry check

| Product | Where the loop and safety live | Evidence | Lesson |
|---|---|---|---|
| Dobot Magician | Controller executes queued JOG/PTP/homing with its own parameters; alarms on the controller; host queries remaining queue space and indices. **One protocol over USB-serial 115 200, TTL, Wi-Fi (UDP 8899) and BLE** (service UUID listed). | VERIFIED: Dobot Communication Protocol V1.1.5 | Queue + index + flow control; transport-independent framing. |
| myCobot 280 (M5) | Arm-side ESP32 "Atom" firmware drives the servos; base "Transponder" relays serial; 115 200 baud | partly VERIFIED: pymycobot README, third-party guides | MCU in the arm, host sends targets. |
| LeRobot SO-101 | Host loop at 60 fps (teleop), servo EEPROM limits, `max_relative_target`, gripper torque limits; no watchdog | VERIFIED: LeRobot v0.5.1 sources | Research kits accept host loops; their documented bus problems still apply to us. |
| Niryo Ned2 | Raspberry Pi 4 + Ubuntu + ROS inside, smart steppers; collision detection on the robot | VERIFIED (vendor docs via search; the sensor source conflicts) | Products for non-experts put the controller in the robot. |
| uFactory xArm / Lite 6 | Controller-side collision detection, "current and dynamic model based", sensitivity 0–5, depends on payload settings | VERIFIED: uFactory docs via search | Model-based residual detection. |
| Trossen/Interbotix, ALOHA | Dynamixel + U2D2, PC/ROS loop (ALOHA ~50 Hz) | UNVERIFIED (background knowledge) | Research class again. |

**Lesson for EduBotics**: an edu product for non-experts that must also collect LeRobot-style data should embed the real-time loop and safety (Dobot/myCobot pattern), keep a Dobot-style queued, flow-controlled, transport-agnostic protocol, and do model-based collision detection (xArm).

---

## 9. Risks, ranked

| # | Risk | Severity | Mitigation / proof |
|---|---|---|---|
| 1 | Joint 3 at HOME near its over-current trip → unload → forearm drops mid-lesson | High | RG-H1 / B2; FW health alarms; maybe change HOME or raise `Protection_Current` (an owner safety decision) |
| 2 | Fixed-threshold collision detection useless on j2/j3 | High | Residual detector + B6; owner sign-off |
| 3 | SW1 flip cuts the firmware off from energised servos / raw bus bypass (HAT) | High (HAT) | Lock the switch; custom board removes it |
| 4 | Goal write auto-enables torque (F3) during hand-guide or on the leader | High if true | Structural no-write rule + B7 |
| 5 | Firmware safety regression vs the reviewed Python driver | High | Shared vectors, simulator, fuzzing, HIL, owner sign-off per guard |
| 6 | Power: undersized supply or connector, brown-out → arm falls | Medium | XT60 ≥ 8 A supply, B2 |
| 7 | ESP32 resets on COM open (DTR/RTS) | Medium | Engine opens lines deasserted; FW adopt-and-hold; B3 |
| 8 | Regulatory: EN 18031 / CRA scope for radios; CRA reporting already live | Medium (business) | Radios off in v1; consultant |
| 9 | Windows locked-down PC: CDC driver swap via Windows Update, USB selective suspend, endpoint stalls | Medium | B13 soak, B-Win driver test |
| 10 | Mixed-model firmware warning on every healthy V1 | Low–Medium | B9, per-model rule |
| 11 | NRND module / supply chain | Low (now) | HAL; the S3 board |
| 12 | Chunk-splice behaviour change (no first-point jump) alters motion feel | Low | VR12 comparison |

---

## 10. Recommended design, proof obligations, bench scripts

**Design**: §6.2 split; §6.3 tasks; integer executor with splice + stop generator; §6.7 protocol (generated fixed structs, CRC-32/zlib, `cmd_id`, flow control); §6.8 FSM; adopt-and-hold boot; USB-only v1; HAT (A) for development; custom ESP32-S3 two-bus controller as the production candidate; a Python "soft board" for dev/CI/fallback.

**Proof obligations**:
* P1 bus cycle p99 ≤ 5 ms (12 servos, full health).
* P2 loop jitter p99 ≤ 0.5 ms, no missed deadline in 1 h (radios on, if enabled).
* P3 every ported guard bit-identical on shared vectors.
* P4 stop within T_link + deceleration after link loss; no motion afterwards without a new command.
* P5 boot writes nothing before HELLO + ENERGISE; adopts torqued servos.
* P6 no goal writes to limp servos (unit + B7).
* P7 50 random power cuts during OTA never brick; a failed self-test rolls back; no OTA while energised and moving.
* P8 CDC with the inbox driver as a standard user on a locked-down PC, online and offline; zero resets in 1 000 port open/close cycles.
* P9 no servo reset or overheated connector under the stress profile with the specified supply.
* P10 collision trip ≤ 150 ms at a defined force; zero false trips in 30 min of free teleop.
* P11 decoder fuzzing clean (10⁸ execs).
* P12 (radios) pairing needs the device secret; replays rejected; no default password.

**Bench scripts to write** (Python on a laptop + a firmware diagnostic mode):

| Id | Script | Measures |
|---|---|---|
| B1 | `bench_bus_timing.py` | §3.6 |
| B2 | `bench_power.py` | 50 Hz log of voltage/current/temperature/status for all servos: (a) HOME 15 min (RG-H1), (b) all-joint stress trajectory, (c) one joint blocked, (d) leader hot-plug; 12 V input current with a meter; per-servo voltage drop along the chain; any unexpected `Torque_Enable 0` |
| B3 | `bench_port_open_reset.py` | 1 000 open/close cycles with pyserial defaults and with DTR/RTS preset False; count resets from HELLO uptime and reason |
| B4 | `bench_link_loss.py` | during a replay: kill the engine, unplug USB, suspend the process 0.2/0.5/1/2 s; stop latency, deceleration distance, no further motion |
| B5 | `bench_stall_smoothness.py` | injected 500 ms engine stalls during replay and inference streaming; joint-error histogram vs no-stall |
| B6 | `bench_collision.py` | load maps over pose and speed, σ at rest and in motion, contact tests with a spring gauge, false-trip soak |
| B7 | `bench_goal_write_autotorque.py` | torque off → write Goal = Present → read `Torque_Enable`; also an out-of-window goal (fixture-held joint) |
| B8 | `bench_damping_mode.py` | `Torque_Enable 2` with `Torque_Limit` 50/100/200 from HOME: descent speed and final pose (needs the register-40 allowlist widened for the bench only) |
| B9 | `bench_registers.py` | read regs 0–4, 7, 13–16, 19–20, 28, 34–38 on factory and provisioned 3215 and 3250, plus the ESP32 chip revision: settles F10, return-delay defaults, firmware uniformity |
| B10 | `bench_seam.py` | VR6/VR14 seam cases through the firmware |
| B11 | `bench_radio_jitter.py` | (if radios) BLE connected + Wi-Fi scanning during the 100 Hz loop |
| B12 | `bench_teleop_lag.py` | leader vs follower position cross-correlation at acceleration 50/150/254 |
| B13 | `bench_windows_soak.py` | 2 h on a cheap battery laptop, app minimised: link stalls > 100 ms, COM disappearances, selective suspend |

---

## 11. Decision questions for the owner

| Q | Question | Options | Recommendation | Evidence |
|---|---|---|---|---|
| Q1 | Where does the servo loop live? | (a) firmware on a controller board; (b) Python engine on today's adapter | **(a)**, keeping (b) as a "soft board" for dev, CI and fallback | §4.5, §3, §7.2 |
| Q2 | Which board? | (a) HAT (A) for prototype and production with mitigations; (b) HAT (A) now, custom ESP32-S3 two-bus controller for production; (c) custom board from day one | **(b)**, decided at the Phase-2 gate | §4.1–4.4 |
| Q3 | Radios in v1? | (a) off (USB only); (b) BLE identify + provisioning; (c) BLE + Wi-Fi roles incl. an iPad app | **(a)**; (b)/(c) as later, separately assessed features | §5.3, §5.5, §5.6 |
| Q4 | Leader/follower bus topology | (a) one shared bus, leader ids 11–16; (b) two buses, still ids 11–16; (c) two boards | **(a) on the HAT, (b) on the custom board** | §3.2, §7.5 |
| Q5 | Link-loss policy (D7 refined) | (a) stop + hold forever; (b) stop + hold, then damping release after T_idle (if B8 passes); (c) torque off after T_off | **(b)**, else (a) + the engine's "Ablegen" | §7.2, F1, B8 |
| Q6 | New firmware guard: continuous tracking-error / wrong-way watchdog (Rule §2) | yes / no | **Yes** | driver residual note, §7.1 |
| Q7 | Collision detector model | (a) OMX-style fixed thresholds; (b) gravity-compensated residual on the board | **(b)** | §7.4 |
| Q8 | Wire units | (a) int16 ticks + µs; (b) float32 SI | **(a)** | §6.5, `float32_check.py` |
| Q9 | Firmware authenticity | (a) signed app updates only; (b) + Secure Boot V2 and secure download mode on the production board | **(a) now, (b) for production** | §5.5 |
| Q10 | Widen register-40 values to allow damping mode (2), first for the bench only | yes / no | **Yes (bench)** | F1, B8 |
| Q11 | A physical stop button on the kit | yes / no | **Yes** | §7.1 |
| Q12 | Supply in the kit | 12 V, ≥ 8 A, XT60 only | **Yes** | §4.3 |
| Q13 | Engage a compliance consultant (RED incl. 2022/30, CRA reporting duties now live, Machinery Regulation scope) | yes / no | **Yes, before radios or production** | §5.6 |
| Q14 | Joint 3 at HOME near its over-current trip: accept (RG-H1 decides) / change HOME / raise `Protection_Current` | — | **Run RG-H1 first, with firmware health logging** | §0 finding 7, KNOWN-ISSUES |

---

## Sources (web)

* Waveshare HAT (A) wiki and product page: https://www.waveshare.com/wiki/Bus_Servo_Driver_HAT_(A) · https://www.waveshare.com/bus-servo-driver-hat-a.htm · schematic https://files.waveshare.com/wiki/Bus-Servo-Driver-HAT-(A)/Bus_Servo_Driver_HAT_(A)_Sch.pdf
* CH343 CDC default: https://www.waveshare.com/wiki/CH343_USB_UART_Board · vendor driver on Windows Update: https://catalog.update.microsoft.com/Search.aspx?q=ch343
* MP9447: https://www.epsglobal.com/products/semiconductors/power/dc-dc-power-conversion/mp9447-high-efficiency,-fast-transient,-5a,-36v-sy
* ST/STS memory tables: https://docs.waveshare.com/Memory_Map_Explanation/ST_Servo_Memory_Map_Explanation · https://wiki.aifitlab.com/feetech-servo-motor-docs/feetech-sts-servo-memory-table-analysis
* Servo specs: https://openelab.io/products/feetech-sts3250-c001-ttl-servo · https://robotshop.com/products/feetech-12v-30kgcm-magnetic-encoding-servo-sts3215
* LeRobot v0.5.1 sources: `motors/feetech/feetech.py`, `motors/feetech/tables.py`, `robots/so_follower/so_follower.py`, `scripts/lerobot_teleoperate.py` (raw.githubusercontent.com/huggingface/lerobot/v0.5.1/…) · issues/PRs: https://github.com/huggingface/lerobot/issues/3585 · https://github.com/huggingface/lerobot/pull/3888
* Sync-read timings: https://github.com/iory/feetech-cli/pull/3 · https://github.com/iory/actuo/pull/6
* Espressif: WROOM-32 NRND https://www.espressif.com/sites/default/files/documentation/esp32-wroom-32_datasheet_en.pdf · Secure Boot V2 https://docs.espressif.com/projects/esp-idf/en/stable/esp32/security/secure-boot-v2.html · flash concurrency https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/spi_flash/spi_flash_concurrency.html · interrupt allocation https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/intr_alloc.html · OTA/rollback https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/ota.html · FPU cores https://developer.espressif.com/blog/2025/10/cores_with_fpu/ · L2CAP CoC https://docs.espressif.com/projects/esp-iot-solution/en/latest/bluetooth/ble_l2cap_coc.html · QEMU https://docs.espressif.com/projects/esp-idf/en/v5.3.1/esp32/api-guides/tools/qemu.html
* BLE/iOS: https://docs.silabs.com/bluetooth/8.3.0/mobile-apps-suitable-connection-parameters · https://developer.apple.com/forums/thread/771242 · https://lists.apple.com/archives/bluetooth-dev/2022/Nov/msg00000.html · ESP32 BLE throughput reports https://esp32.com/viewtopic.php?p=134183
* Browser APIs: https://caniuse.com/web-serial · https://caniuse.com/web-bluetooth · https://raw.githubusercontent.com/whatwg/bluetooth/main/implementation-status.md
* ESP-NOW reports: https://hackaday.io/project/164132-hello-world-for-esp-now/log/160572-latency-and-reliability-testing · https://esp32.com/viewtopic.php?p=151325
* Regulation: https://eur-lex.europa.eu/eli/reg/2022/30/oj · https://eur-lex.europa.eu/eli/dec_impl/2025/138/oj/eng · https://www.sgs.com/en/news/2025/02/safeguards-02625-eu-harmonizes-en-18031-standards · CRA: https://www.freshfields.com/en/our-thinking/blogs/technology-quotient/cyber-resilience-act-reporting-obligations-take-effect-on-11-september-2026-102nzmk · https://digital-strategy.ec.europa.eu/en/policies/cra-reporting
* Industry: Dobot protocol https://download.dobot.cc/product-manual/dobot-magician/pdf/en/Dobot-Communication-Protocol-V1.1.5.pdf · myCobot https://github.com/elephantrobotics/pymycobot · Niryo https://docs.niryo.com/product/ned2/source/introduction.html · uFactory https://docs.supportarticle.ufactory.cc/support_articles/developer/collision-detection-in-ufactory-robotic-arms-current-and-dynamic-model-based-feature.html
* Other boards: https://waveshare.com/wiki/General_Driver_for_Robots · https://wiki.seeedstudio.com/xiao_bus_servo_adapter/
* esptool licence: https://docs.espressif.com/projects/esptool/en/latest/about.html
