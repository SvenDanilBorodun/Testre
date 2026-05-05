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
import os
from pathlib import Path
import threading
import time
import traceback
from typing import Optional

from ament_index_python.packages import get_package_share_directory
from physical_ai_interfaces.msg import (
    HFOperationStatus,
    TaskStatus,
    TrainingStatus,
    WorkflowStatus,
)
from physical_ai_interfaces.srv import (
    AutoPoseSuggest,
    CalibrationCaptureFrame,
    CalibrationSolve,
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

    def _init_ros_service(self):
        self.get_logger().info('Initializing ROS services...')
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
            ('/workshop/mark_destination', MarkDestination, self.mark_destination_callback),
            ('/workflow/start', StartWorkflow, self.workflow_start_callback),
            ('/workflow/stop', StopWorkflow, self.workflow_stop_callback),
        ]

        for service_name, service_type, callback in service_definitions:
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
            response.message = f'Error in set_hf_user_callback:\n{str(e)}'

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
            response.message = f'Failed to retrieve Hugging Face user ID:\n{str(e)}'

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
        if camera_msgs is None:
            if time.perf_counter() - self.start_recording_time > self.DEFAULT_TOPIC_TIMEOUT:
                error_msg = 'Camera data not received within timeout period'
                self.get_logger().error(error_msg)
            else:
                self.get_logger().info('Waiting for camera data...')
                return

        elif follower_msgs is None:
            if time.perf_counter() - self.start_recording_time > self.DEFAULT_TOPIC_TIMEOUT:
                error_msg = 'Follower data not received within timeout period'
                self.get_logger().error(error_msg)
            else:
                self.get_logger().info('Waiting for follower data...')
                return

        elif leader_msgs is None:
            if time.perf_counter() - self.start_recording_time > self.DEFAULT_TOPIC_TIMEOUT:
                error_msg = 'Leader data not received within timeout period'
                self.get_logger().error(error_msg)
            else:
                self.get_logger().info('Waiting for leader data...')
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
            error_msg = f'Failed to convert messages: {str(e)}, please check the robot type again!'
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
            response.dataset_list = []
            response.success = False
            response.message = f'Error: {str(e)}'

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
            response.message = f'Failed to set robot type: {str(e)}'
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

    def _get_latest_camera_frame(self, camera: str):
        """Provider hook for the calibration manager. Returns the most recent
        BGR frame for the named camera, or None if unavailable. Hardware
        wiring lives in a follow-up — until then this returns None and the
        manager produces a German user-facing error message."""
        try:
            if self.communicator is None:
                return None
            getter = getattr(self.communicator, 'get_latest_bgr_frame', None)
            if callable(getter):
                return getter(camera)
        except Exception as e:
            self.get_logger().warning(f'Camera frame provider error: {e}')
        return None

    def _get_current_gripper_pose(self):
        """Provider hook for hand-eye calibration. Returns (R 3x3, t 3,) of
        gripper-in-base, or None when unavailable."""
        try:
            if self.communicator is None:
                return None
            getter = getattr(self.communicator, 'get_current_gripper_pose', None)
            if callable(getter):
                return getter()
        except Exception as e:
            self.get_logger().warning(f'Gripper pose provider error: {e}')
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
        success, captured, required, last_rms, message = manager.capture_frame(request.camera)
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
        success, reproj, disagreement, message = manager.solve(request.camera, request.step)
        response.success = success
        response.reprojection_error = reproj
        response.method_disagreement = disagreement
        response.message = message
        if success and request.step == 'handeye':
            # Releasing the calibration mutex on the final solve lets the
            # student return to the editor; intermediate solves leave the
            # flag set so they can keep capturing more frames.
            self.on_calibration = False
        return response

    def calibration_auto_pose_callback(self, request, response):
        try:
            from physical_ai_server.workflow.auto_pose import suggest_pose
        except ImportError as e:
            response.success = False
            response.message = f'Auto-Pose-Modul fehlt: {e}'
            return response
        manager = self._get_or_create_calibration_manager()
        if manager is None:
            response.success = False
            response.message = 'Kalibrierung kann nicht initialisiert werden.'
            return response
        # Captured quaternions diversity check intentionally weak in v1 (no
        # IK reachability yet); PR3 will swap the stub for the real IK.
        candidate = suggest_pose([])
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
            response.success = False
            response.message = f'Projektionsfehler: {e}'
            return response

    def calibration_execute_pose_callback(self, request, response):
        # Real motion is wired in PR3 via SafetyEnvelope + IKSolver +
        # chunked_publish. PR1 leaves this as a stub returning a German
        # message so the wizard surfaces the dependency clearly.
        response.success = False
        response.message = (
            'Auto-Anfahren ist noch nicht aktiv — bitte den Roboter manuell zur '
            'vorgeschlagenen Pose bewegen oder auf das nächste Update warten.'
        )
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
            self.workflow_status_publisher.publish(msg)
        except Exception as e:
            self.get_logger().warning(f'workflow status publish error: {e}')

    def _trajectory_publisher(self, points):
        """Publish a chunk of (q, t_from_start_s) tuples as one
        JointTrajectory message on /leader/joint_trajectory. Bridges the
        chunked_publish API to the existing topic the controller listens
        on."""
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
        for q, t in points:
            point = JointTrajectoryPoint()
            point.positions = [float(v) for v in q]
            point.time_from_start.sec = int(t)
            point.time_from_start.nanosec = int((t - int(t)) * 1e9)
            msg.points.append(point)
        self._workflow_traj_publisher.publish(msg)

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
            get_scene_frame=lambda: self._get_latest_camera_frame('scene'),
            get_gripper_frame=lambda: self._get_latest_camera_frame('gripper'),
            get_current_pose_xyz=self._get_current_gripper_xyz,
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
        defaults_min = [-_pi, -_pi / 2, -_pi / 2, -_pi, -_pi, -1.0]
        defaults_max = [_pi, _pi / 2, _pi / 2, _pi, _pi, 1.0]
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
        try:
            from physical_ai_server.workflow.perception import Perception
            from physical_ai_server.workflow.color_profile import ColorProfileManager
            perc = Perception()
            color_mgr = ColorProfileManager()
            profile = {}
            for color in ('rot', 'gruen', 'blau', 'gelb'):
                rng = color_mgr.hsv_range(color)
                if rng is not None:
                    profile[color] = rng
            perc.set_color_profile(profile)
            return perc
        except Exception as e:
            self.get_logger().error(f'Perception init failed: {e}')
            return None

    def _get_current_gripper_xyz(self):
        """Convenience wrapper that returns just the (x, y, z) of the
        current gripper pose for the destination_current handler. Falls
        back to None if the communicator doesn't expose forward
        kinematics."""
        try:
            if self.communicator is None:
                return None
            getter = getattr(self.communicator, 'get_current_gripper_xyz', None)
            if callable(getter):
                return getter()
        except Exception:
            return None
        return None

    def _load_workflow_calibration(self):
        """Load scene intrinsics + extrinsics + z_table for projection
        of perception detections to base-frame XYZ. Returns a dict
        consumed by ``WorkflowContext`` — empty when calibration files
        are missing (perception still runs but world_xyz_m won't be
        populated)."""
        try:
            from pathlib import Path
            import os
            import cv2
            calib_dir = Path(os.environ.get('EDUBOTICS_CALIB_DIR', '/root/.cache/edubotics/calibration'))
            scene_intrinsic_path = calib_dir / 'scene_intrinsics.yaml'
            scene_handeye_path = calib_dir / 'scene_handeye.yaml'
            if not scene_intrinsic_path.exists() or not scene_handeye_path.exists():
                return {}
            fs = cv2.FileStorage(str(scene_intrinsic_path), cv2.FILE_STORAGE_READ)
            K = fs.getNode('camera_matrix').mat()
            dist = fs.getNode('distortion_coefficients').mat()
            fs.release()
            fs = cv2.FileStorage(str(scene_handeye_path), cv2.FILE_STORAGE_READ)
            T = fs.getNode('transform').mat()
            z_node = fs.getNode('z_table')
            z_table = float(z_node.real()) if not z_node.empty() else 0.0
            fs.release()
            return {
                'scene_intrinsics': {'K': K, 'dist': dist},
                'scene_extrinsics': T,
                'z_table': z_table,
            }
        except Exception as e:
            self.get_logger().warning(f'Calibration load failed: {e}')
            return {}

    def workflow_start_callback(self, request, response):
        ok, msg = self._assert_no_other_active('workflow')
        if not ok:
            response.success = False
            response.message = msg
            return response
        manager = self._get_or_create_workflow_manager()
        if manager is None:
            response.success = False
            response.message = 'Workflow-Runtime kann nicht initialisiert werden.'
            return response
        success, message = manager.start(request.workflow_json, request.workflow_id)
        if success:
            self.on_workflow = True
            # Reset on completion via the daemon thread; we don't have a
            # neat callback so a wakeup-style poll happens in the timer.
        response.success = success
        response.message = message
        return response

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
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Cleanup HF API Worker before destroying node
        node._cleanup_hf_api_worker()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
