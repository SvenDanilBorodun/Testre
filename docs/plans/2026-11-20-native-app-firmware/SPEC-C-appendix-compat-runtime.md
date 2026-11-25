# SPEC C — Appendix 1: the compatibility runtime, the rosbridge subset and their differential proof

Belongs to `SPEC-C-native-app.md`. Used by work packages C4-03, C4-04, C4-05, C4-06 and the step-2
packages C7-08 … C7-10. Tags: **CONFIRMED** = observed by me on 2026-11-25 (command in §6 of the main
file, output under `scratch/architect-C/logs/`); **PLAUSIBLE** = argued from the code or a document.

---

## A1. Why a runtime with the SAME import names (decision)

The node file `physical_ai_tools/physical_ai_server/physical_ai_server/physical_ai_server.py` must keep
its path, its method names and its text: **41 test files load node methods from that file by path and
AST** (`grep -ln "'physical_ai_server.py'" test/*.py` → 41, CONFIRMED) and 83 server test files name the
file or parse source with `ast` (same grep with `_load_method` and `ast.parse` added). So engine step 1 changes no import line in the server package. Instead the robot-tier
process puts a directory of shim packages on `sys.path` whose names are the ROS names:

```
native/engine/compat_ros/
  rclpy/                    re-exports native/engine/edubotics_runtime (Node, executors, callback_groups, qos, duration, …)
  std_msgs/ std_srvs/ sensor_msgs/ trajectory_msgs/ builtin_interfaces/ geometry_msgs/ nav_msgs/
  physical_ai_interfaces/   classes BUILT AT IMPORT from the .msg/.srv text (never checked in)
```

Rules the coder must hold:

1. `native/engine/compat_ros` is put on `sys.path` by exactly one function, `edubotics_robot.bootstrap.install()`,
   and by nothing else (no `.pth`, no `PYTHONPATH` in a workflow, no `conftest.py` at the repo root). A
   test (`native/tests/test_compat_isolated.py`) asserts that a fresh interpreter with only the server
   package on its path cannot `import rclpy`, and that `build-images.sh`'s staging list and
   `image_source_parity.sh` never name `native/` (the shim must never reach a ROS image, where it would
   shadow the real rclpy).
2. The shim serves **exactly** the import lines the package has today (CONFIRMED list, 42 distinct
   lines, `grep -rhnE "^\s*(from|import) (rclpy|std_msgs|…)"`): `rclpy`, `rclpy.node.Node`,
   `rclpy.callback_groups.{MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup}`,
   `rclpy.executors.{SingleThreadedExecutor, MultiThreadedExecutor, ExternalShutdownException}`,
   `rclpy.qos.{QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy}`, `rclpy.duration.Duration`,
   `std_msgs.msg.{Empty, String, Bool}`, `std_srvs.srv.{Trigger, SetBool}`,
   `sensor_msgs.msg.{CompressedImage, JointState}`, `trajectory_msgs.msg.{JointTrajectory, JointTrajectoryPoint}`,
   `builtin_interfaces.msg.{Duration, Time}`, `geometry_msgs.msg.Twist`, `nav_msgs.msg.Odometry`,
   `physical_ai_interfaces.{msg,srv}.*`. **Not served on purpose** (their importers already degrade):
   `control_msgs` (`safety/collision_monitor.py` imports it inside `try`, fail-open, CONFIRMED lines
   102–106), `dynamixel_interfaces` (function-local, OMX recovery only). **Removed before the shim is
   needed** by C4-02: `rosbag_recorder.srv.SendCommand` (`communication/communicator.py:60`, a hard
   module-top import, CONFIRMED) and `cv_bridge.CvBridge` (`data_processing/data_converter.py:24`,
   replaced in C4-05).
3. The API surface is the one the package calls today and no more (CONFIRMED counts over the package,
   2026-11-25): `get_logger` 289, `create_timer` 23, `create_publisher` 22, `create_subscription` 8,
   `create_service` 7, `create_client` 4, `create_guard_condition` 1 (through `getattr`),
   `destroy_timer` 10, `destroy_subscription` 3, `destroy_publisher` 2, `destroy_service` 1,
   `destroy_client` 1, `destroy_node` 4, `get_clock` 2 (`.now().to_msg()` 1, `.nanoseconds` 1),
   `wait_for_service` 5, `call_async` 4 (+ future `.done()` 7, `.result()` 6, `add_done_callback` 2),
   `get_subscription_count` 1, `declare_parameter` 1, `get_parameter` 2, `has_parameter` 2,
   `rclpy.init` 2, `rclpy.ok` 1, `rclpy.create_node` 1, `rclpy.try_shutdown` 1, timer `.cancel()`,
   `.reset()`, `.destroy()`, guard `.trigger()`, executor `.add_node`, `.spin`, `.spin_once`, `.shutdown`.
   One addition the package itself does not call: `Node.get_name()`, for scenario `r12_node_names`.
   A call outside this table raises `NotImplementedError` naming the attribute (never a silent no-op), and
   `native/tests/test_runtime_surface.py` AST-scans the server package for attribute calls on the shimmed
   names and fails on any the runtime does not implement.
4. **Runtime-only extension points (revision 2).** Three things the arm bridge needs are NOT part of
   the shim surface (real rclpy has none of them, the server package never calls them) and live in
   `edubotics_runtime` only: `cospin.register(node, with_node=name)` — the registered node is added
   to whichever executor `add_node`s the node called `name`, and removed with it, so the bridge's
   timers run on the node's own `MultiThreadedExecutor` although `main()` is not edited;
   `context.find_node(name)`; and `MultiThreadedExecutor.busy_workers()` (a count, for one log line).
   One property is stated and tested although rclpy only implies it: `Publisher.publish` is safe and
   never blocks when called from a thread that is not an executor worker (the arm link's reader
   thread publishes `/joint_states` and `/leader/action`). The executor gives NO group a reserved
   worker and NO priority: spec B's heartbeat (SPEC-B §8.5) must stop when every worker is stuck.

## A2. Reference numbers measured on real rclpy (ROS 2 Jazzy, `ros:jazzy-ros-base`, arm64, 2026-11-25) — CONFIRMED

Probe: `scratch/architect-C/probes/rclpy_semantics_reference.py`, output `logs/rclpy_semantics_reference.log`.

| Scenario | Observed on real rclpy | The runtime must show |
|---|---|---|
| Two 5 ms timers, 20 ms callbacks, SAME MutuallyExclusive group, `MultiThreadedExecutor(8)`, 1 s | max concurrency **1**, 44 calls | max concurrency 1 |
| Same, TWO MutuallyExclusive groups | max concurrency **2**, 92 calls | max concurrency 2 |
| Same, one Reentrant group | max concurrency **8** (= threads), 367 calls | max concurrency = pool size |
| A node's default group: a 5 ms timer + a service, both 20 ms | max concurrency **1**; only **1 of 10** queued service calls finished in 1 s (the busy timer starves the service) | max concurrency 1; starvation is a recorded property, **not** a pass criterion (see A4) |
| A 100 ms ticker in its OWN group while a default-group callback blocks 1 s | 19 ticks in 2 s, gaps 99–100 ms | max gap ≤ 150 ms |
| A 20 ms timer whose first callback blocks 250 ms | gaps 251.9, **7.1**, 20.3, 19.7, 19.9 ms: ONE catch-up call, then the ORIGINAL phase grid (251.9 + 7.1 = 259 ≈ 13 × 20); 0 gaps under 5 ms | exactly one catch-up call; later calls on the original grid; missed periods skipped; never a burst |
| `timer.cancel()` and `node.destroy_timer(t)` from inside the own callback | each ran exactly once; after `reset()` the cancelled timer fired again | same |
| Guard condition triggered twice from a foreign thread before the executor woke | callback ran **once**, on an executor thread | coalesced, on an executor thread |
| TRANSIENT_LOCAL publisher, subscriber created 0.3 s later | late subscriber got **1** message; a VOLATILE one got **0** | same |
| Exception raised in a timer callback | **propagates out of `spin_once`** | same (step 1 keeps crash → exit → respawn, R13) |

## A3. R1–R19: named implementation and differential test

"Differential test" always means: the scenario script in `native/tests/differential/scenarios/` runs
(a) under REAL rclpy inside the pinned reference image and writes `fixtures/rclpy_reference/<id>.json`
(recorded by `native/tests/differential/record_reference.sh`, re-checked by the job
`compat-differential`), and (b) under `edubotics_runtime` on every CI leg (ubuntu, windows) where
`native/tests/test_differential.py::test_<id>` compares the PROPERTIES listed, never an exact
interleaving. A scenario imports only `rclpy…` names, so the same file runs on both sides.

| # | Held today by | Named implementation in the engine | Differential scenario and the property compared |
|---|---|---|---|
| R1 | default MutuallyExclusive group serialises `/task/command`, cloud start/stop, the inference tick | `edubotics_runtime/callback_groups.py::MutuallyExclusiveCallbackGroup`, one default group per `Node` (`node.py::Node.default_callback_group`), `executors.py::MultiThreadedExecutor._can_run` | `r01_default_group`: a timer + two services in the default group, `max_concurrency == 1` |
| R2 | collision one-shot timers (no group) serialise with `/task/command` | same default group; the V1 recovery FSM of C5-03 creates its one-shots with no `callback_group=` (AST fence `test_board_collision_fsm.py::test_one_shots_use_the_default_group`) | `r02_oneshot_vs_service`: one-shot timers created inside a service callback never overlap a second service of the default group |
| R3 | the 25 Hz manual-record and leader-teach samplers (no group) never overlap a default-group service | same | `r03_sampler_vs_service`: a 25 Hz default-group timer and a blocking default-group service: `max_concurrency == 1`, and the sampler resumes within one period after the service returns |
| R4 | `_record_cb_group`: record ticks never overlap each other, do overlap `/task/command` | own `MutuallyExclusiveCallbackGroup` instance | `r04_record_group`: timer in own group + service in default group: per-group concurrency 1, cross-group concurrency 2 |
| R5 | `_collision_cb_group`: trip hand-over (guard condition) and 5 Hz watchdog exclusive | `runtime/guard_condition.py::GuardCondition` + timer in one own group | `r05_guard_and_timer_one_group`: `max_concurrency == 1` |
| R6 | status tickers in own groups survive a blocking default-group callback (8 threads) | pool size 8 (`MultiThreadedExecutor(num_threads=8)` honoured), dedicated group scheduling | `r06_ticker_during_block`: 100 ms ticker, 1 s default-group block → max gap ≤ 150 ms; second run with 7 pool threads blocked 1 s each → the ticker still ticks (one free thread). The arm bridge's 20 Hz heartbeat group (C4-09) rests on exactly this property; its own budget tests are `native/tests/test_heartbeat_budget.py` |
| R7 | sensor node on a `SingleThreadedExecutor`: per-source order, one thread, measurable liveness | `executors.py::SingleThreadedExecutor`; per-subscription FIFO (`subscription.py::Subscription._queue`, KEEP_LAST depth) | `r07_single_thread_order`: two publishers (100 Hz, 30 Hz) into one single-threaded node: each topic's sequence numbers strictly increasing, exactly one callback thread id |
| R8 | studio node: three subscriptions on one thread, the picture only on demand | `workflow/studio_camera.py::StudioCamera` unchanged (it already takes `node_factory`, `executor_factory`; it calls `rclpy.create_node`, which the shim serves) | `r08_on_demand_subscription`: create → messages arrive; `destroy_subscription` → none arrive; create again → arrive |
| R9a | BEST_EFFORT depth 32: a burst after a stall arrives complete | subscription queue depth = `QoSProfile.depth`, oldest dropped only when the queue is full | `r09a_burst_after_stall`: 30 messages published while the subscriber's executor is blocked 0.5 s → all 30 delivered in order; 40 messages → the newest 32 |
| R9b | latched + 1 Hz re-publish for late rosbridge subscribers | `publisher.py::Publisher` keeps the last `depth` messages when `durability == TRANSIENT_LOCAL`; replay on subscribe (in-process and through the rosbridge subset, A5) | `r09b_latched_replay`: late subscriber gets 1 (latched) / 0 (volatile) |
| R9c | `/collision_flag` latched, `False` at boot | OMX only; **retires with the OMX** (C7-07). The V1 has no such topic (C5-03 uses the arm link's teleop freeze) | the QoS it used is R9b's: `r09b_latched_replay` also publishes a latched `Bool False` first and asserts a late subscriber reads `False` (differential); the topic itself retires (Appendix 3) |
| R9d | command-rail publisher created at boot so DDS discovery has matched | **disappears** (in-process bus). Replaced by an ORDER rule: `edubotics_robot.bootstrap` creates the arm bridge's `/leader/joint_trajectory` subscription BEFORE it constructs `PhysicalAIServer` | differential `r18_subscribe_before_publish`: a VOLATILE message published AFTER the subscription exists is delivered, one published before it is lost (both runtimes; real rclpy measured: late VOLATILE subscriber got 0); plus `native/tests/test_bootstrap_order.py::test_rail_subscriber_exists_before_the_node` |
| R9e | `/leader/joint_states` RELIABLE, "safety-load-bearing" | OMX only. V1: replaced by the teleop state (`/edubotics/teleop_state`, C5-02), delivered on the sensor node | differential `r09e_reliable_keep_last`: 200 messages at 100 Hz into a RELIABLE depth-32 subscription on a busy single-threaded node arrive complete and in order while the consumer keeps up (both runtimes); the V1 gate itself: `native/tests/test_teleop_gates.py` (new behaviour, owner decision PLAN §2.4) |
| R10 | guard condition wakes the main executor from the sensor thread | `GuardCondition.trigger()` is thread-safe and coalescing | `r10_guard_from_foreign_thread`: 2 triggers → ≥ 1 and ≤ 2 callbacks, none on the foreign thread |
| R11 | rclpy timer semantics; `TimerManager.start` recreates a stopped timer in the same group | `runtime/timer.py::Timer` (original phase grid, one catch-up, skip missed, self-cancel/destroy, `reset`) | `r11_timer_grid`, `r11_timer_self_destroy`, `r11_timer_recreate_same_group` (drives the real `timer/timer_manager.py::TimerManager`) |
| R12 | `use_global_arguments=False` keeps the extra nodes' names | `rclpy.create_node(name, use_global_arguments=False)` accepted, ignored; names unique per context | differential `r12_node_names`: `rclpy.create_node(n, use_global_arguments=False)` answers a node whose `get_name()` is `n` on both runtimes; plus `native/tests/test_robot_tier_boot.py::test_three_nodes` (exactly `physical_ai_server`, `physical_ai_server_sensors`, `physical_ai_server_studio`) |
| R13 | launch respawn 2 s, s6 down-signal, grace budgets, `os._exit(70)`, uncaught callback exception kills the node | `edubotics_station/supervisor.py::RobotTierSupervisor` (respawn after 2.0 s, exit code 70 included, back-off after 3 exits in 60 s) and the unchanged `_note_sensor_failure` | `r13_exception_propagates` (differential) + `native/tests/test_supervisor.py` + the budget test `native/tests/test_stop_budget.py` (node 30 s < robot-tier kill 40 s < station stop 120 s; it replaces `tests/test_recording_stop_budget.py` at C7-06) |
| R14 | SIGINT → SIG_DFL before `rclpy.init`; `spin()` returns; `finally` finalizes | the shim's `rclpy.init()` starts `runtime/shutdown.py::ControlChannel` (reads the parent's control pipe; `shutdown` or EOF → `Context.shutdown()` → every `spin()` returns); `main()` in the node file is NOT edited | `r14_spin_returns_on_shutdown` (differential: SIGINT under real rclpy ↔ control message under the runtime; property: `spin()` returns without an exception, `finally` runs once) + the smoke scenarios `shutdown_control_after_two_episodes`, `shutdown_parent_gone_after_two_episodes` (C5-08) |
| R15 | rosbridge 2.7.x semantics | `edubotics_robot/rosbridge.py` (A5) | `native/tests/differential/rosbridge_transcript.py` against REAL rosbridge (A5 table) |
| R16 | ROS parameters from the config YAML | `runtime/parameters.py` loads `config/*.yaml`, merges `<node>.ros__parameters`, serves `declare_parameter` / `has_parameter` / `get_parameter(...).value` | `r16_parameters`: the dict `init_ros_params('v1_studio')` builds is equal under both runtimes (the reference side runs the real launch's parameter file list) |
| R17 | ROS clock = wall clock for stamps | `runtime/clock.py::Clock.now()` = `time.time_ns()` for every stamp the node makes itself. **Exception (revision 2, SPEC-B §8.7):** the arm bridge stamps `/joint_states` and `/leader/action` with the ENGINE's `time.monotonic()` (the sample's own time, mapped from board µs by an unchanged `ClockMap`, C4-09 / C5-01), not with the node clock; the only reader of those stamps in the server package is `Communicator`, and React's interpolator absorbs any constant offset (both CONFIRMED) | `r17_clock_is_wall`: `abs(now().nanoseconds − time.time_ns()) < 50 ms` on both |
| R18 | DDS discovery across containers | **disappears**; same order rule as R9d | the same differential scenario `r18_subscribe_before_publish` and `test_bootstrap_order.py` |
| R19 | `get_subscription_count()` as "a browser is attached" and "the arm driver listens" | `Publisher.get_subscription_count()` counts in-process subscriptions PLUS the rosbridge adapter's per-topic browser subscriptions (the adapter registers a bus subscription only while ≥ 1 browser subscribes) | `r19_subscription_count`: 0 → subscribe → 1 → unsubscribe → 0 (both runtimes); plus `test_rosbridge_subset.py::test_browser_subscription_counts` |

**Two properties that are stated, not "fixed"** (owner decision: compatibility layer first):
- The default group stays mutually exclusive across services: a 2–6 s torque switch inside
  `/workflow/start` still blocks `/task/command`, the cloud tick and the 25 Hz samplers. Step 2 may
  change that only with an owner decision.
- An uncaught exception in a callback still ends the process (the robot-tier supervisor restarts it).
  Changing "crash" to "continue" would change behaviour and is not part of this spec.

## A4. What the differential comparison may and may not assert

- Compared: per-group maximum concurrency; per-topic delivery order and counts; timer call times as
  CLASSES (on grid ± 5 ms / catch-up / skipped); latched replay yes/no; whether an exception leaves
  `spin`; whether a stopped entity fires again.
- Never compared: exact interleavings, thread ids, absolute timings below 5 ms, the starvation count
  of the default-group service (rclpy's ready-entity order is an implementation detail; **ASSUMPTION,
  not probed**: the runtime serves ready entities in the order timers, subscriptions, guard
  conditions, clients, services and in creation order within a kind, which is what makes a busy
  default-group timer starve a default-group service as measured in A2. The scenario
  `r01_default_group` records `service_calls_done` on both sides into the report so a reviewer sees the
  difference; it does not fail on it).

## A5. The rosbridge subset — contract, measured on real rosbridge_server 2.7.1 — CONFIRMED

Probe: `scratch/architect-C/probes/rosbridge_reference_client.py` against
`ros2 launch rosbridge_server rosbridge_websocket_launch.xml` in `ros:jazzy-ros-base`
(`ros-jazzy-rosbridge-server 2.7.1-1noble.20261106.004036`; the shipped images carry 2.7.0 per
`pi_agent/constants.py` — the canonical reference for C4-06 is the released server image, see A6).

| # | Client sends | Real rosbridge answered (verbatim) | The subset must |
|---|---|---|---|
| 1 | `call_service` to a service that is not served | `{"op":"service_response","service":"/nope/service","values":"Service /nope/service does not exist","result":false,"id":"c1"}` | answer **byte-equal** apart from the service name. React's `OLD_IMAGE = /does not exist/i` (`features/cloudInference/robotCalls.js:54`, `features/editDataset/hooks/useDatenCommand.js:20`) reads it |
| 2 | `call_service` with a field missing | the field's IDL default (`"values":{"success":true,"message":"fast"}`) | fill defaults from the IDL, `bool private_mode true` included |
| 3 | a string where a bool belongs | `"values":"std_srvs/SetBool_Request message requires a boolean for field data, but got a <class 'str'>","result":false` | refuse; same text shape `<pkg>/<Type>_Request message requires a <kind> for field <f>, but got a <class '<t>'>` |
| 4 | an unknown field | `"values":"Message type std_srvs/SetBool_Request does not have a field extra","result":false` | refuse with the same text shape |
| 5 | a 1.5 s call, then a fast call 50 ms later | fast answered at 56 ms, slow at 1505 ms | run service calls concurrently (bounded pool, default 8) |
| 6 | `subscribe` to a latched topic | first message after 15 ms | replay the last value on subscribe, before any later message |
| 7 | one id, `throttle_rate 1000`, `queue_length 1`, on a 100 Hz topic, 2 s | 2 deliveries | ≤ 3 |
| 8 | a second id on the same topic with `throttle_rate 30`, `queue_length 0` | 56 deliveries in 2 s — ONE stream at the min/min of both ids; a `publish` frame has exactly the keys `msg`, `op`, `topic` (no `id`) | one subscription per client and topic, `throttle = min`, `queue = min`, frame keys exactly those three |
| 9 | `unsubscribe` the fast id | back to 2 deliveries per 2 s | recompute the merge on every unsubscribe |
| 10 | `advertise` + `publish` on a topic | **accepted silently; the message reached a ROS subscriber** (`CLIENT_PUBLISH_ARRIVED forged`) — the VB-03 hole | **refuse**: `advertise`, `unadvertise`, `publish`, `advertise_service`, `unadvertise_service`, `service_response`, `fragment`, `png`, every action op: no effect on the bus, one WARNING log per client and op, **no** frame back (real rosbridge sends none either at its default status level) |
| 11 | an unknown `op` | no frame to the client; an ERROR line server-side | same: no frame, one log line |
| 12 | `subscribe` with a type to a topic nobody publishes | accepted, no frame | accept silently (React subscribes to `/training/status`, which is never published) |
| 13 | `subscribe` WITHOUT a type to an unknown topic | no frame; ERROR server-side | same |
| 14 | a frame that is not JSON | no frame; connection stays | same; after 8 malformed frames in 10 s the connection is closed (new, stricter; logged) |

Also required (PLAUSIBLE from the upstream source read in research/01 §2.8, each gets a transcript case
recorded from the reference before it is asserted): `queue_length = 0` with `throttle > 0` is a
leading-edge throttle; `queue_length > 0` is a FIFO of the newest N, at most one per interval;
`uint8[]` travels as base64 (`sensor_msgs/CompressedImage.data`); no server-side call timeout.

**Never latch `/task/status`** (both publishers are VOLATILE: `communication/communicator.py::init_publishers`
depth `PUB_QOS_SIZE`, `safety/collision_monitor.py`; VERIFICATION §6 A4): the transcript case
`task_status_is_not_replayed` subscribes 1 s after an idle tick and must receive nothing until the next tick.

**Authentication** is not the adapter's: it listens on an internal loopback port and accepts a connection
only with the station's internal secret header (C4-06/C4-07). It keeps no notion of users.

## A6. The reference image and how fixtures are recorded

- Canonical reference: the released server image of 2.41.x, pinned by DIGEST in
  `native/tests/differential/REFERENCE_IMAGE` (one line, `ghcr.io/svendanilborodun/physical-ai-server@sha256:…`
  for amd64; the coder resolves the digest of tag `2.41.0` with `docker buildx imagetools inspect` and
  records the command output in the commit message). It contains real rclpy, rosbridge_server 2.7.0 and
  the real node, so the runtime scenarios, the rosbridge transcript and (C4-06 onwards) the
  characterisation transcripts all come from what schools run. **Not pulled by me** (7 GB, amd64 on an
  arm64 host): PLAUSIBLE that it runs the scenario scripts; the first step of C4-04 proves it.
- Development reference on a workstation: `ros:jazzy-ros-base` + `apt-get install ros-jazzy-rosbridge-server`
  (what I ran; rosbridge 2.7.1). It may be used to iterate; it is never the checked-in fixture's source.
- `record_reference.sh` runs every scenario in the image, writes one JSON per scenario with the image
  digest inside, and refuses to write when the working tree is dirty outside `fixtures/`.
- Job `compat-differential` of `native/ci/native-proof.yml` (ubuntu; on `main` once C4-01b has landed): pulls the pinned image, re-records into a temp dir and fails
  when a property differs from the checked-in fixture (drift of the reference itself), then runs
  `pytest native/tests/test_differential.py`.

## A7. Characterisation transcripts (system level)

Recorded against the reference image's REAL node through its REAL rosbridge, with no hardware
(`EDUBOTICS_ROBOT_TYPE=v1_studio`, simulator runs, synthetic camera and joint publishers written as
rclpy scripts under `native/tests/characterisation/sources/`), normalised (volatile fields masked,
timings in classes), then replayed against the native robot tier with the arm simulator.

| Id | Scenario | Added by |
|---|---|---|
| `t01_idle_connect` | connect, heartbeat cadence, idle identity tick (`robot_type`, `robot_profile`, `capabilities_json` keys), `/edubotics/signal_status` shape, latched topics replayed, `/task/status` not replayed | C4-06 |
| `t02_sim_program` | `/workflow/start` with a `sim` sibling, `/workflow/status` sequence, `/sim/joint_states` stamps increasing, `/workflow/stop` reset | C4-06 |
| `t03_hf_token` | `/register_hf_user` set / same again / clear / malformed; `/edubotics/hf_token_state` fields | C5-06 |
| `t04_record_refusals` | `/task/command` START without sources → the German data-gate error after 5 s; FINISH before a dataset | C5-01 |
| `t05_daten_commands` | `/daten/command` `state`, `link`, an unknown action; `/edubotics/daten_state` | C5-05 |
| `t06_calibration_legacy` | `/calibration/status`, `/calibration/start` with an unknown step, the state topic under no statement | C4-11 |
| `t07_manual_refusals` | `/workshop/jog` out of range, `/workshop/hand_guide` without joints, `/workshop/capture_pose` | C6-01 |
| `t08_code_run_refusal` | `/workflow/start` with `language: python` and no runner → the German `runner_down` sentence | C6-02 |

Every difference between the reference and the engine is either a defect or one entry of
`native/tests/characterisation/INTENTIONAL.py` (a dict `id → reason`, the `blocklyConformance`
pattern); an unused entry fails the test. The entries known today: the capability flags of `v1_studio`
(C5-01), the removed services and topic (C4-02), `/edubotics/teleop_state` (C5-02).
