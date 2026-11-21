#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Roboter Studio's tower-camera consumer (spec 40 S1, WP-08).

``StudioCamera`` owns its node and executor, both made by the factories it is
handed, so these tests run it on a fake node, a fake executor, an injected
clock and an injected sleep (the sleep plays the executor thread: it advances
the clock and delivers the messages that fall due). The spin thread is never
started here (``_start_thread`` is replaced); the tests drive what it runs.

The node's half (``physical_ai_server.py`` imports rclpy and cannot be imported
here) is checked the suite's way: its methods are extracted by ``ast`` and run
on a stub node.
"""

from __future__ import annotations

import ast
import json
import math
import sys
import textwrap
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

from physical_ai_server import scene_provenance
from physical_ai_server.workflow import studio_camera as SC
from physical_ai_server.workflow.autocal import service as SV

_PS = Path(__file__).resolve().parents[1]
_REPO = _PS.parents[1]
_SERVER_PY = _PS / 'physical_ai_server' / 'physical_ai_server.py'
_MODULE_PY = _PS / 'physical_ai_server' / 'workflow' / 'studio_camera.py'
_SCENE_SPLIT_TWINS = (
    _REPO / 'robotis_ai_setup' / 'docker' / 'open_manipulator' / 'scene_split.py',
    _REPO / 'robotis_ai_setup' / 'gui' / 'app' / 'scene_split.py',
)
_INGEST_PY = _REPO / 'robotis_ai_setup' / 'docker' / 'open_manipulator' / 'camera_ingest_node.py'


# ── the fake ROS surface ─────────────────────────────────────────────────────

class FakeShutdown(Exception):
    """Stands in for rclpy's ExternalShutdownException."""


FAKE_ROS = types.SimpleNamespace(
    CompressedImage='CompressedImage', String='String',
    image_qos='IMAGE_QOS', latched_qos='LATCHED_QOS',
    ExternalShutdownException=FakeShutdown)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSleep:
    """Advances the fake clock and fires every delivery that has fallen due."""

    def __init__(self, clock):
        self.clock = clock
        self.due = []
        self.calls = []

    def at(self, t, deliver):
        self.due.append((t, deliver))
        self.due.sort(key=lambda item: item[0])

    def __call__(self, dt):
        assert dt >= 0, dt
        self.calls.append(dt)
        self.clock.t += dt
        while self.due and self.due[0][0] <= self.clock.t + 1e-9:
            self.due.pop(0)[1]()


class FakeSub:
    def __init__(self, msg_type, topic, callback, qos):
        self.msg_type, self.topic, self.callback, self.qos = msg_type, topic, callback, qos


class FakeLog:
    def __init__(self):
        self.lines = []

    def _line(self, msg, *a, **k):
        self.lines.append(str(msg))

    info = warning = error = _line


class FakeNode:
    def __init__(self, name, **kwargs):
        self.name, self.kwargs = name, kwargs
        self.subs = []
        self.destroyed_subs = []
        self.destroyed = False
        self.log = FakeLog()

    def create_subscription(self, msg_type, topic, callback, qos):
        sub = FakeSub(msg_type, topic, callback, qos)
        self.subs.append(sub)
        return sub

    def destroy_subscription(self, sub):
        self.subs.remove(sub)
        self.destroyed_subs.append(sub)
        return True

    def destroy_node(self):
        self.destroyed = True
        self.subs = []          # rclpy destroys every entity of the node with it

    def get_logger(self):
        return self.log


class FakeExecutor:
    def __init__(self):
        self.nodes = []
        self.script = []
        self.spins = 0
        self.shut_down = False

    def add_node(self, node):
        self.nodes.append(node)

    def spin_once(self, timeout_sec=None):
        self.spins += 1
        step = self.script.pop(0) if self.script else None
        if callable(step):
            step()
        elif isinstance(step, BaseException):
            raise step

    def shutdown(self, timeout_sec=None):
        self.shut_down = True


class Kit(types.SimpleNamespace):

    def subs(self, topic):
        return [s for n in self.nodes for s in n.subs if s.topic == topic]

    def image_subs(self):
        return self.subs(SC.STUDIO_TOPIC)

    def deliver(self, data):
        """One studio frame on every live image subscription (as DDS would)."""
        for sub in self.image_subs():
            sub.callback(types.SimpleNamespace(data=data, format='jpeg'))

    def say(self, payload):
        data = payload if isinstance(payload, str) else _json(payload)
        for sub in self.subs(scene_provenance.PROFILE_TOPIC):
            sub.callback(types.SimpleNamespace(data=data))

    def state(self, payload):
        data = payload if isinstance(payload, str) else _json(payload)
        for sub in self.subs(SC.STATE_TOPIC):
            sub.callback(types.SimpleNamespace(data=data))


def _json(obj):
    return json.dumps(obj, separators=(',', ':'))


def _jpeg(value, shape=(24, 32, 3)):
    ok, buf = cv2.imencode('.jpg', np.full(shape, value, np.uint8))
    assert ok
    return buf.tobytes()


JPEG_A, JPEG_B, JPEG_C, JPEG_D = _jpeg(40), _jpeg(120), _jpeg(200), _jpeg(80)


def make_kit(monkeypatch, *, node_factory=None, executor_factory=None, start=True):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    nodes, executors, threads = [], [], []

    def _node_factory(name, **kwargs):
        node = FakeNode(name, **kwargs)
        nodes.append(node)
        return node

    def _executor_factory():
        executor = FakeExecutor()
        executors.append(executor)
        return executor

    monkeypatch.setattr(SC, '_ros_interfaces', lambda: FAKE_ROS)
    monkeypatch.setattr(SC, '_start_thread', lambda target, name: threads.append((target, name)))
    cam = SC.StudioCamera(node_factory or _node_factory, executor_factory or _executor_factory,
                          clock, sleep)
    kit = Kit(cam=cam, clock=clock, sleep=sleep, nodes=nodes, executors=executors,
              threads=threads)
    if start:
        cam.start()
    return kit


@pytest.fixture
def kit(monkeypatch):
    return make_kit(monkeypatch)


# ── a frame request subscribes; the idle release; the bounded first-frame wait ─

def test_the_camera_owns_its_node_and_executor_and_two_permanent_subscriptions(kit):
    assert [n.name for n in kit.nodes] == ['physical_ai_server_studio']
    # The launch's `-r __node:=physical_ai_server` must not rename this node.
    assert kit.nodes[0].kwargs == {'use_global_arguments': False}
    assert [e.nodes for e in kit.executors] == [kit.nodes]
    assert [name for _, name in kit.threads] == ['studio-exec']
    permanent = {(s.topic, s.msg_type, s.qos) for s in kit.nodes[0].subs}
    assert permanent == {(scene_provenance.PROFILE_TOPIC, 'String', 'LATCHED_QOS'),
                         (SC.STATE_TOPIC, 'String', 'LATCHED_QOS')}
    assert kit.image_subs() == []          # no frame wanted, no picture subscribed


def test_a_frame_request_subscribes_and_waits_for_the_first_frame(kit):
    t0 = kit.clock.t
    kit.sleep.at(t0 + 0.1, lambda: kit.deliver(JPEG_A))
    found = kit.cam.latest_frame(1.0)
    subs = kit.image_subs()
    assert len(subs) == 1
    assert (subs[0].msg_type, subs[0].qos) == ('CompressedImage', 'IMAGE_QOS')
    assert found is not None
    bgr, age = found
    assert bgr.shape == (24, 32, 3) and abs(int(bgr.mean()) - 40) <= 2
    assert age == pytest.approx(0.0, abs=1e-9)
    assert kit.clock.t - t0 == pytest.approx(0.1, abs=0.021)


def _request_then_settle(kit):
    """Subscribe with one request, then make a second request 1 s later that is
    answered at once; returns the time of that last request."""
    kit.sleep.at(kit.clock.t + 0.1, lambda: kit.deliver(JPEG_A))
    assert kit.cam.latest_frame(1.0) is not None
    kit.clock.t += 1.0
    last = kit.clock.t
    assert kit.cam.latest_frame(5.0) is not None      # no wait: a frame is there
    assert kit.clock.t == last
    return last


def test_ten_seconds_after_the_last_request_the_picture_is_unsubscribed(kit):
    last = _request_then_settle(kit)
    first = kit.image_subs()[0]
    kit.clock.t = last + 9.99
    kit.cam._release_if_idle()
    assert kit.image_subs() == [first]
    kit.clock.t = last + 10.0
    kit.cam._release_if_idle()
    assert kit.image_subs() == []
    assert kit.nodes[0].destroyed_subs == [first]
    # The statement and the camera state stay subscribed.
    assert {s.topic for s in kit.nodes[0].subs} == {scene_provenance.PROFILE_TOPIC, SC.STATE_TOPIC}
    assert kit.cam.frame_age() is None     # nothing cached from the ended subscription
    assert kit.cam._decoded == (None, None)   # nor the decoded full-size frame


def test_after_the_release_a_request_resubscribes_and_gets_the_frame_arriving_0_4_s_later(kit):
    last = _request_then_settle(kit)
    kit.clock.t = last + 10.0
    kit.cam._release_if_idle()
    old = kit.nodes[0].destroyed_subs[0]
    t2 = kit.clock.t
    kit.sleep.at(t2 + 0.4, lambda: kit.deliver(JPEG_B))
    found = kit.cam.latest_frame(1.0, wait_s=2.0)
    subs = kit.image_subs()
    assert len(subs) == 1 and subs[0] is not old
    assert found is not None, 'review M4: the first request after a release must wait for its frame'
    bgr, age = found
    assert abs(int(bgr.mean()) - 120) <= 2          # the NEW frame, never the cached A
    assert age == pytest.approx(0.0, abs=1e-9)
    assert kit.clock.t - t2 == pytest.approx(0.4, abs=0.021)


def test_without_a_frame_the_request_answers_none_after_its_wait(kit):
    last = _request_then_settle(kit)
    kit.clock.t = last + 10.0
    kit.cam._release_if_idle()
    t2 = kit.clock.t
    assert kit.cam.latest_frame(1.0, wait_s=2.0) is None
    waited = kit.clock.t - t2
    assert 2.0 - 1e-9 <= waited <= 2.0 + 1e-9
    assert all(dt <= SC.WAIT_POLL_S + 1e-12 for dt in kit.sleep.calls)
    assert len(kit.image_subs()) == 1               # the request keeps it for 10 s


def test_the_wait_counts_from_the_subscription_not_from_every_call(kit):
    """A camera that never answers costs one wait per subscription, not one per poll."""
    t0 = kit.clock.t
    assert kit.cam.latest_frame(1.0, wait_s=2.0) is None
    assert kit.clock.t - t0 == pytest.approx(2.0, abs=1e-9)
    t1 = kit.clock.t
    assert kit.cam.latest_frame(1.0, wait_s=2.0) is None
    assert kit.clock.t == pytest.approx(t1, abs=1e-9)


def test_between_hold_and_release_the_subscription_survives_60_s_of_silence(kit):
    kit.cam.hold()
    subs = kit.image_subs()
    assert len(subs) == 1
    kit.clock.t += 60.0
    kit.cam._release_if_idle()
    assert kit.image_subs() == subs
    released = kit.clock.t
    kit.cam.release()
    kit.clock.t = released + 9.99
    kit.cam._release_if_idle()
    assert kit.image_subs() == subs
    kit.clock.t = released + 10.0
    kit.cam._release_if_idle()
    assert kit.image_subs() == []


def test_holds_nest_and_a_request_during_a_hold_does_not_subscribe_twice(kit):
    kit.cam.hold()
    kit.cam.hold()
    kit.deliver(JPEG_A)
    assert kit.cam.latest_frame(1.0) is not None
    assert len(kit.image_subs()) == 1
    kit.cam.release()
    kit.clock.t += 60.0
    kit.cam._release_if_idle()
    assert len(kit.image_subs()) == 1               # one hold is still open
    kit.cam.release()
    kit.cam.release()                               # an extra release is harmless
    kit.clock.t += 10.0
    kit.cam._release_if_idle()
    assert kit.image_subs() == []


def test_one_decode_per_message_and_only_on_demand(kit, monkeypatch):
    decodes = []
    real = SC._decode_jpeg

    def counting(data):
        decodes.append(data)
        return real(data)

    monkeypatch.setattr(SC, '_decode_jpeg', counting)
    kit.cam.hold()
    kit.deliver(JPEG_A)
    first = [kit.cam.latest_frame(1.0)[0] for _ in range(3)]
    assert decodes == [JPEG_A]
    assert first[0] is first[1] is first[2]
    kit.deliver(JPEG_B)
    assert abs(int(kit.cam.latest_frame(1.0)[0].mean()) - 120) <= 2
    kit.deliver(JPEG_C)
    kit.deliver(JPEG_D)                              # C is never asked for
    assert abs(int(kit.cam.latest_frame(1.0)[0].mean()) - 80) <= 2
    assert decodes == [JPEG_A, JPEG_B, JPEG_D]
    assert kit.cam.latest_frame(1.0)[0].flags.writeable is False   # shared: never written


def test_a_frame_older_than_max_age_is_none_and_never_decoded(kit, monkeypatch):
    decodes = []
    monkeypatch.setattr(SC, '_decode_jpeg', lambda data: decodes.append(data))
    kit.cam.hold()
    kit.deliver(JPEG_A)
    kit.clock.t += 1.5
    assert kit.cam.frame_age() == pytest.approx(1.5)
    assert kit.cam.latest_frame(1.0) is None
    assert decodes == []


def test_a_frame_that_does_not_decode_is_none_and_decoded_once(kit, monkeypatch):
    decodes = []

    def broken(data):
        decodes.append(data)
        return None

    monkeypatch.setattr(SC, '_decode_jpeg', broken)
    kit.cam.hold()
    kit.deliver(b'\xff\xd8 not a jpeg')
    assert kit.cam.latest_frame(1.0) is None
    assert kit.cam.latest_frame(1.0) is None
    assert len(decodes) == 1


def test_the_real_decoder_answers_none_for_garbage():
    assert SC._decode_jpeg(b'') is None
    assert SC._decode_jpeg(b'\xff\xd8\xff garbage') is None
    assert SC._decode_jpeg(JPEG_B).shape == (24, 32, 3)


def test_a_late_message_of_an_ended_subscription_is_dropped(kit):
    kit.cam.hold()
    stale = kit.image_subs()[0]
    kit.cam.release()
    kit.clock.t += 10.0
    kit.cam._release_if_idle()
    stale.callback(types.SimpleNamespace(data=JPEG_A, format='jpeg'))
    assert kit.cam.frame_age() is None
    assert kit.cam.latest_frame(1.0, wait_s=0.0) is None


# ── the mat check's looks on a picture subscribed afresh (FIX-K-r1, K-2) ─────

def _mat_check_looks(kit, first_s, count=200, period_s=1.0 / 15.0):
    """``SceneCalibration._frames(MAT_CHECK_FRAMES)`` on this camera, whose picture the first request
    subscribes: its first frame arrives ``first_s`` after the subscription, then one every ``period_s`` (15 Hz,
    ``count`` in all). Returns the looks and the time the call took."""
    t0 = kit.clock.t
    for k in range(count):
        kit.sleep.at(t0 + first_s + k * period_s, lambda: kit.deliver(JPEG_A))
    calibration = types.SimpleNamespace(_clock=kit.clock, _camera=kit.cam, _sleep=kit.sleep,
                                        _detect=lambda frame, roi: frame)
    looks = SV.SceneCalibration._frames(calibration, SV.MAT_CHECK_FRAMES)
    return looks, kit.clock.t - t0


@pytest.mark.parametrize('first_s', [0.05, 0.45, 0.6, 1.0, 1.5])
def test_the_mat_check_gets_three_looks_however_late_the_first_frame_comes(kit, first_s):
    """K-2: the picture is released 10 s after the last request, so the mat check of a program started after a
    pause runs on a picture subscribed afresh. Its window for three distinct looks starts at the first frame: a
    window counted from the request ended inside the first-frame wait and left one look (before the fix: one look
    for a first frame 0.45, 0.6, 1.0 and 1.5 s after the subscription)."""
    assert SV.MAT_FRAMES_WAIT_S == 0.5
    assert SV._FRAME_POLL_S == 0.01
    looks, took = _mat_check_looks(kit, first_s)
    assert len(looks) == SV.MAT_CHECK_FRAMES == 3
    assert len({id(look) for look in looks}) == 3                 # three distinct frames
    assert took <= first_s + SV.MAT_FRAMES_WAIT_S + 1e-9


def test_a_picture_that_stops_after_its_first_frame_still_ends_the_looks_in_time(kit):
    """The window still bounds the wait: one frame and then silence ends the looks MAT_FRAMES_WAIT_S after that
    frame, with the one look."""
    looks, took = _mat_check_looks(kit, 0.6, count=1)
    assert len(looks) == 1
    assert 0.6 + SV.MAT_FRAMES_WAIT_S - 1e-9 <= took <= 0.6 + SV.MAT_FRAMES_WAIT_S + SV._FRAME_POLL_S + 1e-9


def test_a_picture_that_never_comes_ends_the_looks_after_the_first_frame_wait(kit):
    looks, took = _mat_check_looks(kit, 0.0, count=0)
    assert looks == []
    assert took == pytest.approx(SC.FIRST_FRAME_WAIT_S, abs=1e-9)


# ── start, shutdown and the spin loop ────────────────────────────────────────

def test_a_camera_whose_node_cannot_be_made_stays_inert(monkeypatch):
    def boom(name, **kwargs):
        raise RuntimeError('no context')

    kit = make_kit(monkeypatch, node_factory=boom, start=False)
    with pytest.raises(RuntimeError):
        kit.cam.start()
    t0 = kit.clock.t
    assert kit.cam.latest_frame(1.0, wait_s=2.0) is None
    assert kit.clock.t == t0 and kit.sleep.calls == []      # nothing to wait for
    kit.cam.hold()
    kit.cam.release()
    assert kit.cam.frame_age() is None
    assert kit.cam.profile() is None
    assert kit.cam.provider() == {'profile': None, 'profile_id': None, 'platform': None}
    assert kit.threads == []
    kit.cam.shutdown()                                  # never raises


def test_a_failed_executor_destroys_the_node_it_made(monkeypatch):
    def boom():
        raise RuntimeError('no executor')

    kit = make_kit(monkeypatch, executor_factory=boom, start=False)
    with pytest.raises(RuntimeError):
        kit.cam.start()
    assert [n.destroyed for n in kit.nodes] == [True]
    assert kit.threads == []
    assert kit.cam.latest_frame(1.0) is None


def test_a_picture_that_cannot_be_subscribed_is_none_and_never_raises(kit):
    node = kit.nodes[0]
    real = node.create_subscription

    def refuse_pictures(msg_type, topic, callback, qos):
        if topic == SC.STUDIO_TOPIC:
            raise RuntimeError('context is shutting down')
        return real(msg_type, topic, callback, qos)

    node.create_subscription = refuse_pictures
    t0 = kit.clock.t
    assert kit.cam.latest_frame(1.0, wait_s=2.0) is None
    assert kit.clock.t == t0                            # nothing to wait for
    kit.cam.hold()
    kit.cam.release()
    assert kit.image_subs() == []
    assert [line for line in node.log.lines if 'not subscribed' in line]
    node.create_subscription = real                     # the next request tries again
    kit.sleep.at(kit.clock.t + 0.1, lambda: kit.deliver(JPEG_A))
    assert kit.cam.latest_frame(1.0) is not None


def test_shutdown_stops_everything_once_and_never_raises(kit):
    kit.cam.hold()
    kit.cam.shutdown()
    assert kit.executors[0].shut_down and kit.nodes[0].destroyed
    assert kit.cam.latest_frame(1.0, wait_s=2.0) is None
    kit.cam.hold()
    assert kit.image_subs() == []
    kit.cam.shutdown()


def test_the_spin_loop_spins_releases_idle_pictures_and_ends_on_shutdown(kit):
    executor = kit.executors[0]
    kit.cam.latest_frame(1.0, wait_s=0.0)               # subscribes, no frame
    assert len(kit.image_subs()) == 1

    def later():
        kit.clock.t += 10.0

    executor.script = [None, later, FakeShutdown()]
    kit.cam._spin_loop()
    assert executor.spins == 3
    assert kit.image_subs() == []                       # released between two spins


def test_the_spin_loop_survives_a_failing_spin_and_ends_on_the_stop_flag(kit):
    executor = kit.executors[0]
    executor.script = [RuntimeError('boom'), RuntimeError('boom'), kit.cam.shutdown]
    kit.cam._spin_loop()
    assert executor.spins == 3
    errors = [line for line in kit.nodes[0].log.lines if 'boom' in line]
    assert len(errors) == 1                             # logged once, not per spin
    assert kit.sleep.calls == [SC.SPIN_TIMEOUT_S, SC.SPIN_TIMEOUT_S]


# ── the camera state ─────────────────────────────────────────────────────────

def test_the_latest_camera_state_is_kept_and_malformed_ones_are_ignored(kit):
    assert kit.cam.camera_state() is None
    good = {'v': 1, 'profile': 'tower_v1', 'state': 'streaming', 'fault': ''}
    kit.state(good)
    assert kit.cam.camera_state() == good
    kit.state('not json')
    kit.state('[1, 2]')
    kit.state('{"v": 1, "state": "' + 'x' * SC.STATE_MAX_BYTES + '"}')
    assert kit.cam.camera_state() == good
    copy = kit.cam.camera_state()
    copy['state'] = 'changed'
    assert kit.cam.camera_state() == good               # a copy, never the cache


# ── the ROS pieces the default start uses ────────────────────────────────────

def test_the_default_interfaces_are_best_effort_keep_last_1_and_latched_depth_1(monkeypatch):
    made = []

    class QoSProfile:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.kwargs = kwargs

    enum = types.SimpleNamespace
    qos = types.ModuleType('rclpy.qos')
    qos.QoSProfile = QoSProfile
    qos.ReliabilityPolicy = enum(RELIABLE='RELIABLE', BEST_EFFORT='BEST_EFFORT')
    qos.DurabilityPolicy = enum(TRANSIENT_LOCAL='TRANSIENT_LOCAL', VOLATILE='VOLATILE')
    qos.HistoryPolicy = enum(KEEP_LAST='KEEP_LAST')
    executors = types.ModuleType('rclpy.executors')
    executors.ExternalShutdownException = FakeShutdown
    sensor_msg = types.ModuleType('sensor_msgs.msg')
    sensor_msg.CompressedImage = 'CompressedImage'
    std_msg = types.ModuleType('std_msgs.msg')
    std_msg.String = 'String'
    for name, module in (('rclpy', types.ModuleType('rclpy')), ('rclpy.qos', qos),
                         ('rclpy.executors', executors),
                         ('sensor_msgs', types.ModuleType('sensor_msgs')),
                         ('sensor_msgs.msg', sensor_msg),
                         ('std_msgs', types.ModuleType('std_msgs')), ('std_msgs.msg', std_msg)):
        monkeypatch.setitem(sys.modules, name, module)
    ros = SC._ros_interfaces()
    assert (ros.CompressedImage, ros.String) == ('CompressedImage', 'String')
    assert ros.ExternalShutdownException is FakeShutdown
    assert ros.image_qos.kwargs == {'depth': 1, 'history': 'KEEP_LAST',
                                    'reliability': 'BEST_EFFORT'}
    assert ros.latched_qos.kwargs == {'depth': 1, 'reliability': 'RELIABLE',
                                      'durability': 'TRANSIENT_LOCAL'}


# ── what the module names, and what it never touches ─────────────────────────

def _literals(path, names):
    out = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in names:
                out[target.id] = ast.literal_eval(node.value)
    return out


def test_the_topics_and_bounds_equal_the_capture_sides():
    names = ('STUDIO_TOPIC', 'STATE_TOPIC', 'STATE_MAX_BYTES', 'PLATFORMS')
    for twin in _SCENE_SPLIT_TWINS:
        theirs = _literals(twin, names)
        assert theirs == {n: getattr(SC, n) for n in names}, twin
    assert SC.PLATFORMS == scene_provenance.PLATFORMS
    assert _literals(_INGEST_PY, ('_STUDIO_FRAME_ID',)) == {'_STUDIO_FRAME_ID': SC.STUDIO_CAMERA}


def test_the_shipped_numbers_are_the_ones_spec_40_chose():
    assert SC.IDLE_RELEASE_S == 10.0
    assert SC.FIRST_FRAME_WAIT_S == 2.0
    assert SC.WAIT_POLL_S == 0.02
    assert SC.SPIN_TIMEOUT_S == 0.1
    assert SC.STATE_MAX_BYTES == 4096
    assert SC.STATEMENT_VERSION == 1


def test_the_studio_camera_never_touches_the_communicator():
    source = _MODULE_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not any('communicator' in s.lower() for s in names | attrs)
    assert 'communicator' not in source.lower()
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names} | {n.module for n in ast.walk(tree)
                                     if isinstance(n, ast.ImportFrom) and n.module}
    # stdlib at the top; cv2/numpy and the ROS pieces only inside their functions.
    top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert top <= {'functools', 'json', 'threading', 'time', 'types', 'physical_ai_server'}, top
    assert 'physical_ai_server.communication' not in imported


# ── the node: only 'scene_studio' is rerouted ────────────────────────────────

_SRC = _SERVER_PY.read_text(encoding='utf-8')
_TREE = ast.parse(_SRC)
_CLASS = next(n for n in _TREE.body if isinstance(n, ast.ClassDef) and n.name == 'PhysicalAIServer')


def _methods(*names):
    ns = {'math': math, 'STUDIO_CAMERA': SC.STUDIO_CAMERA}
    for node in _CLASS.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            src = textwrap.dedent(ast.get_source_segment(_SRC, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    return {n: ns[n] for n in names}


class _RecordingCommunicator:
    def __init__(self):
        self.calls = []

    def get_latest_bgr_frame(self, camera):
        self.calls.append(('frame', camera))
        return f'comm-{camera}'

    def get_camera_msg_age_s(self, camera):
        self.calls.append(('age', camera))
        return 0.25


class _RecordingStudio:
    def __init__(self):
        self.calls = []

    def latest_frame(self, max_age_s, wait_s=SC.FIRST_FRAME_WAIT_S):
        self.calls.append(('frame', max_age_s))
        return ('studio-bgr', 0.1)

    def frame_age(self):
        self.calls.append(('age',))
        return 0.5


def _stub_node():
    node = types.SimpleNamespace(communicator=_RecordingCommunicator(),
                                 studio_camera=_RecordingStudio(),
                                 get_logger=lambda: FakeLog())
    m = _methods('_get_latest_camera_frame', '_get_camera_frame_age')
    node.frame = lambda camera: m['_get_latest_camera_frame'](node, camera)
    node.age = lambda camera: m['_get_camera_frame_age'](node, camera)
    return node


def test_scene_and_gripper_keep_the_communicator_and_only_scene_studio_is_rerouted():
    node = _stub_node()
    assert node.frame('scene') == 'comm-scene'
    assert node.frame('gripper') == 'comm-gripper'
    assert node.age('scene') == 0.25
    assert node.age('gripper') == 0.25
    assert node.communicator.calls == [('frame', 'scene'), ('frame', 'gripper'),
                                       ('age', 'scene'), ('age', 'gripper')]
    assert node.studio_camera.calls == []
    assert node.frame('scene_studio') == 'studio-bgr'
    assert node.age('scene_studio') == 0.5
    assert node.studio_camera.calls == [('frame', math.inf), ('age',)]
    assert len(node.communicator.calls) == 4          # untouched by 'scene_studio'


def test_scene_studio_answers_none_when_the_camera_has_nothing():
    node = _stub_node()
    node.studio_camera.latest_frame = lambda max_age_s, wait_s=2.0: None
    assert node.frame('scene_studio') is None
    node.communicator = None                           # a degraded node still answers
    node.studio_camera.frame_age = lambda: None
    assert node.age('scene_studio') is None


def _init_source():
    init = next(n for n in _CLASS.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    return ast.get_source_segment(_SRC, init), init


def test_the_node_builds_the_camera_with_its_own_ros_pieces_and_starts_it_guarded():
    src, init = _init_source()
    built = [n for n in ast.walk(init) if isinstance(n, ast.Assign)
             and ast.unparse(n.targets[0]) == 'self.studio_camera']
    assert [ast.unparse(n.value) for n in built] == [
        'StudioCamera(rclpy.create_node, SingleThreadedExecutor, time.monotonic, time.sleep)']
    # Built at the top level of __init__ (never inside a try that could skip it),
    # started inside one.
    assert built[0] in init.body
    starts = [t for t in ast.walk(init) if isinstance(t, ast.Try)
              and any(ast.unparse(s) == 'self.studio_camera.start()' for s in t.body)]
    assert len(starts) == 1 and starts[0].handlers
    assert src.index('self.studio_camera = ') < src.index('self.studio_camera.start()')
    assert src.index('self.studio_camera.start()') < src.index('self._init_robot_profile()')
    assert 'from physical_ai_server.workflow.studio_camera import STUDIO_CAMERA, StudioCamera' in _SRC


def test_main_shuts_the_studio_camera_down_before_the_node():
    main = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    src = ast.get_source_segment(_SRC, main)
    finally_src = src[src.index('finally:'):]
    at = finally_src.index('node.studio_camera.shutdown()')
    assert finally_src.index('node._finalize_recording_on_shutdown()') < at
    assert at < finally_src.index('executor.shutdown()') < finally_src.index('node.destroy_node()')
