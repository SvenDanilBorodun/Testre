# SPEC B — Appendix: bench scripts, Windows spikes, records

Part of `SPEC-B-proofs-firmware-armlink.md`. **Agents never touch hardware**: every bench script is run by the owner at
the bench; every school-PC spike by the owner as a standard user; GitHub spike runs are started by the build session
pushing a `spike/*` branch. Every pass bar below is a **proposal**; the owner approves (or changes) each number at its
gate (PLAN §5 [owner]), and the approval is a sign-off file (§1.3). Where a number can be measured on today's OMX
without spend, it is, and the V1 bar is set against that baseline (Q30): the teleop lag (OMX-LAG) and, where the rig is
available, the collision trip force (B6). **There is no OMX train-and-run baseline** (owner Q-B4, 2026-11-25: no paid
training, no Cloud-Inferenz trials): the V1's train-and-run success bar is set by the owner's judgement at the trial
gate (spec C's `TRIAL-PASSED`), and no script, record or gate of this spec measures one.

---

## 1. Records

### 1.1 Bench record (`native/bench/schema/bench-record-1.json`, validated before write)
```json
{
  "schema": "edubotics-bench-record/1",
  "record_id": "<gate>-<yyyymmdd>-<n>",
  "gate": "RG-H1 | B1 | B2a | … | VR11 | OMX-LAG | V1-LAG | LEADER-LIMP | P7 | HIL",
  "script": "native/bench/<file>.py", "script_sha256": "<hex>", "repo_commit": "<git sha>",
  "started_at": "<UTC ISO-8601>", "operator": "owner",
  "rig": {"arm_serial": "V1-0001", "arm_role": "follower|leader", "board": "hat_a:<mac>|adapter",
          "supply": "<V, A, connector, fuse>", "pc": {"model": "…", "os_build": "…", "cpu": "…", "ram_gb": 8,
          "power": "battery|mains"}, "camera_mounted": "none|wrist:<model>"},
  "firmware": {"variant": "diag|bench|prod", "build_number": 0, "sha256": "<hex>"} ,
  "parameters": {}, "raw_files": [{"path": "raw/<name>.csv.gz", "sha256": "<hex>", "bytes": 0}],
  "summary": {}, "proposed_bar": {}, "verdict_by_script": "pass|fail|no_bar",
  "notes": "free text (English)"
}
```
`firmware` is `null` for runs on today's driver/plain adapter. Records live under
`native/bench/records/<phase>/<record_id>/` (`record.json` + `raw/`), ≤ 5 MB per run (gzip CSV).

### 1.2 Raw CSV conventions
UTF-8, header row, `t_mono_ns` (PC) and, where a board is involved, `board_us`; one row per sample; joint values in
ticks **and** radians; servo ids explicit.

### 1.3 Sign-off file (`native/bench/signoffs/<gate>-<n>.json`)
`{"gate", "record_ids": [...], "decision": "approved|rejected|changed", "bars": {...}, "owner_text": "<verbatim
quote of the owner's message>", "date"}`. The build session writes it **only** from an explicit owner message and
quotes it verbatim; it never infers an approval. `test_native_bench_signoffs.py` checks every bench-derived constant in
`arm_spec.py` cites an approved sign-off. The same schema records the owner's answer at a release gate that is not a
bench: `OWNER-FW-CI-<n>.json` (Q-B1's re-ask before the first pilot firmware: "add a `native-firmware` job to
`ci.yml`?"; `record_ids` empty; `approved` = add the job as its own WP, `rejected` = no job, `changed` = the owner's own
wording, e.g. "later"). `sign_release.sh --channel pilot` accepts any of the three — it enforces that the question was
asked, not its answer.

---

## 2. Bench scripts

Common safety rules in `native/bench/README.md`: a hand on the 12 V switch, nobody under or beside the arm's fall
direction, the arm laid down at the end, B7 on a **loose servo**, never the assembled arm.

| Gate | Script (phase) | Measures | Procedure | Proposed bar | Runs on |
|---|---|---|---|---|---|
| **RG-H1** = B2a | `rg_h1_hold_home.py` (1) | Holding load of the V1 at HOME `(0, 0.64, 0.64, 0, 0)`, claw 0.90 | `bench_arm`: probe → energise → `glide_home` (floor-checked) → hold 15 min; servos 2–4 `Present_Current`, `Present_Temperature`, status at 1 Hz; 12 V input current by meter (manual entry) | KNOWN-ISSUES RG-H1: servo 3 ≤ 1.5 A (~75 % of trip), ≤ 60 °C, no overload / over-current bit, no unexpected `Torque_Enable 0` (owner-defined, D49) | pilot V1 + plain adapter |
| **B2b** | `b2_power.py --profile stress` (1) | Supply sizing under motion | 10 min all-joint stress trajectory (generated in `bench_arm`, velocity-floored like `trajectory_builder`); health at 50 Hz (round robin); meter on the 12 V input; per-servo voltage along the chain | no servo reset; min servo voltage ≥ 10.8 V; supply rating ≥ 1.25 × p99.9 input current (≥ 8 A, research 02 §4.3); connector temperature rise ≤ 30 K (IR, manual) | same |
| **B2c** | `b2_power.py --profile blocked --joint {2,3}` (1) | Protection behaviour | Block one joint against a padded fixture during a slow move; observe current and status for 5 s, then torque off | overload/over-current bits as F5 describes; no other servo resets; fuse rating proposal = next standard value ≥ 1.5 × B2b p99.9 and below the XT60 rating | same |
| **B2d** | `b2_power.py --profile leader-hotplug` (3) | Separate supplies stay separate | Follower energised at HOME; plug/unplug the leader's USB and 12 V 10× | no follower servo reset; follower voltage dip ≤ 0.5 V | V1 follower (HAT) + leader (adapter) |
| **B1** | `b1_bus_timing.py` (1) | Bus cycle on the HAT | `diag` firmware: 10 000 cycles per DIAG shape (appendix protocol §3.7) at 100 Hz, arm limp and arm held by hand moving; optional logic-analyser capture of 50 cycles (t_turn per servo, edge rise) | shape 4 p99 ≤ 3.0 ms, shape 5 p99 ≤ 4.0 ms, corrupt replies < 10⁻⁴, missed replies 0 with all servos powered; t_turn recorded (desk arithmetic: 1.58–2.60 ms, main spec P6) | HAT #1 + V1 follower |
| **B3** (= S13b) | `b3_port_open_reset.py` (1) | Does opening the COM port reset the ESP32? | 1 000 × open/close with DTR/RTS deasserted **before** open, then 1 000 × with pyserial defaults; reset counted from HELLO_ACK `boot_count`/`reset_reason`/`uptime_ms`; on the owner's Windows PC as a **standard user** | deasserted: 0 resets; defaults: count recorded (expected > 0 → any foreign program can reset the board; adopt-and-hold makes it harmless) | HAT #1, Windows 11 PC |
| **S13c** | `s13c_echo_latency.py` (1) | Host link latency | 10 000 `DIAG_ECHO` (64 B) round trips; under usbser and, if Windows installs it, WCH's CH343SER | p99 < 5 ms | HAT #1, school PC + slow laptop |
| **B7** | `b7_goal_write_autotorque.py` (1) | Does a goal write set `Torque_Enable` by itself (F3)? | **Loose single servo** of each model (777, 2825): torque off → write Goal = Present → read 40; then a goal outside a narrowed window (servo shaft free) → read 40 and position after 1 s | no bar (a fact); result sets `feetech_sim`'s F3 default; firmware never writes a goal to a limp servo regardless | loose STS3215, loose STS3250 |
| **B8** | `b8_damping_mode.py` (3) | Damping mode as a controlled release | `BenchBus.write_damping` from HOME with `Torque_Limit` 50/100/200: joint trajectories at 50 Hz until rest; filmed 60 fps | proposal for a `prod` enable: descent ≤ 0.5 rad/s peak on joints 2–3, final pose on the table forward of HOME, no contact with the tower; else damping stays bench-only | V1 follower (HAT `bench` firmware) |
| **B9** | `b9_register_dump.py` (1) | Facts | READ regs 0–70 of: a factory STS3215 and STS3250 (loose), the provisioned pilot V1 (ids 1–6); `esptool.py chip_id`/`flash_id` on the HAT; USB descriptors (VID:PID, serial number string) of both HAT ports and the adapter | no bar; feeds `feetech_sim` defaults, F10 (`Protection_Current` factory), flash size, chip revision, `ARMLINK_USB_IDS`, per-model firmware versions (G14 uniformity rule) | loose servos, pilot V1, HAT |
| **VR1/3/4/9/11/16** | `vr_checks.py` (1) | Hardware facts | VR1 order (move each joint by hand, see which id changes); VR3 signs; VR4 `provision --dry-run` read-back; VR9 base geometry (caliper entry); VR11 jog j4 to ±1.9198 under the product; VR16 claw ratio (protractor entry) | as `docs/v1-hardware-bringup.md` | pilot V1 |
| **VR5–8, 10, 12–15** | today's WSL product (1) | Product-level gates on the follower | `docs/v1-hardware-bringup.md` §5–§11, results recorded with `vr_checks.py --manual` | as KNOWN-ISSUES | pilot V1 + tower kit |
| **OMX-LAG** | `omx/record_teleop_pair.py` + `teleop_lag.py` (1) | OMX teleop position lag | rclpy script in the running `physical_ai_server` container records `/leader/joint_states` and `/joint_states` (header stamps + arrival) for 3 × 60 s of free teleop (each joint ≥ 0.5 rad peak-to-peak); cross-correlation 0–500 ms, 1 kHz resampling, parabolic peak | no bar (baseline); reports per-joint lag, median, p95 | today's OMX rig, WSL product |
| **B4** | `b4_link_loss.py` (2) | Link-loss stop | During a 3 s program glide and during a replay: kill the arm-link/host process; unplug USB; suspend the host 0.2/0.5/1/2 s. The board cannot report while the host is gone, so the `bench` firmware keeps the last 1 000 compact snapshots (10 s) in a RAM ring and the script downloads them after reconnecting (`DIAG_STATE_RING`, appendix protocol §3.7); plus 60 fps video. SETPOINT (M18): 2 s of 30 Hz setpoints, then a 15 s gap with heartbeats only, then the engine killed | stop starts 1.0 s (± 10 ms) after the last HEARTBEAT; HOLD within the §5.4 bound; no motion afterwards until a new command; suspensions < 1 s change nothing; SETPOINT: no latch and no measured motion beyond 2 ticks during the 15 s gap, `setpoint_lost` 1.0 s (± 10 ms) after the last heartbeat | HAT `prod` + V1 |
| **B5** | `b5_stall_smoothness.py` (2) | Smoothness through PC stalls | Replay and setpoint streaming with injected 100/250/500 ms host stalls; joint error histogram vs no stall | p99 joint error with 500 ms stalls ≤ 1.5 × without; no `link_lost` for stalls < 1 s; data feeds E_j/T_track (§7.2) | same |
| **B10** | `b10_seam.py` (2) | VR6/VR14 through the firmware | edge refusal at j2/j3 tick ≥ 4056; provoked wrong-way abort; truncated trajectory at the cap → overshoot measured (VR14); goal clamp at full extension | equal outcomes to the driver; overshoot recorded vs the 128-tick margin | same |
| **HIL** | `hil_checklist.py` (2) | P4–P6, reset-during-motion, read-fail latch by unplugging a servo cable, brown-out by switching 12 V, probe refusals (unprovisioned spare servo; a follower servo with `Max_Torque_Limit` 0, the leader value → `max_torque`), an **unsigned** app flashed with esptool (the bootloader carries the key, main spec §12 P30) | scripted checklist with operator prompts | all items pass; the unsigned app is not booted (the other slot boots, or the board stays in its bootloader and says so) | same |
| **P7** | `p7_ota_power_cut.py` (2) | Update robustness | 50 updates, USB power cut at a random byte offset (relay or by hand at a prompt); then a deliberately failing self-test image; every image signed by the owner with `sign_release.sh --channel bench` | never bricked (always boots one valid image); the failing image rolls back | HAT |
| **B6** | `b6_collision.py` + `b6_fit.py` (3) | Collision residual | load maps over 200 poses × 3 speeds; σ at rest and moving; contact force at trip with a spring gauge at 3 points; 30 min free teleop | trip force ≤ OMX baseline force measured the same way (if the OMX rig is available) else ≤ 15 N at the claw; trip latency ≤ 150 ms + debounce; **0 false trips in 30 min** | V1 follower + leader |
| **B11** | `b11_radio_jitter.py` (2) | Loop jitter with radio on | `bench` firmware, phone connected and subscribed, 1 h; and advertising-only 1 h | P2: cycle-start jitter p99 ≤ 0.5 ms, 0 missed cycles | HAT `bench` |
| **LEADER-LIMP** | `leader_limp_check.py` (3, B11) | The leader is structurally limp (owner Q-B2) | Interim leader (second V1) alone on its own plain adapter, follower disconnected, the leader laid on the table. After provisioning `--role leader`: power-cycle its 12 V; READ registers 16 and 48 of ids 11–16. Then, one servo at a time, with `BenchBus` (bench only — the product's arm link never writes the leader): seed `Goal = Present` (the driver's seed rule), `Torque_Enable 1`, the operator moves that joint by hand through its window at a prompt while `Present_Position`/`Present_Load` are logged at 50 Hz, then `Torque_Enable 0` | registers 16 and 48 read 0 on all six; with `Torque_Enable 1` every joint follows the hand through ≥ 80 % of its window with abs(`Present_Load`) ≤ the limp noise floor measured with `Torque_Enable 0` + 2 % and no felt resistance (operator confirms) — **fail → decided fix:** (a) a joint holds against the hand with `Torque_Enable 1` → the servo firmware does not treat a zero torque limit as "no output": keep register 16 = 0 (harmless), withdraw "structural" from D-B20, the leader rule rests on `Torque_Enable 0` + `leader_torqued` alone, and tell the owner; (b) register 48 ≠ 0 after the power cycle while every joint still follows the hand → register 16 alone caps the output: the arm link's fingerprint drops its register-48 check (`torque_limit_ram`) and B11 drops the power-cycle line | interim leader + plain adapter |
| **V1-LAG** (B12 V1) | `b12_teleop_lag_v1.py` (3) | V1 teleop lag through the PC | as OMX-LAG on the slowest available Windows 11 laptop (record its specs), app minimised | V1 median-over-joints lag ≤ OMX median + 15 ms (proposal) — else the O8 trigger | V1 pair + slow laptop |
| **B13** | `b13_windows_soak.py` (3) | 2 h Windows soak | app minimised, teleop and idle phases; counts link stalls > 100 ms, COM disappearances, selective-suspend events, `link_lost`/`teleop_lost` | 0 COM disappearances; stalls > 250 ms ≤ 1 per hour | slow laptop |

---

## 3. Windows spikes

### 3.1 Spike record (`native/spikes/windows/schema/spike-record-1.json`)
```json
{"schema": "edubotics-spike-record/1", "spike": "S1", "record_id": "S1-<github|schoolpc>-<yyyymmdd>-<n>",
 "context": {"kind": "github|schoolpc", "runner_image": "windows-2025 <ImageVersion>|null",
             "os_build": "10.0.xxxxx.yyy", "edition": "…", "user": "ebspike1", "user_is_admin": false,
             "integrity": "Medium", "python": "3.13.x", "commit": "<sha>", "defender_realtime": true},
 "checks": [{"id": "S1.connect_loopback_engine_port", "expected": "refused", "observed": "refused",
             "detail": "WinError 10013"}],
 "metrics": {}, "verdict": "pass|fail", "failed_checks": []}
```
A record with `user_is_admin: true` or `integrity` ≠ Medium is invalid (the spike proves nothing about schools).

### 3.2 Harness
- GitHub: `native/spikes/github/windows-spikes.yml` copied onto a `spike/*` branch (main spec §3.4); jobs `platform`,
  `stack`, `sandbox` on `windows-2025`, each gated by the branch prefix; `actions/setup-python` 3.13 for the harness
  only (the spikes use their own pinned runtimes where the spike is about the runtime); artifacts kept 3 days.
- `run_as_standard_user.ps1 -Group <g>`: creates `ebspike1`/`ebspike2` (random 24-char passwords, masked), grants them
  read on the checkout and write on `spike-records\`, registers a scheduled task per spike (`-LogonType Password
  -RunLevel Limited`), runs it, waits (timeout per spike), collects records, deletes the users. Every spike's first
  check is `standard_user` (not in an enabled Administrators group, Medium IL).
- School PC: `schoolpc/run_all.ps1` from a zip, started by the owner logged in as a standard user (no admin at any
  step); writes records to `%LOCALAPPDATA%\EduBoticsSpikes\`; the owner copies them back.

### 3.3 Spikes: what, where, pass, and the decided fix for each failure
| Spike | Proves | GitHub part | School-PC part | Pass | Failure → decided fix |
|---|---|---|---|---|---|
| **S1** | CPython 3.13 embeddable in an AppContainer | all checks | all checks | imports ok; loopback/internet/user files/engine process refused; run pipe opens; `sys.monitoring` present; start latency recorded | python.exe does not start → grant RX to the AppContainer SID (and `ALL APPLICATION PACKAGES`) on the runtime folder, re-run; loopback reachable → accepted (the engine's cookie guards it; the student channel is the pipe), record; internet or Documents reachable → retry as LPAC; still reachable on both contexts → O11 (owner) |
| **S2** | JVM + javac in an AppContainer | all | all | `javac` errors are real text; JIT active; no hsperf/tmp failure | start failure → `-Djava.io.tmpdir`, `-Duser.home=<run dir>`, `-XX:-UsePerfData`, `-XX:+DisableAttachMechanism`; JIT failure → never set ACG; else `-Xint` with timing recorded → owner if slower than 3×; Win32k lockdown breaks → keep it off |
| **S3** | Job limits, nesting, kill-on-close | all | all | each limit trips; children die with the broker; a 30 Hz tick in a separate process stays within ±2 ms p99 under the CPU cap | per-process memory limit ineffective → job-wide `JOB_MEMORY`; nesting with UI limits fails → no UI limits on the student job; tick budget broken → lower CPU cap + BELOW_NORMAL; kill-on-close fails → the host holds the job handle |
| **S4** | `code_rpc` over named pipes | listed subset of `test_code_rpc*.py` + `test_code_program_stop.py` through `pipe_socket_adapter.py` (count recorded) | — | subset green; `CODE_STOP_DEADLINE_S` holds | fix the adapter's timeout semantics (overlapped I/O); re-derive the deadline |
| **S5** | Escape matrix | all 8 attempts | all 8 | every attempt refused | any success confirmed on both → O11 (owner) |
| **S-J** | jlinked JDK | size + SmokeMain | — | size recorded; SmokeMain passes inside the AppContainer | missing module → add it to `--add-modules`, re-run |
| **S7** | Curated lock + LeRobot writer on Windows | spike lock install (`--require-hashes --only-binary :all:`), `python -X importtime` of the recorder/Daten closure, `av.codec.Codec('libx264','w')`, a LeRobot-only 2-camera writer smoke (create, add 300 frames, save, finalize, load, decode), and an attempt at `record_fsm_smoke.py` whose blocking imports are recorded | — | the first three green | an import fails → record the module; fix by pin or shim (spec C); no libx264 → licence-reviewed alternative (owner); writer smoke fails → blocker for spec C's recorder |
| **S8** | Installed size | full and with `torch/lib/*.lib` + `torch/include` stripped | — | sizes recorded; S7's writer smoke still passes stripped | stripping breaks → keep that part |
| **S9** | Paths | `dataset_paths.confine` (read-only use) with `C:foo`, `\\?\`, `NUL`, `file.mp4:stream`, 8.3 names, junctions (`mklink /J`), a 250-char root; PyAV/pyarrow open/write | — | refused or handled, no data loss | a case passes `confine` wrongly → test case for spec C's fix; MAX_PATH failures → short root `%LOCALAPPDATA%\EB\d` + a name-length guard (spec C) |
| **S10** | Daten swaps under Windows file semantics | `daten_smoke.py` with a spike-local `fcntl` shim (`msvcrt.locking`), a concurrent reader holding an mp4, Defender on | same | no corrupted swap; leftovers logged | swap fails on open handles → reader quiescing + retry/back-off (spec C portability layer); data loss → blocker for Daten |
| **S11** | Timers; one monotonic clock across processes | `time.sleep`, `Event.wait`, high-res waitable timer (ctypes `CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`) at 33.3 ms and 10 ms; with/without power-throttling opt-out; a 20 Hz timer next to a CPU-bound thread (the heartbeat case, §8.5 of the main spec); **cross-process bracket test**: a child returns `time.monotonic_ns()`, the parent brackets it with its own before/after stamps, 10 000 round trips; **full-pipe heartbeat**: the §8.9 mailbox client over `AF_PIPE` to a peer that accepted and never reads — 2 000 `heartbeat()` calls timed, then the peer killed (main spec §12 P35 is the POSIX leg) | plus a window minimised / occluded (EcoQoS) | 30 Hz and 100 Hz within ±2 ms p99 with the chosen primitive; the 20 Hz timer never gaps ≥ 0.5 s; **0** child stamps outside the parent's bracket; `heartbeat()` p99 ≤ 100 µs and max ≤ 1 ms with the pipe full, `False` within 100 ms after the peer dies | choose the primitive that passes; minimised throttling → opt-out + MMCSS; if nothing passes → the relay tolerates it (board timestamps; B5 bars) and the owner is told; bracket violations → the arm link sends an explicit clock-offset estimate (I_HELLO_ACK + periodic ping) and §8.7's leader mapping becomes offset-corrected; `heartbeat()` over its bound on Windows (it never touches the pipe, so only the slot lock or the GIL can delay it) → the slot becomes a lock-free attribute swap (atomic under the GIL) and the writer wakes on a semaphore; re-run |
| **S12** | Session cookie | loopback HTTP + WS spike server; same user with/without cookie; `ebspike2` connects | same | no-cookie and second user get 401, no data | fix the server; must pass before spec C ships |
| **S13a** | CH343P binding without admin | — (no USB on runners) | plug HAT (both ports) and adapter as a standard user, online and offline; record driver (usbser vs CH343SER), COM appears, the school's device-install policy | COM appears without admin | blocked by policy → IT allowlist `USB\VID_1A86&PID_55D3` (as today); WCH driver installed → S13c on both drivers |
| **S13b** | = B3 | — | B3 as standard user | B3 bar | resets with deasserted lines → owner decides: accept (adopt-and-hold) or a hardware mod |
| **S13c** | Latency | — | as table §2 | p99 < 5 ms | raise/lower baud; batch frames |
| **S16** | MSMF in `opencv-python-headless` | `cv2.videoio_registry.getBackends()` contains `CAP_MSMF` | today's capture parameters (`win_camera.open_scene_capture`, read-only use) with a real camera | backend present and capture works | missing → the capture process ships `opencv-python` (spec C) |

(No S14: the WSL-export spike was dropped with the data import, owner Q-C3, 2026-11-25.)

Every row's result goes to `native/spikes/results/<context>/<spike>.json`; `native/spikes/DECISIONS.md` lists, per
failed check, the fix taken from this table (or the O11 escalation) and the re-run record id.

---

## 4. O4 (lighter leader) and O5 (wrist camera): what software needs, and the interim

### 4.1 O4 — the leader design must deliver to software
servo model(s) and their Model_Number (register 3) → `LEADER_ACCEPTED_MODELS`; confirmation that they speak STS/SMS
(little-endian, sign-magnitude, 4096 ticks/rev, 1 Mbaud) or a protocol note; 12 V; per joint: zero (jig) definition,
direction, gear ratio to the follower joint, travel window → the leader spec in `arm_spec.py` (windows for the
fingerprint, the `mapping.py` table `offset, sign, ratio`); the claw handle → a 16-point leader-angle → `RL_joint`
table (0 = closed … 0.90 open command; the recorded action may exceed 0.90 only if the follower window allows,
clamped at send); ids 11–16, `Return_Delay 0` and `Max_Torque_Limit 0` (structurally limp, owner Q-B2) at
provisioning (`--role leader`), so the chosen servo model must honour `Max_Torque_Limit`/`Torque_Limit` 0 as "no
output" (LEADER-LIMP repeated on it); a plain Bus Servo Adapter (A).
Interim: a second V1 on a plain adapter, provisioned `--role leader` with the follower windows, identity mapping.

### 4.2 O5 — the wrist camera must deliver to software
UVC with MJPG ≥ 640 × 480 at 30 fps on Windows MSMF (S16); cable route that leaves joints 4–5 and the claw free; mass
and CoM on link 5 → the firmware gravity model's masses (B16 re-fit; **B6 must be repeated with the camera mounted**),
the tower guard's arm model and `V1_LINK_BOXES` (spec C / geometry owners), the clip-on calibration target fit, VG1/VG5
re-measured. Interim: no wrist camera; the tower camera alone; B6 is run without it and marked for repetition.
