"""Tests for the native camera bridge (camera_bridge.py).

Platform-agnostic: cv2 is imported lazily inside the capture/sender loops, so
constructing a CameraBridge (without start()) needs no OpenCV. The wire-format
contract with the container's camera_ingest_node.py is asserted against a
hardcoded struct so a drift on either side fails the test.
"""

import struct
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
        self.assertTrue(all(s._stop is bridge._stop for s in bridge._senders))

    def test_connected_false_before_start(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0})
        self.assertFalse(bridge.connected)


class TestConstants(unittest.TestCase):

    def test_bridge_roles_match_default_camera_names(self):
        # The container default EDUBOTICS_CAMERA_NAMES is "gripper,scene"; the
        # GUI's role->cam_id order must match.
        self.assertEqual(constants.CAMERA_BRIDGE_ROLES, ("gripper", "scene"))


if __name__ == "__main__":
    unittest.main()
