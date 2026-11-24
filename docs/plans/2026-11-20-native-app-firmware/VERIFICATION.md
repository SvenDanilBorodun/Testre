# Verification of PLAN.md before the architect's spec (2026-11-24)

Scope: `PLAN.md` (authoritative), checked against `FINDINGS.md`, `BRIEF.md`, the four reports under
`research/` and the code at 410de5f8 (= dba3ac81, release 2.41.0, plus this folder). Method: a full read
of every document by me; every claim about the repository re-checked against the code by one
independent agent (section 3). Nothing here ran on Windows, an iPad or a robot.

---

## 1. Verdict

The direction holds and the evidence behind it is sound. The plan is **not yet spec-ready** for three
reasons:

1. The owner's later decision "HAT (A) is the final follower board" was folded into one table row only.
   Eight other places still assume a custom board (section 2).
2. Two owner decisions changed the research's topology (two boards, teleop through the PC) and the
   reports' derived conclusions were not re-derived: bus timing, the teleop fallback, power, the leader
   board, the recorded action and the teleop catch-up after a stall (section 4).
3. Several decisions a spec cannot invent are not made anywhere: where today's tkinter setup flows go,
   what an orderly app close does to an energised arm, the collision stop's scope, the N−1 rule, the
   firmware version rule, the branch model, numeric pass bars (section 5).

---

## 2. Contradictions inside PLAN.md (stale after "HAT (A) is final")

| # | Place | Says | Problem |
|---|---|---|---|
| C1 | §2.1 "Stop button" | Only on the custom board | There is no custom board any more → no stop button at all, unless decided anew (Q3). |
| C2 | §2.2 "Firmware authenticity" | Secure boot on the custom board at the factory step | Same → Q4. |
| C3 | §2.2 "Compliance" | Review before Bluetooth or the custom board ships | Reflashing the HAT's radio module and shipping it makes us its manufacturer (02 §5.6), radio on or off → Q7. |
| C4 | §4 O6 | Custom board maker and schedule | Obsolete. |
| C5 | §4 O8 | Teleop-lag fallback: a direct cable between the boards on the custom board | Obsolete → Q8. |
| C6 | §6 Phase 2 | "Board decision gate (custom board: who, when; consultant)" | Obsolete; the consultant part remains (Q7). |
| C7 | §6 Tracks | "Custom ESP32-S3 board" track | Obsolete. |
| C8 | §2.1 "Boards per kit" | Same board type, same firmware on the leader | The owner was still deciding second HAT vs plain adapter for the leader (Q1). |
| C9 | §1 | "all arm safety lives on the follower's board" | Contradicts §2.4 "floor and tower checks stay in the engine". Should read: all *real-time* arm safety (limits, watchdog, collision stop). |
| C10 | §2.1 "Rejected" | Waveshare Servo Driver with ESP32 rejected | Fine; note research's alternatives table also rated the S3 custom board as "production target" — now superseded. |

## 3. Factual corrections (documents vs documents, and documents vs code)

From my read:

| # | Place | Says | Correct |
|---|---|---|---|
| F1 | PLAN line 9, §6 Phase 1, §7; FINDINGS §6, §8 | "17 Windows spikes", "S0–S16" | Report 03 §15 defines 16: S0–S5, S7–S14, S16 and S-J. S6 and S15 do not exist. |
| F2 | PLAN §3 item 1 | 6-servo bus cycle "about 1.3–2.3 ms" | Report 02 §3.2: follower 1.58–2.06 ms (pos/vel/load + write, t_turn 20–100 µs), 2.12–2.60 ms with full health reads; a read-only leader ≈ 1.0–1.5 ms by the same arithmetic. All unmeasured (B1). |
| F3 | FINDINGS §6 | "Bus cycle 2.5–4.5 ms" | That is the superseded one-bus, 12-servo topology. |
| F4 | PLAN §3 item 1 | "the two arms share neither power nor bus" | "Power" depends on the kit's supply layout, undecided (Q2). |
| F5 | Report 02 §7.2 | Teleop link loss "continue up to 2 s, then HOLD (owner decision)" | Superseded by PLAN §2.4: about 1 s. |
| F6 | Report 03 §15 S13 | "clean Windows 10 and 11" | Windows 11 only now. |
| F7 | Report 04 §2.1 | "≈31 service names (30 interface types + Trigger)" | 31 names: 29 called (28 server + `/edubotics/activate`) and 2 hook-only; 29 interface types + Trigger. Report 01 is right. |
| F8 | Report 01 finding 7, 02 §7.5, FINDINGS §1.7 | Leader gates and "always fresh" leader on a shared bus | The premise (one board, one bus) no longer holds. The conclusion (gates key on "teleop is on") still holds, because with two boards the leader is equally always present. |

Code-claim verification by the independent agent: see section 6 (appended when it reports).

---

## 4. Consequences of the owner's topology choices the reports did not re-derive

1. **Teleop catch-up after a PC stall.** The follower receives a 100 Hz goal stream through the PC. After
   a stall the next goal may be far from the last one. The firmware design in report 02 refuses any
   chunk whose per-cycle step exceeds a cap; applied to teleop that would end teleop after every stall,
   and applied to inference it contradicts "exactly like LeRobot". Undecided → Q10.
2. **Two clock domains.** Leader and follower are no longer sampled in one bus transaction (01 §4.2 item
   3 assumed so). Each board's µs clock needs its own `ClockMap`; the recorded action's time stamp must
   be defined (Q17).
3. **Leader board power.** A HAT (A) feeds its servo rail with its own input (9–25 V). A leader with
   5–7.4 V "easier-turning" servos cannot run on a HAT (A) (Q2).
4. **The firmware's TELEOP mode changes meaning.** The follower board no longer sees the leader, so the
   0.30 rad proximity check of teleop start moves to the PC; the follower still needs a teleop mode to
   arm its collision detector.
5. **Provisioning path.** With SW1 glued to the ESP32 position, the raw USB port C1 cannot reach the
   servos any more; provisioning needs a firmware passthrough (Q6).
6. **iPad screen mode = Pi mode in the SPA.** A page served from a non-loopback host is treated as a Pi
   (`utils/piMode.js`: marker OR non-loopback page). The Windows engine in network mode therefore has to
   serve the Pi agent's `/api/system` contract (login, claim, robot key, update check, System tab), or
   React needs a third mode (Q21, Q22).

---

## 5. Open questions for the owner

Each question names what it blocks. Recommendations are mine.

### Scope
- **Q0 — Which spec now?** (a) Phase 0 only; (b) Phase 0 and Phase 1 as two separate specs; (c) one
  master spec for all phases. *Rec. (b):* Phases 2–7 are sized by Phase 1's results (PLAN §6 says so).

### Hardware (blocks Phase 1 purchases and Phase 2)
- **Q1 — Leader board.** (a) A second HAT (A) with our firmware (role from ids 11–16), as PLAN §2.1
  says; (b) a plain Bus Servo Adapter (A) read directly by the PC (no firmware; "the leader never writes"
  becomes a PC-software rule). *Rec. (a).*
- **Q2 — Leader servo voltage and kit power.** (a) 12 V servos on the leader, one 12 V XT60 supply per
  board; (b) 12 V servos, one supply split to both boards with a fuse per branch; (c) 5–7.4 V servos →
  the leader cannot use a HAT (A). *Rec. (a) or (b) after the bench power test; (c) only with Q1 (b).*
- **Q3 — Stop button.** (a) None (on-screen stop + firmware watchdog); (b) the HAT's BOOT/IO0 key
  re-used at runtime; (c) an external button on a free ESP32 GPIO of the HAT, "stop and hold"; (d) a
  switch in the 12 V lead (the arm falls). *Rec. (c) if the bench finds a free GPIO, else (b); never (d)
  alone.*
- **Q4 — Secure boot on the HAT.** (a) Never burn eFuses; signed updates with rollback only; (b) Secure
  Boot V2 + secure download mode on every shipped HAT (irreversible; needs chip rev ≥ 3.0, unknown until
  B9). *Rec. (a) for the pilot; revisit after B9 and the consultant.*
- **Q5 — Initial flashing.** (a) EduBotics flashes every HAT before shipping; the app only updates over
  our protocol; (b) the app flashes a factory-fresh HAT through the ROM bootloader (needs a non-GPL
  flasher). *Rec. (a).*
- **Q6 — Servo provisioning with SW1 glued.** (a) A gated firmware passthrough, used only by an
  EduBotics tool; (b) provision before gluing, servo swaps by returning the arm. *Rec. (a).*
- **Q7 — Compliance timing.** (a) A consultant check of the USB-only, radio-off kit before the first
  pilot kit with our firmware leaves us; (b) only before Bluetooth or any sale. *Rec. (a).*
- **Q8 — Teleop-lag fallback** (O8's custom-board cable is gone). (a) Leader on the follower HAT's bus
  (ids 11–16, on-board teleop, the research's original topology); (b) a UART cable between the two HATs
  (free pins unknown); (c) accept the lag. *Rec. (a).*
- **Q9 — Soft board.** (a) Dev and CI only; (b) also a shipped fallback for V1 kits without a HAT (no
  watchdog when the PC dies). *Rec. (a); today's pilot V1 kits get a HAT.*

### Safety (Rule §2; blocks Phases 2–3)
- **Q10 — A relayed goal far from the last one** (after a PC stall; also inference setpoints).
  (a) Executed as is, shaped only by the servos' speed and acceleration registers (like the OMX JTC today
  and LeRobot); (b) refused, teleop ends, a new sync glide is needed; (c) a per-cycle rate limit on the
  board (a new guard that modifies the command stream). *Rec. (a); the collision stop and the tracking
  watchdog are the floor.*
- **Q11 — Resuming teleop after a link loss of ≥ ~1 s.** (a) Automatically when the stream returns and
  every joint is within 0.30 rad, else a sync glide is offered; (b) always a deliberate student action
  and a sync glide. *Rec. (b).*
- **Q12 — Scope of the V1 collision stop.** (a) Teleop and recording only, as the OMX today; (b) also
  Roboter Studio programs and manual moves; (c) also Cloud-Inferenz. *Rec. (a).*
- **Q13 — Tower and table during V1 teleop and Cloud-Inferenz.** (a) Unguarded as Rule §2 says for the
  OMX (collision stop only); (b) a new per-tick check (the 75 ms tower guard is too slow for 10 ms ticks).
  Plus: the teleop-start sync glide is planned by the engine through the floor ladder and the tower guard
  — confirm. *Rec. (a) + guarded sync glide.*
- **Q14 — Orderly end with an energised arm** (app close, "Stoppen"). (a) Keep holding until the
  long-idle damping release (risk: joint 3 at HOME near its trip, RG-H1); (b) "Ablegen": a planned glide
  to a rest pose on the table, away from the tower, then torque off; (c) torque off at once after the
  tower question, as today. *Rec. (b), with (c) when the glide is refused.*
- **Q15 — Tracking/wrong-way watchdog action.** (a) Wrong-way runaway → torque off that joint (as the
  post-energise abort); tracking error while moving → stop and hold; (b) always stop and hold, never
  torque off. *Rec. (a).*
- **Q16 — V1 collision recovery pose.** (a) The V1 HOME (as `safe_home_arm_rad` mirrors it today);
  (b) a separate recovery pose. *Rec. (a) until RG-H1 decides HOME.*

### Recording and inference (blocks Phase 5)
- **Q17 — The recorded action.** (a) The goal the PC sent (after the leader→follower mapping and the
  engine's clamps), stamped with the leader sample's board time; (b) the goal the follower firmware wrote
  (after its edge clamp), echoed in STATE with the follower's board time. PLAN says (a), FINDINGS #11 says
  (b). *Rec. (a), with the engine applying the firmware's clamp rules so sent = written by construction.*
- **Q18 — Recording without teleop.** (a) Refused (Start needs teleop on); (b) allowed with a
  hand-guided follower. *Rec. (a).*
- **Q19 — O9, Cloud-Inferenz chunk blending.** (a) Keep today's cloud schedule (overlap blend, one
  action per tick); (b) plain chunk execution. *Rec. (a).*
- **Q20 — Removal list, beyond PLAN §2.5.** Also remove: (i) MyModels "download to the robot"
  (`/huggingface/control` download; its only consumer was local inference); (ii) the OMX leader joystick
  buttons (or add V1 leader buttons); (iii) `/training/status`, `/training/command`,
  `/training/get_training_info`, `/browse_file`, `/dataset/get_info`, `/get_saved_policies`,
  `/get_policy_list`. Keep the Training page's robot-side lists. *Rec. remove (i)–(iii).*

### Windows app (blocks Phase 4 and 7)
- **Q21 — Home of today's tkinter flows** (arm connection, camera roles, tower camera probe, cloud-only
  choice, start/stop, factory reset, Protokoll/diagnosis export, update modal, legal links).
  (a) A React System page served by the engine with the Pi agent's `/api/system` contract (also what an
  iPad screen sees); (b) a small Python/native window before the React window; (c) no setup screen,
  everything automatic. Also: keep an explicit "Umgebung starten" (nothing energises before it) — yes/no.
  *Rec. (a) + yes.*
- **Q22 — iPad screen mode.** Who switches it on (IT policy from the per-machine MSI, or a teacher
  setting); access (Pi robot login for any role; a teacher "claim" with a setup code per PC, yes/no);
  local window and iPad at the same time (yes/no); how the iPad finds the station (URL + QR with a fixed
  machine-wide port). *Rec. IT policy; Pi login with claim; concurrent allowed; QR + fixed port.*
- **Q23 — O3, plain HTTP on the LAN.** (a) Accept as on the Pi; (b) require TLS. *Rec. (a), stated in
  the IT sheet.*
- **Q24 — N−1 rule.** Cloud and firmware protocol stay compatible with app N and N−1; app N−2 gets a
  blocking German notice for robot work but keeps cloud pages — yes/no.
- **Q25 — Firmware versions across apps.** (a) The app never downgrades firmware; firmware speaks
  protocol N and N−1; an older app says "App aktualisieren"; (b) every app flashes its own firmware.
  *Rec. (a).*
- **Q26 — Signing before the legal entity.** Pilot builds (a) unsigned on open pilot PCs; (b) an
  individual certificate in your name; (c) no pilot before the entity. *Rec. (a).*
- **Q27 — Sandbox fallback if S1–S5 fail.** (a) No Python/Java on the native app until a sandbox
  passes (Blockly only); (b) a WASM sandbox; (c) unsandboxed (breaks Rule §2 condition 4). *Rec. (a),
  then (b).*

### Phase 0 and process
- **Q28 — Phase 0 cost and release.** Per-user import gives every Windows account its own distro and
  its own Docker images (several GB each; the server image alone up to 7 GB). Accept for the pilot —
  yes/no. Phase 0 ships as patch 2.41.1 through `release.yml`, the hardware test and W6 — yes/no.
- **Q29 — Branch model.** Shared code (Daten portability, numpy 2) cannot ship into today's WSL images
  (cv_bridge needs numpy 1.26.4). (a) Cut `release/2.41` now for WSL fixes; main carries the native work;
  the cloud stays on main and serves both; (b) native code on main behind new directories until Phase 7.
  *Rec. (a).*
- **Q30 — Numeric pass bars** (teleop lag, the train-and-run success rate before the OMX is removed,
  collision trip force). (a) You set them now; (b) measured baselines on today's OMX where one exists,
  you approve at each gate. *Rec. (b).*
- **Q31 — "Teleop on refuses …, as today" is a redefinition** (section 6, A9). Today the gate is "a
  leader is powered" and covers exactly: real program start, Cloud-Inferenz start (and ends a running
  run), jog and manual home glide, hand-guide on, manual record start, replay. Not gated on the server:
  simulator programs and previews (React refuses previews through the `:8769` bridge), hand-guide off,
  capture pose, mark destination, the handheld calibration. Leader-teach *requires* a live leader.
  (a) Re-key exactly today's gated set on "teleop is on", keep the React preview refusal, leader-teach
  requires teleop on; (b) refuse every arm-moving action while teleop is on, simulator included.
  *Rec. (a).*
- **Q32 — Is "React unchanged" a hard rule or a default?** The code already needs React changes for the
  plan: V1 phase pictures (`robotAssets.js` is omx_full-only), the hard-coded `localhost:8769` control
  bridge (`utils/piMode.js`), the Pi-mode detection for the iPad screen mode, the touch fixes.
  (a) Default: small React changes allowed when a spec names each one; (b) hard rule: the engine
  emulates everything (incl. a listener on 8769 and the Pi agent surface). *Rec. (a).*

Open by design, not asked now: O1 (iPad remote jog vs tower guard), O2 (touch fixes), O4 (leader
design details), O5 (wrist camera), O7 (power size, HOME) — each is settled by the bench or a later spec.

---

## 6. Code-claim verification (independent agent, 138 tool uses, read-only)

Most code claims hold (counts, the wire surface, R1–R19 anchors, every driver constant, the Phase 0
deletion path, the provisioning registers, the sandbox facts). What did not hold, or is missing:

### A. Wrong or stale
| # | Claim | Code |
|---|---|---|
| A1 | 01 §2.3 "17 published topics" | 16 (the table has 16 rows) |
| A2 | 01 §0 "2 768 comment, 492 blank, 5 359 code lines" | sums to 8 619 (docstring blanks counted twice); 5 359 reproduces; ROS share 2.7–3.1 % by definition |
| A3 | "41 of 43 node-method test files pass without ROS (1 210)" | 47 files: 1 360 passed, 3 failed (`test_arrival_trim.py`), 2 did not collect — all missing `yaml`/`aiohttp`; substance holds, wording loose |
| A4 | 01 §2.8 prototype "latched `/task/status`" | `/task/status` is VOLATILE in both publishers (`communicator.py:399-403`, `collision_monitor.py:347`). Phase 4 must not latch it |
| A5 | 01 §6.1 "`leader_appears_active` and its 5 call sites" names 4 | 5 in the node (also `_leader_teach_sample` :6297, the mid-take abort) + the collision trip gate as a sixth consumer |
| A6 | 04 §2.1 "30 interface types" | 29 + Trigger |
| A7 | FINDINGS #19 "cameras and sizes match" | true only once the V1 gets a wrist camera named `gripper`; joint names (`gripper_joint_1` vs `RL_joint`) differ but are never compared; the refusal still has to be built |
| A8 | FINDINGS #20 tower-kit list | incomplete (C1) |
| A9 | PLAN §2.4 "Teleop on + program/inference/manual start — refused, as today" | today's predicate is "leader powered", the set of gated sites is specific (Q31) |
| A10 | 03 §7.6 `nice` row | `nice` is the spawned *program* (`daten/node_service.py:205`, `edit_worker.py:253`): on Windows downloads and edits fail outright with `FileNotFoundError`, not a priority loss |
| A11 | 02 joint 3 "at its trip (≈2.04 A vs 2.015 A)" | repo estimate is ~94 % (`KNOWN-ISSUES.md:88`); RG-H1 decides |
| A12 | PLAN §3 "1.3–2.3 ms" | derivable (6 servos, goal-only write, t_turn 20–100 µs) but not in 02's table |

### B. Contradictions between documents
- Topics 17 vs 16 → 16. Service types 30 vs 27 → 01 is right.
- `:8769` bridge: 03 says drop it with the OMX, 01 says keep its JSON contract. With a V1 leader
  `LeaderToggle.jsx` and the preview leader gate call `http://localhost:8769` (`utils/piMode.js:66`), so
  01 is right for this plan (Q32).

### C. Gaps the architect would trip over
1. **Tower-kit pieces missing from FINDINGS #20:** the capture side (`scene_split` split, `CaptureVerifier`,
   `selftest`, `_SceneCaptureWorker`, `_HelloSenderWorker`, `probe_scene_camera`, HELLO and ids 16/17);
   the profile key and its one value table, the statement topic from `activation_agent.py:193`;
   `arm_model.CalibratedIK`; `home_planner`'s tower rung; `go_to_observation_pose`'s ladder; the arrival
   trim; `program_start_gate` incl. `camera_not_tower`; the calibration store and `contract_hash`; the arm
   check as an internal program; the GUI's tower-fall question; Daten's eighth merge check; the cloud
   reserve's `scene_camera_profile`. Report 01 B11 is closer to complete.
2. **Stamp write fails on Windows:** `scene_provenance._write_into_meta` opens a directory with
   `os.open` unguarded (:195-199) → every `tower_v1` dataset create and stamp-copying edit fails.
3. **`fcntl` at module top** in `hub_sync.py:45` (imported by `daten/library.py`, `hub_reads.py`,
   `node_service.py`): the Daten sidecar and `DatenService` do not even import on Windows;
   `download_worker`'s cleaner script uses `fcntl` and `/proc`.
4. **iPad screen mode needs the Pi agent surface** (`utils/piMode.js:245`, `robotLocality.js:35`):
   `auth/state`, `auth/session` with tickets, `robot-key`, `update/check`, `status`, the roboter-studio
   routes, the System tab — or a React change.
5. **rosbridge error text is a contract:** React detects an older image by "does not exist"
   (`cloudInference/robotCalls.js:54`, `useDatenCommand.js:20`); missing from R1–R19.
6. **Dropping rosbag does not drop the field:** React sends `record_rosbag2` in every `task_info`;
   with unknown fields refused, `TaskInfo.msg` keeps it or React changes.
7. **Removing local inference must keep the shared parts:** the `'inference'` timer name,
   `operation_mode`, `on_inference`, `_preflight_contract`, `PHASE_INFERENCE_LOADING`,
   `set_action_duration_from_fps`.
8. **The V1's "leader" source today is the command rail** (`v1_studio_config.yaml` → the node hears its
   own writes); the leader stream must replace it, not add to it.
9. **02 §2.2 env knobs incomplete:** `EDUBOTICS_FEETECH_JOINT_SIGNS` (flips the fingerprint, needs
   re-provisioning) and `EDUBOTICS_HOME_FLOOR_TOL_M` read by the driver.
10. **`feetech_provision.py` contradicts itself:** comment :156-157 says registers 34–36 untouched, code
    :384 writes `Overload_Torque` 80. Fix before shared vectors are derived.
11. **Cross-robot refusal needs the dataset's `info.json` at reserve time** (model meta has no robot
    type, `hf_publish.py:266`); Daten's `hub_reads.py:456` other-robot refusal is the pattern.
12. **`daten/http_server._rig_robot_type`** falls back to `omx_f` on any exception (:450-456).
13. **Phase 0:** no executed PowerShell test covers the delete branch or `ERROR_FILE_EXISTS` today.

### D. LeRobot check behind Q17 (done after the agent)
`lerobot/scripts/lerobot_record.py` (the repo's 0.5.1 scratch copy and an installed 0.6.0 agree):
`action_values = act_processed_teleop` (the leader's reading through the identity processor) is what
`build_dataset_frame(…, prefix=ACTION)` saves; `_sent_action = robot.send_action(…)` (clamped by
`max_relative_target`) is computed and never used, although the comment above it says the sent action is
saved. LeRobot async inference's default aggregation is `weighted_average` = 0.3·old + 0.7·new, the same
as today's Cloud-Inferenz schedule; ACT's defaults are `chunk_size` 100, `n_action_steps` 100, no
temporal ensembling.

### E. Not checked
Windows, iPad, vendor, radio and legal claims; wheel resolution and sizes; the research agents' scratch
scripts (prototype, IDL generator, `bus_timing.py`, `gravity_headroom.py` live outside the repo);
the executed PowerShell tests (no `pwsh` here).

---

## 7. Owner answers (2026-11-24) — folded into PLAN.md

| Q | Answer |
|---|---|
| Q0 | At most three specs; Claude decides the division → PLAN §8 (A Phase 0; B Phases 1–3; C Phases 4–7) |
| Q1 | Leader on a plain Bus Servo Adapter (A), no firmware |
| Q2 | 12 V servos, one supply per board |
| Q3 | No stop button |
| Q4 | No eFuses; signed updates with rollback |
| Q5 | EduBotics flashes every HAT before shipping |
| Q6 | Gated firmware passthrough, EduBotics tool only |
| Q7 | Consultant before Bluetooth or a sale; pilot kits may go out first |
| Q8 | Fallback: leader on the follower HAT's bus |
| Q9 | Soft board for dev and CI only |
| Q10 | A far goal is executed as is |
| Q11 | Teleop resumes only by a student action + sync glide |
| Q12 | Left open (O10) |
| Q13 | Unguarded in teleop/inference; sync glide guarded |
| Q14 | Glide to a rest pose, then torque off |
| Q15 | Runaway → that joint off; tracking error → stop and hold |
| Q16 | Recovery glide to the V1 HOME |
| Q17 | Like LeRobot: the mapped leader reading, before any clamp (checked in LeRobot's code, §6 D) |
| Q18 | Recording needs teleop on |
| Q19 | Keep the chunk blending (LeRobot async's default) |
| Q20 | Remove the seven dead services, MyModels' robot download and the OMX joystick buttons; keep the Training page's robot lists |
| Q21 | React System page with the Pi agent's contract; keep an explicit „Umgebung starten“ |
| Q22 | IT policy only; Pi login without a claim step; concurrent with the PC window; QR + fixed port |
| Q23 | Plain HTTP accepted |
| Q24 | N and N−1; N−2 blocks robot work, keeps cloud pages |
| Q25 | Never downgrade firmware |
| Q26 | Unsigned pilot builds |
| Q27 | Confirm the failure first, then decide (O11) |
| Q28 | Per-user import accepted for the pilot; ships as 2.41.1 through the normal pipeline |
| Q29 | `release/2.41` for WSL fixes; main carries the native work |
| Q30 | OMX baselines; owner approves each number |
| Q31 | Today's gated set, re-keyed on "teleop is on" |
| Q32 | React changes allowed when a spec names each one |
