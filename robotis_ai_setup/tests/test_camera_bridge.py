"""Tests for the native camera bridge (camera_bridge.py).

Platform-agnostic: cv2 is imported lazily inside the capture/sender loops, so
constructing a CameraBridge (without start()) needs no OpenCV. The wire-format
contract with the container's camera_ingest_node.py is asserted against a
hardcoded struct so a drift on either side fails the test.
"""

import json
import pathlib
import struct
import threading
import time
import unittest
from unittest import mock

from gui.app import camera_bridge, constants, scene_split
from timeout_guard import BoundedTestCase

try:  # numpy/cv2 may be absent on a bare runner (the split needs both).
    import cv2  # noqa: F401
    import numpy as np
    _HAVE_CV2 = True
except ImportError:  # pragma: no cover - environment dependent
    _HAVE_CV2 = False

_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'scene_split_1080p.jpg'


class TestWireFormat(unittest.TestCase):

    def test_header_struct_matches_ingest_contract(self):
        # camera_ingest_node.py uses struct.Struct(">BIQ"): cam_id (u8),
        # jpeg_len (u32 BE), capture_ns (u64 BE). Both ends MUST agree.
        self.assertEqual(camera_bridge._HEADER.format, ">BIQ")
        self.assertEqual(camera_bridge._HEADER.size, 1 + 4 + 8)

    def test_header_roundtrip_against_independent_unpacker(self):
        packed = camera_bridge._HEADER.pack(1, 12345, 9876543210)
        ingest_side = struct.Struct(">BIQ")  # mirror of the node's _HEADER
        cam_id, jpeg_len, capture_ns = ingest_side.unpack(packed)
        self.assertEqual((cam_id, jpeg_len, capture_ns), (1, 12345, 9876543210))


class TestCameraBridgeMapping(unittest.TestCase):

    def test_role_to_cam_id_mapping(self):
        # gripper -> cam_id 0, scene -> cam_id 1, regardless of dict order or
        # which OpenCV index each camera has. Must match the container's
        # EDUBOTICS_CAMERA_NAMES order (gripper,scene).
        bridge = camera_bridge.CameraBridge({"scene": 3, "gripper": 2})
        by_role = {w.role: w for w in bridge.workers}
        self.assertEqual(by_role["gripper"].cam_id, 0)
        self.assertEqual(by_role["gripper"].index, 2)
        self.assertEqual(by_role["scene"].cam_id, 1)
        self.assertEqual(by_role["scene"].index, 3)

    def test_single_camera_keeps_role_cam_id(self):
        # Only scene assigned -> still cam_id 1 so it publishes /scene/...
        bridge = camera_bridge.CameraBridge({"scene": 5})
        self.assertEqual(len(bridge.workers), 1)
        self.assertEqual(bridge.workers[0].cam_id, 1)

    def test_none_index_skipped(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0, "scene": None})
        self.assertEqual([w.role for w in bridge.workers], ["gripper"])

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            camera_bridge.CameraBridge({"wrist": 0})

    def test_stop_before_start_is_safe(self):
        # _on_close / _stop_camera_bridge may call stop() on a bridge that was
        # constructed but never started — must not raise (threads unstarted).
        bridge = camera_bridge.CameraBridge({"gripper": 0})
        bridge.stop()  # should not raise

    def test_status_shape(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0, "scene": 1})
        status = bridge.status()
        self.assertIn("connected", status)
        self.assertIn("cameras", status)
        self.assertEqual(set(status["cameras"]), {"gripper", "scene"})
        self.assertIn("fps", status["cameras"]["gripper"])
        self.assertIn("negotiated_fps", status["cameras"]["gripper"])

    def test_one_sender_per_camera(self):
        # Each camera gets its OWN sender (own TCP connection) so one camera's
        # stall can't delay the other.
        bridge = camera_bridge.CameraBridge({"gripper": 2, "scene": 3})
        self.assertEqual(len(bridge._senders), 2)
        self.assertEqual({s.worker.role for s in bridge._senders}, {"gripper", "scene"})
        # senders share the bridge's single stop event
        self.assertTrue(all(s._halt is bridge._stop for s in bridge._senders))

    def test_connected_false_before_start(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0})
        self.assertFalse(bridge.connected)


class TestConstants(unittest.TestCase):

    def test_bridge_roles_match_default_camera_names(self):
        # The container default EDUBOTICS_CAMERA_NAMES is "gripper,scene"; the
        # GUI's role->cam_id order must match.
        self.assertEqual(constants.CAMERA_BRIDGE_ROLES, ("gripper", "scene"))


# ---------------------------------------------------------------------------
# The tower scene worker when the camera fails mid-stream (FIX-K-r1, K-1)
# ---------------------------------------------------------------------------

class _StepClock:
    """A monotonic clock that moves only when told (by a read, or by the test)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._t = 1000.0

    def __call__(self):
        with self._lock:
            return self._t

    def advance(self, dt):
        with self._lock:
            self._t += dt


class _FailingCam:
    """The 1080p fixture as MSMF's passthrough sample, one frame per read, each
    read 1/30 s on the clock. While ``raising`` is set a read raises; while
    ``blocking`` is set a read blocks until ``unblock`` is set."""

    def __init__(self, clock, frame, raising, blocking, unblock):
        self.clock = clock
        self.frame = frame
        self.raising = raising
        self.blocking = blocking
        self.unblock = unblock
        self.released = False

    def read(self):
        self.clock.advance(1.0 / 30.0)
        if self.blocking.is_set():
            self.unblock.wait(30.0)
        if self.raising.is_set():
            raise RuntimeError('device gone')
        time.sleep(0.001)
        return True, self.frame

    def release(self):
        self.released = True


def _state_of(worker):
    item, seq = worker.state_slot.latest()
    return json.loads(item[0].decode('utf-8')), seq


def _until(predicate, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class TheTowerWorkerSurvivesAFailingCamera(BoundedTestCase):
    """K-1: a read that raised ended the capture thread silently, and the state
    sender's once-a-second resend kept telling the robot ``streaming`` at
    30 fps; a read that blocked froze the state the same way. Now a raising
    read is a read that returned nothing (``no_frames`` after the verifier's
    silence, then the device is reopened), and the worker judges the camera
    and refreshes its state on a timer of its own while a read blocks."""

    def setUp(self):
        self.clock = _StepClock()
        self.frame = np.frombuffer(_FIXTURE.read_bytes(), np.uint8).reshape(1, -1)
        self.raising, self.blocking, self.unblock = threading.Event(), threading.Event(), threading.Event()
        self.cams = []

        def open_scene_capture(index):
            cam = _FailingCam(self.clock, self.frame, self.raising, self.blocking, self.unblock)
            self.cams.append(cam)
            return cam

        for patcher in (mock.patch.object(camera_bridge.win_camera, 'open_scene_capture',
                                          side_effect=open_scene_capture),
                        mock.patch.object(camera_bridge.scene_split, 'selftest', return_value='')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.worker = camera_bridge._SceneCaptureWorker(1, 'scene', 4, clock=self.clock)
        self.worker.start()
        self.addCleanup(self._stop)
        self.assertTrue(_until(lambda: self.worker.state_slot.latest()[0] is not None
                               and _state_of(self.worker)[0]['state'] == 'streaming', 10.0))

    def _stop(self):
        self.unblock.set()
        self.worker.stop()
        self.worker.join(5.0)

    def _assert_no_frames(self):
        worker = self.worker
        self.assertTrue(worker.is_alive(), 'the capture thread ended')
        self.assertEqual((worker.verifier.state, worker.verifier.fault),
                         (scene_split.STATE_FAULT, scene_split.FAULT_NO_FRAMES))
        self.assertEqual(worker.last_error_de, camera_bridge.SCENE_NO_FRAMES_DE)
        state, _seq = _state_of(worker)
        self.assertEqual((state['state'], state['fault'], state['fps']), ('fault', 'no_frames', None))

    def test_a_read_that_raises_is_a_failed_read_and_the_camera_is_reopened(self):
        self.raising.set()
        self.assertTrue(_until(lambda: _state_of(self.worker)[0]['fault'] == 'no_frames', 10.0),
                        f'the state stayed {_state_of(self.worker)[0]}')
        self._assert_no_frames()
        self.assertTrue(self.cams[0].released, 'the device stays open after the fault')
        # The camera works again: once the fault's back-off has passed, it is opened again and streams.
        self.raising.clear()
        self.clock.advance(scene_split.RETRY_FIRST_S)
        self.assertTrue(_until(lambda: len(self.cams) == 2
                               and _state_of(self.worker)[0]['state'] == 'streaming', 10.0))
        self.assertEqual(self.worker.last_error_de, '')
        self.assertTrue(self.worker.is_alive())

    def test_a_read_that_blocks_turns_the_state_to_no_frames_and_keeps_it_fresh(self):
        self.blocking.set()
        time.sleep(0.2)                         # the worker now waits in read()
        self.clock.advance(scene_split.SILENCE_S + 0.1)
        self.assertTrue(_until(lambda: _state_of(self.worker)[0]['fault'] == 'no_frames', 3.0),
                        f'the state stayed {_state_of(self.worker)[0]}')
        self._assert_no_frames()
        # The worker itself refreshes the state while the read still blocks (the sender only re-sends it).
        _state, seq = _state_of(self.worker)
        self.clock.advance(camera_bridge.STATE_RESEND_S)
        self.assertTrue(_until(lambda: _state_of(self.worker)[1] > seq, 3.0))
        self.assertEqual(_state_of(self.worker)[0]['fault'], 'no_frames')
        # The read returns: the device is closed, as after any fault.
        self.blocking.clear()
        self.unblock.set()
        self.assertTrue(_until(lambda: self.cams[0].released, 5.0))


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class AnUnexpectedEndLeavesNoStreamingState(BoundedTestCase):
    """Anything else that ends the capture loop leaves the camera off until the
    bridge starts again: the state it leaves behind (which the state sender
    re-sends) is ``closed``, not ``streaming``, and the GUI says so."""

    def test_the_state_is_closed_and_the_gui_names_it(self):
        clock = _StepClock()
        frame = np.frombuffer(_FIXTURE.read_bytes(), np.uint8).reshape(1, -1)
        never = threading.Event()
        cam = _FailingCam(clock, frame, never, never, never)
        worker = camera_bridge._SceneCaptureWorker(1, 'scene', 4, clock=clock)
        handed = []

        def hand_over(now, frame, capture_ns):
            handed.append(now)
            if len(handed) == 5:
                raise MemoryError('the split ran out of memory')

        with mock.patch.object(camera_bridge.win_camera, 'open_scene_capture', return_value=cam), \
                mock.patch.object(camera_bridge.scene_split, 'selftest', return_value=''), \
                mock.patch.object(worker, '_hand_over', side_effect=hand_over):
            with self.assertRaises(MemoryError):
                worker.run()
        state, _seq = _state_of(worker)
        self.assertEqual((state['state'], state['fault']), ('closed', ''))
        self.assertEqual(worker.last_error_de, camera_bridge.SCENE_STOPPED_DE)
        self.assertEqual(camera_bridge.SCENE_STOPPED_DE,
                         'Die Turm-Kamera ist ausgefallen — bitte die Umgebung neu starten.')
        self.assertTrue(cam.released)


if __name__ == "__main__":
    unittest.main()
