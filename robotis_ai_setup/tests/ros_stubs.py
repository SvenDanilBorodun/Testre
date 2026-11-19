# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Fake ``rclpy``, ``sensor_msgs`` and ``std_msgs`` for loading the arm
container's ROS scripts in a test that has no ROS installed.

Not a test module (the name does not match ``test*.py``); the test modules
import it as a sibling, the way they import ``timeout_guard``.

``modules()`` answers a fresh set of fake modules by name, for
``mock.patch.dict(sys.modules, ros_stubs.modules())`` around the import of the
script under test. The fakes record what the script does with them: a
``Node``'s logger keeps every line it was given (``FakeLogger.records``) and
each publisher every message (``FakePublisher.published``); nothing reaches a
network or a clock.
"""

import types


class FakeLogger:
    """Records ``(level, text)`` for every call."""

    def __init__(self):
        self.records = []

    def lines(self, level):
        return [text for lvl, text in self.records if lvl == level]

    def info(self, text):
        self.records.append(('info', text))

    def warn(self, text):
        self.records.append(('warn', text))

    def warning(self, text):
        self.records.append(('warn', text))


class FakePublisher:

    def __init__(self, msg_type, topic, qos):
        self.msg_type = msg_type
        self.topic = topic
        self.qos = qos
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


class Time:

    def __init__(self, sec=0, nanosec=0):
        self.sec = sec
        self.nanosec = nanosec


class _Now:

    def __init__(self, sec, nanosec):
        self._sec = sec
        self._nanosec = nanosec

    def to_msg(self):
        return Time(self._sec, self._nanosec)


class FakeClock:
    """``now()`` answers a fixed instant (``NOW_SEC``, ``NOW_NANOSEC``)."""

    NOW_SEC = 1234
    NOW_NANOSEC = 5678

    def now(self):
        return _Now(self.NOW_SEC, self.NOW_NANOSEC)


class Node:
    """The parts of ``rclpy.node.Node`` the arm scripts call."""

    def __init__(self, node_name, *args, **kwargs):
        self.node_name = node_name
        self._logger = FakeLogger()
        self._clock = FakeClock()
        self.publishers = []
        self.timers = []

    def get_logger(self):
        return self._logger

    def get_clock(self):
        return self._clock

    def create_publisher(self, msg_type, topic, qos):
        pub = FakePublisher(msg_type, topic, qos)
        self.publishers.append(pub)
        return pub

    def create_timer(self, period_s, callback):
        timer = types.SimpleNamespace(period_s=period_s, callback=callback)
        self.timers.append(timer)
        return timer

    def destroy_node(self):
        pass


class QoSProfile:

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Header:

    def __init__(self):
        self.stamp = Time()
        self.frame_id = ''


class CompressedImage:

    def __init__(self):
        self.header = Header()
        self.format = ''
        self.data = b''


class String:

    def __init__(self, data=''):
        self.data = data


def modules():
    """A fresh set of fake modules, keyed by import name."""
    rclpy = types.ModuleType('rclpy')
    rclpy.init = lambda *args, **kwargs: None
    rclpy.ok = lambda *args, **kwargs: False
    rclpy.shutdown = lambda *args, **kwargs: None
    rclpy.spin = lambda *args, **kwargs: None

    node = types.ModuleType('rclpy.node')
    node.Node = Node

    qos = types.ModuleType('rclpy.qos')
    qos.QoSProfile = QoSProfile
    qos.HistoryPolicy = types.SimpleNamespace(KEEP_LAST='keep_last', KEEP_ALL='keep_all')
    qos.ReliabilityPolicy = types.SimpleNamespace(RELIABLE='reliable', BEST_EFFORT='best_effort')

    rclpy.node = node
    rclpy.qos = qos

    sensor_msgs = types.ModuleType('sensor_msgs')
    sensor_msgs_msg = types.ModuleType('sensor_msgs.msg')
    sensor_msgs_msg.CompressedImage = CompressedImage
    sensor_msgs.msg = sensor_msgs_msg

    std_msgs = types.ModuleType('std_msgs')
    std_msgs_msg = types.ModuleType('std_msgs.msg')
    std_msgs_msg.String = String
    std_msgs_msg.Header = Header
    std_msgs.msg = std_msgs_msg

    return {
        'rclpy': rclpy,
        'rclpy.node': node,
        'rclpy.qos': qos,
        'sensor_msgs': sensor_msgs,
        'sensor_msgs.msg': sensor_msgs_msg,
        'std_msgs': std_msgs,
        'std_msgs.msg': std_msgs_msg,
    }
