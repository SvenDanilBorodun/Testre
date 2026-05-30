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
# Author: Dongyun Kim

from typing import Any, Dict, List

from builtin_interfaces.msg import Duration
import cv2
from cv_bridge import CvBridge
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
import numpy as np
from sensor_msgs.msg import CompressedImage, JointState
import torch
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint


class DataConverter:

    def __init__(self):
        self._image_converter = CvBridge()  # Image converter using CVBridge
        self._joint_converter = None  # Joint data converter
        # Action-message time_from_start. Historically hardcoded to 50 ms,
        # which is ~1.5x the period at 30 Hz and breaks at any other fps.
        # The ROS node should call `set_action_duration_from_fps(fps)` after
        # reading task_info so the JointTrajectoryController paces
        # correctly. Default 50 ms preserves the original behavior.
        self._action_duration_ns: int = 50_000_000
        # Tracks which "extra joints in trajectory" warnings have already
        # fired so we don't log at 30 Hz.
        self._warned_extra_joints: set = set()

    def set_action_duration_from_fps(self, fps: float) -> None:
        """Configure time_from_start on published action messages.

        Setting this to ~1.5 / fps gives the controller room to finish the
        previous tick's command before the next arrives, which keeps motion
        smooth at any recording rate.
        """
        if fps and fps > 0:
            self._action_duration_ns = max(
                int(1.5 * 1e9 / fps),
                1_000_000,  # 1 ms floor
            )

    def compressed_image2cvmat(
            self,
            msg: CompressedImage,
            desired_encoding: str = 'bgr8') -> np.ndarray:
        # Audit F5: 'passthrough' returned whatever cv_bridge decoded,
        # then callers (data_manager.convert_msgs_to_raw_datas) ran an
        # unconditional BGR2RGB swap on the result. usb_cam MJPEG
        # happens to decode BGR today so it works — but any future
        # driver/kernel returning a non-BGR decode would silently
        # mis-train. 'bgr8' makes cv_bridge raise on a mismatch.
        try:
            cv_image = self._image_converter.compressed_imgmsg_to_cv2(
                    msg,
                    desired_encoding=desired_encoding)
            if cv_image is None:
                raise RuntimeError('cv_bridge returned None')
            if cv_image.dtype == np.uint16:
                cv_image = cv2.normalize(
                        cv_image,
                        None,
                        0,
                        255,
                        cv2.NORM_MINMAX,
                        dtype=cv2.CV_8U)
            return cv_image
        except Exception as e:
            raise RuntimeError(f'Failed to convert compressed image: {str(e)}')

    def joint_trajectory2tensor_array(
            self,
            msg: JointTrajectory,
            joint_order: List[str],
            target_format: str = 'numpy') -> Any:

        try:
            joint_pos_map = dict(zip(
                msg.joint_names,
                msg.points[0].positions
            ))

            # Surface joints that the incoming message has but we're about
            # to drop — a reconfigured robot (7th actuator) would silently
            # lose that joint from every recorded action without this.
            extras = set(msg.joint_names) - set(joint_order)
            if extras:
                key = tuple(sorted(extras))
                if key not in self._warned_extra_joints:
                    self._warned_extra_joints.add(key)
                    import sys
                    print(
                        f'[WARNUNG] JointTrajectory enthält zusätzliche '
                        f'Gelenke {sorted(extras)}, die nicht in joint_order '
                        f'{joint_order} stehen. Diese werden verworfen. '
                        f'Bitte robot-config prüfen, falls der Roboter '
                        f'umgebaut wurde.',
                        file=sys.stderr, flush=True,
                    )

            ordered_positions = [
                joint_pos_map[name]
                for name in joint_order
            ]
            if target_format == 'numpy':
                return np.array(ordered_positions, dtype=np.float32)
            elif target_format == 'torch':
                return torch.tensor(ordered_positions, dtype=torch.float32)
            else:
                raise ValueError(f'Unsupported target format: {target_format}')
        except Exception as e:
            raise RuntimeError(f'Failed to convert joint trajectory: {str(e)}')

    def joint_state2tensor_array(
            self,
            msg: JointState,
            joint_order: List[str],
            target_format: str = 'numpy') -> Any:

        try:
            joint_pos_map = dict(zip(
                msg.name,
                msg.position
            ))
            ordered_positions = [
                joint_pos_map[name] for name in joint_order
            ]
            if target_format == 'numpy':
                return np.array(ordered_positions, dtype=np.float32)
            elif target_format == 'torch':
                return torch.tensor(ordered_positions, dtype=torch.float32)
            else:
                raise ValueError(f'Unsupported target format: {target_format}')
        except Exception as e:
            raise RuntimeError(f'Failed to convert joint state: {str(e)}')

    def twist2tensor_array(
            self,
            msg: Twist,
            target_format: str = 'numpy') -> Any:

        try:
            linear = np.array([
                msg.linear.x,
                msg.linear.y
            ], dtype=np.float32)
            angular = np.array([
                msg.angular.z
            ], dtype=np.float32)

            if target_format == 'numpy':
                return np.concatenate((linear, angular))
            elif target_format == 'torch':
                return torch.tensor(
                    np.concatenate((linear, angular)), dtype=torch.float32)
            else:
                raise ValueError(
                    f'Unsupported target format: {target_format}')
        except Exception as e:
            raise RuntimeError(
                f'Failed to convert twist message: {str(e)}')

    def odometry2tensor_array(
            self,
            msg: Odometry,
            target_format: str = 'numpy') -> Any:

        try:
            position = np.array([
                msg.twist.twist.linear.x,
                msg.twist.twist.linear.y
            ], dtype=np.float32)
            orientation = np.array([
                msg.twist.twist.angular.z
            ], dtype=np.float32)

            if target_format == 'numpy':
                return np.concatenate((position, orientation))
            elif target_format == 'torch':
                return torch.tensor(
                    np.concatenate((position, orientation)), dtype=torch.float32)
            else:
                raise ValueError(
                    f'Unsupported target format: {target_format}')
        except Exception as e:
            raise RuntimeError(
                f'Failed to convert odometry message: {str(e)}')

    def tensor_array2joint_msgs(
            self,
            action,
            leader_topic_types: Dict[str, Any],
            leader_joint_orders: Dict[str, List[str]]):

        start_idx = 0
        joint_pub_msgs = {}

        for key, value in leader_joint_orders.items():
            count = len(value)
            action_slice = action[start_idx:start_idx + count]
            start_idx += count
            if key.startswith('joint_order.'):
                key = key.replace('joint_order.', '')
            if leader_topic_types[key] == JointTrajectory:
                # Duration is set from task_info.fps via
                # set_action_duration_from_fps(). Defaults to 50 ms for
                # backward compatibility at 30 Hz recordings.
                dur_ns = self._action_duration_ns
                joint_pub_msgs[key] = JointTrajectory(
                    joint_names=value,
                    points=[JointTrajectoryPoint(
                        positions=action_slice.astype(float).tolist(),
                        time_from_start=Duration(
                            sec=dur_ns // 1_000_000_000,
                            nanosec=dur_ns % 1_000_000_000,
                        ),
                    )])
            elif leader_topic_types[key] == Twist:
                tmp_twist = Twist()
                tmp_twist.linear.x = float(action_slice[0])
                tmp_twist.linear.y = float(action_slice[1])
                tmp_twist.angular.z = float(action_slice[2])
                joint_pub_msgs[key] = tmp_twist
            else:
                raise ValueError(
                    f'Unsupported leader topic type: {leader_topic_types[key]}')

        return joint_pub_msgs
