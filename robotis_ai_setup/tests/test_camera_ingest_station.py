"""camera_ingest_node.py and the camera station's cam_id 200 (plan §4).

The node imports rclpy at module top, which this suite never has, so it is
loaded against tiny stand-ins for rclpy / sensor_msgs: everything from the TCP
socket to `publisher.publish()` is the shipped node. The real-rclpy proof is
a smoke run inside the arm image (see the WP-A report); the compose keys and
the flag parser are checked here too.
"""

import importlib.util
import os
import re
import socket
import struct
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from gui.app import camera_bridge, config_generator, constants, win_camera

try:
    import cv2
    import numpy as np
except Exception:  # noqa: BLE001
    cv2 = None
    np = None

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_NODE = os.path.join(_ROOT, "robotis_ai_setup", "docker", "open_manipulator",
                     "camera_ingest_node.py")
_COMPOSES = [
    os.path.join(_ROOT, "robotis_ai_setup", "docker", "docker-compose.yml"),
    os.path.join(_ROOT, "robotis_ai_setup", "docker", "docker-compose.opi.yml"),
    os.path.join(_ROOT, "robotis_ai_setup", "pi_agent", "docker",
                 "docker-compose.opi.yml"),
]
_SERVER_CONFIG = os.path.join(_ROOT, "physical_ai_tools", "physical_ai_server",
                              "config")
_HEADER = struct.Struct(">BIQ")


# ── stand-ins for rclpy and sensor_msgs ─────────────────────────────────────
class _Pub:
    def __init__(self, topic, qos):
        self.topic = topic
        self.qos = qos
        self.msgs = []

    def publish(self, msg):
        self.msgs.append(msg)


class _Logger:
    def __init__(self):
        self.warns, self.infos = [], []

    def warn(self, m):
        self.warns.append(m)

    def info(self, m):
        self.infos.append(m)


class _Node:
    def __init__(self, name):
        self.node_name = name
        self.pubs = {}
        self._logger = _Logger()

    def create_publisher(self, _type, topic, qos):
        pub = _Pub(topic, qos)
        self.pubs[topic] = pub
        return pub

    def get_logger(self):
        return self._logger

    def create_timer(self, *_a, **_k):
        return None

    def get_clock(self):
        stamp = types.SimpleNamespace(sec=1, nanosec=2)
        return types.SimpleNamespace(
            now=lambda: types.SimpleNamespace(to_msg=lambda: stamp))

    def destroy_node(self):
        pass


class _Stamp:
    def __init__(self):
        self.sec = 0
        self.nanosec = 0


class _CompressedImage:
    def __init__(self):
        self.header = types.SimpleNamespace(stamp=_Stamp(), frame_id="")
        self.format = ""
        self.data = b""


def _load_node_module():
    rclpy = types.ModuleType("rclpy")
    rclpy.init = lambda *a, **k: None
    rclpy.ok = lambda: False
    node_mod = types.ModuleType("rclpy.node")
    node_mod.Node = _Node
    qos_mod = types.ModuleType("rclpy.qos")
    qos_mod.HistoryPolicy = types.SimpleNamespace(KEEP_LAST="KEEP_LAST")
    qos_mod.ReliabilityPolicy = types.SimpleNamespace(RELIABLE="RELIABLE")
    qos_mod.QoSProfile = lambda **kw: dict(kw)
    msgs = types.ModuleType("sensor_msgs.msg")
    msgs.CompressedImage = _CompressedImage
    sensor = types.ModuleType("sensor_msgs")
    sensor.msg = msgs
    fakes = {"rclpy": rclpy, "rclpy.node": node_mod, "rclpy.qos": qos_mod,
             "sensor_msgs": sensor, "sensor_msgs.msg": msgs}
    with patch.dict(sys.modules, fakes):
        spec = importlib.util.spec_from_file_location("camera_ingest_t", _NODE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


_M = _load_node_module()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _NodeRig:
    def __init__(self, station, port=None):
        self.port = port or _free_port()
        env = {"EDUBOTICS_CAMERA_INGEST_PORT": str(self.port),
               "EDUBOTICS_CAMERA_NAMES": "gripper,scene"}
        if station is not None:
            env["EDUBOTICS_STATION_ENABLED"] = station
        with patch.dict(os.environ, env):
            if station is None:
                os.environ.pop("EDUBOTICS_STATION_ENABLED", None)
            self.node = _M.CameraIngestNode()

    def close(self):
        self.node.destroy_node()
        self.node._server_thread.join(3.0)


class TheStationTopicIsRegisteredOnlyWithTheFlag(unittest.TestCase):

    def test_with_the_flag_cam_200_publishes_the_station_topic(self):
        rig = _NodeRig("1")
        try:
            node = rig.node
            self.assertTrue(node.station_enabled)
            self.assertIn(200, node.publishers_by_id)
            name, pub = node.publishers_by_id[200]
            self.assertEqual(name, "station_scene")
            self.assertEqual(pub.topic, "/station_scene/image_raw/compressed")
            # The same RELIABLE KEEP_LAST 5 QoS as the camera topics.
            self.assertEqual(pub.qos, node.publishers_by_id[1][1].qos)
            self.assertEqual(pub.qos, {"depth": 5, "reliability": "RELIABLE",
                                       "history": "KEEP_LAST"})
            node._publish(200, b"\xff\xd8jpeg", 1_700_000_000_123_456_789)
            msg = pub.msgs[-1]
            self.assertEqual(msg.header.frame_id, "station_scene")
            self.assertEqual(msg.format, "jpeg")
            self.assertEqual(msg.data, b"\xff\xd8jpeg")
            self.assertEqual((msg.header.stamp.sec, msg.header.stamp.nanosec),
                             (1_700_000_000, 123_456_789))
            # The scene topic is untouched by it.
            self.assertEqual(node.publishers_by_id[1][1].msgs, [])
        finally:
            rig.close()

    def test_without_the_flag_cam_200_is_dropped_with_one_warning(self):
        for value in (None, "0", "", "true", "01"):
            with self.subTest(value=value):
                rig = _NodeRig(value)
                try:
                    node = rig.node
                    self.assertFalse(node.station_enabled)
                    self.assertNotIn(200, node.publishers_by_id)
                    self.assertNotIn("/station_scene/image_raw/compressed",
                                     node.pubs)
                    for _ in range(30):     # 2 s at the station's 15 fps
                        node._publish(200, b"x", 1)
                    drops = [w for w in node.get_logger().warns
                             if "unknown cam_id=200" in w]
                    self.assertEqual(len(drops), 1, "rate limited to one")
                finally:
                    rig.close()

    def test_the_warning_returns_after_ten_seconds_per_id(self):
        rig = _NodeRig("0")
        try:
            node = rig.node
            self.assertTrue(node._unknown_cam_warn_due(200, now=100.0))
            self.assertFalse(node._unknown_cam_warn_due(200, now=109.9))
            self.assertTrue(node._unknown_cam_warn_due(7, now=105.0),
                            "per id")
            self.assertTrue(node._unknown_cam_warn_due(200, now=110.0))
            self.assertEqual(_M._UNKNOWN_CAM_WARN_PERIOD_S, 10.0)
        finally:
            rig.close()

    def test_the_8_mb_cap_already_covers_1080p(self):
        self.assertEqual(_M._MAX_JPEG_BYTES, 8 * 1024 * 1024)


class TheWire(unittest.TestCase):
    """Real TCP into the node's own server and demux."""

    def _send(self, port, frames):
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            for cam, payload, ns in frames:
                sock.sendall(_HEADER.pack(cam, len(payload), ns) + payload)
        finally:
            time.sleep(0.2)
            sock.close()

    def _wait_for(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            time.sleep(0.02)
        return pred()

    def test_a_cam_200_frame_over_tcp_reaches_the_station_topic(self):
        rig = _NodeRig("1")
        try:
            time.sleep(0.2)
            self._send(rig.port, [(1, b"scene", 11), (200, b"native", 22)])
            station = rig.node.publishers_by_id[200][1]
            scene = rig.node.publishers_by_id[1][1]
            self.assertTrue(self._wait_for(lambda: station.msgs and scene.msgs))
            self.assertEqual(station.msgs[0].header.frame_id, "station_scene")
            self.assertEqual(station.msgs[0].data, b"native")
            self.assertEqual(scene.msgs[0].header.frame_id, "scene")
        finally:
            rig.close()

    @unittest.skipIf(cv2 is None, "OpenCV + numpy required")
    def test_the_gui_bridge_end_to_end_into_the_node(self):
        """The shipped CameraBridge (fake camera) into the shipped node."""
        rig = _NodeRig("1")

        class _Cap:
            def __init__(self):
                self.n = 0

            def read(self):
                time.sleep(1 / 60.0)
                self.n += 1
                return True, np.full((1080, 1920, 3), self.n % 250, np.uint8)

            def release(self):
                pass
        cap = _Cap()
        bridge = None
        try:
            with patch.object(win_camera, "open_station_capture",
                              return_value=(cap, {"fps": 30.0})):
                bridge = camera_bridge.CameraBridge(
                    {"scene": 3}, host="127.0.0.1", port=rig.port, station=True)
                bridge.start()
                scene = rig.node.publishers_by_id[1][1]
                station = rig.node.publishers_by_id[200][1]
                self.assertTrue(self._wait_for(lambda: len(scene.msgs) > 3))
                self.assertEqual(station.msgs, [])
                bridge.set_station_stream(True)
                self.assertTrue(self._wait_for(lambda: len(station.msgs) > 3))
                img = cv2.imdecode(np.frombuffer(station.msgs[-1].data, np.uint8),
                                   cv2.IMREAD_COLOR)
                rec = cv2.imdecode(np.frombuffer(scene.msgs[-1].data, np.uint8),
                                   cv2.IMREAD_COLOR)
                self.assertEqual(img.shape, (1080, 1920, 3))
                self.assertEqual(rec.shape, (480, 640, 3))
                self.assertEqual(station.msgs[-1].header.frame_id, "station_scene")
                self.assertEqual(scene.msgs[-1].header.frame_id, "scene")
        finally:
            if bridge is not None:
                bridge.stop()
            rig.close()


class OneParsingRuleForTheFlag(unittest.TestCase):
    """The GUI's .env read-back and the ingest node parse the flag the same
    way (whitespace stripped, exactly `1` is on). The server's reader joins
    this table in the cross-package lockstep test after the merge."""

    VALUES = [(None, False), ("", False), ("1", True), (" 1 ", True),
              ("1\n", True), ("\t1\r\n", True), ("\v1\f", True),
              ("0", False), ("true", False), ("yes", False), ("01", False),
              ("11", False), ("1 1", False), ("on", False), (" 1", False),
              ("2", False)]

    def test_gui_and_ingest_node_agree_on_every_value(self):
        for value, on in self.VALUES:
            with self.subTest(value=value):
                self.assertIs(config_generator.station_flag_on(value), on)
                self.assertIs(_M.station_flag_on(value), on)

    def test_both_follow_the_shell_tr_rule(self):
        # tr -d '[:space:]' in the C locale deletes exactly these six.
        for fn in (config_generator.station_flag_on, _M.station_flag_on):
            self.assertTrue(fn(" \t\n\v\f\r1 \t\n\v\f\r"))


class TheContractWithTheGui(unittest.TestCase):

    def test_the_cam_id_is_spelled_the_same_on_both_sides(self):
        self.assertEqual(_M.STATION_SCENE_CAM_ID, 200)
        self.assertEqual(camera_bridge.STATION_SCENE_CAM_ID,
                         _M.STATION_SCENE_CAM_ID)
        with open(_NODE, encoding="utf-8") as fh:
            src = fh.read()
        self.assertRegex(src, r"(?m)^STATION_SCENE_CAM_ID = 200$")
        with open(camera_bridge.__file__, encoding="utf-8") as fh:
            self.assertRegex(fh.read(), r"(?m)^STATION_SCENE_CAM_ID = 200$")
        self.assertEqual(_M.STATION_SCENE_TOPIC,
                         "/station_scene/image_raw/compressed")
        self.assertEqual(_M.STATION_SCENE_FRAME_ID, "station_scene")

    def test_the_header_struct_matches(self):
        self.assertEqual(_M._HEADER.format, camera_bridge._HEADER.format)

    def test_the_station_topic_is_in_no_camera_topic_list(self):
        for name in sorted(os.listdir(_SERVER_CONFIG)):
            if name.endswith(".yaml"):
                with self.subTest(name), open(
                        os.path.join(_SERVER_CONFIG, name), encoding="utf-8") as fh:
                    self.assertNotIn("station_scene", fh.read())


class TheComposeForwardsTheFlag(unittest.TestCase):
    """Both services read it at start, on every target compose (plan §10)."""

    def _service_env(self, path, service):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(rf"(?ms)^  {service}:\n(.*?)(?=^  [a-z_]+:\n|\Z)", text)
        self.assertIsNotNone(m, f"{service} not in {path}")
        return m.group(1)

    def test_both_services_on_all_three_files(self):
        line = "      - EDUBOTICS_STATION_ENABLED=${EDUBOTICS_STATION_ENABLED:-0}"
        for path in _COMPOSES:
            for service in ("open_manipulator", "physical_ai_server"):
                with self.subTest(path=os.path.relpath(path, _ROOT),
                                  service=service):
                    body = self._service_env(path, service)
                    self.assertEqual(body.count(line), 1)

    def test_the_pi_twin_is_byte_equal(self):
        with open(_COMPOSES[1], "rb") as a, open(_COMPOSES[2], "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_the_manager_and_runner_do_not_get_it(self):
        for path in _COMPOSES[:2]:
            for service in ("physical_ai_manager", "code_runner"):
                with self.subTest(path=os.path.relpath(path, _ROOT),
                                  service=service):
                    self.assertNotIn("EDUBOTICS_STATION_ENABLED",
                                     self._service_env(path, service))


if __name__ == "__main__":
    unittest.main()
