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
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
import numpy as np
from sensor_msgs.msg import CompressedImage, JointState
import torch
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint


class DataConverter:

    def __init__(self):
        # cv_bridge is intentionally NOT used. Its boost C-extension is
        # compiled against the numpy 1.x ABI in the ROS Jazzy base, and
        # LeRobot 0.5.1 (v2.5.0) forces numpy 2.2.x — importing/using it
        # crashes the node. compressed_image2cvmat() decodes via cv2
        # directly (opencv-contrib 4.10.x is numpy-2-clean). See that method.
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
        # Decode the JPEG/PNG buffer with OpenCV directly instead of
        # cv_bridge.compressed_imgmsg_to_cv2(). cv_bridge's decode routes
        # through its boost C-extension (cv_bridge.boost.cvtColor2), which
        # is compiled against the numpy 1.x ABI shipped in the ROS Jazzy
        # base image. LeRobot 0.5.1 (v2.5.0) forces numpy 2.2.x, so that
        # extension SEGFAULTs (process exit 139) the first time a frame is
        # converted — i.e. on the first recorded/inferred camera frame, even
        # though the node imports and idles healthy. cv2 (opencv-contrib
        # 4.10.x) is numpy-2-clean, so cv2.imdecode sidesteps the ABI break.
        #
        # CompressedImage.data is the raw encoded buffer. cv2.IMREAD_COLOR
        # always yields an 8-bit, 3-channel BGR ndarray — exactly what
        # desired_encoding='bgr8' meant before, so the downstream
        # BGR2RGB swap in data_manager.convert_msgs_to_raw_datas still
        # produces correct RGB. Behaviour is preserved; only the decoder
        # backend changed.
        try:
            buf = np.frombuffer(msg.data, dtype=np.uint8)
            cv_image = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if cv_image is None:
                raise RuntimeError('cv2.imdecode returned None (corrupt/empty frame?)')
            # IMREAD_COLOR decodes to BGR. Honour non-default encodings so
            # this stays a drop-in replacement for the cv_bridge contract.
            enc = (desired_encoding or 'bgr8').lower()
            if enc in ('rgb8', 'rgb'):
                cv_image = cv2.cvtColor(cv_image, cv2.COLOR_BGR2RGB)
            elif enc in ('mono8', 'mono', 'gray', 'grayscale'):
                cv_image = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
            # 'bgr8' / 'passthrough' → leave as the decoded BGR ndarray.
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
