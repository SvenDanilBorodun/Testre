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

import importlib.util
import json
import math
import os
import subprocess
import sys
import threading
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
from physical_ai_server.data_processing import dataset_paths
from physical_ai_server.data_processing import edit_worker
from physical_ai_server.data_processing.data_editor import DataEditor
from physical_ai_server.utils.file_browse_utils import FileBrowseUtils
from physical_ai_server.utils.parameter_utils import (
    parse_topic_list,
    parse_topic_list_with_names,
)
from rclpy.node import Node
from rclpy.qos import (
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy
)
from rosbag_recorder.srv import SendCommand
from sensor_msgs.msg import CompressedImage, JointState
from std_msgs.msg import String
from trajectory_msgs.msg import JointTrajectory


# Aufnahme 2.0 round 5: every recorder subscription (cameras, follower, leader)
# lives on the sensor-ingest node with this KEEP_LAST depth (BEST_EFFORT). Its
# SingleThreadedExecutor dispatches a message in ~0.18 ms, so the depth is
# cheap, and a burst after a process stall arrives COMPLETE — the capture
# timeline (the per-source gap rule, the leader's back-dating) relies on it.
SENSOR_QOS_DEPTH = 32


def _load_capture_timeline():
    """capture_timeline (stdlib only): by package name in the image, by path
    when a deps-free test loader gave the package no __path__."""
    try:
        from physical_ai_server.communication import capture_timeline
        return capture_timeline
    except ImportError:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'capture_timeline.py')
        spec = importlib.util.spec_from_file_location('_edubotics_capture_timeline', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


_capture_timeline = _load_capture_timeline()
SourceHistory = _capture_timeline.SourceHistory
CAMERA_HISTORY_LEN = _capture_timeline.CAMERA_HISTORY_LEN
JOINT_HISTORY_LEN = _capture_timeline.JOINT_HISTORY_LEN


def _stamp_seconds(msg) -> float:
    """A message's header stamp in seconds; 0.0 when it has none (the
    capture timeline then places the sample at its arrival)."""
    try:
        stamp = msg.header.stamp
        seconds = int(stamp.sec) + int(stamp.nanosec) * 1e-9
    except Exception:  # noqa: BLE001 — no header / a malformed one: no stamp
        return 0.0
    return seconds if math.isfinite(seconds) else 0.0


class Communicator:

    # Define data source categories
    SOURCE_CAMERA = 'camera'
    SOURCE_FOLLOWER = 'follower'
    SOURCE_LEADER = 'leader'

    # Define operation modes
    MODE_COLLECTION = 'collection'  # Full data collection mode (images, follower, leader)
    MODE_INFERENCE = 'inference'    # Inference mode (images, follower only)

    # Canonical OMX-F follower joint order (matches omx_f_config.yaml joint_order.follower,
    # collision_monitor.LEADER_JOINTS, and the IKSolver's joint expectations). The
    # /joint_states broadcaster commonly publishes joints NAME-SORTED — i.e.
    # [gripper_joint_1, joint1, joint2, joint3, joint4, joint5] — so a position vector read
    # in MESSAGE order is scrambled relative to what FK / the workflow start-pose seed expect.
    # get_latest_follower_joints() reorders by name into this canonical order.
    FOLLOWER_JOINT_ORDER = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1')

    PUB_QOS_SIZE = 100

    # Rosbag-service probe retry (T2). The Communicator is now constructed at
    # BOOT (physical_ai_server._init_robot_profile), so the one-shot ctor probe
    # races the rosbag node starting in the same bringup. On a boot-time miss we
    # arm a low-rate background retry instead of disabling record_rosbag2 for the
    # whole session. ~18 attempts × 10 s ≈ 3 min covers a slow bringup.
    _ROSBAG_PROBE_RETRY_PERIOD_S = 10.0
    _ROSBAG_PROBE_MAX_ATTEMPTS = 18

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
        params: Dict[str, Any],
        follower_joint_order: Optional[tuple] = None,
        sensor_node: Optional[Node] = None,
    ):
        self.node = node
        # The node the recorder's camera/follower/leader subscriptions are
        # created on: the sensor-ingest node when the server has one (round 5),
        # else the main node. Everything else (publishers, services, the
        # joystick subscription) stays on the main node.
        self.sensor_node = sensor_node if sensor_node is not None else node
        self.operation_mode = operation_mode
        self.params = params
        # ArmProfile seam (§16.4 2d): the canonical follower joint order becomes
        # an INSTANCE attribute (shadowing the OMX class default) so a profile
        # with different joint names/count reorders /joint_states correctly.
        # get_latest_follower_joints() returns EXACTLY len(order) values by
        # construction — the §16.4 width rail at this choke point.
        if follower_joint_order:
            names = tuple(str(n) for n in follower_joint_order if str(n).strip())
            if names:
                self.FOLLOWER_JOINT_ORDER = names
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

        # Initialize MultiSubscriber with enabled sources, on the sensor node.
        self.multi_subscriber = MultiSubscriber(self.sensor_node, self.enabled_sources)

        # Initialize DataEditor for dataset editing
        self.data_editor = DataEditor()

        # Dataset edits (delete/merge) re-encode video and can peg every CPU
        # core for minutes on legacy AV1 datasets. Run them out-of-process at
        # nice 19 (see edit_worker) so they can't starve the ROS executor; a
        # single-flight lock blocks a client-timeout retry from stacking a
        # second encode. EDUBOTICS_DATASET_EDIT_SUBPROCESS=0 reverts to the
        # in-process call (same routing) for debugging.
        self._edit_lock = threading.Lock()
        self._edit_use_subprocess = (
            os.environ.get('EDUBOTICS_DATASET_EDIT_SUBPROCESS', '1') != '0'
        )
        # Backstop so a wedged worker can't hold the single-flight lock forever
        # (1 h is far above any real student-dataset edit).
        self._edit_timeout_s = 3600

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
        # Wall-clock (monotonic) arrival time of the most recent follower
        # JointState, for the follower-joint staleness gate — the arm-side
        # analogue of _camera_msg_arrival. 0.0 = none arrived yet. Roboter Studio
        # jog / replay refuse when this readback is stale (a frozen joint stream)
        # so a driven move never seeds from a stale pose.
        self._follower_last_arrival_mono: float = 0.0

        self.rosbag_service_available = False

        # Aufnahme 2.0 — per-source arrival counters for /edubotics/signal_status
        # (the node's 1 Hz tick turns them into rates and ages). One entry per
        # subscription init_subscribers() actually creates, in registration order:
        # 'camera:<name>', 'follower:<name>', 'leader:<name>'. Never reset by
        # clear_latest_data() — the rate is measured across the whole boot.
        self._source_counts: Dict[str, int] = {}
        self._source_last_mono: Dict[str, Optional[float]] = {}
        self._source_meta: Dict[str, Tuple[str, str, str]] = {}
        # Round 5 (spec §2.3): the timestamped history of every registered
        # source, PRE-created at registration (never lazily inside a callback,
        # which would race the recorder's snapshot). Cameras keep
        # CAMERA_HISTORY_LEN frames, follower and leader JOINT_HISTORY_LEN.
        # The record tick's slot sampler decides every dataset frame from these.
        self._histories: Dict[str, Any] = {}

        # Audit fix 7 — a raise partway through the init_* registration
        # sequence used to LEAK the already-registered ROS entities:
        # physical_ai_server binds self.communicator only AFTER this ctor
        # returns, so the degraded-boot teardown's `if self.communicator is
        # not None: cleanup()` no-ops on a ctor raise. Clean up the partially
        # built self here (best-effort — the ORIGINAL exception must
        # propagate, so the degraded-boot path in _init_robot_profile still
        # sees the real failure), then re-raise. All the containers cleanup()
        # touches (camera_topic_msgs, joint_publishers, …) are assigned above
        # this point, so cleanup() on a partial self is safe; its sub-helpers
        # are hasattr/getattr-guarded for anything init_* didn't reach.
        try:
            self.init_subscribers()
            self.init_publishers()
            self.init_services()
        except Exception:
            try:
                self.cleanup()
            except Exception:
                # Never mask the original registration failure with a
                # cleanup failure.
                pass
            raise

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
        # Round 5: cameras AND joints use SENSOR_QOS_DEPTH (32), BEST_EFFORT,
        # KEEP_LAST, on the sensor-ingest node. The old depth=1 joint
        # subscriptions ("latest only") lost every sample but the newest after a
        # stall; the capture timeline needs the burst complete (the follower's
        # own-timeline gap rule, the leader's back-dating inside a burst).
        camera_qos = QoSProfile(
            depth=SENSOR_QOS_DEPTH,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )
        joint_qos = QoSProfile(
            depth=SENSOR_QOS_DEPTH,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
        )
        # Initialize camera subscribers if defined
        for name, topic in self.camera_topics.items():
            self.multi_subscriber.add_subscriber(
                category=self.SOURCE_CAMERA,
                name=name,
                topic=topic,
                msg_type=CompressedImage,
                callback=partial(self._camera_callback, name),
                qos_profile=camera_qos,
            )
            self.camera_topic_msgs[name] = None
            if self.multi_subscriber.is_source_enabled(self.SOURCE_CAMERA):
                self._register_source(self.SOURCE_CAMERA, name, topic)
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
                callback=callback,
                qos_profile=joint_qos,
            )
            if self.multi_subscriber.is_source_enabled(category):
                self._register_source(category, name, topic)
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

        # The 1 Hz liveness heartbeat publisher lives on the node
        # (physical_ai_server._init_ros_publisher), decoupled from the
        # data-pipeline bring-up so liveness is reported even when boot-init
        # degrades (communicator=None).

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

        # Own callback group: a dataset edit blocks its callback thread for the
        # whole (possibly multi-minute) edit. On the node's default
        # MutuallyExclusiveCallbackGroup that serialized out heartbeat / status /
        # every node-default service — the dashboard went dead. A dedicated group
        # lets the blocking wait run concurrently with the default group.
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
        self._edit_cb_group = MutuallyExclusiveCallbackGroup()
        self.data_editor_service = self.node.create_service(
            EditDataset,
            '/dataset/edit',
            self.dataset_edit_callback,
            callback_group=self._edit_cb_group
        )

        self.get_dataset_info_service = self.node.create_service(
            GetDatasetInfo,
            '/dataset/get_info',
            self.get_dataset_info_callback
        )

        self._rosbag_send_command_client = self.node.create_client(
            SendCommand,
            'rosbag_recorder/send_command')

        # Background retry bookkeeping (armed only on a boot-time probe miss).
        self._rosbag_probe_timer = None
        self._rosbag_probe_attempts = 0

        if self._check_rosbag_services_available():
            self.rosbag_service_available = True
            self.node.get_logger().info('Rosbag service is available')
        else:
            # Boot-init constructs the Communicator at STARTUP, racing the rosbag
            # node in the same bringup — so a miss here is expected on a cold boot,
            # not a hard failure. Arm a low-rate background retry (own callback
            # group) so record_rosbag2 comes online once the recorder is up
            # instead of staying silently disabled for the whole session.
            self.rosbag_service_available = False
            self.node.get_logger().warning(
                'Rosbag service not yet available — retrying in the background')
            self._start_rosbag_probe_retry()

    def _check_rosbag_services_available(self):
        return self._rosbag_send_command_client.wait_for_service(timeout_sec=3.0)

    def _start_rosbag_probe_retry(self):
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
        self._rosbag_probe_timer = self.node.create_timer(
            self._ROSBAG_PROBE_RETRY_PERIOD_S,
            self._rosbag_probe_retry_cb,
            callback_group=MutuallyExclusiveCallbackGroup(),
        )

    def _rosbag_probe_retry_cb(self):
        self._rosbag_probe_attempts += 1
        # Short per-attempt wait — the retry cadence is the timer period.
        if self._rosbag_send_command_client.wait_for_service(timeout_sec=0.5):
            self.rosbag_service_available = True
            self.node.get_logger().info('Rosbag service is available (retry)')
            self._cancel_rosbag_probe_timer()
            return
        if self._rosbag_probe_attempts >= self._ROSBAG_PROBE_MAX_ATTEMPTS:
            self.node.get_logger().error(
                'Rosbag service still unavailable after retries — '
                'record_rosbag2 disabled until restart')
            self._cancel_rosbag_probe_timer()

    def _cancel_rosbag_probe_timer(self):
        timer = getattr(self, '_rosbag_probe_timer', None)
        if timer is None:
            return
        try:
            timer.cancel()
        except Exception:
            pass
        try:
            self.node.destroy_timer(timer)
        except Exception:
            pass
        self._rosbag_probe_timer = None

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

    def _register_source(self, kind: str, name: str, topic: str) -> None:
        # A camera is reported by its stream, not its transport.
        if kind == self.SOURCE_CAMERA and topic.endswith('/compressed'):
            topic = topic[:-len('/compressed')]
        source_id = f'{kind}:{name}'
        self._source_counts[source_id] = 0
        self._source_last_mono[source_id] = None
        self._source_meta[source_id] = (kind, name, topic)
        histories = getattr(self, '_histories', None)
        if histories is not None and source_id not in histories:
            histories[source_id] = SourceHistory(
                CAMERA_HISTORY_LEN if kind == self.SOURCE_CAMERA else JOINT_HISTORY_LEN)

    def _note_arrival(self, source_id: str) -> None:
        # Guarded: tests (and helpers) build a Communicator via __new__ without
        # the counters; a callback there must keep doing its real job.
        counts = getattr(self, '_source_counts', None)
        if counts is None or source_id not in counts:
            return
        counts[source_id] += 1
        self._source_last_mono[source_id] = time.monotonic()

    def source_counters(self) -> List[Dict[str, Any]]:
        """Arrival counters for /edubotics/signal_status, registration order.

        Each entry: {'id', 'kind', 'name', 'topic', 'count', 'last_mono'}
        (last_mono is time.monotonic() of the last message, None until one
        arrived). Returns fresh dicts; the caller may keep or mutate them.
        """
        counts = getattr(self, '_source_counts', None) or {}
        out = []
        for source_id, count in list(counts.items()):
            kind, name, topic = self._source_meta[source_id]
            out.append({
                'id': source_id,
                'kind': kind,
                'name': name,
                'topic': topic,
                'count': int(count),
                'last_mono': self._source_last_mono.get(source_id),
            })
        return out

    # ── Sensor callbacks (round 5, F3) ───────────────────────────────────────
    # They run on the sensor-ingest executor's thread. Each public callback is a
    # thin guard around its body: a callback NEVER raises into that executor (a
    # raise there counts as an executor failure, and repeated ones end the
    # process for a respawn). A failure is counted and logged — the first five,
    # then every 100th.

    _SENSOR_CB_LOG_FIRST = 5
    _SENSOR_CB_LOG_EVERY = 100

    def _sensor_callback_failed(self, which: str, error: Exception) -> None:
        count = getattr(self, '_sensor_cb_errors', 0) + 1
        self._sensor_cb_errors = count
        if count <= self._SENSOR_CB_LOG_FIRST or count % self._SENSOR_CB_LOG_EVERY == 0:
            try:
                self.node.get_logger().error(
                    f'{which} failed ({count} sensor callback failures): {error!r}')
            except Exception:  # noqa: BLE001 — a log failure must not raise either
                pass

    def _camera_callback(self, name: str, msg: CompressedImage) -> None:
        try:
            self._camera_callback_body(name, msg)
        except Exception as e:  # noqa: BLE001 — never raise into the sensor executor
            self._sensor_callback_failed('_camera_callback', e)

    def _append_history(self, source_id: str, arrival: float, stamp_s: float, msg) -> None:
        histories = getattr(self, '_histories', None)
        history = histories.get(source_id) if histories else None
        if history is not None:
            history.append(arrival, stamp_s, msg)

    def _camera_callback_body(self, name: str, msg: CompressedImage) -> None:
        # Round 5: the arrival time FIRST, then the timestamped history, then
        # the existing work (latest cache, F65 rings for inference, counters).
        arrival = time.monotonic()
        self._append_history(f'{self.SOURCE_CAMERA}:{name}', arrival, _stamp_seconds(msg), msg)
        self._note_arrival(f'{self.SOURCE_CAMERA}:{name}')
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
        try:
            self._follower_callback_body(name, msg)
        except Exception as e:  # noqa: BLE001 — never raise into the sensor executor
            self._sensor_callback_failed('_follower_callback', e)

    def _follower_callback_body(self, name: str, msg: JointState) -> None:
        arrival = time.monotonic()
        self._append_history(
            f'{self.SOURCE_FOLLOWER}:{name}', arrival, _stamp_seconds(msg), msg)
        self._note_arrival(f'{self.SOURCE_FOLLOWER}:{name}')
        self.follower_topic_msgs[name] = msg
        self._follower_last_arrival_mono = time.monotonic()

    def get_follower_joints_age_s(self) -> Optional[float]:
        """Seconds since the last follower JointState arrived, or None when none
        has arrived yet. Used by Roboter Studio jog / replay to refuse a driven
        move against a STALE (frozen) joint stream — a fresh readback is required
        so the move never seeds from an old pose. Mirrors get_camera_msg_age_s."""
        if self._follower_last_arrival_mono <= 0.0:
            return None
        return time.monotonic() - self._follower_last_arrival_mono

    def _leader_callback(self, name: str, msg: JointTrajectory) -> None:
        try:
            self._leader_callback_body(name, msg)
        except Exception as e:  # noqa: BLE001 — never raise into the sensor executor
            self._sensor_callback_failed('_leader_callback', e)

    def _leader_callback_body(self, name: str, msg: JointTrajectory) -> None:
        # The leader broadcaster's JointTrajectory carries stamp 0 by design:
        # its time is its arrival (back-dated by the sampler inside a burst).
        arrival = time.monotonic()
        self._append_history(f'{self.SOURCE_LEADER}:{name}', arrival, 0.0, msg)
        self._note_arrival(f'{self.SOURCE_LEADER}:{name}')
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
        """Return the most recent follower-arm joint vector in the CANONICAL
        order (joint1..joint5 + gripper_joint_1) or None when no joint state
        has arrived. Used by the Roboter-Studio workflow start-pose seed and
        the touch-off / hand-eye FK (gripper-in-base pose).

        The /joint_states broadcaster commonly publishes joints NAME-SORTED
        ([gripper_joint_1, joint1..joint5]), NOT in the canonical arm order, so
        reading msg.position in MESSAGE order scrambles the vector — a scrambled
        seed makes the first workflow move lurch, and scrambled FK yields the
        wrong z_table at touch-off. Reorder by msg.name (the same name-keyed
        pattern the recording path and the collision monitor use) so the caller
        always gets canonical order regardless of publish order.

        Fails LOUD on a partial message (a canonical joint missing from
        msg.name): returns None rather than silently zero-filling a missing
        joint, which would otherwise feed a bogus value into FK / the seed.
        """
        # The follower topic in omx_f_config.yaml is keyed simply 'follower'
        # but other configs may name it 'follower_arm'. Pick the first non-
        # mobile (JointState) follower message available.
        for name, msg in self.follower_topic_msgs.items():
            if msg is None:
                continue
            if 'mobile' in name.lower():
                continue
            names = list(getattr(msg, 'name', []) or [])
            positions = list(getattr(msg, 'position', []) or [])
            if not names or not positions or len(names) != len(positions):
                continue
            name_to_pos = dict(zip(names, positions))
            # Require every canonical joint to be present — a partial message
            # (e.g. mid-startup before the gripper joint is published) must not
            # be silently zero-filled into a bogus pose.
            if not all(j in name_to_pos for j in self.FOLLOWER_JOINT_ORDER):
                continue
            return [float(name_to_pos[j]) for j in self.FOLLOWER_JOINT_ORDER]
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

    def history_snapshots(self):
        """``(cams, follower, leader)`` snapshots of the timestamped histories
        for the record tick's slot sampler (round 5): ``cams`` maps each camera
        name to its ``[(seq, arrival, t, msg), …]``; ``follower`` and ``leader``
        are the first registered follower's / leader's lists. ``leader`` is None
        when no leader source is registered (a session that reads none)."""
        histories = getattr(self, '_histories', None) or {}
        cams = {}
        for name in self.camera_topic_msgs:
            history = histories.get(f'{self.SOURCE_CAMERA}:{name}')
            if history is not None:
                cams[name] = history.snapshot()
        follower_name, leader_name = self.history_source_names()
        follower = histories[f'{self.SOURCE_FOLLOWER}:{follower_name}'].snapshot() \
            if follower_name is not None else []
        leader = histories[f'{self.SOURCE_LEADER}:{leader_name}'].snapshot() \
            if leader_name is not None else None
        return cams, follower, leader

    def history_source_names(self):
        """Names of the follower and leader whose histories history_snapshots()
        returns (registration order); None when there is none."""
        histories = getattr(self, '_histories', None) or {}
        follower = next((sid.split(':', 1)[1] for sid in histories
                         if sid.startswith(f'{self.SOURCE_FOLLOWER}:')), None)
        leader = next((sid.split(':', 1)[1] for sid in histories
                       if sid.startswith(f'{self.SOURCE_LEADER}:')), None)
        return follower, leader

    def clear_latest_data(self):
        for key in self.camera_topic_msgs.keys():
            self.camera_topic_msgs[key] = None
        for key in self.follower_topic_msgs.keys():
            self.follower_topic_msgs[key] = None
        # Drop the follower arrival stamp too so a resumed session's first tick
        # reports the readback as "not yet arrived" (None age) rather than a huge
        # stale age carried over from before the multi-second recovery motion.
        self._follower_last_arrival_mono = 0.0
        for key in self.leader_topic_msgs.keys():
            self.leader_topic_msgs[key] = None
        # Audit F66: also drop the F65 sync rings. Without this, the first
        # tick of a re-recorded episode could pair a fresh msg with a
        # stale msg whose stamp_ns was carried over from the prior
        # episode — exactly the silent misalignment F65 set out to
        # prevent. clear() is atomic under the GIL.
        for ring in self._camera_recent_msgs.values():
            ring.clear()
        # Round 5: and the timestamped histories (a resumed session never
        # samples a frame captured before the recovery motion).
        for history in (getattr(self, '_histories', None) or {}).values():
            history.clear()
        self.node.get_logger().info('Cleared latest data from communicator')

    def publish_action(self, joint_msg_datas: Dict[str, Any]):
        for name, joint_msg in joint_msg_datas.items():
            self.joint_publishers[name].publish(joint_msg)

    def publish_status(self, status: TaskStatus):
        # Stamp robot IDENTITY onto every status that doesn't already carry it.
        # Several callers build a bare TaskStatus() (the idle identity tick, the
        # stale-recording-session notice, the record/inference error branches)
        # which would otherwise publish empty identity and be read as "robot
        # deselected". The node's robot_type / robot_profile / capabilities_json
        # are boot-set from EDUBOTICS_ROBOT_TYPE (physical_ai_server.
        # _init_robot_profile) and self-heal on respawn, so the idle tick (D8)
        # re-delivers them to any (re)connecting client within ~2-3 s — there is
        # no separate identity service. They stay empty ONLY on the degraded-boot
        # path (communicator rebuilt but profile-init failed), which is correct.
        if not getattr(status, 'robot_type', ''):
            status.robot_type = getattr(self.node, 'robot_type', '') or ''
        # robot_profile / capabilities_json are the NEW TaskStatus string fields.
        # hasattr-guarded (mirror collision_monitor's joint_dist_to_home pattern):
        # a container running pre-rebuild COMPILED interfaces lacks the fields, and
        # a bare setattr would raise on every status publish (__slots__).
        if hasattr(status, 'robot_profile') and not getattr(status, 'robot_profile', ''):
            status.robot_profile = getattr(self.node, 'robot_profile', '') or ''
        if hasattr(status, 'capabilities_json') and not getattr(status, 'capabilities_json', ''):
            status.capabilities_json = getattr(self.node, 'capabilities_json', '') or ''
        self.status_publisher.publish(status)
        # D8 belt: record the last /task/status publish so the idle identity tick
        # only fires after 3 s of genuine silence (never stomping a live tick).
        self.node._last_task_status_mono = time.monotonic()

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
        # A dataset edit (delete/merge) re-encodes video and can saturate every
        # CPU core for minutes on legacy AV1 datasets. We run it out-of-process
        # at nice 19 (or in-process when EDUBOTICS_DATASET_EDIT_SUBPROCESS=0) so
        # the ROS executor keeps answering services and the dashboard stays
        # alive. The routing (v3-vs-legacy / delete-vs-merge, leLab PR-1) lives
        # in edit_worker.run_edit, shared by both paths.

        # Single-flight: a long edit can outlive the React client timeout; reject
        # a concurrent retry instead of stacking a second multi-minute encode.
        if not self._edit_lock.acquire(blocking=False):
            response.success = False
            response.message = (
                'Es läuft bereits eine Bearbeitung. Bitte warten, bis sie '
                'abgeschlossen ist.'
            )
            return response

        try:
            payload = self._build_edit_payload(request)
            if self._edit_use_subprocess:
                result = self._run_edit_subprocess(payload)
            else:
                result = edit_worker.run_edit(
                    payload, logger=self.node.get_logger())
            response.success = bool(result.get('success'))
            response.message = str(result.get('message', ''))
            return response
        except Exception as e:
            self.node.get_logger().error(f'Error in dataset_edit_callback: {e}')
            response.success = False
            response.message = f'Error: {e}'
            return response
        finally:
            self._edit_lock.release()

    def _build_edit_payload(self, request):
        """Translate an EditDataset request into edit_worker's JSON payload.

        The wire mode int is mapped to edit_worker's string constants so the
        worker stays ROS-interface-free.
        """
        if request.mode == EditDataset.Request.MERGE:
            mode = edit_worker.MODE_MERGE
        elif request.mode == EditDataset.Request.DELETE:
            mode = edit_worker.MODE_DELETE
        else:
            mode = str(request.mode)
        return {
            'mode': mode,
            'merge_dataset_list': list(request.merge_dataset_list),
            'delete_dataset_path': request.delete_dataset_path,
            'delete_episode_num': [int(i) for i in request.delete_episode_num],
            'output_path': request.output_path,
            # upload_huggingface is accepted on the wire but not yet implemented
            # (TODO carried over from the original callback).
        }

    def _run_edit_subprocess(self, payload):
        """Run edit_worker as a nice'd subprocess and parse its result line.

        Inherits the node env so the colcon overlay PYTHONPATH resolves the
        ``-m`` module. stderr is folded into stdout so all lerobot/ffmpeg/SVT
        noise is in one stream we grep for the RESULT_MARKER line. The blocking
        wait is safe: this callback is on its own callback group (does not block
        heartbeat) and the child is nice 19 (does not starve the executor).
        """
        cmd = edit_worker.build_command(sys.executable)
        self.node.get_logger().info(
            f'Dataset edit subprocess (nice 19): {" ".join(cmd)}')
        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                text=True,
                timeout=self._edit_timeout_s,
            )
        except subprocess.TimeoutExpired:
            self.node.get_logger().error(
                'Dataset edit worker exceeded the time limit and was killed.')
            return {
                'success': False,
                'message': (
                    'Die Bearbeitung hat zu lange gedauert und wurde '
                    'abgebrochen. Der Datensatz wurde nicht verändert.'
                ),
            }

        result = edit_worker.parse_output(proc.stdout)
        if result is None:
            tail = '\n'.join((proc.stdout or '').splitlines()[-15:])
            self.node.get_logger().error(
                f'Dataset edit worker exited {proc.returncode} with no result. '
                f'Tail:\n{tail}'
            )
            return {
                'success': False,
                'message': (
                    'Beim Bearbeiten des Datensatzes ist ein unerwarteter '
                    'Fehler aufgetreten. Der Datensatz wurde nicht verändert.'
                ),
            }
        return result

    def get_dataset_info_callback(self, request, response):
        try:
            # Client-supplied and reachable from the unauthenticated rosbridge.
            # Unconfined it read any `<dir>/meta/info.json` on the container and
            # doubled as a directory-existence oracle (measured 2026-08-08:
            # five fields returned from a planted file outside every root, and
            # `/etc/ssh` distinguishable from a nonexistent path by the message
            # alone). Read-only, so the dataset root is the right confinement —
            # narrower than `browsable_roots`, which exists for the BROWSER.
            try:
                dataset_path = str(dataset_paths.confine(
                    request.dataset_path, dataset_paths.dataset_root()))
            except dataset_paths.DatasetPathError as e:
                self.node.get_logger().warning(f'Refused dataset_path: {e}')
                response.success = False
                response.message = str(e)
                return response
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

        # Cancel any in-flight rosbag-probe retry so a degraded-boot teardown
        # (physical_ai_server._init_robot_profile) doesn't leak a live timer.
        self._cancel_rosbag_probe_timer()

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
            'status_publisher'
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

    def joystick_trigger_callback(self, msg: String):
        self.node.get_logger().info(f'Received joystick trigger: {msg.data}')
        self.joystick_state['updated'] = True
        self.joystick_state['mode'] = msg.data
