"""Tests for the native camera bridge (camera_bridge.py).

Platform-agnostic: cv2 is imported lazily inside the capture/sender loops, so
constructing a CameraBridge (without start()) needs no OpenCV. The wire-format
contract with the container's camera_ingest_node.py is asserted against a
hardcoded struct so a drift on either side fails the test.
"""

import ast
import inspect
import struct
import threading
import time
import unittest

from gui.app import camera_bridge, constants


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
        self.assertTrue(all(s._stop_event is bridge._stop for s in bridge._senders))

    def test_connected_false_before_start(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0})
        self.assertFalse(bridge.connected)


class TestStopEventIsNotCalledStop(unittest.TestCase):
    """`_stop` on a Thread subclass shadows `Thread._stop()`, which
    `Thread.join()` calls on CPython 3.11/3.12: join() then raised
    `TypeError: 'Event' object is not callable` and CameraBridge.stop() aborted
    at the first finished thread (skipping the camera joins and
    timeEndPeriod). The AST half has teeth on every Python; the join half on
    the 3.11/3.12 the installer and CI run."""

    def _thread_classes(self):
        with open(camera_bridge.__file__, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        thread_like = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = {ast.unparse(b) for b in node.bases}
                if "threading.Thread" in bases or "_ConnectMixin" in bases \
                        or node.name == "_ConnectMixin":
                    thread_like.append(node)
        return thread_like

    def test_no_thread_class_names_an_attribute_stop(self):
        classes = self._thread_classes()
        self.assertGreaterEqual(len(classes), 4, [c.name for c in classes])
        for cls in classes:
            for node in ast.walk(cls):
                if (isinstance(node, ast.Attribute) and node.attr == "_stop"
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "self"):
                    self.fail(f"{cls.name} uses self._stop (line {node.lineno})")

    def test_finished_worker_threads_join_without_raising(self):
        worker = camera_bridge._CaptureWorker(1, "scene", 0)
        worker.stop()            # loop exits before touching cv2
        worker.start()
        worker.join(timeout=2.0)  # TypeError here on 3.11/3.12 with `_stop`
        self.assertFalse(worker.is_alive())

        stop = threading.Event()
        stop.set()
        ext = camera_bridge._ExternalSenderWorker(None, 2, "127.0.0.1", 1, stop)
        ext.start()
        ext.join(timeout=2.0)
        self.assertFalse(ext.is_alive())

    def test_bridge_stop_completes_after_threads_finished(self):
        bridge = camera_bridge.CameraBridge({"scene": 0})
        bridge.register_external_source(2, None)
        bridge._stop.set()
        bridge.workers[0].stop()
        # Start only the threads whose loop exits without cv2.
        bridge.workers[0].start()
        bridge._external_senders[0].start()
        bridge._timer_raised = True   # stop() must reach the timer reset
        bridge.stop()
        self.assertFalse(bridge._timer_raised)


class TestSharedStopDeadline(unittest.TestCase):
    """stop() runs on the Tk thread (`_on_close`): every join shares ONE
    deadline of STOP_DEADLINE_S (2 s), never 3 s per thread."""

    class _HungThread:
        def __init__(self, log):
            self.ident = 1
            self._log = log

        def stop(self):
            pass

        def join(self, timeout=None):
            self._log.append(timeout)
            time.sleep(timeout)

    def test_the_deadline_constant_is_two_seconds(self):
        self.assertEqual(camera_bridge.STOP_DEADLINE_S, 2.0)
        self.assertEqual(
            inspect.signature(camera_bridge.CameraBridge.stop)
            .parameters["join_timeout"].default, 2.0)

    def test_five_hung_threads_block_at_most_the_deadline(self):
        bridge = camera_bridge.CameraBridge({})
        log = []
        hung = [self._HungThread(log) for _ in range(5)]
        bridge.workers = hung[:2]
        bridge._senders = hung[2:4]
        bridge._external_senders = hung[4:]
        t0 = time.monotonic()
        bridge.stop(join_timeout=0.5)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(log), 5, "every thread is still joined")
        self.assertLess(elapsed, 0.5 + 0.25)
        self.assertLessEqual(sum(log), 0.5 + 0.05)
        # The first join got (almost) the whole budget, the rest what is left.
        self.assertGreater(log[0], 0.4)
        self.assertTrue(all(t >= 0.0 for t in log))


class TestConstants(unittest.TestCase):

    def test_bridge_roles_match_default_camera_names(self):
        # The container default EDUBOTICS_CAMERA_NAMES is "gripper,scene"; the
        # GUI's role->cam_id order must match.
        self.assertEqual(constants.CAMERA_BRIDGE_ROLES, ("gripper", "scene"))


if __name__ == "__main__":
    unittest.main()
