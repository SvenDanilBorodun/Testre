# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""D21 (b): the camera ingest node warns about an unknown camera id once, not
once per frame.

A GUI that streams a camera id the container has no name for (``cam_id``
indexes ``EDUBOTICS_CAMERA_NAMES``) used to log one warning per dropped frame,
about 30 a second into ``docker logs open_manipulator``. The node now warns on
the first frame of an id and then at most once per 10 s per id, naming how many
frames it dropped since. Ids 0, 1 and 2 publish exactly as before.

``camera_ingest_node.py`` imports rclpy at module top, so it is loaded here
under ``ros_stubs``' fake ROS modules; its TCP server is replaced by a no-op so
no socket is opened.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

import ros_stubs

_HERE = os.path.dirname(os.path.abspath(__file__))
_NODE = os.path.join(_HERE, '..', 'docker', 'open_manipulator', 'camera_ingest_node.py')

_JPEG = b'\xff\xd8\xff\xe0 not really a jpeg \xff\xd9'


def _load_node_module():
    with mock.patch.dict(sys.modules, ros_stubs.modules()):
        spec = importlib.util.spec_from_file_location('camera_ingest_node_d21', _NODE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


_MOD = _load_node_module()


def _make_node(names='gripper,scene'):
    env = {'EDUBOTICS_CAMERA_NAMES': names}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(_MOD.CameraIngestNode, '_serve_forever', lambda self: None):
        node = _MOD.CameraIngestNode()
    node._server_thread.join(5.0)
    return node


class UnknownCameraIdWarnsOnce(unittest.TestCase):

    def test_100_frames_of_an_unknown_id_log_exactly_one_warning(self):
        node = _make_node()
        for _ in range(100):
            node._publish(9, _JPEG, 1_700_000_000_000_000_000)
        warnings = node.get_logger().lines('warn')
        self.assertEqual(len(warnings), 1, warnings[:3])
        self.assertIn('cam_id=9', warnings[0])
        self.assertEqual([p.published for p in node.publishers], [[], [], [], []])

    def test_the_warning_repeats_after_10_s_with_the_count_and_per_id(self):
        node = _make_node()
        clock = [100.0]
        fake_time = types.SimpleNamespace(monotonic=lambda: clock[0])
        with mock.patch.object(_MOD, 'time', fake_time, create=True):
            node._publish(9, _JPEG)                      # the first frame: warned
            for i in range(99):                          # 9.9 s of drops: silent
                clock[0] = 100.0 + i * 0.1
                node._publish(9, _JPEG)
            clock[0] = 110.0                             # 10 s after the warning
            node._publish(9, _JPEG)
            clock[0] = 115.0
            node._publish(9, _JPEG)                      # inside the next 10 s
            node._publish(7, _JPEG)                      # another id: its own first warning
        warnings = node.get_logger().lines('warn')
        self.assertEqual(len(warnings), 3, warnings[:5])
        self.assertIn('cam_id=9', warnings[0])
        self.assertIn('cam_id=9', warnings[1])
        self.assertIn('100', warnings[1])                # 99 silent drops + the one at 10 s
        self.assertIn('cam_id=7', warnings[2])
        self.assertEqual([p.published for p in node.publishers], [[], [], [], []])


class KnownCameraIdsPublishAsBefore(unittest.TestCase):

    def test_ids_0_1_2_publish_their_frame_on_their_topic(self):
        node = _make_node('gripper,scene,phone')
        self.assertEqual(
            [p.topic for p in node.publishers],
            ['/gripper/image_raw/compressed', '/scene/image_raw/compressed',
             '/phone/image_raw/compressed',
             # WP-05: the tower camera's studio and state topics, silent here.
             '/studio/tower/compressed', '/edubotics/scene_camera_state'])
        for pub in node.publishers[:3]:
            self.assertEqual(pub.msg_type.__name__, 'CompressedImage')
            self.assertEqual(pub.qos.depth, 5)
            self.assertEqual(pub.qos.reliability, 'reliable')
            self.assertEqual(pub.qos.history, 'keep_last')
        capture_ns = 1_700_000_000_123_456_789
        for cam_id in (0, 1, 2):
            node._publish(cam_id, _JPEG + bytes([cam_id]), capture_ns + cam_id)
        node._publish(1, _JPEG, 0)                       # no capture time: the node's clock
        for cam_id, (pub, name) in enumerate(zip(node.publishers, ('gripper', 'scene', 'phone'))):
            msg = pub.published[0]
            self.assertEqual(msg.header.frame_id, name)
            self.assertEqual(msg.format, 'jpeg')
            self.assertEqual(msg.data, _JPEG + bytes([cam_id]))
            self.assertEqual(msg.header.stamp.sec, 1_700_000_000)
            self.assertEqual(msg.header.stamp.nanosec, 123_456_789 + cam_id)
        clock_msg = node.publishers[1].published[1]
        self.assertEqual((clock_msg.header.stamp.sec, clock_msg.header.stamp.nanosec),
                         (ros_stubs.FakeClock.NOW_SEC, ros_stubs.FakeClock.NOW_NANOSEC))
        self.assertEqual([len(p.published) for p in node.publishers], [1, 2, 1, 0, 0])
        self.assertEqual(node._frame_counts, {0: 1, 1: 2, 2: 1})
        self.assertEqual(node.get_logger().lines('warn'), [])


if __name__ == '__main__':
    unittest.main()
