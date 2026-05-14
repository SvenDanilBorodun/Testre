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
    AutoPoseSuggest,
    CalibrationCaptureColor,
    CalibrationCaptureFrame,
    CalibrationHistory,
    CalibrationPreview,
    CalibrationSolve,
    CalibrationStatus,
    CancelCalibration,
    ControlHfServer,
    ExecuteCalibrationPose,
    GetDatasetList,
    GetHFUser,
    GetModelWeightList,
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
)

from physical_ai_server.communication.communicator import Communicator
from physical_ai_server.data_processing.data_manager import DataManager
from physical_ai_server.data_processing.hf_api_worker import HfApiWorker
from physical_ai_server.inference.inference_manager import InferenceManager
from physical_ai_server.timer.timer_manager import TimerManager
from physical_ai_server.training.training_manager import TrainingManager
from physical_ai_server.utils.parameter_utils import (
    declare_parameters,
    load_parameters,
    log_parameters,
)

import rclpy
from rclpy.node import Node


class PhysicalAIServer(Node):
    # Define operation modes (constants taken from Communicator)

    DEFAULT_SAVE_ROOT_PATH = Path.home() / '.cache/huggingface/lerobot'
    DEFAULT_TOPIC_TIMEOUT = 5.0  # seconds
    PUB_QOS_SIZE = 10
    TRAINING_STATUS_TIMER_FREQUENCY = 0.5  # seconds

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
        # Audit F9: consecutive `convert_msgs_to_raw_datas` failures.
        # A single bad frame (malformed JPEG, cv_bridge transient)
        # used to nuke the whole inference run. Now we skip the tick
        # and only abort after N consecutive failures.
        self._inference_convert_fail_count: int = 0
        # Cached Supabase JWT forwarded from the React app's
        # StartWorkflow request — consumed by _cloud_vision_burst.
        # None until the next workflow start.
        self._cloud_vision_auth_token: str | None = None
        # Audit O1: last detection list emitted by the workflow runtime,
        # consumed by _sensor_snapshot_timer_callback to populate the
        # React Sensoren tab. The list is whatever the most recent
        # perception block (detect_color / detect_object /
        # detect_marker / detect_open_vocab) produced — labels follow
        # the perception.py conventions (color literal / COCO class /
        # 'tag{id}'). Cleared by TTL in the timer when it goes stale.
        self._workflow_last_detections: list = []
        self._workflow_last_detections_ts: float = 0.0
        # Cleared by /calibration/start, set by /calibration/cancel.
        # /calibration/execute_pose's chunked_publish polls this so a
        # mis-planned 4-second motion can be aborted mid-flight.
        self._calibration_stop_event = threading.Event()

        self.hf_cancel_on_progress = False

        self.robot_type_list = self.get_robot_type_list()
        self.start_recording_time: float = 0.0

        self.training_thread = None
        self.is_training = False
        self.training_status_timer = None

        self._init_core_components()

        self._init_ros_publisher()
        self._init_ros_service()

        self._setup_timer_callbacks()

        self.previous_data_manager_status = None

        self.goal_repo_id = None

    def _init_core_components(self):
        self.communicator: Optional[Communicator] = None
        self.data_manager: Optional[DataManager] = None
        self.timer_manager: Optional[TimerManager] = None
        self.heartbeat_timer: Optional[TimerManager] = None
        self.training_timer: Optional[TimerManager] = None
        self.inference_manager: Optional[InferenceManager] = None
        self.training_manager: Optional[TrainingManager] = None
        # Calibration manager is constructed lazily on first calibration call
        # — same pattern as TrainingManager — so a server that never enters
        # Roboter Studio doesn't pay the OpenCV-aruco import cost.
        self.calibration_manager = None
        # Workflow runtime is also lazily constructed.
        self.workflow_manager = None

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

    def _init_ros_service(self):
        self.get_logger().info('Initializing ROS services...')
        # Reentrant group for the two services that MUST preempt an
        # in-flight long motion: /calibration/cancel and /calibration/
        # status. /calibration/execute_pose's chunked_publish blocks
        # its callback for ~4 seconds; without a reentrant group on
        # cancel/status they would queue behind it and the stop-event
        # polling at 50ms cadence would be unreachable. Read-only
        # services (status) and preemption services (cancel) are safe
        # to dispatch concurrently because they only set/clear flags
        # and read disk state.
        from rclpy.callback_groups import ReentrantCallbackGroup
        self._preempt_cb_group = ReentrantCallbackGroup()
        service_definitions = [
            ('/task/command', SendCommand, self.user_interaction_callback),
            ('/get_robot_types', GetRobotTypeList, self.get_robot_types_callback),
            ('/set_robot_type', SetRobotType, self.set_robot_type_callback),
            ('/register_hf_user', SetHFUser, self.set_hf_user_callback),
            ('/get_registered_hf_user', GetHFUser, self.get_hf_user_callback),
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
            ('/calibration/auto_pose', AutoPoseSuggest, self.calibration_auto_pose_callback),
            (
                '/calibration/execute_pose',
                ExecuteCalibrationPose,
                self.calibration_execute_pose_callback,
            ),
            (
                '/calibration/capture_color',
                CalibrationCaptureColor,
                self.calibration_capture_color_callback,
            ),
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
            # Phase-2 calibration helpers (see CalibrationManager).
            ('/calibration/preview', CalibrationPreview, self.calibration_preview_callback),
            ('/calibration/verify', VerifyCalibration, self.calibration_verify_callback),
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

        if self.heartbeat_timer is None:
            self.heartbeat_timer = TimerManager(node=self)
            self.heartbeat_timer.set_timer(
                timer_name='heartbeat',
                timer_frequency=1.0,
                callback_function=self.communicator.heartbeat_timer_callback
            )
            self.heartbeat_timer.start(timer_name='heartbeat')

        self.inference_manager = InferenceManager()
        self.get_logger().info(
            f'ROS parameters initialized successfully for robot type: {robot_type}')

    def get_training_status(self):
        msg = TrainingStatus()
        if self.training_manager is None:
            return
        try:
            current_status = self.training_manager.get_current_training_status()
            training_info = current_status.training_info
            current_step = current_status.current_step
            current_loss = current_status.current_loss
            msg.training_info = training_info
            msg.current_step = current_step
            msg.current_loss = current_loss
            msg.is_training = self.is_training
            msg.error = ''
        except Exception as e:
            msg.current_step = 0
            msg.current_loss = float('nan')
            msg.error = str(e)
            self.get_logger().error(f'Error publishing training status: {msg.error}')
            return msg
        return msg

    def init_robot_control_parameters_from_user_task(
            self,
            task_info):
        self.get_logger().info(
            'Initializing robot control parameters from user task...')
        self.data_manager = DataManager(
            save_root_path=self.DEFAULT_SAVE_ROOT_PATH,
            robot_type=self.robot_type,
            task_info=task_info
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

        if self.heartbeat_timer is not None:
            self.heartbeat_timer.stop(timer_name='heartbeat')
            self.heartbeat_timer = None

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
        if self.calibration_manager is not None:
            self.calibration_manager = None
            self.on_calibration = False

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
        # Audit F17: one-shot camera-fps sanity check ~1.5 s after
        # start_recording_time. If an enabled camera publishes below
        # 0.8x of task_info.fps, surface a German [WARNUNG] so the
        # operator knows the dataset will repeat frames (which trains
        # strobing behavior into the policy).
        if (
            self.start_recording_time > 0
            and not getattr(self, '_camera_fps_checked', False)
            and (time.perf_counter() - self.start_recording_time) > 1.5
        ):
            self._camera_fps_checked = True
            try:
                target_fps = float(getattr(self.task_info, 'fps', 0) or 0)
                if target_fps > 0 and hasattr(self.communicator, 'get_camera_observed_hz'):
                    for cam_name in self.communicator.camera_topic_msgs.keys():
                        observed = self.communicator.get_camera_observed_hz(cam_name, 1.0)
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

    def _inference_timer_callback(self):
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
            # Audit F9: one bad frame used to nuke the run. Skip the
            # tick and only abort after 30 consecutive failures
            # (~1 s @ 30 Hz). Resets on the first successful tick below.
            self._inference_convert_fail_count += 1
            if self._inference_convert_fail_count < 30:
                if self._inference_convert_fail_count % 10 == 1:
                    self.get_logger().warning(
                        f'convert_msgs_to_raw_datas failed (tick skipped, '
                        f'count={self._inference_convert_fail_count}/30): {e}'
                    )
                return
            error_msg = (
                f'Failed to convert messages for >1s ({str(e)}). '
                f'Please check the robot type again!'
            )
            self.on_inference = False
            self._inference_convert_fail_count = 0
            current_status.phase = TaskStatus.READY
            current_status.error = error_msg
            self.communicator.publish_status(status=current_status)
            self.inference_manager.clear_policy()
            self.timer_manager.stop(timer_name=self.operation_mode)
            return
        # Reset counter on the first successful tick — sporadic
        # failures don't snowball into a forced abort.
        self._inference_convert_fail_count = 0

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

            # predict() returns None when the safety envelope rejects the tick
            # (NaN/inf in action, frozen camera, shape mismatch, camera-name
            # mismatch). In that case skip publishing so we don't drive the
            # arm with a bad or stale command.
            if action is None:
                return

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
        """
        Handle training command requests (START/FINISH).

        Supports both new training and resume functionality with proper validation.
        """
        try:
            if request.command == SendTrainingCommand.Request.START:
                ok, msg = self._assert_no_other_active('training')
                if not ok:
                    response.success = False
                    response.message = msg
                    return response

                # Initialize training components
                self.training_manager = TrainingManager()
                self.training_timer = TimerManager(node=self)
                self._setup_training_status_timer()

                # Validate training state
                if self.training_thread and self.training_thread.is_alive():
                    response.success = False
                    response.message = 'Training is already in progress'
                    return response

                # Extract resume parameters
                resume = getattr(request, 'resume', False)
                resume_model_path = getattr(request, 'resume_model_path', '').strip()

                # Log training request details
                output_folder_name = request.training_info.output_folder_name
                weight_save_root_path = TrainingManager.get_weight_save_root_path()
                self.get_logger().info(
                    f'Training request - Output: {output_folder_name}, '
                    f'Resume: {resume}, Model path: {resume_model_path}'
                )

                # Validate training configuration
                validation_result = self._validate_training_request(
                    resume, resume_model_path, output_folder_name, weight_save_root_path
                )
                if not validation_result['success']:
                    response.success = False
                    response.message = validation_result['message']
                    self._cleanup_training_on_error()
                    return response

                # Configure and start training
                self._configure_training_manager(request, resume, resume_model_path)
                self._start_training_thread()

                response.success = True
                response.message = 'Training started successfully'

            else:
                # Handle FINISH command
                if request.command == SendTrainingCommand.Request.FINISH:
                    self._stop_training()
                    response.success = True
                    response.message = 'Training stopped successfully'
                else:
                    response.success = False
                    response.message = f'Unknown command: {request.command}'

        except Exception as e:
            self.get_logger().error(f'Error in training callback: {str(e)}')
            response.success = False
            response.message = f'Training error: {str(e)}'
            self._cleanup_training_on_error()

        return response

    def _setup_training_status_timer(self):
        """Set up timer for publishing training status updates."""
        self.training_timer.set_timer(
            timer_name='training_status',
            timer_frequency=self.TRAINING_STATUS_TIMER_FREQUENCY,
            callback_function=lambda: self.training_status_publisher.publish(
                self.get_training_status()
            )
        )
        self.training_timer.start(timer_name='training_status')

    def _validate_training_request(
            self,
            resume,
            resume_model_path,
            output_folder_name,
            weight_save_root_path
    ):
        """
        Validate training request parameters.

        Returns
        -------
        dict
            {'success': bool, 'message': str}

        """
        # Check output folder conflicts for new training
        if not resume:
            output_path = weight_save_root_path / output_folder_name
            if output_path.exists():
                return {
                    'success': False,
                    'message': f'Output folder already exists: {output_path}'
                }

        # Validate resume configuration
        if resume:
            if not resume_model_path:
                return {
                    'success': False,
                    'message': 'Resume model path is required when resume=True'
                }

            # Check if resume config file exists
            full_config_path = weight_save_root_path / resume_model_path
            if not full_config_path.exists():
                return {
                    'success': False,
                    'message': f'Resume config file not found: {full_config_path}'
                }

        return {'success': True, 'message': 'Validation passed'}

    def _configure_training_manager(self, request, resume, resume_model_path):
        """Configure training manager with request parameters."""
        self.training_manager.training_info = request.training_info
        self.training_manager.resume = resume
        self.training_manager.resume_model_path = resume_model_path

    def _start_training_thread(self):
        """Start training in a separate thread."""
        def run_training():
            try:
                self.training_manager.train()
            except Exception as e:
                self.get_logger().error(f'Training error: {str(e)}')
            finally:
                self._cleanup_training_on_completion()

        self.training_thread = threading.Thread(target=run_training, daemon=True)
        self.training_thread.start()
        self.is_training = True

    def _stop_training(self):
        """Stop training gracefully."""
        self.is_training = False
        if self.training_manager:
            self.training_manager.stop_event.set()
        if self.training_thread and self.training_thread.is_alive():
            self.training_thread.join(timeout=self.DEFAULT_TOPIC_TIMEOUT)
        self._cleanup_training_on_completion()

    def _cleanup_training_on_completion(self):
        """Cleanup training resources on normal completion."""
        self.is_training = False
        self.get_logger().info('Training completed.')
        training_status = self.get_training_status()
        self.training_status_publisher.publish(training_status)
        if self.training_manager:
            self.training_manager.stop_event.set()
        if hasattr(self, 'training_timer'):
            self.training_timer.stop('training_status')

    def _cleanup_training_on_error(self):
        """Cleanup training resources on error."""
        self.is_training = False
        training_status = self.get_training_status()
        self.training_status_publisher.publish(training_status)
        if self.training_manager:
            self.training_manager.stop_event.set()
        if hasattr(self, 'training_timer'):
            self.training_timer.stop('training_status')

    def user_interaction_callback(self, request, response):
        try:
            if request.command == SendCommand.Request.START_RECORD:
                if self.on_recording:
                    self.get_logger().info('Restarting the recording.')
                    self.data_manager.re_record()
                    response.success = True
                    response.message = 'Restarting the recording.'
                    return response

                ok, msg = self._assert_no_other_active('recording')
                if not ok:
                    response.success = False
                    response.message = msg
                    return response

                self.get_logger().info('Start recording')
                self.operation_mode = 'collection'
                task_info = request.task_info
                self.init_robot_control_parameters_from_user_task(
                    task_info
                )

                self.start_recording_time = time.perf_counter()
                # Audit F17: re-arm the one-shot camera-fps check at
                # every START_RECORD so a slow-Hz warning fires once
                # per session, not once per process.
                self._camera_fps_checked = False
                self.on_recording = True
                response.success = True
                response.message = 'Recording started'

            elif request.command == SendCommand.Request.START_INFERENCE:
                ok, msg = self._assert_no_other_active('inference')
                if not ok:
                    response.success = False
                    response.message = msg
                    return response

                self.joint_topic_types = self.communicator.get_publisher_msg_types()
                self.operation_mode = 'inference'
                task_info = request.task_info
                self.task_instruction = task_info.task_instruction

                valid_result, result_message = self.inference_manager.validate_policy(
                    policy_path=task_info.policy_path)

                if not valid_result:
                    response.success = False
                    response.message = result_message
                    self.get_logger().error(response.message)
                    return response

                self.init_robot_control_parameters_from_user_task(
                    task_info
                )
                if task_info.record_inference_mode:
                    self.on_recording = True
                # Wire the safety envelope BEFORE enabling inference so
                # the first predicted action is already bounded. The
                # values come from the same loader the workflow runtime
                # uses, so both code paths share one source of truth.
                try:
                    joint_min, joint_max, base_delta = self._load_safety_clamps()
                    fps = float(getattr(task_info, 'fps', 30) or 30)
                    # `base_delta` is calibrated against 30 Hz; rescale
                    # for the actual policy fps so a 60 Hz policy gets
                    # half the per-tick delta budget.
                    max_delta = [d * 30.0 / fps for d in base_delta]
                    if hasattr(self.inference_manager, 'set_action_limits'):
                        self.inference_manager.set_action_limits(
                            joint_min=joint_min,
                            joint_max=joint_max,
                            max_delta_per_tick=max_delta,
                        )
                except Exception as _e:
                    self.get_logger().warning(
                        f'Safety envelope not configured: {_e}')
                self.on_inference = True
                self.inference_manager.reset_policy()
                self.start_recording_time = time.perf_counter()
                response.success = True
                response.message = 'Inference started'

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
                # Create publisher for HF status
                self.hf_status_publisher = self.create_publisher(
                    HFOperationStatus,
                    '/huggingface/status',
                    self.PUB_QOS_SIZE
                )
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
        'calibration', 'workflow', 'recording', 'inference', 'training' —
        used only for error message clarity.
        """
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
        return True, ''

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

    def _get_latest_camera_frame(self, camera: str, max_age_s: float | None = None):
        """Provider hook for the calibration manager + colour profile
        capture. Returns the most recent BGR frame for the named camera,
        or None when no frame has arrived yet / decode fails. Delegates to
        ``Communicator.get_latest_bgr_frame``, which decodes the cached
        ``CompressedImage`` JPEG via cv2.imdecode.

        Audit F58: when ``max_age_s`` is set, returns None for frames
        older than the threshold so cloud-vision bursts don't fire on
        30-s-stale frames and waste Modal time. Calibration / colour
        capture callers pass None (no age limit) since they're
        student-driven, not autonomous.
        """
        if self.communicator is None:
            return None
        if max_age_s is not None:
            age_getter = getattr(self.communicator, 'get_camera_msg_age_s', None)
            if callable(age_getter):
                try:
                    age = age_getter(camera)
                except Exception:
                    age = None
                if age is None or age > max_age_s:
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

    # Soft TTL on a failed IK build so a missing /robot_description
    # doesn't trigger a full URDF parameter lookup (with 1+2s wait
    # timeouts) on every FK call. Re-tries every _IK_BUILD_RETRY_S
    # seconds and logs at most once per window.
    _IK_BUILD_RETRY_S = 5.0

    def _get_current_gripper_pose(self):
        """Provider hook for hand-eye calibration. Returns ``(R 3x3, t 3,)``
        of gripper-in-base, or None when joint state hasn't arrived yet or
        FK is unavailable. Computes FK via the same IKSolver instance used
        for /calibration/execute_pose so the URDF is loaded exactly once
        per server lifetime."""
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
        ik = getattr(self, '_cached_ik_solver', None)
        if ik is None:
            # Honour the failed-build TTL so we don't retry per-tick.
            import time as _time
            last_attempt = getattr(self, '_ik_build_last_attempt_ts', 0.0)
            now = _time.monotonic()
            if now - last_attempt < self._IK_BUILD_RETRY_S:
                return None
            self._ik_build_last_attempt_ts = now
            try:
                ik = self._build_ik_solver()
            except Exception as e:
                self.get_logger().warning(f'IK build for FK failed: {e}')
                return None
            if ik is None:
                return None
            self._cached_ik_solver = ik
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
        # newly-started step's execute_pose call isn't aborted on its
        # very first tick.
        self._calibration_stop_event.clear()
        success, message = manager.start_step(request.camera, request.step)
        if success:
            self.on_calibration = True
        response.success = success
        response.message = message
        return response

    def calibration_capture_callback(self, request, response):
        manager = self.calibration_manager
        if manager is None:
            response.success = False
            response.message = 'Kalibrierung wurde nicht gestartet.'
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
        return response

    def calibration_solve_callback(self, request, response):
        manager = self.calibration_manager
        if manager is None:
            response.success = False
            response.message = 'Kalibrierung wurde nicht gestartet.'
            return response
        # The 'color_profile' step has no per-step solve (each
        # capture_color call persists its own YAML). Treat a solve
        # request for it as a "finish" signal: release the mutex if all
        # four canonical colours are present.
        if request.step == 'color_profile':
            try:
                from physical_ai_server.workflow.color_profile import ColorProfileManager
                cm = ColorProfileManager()
                if not cm.has_all_colors():
                    response.success = False
                    response.reprojection_error = 0.0
                    response.method_disagreement = 0.0
                    response.message = (
                        'Farbprofil unvollständig — bitte alle vier '
                        'Farben (rot/grün/blau/gelb) erfassen.'
                    )
                    return response
                response.success = True
                response.reprojection_error = 0.0
                response.method_disagreement = 0.0
                response.message = 'Farbprofil abgeschlossen.'
                self.on_calibration = False
                return response
            except Exception as e:
                response.success = False
                response.reprojection_error = 0.0
                response.method_disagreement = 0.0
                self.get_logger().error(f'color_profile finish-check raised: {e}')
                response.message = 'Farbprofil-Status konnte nicht gelesen werden.'
                return response

        # Same guard as capture_frame: an unhandled solver/numpy/cv2
        # exception used to leave on_calibration stuck True forever.
        # Force-release on the exception path so the student can retry
        # from a clean state.
        try:
            success, reproj, disagreement, message = manager.solve(request.camera, request.step)
        except Exception as e:
            self.get_logger().error(f'solve raised: {e}')
            self.on_calibration = False
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
            self.on_calibration = False
        return response

    def calibration_cancel_callback(self, request, response):
        """Drop in-flight calibration buffers and release the mutex.
        Called by the frontend wizard's cleanup useEffect when the
        student navigates away mid-step. Idempotent — returns success
        even if no step was active. Camera '' (empty) cancels every
        camera; otherwise narrows to the named camera. Also signals
        ``_calibration_stop_event`` so an in-flight execute_pose motion
        halts within ≤50 ms instead of running to its 4-second end."""
        camera = request.camera if request.camera else None
        # Set BEFORE dropping buffers so a mid-flight chunked_publish
        # observes the stop on its next poll without racing the manager.
        self._calibration_stop_event.set()
        manager = self.calibration_manager
        if manager is not None:
            try:
                _ok, _msg = manager.cancel_step(camera)
            except Exception as e:
                self.get_logger().warning(f'cancel_step raised: {e}')
        self.on_calibration = False
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
        has_handeye / has_color_profile — no manager state required, so
        works even when the server hasn't seen any /calibration/start
        call yet in its lifetime."""
        try:
            from physical_ai_server.workflow.calibration_manager import (
                CalibrationManager,
            )
            from physical_ai_server.workflow.color_profile import ColorProfileManager
            mgr = CalibrationManager()
            color_mgr = ColorProfileManager()
            response.has_gripper_intrinsics = bool(mgr.has_intrinsics('gripper'))
            response.has_scene_intrinsics = bool(mgr.has_intrinsics('scene'))
            response.has_gripper_handeye = bool(mgr.has_handeye('gripper'))
            response.has_scene_handeye = bool(mgr.has_handeye('scene'))
            response.has_color_profile = bool(color_mgr.has_all_colors())
            response.success = True
            response.message = 'Kalibrier-Status geladen.'
        except Exception as e:
            self.get_logger().warning(f'status read failed: {e}')
            response.has_gripper_intrinsics = False
            response.has_scene_intrinsics = False
            response.has_gripper_handeye = False
            response.has_scene_handeye = False
            response.has_color_profile = False
            response.success = False
            response.message = 'Status konnte nicht gelesen werden.'
        return response

    def calibration_auto_pose_callback(self, request, response):
        try:
            import numpy as _np
            from physical_ai_server.workflow.auto_pose import suggest_pose
        except ImportError as e:
            self.get_logger().error(f'auto_pose import failed: {e}')
            response.success = False
            response.message = 'Auto-Pose-Modul fehlt.'
            return response
        manager = self._get_or_create_calibration_manager()
        if manager is None:
            response.success = False
            response.message = 'Kalibrierung kann nicht initialisiert werden.'
            return response
        # The hemisphere sampler in suggest_pose generates candidates
        # AROUND ``board_centre_base``. The v1 ship called it with the
        # default origin (0, 0, 0), which is the BASE of the arm — every
        # candidate landed inside the robot's own footprint and IK
        # rejected them all (audit §1.6b).
        #
        # 0.25 m in front of the base on the table plane is the typical
        # OMX-F + classroom-kit setup (see tools/classroom_kit_README.md).
        # If a previous scene handeye solve has been persisted, prefer
        # the z_table written there over the 0.0 default so the
        # candidate elevations track the real table.
        board_xyz = _np.array([0.25, 0.0, 0.0])
        try:
            calib = self._load_workflow_calibration()
            z_table = calib.get('z_table')
            if z_table is not None:
                board_xyz = _np.array([0.25, 0.0, float(z_table)])
        except Exception:
            pass

        # Captured quaternions: feed in any handeye captures the manager
        # already has so the diversity score tracks real progress.
        captured_quats: list = []
        try:
            buf = manager._handeye_buffers.get(request.camera)
            if buf is not None:
                from physical_ai_server.workflow.auto_pose import _rotation_matrix_to_quaternion
                for R in buf.R_target2cam:
                    captured_quats.append(_rotation_matrix_to_quaternion(R))
        except Exception:
            captured_quats = []

        candidate = suggest_pose(captured_quats, board_centre_base=board_xyz)
        if candidate is None:
            response.success = False
            response.message = 'Konnte keine erreichbare Pose finden — bitte Tafel umpositionieren.'
            return response
        response.success = True
        response.target_x, response.target_y, response.target_z = (
            float(candidate.target_xyz[0]),
            float(candidate.target_xyz[1]),
            float(candidate.target_xyz[2]),
        )
        response.target_qx, response.target_qy, response.target_qz, response.target_qw = (
            float(candidate.target_quat[0]),
            float(candidate.target_quat[1]),
            float(candidate.target_quat[2]),
            float(candidate.target_quat[3]),
        )
        response.message = 'Pose vorgeschlagen.'
        return response

    def calibration_capture_color_callback(self, request, response):
        """Capture one cube colour for the per-classroom LAB profile.

        Bound to /calibration/capture_color. The student places a cube
        of the requested colour ('rot' | 'gruen' | 'blau' | 'gelb')
        centrally in the scene camera frame and the server segments +
        records its LAB cluster. See audit §1.7a.

        Enforces the scene-calibration prerequisite at the service
        boundary instead of relying on the frontend wizard ordering —
        without intrinsic + hand-eye for the scene camera the LAB
        cluster cannot be projected to base frame at runtime, so
        capturing without those is meaningless. (Replaces the dead
        start_step('color_profile') branch in calibration_manager.)
        """
        try:
            from physical_ai_server.workflow.color_profile import ColorProfileManager
            from physical_ai_server.workflow.calibration_manager import (
                CalibrationManager,
            )
        except Exception as e:
            self.get_logger().error(f'capture_color import failed: {e}')
            response.success = False
            response.message = 'Farbprofil-Modul fehlt.'
            response.lab_center = []
            response.lab_std = []
            return response
        # Prerequisite check: scene must be fully calibrated.
        try:
            cal = CalibrationManager()
            if not (cal.has_intrinsics('scene') and cal.has_handeye('scene')):
                response.success = False
                response.message = (
                    'Farbprofil benötigt eine kalibrierte Szenen-Kamera. '
                    'Bitte zuerst die Schritte 2 und 4 abschließen.'
                )
                response.lab_center = []
                response.lab_std = []
                return response
        except Exception as e:
            self.get_logger().warning(f'capture_color prereq check failed: {e}')
        bgr = self._get_latest_camera_frame('scene')
        if bgr is None:
            response.success = False
            response.message = 'Kein Kamerabild der Szenen-Kamera verfügbar.'
            response.lab_center = []
            response.lab_std = []
            return response
        try:
            mgr = ColorProfileManager()
            ok, message, center, std = mgr.capture(request.color, bgr)
            response.success = bool(ok)
            response.message = message
            response.lab_center = [float(v) for v in center]
            response.lab_std = [float(v) for v in std]
            return response
        except Exception as e:
            self.get_logger().error(f'capture_color failed: {e}')
            response.success = False
            response.message = 'Farbprofil konnte nicht gespeichert werden.'
            response.lab_center = []
            response.lab_std = []
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
            z_table = float(z_table_node.real()) if not z_table_node.empty() else 0.0
            fs.release()
            K = manager._intrinsics[request.camera]['K']
            dist = manager._intrinsics[request.camera]['dist']
            point = project_pixel_to_table(
                float(request.pixel_x), float(request.pixel_y), K, dist, T, z_table,
            )
            if point is None:
                response.success = False
                response.message = (
                    'Klick konnte nicht auf den Tisch projiziert werden.'
                )
                return response
            response.success = True
            response.world_x = float(point[0])
            response.world_y = float(point[1])
            response.world_z = float(point[2])
            response.message = f'Ziel "{request.label}" gespeichert.'
            # Persist into the WorkflowManager so the next workflow run
            # can resolve "ablegen bei <label>" without a second click.
            try:
                wfm = self._get_or_create_workflow_manager()
                if wfm is not None and request.label:
                    wfm.set_destination(
                        request.label,
                        float(point[0]), float(point[1]), float(point[2]),
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

    def calibration_execute_pose_callback(self, request, response):
        """Drive the arm to the auto-pose target via IK + chunked_publish.

        Plugs into the same trajectory publisher + safety envelope the
        Roboter Studio workflow runtime uses. Audit §1.7b — replaces
        the v1 stub that returned "Auto-Anfahren ist noch nicht aktiv".
        """
        import math as _math
        # Clear any leftover stop flag from the previous cancel/start so
        # this newly-requested motion isn't aborted on its first poll.
        self._calibration_stop_event.clear()
        manager = self._get_or_create_workflow_manager()
        if manager is None:
            response.success = False
            response.message = (
                'Workflow-Runtime kann nicht initialisiert werden — '
                'IK + Sicherheits-Envelope sind nicht verfügbar.'
            )
            return response
        # Need a fresh IK solver: build one or reuse the lazily-built
        # one on the workflow_manager via its factory.
        ik = None
        try:
            ik = self._build_ik_solver()
        except Exception as e:
            self.get_logger().error(f'IK build failed: {e}')
        if ik is None:
            response.success = False
            response.message = 'IK-Solver konnte nicht initialisiert werden.'
            return response

        raw_target_z = float(request.target_z)
        target_z = raw_target_z
        # Floor-clamp target_z against z_table from the scene-cam
        # hand-eye calibration so a caller bypassing auto_pose (or
        # auto_pose with a stale hemisphere) cannot drive the gripper
        # below the table plane. The 5 mm headroom keeps the tip from
        # scraping. When no scene handeye is solved yet, z_table is
        # absent and the clamp is skipped — auto_pose's hemisphere
        # already keeps suggested poses safe in that branch.
        try:
            calib = self._load_workflow_calibration()
            z_table = calib.get('z_table')
            if z_table is not None:
                floor = float(z_table) - 0.005
                if target_z < floor:
                    self.get_logger().warning(
                        f'execute_pose: target_z={raw_target_z:.4f} below '
                        f'z_table-5mm={floor:.4f}; clamping.'
                    )
                    target_z = floor
        except Exception as e:
            self.get_logger().warning(f'execute_pose: z_table clamp skipped: {e}')

        target_xyz = (
            float(request.target_x),
            float(request.target_y),
            target_z,
        )
        target_quat = (
            float(request.target_qx),
            float(request.target_qy),
            float(request.target_qz),
            float(request.target_qw),
        )
        # Locked yaw — we want the gripper to face the board the way
        # auto_pose suggested, not free-spin to a different approach.
        seed = getattr(self, '_last_published_joints', None)
        seed_arm = list(seed[:5]) if seed and len(seed) >= 5 else None
        try:
            arm_q = ik.solve_quat(target_xyz, target_quat, seed=seed_arm, free_yaw=False)
        except Exception as e:
            self.get_logger().error(f'IK solve_quat raised: {e}')
            response.success = False
            response.message = 'IK-Aufruf fehlgeschlagen.'
            return response
        if arm_q is None:
            response.success = False
            response.message = 'Pose außerhalb des Arbeitsbereichs.'
            return response

        # Build a 4-second segment from the cached last-published joints
        # to the target. The gripper joint stays put. On cold start
        # (server just rebuilt, no trajectory published yet), the cache
        # is empty — seed from /joint_states via the communicator so the
        # segment starts from where the arm ACTUALLY is, not a hardcoded
        # HOME it may not be at. The HOME fallback only fires when the
        # joint-state subscription is also empty (very early startup),
        # logged loudly because the first commanded waypoint then
        # bypasses the per-tick delta cap.
        from physical_ai_server.workflow.trajectory_builder import (
            build_segment, chunked_publish,
        )
        HOME = [0.0, -_math.pi / 4, _math.pi / 4, 0.0, 0.0]
        last = getattr(self, '_last_published_joints', None)
        if not last or len(last) < 6:
            live = None
            if self.communicator is not None:
                joints_getter = getattr(
                    self.communicator, 'get_latest_follower_joints', None
                )
                if callable(joints_getter):
                    try:
                        live = joints_getter()
                    except Exception as e:
                        self.get_logger().warning(
                            f'execute_pose: live joint seed failed: {e}'
                        )
            if live and len(live) >= 6:
                last = list(live)
            else:
                self.get_logger().warning(
                    'execute_pose: no cached or live joints — falling back '
                    'to HOME. First-tick teleport possible if the arm is '
                    'not actually at HOME.'
                )
                last = list(HOME) + [0.8]
        full_target = list(arm_q) + [last[5]]
        waypoints = build_segment(last, full_target, duration_s=4.0)
        # Bind the cancel event so /calibration/cancel can interrupt a
        # mis-planned 4-second motion. chunked_publish polls this between
        # chunks (≤50 ms latency), so the worst-case time from cancel to
        # arm-stationary is one chunk_duration_s plus a poll tick.
        try:
            ok = chunked_publish(
                publisher=self._trajectory_publisher,
                points=waypoints,
                safety_apply=manager._safety.apply if hasattr(manager, '_safety') else None,
                should_stop=self._calibration_stop_event.is_set,
            )
        except Exception as e:
            self.get_logger().error(f'execute_pose chunked_publish raised: {e}')
            response.success = False
            response.message = 'Bewegung fehlgeschlagen.'
            return response
        if not ok:
            response.success = False
            response.message = 'Bewegung wurde abgebrochen.'
            return response
        response.success = True
        response.message = 'Pose erreicht.'
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
            # visible_apriltag_ids / color_counts / visible_object_classes
            # fields (which were hardcoded empty before this cache).
            # Each detection block in the workflow runtime emits a new
            # list (color/object/apriltag); the cache holds whichever
            # ran most recently. TTL is enforced in the timer callback
            # so a stale list from a finished workflow doesn't keep
            # showing up.
            if detections:
                self._workflow_last_detections = list(detections)
                self._workflow_last_detections_ts = time.monotonic()
        except Exception as e:
            self.get_logger().warning(f'workflow status publish error: {e}')

    def _trajectory_publisher(self, points):
        """Publish a chunk of (q, t_from_start_s) tuples as one
        JointTrajectory message on /leader/joint_trajectory. Bridges the
        chunked_publish API to the existing topic the controller listens
        on. Side-effect: caches the LAST published joint vector so the
        calibration_execute_pose handler can chain segments without
        depending on a /joint_states subscription."""
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
            get_current_pose_xyz=self._get_current_gripper_xyz,
            # Audit S2: lambda so the call is deferred to workflow start
            # time (communicator may not yet be wired at WorkflowManager
            # construction). Returns None on absent comm or stale joints.
            get_follower_joints=lambda: (
                self.communicator.get_latest_follower_joints()
                if self.communicator is not None
                else None
            ),
        )
        joint_min, joint_max, max_delta = self._load_safety_clamps()
        try:
            self.workflow_manager.configure_safety(
                joint_min=joint_min,
                joint_max=joint_max,
                max_delta_per_tick=max_delta,
            )
        except Exception as e:
            self.get_logger().warning(f'Workflow safety envelope not configured: {e}')
        return self.workflow_manager

    def _load_safety_clamps(self):
        """Single source of truth for the per-joint clamp + per-tick
        delta cap. Reads ``safety_envelope`` from omx_f_config.yaml when
        the keys are present; falls back to the same hardcoded defaults
        the inference path uses if the config is missing or malformed."""
        import math as _math
        _pi = _math.pi
        # Mirror the safety_envelope block in omx_f_config.yaml — see the
        # comment there for the joint1/4/5 tightening rationale.
        defaults_min = [-_pi / 2, -_pi / 2, -_pi / 2, -0.85 * _pi, -0.85 * _pi, -1.0]
        defaults_max = [_pi / 2, _pi / 2, _pi / 2, 0.85 * _pi, 0.85 * _pi, 1.0]
        defaults_delta = [0.3] * 6

        try:
            robot_type = getattr(self, 'robot_type', None) or 'omx_f'
            param_names = [
                'safety_envelope.joint_min',
                'safety_envelope.joint_max',
                'safety_envelope.max_delta_per_tick_at_30hz',
            ]
            declare_parameters(
                node=self,
                robot_type=robot_type,
                param_names=param_names,
                default_value=[0.0],
            )
            values = load_parameters(
                node=self,
                robot_type=robot_type,
                param_names=param_names,
            )
            j_min = list(values.get('safety_envelope.joint_min') or [])
            j_max = list(values.get('safety_envelope.joint_max') or [])
            d_max = list(values.get('safety_envelope.max_delta_per_tick_at_30hz') or [])
            if len(j_min) == 6 and len(j_max) == 6 and len(d_max) == 6:
                return j_min, j_max, d_max
        except Exception as e:
            self.get_logger().info(f'safety_envelope config not available, using defaults: {e}')
        return defaults_min, defaults_max, defaults_delta

    def _build_ik_solver(self):
        """Construct an IKSolver instance from the URDF in
        ``/robot_description``. Returns None if the param isn't
        available (the motion handler will then surface a German error)."""
        try:
            from rcl_interfaces.srv import GetParameters
            urdf_string = None
            if self.has_parameter('robot_description'):
                urdf_string = self.get_parameter('robot_description').value
            if not urdf_string:
                # Try cross-node parameter lookup. /robot_state_publisher
                # canonically owns robot_description on a typical bringup.
                client = self.create_client(GetParameters, '/robot_state_publisher/get_parameters')
                if client.wait_for_service(timeout_sec=1.0):
                    request = GetParameters.Request()
                    request.names = ['robot_description']
                    future = client.call_async(request)
                    rclpy.spin_until_future_complete(self, future, timeout_sec=2.0)
                    if future.done() and future.result() is not None:
                        params = future.result().values
                        if params and params[0].string_value:
                            urdf_string = params[0].string_value
                self.destroy_client(client)
            if not urdf_string:
                self.get_logger().warning('robot_description not available; IK disabled.')
                return None
            from physical_ai_server.workflow.ik_solver import IKSolver
            return IKSolver(urdf_string=urdf_string)
        except Exception as e:
            self.get_logger().error(f'IKSolver init failed: {e}')
            return None

    def _build_perception(self):
        # No silent fallback: any failure here is a build/config bug
        # that should surface as a German error in the workflow editor
        # instead of a workflow that "runs" with disabled perception.
        # WorkflowManager.start wraps this call in its own try/except
        # and reports the message back to the student.
        from physical_ai_server.workflow.perception import Perception
        from physical_ai_server.workflow.color_profile import ColorProfileManager
        perc = Perception()
        color_mgr = ColorProfileManager()
        profile = {}
        for color in ('rot', 'gruen', 'blau', 'gelb'):
            entry = color_mgr.lab_profile(color)
            if entry is not None:
                profile[color] = entry
        perc.set_color_profile(profile)
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
        """Load scene intrinsics + extrinsics + z_table for projection
        of perception detections to base-frame XYZ. Returns a dict
        consumed by ``WorkflowContext``.

        Audit fix: the v1 ship returned ``{}`` unless BOTH
        scene_intrinsics.yaml AND scene_handeye.yaml existed — even
        scene_intrinsics alone (which Auto-Pose already needs to derive
        ``z_table`` for the gripper-handeye pose suggestions) was thrown
        away. Now we load whatever is available and fill the rest with
        sensible defaults. ``z_table`` falls back to 0.0 only when the
        scene-handeye YAML is genuinely missing; once that exists, even
        if the intrinsics are stale, the table-plane projection still
        works."""
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
                fs.release()
                if K is not None and dist is not None:
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
                z_table = float(z_node.real()) if not z_node.empty() else 0.0
                fs.release()
                if T is not None:
                    result['scene_extrinsics'] = T
                result['z_table'] = z_table
            except Exception as e:
                self.get_logger().warning(
                    f'scene_handeye.yaml load failed: {e}'
                )
        return result

    def workflow_start_callback(self, request, response):
        ok, msg = self._assert_no_other_active('workflow')
        if not ok:
            response.success = False
            response.message = msg
            response.unreachable_block_ids = []
            response.unreachable_messages = []
            return response
        manager = self._get_or_create_workflow_manager()
        if manager is None:
            response.success = False
            response.message = 'Workflow-Runtime kann nicht initialisiert werden.'
            response.unreachable_block_ids = []
            response.unreachable_messages = []
            return response
        # Phase-3: forward cloud_vision_enabled and resolve the burst
        # callable here. The runtime block reads ctx.cloud_vision['enabled']
        # and ctx.cloud_vision['cloud_burst'] (callable) to decide whether
        # to bypass to Modal OWLv2. When disabled, the open-vocab block
        # raises the German "Cloud-Erkennung deaktiviert" error.
        # The auth_token (Supabase JWT) is cached on the node so
        # _cloud_vision_burst can attach it as a Bearer header. Lives
        # only in memory for the lifetime of the run.
        auth_token = str(getattr(request, 'auth_token', '') or '')
        self._cloud_vision_auth_token = auth_token if auth_token else None
        cloud_burst = self._cloud_vision_burst if auth_token else None
        cloud_vision = {
            'enabled': bool(getattr(request, 'cloud_vision_enabled', False)),
            'translate': self._cloud_vision_synonyms(),
            'cloud_burst': cloud_burst,
        }
        success, message, unreachable = manager.start(
            request.workflow_json,
            request.workflow_id,
            cloud_vision=cloud_vision,
        )
        if success:
            self.on_workflow = True
            # The on_finished callback (passed into WorkflowManager via
            # _get_or_create_workflow_manager) flips this back to False
            # when the daemon thread exits, regardless of how it exited.
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
        return response

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

    def workflow_stop_callback(self, request, response):
        manager = self.workflow_manager
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
        manager = self.workflow_manager
        if manager is None or not manager.is_running:
            response.success = False
            response.message = 'Es läuft kein Workflow.'
            return response
        success, message = manager.pause()
        response.success = success
        response.message = message
        return response

    def workflow_step_callback(self, request, response):
        manager = self.workflow_manager
        if manager is None or not manager.is_running:
            response.success = False
            response.message = 'Es läuft kein Workflow.'
            return response
        success, message = manager.step()
        response.success = success
        response.message = message
        return response

    def workflow_continue_callback(self, request, response):
        manager = self.workflow_manager
        if manager is None or not manager.is_running:
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
        manager = self._get_or_create_workflow_manager()
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
        response.detected = False
        response.corners_x = []
        response.corners_y = []
        response.board_area_pct = 0
        response.message = 'Live-Vorschau wird in einer späteren Version aktiviert.'
        return response

    def calibration_verify_callback(self, request, response):
        response.success = False
        response.predicted_pixel_x = 0.0
        response.predicted_pixel_y = 0.0
        response.residual_mm = 0.0
        response.message = 'Kalibrierungsprüfung wird in einer späteren Version aktiviert.'
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
    # Phase-3 cloud-vision plumbing
    # ------------------------------------------------------------------
    def _cloud_vision_synonyms(self) -> dict[str, dict]:
        """Local German→COCO synonym dict consulted BEFORE bursting to
        the Modal OWLv2 endpoint. Each entry maps a German prompt to a
        local-detection fast-path that skips the cloud roundtrip.

        Format per entry:
          {'mode': 'object'|'color', 'class': '<german_label>',
           'color': '<rot|gruen|blau|gelb>'}

        German class labels MUST match keys in COCO_CLASSES (e.g.
        ``Tasse``, ``Banane``, ``Apfel``) so the downstream YOLO filter
        (``coco_class in COCO_CLASSES``) matches; English labels here
        would silently return every detected object unfiltered (audit F1).

        Audit F55: also consults
        ``/root/.cache/edubotics/cloud_vision_synonyms.yaml`` if present
        (the calibration volume survives ``docker compose down``).
        This lets a classroom add "Stift", "Lineal", "Maus" without a
        Hub rebuild + image pull. Hardcoded fallback below stays as
        the source of truth so the system still works without the
        YAML file.

        The perception_blocks.detect_open_vocab handler resolves a prompt
        against this dict first; on miss it calls ctx.cloud_vision[
        'cloud_burst'] (the bound _cloud_vision_burst method) to reach
        Modal via the cloud_training_api proxy.
        """
        hardcoded = self._cloud_vision_synonyms_hardcoded()
        try:
            from pathlib import Path as _Path
            yaml_path = _Path('/root/.cache/edubotics/cloud_vision_synonyms.yaml')
            if not yaml_path.exists():
                return hardcoded
            try:
                import yaml as _yaml
            except ImportError:
                self.get_logger().warning(
                    'cloud_vision_synonyms.yaml present but PyYAML missing — '
                    'falling back to hardcoded dict.'
                )
                return hardcoded
            with yaml_path.open('r', encoding='utf-8') as fh:
                custom = _yaml.safe_load(fh) or {}
            if not isinstance(custom, dict):
                self.get_logger().warning(
                    'cloud_vision_synonyms.yaml is not a mapping — ignored.'
                )
                return hardcoded
            merged: dict[str, dict] = dict(hardcoded)
            for k, v in custom.items():
                if isinstance(k, str) and isinstance(v, dict):
                    merged[k.lower()] = v
            return merged
        except Exception as e:
            self.get_logger().warning(
                f'cloud_vision_synonyms.yaml load failed, using hardcoded: {e}'
            )
            return hardcoded

    def _cloud_vision_synonyms_hardcoded(self) -> dict[str, dict]:
        return {
            'rote tasse': {'mode': 'object', 'class': 'Tasse', 'color': 'rot'},
            'tasse': {'mode': 'object', 'class': 'Tasse'},
            'banane': {'mode': 'object', 'class': 'Banane'},
            'gelbe banane': {'mode': 'object', 'class': 'Banane', 'color': 'gelb'},
            'flasche': {'mode': 'object', 'class': 'Flasche'},
            'apfel': {'mode': 'object', 'class': 'Apfel'},
            'roter apfel': {'mode': 'object', 'class': 'Apfel', 'color': 'rot'},
            'orange': {'mode': 'object', 'class': 'Orange'},
            'buch': {'mode': 'object', 'class': 'Buch'},
            'schere': {'mode': 'object', 'class': 'Schere'},
            'roter wuerfel': {'mode': 'color', 'color': 'rot'},
            'roter würfel': {'mode': 'color', 'color': 'rot'},
            'blauer wuerfel': {'mode': 'color', 'color': 'blau'},
            'blauer würfel': {'mode': 'color', 'color': 'blau'},
            'gruener wuerfel': {'mode': 'color', 'color': 'gruen'},
            'grüner würfel': {'mode': 'color', 'color': 'gruen'},
            'gelber wuerfel': {'mode': 'color', 'color': 'gelb'},
            'gelber würfel': {'mode': 'color', 'color': 'gelb'},
        }

    def _cloud_vision_burst(self, bgr_frame, prompt: str, should_stop=None):
        """Forward a BGR frame and a German prompt to the cloud_training_api
        /vision/detect endpoint. The endpoint proxies to the Modal
        edubotics-vision app (OWLv2). Returns a list of perception.Detection
        objects (possibly empty).

        Requires:
          - ``EDUBOTICS_CLOUD_API_URL`` env var (set by GUI / compose .env)
          - A valid student JWT cached on ``self._cloud_vision_auth_token``
            by ``workflow_start_callback`` (forwarded from the React app's
            StartWorkflow.srv ``auth_token`` field).

        Raises ``WorkflowError`` (German) on any failure so the workflow
        runtime surfaces a clean message to the student.

        Audit O3: ``should_stop`` is a callable from the workflow ctx so
        we can abort between JPEG encode and the requests.post — without
        it, a 15s cold-start HTTP wait can't be cancelled by /workflow/stop
        and the student is billed for a burst they cancelled. Passed
        through perception_blocks.detect_open_vocab so it tracks the
        active context's stop event.
        """
        import base64
        import os

        import cv2
        from physical_ai_server.workflow.handlers.motion import WorkflowError
        from physical_ai_server.workflow.perception import Detection

        cloud_api_url = os.environ.get('EDUBOTICS_CLOUD_API_URL', '').rstrip('/')
        if not cloud_api_url:
            raise WorkflowError(
                'Cloud-Erkennung ist auf diesem Server nicht konfiguriert '
                '(EDUBOTICS_CLOUD_API_URL fehlt).'
            )
        # Audit F58: refuse to burst on a >2 s-stale scene frame so we
        # don't pay Modal to detect objects in a dead view. The caller
        # passes the frame they already grabbed; we just confirm the
        # underlying communicator received it recently.
        if self.communicator is not None:
            age_getter = getattr(self.communicator, 'get_camera_msg_age_s', None)
            if callable(age_getter):
                try:
                    age = age_getter('scene')
                except Exception:
                    age = None
                if age is not None and age > 2.0:
                    from physical_ai_server.workflow.handlers.motion import WorkflowError as _WE
                    raise _WE(
                        'Szenenbild ist veraltet (kein neues Bild seit '
                        f'{age:.1f}s). Cloud-Erkennung abgebrochen.'
                    )
        token = getattr(self, '_cloud_vision_auth_token', None)
        if not token:
            raise WorkflowError(
                'Anmeldung fehlt für Cloud-Erkennung — bitte erneut einloggen '
                'und Workflow neu starten.'
            )
        try:
            import requests  # type: ignore
        except ImportError:
            raise WorkflowError(
                'HTTP-Client (requests) ist nicht installiert — bitte Image '
                'neu bauen.'
            )

        ok, jpg = cv2.imencode(
            '.jpg', bgr_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80],
        )
        if not ok:
            raise WorkflowError('JPEG-Kompression des Szenenbildes fehlgeschlagen.')
        b64 = base64.b64encode(jpg.tobytes()).decode('ascii')

        # Audit O3: re-check should_stop after the encode but BEFORE
        # the HTTP send — encode can take a few ms on slower hosts, and
        # the student may have hit Stop in between.
        if callable(should_stop) and should_stop():
            raise WorkflowError('Workflow wurde gestoppt.')

        try:
            response = requests.post(
                f'{cloud_api_url}/vision/detect',
                json={
                    'image_b64': b64,
                    'prompts': [prompt],
                    'score_threshold': 0.25,
                },
                headers={'Authorization': f'Bearer {token}'},
                timeout=15,
            )
        except requests.exceptions.Timeout:
            raise WorkflowError(
                'Cloud-Erkennung antwortet nicht — bitte erneut versuchen.'
            )
        except requests.exceptions.RequestException as e:
            raise WorkflowError(
                f'Cloud-Erkennung nicht erreichbar: {type(e).__name__}.'
            )

        if response.status_code == 401 or response.status_code == 403:
            raise WorkflowError(
                'Anmeldung für Cloud-Erkennung abgelaufen — bitte neu einloggen.'
            )
        if response.status_code == 429:
            raise WorkflowError(
                'Cloud-Erkennungs-Kontingent für dieses Halbjahr erreicht.'
            )
        if response.status_code == 503:
            raise WorkflowError(
                'Cloud-Erkennung ist gerade nicht erreichbar.'
            )
        if response.status_code == 504:
            raise WorkflowError(
                'Cloud-Erkennung lädt noch — bitte gleich erneut versuchen.'
            )
        if response.status_code >= 400:
            raise WorkflowError(
                'Cloud-Erkennung ist fehlgeschlagen. Bitte erneut versuchen.'
            )

        try:
            data = response.json()
        except ValueError:
            raise WorkflowError(
                'Cloud-Erkennung lieferte ungültige Antwort.'
            )

        raw_detections = data.get('detections', []) or []
        h, w = bgr_frame.shape[:2]
        out: list = []
        for d in raw_detections:
            try:
                bbox = d.get('bbox') or [0, 0, 0, 0]
                x1, y1, x2, y2 = (float(bbox[0]), float(bbox[1]),
                                  float(bbox[2]), float(bbox[3]))
                x1 = max(0, min(w - 1, int(round(x1))))
                y1 = max(0, min(h - 1, int(round(y1))))
                x2 = max(0, min(w - 1, int(round(x2))))
                y2 = max(0, min(h - 1, int(round(y2))))
                bw = max(1, x2 - x1)
                bh = max(1, y2 - y1)
                cx = x1 + bw // 2
                cy = y1 + bh // 2
                out.append(Detection(
                    centroid_px=(cx, cy),
                    bbox_px=(x1, y1, bw, bh),
                    confidence=float(d.get('score', 0.0)),
                    label=str(d.get('label', prompt)),
                ))
            except (TypeError, ValueError, KeyError):
                continue
        return out

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
            color_counts = [0, 0, 0, 0]  # [rot, gruen, blau, gelb]
            object_classes: list[str] = []
            last = self._workflow_last_detections or []
            last_ts = self._workflow_last_detections_ts
            if last and (time.monotonic() - last_ts) < 2.0:
                color_index = {'rot': 0, 'gruen': 1, 'blau': 2, 'gelb': 3}
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
                        continue
                    idx = color_index.get(label)
                    if idx is not None:
                        color_counts[idx] += 1
                        continue
                    # Anything else is treated as an object class
                    # (COCO names + open-vocab prompts). De-dupe to
                    # keep the chip count compact for the UI.
                    if label and label not in object_classes:
                        object_classes.append(label)
            msg.visible_apriltag_ids = apriltag_ids
            msg.color_counts = color_counts
            msg.visible_object_classes = object_classes
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
    # Multi-threaded executor so /calibration/cancel can dispatch while
    # /calibration/execute_pose is blocked inside chunked_publish's
    # 4-second sleep loop. Without this the stop-event polling at
    # ~50ms cadence is unreachable — a single-threaded spin would
    # serialise both callbacks and the cancel signal would never
    # arrive in time. Three threads is enough for the typical wizard
    # (one execute_pose in flight + concurrent cancel + status).
    from rclpy.executors import MultiThreadedExecutor
    executor = MultiThreadedExecutor(num_threads=3)
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
