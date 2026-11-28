# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-07: ``scene_camera_node.py``, the tower scene camera's capture owner on
the Orange Pi (spec 40 S1).

In the tower profile the entrypoint hands the scene slot to this node instead
of usb_cam. It runs ``scene_split.selftest()`` once, opens the camera through
V4L2 in MJPG 1920x1080 at 30 fps with ``CONVERT_RGB`` off, feeds every frame to
``scene_split.CaptureVerifier`` (platform ``pi``, outside quality 95) and lets a
frame out only while the verifier streams: the 640x480 outside frame on
``/<name>/image_raw/compressed`` exactly as the ingest node publishes a camera,
the camera's own JPEG at most 15 Hz on ``STUDIO_TOPIC``, and the state message
on ``STATE_TOPIC`` on every change and once a second. A truncated frame is
published on neither topic and counted in the state's ``dropped`` (D59). The
node reads no environment variable: its three arguments are all it knows.

The node is loaded under ``ros_stubs``' fake ROS modules and driven one step at
a time with a fake capture and a fake monotonic clock; the frames are the real
1080p fixture of ``test_scene_split.py``.
"""

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

import numpy as np

import ros_stubs
from timeout_guard import BoundedTestCase

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
_ARM_DIR = os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'open_manipulator')
_NODE = os.path.join(_ARM_DIR, 'scene_camera_node.py')
_SPLIT_FILE = os.path.join(_ARM_DIR, 'scene_split.py')
_DOCKERFILE = os.path.join(_ARM_DIR, 'Dockerfile')
_PARITY_SH = os.path.join(_REPO, '.github', 'scripts', 'image_source_parity.sh')
_FIXTURE = os.path.join(_HERE, 'fixtures', 'scene_split_1080p.jpg')

_SCENE_TOPIC = '/scene/image_raw/compressed'
_WALL_NS = 1_700_000_000_123_456_789
_SELFTEST_DE = ('Selbsttest der Bildaufbereitung fehlgeschlagen – die Turm-Kamera '
                'bleibt aus. Bitte EduBotics aktualisieren.')


def _load_split():
    spec = importlib.util.spec_from_file_location('scene_split_for_node_tests', _SPLIT_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SPLIT = _load_split()


def _load_node_module(name):
    """The node module under the fake ROS; it imports its sibling
    ``scene_split.py`` itself, exactly as it does from /usr/local/bin."""
    with mock.patch.dict(sys.modules, ros_stubs.modules()):
        sys.modules.pop('scene_split', None)
        spec = importlib.util.spec_from_file_location(name, _NODE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


_MOD = _load_node_module('scene_camera_node_under_test')

with open(_FIXTURE, 'rb') as _handle:
    _JPEG = _handle.read()


def _raw(data):
    """A passthrough sample as OpenCV's V4L2 backend hands it over with
    CONVERT_RGB off: the camera's JPEG as a 1xN uint8 array."""
    return np.frombuffer(data, np.uint8).reshape(1, -1)


_GOOD = _raw(_JPEG)
_TRUNCATED = _raw(_JPEG[:len(_JPEG) // 2])


class _Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class _FakeCapture:
    """Hands out ``frames`` one per read, advancing the clock one frame period
    per read; an empty queue reads as a failed read."""

    def __init__(self, clock, frames=(), period=1 / 30):
        self.clock = clock
        self.frames = list(frames)
        self.period = period
        self.released = False

    def read(self):
        self.clock.t += self.period
        if not self.frames:
            return False, None
        return True, self.frames.pop(0)

    def release(self):
        self.released = True


class _NodeCase(BoundedTestCase):
    TEST_TIMEOUT_S = 60.0

    def setUp(self):
        self.clock = _Clock()
        self.capture = _FakeCapture(self.clock)
        self.opened = []
        self.controls = []
        self._patch(_MOD.scene_split, 'selftest', lambda: '')
        self._patch(_MOD, 'open_capture', self._open)
        self._patch(_MOD.subprocess, 'run', self._run)
        self._patch(_MOD.time, 'time_ns', lambda: _WALL_NS)
        self.node = _MOD.SceneCameraNode('/dev/video4', 'scene', clock=self.clock)

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _open(self, device):
        self.opened.append(device)
        return self.capture

    def _run(self, argv, **kwargs):
        self.controls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    # -- what the node published ------------------------------------------

    def published(self, topic):
        found = [p for p in self.node.publishers if p.topic == topic]
        if len(found) != 1:
            raise AssertionError(f'{len(found)} publishers on {topic}: '
                                 f'{[p.topic for p in self.node.publishers]}')
        return found[0].published

    def states(self):
        return [json.loads(m.data) for m in self.published(_SPLIT.STATE_TOPIC)]

    def feed(self, *frames):
        """Queue the frames and step once per frame (the first step opens)."""
        self.capture.frames.extend(frames)
        if not self.opened:
            self.node.step()       # the self-test
            self.node.step()       # the open
        for _ in frames:
            self.node.step()

    def stream(self):
        """Open the camera and feed the 20 frames that prove the mode."""
        self.feed(*([_GOOD] * _SPLIT.VERIFY_FRAMES))
        self.assertEqual(self.node.verifier.state, _SPLIT.STATE_STREAMING)


class TheTopics(_NodeCase):

    def test_three_publishers_with_the_ingest_nodes_qos(self):
        topics = sorted(p.topic for p in self.node.publishers)
        self.assertEqual(topics, sorted([_SCENE_TOPIC, _SPLIT.STUDIO_TOPIC, _SPLIT.STATE_TOPIC]))
        scene = next(p for p in self.node.publishers if p.topic == _SCENE_TOPIC)
        self.assertIs(scene.msg_type, ros_stubs.CompressedImage)
        self.assertEqual((scene.qos.depth, scene.qos.reliability, scene.qos.history),
                         (5, 'reliable', 'keep_last'))
        studio = next(p for p in self.node.publishers if p.topic == _SPLIT.STUDIO_TOPIC)
        self.assertIs(studio.msg_type, ros_stubs.CompressedImage)
        self.assertEqual((studio.qos.depth, studio.qos.reliability), (2, 'reliable'))
        state = next(p for p in self.node.publishers if p.topic == _SPLIT.STATE_TOPIC)
        self.assertIs(state.msg_type, ros_stubs.String)
        self.assertEqual((state.qos.depth, state.qos.reliability, state.qos.durability),
                         (1, 'reliable', 'transient_local'))

    def test_the_node_keeps_rclpys_own_clock(self):
        # rclpy's Node keeps its ROS clock in ``_clock`` and its timers run on
        # it; a monotonic callable stored under that name made create_timer
        # raise under the real rclpy (measured in the arm image).
        self.assertIsInstance(self.node.get_clock(), ros_stubs.FakeClock)

    def test_the_name_argument_names_the_topic(self):
        node = _MOD.SceneCameraNode('/dev/video4', 'tower', clock=self.clock)
        self.assertIn('/tower/image_raw/compressed', [p.topic for p in node.publishers])


class NothingLeavesBeforeStreaming(_NodeCase):

    def test_nineteen_good_frames_publish_no_image(self):
        self.feed(*([_GOOD] * (_SPLIT.VERIFY_FRAMES - 1)))
        self.assertEqual(self.node.verifier.state, _SPLIT.STATE_VERIFYING)
        self.assertEqual(self.published(_SCENE_TOPIC), [])
        self.assertEqual(self.published(_SPLIT.STUDIO_TOPIC), [])
        self.assertEqual(self.states()[-1]['state'], 'verifying')

    def test_the_twentieth_frame_is_the_first_one_out(self):
        self.stream()
        self.assertEqual(len(self.published(_SCENE_TOPIC)), 1)
        self.assertEqual(len(self.published(_SPLIT.STUDIO_TOPIC)), 1)
        self.assertEqual(self.states()[-1]['state'], 'streaming')

    def test_a_wrong_mode_never_streams(self):
        # YUYV handed over raw: a 1xN array that is no JPEG.
        yuyv = np.zeros((1, 1920 * 1080 * 2), np.uint8)
        self.feed(*([yuyv] * 100))
        self.assertEqual(self.node.verifier.fault, _SPLIT.FAULT_WRONG_MODE)
        self.assertEqual(self.published(_SCENE_TOPIC), [])
        self.assertEqual(self.published(_SPLIT.STUDIO_TOPIC), [])


class TheMessages(_NodeCase):

    def test_the_scene_frame_is_the_640x480_outside_frame(self):
        self.stream()
        msg = self.published(_SCENE_TOPIC)[0]
        self.assertEqual((msg.format, msg.header.frame_id), ('jpeg', 'scene'))
        data = bytes(msg.data)
        self.assertEqual(_SPLIT.jpeg_size(data), (640, 480))
        self.assertTrue(_SPLIT.jpeg_complete(data))
        import cv2
        decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        reference = _SPLIT.outside_from_jpeg(_JPEG)
        diff = float(np.abs(decoded.astype(float) - reference.astype(float)).mean())
        self.assertLess(diff, 2.0, 'the outside frame is the derivation, encoded at 95')
        self.assertEqual((msg.header.stamp.sec, msg.header.stamp.nanosec),
                         (_WALL_NS // 1_000_000_000, _WALL_NS % 1_000_000_000))

    def test_the_studio_frame_is_the_cameras_own_jpeg(self):
        self.stream()
        msg = self.published(_SPLIT.STUDIO_TOPIC)[0]
        self.assertEqual((msg.format, msg.header.frame_id), ('jpeg', 'scene_studio'))
        self.assertEqual(bytes(msg.data), _JPEG)
        self.assertEqual((msg.header.stamp.sec, msg.header.stamp.nanosec),
                         (_WALL_NS // 1_000_000_000, _WALL_NS % 1_000_000_000))

    def test_the_state_message(self):
        self.stream()
        raw = self.published(_SPLIT.STATE_TOPIC)[-1].data
        state = json.loads(raw)
        self.assertEqual(list(state), list(_SPLIT.STATE_KEYS))
        self.assertEqual(state['v'], 1)
        self.assertEqual(state['profile'], 'tower_v1')
        self.assertEqual(state['platform'], 'pi')
        self.assertEqual(state['profile_id'], '8b7f72cb250c')
        self.assertEqual(state['profile_id'], _SPLIT.profile_id(95))
        self.assertEqual((state['width'], state['height']), (1920, 1080))
        self.assertIs(state['passthrough'], True)
        self.assertEqual((state['outside'], state['fault'], state['dropped']), ('640x480', '', 0))
        self.assertEqual(raw.encode('utf-8'),
                         _SPLIT.state_message(**self.node.verifier.state_fields()))

    def test_the_quality_argument_is_the_profile_ids(self):
        node = _MOD.SceneCameraNode('/dev/video4', 'scene', quality=80, clock=self.clock)
        self.assertEqual(node.verifier.profile_id, _SPLIT.profile_id(80))
        self.assertEqual(node.verifier.platform, 'pi')

    def test_the_studio_stream_is_at_most_15_hz_while_every_frame_is_recorded(self):
        self.stream()
        scene0 = len(self.published(_SCENE_TOPIC))
        studio0 = len(self.published(_SPLIT.STUDIO_TOPIC))
        self.feed(*([_GOOD] * 60))           # 2 s at 30 fps
        self.assertEqual(len(self.published(_SCENE_TOPIC)) - scene0, 60)
        studio = len(self.published(_SPLIT.STUDIO_TOPIC)) - studio0
        self.assertGreaterEqual(studio, 29)
        self.assertLessEqual(studio, 31)

    def test_the_state_is_sent_on_a_change_and_once_a_second(self):
        self.stream()
        sent = len(self.states())
        self.feed(*([_GOOD] * 15))           # half a second: nothing changed
        self.assertEqual(len(self.states()), sent)
        self.feed(*([_GOOD] * 16))           # past the second
        self.assertEqual(len(self.states()), sent + 1)

    def test_the_timer_keeps_the_state_alive_while_the_capture_is_silent(self):
        self.stream()
        sent = len(self.states())
        self.clock.t += 1.1
        self.node._tick()
        self.assertEqual(len(self.states()), sent + 1)
        self.assertEqual(self.states()[-1]['state'], 'streaming')
        self.clock.t += 1.0
        self.node._tick()
        self.assertEqual(self.states()[-1]['fault'], 'no_frames')
        self.assertEqual(self.node.timers[0].callback, self.node._tick)
        self.assertLessEqual(self.node.timers[0].period_s, 0.5)


class ATruncatedFrameIsDropped(_NodeCase):
    """D59: a lost USB packet cuts a JPEG short, and OpenCV still decodes it to
    full size; the frame check drops it, on both topics, and counts it."""

    def test_a_truncated_frame_is_published_on_neither_topic(self):
        self.stream()
        scene0 = len(self.published(_SCENE_TOPIC))
        studio0 = len(self.published(_SPLIT.STUDIO_TOPIC))
        self.clock.t += 0.2                  # the studio slot is free again
        self.feed(_TRUNCATED)
        self.assertEqual(len(self.published(_SCENE_TOPIC)), scene0)
        self.assertEqual(len(self.published(_SPLIT.STUDIO_TOPIC)), studio0)
        self.feed(*([_GOOD] * 33))           # past the next state message
        self.assertEqual(len(self.published(_SCENE_TOPIC)), scene0 + 33)
        self.assertEqual(self.states()[-1]['dropped'], 1)
        self.assertEqual(self.states()[-1]['state'], 'streaming')

    def test_a_frame_the_split_refuses_is_dropped_and_counted_too(self):
        # A frame whose header passes the check but whose half-size decode
        # fails: the verifier released it, the split refuses it.
        self.stream()
        scene0 = len(self.published(_SCENE_TOPIC))
        studio0 = len(self.published(_SPLIT.STUDIO_TOPIC))

        def refuse(_data):
            raise _MOD.scene_split.SplitError('the half-size decode failed')
        with mock.patch.object(_MOD.scene_split, 'outside_from_jpeg', refuse):
            self.clock.t += 0.2
            self.feed(_GOOD)
        self.assertEqual(len(self.published(_SCENE_TOPIC)), scene0)
        self.assertEqual(len(self.published(_SPLIT.STUDIO_TOPIC)), studio0)
        self.assertEqual(self.node.verifier.dropped, 1)
        self.feed(*([_GOOD] * 33))
        self.assertEqual(self.states()[-1]['dropped'], 1)

    def test_fifteen_the_split_refuses_in_a_row_close_the_device(self):
        self.stream()
        scene0 = len(self.published(_SCENE_TOPIC))

        def refuse(_data):
            raise _MOD.scene_split.SplitError('the half-size decode failed')
        with mock.patch.object(_MOD.scene_split, 'outside_from_jpeg', refuse):
            self.feed(*([_GOOD] * _SPLIT.BAD_FRAMES_LIMIT))
        self.assertEqual(self.states()[-1]['fault'], 'bad_frames')
        self.assertEqual(self.states()[-1]['dropped'], _SPLIT.BAD_FRAMES_LIMIT)
        self.assertTrue(self.capture.released)
        self.assertEqual(len(self.published(_SCENE_TOPIC)), scene0)

    def test_fifteen_in_a_row_close_the_device(self):
        self.stream()
        self.feed(*([_TRUNCATED] * _SPLIT.BAD_FRAMES_LIMIT))
        self.assertEqual(self.states()[-1]['fault'], 'bad_frames')
        self.assertEqual(self.states()[-1]['dropped'], _SPLIT.BAD_FRAMES_LIMIT)
        self.assertTrue(self.capture.released)
        self.assertEqual(len(self.published(_SCENE_TOPIC)), 1)


class _RaisingCapture(_FakeCapture):
    """A fake capture whose read raises while ``raising`` is set, each such read
    advancing the clock half a second (a pulled device under OpenCV)."""

    def __init__(self, clock):
        super().__init__(clock)
        self.raising = False
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.raising:
            self.clock.t += 0.5
            raise RuntimeError('the device went away')
        return super().read()


class ARaisingReadIsAFailedRead(_NodeCase):
    """X-K1PI (K-1's Pi half): a ``cap.read()`` that raises ended the capture
    thread silently; the timer then reported ``no_frames`` for ever and the
    device was never opened again, so „sie wird erneut geöffnet“ was untrue.
    A read that raises is a read that returned nothing (the Windows twin,
    ``camera_bridge._SceneCaptureWorker``, since K-1)."""

    def setUp(self):
        super().setUp()
        self.capture = _RaisingCapture(self.clock)

    def test_a_read_that_raises_is_a_failed_read_and_the_camera_is_reopened(self):
        self.stream()
        self.capture.raising = True
        for _ in range(6):                   # 3 s of reads that raise: past SILENCE_S
            self.node.step()
        self.assertEqual((self.states()[-1]['state'], self.states()[-1]['fault']), ('fault', 'no_frames'))
        self.assertTrue(self.capture.released)
        self.capture.raising = False
        self.capture.released = False
        self.clock.t += _SPLIT.RETRY_FIRST_S + 0.1
        self.node.step()                     # the back-off is over: opened again
        self.assertEqual(self.opened, ['/dev/video4', '/dev/video4'])
        self.feed(*([_GOOD] * _SPLIT.VERIFY_FRAMES))
        self.assertEqual(self.states()[-1]['state'], 'streaming')
        self.assertFalse(self.capture.released)

    def test_a_read_that_keeps_raising_is_no_frames_and_the_device_is_opened_again(self):
        """While streaming, SILENCE_S (2 s) of raising reads end it; after a
        reopen the camera has its start-up allowance first (FIRST_FRAME_WAIT_S,
        2.41.2), so a reopened device that never sends a frame takes longer."""
        self.stream()
        self.capture.raising = True
        for attempt in range(3):
            steps = 6 if attempt == 0 else int(2 * _SPLIT.FIRST_FRAME_WAIT_S) + 2
            for _ in range(steps):
                self.node.step()
            self.assertEqual(self.states()[-1]['fault'], 'no_frames', attempt)
            self.assertTrue(self.capture.released, attempt)
            self.assertIsNone(self.node._cap, attempt)
            self.clock.t += _SPLIT.RETRY_MAX_S + 0.1     # past any back-off
            self.node.step()
        self.assertEqual(len(self.opened), 4)

    def test_the_capture_thread_survives_a_read_that_raises(self):
        self.capture.raising = True
        self.node.start()
        deadline = time.monotonic() + 5.0
        while self.capture.reads < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(self.capture.reads, 4)
        self.assertTrue(self.node._thread.is_alive())
        self.node.destroy_node()
        self.assertFalse(self.node._thread.is_alive())
        self.assertTrue(self.capture.released)

    def test_an_unexpected_end_of_the_loop_leaves_no_streaming_state(self):
        self.stream()

        def broken():
            raise RuntimeError('a defect in the loop')
        self.node.step = broken
        with self.assertRaises(RuntimeError):
            self.node._run()
        self.assertEqual(self.states()[-1]['state'], 'closed')
        self.assertTrue(self.capture.released)
        self.clock.t += 2.5                  # the timer keeps the state message alive, still closed
        self.node._tick()
        self.assertEqual(self.states()[-1]['state'], 'closed')
        self.assertTrue(any('RuntimeError' in line for line in self.node.get_logger().lines('warn')))


class TheDecodedFallback(_NodeCase):

    def test_a_decoded_stream_is_cut_and_its_studio_frame_encoded(self):
        import cv2
        frame = cv2.imdecode(np.frombuffer(_JPEG, np.uint8), cv2.IMREAD_COLOR)
        self.feed(*([frame] * _SPLIT.VERIFY_FRAMES))
        self.assertEqual(self.node.verifier.state, _SPLIT.STATE_STREAMING)
        self.assertIs(self.states()[-1]['passthrough'], False)
        self.assertEqual(_SPLIT.jpeg_size(bytes(self.published(_SCENE_TOPIC)[0].data)), (640, 480))
        studio = bytes(self.published(_SPLIT.STUDIO_TOPIC)[0].data)
        self.assertEqual(_SPLIT.jpeg_size(studio), (1920, 1080))


class TheSelfTestAndTheOpen(_NodeCase):

    def test_a_failed_self_test_never_opens_the_camera(self):
        self._patch(_MOD.scene_split, 'selftest', lambda: 'the window moved')
        self.assertFalse(self.node.step())
        self.assertEqual(self.opened, [])
        self.assertEqual(self.controls, [])
        self.assertEqual(self.states()[-1]['fault'], 'selftest')
        self.assertIn(_SELFTEST_DE, ' '.join(self.node.get_logger().lines('warn')))
        self.assertEqual(self.published(_SCENE_TOPIC), [])

    def test_a_failed_open_is_open_failed_and_retried_after_two_seconds(self):
        self._patch(_MOD, 'open_capture', lambda device: self.opened.append(device))
        self.node.step()
        self.node.step()
        self.assertEqual(self.states()[-1]['fault'], 'open_failed')
        self.assertEqual(len(self.opened), 1)
        self.clock.t += 1.0
        self.node.step()
        self.assertEqual(len(self.opened), 1, 'retried before the back-off')
        self.clock.t += 1.1
        self.node.step()
        self.assertEqual(len(self.opened), 2)

    def test_the_v4l2_controls_are_tried_before_the_open(self):
        def run(argv, **kwargs):
            self.controls.append((list(argv), kwargs))
            self.assertEqual(self.opened, [], 'a control after the open')
            return subprocess.CompletedProcess(argv, 1, b'', b'unknown control')
        self._patch(_MOD.subprocess, 'run', run)
        self.feed(_GOOD)
        self.assertEqual([argv for argv, _kw in self.controls], [
            ['v4l2-ctl', '-d', '/dev/video4', '-c', 'exposure_dynamic_framerate=0'],
            ['v4l2-ctl', '-d', '/dev/video4', '-c', 'exposure_auto_priority=0']])
        self.assertTrue(all(kw.get('timeout') for _argv, kw in self.controls), self.controls)
        self.assertEqual(self.opened, ['/dev/video4'])

    def test_the_first_control_that_works_ends_the_attempt(self):
        self.feed(_GOOD)
        self.assertEqual([argv[-1] for argv, _kw in self.controls],
                         ['exposure_dynamic_framerate=0'])

    def test_a_missing_tool_is_one_log_line_and_never_stops_the_camera(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])
        self._patch(_MOD.subprocess, 'run', missing)
        self._patch(_MOD, 'open_capture', lambda device: self.opened.append(device))
        before = len(self.node.get_logger().records)
        self.node.step()
        self.node.step()
        self.clock.t += 2.1
        self.node.step()
        self.assertEqual(len(self.opened), 2)
        lines = [t for _lvl, t in self.node.get_logger().records[before:] if 'v4l2' in t]
        self.assertEqual(len(lines), 1, lines)


class _RecordingCapture:

    def __init__(self, calls, opened):
        self.calls = calls
        self.opened = opened

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.calls.append(('set', prop, value))
        return True

    def release(self):
        self.calls.append(('release',))


def _recording_cv2(calls, opened=True):
    """A stand-in cv2 that records how ``open_capture`` builds and sets the
    capture; the ids are OpenCV's own."""
    def video_capture(*args):
        calls.append(('VideoCapture',) + args)
        return _RecordingCapture(calls, opened)

    return types.SimpleNamespace(
        CAP_V4L2=200, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5,
        CAP_PROP_FOURCC=6, CAP_PROP_CONVERT_RGB=16,
        VideoCapture=video_capture,
        VideoWriter_fourcc=lambda *chars: ''.join(chars))


class TheV4l2Open(unittest.TestCase):
    """Spec 40 S1: the Pi opens the tower camera through V4L2 in MJPG 1920x1080
    at 30 fps with ``CONVERT_RGB`` off. Without the FOURCC the driver keeps the
    format the device had (YUY2 at 1080p is 5 fps by the manual, refused as
    ``wrong_mode``); without ``CONVERT_RGB`` 0 OpenCV decodes every frame and
    the camera's own JPEG never passes through. Every other test of this file
    replaces ``open_capture``, so this is its one fence."""

    def _open(self, opened=True):
        calls = []
        fake = _recording_cv2(calls, opened)
        with mock.patch.dict(sys.modules, {'cv2': fake}):
            cap = _MOD.open_capture('/dev/video4')
        return fake, cap, calls

    def test_the_mode_is_set_after_a_v4l2_open(self):
        fake, cap, calls = self._open()
        self.assertIsInstance(cap, _RecordingCapture)
        self.assertEqual(calls, [
            ('VideoCapture', '/dev/video4', fake.CAP_V4L2),
            ('set', fake.CAP_PROP_FOURCC, 'MJPG'),
            ('set', fake.CAP_PROP_FRAME_WIDTH, 1920),
            ('set', fake.CAP_PROP_FRAME_HEIGHT, 1080),
            ('set', fake.CAP_PROP_FPS, 30),
            ('set', fake.CAP_PROP_CONVERT_RGB, 0),
        ])

    def test_a_device_that_does_not_open_is_released_and_none(self):
        fake, cap, calls = self._open(opened=False)
        self.assertIsNone(cap)
        self.assertEqual(calls, [('VideoCapture', '/dev/video4', fake.CAP_V4L2), ('release',)])

    def test_the_stand_in_ids_are_opencvs(self):
        import cv2
        fake = _recording_cv2([])
        for name in ('CAP_V4L2', 'CAP_PROP_FRAME_WIDTH', 'CAP_PROP_FRAME_HEIGHT', 'CAP_PROP_FPS',
                     'CAP_PROP_FOURCC', 'CAP_PROP_CONVERT_RGB'):
            with self.subTest(name):
                self.assertEqual(getattr(fake, name), getattr(cv2, name))


class TheArguments(unittest.TestCase):

    def test_device_name_and_the_default_quality(self):
        args = _MOD.parse_args(['--device', '/dev/video2', '--name', 'scene'])
        self.assertEqual((args.device, args.name, args.quality), ('/dev/video2', 'scene', 95))
        args = _MOD.parse_args(['--device', '/dev/video2', '--name', 'scene', '--quality', '80'])
        self.assertEqual(args.quality, 80)

    def test_device_and_name_are_required(self):
        with mock.patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                _MOD.parse_args(['--name', 'scene'])
            with self.assertRaises(SystemExit):
                _MOD.parse_args(['--device', '/dev/video2'])


def _environment_reads(source):
    """Every place the source reads the process environment."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in ('environ', 'environb', 'getenv'):
            found.append(f'line {node.lineno}: .{node.attr}')
        elif isinstance(node, ast.Name) and node.id in ('environ', 'getenv'):
            found.append(f'line {node.lineno}: {node.id}')
        elif isinstance(node, ast.ImportFrom) and node.module == 'os' and any(
                alias.name in ('environ', 'environb', 'getenv') for alias in node.names):
            found.append(f'line {node.lineno}: from os import')
    return found


class ItReadsNoEnvironmentVariable(unittest.TestCase):
    """Spec 40 S1: the node knows its device, its name and its quality from its
    arguments and nothing else; the profile decision is the entrypoint's."""

    def test_the_source_reads_no_environment(self):
        with open(_NODE, encoding='utf-8') as handle:
            self.assertEqual(_environment_reads(handle.read()), [])

    def test_the_scan_sees_a_planted_read(self):
        with open(_NODE, encoding='utf-8') as handle:
            source = handle.read()
        for planted in ("os.environ.get('EDUBOTICS_SCENE_CAMERA_PROFILE')",
                        "os.getenv('X')", "__import__('os').environ"):
            with self.subTest(planted=planted):
                self.assertNotEqual(_environment_reads(source + f'\nVALUE = {planted}\n'), [])
        self.assertNotEqual(_environment_reads(source + '\nfrom os import environ\n'), [])


def _logical_lines():
    with open(_DOCKERFILE, encoding='utf-8') as handle:
        return re.sub(r'\\\n', ' ', handle.read()).splitlines()


class ItShipsAsAPlainCopy(unittest.TestCase):

    def test_the_dockerfile_copies_it_and_lists_it_for_crlf_and_exec(self):
        lines = _logical_lines()
        self.assertIn('COPY scene_camera_node.py /usr/local/bin/scene_camera_node.py', lines)
        sed = next(line for line in lines if "sed -i 's/\\r$//'" in line)
        self.assertIn('/usr/local/bin/scene_camera_node.py', sed.split('&&')[0])
        self.assertIn('/usr/local/bin/scene_camera_node.py', sed.split('chmod +x', 1)[1])

    def test_the_parity_loop_byte_verifies_it(self):
        with open(_PARITY_SH, encoding='utf-8') as handle:
            text = handle.read()
        match = re.search(r'extract /usr/local/bin /tmp/parity_bin\n\s*for f in ([^;]+); do', text)
        self.assertIsNotNone(match)
        self.assertIn('scene_camera_node.py', match.group(1).split())

    def test_the_node_imports_its_sibling_scene_split(self):
        self.assertTrue(os.path.samefile(_MOD.scene_split.__file__, _SPLIT_FILE),
                        _MOD.scene_split.__file__)


if __name__ == '__main__':
    unittest.main()
