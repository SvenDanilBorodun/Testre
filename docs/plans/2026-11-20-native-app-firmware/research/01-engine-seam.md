# 01 — The engine seam and functional parity (server side)

Research architect 1 of 4, 2026-11-24, HEAD `dba3ac81` (release 2.41.0). Read-only on the repo; every
probe ran under `scratchpad/agent1/` (outline, glue counter, IDL generator, rosbridge-subset prototype,
RoboStack index check). Tags: **VERIFIED** (file::symbol, command or URL), **REFUTED** (brief or
CLAUDE.md vs. code), **UNVERIFIED-BENCH** (needs the arm), **UNVERIFIED-WINDOWS** (needs a Windows
run). The code wins over the brief and over CLAUDE.md.

---

## 0. Executive findings

1. **The node is overwhelmingly logic.** `physical_ai_server.py` has 8 542 lines. 2 768 of them are
   comments or docstrings, 492 are blank and 5 359 are code. Only **143 code lines (2.7 %)** touch the
   ROS API or message construction. Another 212 are `get_logger` calls and about 480 fill a service
   response or read a request. **VERIFIED** with `scratchpad/agent1/glue.py`.
2. **The ROS parts of the package are small, and seams already exist.** 10 of the 111 server files
   touch ROS: 8 import rclpy, 7 import message types. `workflow/`, `daten/` (except its 25-line
   `_wire_ros`), `inference/`, `capture_timeline`, `collision_detector` and `signal_status` are
   ROS-free.
   - `StudioCamera` takes node and executor *factories*.
   - `DatenService` has a `ros=` switch.
   - `WorkflowManager` takes 16 injected callables.
   - **41 of the 43 test files that run the node's own methods (by AST) passed on a ROS-free
     macOS Python 3.13 / numpy 2.4.6:** 1 210 passed. The 3 failures and the 2 files that did not
     collect were all missing `yaml`/`aiohttp`. **VERIFIED.**
3. **What ROS really does here is semantics, not code.** It supplies three executors and their
   callback groups (§3.2), a guard condition, QoS latching and history depth, rclpy timer behaviour,
   the SIGINT finalize chain, s6/launch respawn, and rosbridge's own subscription semantics. **19
   invariants (R1–R19, §3.3) hold today only because of that machinery.** A ROS-free core must recreate
   each one explicitly. That, not the line count, is the risk.
4. **The browser needs very little of rosbridge v2.**
   - Client → server: `subscribe` (throttle_rate, queue_length, compression always `none`),
     `unsubscribe` and `call_service`. The client never advertises or publishes.
   - Server → client: `publish`, `service_response` and `status`.
   - **I built a 172-line stdlib Python server of that subset (plus the 138-line IDL generator),
     and the real roslib 1.4.1 from the SPA's `node_modules` worked against it.** It handled latched replay, default filling
     (`private_mode true`), refusal of hostile requests, base64 `uint8[]`, rosbridge's per-client
     min/min subscription merge, and concurrent service calls. **VERIFIED** (§2.8).
5. **The wire contract can stay generated from the existing IDL.** All 50 `.msg`/`.srv` files (plus the
   9 standard types the node uses) parse into a schema that keeps defaults, constants and strict types.
   The generator is 138 lines, and all 90 project types build from `{}`. `ci.yml::enum-parity` keeps working
   because it parses the same text files. **VERIFIED** (§7).
6. **ROS 2 natively on Windows (RoboStack) is technically present but cornered.**
   - The `robostack-jazzy` win-64 channel has rclpy, rosbridge 2.7.1, cv_bridge and rosbag2, but
     **no `web_video_server`**.
   - Its rclpy is built only for **cp312 and cp314**. Torch 2.7.0 (the pinned training surface) has
     Windows wheels only up to cp313, and Python ≥ 3.13 is needed for the QPC `time.monotonic`.
   - **No (rclpy, torch 2.7, Python ≥ 3.13) combination exists without moving a pin.** **VERIFIED**
     (§7a).
7. **On the shared V1 bus, today's "is a leader live?" test becomes wrong for every gate.** On the V1
   (`collision_enabled=False`) `collision_monitor._init_collision_monitor` returns *before* creating
   its `/joint_states` and `/leader/joint_states` subscriptions, so `leader_appears_active()` is always
   False there. A V1 leader would therefore be refused by leader-arm Vormachen
   ("Der Leader-Arm sendet keine Daten"). Every leader gate keys on "leader joint states are fresh"
   (HIGH-6 workflow start, the manual block, the Cloud-Inferenz refusal, the collision trip gate).
   With leader and follower on one board the leader is *always* fresh. The predicate has to become
   "the board's command source is teleop". **VERIFIED** by reading; **redesign** (§6.2).
8. **The recorder's "action" is a topic that will not exist.** On the OMX the action is the leader
   broadcaster's message on the command rail `/leader/joint_trajectory` (stamp 0, back-dated), which is
   also the follower's goal. With teleop on the board the PC sees no such message, so the action must
   come from the board's STATE frame. **Owner decision:** the raw leader angles or the follower goal
   actually commanded (§6.2). A recording with teleop off must also be refused: on the OMX the data
   gate did that implicitly.
9. **The brief's principle "the PC never sends per-tick setpoints" is REFUTED for inference.**
   Cloud-Inferenz publishes one action per tick (30 Hz) as a single-point trajectory with
   `time_from_start = 1.5/fps` (`data_converter.tensor_array2joint_msgs`, `_cloud_inference_tick_body`).
   The board must accept a setpoint stream, unless the owner changes inference timing (§6.2, Q-E5).
10. **Cross-robot models are not refused today.** The Cloud-Inferenz checks compare cameras, fps,
    `state_dim` and `action_dim`, never the robot type or the joint *names*
    (`_cloud_preflight`, `inference_sessions.summary_from_config`). The model meta carries only
    `dataset_repo_id`, `dataset_fps` and `policy_type`. An OMX model with matching cameras
    (gripper + scene) has the same 6/6 dimensions and would be accepted on a V1. The brief says OMX
    models "do not run on a V1", but nothing enforces it. **VERIFIED.**
11. **Dead surface we would carry by default.** No current UI path uses: 7 `.srv` types; 5 served
    services with no caller (`/browse_file`, `/dataset/edit`, `/dataset/get_info`,
    `/get_saved_policies`, `/get_policy_list`); 2 services whose hook functions no component calls
    (`/training/command`, `/training/get_training_info`); the topic `/training/status`, which is
    subscribed but never published; the rosbag2 mode; and START_INFERENCE (local GPU inference). The
    replacement should not serve these without an owner decision (§5, list L).
12. **Recommendation, in short:** a ROS-free Python engine, carried over in two steps.
    - **Step 1:** today's node runs nearly unchanged on a small, semantics-faithful runtime with an
      rclpy-shaped API (executors, groups, timers, guard condition, latching), using generated message
      classes. It is **differential-tested against real rclpy** in today's Linux image. The browser
      talks to a faithful rosbridge-v2 *subset* adapter, so React is unchanged.
    - **Step 2:** replace the compat API with explicit lanes and a bus, module by module, under the
      same characterization traces.
    - Option (a), ROS on Windows, is rejected. Option (c), a typed API now, is deferred but enabled by
      the generated contract. Option (d), a rewrite, is rejected.

---

## 1. Refutations and corrections of the brief

| # | Brief says | Code says | Tag |
|---|---|---|---|
| B1 | `physical_ai_server/` has 95 `.py` files; 6 import rclpy; 9 import ROS message types | **111 files, 61 088 lines.** **8** import rclpy: the 6 named plus `daten/node_service.py` and `workflow/studio_camera.py`, both lazily. **7** files import message/service types: communicator, data_converter, data_manager, daten/node_service, physical_ai_server, collision_monitor, studio_camera. **10 files** touch ROS in total. | REFUTED (`grep -rlE '^\s*(import rclpy\|from rclpy)'`) |
| B2 | `physical_ai_server.py` 8 908 lines; `feetech_arm_node.py` 2 358 | **8 542** and **2 643** | REFUTED (`wc -l`) |
| B3 | React talks to 40 `.srv` services and 10 `.msg` types over ~43 files | **React calls 29 services:** 28 on the server and `/edubotics/activate` on the arm container. They use 27 `physical_ai_interfaces` srv types plus `std_srvs/Trigger`. 7 `.srv` types are dead (legacy calibration). React subscribes to **20 topics:** 14 from the server (one never published) and 6 from the arm container. **56 non-test React files** touch the transport; 19 import roslib directly. | REFUTED (§2) |
| B4 | Recorder is a "ROS-free core" to reuse | The logic is ROS-free, but `data_manager.py` and `data_converter.py` import `sensor_msgs`, `trajectory_msgs`, `geometry_msgs`, `nav_msgs`, `physical_ai_interfaces`, `builtin_interfaces` and **cv_bridge** at module top. `joint_msgs2tensor_array` dispatches on `isinstance(msg, JointTrajectory/JointState/…)`. The tests load it only behind `sys.modules` stubs (`tests/test_data_manager_record_fsm.py::_stub`). Removing cv_bridge also **removes the only reason for the numpy==1.26.4 cap**: LeRobot 0.5.1 itself declares `numpy>=2.0,<2.3`, and the training lock runs numpy 2.2.6. | REFUTED in the letter, true in spirit; needs a mechanical split + a decode-parity proof |
| B5 | Roboter Studio: "~14 injected callables" | `WorkflowManager.__init__` takes **16 callables** plus `arm_profile`, `code_rpc` and `simulated`. `workflow/` is ROS-free except `studio_camera.py` (factory-injected). | Corrected (`workflow_manager.py::WorkflowManager.__init__`) |
| B6 | "The PC sends targets and trajectories, never per-tick setpoints" | Inference does send per-tick setpoints: `_cloud_inference_tick_body` → `tensor_array2joint_msgs` (single point, `time_from_start = 1.5/fps`) → `communicator.publish_action`, every tick. | REFUTED for inference |
| B7 | Container table: `open_manipulator` replaced, "lost: nothing" | The container also runs `activation_agent.py` (the activation state machine and the **scene-camera profile statement**, D63). The V1 driver holds the **tower check** and **fall direction** (`feetech_geometry.tower_glide_decision`, `limp_fall_direction`, `/edubotics/tower_state`). The ingest node runs the **HELLO handshake + tower split** (cam ids 16/17), and the Pi has `scene_camera_node.py`. None of this can live in firmware, so it moves into the engine. | Omission |
| B8 | `physical_ai_server` → engine; lost: ROS tools + rosbag | Also changes: the SIGINT finalize chain (launch plan step 8), s6/launch respawn, the sensor supervisor's `os._exit(70)` for respawn, the stale-session notice keyed on `get_subscription_count()`, and the per-PC `ROS_DOMAIN_ID` (DDS LAN isolation; becomes unnecessary). | Omission |
| B9 | nginx → "the engine serves React; Origin/Host checks re-implemented" | nginx also does more for `/daten-api/` (GET/HEAD only, no logs because the token is in the path, `Referrer-Policy: no-referrer`, security headers) and serves `/video/stream` as an exact match, plus the 444 Host check. **The `:8769` Roboter-Studio control bridge is a React-visible HTTP contract** (`LeaderToggle.jsx`, `useRsBridgeStatus`, the sim-preview leader gate) that the brief does not mention. | Omission |
| B10 | HF token → "in-process memory + DPAPI" | `huggingface_hub` and **three child processes** (HF worker spawn, download worker, Daten sidecar) read the token from the `HF_TOKEN_PATH` *file* on every call. A DPAPI blob is not readable by them. Keeping the token in memory only means passing it into every subprocess, a design change to "no consumer passes a token". | Correction |
| B11 | The 2.41.0 tower kit | Not mentioned. In the engine it means: a third node (`StudioCamera`); the calibration services and state topic; the program-start gate; the tower guard; the V1 driver's tower check; `/edubotics/tower_state` and `/edubotics/home_glide_state`; the placement hint; the GUI's tower-fall question before Stop/Close/Scan, **which interacts with D7**; the D58 stamp provider; and the 1080p MJPEG split, which costs CPU and GIL time. | Omission |
| B12 | rosbridge emulation "with the same names and message shapes" | Names and shapes are not enough. React also relies on rosbridge **behaviour**: per-client per-topic `min(throttle_rate)`/`min(queue_length)` merging, leading-edge throttle drops, FIFO queue pacing, concurrent service calls (rosbridge ≥ 2.2; the images ship 2.7.0), request default filling and base64 `uint8[]` (§2.8). | Correction |
| B13 | Leader gates carry over | They do not: see finding 7. | REFUTED |
| B14 | Code sandbox sockets "become AF_UNIX (Windows 10+)" | CPython's `socket` module does not appear to expose `AF_UNIX` on Windows (bpo-33408 never merged). Named pipes (`multiprocessing.connection` `AF_PIPE`) or loopback TCP with a token are the portable choices. | UNVERIFIED-WINDOWS (sandbox agent's spike) |
| B15 | Daten "reuse as is" | `hub_sync.stage_lock` and `download_worker` use **`fcntl.flock`**. `node_service` uses **`os.killpg` / `start_new_session`**. The launch uses a **`nice -n` prefix**, and the secret lives in `/run/edubotics-daten`. All four are POSIX-only and need porting (LockFileEx, Job Objects, priority classes, a per-user directory). | Correction |
| B16 | Python 3.13+ for the clock | Confirmed: since 3.13, `time.monotonic` uses QueryPerformanceCounter on Windows (gh-88494). The **record timer's own jitter** also matters: the on-time rule is `≤ 1.5/fps` = 50 ms at 30 fps, and a tick late by one 15.6 ms default scheduler quantum arrives at 48.9 ms. The engine needs `timeBeginPeriod(1)`, which the GUI's `camera_bridge` already calls, or high-resolution waitable timers. | VERIFIED (sources) + UNVERIFIED-WINDOWS (jitter) |

---

## 2. Q1 — The complete wire surface of `physical_ai_server` today

### 2.1 Processes and transports of the server container

```
physical_ai_server container (s6 → ros2 launch physical_ai_server_bringup.launch.py)
 ├─ physical_ai_server (rclpy, MultiThreadedExecutor 8 threads)      ← node "physical_ai_server"
 │    ├─ physical_ai_server_sensors (SingleThreadedExecutor, thread "sensor-exec")
 │    └─ physical_ai_server_studio  (SingleThreadedExecutor, own thread)
 │    + threads: cloud client (asyncio), workflow daemon + hat threads, SimArm player,
 │      eager policy load, collision-reboot, code RPC accept/serve, daten recovery/remember/account
 │    + processes: HF worker (multiprocessing spawn), download worker, edit worker
 ├─ rosbridge_websocket (rosbridge_server 2.7.0, defaults: no auth, check_origin=True)
 ├─ web_video_server :8080 (/stream, respawn)
 ├─ rosbag_recorder service_bag_recorder (vestigial: no UI path)
 └─ daten sidecar :8095 (ExecuteProcess, nice 10, respawn)   ← reached via nginx /daten-api/
```
VERIFIED: `physical_ai_server_bringup.launch.py::generate_launch_description`,
`physical_ai_server.py::main`, `_ensure_sensor_executor`, `studio_camera.StudioCamera`.
The rosbridge version is 2.7.0 as measured on the image (`pi_agent/constants.py` comment,
`tests/test_rosbridge_origin_gate.py`).

### 2.2 Services the server serves (35 endpoints)

Group legend: **D** = the node default MutuallyExclusive group; **P** = `_preempt_cb_group`
(Reentrant); **H** = `_hf_cb_group` (Reentrant); **own** = its own MutuallyExclusive group.

| Service | Type | Callback | Group | Caller today |
|---|---|---|---|---|
| `/task/command` | SendCommand | `user_interaction_callback` | D | `useRosServiceCaller.sendRecordCommand` (Record page, `recordCommands.js`), `CollisionModal.js` (commands 8/9/10) |
| `/register_hf_user` | SetHFUser | `set_hf_user_callback` | H | `features/hfToken/robotChannel.js`; the GUI's window-close clear (`docker_manager._HF_CLEAR_SCRIPT`, `docker exec … ros2 service call`) |
| `/get_registered_hf_user` | GetHFUser | `get_hf_user_callback` | H | `hooks/useHfUserList.js` |
| `/get_policy_list` | GetPolicyList | `get_policy_list_callback` | D | **none** |
| `/get_saved_policies` | GetSavedPolicyList | `get_saved_policies_callback` | D | **none** |
| `/training/command` | SendTrainingCommand | `user_training_interaction_callback` (stub) | D | hook only, no component |
| `/training/get_available_policy` | GetPolicyList | `get_available_list_callback` | D | `PolicySelector.js` (falls back to the cloud `/policies`) |
| `/training/get_user_list` | GetUserList | `get_user_list_callback` | D | `DatasetSelector.js` |
| `/training/get_dataset_list` | GetDatasetList | `get_dataset_list_callback` | D | `DatasetSelector.js` |
| `/training/get_model_weight_list` | GetModelWeightList | `get_model_weight_list_callback` | D | `TrainingOutputFolderInput.js` |
| `/huggingface/control` | ControlHfServer | `control_hf_server_callback` | D | `MyModels.js` (model **download** to the robot) |
| `/training/get_training_info` | GetTrainingInfo | `get_training_info_callback` | D | hook only |
| `/calibration/start` | StartCalibration | `calibration_start_callback` | D | `CalibrationPanel.jsx`, `WorkshopPage.js` |
| `/calibration/cancel` | CancelCalibration | `calibration_cancel_callback` | P | `CalibrationPanel.jsx` |
| `/calibration/status` | CalibrationStatus | `calibration_status_callback` | P | `Home/HealthCard.jsx`, `CalibrationPanel.jsx` |
| `/workshop/mark_destination` | MarkDestination | `mark_destination_callback` | D | `CameraFeedOverlay.jsx` |
| `/workshop/capture_pose` | WorkshopCapturePose | `capture_pose_callback` | P | `teach/TeachOverlay.jsx`, `teach/useTeachSession.js` |
| `/workshop/jog` | WorkshopJog | `workshop_jog_callback` | P | `JogPanel.jsx`, `blocks/destinations.js`, `WorkshopPage.js`, `HomeGlidePrompt.jsx` (mode `home`) |
| `/workshop/hand_guide` | WorkshopHandGuide | `workshop_hand_guide_callback` | P | `JogPanel`, `HomeGlidePrompt`, `TeachOverlay`, `useTeachSession`, `TowerHint/TowerHint.jsx` |
| `/workshop/record` | WorkshopRecordControl | `workshop_record_callback` | P | `TeachOverlay`, `useTeachSession` |
| `/workshop/replay` | WorkshopReplay | `workshop_replay_callback` | P | `TeachOverlay`, `useTeachSession` |
| `/workshop/get_object_catalog` | GetObjectCatalog | `get_object_catalog_callback` | D | `WorkshopPage.js` |
| `/workflow/start` | StartWorkflow | `workflow_start_callback` | D | `RunControls.jsx`, `hooks/useSimPreview.js`; also internally from `_start_calibration_program` (arm check) |
| `/workflow/stop` | StopWorkflow | `workflow_stop_callback` | D | `RunControls.jsx`, `SimScene.jsx` (simulator reset), `CalibrationPanel.jsx` |
| `/workflow/pause` · `/step` · `/continue` | WorkflowPause/Step/Continue | `workflow_*_callback` | P | `RunControls.jsx` |
| `/workflow/set_breakpoints` | WorkflowSetBreakpoints | `workflow_set_breakpoints_callback` | P | `RunControls.jsx`, `code/CodeWorkspace.jsx`, `BreakpointList.jsx` |
| `/edubotics/cloud_inference/start` | StartCloudInference | `start_cloud_inference_callback` | D | `features/cloudInference/robotCalls.js` |
| `/edubotics/cloud_inference/stop` | std_srvs/Trigger | `cloud_inference_stop_callback` | D | `robotCalls.js`; the GUI's window-close hook (`docker_manager._CLOUD_STOP_SCRIPT`) |
| `/image/get_available_list` | GetImageTopicList | `Communicator.get_image_topic_list_callback` | D | `ImageGrid.js`, `Home/HealthCard.jsx`, `RecordPage.js` |
| `/browse_file` | BrowseFile | `Communicator.browse_file_callback` | D | **none** |
| `/dataset/edit` | EditDataset | `Communicator.dataset_edit_callback` | own | **none** (the old page went with Daten 2.0) |
| `/dataset/get_info` | GetDatasetInfo | `Communicator.get_dataset_info_callback` | D | **none** |
| `/daten/command` | DatenCommand | `DatenService._command_callback` | own | `features/editDataset/hooks/useDatenCommand.js` |

VERIFIED: `physical_ai_server.py::_init_ros_service`, `communicator.py::Communicator.init_services`,
`daten/node_service.py::DatenService._wire_ros`. The caller mapping comes from a Python scan of
`src/` (hook function → component users), run in scratch.

**Unserved `.srv` types (7, dead):** AutoPoseSuggest, CalibrationCaptureFrame, CalibrationHistory,
CalibrationPreview, CalibrationSolve, ExecuteCalibrationPose, VerifyCalibration. These are non-goal N6.

### 2.3 Topics the server publishes (17)

| Topic | Type | QoS (publisher) | Rate / trigger | Node | Consumer |
|---|---|---|---|---|---|
| `/task/status` | TaskStatus | **two publishers**: `Communicator.status_publisher` (RELIABLE, VOLATILE, depth 100) and `collision_monitor._collision_status_pub` (depth 10, OMX only) | every record/inference tick (fps; cloud ≤ 2 Hz), idle identity tick every 2 s after ≥ 3 s of silence, events | main | `useRosTopicSubscription`, `StudentApp.js`, `CollisionModal`, … |
| `/heartbeat` | std_msgs/Empty | BEST_EFFORT, VOLATILE, depth 1 | 1 Hz, own group | main | `useHeartbeatWatchdog` (3 s → "Getrennt") |
| `/training/status` | TrainingStatus | depth 100 | **never published** | main | `useRosTopicSubscription` (dead) |
| `/workflow/status` | WorkflowStatus | depth 50 | per block / log line / detections | main | `workshopSlice`, `studioAssetsSlice`, `SimStage`, the `[VAR:]`/`[CNT:]` parsers |
| `/workflow/sensors` | SensorSnapshot | depth 5 | 5 Hz while `on_workflow` | main | `SensorPanel.jsx` |
| `/huggingface/status` | HFOperationStatus | depth 10 | 2 Hz while the HF worker lives | main | `useRosTopicSubscription`, `DatenPage`, `recordSession` |
| `/edubotics/signal_status` | String (JSON v1) | RELIABLE + TRANSIENT_LOCAL, depth 1 | 1 Hz, own group | main | `useSignalStatus` |
| `/edubotics/hf_token_state` | String (JSON) | latched | 1 Hz, own group | main | `robotChannel.js` |
| `/edubotics/cloud_inference_state` | String (JSON) | latched | 1 Hz, own group + transitions | main | `useCloudInferenceState` |
| `/edubotics/daten_state` | String (JSON) | latched | 1 Hz + every job change | main | `useDatenState` |
| `/edubotics/calibration_state` | String (JSON) | latched | 1 Hz + every change (tower-kit arms only) | main | `useCalibrationState` |
| `/edubotics/home_glide_state` | String (JSON) | latched | 2 Hz; V1 + `tower_v1` + idle only; publisher created lazily | main | `useTowerState` |
| `/sim/joint_states` | JointState | depth 10 | the player's frames (~30 Hz); a 2 Hz heartbeat when quiet | main | `SimScene` → `UrdfTwin` (interpolates on `header.stamp`) |
| `/sim/objects` | String (JSON) | depth 10 | on change + heartbeat | main | `useSimObjects` |
| `/leader/joint_trajectory` | JointTrajectory | **three publishers**: workflow/manual/hold (depth 10, created at boot), inference (`Communicator.joint_publishers`, depth 100), collision relax/home/resync (RELIABLE + TRANSIENT_LOCAL, depth 1, OMX only) | chunks ≤ 1 s; one point per inference tick | main | the arm: V1 driver / OMX JTC |
| `/collision_flag` | std_msgs/Bool | RELIABLE + TRANSIENT_LOCAL, depth 1 | at the trip + a 5 Hz watchdog; `False` at boot | main | the OMX leader broadcaster (OMX only) |

VERIFIED: every `create_publisher` call (`grep -rnE "create_publisher\("`).

### 2.4 Topics the server subscribes to

| Node | Topic | Type | QoS | Notes |
|---|---|---|---|---|
| sensors | `/<cam>/image_raw/compressed` (YAML: V1 `scene`; OMX `gripper`, `scene`) | CompressedImage | BEST_EFFORT, KEEP_LAST 32 | recorder, inference, perception, signal status |
| sensors | `/joint_states` | JointState | BEST_EFFORT, 32 | follower source |
| sensors | `/leader/joint_trajectory` | JointTrajectory | BEST_EFFORT, 32 | **leader source = action**; subscribed at boot in every profile (the Communicator is built in `collection` mode), so on the V1 it also hears the server's own trajectories |
| sensors (OMX only) | `/joint_states`, `/leader/joint_states`, `/gpio_command_controller/gpio_states` | JointState ×2, `control_msgs/DynamicInterfaceGroupValues` | RELIABLE, 32 | collision detector; `leader_appears_active()`; **skipped on the V1** (`collision_enabled=False`) |
| main | `/leader/joystick_controller/tact_trigger` | String | depth 10 | OMX leader buttons |
| studio | `/studio/tower/compressed` | CompressedImage | BEST_EFFORT depth 1, **on demand** | arm check, mat check, perception |
| studio | `/edubotics/scene_camera_state`, `/edubotics/scene_camera_profile` | String | latched | D63 statement; `profile_id` / `platform` for the D58 stamp |

**Service clients:** `profile.torque_service` (SetBool; V1 `/v1/set_torque`, OMX
`/dynamixel_hardware_interface/set_dxl_torque`). OMX collision recovery:
`/dynamixel_hardware_interface/reboot_dxl` (`dynamixel_interfaces/RebootDxl`) and `set_dxl_torque`.
Also `rosbag_recorder/send_command`, with a 3 s boot probe and a 10 s retry timer.

### 2.5 Arm-container surfaces the browser reaches directly

React uses these without the server:
- **Topics:** `/joint_states` (`UrdfTwin` 30 ms / queue 1, `TeachOverlay` 30 ms / 0,
  `useJointLiveness` 1 s / 1, `LeaderToggle` 150 ms / 1); `/edubotics/activation_state`
  (`useRobotActivation`); `/edubotics/tower_state` (`useTowerState`); `/edubotics/scene_camera_profile`
  (`useSceneCameraProfile`, `useCalibrationState`, `useTowerState`); `/scene/image_raw/compressed` and
  `/gripper/image_raw/compressed` (`CameraFeedOverlay` 1 s, `useCameraLiveness` 4 s, `LeaderToggle`
  0.5 s; the payload is ignored).
- **Service:** `/edubotics/activate` (Trigger, `useRobotActivation`).
- **The GUI** reads `/edubotics/tower_state` with `docker exec … ros2 topic echo --once`
  (`docker_manager.TOWER_STATE_SCRIPT`).

### 2.6 Non-ROS surfaces

| Surface | Where | Consumer |
|---|---|---|
| `GET /video/stream?quality=…&type=ros_compressed&default_transport=compressed&topic=…` (MJPEG passthrough) | web_video_server :8080, behind nginx's exact `location = /video/stream` | `ImageGridCell.js`, `CameraFeedOverlay.jsx`, Startseite hero |
| `/daten-api/v1/{health, lib/<T>/…, ds/<T>/…}`, GET/HEAD only | Daten sidecar :8095 (`daten/http_server.py`), behind nginx (no logs, `no-referrer`) | `features/editDataset/api/datenHttp.js` |
| Link tokens `v1.<payload>.<HMAC>` | minted by the node (`/daten/command link`), verified by the sidecar; secret `/run/edubotics-daten/secret` | Daten page |
| `http://localhost:8769/roboter-studio/{status, leader-enable, leader-disable}`; on the Pi `/api/system/roboter-studio/…` | the GUI's `roboter_studio_control.py`; the Pi agent | `LeaderToggle.jsx`, `useRsBridgeStatus.js`, the sim-preview leader gate |
| Unix sockets `/run/edubotics/code/rpc.sock` (the server binds it, 0666) and `runner.sock` (the supervisor binds it; the server connects) | `workflow/code_rpc.py::CodeRpcServer`, `code_program.py` | `code_runner` container |
| HF token slot (tmpfs file `$HF_TOKEN_PATH=/run/edubotics-hf/token`, 0600) | `hf_token_store` | huggingface_hub in the node, HF worker, download worker, sidecar |
| Robot key file `/run/edubotics-robot-key/hf_token_key.json` | `hf_token_crypto.write_key_file` | Pi agent `/robot-key` |
| Cloud-run marker `/tmp/edubotics-cloud-inference.active` | `cloud_contract.ACTIVE_MARKER_PATH` | the GUI's window-close stop |
| Dataset-root siblings `.session.json`, `.sync.json`, `.lock`, `.journal.json`; calibration store `<EDUBOTICS_CALIB_DIR>/calibration/*.json` | recorder, Daten, autocal | node + workers |
| Outbound HTTPS | Hugging Face (token file), Modal `edubotics-inference` (cloud client) | — |
| 42 `EDUBOTICS_*` environment knobs read by the server package (one-variable rollbacks) | `grep -rhoE "['\"]EDUBOTICS_[A-Z_0-9]+['\"]"` | the replacement needs a settings source |

### 2.7 ROS parameters

The node uses one: the config YAML, glob-loaded and namespaced by `data_robot_type`. It holds
`camera_topic_list`, `joint_topic_list`, `observation_list`, `joint_list`, `rosbag_extra_topic_list`
and `joint_order.<name>` (`parameter_utils.declare_parameters` / `load_parameters`,
`init_ros_params`). Nothing else is parameterized through ROS. VERIFIED.

### 2.8 What the browser actually uses of rosbridge v2, and the proof

- **roslib 1.4.1** (`node_modules/roslib/package.json`).
  - Client ops: `subscribe` `{id, type, topic, compression:'none', throttle_rate, queue_length}`,
    `unsubscribe` `{id, topic}` and `call_service` `{id, service, type, args}`
    (`roslib/src/core/Topic.js`, `Service.js`).
  - No component calls `advertise`, `publish`, `advertise_service`, actions, `png`/`cbor` or
    `rosapi`.
  - Server ops handled by roslib: `publish`, `service_response` (`result` false → the failed
    callback) and `status` (`SocketAdapter.js::handleMessage`).
  - VERIFIED: `grep` over `src/` found no `.publish(` or `advertise` call.
- **rosbridge ≥ 2.x behaviour React implicitly relies on.** VERIFIED from the upstream source
  (`rosbridge_library/capabilities/subscribe.py`, `internal/subscription_modifiers.py`):
  1. One subscription per topic **per client**, with `throttle_rate = min`, `queue_length = min` over
     subscription ids, and one delivery per message. roslib then fans out to every `ROSLIB.Topic` with
     that name, so a 1 s liveness hook sharing `/joint_states` with the twin also receives 30 ms
     messages.
  2. `queue_length = 0` with `throttle > 0` → leading-edge throttle; the last value in a window can be
     lost.
  3. `queue_length > 0` → a FIFO of the newest N, at most one per throttle interval.
  4. Service calls run concurrently (2.2.0+, Feb 2025); the images measured 2.7.0.
  5. Missing request fields are default-filled and unknown fields refused.
  6. `uint8[]` travels as base64.
  7. No server-side call timeout (React's 10 s `callService` timeout governs).
- **Prototype:** `scratchpad/agent1/rbproto/rb_server.py` (stdlib asyncio WebSocket + the generated
  schema) was driven by the SPA's own roslib 1.4.1 (`client.js`). Result:
  - latched `/task/status` delivered 5 ms after subscribe;
  - partial `SendCommand` → `private_mode=True`, `fps=30` filled;
  - `command:"1"` → failed callback ("integer expected");
  - CompressedImage `data` base64 round trip;
  - two subscriptions to `/joint_states` (30 ms/0 and 1 000 ms/1) **both received 60 deliveries in
    2 s** (the merge semantic);
  - a 1.5 s call and a later fast call finished fast@55 ms, slow@1 509 ms.

  **VERIFIED.**

### 2.9 Checklist: what a replacement must serve (React unchanged)

1. WebSocket at `ws://<page-host>/rosbridge`: ops `subscribe`, `unsubscribe`, `call_service` → out
   `publish`, `service_response`, `status`, with the semantics of §2.8 and every other op **refused**.
2. **The 28 server services in §2.2 that have a UI caller**, plus **`/edubotics/activate`** (today
   served by the arm container). The owner decides on the 7 served services without a caller
   (§11 Q8).
3. **13 live server topics** + `/training/status` (or an owner-approved drop) + **6 arm-side topics**:
   `/joint_states` (stamped for the interpolator), `/edubotics/activation_state`,
   `/edubotics/tower_state`, `/edubotics/scene_camera_profile` and the two camera topics, latched
   wherever the publisher is latched today.
4. HTTP:
   - `/video/stream` with today's query string;
   - `/daten-api/` proxy with nginx's rules;
   - the SPA build, `/version.json`, `/third-party-licenses.txt`, fonts;
   - the `:8769` control-bridge JSON contract (or a React change);
   - Origin/Host checks equal to `nginx.conf`, tested like `tests/test_student_nginx_rebinding.py`.
5. Files and processes:
   - a token slot file readable by the HF/download/sidecar processes;
   - the Daten secret;
   - the cloud-run marker only if something outside the engine still needs it (it does not, once the
     GUI is in-process);
   - two code-runner channels.

---

## 3. Q2 — The rclpy seam, precisely

### 3.1 The ROS imports and what each file uses

| File | rclpy API | Message types |
|---|---|---|
| `physical_ai_server.py` | Node, MutuallyExclusive/Reentrant groups, Single/MultiThreadedExecutor, ExternalShutdownException, QoSProfile + 3 enums, Duration, `create_node`, `init`/`ok`/`try_shutdown`, timers, services, client futures, `get_clock` | 6 msgs + 28 srvs, std_msgs Empty/String, std_srvs Trigger/SetBool, trajectory_msgs, sensor_msgs (lazy) |
| `communication/communicator.py` | `create_publisher/service/client/timer/subscription` (via MultiSubscriber), QoS | sensor_msgs, trajectory_msgs, geometry_msgs, nav_msgs, std_msgs, `rosbag_recorder.srv`, 4 interface srvs |
| `communication/multi_subscriber.py` | `create_subscription`, `destroy_subscription`, QoS | — |
| `safety/collision_monitor.py` | publishers, timers, **guard condition**, clients, latched QoS | TaskStatus, JointState, Bool, JointTrajectory(+Point), `control_msgs` gpio, `dynamixel_interfaces/RebootDxl`, SetBool |
| `timer/timer_manager.py` | `create_timer(callback_group=)`, `Timer.destroy` | — |
| `utils/parameter_utils.py` | `declare_parameter`, `has_parameter`, `get_parameter` | — |
| `daten/node_service.py` | (lazy) publisher, service, timer, groups, latched QoS | DatenCommand, String |
| `workflow/studio_camera.py` | (lazy, via factories) node, SingleThreadedExecutor, ExternalShutdownException, QoS | CompressedImage, String |
| `data_processing/data_converter.py` | — | **cv_bridge**, builtin Duration, JointState, JointTrajectory(+Point), Twist, Odometry, CompressedImage (+ `import torch`) |
| `data_processing/data_manager.py` | — | TaskStatus (built per tick), JointState/JointTrajectory/Twist/Odometry (`isinstance` dispatch) |

API counts measured over the package: `get_logger` 289, `MutuallyExclusiveCallbackGroup` 32,
`create_timer` 23, `create_publisher` 22, `QoSProfile` 17, `destroy_timer` 10,
`SingleThreadedExecutor` 9, `ReentrantCallbackGroup` 8, `create_service` 7, `create_subscription` 5,
`wait_for_service` 5, `create_client` 4, `call_async` 4, `trigger` 1, `get_subscription_count` 1.
**This is the whole rclpy surface the code needs: about 20 classes and functions.** VERIFIED.

### 3.2 Executors, nodes, callback groups, timers

| rclpy construct | Members | Rate |
|---|---|---|
| **main node, MultiThreadedExecutor(8)** — the default ME group | `/task/command`, `/workflow/start`, `/workflow/stop`, `/calibration/start`, `/workshop/mark_destination`, `/workshop/get_object_catalog`, `/huggingface/control`, cloud start/stop, the legacy list services, the Communicator's 3 services; **timers:** inference tick (local and cloud), sensor snapshot, HF status, sim heartbeat, stale-session notice, **manual-record sampler, leader-teach sampler**, their cap/reap one-shots, **collision relax / glide / resync one-shots**; the joystick subscription | fps / 5 Hz / 2 Hz / 2 Hz / 5 s / 25 Hz / 25 Hz / one-shots |
| `_record_cb_group` | record timer | task fps |
| `_collision_cb_group` | `_on_trip_gc` (guard condition) + `_collision_watchdog_cb` | event + 5 Hz |
| `_heartbeat_cb_group` | heartbeat | 1 Hz |
| one own ME group each | idle identity tick, signal status, manual idle watchdog, re-init retry, HF-token state, cloud state, calibration state, home-glide state, rosbag probe, Daten tick, `/daten/command`, `/dataset/edit` | 0.5 Hz, 1 Hz, 0.2 Hz, 1/30 Hz, 1 Hz, 1 Hz, 1 Hz, 2 Hz, 0.1 Hz, 1 Hz |
| `_preempt_cb_group` (Reentrant) | 11 services (calibration cancel/status, capture_pose, jog, hand_guide, record, replay, workflow pause/step/continue/set_breakpoints) | — |
| `_hf_cb_group` (Reentrant) | `/register_hf_user`, `/get_registered_hf_user` | — |
| **sensor node, SingleThreadedExecutor** (thread `sensor-exec`, supervised) | cameras, follower, leader + (OMX) the collision monitor's 3 subscriptions | ~30–100 Hz each |
| **studio node, SingleThreadedExecutor** | statement, camera state, studio picture (on demand) | ≤ 15 Hz |

VERIFIED: `physical_ai_server.py::__init__`, `_init_ros_publisher`, `_init_ros_service`,
`_init_hf_token_state`, `_init_cloud_inference_state`, `_init_calibration_state`,
`_init_home_glide_state`, `init_robot_control_parameters_from_user_task`, `_init_hf_api_worker`
(TimerManager with no group → default), `workshop_record_callback` (samplers with no group → default),
`collision_monitor._schedule_relax/_schedule_glide/_schedule_resync_completion` (no group → default),
`communicator.init_services`, `daten/node_service._wire_ros`.

### 3.3 Invariants enforced by rclpy/DDS/rosbridge semantics, not by explicit code

Each row says what holds today, the evidence, and what the ROS-free engine must do explicitly.

| # | Invariant held only by the runtime | Evidence | Explicit replacement |
|---|---|---|---|
| R1 | **The default ME group serialises the cloud tick, cloud start/stop and `/task/command` FINISH/STOP** ("so both serialize … and with the cloud tick; neither blocks") | comment at the cloud entries of `_init_ros_service` | one serial `default` lane for these handlers and the inference timer |
| R2 | **The collision FSM's one-shot timers (relax, glide, resync completion) serialise with HOME_FOLLOWER / RESUME_TELEOP / FORCE_RESUME_TELEOP** (all in `/task/command`). They mutate `_collision_homing`/`_homed`/`_resync_timer` without locks | `collision_monitor._on_resync_complete` ("runs in the node's default callback group with /task/command"), `_schedule_*` | the collision FSM's timers and commands on the same serial lane (or an explicit FSM lock) |
| R3 | **The manual-record and leader-teach samplers (25 Hz) never overlap a default-group service** (`/workflow/start`, `/calibration/start`, …) | `workshop_record_callback` creates both timers with no group | assign them to the `default` lane, or prove each sampler independent |
| R4 | Record ticks never overlap each other; they overlap `/task/command` on purpose (handled by explicit locks, round 5) | `_record_cb_group` | a serial `record` lane |
| R5 | Trip hand-over and watchdog are mutually exclusive ("a watchdog False can never race the trip") | `_collision_cb_group` | a serial `collision` lane |
| R6 | **Status tickers are not starved by a blocking callback** (heartbeat → "Getrennt" after 3 s) as long as one of the 8 threads is free | own groups + `MultiThreadedExecutor(num_threads=8)` in `main` | dedicated ticker threads; a CI test: heartbeat jitter < 100 ms while a 5 s handler blocks every pool |
| R7 | **The sensor thread processes cameras, follower, leader, gpio *in arrival order on one thread*:** (a) per-source order; (b) the detector reads the velocity of the latest follower message processed *before* the gpio message — no race; (c) liveness can be measured (`_sensor_alive_mono`) | `_ensure_sensor_executor`, `_process_gpio_states` reads `_collision_follower_vel` | one ingest thread per board link (velocity, effort and leader come in one frame — stronger); the supervisor must exist |
| R8 | The studio node's three subscriptions on one thread; the picture subscribed only on demand | `StudioCamera` | same in the engine (an in-process consumer) |
| R9 | **QoS:** (a) BEST_EFFORT depth 32 → "a burst after a stall arrives complete" (the capture timeline relies on it); (b) latched + 1 Hz re-publish for late rosbridge subscribers; (c) `/collision_flag` latched + `False` at boot; (d) a VOLATILE command rail with the publisher created at boot so DDS discovery has matched before the first hold; (e) `/leader/joint_states` RELIABLE ("safety-load-bearing") | `communicator.SENSOR_QOS_DEPTH`, the state publishers, `collision_monitor._init_collision_monitor`, the `__init__` comment "The command-rail publisher is created at BOOT", `_wait_for_command_rail_subscriber` | (a) a lossless, ordered board stream with a buffer ≥ the longest PC stall; (b) latched = replay the last value on subscribe (deterministic), and **keep** the 1 Hz re-sends because React treats them as liveness; (c)–(e) disappear with DDS |
| R10 | The guard condition wakes the main executor from the sensor thread | `_hand_over_trip` → `guard.trigger()` | a lane post (thread-safe queue) |
| R11 | rclpy timer semantics: periodic, no backlog burst, self-destroy from inside the callback allowed; `TimerManager.start` recreates a stopped timer *in the same group* (the seamless collision resume relies on it) | `timer_manager.TimerManager`, `_data_collection_timer_callback` stops its own timer | a timer facility with exactly these semantics + tests |
| R12 | `use_global_arguments=False` keeps the launch remap from renaming the extra nodes | `_ensure_sensor_executor` | disappears |
| R13 | **Supervision:** launch `respawn` 2 s, s6 down-signal SIGINT, `sigterm_timeout` 10 / `sigkill_timeout` 30, compose `stop_grace_period` 50, GUI/Pi 120; the sensor supervisor `os._exit(70)` after 3 failures in 10 s → respawn; an uncaught callback exception kills the node → respawn | `physical_ai_server.launch.py`, `tests/test_recording_stop_budget.py`, `_note_sensor_failure` | the app supervises the engine (restart, backoff); the engine catches per-callback exceptions (a *behaviour change*: crash → continue) |
| R14 | **SIGINT → SIG_DFL before `rclpy.init`** so rclpy's handler only shuts the context down and `finally` finalizes a recording with no KeyboardInterrupt | `main()` | an explicit shutdown command from the app + a bounded finalize (30 s), then a Job-Object kill; WM_ENDSESSION handling (UNVERIFIED-WINDOWS) |
| R15 | **rosbridge semantics** (§2.8): per-client min/min merge, leading-edge throttle, FIFO queue pacing, concurrent calls, default filling, base64; and its holes: unauthenticated, any client may publish/advertise (VB-03) | upstream source; `pi_agent/constants.py` (2.7.0) | port `subscription_modifiers` faithfully; **refuse every client publish/advertise**; authenticate the page |
| R16 | ROS parameters: YAML namespaced by robot type | `parameter_utils` | a plain YAML loader with the same keys |
| R17 | ROS clock = wall clock for `/sim/joint_states` and SensorSnapshot stamps; the V1 driver stamps `/joint_states` with its wall clock; the React interpolator works on publisher stamps | `_stamp_sim_joint_state`, `feetech_arm_node` read loop, `utils/jointStateInterpolator.js` | stamp outgoing JointStates in epoch time mapped from board µs |
| R18 | DDS discovery across containers (the "publisher created at boot" lesson) | `__init__` comment | disappears |
| R19 | `get_subscription_count()` as "a browser is attached" (stale-session notice) and "the arm driver listens" (hold delivery aid) | `init_ros_params::_stale_session_poll`, `_wait_for_command_rail_subscriber` | ask the WS adapter / the board client directly |

**Two more properties to state, not lose:**
- The default group is *mutually exclusive across services*. A 2–6 s torque switch inside
  `/workflow/start` blocks `/task/command`, the cloud tick and the 25 Hz samplers today. That is a
  latent liability, not a feature. The engine should keep the serialization where R1–R3 need it and
  document the rest.
- **Two publishers on `/task/status`** have no cross-publisher order guarantee in DDS. Today
  `_record_publish_lock` prevents a SAVING after COLLISION. An in-process bus gives total order, which
  is strictly better.

### 3.4 `physical_ai_server.py` by function

Measured with `scratchpad/agent1/categorize.py` (method spans, AST):

| Function | Lines | Share |
|---|---|---|
| Manual control: jog, home glide (+ V1 state), hand-guide, record, leader-teach, replay, capture pose, idle watchdog | 1 965 | 23.0 % |
| Record tick (round 5) + `/task/command` + disk floor + joystick | 851 | 10.0 % |
| Module header, constants, `_CloudRun`, `main` | 684 | 8.0 % |
| Workflow runtime glue (status packing, trajectory publisher, manager wiring, start/stop/debugger, sensor snapshot) | 642 | 7.5 % |
| Boot, degraded boot, re-init | 620 | 7.3 % |
| Simulator glue (`/sim/*`, reset, sim manager) | 518 | 6.1 % |
| Cloud inference | 447 | 5.2 % |
| HF worker + uploads + `/huggingface/control` | 412 | 4.8 % |
| Mode arbiter, torque switch, hold-in-place | 360 | 4.2 % |
| Service table, parameters, task init | 355 | 4.2 % |
| IK / perception / tower guard / calibration loading | 291 | 3.4 % |
| HF token slot | 275 | 3.2 % |
| Tower calibration glue | 260 | 3.0 % |
| Legacy list / training services | 247 | 2.9 % |
| Local inference | 205 | 2.4 % |
| Status topics (heartbeat, idle, signal) | 182 | 2.1 % |
| Sensor executor + shutdown finalize | 160 | 1.9 % |
| rosbag (legacy) | 68 | 0.8 % |

**Glue vs logic.** Methods whose main job is wiring come to about 1 100–1 200 lines (≈ 14 %):
`_init_ros_*`, sensor executor, `_emit_workflow_status` packing, `_trajectory_publisher`, the state
publishers, `main`. Of the 5 359 code lines, **2.7 % call the ROS API**, 4 % log, and 9 % fill
responses. **Everything else is behaviour that must survive byte for byte.** In the other ROS files
the share of ROS lines is: communicator 4.2 %, collision_monitor 6.0 %, daten/node_service 1.0 %,
studio_camera 5.7 %. VERIFIED.

---

## 4. Q3 — What the server consumes from the arm container (the requirements for the board client and the firmware stream)

### 4.1 Consumed today

| Item | Producer today (V1 / OMX) | Content, rate, stamp, QoS | Consumers in the server | Board-world source |
|---|---|---|---|---|
| Follower state `/joint_states` | V1 `feetech_arm_node._read_loop` **50 Hz** (`LOOP_HZ`), stamp = node wall clock after the read; OMX `joint_state_broadcaster` 100 Hz | names (URDF-native: `joint1..5`, `RL_joint` / `gripper_joint_1`), position, velocity, effort (V1 signed load fraction /1000); pub depth 10 RELIABLE | recorder state (`SourceHistory`, `ClockMap`); `get_latest_follower_joints` (jog, replay, capture, home glide, workflow seed, FK, arrival trim, tower-guard start, home-glide state); 1 s staleness gate; collision velocities (OMX) | STATE frame follower q, dq, load, board µs |
| Leader action `/leader/joint_trajectory` | OMX `joint_trajectory_command_broadcaster` 100 Hz after activation; **V1: none** (only the server's own writers) | `points[0].positions`, **stamp 0** → arrival, back-dated inside a burst | recorder action; `signal_status` (leader row only when `has_leader`) | STATE frame leader q (or goal q, owner) with the **board stamp** |
| `/leader/joint_states` | OMX leader broadcaster 100 Hz | RELIABLE | `leader_appears_active()` (2 s window) → HIGH-6 workflow gate, manual block, cloud refusal/end, leader-teach start, collision trip gate | **replaced by the board's command-source state** (§6.2) |
| Effort/overload `/gpio_command_controller/gpio_states` | OMX gpio controller 100 Hz | XL430 Present Load, XL330 Present Current, Hardware Error Status | collision detector (debounce computed at 100 Hz) | STATE load + error bits (≥ 100 Hz) or a board EVENT (D5) |
| Camera frames | Windows: GUI MSMF → TCP 5557 → `camera_ingest_node` (`capture_ns` stamp); Pi: usb_cam / `scene_camera_node` | CompressedImage ~30 Hz, RELIABLE depth 5 → BEST_EFFORT 32 | recorder, cloud inference (JPEG passthrough + SOF size), perception, signal status, MJPEG | the engine's own capture (in- or out-of-process, §10) |
| Studio picture / camera state / profile statement | ingest cam 16/17, activation agent | ≤ 15 Hz JPEG; latched JSON | `StudioCamera`: calibration, perception, D58 provider, guard scope | engine capture + engine settings |
| Torque service | V1 `/v1/set_torque` (+ alias); OMX `set_dxl_torque` | SetBool; refusal message = a German reason (edge, wrongway, …) | hand-guide, re-lock, jog/workflow assert, idle watchdog | board TORQUE (seed Goal = Present) + a reason code → German on the PC |
| Reboot of latched joints | OMX `reboot_dxl` | — | collision recovery | STS overload semantics differ → board design |
| **Published to the arm** `/leader/joint_trajectory` | — | multi-point chunks ≤ 1 s (30 pts/s), single-point holds (t = 0.05/0.25 s, zero velocity), inference single points (t = 1.5/fps); **a new message replaces** the running one | — | board TRAJ (replace / append) + HOLD-at-current |
| `/collision_flag` | — | latched Bool | OMX leader broadcaster | board MODE (teleop freeze) |

### 4.2 Requirement list for the board client and the STATE stream

Derived from the consumers above, not from the brief.

1. **Fields per frame.**
   - follower q[6], dq[6], load[6] (signed fraction), servo error/status bits[6], torque state;
   - **leader q[6]**, leader present flag, **follower goal actually written[6]** (the recorded action
     if the owner picks it);
   - active command source (hold / trajectory / teleop / limp / fault) and trajectory progress
     (chunk id, index);
   - board timestamp **µs, 64-bit** (a 32-bit µs counter wraps after 71.6 min) and seq (u16, the
     engine unwraps it);
   - bus-read health (the driver's `READ_FAIL_STOP_S` = 1 s latch);
   - temperature and voltage, optional but useful for stall diagnosis.
2. **Rate.** ≥ 50 Hz minimum (today's V1); **100 Hz recommended**. The collision detector's debounce
   is defined in ticks at `update_rate_hz=100` (`build_detector_from_env`). The sampler takes the
   newest sample ≤ R (10 ms old at 100 Hz). `ARM_DATA_LATE_WARN_S` is 0.1 s.
3. **Timestamp semantics.** Stamps mean "sampled at"; follower and leader are sampled in **one bus
   transaction**, so one stamp is valid for both (better than today's stamp-0 leader). The engine maps
   them through `ClockMap` (running min of arrival − stamp over 2 s). **`ClockMap.MAX_LAG_S = 0.5`
   clamps a backlog older than 0.5 s and collapses its spacing.** Board stamps are monotonic and
   trustworthy, so the board source should get a per-source ClockMap config (offset tracking without
   the lag clamp) — proof: injected 0.5–1 s PC stalls produce no spurious gap re-record
   (UNVERIFIED-WINDOWS).
4. **Ordering and loss.** In order, no duplicates. Loss is tolerated: a CRC-dropped frame is a 10 ms
   gap, far below `SOURCE_GAP_S` = 0.25 s. **A stalled PC must not lose the backlog:** the reader
   thread drains OS buffers; at 100 Hz × ~120 B = 12 kB/s, a 1 s stall is 12 kB.
   `SOURCE_STOPPED_S` = 2 s applies to *arrival*, so the engine must keep reading during long record
   steps (a dedicated reader thread; GIL contention is a risk, §9).
5. **Commands.**
   - TRAJ with *replace now* and *append after current* (the latter fixes KNOWN-ISSUES' chunk-boundary
     bump, UNVERIFIED-BENCH VR12);
   - single-point HOLD-at-current executed with the board's own reading, which removes the stale-pose
     `allow_stale` nuance;
   - TORQUE on/off returning a reason code (`edge`, `wrongway`, `bus_fault`, `eeprom`);
   - MODE teleop on/off, limp, hold;
   - an EVENT when a trajectory is refused or finished (today a refusal only logs, and the program
     "finishes green" — `_launch_workflow` torque assert);
   - inference setpoints at fps (a single point with t = 1.5/fps).
6. **Identity.** A HELLO_ACK with servo ids, models (777/2825), EEPROM fingerprint ok/fail (the
   driver's `probe_bus` German causes), firmware version, joint count, and leader present.
7. **The engine still owns** (the firmware cannot):
   - the floor check and the **tower check** before any home glide (`boot_home_floor_decision`,
     `tower_glide_decision`, stdlib geometry);
   - the **fall direction** (`limp_fall_direction`, URDF inertials);
   - the activation sequence and its German state machine (`activation_agent`);
   - the scene-camera profile statement;
   - the HELLO / tower split for cameras.

---

## 5. Q4 — Functional parity matrix

Status codes:
- **U** = carries unchanged (pure logic, UI or cloud).
- **R** = needs rebinding (transport, process or path changes, same behaviour).
- **D** = needs redesign (semantics change: board, Windows, a shared leader bus).
- **N** = OMX-only, intentionally retired with the OMX (D3).
- **L** = would be lost (owner decides).
- **dead** = no UI path today.

Dependency letters: ROS · Dk (Docker) · W (WSL) · OMX (OMX hardware) · Ld (separate leader arm) ·
G (GUI process).

| # | Capability (today's mechanism) | Depends on | V1-only, ROS-free | Note |
|---|---|---|---|---|
| **A. Bring-up and safety** |||||
| 1 | Arm scan + family/conflict checks (`identify_arm`, usbipd, `_ARM_MARKERS`) | Dk W G | D | becomes board HELLO_ACK; family checks collapse to one family |
| 2 | EEPROM fingerprint refusal at boot (driver `probe_bus`) | ROS Dk | D → firmware | the German causes stay on the PC |
| 3 | Activation gate, nothing moves at start (`activation_agent`, entrypoint, driver `boot_energise`) | ROS Dk | D | engine orchestrates home → **sync → teleop**, which is new for the V1 |
| 4 | Torque on/off with German refusal reasons (edge, wrong-way, bus fault) | ROS | D → firmware + engine texts | `_last_torque_reason` |
| 5 | Torque-off on SIGTERM / atexit (V1: none on a dead host) | ROS Dk | D (stronger: firmware watchdog, D7) | **D7 decides what "Stoppen" means** (row 47) |
| 6 | Edge refusal, post-energise abort, tick clamp, register 40 | — | D → firmware | — |
| 7 | Boot/activation home floor check + **tower check** (driver) | ROS | D → engine pre-check, board executes | must stay single-writer |
| 8 | Teleop collision e-stop + two-step recovery + CollisionModal | OMX Ld ROS | **N for OMX; D (new) for the V1** | Rule §2 sign-off; OMX constants (`GPIO_NAME_TO_JOINT`, `JOINT_TO_DXL_ID`, `SAFE_HOME_ARM`, `LEADER_JOINTS` with `gripper_joint_1`) make the module unusable for the V1 as is |
| 9 | Camera disambiguation (exit 3), device wait (exit 1) | Dk | R (engine enumeration, the `win_camera` USB-topology rules) | — |
| 10 | Xacro Dynamixel limits, current floors, trigger spring | OMX | N | the V1 leader trigger is new (§6.2) |
| **B. Startseite and global** |||||
| 11 | Heartbeat / "Verbunden" | ROS | R | — |
| 12 | Identity + capabilities (idle identity tick) | ROS | R | profile flags change for the V1 |
| 13 | Activation card | ROS Dk | R/D | row 3 |
| 14 | HF token card: cloud save, robot slot, encryption for remote pages, busy-queued clear, window-close clear | ROS Dk W | D | slot storage on Windows; `docker exec` clear → engine-internal |
| 15 | Hero twin (`/joint_states` interpolated on stamps) | ROS | R | stamp mapping |
| 16 | Hero camera, MJPEG | ROS | R | engine MJPEG passthrough |
| 17 | Health card (signal status, calibration status, cameras) | ROS | R | — |
| 18 | V1 placement hint (`tower_state`, hand-guide keepalive, `TowerHint`) | ROS | R | fall direction computed by the engine |
| 19 | Login gate, sign-out (incl. the robot token clear), boot scrub, version reload, legal links | — | U | — |
| 20 | CollisionModal (app-global) | ROS | R (V1 D, row 8) | — |
| **C. Aufnahme** |||||
| 21 | Record FSM (warm-up/run/save/reset/finish; next, re-record, skip, finish, stop; busy; queued end) | ROS (lanes) | U logic + R lanes | R1–R4 |
| 22 | Slot sampler, ClockMap, gap re-record, source stop, C5 warnings, frame-loss re-record | — | U + R (board stamps) | proof on Windows |
| 23 | Disk floors | — | U (Windows path) | — |
| 24 | Collision discard + seamless resume | OMX Ld | V1: D (new) | — |
| 25 | Error-stop finalize (D5); **shutdown finalize via SIGINT chain** | ROS s6 | U / **D** | R14 |
| 26 | D14/D7/D4 hub decisions, guarded single-commit upload, dataset card | — | U | — |
| 27 | D58 scene stamp + resume refusal | ROS (statement) | R | the provider becomes an engine setting |
| 28 | Signal status | ROS | R | — |
| 29 | Camera tiles (MJPEG), 3D view, **phase pictures** | ROS | R; pictures **D** | `robotAssets.phasePicture` is omx_full-only, so the V1 needs renders |
| 30 | Benutzer-ID list (whoami) | ROS | R | — |
| 31 | **Leader-required data gate** (the OMX recorder waits for leader rail messages; not activated → error after 5 s) | Ld | **D** | on a shared bus the leader q is always present → an explicit "teleop active" gate is needed |
| 32 | Leader joystick buttons (next / re-record) | OMX Ld | L (or a V1 leader-button feature) | — |
| 33 | rosbag2 recording | ROS | dead → L | forced off by `forceRecordTaskInfo` |
| 34 | Multi-task recording | — | U (unreachable from the page) | — |
| **D. Training** |||||
| 35 | Start / continue / cancel / hours / chart | cloud | U | — |
| 36 | Robot-side lists (`/training/get_user_list`, `get_dataset_list`, `get_available_policy` with cloud fallback, `get_model_weight_list`) | ROS | R (or drop) | — |
| 37 | MyModels "download to robot" (`/huggingface/control download`) | ROS | R — **but its consumer (local inference) is gone** | candidate removal |
| **E. Inferenz** |||||
| 38 | Cloud-Inferenz (reserve, arm, start/stop, schedule, ceilings, stale camera, state topic) | ROS | U logic + R publish path | setpoint stream (B6) |
| 39 | Cloud refusal / end on a live leader (D23) | Ld | **D** | must key on board teleop source |
| 40 | Window-close cloud stop (GUI `docker exec`) | Dk W G | R (engine-internal) | — |
| 41 | Local GPU inference (START_INFERENCE, `InferenceManager`) | ROS | dead → L | no UI sender; no GPU target |
| 42 | **Cross-robot model refusal** | — | **missing today** | §0 finding 10 |
| **F. Daten** |||||
| 43 | Library, player, edits, downloads, uploads, sync records, boot recovery | ROS (node side only) | U logic + **D** POSIX parts | `fcntl`, `killpg`, `nice`, `/run`; rename-over-open-file on Windows |
| 44 | `/daten-api` gateway rules | nginx | R | — |
| **G. Roboter Studio** |||||
| 45 | Blockly / Python / Java programs, real + sim; `WorkflowManager` | ROS (glue) | U + R (publisher → TRAJ, frames) | — |
| 46 | Debugger, live values, Python breakpoints | — | U | — |
| 47 | Code-runner sandbox, four conditions | Dk | D (AppContainer, owner sign-off) | other agent |
| 48 | Jog / home glide (+ V1 `home_glide_state`) / hand-guide / record / replay / capture / mark | ROS | U + R | torque/limp/hold via the board |
| 49 | Vormachen, hand mode | ROS | R | — |
| 50 | Vormachen, **leader mode** (D8) | Ld ROS | **D** | impossible on the V1 today (finding 7) |
| 51 | LeaderToggle (follower-only flip, `:8769`, container recreate) | Dk W G | **D** | board MODE; keep the JSON contract |
| 52 | Tower calibration (mat check, arm check as program, handheld, target removal, home step, state topic, start gate) | ROS | U + R | studio picture in-process |
| 53 | Tower guard; V1 driver tower check | ROS | U; D (engine pre-check) | — |
| 54 | Arrival trim | — | U | needs a fresh board stream |
| 55 | Simulator (SimArm/SimWorld, `/sim/*`, reset via `/workflow/stop`) | ROS | U + R | — |
| 56 | AprilTag perception, catalog | — | U | pupil-apriltags Windows wheel (UNVERIFIED-WINDOWS) |
| 57 | Sensor panel, variable/counter inspector | ROS | R | — |
| 58 | Mode arbiter, manual arbiter, motion-lock policy | — | U | explicit locks |
| 59 | Sammlung, Ziele/Positionen, cloud persistence, tutorials, submissions | cloud | U | — |
| **H. Cameras** |||||
| 60 | MSMF capture + JPEG + ingest + HELLO + **tower split** + drop warnings | G Dk ROS | R/D | in-process vs helper process (§10) |
| 61 | Phone camera (HTTPS 8444) | G | R | binding to 0.0.0.0 may trigger a firewall prompt without admin (UNVERIFIED-WINDOWS) |
| 62 | Scene-camera profile (one key, checkbox + probe) | G Dk | R | — |
| **I. Lifecycle** |||||
| 63 | Umgebung starten/stoppen, teardown order (cameras after containers, VA-07) | Dk W G | D | — |
| 64 | Degraded boot, bounded re-init, respawn | ROS s6 | D | new "arm not connected" states |
| 65 | Stale-session notice | ROS | R | — |
| 66 | Keepalive (WSL idle) | W | not needed | — |
| 67 | Update channel / forced update | G | D (other agent) | — |
| **J. Pi** |||||
| 68 | Wizard, login, claim, firewall, robot key, update | Dk | unchanged until Phase 7 (D11) | the engine must later run behind nginx's `auth_request` |
| **K. Cloud / teacher / admin** | | cloud | U | — |

**Dead today (list L)** — serve only on owner request:
- services `/browse_file`, `/dataset/edit`, `/dataset/get_info`, `/get_saved_policies`,
  `/get_policy_list`, `/training/command`, `/training/get_training_info`;
- the topic `/training/status`;
- the rosbag2 mode and the `rosbag_recorder` node;
- START_INFERENCE / local inference;
- the 7 calibration `.srv`;
- the OMX joystick.

---

## 6. Q5 — What the V1 needs to become recordable, trainable and inferable with a V1 leader

### 6.1 Server changes, file by file

| File / object | Change | Evidence |
|---|---|---|
| `robot_profiles._V1_STUDIO` | `recordable/editable/trainable/inferable = True`, `has_leader = True`, `follower_only = False`. `collision_enabled` must change *meaning* (board-side detection, engine FSM). `safe_home_arm_rad` is a real V1 recovery pose (today it mirrors HOME). `camera_roles` gains `gripper` (D9). `display_de`/`help_de` lose "Nur für Roboter Studio". | `robot_profiles.py` |
| `config/v1_studio_config.yaml` | Keep the joint order `joint1..5, RL_joint` (state names = action names, which the cloud preflight requires). The leader source moves from a topic to the board stream. `camera_topic_list` adds the wrist camera. | `dataset_preflight` ("state_names != action_names") |
| `Communicator` | Follower and leader histories fed by the board client, both with the board stamp. `FOLLOWER_JOINT_ORDER` from the profile is already in place. The leader source must not be the command rail. | `_leader_callback_body` (stamp 0), `init_subscribers` |
| `data_manager` / `data_converter` | A message-free conversion (dataclasses), the `isinstance` dispatch replaced, a cv_bridge-free decode with a parity proof. | §1 B4 |
| `collision_monitor` | Split detection from recovery. V1 constants: names (`RL_joint`), safe home, thresholds (`EDUBOTICS_COLLISION_EFFORT_J*` were tuned for XL430/XL330), overload semantics (STS, not `reboot_dxl`). Without this, `_publish_hold_current_pose` skips forever on the V1 (`LEADER_JOINTS` names `gripper_joint_1`). | `collision_monitor.LEADER_JOINTS`, `GPIO_NAME_TO_JOINT`, `JOINT_TO_DXL_ID`, `SAFE_HOME_ARM` |
| `leader_appears_active` and its 5 call sites | Re-key on "board command source == teleop" (exact). Today it is "/leader/joint_states fresh", and that subscription does not exist on the V1. | `collision_monitor._init_collision_monitor` early return; `physical_ai_server.py` (`_cloud_leader_active`, `_manual_leader_blocks`, `start_leader`, `_launch_workflow` HIGH-6) |
| Activation (`activation_agent` → engine) | V1 branch: home → **sync to the leader** → teleop (on the board), with the same German states. | `activation_agent.FEETECH_ROBOT_TYPES` stops after home today |
| LeaderToggle / `:8769` | follower-only = board MODE teleop off (instant); the two-container readiness gate (`PREP_*`) disappears. | `LeaderToggle.jsx` |
| Inference publish path | `publish_action` → board setpoint (one point, t = 1.5/fps). | `tensor_array2joint_msgs` |
| Capability manifest | `cloud_inference` follows `inferable`. | `capabilities_json` |
| Dataset naming | `data_robot_type 'v1_studio'` → repo `{user}/v1_studio_{task}`. It is also the config key, the Contract-B tag (`TRAJECTORY_ROBOT_PROFILES`, migration 051) and Daten's `other_robot` filter. **Keep it** (recommended; renaming touches a CHECK migration and React `DESTINATION_ROBOT_TYPES`), or rename (owner Q-E3). | `validators/workflow.py`, `daten/hub_reads.refusal_of` |
| Daten | Unchanged; the default `robot_type='omx_f'` fallbacks (`daten/http_server._rig_robot_type`) should follow the V1. | — |

### 6.2 Semantic redesigns the code forces (owner decisions)

1. **Leader liveness.** On one bus the leader is always powered, so every gate must ask "is teleop
   driving the follower?" ("Is the leader arm on?" is no longer meaningful.) Workflow, manual and
   inference *refuse* while teleop is on (today's rule), or switch it off themselves (owner Q-E1).
2. **The recorded action.** OMX action = the broadcaster's command = the follower goal. On the board:
   the raw leader q, or the goal actually written after clamps and the claw mapping. Recommended: the
   **written goal**, because it is what inference will command. The STATE frame must carry it.
3. **Recording gate.** Refuse Start unless the board reports teleop active. The OMX got this from the
   5 s data gate; on a shared bus the leader data always flows.
4. **The rotating claw as a leader input.**
   - The V1 claw is two geared blades, `RL_joint` 0 = closed … 1.5708 open, commanded 0..0.90, and
     `LF_joint` mimics at −18/19 (`robot_profiles`, URDF).
   - Squeezing a leader copy's two small blades by hand is awkward.
   - The OMX solution is a current-limited position "spring" trigger (−0.7 rad at 300 mA). On STS that
     maps to a low `Torque_Limit` position hold, feasible on the board.
   - The mapping leader → follower claw and its clamp define the action range.
   - **Thermal risk:** the follower claw stalls on an object for a whole carry (Max_Torque 150 on the
     claw, VR4/VR5). **UNVERIFIED-BENCH:** claw temperature over a 120 s held grasp.
5. **Inference timing.** Today: a per-tick setpoint stream (parity with how actions were recorded,
   one per frame). A board chunk-buffer (send the planned actions with timestamps) would remove PC
   jitter but changes when actions apply relative to observations. Recommended: keep the per-tick
   stream first, measure, then decide (Q-E5).
6. **Cross-robot models.** Add a refusal: the training dataset's `robot_type` / joint names must equal
   the robot's. It belongs in the reserve route (the API reads the dataset info) and in the robot's
   preflight. It is a refusal, like the camera contract, not an envelope (Rule §2 compatible; owner
   Q-E4).

### 6.3 Cloud side: no OMX-only assumption found

- Generic: 1–4 cameras, ≤ 32 state/action dims, state names = action names, ACT only
  (`artifact_validation.MAX_*`, `inference_sessions.summary_from_config`,
  `dataset_preflight._get_joint_names`, `cloud_protocol.MAX_*`).
- Trajectory tags already accept `v1_studio` (`TRAJECTORY_ROBOT_PROFILES`, migration 051).
- The training handler is robot-agnostic.
- The only gap is the cross-robot refusal (6.2 item 6). **VERIFIED by reading.**

### 6.4 React touches, for completeness

- Phase pictures and a leader glyph for the V1 (`icons/robotAssets.js`, `GLYPH_ORDER`).
- LeaderToggle's meaning.
- The CollisionModal is generic over phases.
- About 18 React files name OMX ids.

---

## 7. Q6 — Engine design options, judged from the code

**(a) Keep rclpy, ROS 2 natively on Windows (RoboStack / pixi). Rejected.**
- The `robostack-jazzy` win-64 index (fetched 2026-11-24) has `ros-jazzy-rclpy` 7.1.13, rosbridge
  2.7.1, cv_bridge 4.1.0, rosbag2 0.26.11, three RMWs, ros2_control 4.50 — but **no
  `web_video_server`** and no usb_cam. **VERIFIED** (`scratchpad/agent1/robostack/win64_repodata.json`).
- `ros2-rclpy` builds exist only for **cp312 and cp314**. Torch 2.7.0 Windows wheels exist only for
  **cp39–cp313** (PyPI JSON). LeRobot 0.5.1 needs `torch>=2.7,<2.11`, and the monotonic clock needs
  CPython ≥ 3.13. **VERIFIED.** Option (a) therefore forces either cp312 plus a codebase-wide
  `monotonic` → `perf_counter` change, or cp314 plus a torch ≥ 2.9 move (Rule §5 lockstep, the
  training-lock surface).
- `physical_ai_interfaces` needs a rosidl build with MSVC in CI and a conda package.
- It brings DDS on Windows: loopback discovery, firewall prompts, long-path risk (UNVERIFIED-WINDOWS),
  and about 1 GB of environment.
- It keeps every rclpy liability: the executor-starvation class, crash-on-uncaught-exception, QoS
  mismatch. Its only gain is "no code change", and §3.4 shows the code change is small.

**(b) ROS-free core serving the rosbridge-v2 protocol (React unchanged). Recommended, carried over in
two steps (§10).**
- The subset is small and proven against the real roslib (§2.8).
- Message (de)serialization comes from the existing IDL:
  - The grammar actually used: primitives, unbounded arrays, nested package types, one
    `std_msgs/Header`, constants, one default (`bool private_mode true`). No bounded or fixed arrays.
  - `scratchpad/agent1/idl/idl_gen.py` parses all 50 files plus 9 standard types; `test_idl.py`
    proves the defaults, the constants, the hostile refusals, base64 and `{}` construction of all 90
    project types. **VERIFIED.**
  - The generated classes can keep the ROS module paths (`physical_ai_interfaces.msg.TaskStatus`), so
    the `isinstance` dispatch and the `getattr(TaskStatus, 'X', n)` fallbacks keep working.
  - `ci.yml::enum-parity` (`.github/scripts/enum_parity.py`) reads the `.msg`/`.srv` **text** and the
    JS constants, so it holds unchanged. The same is true for `test_workshop_manual_srv.py` (field
    order), `test_cloud_inference_lockstep.py` and the other lockstep tests.
  - `interfaces-validate` (CMakeLists ↔ files) becomes "generator inputs ↔ files".
- **Security gains by construction:** no client publish or advertise (closes the forged-topic class,
  VB-03); strict frame validation; a page token (the WebView2 URL → a cookie on the same-origin WS
  handshake); and Origin/Host checks in the engine.

**(c) A typed API plus a roslib-compatible shim in React. Deferred.**
- It changes `rosConnectionManager` and 19 roslib importers, or aliases `roslib` at build time.
- It buys typed binary frames and auth, but doubles the migration risk while the engine itself is new.
- The generated contract lets TypeScript types come from the same IDL later.

**(d) A rewrite in another language. Rejected.**
- About 61 k lines of Python are bound to Python-only dependencies: LeRobot (the dataset writer, the
  frame-loss warning seam, the public metadata API), PyAV/pyarrow (Daten), NumPy/OpenCV/pupil (the
  tower guard and perception), and the Python code runner.
- Rule §5's lockstep and the recorder's reliance on LeRobot's public surface make a non-Python core a
  re-validation of everything.

---

## 8. Q7 — The test surface and the proof strategy

| Suite | Today | Under a ROS-free engine |
|---|---|---|
| Server `test/`: 182 files, 3 023 `def test_` (4 176 incl. parametrization, CLAUDE.md snapshot) | CI runs it **without ROS** (Python 3.12 venv); only `test_cloud_decode_parity.py` needs the image (cv_bridge) | ~139 module-level files guard as they are. **43 files / 964 test functions run node methods by AST** (`_load_method` + exec on stubs); 41 of them passed here without ROS (§0). They re-target to the new file layout (same method names → near-mechanical) |
| `robotis_ai_setup/tests/` (169 files) | 16 read node, collision or communicator source; many are GUI/WSL/installer | the 16 node-reading ones migrate; GUI/WSL tests retire with WSL |
| `record_fsm_smoke.py` | DataManager directly + `physical_ai_interfaces.msg.TaskInfo`; **two shutdown scenarios under real rclpy** | TaskInfo from the generated module; the shutdown scenarios rewritten to the engine's shutdown protocol; **run on a Windows runner** |
| `daten_smoke.py` | ROS-free, needs LeRobot | run on Windows; proves the POSIX ports |
| `code_e2e_smoke.py` | two containers + `test/rig_emulation.py` | native runner (sandbox agent) |
| React vitest (349 files) | mocks roslib | unchanged |
| `test_cloud_decode_parity.py` | cv_bridge rgb8 = chunk_engine decode | becomes "engine decode = cv_bridge rgb8" on the Linux image (the reference) |

**New proof a rewrite of the node needs:**

1. **Wire inventory lockstep.** A test derives every topic, service and type React references from
   `src/` (the `test_pi_cloud_cors_lockstep.py` pattern) and asserts it equals what the engine serves,
   plus an allowlist. A served name with no caller or a caller with no served name fails.
2. **Lane table.** Generated today by AST from `create_service/create_timer/create_subscription`
   calls and their groups (§3.2), and asserted equal to the engine's registration table. A behavioural
   test per lane class: two default-lane handlers never overlap; the heartbeat keeps < 100 ms jitter
   while every pool thread blocks 5 s; record ticks never overlap; trip and watchdog never overlap.
3. **Differential runtime test.** The *same* scenario scripts (timer + service storms with recorded
   start/end per callback) run under real rclpy in today's Linux image and under the engine runtime;
   the per-group overlap and ordering traces are compared.
4. **Characterization transcripts.** Record normalized rosbridge transcripts against today's stack, with
   no hardware: sim runs, `rig_emulation`, synthetic camera/joint publishers, a fake cloud server.
   Normalized means volatile fields masked, timing in classes. Scenarios: idle connect, record happy
   path and error stops, busy/queued FINISH, workflow sim with breakpoints, manual jog / hand-guide /
   record / replay, calibration flows, HF token set/clear/busy/encrypted/replayed, cloud inference
   ready/hold/stall/end, Daten commands, sim reset. Replay the same scripts against the engine plus the
   board simulator and diff.
5. **No silent test loss.** A CI rule: the server test count never decreases, and each removed test is
   named with its replacement.
6. **Invariant registry.** Each R1–R19 row and each CLAUDE.md safety invariant maps to ≥ 1 test id; CI
   asserts the mapping.
7. **Windows gates (UNVERIFIED-WINDOWS):**
   - record/daten/code smokes;
   - shutdown finalize without SIGINT;
   - timer jitter with and without `timeBeginPeriod(1)`;
   - **GIL contention** (board reader + cameras + record tick + WS) on a slow laptop;
   - the port-80 / loopback bind as a standard user;
   - the firewall prompt behaviour.
8. **Board simulator.** A Python model of the firmware protocol for CI (stamps, stalls, refusals),
   plus bench HIL (UNVERIFIED-BENCH).
9. **React E2E.** Playwright against the engine with the unchanged build: connect, twin, activation,
   sim program, recording with synthetic sources, Daten library.

---

## 9. Risks, ranked

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| K1 | **An implicit runtime invariant (R1–R19) silently disappears** (e.g. the collision FSM racing `/task/command` → a stuck CollisionModal; samplers overlapping a workflow start) | High | step-1 compat runtime with a differential test vs rclpy; lane table test; characterization transcripts; invariant registry |
| K2 | **Leader gates on a shared bus** (finding 7) left keyed on "leader fresh" → inference/Studio permanently refused, or worse, unguarded | High | re-key on board command source; one predicate, tested at every call site |
| K3 | **Action semantics** recorded wrong (raw leader vs goal) → trained models behave differently at inference | High | owner decision + the STATE frame carrying the goal + an A/B on one task |
| K4 | **GIL contention** on a slow Windows laptop: board reader, MSMF capture (1080p tower decode), record tick, LeRobot encoder threads, WS JSON | Medium-High | keep camera capture in a helper process (today's TCP 5557 + HELLO wire is reusable); measure; board stamps make arrival jitter harmless except for `SOURCE_STOPPED_S` |
| K5 | Cross-robot model accepted on the V1 (finding 10) | Medium | add the refusal (Q-E4) |
| K6 | rosbridge emulation differs in a behaviour React depends on (throttle/queue merge, latched order, concurrent calls) | Medium | port `subscription_modifiers` logic; transcripts; React E2E |
| K7 | Shutdown finalize without SIGINT (logoff, kill) loses saved episodes | Medium | an explicit shutdown command; WM_ENDSESSION; a bounded finalize; a recovery marker (exists) |
| K8 | Windows file and process semantics in Daten (`fcntl`, `killpg`, rename over open files) | Medium | port + `daten_smoke` on Windows |
| K9 | numpy 1.26.4 → 2.x when cv_bridge goes (behaviour change in stats or decode) | Low-Medium | `record_fsm_smoke`, `daten_smoke`, decode parity against cv_bridge in the Linux image |
| K10 | Timer jitter on Windows breaks the on-time rule (borderline 48.9 ms vs 50 ms) | Low-Medium | `timeBeginPeriod(1)`/high-resolution timers; a jitter test |
| K11 | Carrying dead surface (list L) into the new engine | Low | owner prunes once (Q8) |

---

## 10. Recommended design for this area, and its proof obligations

**Shape.** One ROS-free Python ≥ 3.13 engine process. It keeps today's threaded concurrency model and
puts asyncio only at the edge. Helper processes stay as today: HF worker, download worker, edit worker,
Daten sidecar, code runner, plus a **camera helper process** that reuses `camera_bridge` and the
5557/HELLO wire, so the 1080p tower split stays off the engine's GIL.

**Step 1 — "same code, new runtime"** (Phase 4):
- `edubotics_runtime`: about 20 rclpy-shaped primitives with faithful semantics (Node-like components;
  MutuallyExclusive/Reentrant groups mapped to serial lanes and bounded pools; Multi/Single-threaded
  executors; periodic timers with rclpy semantics; guard condition; publishers with an in-process bus
  and `latched` replay; services; client futures; clock; logger).
- `edubotics_msgs`: generated from `physical_ai_interfaces/*.msg|*.srv` plus 9 standard types, same
  module paths and constants.
- Today's node, communicator, collision monitor, timer manager, Daten and studio camera then run with
  import-level changes only.
- **The board client is a "virtual arm container":** it satisfies exactly the ROS contract
  `feetech_arm_node.py`'s docstring lists (`/joint_states`, `/leader/joint_trajectory` subscriber,
  `/v1/set_torque` + alias, `/v1/home` + `/edubotics/home_arm`, `/edubotics/tower_state`), plus
  `activation_agent`'s (`/edubotics/activate`, `activation_state`, `scene_camera_profile`) and the
  ingest node's (camera topics, studio picture, camera state). The rest of the code is untouched.
- **The rosbridge-subset adapter** is a component on the same bus (§2.8 semantics, publish refused,
  frames validated, page token), and the HTTP edge implements nginx's rules, `/video/stream`, the
  `/daten-api/` proxy and the `:8769` contract.

**Step 2 — "honest names"** (later phases): replace the compat API with explicit lanes and a bus,
module by module, under the same transcripts. Delete list L on owner approval.

**V1-specific redesigns** (Phase 3/5, owner sign-offs): leader-source and teleop-source gates (§6.2);
the action definition; the collision split (board detection, engine FSM); the claw trigger; the
cross-robot refusal.

**Proof obligations** (each a named test or gate):
1. the wire inventory lockstep;
2. the lane table equality + behaviour tests;
3. rclpy differential traces;
4. characterization transcripts (green against both today's stack and the engine);
5. the invariant registry complete;
6. AST-node tests re-targeted, with the count not decreasing;
7. Windows smokes (record, daten, code), shutdown finalize, timer jitter, GIL load;
8. board-sim CI + bench HIL (stamps, stalls, refusals, teleop gate);
9. React E2E against the unchanged build;
10. decode parity vs cv_bridge;
11. the cross-robot refusal test.

---

## 11. Decision questions for the owner

| Q | Decision | Options | Recommendation | Evidence |
|---|---|---|---|---|
| Q-E1 | When teleop is on and the student starts inference / a Studio program / a manual op | (a) refuse, as today ("in den Follower-Modus wechseln"); (b) the engine switches teleop off itself | (a) first: unchanged semantics, and switching becomes one instant click | `_manual_leader_blocks`, `_cloud_leader_active`, HIGH-6 |
| Q-E2 | The recorded action | (a) raw leader q; (b) the follower goal actually written (after the claw mapping and clamps) | (b): it is what inference commands, and today's OMX action is also the commanded goal | `_leader_callback_body`, `tensor_array2joint_msgs` |
| Q-E3 | V1 dataset robot type | (a) keep `v1_studio` (dataset `…/v1_studio_<task>`); (b) rename (`v1`) with a migration of 051's CHECK, the API table and React | (a): cosmetic cost only; renaming touches four layers | `validators/workflow.py`, migration 051 |
| Q-E4 | Refuse models trained on another robot | (a) a refusal in the reserve route + the robot preflight on robot_type / joint names; (b) rely on dims/cameras (today) | (a) | finding 10 |
| Q-E5 | Inference command timing | (a) a per-tick setpoint stream (today); (b) board-buffered timed chunks | (a) first, then measure (b) on the bench | `_cloud_inference_tick_body` |
| Q-E6 | Engine transport to React | (a) the rosbridge-subset adapter, React unchanged (B-D8 "emulation first"); (b) a typed API now | (a), with the generated contract so (b) can follow | §2.8 prototype |
| Q-E7 | Port strategy | (a) a compat runtime first + differential tests, then explicit API; (b) a direct port | (a): separates the runtime change from the code change | §3.3 |
| Q8 | Dead surface (list L) | keep / drop each | drop all except, possibly, the robot-side training lists (check the teacher workflow) and MyModels' download (only if a local use returns) | §5 |
| Q9 | Camera capture placement | (a) in the engine process; (b) a camera helper process (today's wire) | (b) until a Windows GIL measurement proves (a) safe | §9 K4 |
| Q10 | HF token slot on Windows | (a) a per-user ACL'd file wiped at sign-out, close and boot; (b) memory only, passing the token into every child | (a): keeps "no consumer passes a token"; disclose that a crash leaves it until the next start | §1 B10 |

Sources (external):
- [rosbridge_server changelog (2.2.0: services in new threads by default)](https://raw.githubusercontent.com/RobotWebTools/rosbridge_suite/ros2/rosbridge_server/CHANGELOG.rst)
- [rosbridge subscribe capability](https://raw.githubusercontent.com/RobotWebTools/rosbridge_suite/ros2/rosbridge_library/src/rosbridge_library/capabilities/subscribe.py)
- [rosbridge subscription modifiers](https://raw.githubusercontent.com/RobotWebTools/rosbridge_suite/ros2/rosbridge_library/src/rosbridge_library/internal/subscription_modifiers.py)
- [RoboStack getting started](https://robostack.github.io/GettingStarted.html)
- [robostack-jazzy win-64 repodata](https://conda.anaconda.org/robostack-jazzy/win-64/repodata.json)
- [PyPI torch 2.7.0](https://pypi.org/pypi/torch/2.7.0/json)
- [PyPI lerobot 0.5.1](https://pypi.org/pypi/lerobot/0.5.1/json)
- [CPython gh-88494 (QPC monotonic, 3.13)](https://github.com/python/cpython/pull/116781)
- [bpo-33408 AF_UNIX on Windows](https://bugs.python.org/issue33408)
- [caniuse Web Serial](https://caniuse.com/web-serial)
- [Web Bluetooth implementation status](https://raw.githubusercontent.com/whatwg/bluetooth/main/implementation-status.md)
