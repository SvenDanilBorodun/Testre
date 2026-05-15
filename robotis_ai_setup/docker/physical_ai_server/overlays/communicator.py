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
# Author: Dongyun Kim, Seongwoo Kim, Kiwoong Park

import time
from collections import deque
from functools import partial
from typing import Any, Dict, List, Optional, Set, Tuple

from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from physical_ai_interfaces.msg import (
    BrowserItem,
    DatasetInfo,
    TaskStatus
)
from physical_ai_interfaces.srv import (
    BrowseFile,
    EditDataset,
    GetDatasetInfo,
    GetImageTopicList
)
from physical_ai_server.communication.multi_subscriber import MultiSubscriber
from physical_ai_server.data_processing.data_editor import DataEditor
from physical_ai_server.utils.file_browse_utils import FileBrowseUtils
from physical_ai_server.utils.parameter_utils import (
    parse_topic_list,
    parse_topic_list_with_names,
)
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy
)
from rosbag_recorder.srv import SendCommand
from sensor_msgs.msg import CompressedImage, JointState
from std_msgs.msg import Empty, String
from trajectory_msgs.msg import JointTrajectory


class Communicator:

    # Define data source categories
    SOURCE_CAMERA = 'camera'
    SOURCE_FOLLOWER = 'follower'
    SOURCE_LEADER = 'leader'

    # Define operation modes
    MODE_COLLECTION = 'collection'  # Full data collection mode (images, follower, leader)
    MODE_INFERENCE = 'inference'    # Inference mode (images, follower only)

    PUB_QOS_SIZE = 100

    # Audit F65: paired-camera capture tolerance + ring depth.
    # 15 ms is the slop budget — at 30 fps the inter-frame interval is
    # 33 ms, so two msgs within 15 ms of each other are reliably from the
    # "same" world instant. Larger slop trades tighter pairing for fewer
    # fallback hits; smaller slop is academic since USB jitter alone is
    # ~5-10 ms. History of 8 covers ~0.27 s back at 30 Hz which is more
    # than enough to catch the freshest matched pair even when one camera
    # is one frame behind.
    _CAMERA_SYNC_SLOP_NS = 15_000_000
    _CAMERA_SYNC_HISTORY = 8

    def __init__(
        self,
        node: Node,
        operation_mode: str,
        params: Dict[str, Any]
    ):
        self.node = node
        self.operation_mode = operation_mode
        self.params = params
        self.file_browse_utils = FileBrowseUtils(
            max_workers=8,
            logger=self.node.get_logger())

        # Parse topic lists for more convenient access
        self.camera_topics = parse_topic_list_with_names(self.params['camera_topic_list'])
        self.joint_topics = parse_topic_list_with_names(self.params['joint_topic_list'])
        self.rosbag_extra_topics = parse_topic_list(
            self.params['rosbag_extra_topic_list']
        )

        # Determine which sources to enable based on operation mode
        self.enabled_sources = self._get_enabled_sources_for_mode(self.operation_mode)

        # Initialize MultiSubscriber with enabled sources
        self.multi_subscriber = MultiSubscriber(self.node, self.enabled_sources)

        # Initialize DataEditor for dataset editing
        self.data_editor = DataEditor()

        # Initialize joint publishers
        self.joint_publishers = {}

        # Log topic information
        node.get_logger().info(f'Parsed camera topics: {self.camera_topics}')
        node.get_logger().info(f'Parsed joint topics: {self.joint_topics}')
        node.get_logger().info(f'Parsed rosbag extra topics: {self.rosbag_extra_topics}')

        self.camera_topic_msgs = {}
        # Audit F17/F18: per-camera wall-clock arrival deque (length 30
        # → ~1 s window at 30 Hz) for liveness + observed-Hz queries.
        # Header.stamp is also tracked so a driver re-emitting the same
        # buffer with incrementing stamps doesn't defeat the
        # stale-camera halt (which uses byte hashes).
        self._camera_msg_arrival: Dict[str, deque] = {}
        self._camera_msg_stamp_ns: Dict[str, int] = {}
        # Audit F65: short ring of (stamp_ns, msg) per camera used by
        # ``_pick_synced_camera_msgs`` to pair multi-camera frames within
        # ``_CAMERA_SYNC_SLOP_S``. Falls back to ``camera_topic_msgs``
        # (latest-per-camera) when no matched pair exists, so all
        # single-camera / out-of-sync paths keep working unchanged.
        self._camera_recent_msgs: Dict[str, deque] = {}
        self.follower_topic_msgs = {}
        self.leader_topic_msgs = {}

        self.heartbeat_qos_profile = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST
        )

        self.rosbag_service_available = False

        self.init_subscribers()
        self.init_publishers()
        self.init_services()

        self.joystick_state = {
            'updated': False,
            'mode': None
        }

    def get_all_topics(self):
        result = []
        for name, topic in self.camera_topics.items():
            result.append(topic)
        for name, topic in self.joint_topics.items():
            result.append(topic)
        result.extend(self.rosbag_extra_topics)
        return result

    def _get_enabled_sources_for_mode(self, mode: str) -> Set[str]:
        enabled_sources = set()

        # Camera and follower are always needed
        enabled_sources.add(self.SOURCE_CAMERA)
        enabled_sources.add(self.SOURCE_FOLLOWER)

        # Leader is only needed in collection mode
        if mode == self.MODE_COLLECTION:
            enabled_sources.add(self.SOURCE_LEADER)

        self.node.get_logger().info(f'Enabled sources for {mode} mode: {enabled_sources}')
        return enabled_sources

    def init_subscribers(self):
        # Initialize camera subscribers if defined
        for name, topic in self.camera_topics.items():
            self.multi_subscriber.add_subscriber(
                category=self.SOURCE_CAMERA,
                name=name,
                topic=topic,
                msg_type=CompressedImage,
                callback=partial(self._camera_callback, name)
            )
            self.camera_topic_msgs[name] = None
            self.node.get_logger().info(f'Camera subscriber: {name} -> {topic}')

        # Initialize joint subscribers with appropriate message types and callbacks
        for name, topic in self.joint_topics.items():
            # Determine category and message type based on name patterns
            if 'follower' in name.lower():
                if 'mobile' in name.lower():
                    msg_type = Odometry
                else:
                    msg_type = JointState
                category = self.SOURCE_FOLLOWER
                callback = partial(self._follower_callback, name)
                self.follower_topic_msgs[name] = None
            elif 'leader' in name.lower():
                if 'mobile' in name.lower():
                    msg_type = Twist
                else:
                    msg_type = JointTrajectory
                category = self.SOURCE_LEADER
                callback = partial(self._leader_callback, name)
                self.leader_topic_msgs[name] = None
            else:
                # Log an error message if the topic name does not include 'follower' or 'leader'
                self.node.get_logger().error(
                    '[Error] Please include follower or leader in the topic name.'
                )
                continue  # Move to the next topic

            self.multi_subscriber.add_subscriber(
                category=category,
                name=name,
                topic=topic,
                msg_type=msg_type,
                callback=callback
            )
            self.node.get_logger().info(
                f'Joint subscriber: {name} -> {topic} ({msg_type.__name__})')

        self.joystick_trigger_subscriber = self.node.create_subscription(
            String,
            '/leader/joystick_controller/tact_trigger',
            self.joystick_trigger_callback,
            10
        )

    def init_publishers(self):
        self.node.get_logger().info('Initializing joint publishers...')
        for name, topic_name in self.joint_topics.items():
            if 'leader' in name.lower():
                if 'mobile' in name.lower():
                    self.joint_publishers[name] = self.node.create_publisher(
                        Twist,
                        topic_name,
                        self.PUB_QOS_SIZE
                    )
                else:
                    self.joint_publishers[name] = self.node.create_publisher(
                        JointTrajectory,
                        topic_name,
                        self.PUB_QOS_SIZE
                    )
        self.node.get_logger().info('Initializing joint publishers... done')

        self.status_publisher = self.node.create_publisher(
            TaskStatus,
            '/task/status',
            self.PUB_QOS_SIZE
        )

        self.heartbeat_publisher = self.node.create_publisher(
            Empty,
            'heartbeat',
            self.heartbeat_qos_profile)

    def init_services(self):
        self.image_topic_list_service = self.node.create_service(
            GetImageTopicList,
            '/image/get_available_list',
            self.get_image_topic_list_callback
        )

        self.file_browser_service = self.node.create_service(
            BrowseFile,
            '/browse_file',
            self.browse_file_callback
        )

        self.data_editor_service = self.node.create_service(
            EditDataset,
            '/dataset/edit',
            self.dataset_edit_callback
        )

        self.get_dataset_info_service = self.node.create_service(
            GetDatasetInfo,
            '/dataset/get_info',
            self.get_dataset_info_callback
        )

        self._rosbag_send_command_client = self.node.create_client(
            SendCommand,
            'rosbag_recorder/send_command')

        if self._check_rosbag_services_available():
            self.rosbag_service_available = True
            self.node.get_logger().info('Rosbag service is available')
        else:
            self.node.get_logger().error('Failed to connect to rosbag service')
            self.rosbag_service_available = False

    def _check_rosbag_services_available(self):
        return self._rosbag_send_command_client.wait_for_service(timeout_sec=3.0)

    def prepare_rosbag(self, topics: List[str]):
        self._send_rosbag_command(
            command=SendCommand.Request.PREPARE,
            topics=topics
        )

    def start_rosbag(self, rosbag_uri: str):
        self._send_rosbag_command(
            command=SendCommand.Request.START,
            uri=rosbag_uri
        )

    def stop_rosbag(self):
        self._send_rosbag_command(
            command=SendCommand.Request.STOP
        )

    def stop_and_delete_rosbag(self):
        self._send_rosbag_command(
            command=SendCommand.Request.STOP_AND_DELETE
        )

    def finish_rosbag(self):
        self._send_rosbag_command(
            command=SendCommand.Request.FINISH
        )

    def _send_rosbag_command(self,
                             command: int,
                             topics: List[str] = None,
                             uri: str = None):

        if not self.rosbag_service_available:
            self.node.get_logger().error('Rosbag service is not available')
            raise RuntimeError('Rosbag service is not available')

        req = SendCommand.Request()
        req.command = command
        req.topics = topics if topics is not None else []
        req.uri = uri if uri is not None else ''

        # Asynchronous service call - fire and forget
        future = self._rosbag_send_command_client.call_async(req)
        future.add_done_callback(
            lambda f: self.node.get_logger().info(
                f'Sent rosbag record command: {command} {f.result().message}'
                if f.done() and f.result().success
                else 'Failed to send command: '
                     f'{command} {f.result().message if f.done() else "timeout"}'
            )
        )

    def _camera_callback(self, name: str, msg: CompressedImage) -> None:
        self.camera_topic_msgs[name] = msg
        # Audit F18: track receive monotonic time + header stamp so
        # workflow / recording can detect a stale or low-fps stream
        # even when the driver re-emits identical buffers.
        now = time.monotonic()
        dq = self._camera_msg_arrival.get(name)
        if dq is None:
            dq = deque(maxlen=64)
            self._camera_msg_arrival[name] = dq
        dq.append(now)
        try:
            stamp = msg.header.stamp
            stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
            self._camera_msg_stamp_ns[name] = stamp_ns
        except Exception:
            stamp_ns = 0
            self._camera_msg_stamp_ns[name] = 0
        # Audit F65: feed the sync ring. We tolerate stamp_ns == 0
        # (bad/missing header.stamp) by still appending — the picker
        # treats 0 as "no useful stamp" and falls through to latest.
        ring = self._camera_recent_msgs.get(name)
        if ring is None:
            ring = deque(maxlen=self._CAMERA_SYNC_HISTORY)
            self._camera_recent_msgs[name] = ring
        ring.append((stamp_ns, msg))

    def get_camera_msg_age_s(self, name: str) -> Optional[float]:
        """Seconds since the last CompressedImage arrived for ``name``.
        Returns None when no message has arrived yet.
        Used by Roboter Studio / inference to reject stale-frame bursts.
        """
        dq = self._camera_msg_arrival.get(name)
        if not dq:
            return None
        return time.monotonic() - dq[-1]

    def get_camera_observed_hz(self, name: str, window_s: float = 1.0) -> Optional[float]:
        """Audit F17: observed Hz over the last ``window_s`` seconds.
        Returns None when fewer than 2 messages in the window.
        Used at recording start to warn the operator when the camera
        is running slower than ``task_info.fps`` (which causes the
        cached CompressedImage to repeat → trained model learns from
        strobing data).
        """
        dq = self._camera_msg_arrival.get(name)
        if not dq or len(dq) < 2:
            return None
        now = time.monotonic()
        cutoff = now - max(0.1, window_s)
        in_window = [t for t in dq if t >= cutoff]
        if len(in_window) < 2:
            return None
        span = in_window[-1] - in_window[0]
        if span <= 0:
            return None
        return (len(in_window) - 1) / span

    def _follower_callback(self, name: str, msg: JointState) -> None:
        self.follower_topic_msgs[name] = msg

    def _leader_callback(self, name: str, msg: JointTrajectory) -> None:
        self.leader_topic_msgs[name] = msg

    def get_latest_bgr_frame(self, camera: str):
        """Decode the most recent CompressedImage for the named camera into
        a BGR ndarray. Returns None when no frame has arrived yet, when the
        camera is not subscribed, or when the JPEG payload fails to decode.
        Used by the Roboter Studio calibration manager and color-profile
        capture path. Importing OpenCV/numpy is deferred so the import-time
        cost only lands when the calibration wizard is opened."""
        msg = self.camera_topic_msgs.get(camera)
        if msg is None:
            return None
        try:
            import cv2
            import numpy as np
            buf = np.frombuffer(msg.data, dtype=np.uint8)
            if buf.size == 0:
                return None
            frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            return frame
        except Exception as e:
            self.node.get_logger().warning(
                f'get_latest_bgr_frame({camera}) decode failed: {e}'
            )
            return None

    def get_latest_follower_joints(self) -> Optional[List[float]]:
        """Return the most recent follower-arm joint vector in the canonical
        order (joint1..joint5 + gripper_joint_1) or None when no joint
        state has arrived. Used by the hand-eye calibration to obtain
        gripper-in-base pose via FK."""
        # The follower topic in omx_f_config.yaml is keyed simply 'follower'
        # but other configs may name it 'follower_arm'. Pick the first non-
        # mobile (JointState) follower message available.
        for name, msg in self.follower_topic_msgs.items():
            if msg is None:
                continue
            if 'mobile' in name.lower():
                continue
            positions = getattr(msg, 'position', None)
            if positions is None or len(positions) == 0:
                continue
            return list(positions)
        return None

    # ``get_current_gripper_pose`` lives on the Node-level wrapper
    # ``physical_ai_server._get_current_gripper_pose`` because the FK
    # path needs the URDF and IK solver, which the Communicator does
    # not own. Communicator only exposes the raw joint-state read
    # (``get_latest_follower_joints``) that the wrapper composes with
    # ``IKSolver.fk()``.

    def _pick_synced_camera_msgs(self) -> Dict[str, Any]:
        """Audit F65: return a per-camera msg dict where the picked msgs come
        from the same world instant (within ``_CAMERA_SYNC_SLOP_NS``), or
        fall back to ``camera_topic_msgs`` when no matched tuple exists
        (single camera, fresh start, one camera dropped a frame, or any
        msg with ``stamp_ns == 0``).

        For two cameras this is O(history²) ≤ 64 comparisons — cheap at
        30 Hz. Generalised loop handles 1..N cameras; never blocks.
        """
        names = list(self.camera_topic_msgs.keys())
        if len(names) <= 1:
            return self.camera_topic_msgs

        # Audit F66: snapshot each ring safely. `physical_ai_server.py`
        # uses `MultiThreadedExecutor(num_threads=3)`, so two camera
        # callbacks plus the recording timer can run concurrently.
        # CPython's deque iterator raises ``RuntimeError: deque mutated
        # during iteration`` if ``_camera_callback`` ``append()``s while
        # the picker is iterating — and an unhandled RuntimeError here
        # would silently kill the recording tick (no try/except up the
        # call stack). We tolerate the race by retrying the snapshot a
        # couple of times then falling back to ``camera_topic_msgs`` if
        # the deque is genuinely contested. At 30 Hz × 2 cams × 1 timer
        # = ~90 reads/s, real races are rare and a fallback tick is
        # benign (matches pre-F65 behaviour for that tick).
        rings: List[List[Tuple[int, Any]]] = []
        for name in names:
            ring = self._camera_recent_msgs.get(name)
            if not ring:
                return self.camera_topic_msgs
            entries: Optional[List[Tuple[int, Any]]] = None
            for _attempt in range(3):
                try:
                    entries = [
                        (s, m) for (s, m) in ring
                        if s > 0 and m is not None
                    ]
                    break
                except RuntimeError:
                    # Deque mutated during iteration — let the executor
                    # finish the in-flight append and try again. Three
                    # attempts is overkill but cheap.
                    entries = None
                    continue
            if not entries:
                return self.camera_topic_msgs
            rings.append(entries)

        # Walk the Cartesian product newest-first. For each candidate
        # tuple, accept the first one whose max-min stamp ≤ slop.
        # Newest-first ordering means the first acceptable tuple is also
        # the freshest acceptable tuple — exactly what we want.
        def _walk(idx: int, picked: List[Tuple[int, Any]]):
            if idx == len(rings):
                stamps = [s for (s, _) in picked]
                if max(stamps) - min(stamps) <= self._CAMERA_SYNC_SLOP_NS:
                    yield picked
                return
            for entry in reversed(rings[idx]):
                yield from _walk(idx + 1, picked + [entry])

        best = next(_walk(0, []), None)
        if best is None:
            return self.camera_topic_msgs
        return {name: entry[1] for name, entry in zip(names, best)}

    def get_latest_data(self) -> Optional[Tuple[Dict, Dict, Dict]]:
        # Audit F65: try to pair multi-camera msgs by header.stamp within
        # ``_CAMERA_SYNC_SLOP_NS`` (default 15 ms). When pairing fails or
        # only one camera is configured, falls back to the original
        # latest-per-camera behaviour — so single-camera, single-frame
        # warmup, and out-of-sync edge cases keep working unchanged.
        synced_cameras = self._pick_synced_camera_msgs()

        if any(msg is None for msg in synced_cameras.values()):
            return None, None, None

        if any(msg is None for msg in self.follower_topic_msgs.values()):
            return synced_cameras, None, None

        if self.operation_mode == self.MODE_COLLECTION:
            if any(msg is None for msg in self.leader_topic_msgs.values()):
                return synced_cameras, self.follower_topic_msgs, None
            return synced_cameras, self.follower_topic_msgs, self.leader_topic_msgs
        elif self.operation_mode == self.MODE_INFERENCE:
            return synced_cameras, self.follower_topic_msgs, None
        else:
            raise NotImplementedError(
                f'Operation mode {self.operation_mode} is not supported')

    def clear_latest_data(self):
        for key in self.camera_topic_msgs.keys():
            self.camera_topic_msgs[key] = None
        for key in self.follower_topic_msgs.keys():
            self.follower_topic_msgs[key] = None
        for key in self.leader_topic_msgs.keys():
            self.leader_topic_msgs[key] = None
        # Audit F66: also drop the F65 sync rings. Without this, the first
        # tick of a re-recorded episode could pair a fresh msg with a
        # stale msg whose stamp_ns was carried over from the prior
        # episode — exactly the silent misalignment F65 set out to
        # prevent. clear() is atomic under the GIL.
        for ring in self._camera_recent_msgs.values():
            ring.clear()
        self.node.get_logger().info('Cleared latest data from communicator')

    def publish_action(self, joint_msg_datas: Dict[str, Any]):
        for name, joint_msg in joint_msg_datas.items():
            self.joint_publishers[name].publish(joint_msg)

    def publish_status(self, status: TaskStatus):
        self.status_publisher.publish(status)

    def get_image_topic_list_callback(self, request, response):
        camera_topic_list = []
        for topic_name in self.camera_topics.values():
            topic = topic_name
            if topic.endswith('/compressed'):
                topic = topic[:-11]
            camera_topic_list.append(topic)

        if len(camera_topic_list) == 0:
            self.node.get_logger().error('No image topics found')
            response.image_topic_list = []
            response.success = False
            response.message = 'Please check image topics in your robot configuration.'
            return response

        response.image_topic_list = camera_topic_list
        response.success = True
        response.message = 'Image topic list retrieved successfully'
        return response

    def browse_file_callback(self, request, response):
        try:
            if request.action == 'get_path':
                result = self.file_browse_utils.handle_get_path_action(
                    request.current_path)
            elif request.action == 'go_parent':
                # Check if target_files or target_folders are provided
                target_files = None
                target_folders = None

                if hasattr(request, 'target_files') and request.target_files:
                    target_files = set(request.target_files)
                if hasattr(request, 'target_folders') and request.target_folders:
                    target_folders = set(request.target_folders)

                if target_files or target_folders:
                    # Use parallel target checking for go_parent
                    result = self.file_browse_utils.handle_go_parent_with_target_check(
                        request.current_path,
                        target_files,
                        target_folders)
                else:
                    # Use standard go_parent (no targets specified)
                    result = self.file_browse_utils.handle_go_parent_action(
                        request.current_path)
            elif request.action == 'browse':
                # Check if target_files or target_folders are provided
                target_files = None
                target_folders = None

                if hasattr(request, 'target_files') and request.target_files:
                    target_files = set(request.target_files)
                if hasattr(request, 'target_folders') and request.target_folders:
                    target_folders = set(request.target_folders)

                if target_files or target_folders:
                    # Use parallel target checking
                    result = self.file_browse_utils.handle_browse_with_target_check(
                        request.current_path,
                        request.target_name,
                        target_files,
                        target_folders)
                else:
                    # Use standard browsing (no targets specified)
                    result = self.file_browse_utils.handle_browse_action(
                        request.current_path, request.target_name)
            else:
                result = {
                    'success': False,
                    'message': f'Unknown action: {request.action}',
                    'current_path': '',
                    'parent_path': '',
                    'selected_path': '',
                    'items': []
                }

            # Convert result dict to response object
            response.success = result['success']
            response.message = result['message']
            response.current_path = result['current_path']
            response.parent_path = result['parent_path']
            response.selected_path = result['selected_path']

            # Convert item dicts to BrowserItem objects
            response.items = []
            for item_dict in result['items']:
                item = BrowserItem()
                item.name = item_dict['name']
                item.full_path = item_dict['full_path']
                item.is_directory = item_dict['is_directory']
                item.size = item_dict['size']
                item.modified_time = item_dict['modified_time']
                # Set has_target_file field (default False for files)
                item.has_target_file = item_dict.get('has_target_file', False)
                response.items.append(item)

        except Exception as e:
            self.node.get_logger().error(f'Error in browse file handler: {str(e)}')
            response.success = False
            response.message = f'Error: {str(e)}'
            response.current_path = ''
            response.parent_path = ''
            response.selected_path = ''
            response.items = []

        return response

    def dataset_edit_callback(self, request, response):
        try:
            if request.mode == EditDataset.Request.MERGE:
                merge_dataset_list = request.merge_dataset_list
                output_path = request.output_path
                # TODO: Implement HuggingFace upload functionality if needed
                # upload_huggingface = request.upload_huggingface
                self.data_editor.merge_datasets(
                    merge_dataset_list, output_path)

            elif request.mode == EditDataset.Request.DELETE:
                delete_dataset_path = request.delete_dataset_path
                delete_episode_num = list(request.delete_episode_num)
                # TODO: Implement HuggingFace upload functionality if needed
                # upload_huggingface = request.upload_huggingface

                # Use batch delete for better performance
                if len(delete_episode_num) > 1:
                    self.data_editor.delete_episodes_batch(
                        delete_dataset_path, delete_episode_num
                    )
                else:
                    # Single episode deletion
                    self.data_editor.delete_episode(
                        delete_dataset_path, delete_episode_num[0]
                    )
            else:
                response.success = False
                response.message = f'Unknown edit mode: {request.mode}'
                return response

            response.success = True
            response.message = f'Successfully processed edit mode: {request.mode}'
            return response

        except Exception as e:
            self.node.get_logger().error(f'Error in dataset_edit_callback: {str(e)}')
            response.success = False
            response.message = f'Error: {str(e)}'

        return response

    def get_dataset_info_callback(self, request, response):
        try:
            dataset_path = request.dataset_path
            dataset_info = self.data_editor.get_dataset_info(dataset_path)

            info = DatasetInfo()
            info.codebase_version = dataset_info.get('codebase_version', 'unknown') if isinstance(
                dataset_info.get('codebase_version'), str) else 'unknown'
            info.robot_type = dataset_info.get('robot_type', 'unknown') if isinstance(
                dataset_info.get('robot_type'), str) else 'unknown'
            info.total_episodes = dataset_info.get('total_episodes', 0) if isinstance(
                dataset_info.get('total_episodes'), int) else 0
            info.total_tasks = dataset_info.get('total_tasks', 0) if isinstance(
                dataset_info.get('total_tasks'), int) else 0
            info.fps = dataset_info.get('fps', 0) if isinstance(
                dataset_info.get('fps'), int) else 0

            response.dataset_info = info
            response.success = True
            response.message = 'Dataset info retrieved successfully'
            return response

        except Exception as e:
            self.node.get_logger().error(f'Error in get_dataset_info_callback: {str(e)}')
            response.success = False
            response.message = f'Error: {str(e)}'
            response.dataset_info = DatasetInfo()
            return response

    def get_publisher_msg_types(self):
        msg_types = {}
        for publisher_name, publisher in self.joint_publishers.items():
            msg_types[publisher_name] = publisher.msg_type
        return msg_types

    def _destroy_service_if_exists(self, service_attr_name: str):
        if hasattr(self, service_attr_name):
            service = getattr(self, service_attr_name)
            if service is not None:
                self.node.destroy_service(service)
                setattr(self, service_attr_name, None)

    def _destroy_client_if_exists(self, client_attr_name: str):
        if hasattr(self, client_attr_name):
            client = getattr(self, client_attr_name)
            if client is not None:
                self.node.destroy_client(client)
                setattr(self, client_attr_name, None)

    def _destroy_publisher_if_exists(self, publisher_attr_name: str):
        if hasattr(self, publisher_attr_name):
            publisher = getattr(self, publisher_attr_name)
            if publisher is not None:
                self.node.destroy_publisher(publisher)
                setattr(self, publisher_attr_name, None)

    def cleanup(self):
        self.node.get_logger().info('Cleaning up Communicator resources...')

        self._cleanup_publishers()
        self._cleanup_subscribers()
        self._cleanup_services()

        # Clear message containers
        self.camera_topic_msgs.clear()
        self._camera_recent_msgs.clear()
        self.follower_topic_msgs.clear()
        self.leader_topic_msgs.clear()

        self.node.get_logger().info('Communicator cleanup completed')

    def _cleanup_publishers(self):
        publisher_names = [
            'status_publisher',
            'heartbeat_publisher'
        ]
        for publisher_name in publisher_names:
            self._destroy_publisher_if_exists(publisher_name)

        # Clean up joint publishers
        for _, publisher in self.joint_publishers.items():
            self.node.destroy_publisher(publisher)
        self.joint_publishers.clear()

    def _cleanup_subscribers(self):
        # Clean up multi subscriber
        if hasattr(self, 'multi_subscriber') and self.multi_subscriber is not None:
            self.multi_subscriber.cleanup()
            self.multi_subscriber = None

        # Clean up joystick trigger subscriber
        if hasattr(self, 'joystick_trigger_subscriber') and \
           self.joystick_trigger_subscriber is not None:
            self.node.destroy_subscription(self.joystick_trigger_subscriber)
            self.joystick_trigger_subscriber = None

    def _cleanup_services(self):
        service_names = [
            'image_topic_list_service',
            'file_browser_service',
            'data_editor_service',
            'get_dataset_info_service'
        ]
        for service_name in service_names:
            self._destroy_service_if_exists(service_name)

    def _cleanup_clients(self):
        client_names = [
            '_rosbag_send_command_client'
        ]
        for client_name in client_names:
            self._destroy_client_if_exists(client_name)

    def heartbeat_timer_callback(self):
        heartbeat_msg = Empty()
        self.heartbeat_publisher.publish(heartbeat_msg)

    def joystick_trigger_callback(self, msg: String):
        self.node.get_logger().info(f'Received joystick trigger: {msg.data}')
        self.joystick_state['updated'] = True
        self.joystick_state['mode'] = msg.data
