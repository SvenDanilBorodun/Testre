# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-05: the camera ingest node's hello, its studio and state topics and the
tower profile's size check on camera id 1 (spec 40 S1, guards G7b and G8).

The Windows GUI's tower path adds three camera ids to the ingest wire
(``scene_split``): 255 is a hello that the node answers with the one byte
``HELLO`` on the same connection (an older image answers nothing, which is how
the GUI tells it is older), 16 is the tower camera's own picture for Roboter
Studio (``STUDIO_TOPIC``), 17 the camera's JSON state (``STATE_TOPIC``,
latched). With ``EDUBOTICS_SCENE_CAMERA_PROFILE=tower_v1`` the recorded scene
picture on camera id 1 must be exactly 640x480; ``legacy`` (and every other
value) publishes camera id 1 as before. Ids 0, 1 and 2 keep their wire and
their topics; a connection that sends no hello is never written to.

``camera_ingest_node.py`` imports rclpy at module top, so it is loaded under
``ros_stubs``' fake ROS modules with its TCP accept loop replaced by a no-op;
the hello tests drive the node's real per-connection handler over a loopback
TCP pair. G8 reads the composes' ``open_manipulator`` healthchecks as text.
"""

import importlib.util
import os
import re
import socket
import struct
import sys
import threading
import unittest
from unittest import mock

import ros_stubs
from timeout_guard import BoundedTestCase

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
_ARM_DIR = os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'open_manipulator')
_NODE = os.path.join(_ARM_DIR, 'camera_ingest_node.py')
_SPLIT_FILE = os.path.join(_ARM_DIR, 'scene_split.py')
_DOCKERFILE = os.path.join(_ARM_DIR, 'Dockerfile')
_PARITY_SH = os.path.join(_REPO, '.github', 'scripts', 'image_source_parity.sh')
_COMPOSES = (
    os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'docker-compose.yml'),
    os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'docker-compose.opi.yml'),
    os.path.join(_REPO, 'robotis_ai_setup', 'pi_agent', 'docker', 'docker-compose.opi.yml'),
)

# The wire header, spelled independently of the node: cam_id (u8),
# payload length (u32 BE), capture time in unix ns (u64 BE).
_WIRE = struct.Struct('>BIQ')
_CAPTURE_NS = 1_700_000_000_123_456_789
_OLD_STUDIO_NAME = '/studio/scene/image_raw/compressed'   # G8's mutation (spec 40 S1)


def _load_split(name):
    spec = importlib.util.spec_from_file_location(name, _SPLIT_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SPLIT = _load_split('scene_split_for_ingest_tests')


def _load_node_module(name, split=None):
    """The node module under the fake ROS. Without ``split`` the node imports
    its sibling ``scene_split.py`` itself, exactly as it does from
    /usr/local/bin in the image; with ``split`` that module stands in for it."""
    with mock.patch.dict(sys.modules, ros_stubs.modules()):
        sys.modules.pop('scene_split', None)
        if split is not None:
            sys.modules['scene_split'] = split
        spec = importlib.util.spec_from_file_location(name, _NODE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


_MOD = _load_node_module('camera_ingest_node_studio')


def _make_node(profile=None, names='gripper,scene,phone', module=None):
    """A node with ``EDUBOTICS_SCENE_CAMERA_PROFILE`` set to ``profile``
    (``None``: unset) and no TCP server."""
    module = module or _MOD
    with mock.patch.dict(os.environ, {'EDUBOTICS_CAMERA_NAMES': names}):
        os.environ.pop('EDUBOTICS_SCENE_CAMERA_PROFILE', None)
        if profile is not None:
            os.environ['EDUBOTICS_SCENE_CAMERA_PROFILE'] = profile
        with mock.patch.object(module.CameraIngestNode, '_serve_forever', lambda self: None):
            node = module.CameraIngestNode()
    node._server_thread.join(5.0)
    return node


def _jpeg(width, height, body=b'\x12\x34\x56\x78'):
    """A baseline JPEG header naming ``width`` x ``height`` (SOF0, three
    components, one scan), a stand-in entropy body and the EOI. No pixel
    decodes from it; the node reads only the header."""
    sof0 = (b'\xff\xc0' + struct.pack('>HBHHB', 17, 8, height, width, 3)
            + bytes((1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1)))
    sos = (b'\xff\xda' + struct.pack('>HB', 12, 3)
           + bytes((1, 0x00, 2, 0x11, 3, 0x11, 0x00, 0x3f, 0x00)))
    return b'\xff\xd8' + sof0 + sos + body + b'\xff\xd9'


def _frame(cam_id, payload, capture_ns=_CAPTURE_NS):
    return _WIRE.pack(cam_id, len(payload), capture_ns) + payload


def _publisher(node, topic):
    found = [p for p in node.publishers if p.topic == topic]
    if len(found) != 1:
        raise AssertionError(f'{len(found)} publishers on {topic}; the node has '
                             f'{[p.topic for p in node.publishers]}')
    return found[0]


def _published(node):
    return {p.topic: list(p.published) for p in node.publishers}


def _tower_state():
    return _SPLIT.state_message(
        profile='tower_v1', state='streaming', width=1920, height=1080, fps=29.9,
        fps_low=False, passthrough=True, outside='640x480',
        profile_id=_SPLIT.profile_id(80), platform='windows', fault='', dropped=0)


class _LoopbackClient:
    """One TCP connection to the node's real per-connection handler (the
    accept loop is not started): the test is the GUI's sender."""

    def __init__(self, node):
        listener = socket.create_server(('127.0.0.1', 0))
        try:
            self.sock = socket.create_connection(listener.getsockname(), timeout=5.0)
            conn, addr = listener.accept()
        finally:
            listener.close()
        self.handler = threading.Thread(
            target=node._client_handler, args=(conn, addr), daemon=True)
        self.handler.start()

    def send(self, data):
        self.sock.sendall(data)

    def recv_exactly(self, n, timeout_s=2.0):
        """``n`` bytes, or fewer when the node answered less in time."""
        self.sock.settimeout(timeout_s)
        data = b''
        try:
            while len(data) < n:
                chunk = self.sock.recv(n - len(data))
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        return data

    def finish(self):
        """Close the sending side, wait for the node to end the connection and
        return every byte it wrote after what the test already read."""
        self.sock.shutdown(socket.SHUT_WR)
        self.sock.settimeout(5.0)
        rest = b''
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            rest += chunk
        self.sock.close()
        self.handler.join(5.0)
        if self.handler.is_alive():
            raise AssertionError('the node did not end the connection after EOF')
        return rest


class HelloIsAnsweredOnTheSameConnection(BoundedTestCase):
    """G7's server half: only a hello is ever answered, with exactly ``HELLO``."""

    TEST_TIMEOUT_S = 30.0

    def test_a_hello_is_answered_with_exactly_one_byte_and_published_nowhere(self):
        node = _make_node('tower_v1')
        client = _LoopbackClient(node)
        client.send(_frame(_SPLIT.HELLO_CAM_ID, _SPLIT.HELLO, 0))
        self.assertEqual(client.recv_exactly(1), b'\x01',
                         'no answer to the hello on its connection')
        self.assertEqual(client.finish(), b'', 'the node wrote more than the one-byte answer')
        self.assertEqual(_SPLIT.HELLO, b'\x01')
        self.assertTrue(all(not msgs for msgs in _published(node).values()), _published(node))
        self.assertEqual(node.get_logger().lines('warn'), [],
                         'a hello is not an unknown camera id')

    def test_the_connection_stays_usable_after_the_hello(self):
        node = _make_node('tower_v1')
        client = _LoopbackClient(node)
        client.send(_frame(_SPLIT.HELLO_CAM_ID, _SPLIT.HELLO, 0))
        self.assertEqual(client.recv_exactly(1), _SPLIT.HELLO)
        studio = _jpeg(1920, 1080)
        client.send(_frame(_SPLIT.STUDIO_CAM_ID, studio))
        self.assertEqual(client.finish(), b'')
        msgs = _publisher(node, _SPLIT.STUDIO_TOPIC).published
        self.assertEqual([m.data for m in msgs], [studio])

    def test_a_client_that_sends_no_hello_receives_nothing(self):
        # A legacy connection (ids 0, 1, 2) is never written to: frames in,
        # nothing out, published as before.
        for profile in (None, 'tower_v1'):
            with self.subTest(profile=profile):
                node = _make_node(profile)
                client = _LoopbackClient(node)
                client.send(_frame(0, _jpeg(640, 480)) + _frame(1, _jpeg(640, 480))
                            + _frame(2, _jpeg(640, 480)))
                self.assertEqual(client.finish(), b'')
                counts = {topic: len(msgs) for topic, msgs in _published(node).items()}
                self.assertEqual(counts['/gripper/image_raw/compressed'], 1)
                self.assertEqual(counts['/scene/image_raw/compressed'], 1)
                self.assertEqual(counts['/phone/image_raw/compressed'], 1)


class StudioAndStateTopics(unittest.TestCase):

    def test_id_16_is_published_on_the_studio_topic_as_scene_studio(self):
        node = _make_node('tower_v1')
        pub = _publisher(node, _SPLIT.STUDIO_TOPIC)
        self.assertEqual(pub.msg_type.__name__, 'CompressedImage')
        self.assertEqual((pub.qos.depth, pub.qos.reliability, pub.qos.history),
                         (2, 'reliable', 'keep_last'))
        self.assertEqual(getattr(pub.qos, 'durability', 'volatile'), 'volatile')
        studio = _jpeg(1920, 1080)
        node._publish(_SPLIT.STUDIO_CAM_ID, studio, _CAPTURE_NS)
        node._publish(_SPLIT.STUDIO_CAM_ID, studio + b'\x00', 0)
        first, second = pub.published
        self.assertEqual(first.header.frame_id, 'scene_studio')
        self.assertEqual(first.format, 'jpeg')
        self.assertEqual(first.data, studio)
        self.assertEqual((first.header.stamp.sec, first.header.stamp.nanosec),
                         (1_700_000_000, 123_456_789))
        self.assertEqual((second.header.stamp.sec, second.header.stamp.nanosec),
                         (ros_stubs.FakeClock.NOW_SEC, ros_stubs.FakeClock.NOW_NANOSEC))
        self.assertEqual(second.header.frame_id, 'scene_studio')
        others = {t: m for t, m in _published(node).items() if t != _SPLIT.STUDIO_TOPIC}
        self.assertTrue(all(not m for m in others.values()), others)
        self.assertEqual(node.get_logger().lines('warn'), [])

    def test_the_studio_frame_is_not_size_checked_and_exists_in_legacy_too(self):
        node = _make_node('legacy')
        node._publish(_SPLIT.STUDIO_CAM_ID, b'not a jpeg at all', _CAPTURE_NS)
        self.assertEqual([m.data for m in _publisher(node, _SPLIT.STUDIO_TOPIC).published],
                         [b'not a jpeg at all'])

    def test_id_17_with_valid_json_is_published_on_the_latched_state_topic(self):
        node = _make_node('tower_v1')
        pub = _publisher(node, _SPLIT.STATE_TOPIC)
        self.assertEqual(pub.msg_type.__name__, 'String')
        self.assertEqual((pub.qos.depth, pub.qos.reliability, pub.qos.history, pub.qos.durability),
                         (1, 'reliable', 'keep_last', 'transient_local'))
        state = _tower_state()
        node._publish(_SPLIT.STATE_CAM_ID, state, _CAPTURE_NS)
        self.assertEqual([m.data for m in pub.published], [state.decode('utf-8')])
        others = {t: m for t, m in _published(node).items() if t != _SPLIT.STATE_TOPIC}
        self.assertTrue(all(not m for m in others.values()), others)

    def test_id_17_invalid_or_larger_than_4096_bytes_is_dropped(self):
        node = _make_node('tower_v1')
        pub = _publisher(node, _SPLIT.STATE_TOPIC)
        prefix, suffix = b'{"v":1,"pad":"', b'"}'
        fits = prefix + b'x' * (_SPLIT.STATE_MAX_BYTES - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(fits), 4096)
        too_big = prefix + b'x' * (_SPLIT.STATE_MAX_BYTES + 1 - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(too_big), 4097)
        dropped = {
            'one byte over the cap': too_big,
            'not UTF-8': b'{"v":1,"state":"\xff"}',
            'not JSON': b'{"v":1,',
            'empty': b' ',
            'NaN is not JSON': b'{"v":1,"fps":NaN}',
            'Infinity is not JSON': b'{"v":1,"fps":Infinity}',
            'nested past the parser': b'[' * 4000,
            'not an object': b'[1,2,3]',
            'a bare string': b'"streaming"',
        }
        for case, payload in dropped.items():
            node._publish(_SPLIT.STATE_CAM_ID, payload, _CAPTURE_NS)
            self.assertEqual(pub.published, [], case)
        node._publish(_SPLIT.STATE_CAM_ID, fits, _CAPTURE_NS)
        self.assertEqual([m.data for m in pub.published], [fits.decode('utf-8')])
        warnings = node.get_logger().lines('warn')
        self.assertEqual(len(warnings), 1, warnings)      # first drop, then throttled
        self.assertIn('cam_id=17', warnings[0])


class TowerProfileChecksTheSceneSize(unittest.TestCase):
    """G7b: in ``tower_v1`` the recorded scene picture is 640x480 or nothing."""

    def test_tower_drops_a_1280x960_frame_on_id_1_and_publishes_640x480(self):
        big, small = _jpeg(1280, 960), _jpeg(640, 480)
        self.assertEqual(_SPLIT.jpeg_size(big), (1280, 960))
        self.assertEqual(_SPLIT.jpeg_size(small), (640, 480))
        node = _make_node('tower_v1')
        scene = _publisher(node, '/scene/image_raw/compressed')
        node._publish(1, big, _CAPTURE_NS)
        self.assertEqual(scene.published, [])
        node._publish(1, small, _CAPTURE_NS)
        self.assertEqual([m.data for m in scene.published], [small])
        self.assertEqual(scene.published[0].header.frame_id, 'scene')
        self.assertEqual(node._frame_counts[1], 1)
        warnings = node.get_logger().lines('warn')
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn('cam_id=1', warnings[0])
        self.assertIn('1280x960', warnings[0])

    def test_tower_drops_a_frame_whose_size_cannot_be_read(self):
        node = _make_node('tower_v1')
        scene = _publisher(node, '/scene/image_raw/compressed')
        for payload in (b'\xff\xd8\xff\xe0 not really a jpeg \xff\xd9', b'\x00' * 64,
                        _jpeg(640, 480)[:20]):
            node._publish(1, payload, _CAPTURE_NS)
        self.assertEqual(scene.published, [])

    def test_the_profile_value_is_normalised(self):
        for raw in ('tower_v1', ' "Tower_V1" ', "'TOWER_V1'\n"):
            with self.subTest(raw=raw):
                node = _make_node(raw)
                node._publish(1, _jpeg(1280, 960), _CAPTURE_NS)
                self.assertEqual(_publisher(node, '/scene/image_raw/compressed').published, [])

    def test_only_camera_id_1_is_checked(self):
        node = _make_node('tower_v1')
        big = _jpeg(1280, 960)
        node._publish(0, big, _CAPTURE_NS)
        node._publish(2, big, _CAPTURE_NS)
        self.assertEqual(len(_publisher(node, '/gripper/image_raw/compressed').published), 1)
        self.assertEqual(len(_publisher(node, '/phone/image_raw/compressed').published), 1)

    def test_legacy_publishes_any_size_on_id_1_as_today(self):
        for raw in (None, '', 'legacy', 'tower', 'TOWER_V2', 'auto'):
            with self.subTest(raw=raw):
                node = _make_node(raw)
                payloads = (_jpeg(1280, 960), _jpeg(1920, 1080), _jpeg(640, 480),
                            b'\xff\xd8\xff\xe0 not really a jpeg \xff\xd9')
                for payload in payloads:
                    node._publish(1, payload, _CAPTURE_NS)
                self.assertEqual([m.data for m in _publisher(node, '/scene/image_raw/compressed').published],
                                 list(payloads))
                self.assertEqual(node.get_logger().lines('warn'), [])

    def test_100_dropped_scene_frames_warn_once(self):
        node = _make_node('tower_v1')
        for _ in range(100):
            node._publish(1, _jpeg(1280, 960), _CAPTURE_NS)
        warnings = node.get_logger().lines('warn')
        self.assertEqual(len(warnings), 1, warnings[:3])


# ---------------------------------------------------------------------------
# G8: a studio topic never satisfies a healthcheck that waits for a camera
# ---------------------------------------------------------------------------

_CAMERA_ROLE_NAMES = ('gripper', 'scene', 'phone')


def _healthcheck_patterns(path):
    """Every pattern the ``open_manipulator`` healthcheck greps the topic list
    for, with the camera-name variables filled in with every role name."""
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    start = text.index('\n  open_manipulator:\n')
    block = text[start:text.index('\n  physical_ai_server:\n', start)]
    check = block[block.index('\n    healthcheck:\n'):]
    code = '\n'.join(line for line in check.splitlines() if not line.lstrip().startswith('#'))
    raw = re.findall(r'grep -q\s+"?([^"\s)]+)', code)
    patterns = set()
    for pattern in raw:
        for name in _CAMERA_ROLE_NAMES:
            patterns.add(re.sub(r'\$\$\{[A-Z0-9_]+(?::-[a-z]*)?\}', name, pattern))
    return patterns


def _healthcheck_violations(topics):
    """``(compose, topic, pattern)`` for every topic a healthcheck grep would
    accept. The patterns hold only letters, digits, '_' and '/', so grep's
    regular expression is a plain substring test."""
    out = []
    for path in _COMPOSES:
        for pattern in _healthcheck_patterns(path):
            if not re.fullmatch(r'[A-Za-z0-9_/]+', pattern):
                raise AssertionError(f'{path}: {pattern!r} is not a plain substring pattern')
            out.extend((os.path.relpath(path, _REPO), topic, pattern)
                       for topic in topics if pattern in topic)
    return out


def _non_camera_topics(node):
    cameras = {f'/{name}/image_raw/compressed' for name, _pub in node.publishers_by_id.values()}
    return sorted(p.topic for p in node.publishers if p.topic not in cameras)


class StudioTopicMatchesNoHealthcheck(unittest.TestCase):
    """G8 (spec 40 S1): the composes' healthchecks wait for a camera topic by
    grepping ``ros2 topic list``; the studio and state publishers exist in
    every profile and must never stand in for the scene camera."""

    def test_the_scan_reads_every_healthcheck(self):
        for path in _COMPOSES:
            with self.subTest(compose=path):
                patterns = _healthcheck_patterns(path)
                self.assertIn('/joint_states', patterns)
                self.assertIn('/scene/image_raw/compressed', patterns)
                self.assertIn('/gripper/image_raw/compressed', patterns)

    def test_the_studio_and_state_topics_match_no_healthcheck_pattern(self):
        node = _make_node('tower_v1')
        topics = _non_camera_topics(node)
        self.assertEqual(topics, sorted((_SPLIT.STUDIO_TOPIC, _SPLIT.STATE_TOPIC)))
        self.assertEqual(_healthcheck_violations(topics), [])
        self.assertEqual(_healthcheck_violations([_SPLIT.STUDIO_TOPIC]), [])

    def test_the_old_studio_name_would_have_been_caught(self):
        mutant = _load_split('scene_split_g8_mutant')
        mutant.STUDIO_TOPIC = _OLD_STUDIO_NAME
        module = _load_node_module('camera_ingest_node_g8_mutant', split=mutant)
        node = _make_node('tower_v1', module=module)
        self.assertIn(_OLD_STUDIO_NAME, _non_camera_topics(node))
        violations = _healthcheck_violations(_non_camera_topics(node))
        self.assertEqual(sorted({c for c, _t, _p in violations}),
                         sorted(os.path.relpath(p, _REPO) for p in _COMPOSES))
        self.assertTrue(all(t == _OLD_STUDIO_NAME for _c, t, _p in violations), violations)


# ---------------------------------------------------------------------------
# scene_split.py ships beside the node
# ---------------------------------------------------------------------------

def _copied_bin_scripts():
    with open(_DOCKERFILE, encoding='utf-8') as handle:
        lines = re.sub(r'\\\n', ' ', handle.read()).splitlines()
    out = set()
    for line in lines:
        parts = line.split()
        if parts and parts[0] == 'COPY' and parts[-1].startswith('/usr/local/bin/') \
                and parts[-1].endswith('.py'):
            out.add(os.path.basename(parts[-1]))
    return out


def _parity_bin_loop():
    with open(_PARITY_SH, encoding='utf-8') as handle:
        text = handle.read()
    match = re.search(r'extract /usr/local/bin /tmp/parity_bin\n\s*for f in ([^;]+); do', text)
    if match is None:
        raise AssertionError('image_source_parity.sh: no plain-COPY loop after the /usr/local/bin extract')
    return set(match.group(1).split())


class SceneSplitShipsBesideTheNode(unittest.TestCase):

    def test_the_node_imports_its_sibling_scene_split(self):
        self.assertTrue(os.path.samefile(_MOD.scene_split.__file__, _SPLIT_FILE),
                        _MOD.scene_split.__file__)

    def test_the_parity_loop_byte_verifies_every_plain_copy_script(self):
        copied = _copied_bin_scripts()
        self.assertIn('scene_split.py', copied)
        self.assertIn('camera_ingest_node.py', copied)
        self.assertEqual(_parity_bin_loop(), copied)


if __name__ == '__main__':
    unittest.main()
