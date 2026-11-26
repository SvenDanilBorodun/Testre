# SPEC C — The native app (PLAN Phases 4–7)

Date 2026-11-25. Architect C. Base `main` @ `66382d27`. Proposed build branch `feat/native-c-app` (from
`66382d27`; not created by me). Decisions file: `PLAN.md` (the owner-agreed plan) + `VERIFICATION.md` §7
(owner answers Q0–Q32). They override `FINDINGS.md`, `BRIEF.md`, the research reports and `CLAUDE.md`;
the CODE overrides all of them (§10).

**Release numbers amended 2026-11-26 (owner, `SPEC-DECISIONS.md` Q1-A):** spec A's release is 2.41.2 (the arm-check fix shipped alone as 2.41.1); `A-BRANCH-CUT` and `A-WP-A8` therefore mean `v2.41.2`. Every „2.41.1" below that names spec A's release or the cut reads „2.41.2".

**Timing override (owner, 2026-11-25).** PLAN §8 wanted this spec written after spec B's Phase-1
results. It is written now. So every decision that depends on a spec-B result has a DEFAULT (the
expected result), an ALTERNATIVE (if it fails) and an `externalGates` entry on the affected packages
(§3.1). No number that spec B will measure is invented here; such numbers are named as gates.

**Claim labels.** CONFIRMED = I observed it on 2026-11-25 (§11 lists command and observation).
PLAUSIBLE = argued from code or a document. ASSUMPTION = taken as given, a named gate proves it.
Nothing in this spec ran on Windows, on an iPad or on a robot.

**Revision 2 (2026-11-25, after the owner's answers).** `SPEC-DECISIONS.md` (same folder) records the
owner's and the conductor's answers to §12.3 and the seam points settled with spec B; where it and
this spec differ, it wins. Folded in here: Q-C1 … Q-C12, spec B's Q-B2, Q-B3 and Q-B4, spec B
§8.1–§8.8 as revised (the heartbeat, `traj`, the clock mapping, the moved paths, the pilot firmware
artefact, the leader's limp provisioning) and spec A's WP-A8. §13 lists every change of this
revision. The spec now has 44 work packages (revision 1 had 42; my first returned index said 41, a
miscount).

**Revision 3 (2026-11-25, final round).** Owner Q-C13 = (b): `main` gets a CLOUD-ONLY release mode
instead of a refusal (C4-00 rewritten; C7-01 replaces the mode by the native one). Spec B settled
the four open seam points (SPEC-B §8.6, §8.9–§8.11): the fixture's shape is confirmed; `heartbeat()`
and `setpoint()` never block; SETPOINT has no stream watchdog, so C4-09 has no idle mode switch, and
a `setpoint_lost` latch ENDS a cloud run (conductor's decision); `firmware.pin` is `none` or a build
number and spec B's `fwcheck` is the one check. §13 lists these changes after revision 2's.

**Appendices (part of the spec):**

| File | Holds |
|---|---|
| `SPEC-C-appendix-compat-runtime.md` (Appendix 1) | The compatibility runtime; R1–R19 each with its implementation and differential test; reference numbers measured on real rclpy and real rosbridge; the rosbridge subset's contract; characterisation transcripts |
| `SPEC-C-appendix-tower-kit.md` (Appendix 2) | Every 2.41.0 tower-kit piece: today's home, native home, carrying package, tests |
| `SPEC-C-appendix-invariants.md` (Appendix 3) | Every CLAUDE.md invariant kept / changed / retired with its owner decision; unfenced invariants; the CLAUDE.md rewrite plan per section |
| `SPEC-C-appendix-system-api.md` (Appendix 4) | Process tree, trust zones, the station's paths, the `/api/system` contract route by route, every named React change (RC-1 … RC-12), settings and policy |
| `SPEC-C-appendix-wp-phase4-5.md` (Appendix 5) | Work packages C4-0R, C4-00 … C5-10 in full |
| `SPEC-C-appendix-wp-phase6-7.md` (Appendix 6) | Work packages C6-01 … C7-11 in full |

---

## 1. Scope

**In:** PLAN §6 Phases 4–7 and §8 row C. Engine step 1 (compatibility runtime, messages built from
the IDL, the arm-link client in place of the arm container, the rosbridge subset behind a session,
the capture process with the tower split and the wrist camera, the whole 2.41.0 tower kit, the System
page on the Pi agent's contract, the pywebview host with supervision) → recording, Daten and
Cloud-Inferenz on the V1 (profile flags, the teleop gates, the recorded action, the cross-robot
refusal, the Windows portability layer, the token in memory) → Roboter Studio and the sandbox →
product (two installers, signing hook, updater, N / N−1, the iPad screen mode, the cloud-host build,
the upload-first notice for schools that still have the old product — owner Q-C3: NO data import —,
removal of the OMX and the WSL stack, engine step 2).

**Out (spec B's):** everything below the seam — the board protocol, the firmware, provisioning, the
arm-link process and its client library, the soft board and bus simulator, every bench and Windows
spike. **Out (spec A's):** today's WSL installer, 2.41.1, cutting `release/2.41`.

**The cut inside "the engine".** PLAN §1 draws one engine box. This spec builds it as two processes,
a **station** (HTTP front, `/api/system`, sessions; always up) and a **robot tier** (today's node on
the runtime, rosbridge subset, arm bridge, capture ingest; up only between „Umgebung starten" and
„Stoppen"). Reasons, each from the code (Appendix 4 B1): a node crash is healed by a respawn without a
page reload today; a fresh node process per start is what gives every "per boot" invariant; the System
page must work with the robot tier down. The host still supervises ONE child (the station), as PLAN
§2.5 says.

## 2. Architecture in one page

```
native/                                   (spec B: protocol/ firmware/ armlink/ devboard/ bench/ spikes/)
  engine/
    edubotics_idl/        parser + class builder for .msg/.srv text                          C4-03
    compat_ros/           shim packages NAMED rclpy, std_msgs, …, physical_ai_interfaces     C4-03, C4-04
    edubotics_runtime/    the rclpy-shaped runtime (Appendix 1)                              C4-04
    edubotics_robot/      robot tier: bootstrap, control, rosbridge, mjpeg, internal_http,
                          capture_ingest, orderly_end, texts_de, arm_bridge/                 C4-05 … C5-04
    edubotics_station/    station: front, zones, session, ports, static, proxy, system_api,
                          settings, policy, supervisor, robot_env, robot_login,
                          legacy_product (read-only notice, no import)                       C4-07 … C7-05
    edubotics_sources.py  the table of repo paths the native packages import from            C4-05
  capture/edubotics_capture/     the capture process                                         C4-10
  host/edubotics_host/           window, supervisor, session bootstrap, updater, close guard C4-12, C7-02
  sandbox/edubotics_sandbox/     broker, AppContainer + Job backend                          C6-03
  packaging/                     locks, build.ps1, launcher, installer, sign.ps1, parity     C4-01, C7-01
  ci/native-proof.yml            the proof workflow's SOURCE (not a workflow on main)        C4-01
  tests/                         pytest; differential/, characterisation/, e2e/, fixtures/
                                 (fixtures/rail_messages_v1.json + tools/make_rail_fixture.py:
                                  spec B's driver-parity input)                              C4-0R
```

The engine's LOGIC stays where it is: `physical_ai_tools/physical_ai_server/physical_ai_server/` runs
unchanged on the runtime. Files of the arm container and the old GUI that the native app needs
(`feetech_arm_node.py`'s decisions, `feetech_geometry.py`, `scene_split.py`, `camera_ingest_node.py`,
`activation_agent.py`'s pure functions, `camera_bridge.py`, `win_camera.py`, the runner tree) are
IMPORTED from where they are through `edubotics_sources.py` until C7-06 moves them with `git mv`. No
third copy of a twin is ever made.

Data flow of one recording frame: capture process → `>BIQ` over loopback TCP with a preamble secret →
`NativeCameraIngest` → bus topic → sensor node → `Communicator` history → `SlotSampler`; arm link →
`on_follower` / `on_leader` → `/joint_states` and `/leader/action` on the bus, each stamped with
the sample's own time on the engine's monotonic clock (SPEC-B §8.7) → the same histories.
One command: node → `/leader/joint_trajectory` on the bus → `arm_bridge/rail.py` → the message
unchanged into `traj(joint_names, points, …)`, or its last point into `setpoint()` during inference. One browser message: WebView2 → station (cookie, Host, Origin) → relay → rosbridge subset
→ bus.

## 3. Spec B: what this spec depends on, and what it requires

### 3.1 Results this spec waits for (`bDependencies`)

| Result (spec B) | Default assumption | If it fails | Gated packages |
|---|---|---|---|
| Arm-link client API and dev IPC (B14), soft board (B08) | SPEC-B §8.2–§8.8 as revised on 2026-11-25 (heartbeat §8.5, `traj` §8.6, clock mapping §8.7) | only `arm_bridge/link_adapter.py` changes; `link_iface.py` is C's | C4-09, C5-01 … C5-04, C6-01 |
| Seam freeze: protocol 1, IPC 1 (B18) | append-only afterwards | regenerate; N / N−1 rule applies | C7-01, C7-02 |
| S1–S5, S-J (sandbox) | AppContainer + Job + named pipes pass as a standard user | confirm the failure, then the owner decides O11 (Blockly only, WASM, …): C6-02 … C6-04 are replaced, never silently weakened | C6-02, C6-03, C6-04, C7-01 (JDK) |
| S7 (stack imports and records), S8 (size) | the curated cp313 lock works on the embeddable CPython; **CONFIRMED today only that it RESOLVES** (97 packages, numpy 2.2.6, opencv-python-headless 4.13.0.92, no torchcodec) | a missing wheel → another version inside LeRobot's range; the embeddable layout breaks DLL loading → the full python.org layout | C4-01, C7-01 |
| S16 (MSMF in the headless OpenCV wheel) | one OpenCV provider for every process | the capture process alone gets `opencv-python` in its own site directory | C4-10 |
| S10 (Daten with a concurrent reader), S9 (paths) | quiesce the sidecar's readers + retry is enough | generation directories + a pointer file, inside `hub_sync.swap_in` / `recover` | C5-05 |
| S11 (timer jitter) | `time.sleep` ticker + `timeBeginPeriod(1)` + throttling opt-out meets the bar spec B records | a high-resolution waitable timer behind `runtime/timer.py::_wait_until` | C4-04, C5-01 |
| S11's cross-process check (`B-S11-XPROC`): two processes' `time.monotonic_ns()` are one clock on Windows (SPEC-B §8.4, B04) | one system-wide clock (CONFIRMED on macOS by spec B's probe; QPC on Windows PLAUSIBLE) | `arm_bridge/clock_offset.py` estimates the offset per connection with an IPC round-trip bracket and adds it before the mapping; the estimate's bound joins the teleop-lag record | C5-01 |
| S12 (a second Windows account) | 401, no data | fix in `zones.py` before anything else ships | C4-07 |
| S13 (CH343 without admin, no reset on open) | both ports open for a standard user | (a) driver policy → an IT-sheet line; (b) reset on open → the event `boot` already forces a new activation (C4-09) | C4-09, C7-01 |
| ~~S14 (WSL export)~~ | dropped by spec B after owner Q-C3 (no data import) | — | none |
| Standard-user harness (`native/spikes/windows/run_as_standard_user.ps1`, B04) | reused | C writes `native/ci/run_as_standard_user.ps1` | C4-01, C5-08, C6-04 |
| Link-loss bench (B4) | the board stops and holds after about 1 s; teleop `LOST` | only C's sentences and waits change | C5-02 |
| Stall-smoothness bench | a far goal is executed as is (owner Q10) | the owner decides; nothing in C changes (the recorder's clocks no longer depend on it) | none (was C5-01) |
| Teleop-lag bench, O8; its bar is set against the OMX teleop-lag baseline, which still exists | relay in the arm link; leader samples stamped by the arm link | owner's fallback: leader on the HAT's bus → `/leader/action` from the board's state frame, its stamp through the follower's mapping | C5-01, C5-02 |
| Collision bench (B6) + O10 | scope = teleop and recording (the board's compiled `{TELEOP}`) | widened by `config_tighten('collision_scope_add', …)`; C5-03's mode table grows; new sign-off | C5-03 |
| O4 (leader mapping) | spec B's `mapping.py` delivers `action_rad`; opaque to the engine | a calibration step on the System page — not in this spec | C5-01, C5-09 |
| O5 (wrist camera), RG-H1 / O7 (HOME) | role `gripper`, 640 × 480; HOME as today | guard-model or contract change = ask-first, not pre-approved | C5-01, C5-04 |
| The update path `firmware_update` (B10) | as SPEC-B §8.3 | no update UI in the first release | C7-02 (`B-FIRMWARE-UPDATE`) |
| The pilot firmware artefact (`B-FIRMWARE-ARTIFACT`): the first image made with `sign_release.sh --channel pilot` and its committed `native/firmware/releases/<build_number>/SIGNED-MANIFEST.json` (SPEC-B §8.1). It cannot exist before B18 AND the owner's answer to spec B's release gate `OWNER-FW-CI` | one owner-signed `prod` image; the binary is a release asset in the public release channel (`hat-a-fw-b<n>` / `edubotics-hat_a-prod-b<n>.bin`, uploaded by the owner), not in the repository; `firmware.pin` is `none` or the build number, the URL is derived, spec B's `fwcheck.verify_signed_image` is the one check (SPEC-B §8.11); C's allowlist is generated from the manifest | until it exists a development build embeds no firmware (`firmware.pin` = `none`, `firmware_embedded: false`) and a TAGGED native build is refused | C7-01, C7-02 |
| The leader is provisioned limp (`Max_Torque_Limit = 0`, owner Q-B2) and checked before every PREPARE (SPEC-B §8.4) | a leader that is not limp is refused with `leader_fingerprint` (`fp_code` 209 `max_torque` or 218 `torque_limit_ram`) or `leader_torqued`, each with spec B's German sentence | — (C shows the sentence and never writes the leader) | C4-09 (System page rows), C5-02 (activation, leader-enable) |

**The reverse dependency.** Spec B's B13 waits for ONE file of this spec:
`native/tests/fixtures/rail_messages_v1.json` (SPEC-B §8.6). It is delivered by C4-0R, the first
package, which has no dependency and no gate.

### 3.2 Interface requirements on spec B (what the engine needs from the arm link)

Derived from the node's consumers, not from spec B's text. R-n marks a requirement; "met" refers to
SPEC-B §8 as revised on 2026-11-25, after the seam points were settled (§12.2 lists what is open).

1. **R-1 State stream.** Follower position, velocity, signed load fraction, per-servo status, torque
   mask, mode / command source, latch mask, board time (64-bit µs) and PC arrival (monotonic ns), in
   order, no duplicates, at the rate spec B measures; nothing lost across an engine stall of 2 s
   (`SOURCE_STOPPED_S`). *Met* (`on_follower`).
2. **R-2 The recorded action.** For every relayed leader sample: the mapped reading BEFORE any clamp
   and its PC arrival time, delivered also when the sent goal was clamped. *Met* (`on_leader.action_rad`).
3. **R-3 Monotonic time comparable across processes.** `arrival_mono_ns` and the engine's
   `time.monotonic_ns()` are one clock. *Stated by spec B now* (§8.4); proven on Windows by spike
   S11's bracket test (gate `B-S11-XPROC`), fenced on every CI leg by C5-01's two-process test.
4. **R-4 Trajectory with today's semantics.** A message of n points with the points' own
   `time_from_start` (the first is usually > 0) replaces what runs. *Met* (§8.6):
   `traj(joint_names, points, …)` accepts exactly what the driver's `_trajectory_cb` accepts; the
   proof input is C's fixture (C4-0R).
5. **R-5 Setpoints** for inference, one per tick, never buffered. *Met* (`mode('setpoint')`, `setpoint`).
6. **R-6 Torque** on with `Goal = Present` seeding, off always; a refusal carries a code and the
   German sentence. *Met* (`energise`, `torque_off`, `Result.sentence_de`).
7. **R-7 Teleop is a state** the engine can read and must be able to trust for every gate; start
   needs a fresh snapshot for planning; it never resumes by itself. *Met* (`teleop_prepare`,
   `teleop_on`, `on_teleop_state`).
8. **R-8 Events** for every stop the board decides (collision, tracking stop, runaway torque-off,
   link loss, firmware restart, trajectory refused / done), each with a code and sentence. *Met in
   kind* (`on_event`, latches, `clear_latch`); the exact code list is spec B's appendix.
9. **R-9 Discovery verdicts** for the System page: follower found / not provisioned / unconverted,
   leader found / missing, port busy, app too old, firmware too old. *Met* (`on_link_state`,
   `firmware_info`).
10. **R-10 A fake for tests** behind the same client API on Linux, macOS and Windows. *Met on POSIX*
    (soft board through `cc`); on Windows C uses its own `fake_link.py`.
11. **R-11 One starter, one client.** The station starts the process; the robot tier is the client;
    a scan with the robot tier down uses its own short-lived process. *Compatible* with "one client at
    a time; restart = new process + new authkey".
12. **R-12 The heartbeat rule must not trip on the node's legitimate blocking.** The default callback
    group blocks for seconds by design (R1–R3). *Settled* (§8.5): a 20 Hz timer in its own
    MutuallyExclusive group on the main executor, sent only while `_sensor_alive_mono` is younger
    than 0.25 s; the board's watchdog is 1.0 s. C fences its thread budget (C4-09).
13. **R-13 Source stamps.** Follower samples carry `board_us` and the arm link's `arrival_mono_ns`,
    leader samples `request_mono_ns` and `arrival_mono_ns`. *Settled* (§8.7): the unchanged `ClockMap`
    fed those two follower numbers; the leader's mid-point used directly.
14. **R-14 `heartbeat()` must not block its caller.** A call that can wait for the arm-link process
    (a full pipe, a reply) would hold the heartbeat group's only callback. *Met* (SPEC-B §8.9):
    `heartbeat()` and `setpoint()` never block and never raise — one latest-wins slot each, a writer
    thread owns the pipe, `True` = handed over, `False` = the arm link is gone (code 69
    `armlink_gone`); request methods wait at most 2.0 s. C's adapter adds no drop layer.
15. **R-15 SETPOINT mode must survive Cloud-Inferenz's legitimate holds.** The cloud tick may send
    nothing for up to 15 s (`stall_end_s`). *Settled, not as I proposed* (SPEC-B §8.10): SETPOINT has
    NO stream watchdog; it holds its last goal through any gap while heartbeats arrive and latches
    `setpoint_lost` 1.0 s after they stop. So C4-09 stays in `setpoint` for the whole run, and a
    `setpoint_lost` latch ENDS the run through the node's one `_end_inference` with a German sentence
    (conductor's decision).

## 4. Work packages

### 4.1 Test command and gate ids

| Id | Command (from the repo root unless a `cd` is shown) |
|---|---|
| T-NATIVE | `python -m pytest native/tests -q -p no:cacheprovider` (+ the paths a package names); interpreter = Python 3.13 with `native/packaging/requirements-engine.*.lock` installed; the dependency-free subset also runs as `cd robotis_ai_setup && python -m unittest discover -s tests -p "test_native_c_suite.py"` |
| T-SERVER | `cd physical_ai_tools/physical_ai_server && python -m pytest test/ -q -n auto --dist loadfile -p no:cacheprovider` |
| T-DEPSFREE | `cd robotis_ai_setup && python -m unittest discover -s tests` (or `-p "<file>"`) |
| T-CLOUD | `cd robotis_ai_setup/cloud_training_api && SUPABASE_URL=http://ci.test SUPABASE_SERVICE_ROLE_KEY=ci_test MODAL_TOKEN_ID=ci_test MODAL_TOKEN_SECRET=ci_test EDUBOTICS_HF_TOKEN_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= python -m unittest discover -s app/tests` |
| T-REACT | `cd physical_ai_tools/physical_ai_manager && npm test` (the whole suite) |
| T-ESLINT | `cd physical_ai_tools/physical_ai_manager && npx eslint --ext .js,.jsx --max-warnings 0 src/` |
| T-DIFF | `bash native/tests/differential/record_reference.sh --check` (needs Docker; pulls the pinned reference image) then `python -m pytest native/tests/test_differential.py -q` |
| T-WIN | the `native-windows` leg (and the named job) of `native/ci/native-proof.yml`: on `main` once C4-01b has landed, otherwise on a branch `spike/c-<package>-<yyyymmdd>-<n>` = the package head + ONE commit that copies the file to `.github/workflows/` (message `spike: enable the native proof workflow (never merge)`). Pushed by the build session (owner pre-approved spike pushes; no merge, tag or deploy); the run's summary is copied to `native/ci/results/<package>/` |

| Gate id | Meaning |
|---|---|
| `A-BRANCH-CUT`, `A-WP-A8`, `A-RELEASE-241-GREEN` | spec A: 2.41.1 shipped and `release/2.41` cut; WP-A8 (CI on `release/**`, the maintenance mode of `release.yml`) is merged on `main` and so in `v2.41.1` (revision 1 called this `A-Q3-PIPELINE`); a 2.41.x tag there builds without `main` |
| `B-…` | the spec-B results of §3.1 (`B-S1` … `B-S13`, `B-S16`, `B-SJ`, `B-S11-XPROC`, `B-ARMLINK-API`, `B-SOFTBOARD`, `B-PROTOCOL-FREEZE`, `B-STDUSER-HARNESS`, `B-BENCH-LINKLOSS`, `B-BENCH-TELEOPLAG`, `B-BENCH-COLLISION`, `B-FIRMWARE-ARTIFACT`, `B-FIRMWARE-UPDATE`). `B-S14` and `B-BENCH-STALL` gate nothing any more |
| `OWNER-FW-CI` | spec B's release gate (asked when B18 merges): the owner's answer to "a `native-firmware` job in `ci.yml` before the first pilot firmware?"; `sign_release.sh --channel pilot` refuses until it is recorded, so `B-FIRMWARE-ARTIFACT` cannot exist before it |
| `O4`, `O5`, `O5-geometry`, `O7/RG-H1`, `O10`, `O11` | PLAN §4 open points |
| `OWNER-SIGNOFF-*` | a Rule §2 sign-off the owner gave in principle, conditional on its bench (PLAN §2.4): `BOOT`, `TELEOP-GATES`, `COLLISION`, `REST-POSE`, `SANDBOX` |
| `OWNER-CI-EDIT`, `OWNER-CI-REASK`, `OWNER-MERGE-CLOUD`, `OWNER-UPDATE-KEY`, `OWNER-POLICY`, `OWNER-RAILWAY-SERVICE`, `OWNER-RENDER-TOOL`, `OWNER-GO-REMOVAL` | owner decisions or actions of §7 and §12.3. `OWNER-CI-REASK`: Q-C8 is put to the owner again before C4-12. `OWNER-UPDATE-KEY`: he generates the Ed25519 pair offline and hands over the public key (Q-C4). `OWNER-RAILWAY-SERVICE`: he creates the new service (Q-C9). `OWNER-GO-REMOVAL` carries Q-C12 (`pi_agent/` on `main`). Gone since revision 1: `OWNER-INSTALLER-TECH` (Q-C2 decided), `OWNER-IMPORT-SCOPE` (Q-C3 decided), `OWNER-LICENCE-REVIEW` (Q-C10: a condition of non-pilot distribution, not of a package) |
| `SPIKE-RUN` | a T-WIN run on a spike branch (build session pushes) |
| `RIG-V1-HAT`, `SCHOOL-PC`, `SCHOOL-LAN`, `IPAD` | needs the converted V1 with the HAT and the tower kit; a locked-down school PC with a standard account; a school network; an iPad 9 and a USB-C iPad |
| `TRIAL-PASSED` | record → train → Cloud-Inferenz on the V1, judged by the owner at the gate. There is NO measured OMX train-and-run baseline (owner, spec B's Q-B4: skipped); this amends PLAN §5's "Pass bars" for this gate. The protocol and what is recorded: `docs/NATIVE-TRIAL.md` (C5-10) |

### 4.2 Index (full text in Appendices 5 and 6)

Proof column: **M** this macOS host can prove it; **L** today's `python-tests` (directly, or through
the bridge test); **W** a Windows proof run; **R** rig; **S** school PC; **I** iPad; **O** owner
sign-off or action.

| Id | Title | dependsOn | externalGates | risk | Proof |
|---|---|---|---|---|---|
| C4-0R | The rail-message fixture that spec B's driver-parity test reads | — | — (it is B13's gate) | medium | M, L |
| C4-00 | `main` stops building the WSL product; a tag on `main` is a cloud-only release (owner Q-C1, Q-C13 = b) | — | `A-BRANCH-CUT`, `A-WP-A8` | high | M, O |
| C4-01 | Native Python stack: hash lock, proof workflow, server suite green on it | C4-00 | `B-S7`, `B-S8`, `B-STDUSER-HARNESS`, `SPIKE-RUN` | high | W (+M for the shape tests) |
| C4-01b | The proof workflow runs on `main` | C4-01 | `OWNER-CI-EDIT` (Q-C8: not now) | high | M, O |
| C4-02 | Remove what the owner removed | C4-01 | — | high | M, L |
| C4-03 | Messages built from the IDL text at import | C4-02 | — | medium | M, L |
| C4-04 | The compatibility runtime and its differential proof | C4-03 | `B-S11`, `SPIKE-RUN` | high | M (Docker), L, W |
| C4-05 | The robot tier boots: today's node on the runtime | C4-04 | `SPIKE-RUN` | high | M (a venv with LeRobot 0.5.1), W |
| C4-05b | The recorder and Daten smokes on the native stack (Linux leg) | C4-05 | `SPIKE-RUN` | medium | M (a venv with LeRobot 0.5.1), W (Linux leg) |
| C4-06 | The rosbridge subset and the MJPEG source | C4-05 | `SPIKE-RUN` | high | M (Docker), W |
| C4-07 | The station front: zones, sessions, static files, proxies | C4-06 | `B-S12`, `SPIKE-RUN` | high | M, L, W |
| C4-08a | The station's `/api/system`, settings, ownership of the robot tier | C4-07 | — | high | M, L |
| C4-08b | React: station mode (RC-1 … RC-6) | C4-08a | — | medium | M |
| C4-09 | The arm bridge (heartbeat, rail, stamps, thread budget; a lost link ends a cloud run) | C4-05, C4-08a | `B-ARMLINK-API`, `B-SOFTBOARD`, `OWNER-SIGNOFF-BOOT`, `RIG-V1-HAT` | high | M (fake + soft board), R, O |
| C4-10 | The capture process and its ingest | C4-08a | `B-S16`, `SPIKE-RUN`, `O5` | high | M (fakes), W, S |
| C4-11 | The tower kit is whole: inventory gate | C4-09, C4-10 | `RIG-V1-HAT` | medium | M, R |
| C4-12 | The host; the Phase 4 end-to-end run | C4-08b, C4-11 | `SPIKE-RUN`, `SCHOOL-PC`, `RIG-V1-HAT`, `OWNER-CI-REASK` | high | W, M (logic + Playwright), S, R |
| C4-13 | Phase 4 documentation | C4-12 | — | low | M |
| C5-01 | The V1 records: flags, wrist camera, recorded action, source-stamped arm data | C4-11 | `B-BENCH-TELEOPLAG`, `B-S11-XPROC`, `B-S11`, `O4`, `O5-geometry`, `B-S13` | high | M (sim), R |
| C5-02 | Teleop is a state: `home → sync → teleop`, gates, link loss, the leader's limp check shown | C5-01 | `B-BENCH-LINKLOSS`, `OWNER-SIGNOFF-TELEOP-GATES`, `RIG-V1-HAT` | high | M (sim), R, O |
| C5-03 | The collision stop's engine half | C5-02 | `B-BENCH-COLLISION`, `O10`, `OWNER-SIGNOFF-COLLISION` | high | M (sim), R, O |
| C5-04 | The orderly end: rest pose, then torque off | C5-02 | `O7/RG-H1`, `OWNER-SIGNOFF-REST-POSE`, `RIG-V1-HAT` | high | M (planner proof), R, O |
| C5-05 | Daten and the recorder on Windows: portability layer | C4-11 | `B-S10`, `B-S9`, `SPIKE-RUN` | high | M (POSIX unchanged), W |
| C5-06 | The Hugging Face token lives in memory | C5-05 | — | high | M, W |
| C5-07 | A model from another robot is refused | C5-01 | `OWNER-MERGE-CLOUD` | high | M, L |
| C5-08 | Recorder and Daten smokes on Windows as a standard user | C4-05b, C5-05, C5-06 | `B-STDUSER-HARNESS`, `SPIKE-RUN` | medium | W |
| C5-09 | V1 pictures (RC-9) | C5-01 | `O4`, `OWNER-RENDER-TOOL` | low | M |
| C5-10 | Phase 5 documentation and the trial protocol (the bar is the owner's judgement) | C5-08 | — | low | M |
| C6-01 | Roboter Studio and the manual modes on the arm bridge | C5-02 | `B-ARMLINK-API`, `RIG-V1-HAT` | high | M (sim), R |
| C6-02 | The code RPC over named pipes on Windows | C6-01 | `B-S4`, `SPIKE-RUN` | high | M (POSIX), W |
| C6-03 | The Windows sandbox: broker, AppContainer, Job, JDK | C6-02 | `B-S1`, `B-S2`, `B-S3`, `B-S5`, `B-SJ`, `O11`, `OWNER-SIGNOFF-SANDBOX`, `SPIKE-RUN`, `SCHOOL-PC` | high | W, S, O |
| C6-04 | Rule §2's four conditions re-proven; code smoke on Windows | C6-03 | `B-STDUSER-HARNESS`, `OWNER-SIGNOFF-SANDBOX`, `SPIKE-RUN` | high | W, O |
| C6-05 | Phase 6 documentation | C6-04 | — | low | M |
| C7-01 | Packaging: one build, two installers, signing hook, parity gate, the embedded pilot firmware | C5-07, C5-08, C6-04 | `B-PROTOCOL-FREEZE`, `B-S7`, `B-S8`, `B-SJ`, `B-FIRMWARE-ARTIFACT`, `OWNER-FW-CI`, `OWNER-CI-EDIT`, `SCHOOL-PC` | high | W, S, O |
| C7-02 | Update channel (owner-signed manifest), N / N−1, firmware updates | C7-01 | `OWNER-MERGE-CLOUD`, `OWNER-CI-EDIT`, `OWNER-UPDATE-KEY`, `B-FIRMWARE-UPDATE`, `B-FIRMWARE-ARTIFACT`, `OWNER-FW-CI`, `SCHOOL-PC` | high | M (cloud, logic), W, S, O |
| C7-03 | iPad as the station's screen: the lan zone | C7-01 | `IPAD`, `SCHOOL-LAN`, `OWNER-POLICY` | high | M (logic), I, O |
| C7-04 | The cloud-host build (RC-12) | C4-08b | `OWNER-CI-EDIT`, `OWNER-RAILWAY-SERVICE`, `IPAD` | medium | M, I, O |
| C7-05 | The old product is still on this PC: the upload-first notice (no data import) | C7-01 | `SCHOOL-PC` | high | M, S |
| C7-06 | Remove the WSL stack from `main` (whole-file moves; spec B's path readers carried) | C7-02, C7-05 | `TRIAL-PASSED`, `OWNER-GO-REMOVAL` (with Q-C12), `A-RELEASE-241-GREEN`, `OWNER-CI-EDIT` | high | M, L, W, O |
| C7-07 | Remove the OMX from the engine and the student build | C7-06 | `TRIAL-PASSED`, `OWNER-GO-REMOVAL` | high | M, L |
| C7-08 | Engine step 2a: honest imports | C7-07 | — | high | M, L, W |
| C7-09 | Engine step 2b: the lane table | C7-08 | — | high | M, L, W |
| C7-10 | Engine step 2c: the node file by function | C7-09 | — | high | M, L |
| C7-11 | Documentation: CLAUDE.md rewritten | C7-10 | — | medium | M |

Lenses: every `high` package gets `correctness, delivery, regression`; `medium` the lenses its
appendix entry names; `low` gets `combined`.

**Phase exit gates (PLAN §6), mapped.** Phase 4: C4-12's Playwright run (CI half) + the same sequence
on the rig (`RIG-V1-HAT`). Phase 5: C5-08 (smokes) + `TRIAL-PASSED` (the owner's judgement, no OMX
number). Phase 6: C6-04 + `OWNER-SIGNOFF-SANDBOX`. Phase 7: C7-01's install as a standard user on a
clean Windows 11 (`SCHOOL-PC`) + C7-03's checklist (`IPAD`).

**The removal order (hard requirement).** C7-06 and C7-07 run only after `TRIAL-PASSED` and
`OWNER-GO-REMOVAL`, after the upload-first notice and the switch guide have shipped (C7-05) and the
update channel exists (C7-02), and after `release/2.41` is proven able to build and release without
`main` (`A-RELEASE-241-GREEN`). Today's fleet is kept working by fences that exist BEFORE anything is
deleted: spec A's WP-A8 (the branch has its own CI and release path), C4-00 (a tag on `main` builds
no client at all; the client jobs open only for `client_build == 'true'`), `test_legacy_client_contract.py` (C7-06: the cloud keeps every request a 2.41.x
client makes), and C7-02's test that the old `/version` keys are byte-identical. Nothing of the old
product on a school PC is read, copied or deleted by the native app (C7-05, C7-01).

## 5. What this round does not do (and the trigger that brings it in)

| Not done | Trigger |
|---|---|
| A typed engine API instead of the rosbridge subset | owner decision after C7-10 (PLAN §2.5 "a typed interface later") |
| Removing `/huggingface/control` entirely (only its `download` mode goes) | `test_wire_inventory.py` shows no caller after C4-02 → step 2 |
| A faster on-screen stop (`stop('path')` on „Stopp") | bench shows the ≤ 1 s chunk tail is too long; needs a Rule §2 note (it changes when the arm stops) |
| Catching callback exceptions instead of exit → respawn | owner decision; it changes crash behaviour (Appendix 1 A3) |
| Changing the tower guard's arm model, `V1_LINK_BOXES`, any `TOWER_*` constant for the wrist camera | O5 delivers a camera that leaves box 5 → ask-first |
| Touch fixes for the iPad (O2) | the checklist of C7-03 → the owner picks |
| The iPad remote app, Bluetooth | PLAN tracks beside the phases; O1 |
| A V1 engine on the Orange Pi; any Pi change | never (PLAN §2.3: frozen) |
| MSI packaging | a pilot school's IT requires it (Q-C2, decided by the conductor: Inno Setup for both channels) |
| ANY import from the WSL product: datasets, the arm check, settings (`wsl --export`, `tar` over `wsl.exe`, reading `\\wsl$`) | never by this plan (owner Q-C3: no copy, upload first); the owner reverses it |
| An installer page that shows the upload-first notice | pilot feedback says the System page and the guide are not seen |
| Carrying `EDUBOTICS_REQUIRE_ACTIVATION=0` into the native app | never (owner Q-C6) |
| The GPL licence review (x264 inside PyAV, the JDK) | before any NON-pilot distribution (owner Q-C10): an open release condition in `docs/KNOWN-ISSUES.md` and `docs/deploy/NATIVE-RELEASE.md`, not a package |
| A measured OMX train-and-run baseline | never (owner, spec B's Q-B4: skipped); the trial's bar is his judgement |
| A cloud-only release AFTER C7-01 (a `main` tag that skips the native build) | the owner asks for it; C7-01 removes the `cloud-only` value on purpose so one mode exists at a time |
| Skipping the release-channel check for a cloud-only tag (it uploads nothing, the check still runs) | the check blocks a cloud release that had nothing to do with the channel |
| Making `docker-publish.yml` and `release-installer.yml` refuse a hand dispatch on `main` | someone dispatches one on `main`; until then the runbook forbids it |
| Deleting the ROS node class inside the moved V1 driver file, or extracting its decisions into a new module | spec B freezes its vectors as checked-in data and drops its AST readers of that file |
| A reserved executor worker or a priority for the heartbeat | never as long as spec B's rule holds that a starved heartbeat must stop the arm |
| Windows code signing beyond the hook | the legal entity exists (PLAN §2.2) |
| TLS on the lan zone | owner reverses Q23 |
| A persisted erasure queue, the student export button, other open cloud work | unrelated; `docs/KNOWN-ISSUES.md` |
| Removing the OMX entries from the fixture contract block | never without re-checking every kit (it changes `contract_hash()`) |
| A second OpenCV provider | S16 fails |
| numpy / Python moves for the WSL images | `release/2.41` only |

## 6. Invariants and their fences

The full disposition is Appendix 3. The ones a coder is most likely to break, with the fence:

| Invariant | Fence |
|---|---|
| The node file is not edited in step 1 (path, method names, text) except where a package names the exact lines | `native/tests/test_node_file_is_where_tests_expect.py` (C4-04); `git diff --stat` in C4-05's acceptance |
| The shim never reaches a ROS image and is on `sys.path` only through `bootstrap.install()` | `native/tests/test_compat_isolated.py` |
| No rosbridge client can publish, advertise or serve | `test_rosbridge_subset.py` (Appendix 1 A5 row 10, CONFIRMED hole in real rosbridge) |
| `/task/status` is never latched | transcript case `task_status_is_not_replayed` |
| The default group's mutual exclusion (R1–R3) | differential r01–r03 + `test_lane_snapshot.py` |
| Nothing moves before `/edubotics/activate`; a refused glide leaves torque on | `test_activation_bridge.py`, `test_arm_bridge_home.py` |
| Teleop on refuses exactly the PLAN §2.4 set, and only that set | `test_teleop_gates.py` |
| The recorded action is the mapped leader reading before any clamp | `test_recorded_action_is_the_leader_reading.py` |
| A stop finalizes a running recording inside the budget chain | `native/tests/test_stop_budget.py`, the smoke's shutdown twins |
| Every tower-kit piece is live | `test_tower_kit_inventory.py` (Appendix 2) |
| The local zone is unreachable for another Windows account; the zones never accept each other's cookie | `test_front_local_zone.py`, `test_front_lan_zone.py`, gate `B-S12` |
| The token is in no file | `test_hf_token_memory.py` |
| The four conditions of the second execution path | C6-04 |
| `edubotics_devboard`, `native/bench`, `native/spikes` never ship | `test_no_devboard_import.py`, `app_source_parity.py` |
| The installed app is the repo | `app_source_parity.py` |
| The cloud keeps serving 2.41.x clients | `test_legacy_client_contract.py`, `test_version_native_keys.py` |
| The board gets a heartbeat only from an executor worker and only while the sensor loop's stamp is younger than 0.25 s (SPEC-B §8.5) | `test_arm_bridge_heartbeat.py` |
| The heartbeat group is never starved by legitimate load, and IS starved when every worker is stuck | `thread_budget.py` + `test_heartbeat_budget.py` (three tests) |
| A rail message reaches the arm link unchanged; what the producers emit is what spec B's parity test reads | `test_arm_bridge_rail.py`, `RAS/tests/test_native_rail_fixture.py` (C4-0R) |
| An engine stall never changes the spacing of arm samples in the recorder; a stamped leader sample is never moved; `ClockMap` and `SourceHistory` are not edited | `test_board_stamps_keep_spacing.py` (CONFIRMED red on today's path), the class-body hash in C5-01 |
| Cloud-Inferenz's holds latch nothing (SETPOINT has no stream watchdog) and need no mode switch | `test_arm_bridge_rail.py::test_a_fifteen_second_gap_latches_nothing` (soft board) |
| A `setpoint_lost` latch or a lost arm link ENDS a cloud run, once, in German, on the default group; nothing clears a latch but a student's new activation | `test_arm_bridge_rail.py::test_a_setpoint_lost_latch_ends_the_cloud_run_once_in_german` |
| A tag on `main` releases the cloud and builds no client; the mode comes from a committed marker, never from a missing value; a cloud-only tag is `v3.0.N` and has no GitHub Release | `test_deploy_approval_gate.py` (cloud-only arrivals, four mutations, `test_a_cloud_only_release_cannot_run_w4_w5_w5b`, `ReleaseProductIsDecidedByTheMarker`), `test_main_cloud_only_release.py` (C4-00 … C7-01) |
| The native app shares no id, no directory and no data root with the WSL product, starts no `wsl.exe`, and copies nothing from it | `test_packaging_shape.py::test_the_native_installer_shares_no_id_and_no_path_with_the_wsl_product`, `test_no_wsl_invocation.py`, `test_policy.py` |
| No private key is in the repository, in CI or in a build; firmware and update manifests are signed by the owner by hand | `test_update_manifest.py` (scan), C7-01 acceptance 8, spec B's `test_native_keys.py` |
| Only the one embedded, owner-signed pilot image can be sent to a board | generated `firmware_allowlist.py`, C7-02 acceptance 5 |

**Unfenced today, relied on here** (each gets its fence in the package named; Appendix 3 C4): the
non-latched `/task/status`; the node file's path as a test contract; `feetech_arm_node.py` importable
as a library; cross-process monotonic time; the default group's exclusion as a safety property;
`_sensor_alive_mono` as the heartbeat's input; a free executor worker; the nanosecond truncation of
`_trajectory_publisher`.
**Still unfenced after this spec:** the Windows budget of the tower guard (no number exists; C4-01
only records it); real camera capture on Windows (GitHub runners have no camera; `SCHOOL-PC` / rig);
the lan zone's plain HTTP (accepted by the owner).

## 7. Ask-first items (CLAUDE.md "When to ask the user") this work touches

Status after `SPEC-DECISIONS.md` (2026-11-25).

| Item | Where | Status |
|---|---|---|
| Editing CI/CD config | C4-00 | **Approved**: owner Q-C1 = yes (stop building the WSL product) and Q-C13 = (b) (the cloud-only release mode), for exactly the edits C4-00 lists: the `push` trigger of `docker-publish.yml`; in `release.yml` one output line, one step, one appended `if:` line and one new job; the marker file |
| | C4-01b | **Not now** (Q-C8, conductor: spike-branch proof runs); asked again before C4-12 (`OWNER-CI-REASK`) |
| | C7-01, C7-02, C7-04, C7-06 | **Not approved yet**: gate `OWNER-CI-EDIT` on each, raised when the package is due |
| `git push` | T-WIN runs | Pre-approved for `spike/*` branches only, by the build session; never by a coder or by me |
| Safety paths: the activation gate | C4-09 | In principle (P§2.4 "Boot", "Energise"), signed after the rig run. The `=0` rollback: **decided, owner Q-C6: not carried** |
| Safety paths: collision gating, the two HOME families and the V1's HOME as recovery pose | C5-03 | In principle (P§2.4, Q16), conditional on the collision bench and O10. Not blanket |
| Safety paths: the leader gates (HIGH-6, the manual block, the cloud refusal) re-keyed | C5-02 | Yes (P§2.4 "Teleop on blocks …", Q31), conditional on the link-loss bench |
| Safety paths: the fixture contract (the rest pose) and every constant the guards read | C5-04 | The rest pose joining the contract: yes (P§2.4 "Orderly end"). Its VALUE and the resulting `contract_hash()` change: owner signs (`OWNER-SIGNOFF-REST-POSE`) |
| Safety paths: the tower guard's model for the wrist camera | not touched | **Not pre-approved**; gate `O5-geometry` |
| Safety paths: the second execution path's sandbox | C6-02 … C6-04 | In principle (P§2.4 "Student-code sandbox"), conditional on S1–S5; a failure goes to O11 |
| Safety paths: Cloud-Inferenz's hold / stall / end rules | C4-09 | The hold rule is unchanged (on an empty queue the node publishes nothing; the board holds its last goal, SPEC-B §8.10). **One NEW end rule:** a `setpoint_lost` latch or a lost arm link ends the run through `_end_inference` — decided by the conductor on spec B's open point. It is the conservative direction (it only ever ends a run), but an end rule is on CLAUDE.md's ask-first list, so the owner confirms it with C4-09's sign-off (`OWNER-SIGNOFF-BOOT`) |
| The manual-mode arbiter / lock order, the motion-lock policy | not changed | C5-02's glide takes the EXISTING transient manual claim; no new lock, no new order |
| Renaming files other layers reference; env var names | C4-08a (`robot_env.py`), C5-05 (`EDUBOTICS_DATA_ROOT`), C7-06 (whole-file moves, and one file of spec B's — `native/protocol/reference_paths.py` — as SPEC-B §8.8 asks) | Implied by P§1 (no compose); each move is listed in its package |
| A merge to `main` that deploys the cloud API (W2 fires on the path) | C5-07, C7-02 | **Owner action** (`docs/deploy/MERGE-CHECKLIST.md`); both changes are additive, no migration |
| Production secrets | C7-02 (the update manifest's Ed25519 key) | **Decided, owner Q-C4**: he creates and holds the key offline and signs by hand; no CI secret; no agent creates, receives or holds a private key. `OWNER-UPDATE-KEY` is his action. The firmware keys are spec B's (Q-B3, same custody, `native/KEYS.md`) |
| Changing `GUI_RELEASE_REPO` / the update channel | C7-02 adds variables, changes none | The new workflow is production: owner starts it |
| The trainer image's production variable (`RUNPOD_TRAINER_IMAGE`) | no package sets it by hand; its one automated writer, `release.yml::trainer-image-var`, keeps running for every tag on `main` | **Decided, owner Q-C13 = (b)**: a cloud-only tag rebuilds the trainer, pins the variable and redeploys the API together (Rule §3's coupling); no freeze |
| Writing the policy key and a firewall rule | C7-03 (the per-machine installer, run by IT) | Yes in substance (P§2.3) |
| Railway: a new service | C7-04 | **Decided, owner Q-C9**: he creates it; `OWNER-RAILWAY-SERVICE` is his action, no workflow of this spec does it |
| `wsl --unregister`, volume deletion, anything inside the WSL product | never | C7-05 starts no `wsl.exe`, reads two registry keys and one file's existence, and copies nothing (owner Q-C3); C7-01 shares no path with it |
| Paid checks | none | The OMX train-and-run baseline is skipped (owner, spec B's Q-B4); the V1 trial's training is the owner's own run at the gate |
| Stopping a student's training; RunPod; Modal deploys; migrations | not touched | — |
| Reintroducing a software inference guard | none | — |

Blockers that still need the owner before their package: `OWNER-CI-EDIT` (C4-01b, C7-01, C7-02, C7-04,
C7-06), the `OWNER-SIGNOFF-*` set (C4-09's now includes the new cloud-run end rule), `O5-geometry`,
`OWNER-FW-CI` (spec B's, before any firmware can be embedded) and `OWNER-GO-REMOVAL`. No owner
question is open. None of the blockers stops C4-0R, or C4-00 … C4-08b once spec A's WP-A8 and the
branch cut exist.

## 8. What this machine cannot prove

Host: macOS arm64, Docker available, `python3` 3.13.13 (not 3.14.5 as the matrix says), conda env
`lerobot` (Python 3.12.13, numpy 2.2.6, OpenCV 4.13.0, **LeRobot 0.5.2 not 0.5.1, no `cryptography`,
no `pytest-xdist`, no `pupil_apriltags`**), no `pwsh`, no Windows, no robot, no iPad.

| Cannot be proven here | Packages whose real proof is elsewhere |
|---|---|
| Anything on Windows: pywebview / WebView2, AppContainer, Job Objects, named pipes with security descriptors, MSMF, `LockFileEx`, timer jitter, installers, the firewall rule | C4-01, C4-04 (timer leg), C4-07 (S12), C4-10, C4-12, C5-05, C5-08, C6-02, C6-03, C6-04, C7-01, C7-02, C7-03 → `SPIKE-RUN`, `SCHOOL-PC` |
| The server suite on CI's pinned stacks (1.26.4 leg; the native lock) | every package that edits the server package → CI |
| LeRobot-dependent tests at 0.5.1 | C4-05, C5-01, C5-08 → the native lock (a fresh venv on this host would do; I did not build one) |
| The real detector tests (`pupil_apriltags`) | C4-11 → CI (`EDUBOTICS_REQUIRE_PUPIL=1`) |
| The reference image (amd64, 7 GB) | C4-04 … C4-06 record against it in CI; here I used `ros:jazzy-ros-base` (arm64) |
| Anything on the arm: timing, teleop, collision, the rest pose, the arm check on the native app | C4-09, C4-11, C5-01 … C5-04, C6-01 → `RIG-V1-HAT` |
| Camera capture on real hardware | C4-10 → `SCHOOL-PC` / rig |
| A locked-down school PC (AppLocker, frozen PCs, device policies) | C4-12, C6-03, C7-01, C7-05 → `SCHOOL-PC` |
| A PC that has the WSL product installed: the detector's registry keys and file, both products side by side, what `wsl.exe` does where WSL is absent | C7-05, C7-01 → `SCHOOL-PC` |
| A real board's behaviour on `mode('hold')` from SETPOINT, and a heartbeat under a slow laptop's load | C4-09 → spec B's soft board (CI), then `RIG-V1-HAT` and `SCHOOL-PC` |
| The pilot firmware image (owner-signed, after `OWNER-FW-CI`) | C7-01, C7-02 → it does not exist yet |
| iPad Safari | C7-03, C7-04 → `IPAD` |
| The cloud suite on CI's pins (here 7 failures on Python 3.13 without FastAPI) | C5-07, C7-02 → CI |

## 9. Rollback

| Phase | What a single variable, setting or revert does |
|---|---|
| 4–6 | Nothing built here ships to a school from `main` before C7-01; rollback is `git revert` of the package. C4-00's revert restores the `docker-publish` trigger and the full release from a `main` tag, and is meaningful only before C4-02 (afterwards `main`'s server package no longer matches the ROS images). A bad cloud-only release is rolled back as a cloud release is today (W2 / W3 with `allow_rollback`, the trainer variable reset to the previous digest with `--skip-deploys`); there is no client to roll back. The WSL product is untouched on `release/2.41`, which ships through spec A's WP-A8 |
| 5 | `wrist_camera = false` (settings) returns to one camera; `EDUBOTICS_ARM_TRIM=0` and `EDUBOTICS_HOME_FLOOR_TOL_M=0` keep working (they are in `robot_env.FORWARDED`). The cloud reserve's two keys are additive: revert + W2 |
| 6 | The sandbox has no off switch (a code run without it would break Rule §2 condition 4); the rollback is "no code runs" — the station does not start the broker, and the node refuses in German (`runner_down`), Blockly keeps working |
| 7 | Per-user install: run the previous installer. An advertised version: start `publish-native-version` with the previous version. `LanMode = 0` closes the lan zone. `UpdateMode = managed` stops self-updates. A firmware is never rolled back by the app (owner Q25): spec B's roll-forward rule. A lost or exposed update key: a new key pair and a new app build carrying the new public key, installed by hand once (`native/KEYS.md`) — an installed app cannot be told to trust another key |
| 7 removal | `git revert` of C7-06 / C7-07 restores the trees; the WSL product never depended on `main` after C4-00 |

## 10. Code-truth corrections (documents vs. the code; the code wins)

1. **PLAN §3 item 6 / VERIFICATION §4.6** ("a page from a non-loopback host is a Pi, so the engine
   serves the Pi agent's contract") covers the iPad only. The PC's own window is a LOOPBACK page: the
   System tab is `piOnly` (`StudentApp.js`: `.filter((n) => piMode || !n.piOnly)`), and Pi mode cannot
   simply be switched on for it — `features/piAuth/piAuth.js::ensurePiSession` would demand a cloud
   ticket before any rosbridge connection (offline recording gone) and
   `utils/robotLocality.js::robotIsRemote` would flip. Hence RC-1's station mode. CONFIRMED by reading.
2. **PLAN §6 Phase 4 gate** ("the React build connects …") cannot be met by an unchanged build for the
   same reason; RC-1 … RC-6 are its precondition.
3. **research/01 §10** ("today's node … runs with import-level changes only") is not literally true:
   three module-top imports stop it — `communication/communicator.py` imports `rosbag_recorder.srv`,
   `data_processing/data_converter.py` imports `cv_bridge`, and `physical_ai_server.py` imports
   `InferenceManager` (torch and `lerobot.policies` at import). C4-02 and C4-05 remove them first.
   CONFIRMED (`grep`).
4. **research/01 §2.8** (prototype "latched `/task/status`"): both publishers are VOLATILE
   (`Communicator.init_publishers`; VERIFICATION §6 A4). CONFIRMED by reading.
5. **research/03 §3.1** ("`roboter_studio_control.py`: drop with the OMX"): `LeaderToggle.jsx`,
   `useRsBridgeStatus.js` and the preview leader gate use that JSON contract, and with a V1 leader it
   is the teleop switch. It is served same-origin (Appendix 4 B4).
6. **VERIFICATION §6 C2** is right and narrower than it reads: only
   `scene_provenance._write_into_meta` opens a directory unguarded; the same pattern in
   `workflow/autocal/store.py` and `hf_token_crypto.write_key_file` is already inside `except OSError`.
   CONFIRMED by reading; the Windows failure itself is PLAUSIBLE.
7. **VERIFICATION §6 C3** CONFIRMED by simulation (`fcntl` made unimportable): `hub_sync`,
   `daten.hub_reads`, `daten.library`, `daten.node_service` fail to import. A naive scan also flags
   `lerobot_dataset_wrapper` and `inference_manager` — a false positive (pyserial's POSIX backend).
8. **CLAUDE.md, v1_studio section** says the boot-home floor decision is
   `feetech_arm_node.boot_home_floor_decision` — correct; **research/01 §4.2 item 7** lists it under
   "stdlib geometry". All of `boot_home_floor_decision`, `boot_home_tower_decision`,
   `boot_home_resend_refusal`, `tower_glide_state`, `tower_fall`, `tower_state_payload`,
   `build_boot_home`, `boot_home_verify_decision` are module-level in `feetech_arm_node.py`, whose top
   imports rclpy; the engine imports that file as a library under the shim. CONFIRMED.
9. **CLAUDE.md, React section** ("rendered by the untracked `tools/frontend-harness/render-robot-pictures.sh`"):
   that directory does not exist in this checkout. C5-09 is gated on the owner providing it. CONFIRMED (`ls`).
10. **The test matrix handed to me** (2026-09-20): `python3` is 3.13.13 here, not 3.14.5; the `lerobot`
    env has no `pytest-xdist` (the server suite ran serially, 22 min 52 s), carries LeRobot 0.5.2 and
    no `cryptography`. CONFIRMED.
11. **CLAUDE.md "Common commands"** (gui suite "Ran 3390 … errors=5"): here `Ran 3218`, `errors=10`
    (nine `No module named 'yaml'`), `skipped=92` — environmental.
12. **`RUN/runner_limits.py`** imports `resource` at module top and `supervisor.py` imports it, so the
    supervisor's reusable parts cannot be imported on Windows as they are (research/03 §6 treats the
    supervisor as a design, not as importable code). CONFIRMED.
13. **research/03 §8.1** proposes that the capture process serves `/video/stream` so frames never
    transit the recorder's process. They transit it anyway (the recorder needs them); the robot tier
    serves MJPEG from the newest frame it already holds. Design choice, stated so nobody builds both.
14. **My own revision-1 claim** ("lifting `ClockMap.MAX_LAG_S` for the two board sources keeps a
    stalled engine's backlog spacing") was wrong beyond `WINDOW_S`: the deque is pruned by arrival
    time, as spec B's correction C12 says. CONFIRMED again here with the real `SourceHistory`: on
    today's path (arrival = engine receipt) a burst after stalls of 0.8, 1.5 and 3.0 s sits 0.005 ms
    apart; fed the sample's own time it stays 9.998–10.003 ms. C5-01 is rewritten on SPEC-B §8.7.
15. **My revision-1 premise for the heartbeat** (spec B's correction C13): the HF whoami wait is not
    on the default group — `/register_hf_user` and `/get_registered_hf_user` run on `_hf_cb_group`
    (Reentrant). The default group still blocks about 5.2 s inside `/workflow/start`. The
    thread-budget scenarios of C4-09 name the right groups.
16. **`%LOCALAPPDATA%\EduBotics` is not free.** The WSL GUI writes its `.env` and profile there
    (CONFIRMED, `gui/app/constants.py`), and spec A's D-A3 puts each account's WSL disk at
    `…\EduBotics\wsl\ext4.vhdx` from 2.41.1 on. Revision 1 made that folder the native data root,
    with an uninstaller that may delete the data root. Fixed: `%LOCALAPPDATA%\EduBoticsStation`, an
    own `AppId` (the WSL product's is `{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}`, its directory
    `{autopf}\EduBotics` — CONFIRMED), and a test (C7-01, Appendix 4 B6).
17. **CLAUDE.md, manual-mode arbiter** ("the Feetech arm must not get the hold"): true for the
    pre-energise use. `_hold_follower_at_measured_pose` has a second use — "after an aborted manual
    glide it stops the ≤ 1 s chunk still playing on either rail" (its docstring, CONFIRMED) — and that
    one-point message, with zero velocities at 0.05 s, reaches the V1 too. The rail fixture carries it.
18. **Nothing documents that `_trajectory_publisher` truncates `time_from_start`**
    (`int((t − int(t))·1e9)`). CONFIRMED by running it: 1/30 s becomes 33 333 333 ns, and the driver's
    own 0.02 s home grid has a smallest step of 19 999 999 ns. Spec B's parity with the driver depends
    on these exact values, hence a generated fixture and not a hand-written one.
19. **`RUNPOD_TRAINER_IMAGE` has exactly one automated writer**, `release.yml::trainer-image-var`
    (CONFIRMED by grep over `.github/workflows`). A `main` that refuses every tag, as revision 2's
    C4-00 did, would have left it without any path while maintenance mode skips W2a/W2b. Resolved by
    the owner (Q-C13 = b): a tag on `main` stays a release of the cloud half.
20. **The image smokes are the only place the recorder, Daten and code smokes run**
    (`docker-publish.yml::smoke-test`); they stop for `main` with C4-00. `record_fsm_smoke.py` needs
    rclpy only in its shutdown child and `daten_smoke.py` not at all (CONFIRMED by reading), so they
    come back early on the native stack (C4-05b).
21. **Revision 1's C7-06 gutted the moved driver file** ("the node class and the bus loops are
    deleted"). Spec B's tests read that file by AST as the reference of the firmware's guards
    (SPEC-B §8.8); C7-06 now moves it whole.
22. **My revision-2 workaround for SETPOINT** (leave it with `mode('hold')` after 0.5 s without an
    action, re-enter on the next) would itself have latched against the protocol as then written:
    nothing restarted the stream clock on re-entry (spec B's reference model, SPEC-B §8.10 / P34).
    Settled instead: SETPOINT has no stream watchdog. The idle switch is gone from C4-09.
23. **Every end of a cloud run is serialised with the cloud tick only because it runs in the node's
    default group** (`_end_inference` is called from the tick and from default-group services;
    CONFIRMED by reading its call sites). An end started from the arm bridge's own group would run
    beside the tick. Hence C4-09 ends a run through a one-shot timer created on the server node with
    no callback group — the pattern the collision one-shots use (R2).
24. **A cloud-only tag creates no GitHub Release**: only `release-installer.yml` (W5) and the Pi
    tarball job publish one, and W6's first proof is that the release exists with both assets
    (`test_publish_gui_version.py`, case „no such release" — CONFIRMED by reading). So W6 cannot
    advertise a cloud-only tag even if it is started for one.

## 11. Probes run (all under `docs/plans/2026-11-20-native-app-firmware/scratch/architect-C/`)

| # | Command | Observed |
|---|---|---|
| P1 | `git rev-parse HEAD`, `git status --porcelain` at start | `66382d27…`, clean |
| P2 | `find PKG -name '*.py' \| wc -l`; `wc -l physical_ai_server.py`; `grep -rlE '^\s*(import rclpy\|from rclpy)'` | 111 files, 61 088 lines; node 8 542 lines; 8 rclpy importers, 7 message importers |
| P3 | `probes/win_import_blockers.py` (POSIX-only stdlib made unimportable, every module imported) | 95 import; blocked by `fcntl`: `hub_sync`, `daten.hub_reads`, `daten.library`, `daten.node_service` (+ two false positives through pyserial) |
| P4 | `docker run ros:jazzy-ros-base` + `apt install ros-jazzy-rosbridge-server`; `probes/rosbridge_reference_client.py` | rosbridge 2.7.1; the 14 rows of Appendix 1 A5, incl. `"Service /nope/service does not exist"` verbatim and a client `publish` reaching a ROS subscriber |
| P5 | `probes/rclpy_semantics_reference.py` in the same container | Appendix 1 A2 (group concurrency 1 / 2 / 8; timer catch-up once then the original grid; guard coalescing; latched replay; exception leaves `spin`) |
| P6 | per-API `grep -rE` counts over the package | Appendix 1 A1 item 3 |
| P7 | a Python scan of `MGR/src` (non-test) for the server's and the arm container's names | 31 service names, 20 topics named by React; not named: `/get_policy_list`, `/get_saved_policies`, `/browse_file`, `/dataset/edit`, `/dataset/get_info` |
| P8 | `grep -ln "'physical_ai_server.py'" test/*.py` | 41 files |
| P9 | T-SERVER serially in the `lerobot` env (`--continue-on-collection-errors`) | 4 217 passed, 53 failed, 13 skipped, 14 errors, 1 372 s. Causes: `cryptography` missing (28), LeRobot 0.5.2 (15 incl. the collection error), the owner's uncommitted `test_autocal_service.py` (20). No numpy-2 or OpenCV failure |
| P10 | T-DEPSFREE with `python3` 3.13.13 | Ran 3 218, errors=10 (PyYAML missing: 8 modules + 2 tests), skipped=92 |
| P11 | pi_agent suite | Ran 769, OK (skipped=1) |
| P12 | T-CLOUD with `python3` 3.13.13 | Ran 1 374, failures=7, skipped=28 (4 need FastAPI in a child interpreter; 3 others not triaged: `test_two_instances_claim_one_row_once`, two `CkptBad` tests) — not CI's stack |
| P13 | T-REACT, T-ESLINT | 349 files, 5 991 passed, 2 expected fail; eslint exit 0 |
| P14 | `python3 -m unittest discover -s tests -p "test_scene_split.py"` | Ran 80, OK on OpenCV 4.13.0 / numpy 2.4.6 |
| P15 | `uv pip compile` of the engine's direct pins for `x86_64-pc-windows-msvc`, Python 3.13, `--only-binary :all:` | resolves: 97 packages; numpy 2.2.6, opencv-python-headless 4.13.0.92, torch 2.7.0, torchvision 0.22.0, av 15.1.0, pyarrow 25.0.0, huggingface-hub 1.23.0, pupil-apriltags 1.0.4.post11; no torchcodec; rerun-sdk, wandb, cmake, imageio-ffmpeg present in the full closure |
| P16 | reads: `piMode.js`, `pageHost.js`, `robotLocality.js`, `piAuth.js`, `authGate.js`, `StudentApp.js`, `SystemPage.js`, `nginx.conf`, `agent.py` router and `handle_status`, `pi_tickets.py`, `routes/version.py`, `inference_sessions.py`, `v1_studio_config.yaml`, `robot_profiles.py`, `communicator.py`, `collision_monitor.py`, `scene_provenance.py`, `hub_sync.py`, `camera_bridge.py`, `camera_ingest_node.py`, `feetech_arm_node.py`, `activation_agent.py`, `runner_limits.py`, `supervisor.py`, `ci.yml::python-tests`, the bringup launch | the facts tagged CONFIRMED in the appendices |
| P17 | `ls tools/frontend-harness` | absent |
| P18 | `git status --porcelain` during and at the end | see the tree check at the end of §12 |
| P19 (rev. 2) | reads: `SPEC-DECISIONS.md`; SPEC-B §8.1–§8.8, B04, B10, B13, B14, §9, §11–§13, appendix protocol §4 (codes 50–68, 200–218 and their sentences), §7 (the stream watchdog); SPEC-A WP-A8 and §12 | the facts cited as SPEC-B / SPEC-A in this revision |
| P20 | `grep -n "_sensor_alive_mono\|_heartbeat_cb_group\|num_threads" physical_ai_server.py`; `sed` of `_ensure_sensor_executor`, `_sensor_loop`, the heartbeat group, `main()` | the sensor loop stamps `_sensor_alive_mono` before every `spin_once(timeout_sec=0.1)`; the 1 Hz heartbeat has its own MutuallyExclusive group; `MultiThreadedExecutor(num_threads=8)`; `executor.add_node(node)` |
| P21 | `sed` of `communicator.py` (the three callbacks, `_note_arrival`) and `capture_timeline.py` (`ClockMap`, `SourceHistory`, `_backdated`, `_newest_le`, `gap_source`, the sampler's leader path); `grep "SlotSampler(\|TakeIntegrity("` | each callback takes `time.monotonic()` and calls `_append_history(source, arrival, stamp, msg)`; the leader passes stamp 0.0; the sampler always back-dates the leader; one construction site in the node |
| P22 | `probes/board_stamp_probe.py` (the real `capture_timeline` module) | stalls 0.3 / 0.8 / 1.5 / 3.0 s: today's path min spacing 10.000 / 0.005 / 0.005 / 0.005 ms; history fed the sample's own time 10.000 / 10.000 / 10.000 / 9.998 ms; `_backdated` moved 32 of 400 stamped, jittered leader samples, worst 8.50 ms |
| P23 | `grep -rn "header.stamp"` over the server package and `MGR/src`; read of `jointStateInterpolator.js` | in the server package only `Communicator` READS a JointState stamp; the interpolator uses the sliding minimum of (arrival − stamp) and "absorbs any constant offset" |
| P24 | `probes/rail_fixture_probe.py` (real `trajectory_builder`, the node's `_trajectory_publisher` and the driver's `build_boot_home` loaded by AST, stand-in message classes) | 6 entries with exactly the five keys of SPEC-B §8.6: workflow chunks 30 points 0.033333333 … 1.0 s; a 25 fps `build_linear_segment` chunked at 25 points from 0.04 s; inference point 0.05 s; activation home 150 points 0.02 … 3.0 s; smallest step 19 999 999 ns. Separately: `physical_ai_server.workflow.handlers.trajectory.resegment_trajectory` imports and runs with neither rclpy nor LeRobot loaded (84 waypoints for a 40-point take with a lead-in) |
| P25 | `grep -rln "docker/open_manipulator\|'docker', 'open_manipulator'"` over tests, `tools/`, `.github/scripts`; `grep -n sys.path tools/feetech_provision.py`; `grep -n _DOCKER` | 24 readers (listed in C7-06); `feetech_provision.py` lines 81–82; `_DOCKER` in `test_feetech_bus.py` and `test_activation_agent.py` |
| P26 | `grep -ln RUNPOD_TRAINER_IMAGE .github/workflows/*.yml`; the `on:` blocks and job list of `docker-publish.yml` and `release.yml` | only `release.yml` sets the variable (`trainer-image.yml` names it); `docker-publish.yml` pushes on `main` for four path globs; `release.yml` jobs: version-preflight, supabase, trainer-image, trainer-image-var, cloud-api, teacher-web, docker, installer, pi-agent-tarball, next-step |
| P27 | `grep -n "AppId\|DefaultDirName\|UninstallDelete"` in `robotis_ai_setup.iss`; `grep LOCALAPPDATA gui/app/constants.py` | AppId `{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}`, `{autopf}\EduBotics`; `[UninstallDelete]` names only `{commonappdata}\EduBotics\bundle`; the GUI keeps `.env`, the phone certificate and a WebView profile under `%LOCALAPPDATA%\EduBotics` |
| P28 | heads and imports of `.github/scripts/record_fsm_smoke.py`, `daten_smoke.py` | the record smoke has no scenario selection and imports rclpy only in its shutdown child; the Daten smoke imports LeRobot, PyAV, pyarrow and the package, no ROS |
| P29 | `git status --porcelain` before and after this revision | the same six files the owner's session modified; nothing of mine |
| P30 (rev. 3) | reads: SPEC-B §8.6 (the fixture's shape), §8.9, §8.10, §8.11; `release.yml`'s `version-preflight` and the `if:` / `needs` of every job; `test_deploy_approval_gate.py::_arrivals` and `workflow_problems`; `test_publish_gui_version.py::test_every_refusal_changes_nothing` | today a `main` tag runs `docker` (`always() && needs.teacher-web.result == 'success'`), `installer` (needs `docker` success), `pi-agent-tarball` (needs `installer` success) and `next-step`; the simulator evaluates `if:` expressions with STATED outputs and refuses an unstated name; W6 has the refusal case „no such release" |
| P31 | `grep -n "def _end_inference" -A32` and its call sites in `physical_ai_server.py`; the constants of `inference/cloud_texts_de.py` | `_end_inference(self, reason_de='', end_reason='robot_stop', publish=True)`, idempotent under `_cloud_end_lock`, called from the tick and from default-group callbacks; no sentence for a lost arm link exists yet |
| P32 | `git status --porcelain` before and after revision 3 | unchanged: the owner's six files |

Not probed (stated so nobody assumes it): the released 2.41.0 server image; anything on Windows —
including what `wsl.exe` does on a PC without WSL (the reason C7-05 starts no process is PLAUSIBLE,
not observed) and the registry layout of an installed WSL product; LeRobot 0.5.1 in a fresh venv;
spec B's client (it does not exist as code yet); rclpy's ready-entity order; whether rclpy's
`publish` blocks from a foreign thread (my runtime states its own rule); whether a real board keeps
its goal across `mode('hold')` from SETPOINT (M18).

## 12. Open risks, seam points, owner questions

### 12.1 Open risks

| # | Risk | Severity | Handling |
|---|---|---|---|
| K1 | One of R1–R19 is reproduced "almost": the runtime passes its scenarios and the node still behaves differently under load | High | characterisation transcripts against the real stack (Appendix 1 A7), added per area; the rig run of every phase gate |
| K2 | The compat runtime's Windows timer misses the on-time rule (`ON_TIME_TICK_PERIODS` 1.5 at 30 fps = 50 ms) | High | S11's bar; the runtime records jitter on every Windows run; alternative primitive named |
| K3 | GIL load in the robot tier (node + rosbridge JSON + base64 camera frames + MJPEG + ingest) on a slow laptop | High | measured in C4-12's run and on `SCHOOL-PC`; alternative: MJPEG and the camera liveness subscriptions served by the capture process. Since revision 2 it also bears on the heartbeat (K14) |
| K4 | `main` and `release/2.41` drift in shared server code; a fix is applied to one only | Medium | spec A's cherry-pick rule; `test_legacy_client_contract.py` guards only the cloud contract |
| K5 | The station is a new network-facing program (lan zone) written by us | High | it ports `pi_auth.py` rather than re-inventing it; lockstep test; the listener exists only under an admin-written policy |
| K6 | A node crash loop (respawn every 2 s) hammers the arm link with reconnects | Medium | supervisor back-off after 3 exits in 60 s (C4-08a) |
| K7 | Two React "modes" already exist (Pi, Windows) and this adds station and cloud; a wrong branch shows the wrong surface | Medium | RC-1's four-marker × two-host truth table; the Pi snapshot must equal today's |
| K8 | GPL components in a proprietary installer (x264 inside the PyAV wheel; the JDK is GPLv2 + Classpath Exception) | Medium (legal) | decided, owner Q-C10: review before any non-pilot distribution; recorded as an open release condition (C7-01, C7-02's runbook); the notices are generated in C7-01 |
| K9 | The owner's working tree changed under this spec (see the tree check) | Low | the spec cites symbols; every "files.may" names files, not lines |
| K10 | `CLAUDE.md` is edited by spec B's B19 and by C4-13 / C5-10 / C6-05 / C7-11; `native/KEYS.md` by spec B and by C7-02 | Low | the later package rebases; C7-02 edits only the reserved section |
| K11 | The rest pose cannot be found (no pose is both tower-clear from every start and stable when limp) | Medium | then the orderly end is "the tower question, then torque off" for every stop — today's behaviour — and the owner is told; C5-04 says so in its record |
| K12 | The lock's four unused heavy packages (rerun-sdk, wandb, cmake, imageio-ffmpeg; CONFIRMED in the closure) | Low | C4-01's curated `--no-deps` lock leaves them out; `test_stack_lock.py` asserts it |
| K13 | After C4-00 the recorder, Daten and code smokes no longer run for `main` (they lived in the image build), while C4-02 and C4-05 edit the recorder's files | Medium | C4-05b brings the recorder and Daten smokes back on the native stack right after the runtime boots; the code smoke returns with C6-04; until C4-05b the server suite and the proof runs are the only fence — stated in C4-00 |
| K14 | The heartbeat rides the node's executor: 8 stuck workers, or one GIL hold near a second, stop a moving arm (stop-and-hold, fail-safe, but a lesson is interrupted) | High | C4-09's budget table and three tests; a WARN line with the busy-worker count at a 0.5 s gap; the slow-laptop number is `SCHOOL-PC`'s |
| K15 | No import (owner Q-C3): a school that uninstalls the old product with its disk before uploading loses its datasets, and every kit's arm check is run again | Medium (accepted by the decision) | the notice (C7-05), the switch guide, the old uninstaller's own consent box (default no); nothing of ours deletes it |
| K16 | Both products on one PC: ports, the arm's COM port, the camera | Low | separate ids, directories, data root and ports (C7-01, Appendix 4 B2); a held COM port answers spec B's `port_busy` with its sentence; coexistence itself is proven only on `SCHOOL-PC` |
| K17 | `TRIAL-PASSED` has no number: the removal of the OMX and the WSL stack rests on a judgement | Medium (accepted by the decision) | the protocol fixes what is recorded and makes the owner write the success rule BEFORE the trials (C5-10) |
| K18 | `main` now has a third release mode. A cloud-only release bumps four version sites, three of them in frozen WSL files; its channel check still runs although nothing is uploaded; a hand dispatch of `docker-publish.yml` on `main` would still build from frozen sources | Low | the marker and the `== 'true'` gate fail closed; the runbook section in `docs/deploy/DEPLOY.md`; C7-01 ends the mode |
| K20 | Ending a cloud run on `setpoint_lost` is a new end rule on an ask-first path, decided by the conductor, not by the owner | Low (it only ever ends a run) | named in §7; confirmed with C4-09's sign-off |
| K19 | The pilot firmware binary is not in the repository; a build depends on a fetched file | Low | the sha256 comes only from the committed, owner-signed manifest; a mismatch stops the build (C7-01) |

### 12.2 Seam points with spec B and spec A (state after the revisions of 2026-11-25)

| # | Point | State |
|---|---|---|
| M1 | Test location: B's tests in `robotis_ai_setup/tests/test_native_*.py`; C's in `native/tests/` (pytest) | Both stand. C's bridge file and C4-0R's test follow B's naming so today's job runs them. No file overlaps |
| M2 | What `traj` accepts | **Settled** (SPEC-B §8.6): the driver's `_trajectory_cb` rule. C delivers `rail_messages_v1.json` in C4-0R, first and without a gate, because it is B13's gate |
| M3 | `setpoint()` only in mode `setpoint` | Adopted; see M18 for the stream watchdog |
| M4 | The heartbeat's source and rule | **Settled** (SPEC-B §8.5) and built exactly so in C4-09: 20 Hz, own MutuallyExclusive group, main executor, only while `_sensor_alive_mono` is younger than 0.25 s. C fences the thread budget with a table, scenario tests, a saturation test and a GIL probe; no reserved worker, no priority |
| M5 | Proximity for `teleop_on` covers all six joints | Adopted: the sync glide's target includes the mapped leader claw value |
| M6 | Paths C will move | **Settled** (SPEC-B §8.8). C7-06 moves the driver files WHOLE and carries `native/protocol/reference_paths.py`, `tools/feetech_provision.py`'s `sys.path` line and the tests' `_DOCKER` paths. Revision 1 would have deleted the node class inside the moved file: withdrawn |
| M7 | Spike workflows | C's branches are `spike/c-…` and copy only C's file |
| M8 | Runner `windows-2025` | Adopted |
| M9 | Clock mapping of the board sources | **Settled, not as I proposed** (SPEC-B §8.7): `ClockMap` unchanged, fed the arm link's `arrival_mono_ns` and `board_us`; the leader's mid-point directly. C's placement inside that rule: the unchanged `ClockMap` instance lives in the arm bridge, `/joint_states` and `/leader/action` carry the result as their header stamp (engine-monotonic seconds), and `Communicator` appends such a sample with arrival = time = that stamp. Spec B's test 3b is unaffected. For spec B to know: those two topics' stamps are no longer wall time |
| M10 | Who starts the arm link, and scans with the robot tier down | The station starts it; a scan uses a short-lived process. B's "one client" rule allows it |
| M11 | Firmware update: consent and "arm laid down" are the engine's; the allowlist | Adopted in C7-02; the allowlist is generated from the owner-signed `SIGNED-MANIFEST.json` |
| M12 | Shared documents | `CLAUDE.md` (K10); `native/KEYS.md`: C7-02 writes only the section spec B reserved |
| M13 | Spike S14 | Dropped by spec B (owner Q-C3); no package of C waits for it |
| M14 | The fixture's outer shape | **Settled** (SPEC-B §8.6): B13's reader takes exactly C's shape — a non-empty JSON array, the five keys, integer `sec` / `nanosec`, six finite measured values, a real boolean `torque_on`; the meta file is not read — and requires one message over 60 points, one single-point message and one with `torque_on: false`. C4-0R's acceptance names all three |
| M15 | `traj(…, purpose=…)` | The bridge cannot tell a jog from a replay or a program on the rail: every rail message is `program`; only the bridge's own home glide says `home`. Spec B calls the field diagnostic only |
| M16 | The leader's limp check (`leader_fingerprint` / 209 `max_torque`, 218 `torque_limit_ram`; `leader_torqued`) | Adopted: shown through C4-09's generic sentence path; activation and `leader-enable` fail with spec B's sentence in C5-02; C never writes the leader |
| M17 | The pilot firmware artefact | Adopted (SPEC-B §8.1): C7-01 embeds the `--channel pilot` image named by `SIGNED-MANIFEST.json`; gates `B-FIRMWARE-ARTIFACT` and `OWNER-FW-CI` |
| M18 | SETPOINT across Cloud-Inferenz's holds (up to 15 s) | **Settled, not as I proposed** (SPEC-B §8.10): SETPOINT has no stream watchdog; the heartbeat watchdog guards it and latches `setpoint_lost` 1.0 s after heartbeats stop. C4-09 drops its idle mode switch. On spec B's open point the conductor decided: the latch ENDS the cloud run (C4-09: one default-group one-shot → `_end_inference`, a German sentence, no automatic `clear_latch`) |
| M19 | `heartbeat()` must return without waiting for the arm-link process | **Settled** (SPEC-B §8.9): `heartbeat()` and `setpoint()` never block and never raise; `True` / `False`; code 69 `armlink_gone` through `on_link_state`; request methods wait at most 2.0 s. C4-09's adapter has no drop layer and its budget table uses 2.0 s for a link request |
| M20 | Where the signed pilot binary is fetched from, and its file name | **Settled** (SPEC-B §8.11): release `hat-a-fw-b<n>`, asset `edubotics-hat_a-prod-b<n>.bin` in the public release channel, uploaded by the owner; `firmware.pin` is exactly `none` or the build number; the URL is derived; the build calls spec B's `fwcheck.verify_signed_image`, never a check of its own (C7-01) |

**Spec A.** (1) C4-00 is built on WP-A8 and composes with it without editing a line WP-A8 wrote:
in `release.yml` it adds one output line and one step (after WP-A8's `mode` step) to
`version-preflight`, appends one line to `docker`'s `if:` and adds one job. In the shared test file
`test_deploy_approval_gate.py` WP-A8's functions and its maintenance arrival stay; its two `main`
arrivals (every job runs) are replaced by the cloud-only arrivals, because that is the behaviour
the owner changed. (2) Spec A's D-A3 puts
each account's WSL disk at `%LOCALAPPDATA%\EduBotics\wsl`; the native app therefore keeps out of that
folder altogether (§10 item 16). (3) W6 stays on `main` for 2.41.x releases and no package of C
touches it. Of spec A I read WP-A8, §12 and the decisions D-A3, D-A8, D-A12 and D-A13; of spec B the main file's §8 in full, B04,
B10, B13, B14, §9, §11–§13 and the code and watchdog parts of its protocol appendix.

### 12.3 Owner questions

**Answered on 2026-11-25** (`SPEC-DECISIONS.md`); each is folded in where the last column says.

| Id | Answer | By | Folded into |
|---|---|---|---|
| Q-C1 | Yes: `main` stops building and tagging the WSL product; WSL fixes ship from `release/2.41` through spec A's WP-A8 | owner | C4-00 |
| Q-C2 | Inno Setup for both channels from one script; MSI only if a pilot school's IT asks | conductor | C7-01 |
| Q-C3 | No copy, upload first: there is no WSL data import | owner | C7-05 (rewritten), C7-06, §5, Appendix 3 and 4 |
| Q-C4 | Ed25519, the key created and held by the owner offline, signed by hand; no CI secret | owner | C7-02, `native/KEYS.md`'s reserved section |
| Q-C6 | `EDUBOTICS_REQUIRE_ACTIVATION=0` is not carried | owner | C4-09, Appendix 3 (retired rollback) |
| Q-C7 | Maintenance on a lan session: teacher or admin only | conductor | C7-03, Appendix 4 |
| Q-C8 | No `ci.yml` change now; spike runs; asked again before C4-12 | conductor | C4-01b, C4-12 (`OWNER-CI-REASK`) |
| Q-C9 | A new Railway service that the owner creates | owner | C7-04 |
| Q-C10 | The licence review happens before any non-pilot distribution | owner | C7-01, C7-02's runbook, §5 |
| Q-C12 | Deferred to the removal gate | conductor | C7-06 (`OWNER-GO-REMOVAL`) |
| spec B's Q-B4 | The OMX train-and-run baseline is skipped; the V1's bar is the owner's judgement | owner | `TRIAL-PASSED`, C5-10 |
| Q-C13 | (b): a cloud-only release mode on `main` (W1, the trainer jobs, W2, W3 run; W4, W5, W5b do not) | owner | C4-00 (rewritten), C7-01, §5–§7, Appendix 3 |

**No owner question is open.** For the record, Q-C13 as it was asked in revision 2:

| Id | Question | Options | Recommendation (not taken) | Why |
|---|---|---|---|---|
| Q-C13 | Between C4-00 and C7-01 `main` cannot tag, and a 2.41.x tag skips the trainer jobs: nothing can set `RUNPOD_TRAINER_IMAGE`. What if the trainer's handler or lock must change in that time? | (a) freeze the four trainer inputs on `main` for that window (a test enforces it; an emergency goes by hand: dispatch `trainer-image.yml`, set the variable with `--skip-deploys`, then W2); (b) give `main`'s `release.yml` a cloud-only mode in C4-00 (W1, the trainer jobs, W2, W3 run; W4, W5, W5b do not), which needs a version for such a tag and a larger CI edit beside WP-A8 | (a) | The trainer is no part of this plan; (b) enlarges the CI change the owner approved with Q-C1 and puts a second mode, beside WP-A8's, into the pipeline that writes production |

**Decided by me in this revision, listed so the owner can overrule them:** the old product is
detected by reading two registry keys and one file's existence, never by starting `wsl.exe` (C7-05);
no installer page for the notice; the native data root, install directories and `AppId` are separate
from the WSL product's (C7-01); two early packages were added (C4-0R for spec B's gate, C4-05b for
the smokes C4-00 takes from `main`). In revision 3: the cloud-only mode is decided by a committed
marker `.github/release-mode` (not by a VERSION rule alone); `main`'s `VERSION` becomes `3.0.0` and a
cloud-only tag is `v3.0.N`; the cloud-only summary is a job of its own (`next-step-cloud`), so that
WP-A8's `next-step` is not touched; a `setpoint_lost` latch ends the cloud run in C4-09 through a
default-group one-shot and also sets the activation state to `failed`, so that only a student's new
activation clears a latch.

### 12.4 Tree check

`git status --porcelain` was empty when I started revision 1. During that run six tracked files
became modified by someone else (the owner's working tree): `CLAUDE.md`, `docs/CLAUDE-CHANGELOG.md`,
`docs/KNOWN-ISSUES.md`, `physical_ai_tools/physical_ai_server/physical_ai_server/physical_ai_server.py`,
`…/workflow/autocal/service.py`, `…/test/test_autocal_service.py` (the arm-check stillness fix that
2.41.1 will carry, `SPEC-DECISIONS.md` Q1). Before and after revisions 2 and 3 the status shows
exactly those six and nothing else; HEAD is still `66382d27` on `main`. I modified no tracked file, staged
nothing, committed nothing, switched no branch. Everything I wrote is under
`docs/plans/2026-11-20-native-app-firmware/` (gitignored): this spec, its six appendices and
`scratch/architect-C/` (probes, logs, the revision-1 copies and the patch scripts of revision 2).

## 13. Revision 2: what changed (2026-11-25)

| # | Change | Where | Because |
|---|---|---|---|
| 1 | New package C4-0R: the rail-message fixture with its generator, schema and test | Appendix 5 | SPEC-B §8.6: it is an external gate of spec B's B13 and must not wait for any package of C |
| 2 | C4-00 rewritten: starts after WP-A8; ONE step after WP-A8's `mode` step; the conflict surface named; what stops on `main`; the trainer-input freeze (the refusal and the freeze were replaced in revision 3, below) | Appendix 5 | owner Q-C1; spec A's WP-A8 |
| 3 | New package C4-05b: the recorder and Daten smokes on the native stack (Linux); C5-08 reduced to the Windows legs | Appendix 5 | C4-00 takes the image smokes from `main` (K13) |
| 4 | C4-04: three runtime-only extension points and the foreign-thread publish rule | Appendix 5, Appendix 1 A1 | the arm bridge's timers must run on the node's executor without editing `main()` |
| 5 | C4-09 rewritten: the heartbeat exactly as SPEC-B §8.5; the thread budget and its tests; `traj(joint_names, points, …)` pass-through; `/joint_states` stamped with the mapped engine time; setpoint mode left on idle; one generic path for every displayed code | Appendix 5 | SPEC-B §8.3–§8.7 |
| 6 | C5-01 rewritten: `ClockMap` and `SourceHistory` untouched; engine-stamped sources; `leader_stamped` in the sampler; new failing test (CONFIRMED red today) | Appendix 5 | SPEC-B §8.7 and its correction C12 |
| 7 | C5-02: a leader that is not limp fails activation and `leader-enable` with spec B's sentence (codes 209, 218, `leader_torqued`) | Appendix 5 | SPEC-B §8.4, owner Q-B2 |
| 8 | C5-10 and `TRIAL-PASSED`: no OMX baseline, the owner's judgement, what the protocol records | Appendix 5, §4.1 | owner, spec B's Q-B4 |
| 9 | C7-01: the embedded firmware is the owner-signed pilot image named by `SIGNED-MANIFEST.json`, allowlist generated, gates `B-FIRMWARE-ARTIFACT` + `OWNER-FW-CI`; own `AppId`, directories and data root; `release.yml` keeps WP-A8's parts and the trainer jobs; the licence review is a release condition | Appendix 6 | SPEC-B §8.1, B10; spec A's D-A3; Q-C2, Q-C10 |
| 10 | C7-02: the manifest is signed by the owner by hand, the workflow only verifies; `native/KEYS.md`'s reserved section; the firmware allowlist; `docs/deploy/NATIVE-RELEASE.md` | Appendix 6 | owner Q-C4; SPEC-B §8.4 |
| 11 | C7-05 replaced: no import; a read-only detector, one status key, one card, the switch guide; no `wsl.exe` | Appendix 6, Appendix 4 | owner Q-C3 |
| 12 | C7-06: whole-file moves; spec B's `reference_paths.py`, the provisioning tool's path and the tests' `_DOCKER` carried; `pi_agent/` decided at the gate; depends on the notice having shipped | Appendix 6 | SPEC-B §8.8; Q-C12 |
| 13 | C7-03, C7-04, C4-01b, C4-12: the decided questions written in; `OWNER-CI-REASK` before C4-12 | Appendices 5, 6 | Q-C7, Q-C9, Q-C8 |
| 14 | Appendix 3: the activation rollback is a retired rollback with the owner's decision; the no-import row; three more unfenced invariants | Appendix 3 | Q-C6, Q-C3 |
| 15 | Appendix 4: the data root; the import routes removed; the notice's key and route; the firmware route and the `embedded` flag | Appendix 4 | Q-C3; SPEC-B §8.1 |
| 16 | Appendix 1: rule 4 (extension points), R6 and R17 | Appendix 1 | items 4–6 |
| 17 | This file: §3.1 (S14 and the stall bench gate nothing; the pilot artefact; the leader), §3.2 (R-3, R-4, R-12 settled; R-13 … R-15), the gate table, the index (44 packages), §5, §6, §7, §9, §10 items 14–21, §11 P19–P29, §12 | here | all of the above |

### Revision 3 (2026-11-25, final round)

| # | Change | Where | Because |
|---|---|---|---|
| 1 | C4-00 rewritten again: no refusal; a tag on `main` is a CLOUD-ONLY release (W1, W2a, W2b, W2, W3 run; W4, W5, W5b skipped). Decided by the committed marker `.github/release-mode` through one new step after WP-A8's `mode` step; `docker` gated with `client_build == 'true'` (fail closed); a new job `next-step-cloud`; `VERSION` on `main` becomes `3.0.0`, cloud-only tags are `v3.0.N`; no GitHub Release, so W6 cannot advertise one; the runbook section; the trainer-input freeze is dropped | Appendix 5 | owner Q-C13 = (b) |
| 2 | `test_deploy_approval_gate.py`: cloud-only arrivals with must_run / must_skip, an empty-output arrival, four review mutations, `test_a_cloud_only_release_cannot_run_w4_w5_w5b`, the executed `ReleaseProductIsDecidedByTheMarker` | Appendix 5 (C4-00) | the coordinator's test list |
| 3 | C7-01: replaces the cloud-only mode by the native mode (marker `native`, `native-build` takes `docker`'s whole `if:`, `next-step-cloud` deleted) | Appendix 6 | Q-C13 = (b) |
| 4 | C4-09: no idle `mode('hold')`; the board stays in `setpoint` for the whole run; a `setpoint_lost` latch or `armlink_gone` ENDS the cloud run through a default-group one-shot → `_end_inference` with `CLOUD_ARM_LINK_LOST_DE`; no automatic `clear_latch`; the activation state turns `failed`; new failing test | Appendix 5 | SPEC-B §8.10; conductor's decision |
| 5 | C4-09: `heartbeat()` / `setpoint()` never block (no drop layer in the adapter); request methods wait at most 2.0 s in the budget table; `armlink_gone` handled | Appendix 5 | SPEC-B §8.9 |
| 6 | C4-0R: the shape is confirmed by spec B; acceptance names the three kinds its reader requires (over 60 points, one point, `torque_on: false`) | Appendix 5 | SPEC-B §8.6 |
| 7 | C7-01 / C7-02: `firmware.pin` is `none` or the build number; the URL is derived from the channel and the manifest's `release_tag` / `asset_name`; spec B's `fwcheck.verify_signed_image` is the one check | Appendix 6 | SPEC-B §8.11 |
| 8 | This file: R-14 and R-15 settled; M14, M18, M19, M20 settled; Q-C13 answered; K18 replaced, K20 added; §5, §6, §7, §9; §10 items 19 and 22–24; P30–P32; Appendix 3 gains the cloud-only row | here, Appendix 3 | all of the above |
