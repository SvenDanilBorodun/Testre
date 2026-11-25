# SPEC C — Appendix 3: every CLAUDE.md invariant the native app keeps, changes or retires, and the CLAUDE.md rewrite plan

Belongs to `SPEC-C-native-app.md`. **K** = kept as written. **C** = kept in substance, mechanism or
wording changes (the new fence is named). **R** = retired on `main` by the package named, with the
owner decision that retires it; an R row never means "stop caring on `release/2.41`", where the WSL
product keeps every rule until that branch ends (PLAN §2.3, §2.5 "Branches"). A row with **⚠** touches
an ask-first path of CLAUDE.md and is listed again in §7 of the main file.

Owner decisions are cited as P§x (PLAN.md section) or Qn (VERIFICATION.md §7 answer).

## C1. The six non-negotiable rules

| Invariant (CLAUDE.md) | | What happens | Package | Fence afterwards |
|---|---|---|---|---|
| §1 German UI / English code; the `[TECHNIK]` exception | K | every new sentence is German with literal umlauts, in ONE module per package (`edubotics_station/texts_de.py`, `edubotics_robot/texts_de.py`, `edubotics_host/texts_de.py`) | all | `german_detail_lint.py::SCAN_SCOPES` gains the station's `message=` scope (C4-08a); `native/tests/test_texts_de.py` applies the lint's `TRANSLITERATIONS` and `GERMAN_WORDS` to every constant of the three modules (the `test_credit_texts_de.py` pattern) |
| §2 Xacro Dynamixel limits, ros2_control YAML, `entrypoint_omx.sh` torque-disable and phases, the torque-free OMX leader | R | gone with the OMX | C7-07 | owner: P§1, P§2.3 "OMX retired" |
| §2 Recording-side guards: source stop, gap re-record, `MAX_REDOS_PER_EPISODE`, error-stop finalize, disk floors | K | unchanged code on the runtime | C4-05, C5-01 | existing server tests + `record_fsm_smoke.py` on Windows (C5-08) |
| §2 "Boot motion is GATED", four agreeing layers | C ⚠ | ONE layer: `arm_bridge/activation.py`. Torque comes up at „Umgebung starten" (firmware keeps holding), nothing moves before `/edubotics/activate` | C4-09 | `native/tests/test_activation_bridge.py::test_nothing_is_sent_before_activate`; owner: P§2.4 "Energise", "Boot" |
| §2 the one-variable rollback `EDUBOTICS_REQUIRE_ACTIVATION=0` (the arm homes at container start) | **R** | RETIRED in the native app — **owner decision Q-C6, 2026-11-25 (`SPEC-DECISIONS.md`): "Not carried. Nothing moves until a student activates the robot."** No setting, no environment variable and no policy key moves the arm unattended; the name is in `robot_env.RETIRED`, so a value in the environment is ignored and logged once. It stays what it is on `release/2.41` | C4-08a (`RETIRED`), C4-09 | `native/tests/test_robot_env.py::test_the_activation_rollback_is_retired_and_ignored`; `test_activation_bridge.py::test_no_setting_starts_a_motion_at_start` (every key of `settings.json` and the policy key varied: the link sees no `traj`) |
| §2 The teleop force/collision e-stop (OMX detector, thresholds, gating, two-step recovery) | R + new ⚠ | OMX detector retires (C7-07). The V1 gets board-side detection (spec B) and the engine keeps the two-step recovery (C5-03) | C5-03, C7-07 | owner: P§2.4 "Collision stop" — in principle, conditional on the collision bench and on O10 (scope). Not blanket |
| §2 The second execution path and its four conditions | C ⚠ | conditions 1–3 unchanged code; condition 4 becomes "a different PRINCIPAL" (AppContainer + Low IL, Job Object) instead of a different uid; stop = `TerminateJobObject` | C6-02 … C6-04 | `code_e2e_smoke.py` on Windows as a standard user; owner sign-off after S1–S5 (P§2.4 "Student-code sandbox", O11 if they fail) |
| §2 Cloud-Inferenz drives the arm on the same rail and adds nothing | K | the cloud tick still publishes on `/leader/joint_trajectory`; the arm bridge turns a one-point message into one setpoint (P§2.5 "Inference") | C4-09 | `test_cloud_inference_node.py` + `native/tests/test_arm_bridge_rail.py::test_one_point_is_one_setpoint` |
| §2 "A live leader refuses / ends a cloud run", HIGH-6, the manual leader block | C ⚠ | one predicate, re-keyed on "teleop is on" (P§2.4 "Teleop on blocks …", Q31) | C5-02 | `native/tests/test_teleop_gates.py` (every one of the five node call sites and the trip gate) |
| §2 The Roboter-Studio tower guard | K | unchanged | C4-05 | `test/test_tower_guard.py` |
| §2 The V1 driver's tower check; a refused glide leaves the arm torqued | C | same functions, called by the arm bridge before anything is sent | C4-09 | Appendix 2 T13 |
| §2 new, owner-decided (P§2.4): link-loss stop-and-hold; resume only by a student action + sync glide; a far goal is executed as is; tracking / wrong-way watchdog; orderly end glides to a rest pose | new ⚠ | firmware half: spec B. Engine half: C5-02, C5-04 | C5-02, C5-04 | each conditional on its bench (P§2.4 header) |
| §3 physical-ai-server COPY-wholesale; open-manipulator overlay chain; code-runner image; `image-source-parity`; the Orange Pi bases | R | on `main` by C7-06. Their rule "the shipped thing is the repo" continues as **the installed app is the repo**: the build stages named source trees and `app_source_parity.py` diffs the staged app against HEAD | C7-01, C7-06 | `native/packaging/app_source_parity.py`, run by the native build workflow; owner: P§1 "The OMX and the WSL/Docker/ROS stack are retired at the end" |
| §3 edubotics-trainer image, the lock and handler hashes | K | cloud side, untouched | — | unchanged |
| §4 Service-role key, ownership asserts, default-deny, the ledger, worker tokens | K | no migration in this spec; two additive API changes (C5-07, C7-02) touch no table | C5-07, C7-02 | the cloud suite; `test_platform_token_scope.py` gains the new anonymous `info.json` read |
| §5 LeRobot pin sites | C | a SIXTH robot-side site joins: `native/packaging/requirements-engine.in` (`lerobot==0.5.1`, no extras). The three server Dockerfiles leave `main` at C7-06 | C4-01, C7-06 | `tests/test_lerobot_pin_lockstep.py` reads the new site |
| §5 `numpy==1.26.4` cap, `scipy<1.18` in server images | R on `main` | the cap exists only for ROS `cv_bridge`; the engine runs numpy 2.x (P§2.5 "Runtime") | C4-01 | the server suite on the native lock (C4-01); decode parity vs cv_bridge (C4-05) |
| §5 No torchcodec; PyAV is the decoder | K | LeRobot's marker already excludes torchcodec on Windows; the lock test asserts its absence | C4-01 | `native/tests/test_stack_lock.py` |
| §6 W1, W2, W3, W6, the approval gate, the pinned CLIs, the hash locks | K | untouched, except one additive W6-like workflow for the native channel (C7-02) | C7-02 | `tests/test_deploy_approval_gate.py` gains the new production job |
| §6 (new, owner Q-C13 = b) "a tag on `main` releases the cloud only" from C4-00 until C7-01 | new | the committed marker `.github/release-mode` (`cloud-only`) decides; `release.yml` then runs W1, W2a, W2b, W2, W3 and skips W4, W5, W5b; `VERSION` on `main` is `3.0.N`; no GitHub Release exists for such a tag, so W6 cannot advertise it; CLAUDE.md Rule §3's coupling holds (a handler or lock edit ships with such a tag) | C4-00, C7-01 | `test_deploy_approval_gate.py` (cloud-only arrivals, four mutations, `test_a_cloud_only_release_cannot_run_w4_w5_w5b`, the executed `ReleaseProductIsDecidedByTheMarker`), `test_main_cloud_only_release.py` |
| §6 W4 (`docker-publish.yml`), W5 (`release-installer.yml`), the Pi tarball job, `version-preflight`'s four sites | R on `main` | replaced by the native build workflow and a native preflight | C7-01, C7-06 | `tests/test_native_release_shape.py` |

## C2. Critical architectural choices

| Invariant | | What happens | Package |
|---|---|---|---|
| No Docker Desktop; WSL distro; usbipd; pinned Docker; `start-dockerd.sh`; keepalive; elevated repairs; finalize; rootfs gate; `.reboot_required`; the virtualization verdict | R | on `main` at C7-06 (owner: P§1; Phase 0's fix lives on `release/2.41`, spec A) | C7-06 |
| "The GUI is the sole owner of the container lifecycle"; every early return below the teardown | C | "the station is the sole owner of the robot tier": nothing but `POST /environment/start` starts it; the station stops a robot tier it finds running at its own start | C4-08a (`native/tests/test_station_lifecycle.py`) |
| Compose boot order, health gates, `mem_limit`, published ports | R | process start order is the station's (`arm link → capture → robot tier`), stop order its reverse with the recorder first (VA-07) | C4-08a |
| WebView2 child process, `_v=`, `?cloud=1`, `?fresh=<nonce>`, `has_live_window`, the exit callback and session generation | C | the host IS the window process; it navigates with `_v=<version>` and `fresh=<nonce>` at every app start; when the window ends the engine ends with it, which clears the in-memory token and ends a cloud run through the node's own `finally` | C4-12 |
| The GUI-managed `.env`, `MANAGED_KEYS`, registries, `versions.env`, IMAGE/COMPOSE coupling | R | replaced by `settings.json` + the policy key (Appendix 4 B6). The native app never reads or writes `%LOCALAPPDATA%\EduBotics\` (the WSL product's `.env`, and since 2.41.1 each account's WSL disk): its data root is `%LOCALAPPDATA%\EduBoticsStation` | C4-08a, C7-01, C7-06 |
| (new) What the WSL product holds on a PC — datasets, calibration, settings inside the distro and its volumes | **not carried** | **owner decision Q-C3, 2026-11-25: no copy, upload first.** The native app imports nothing, starts no `wsl.exe`, and only tells a school that still has the old product to upload its datasets before removing it; the arm check runs once more in the native app | C7-05 |
| The Hugging Face token belongs to the account; encrypted relay; `enc1:` envelopes; one slot; busy-queued clear | K | wire and node logic unchanged | C5-06 |
| "…writes it atomically to the tmpfs file `$HF_TOKEN_PATH` … no consumer passes a token"; compose must never set `HF_TOKEN`, `HF_HUB_DISABLE_IMPLICIT_TOKEN` | C | the slot is process memory; children ask the engine through ONE accessor; every hub client is built by `hf_token_store.hub_api()`; `HF_HUB_DISABLE_IMPLICIT_TOKEN=1` and a private `HF_HOME` are set by the robot tier (owner: P§2.5 "HF token: in memory only") | C5-06 (`test_hf_token_memory.py`, an AST fence: no bare `HfApi(` in the package) |
| Per-machine `ROS_DOMAIN_ID` | R | no DDS | C7-06 |
| The Orange Pi agent, robot login, firewall, self-repair | R on `main`, alive on `release/2.41` | the Pi is frozen (P§2.3). Its robot login is PORTED (not shared) into the station for the lan zone | C7-03, C7-06 |
| `NOTHING MOVES AT CONTAINER START` | C | see C1 |
| `/leader/joint_trajectory` is the command rail; anything publishing there drives the follower | K | in-process bus topic; the arm bridge is its only subscriber; a rosbridge client cannot publish (Appendix 1 A5 row 10) — the VB-03 class closes by construction | C4-06, C4-09 |
| React dual build; teacher-web CSP; Blockly media | K | plus a third build mode `cloud` (C7-04) |
| Native camera capture bridge, `>BIQ` wire, crop to aspect, `_halt` events | C | same code in the capture process; TCP stays, bound to loopback on an ephemeral port, with a preamble secret | C4-10 |
| The scene-camera profile; the tower camera's fixed mode | K | Appendix 2 |
| `usb_cam`; identical-serial defenses 1 and 2 (Linux) | R | Windows PnP filter (defense 3) stays | C7-06 |
| `EDUBOTICS_*` reach a container only through a compose list; `env-forwarding-guard`; `compose-env-parity` | C | the station passes ONE table `edubotics_station/robot_env.py::FORWARDED` to the robot tier; a test fails for any `EDUBOTICS_*` name the server package reads that is in neither `FORWARDED` nor `RETIRED` | C4-08a |
| Robot type is setup-time and hardset; the registry exists three times | C | one robot type; the server registry is the only one after C7-07; the station derives its row from it by import | C5-01, C7-07 |
| The profile-aware setup wizard on both platforms; family conflict | C | the React System page only; one family | C4-08a |
| `v1_studio` — every geometry, IK, HOME, claw and reach rule | K | unchanged |
| `v1_studio` — "follower-only, Roboter Studio only", `inferable` false, `has_leader` false, `collision_enabled=False` | C ⚠ | P§1, P§2.4, P§2.5: recordable, trainable, inferable, leader, board collision stop | C5-01, C5-03 |
| `v1_studio` — the driver (`feetech_arm_node.py`): probe, edge refusal, post-energise abort, tick clamp, register 40, read-fail latch | → spec B | firmware; the German sentence of every board and arm-link code is spec B's (`edubotics_boardproto.texts_de`, byte-equal to today's driver strings where the meaning is unchanged) and reaches the page through `Result.sentence_de` unchanged; the engine's own decisions (floor, tower, activation) keep the driver's and the activation agent's sentences | C4-09 (use), spec B (guards, texts) |
| `.s6-keep`, the s6-agent strip | R | C7-06 |
| Daten 2.0: sidecar, link tokens, download worker, leases, one guarded commit, the lossless engine | K | behind a portability layer; POSIX behaviour byte-identical | C5-05 |
| Single uvicorn worker; boot schema fingerprint; `/health`; `/version` | K | `/version` gains additive keys (C7-02) |
| HF identity anchor; deletion requests; Art. 15 export | K | untouched |

## C3. Roboter Studio, recording, inference, React, cloud

| Invariant | | What happens | Package |
|---|---|---|---|
| Everything under "Roboter Studio (layer 9)": the arbiter and lock order, the motion-lock policy, IK, floors, destinations, grasp rules, calibration, perception, replay, simulator, zones, the interpreter, editor invariants | K | the code runs unchanged; its tests are the fence | C6-01 |
| "Two sockets in `code_runner_ipc`, and the split is the security model"; the sandbox compose keys | C ⚠ | two named pipes with explicit security descriptors; the keys become AppContainer + Job limits (Appendix 1 does not cover this; see C6-02, C6-03) | C6-02, C6-03 |
| "The Pi starts three robot-tier containers" | R | `release/2.41` only | C7-06 |
| Recording FSM, slot sampler, one recorder lock, D14/D7/D4, dataset names, disk floors, `/edubotics/signal_status`, D58 | K | unchanged; the leader source changes (P§2.5 "Recorded action") | C5-01 |
| "An environment stop finalizes a running recording; the signal chain and its budget" | C | the chain is: station → control message → `Context.shutdown()` → `spin()` returns → the same `finally`. Budgets: node 30 s < robot-tier kill 40 s < station stop 120 s | C4-05, C5-08 |
| Local GPU inference: `InferenceManager`, START_INFERENCE, the processor allowlist `validate_processor_steps`, S17 on the robot | R | owner: P§2.5 "Removed". The shared parts stay (the `'inference'` timer name, `operation_mode`, `on_inference`, `_preflight_contract`, `PHASE_INFERENCE_LOADING`) | C4-02 |
| rosbag recording; the seven dead services; the seven unserved `.srv`; `/dataset/edit`; MyModels' robot download; the OMX joystick; the phone camera | R | owner: P§2.5 "Removed", Q20. `TaskInfo.record_rosbag2` stays | C4-02 |
| Cloud-Inferenz: contract across five places, HTTPS only, schedule, ceilings, reserve/arm/connect | K | plus the cross-robot refusal (P§2.4 "Models from another robot") | C5-07 |
| "A student window that ends on its own clears the robot's token / stops a cloud run" (`docker exec` scripts) | C | there is no window without the engine; closing the app ends both | C4-12 |
| Managed ACT training | K | untouched |
| React: Sucher, icons, storage partition, sign-out order, login gate, Daten tab, Aufnahme page | K | only Appendix 4 B5's named changes |
| "Pi mode … fail-closed on the page's own host" | K | the lan zone relies on exactly this; the local zone is a loopback page with marker `pi: false` |
| Windows GUI + installer section (diagnostics leaf, usbipd, PS 5.1 traps, finalize marker, Schritt colours) | R | replaced by host + packaging; the behaviours research/03 §3.3 lists are each mapped in C4-08a, C4-12, C7-01, C7-02 | C7-06 |
| Cloud stack: rate limits, CORS allowlist, `/me/hf-token`, migrations | K | the native page's origin `http://127.0.0.1:<P>` is an unlisted origin and is served by `_ANY_ORIGIN_BEARER_ROUTES` exactly like a Pi's LAN origin (PLAUSIBLE: same mechanism; proven in C4-12's end-to-end login step) |

## C4. Invariants this spec relies on that NOTHING fences today

1. **`/task/status` is not latched.** Only a code reading shows it. Fenced from C4-06 by the transcript
   case `task_status_is_not_replayed`.
2. **The node file's path and method names.** 41 tests depend on them; nothing says so in one place.
   Fenced from C4-04 by `native/tests/test_node_file_is_where_tests_expect.py` (path exists; the AST
   loaders' method list resolves).
3. **`feetech_arm_node.py` is importable as a library** (module-level decisions and German builders).
   `tests/ros_stubs.py` proves it for five test modules today; fenced from C4-09 by importing it under
   the real shim.
4. **Monotonic time is comparable across processes on one PC** (the arm link stamps both arm sources
   in its process, the engine uses those stamps as its own time). Spec B states it now (SPEC-B §8.4):
   CONFIRMED on macOS by spec B's probe, proven on Windows by spike S11's bracket test (gate
   `B-S11-XPROC`); fenced on every CI leg by C5-01's two-process test.
5. **The default callback group's mutual exclusion is load-bearing for the collision FSM and the
   samplers** (R2, R3). Fenced by Appendix 1's scenarios, and by AST tests that the node creates those
   timers with no `callback_group=`.
6. **`PhysicalAIServer._sensor_alive_mono` is stamped before every `spin_once` of the sensor loop.**
   A private attribute, read today only by the signal status; from C4-09 it decides whether the
   board gets a heartbeat (SPEC-B §8.5). Fenced by an AST check in `test_arm_bridge_heartbeat.py`.
7. **The main executor always has a free worker for a short callback.** Today only the 1 Hz
   heartbeat's own group and the comment above `MultiThreadedExecutor(num_threads=8)` say so; nothing
   counts what can block at once. From C4-09 a starved heartbeat stops the arm, so the budget gets
   its table and three tests (`thread_budget.py`, `test_heartbeat_budget.py`).
8. **`_trajectory_publisher` truncates `time_from_start` to whole nanoseconds** (`int((t − int(t))·1e9)`),
   and the driver reads it back as `sec + nanosec·1e-9`. Nothing pins it; spec B's parity with the
   driver depends on the exact values. Fenced from C4-0R by the fixture's regeneration test.

## C5. CLAUDE.md rewrite plan, section by section (executed by C7-11; interim notes by C4-13, C5-10, C6-05)

Interim rule: while Phases 4–6 are in flight CLAUDE.md gains ONE section, „Native app (in progress)",
directly under „What this is", maintained by the phase documentation packages. It states which
sentences of the rest of the file are true only for `release/2.41`. The final package removes it.

| CLAUDE.md section | Final treatment (C7-11) |
|---|---|
| Header paragraph, companion table, maintenance rules | keep; add this spec to the "folded in from" list; point `docs/plans/` readers to the three specs |
| What this is | rewrite: V1 follower on a HAT with firmware, leader on a plain adapter, one native Windows 11 app, no WSL/Docker/ROS, cloud unchanged; the WSL product is `release/2.41` |
| Repository topology | rewrite the table: `native/` packages, spec B's trees (names from spec B), `physical_ai_tools/physical_ai_server` (engine logic), `physical_ai_manager`, `cloud_training_api`, `modal_*`, `runpod_training`, `supabase`; remove `open_manipulator/`, `gui/`, `installer/`, `wsl_rootfs/`, `docker/`, `pi_agent/` |
| Licensing | keep; add the JDK runtime's licence and the native lists (who generates `THIRD_PARTY_PYTHON.txt` now) |
| Big-picture architecture | rewrite the layer list (firmware · arm link · engine: station + robot tier · capture · host · sandbox · React · cloud · inference · Roboter Studio) and the diagram (PLAN §1's) |
| Rule §1 | keep; replace the pi_agent scope sentence by the station scope |
| Rule §2 | rewrite the lead list: firmware guards (spec B's names), engine guards (tower guard, floor ladder, teleop gates, recovery FSM, rest-pose end); delete the xacro / entrypoint / OMX e-stop bullets; keep the four conditions with the Windows mechanism; keep the Cloud-Inferenz and tower-guard paragraphs verbatim where still true |
| Rule §3 | rewrite as "the installed app must reflect the repo" (staging list, `app_source_parity.py`, the generated-at-import messages); keep the trainer paragraph |
| Rule §4 | keep; add the two additive routes' sentences |
| Rule §5 | rewrite the site list; delete the numpy cap; keep the lock paragraph |
| Rule §6 | rewrite: cloud workflows as today; the native build, the native W6, what a tag does on `main` |
| Windows host, WSL2, container lifecycle | replace by „Host, station and robot tier" (process tree, zones, start/stop order, budgets) |
| Configuration, registries, version coupling | replace by „Settings, policy, update channel, N / N−1" |
| The Orange Pi agent | delete; one sentence: frozen on `release/2.41` |
| Robot control, cameras, env forwarding | rewrite: activation, teleop relay and gates, the rail, capture process, profile, tower camera |
| Robot type + ArmProfile registry; setup wizard | shrink to one profile and the System page |
| v1_studio | rewrite around the firmware seam; keep geometry, IK, HOME, claw, reach |
| Server packaging + cloud invariants | keep the cloud bullets; replace the server ones by the engine's (sidecar, portability layer, token accessor) |
| Roboter Studio | keep; replace the two-sockets and sandbox-keys bullets |
| Recording, data, inference | keep; edit the shutdown bullet, the leader source, remove local inference |
| Cloud-Inferenz; Managed ACT training | keep; add the cross-robot refusal |
| React manager app | keep; add station mode, the cloud build mode |
| Windows GUI + installer | replace by „Packaging, installers, updater" |
| Cloud stack | keep; `/version` keys |
| Common commands; CI guardrails | rewrite with re-measured counts and the job list as it then is |
| When to ask the user | keep; add: writing the policy key, changing `LAN_PORT`, pushing a firmware update, changing the fixture contract's rest pose, anything that touches the update-manifest key or `native/KEYS.md` (owner Q-C4: he holds the key offline and signs by hand; no agent creates, receives or holds a private key); remove the WSL and Docker items |
