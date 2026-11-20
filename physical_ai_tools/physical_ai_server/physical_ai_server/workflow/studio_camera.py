#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Roboter Studio's tower-camera consumer (spec 40 S1; decisions D4, D63).

The tower camera's own full-resolution picture arrives on ``STUDIO_TOPIC``
(the ingest node or the Pi's capture node publishes it, ``frame_id``
``'scene_studio'``). Only Roboter Studio reads it: the recorder, inference and
the pages keep the 640×480 outside stream exactly as before.

``StudioCamera`` owns its OWN node (``physical_ai_server_studio``) and a
SingleThreadedExecutor on its own daemon thread — never the main node, never
the recorder's sensor node — both made by the factories it is handed, so it
runs on fakes in the tests.

* The statement on ``scene_provenance.PROFILE_TOPIC`` and the camera state on
  ``STATE_TOPIC`` are subscribed for the camera's whole life (latched).
* The picture is subscribed only while it is wanted: between ``hold()`` and
  ``release()`` (a real program run, an arm check), and after a frame request
  until ``IDLE_RELEASE_S`` after the last one. The first request after a
  (re)subscription waits for its first frame (review M4); every message is
  decoded at most once, and only when asked for.

THE PROFILE IS THE STATEMENT (D63): ``profile()`` answers what the arm
container's latest statement says and ``None`` while there is none or it
cannot be read — silence is unknown, never ``legacy``, however long it lasts.
The camera state never changes it (a faulted or unplugged camera on a tower
rig leaves the tower standing); it only supplies the stamp's ``profile_id``
and ``platform`` through ``provider()``, the recorder's provider
(``scene_provenance``).
"""

import functools
import json
import threading
import time
import types

from physical_ai_server import scene_provenance

NODE_NAME = 'physical_ai_server_studio'
# The camera name Roboter Studio asks the node for; the ingest node stamps it
# as the studio picture's frame_id.
STUDIO_CAMERA = 'scene_studio'
# Equal to scene_split's (both twins; test_studio_camera.py): the server ships
# without scene_split, which needs numpy and cv2 at import.
STUDIO_TOPIC = '/studio/tower/compressed'
STATE_TOPIC = '/edubotics/scene_camera_state'
STATE_MAX_BYTES = 4096
PLATFORMS = scene_provenance.PLATFORMS
PROFILE_TOWER = 'tower_v1'
STATEMENT_VERSION = 1

IDLE_RELEASE_S = 10.0       # a requested picture is unsubscribed this long after the last request
FIRST_FRAME_WAIT_S = 2.0    # a request waits this long after a (re)subscription for its first frame
WAIT_POLL_S = 0.02
SPIN_TIMEOUT_S = 0.1

_UNKNOWN = {'profile': None, 'profile_id': None, 'platform': None}


def _ros_interfaces():
    """The message types, QoS profiles and shutdown exception, imported only
    when a camera starts (this module imports no ROS package at load)."""
    from rclpy.executors import ExternalShutdownException
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CompressedImage
    from std_msgs.msg import String
    return types.SimpleNamespace(
        CompressedImage=CompressedImage, String=String,
        ExternalShutdownException=ExternalShutdownException,
        # Only the newest picture matters; BEST_EFFORT also matches the
        # publishers' RELIABLE.
        image_qos=QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                             reliability=ReliabilityPolicy.BEST_EFFORT),
        # The statement's and the camera state's publishers are latched.
        latched_qos=QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                               durability=DurabilityPolicy.TRANSIENT_LOCAL))


def _start_thread(target, name):
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def _decode_jpeg(data):
    """BGR array of a JPEG, or None when it does not decode."""
    import cv2
    import numpy as np
    buf = np.frombuffer(bytes(data), dtype=np.uint8)
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _json_object(data):
    """The JSON object a std_msgs/String carries, or None (too large, not
    JSON, not an object)."""
    if not isinstance(data, str) or len(data.encode('utf-8', 'surrogatepass')) > STATE_MAX_BYTES:
        return None
    try:
        obj = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return obj if type(obj) is dict else None


def _statement_profile(data):
    """The profile a statement names, or None for anything else."""
    obj = _json_object(data)
    if obj is None:
        return None
    version, profile = obj.get('v'), obj.get('profile')
    if type(version) is not int or version != STATEMENT_VERSION:
        return None
    return profile if type(profile) is str and profile in scene_provenance.PROFILES else None


def _tower_ids(state):
    """The stamp's ``(profile_id, platform)`` from a tower camera state, each
    None where the field is unusable (the other one still counts)."""
    profile_id, platform = state.get('profile_id'), state.get('platform')
    if not (type(profile_id) is str and len(profile_id) == 12
            and all(c in '0123456789abcdef' for c in profile_id)):
        profile_id = None
    if not (type(platform) is str and platform in PLATFORMS):
        platform = None
    return profile_id, platform


class StudioCamera:
    """The tower camera as Roboter Studio sees it. Thread-safe; never raises
    out of a request, a hold or a callback."""

    def __init__(self, node_factory, executor_factory, clock=time.monotonic, sleep=time.sleep):
        self._node_factory = node_factory
        self._executor_factory = executor_factory
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._decode_lock = threading.Lock()
        self._ros = None
        self._node = None
        self._executor = None
        self._stopping = False
        # The picture subscription and what it delivered.
        self._image_sub = None
        self._image_gen = 0
        self._subscribed_at = None
        self._last_request = None
        self._holds = 0
        self._msg = None
        self._msg_seq = 0
        self._msg_arrival = None
        self._decoded = (None, None)        # (message seq, frame)
        # The statement and the camera state.
        self._profile = None
        self._state = None
        self._tower = (None, None)          # (profile_id, platform) of the latest tower state

    # ── life cycle ───────────────────────────────────────────────────────────

    def start(self):
        """Make the node and executor, subscribe the statement and the camera
        state, start the spin thread. Raises on failure (the caller logs it);
        the camera then stays inert: no picture, profile unknown."""
        ros = _ros_interfaces()
        node = self._node_factory(NODE_NAME, use_global_arguments=False)
        try:
            executor = self._executor_factory()
            executor.add_node(node)
            node.create_subscription(ros.String, scene_provenance.PROFILE_TOPIC,
                                     self._on_statement, ros.latched_qos)
            node.create_subscription(ros.String, STATE_TOPIC, self._on_state, ros.latched_qos)
        except Exception:
            try:
                node.destroy_node()
            except Exception:  # noqa: BLE001 — the original failure is the one to report
                pass
            raise
        with self._lock:
            self._ros, self._node, self._executor = ros, node, executor
        _start_thread(self._spin_loop, 'studio-exec')

    def shutdown(self):
        """Stop the spin loop, shut the executor down, destroy the node.
        Idempotent; never raises."""
        with self._lock:
            self._stopping = True
            node, executor = self._node, self._executor
            self._node = self._executor = None
            self._image_sub = None
            self._msg = None
            self._decoded = (None, None)
        if executor is not None:
            try:
                executor.shutdown()
            except Exception:  # noqa: BLE001 — shutdown must never raise
                pass
        if node is not None:
            try:
                node.destroy_node()
            except Exception:  # noqa: BLE001
                pass

    def _spin_loop(self):
        executor = self._executor
        logged = False
        while not self._stopping:
            try:
                executor.spin_once(timeout_sec=SPIN_TIMEOUT_S)
            except self._ros.ExternalShutdownException:
                return
            except Exception as e:  # noqa: BLE001 — logged once, then retried
                if self._stopping:
                    return
                if not logged:
                    logged = True
                    self._log_error(f'studio camera executor failed: {e!r}')
                self._sleep(SPIN_TIMEOUT_S)
                continue
            self._release_if_idle()

    def _log_error(self, text):
        node = self._node
        if node is not None:
            try:
                node.get_logger().error(text)
            except Exception:  # noqa: BLE001
                pass

    # ── the picture ──────────────────────────────────────────────────────────

    def hold(self):
        """Keep the picture subscribed until the matching ``release()``."""
        with self._lock:
            if self._node is None:
                return
            self._holds += 1
            self._ensure_subscribed_locked()

    def release(self):
        """End one ``hold()``; the picture then follows the idle rule, counted
        from now."""
        with self._lock:
            if self._holds > 0:
                self._holds -= 1
                if self._holds == 0:
                    self._last_request = self._clock()

    def latest_frame(self, max_age_s, wait_s=FIRST_FRAME_WAIT_S):
        """``(bgr, age_s)`` of the newest picture, or None (no camera, no
        frame within the first-frame wait, older than ``max_age_s``, not
        decodable). The frame is shared: never write into it."""
        with self._lock:
            if self._node is None or not self._ensure_subscribed_locked():
                return None
            self._last_request = self._clock()
            deadline = self._subscribed_at + wait_s
        while True:
            with self._lock:
                if self._image_sub is None:
                    return None
                if self._msg is not None:
                    msg, seq, arrival = self._msg, self._msg_seq, self._msg_arrival
                    break
                self._last_request = self._clock()
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            self._sleep(min(WAIT_POLL_S, remaining))
        age = self._clock() - arrival
        if age > max_age_s:
            return None
        frame = self._decode(seq, msg)
        return None if frame is None else (frame, age)

    def frame_age(self):
        """Seconds since the newest picture arrived, or None."""
        with self._lock:
            if self._msg is None:
                return None
            return self._clock() - self._msg_arrival

    def _decode(self, seq, msg):
        with self._decode_lock:
            cached_seq, frame = self._decoded
            if cached_seq == seq:
                return frame
            try:
                frame = _decode_jpeg(msg.data)
            except Exception:  # noqa: BLE001 — a frame that does not decode is no frame
                frame = None
            if frame is not None:
                frame.flags.writeable = False
            self._decoded = (seq, frame)
            return frame

    def _ensure_subscribed_locked(self):
        """True when the picture is subscribed (now or already)."""
        if self._image_sub is not None:
            return True
        self._image_gen += 1
        try:
            self._image_sub = self._node.create_subscription(
                self._ros.CompressedImage, STUDIO_TOPIC,
                functools.partial(self._on_image, self._image_gen), self._ros.image_qos)
        except Exception as e:  # noqa: BLE001 — no picture, never a raise
            self._log_error(f'studio picture not subscribed: {e!r}')
            return False
        self._subscribed_at = self._last_request = self._clock()
        self._msg = None
        return True

    def _release_if_idle(self):
        """Unsubscribe the picture once nobody holds it and the last request
        is ``IDLE_RELEASE_S`` old (run by the spin thread between spins)."""
        with self._lock:
            sub, node = self._image_sub, self._node
            if sub is None or self._holds > 0:
                return
            if self._clock() - self._last_request < IDLE_RELEASE_S:
                return
            self._image_sub = None
            self._msg = None
            self._decoded = (None, None)    # a full-size frame is not kept for nobody
        try:
            node.destroy_subscription(sub)
        except Exception as e:  # noqa: BLE001
            self._log_error(f'studio picture not unsubscribed: {e!r}')

    def _on_image(self, gen, msg):
        with self._lock:
            if gen != self._image_gen or self._image_sub is None:
                return      # a late message of an ended subscription
            self._msg = msg
            self._msg_seq += 1
            self._msg_arrival = self._clock()

    # ── the statement and the camera state ──────────────────────────────────

    def _on_statement(self, msg):
        profile = _statement_profile(getattr(msg, 'data', None))
        with self._lock:
            self._profile = profile

    def _on_state(self, msg):
        state = _json_object(getattr(msg, 'data', None))
        if state is None:
            return
        with self._lock:
            self._state = state
            if state.get('profile') == PROFILE_TOWER:
                self._tower = _tower_ids(state)

    def profile(self):
        """``'tower_v1'`` or ``'legacy'`` as the arm container states it;
        None while there is no statement or the latest cannot be read."""
        with self._lock:
            return self._profile

    def provider(self):
        """The recorder's scene-camera provider (``scene_provenance``)."""
        with self._lock:
            profile, (profile_id, platform) = self._profile, self._tower
        if profile is None:
            return dict(_UNKNOWN)
        if profile != PROFILE_TOWER:
            return {'profile': profile, 'profile_id': None, 'platform': None}
        return {'profile': profile, 'profile_id': profile_id, 'platform': platform}

    def camera_state(self):
        """A copy of the latest camera state, or None."""
        with self._lock:
            return None if self._state is None else dict(self._state)
