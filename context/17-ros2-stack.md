# 17 — ROS2 Robot Stack

> **Layer:** Robot software (recording + inference) running inside the `physical_ai_server` and `open_manipulator` containers
> **Location:** `Testre/open_manipulator/`, `Testre/physical_ai_tools/physical_ai_server/`, `Testre/physical_ai_tools/physical_ai_interfaces/`, `Testre/physical_ai_tools/physical_ai_bt/`
> **Owner:** ROBOTIS upstream (absorbed) + our overlays in `robotis_ai_setup/docker/`
> **Read this before:** editing launch files, xacros, controller configs, behavior trees, recording/inference logic. Always read [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md) before touching any overlay.

---

## 1. Package inventory

| Package | Location | Role |
|---|---|---|
| `open_manipulator` | `open_manipulator/open_manipulator/` | Top-level meta package |
| `open_manipulator_bringup` | `open_manipulator/open_manipulator_bringup/` | Launch files + controller config |
| `open_manipulator_description` | `open_manipulator/open_manipulator_description/` | URDF + xacro + Gazebo plugins |
| `physical_ai_server` | `physical_ai_tools/physical_ai_server/` | Main ROS2 node: recording + inference + training trigger |
| `physical_ai_interfaces` | `physical_ai_tools/physical_ai_interfaces/` | Custom .msg + .srv definitions |
| `physical_ai_bt` | `physical_ai_tools/physical_ai_bt/` | Behavior trees (move_arms, move_head, etc.) |

**Skip:** `physical_ai_tools/lerobot/` is a byte-identical upstream snapshot — don't audit, don't modify.

---

## 2. Two-arm hardware overview

| | OMX-F follower | OMX-L leader |
|---|---|---|
| Servo IDs | 11–16 | 1–6 |
| Namespace | global | `/leader/` (PushRosNamespace) |
| Control | position; gripper current-controlled (Op Mode 5, **350 mA**) | joints 1–5 read-only + gravity comp; gripper current-controlled (Op Mode 5, **300 mA**, reversed) |
| Launch file | `omx_f_follower_ai.launch.py` | `omx_l_leader_ai.launch.py` |
| ros2_control xacro | `omx_f.ros2_control.xacro` | `omx_l.ros2_control.xacro` |
| Hardware plugin | `dynamixel_hardware_interface/DynamixelHardware`, 1 Mbps Protocol 2.0, 100 Hz update |
| Default device | `/dev/ttyACM0` (overridden by `FOLLOWER_PORT` env) | `/dev/ttyACM2` (overridden by `LEADER_PORT`) |
| USB VID/PID | `2F5D:0103` (OpenRB-150) or `2F5D:2202` (alt fw) | same |

---

## 3. Launch topology

### `omx_f_follower_ai.launch.py` — the follower

Critical line 144:
```python
remappings=[('/arm_controller/joint_trajectory', '/leader/joint_trajectory')]
```

**This is the single most important line in the system.** It remaps the follower's `JointTrajectoryController` input topic so anyone publishing to `/leader/joint_trajectory` drives the follower. Three sources publish there:
1. Leader's `joint_trajectory_command_broadcaster` (teleoperation)
2. `entrypoint_omx.sh`'s startup quintic-sync trajectory
3. Inference node's predicted actions

### Nodes launched

- `ros2_control_node` (Dynamixel hardware interface, follower IDs 11-16)
- `controller_manager/spawner` for `arm_controller` (JointTrajectoryController) + `joint_state_broadcaster`
- `robot_state_publisher` (TF tree)
- `joint_trajectory_executor` (initial position warmup)
- `rviz2` (optional)
- Event handlers via `OnProcessExit` for sequencing

### `omx_l_leader_ai.launch.py` — the leader

`GroupAction + PushRosNamespace('leader')` wraps everything. Nodes:
- `ros2_control_node` (leader IDs 1-6, joints 1-5 use effort/Goal Current command interface for gravity compensation)
- `controller_manager/spawner` for `gravity_compensation_controller` (spawned first), `joint_state_broadcaster`, `trigger_position_controller`, `joint_trajectory_command_broadcaster` (publishes leader's observed positions to `/leader/joint_trajectory`)
- `robot_state_publisher` (TF prefix `leader_`)
- `position_command_process` (50 Hz × 50 iterations, sends initial trigger to gripper)

### `omx_ai.launch.py` — orchestration

Launches both arms via event handlers:
1. Start follower
2. On follower start → run `joint_trajectory_executor`
3. On executor exit → start leader

---

## 4. Controller configs

### Follower: `omx_f_follower_ai/hardware_controller_manager.yaml`

```yaml
controller_manager:
  ros__parameters:
    update_rate: 100  # Hz

arm_controller:
  ros__parameters:
    type: joint_trajectory_controller/JointTrajectoryController
    joints: [joint1, joint2, joint3, joint4, joint5, gripper_joint_1]
    command_interfaces: [position]
    state_interfaces: [position, velocity]
    allow_partial_goals: true
    goal_time: 1.0  # tolerance
    constraints:
      joint1: { trajectory: 0.15, goal: 0.05 }
      ...
      gripper_joint_1: { trajectory: 0.1, goal: 0.05 }
```

### Leader: `omx_l_leader_ai/hardware_controller_manager.yaml`

- `gravity_compensation_controller` for joints 1-5 (effort command interface → Dynamixel Goal Current via `dynamixel_hardware_interface` plugin, KDL RNE computes per-joint torques):
  - kinetic friction scalars: `[0.0005, 0.15, 0.15, 0.15, 0.15]`
  - torque scaling: `[200, 350, 300, 300, 300]`
- `trigger_position_controller` for gripper_joint_1 only
- `joint_trajectory_command_broadcaster` for all 6 joints, **reverse_joints: [gripper_joint_1]** (gripper sign inversion), publishes to `/leader/joint_trajectory`

---

## 5. xacro (URDF)

### `omx_f.ros2_control.xacro` — follower hardware definition

- Hardware plugin chain: Gazebo → Mock → DynamixelHardware (real)
- `is_async="true"` (added in session 2026-04-06; eliminated 40+ SYNC_READ_FAIL errors at 100 Hz)
- Baud: 1 Mbps, error timeout 500 ms, `disable_torque_at_init: true`
- 6 joints: position command + position/velocity state
- gripper_joint_2 in simulation only (mimic joint)
- GPIO definitions per servo: ID, Op Mode, P/I/D gains, Profile Velocity/Acceleration, position limits
- **Gripper (ID 16):** Op Mode 5 (current-based position), **Current Limit 350 mA** (aligned with leader's 300 mA), Goal Current 350 mA, Shutdown 21 (E-stop on overload)

### `omx_l.ros2_control.xacro` — leader

Same plugin chain. Joints 1-5: `effort` command interface; dxl1-5 GPIOs declare `Goal Current`, Operating Mode 0 (Current Control). Gripper (ID 6): Op Mode 5, **Current Limit 300 mA** (lower than follower for safety), P=1000, D=1500 (higher D for smoother manual operation). Leader file shipped via overlay (`docker/open_manipulator/overlays/omx_l.ros2_control.xacro`).

---

## 6. entrypoint_omx.sh — PID 1 of the open_manipulator container

`docker/open_manipulator/entrypoint_omx.sh` (~270 lines). Five phases:

### Phase 0: setup (lines 1-30)

```bash
disable_torque() {
    timeout 2 ros2 service call /dynamixel_hardware_interface/set_dxl_torque ...
    timeout 2 ros2 service call /leader/dynamixel_hardware_interface/set_dxl_torque ...
}
trap "disable_torque; kill_children" SIGTERM SIGINT
source /opt/ros/jazzy/setup.bash
source /root/ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-30}
```

### Phase 1: validate hardware (lines 40-60)

```bash
wait_for_device() {  # loop up to 60 s waiting for device file
    local dev=$1 max=60
    for ((i=0; i<max; i++)); do
        [ -e "$dev" ] && return 0
        sleep 1
    done
    return 1
}
wait_for_device "$FOLLOWER_PORT" || { echo "FOLLOWER_PORT not found"; exit 1; }
wait_for_device "$LEADER_PORT"   || { echo "LEADER_PORT not found"; exit 1; }
chmod 666 "$FOLLOWER_PORT" "$LEADER_PORT"
```

### Phase 2: launch leader FIRST (lines 62-76)

```bash
ros2 launch open_manipulator_bringup omx_l_leader_ai.launch.py port_name:=$LEADER_PORT &
LEADER_PID=$!
wait_for_topic /leader/joint_states 30 || exit 1
sleep 2  # leader stabilization
```

### Phase 3: read leader position (lines 78-109)

Inline Python subscribes to `/leader/joint_states`, reads first complete message containing all 6 joints (joint1-5 + gripper_joint_1), JSON-encodes positions to a shell variable.

### Phase 4: launch follower + smooth quintic sync (lines 111-244)

```bash
ros2 launch open_manipulator_bringup omx_f_follower_ai.launch.py port_name:=$FOLLOWER_PORT &
FOLLOWER_PID=$!
wait_for_topic /joint_states 60 || exit 1
sleep 3  # arm_controller activation
```

If `LEADER_POS` is valid, publish a **quintic polynomial trajectory** over 3 seconds (50 waypoints) to `/leader/joint_trajectory`:

```
s(t) = 10t³ − 15t⁴ + 6t⁵    # smoothing function
positions(t) = current + s(t) * (leader_target - current)
```

Explicit velocities + accelerations populated. After motion: 0.5 s settle, then **verify follower reached target within 0.08 rad tolerance per joint**. Hard fail (exit 2) on mismatch.

### Phase 5: launch cameras (lines 246-263)

For i in 1, 2:
- if `CAMERA_DEVICE_$i` exists:
  - `ros2 launch open_manipulator_bringup camera_usb_cam.launch.py name:=$CAMERA_NAME_$i video_device:=$CAMERA_DEVICE_$i &`
  - else: warn + skip

Topics: `/$CAMERA_NAME/image_raw/compressed`.

### Final: `wait` (line 269)

Blocks until any background process exits → entrypoint exits with that process's return code.

---

## 7. Custom messages + services (`physical_ai_interfaces`)

### Messages

#### `TaskInfo`
```
string task_name, task_type, user_id
string[] task_instruction              # multi-instruction support
string policy_path
uint8 fps
string[] tags
uint16 warmup_time_s, episode_time_s, reset_time_s
uint16 num_episodes
bool push_to_hub, private_mode, use_optimized_save_mode, record_inference_mode, record_rosbag2
```

#### `TaskStatus`
```
TaskInfo task_info
string robot_type
uint8 phase                            # 0:READY 1:WARMING_UP 2:RESETTING 3:RECORDING 4:SAVING 5:STOPPED 6:INFERENCING
uint16 total_time, proceed_time
uint16 current_episode_number, current_scenario_number
string current_task_instruction
float32 encoding_progress              # %
float32 used_storage_size, total_storage_size  # GB
float32 used_cpu, total_ram_size       # %, GB
string error                           # German for student-facing errors
```

#### `TrainingInfo`
```
string dataset, policy_type, output_folder_name, policy_device
uint32 seed, num_workers, batch_size, steps, eval_freq, log_freq, save_freq
```

#### `TrainingStatus`
```
TrainingInfo training_info
uint32 current_step
float32 current_loss
bool is_training
string error
```

#### `HFOperationStatus`
HuggingFace upload/download progress.

### Services

#### `SendCommand.srv`
```
# Request:
uint8 IDLE=0, START_RECORD=1, START_INFERENCE=2, STOP=3,
      MOVE_TO_NEXT=4, RERECORD=5, FINISH=6, SKIP_TASK=7
uint8 command
TaskInfo task_info
---
# Response:
bool success
string message
```

#### `SendTrainingCommand.srv`
- START / FINISH commands
- Optional resume + resume_model_path

#### `GetPolicyList.srv`, `GetSavedPolicyList.srv`, `GetDatasetList.srv`, `GetModelWeightList.srv`, `GetAvailableImageList.srv`
Listing services: return `string[]` of file paths.

#### `GetRobotTypeList.srv`, `SetRobotType.srv`
Robot config selection (auto-discovered from `physical_ai_server/config/*.yaml`).

#### `BrowseFile.srv`
Filesystem tree browser (used by FileBrowserModal in React).

#### `EditDataset.srv`, `GetDatasetInfo.srv`
Dataset edit operations (merge, delete episodes).

---

## 8. physical_ai_server modules

### `physical_ai_server.py` (main ROS2 node)

State (lines 79-106):
- `params` — robot config dict from YAML
- `on_recording`, `on_inference` — state flags
- `is_training`, `training_thread`, `training_status_timer`

Components (lines 108-120):
- `communicator: Communicator`
- `data_manager: DataManager`
- `timer_manager: TimerManager`
- `inference_manager: InferenceManager`
- `training_manager: TrainingManager`
- `hf_api_worker: HfApiWorker`

Publishers:
- `training_status_publisher` → `/training/status` (TrainingStatus, buffer 100)

Services (15 total):
- `/task/command` (SendCommand) → `user_interaction_callback`
- `/get_robot_types`, `/set_robot_type`
- `/register_hf_user`, `/get_registered_hf_user`
- `/get_policy_list`, `/get_saved_policies`
- `/training/command` (SendTrainingCommand) → `user_training_interaction_callback`
- `/training/get_*` (info queries)
- `/huggingface/control`, `/browse_file`, `/dataset/edit`, `/dataset/get_info`

### `Communicator` (`physical_ai_server/communication/communicator.py`)

Sources: CAMERA, FOLLOWER, LEADER. Modes:
- `MODE_COLLECTION` — subscribe to all three (recording)
- `MODE_INFERENCE` — camera + follower only (no leader)

Subscribers (with QoS BEST_EFFORT for streams, RELIABLE for data):
- Cameras (CompressedImage) → `_camera_callback`
- Follower (JointState) → `_follower_callback`
- Leader (JointTrajectory) → `_leader_callback`
- Joystick trigger (String) for manual skip/rerecord

Publishers:
- Action publishers per leader topic (JointTrajectory)
- Rosbag2 service control

### `DataManager` (`physical_ai_server/data_processing/data_manager.py` — overlay)

State machine: `warmup → run → save → reset → (loop) → finish`. Each phase time-gated. `TaskStatus` published per tick.

Frame recording (per 30 Hz tick):
1. `communicator.get_latest_data()` (5 s timeout per topic)
2. `convert_msgs_to_raw_datas()`:
   - Images: cv_bridge → BGR → BGR2RGB → uint8 HWC
   - Follower: JointState → reordered float32[6] (overlay raises German error on missing joint)
   - Leader: `points[0].positions` → reordered float32[6] (overlay raises German error on empty trajectory)
3. `create_frame()` assembles dict, dtype-casts to float32
4. `add_frame_without_write_image()` (LeRobotDatasetWrapper) appends to episode buffer
5. Video encoding: piped to `ffmpeg libx264 -crf 28 -pix_fmt yuv420p` (async)
6. Save: parquet + mp4 + meta/info.json (`codebase_version: "v2.1"`)

Overlay-added behaviors:
- Lines 143-160: RAM check &lt; 2 GB → force early save, set `_early_saved_due_to_ram` flag, German warning
- Lines 289-333: `_verify_saved_video_files()` — sha256 + non-zero size; catches silent encoder failures
- Lines 335-403: `_validate_episode_buffer()` — RAM warnings + timestamp gap detection (&gt;2× expected interval)
- Lines 676-702: camera-name resume check — exact-match against existing `meta/info.json`, German `[FEHLER]` if mismatch
- Lines 741-795: `_upload_dataset` with 1-hour timeout in daemon thread
- Lines 824-865, 868-914: HF user/token queries with 1.5 s timeout

### `DataConverter` (`physical_ai_server/data_processing/data_converter.py` — overlay)

- `__init__`: `_action_duration_ns = 50_000_000` (default 50 ms; overridden via `set_action_duration_from_fps()`)
- `joint_trajectory2tensor_array` (lines 82-127):
  - Empty trajectory guard: raises German RuntimeError "JointTrajectory hat keine Punkte..."
  - Extra-joint detection: prints once-per-pattern warning if message has joints not in joint_order
- `joint_state2tensor_array` (lines 129-157):
  - Missing joint: raises RuntimeError with German "expected vs available" lists
- `tensor_array2joint_msgs` (lines 211-250):
  - Action array → JointTrajectory or Twist messages per topic type
  - Per-topic joint reordering + reversals (e.g., gripper sign inversion)
  - **fps-aware time_from_start** (overlay computes from `_action_duration_ns`)

### `InferenceManager` (`physical_ai_server/inference/inference_manager.py` — overlay)

State (overlay):
- `_expected_image_keys` — read from policy `config.input_features`
- `_expected_image_shapes` — for resolution validation
- `_last_image_hashes`, `_last_image_change_time`, `_stale_warn_interval=5s`, `_stale_threshold=2s`, `_stale_halt_threshold=5s`
- `_action_min`, `_action_max`, `_action_max_delta` — safety envelope arrays

Methods (overlay-added):
- `set_action_limits(min, max, max_delta)` — runtime-configured per-joint clamp + per-tick velocity cap
- `validate_policy(path)` — pre-load: path exists + config.json present + has 'type'/'model_type' field
- `_read_expected_image_keys()` (lines 154-163): from `config.input_features` keys matching `observation.images.*`
- `_read_expected_image_shapes()` (lines 165-181): shape tuples per camera
- `_check_stale_cameras()` (lines 183-225): hash 4 sparse 256-byte slices per image; warn if frozen &gt;2 s; **halt (return camera name) if &gt;5 s**
- `predict()` override (lines 237-303):
  1. **Camera name validation** (lines 243-259): exact match to `_expected_image_keys`. Mismatch → German error "Das Modell erwartet die Kameras {expected}, aber verbunden sind nur {provided}" → return None (skip tick).
  2. **Stale check** (lines 261-271): if any camera frozen >5 s → return None.
  3. **Image shape validation** (lines 275-289): match expected (H, W) → return None on mismatch with German error.
  4. `_preprocess(images, state)` (lines 220-233 below): each image `torch.from_numpy → /255 → permute(2,0,1) → unsqueeze(0)`, keyed `observation.images.{name}`.
  5. `policy.select_action(observation)` under `torch.inference_mode()`.
  6. `_apply_safety_envelope(action)` (lines 305-367): NaN/inf reject, joint-limit clamp, per-tick velocity cap.
- Returns numpy action array OR None (caller skips publish).

### `LeRobotDatasetWrapper` (`physical_ai_server/data_processing/lerobot_dataset_wrapper.py`)

- `total_frame_buffer` accumulates across episodes
- `add_frame_without_write_image()` validates schema, appends, auto-timestamps as `frame_index / fps`
- `append_episodes()` merges buffer, tracks episode_ranges
- Video encoding spawns FFmpegEncoder per camera key

---

## 9. Topic table

| Topic | Type | Pub | Sub |
|---|---|---|---|
| `/joint_states` | JointState | follower joint_state_broadcaster | physical_ai_server (record + inference) |
| `/leader/joint_states` | JointState | leader joint_state_broadcaster | entrypoint sync reader, physical_ai_server (record) |
| `/leader/joint_trajectory` | JointTrajectory | leader joint_trajectory_command_broadcaster + entrypoint sync + physical_ai_server inference | follower arm_controller (via remap) |
| `/arm_controller/follow_joint_trajectory` | Action | physical_ai_server inference | follower JointTrajectoryController |
| `/leader/trigger_position_controller/commands` | Float64MultiArray | omx_l launch ExecuteProcess | leader trigger_position_controller |
| `/gripper/image_raw/compressed` | CompressedImage | usb_cam #1 | physical_ai_server |
| `/scene/image_raw/compressed` | CompressedImage | usb_cam #2 | physical_ai_server |
| `/training/status` | TrainingStatus | physical_ai_server | React (rosbridge) |

---

## 10. Recording state machine

Phases (TaskStatus.phase values):

| # | Name | data_manager._status | Behavior |
|---|---|---|---|
| 0 | READY | — | no task active |
| 1 | WARMING_UP | warmup | wait `warmup_time_s` |
| 2 | RESETTING | reset | inter-episode pause |
| 3 | RECORDING | run | add frames at fps |
| 4 | SAVING | save | encode video + write metadata async |
| 5 | STOPPED | stop | user abort, finalize current |
| 6 | INFERENCING | (different status set) | running policy on follower |

Transitions (in `data_manager.record()`, lines 108-207):
```
warmup    → (time check passes) → run
run       → (episode_time_s expires) → save
run       → (RAM < 2 GB) → finish (multi-task) or record_early_save (single-task)
save      → (encoding done) → reset (more eps) or finish (num_episodes reached)
reset     → (reset_time_s expires) → run
stop      → (encoding done) → finished
```

`_check_time(expected_duration_s, next_status)` increments `_proceed_time`, transitions when elapsed ≥ expected.

---

## 11. Inference loop (`_inference_timer_callback` lines 526-608)

30 Hz on the main ROS2 executor (single-threaded; slow topic blocks loop).

1. **Wait for topics** (5 s timeout per topic). If camera or follower missing → log "Waiting..." + return.
2. **Convert messages**: `data_manager.convert_msgs_to_raw_datas()` → camera_data dict + follower_data float32[6].
3. **Lazy load policy** (first tick only): `inference_manager.load_policy()`. Reads config.json, populates `_expected_image_keys`, moves weights to GPU.
4. **Predict action** via overlay: camera name check → stale check → shape check → preprocess → `policy.select_action()` → safety envelope. Returns None if any check fails (skip tick, no publish).
5. **Convert action to messages**: `data_converter.tensor_array2joint_msgs(action, joint_topic_types, joint_order)`. Splits action by leader topic (arm + gripper → 2 JointTrajectory messages typically combined into one).
6. **Publish action**: `communicator.publish_action(...)` → `/arm_controller/follow_joint_trajectory` → remapped to `/leader/joint_trajectory` → drives follower.
7. **Publish status**: phase=INFERENCING.

Error handling (lines 597-608): on exception → log + clear policy + stop timer + status phase=READY.

---

## 12. Overlays applied at build time

See [`15-docker.md`](15-docker.md) §6 for the build-time mechanism. The 5 overlays in `docker/physical_ai_server/overlays/`:

| Overlay | Replaces | Lines |
|---|---|---|
| `inference_manager.py` | `physical_ai_server/inference/inference_manager.py` | ~508 |
| `data_manager.py` | `physical_ai_server/data_processing/data_manager.py` | ~1249 |
| `data_converter.py` | `physical_ai_server/data_processing/data_converter.py` | ~251 |
| `omx_f_config.yaml` | `physical_ai_server/config/omx_f_config.yaml` (or similar) | ~28 |
| `physical_ai_server.py` | `physical_ai_server/physical_ai_server.py` (main node) | ~100 |

`omx_f_config.yaml` content:
```yaml
observation_list: [gripper, scene, state]
camera_topic_list:
  - gripper:/gripper/image_raw/compressed
  - scene:/scene/image_raw/compressed
joint_topic_list:
  - follower:/joint_states
  - leader:/leader/joint_trajectory
joint_order:
  leader: [joint1, joint2, joint3, joint4, joint5, gripper_joint_1]
```

Plus 5 overlays in `docker/open_manipulator/overlays/`:
- `omx_f.ros2_control.xacro` — adds `is_async="true"` and tuned safety params
- `omx_f_hardware_controller_manager.yaml` — joint trajectory tolerances + gripper current limits
- `omx_l.ros2_control.xacro` — enables effort/Goal Current on joints 1-5 for gravity comp
- `omx_l_leader_ai.launch.py` — spawns `gravity_compensation_controller` first
- `omx_l_leader_ai_hardware_controller_manager.yaml` — gravity comp params (5-joint friction thresholds)

For source-level diffs, see the agent dive in commit history or `git diff` against `_upstream/`.

---

## 13. Behavior trees (`physical_ai_bt`)

`bt_node.py` (lines 33+):
- `BehaviorTreeNode` ROS2 node
- Loads XML tree from disk
- Params: robot_type, tree_xml, tick_rate
- Tick callback at configurable frequency
- Blackboard state

Action nodes in `physical_ai_bt/physical_ai_bt/actions/`:
- `MoveArms` — publish JointTrajectory to L/R arm controllers, wait for joint state feedback within position threshold, default duration `MOVE_ARMS_DURATION_SEC`
- `MoveHead`, `MoveLift`, `Rotate` — similar pattern

Sequence control: `sequence.py` — execute children in order.

Invocation: blackboard commands via ROS services or `RemoteTrigger`.

---

## 14. Footguns

1. **Don't remove the `/arm_controller/joint_trajectory → /leader/joint_trajectory` remap.** Everything depends on it. The follower has no concept of "leader".
2. **Don't change camera names without rebuilding policies.** Inference enforces exact match (no remap). Rename without coordination = "Das Modell erwartet die Kameras..." error.
3. **Don't disable safety overlay features.** Joint clamp, NaN guard, stale-camera halt protect real hardware. See [`WORKFLOW.md`](WORKFLOW.md) §1.
4. **Don't change `codebase_version: "v2.1"` casually.** Modal preflight enforces it. Old datasets become orphans without a migration script.
5. **Don't change ROS_DOMAIN_ID across sessions** without coordinating with the GUI's per-machine derivation. Mixed defaults = LAN cross-talk.
6. **Don't add joints to the robot config** without updating overlays + `joint_order` in `omx_f_config.yaml` + ros2_control xacro. Extra-joint detection in data_converter warns but doesn't auto-fix.
7. **`identify_arm.py` is dead code in entrypoint** — used only by the GUI's device scanner. Don't add new entrypoint logic depending on it.
8. **The startup quintic-sync trajectory shows up in the first frames** if recording starts immediately after entrypoint exits. Don't auto-start recording in &lt;5 s.
9. **`physical_ai_server` does NOT have /dev access.** Don't add USB-touching code there; that lives in `open_manipulator`.

---

## 15. Local dev

ROS2 development requires Linux + ROS2 Jazzy (or use the container). Most edits to overlays are tested by:
1. Edit overlay file in `docker/physical_ai_server/overlays/`
2. `cd docker && REGISTRY=nettername ./build-images.sh` (rebuilds physical-ai-server only if Dockerfile/overlay changes)
3. Restart container: `wsl -d EduBotics -- docker compose restart physical_ai_server`
4. Tail logs: `wsl -d EduBotics -- docker logs physical_ai_server -f`

For ROS topic inspection:
```bash
wsl -d EduBotics -- docker exec physical_ai_server ros2 topic list
wsl -d EduBotics -- docker exec physical_ai_server ros2 topic echo /joint_states
```

For interactive debugging, attach to the running container:
```bash
wsl -d EduBotics -- docker exec -it physical_ai_server bash
source /opt/ros/jazzy/setup.bash && source /root/ros2_ws/install/setup.bash
ros2 node list
```

---

## 16. Cross-references

- Overlay build mechanism: [`15-docker.md`](15-docker.md) §5–7
- Workflow for changing an overlay: [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md)
- Recording → training pipeline: [`02-pipeline.md`](02-pipeline.md) §5, §7
- Dataset format read by Modal: [`11-modal-training.md`](11-modal-training.md) §3 (preflight)
- React side that calls these ROS services: [`13-frontend-react.md`](13-frontend-react.md) §5
- Known issues (safety, drift): [`21-known-issues.md`](21-known-issues.md) §3.3, §3.5, §3.8

---

**Last verified:** 2026-05-06.
