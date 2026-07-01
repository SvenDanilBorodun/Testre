#!/usr/bin/env python3
#
# Copyright 2025 ROBOTIS CO., LTD.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Dongyun Kim, Seongwoo Kim

import glob
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
import traceback
from typing import Optional


# Audit N3: scrub `Authorization: Bearer <jwt>` headers out of every
# log record emitted by urllib3 / requests at DEBUG level. A bare
# `logging.basicConfig(level=DEBUG)` anywhere in the process — or a
# Modal cold-debug session — would otherwise print the cached student
# JWT into the container log. Filter is installed once on import; the
# match cost is negligible vs the alternative of dropping debug logging
# entirely.
class _BearerTokenScrubber(logging.Filter):
    _PATTERN = re.compile(r'(?i)Bearer\s+[A-Za-z0-9._\-]+')

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = self._PATTERN.sub('Bearer ***', record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: (self._PATTERN.sub('Bearer ***', v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        self._PATTERN.sub('Bearer ***', a) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:
            # Filter must never raise — drop the scrub on malformed
            # records and let the original message through.
            pass
        return True


_BEARER_SCRUBBER = _BearerTokenScrubber()
for _log_name in ('urllib3', 'urllib3.connectionpool', 'requests', 'http.client'):
    logging.getLogger(_log_name).addFilter(_BEARER_SCRUBBER)

from ament_index_python.packages import get_package_share_directory
from physical_ai_interfaces.msg import (
    Detection,
    HFOperationStatus,
    SensorSnapshot,
    TaskStatus,
    TrainingStatus,
    WorkflowStatus,
)
from physical_ai_interfaces.srv import (
    CalibrationCaptureFrame,
    CalibrationHistory,
    CalibrationPreview,
    CalibrationSolve,
    CalibrationStatus,
    CancelCalibration,
    ControlHfServer,
    GetDatasetList,
    GetHFUser,
    GetModelWeightList,
    GetObjectCatalog,
    GetPolicyList,
    GetRobotTypeList,
    GetSavedPolicyList,
    GetTrainingInfo,
    GetUserList,
    MarkDestination,
    SendCommand,
    SendTrainingCommand,
    SetHFUser,
    SetRobotType,
    StartCalibration,
    StartWorkflow,
    StopWorkflow,
    VerifyCalibration,
    WorkflowContinue,
    WorkflowPause,
    WorkflowSetBreakpoints,
    WorkflowStep,
    WorkshopCapturePose,
    WorkshopHandGuide,
    WorkshopJog,
    WorkshopRecordControl,
    WorkshopReplay,
)

from physical_ai_server.communication.communicator import Communicator
from physical_ai_server.data_processing.data_manager import DataManager
from physical_ai_server.data_processing.hf_api_worker import HfApiWorker
from physical_ai_server.inference.inference_manager import InferenceManager
from physical_ai_server.safety.collision_monitor import CollisionMonitorMixin
from physical_ai_server.timer.timer_manager import TimerManager
from physical_ai_server.training.training_manager import TrainingManager
from physical_ai_server.utils.parameter_utils import (
    declare_parameters,
    load_parameters,
    log_parameters,
    parse_topic_list_with_names,
)

import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import Empty


# Roboter Studio Batch 2b — manual hand-guide recording (/workshop/record).
# Module consts (NOT env vars — a new EDUBOTICS_* knob would need a
# docker-compose forward per the env-forwarding-guard). RECORD_MAX_S caps a
# single recording; _MANUAL_RECORD_FPS is the sampling rate; the hard sample cap
# is derived from the two with a margin so a wall-clock-runaway can't grow the
# buffer without bound. Sub-threshold-identical samples (< _MANUAL_RECORD_MIN_DELTA_RAD
# on every joint vs the previous kept sample) are dropped so a still arm doesn't
# fill the buffer with duplicates.
RECORD_MAX_S = 120.0
_MANUAL_RECORD_FPS = 25
_MANUAL_RECORD_MAX_SAMPLES = int(RECORD_MAX_S * _MANUAL_RECORD_FPS) + 100
_MANUAL_RECORD_MIN_DELTA_RAD = 0.003
# Timeout (s) for acquiring the manual-control mutex in a driving/torque-switch
# callback. If a jog/replay drive is holding it, a new manual op is refused in
# German rather than queueing (the callbacks run on the reentrant preempt group,
# so a refusal is immediate and never starves the heartbeat).
_MANUAL_LOCK_TIMEOUT_S = 2.0
# F6 — a follower JointState older than this (seconds) is treated as STALE: a
# driven jog/replay refuses rather than seed from a frozen pose.
_FOLLOWER_JOINT_MAX_AGE_S = 1.0
# F7 — bounded escape from a wedged Handbetrieb: after this many CONSECUTIVE
# failed torque-ON asserts in the jog path, clear on_manual (with a loud German
# error) so the student is never permanently blocked behind „Handbetrieb ist
# aktiv" when the arm cannot be re-locked.
_MANUAL_TORQUE_FAIL_LIMIT = 3
# Backend idle re-torque watchdog — if a manual session is left open + idle (no
# manual op) this long, the follower is re-torqued and on_manual cleared, so a
# tab-close during „Arm freischalten" never leaves the arm limp forever (and no
# mode stays wedged behind on_manual). Never fires during an active record or a
# mid-jog/replay.
_MANUAL_IDLE_RETORQUE_S = 30.0
_MANUAL_IDLE_WATCHDOG_PERIOD_S = 5.0


class PhysicalAIServer(CollisionMonitorMixin, Node):
    # Define operation modes (constants taken from Communicator)

    DEFAULT_SAVE_ROOT_PATH = Path.home() / '.cache/huggingface/lerobot'
    DEFAULT_TOPIC_TIMEOUT = 5.0  # seconds
    PUB_QOS_SIZE = 10

    # Wire int for the policy-loading phase. getattr fallback mirrors the
    # collision constants (collision_monitor.py): a container whose COMPILED
    # physical_ai_interfaces predate the constant still publishes the right
    # raw value — React compares ints (taskPhases.js); enum-parity CI keeps
    # the literal in sync with the .msg.
    PHASE_INFERENCE_LOADING = getattr(TaskStatus, 'INFERENCE_LOADING', 10)

    class RosbagNotReadyException(Exception):
        """Exception raised when rosbag recording cannot start yet."""

        pass

    def __init__(self):
        super().__init__('physical_ai_server')
        self.get_logger().info('Start Physical AI Server')

        self.params = None
        self.total_joint_order = None
        self.on_recording = False
        self.on_inference = False
        self.on_calibration = False
        self.on_workflow = False
        # Batch 2b — manual-control session (jog / hand-guide / record / replay).
        # While True the arm is owned by Handbetrieb: capture/jog/record/replay
        # all coexist within the one session (they request mode 'manual', which
        # _assert_no_other_active RELAXES against on_manual), and every OTHER mode
        # (recording/inference/…) is refused so nothing claims the arm mid
        # hand-guide (limp!) or mid driven jog/replay. Opened by hand_guide(true)
        # / record(start) / (transiently) jog+replay; closed by hand_guide(false)
        # / record(cancel).
        self.on_manual = False
        # W1 — force recalibration on every fresh container start (DEFAULT ON).
        # Read ONCE at startup. When ON, calibration_status_callback reports the
        # scene calibration as ABSENT (intrinsics + extrinsic + table-plane) until
        # the FULL flow's last step (the touch-off plane solve) completes THIS
        # boot — at which point _recalibrated_this_boot latches True and the
        # reported status reflects the real on-disk YAMLs again. No YAML is
        # deleted; the latch resets on the next restart. One-var rollback: set
        # EDUBOTICS_FORCE_RECALIBRATION=0 (or false/no/off) for the classroom.
        self._force_recalib = os.environ.get(
            'EDUBOTICS_FORCE_RECALIBRATION', '1'
        ).strip().lower() not in ('0', 'false', 'no', 'off', '')
        self._recalibrated_this_boot = False
        # W5 — collected ground-truth verify points for /calibration/verify:
        # each entry is (truth_xy, detected_xy, truth_yaw_rad|None,
        # detected_yaw_rad|None). Mutated only inside the verify callback (which
        # runs on the reentrant preempt group), so guard it with a lock.
        self._verify_points: list = []
        self._verify_lock = threading.Lock()
        # True while the eager background policy load (START_INFERENCE ->
        # _eager_load_policy thread) is in flight; the inference FPS timer
        # skips its tick and re-publishes INFERENCE_LOADING instead.
        self._inference_policy_loading = False
        # Audit O1: last detection list emitted by the workflow runtime,
        # consumed by _sensor_snapshot_timer_callback to populate the
        # React Sensoren tab. The list is whatever the most recent
        # named-object detection block produced — labels follow the
        # perception.py 'tag{id}' convention. Cleared by TTL in the timer
        # when it goes stale.
        # Audit fix #15: pack (ts, detections) into a single tuple that
        # writers rebind atomically and readers snapshot in one read.
        # Avoids the dual-attribute race where the sensor timer could
        # observe a fresh ts paired with the previous detections list
        # (or vice-versa). Kept the legacy attribute names too so any
        # other reader (tests, debug introspection) keeps working.
        self._workflow_detections_state: tuple[float, list] = (0.0, [])
        self._workflow_last_detections: list = []
        self._workflow_last_detections_ts: float = 0.0
        # Cleared by /calibration/start, set by /calibration/cancel. The
        # touch-off (table_touch) step polls this so a manual-teach session
        # can be aborted mid-flight.
        self._calibration_stop_event = threading.Event()
        # Serialises follower-torque switching. /calibration/solve runs on the
        # node default (mutually-exclusive) group, but /calibration/cancel runs
        # on the REENTRANT _preempt_cb_group — so a cancel can fire while a
        # touch-off solve is mid-flight, and both call _set_follower_torque on
        # the shared lazily-created _dxl_torque_client. Without a lock the
        # lazy `getattr(...) is None` create is a check-then-set race (two
        # clients created, one leaked) and two concurrent call_async race the
        # same client. The lock makes the torque switch atomic.
        self._dxl_torque_lock = threading.Lock()

        # Batch 2b — manual-control (jog / hand-guide / record / replay).
        # _manual_lock serializes the actual arm-driving/torque-switching across
        # the manual services (held for a jog drive, briefly for hand-guide /
        # record, and by the async replay daemon for its whole publish).
        # _manual_stop_event is polled by chunked_publish so an in-flight jog /
        # replay can be aborted (set by hand_guide(false); cleared at each new
        # drive). _handguide_buffer holds the recorded [t_s, j1..j5, grip] samples.
        self._manual_lock = threading.Lock()
        self._manual_stop_event = threading.Event()
        self._handguide_buffer: list = []
        self._manual_record_active = False
        self._manual_record_timer = None
        self._manual_record_start_mono = 0.0
        self._manual_replay_thread = None
        # F1 — atomic mode arbiter. The four mode-ownership flags
        # (on_manual / on_workflow / on_recording / on_inference) are set by
        # callbacks dispatched across DIFFERENT callback groups; without one
        # shared lock the check-then-set is a race (two tabs both pass
        # _assert_no_other_active then both start, putting two writers on
        # /leader/joint_trajectory or silently disabling the collision guard).
        # Every mode entry point holds _mode_lock around BOTH its
        # _assert_no_other_active check AND its owning-flag set, and sets the flag
        # BEFORE spawning any driver. Held BRIEFLY only — always RELEASED before
        # _manual_lock / motion_lock / any torque switch, so it never participates
        # in a lock-ordering cycle (no deadlock). Distinct from _manual_lock
        # (which serialises the actual arm drive within a manual session).
        self._mode_lock = threading.Lock()
        # F4 — one-shot cleanup timer + flag when a manual recording hits
        # RECORD_MAX_S (re-torque + clear on_manual on the executor, off the
        # sampler which must never block).
        self._manual_record_cap_timer = None
        self._manual_record_cap_reached = False
        # F7 — consecutive failed torque-ON asserts in the jog path (reset on a
        # successful torque switch); a bounded run triggers the on_manual escape.
        self._manual_torque_fail_count = 0
        # Idle re-torque watchdog bookkeeping: last manual-op wall-clock and the
        # last confirmed follower torque state (None = unknown). _set_follower_torque
        # is the single choke point that updates the torque state.
        self._manual_last_activity_mono = 0.0
        self._follower_torque_on: Optional[bool] = None

        # FIX 1 + FIX 4 — session-ownership REFCOUNT + hard-exit GENERATION.
        # on_manual is a DERIVED boolean (see _recompute_on_manual_locked): it is
        # True iff a PERSISTENT hand-guide/record session is open OR at least one
        # TRANSIENT jog/replay is in flight. The old per-call `opened` boolean
        # cleared on_manual unlocked, so two concurrent transient ops let the first
        # clear it while the second still drove — a workflow/inference start then
        # saw the arm free (two writers on /leader/joint_trajectory). The refcount
        # + persistent flag are mutated ONLY under _mode_lock, so the derived
        # on_manual can never be cleared while another manual op still owns the arm.
        #   INVARIANT: on_manual == (_manual_persistent or _manual_transient_ops > 0)
        # _manual_exit_gen is bumped by every „Beenden" hard-exit so a jog/replay
        # that committed (snapshotted the old generation) but has not driven yet
        # ABORTS instead of driving the arm after the session was torn down.
        self._manual_transient_ops = 0
        self._manual_persistent = False
        self._manual_exit_gen = 0

        self.hf_cancel_on_progress = False

        self.robot_type_list = self.get_robot_type_list()
        self.start_recording_time: float = 0.0

        # Retained for legacy mode-arbitration in _assert_no_other_active;
        # the value is never flipped to True after on-device training was
        # removed in v2.5.0, so the training branch there is dead but
        # harmless (and keeping the attribute avoids an AttributeError if
        # some other code path reads it during transition).
        self.is_training = False

        self._init_core_components()

        self._init_ros_publisher()
        self._init_ros_service()

        # EduBotics teleop force/collision e-stop (Rule §2 software guard, teleop-only).
        # Arms the read-only force monitor + the safe-home/resync orchestration.
        self._init_collision_monitor()

        # Roboter Studio runs FOLLOWER-ONLY (the GUI starts it with
        # EDUBOTICS_FOLLOWER_ONLY=1 so the leader is never launched). With no
        # leader there is no joint_trajectory_command_broadcaster flooding
        # /leader/joint_trajectory, so the workflow/calibration publisher is the
        # sole writer and no teleop arbitration is needed. (The 2026-06-17
        # arbiter/bridge stack was removed — see docs/plans/2026-06-18-*.)

        self._setup_timer_callbacks()

        # Backend idle re-torque watchdog (low-rate safety net for Handbetrieb).
        # On its own MutuallyExclusiveCallbackGroup so a re-torque switch (up to a
        # few seconds) never stalls the node-default services / heartbeat.
        self._manual_idle_watchdog = self.create_timer(
            _MANUAL_IDLE_WATCHDOG_PERIOD_S,
            self._manual_idle_watchdog_cb,
            callback_group=MutuallyExclusiveCallbackGroup(),
        )

        self.previous_data_manager_status = None

        self.goal_repo_id = None

    def _init_core_components(self):
        self.communicator: Optional[Communicator] = None
        self.data_manager: Optional[DataManager] = None
        self.timer_manager: Optional[TimerManager] = None
        # Liveness heartbeat is node-owned now (see _init_ros_publisher); no
        # TimerManager-based heartbeat tied to robot selection any more.
        self.training_timer: Optional[TimerManager] = None
        self.inference_manager: Optional[InferenceManager] = None
        # EduBotics v2.5.0: on-device training removed; Modal Cloud handles training.
        # The TrainingManager class is kept only as a holder for two static helpers
        # (get_weight_save_root_path, get_available_list) used by inference-side
        # callbacks. There is no instance.
        self.training_manager = None
        # Calibration manager is constructed lazily on first calibration call
        # — same pattern as TrainingManager — so a server that never enters
        # Roboter Studio doesn't pay the OpenCV-aruco import cost.
        self.calibration_manager = None
        # Workflow runtime is also lazily constructed.
        self.workflow_manager = None
        # Roboter Studio Phase-3 simulation runtime: a parallel WorkflowManager
        # driving a virtual arm (SimArm) + synthetic perception (SimPerception)
        # over the sim-only /sim/joint_states topic. Lazily constructed; the
        # placed virtual objects ride INSIDE the /workflow/start payload (a `sim`
        # sibling key in workflow_json) — no .srv change.
        self.sim_workflow_manager = None
        self._sim_arm = None
        self._sim_objects = []
        self._sim_joint_state_publisher = None
        self._sim_idle_timer = None
        self._last_sim_joints = None

        # Initialize HF API Worker
        self.hf_api_worker: Optional[HfApiWorker] = None
        self.hf_status_timer: Optional[TimerManager] = None
        self._init_hf_api_worker()

    def _init_ros_publisher(self):
        self.get_logger().info('Initializing ROS publishers...')
        pub_qos_size = 100
        self.training_status_publisher = self.create_publisher(
            TrainingStatus,
            '/training/status',
            pub_qos_size
        )
        # Roboter Studio publishes per-block phase + log strip on this
        # topic. Depth 50 is a generous buffer for the log messages a
        # student workflow produces while still bounded.
        self.workflow_status_publisher = self.create_publisher(
            WorkflowStatus,
            '/workflow/status',
            50,
        )
        # Phase-2 sensor snapshot — ~5 Hz heartbeat for the React debug
        # panel. Shallow depth (5) because each sample is independent
        # and a 0.2 s staleness threshold means we'd rather drop than
        # buffer.
        self.sensor_snapshot_publisher = self.create_publisher(
            SensorSnapshot,
            '/workflow/sensors',
            5,
        )
        # 5 Hz timer that publishes a SensorSnapshot whenever a workflow
        # is active. The callback short-circuits to a no-op when
        # ``on_workflow`` is False so we don't pay perception cost during
        # inference / recording.
        self._sensor_snapshot_timer = self.create_timer(
            0.2, self._sensor_snapshot_timer_callback,
        )

        # Liveness heartbeat — node-owned and started HERE in __init__, NOT in
        # init_ros_params(). It used to live on the Communicator and was created
        # only when the student selected a robot type (/set_robot_type ->
        # init_ros_params), which coupled "is the node alive" to "has a robot been
        # picked": after ANY node restart (crash/respawn/OOM) the React app showed
        # "Getrennt" until the student re-selected the robot on the Start page, even
        # though the node was already up. Decoupling it here means liveness is
        # reported the moment the node starts.
        #
        # On its OWN MutuallyExclusiveCallbackGroup so a long-blocking callback on
        # the node's default group (a slow service, a recording-tick decode) can no
        # longer stall the 1 Hz tick and flicker the React connection to
        # "disconnected" — the documented heartbeat-starvation class. QoS mirrors
        # the old Communicator heartbeat (BEST_EFFORT / VOLATILE / KEEP_LAST depth 1).
        self._heartbeat_cb_group = MutuallyExclusiveCallbackGroup()
        heartbeat_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
        )
        self._node_heartbeat_pub = self.create_publisher(
            Empty, 'heartbeat', heartbeat_qos)
        self._node_heartbeat_timer = self.create_timer(
            1.0,
            self._heartbeat_timer_callback,
            callback_group=self._heartbeat_cb_group,
        )

    def _heartbeat_timer_callback(self):
        # Node-level liveness beacon (1 Hz). Independent of robot selection /
        # recording so the React heartbeat watchdog reports "Verbunden" whenever
        # the node is alive. Publishing Empty() is allocation-cheap and cannot block.
        self._node_heartbeat_pub.publish(Empty())

    def _init_ros_service(self):
        self.get_logger().info('Initializing ROS services...')
        # Reentrant group for the two services that MUST preempt an
        # in-flight long-running calibration callback: /calibration/cancel
        # and /calibration/status. Without a reentrant group on
        # cancel/status they would queue behind a blocking calibration
        # callback and the stop-event polling at 50ms cadence would be
        # unreachable. Read-only services (status) and preemption services
        # (cancel) are safe to dispatch concurrently because they only
        # set/clear flags and read disk state.
        from rclpy.callback_groups import ReentrantCallbackGroup
        self._preempt_cb_group = ReentrantCallbackGroup()
        # Dedicated group for the HuggingFace services. whoami()/login do
        # blocking network I/O (seconds on a cold/slow school LAN); without
        # isolating them from the node's default MutuallyExclusiveCallbackGroup
        # they serialize against the 1 Hz heartbeat + /task/status timers, so a
        # slow Benutzer-ID lookup stalls the heartbeat and flickers the React
        # connection to "disconnected". A separate group lets the 6-thread
        # executor run them on another thread.
        self._hf_cb_group = ReentrantCallbackGroup()
        service_definitions = [
            ('/task/command', SendCommand, self.user_interaction_callback),
            ('/get_robot_types', GetRobotTypeList, self.get_robot_types_callback),
            ('/set_robot_type', SetRobotType, self.set_robot_type_callback),
            ('/register_hf_user', SetHFUser, self.set_hf_user_callback, self._hf_cb_group),
            ('/get_registered_hf_user', GetHFUser, self.get_hf_user_callback, self._hf_cb_group),
            ('/get_policy_list', GetPolicyList, self.get_policy_list_callback),
            ('/get_saved_policies', GetSavedPolicyList, self.get_saved_policies_callback),
            ('/training/command', SendTrainingCommand, self.user_training_interaction_callback),
            ('/training/get_available_policy', GetPolicyList, self.get_available_list_callback),
            ('/training/get_user_list', GetUserList, self.get_user_list_callback),
            ('/training/get_dataset_list', GetDatasetList, self.get_dataset_list_callback),
            (
                '/training/get_model_weight_list',
                GetModelWeightList,
                self.get_model_weight_list_callback
            ),
            ('/huggingface/control', ControlHfServer, self.control_hf_server_callback),
            ('/training/get_training_info', GetTrainingInfo, self.get_training_info_callback),
            ('/calibration/start', StartCalibration, self.calibration_start_callback),
            (
                '/calibration/capture_frame',
                CalibrationCaptureFrame,
                self.calibration_capture_callback,
            ),
            ('/calibration/solve', CalibrationSolve, self.calibration_solve_callback),
            (
                '/calibration/cancel',
                CancelCalibration,
                self.calibration_cancel_callback,
                self._preempt_cb_group,
            ),
            (
                '/calibration/status',
                CalibrationStatus,
                self.calibration_status_callback,
                self._preempt_cb_group,
            ),
            ('/workshop/mark_destination', MarkDestination, self.mark_destination_callback),
            # "Position merken" — capture the arm's CURRENT gripper pose into a
            # named destination. A quick FK read + dict write; rides the
            # reentrant preempt group so it can dispatch while a chunked
            # workflow motion holds the node-default group (it gates on
            # _assert_no_other_active anyway, so it refuses during an active run).
            (
                '/workshop/capture_pose',
                WorkshopCapturePose,
                self.capture_pose_callback,
                self._preempt_cb_group,
            ),
            # Batch 2b — manual control (jog / hand-guide / record / replay). All on
            # the reentrant preempt group: a blocking jog / the async replay must
            # not queue behind the node-default group and starve the heartbeat, and
            # record start/stop must dispatch while a workflow motion holds the node
            # default group. Each gates on _assert_no_other_active('manual') +
            # leader_appears_active() and serializes on _manual_lock.
            (
                '/workshop/jog',
                WorkshopJog,
                self.workshop_jog_callback,
                self._preempt_cb_group,
            ),
            (
                '/workshop/hand_guide',
                WorkshopHandGuide,
                self.workshop_hand_guide_callback,
                self._preempt_cb_group,
            ),
            (
                '/workshop/record',
                WorkshopRecordControl,
                self.workshop_record_callback,
                self._preempt_cb_group,
            ),
            (
                '/workshop/replay',
                WorkshopReplay,
                self.workshop_replay_callback,
                self._preempt_cb_group,
            ),
            ('/workshop/get_object_catalog', GetObjectCatalog, self.get_object_catalog_callback),
            ('/workflow/start', StartWorkflow, self.workflow_start_callback),
            ('/workflow/stop', StopWorkflow, self.workflow_stop_callback),
            # Phase-2 debugger plumbing. Pause/Step/Continue must dispatch
            # while the main workflow callback is in-flight (an editing
            # student presses Pause while a chunked motion is running),
            # so they go on the reentrant preempt group like
            # /calibration/cancel — otherwise they queue behind the long
            # motion service callback and the UI looks unresponsive.
            (
                '/workflow/pause',
                WorkflowPause,
                self.workflow_pause_callback,
                self._preempt_cb_group,
            ),
            (
                '/workflow/step',
                WorkflowStep,
                self.workflow_step_callback,
                self._preempt_cb_group,
            ),
            (
                '/workflow/continue',
                WorkflowContinue,
                self.workflow_continue_callback,
                self._preempt_cb_group,
            ),
            (
                '/workflow/set_breakpoints',
                WorkflowSetBreakpoints,
                self.workflow_set_breakpoints_callback,
                self._preempt_cb_group,
            ),
            # Phase-2 calibration helpers (see CalibrationManager). The live
            # ChArUco preview is polled a few Hz while the intrinsic step is
            # active; it runs the lock-free detector and must NOT queue behind /
            # starve the 1 Hz heartbeat on the node default group, so it rides
            # the reentrant preempt group like /calibration/status.
            (
                '/calibration/preview',
                CalibrationPreview,
                self.calibration_preview_callback,
                self._preempt_cb_group,
            ),
            # The verify "add_point" branch grabs a scene frame + runs the
            # AprilTag detector (blocking), so it rides the reentrant preempt
            # group — otherwise it queues behind / starves the 1 Hz heartbeat
            # timer on the node default mutually-exclusive group (the documented
            # whoami/HF pattern).
            (
                '/calibration/verify',
                VerifyCalibration,
                self.calibration_verify_callback,
                self._preempt_cb_group,
            ),
            ('/calibration/history', CalibrationHistory, self.calibration_history_callback),
        ]

        for entry in service_definitions:
            # 4-tuple (name, type, callback, callback_group) for the
            # reentrant-preempt services; 3-tuple for everything else.
            if len(entry) == 4:
                service_name, service_type, callback, cb_group = entry
                self.create_service(
                    service_type, service_name, callback,
                    callback_group=cb_group,
                )
            else:
                service_name, service_type, callback = entry
                self.create_service(service_type, service_name, callback)

        self.get_logger().info('ROS services initialized successfully')

    def _setup_timer_callbacks(self):
        self.timer_callback_dict = {
            'collection': self._data_collection_timer_callback,
            'inference': self._inference_timer_callback
        }

    def init_ros_params(self, robot_type):
        self.get_logger().info(f'Initializing ROS parameters for robot type: {robot_type}')
        param_names = [
            'camera_topic_list',
            'joint_topic_list',
            'observation_list',
            'joint_list',
            'rosbag_extra_topic_list',
        ]

        # Declare parameters
        declare_parameters(
            node=self,
            robot_type=robot_type,
            param_names=param_names,
            default_value=['']
        )

        # Load parameters
        self.params = load_parameters(
            node=self,
            robot_type=robot_type,
            param_names=param_names
        )

        self.joint_order_list = [
            f'joint_order.{joint_name}' for joint_name in self.params['joint_list']
        ]

        declare_parameters(
            node=self,
            robot_type=robot_type,
            param_names=self.joint_order_list,
            default_value=['']
        )

        self.joint_order = load_parameters(
            node=self,
            robot_type=robot_type,
            param_names=self.joint_order_list
        )

        self.total_joint_order = []
        for joint_list in self.joint_order.values():
            self.total_joint_order.extend(joint_list)

        # Log loaded parameters
        log_parameters(self, self.params)
        log_parameters(self, self.joint_order)

        # Initialize observation manager
        self.communicator = Communicator(
            node=self,
            operation_mode=self.operation_mode,
            params=self.params
        )

        # NOTE: the 1 Hz liveness heartbeat is no longer created here — it is
        # node-owned and started in _init_ros_publisher (__init__) so it is
        # independent of robot selection. See the comment there.

        # One-shot stale-session notice (leLab-comparison PR-3): a
        # *.session.json sibling marker means a recording crashed mid-
        # session — the buffered episodes died with the process, so the
        # dataset on disk is incomplete (the finalize() footer never
        # wrote). DETECT + INFORM ONLY, never auto-finalize/delete: the
        # student decides in the Daten tab. Delayed 5 s so the React
        # /task/status subscriber is attached when the toast fires.
        if not getattr(self, '_stale_session_notice_done', False):
            self._stale_session_notice_done = True
            try:
                stale_markers = sorted(
                    self.DEFAULT_SAVE_ROOT_PATH.glob('*/*.session.json'))
            except OSError:
                stale_markers = []
            if stale_markers:
                names = [
                    m.name[: -len('.session.json')] for m in stale_markers
                ]
                shown = ', '.join(names[:3]) + (' …' if len(names) > 3 else '')
                self.get_logger().warning(
                    f'Stale recording session markers found: {names}')

                def _stale_session_notify():
                    self._stale_notice_timer.cancel()
                    notice = TaskStatus()
                    notice.phase = TaskStatus.READY
                    notice.error = (
                        f'Eine frühere Aufnahme wurde unterbrochen und ist '
                        f'unvollständig: {shown}. Bitte im Daten-Tab prüfen '
                        f'und gegebenenfalls löschen.'
                    )
                    self.communicator.publish_status(status=notice)

                self._stale_notice_timer = self.create_timer(
                    5.0, _stale_session_notify)

        self.inference_manager = InferenceManager()
        self.get_logger().info(
            f'ROS parameters initialized successfully for robot type: {robot_type}')

    def get_training_status(self):
        """Stub — on-device training is gone (v2.5.0). Status is reported via
        the Web UI for cloud trainings; this ROS topic publishes an empty
        status so legacy listeners don't crash on missing field."""
        msg = TrainingStatus()
        msg.current_step = 0
        msg.current_loss = float('nan')
        msg.is_training = False
        msg.error = ''
        return msg

    def init_robot_control_parameters_from_user_task(
            self,
            task_info):
        self.get_logger().info(
            'Initializing robot control parameters from user task...')
        self.data_manager = DataManager(
            save_root_path=self.DEFAULT_SAVE_ROOT_PATH,
            robot_type=self.robot_type,
            task_info=task_info,
            upload_callback=self._enqueue_dataset_upload,
        )
        self.communicator.clear_latest_data()

        self.timer_manager = TimerManager(node=self)
        self.timer_manager.set_timer(
            timer_name=self.operation_mode,
            timer_frequency=task_info.fps,
            callback_function=self.timer_callback_dict[self.operation_mode]
        )
        self.timer_manager.start(timer_name=self.operation_mode)
        self.get_logger().info(
            'Robot control parameters initialized successfully')

    def clear_parameters(self):
        if self.communicator is not None:
            self.communicator.cleanup()
            self.communicator = None

        if self.timer_manager is not None:
            self.timer_manager = None

        # The heartbeat is node-owned (created in __init__) and intentionally
        # NOT torn down here: a robot-type switch (clear_parameters +
        # init_ros_params) must not silence liveness even momentarily.

        if self.training_timer is not None:
            self.training_timer.stop(timer_name='training_status')
            self.training_timer = None

        # Audit §3.15 — robot-type switch needs to discard the
        # workflow + calibration managers too, otherwise the next
        # workflow run uses the previous robot's joint topology / IK
        # chain / camera-keyed calibration files.
        if self.workflow_manager is not None:
            try:
                self.workflow_manager.stop()
            except Exception:
                pass
            self.workflow_manager = None
            self.on_workflow = False
        # Phase-3 sim runtime — discard the parallel manager + virtual arm on a
        # robot-type switch for the same reason (new robot's IK chain / catalog).
        if self.sim_workflow_manager is not None:
            try:
                self.sim_workflow_manager.stop()
            except Exception:
                pass
            self.sim_workflow_manager = None
            self._sim_arm = None
            self.on_workflow = False
            # LOW-3: clear the cached sim pose + scene so the idle-republish timer
            # doesn't keep emitting the PREVIOUS robot's last pose on
            # /sim/joint_states after a robot-type switch (the publisher/timer are
            # reused and re-seeded on the next sim run; _sim_idle_republish
            # returns early while _last_sim_joints is None).
            self._last_sim_joints = None
            self._sim_objects = []
        if self.calibration_manager is not None:
            # If a touch-off session left the follower torqued OFF, re-torque it
            # before tearing the manager down — a robot-type switch mid-touch-off
            # would otherwise leak a limp follower (best-effort here: the node is
            # re-initialising and the new controllers will re-activate torque,
            # but assert it so the arm holds during the switch window).
            if getattr(self, '_active_calib_step', None) == 'table_touch':
                self._set_follower_torque(True)
                self._active_calib_step = None
            self.calibration_manager = None
            # WS1: dropping the session here must also resume teleop, else a
            # robot-type switch mid-calibration leaks the suspend (broadcaster
            # stays inactive -> teleop dead).
            self._set_calibration_active(False)

        self.params = None
        self.total_joint_order = None
        self.joint_order = None

    def set_hf_user_callback(self, request, response):
        request_hf_token = request.token
        try:
            if DataManager.register_huggingface_token(request_hf_token):
                self.get_logger().info('Hugging Face user token registered successfully')
                response.user_id_list = DataManager.get_huggingface_user_id()
                response.success = True
                response.message = 'Hugging Face user token registered successfully'
            else:
                self.get_logger().error('Failed to register Hugging Face user token')
                response.user_id_list = []
                response.success = False
                response.message = 'Failed to register token, Please check your token'
        except Exception as e:
            self.get_logger().error(f'Error in set_hf_user_callback: {str(e)}')
            response.user_id_list = []
            response.success = False
            # Audit §3.21 — don't leak the raw exception text to the
            # client (paths / stack-trace fragments would land in the
            # React toast). Log it server-side, return generic German.
            response.message = 'Hugging-Face-Token konnte nicht registriert werden.'

        return response

    def get_hf_user_callback(self, request, response):
        try:
            user_ids = DataManager.get_huggingface_user_id()
            if user_ids is not None:
                response.user_id_list = user_ids
                self.get_logger().info(f'Hugging Face user IDs: {user_ids}')
                response.success = True
                response.message = 'Hugging Face user IDs retrieved successfully'
            else:
                self.get_logger().error('Failed to retrieve Hugging Face user ID')
                response.user_id_list = []
                response.success = False
                response.message = 'Failed to retrieve Hugging Face user ID'
        except Exception as e:
            self.get_logger().error(f'Error in get_hf_user_callback: {str(e)}')
            response.user_id_list = []
            response.success = False
            # Audit §3.21 — sanitize exception text.
            response.message = 'Hugging-Face-Benutzer konnte nicht gelesen werden.'

        return response

    def get_robot_type_list(self):
        pkg_dir = get_package_share_directory('physical_ai_server')
        config_dir = os.path.join(pkg_dir, 'config')
        config_files = glob.glob(os.path.join(config_dir, '*.yaml'))
        config_files.sort()

        robot_type_list = []
        for config_file in config_files:
            robot_type = os.path.splitext(os.path.basename(config_file))[0]
            if robot_type.endswith('_config'):
                robot_type = robot_type[:-7]
            robot_type_list.append(robot_type)

        self.get_logger().info(f'Available robot types: {robot_type_list}')
        return robot_type_list

    def handle_rosbag_recording(self):
        try:
            current = self.data_manager.get_status()
            previous = self.previous_data_manager_status

            # Early return if no status change
            if current == previous:
                return

            handlers = {
                ('*', 'warmup'): self._handle_warmup_transition,
                ('*', 'run'): self._handle_run_transition,
                ('run', 'save'): self._handle_save_transition,
                ('run', 'stop'): self._handle_stop_transition,
                ('*', 'finish'): self._handle_finish_transition,
                ('run', 'reset'): self._handle_reset_transition,
            }

            # Try exact match first, then wildcard match
            handler = handlers.get((previous, current)) or handlers.get(('*', current))

            if handler:
                handler(previous)

            self.previous_data_manager_status = current

        except PhysicalAIServer.RosbagNotReadyException as e:
            # Expected condition: rosbag not ready yet
            self.get_logger().warn(str(e))
        except Exception as e:
            error_msg = f'Error in rosbag recording: {str(e)}'
            self.get_logger().error(traceback.format_exc())
            self.get_logger().error(error_msg)

    def _handle_warmup_transition(self, previous_status: str):
        self.get_logger().info('Preparing rosbag recording, '
                               f'previous status: {previous_status}')

        rosbag_topics = self.communicator.get_all_topics()
        self.communicator.prepare_rosbag(topics=rosbag_topics)

    def _handle_run_transition(self, previous_status: str):
        self.get_logger().info('Starting rosbag recording, '
                               f'previous status: {previous_status}')

        rosbag_path = self.data_manager.get_save_rosbag_path()

        if rosbag_path is None:
            raise PhysicalAIServer.RosbagNotReadyException(
                'Episode buffer not initialized yet, '
                'rosbag recording will start shortly')

        self.communicator.start_rosbag(rosbag_uri=rosbag_path)

    def _handle_save_transition(self, previous_status: str):
        self.get_logger().info('Stopping rosbag recording(save), '
                               f'previous status: {previous_status}')
        self.communicator.stop_rosbag()

    def _handle_stop_transition(self, previous_status: str):
        self.get_logger().info('Stopping rosbag recording(stop), '
                               f'previous status: {previous_status}')
        self.communicator.stop_rosbag()

    def _handle_finish_transition(self, previous_status: str):
        self.get_logger().info('Finishing rosbag recording, '
                               f'previous status: {previous_status}')
        self.communicator.finish_rosbag()

    def _handle_reset_transition(self, previous_status: str):
        self.get_logger().info(
                'Stopping rosbag recording and delete recorded bag, '
                f'previous status: {previous_status}')
        self.communicator.stop_and_delete_rosbag()

    def _data_collection_timer_callback(self):
        error_msg = ''
        current_status = TaskStatus()
        camera_msgs, follower_msgs, leader_msgs = self.communicator.get_latest_data()
        # Throttle the "waiting for X data" info logs to once per second
        # per source. At 30Hz timer firing this used to print 90
        # lines/sec while a topic was lagging (audit §3.20).
        now = time.perf_counter()
        if not hasattr(self, '_last_waiting_log'):
            self._last_waiting_log = {}
        # Audit F17 (re-armed): camera-fps sanity check every 5 s during
        # recording. Was one-shot at +1.5 s; that missed mid-recording
        # USB starvation / thermal throttling / hub contention, and the
        # dataset silently grew with the same frame repeated N times
        # (`camera_topic_msgs[name]` overwrite-in-place is what feeds
        # convert_msgs_to_raw_datas — a stale cache reads the same
        # CompressedImage twice). Strobing dataset trains a jittery ACT
        # policy. 5 s cadence + 3 s observation window catches typical
        # degradations within the same episode and re-fires the German
        # warning, while staying out of the way of momentary jitter.
        last_check_t = getattr(self, '_camera_fps_last_check_t', 0.0)
        now_t = time.perf_counter()
        if (
            self.start_recording_time > 0
            and (now_t - self.start_recording_time) > 1.5
            and (now_t - last_check_t) > 5.0
        ):
            self._camera_fps_last_check_t = now_t
            try:
                target_fps = float(getattr(self.task_info, 'fps', 0) or 0)
                if target_fps > 0 and hasattr(self.communicator, 'get_camera_observed_hz'):
                    for cam_name in self.communicator.camera_topic_msgs.keys():
                        observed = self.communicator.get_camera_observed_hz(cam_name, 3.0)
                        if observed is not None and observed < target_fps * 0.8:
                            warning = (
                                f'Kamera "{cam_name}" liefert nur '
                                f'{observed:.1f} Hz, Aufnahme erwartet '
                                f'{target_fps:.0f} Hz. Datensatz enthaelt '
                                f'wiederholte Frames — bitte Aufloesung '
                                f'reduzieren oder Kabel pruefen.'
                            )
                            self.get_logger().warning(warning)
                            try:
                                # Stash on data_manager so the status
                                # publisher surfaces it as a banner.
                                if self.data_manager is not None:
                                    self.data_manager._last_warning_message = warning
                            except Exception:
                                pass
            except Exception as e:
                self.get_logger().warning(f'camera fps check failed: {e}')
        def _log_waiting(source: str, msg: str) -> None:
            last = self._last_waiting_log.get(source, 0.0)
            if now - last > 1.0:
                self.get_logger().info(msg)
                self._last_waiting_log[source] = now

        # Topic-availability gates: when a stream is missing we either
        # wait (still inside DEFAULT_TOPIC_TIMEOUT) or hard-fail. Falling
        # through with a None message used to call convert_msgs_to_raw_datas
        # with image_msgs=None — convert is None-safe, but check_lerobot_dataset
        # would then create the dataset object with NO observation.images.*
        # features at all, permanently corrupting `self._lerobot_dataset`
        # for the rest of the session. Always halt the tick once we set
        # error_msg, then surface it via TaskStatus below.
        def _missing_or_wait(source: str, label: str) -> str | None:
            if now - self.start_recording_time > self.DEFAULT_TOPIC_TIMEOUT:
                msg = f'{label} data not received within timeout period'
                self.get_logger().error(msg)
                return msg
            _log_waiting(source, f'Waiting for {source} data...')
            return ''  # signal "still waiting, skip this tick"

        if camera_msgs is None:
            error_msg = _missing_or_wait('camera', 'Camera')
            if not error_msg:
                return
        elif follower_msgs is None:
            error_msg = _missing_or_wait('follower', 'Follower')
            if not error_msg:
                return
        elif leader_msgs is None:
            error_msg = _missing_or_wait('leader', 'Leader')
            if not error_msg:
                return

        if error_msg:
            self.on_recording = False
            current_status.phase = TaskStatus.READY
            current_status.error = error_msg
            self.communicator.publish_status(status=current_status)
            self.timer_manager.stop(timer_name=self.operation_mode)
            return

        try:
            camera_data, follower_data, leader_data = self.data_manager.convert_msgs_to_raw_datas(
                camera_msgs,
                follower_msgs,
                self.total_joint_order,
                leader_msgs,
                self.joint_order)

        except Exception as e:
            error_msg = f'Failed to convert messages: {str(e)}, please check the robot type again!'
            self.on_recording = False
            current_status.phase = TaskStatus.READY
            current_status.error = error_msg
            self.communicator.publish_status(status=current_status)
            self.timer_manager.stop(timer_name=self.operation_mode)
            return

        if not self.data_manager.check_lerobot_dataset(
                camera_data,
                self.total_joint_order):
            # check_lerobot_dataset writes a specific German warning to
            # _last_warning_message when the failure is a camera-name
            # mismatch against a resumed dataset. Prefer that over the
            # generic English fallback so the student sees something
            # actionable (which cameras mismatch, what the expected names
            # were) instead of being misdirected toward repo-name issues.
            specific = getattr(self.data_manager, '_last_warning_message', '')
            if specific:
                error_msg = specific
                # Consume so it isn't re-surfaced by the next
                # get_current_record_status() tick.
                self.data_manager._last_warning_message = ''
            else:
                error_msg = 'Invalid repository name, Please change the repository name'
            self.get_logger().info(error_msg)

        if error_msg:
            self.on_recording = False
            current_status.phase = TaskStatus.READY
            current_status.error = error_msg
            self.communicator.publish_status(status=current_status)
            self.timer_manager.stop(timer_name=self.operation_mode)
            return

        if self.communicator.joystick_state['updated']:
            self.handle_joystick_trigger(
                joystick_mode=self.communicator.joystick_state['mode'])
            self.communicator.joystick_state['updated'] = False

        record_completed = self.data_manager.record(
            images=camera_data,
            state=follower_data,
            action=leader_data)

        current_status = self.data_manager.get_current_record_status()
        self.communicator.publish_status(status=current_status)

        if self.data_manager.should_record_rosbag2():
            self.handle_rosbag_recording()

        if record_completed:
            self.get_logger().info('Recording completed')
            current_status.phase = TaskStatus.READY
            current_status.proceed_time = int(0)
            current_status.total_time = int(0)
            self.communicator.publish_status(status=current_status)
            self.on_recording = False
            self.timer_manager.stop(timer_name=self.operation_mode)
            return

    def _eager_load_policy(self):
        """Background policy load kicked by START_INFERENCE.

        Plain daemon thread on purpose: load_policy() is pure torch/file
        I/O (no rclpy calls inside), so it needs no executor thread — and
        must not occupy one for 10-30 s. The FPS timer skips its tick while
        ``_inference_policy_loading`` is set; because the flag clears only
        AFTER the failure teardown below, the timer's lazy-load fallback and
        predict() can never race a failed/half-loaded policy.
        """
        try:
            ok = self.inference_manager.load_policy()
        except Exception as e:  # defensive — load_policy logs its own errors
            self.get_logger().error(f'Eager policy load crashed: {e}')
            ok = False
        # Teardown-before-flag-clear: while the flag is set the timer keeps
        # skipping ticks, so this teardown cannot race the lazy-load fallback
        # or a predict() against a half-loaded policy (load_policy may set
        # self.policy and then fail building the processors). The finally
        # guarantees the flag ALWAYS clears — a teardown exception must not
        # freeze the loading UI forever.
        try:
            if not ok and self.on_inference:
                self.get_logger().error('Eager policy load failed')
                current_status = self.data_manager.get_current_record_status()
                current_status.phase = TaskStatus.READY
                current_status.error = (
                    'Das Modell konnte nicht geladen werden. Bitte Modellpfad '
                    'prüfen und erneut versuchen.'
                )
                self.on_inference = False
                self.on_recording = False
                self.communicator.publish_status(status=current_status)
                self.inference_manager.clear_policy()
                self.timer_manager.stop(timer_name=self.operation_mode)
        finally:
            self._inference_policy_loading = False

    def _inference_timer_callback(self):
        if self._inference_policy_loading:
            # Eager load still in flight — keep the React side on the
            # German "Modell wird geladen…" state instead of silently
            # waiting on the lazy load with a stale phase.
            current_status = self.data_manager.get_current_record_status()
            current_status.phase = self.PHASE_INFERENCE_LOADING
            self.communicator.publish_status(status=current_status)
            return

        error_msg = ''
        current_status = TaskStatus()
        camera_msgs, follower_msgs, _ = self.communicator.get_latest_data()
        if (camera_msgs is None or
                len(camera_msgs) != len(self.params['camera_topic_list'])):
            self.get_logger().info('Waiting for camera data...')
            return
        elif follower_msgs is None:
            self.get_logger().info('Waiting for follower data...')
            return

        try:
            camera_data, follower_data, _ = self.data_manager.convert_msgs_to_raw_datas(
                camera_msgs,
                follower_msgs,
                self.total_joint_order)
        except Exception as e:
            error_msg = (
                f'Failed to convert messages ({str(e)}). '
                f'Please check the robot type again!'
            )
            self.on_inference = False
            current_status.phase = TaskStatus.READY
            current_status.error = error_msg
            self.communicator.publish_status(status=current_status)
            self.inference_manager.clear_policy()
            self.timer_manager.stop(timer_name=self.operation_mode)
            return

        if self.inference_manager.policy is None:
            if not self.inference_manager.load_policy():
                self.get_logger().error('Failed to load policy')
                return

        try:
            if not self.on_inference:
                self.get_logger().info('Inference mode is not active')
                current_status = self.data_manager.get_current_record_status()
                current_status.phase = TaskStatus.READY
                self.communicator.publish_status(status=current_status)
                self.inference_manager.clear_policy()
                self.timer_manager.stop(timer_name=self.operation_mode)
                return

            action = self.inference_manager.predict(
                images=camera_data,
                state=follower_data,
                task_instruction=self.task_instruction[0]
            )

            self.get_logger().info(
                f'Action data: {action}')

            action_pub_msgs = self.data_manager.data_converter.tensor_array2joint_msgs(
                action,
                self.joint_topic_types,
                self.joint_order
            )

            self.communicator.publish_action(
                joint_msg_datas=action_pub_msgs
            )
            current_status = self.data_manager.get_current_record_status()
            current_status.phase = TaskStatus.INFERENCING
            self.communicator.publish_status(status=current_status)

        except Exception as e:
            self.get_logger().error(f'Inference failed, please check : {str(e)}')
            error_msg = f'Inference failed, please check : {str(e)}'
            self.on_recording = False
            self.on_inference = False
            current_status = self.data_manager.get_current_record_status()
            current_status.phase = TaskStatus.READY
            current_status.error = error_msg
            self.communicator.publish_status(status=current_status)
            self.inference_manager.clear_policy()
            self.timer_manager.stop(timer_name=self.operation_mode)
            return

    def user_training_interaction_callback(self, request, response):
        """Cloud-only stub (EduBotics v2.5.0).

        On-device training was removed because all classroom training runs on
        Modal cloud workers. Returns success=False with a German operator-
        facing message so the React UI surfaces a clear error if a legacy
        client still calls this service.

        SendTrainingCommand.Request.START and .FINISH both reject with the
        same message — the React UI's "Training starten" / "Training stoppen"
        buttons should be disabled / re-pointed at the Cloud Web UI.
        """
        response.success = False
        response.message = (
            'Training läuft nur in der Cloud — bitte Web-UI nutzen.'
        )
        return response

    def user_interaction_callback(self, request, response):
        try:
            if request.command == SendCommand.Request.START_RECORD:
                if self.on_recording:
                    self.get_logger().info('Restarting the recording.')
                    self.data_manager.re_record()
                    response.success = True
                    response.message = 'Restarting the recording.'
                    return response

                # F1 — atomic claim: hold _mode_lock around the ownership check AND
                # the on_recording set so a concurrent manual / inference / workflow
                # start can't slip in between (two writers on the arm). Set
                # on_recording BEFORE the collection timer spawns (inside
                # init_robot_control_parameters); reset it if that init raises.
                # _mode_lock is RELEASED before the init so it never nests
                # _manual_lock/motion_lock (no lock-ordering deadlock).
                with self._mode_lock:
                    ok, msg = self._assert_no_other_active('recording')
                    if not ok:
                        response.success = False
                        response.message = msg
                        return response
                    self.on_recording = True

                self.get_logger().info('Start recording')
                self.operation_mode = 'collection'
                task_info = request.task_info
                try:
                    self.init_robot_control_parameters_from_user_task(
                        task_info
                    )
                except Exception:
                    # Init failed → release the claim so the arm isn't wedged.
                    self.on_recording = False
                    raise

                self.start_recording_time = time.perf_counter()
                # Audit F17 (re-armed): zero the throttle timestamp so
                # the first 5 s check window starts fresh per episode.
                # Legacy `_camera_fps_checked` kept for back-compat with
                # any other reader; new logic uses _camera_fps_last_check_t.
                self._camera_fps_checked = False
                self._camera_fps_last_check_t = 0.0
                # on_recording already claimed under _mode_lock above.
                # Arm the crash-recovery session marker — RECORDING sessions
                # only (see DataManager._session_marker_enabled).
                self.data_manager._session_marker_enabled = True
                response.success = True
                response.message = 'Recording started'

            elif request.command == SendCommand.Request.START_INFERENCE:
                # F1 — atomic claim under _mode_lock (see START_RECORD). on_inference
                # is set here, BEFORE the pre-flight + eager-load driver; every
                # pre-flight refusal below releases it inline, and the driver-start
                # section resets both flags on any exception. _mode_lock is RELEASED
                # before the pre-flight so it never nests other locks.
                with self._mode_lock:
                    ok, msg = self._assert_no_other_active('inference')
                    if not ok:
                        response.success = False
                        response.message = msg
                        return response
                    self.on_inference = True

                # Everything after the claim runs under ONE guard: a pre-flight
                # refusal returns normally (each releases the claim inline), while
                # ANY exception (a checkpoint read / policy validate / init raising)
                # releases both mode claims + the loading flag before re-raising
                # into the outer handler — so a failed start never leaks on_inference
                # and wedges the arm / UI.
                try:
                    self.joint_topic_types = self.communicator.get_publisher_msg_types()
                    self.operation_mode = 'inference'
                    task_info = request.task_info
                    self.task_instruction = task_info.task_instruction

                    valid_result, result_message = self.inference_manager.validate_policy(
                        policy_path=task_info.policy_path)

                    if not valid_result:
                        self.on_inference = False  # F1 — release the claim on refusal
                        response.success = False
                        response.message = result_message
                        self.get_logger().error(response.message)
                        return response

                    # Pre-flight camera contract (leLab-comparison PR-3): the
                    # checkpoint's config.json names the cameras it was trained
                    # on; predict() would tear the run down mid-flight with a
                    # RuntimeError when one is missing. Reject HERE, before any
                    # timer starts, with a German message naming both sides.
                    # Camera names come from the same params the pipeline keys
                    # its image dict on (communicator.camera_topics).
                    summary = InferenceManager.read_policy_summary(
                        task_info.policy_path)
                    expected_cams = set(summary['expected_cameras'])
                    available_cams = set(
                        parse_topic_list_with_names(
                            self.params['camera_topic_list']).keys()
                    ) if self.params else set()
                    # A language-conditioned policy (SmolVLA/Pi0 family) with an
                    # EMPTY Aufgabenanweisung produces unguided behavior — reject
                    # at START with the fix, mirroring the camera pre-flight.
                    if summary['requires_task'] and not any(
                            s.strip() for s in task_info.task_instruction):
                        self.on_inference = False  # F1 — release the claim on refusal
                        response.success = False
                        response.message = (
                            'Dieses Modell benötigt eine Aufgabenanweisung '
                            '(z. B. „Greife den roten Würfel"). Bitte das Feld '
                            '„Aufgabenanweisung" ausfüllen und erneut starten.'
                        )
                        self.get_logger().error(
                            'Pre-flight: language-conditioned policy started '
                            'without a task instruction')
                        return response

                    missing_cams = expected_cams - available_cams
                    if missing_cams:
                        self.on_inference = False  # F1 — release the claim on refusal
                        response.success = False
                        response.message = (
                            f'Dieses Modell erwartet die Kamera(s) '
                            f'{", ".join(sorted(missing_cams))} — verfügbar '
                            f'{"ist" if len(available_cams) == 1 else "sind"}: '
                            f'{", ".join(sorted(available_cams)) or "keine"}. '
                            f'Bitte Kameras prüfen („Hardware neu erkennen") '
                            f'und erneut starten.'
                        )
                        self.get_logger().error(
                            f'Camera contract pre-flight failed: model expects '
                            f'{sorted(expected_cams)}, available '
                            f'{sorted(available_cams)}')
                        return response

                    self.init_robot_control_parameters_from_user_task(
                        task_info
                    )
                    if task_info.record_inference_mode:
                        self.on_recording = True
                        # This inference session RECORDS — arm the crash marker
                        # like a plain recording session.
                        self.data_manager._session_marker_enabled = True
                    self.inference_manager.reset_policy()
                    self.start_recording_time = time.perf_counter()

                    # Eager policy load (leLab-comparison PR-2): the first load
                    # takes 10-30 s (HF cache read + make_pre_post_processors).
                    # The lazy load inside the FPS timer left the student staring
                    # at a frozen arm with a stale phase the whole time; loading
                    # HERE would be worse — /task/command runs on the default
                    # MutuallyExclusiveCallbackGroup, and a 10-30 s block there
                    # starves the 1 Hz heartbeat + /task/status timers (the
                    # documented "disconnected"-flicker class, see _hf_cb_group).
                    # So: publish INFERENCE_LOADING immediately, load on a daemon
                    # thread, and let the timer skip ticks until the load ends.
                    self._inference_policy_loading = True
                    loading_status = self.data_manager.get_current_record_status()
                    loading_status.phase = self.PHASE_INFERENCE_LOADING
                    self.communicator.publish_status(status=loading_status)
                    threading.Thread(
                        target=self._eager_load_policy, daemon=True
                    ).start()
                except Exception:
                    self.on_inference = False
                    self.on_recording = False
                    self._inference_policy_loading = False
                    raise

                response.success = True
                response.message = 'Inference started'

            elif request.command == getattr(SendCommand.Request, 'HOME_FOLLOWER', 9):
                # EduBotics teleop collision e-stop STEP 1: after the student removed the
                # obstacle, glide the follower to the safe home pose (verified, non-blocking).
                # getattr fallback: a container whose compiled interfaces predate the new srv
                # constant still routes the raw command id (the React side sends the int).
                success, message = self.home_follower()
                response.success = success
                response.message = message

            elif request.command == getattr(SendCommand.Request, 'RESUME_TELEOP', 8):
                # EduBotics teleop collision e-stop STEP 2: resync the follower to the leader
                # and resume (refused until the follower reached home — strict ordering).
                # Handled here (not under the "currently recording" guard below) because a
                # collision sets on_recording=False, so the system is idle when the student
                # clicks "Teleoperation fortsetzen".
                # getattr fallback mirrors HOME_FOLLOWER above: with pre-rebuild compiled
                # interfaces a bare attribute access would raise AttributeError and break
                # /task/command for EVERY command, not just this one.
                success, message = self.resume_teleop()
                response.success = success
                response.message = message

            elif request.command == getattr(SendCommand.Request, 'FORCE_RESUME_TELEOP', 10):
                # EduBotics teleop collision e-stop ESCAPE HATCH (issue #19 finding #4): when
                # the verified safe-home glide cannot complete, the student forces a resync
                # from the follower's CURRENT pose to the leader (proximity-gated) so a stuck
                # collision no longer wedges everything behind the freeze (env restart only).
                # getattr fallback mirrors RESUME_TELEOP/HOME_FOLLOWER above.
                success, message = self.force_resume_teleop()
                response.success = success
                response.message = message

            else:
                if not self.on_recording and not self.on_inference:
                    response.success = False
                    response.message = 'Not currently recording'
                else:
                    if request.command == SendCommand.Request.STOP:
                        self.get_logger().info('Stopping recording')
                        self.data_manager.record_stop()
                        response.success = True
                        response.message = 'Recording stopped'

                    elif request.command == SendCommand.Request.MOVE_TO_NEXT:
                        self.get_logger().info('Moving to next episode')
                        if len(request.task_info.task_instruction) > 1:
                            self.data_manager.record_next_episode()
                        else:
                            self.data_manager.record_early_save()
                        response.success = True
                        response.message = 'Moved to next episode'

                    elif request.command == SendCommand.Request.RERECORD:
                        self.get_logger().info('Re-recording current episode')
                        self.data_manager.re_record()
                        response.success = True
                        response.message = 'Re-recording current episode'

                    elif request.command == SendCommand.Request.FINISH:
                        self.get_logger().info('Terminating all operations')
                        self.data_manager.record_finish()
                        self.on_inference = False
                        response.success = True
                        response.message = 'All operations terminated'

                    elif request.command == SendCommand.Request.SKIP_TASK:
                        self.get_logger().info('Skipping task')
                        self.data_manager.record_skip_task()
                        response.success = True
                        response.message = 'Task skipped successfully'

        except Exception as e:
            self.get_logger().error(f'Error in user interaction: {str(e)}')
            response.success = False
            response.message = f'Error in user interaction: {str(e)}'
            return response
        return response

    def get_robot_types_callback(self, request, response):
        if self.robot_type_list is None:
            self.get_logger().error('Robot type list is not set')
            response.robot_types = []
            response.success = False
            response.message = 'Robot type list is not set'
            return response

        self.get_logger().info(f'Available robot types: {self.robot_type_list}')
        response.robot_types = self.robot_type_list
        response.success = True
        response.message = 'Robot type list retrieved successfully'
        return response

    def get_policy_list_callback(self, request, response):
        policy_list = InferenceManager.get_available_policies()
        if not policy_list:
            self.get_logger().warning('No policies available')
            response.success = False
            response.message = 'No policies available'
        else:
            self.get_logger().info(f'Available policies: {policy_list}')
            response.success = True
            response.message = 'Policy list retrieved successfully'
        response.policy_list = policy_list
        return response

    def get_available_list_callback(self, request, response):
        response.success = True
        response.message = 'Policy and device lists retrieved successfully'
        response.policy_list, response.device_list = TrainingManager.get_available_list()
        return response

    def get_user_list_callback(self, request, response):
        try:
            if not self.DEFAULT_SAVE_ROOT_PATH.exists():
                response.user_list = []
                response.success = False
                response.message = f'Path {self.DEFAULT_SAVE_ROOT_PATH} does not exist.'
                return response

            folder_names = [
                name for name in os.listdir(self.DEFAULT_SAVE_ROOT_PATH)
                if (self.DEFAULT_SAVE_ROOT_PATH / name).is_dir()
            ]

            response.user_list = folder_names
            response.success = True
            response.message = f'Found {len(folder_names)} user(s).'

        except Exception as e:
            response.user_list = []
            response.success = False
            response.message = f'Error: {str(e)}'

        return response

    def get_dataset_list_callback(self, request, response):
        user_id = request.user_id
        user_path = self.DEFAULT_SAVE_ROOT_PATH / user_id

        try:
            if not user_path.exists() or not user_path.is_dir():
                response.dataset_list = []
                response.success = False
                response.message = f"User ID '{user_id}' does not exist at path: {user_path}"
                return response

            dataset_names = [
                name for name in os.listdir(user_path)
                if (user_path / name).is_dir()
            ]

            response.dataset_list = dataset_names
            response.success = True
            response.message = f"Found {len(dataset_names)} dataset(s) for user '{user_id}'."

        except Exception as e:
            self.get_logger().error(f'get_dataset_list failed: {e}')
            response.dataset_list = []
            response.success = False
            # Audit §3.21 — sanitize.
            response.message = 'Datensatz-Liste konnte nicht gelesen werden.'

        return response

    def get_model_weight_list_callback(self, request, response):
        save_root_path = TrainingManager.get_weight_save_root_path()
        try:
            if not save_root_path.exists():
                response.success = False
                response.message = f'Path does not exist: {save_root_path}'
                response.model_weight_list = []
                return response

            model_folders = [
                f.name for f in save_root_path.iterdir()
                if f.is_dir()
            ]

            response.success = True
            response.message = f'Found {len(model_folders)} model weights'
            response.model_weight_list = model_folders

        except Exception as e:
            response.success = False
            response.message = f'Error: {str(e)}'
            response.model_weight_list = []

        return response

    def get_saved_policies_callback(self, request, response):
        saved_policy_path, saved_policy_type = InferenceManager.get_saved_policies()
        if not saved_policy_path and not saved_policy_type:
            self.get_logger().warning('No saved policies found')
            response.saved_policy_path = []
            response.saved_policy_type = []
            response.success = False
            response.message = 'No saved policies found'
        else:
            self.get_logger().info(f'Saved policies path: {saved_policy_path}')
            response.saved_policy_path = saved_policy_path
            response.saved_policy_type = saved_policy_type
            response.success = True
            response.message = 'Saved policies retrieved successfully'
        return response

    def get_training_info_callback(self, request, response):
        """
        Retrieve training configuration from a saved model.

        Loads configuration from train_config.json and populates TrainingInfo message.
        """
        try:
            # Validate request
            if not request.train_config_path:
                response.success = False
                response.message = 'train_config_path is required'
                return response

            # Clean up path (remove leading/trailing whitespace)
            train_config_path = request.train_config_path.strip()
            weight_save_root_path = TrainingManager.get_weight_save_root_path()
            config_path = weight_save_root_path / train_config_path

            # Check if config file exists
            if not config_path.exists():
                response.success = False
                response.message = f'Model config file not found: {config_path}'
                return response

            # Load and parse configuration
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)

                self.get_logger().info(f'Successfully loaded config from: {config_path}')

                # Populate TrainingInfo message from config
                training_info = response.training_info

                # Dataset configuration
                dataset_config = config_data.get('dataset', {})
                training_info.dataset = dataset_config.get('repo_id', '')

                # Policy configuration
                policy_config = config_data.get('policy', {})
                training_info.policy_type = policy_config.get('type', '')
                training_info.policy_device = policy_config.get('device', 'cuda')

                # Output directory (extract folder name)
                output_dir = config_data.get('output_dir', '')
                if output_dir:
                    training_info.output_folder_name = Path(output_dir).name
                else:
                    training_info.output_folder_name = ''

                # Training parameters with defaults
                training_info.seed = config_data.get('seed', 1000)
                training_info.num_workers = config_data.get('num_workers', 4)
                training_info.batch_size = config_data.get('batch_size', 8)
                training_info.steps = config_data.get('steps', 100000)
                training_info.eval_freq = config_data.get('eval_freq', 20000)
                training_info.log_freq = config_data.get('log_freq', 200)
                training_info.save_freq = config_data.get('save_freq', 1000)

                response.success = True
                response.message = \
                    f'Training configuration loaded successfully from {train_config_path}'

            except json.JSONDecodeError as e:
                response.success = False
                response.message = f'Invalid JSON in config file: {str(e)}'
                return response
            except KeyError as e:
                response.success = False
                response.message = f'Missing required field in config: {str(e)}'
                return response

        except Exception as e:
            self.get_logger().error(f'Error in get_training_info_callback: {str(e)}')
            response.success = False
            response.message = f'Failed to retrieve training info: {str(e)}'

        return response

    def set_robot_type_callback(self, request, response):
        try:
            self.get_logger().info(f'Setting robot type to: {request.robot_type}')
            self.operation_mode = 'collection'
            self.robot_type = request.robot_type
            self.clear_parameters()
            self.init_ros_params(self.robot_type)
            response.success = True
            response.message = f'Robot type set to {self.robot_type}'
            return response

        except Exception as e:
            self.get_logger().error(f'Failed to set robot type: {str(e)}')
            response.success = False
            # Audit §3.21 — sanitize.
            response.message = 'Roboter-Typ konnte nicht gesetzt werden.'
            return response

    def _init_hf_api_worker(self):
        """Initialize HF API Worker and status monitoring timer."""
        # Publisher is created eagerly + once, so the synthetic Failed
        # emit path in _enqueue_dataset_upload always has a target —
        # even when the worker fails to start. Idempotent across re-init
        # cycles (post-idle-shutdown, post-cancel) because we only
        # create when the attribute is missing.
        if not hasattr(self, 'hf_status_publisher') or self.hf_status_publisher is None:
            self.hf_status_publisher = self.create_publisher(
                HFOperationStatus,
                '/huggingface/status',
                self.PUB_QOS_SIZE
            )
        try:
            self.hf_api_worker = HfApiWorker()
            if self.hf_api_worker.start():
                self.get_logger().info('HF API Worker started successfully')
                # Initialize idle count
                self._hf_idle_count = 0
                # Initialize status monitoring timer
                self.hf_status_timer = TimerManager(node=self)
                self.hf_status_timer.set_timer(
                    timer_name='hf_status',
                    timer_frequency=2.0,
                    callback_function=self._hf_status_timer_callback
                )
                self.hf_status_timer.start(timer_name='hf_status')
            else:
                self.get_logger().error('Failed to start HF API Worker')
        except Exception as e:
            self.get_logger().error(f'Error initializing HF API Worker: {str(e)}')

    def _hf_status_timer_callback(self):
        """Timer callback to check HF API Worker status and publish updates."""
        if self.hf_api_worker is None:
            return
        try:
            status = self.hf_api_worker.check_task_status()
            self._publish_hf_operation_status_msg(status)

            # Log status changes (avoid spamming logs)
            last_status = self._last_hf_status.get('status', 'Unknown') \
                if hasattr(self, '_last_hf_status') else 'Unknown'
            current_status = status.get('status', 'Unknown')

            if hasattr(self, '_last_hf_status') and last_status != current_status:
                self.get_logger().info(f'HF API Status changed: {last_status} -> {current_status}')

            self._last_hf_status = status
            # Idle status count and automatic shutdown
            if status.get('status', 'Unknown') == 'Idle':
                self._hf_idle_count = getattr(self, '_hf_idle_count', 0) + 1
                if self._hf_idle_count >= 5:
                    self.get_logger().info(
                        'HF API Worker idle for 5 cycles, shutting down worker and timer.')
                    self._cleanup_hf_api_worker()
            else:
                self._hf_idle_count = 0
        except Exception as e:
            self.get_logger().error(f'Error in HF status timer callback: {str(e)}')

    def _publish_synthetic_upload_failed(self, repo_id, german_message):
        """Publish a synthetic HFOperationStatus(Failed) for the auto-upload path.

        Used when _enqueue_dataset_upload bails BEFORE the worker
        starts the task (worker dead, busy, send_request False). Without
        this, the React side has no /huggingface/status event to render
        a toast on AND never fires registerDataset — so the student
        thinks the recording was uploaded when it wasn't. The message
        body is German; the React subscriber at useRosTopicSubscription
        :520-521 displays it verbatim via toast.error(message).
        """
        try:
            self._publish_hf_operation_status_msg({
                'operation': 'upload',
                'status': 'Failed',
                'repo_id': repo_id or '',
                'local_path': '',
                'message': german_message,
                'progress': {'current': 0, 'total': 0, 'percentage': 0.0},
            })
        except Exception as e:
            self.get_logger().error(
                f'Failed to publish synthetic upload-failed status: {e}')

    def _enqueue_dataset_upload(self, repo_id, local_dir, private=True):
        """Enqueue the end-of-recording dataset push into HfApiWorker.

        Wired into DataManager via the ``upload_callback`` constructor
        argument. ``private`` carries the student's "Privater Modus"
        choice (TaskInfo.private_mode) through to HfApiWorker, which
        creates the HF repo with that visibility; it defaults True so a
        missing flag fails safe to a private repo. Restarts the worker
        if it auto-shut-down idle, then sends a standard 'upload'
        request. Status events flow back via
        /huggingface/status, which (a) renders German toasts in the
        React UI on failure and (b) triggers the React side to call
        /datasets/register on the Cloud API so Modal training can
        discover the dataset. Closes the "record → upload → train"
        seam that was previously broken because the recording path
        called push_to_hub directly and bypassed the worker / status
        publisher entirely.

        Every bail path publishes a synthetic Failed status so the
        student gets a German toast — silent skip would let "Aufnahme
        abgeschlossen" lie to them.
        """
        try:
            if self.hf_api_worker is None or not self.hf_api_worker.is_alive():
                self.get_logger().info(
                    'HF API Worker not running, restarting for auto-upload...')
                self._init_hf_api_worker()

            if self.hf_api_worker is None or not self.hf_api_worker.is_alive():
                self.get_logger().error(
                    f'HF API Worker failed to start; auto-upload skipped: {repo_id}')
                self._publish_synthetic_upload_failed(
                    repo_id,
                    'Automatischer Upload fehlgeschlagen: HF-Worker konnte '
                    'nicht gestartet werden. Bitte über "Datensatz '
                    'bearbeiten" manuell hochladen.',
                )
                return

            if self.hf_api_worker.is_busy():
                self.get_logger().warning(
                    f'HF API Worker busy; auto-upload skipped: {repo_id}')
                self._publish_synthetic_upload_failed(
                    repo_id,
                    'Automatischer Upload übersprungen: Ein anderer HF-Vorgang '
                    'läuft gerade. Bitte über "Datensatz bearbeiten" manuell '
                    'hochladen, wenn er beendet ist.',
                )
                return

            request_data = {
                'mode': 'upload',
                'repo_id': repo_id,
                'local_dir': local_dir,
                'repo_type': 'dataset',
                'author': '',
                'private': bool(private),
            }
            if self.hf_api_worker.send_request(request_data):
                self.get_logger().info(f'Auto-upload enqueued: {repo_id}')
            else:
                self.get_logger().error(
                    f'Failed to enqueue auto-upload: {repo_id}')
                self._publish_synthetic_upload_failed(
                    repo_id,
                    'Automatischer Upload fehlgeschlagen: HF-Worker hat die '
                    'Anfrage abgelehnt. Bitte über "Datensatz bearbeiten" '
                    'manuell hochladen.',
                )
        except Exception as e:
            self.get_logger().error(
                f'Error enqueuing auto-upload for {repo_id}: {str(e)}')
            self._publish_synthetic_upload_failed(
                repo_id,
                f'Automatischer Upload fehlgeschlagen: {e}. Bitte über '
                f'"Datensatz bearbeiten" manuell hochladen.',
            )

    def _publish_hf_operation_status_msg(self, status):
        status_msg = HFOperationStatus()
        status_msg.operation = status.get('operation', 'Unknown')
        status_msg.status = status.get('status', 'Unknown')
        status_msg.repo_id = status.get('repo_id', '')
        status_msg.local_path = status.get('local_path', '')
        status_msg.message = status.get('message', '')

        progress_progress = status.get('progress', {})

        status_msg.progress_current = progress_progress.get('current', 0)
        status_msg.progress_total = progress_progress.get('total', 0)
        status_msg.progress_percentage = progress_progress.get('percentage', 0.0)

        # self.get_logger().info(f'HF API Status: {status_msg}')
        self.hf_status_publisher.publish(status_msg)

    def control_hf_server_callback(self, request, response):
        try:
            mode = request.mode
            repo_id = request.repo_id
            local_dir = request.local_dir
            repo_type = request.repo_type
            author = request.author

            if self.hf_cancel_on_progress:
                response.success = False
                response.message = 'HF API Worker is currently canceling'
                return response

            if mode == 'cancel':
                # Immediate cleanup - force stop the worker
                try:
                    self.hf_cancel_on_progress = True
                    self._cleanup_hf_api_worker_with_threading()
                    response.success = True
                    response.message = 'Cancellation started.'
                except Exception as e:
                    self.get_logger().error(f'Error during cancel: {e}')
                finally:
                    self.hf_cancel_on_progress = False
                    return response

            # Restart HF API Worker if it does not exist or is not running
            if self.hf_api_worker is None or not self.hf_api_worker.is_alive():
                self.get_logger().info('HF API Worker not running, restarting...')
                self._init_hf_api_worker()
            # Return error if the worker is busy
            if self.hf_api_worker.is_busy():
                self.get_logger().warning('HF API Worker is currently busy with another task')
                response.success = False
                response.message = 'HF API Worker is currently busy with another task'
                return response
            # Prepare request data for the worker
            request_data = {
                'mode': mode,
                'repo_id': repo_id,
                'local_dir': local_dir,
                'repo_type': repo_type,
                'author': author
            }
            # Send request to HF API Worker
            if self.hf_api_worker.send_request(request_data):
                self.get_logger().info(f'HF API request sent successfully: {mode} for {repo_id}')
                response.success = True
                response.message = f'HF API request started: {mode} for {repo_id}'
            else:
                self.get_logger().error('Failed to send request to HF API Worker')
                response.success = False
                response.message = 'Failed to send request to HF API Worker'
            return response
        except Exception as e:
            self.get_logger().error(f'Error in HF server callback: {str(e)}')
            response.success = False
            response.message = f'Error in HF server callback: {str(e)}'
            return response

    # ------------------------------------------------------------------
    # Roboter Studio — calibration services
    # ------------------------------------------------------------------
    def _assert_no_other_active(self, requested_mode: str) -> tuple[bool, str]:
        """Reject a Roboter Studio request when another mode owns the arm.

        Returns (ok, german_message). `requested_mode` is one of
        'calibration', 'workflow', 'recording', 'inference', 'training',
        'manual' — used only for error message clarity.
        """
        # A collision recovery owns the arm until the student finishes the two-step
        # home→resume flow (which, mid-recording, seamlessly resumes that recording). Block
        # any new operation until then — defense-in-depth behind the non-dismissible
        # CollisionModal, and it keeps the collision watchdog from fighting a new session's
        # /task/status. Cleared implicitly: _collision_active goes False at resync-complete.
        if getattr(self, '_collision_active', False):
            return False, ('Eine Kollision wird gerade behoben — bitte zuerst die Schritte '
                           'im Hinweisfenster abschließen.')
        if self.on_recording:
            return False, 'Aufnahme läuft gerade — bitte zuerst stoppen.'
        if self.on_inference:
            return False, 'Inferenz läuft gerade — bitte zuerst stoppen.'
        if self.is_training:
            return False, 'Training läuft gerade — bitte abwarten oder abbrechen.'
        if requested_mode != 'calibration' and self.on_calibration:
            return False, 'Kalibrierung läuft gerade — bitte zuerst beenden.'
        if requested_mode != 'workflow' and self.on_workflow:
            return False, 'Ein Workflow läuft gerade — bitte zuerst stoppen.'
        # Batch 2b — Handbetrieb (manual jog / hand-guide / record / replay) owns
        # the arm during a manual session. A 'manual' request RELAXES here so
        # capture/jog/record/replay coexist within the one session; every OTHER
        # mode refuses so a recording / inference can't claim the arm mid
        # hand-guide (the follower is LIMP) or mid driven jog/replay.
        if requested_mode != 'manual' and getattr(self, 'on_manual', False):
            return False, 'Handbetrieb ist aktiv — bitte zuerst den Handbetrieb beenden.'
        return True, ''

    # ------------------------------------------------------------------
    # WS1 — teleop arbitration (suspend leader broadcaster during
    # programmatic Roboter Studio motion)
    # ------------------------------------------------------------------
    def _set_calibration_active(self, active: bool) -> None:
        """Set the calibration-session mutex flag.

        Roboter Studio (incl. calibration) runs FOLLOWER-ONLY, so there is no
        leader broadcaster to suspend — the workflow/calibration trajectory
        publisher is the sole writer on /leader/joint_trajectory. The
        single-shot scene-cam calibration also moves the arm zero times. The
        2026-06-17 teleop arbiter that used to bracket this is removed.
        """
        self.on_calibration = active

    def _get_or_create_calibration_manager(self):
        if self.calibration_manager is not None:
            return self.calibration_manager
        try:
            from physical_ai_server.workflow.calibration_manager import CalibrationManager
        except ImportError as e:
            self.get_logger().error(f'Cannot import CalibrationManager: {e}')
            return None
        self.calibration_manager = CalibrationManager(
            get_frame=self._get_latest_camera_frame,
            get_gripper_pose=self._get_current_gripper_pose,
        )
        return self.calibration_manager

    def _get_latest_camera_frame(self, camera: str):
        """Provider hook for the calibration manager + colour profile
        capture. Returns the most recent BGR frame for the named camera,
        or None when no frame has arrived yet / decode fails. Delegates to
        ``Communicator.get_latest_bgr_frame``, which decodes the cached
        ``CompressedImage`` JPEG via cv2.imdecode.
        """
        if self.communicator is None:
            return None
        getter = getattr(self.communicator, 'get_latest_bgr_frame', None)
        if not callable(getter):
            self.get_logger().warning(
                'Communicator has no get_latest_bgr_frame; calibration capture disabled. '
                'Update communicator.py to the post-v1.1 build.'
            )
            return None
        try:
            return getter(camera)
        except Exception as e:
            self.get_logger().warning(f'Camera frame provider error: {e}')
            return None

    def _get_camera_frame_age(self, camera: str):
        """Age (seconds) of the latest frame for the named camera, or None.
        Drives the workflow perception staleness gate (reject a frozen camera)."""
        if self.communicator is None:
            return None
        getter = getattr(self.communicator, 'get_camera_msg_age_s', None)
        if not callable(getter):
            return None
        try:
            return getter(camera)
        except Exception:  # noqa: BLE001 — age is advisory
            return None

    def _get_current_gripper_pose(self):
        """Provider hook for hand-eye calibration. Returns ``(R 3x3, t 3,)``
        of gripper-in-base, or None when joint state hasn't arrived yet or
        FK is unavailable. Computes FK via the same IKSolver instance used
        by the Roboter Studio workflow runtime so the URDF is loaded exactly
        once per server lifetime."""
        if self.communicator is None:
            return None
        joints_getter = getattr(self.communicator, 'get_latest_follower_joints', None)
        if not callable(joints_getter):
            return None
        try:
            joints = joints_getter()
        except Exception as e:
            self.get_logger().warning(f'Follower-joints provider error: {e}')
            return None
        if joints is None:
            return None
        # The closed-form IKSolver is always available + cached once per server
        # lifetime (no URDF fetch), so FK shares the same instance as the
        # workflow IK with no TTL/retry dance.
        ik = self._build_ik_solver()
        if ik is None:
            return None
        try:
            return ik.fk(joints)
        except Exception as e:
            self.get_logger().warning(f'FK call failed: {e}')
            return None

    def calibration_start_callback(self, request, response):
        ok, msg = self._assert_no_other_active('calibration')
        if not ok:
            response.success = False
            response.message = msg
            return response
        manager = self._get_or_create_calibration_manager()
        if manager is None:
            response.success = False
            response.message = 'Kalibrierung kann nicht initialisiert werden.'
            return response
        # Clear any leftover stop flag from a previous cancel so the
        # newly-started step's touch-off capture isn't aborted on its
        # very first tick.
        self._calibration_stop_event.clear()
        # If a previous touch-off session left the follower torqued OFF (the
        # student switched to another step without cancelling), re-torque before
        # starting the new step so the arm is never left limp. If the re-torque
        # persistently fails, REFUSE to start the new step and keep the mutex
        # held + the step marked (so a retry re-attempts the re-torque) rather
        # than proceeding onto — and later releasing the mutex over — a limp arm.
        if (getattr(self, '_active_calib_step', None) == 'table_touch'
                and request.step != 'table_touch'):
            if not self._retorque_follower_or_keep_locked(response):
                return response
        # Touch-off ("Tisch vermessen") is a MANUAL-TEACH step: the student
        # hand-guides the torque-off follower to tap the table. No image, no
        # arm motion driven by us.
        if request.step == 'table_touch':
            success, message = manager.start_table_touch()
            if success:
                # Mark the step ONLY after a successful start: a FAILED start
                # (e.g. extrinsic not yet calibrated) never torqued the follower
                # off, so leaving _active_calib_step at 'table_touch' would
                # wrongly fire the re-torque path on the next /start or /cancel
                # (MEDIUM ride-along). Set it BEFORE the torque-off so a later
                # abort still correctly re-torques the now-limp arm.
                self._active_calib_step = request.step
                if not self._set_follower_torque(False):
                    message += (' [Hinweis: Arm konnte nicht automatisch '
                                'freigeschaltet werden — bitte vorsichtig führen.]')
                self._set_calibration_active(True)
            response.success = success
            response.message = message
            return response
        # Non-touch steps drive no torque side-effect; marking the step
        # unconditionally is safe (and needed by capture_frame/solve routing).
        self._active_calib_step = request.step
        success, message = manager.start_step(request.camera, request.step)
        if success:
            self._set_calibration_active(True)
        response.success = success
        response.message = message
        return response

    def _set_follower_torque(self, enabled: bool) -> bool:
        """Enable/disable follower servo torque via the Dynamixel hardware
        interface (std_srvs/SetBool, ABI-safe). Used by the touch-off step to
        let the student hand-guide the limp arm, then re-torque on finish.
        Returns True on a confirmed switch.

        Held under _dxl_torque_lock so the lazily-created client and the
        call_async are atomic against a concurrent cancel on the reentrant
        preempt group (see the lock's init comment)."""
        with self._dxl_torque_lock:
            try:
                from std_srvs.srv import SetBool
                client = getattr(self, '_dxl_torque_client', None)
                if client is None:
                    from rclpy.callback_groups import ReentrantCallbackGroup
                    client = self.create_client(
                        SetBool, '/dynamixel_hardware_interface/set_dxl_torque',
                        callback_group=ReentrantCallbackGroup())
                    self._dxl_torque_client = client
                if not client.wait_for_service(timeout_sec=2.0):
                    self.get_logger().warning('set_dxl_torque service unavailable.')
                    return False
                req = SetBool.Request()
                req.data = bool(enabled)
                future = client.call_async(req)
                deadline = time.monotonic() + 3.0
                while not future.done() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not future.done():
                    self.get_logger().warning('set_dxl_torque call timed out.')
                    return False
                res = future.result()
                ok = bool(getattr(res, 'success', False)) if res is not None else False
                if ok:
                    # Track the confirmed torque state for the idle watchdog so it
                    # knows whether a limp arm needs re-torquing.
                    self._follower_torque_on = bool(enabled)
                    if enabled:
                        # F5 — a re-torque can spike holding current; arm the
                        # collision settle window so it doesn't trip the leader-
                        # requiring recovery in follower-only mode (getattr-safe if
                        # the collision monitor isn't mixed in).
                        resettle = getattr(self, 'note_collision_resettle', None)
                        if callable(resettle):
                            resettle()
                return ok
            except Exception as e:  # noqa: BLE001
                self.get_logger().warning(f'set_dxl_torque failed: {e!r}')
                return False

    def _retorque_follower_or_keep_locked(self, response) -> bool:
        """Re-assert follower torque after a touch-off that left the arm limp.

        The touch-off torques the follower OFF for hand-guiding; on every
        finish/cancel path the arm MUST be re-torqued before the calibration
        mutex (on_calibration) is released — otherwise a subsequent workflow /
        recording / inference start drives a LIMP follower (the arm_controller
        writes Goal Position but Torque Enable is 0, so the servos ignore it and
        slump under gravity). Nothing else re-asserts follower torque before
        motion (the Rule §2 collision monitor is inference-gated and not on the
        Roboter-Studio path).

        Tries the torque-on switch up to 3 times. On success returns True (the
        caller may then release the mutex). On persistent failure it does NOT
        release the mutex — it leaves on_calibration True so the next mode start
        is refused, sets a German error on ``response``, and returns False. The
        arm is never left limp with the mutex released.
        """
        for _ in range(3):
            if self._set_follower_torque(True):
                return True
        self.get_logger().error(
            'Follower re-torque failed after touch-off — keeping the '
            'calibration mutex held so no motion runs against a limp arm.')
        response.message = (
            'Arm konnte nicht wieder verriegelt werden — der Greifer ist noch '
            'frei beweglich. Bitte die Umgebung neu starten, bevor du '
            'fortfährst.'
        )
        response.success = False
        return False

    def calibration_capture_callback(self, request, response):
        manager = self.calibration_manager
        if manager is None:
            response.success = False
            response.message = 'Kalibrierung wurde nicht gestartet.'
            return response
        # Touch-off capture: record an FK table-contact point (no image).
        if getattr(self, '_active_calib_step', None) == 'table_touch':
            try:
                success, captured, required, message = manager.capture_touch_point()
            except Exception as e:
                self.get_logger().error(f'capture_touch_point raised: {e}')
                response.success = False
                response.frames_captured = 0
                response.frames_required = 0
                response.last_view_rms = 0.0
                response.message = 'Tisch-Punkt konnte nicht erfasst werden.'
                return response
            response.success = success
            response.frames_captured = captured
            response.frames_required = required
            response.last_view_rms = 0.0
            response.message = message
            return response
        # Wrap the manager call so an unhandled solver/cv2 exception
        # surfaces as a German message instead of a Python traceback,
        # and the on_calibration mutex isn't poisoned by an exception
        # path that bypassed every release branch.
        try:
            success, captured, required, last_rms, message = manager.capture_frame(request.camera)
        except Exception as e:
            self.get_logger().error(f'capture_frame raised: {e}')
            response.success = False
            response.frames_captured = 0
            response.frames_required = 0
            response.last_view_rms = 0.0
            response.message = (
                'Kalibrier-Aufnahme fehlgeschlagen. Bitte erneut versuchen.'
            )
            return response
        response.success = success
        response.frames_captured = captured
        response.frames_required = required
        response.last_view_rms = last_rms
        response.message = message
        # Phase-2 wizard UX: surface the 4x4 coverage cell + 1/2/3 quality badge
        # for the just-captured INTRINSIC frame so the React side can fill the
        # coverage mosaic + quality strip. Returns (0, 0) for an extrinsic /
        # touch-off capture or a failed capture (the wizard only consumes these
        # on success). Best-effort: a meta lookup error never fails the capture.
        try:
            cell, quality = manager.last_intrinsic_capture_meta(request.camera)
        except Exception:  # noqa: BLE001
            cell, quality = 0, 0
        response.coverage_cell = int(cell)
        response.quality = int(quality)
        return response

    def calibration_solve_callback(self, request, response):
        manager = self.calibration_manager
        if manager is None:
            response.success = False
            response.message = 'Kalibrierung wurde nicht gestartet.'
            return response
        # Touch-off solve: fit the table plane, then re-torque the follower so
        # it holds again. A FAILED fit (too few points) keeps torque OFF so the
        # student can keep tapping; only success / an exception re-torques.
        # On EITHER terminal path the follower MUST be re-torqued before the
        # mutex is released — _retorque_follower_or_keep_locked keeps the mutex
        # HELD (and surfaces a German error) if it can't, so no later motion
        # ever runs against a limp arm.
        if request.step == 'table_touch':
            try:
                success, residual, message = manager.solve_table_plane(
                    request.camera or 'scene')
            except Exception as e:
                self.get_logger().error(f'solve_table_plane raised: {e}')
                response.reprojection_error = 0.0
                response.method_disagreement = 0.0
                if not self._retorque_follower_or_keep_locked(response):
                    # Mutex stays held; response already carries the German
                    # re-torque-failure message. Keep the step marked so a
                    # later /start re-attempts the re-torque too.
                    return response
                self._set_calibration_active(False)
                self._active_calib_step = None
                response.success = False
                response.message = (
                    'Tischvermessung fehlgeschlagen. Bitte erneut starten.'
                )
                return response
            response.reprojection_error = float(residual)
            response.method_disagreement = 0.0
            if success:
                if not self._retorque_follower_or_keep_locked(response):
                    # Plane WAS measured + persisted, but the arm is still limp
                    # — refuse the success so the student restarts before any
                    # motion. The mutex stays held.
                    response.reprojection_error = float(residual)
                    return response
                self._set_calibration_active(False)
                self._active_calib_step = None
                # W1 — the touch-off plane solve is the FINAL step of the full
                # calibration flow; its success marks the rig as recalibrated for
                # this boot, so the force-recalib gate in
                # calibration_status_callback stops hiding the on-disk YAMLs.
                self._recalibrated_this_boot = True
            response.success = success
            response.message = message
            return response

        # Same guard as capture_frame: an unhandled solver/numpy/cv2
        # exception used to leave on_calibration stuck True forever.
        # Force-release on the exception path so the student can retry
        # from a clean state.
        try:
            success, reproj, disagreement, message = manager.solve(request.camera, request.step)
        except Exception as e:
            self.get_logger().error(f'solve raised: {e}')
            self._set_calibration_active(False)
            response.success = False
            response.reprojection_error = 0.0
            response.method_disagreement = 0.0
            response.message = (
                'Kalibrier-Lösung fehlgeschlagen. Bitte erneut starten und neu erfassen.'
            )
            return response
        response.success = success
        response.reprojection_error = reproj
        response.method_disagreement = disagreement
        response.message = message
        # Release the mutex on every successful solve, not just handeye.
        # Intrinsic-only or handeye-only sessions are valid student flows
        # and leaving on_calibration stuck blocks recording / inference /
        # training when they navigate away (audit §1.3).
        if success:
            self._set_calibration_active(False)
        return response

    def calibration_cancel_callback(self, request, response):
        """Drop in-flight calibration buffers and release the mutex.
        Called by the frontend wizard's cleanup useEffect when the
        student navigates away mid-step. Idempotent — returns success
        even if no step was active. Camera '' (empty) cancels every
        camera; otherwise narrows to the named camera. Also signals
        ``_calibration_stop_event`` so an in-flight touch-off capture
        halts within ≤50 ms instead of running to completion."""
        camera = request.camera if request.camera else None
        # Set BEFORE dropping buffers so a mid-flight chunked_publish
        # observes the stop on its next poll without racing the manager.
        self._calibration_stop_event.set()
        # If a touch-off session was active the follower was torqued OFF for
        # hand-guiding — re-torque it BEFORE releasing the mutex so the arm is
        # never left limp on cancel. If the re-torque persistently fails, keep
        # the mutex held + the step marked (so a later cancel/start retries) and
        # fail loud in German instead of silently releasing onto a limp arm.
        if getattr(self, '_active_calib_step', None) == 'table_touch':
            if not self._retorque_follower_or_keep_locked(response):
                manager = self.calibration_manager
                if manager is not None:
                    try:
                        manager.cancel_step(camera)  # drop buffers; keep mutex
                    except Exception as e:
                        self.get_logger().warning(f'cancel_step raised: {e}')
                return response
        self._active_calib_step = None
        manager = self.calibration_manager
        if manager is not None:
            try:
                _ok, _msg = manager.cancel_step(camera)
            except Exception as e:
                self.get_logger().warning(f'cancel_step raised: {e}')
        self._set_calibration_active(False)
        response.success = True
        response.message = (
            'Kalibrierung abgebrochen.' if camera is None
            else f'Kalibrierung für {camera} abgebrochen.'
        )
        return response

    def calibration_status_callback(self, request, response):
        """Report which calibration artefacts already exist on disk so
        the wizard can show the right per-step badges after a page
        reload. Reads the persisted YAMLs via has_intrinsics /
        has_extrinsic — no manager state required, so works even when the
        server hasn't seen any /calibration/start call yet in its lifetime.

        WS4 (2026-06-17): the gripper camera is no longer calibrated
        (scene-cam-only). The CalibrationStatus.srv still carries the two
        ``has_gripper_*`` fields (no interfaces rebuild), but they are
        reported as ``True`` = "not required / satisfied" so a mixed-version
        frontend that still ANDs them into its gate is never blocked. The
        vestigial ``has_color_profile`` field is likewise reported ``True``
        (the colour-detection workflow was removed — named-object grasping is
        AprilTag-only). The current frontend gate (WorkshopPage.js) requires
        only scene intrinsics + scene extrinsic + the touch-off.
        """
        try:
            from physical_ai_server.workflow.calibration_manager import (
                CalibrationManager,
            )
            mgr = CalibrationManager()
            # Gripper + colour-profile fields are vestigial — always satisfied
            # (not required; colour detection was removed).
            response.has_gripper_intrinsics = True
            response.has_gripper_handeye = True
            response.has_color_profile = True
            response.has_scene_intrinsics = bool(mgr.has_intrinsics('scene'))
            response.has_scene_handeye = bool(mgr.has_extrinsic('scene'))
            # has_table_plane is a post-rebuild field; set it getattr-safe so a
            # pre-rebuild compiled interface (lacking the field) doesn't crash.
            if hasattr(response, 'has_table_plane'):
                response.has_table_plane = bool(mgr.has_table_plane('scene'))
            # W1 — force a FULL recalibration on every fresh container start.
            # While the flag is ON and the touch-off plane solve hasn't completed
            # THIS boot, report the scene calibration as absent (intrinsics +
            # extrinsic + table-plane) so the wizard re-runs the whole flow. The
            # YAMLs are untouched on disk; once the student re-runs the flow to
            # the touch-off, _recalibrated_this_boot latches and the real status
            # is reported for the rest of the session. Resets next restart.
            if self._force_recalib and not self._recalibrated_this_boot:
                response.has_scene_intrinsics = False
                response.has_scene_handeye = False
                if hasattr(response, 'has_table_plane'):
                    response.has_table_plane = False
            response.success = True
            response.message = 'Kalibrier-Status geladen.'
        except Exception as e:
            self.get_logger().warning(f'status read failed: {e}')
            # On a read failure, leave gripper fields satisfied (not the
            # blocker) but mark scene fields incomplete so the wizard shows.
            response.has_gripper_intrinsics = True
            response.has_gripper_handeye = True
            response.has_color_profile = True
            response.has_scene_intrinsics = False
            response.has_scene_handeye = False
            response.success = False
            response.message = 'Status konnte nicht gelesen werden.'
        return response

    def mark_destination_callback(self, request, response):
        """Project a pixel click on a calibrated camera to a base-frame
        point on the table plane."""
        manager = self._get_or_create_calibration_manager()
        if manager is None or not manager.has_intrinsics(request.camera):
            response.success = False
            response.message = (
                f'Kamera {request.camera} ist nicht kalibriert.'
            )
            return response
        try:
            import cv2
            from physical_ai_server.workflow.projection import project_pixel_to_table
            handeye_path = manager._handeye_path(request.camera)
            if not handeye_path.exists():
                response.success = False
                response.message = (
                    f'Hand-Auge-Kalibrierung der {request.camera}-Kamera fehlt.'
                )
                return response
            fs = cv2.FileStorage(str(handeye_path), cv2.FILE_STORAGE_READ)
            T = fs.getNode('transform').mat()
            z_table_node = fs.getNode('z_table')
            # No 0.0 fallback — refuse if the grasp height was never measured.
            z_table = float(z_table_node.real()) if not z_table_node.empty() else None
            # board_table_z = the SURFACE height — the plane the click ray is
            # intersected with to recover (x, y). z_table = the MEASURED grasp
            # height — what move_to/drop_at descend to. Don't conflate them.
            board_z_node = fs.getNode('board_table_z')
            board_table_z = (
                float(board_z_node.real()) if not board_z_node.empty() else None
            )
            fs.release()
            # The grasp height (z_table) is what move_to/drop_at descend to, so a
            # destination is only meaningful once the touch-off has run.
            if z_table is None:
                response.success = False
                response.message = (
                    'Die Tischhöhe ist noch nicht vermessen — bitte zuerst den '
                    'Schritt „Tisch vermessen" abschließen.'
                )
                return response
            K = manager._intrinsics[request.camera]['K']
            dist = manager._intrinsics[request.camera]['dist']
            # Recover (x, y) by intersecting the ray with the table SURFACE
            # (board_table_z), NOT the grasp height — same tilted-camera lateral
            # error fix as perception. Then store z = z_table so the descend is
            # correct. Fall back to z_table for the projection plane only when an
            # old YAML predates board_table_z (better than refusing the click).
            surface_z = board_table_z if board_table_z is not None else z_table
            point = project_pixel_to_table(
                float(request.pixel_x), float(request.pixel_y), K, dist, T,
                surface_z,
            )
            if point is None:
                response.success = False
                response.message = (
                    'Klick konnte nicht auf den Tisch projiziert werden.'
                )
                return response
            # W5 — apply the ground-truth XY correction (identity when the
            # accuracy-verify step was never run) so a pinned destination gets
            # the SAME correction a named grasp does. z / table_plane unchanged.
            corr_x, corr_y = float(point[0]), float(point[1])
            try:
                corr = manager.read_verify_correction(request.camera)
                if corr is not None and corr.get('xy_correction') is not None:
                    from physical_ai_server.workflow.calibration_manager import (
                        CalibrationManager,
                    )
                    corr_x, corr_y = CalibrationManager.apply_xy_correction(
                        (corr_x, corr_y), corr['xy_correction']
                    )
            except Exception as e:
                self.get_logger().warning(
                    f'mark_destination correction skipped: {e}'
                )
            response.success = True
            response.world_x = corr_x
            response.world_y = corr_y
            response.world_z = float(z_table)
            response.message = f'Ziel "{request.label}" gespeichert.'
            # Persist into the WorkflowManager so the next workflow run
            # can resolve "ablegen bei <label>" without a second click.
            try:
                wfm = self._get_or_create_workflow_manager()
                if wfm is not None and request.label:
                    # Store the grasp height (z_table), matching response.world_z
                    # — point[2] is the surface plane used only for (x, y). The
                    # corrected (corr_x, corr_y) is what response carried, so the
                    # persisted destination matches the click feedback exactly.
                    wfm.set_destination(
                        request.label,
                        corr_x, corr_y, float(z_table),
                    )
            except Exception:
                pass
            return response
        except Exception as e:
            self.get_logger().error(f'mark_destination failed: {e}')
            response.success = False
            # Audit §3.21 — generic German, log details server-side.
            response.message = 'Projektion fehlgeschlagen — bitte Kalibrierung prüfen.'
            return response

    def capture_pose_callback(self, request, response):
        """"Position merken" — capture the follower arm's CURRENT base-frame
        gripper position (forward kinematics) and persist it under a name. The
        hand-guide counterpart to ``mark_destination_callback``'s pixel-click
        pinning: same shape (refuse when another mode owns the arm, validate the
        name, persist via ``WorkflowManager.set_destination``), except it reads
        the CURRENT pose instead of projecting a clicked pixel. The FK Z is
        stored VERBATIM — NOT snapped to z_table (product decision) — so a
        student can save a point at whatever height the arm currently holds.
        """
        # Gate: refuse while recording / inference / training / calibration /
        # workflow / collision-recovery owns the arm. 'manual' relaxes only against
        # on_manual, so it refuses on EVERY active owner. F1 — hold _mode_lock
        # around the check for arbiter consistency (this is a read-only FK snapshot
        # that drives NOTHING and claims no flag, so the lock is released
        # immediately; belt-and-suspenders with the mode arbiter).
        with self._mode_lock:
            ok, msg = self._assert_no_other_active('manual')
        if not ok:
            response.success = False
            response.world_x = 0.0
            response.world_y = 0.0
            response.world_z = 0.0
            response.message = msg
            return response

        name = (request.name or '').strip()
        # Reuse the canonical destination-name alphabet (ASCII + ä ö ü ß, 1..40
        # chars) so a captured point and a teacher-pinned point validate
        # identically and the React editor's destination list stays clean.
        try:
            from physical_ai_server.workflow.handlers.destinations import (
                _DESTINATION_NAME_RE,
            )
            valid_name = bool(name) and bool(_DESTINATION_NAME_RE.match(name))
        except Exception:  # noqa: BLE001 — validator import must never crash the service
            # Fail CLOSED: if the shared validator import ever fails, fall back to a
            # local charset check (NOT a bare non-empty test) so name/sentinel
            # injection stays blocked even on the degraded path.
            import re as _re
            valid_name = bool(name) and bool(
                _re.match(r'^[A-Za-zÄÖÜäöüß0-9 _\-]{1,40}$', name)
            )
        if not valid_name:
            response.success = False
            response.world_x = 0.0
            response.world_y = 0.0
            response.world_z = 0.0
            response.message = 'Ungültiger Ziel-Name.'
            return response

        # No robot selected yet → communicator not wired → a distinct hint so
        # the student knows to pick the robot first (vs. a transient missing
        # joint state). Mirrors _get_current_gripper_pose's own None-on-comm
        # guard, surfaced here as its own message.
        if self.communicator is None:
            response.success = False
            response.world_x = 0.0
            response.world_y = 0.0
            response.world_z = 0.0
            response.message = 'Bitte zuerst den Roboter auswählen.'
            return response

        # FIX 3 — refuse a capture against a STALE follower readback (a frozen
        # joint stream): the FK would snapshot an outdated pose and persist a wrong
        # destination. Mirrors the jog / replay staleness guard.
        if self._follower_joints_stale():
            response.success = False
            response.world_x = 0.0
            response.world_y = 0.0
            response.world_z = 0.0
            response.message = ('Aktuelle Position ist noch nicht bekannt — bitte '
                                'kurz warten und erneut versuchen.')
            return response

        # FK read of the current gripper position. Returns None when joint state
        # hasn't arrived yet or the IK/FK solver is unavailable.
        xyz = self._get_current_gripper_xyz()
        if xyz is None:
            response.success = False
            response.world_x = 0.0
            response.world_y = 0.0
            response.world_z = 0.0
            response.message = (
                'Aktuelle Position ist unbekannt — bitte den Roboter bewegen '
                'und erneut versuchen.'
            )
            return response

        x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
        # FIX 3 — a NaN/Inf FK result would persist a garbage destination that a
        # later move_to would drive toward. Refuse rather than store it.
        if not all(math.isfinite(v) for v in (x, y, z)):
            response.success = False
            response.world_x = 0.0
            response.world_y = 0.0
            response.world_z = 0.0
            response.message = ('Aktuelle Position ist ungültig — bitte kurz '
                                'warten und erneut versuchen.')
            return response
        # Persist into the WorkflowManager exactly as mark_destination_callback
        # does (set_destination(name, x, y, z)) so the next workflow run resolves
        # the captured name without a second action. Unlike mark_destination —
        # where the projection result is the primary output and persistence is a
        # best-effort bonus — here persistence IS the operation, so a failure to
        # store fails LOUD rather than reporting a success that wasn't saved.
        try:
            wfm = self._get_or_create_workflow_manager()
            if wfm is None:
                raise RuntimeError('workflow manager unavailable')
            wfm.set_destination(name, x, y, z)
        except Exception as e:
            self.get_logger().error(f'capture_pose persist failed: {e}')
            response.success = False
            response.world_x = 0.0
            response.world_y = 0.0
            response.world_z = 0.0
            response.message = 'Position konnte nicht gespeichert werden.'
            return response

        response.success = True
        response.world_x = x
        response.world_y = y
        response.world_z = z
        response.message = f'Position „{name}" gespeichert.'
        return response

    # ------------------------------------------------------------------
    # Roboter Studio Batch 2b — manual control (jog / hand-guide / record /
    # replay). All four services gate on _assert_no_other_active('manual') +
    # leader_appears_active() and serialize on _manual_lock. Rule §2: these are
    # WORKFLOW-LEVEL teaching features that drive the REAL follower — NOT
    # inference safety envelopes. SAFETY: torque-off (hand-guide/record) opens the
    # on_manual mutex to protect the limp arm; every driving/torque path
    # re-torques fail-loud and keeps the mutex if the re-torque fails.
    # ------------------------------------------------------------------
    def _manual_leader_blocks(self):
        """(blocked, german_message) — True when a leader arm is live (a
        both-arms session), so a follower-only manual op MUST NOT drive
        /leader/joint_trajectory. Mirrors the workflow-start leader gate;
        getattr-guarded so a server without the collision monitor still works."""
        leader_check = getattr(self, 'leader_appears_active', None)
        if not callable(leader_check):
            return False, ''
        try:
            active = leader_check()
        except Exception as e:  # noqa: BLE001 — gate must not block a normal op
            self.get_logger().warning(f'leader_appears_active check failed: {e}')
            return False, ''
        if active:
            return True, (
                'Handbetrieb nicht möglich, solange der Leader-Arm aktiv ist. '
                'Bitte zuerst in den Follower-Modus wechseln (Leader-Schalter im '
                'Roboter-Studio-Tab).'
            )
        return False, ''

    def _recompute_on_manual_locked(self):
        """FIX 1 — recompute the DERIVED on_manual mode flag. MUST be called ONLY
        while holding ``self._mode_lock``.

            INVARIANT: on_manual == (_manual_persistent or _manual_transient_ops > 0)

        on_manual gates the collision detector off (collision_monitor.py) and
        blocks every other mode, so it MUST accurately reflect whether ANY manual
        op owns the arm — a persistent hand-guide/record session OR a transient
        jog/replay still in flight. Never clear on_manual by hand; mutate
        ``_manual_persistent`` / ``_manual_transient_ops`` under ``_mode_lock`` and
        call this."""
        self.on_manual = bool(
            self._manual_persistent or self._manual_transient_ops > 0)

    def _jog_solve_floor(self, ik, target, roll, require_floor=False):
        """Solve a Cartesian jog target with the workspace-floor refusal + reach
        annulus, reusing ``motion._solve_or_raise`` (its German errors) via a
        light ctx-shim. Raises ``motion.WorkflowError`` on a floor violation /
        unreachable target.

        ``require_floor``: when True and the table height is UNKNOWN (no touch-off
        yet: both ``z_table`` and ``table_plane`` absent), REFUSE — ``_solve_or_raise``
        can only enforce the floor when a height is known, so a downward move / a
        drive-to on an uncalibrated rig has no floor and could drive the tool into
        the table. Downward cartesian-Z and ``drive_to`` pass True; joint jog and
        X/Y/roll leave it False (they can't descend past a known floor unaided)."""
        from types import SimpleNamespace
        from physical_ai_server.workflow.handlers import motion as _motion
        calib = {}
        try:
            calib = self._load_workflow_calibration() or {}
        except Exception:  # noqa: BLE001 — floor is advisory; solve still runs
            calib = {}
        z_table = calib.get('z_table')
        table_plane = calib.get('table_plane')
        if require_floor and z_table is None and table_plane is None:
            raise _motion.WorkflowError(
                'Tisch noch nicht vermessen — bitte zuerst „Tisch vermessen" in '
                'der Kalibrierung abschließen, bevor der Arm nach unten oder zu '
                'einem Ziel gefahren wird.')
        shim = SimpleNamespace(
            ik=ik,
            z_table=z_table,
            table_plane=table_plane,
            last_arm_joints=None,
        )
        return _motion._solve_or_raise(shim, target, roll=roll)

    def _compute_jog_target(self, mode, request, joints):
        """Resolve a jog request into the achieved 6-joint vector + the FK world
        (x, y, z). ``joints`` is the LIVE 6-joint follower readback. Clamps/REFUSES
        (never silently clamps into a wall) via a German ``motion.WorkflowError``.
        Modes: joint | cartesian | drive_to (see WorkshopJog.srv)."""
        from physical_ai_server.workflow.handlers import motion as _motion
        from physical_ai_server.workflow.ik_solver import _DXL_JOINT_LIMITS_RAD
        ik = self._build_ik_solver()
        if ik is None:
            raise _motion.WorkflowError(
                'Roboter-Beschreibung nicht verfügbar — der Bewegungsrechner (IK) '
                'konnte nicht gestartet werden. Bitte die Umgebung neu starten.'
            )
        arm = [float(v) for v in joints[:5]]
        grip = float(joints[5])
        mode = (mode or '').strip().lower()

        if mode == 'joint':
            idx = int(request.index)
            delta = float(request.delta)
            if idx < 0 or idx > 5:
                raise _motion.WorkflowError('Ungültiger Gelenk-Index.')
            if idx == 5:
                new = grip + delta
                lo, hi = _motion.GRIPPER_CLOSED_RAD, _motion.GRIPPER_OPEN_RAD
                if new < lo or new > hi:
                    raise _motion.WorkflowError(
                        'Greifer außerhalb des zulässigen Bereichs.')
                q_end = arm + [new]
            else:
                new = arm[idx] + delta
                lo, hi = _DXL_JOINT_LIMITS_RAD[idx]
                if new < lo or new > hi:
                    raise _motion.WorkflowError(
                        f'Gelenk {idx + 1} außerhalb des zulässigen Bereichs.')
                arm2 = list(arm)
                arm2[idx] = new
                q_end = arm2 + [grip]
        elif mode == 'cartesian':
            idx = int(request.index)
            delta = float(request.delta)
            pose = ik.fk(arm)
            if pose is None:
                raise _motion.WorkflowError('Aktuelle Position ist unbekannt.')
            _R, t = pose
            x, y, z = float(t[0]), float(t[1]), float(t[2])
            if idx in (0, 1, 2):
                target = [x, y, z]
                target[idx] += delta
                # A downward Z step (idx 2, delta < 0) needs a known table floor —
                # refuse it on an uncalibrated rig. X/Y and an upward Z are always safe.
                arm_q = self._jog_solve_floor(
                    ik, (target[0], target[1], target[2]), roll=arm[4],
                    require_floor=(idx == 2 and delta < 0.0))
                q_end = list(arm_q) + [grip]
            elif idx == 3:
                new_roll = arm[4] + delta
                lo, hi = _DXL_JOINT_LIMITS_RAD[4]
                if new_roll < lo or new_roll > hi:
                    raise _motion.WorkflowError(
                        'Drehung außerhalb des zulässigen Bereichs.')
                arm2 = list(arm)
                arm2[4] = new_roll
                q_end = arm2 + [grip]
            else:
                raise _motion.WorkflowError(
                    'Nur Bewegung in X, Y, Z und Drehung ist möglich '
                    '(keine Neigung).')
        elif mode == 'drive_to':
            target = (float(request.target_x), float(request.target_y),
                      float(request.target_z))
            # A drive_to descends toward the table — refuse it without a measured floor.
            arm_q = self._jog_solve_floor(
                ik, target, roll=_motion.GRASP_ROLL_RAD, require_floor=True)
            q_end = list(arm_q) + [grip]
        else:
            raise _motion.WorkflowError('Unbekannter Handbetrieb-Modus.')

        world = (0.0, 0.0, 0.0)
        fk = ik.fk(q_end[:5])
        if fk is not None:
            _R2, t2 = fk
            world = (float(t2[0]), float(t2[1]), float(t2[2]))
        return [float(v) for v in q_end], world

    def workshop_jog_callback(self, request, response):
        """"/workshop/jog" — jog the follower one step. Reuses the workflow
        trajectory path (quintic + velocity floor + the SOLE
        /leader/joint_trajectory publisher — NO second publisher). Blocking
        (a bounded, velocity-safe move) on the reentrant preempt group. Seeds from
        LIVE joints each call; clamps/REFUSES out-of-range; torque asserted ON."""
        from physical_ai_server.workflow.handlers.motion import WorkflowError
        from physical_ai_server.workflow.trajectory_builder import (
            build_segment, chunked_publish,
        )
        response.success = False
        response.joints = []
        response.world_x = 0.0
        response.world_y = 0.0
        response.world_z = 0.0
        response.message = ''

        # F6 — refuse a driven jog against a STALE follower readback (a frozen
        # joint stream) so the move never seeds from an old pose. Pre-claim: this
        # gate doesn't need the arbiter.
        if self._follower_joints_stale():
            response.message = ('Armstellung noch nicht bekannt — bitte kurz '
                                'warten und erneut versuchen.')
            return response

        # F1 — atomic claim: hold _mode_lock around the ownership check AND the
        # on_manual claim so a concurrent recording / inference / workflow start
        # can't slip in between (two writers on the arm). FIX 1 — a jog is a
        # TRANSIENT op: it claims a refcount slot (released in the outer finally on
        # completion) instead of the old per-call `opened` boolean, so two
        # overlapping jogs can't have the first clear on_manual out from under the
        # second. FIX 4 — snapshot the exit generation so a „Beenden" that lands
        # after this claim but before we drive aborts us. _mode_lock is RELEASED
        # before _manual_lock / torque, so it never nests them (no deadlock).
        with self._mode_lock:
            ok, msg = self._assert_no_other_active('manual')
            if not ok:
                response.message = msg
                return response
            self._manual_transient_ops += 1
            self._recompute_on_manual_locked()
            exit_gen = self._manual_exit_gen
            # Finding 2 — stamp activity INSIDE the claim lock (atomic with the
            # ops increment) so the idle watchdog's under-_mode_lock idle re-check
            # can never zero this fresh ref while re-torquing an "idle" session.
            self._manual_last_activity_mono = time.monotonic()
        keep_session = False  # set True to retain the ref (limp-arm protection)
        try:
            # Batch 2b: a driven jog while a hand-guide RECORDING is in flight would
            # move the limp arm out from under the student's hand and corrupt the
            # take. Refuse until the student ends the recording (frontend also
            # blocks the buttons).
            if self._manual_record_active:
                response.message = 'Erst die Aufnahme beenden.'
                return response
            blocked, bmsg = self._manual_leader_blocks()
            if blocked:
                response.message = bmsg
                return response
            if self.communicator is None:
                response.message = 'Bitte zuerst den Roboter auswählen.'
                return response

            acquired = self._manual_lock.acquire(timeout=_MANUAL_LOCK_TIMEOUT_S)
            if not acquired:
                response.message = ('Der Arm führt gerade eine andere Bewegung aus '
                                    '— bitte kurz warten.')
                return response
            try:
                # FIX 4 — a „Beenden" (hand_guide(false)) that bumped the exit
                # generation while we waited on _manual_lock means the session was
                # torn down; do NOT drive the (re-torqued) arm after it.
                if self._manual_exit_gen != exit_gen:
                    response.message = 'Handbetrieb wurde beendet.'
                    return response
                # F2 — re-validate the record flag INSIDE _manual_lock: a record
                # start could have grabbed the lock first between our pre-check and
                # here; driving the limp arm now would fight the student's hand.
                if self._manual_record_active:
                    response.message = 'Erst die Aufnahme beenden.'
                    return response
                self._manual_stop_event.clear()
                # Assert torque ON defensively (a prior hand-guide may have left it
                # OFF). F7 — after a bounded run of consecutive failures, clear
                # on_manual so the student is never wedged behind „Handbetrieb".
                if not self._set_follower_torque(True):
                    self._manual_torque_fail_count += 1
                    if self._manual_torque_fail_count >= _MANUAL_TORQUE_FAIL_LIMIT:
                        # FIX 1/4 — EMERGENCY escape: a bounded run of torque-ON
                        # failures would wedge the student behind „Handbetrieb".
                        # Each failed retry is a SEPARATE jog call that leaked its
                        # retained ref (keep_session), so by the limit several refs
                        # have accumulated — absolute-zero is required to escape the
                        # wedge (a single decrement would leave the arm wedged).
                        # keep_session stops the outer finally from double-clearing.
                        # (A concurrent jog that claimed a ref during this window
                        # returns WITHOUT driving on its own torque-ON fail against
                        # the same broken hardware, so zeroing its ref here cannot
                        # produce a live second writer.)
                        with self._mode_lock:
                            self._manual_transient_ops = 0
                            self._manual_persistent = False
                            self._recompute_on_manual_locked()
                        keep_session = True
                        self._manual_torque_fail_count = 0
                        response.message = (
                            'Arm konnte wiederholt nicht verriegelt werden — der '
                            'Handbetrieb wurde zurückgesetzt. Bitte die Umgebung '
                            'neu starten, bevor der Arm weiter bewegt wird.')
                        return response
                    # Keep the ref (protect a possibly-limp arm); retry re-locks on
                    # the next call (bounded by the limit above). The idle watchdog
                    # resets the retained ref if the session is then left idle.
                    keep_session = True
                    response.message = ('Arm konnte nicht verriegelt werden — bitte '
                                        'die Umgebung neu starten.')
                    return response
                self._manual_torque_fail_count = 0  # reset on a successful switch
                joints = None
                try:
                    joints = self.communicator.get_latest_follower_joints()
                except Exception:  # noqa: BLE001 — treat as "pose unknown"
                    joints = None
                if not joints or len(joints) < 6:
                    response.message = ('Aktuelle Armstellung ist noch nicht bekannt '
                                        '— bitte kurz warten und erneut versuchen.')
                    return response
                joints = [float(v) for v in joints[:6]]
                # Non-finite seed guard: a NaN/Inf readback would publish NaN
                # (garbage arm command) or crash build_segment on an Inf delta.
                if not all(math.isfinite(v) for v in joints):
                    response.message = ('Aktuelle Armstellung ist ungültig — bitte '
                                        'kurz warten und erneut versuchen.')
                    return response
                try:
                    q_end, world = self._compute_jog_target(request.mode, request, joints)
                except WorkflowError as e:
                    response.message = str(e)
                    return response
                try:
                    duration = float(request.duration_s)
                except (TypeError, ValueError):
                    duration = 0.0
                if not (duration > 0.0):
                    duration = 1.0
                duration = max(0.2, min(4.0, duration))
                points = build_segment(joints, q_end, duration)
                published = chunked_publish(
                    publisher=self._trajectory_publisher,
                    points=points,
                    should_stop=self._manual_stop_event.is_set,
                )
                if not published:
                    response.message = 'Bewegung abgebrochen.'
                    return response
                response.success = True
                response.joints = [float(v) for v in q_end]
                response.world_x = float(world[0])
                response.world_y = float(world[1])
                response.world_z = float(world[2])
                response.message = 'Bewegung ausgeführt.'
                return response
            finally:
                try:
                    self._manual_lock.release()
                except RuntimeError:
                    pass
        finally:
            # FIX 1 — release this transient jog's ref under the arbiter lock,
            # unless we must retain it to protect a possibly-limp arm (a torque-ON
            # failure left keep_session True; the idle watchdog resets a retained
            # ref if the session is then left idle).
            self._manual_last_activity_mono = time.monotonic()
            if not keep_session:
                with self._mode_lock:
                    self._manual_transient_ops = max(
                        0, self._manual_transient_ops - 1)
                    self._recompute_on_manual_locked()

    def _follower_joints_stale(self) -> bool:
        """True when the latest follower JointState is older than
        _FOLLOWER_JOINT_MAX_AGE_S (F6) — i.e. the joint stream is FROZEN. A None
        age (never arrived / no provider) is NOT stale here; the callers' own
        `not joints` guard covers the never-arrived case. Never raises."""
        comm = getattr(self, 'communicator', None)
        if comm is None:
            return False
        getter = getattr(comm, 'get_follower_joints_age_s', None)
        if not callable(getter):
            return False
        try:
            age = getter()
        except Exception:  # noqa: BLE001 — staleness is advisory
            return False
        return age is not None and age > _FOLLOWER_JOINT_MAX_AGE_S

    def _manual_idle_watchdog_cb(self):
        """Backend idle re-torque watchdog (low-rate safety net). If a Handbetrieb
        session is left open + idle (no manual op) for _MANUAL_IDLE_RETORQUE_S —
        e.g. the student closed the tab during „Arm freischalten", leaving the arm
        limp forever, OR a rare transient-jog claim leaked on_manual — re-torque
        the follower and clear on_manual so the arm is never left limp and no mode
        stays wedged behind „Handbetrieb ist aktiv". NEVER acts during an active
        record or a mid-jog/replay (the _manual_lock is held / _manual_record_active
        is set then). Must never crash the node."""
        try:
            if not getattr(self, 'on_manual', False):
                return
            if getattr(self, '_manual_record_active', False):
                return  # an active recording owns the limp arm — never re-torque under it
            replay = getattr(self, '_manual_replay_thread', None)
            if replay is not None and replay.is_alive():
                return  # a replay is driving
            idle_for = time.monotonic() - getattr(self, '_manual_last_activity_mono', 0.0)
            if idle_for < _MANUAL_IDLE_RETORQUE_S:
                return
            # Grab the manual mutex WITHOUT blocking the executor; if a drive holds
            # it, a manual op is in flight → skip this cycle and retry next tick.
            if not self._manual_lock.acquire(blocking=False):
                return
            try:
                # Re-check under the lock (state may have changed while acquiring).
                if not getattr(self, 'on_manual', False):
                    return
                if getattr(self, '_manual_record_active', False):
                    return
                # Re-torque (idempotent) so a limp arm is re-locked; keep on_manual
                # if it fails so we retry next cycle rather than releasing onto a
                # limp arm. A confirmed-ON torque state needs no switch.
                retorqued = True
                if self._follower_torque_on is not True:
                    retorqued = self._set_follower_torque(True)
                if retorqued:
                    # FIX 1 — BACKSTOP reset: an idle-leaked session (a stranded
                    # persistent open OR a retained transient ref) is fully cleared
                    # under _mode_lock so on_manual can never stay wedged True.
                    # Finding 2 — re-check idle UNDER _mode_lock first: a manual op
                    # may have claimed a ref AND stamped fresh activity (both under
                    # this same lock) after our pre-check above but before we got
                    # here. Only reset a session that is STILL idle so we never zero
                    # a fresh legitimate claim (which would drop on_manual to False
                    # while that op drives → two writers on the arm).
                    reset = False
                    with self._mode_lock:
                        idle = (time.monotonic()
                                - getattr(self, '_manual_last_activity_mono', 0.0))
                        if idle >= _MANUAL_IDLE_RETORQUE_S:
                            self._manual_persistent = False
                            self._manual_transient_ops = 0
                            self._recompute_on_manual_locked()
                            reset = True
                    if reset:
                        self._manual_torque_fail_count = 0
                        self.get_logger().warning(
                            '[Handbetrieb] Sitzung war ungenutzt offen — Arm '
                            'automatisch wieder verriegelt und Handbetrieb beendet.')
            finally:
                try:
                    self._manual_lock.release()
                except RuntimeError:
                    pass
        except Exception as e:  # noqa: BLE001 — watchdog must never crash the node
            self.get_logger().error(f'manual idle watchdog failed: {e}')

    def workshop_hand_guide_callback(self, request, response):
        """"/workshop/hand_guide" — toggle hand-guiding. true → torque the
        follower OFF (limp for hand-guiding) + open the manual session. false →
        abort any in-flight jog/replay, re-torque (fail-loud, keep the ON_MANUAL
        SESSION claimed if it fails — the _manual_lock itself is always released) +
        close the session. false is NOT gated (re-locking must always be
        possible)."""
        response.success = False
        response.message = ''
        enabled = bool(request.enabled)

        if enabled:
            # F1 — atomic claim: hold _mode_lock around the ownership check AND the
            # on_manual claim. FIX 1 — hand-guide opens a PERSISTENT session (it
            # holds the arm limp until an explicit „Beenden"). `was_persistent`
            # remembers the prior value so a refusal RESTORES it (never clobbers an
            # already-open session). _mode_lock is RELEASED before _manual_lock /
            # torque (no lock-ordering deadlock).
            with self._mode_lock:
                ok, msg = self._assert_no_other_active('manual')
                if not ok:
                    response.message = msg
                    return response
                was_persistent = self._manual_persistent
                self._manual_persistent = True
                self._recompute_on_manual_locked()
                # Finding 2 — stamp INSIDE the claim lock so the idle watchdog's
                # under-_mode_lock idle re-check can't clobber this fresh persistent
                # claim (which would re-torque the arm + re-arm the collision
                # detector on a hand-guided arm).
                self._manual_last_activity_mono = time.monotonic()
            blocked, bmsg = self._manual_leader_blocks()
            if blocked:
                with self._mode_lock:
                    self._manual_persistent = was_persistent
                    self._recompute_on_manual_locked()
                response.message = bmsg
                return response
            acquired = self._manual_lock.acquire(timeout=_MANUAL_LOCK_TIMEOUT_S)
            if not acquired:
                with self._mode_lock:
                    self._manual_persistent = was_persistent
                    self._recompute_on_manual_locked()
                response.message = ('Der Arm führt gerade eine andere Bewegung '
                                    'aus — bitte kurz warten.')
                return response
            try:
                # Persistent session already claimed under _mode_lock above.
                if not self._set_follower_torque(False):
                    # Could not make the arm limp; it stays torqued (SAFE, not
                    # limp) → restore the prior persistent state rather than dangle
                    # a session that never opened the arm.
                    with self._mode_lock:
                        self._manual_persistent = was_persistent
                        self._recompute_on_manual_locked()
                    response.message = ('Arm konnte nicht freigeschaltet werden '
                                        '— bitte die Umgebung neu starten.')
                    return response
                response.success = True
                response.message = ('Arm ist frei beweglich — du kannst ihn von '
                                    'Hand führen.')
                return response
            finally:
                try:
                    self._manual_lock.release()
                except RuntimeError:
                    pass

        # enabled == False → exit manual mode / re-lock.
        self._manual_last_activity_mono = time.monotonic()
        # FIX 4 — register the „Beenden" intent BEFORE the re-torque (and before we
        # even block on _manual_lock) so a committed jog/replay that snapshotted the
        # old exit generation aborts even if the re-torque below later fails.
        with self._mode_lock:
            self._manual_exit_gen += 1
        self._manual_stop_event.set()  # abort any in-flight jog/replay drive
        acquired = self._manual_lock.acquire(timeout=6.0)
        if not acquired:
            # A drive is still winding down — it will observe the stop flag and
            # release the lock shortly. Leave the flag SET so it halts; the
            # frontend retries. Never re-torque without the mutex (races the drive).
            response.message = ('Der Arm ist noch in Bewegung — bitte kurz warten '
                                'und erneut „Beenden" drücken.')
            return response
        try:
            # Stop any active recording session first (its timer + limp arm).
            self._manual_record_active = False
            self._cancel_manual_record_cap_timer()  # F4 one-shot no longer needed
            self._manual_record_cap_reached = False
            if self._manual_record_timer is not None:
                try:
                    self.destroy_timer(self._manual_record_timer)
                except Exception:  # noqa: BLE001
                    pass
                self._manual_record_timer = None
            if not self._retorque_follower_or_keep_locked(response):
                # F8 / FIX 1 — keep the PERSISTENT session claimed (no other mode
                # drives a possibly-limp arm). NOTE: _manual_lock is NOT held after
                # this returns — the finally below always releases it; only the
                # persistent session flag is retained. response already carries the
                # German failure message.
                return response
            with self._mode_lock:
                # „Beenden" clears the persistent session. It deliberately does NOT
                # touch _manual_transient_ops: a fresh jog/replay may have claimed a
                # transient ref AFTER the exit-generation bump above — so its snapshot
                # already equals the new generation and its exit-gen guard passes —
                # and zeroing the refcount here would drop on_manual to False while
                # that op then drives → TWO writers on /leader/joint_trajectory (the
                # generation can't distinguish a leaked ref from a fresh live one).
                # A leaked ref only arises from a jog torque-ON-FAIL retain, where
                # on_manual staying True is CORRECT (the arm may be limp); it is
                # cleared safely by the 30 s idle watchdog, which re-checks activity
                # freshness under _mode_lock so it can never clobber a live claim.
                self._manual_persistent = False
                self._recompute_on_manual_locked()
            response.success = True
            response.message = 'Arm ist wieder verriegelt.'
            return response
        finally:
            self._manual_stop_event.clear()
            try:
                self._manual_lock.release()
            except RuntimeError:
                pass

    def workshop_record_callback(self, request, response):
        """"/workshop/record" — start/stop/cancel a hand-guided recording.
        Non-blocking: ``start`` torques OFF and arms a ~25 Hz sampling timer;
        ``stop`` stops it, re-torques (fail-loud) and returns the CONTRACT-B
        points_json; ``cancel`` stops it, re-torques and discards. stop/cancel are
        NOT gated (re-locking must always be possible)."""
        response.success = False
        response.sample_count = 0
        response.duration_s = 0.0
        response.points_json = ''
        response.message = ''
        action = (request.action or '').strip().lower()

        if action == 'start':
            # F1 — atomic claim under _mode_lock (released before _manual_lock /
            # torque). FIX 1 — a record start opens a PERSISTENT session (it holds
            # the arm limp + samples until stop/cancel). `was_persistent` remembers
            # the prior value so a refusal RESTORES it (never clobbers an
            # already-open manual session).
            with self._mode_lock:
                ok, msg = self._assert_no_other_active('manual')
                if not ok:
                    response.message = msg
                    return response
                was_persistent = self._manual_persistent
                self._manual_persistent = True
                self._recompute_on_manual_locked()
                # Finding 2 — stamp INSIDE the claim lock (see hand_guide) so the
                # idle watchdog can't clobber this fresh persistent record claim.
                self._manual_last_activity_mono = time.monotonic()
            blocked, bmsg = self._manual_leader_blocks()
            if blocked:
                with self._mode_lock:
                    self._manual_persistent = was_persistent
                    self._recompute_on_manual_locked()
                response.message = bmsg
                return response
            if self.communicator is None:
                with self._mode_lock:
                    self._manual_persistent = was_persistent
                    self._recompute_on_manual_locked()
                response.message = 'Bitte zuerst den Roboter auswählen.'
                return response
            acquired = self._manual_lock.acquire(timeout=_MANUAL_LOCK_TIMEOUT_S)
            if not acquired:
                with self._mode_lock:
                    self._manual_persistent = was_persistent
                    self._recompute_on_manual_locked()
                response.message = ('Der Arm führt gerade eine andere Bewegung '
                                    'aus — bitte kurz warten.')
                return response
            try:
                if self._manual_record_active:
                    with self._mode_lock:
                        self._manual_persistent = was_persistent
                        self._recompute_on_manual_locked()
                    response.message = 'Es läuft bereits eine Aufnahme.'
                    return response
                # Persistent session already claimed under _mode_lock above.
                if not self._set_follower_torque(False):
                    # Arm stays torqued (SAFE, not limp) → restore prior state.
                    with self._mode_lock:
                        self._manual_persistent = was_persistent
                        self._recompute_on_manual_locked()
                    response.message = ('Arm konnte nicht freigeschaltet werden '
                                        '— bitte die Umgebung neu starten.')
                    return response
                self._handguide_buffer = []
                self._manual_record_cap_reached = False  # fresh recording
                self._manual_record_start_mono = time.monotonic()
                self._manual_record_active = True
                try:
                    self._manual_record_timer = self.create_timer(
                        1.0 / _MANUAL_RECORD_FPS, self._manual_record_sample)
                except Exception as e:  # noqa: BLE001
                    self._manual_record_active = False
                    self.get_logger().error(f'record timer create failed: {e}')
                    if not self._retorque_follower_or_keep_locked(response):
                        return response  # keep the session (arm may be limp)
                    with self._mode_lock:
                        self._manual_persistent = was_persistent
                        self._recompute_on_manual_locked()
                    response.message = 'Aufnahme konnte nicht gestartet werden.'
                    return response
                response.success = True
                response.message = 'Aufnahme gestartet — bewege den Arm von Hand.'
                return response
            finally:
                try:
                    self._manual_lock.release()
                except RuntimeError:
                    pass

        if action in ('stop', 'cancel'):
            self._manual_last_activity_mono = time.monotonic()
            acquired = self._manual_lock.acquire(timeout=6.0)
            if not acquired:
                response.message = ('Der Arm ist noch in Bewegung — bitte kurz '
                                    'warten und erneut versuchen.')
                return response
            try:
                self._manual_record_active = False
                # Cancel the F4 cap-finish one-shot (if the RECORD_MAX_S auto-stop
                # scheduled it) so it doesn't re-torque / re-clear behind us.
                self._cancel_manual_record_cap_timer()
                cap_reached = self._manual_record_cap_reached
                self._manual_record_cap_reached = False
                if self._manual_record_timer is not None:
                    try:
                        self.destroy_timer(self._manual_record_timer)
                    except Exception:  # noqa: BLE001
                        pass
                    self._manual_record_timer = None
                buf = list(self._handguide_buffer)
                if action == 'cancel':
                    self._handguide_buffer = []
                    if not self._retorque_follower_or_keep_locked(response):
                        return response  # keep the session (arm may be limp)
                    with self._mode_lock:
                        self._manual_persistent = False
                        self._recompute_on_manual_locked()
                    response.success = True
                    response.message = 'Aufnahme verworfen.'
                    return response
                # stop
                if not self._retorque_follower_or_keep_locked(response):
                    response.sample_count = len(buf)  # report what was captured
                    return response  # keep on_manual True (arm may be limp)
                duration = float(buf[-1][0]) if buf else 0.0
                # Buffer sample layout: [t_s, j1..j5, grip]. CONTRACT B point:
                # [j1..j5, grip, t_s].
                points = [[s[1], s[2], s[3], s[4], s[5], s[6], s[0]] for s in buf]
                response.points_json = json.dumps({
                    'fps': _MANUAL_RECORD_FPS, 'points': points,
                })
                response.sample_count = len(buf)
                response.duration_s = duration
                response.success = True
                response.message = f'Aufnahme beendet — {len(buf)} Punkte.'
                if cap_reached:
                    # The RECORD_MAX_S cap auto-stopped this recording — tell the
                    # student so a silently-truncated take isn't a surprise.
                    response.message += (' Maximale Aufnahmedauer erreicht — die '
                                         'Aufnahme wurde automatisch beendet.')
                # _manual_persistent stays True (record STOP leaves the session
                # open): the frontend exits via hand_guide(false), so record→replay
                # works within the one session.
                return response
            finally:
                try:
                    self._manual_lock.release()
                except RuntimeError:
                    pass

        response.message = 'Unbekannte Aufnahme-Aktion.'
        return response

    def _manual_record_sample(self):
        """~25 Hz sampler for /workshop/record: append [t_s, j1..j5, grip] to the
        hand-guide buffer, dropping sub-threshold-identical samples (still arm)
        and enforcing RECORD_MAX_S + the hard sample cap. Runs on the executor;
        fast + never blocks (drops a frame rather than waiting on the mutex)."""
        if not self._manual_record_active:
            return
        joints = None
        if self.communicator is not None:
            try:
                joints = self.communicator.get_latest_follower_joints()
            except Exception:  # noqa: BLE001
                joints = None
        if not joints or len(joints) < 6:
            return
        t = time.monotonic() - self._manual_record_start_mono
        sample = [float(t)] + [float(v) for v in joints[:6]]
        acquired = self._manual_lock.acquire(timeout=0.05)
        if not acquired:
            return  # a stop/jog holds the mutex; drop this frame (bounded loss)
        try:
            if not self._manual_record_active:
                return
            self._manual_last_activity_mono = time.monotonic()
            buf = self._handguide_buffer
            if buf:
                last = buf[-1]
                if all(abs(sample[1 + i] - last[1 + i]) < _MANUAL_RECORD_MIN_DELTA_RAD
                       for i in range(6)):
                    # Still enforce the wall-clock cap while dropping duplicates.
                    if t >= RECORD_MAX_S:
                        self._manual_record_active = False
                        # F4 — clean stop: schedule the executor to re-torque +
                        # clear on_manual (the sampler must not block on it).
                        self._schedule_manual_record_cap_finish()
                    return
            buf.append(sample)
            if len(buf) >= _MANUAL_RECORD_MAX_SAMPLES or t >= RECORD_MAX_S:
                self._manual_record_active = False
                self._schedule_manual_record_cap_finish()  # F4 clean stop
        finally:
            try:
                self._manual_lock.release()
            except RuntimeError:
                pass

    def _schedule_manual_record_cap_finish(self):
        """Schedule the F4 one-shot clean-stop after a RECORD_MAX_S auto-stop.
        Called from the sampler (which must NEVER block): the actual re-torque +
        on_manual clear runs on the executor in _finish_manual_record_on_cap.
        Idempotent — schedules at most one pending cap-finish."""
        if getattr(self, '_manual_record_cap_timer', None) is not None:
            return
        try:
            self._manual_record_cap_timer = self.create_timer(
                0.1, self._finish_manual_record_on_cap)
        except Exception as e:  # noqa: BLE001
            self.get_logger().error(f'record cap-finish schedule failed: {e}')

    def _cancel_manual_record_cap_timer(self):
        """Cancel/destroy the F4 cap-finish one-shot if pending (a manual stop /
        cancel / re-lock got there first)."""
        timer = getattr(self, '_manual_record_cap_timer', None)
        if timer is not None:
            try:
                self.destroy_timer(timer)
            except Exception:  # noqa: BLE001
                pass
            self._manual_record_cap_timer = None

    def _finish_manual_record_on_cap(self):
        """F4 one-shot: after RECORD_MAX_S auto-stopped a manual recording, tear
        down the sampler timer, re-torque the follower (fail-loud → keep on_manual
        so the next attempt retries rather than releasing onto a limp arm), clear
        on_manual and surface a German „Maximale Aufnahmedauer erreicht" so the
        student sees the auto-stop. Runs on the executor (scheduled from the
        sampler, which must never block on the ~seconds-long torque switch).
        One-shot: cancels its own timer first (create_timer is periodic)."""
        from types import SimpleNamespace
        self._cancel_manual_record_cap_timer()
        acquired = self._manual_lock.acquire(timeout=6.0)
        if not acquired:
            # A stop/jog holds the mutex; retry shortly (the manual stop path will
            # otherwise finish the cleanup and cancel this reschedule).
            self._schedule_manual_record_cap_finish()
            return
        try:
            self._manual_record_active = False
            if self._manual_record_timer is not None:
                try:
                    self.destroy_timer(self._manual_record_timer)
                except Exception:  # noqa: BLE001
                    pass
                self._manual_record_timer = None
            holder = SimpleNamespace(message='', success=None)
            if self._retorque_follower_or_keep_locked(holder):
                # FIX 1 — close the persistent record session on a clean re-torque.
                with self._mode_lock:
                    self._manual_persistent = False
                    self._recompute_on_manual_locked()
            # Frontend-observable: surfaced on the next /workshop/record stop AND
            # via the /task/status notice below.
            self._manual_record_cap_reached = True
        finally:
            try:
                self._manual_lock.release()
            except RuntimeError:
                pass
        # Best-effort German notice on /task/status (React renders a toast on a
        # non-empty error). Outside the lock so a slow publish can't hold it.
        try:
            if self.communicator is not None:
                notice = TaskStatus()
                notice.phase = TaskStatus.READY
                notice.error = ('Maximale Aufnahmedauer erreicht — die Aufnahme '
                                'wurde automatisch beendet.')
                self.communicator.publish_status(status=notice)
        except Exception as e:  # noqa: BLE001
            self.get_logger().warning(f'record cap notice publish failed: {e}')

    def workshop_replay_callback(self, request, response):
        """"/workshop/replay" — replay a recorded hand-guided trajectory on the
        follower. Async: re-torque ON (fail-loud) then spawn a daemon that
        re-segments through the velocity floor (handlers/trajectory) and holds
        _manual_lock for the whole publish. Points from ``points_json`` (inline,
        CONTRACT B) OR a server-persisted trajectory named ``name``."""
        from physical_ai_server.workflow.handlers.motion import WorkflowError
        from physical_ai_server.workflow.handlers.trajectory import (
            clamp_speed, extract_points, resegment_trajectory,
        )
        response.success = False
        response.message = ''

        # F6 — refuse a driven replay against a STALE follower readback (frozen
        # joint stream) so the lead-in never seeds from an old pose. Pre-claim.
        if self._follower_joints_stale():
            response.message = ('Armstellung noch nicht bekannt — bitte kurz '
                                'warten und erneut versuchen.')
            return response

        # Fast pre-check (re-checked atomically under _mode_lock below).
        ok, msg = self._assert_no_other_active('manual')
        if not ok:
            response.message = msg
            return response
        # Batch 2b: driving the follower to replay a motion while a hand-guide
        # RECORDING is still running would fight the student's hand + contaminate the
        # take. Refuse until the recording is stopped (frontend also blocks this).
        if self._manual_record_active:
            response.message = 'Erst die Aufnahme beenden.'
            return response
        blocked, bmsg = self._manual_leader_blocks()
        if blocked:
            response.message = bmsg
            return response
        if self.communicator is None:
            response.message = 'Bitte zuerst den Roboter auswählen.'
            return response

        existing = self._manual_replay_thread
        if existing is not None and existing.is_alive():
            response.message = 'Es läuft bereits eine Wiedergabe.'
            return response

        # Resolve the points: inline JSON first, else a persisted named trajectory.
        points_json = (request.points_json or '').strip()
        traj = None
        if points_json:
            try:
                traj = json.loads(points_json)
            except (ValueError, TypeError):
                response.message = 'Aufnahme-Daten sind ungültig.'
                return response
        else:
            name = (request.name or '').strip()
            if name:
                try:
                    wfm = self._get_or_create_workflow_manager()
                    if wfm is not None:
                        traj = wfm.get_trajectories().get(name)
                except Exception:  # noqa: BLE001
                    traj = None
            if traj is None:
                response.message = 'Keine Aufnahme angegeben.'
                return response
        try:
            points = extract_points(traj)
        except WorkflowError as e:
            response.message = str(e)
            return response
        speed = clamp_speed(request.speed)

        current = None
        try:
            current = self.communicator.get_latest_follower_joints()
        except Exception:  # noqa: BLE001
            current = None
        if not current or len(current) < 6:
            response.message = ('Aktuelle Armstellung ist noch nicht bekannt — '
                                'bitte kurz warten und erneut versuchen.')
            return response
        current = [float(v) for v in current[:6]]
        # Non-finite guard: a NaN/Inf readback would publish NaN through the
        # lead-in (garbage arm command) or crash build_segment on an Inf delta.
        if not all(math.isfinite(v) for v in current):
            response.message = ('Aktuelle Armstellung ist ungültig — bitte kurz '
                                'warten und erneut versuchen.')
            return response
        # Floor guard for the synthetic lead-in (the recorded points are reachable-
        # above-table by construction; the current-pose→start interpolation is
        # not). Returns None (no check) when the floor is unknown → lead-in behaves
        # exactly as before.
        lead_floor = self._build_replay_lead_floor_check()
        try:
            segmented = resegment_trajectory(
                points, speed, lead_in_from=current,
                lead_in_floor_check=lead_floor,
                point_floor_check=lead_floor)  # FIX 5 — floor-check recorded points
        except WorkflowError as e:
            response.message = str(e)
            return response
        if not segmented:
            response.message = 'Die Aufnahme enthält keine Bewegung.'
            return response

        # Pre-create the trajectory publisher on the executor thread so the daemon
        # never races a cross-thread create_publisher (mirrors _ensure_sim_publisher).
        if getattr(self, '_workflow_traj_publisher', None) is None:
            try:
                from trajectory_msgs.msg import JointTrajectory
                self._workflow_traj_publisher = self.create_publisher(
                    JointTrajectory, '/leader/joint_trajectory', 10)
            except Exception as e:  # noqa: BLE001
                self.get_logger().warning(f'replay publisher pre-create failed: {e}')

        # F1 — atomic claim: hold _mode_lock around the ownership re-check AND the
        # on_manual claim so a concurrent recording/inference/workflow start can't
        # slip in. The claim MUST live here (not in the daemon) so success=True
        # never races a mode start against the not-yet-driving daemon. F9 — register
        # _manual_replay_thread under the same lock (atomic with the claim + the
        # replay-already-running re-check). F3 — the re-torque AND the stop-event
        # clear happen in _run_replay AFTER it acquires _manual_lock (never clear
        # the shared stop-event unlocked — it could defeat an in-flight jog's
        # abort). _mode_lock is RELEASED before the daemon starts (it acquires
        # _manual_lock, not _mode_lock → no deadlock).
        with self._mode_lock:
            ok, msg = self._assert_no_other_active('manual')
            if not ok:
                response.message = msg
                return response
            existing = self._manual_replay_thread
            if existing is not None and existing.is_alive():
                response.message = 'Es läuft bereits eine Wiedergabe.'
                return response
            # FIX 1 — a replay is a TRANSIENT op: it claims a refcount slot for the
            # duration of the drive (released in _run_replay's finally), instead of
            # the old per-call `opened` boolean. FIX 4 — snapshot the exit
            # generation so a „Beenden" that lands before the daemon drives aborts
            # it (checked in _run_replay under _manual_lock).
            self._manual_transient_ops += 1
            self._recompute_on_manual_locked()
            exit_gen = self._manual_exit_gen
            thread = threading.Thread(
                target=self._run_replay,
                args=(segmented, exit_gen),
                name='workshop-replay',
                daemon=True,
            )
            self._manual_replay_thread = thread
            # Finding 2 — stamp activity INSIDE the claim lock (see the jog claim)
            # so the idle watchdog never clobbers this fresh replay ref.
            self._manual_last_activity_mono = time.monotonic()
        thread.start()
        response.success = True
        response.message = 'Wiedergabe gestartet.'
        return response

    def _build_replay_lead_floor_check(self):
        """Return a ``callable(q6) -> bool`` (True when a lead-in waypoint dips the
        tool below the table floor) for the replay lead-in guard, or None when no
        floor is known / IK is unavailable. Reuses motion._floor_z_at so the
        plane/scalar table-height logic matches every other motion."""
        ik = self._build_ik_solver()
        if ik is None:
            return None
        try:
            calib = self._load_workflow_calibration() or {}
        except Exception:  # noqa: BLE001
            calib = {}
        z_table = calib.get('z_table')
        table_plane = calib.get('table_plane')
        if z_table is None and table_plane is None:
            return None
        from types import SimpleNamespace
        from physical_ai_server.workflow.handlers import motion as _motion
        shim = SimpleNamespace(z_table=z_table, table_plane=table_plane)

        def _lead_floor(q6):
            try:
                pose = ik.fk([float(v) for v in q6[:5]])
            except Exception:  # noqa: BLE001
                return False
            if pose is None:
                return False
            _R, t = pose
            fz = _motion._floor_z_at(shim, float(t[0]), float(t[1]))
            if fz is None:
                return False
            return float(t[2]) < fz - _motion.WORKSPACE_FLOOR_MARGIN_M

        return _lead_floor

    def _run_replay(self, segmented, exit_gen):
        """Daemon body for /workshop/replay. Holds _manual_lock for the whole
        chunked publish (so a jog can't drive concurrently). F3: the re-torque AND
        the _manual_stop_event.clear() happen HERE, AFTER acquiring _manual_lock —
        the stop-event is a SHARED abort signal, so clearing it unlocked could
        defeat an in-flight jog's abort. On a re-torque failure the arm may be limp
        → keep the transient ref (do NOT drive); the idle watchdog re-torques later.

        FIX 4 — ``exit_gen`` is the exit generation snapshotted by the callback; if
        a „Beenden" bumped it while we waited on _manual_lock the session was torn
        down and we abort without driving. FIX 2 — a record start seizing the
        (limp) arm between the claim and here also aborts the drive."""
        from types import SimpleNamespace
        from physical_ai_server.workflow.trajectory_builder import chunked_publish
        acquired = self._manual_lock.acquire(timeout=30.0)
        keep_session = False
        try:
            if not acquired:
                self.get_logger().warning('replay could not acquire manual lock')
                return
            # FIX 4 — a „Beenden" bumped the exit generation while we waited on
            # _manual_lock → the session was torn down; do NOT drive after it.
            if self._manual_exit_gen != exit_gen:
                self.get_logger().warning(
                    'replay aborted — Handbetrieb was ended before the drive '
                    'started.')
                return
            # FIX 2 — a record start seized the (limp) arm between the callback's
            # claim and here; driving now would fight the student's hand. Mirror
            # the jog F2 record re-check.
            if self._manual_record_active:
                self.get_logger().warning(
                    'replay aborted — a recording started before the drive.')
                return
            # F3 — re-torque UNDER the lock (serialised against jogs), fail-loud.
            if not self._retorque_follower_or_keep_locked(
                    SimpleNamespace(message='', success=None)):
                keep_session = True  # arm may be limp → keep the ref, do NOT drive
                self.get_logger().error(
                    'replay re-torque failed — not driving; Handbetrieb bleibt aktiv.')
                return
            # FIX 4 (Finding 1) — re-check the exit generation AFTER the blocking
            # re-torque. A „Beenden" that landed DURING the re-torque bumped
            # _manual_exit_gen and set the stop-event, but the pre-torque check
            # above ran before it. Without this re-check the clear() below would
            # wipe that stop-event and drive the whole replay after the student
            # already pressed „Beenden".
            if self._manual_exit_gen != exit_gen:
                self.get_logger().warning(
                    'replay aborted — Handbetrieb was ended during the re-torque.')
                return
            # F3 — clear the SHARED stop-event only now, under the lock.
            self._manual_stop_event.clear()
            # A „Beenden" that races the clear above still bumped the exit
            # generation, so fold it into should_stop → chunked_publish halts at
            # the next chunk boundary (≤1 velocity-safe segment) instead of driving
            # the full trajectory even if the raw stop-event was just cleared.
            chunked_publish(
                publisher=self._trajectory_publisher,
                points=segmented,
                should_stop=lambda: (self._manual_stop_event.is_set()
                                     or self._manual_exit_gen != exit_gen),
            )
        except Exception as e:  # noqa: BLE001 — daemon must never crash the node
            self.get_logger().error(f'replay failed: {e!r}')
        finally:
            # FIX 1 — release this transient replay's ref under the arbiter lock,
            # unless a re-torque failure left the arm possibly-limp (keep_session
            # retains the ref; the idle watchdog resets it later).
            if not keep_session:
                with self._mode_lock:
                    self._manual_transient_ops = max(
                        0, self._manual_transient_ops - 1)
                    self._recompute_on_manual_locked()
            self._manual_last_activity_mono = time.monotonic()
            # F9 — only clear the registration if it still points at THIS thread
            # (a newer replay may have registered after we finished).
            if self._manual_replay_thread is threading.current_thread():
                self._manual_replay_thread = None
            if acquired:
                try:
                    self._manual_lock.release()
                except RuntimeError:
                    pass

    def get_object_catalog_callback(self, request, response):
        """Return the named-object catalog's type list (keys + German labels)
        for the Roboter Studio Blockly dropdowns. Reads the catalog JSON from
        the calib volume at call time. On an absent/corrupt/invalid catalog:
        empty arrays, success=False, German message (the load error)."""
        try:
            from physical_ai_server.workflow.object_catalog import (
                load_catalog, seed_default_catalog_if_missing,
            )
            # Seed a working example catalog on a fresh calib volume so the
            # dropdown is never empty (best-effort; never raises).
            seed_default_catalog_if_missing()
            catalog = load_catalog()
            labels = catalog.labels()  # [(type_name, label_de), ...] in catalog order
            response.type_names = [name for name, _label in labels]
            response.labels_de = [label for _name, label in labels]
            response.success = True
            response.message = ''
        except Exception as e:
            # load_catalog raises German ObjectCatalogError on every bad-catalog
            # path; surface it so the editor can show the teacher what to fix.
            response.type_names = []
            response.labels_de = []
            response.success = False
            response.message = str(e)
        return response

    # ------------------------------------------------------------------
    # Roboter Studio — workflow runtime services
    # ------------------------------------------------------------------
    def _emit_workflow_status(self, payload: dict) -> None:
        try:
            msg = WorkflowStatus()
            msg.workflow_id = str(payload.get('workflow_id', ''))
            msg.current_block_id = str(payload.get('current_block_id', ''))
            msg.phase = str(payload.get('phase', ''))
            msg.log_message = str(payload.get('log_message', ''))
            msg.progress = float(payload.get('progress', 0.0))
            msg.error = str(payload.get('error', ''))
            # Pack Detection list — each item is a perception.Detection
            # dataclass instance pushed by handlers via ctx.emit_detections.
            detections = payload.get('detections') or []
            packed = []
            import math  # local import (module has no top-level math; see _math usages)
            for d in detections:
                try:
                    cx, cy = d.centroid_px
                    bx, by, bw, bh = d.bbox_px
                    det = Detection()
                    det.cx = int(cx)
                    det.cy = int(cy)
                    det.w = int(bw)
                    det.h = int(bh)
                    det.label = str(getattr(d, 'label', '') or '')
                    det.confidence = float(getattr(d, 'confidence', 0.0) or 0.0)
                    # Grasp-orientation overlay: for an AprilTag detection the
                    # editor draws a grasp dot + pinch-axis line. The pinch axis
                    # is the tag's +x edge ((c1-c0)+(c2-c3) in pupil's CCW corner
                    # order — the SAME edge tag_pose.tag_yaw_base reads), measured
                    # here in IMAGE pixels (y-down) so the SVG draws it with no
                    # calibration round-trip. Colour/COCO detections have no
                    # corners → has_grasp_angle stays False (plain bbox only).
                    det.grasp_angle_rad = 0.0
                    det.has_grasp_angle = False
                    corners = getattr(d, 'corners_px', None)
                    if corners is not None:
                        try:
                            c = [(float(p[0]), float(p[1])) for p in corners]
                            if len(c) >= 4:
                                dx = (c[1][0] - c[0][0]) + (c[2][0] - c[3][0])
                                dy = (c[1][1] - c[0][1]) + (c[2][1] - c[3][1])
                                if abs(dx) > 1e-9 or abs(dy) > 1e-9:
                                    det.grasp_angle_rad = float(math.atan2(dy, dx))
                                    det.has_grasp_angle = True
                        except (TypeError, ValueError, IndexError):
                            pass
                    packed.append(det)
                except Exception:
                    # One malformed detection shouldn't kill the
                    # status publish for the others — skip it but
                    # continue.
                    continue
            msg.active_detections = packed
            self.workflow_status_publisher.publish(msg)
            # Audit O1: cache the latest detection set so the 5 Hz
            # SensorSnapshot timer can populate the React Sensoren tab's
            # visible_apriltag_ids field (hardcoded empty before this cache).
            # The named-object detection blocks emit a fresh list; the cache
            # holds whichever ran most recently. TTL is enforced in the timer
            # callback so a stale list from a finished workflow doesn't keep
            # showing up.
            if detections:
                # Audit fix #15: pack ts + list into one tuple that
                # writers rebind atomically. Reader (sensor timer)
                # snapshots the tuple reference in one read and
                # destructures locally, so a half-updated state can
                # never be observed.
                snapshot = list(detections)
                now = time.monotonic()
                self._workflow_detections_state = (now, snapshot)
                # Keep the legacy attributes in sync for any other
                # introspection — assignment order doesn't matter
                # because the canonical reader uses the tuple.
                self._workflow_last_detections = snapshot
                self._workflow_last_detections_ts = now
        except Exception as e:
            self.get_logger().warning(f'workflow status publish error: {e}')

    def _trajectory_publisher(self, points):
        """Publish a chunk of (q, t_from_start_s) tuples as one
        JointTrajectory message on /leader/joint_trajectory. Bridges the
        chunked_publish API to the existing topic the controller listens
        on. Side-effect: caches the LAST published joint vector so the
        workflow runtime can chain segments without depending on a
        /joint_states subscription."""
        try:
            from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
        except ImportError:
            self.get_logger().error('trajectory_msgs not available')
            return
        if not hasattr(self, '_workflow_traj_publisher') or self._workflow_traj_publisher is None:
            self._workflow_traj_publisher = self.create_publisher(
                JointTrajectory, '/leader/joint_trajectory', 10,
            )
        joint_names = (
            self.total_joint_order
            if self.total_joint_order
            else ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
        )
        msg = JointTrajectory()
        msg.joint_names = list(joint_names)
        last_q = None
        for q, t in points:
            point = JointTrajectoryPoint()
            point.positions = [float(v) for v in q]
            point.time_from_start.sec = int(t)
            point.time_from_start.nanosec = int((t - int(t)) * 1e9)
            msg.points.append(point)
            last_q = list(point.positions)
        self._workflow_traj_publisher.publish(msg)
        if last_q is not None:
            self._last_published_joints = last_q

    def _get_or_create_workflow_manager(self):
        if self.workflow_manager is not None:
            return self.workflow_manager
        try:
            from physical_ai_server.workflow.workflow_manager import WorkflowManager
        except ImportError as e:
            self.get_logger().error(f'Cannot import WorkflowManager: {e}')
            return None
        self.workflow_manager = WorkflowManager(
            publisher=self._trajectory_publisher,
            ik_factory=self._build_ik_solver,
            perception_factory=self._build_perception,
            load_destinations=lambda: {},
            load_calibration=self._load_workflow_calibration,
            emit_status=self._emit_workflow_status,
            on_finished=self._on_workflow_finished,
            get_scene_frame=lambda: self._get_latest_camera_frame('scene'),
            get_gripper_frame=lambda: self._get_latest_camera_frame('gripper'),
            get_scene_frame_age=lambda: self._get_camera_frame_age('scene'),
            get_current_pose_xyz=self._get_current_gripper_xyz,
            # Audit S2: lambda so the call is deferred to workflow start
            # time (communicator may not yet be wired at WorkflowManager
            # construction). Returns None on absent comm or stale joints.
            get_follower_joints=lambda: (
                self.communicator.get_latest_follower_joints()
                if self.communicator is not None
                else None
            ),
            # Roboter Studio named-object grasping: load the teacher-edited
            # object catalog from the calib volume at each workflow start. Raises
            # a German ObjectCatalogError on a missing/corrupt/invalid catalog,
            # which WorkflowManager.start() catches and surfaces at the first
            # named-object block (a workflow with no named blocks is unaffected).
            load_object_catalog=self._load_object_catalog,
        )
        return self.workflow_manager

    # ------------------------------------------------------------------
    # Roboter Studio Phase-3 simulation runtime (virtual arm + perception)
    # ------------------------------------------------------------------
    _SIM_JOINT_NAMES = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5',
                        'gripper_joint_1']

    def _ensure_sim_publisher(self):
        """Lazily create the sim-only /sim/joint_states JointState publisher +
        the idle-republish timer. Created on the ROS executor thread (from
        workflow_start_callback) so SimArm.publish — which runs on the workflow
        daemon thread — never races a cross-thread create_publisher. Reuses
        sensor_msgs/msg/JointState, so NO interfaces rebuild."""
        if self._sim_joint_state_publisher is not None:
            return
        try:
            from sensor_msgs.msg import JointState
        except ImportError:
            self.get_logger().error('sensor_msgs not available for /sim/joint_states')
            return
        self._sim_joint_state_publisher = self.create_publisher(
            JointState, '/sim/joint_states', 10,
        )
        if self._sim_idle_timer is None:
            try:
                # Low-rate republish of the last commanded pose so a freshly
                # mounted React twin gets the current rest pose without waiting
                # for the next motion.
                self._sim_idle_timer = self.create_timer(0.5, self._sim_idle_republish)
            except Exception as e:  # noqa: BLE001 — idle republish is best-effort
                self.get_logger().warning(f'sim idle timer create failed: {e}')
                self._sim_idle_timer = None

    def _publish_sim_joint_state(self, q):
        """Publish ONE virtual JointState(name=joint1..joint5,gripper_joint_1,
        position=q) on /sim/joint_states and cache it for the idle republish.
        The per-frame burst within a chunk is paced at the chunk level by
        chunked_publish._pace (SimArm.publish never sleeps); the React twin
        interpolates between received poses for smoothness."""
        pub = self._sim_joint_state_publisher
        if pub is None:
            return
        try:
            from sensor_msgs.msg import JointState
        except ImportError:
            return
        msg = JointState()
        try:
            msg.header.stamp = self.get_clock().now().to_msg()
        except Exception:  # noqa: BLE001 — stamp is advisory
            pass
        positions = [float(v) for v in q]
        msg.name = list(self._SIM_JOINT_NAMES)
        msg.position = positions
        pub.publish(msg)
        self._last_sim_joints = positions

    def _sim_idle_republish(self):
        """Timer callback: republish the last commanded sim pose at a low rate
        while no workflow is actively driving the virtual arm, so a twin that
        mounts mid-idle still gets the rest pose. Suppressed during an active
        run (the daemon thread is already streaming frames)."""
        if getattr(self, 'on_workflow', False):
            return
        q = self._last_sim_joints
        if q is None:
            return
        self._publish_sim_joint_state(q)

    def _get_or_create_sim_workflow_manager(self):
        """Mirror of _get_or_create_workflow_manager with the sim swaps:
        publisher → SimArm.publish, perception → SimPerception, joints → SimArm,
        scene frame → a 1x1 non-None dummy (only the None-check matters), scene
        age → 0.0, calibration → empty dict (the critical bypass so
        _attach_named_world preserves the preset world_xyz_m). The IK + object
        catalog are the REAL ones, and emit_status / on_finished are reused so
        the React editor, the on_workflow mutex, and status all behave
        identically to a real run."""
        if self.sim_workflow_manager is not None:
            return self.sim_workflow_manager
        try:
            from physical_ai_server.workflow.workflow_manager import WorkflowManager
            from physical_ai_server.workflow.sim_arm import SimArm
            from physical_ai_server.workflow.sim_perception import SimPerception
            import numpy as np   # E4: inside the guarded import — a missing dep
            #                      degrades to a clean German error, not a hang.
        except ImportError as e:
            self.get_logger().error(f'Cannot import sim WorkflowManager: {e}')
            return None
        self._ensure_sim_publisher()
        sim_arm = SimArm(
            joint_state_sink=self._publish_sim_joint_state,
            ik=self._build_ik_solver(),
            objects=list(getattr(self, '_sim_objects', []) or []),
        )
        self._sim_arm = sim_arm
        # 1x1 non-None dummy frame: perception_blocks._scene_frame only checks
        # for None (SimPerception ignores the pixels entirely).
        sim_dummy_frame = np.zeros((1, 1, 3), dtype=np.uint8)
        self.sim_workflow_manager = WorkflowManager(
            publisher=sim_arm.publish,
            ik_factory=self._build_ik_solver,
            perception_factory=lambda: SimPerception(
                list(getattr(self, '_sim_objects', []) or []),
                self._load_object_catalog_tolerant(),
            ),
            load_destinations=lambda: {},
            # The critical bypass: no calibration in sim, so _attach_named_world
            # early-returns and preserves SimPerception's preset world_xyz_m, and
            # the workspace floor (z_table/table_plane both None) is disabled.
            load_calibration=lambda: {},
            emit_status=self._emit_workflow_status,
            on_finished=self._on_workflow_finished,
            get_scene_frame=lambda: sim_dummy_frame,
            get_scene_frame_age=lambda: 0.0,
            get_current_pose_xyz=lambda: sim_arm.fk_xyz(),
            get_follower_joints=sim_arm.get_joints,
            load_object_catalog=self._load_object_catalog,
        )
        return self.sim_workflow_manager

    def _load_object_catalog(self):
        """Load the named-object catalog (tag_id → type → grasp recipe) from the
        edubotics_calib volume. Re-read each workflow start so a catalog edit
        applies on the next run without an environment restart."""
        from physical_ai_server.workflow.object_catalog import (
            load_catalog, seed_default_catalog_if_missing,
        )
        # Seed a working example catalog on a fresh calib volume so the first
        # named-object workflow finds a file instead of failing loud (best-effort).
        seed_default_catalog_if_missing()
        return load_catalog()

    def _load_object_catalog_tolerant(self):
        """Like ``_load_object_catalog`` but returns ``None`` on a corrupt /
        unreadable catalog instead of raising (MED-2 / D6). The sim
        ``perception_factory`` uses this so a SIM run with NO named-object blocks
        still starts on a bad catalog — ``SimPerception`` handles a ``None``
        catalog by skipping every object — matching the REAL path, where a bad
        catalog only surfaces at the first named block, not at workflow start."""
        from physical_ai_server.workflow.object_catalog import ObjectCatalogError
        try:
            return self._load_object_catalog()
        except ObjectCatalogError as e:
            self.get_logger().warning(f'Sim object catalog load failed: {e}')
            return None

    def _build_ik_solver(self):
        """Return the closed-form IKSolver, built ONCE per server lifetime.

        The solver is exact analytical geometry with the OMX-F kinematics
        baked in — it needs NO ``/robot_description`` fetch and is ALWAYS
        available, so the old "robot_description not available; IK disabled"
        failure mode (and the spin-from-callback URDF-fetch fragility) is gone.
        If the URDF parameter happens to be local we pass it so the solver can
        verify its baked constants; we never block on a cross-node fetch."""
        cached = getattr(self, '_ik_solver', None)
        if cached is not None:
            return cached
        try:
            from physical_ai_server.workflow.ik_solver import IKSolver
            urdf_string = None
            if self.has_parameter('robot_description'):
                urdf_string = self.get_parameter('robot_description').value or None
            self._ik_solver = IKSolver(urdf_string=urdf_string)
            return self._ik_solver
        except Exception as e:
            self.get_logger().error(f'IKSolver init failed: {e}')
            return None

    def _build_perception(self):
        # Cache the Perception instance (AprilTag detector) for the server
        # lifetime. Building a FRESH one per workflow start (the old behaviour)
        # allocated a new detector each run on top of the resident torch/LeRobot
        # baseline; repeated Start/Stop in Roboter Studio could push the container
        # RSS past its 6 GB mem_limit -> cgroup OOM-kill -> the WHOLE container
        # (incl. rosbridge) restarts -> the React app's rosbridge connection drops
        # AND the node's in-RAM robot_type is lost (the student then has to
        # re-select the robot). The IK solver is cached the same way
        # (_build_ik_solver). Detection is stateless per call, so one shared
        # instance is safe.
        #
        # No silent fallback: if Perception() raises (a build/config bug) it
        # propagates and WorkflowManager.start reports the German message to the
        # student; nothing is cached, so the next start retries cleanly.
        perc = getattr(self, '_perception', None)
        if perc is None:
            from physical_ai_server.workflow.perception import Perception
            perc = Perception()
            self._perception = perc
        return perc

    def _get_current_gripper_xyz(self):
        """Convenience wrapper that returns just the (x, y, z) of the
        current gripper pose for the destination_current handler. Reuses
        the FK path from ``_get_current_gripper_pose`` so the URDF is
        loaded once per server lifetime."""
        pose = self._get_current_gripper_pose()
        if pose is None:
            return None
        _R, t = pose
        return (float(t[0]), float(t[1]), float(t[2]))

    def _load_workflow_calibration(self):
        """Load scene intrinsics + extrinsic + the two table-height keys for
        the workflow runtime. Returns a dict consumed by ``WorkflowContext``.

        WS4 (2026-06-17): the scene extrinsic is now the single-shot
        board-on-table transform; it is still persisted to
        ``scene_handeye.yaml`` (same filename), so this loader is
        unchanged. The gripper camera is never loaded here (and never was
        — this has always been scene-only, which is why dropping gripper
        calibration is safe for the runtime).

        DUAL Z-KEY (don't conflate — see CLAUDE.md):
        - ``board_table_z`` = the table SURFACE height (written by the extrinsic
          solve, default 0). This is the plane the camera ray must intersect to
          recover an object's (x, y) — the object sits ON the surface, NOT at the
          gripper's grasp height. Used by the named-object tag-pose projection.
        - ``z_table`` = the MEASURED end-effector-frame grasp height (written ONLY
          by the touch-off, ~finger-length ABOVE the surface). This is the
          DESCEND target for motion.pickup — NOT a projection plane. NO 0.0
          fallback: a missing ``z_table`` leaves it None so grasp correctly
          refuses ("Tisch vermessen zuerst").

        ``table_plane`` (a, b, c) is the MEASURED EE-frame tilt from the touch-off;
        it rides along for motion's descend, NOT for object projection (it is
        offset above the surface, so projecting objects onto it reintroduces the
        lateral error on a tilted camera that board_table_z avoids)."""
        from pathlib import Path
        import os
        import cv2

        calib_dir = Path(os.environ.get(
            'EDUBOTICS_CALIB_DIR', '/root/.cache/edubotics/calibration'
        ))
        scene_intrinsic_path = calib_dir / 'scene_intrinsics.yaml'
        scene_handeye_path = calib_dir / 'scene_handeye.yaml'

        result: dict = {}
        if scene_intrinsic_path.exists():
            try:
                fs = cv2.FileStorage(str(scene_intrinsic_path), cv2.FILE_STORAGE_READ)
                K = fs.getNode('camera_matrix').mat()
                dist = fs.getNode('distortion_coefficients').mat()
                src_node = fs.getNode('source')
                source = src_node.string() if not src_node.empty() else ''
                fs.release()
                # #1 (2026-06-19): per-rig intrinsic calibration is mandatory.
                # A stale factory-default YAML (guessed K) from an older install
                # must NOT be loaded — leave scene_intrinsics absent so detection
                # projection refuses loudly and the student calibrates for real.
                if source == 'factory_default':
                    self.get_logger().warning(
                        'Ignoring factory-default scene_intrinsics.yaml — a '
                        'per-rig intrinsic calibration is now required.'
                    )
                elif K is not None and dist is not None:
                    result['scene_intrinsics'] = {'K': K, 'dist': dist}
            except Exception as e:
                self.get_logger().warning(
                    f'scene_intrinsics.yaml load failed: {e}'
                )
        if scene_handeye_path.exists():
            try:
                fs = cv2.FileStorage(str(scene_handeye_path), cv2.FILE_STORAGE_READ)
                T = fs.getNode('transform').mat()
                z_node = fs.getNode('z_table')
                # No 0.0 fallback: leave z_table absent if the YAML lacks it.
                z_table = float(z_node.real()) if not z_node.empty() else None
                # board_table_z = the SURFACE height (the projection plane).
                # Written by the extrinsic solve, so present whenever the
                # extrinsic is. No 0.0 fallback here either — absence means an
                # old YAML; the projection then has no plane and refuses loudly.
                board_z_node = fs.getNode('board_table_z')
                board_table_z = (
                    float(board_z_node.real()) if not board_z_node.empty() else None
                )
                plane_node = fs.getNode('table_plane')
                plane_mat = plane_node.mat() if not plane_node.empty() else None
                fs.release()
                if T is not None:
                    result['scene_extrinsics'] = T
                if z_table is not None:
                    result['z_table'] = z_table
                if board_table_z is not None:
                    result['board_table_z'] = board_table_z
                if plane_mat is not None and plane_mat.size == 3:
                    flat = [float(v) for v in plane_mat.reshape(-1)]
                    result['table_plane'] = (flat[0], flat[1], flat[2])
            except Exception as e:
                self.get_logger().warning(
                    f'scene_handeye.yaml load failed: {e}'
                )
        # W5 — ground-truth XY correction + yaw bias (identity / 0 when the
        # student never ran the accuracy-verify step). perception_blocks
        # (_attach_named_world) + mark_destination apply these AFTER projecting
        # to base, via CalibrationManager.apply_xy_correction. Default safe:
        # xy_correction None → no correction; yaw_bias_rad 0.0 → no change.
        result['xy_correction'] = None
        result['yaw_bias_rad'] = 0.0
        try:
            manager = self._get_or_create_calibration_manager()
            if manager is not None:
                corr = manager.read_verify_correction('scene')
                if corr is not None:
                    result['xy_correction'] = corr.get('xy_correction')
                    result['yaw_bias_rad'] = float(corr.get('yaw_bias_rad', 0.0))
        except Exception as e:
            self.get_logger().warning(f'verify correction load failed: {e}')
        return result

    def workflow_start_callback(self, request, response):
        # F1 — atomic mode arbitration: hold _mode_lock around the ownership check
        # AND the on_workflow claim so two tabs can't both pass the gate and end up
        # with two writers on /leader/joint_trajectory. Claim on_workflow BEFORE
        # any driver spawns (in _launch_workflow); the finally releases it when no
        # workflow actually started. _mode_lock is RELEASED before _launch_workflow
        # so it never nests _manual_lock/motion_lock (no lock-ordering deadlock).
        with self._mode_lock:
            ok, msg = self._assert_no_other_active('workflow')
            if not ok:
                response.success = False
                response.message = msg
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return response
            # HIGH-1 — mutual exclusion across BOTH manager instances. The mode
            # mutex above deliberately skips the on_workflow check for workflow
            # requests (the single-manager design relied on WorkflowManager.start's
            # own is_running guard). With a separate sim manager, a cross-kind
            # start (a real start during a sim run, or vice versa, e.g. a second
            # browser tab) would pass the mutex AND the target manager's is_running
            # (a DIFFERENT instance) — running both, one driving the real follower.
            # Refuse a start while ANY workflow (sim or real) is already live.
            #
            # ALSO gate on `on_workflow` (the teardown-race fix): `is_running`
            # flips False at the daemon's FIRST finally line (`_stop_event.set()`),
            # but `on_workflow` is cleared LAST (after the hat-thread joins, up to
            # ~1 s each). In that window `_active_workflow_manager()` is None yet a
            # workflow is still tearing down — without this a new start would slip
            # in and the finishing daemon would then clear `on_workflow=False`
            # mid-run, letting a concurrent recording/inference claim the arm.
            # `on_workflow` stays True through teardown, so this closes the window
            # without blocking a legit restart (clean finish → already False).
            if self._active_workflow_manager() is not None or getattr(
                    self, 'on_workflow', False):
                response.success = False
                response.message = 'Es läuft bereits ein Workflow. Bitte zuerst stoppen.'
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return response
            # Claim ownership under the lock BEFORE any driver spawns; a concurrent
            # start now sees on_workflow True and refuses at the gate above.
            self.on_workflow = True
        started = False
        try:
            started = self._launch_workflow(request, response)
        finally:
            # No running workflow took the claim (refused routing / failed start /
            # an exception) → release it so the arm isn't wedged.
            if not started:
                self.on_workflow = False
        return response

    def _launch_workflow(self, request, response) -> bool:
        """Route + start a workflow (sim or real). Returns True iff a workflow
        actually started (the running daemon then owns on_workflow); False on any
        refusal/failure. Populates the response on every path. Split out of
        workflow_start_callback so the F1 on_workflow claim (set by the caller
        under _mode_lock) can be released in a finally without re-indenting the
        whole routing body."""
        # Roboter Studio Phase-3 sim: the run flag rides a `sim` sibling key
        # INSIDE workflow_json (Interpreter.from_json reads only data['blocks'],
        # so the sibling is silently ignored by the interpreter). When enabled we
        # route to the virtual-arm runtime and BYPASS the leader-contention gate
        # below — a sim run never publishes to /leader/joint_trajectory, so a live
        # leader can't contend with it.
        # MED-2 — defensive parse: a crafted non-dict `sim` (string/int/list) or a
        # non-list `objects` must fail cleanly to the real path, never crash the
        # service callback (which would leave the response unpopulated → client
        # hang). Only the top-level json.loads is wrapped; the field reads must be
        # isinstance-guarded.
        sim_cfg = None
        try:
            parsed = json.loads(request.workflow_json)
            sim_cfg = parsed.get('sim') if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            sim_cfg = None
        sim_enabled = isinstance(sim_cfg, dict) and bool(sim_cfg.get('enabled'))

        if sim_enabled:
            raw_objs = sim_cfg.get('objects')
            self._sim_objects = list(raw_objs) if isinstance(raw_objs, list) else []
            manager = self._get_or_create_sim_workflow_manager()
            if manager is None:
                response.success = False
                response.message = 'Simulations-Runtime kann nicht initialisiert werden.'
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return False
            # Refresh the virtual scene on the cached SimArm so the held
            # simulation sees the objects placed for THIS run.
            try:
                if self._sim_arm is not None:
                    self._sim_arm.set_objects(self._sim_objects)
            except Exception as e:  # noqa: BLE001 — scene refresh is best-effort
                self.get_logger().warning(f'sim scene refresh failed: {e}')
            # B2: RunControls sends breakpoints BEFORE /workflow/start (the BP-r1
            # contract — else early-block breakpoints are skipped); with no run
            # active they land on the REAL manager via set_breakpoints' idle
            # fallback. Copy them onto the sim manager so breakpoints work in sim.
            try:
                if self.workflow_manager is not None:
                    manager.set_breakpoints(
                        list(getattr(self.workflow_manager, '_breakpoints', ()) or ()))
            except Exception as e:  # noqa: BLE001 — best-effort
                self.get_logger().warning(f'sim breakpoint seed failed: {e}')
        else:
            # HIGH-6 — refuse a follower-only Roboter-Studio workflow while a leader
            # arm is live (a both-arms session). Roboter Studio's trajectory writer is
            # the SOLE intended writer on /leader/joint_trajectory; with a leader
            # broadcaster also publishing there the two contend and the follower jumps
            # between the two command streams. The GUI flips the arm container into
            # follower-only (EDUBOTICS_FOLLOWER_ONLY=1) BEFORE offering Roboter Studio;
            # this is the server-side backstop (leader env says both-arms, or leader
            # joint-states are actively flowing). getattr-guarded so a server built
            # without the collision monitor still starts workflows normally.
            leader_check = getattr(self, 'leader_appears_active', None)
            if callable(leader_check):
                try:
                    leader_active = leader_check()
                except Exception as e:  # noqa: BLE001 — guard must not block a normal start
                    self.get_logger().warning(f'leader_appears_active check failed: {e}')
                    leader_active = False
                if leader_active:
                    response.success = False
                    response.message = (
                        'Roboter Studio kann nicht starten, solange der Leader-Arm '
                        'aktiv ist. Bitte zuerst in den Follower-Modus wechseln '
                        '(Leader-Schalter im Roboter-Studio-Tab) und erneut starten.'
                    )
                    response.unreachable_block_ids = []
                    response.unreachable_messages = []
                    return False
            manager = self._get_or_create_workflow_manager()
            if manager is None:
                response.success = False
                response.message = 'Workflow-Runtime kann nicht initialisiert werden.'
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return False
        success, message, unreachable = manager.start(
            request.workflow_json,
            request.workflow_id,
        )
        # on_workflow was claimed under _mode_lock in workflow_start_callback; the
        # caller's finally releases it when this returns False. On success the
        # running daemon owns it (the on_finished callback clears it on exit).
        response.success = success
        response.message = message
        # IK pre-check warnings — the React side renders setWarningText()
        # on each block id; the safety envelope still gates motion at
        # runtime so these are advisory.
        response.unreachable_block_ids = [
            str(u.get('block_id', '')) for u in (unreachable or [])
        ]
        response.unreachable_messages = [
            str(u.get('message', '')) for u in (unreachable or [])
        ]
        return bool(success)

    def _on_workflow_finished(self, terminal_phase: str) -> None:
        """Fired by WorkflowManager._run on every exit path. Releases
        the on_workflow mutex so the next mode (Aufnahme / Inferenz /
        Training / Kalibrierung) can claim the arm without the student
        having to press Stop on a workflow that's already done.

        Runs on the daemon thread, not the ROS executor thread — keep
        it tiny and side-effect-only. Phase is one of 'finished',
        'stopped', 'error'.
        """
        try:
            self.get_logger().info(
                f'Workflow daemon exited (phase={terminal_phase}); '
                f'releasing on_workflow.'
            )
        except Exception:
            pass
        self.on_workflow = False

    def _active_workflow_manager(self):
        """The currently-running workflow manager — the Phase-3 SIM manager takes
        precedence (``on_workflow`` serialises sim vs real, so at most one runs at
        a time). ``None`` when neither is running. The lifecycle callbacks
        (stop/pause/step/continue) route through this so the Stop/Pause buttons
        reach a sim run, not just a real one."""
        sim = self.sim_workflow_manager
        if sim is not None and sim.is_running:
            return sim
        real = self.workflow_manager
        if real is not None and real.is_running:
            return real
        return None

    def workflow_stop_callback(self, request, response):
        manager = self._active_workflow_manager()
        if manager is None:
            response.success = True
            response.message = 'Es läuft kein Workflow.'
            return response
        success, message = manager.stop()
        self.on_workflow = manager.is_running
        response.success = success
        response.message = message
        return response

    # ------------------------------------------------------------------
    # Phase-2 debugger plumbing — pause / step / continue / breakpoints
    # ------------------------------------------------------------------
    def workflow_pause_callback(self, request, response):
        manager = self._active_workflow_manager()
        if manager is None:
            response.success = False
            response.message = 'Es läuft kein Workflow.'
            return response
        success, message = manager.pause()
        response.success = success
        response.message = message
        return response

    def workflow_step_callback(self, request, response):
        manager = self._active_workflow_manager()
        if manager is None:
            response.success = False
            response.message = 'Es läuft kein Workflow.'
            return response
        success, message = manager.step()
        response.success = success
        response.message = message
        return response

    def workflow_continue_callback(self, request, response):
        manager = self._active_workflow_manager()
        if manager is None:
            response.success = False
            response.message = 'Es läuft kein Workflow.'
            return response
        success, message = manager.resume()
        response.success = success
        response.message = message
        return response

    def workflow_set_breakpoints_callback(self, request, response):
        # Cap the incoming list size — the React side bounds at ~50
        # but a buggy editor or hand-crafted rosbridge call could
        # flood the runtime.
        block_ids = list(request.block_ids or [])
        if len(block_ids) > 256:
            response.success = False
            response.message = (
                'Zu viele Haltepunkte (max 256). Bitte einige entfernen.'
            )
            return response
        # Target the running manager (sim or real) so a breakpoint set during a
        # sim run reaches the sim runtime; fall back to the real manager when
        # nothing is running (the pre-arm-breakpoints case).
        manager = self._active_workflow_manager() or self._get_or_create_workflow_manager()
        if manager is None:
            response.success = False
            response.message = 'Workflow-Runtime kann nicht initialisiert werden.'
            return response
        manager.set_breakpoints(block_ids)
        response.success = True
        response.message = f'{len(block_ids)} Haltepunkt(e) gesetzt.'
        return response

    # ------------------------------------------------------------------
    # Phase-2 calibration helpers — preview / verify / history
    # ------------------------------------------------------------------
    # These are presently lightweight stubs that report not-implemented
    # so the React UI can render a clear "noch nicht verfügbar" message
    # rather than failing with rosbridge "service not advertised". Full
    # implementations live behind ROBOTER_STUDIO_DEFERRED.md §1.2.
    def calibration_preview_callback(self, request, response):
        """Live ChArUco corner overlay for the intrinsic wizard (polled ~3-4 Hz).

        Grabs the latest scene-camera frame and runs the manager's LOCK-FREE
        ``detect_preview`` so the poll never queues behind a multi-second
        intrinsic solve (and, on the reentrant preempt group, never starves the
        1 Hz heartbeat). The returned corner pixels are in the camera's NATIVE
        resolution — the SAME resolution the :8080 MJPEG stream serves (both
        come from the one ``/scene/image_raw/compressed`` topic; web_video_server
        only re-encodes JPEG quality, it does not resize), so the React SVG
        overlay's ``naturalSize`` viewBox lines up with no offset.
        """
        response.detected = False
        response.corners_x = []
        response.corners_y = []
        response.board_area_pct = 0
        manager = self.calibration_manager
        if manager is None:
            response.message = 'Kalibrierung wurde nicht gestartet.'
            return response
        camera = (getattr(request, 'camera', '') or 'scene')
        frame = self._get_latest_camera_frame(camera)
        if frame is None:
            response.message = 'Kein Kamerabild verfügbar.'
            return response
        # Freshness gate: never preview against a frozen camera (mirrors the
        # workflow perception staleness gate). Age is advisory — None means the
        # provider can't report it, in which case we proceed.
        age = self._get_camera_frame_age(camera)
        if age is not None and age > 1.0:
            response.message = 'Kamerabild ist veraltet — bitte Kamera prüfen.'
            return response
        try:
            detected, xs, ys, area_pct = manager.detect_preview(frame)
        except Exception as e:  # noqa: BLE001 — preview is advisory, never fatal
            self.get_logger().warning(f'Calibration preview failed: {e}')
            response.message = 'Vorschau fehlgeschlagen.'
            return response
        response.detected = bool(detected)
        response.corners_x = [float(x) for x in xs]
        response.corners_y = [float(y) for y in ys]
        response.board_area_pct = int(area_pct)
        response.message = 'Tafel erkannt.' if detected else 'Tafel nicht erkannt.'
        return response

    def _verify_reset_response(self, response):
        """Zero-fill the VerifyCalibration response numeric fields so every
        return path is fully populated regardless of the branch."""
        response.detected_x = 0.0
        response.detected_y = 0.0
        response.detected_yaw_deg = 0.0
        response.has_detection = False
        response.point_count = 0
        response.residual_mm_mean = 0.0
        response.residual_mm_max = 0.0
        response.yaw_bias_deg = 0.0
        response.mirror_detected = False
        return response

    def calibration_verify_callback(self, request, response):
        """W5 — ground-truth correction + orientation verify.

        Routes ``add_point`` / ``solve`` / ``reset``. The student places a single
        tag36h11 at a KNOWN base (x, y) (+ optional yaw); ``add_point`` projects
        the detected tag with the CURRENT scene calibration (the same intrinsics +
        extrinsic + board_table_z projection path mark_destination /
        _attach_named_world use) and records (truth, detected). ``solve`` fits a 2D
        similarity (Umeyama, via CalibrationManager.fit_xy_correction); on success
        it persists xy_correction + yaw_bias into scene_handeye.yaml, on a
        mirror/handedness flip it refuses + persists nothing. German throughout."""
        import math as _math

        self._verify_reset_response(response)
        command = (getattr(request, 'command', '') or '').strip().lower()

        if command == 'reset':
            with self._verify_lock:
                self._verify_points = []
            response.success = True
            response.message = 'Prüfpunkte zurückgesetzt.'
            response.point_count = 0
            return response

        if command == 'add_point':
            return self._verify_add_point(request, response)

        if command == 'solve':
            return self._verify_solve(response)

        response.success = False
        response.message = (
            f'Unbekannter Befehl „{command}". Erwartet: add_point, solve, reset.'
        )
        return response

    def _verify_add_point(self, request, response):
        """Detect exactly one tag in the current scene frame, project its centre
        to base, read its yaw, and append (truth, detected) to the verify buffer.
        Fails loud in German on: camera not calibrated / no frame / no tag /
        multiple tags."""
        import math as _math

        calib = self._load_workflow_calibration()
        scene_intr = calib.get('scene_intrinsics')
        T = calib.get('scene_extrinsics')
        board_table_z = calib.get('board_table_z')
        if scene_intr is None or T is None or board_table_z is None:
            response.success = False
            response.message = (
                'Die Szenen-Kamera ist noch nicht vollständig kalibriert '
                '(Intrinsik + Extrinsik). Bitte zuerst die Kalibrierung '
                'abschließen.'
            )
            return response

        bgr = self._get_latest_camera_frame('scene')
        if bgr is None:
            response.success = False
            response.message = 'Kein Kamerabild der Szenen-Kamera verfügbar.'
            return response

        try:
            from physical_ai_server.workflow.perception import Perception
            from physical_ai_server.workflow.projection import project_pixel_to_table
            from physical_ai_server.workflow.tag_pose import tag_yaw_base
        except Exception as e:
            self.get_logger().error(f'verify add_point import failed: {e}')
            response.success = False
            response.message = 'Erkennungsmodul fehlt.'
            return response

        perc = Perception()
        if not perc.apriltag_available():
            response.success = False
            response.message = (
                'Der AprilTag-Detektor ist nicht verfügbar (pupil_apriltags fehlt).'
            )
            return response

        try:
            detections = perc.detect(bgr, camera='scene', mode='apriltag', aruco_id=None)
        except Exception as e:
            self.get_logger().error(f'verify add_point detect failed: {e}')
            response.success = False
            response.message = 'Markererkennung fehlgeschlagen.'
            return response

        if not detections:
            response.success = False
            response.message = (
                'Kein AprilTag im Bild erkannt. Bitte genau einen Tag flach in '
                'die Sicht der Szenen-Kamera legen.'
            )
            return response
        if len(detections) > 1:
            response.success = False
            response.message = (
                f'{len(detections)} Tags erkannt. Bitte genau einen Tag in der '
                'Sicht lassen.'
            )
            return response

        d = detections[0]
        K = scene_intr['K']
        dist = scene_intr['dist']
        try:
            cx, cy = d.centroid_px
            point = project_pixel_to_table(
                float(cx), float(cy), K, dist, T, float(board_table_z)
            )
        except Exception as e:
            self.get_logger().error(f'verify add_point projection failed: {e}')
            point = None
        if point is None:
            response.success = False
            response.message = (
                'Der erkannte Tag konnte nicht auf den Tisch projiziert werden — '
                'bitte Kalibrierung prüfen.'
            )
            return response

        detected_x = float(point[0])
        detected_y = float(point[1])
        detected_yaw = None
        if getattr(d, 'corners_px', None) is not None:
            try:
                detected_yaw = tag_yaw_base(
                    d.corners_px, K, dist, T, float(board_table_z)
                )
            except Exception as e:
                self.get_logger().warning(f'verify add_point yaw failed: {e}')
                detected_yaw = None

        truth_xy = (float(request.truth_x), float(request.truth_y))
        truth_yaw = None
        if bool(getattr(request, 'has_yaw', False)):
            truth_yaw = _math.radians(float(request.truth_yaw_deg))

        with self._verify_lock:
            self._verify_points.append(
                (truth_xy, (detected_x, detected_y), truth_yaw, detected_yaw)
            )
            count = len(self._verify_points)

        response.success = True
        response.has_detection = True
        response.detected_x = detected_x
        response.detected_y = detected_y
        response.detected_yaw_deg = (
            _math.degrees(detected_yaw) if detected_yaw is not None else 0.0
        )
        response.point_count = int(count)
        # Live per-point error (mm) so the student sees the residual building up.
        err_mm = _math.hypot(detected_x - truth_xy[0],
                             detected_y - truth_xy[1]) * 1000.0
        response.message = (
            f'Punkt {count} erfasst (Abweichung {err_mm:.0f} mm). '
            'Empfohlen: 4–6 über die Arbeitsfläche verteilte Punkte.'
        )
        return response

    def _verify_solve(self, response):
        """Fit + persist the ground-truth correction from the collected points."""
        import math as _math

        with self._verify_lock:
            points = list(self._verify_points)

        n = len(points)
        response.point_count = int(n)
        if n < 2:
            response.success = False
            response.message = (
                'Mindestens zwei Prüfpunkte werden benötigt (empfohlen 4–6, über '
                'die Arbeitsfläche verteilt). Bitte mehr Punkte erfassen.'
            )
            return response

        truth_xy = [p[0] for p in points]
        detected_xy = [p[1] for p in points]
        # Yaw pairs only where BOTH truth and detected yaw are present.
        truth_yaws = []
        detected_yaws = []
        for _t, _d, ty, dy in points:
            if ty is not None and dy is not None:
                truth_yaws.append(ty)
                detected_yaws.append(dy)
        ty_arg = truth_yaws or None
        dy_arg = detected_yaws or None

        manager = self._get_or_create_calibration_manager()
        if manager is None:
            response.success = False
            response.message = 'Kalibrierung kann nicht initialisiert werden.'
            return response

        try:
            result = manager.fit_xy_correction(
                truth_xy, detected_xy, truth_yaws=ty_arg, detected_yaws=dy_arg
            )
        except Exception as e:
            self.get_logger().error(f'verify solve fit failed: {e}')
            response.success = False
            response.message = 'Korrekturberechnung fehlgeschlagen.'
            return response

        response.point_count = int(result.get('point_count', n))
        response.residual_mm_mean = float(result.get('residual_mm_mean', 0.0))
        response.residual_mm_max = float(result.get('residual_mm_max', 0.0))
        response.yaw_bias_deg = _math.degrees(float(result.get('yaw_bias_rad', 0.0)))
        response.mirror_detected = bool(result.get('mirror_detected', False))

        if not result.get('ok'):
            # Includes the mirror/handedness case — persist nothing, surface the
            # German message verbatim.
            response.success = False
            response.message = result.get('message', 'Korrektur fehlgeschlagen.')
            return response

        wrote = False
        try:
            wrote = manager.write_verify_correction(
                'scene',
                result['xy_correction'],
                float(result.get('yaw_bias_rad', 0.0)),
                float(result.get('residual_mm_mean', 0.0)),
                int(result.get('point_count', n)),
            )
        except Exception as e:
            self.get_logger().error(f'verify solve persist failed: {e}')
            wrote = False

        if not wrote:
            response.success = False
            response.message = (
                'Korrektur berechnet, konnte aber nicht gespeichert werden — '
                'bitte die Szenen-Extrinsik prüfen.'
            )
            return response

        response.success = True
        msg = result.get('message', 'Korrektur gespeichert.')
        if response.point_count < 4:
            msg += (' Hinweis: für eine zuverlässige Korrektur werden 4–6 '
                    'verteilte Punkte empfohlen.')
        response.message = msg
        return response

    def calibration_history_callback(self, request, response):
        response.success = True
        response.timestamps = []
        response.step_names = []
        response.reprojection_errors_px = []
        response.agreement_deg = []
        response.message = 'Kalibrierungsverlauf wird in einer späteren Version aktiviert.'
        return response

    # ------------------------------------------------------------------
    # Phase-2 sensor snapshot publisher (~5 Hz while a workflow runs)
    # ------------------------------------------------------------------
    def _sensor_snapshot_timer_callback(self):
        """Emit a SensorSnapshot on /workflow/sensors. The React debugger
        panel subscribes; payload is intentionally lightweight (latest
        joints, gripper opening, visible markers / colors / objects).

        Quiet when no workflow is running so we don't pay the
        marker/color detection cost during inference or recording.
        """
        if not self.on_workflow:
            return
        try:
            msg = SensorSnapshot()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = 'workflow'
            # Latest follower joints from the inference manager / data
            # manager — both keep a rolling buffer.
            joints = []
            try:
                if (
                    self.communicator is not None
                    and hasattr(self.communicator, 'get_latest_follower_joints')
                ):
                    js = self.communicator.get_latest_follower_joints()
                    if js is not None:
                        joints = [float(v) for v in list(js)[:6]]
            except Exception:
                joints = []
            if not joints:
                joints = [0.0] * 6
            msg.follower_joints = joints[:5]
            msg.gripper_opening = float(joints[5]) if len(joints) >= 6 else 0.0
            # Audit O1: derive the perception fields from the last
            # detection list that the workflow runtime pushed via
            # _emit_workflow_status. TTL is 2.0s so a finished detect
            # block's results don't keep showing up forever — after
            # TTL the fields go back to empty (matches the React
            # side's "—" rendering for stale data).
            apriltag_ids: list[int] = []
            # Audit fix #15: snapshot the tuple in one read so the ts
            # and list are guaranteed to belong together. Rebinding
            # _workflow_detections_state is atomic in CPython (single
            # STORE_ATTR bytecode), so this read can't tear.
            state = self._workflow_detections_state
            try:
                last_ts, last = state
            except (TypeError, ValueError):
                last_ts, last = 0.0, []
            if last and (time.monotonic() - last_ts) < 2.0:
                for d in last:
                    label = str(getattr(d, 'label', '') or '')
                    if (
                        label.startswith('tag')
                        and len(label) > 3
                        and label[3:].isdigit()
                    ):
                        try:
                            apriltag_ids.append(int(label[3:]))
                        except ValueError:
                            pass
            msg.visible_apriltag_ids = apriltag_ids
            self.sensor_snapshot_publisher.publish(msg)
        except Exception:
            # Snapshot publish must never crash the timer thread.
            pass

    def handle_joystick_trigger(self, joystick_mode: str):
        self.get_logger().info(
            f'Joystick mode updated: {joystick_mode}')
        if self.data_manager is None:
            self.get_logger().warning(
                'Data manager is not initialized')
            return

        if not self.on_recording:
            self.get_logger().warning(
                'Not currently recording')
            return

        if joystick_mode == 'right':
            self.get_logger().info(
                'Right tact triggered - Moving to next episode')
            if len(self.data_manager.get_task_info().task_instruction) > 1:
                self.data_manager.record_next_episode()
            else:
                self.data_manager.record_early_save()
        elif joystick_mode == 'left':
            self.get_logger().info(
                'Left tact triggered - Re-record current episode')
            self.data_manager.re_record()
        elif joystick_mode == 'right_long_time':
            self.get_logger().info(
                'Right long tact triggered - Custom')
            # If you want, you can add custom functionality.
        elif joystick_mode == 'left_long_time':
            self.get_logger().info(
                'Left long tact triggered - Custom')
            # If you want, you can add custom functionality.
        else:
            self.get_logger().info(
                f'Received joystick trigger: {joystick_mode}')

    def _cleanup_hf_api_worker_with_threading(self):
        """
        Non-blocking cleanup of HF API Worker using threading.

        This method starts a separate thread to run the existing
        _cleanup_hf_api_worker method, preventing the main process.
        from blocking during shutdown.
        """
        import threading
        import time

        def cleanup_worker_thread():
            """Worker thread to run _cleanup_hf_api_worker."""
            try:
                # Call the existing cleanup method
                self._cleanup_hf_api_worker()
            except Exception as e:
                self.get_logger().error(f'Error in cleanup worker thread: {e}')

        try:
            if self.hf_status_timer is None and self.hf_api_worker is None:
                self.get_logger().info('No HF API components to cleanup')
                return

            self.get_logger().info('Starting non-blocking HF API Worker cleanup...')

            # Start cleanup thread
            cleanup_thread = threading.Thread(target=cleanup_worker_thread, daemon=True)
            cleanup_thread.start()

            # Reset references immediately (don't wait for cleanup to complete)
            self.hf_status_timer = None
            self.hf_api_worker = None

            self.get_logger().info('HF API Worker cleanup thread started')

            # Publish cancel status messages
            for i in range(3):
                self._publish_hf_operation_status_msg({
                    'status': 'Idle',
                    'operation': 'stop',
                    'repo_id': '',
                    'local_path': '',
                    'message': 'Canceled by stop command',
                    'progress': {
                        'current': 0,
                        'total': 0,
                        'percentage': 0.0,
                    }
                })
                time.sleep(0.5)

        except Exception as e:
            self.get_logger().error(
                f'Error starting non-blocking HF API Worker cleanup: {str(e)}'
            )
            # Fallback to blocking cleanup if threading fails
            self._cleanup_hf_api_worker()
        finally:
            self.hf_cancel_on_progress = False

    def _cleanup_hf_api_worker(self):
        """Cleanup HF API Worker and related timers."""
        try:
            if self.hf_status_timer is not None:
                self.hf_status_timer.stop(timer_name='hf_status')
                self.hf_status_timer = None

            if self.hf_api_worker is not None:
                self.hf_api_worker.stop()
                self.hf_api_worker = None

            self.get_logger().info('HF API Worker cleaned up successfully')
        except Exception as e:
            self.get_logger().error(f'Error cleaning up HF API Worker: {str(e)}')


def main(args=None):
    rclpy.init(args=args)
    node = PhysicalAIServer()
    # Multi-threaded executor. Originally 3 threads — enough for the
    # calibration wizard alone but provably too few for recording: this
    # node carries ~35 service callbacks + 2 CompressedImage subs +
    # several timers + rosbridge surface, all default
    # MutuallyExclusiveCallbackGroup. A recording-tick decode (~20 ms
    # per camera) can occupy 2 of 3 threads; a sensor_snapshot timer
    # tick takes the third; any rosbridge call then queues at the
    # executor level and drops camera frames at depth=1 QoS — the
    # documented compounding bug that caused "10 Hz Aufnahme" classroom
    # reports. 6 threads gives true parallel decode (cv2 releases the
    # GIL during JPEG decode) and leaves headroom for services. Python
    # GIL-bound for pure-Python callbacks; yields better under contention.
    from rclpy.executors import MultiThreadedExecutor
    executor = MultiThreadedExecutor(num_threads=6)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        # Cleanup HF API Worker before destroying node
        node._cleanup_hf_api_worker()
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
