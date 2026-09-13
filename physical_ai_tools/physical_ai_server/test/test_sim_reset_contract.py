#!/usr/bin/env python3
"""The simulator always returns to a defined start state.

Two defects shared one shape: the server's ``SimWorld`` was mutated by a run and
then only ever reset at the NEXT run start, while ``_sim_idle_republish``
re-broadcast it with ``force=True`` every 0.5 s in the meantime. After „Stopp"
the React twin was pinned to wherever the run left the cubes, a cube stopped
mid-carry stayed attached to the gripper, and the next run inherited both.

``physical_ai_server.py`` cannot be imported without rclpy, so this file follows
``test_sim_node_wiring.py``'s split: the three new node helpers are COMPILED OUT
of the source and bound to a stub self (they touch nothing but ``self`` and the
logger, so this is the real code, not a re-implementation), while the wiring
facts that have no runtime surface are asserted against the source AST.
"""

from __future__ import annotations

import ast
import json
import pathlib
import threading
import time

import pytest

from physical_ai_server import robot_profiles
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_world import SimWorld


_NODE = (pathlib.Path(__file__).resolve().parents[1]
         / 'physical_ai_server' / 'physical_ai_server.py')
_SRC = _NODE.read_text(encoding='utf-8')
_TREE = ast.parse(_SRC)

_HELPERS = ('_sim_home_full_joints', '_seed_sim_rest_pose', '_reset_sim_scene',
            '_sim_joint_names', '_publish_sim_joint_state', '_publish_sim_frame',
            '_publish_sim_objects', '_sim_idle_republish', '_stamp_sim_joint_state',
            '_publish_sim_teleport')


def _node_class_constant(name):
    """A class-level constant of the node, read from its SOURCE — a stub that
    restated the number would let a mutation of the shipped value pass."""
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)
                        and stmt.targets[0].id == name):
                    return ast.literal_eval(stmt.value)
    raise AssertionError(f'{name} is no longer a class constant of the node')


def _load_helpers():
    """Compile the node helpers out of the source and return them as plain
    functions, so they can be bound to a stub self and actually EXERCISED. The
    namespace carries the module-level imports they reference."""
    wanted = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name in _HELPERS:
            wanted[node.name] = node
    missing = set(_HELPERS) - set(wanted)
    assert not missing, f'node helper(s) renamed or removed: {sorted(missing)}'
    module = ast.Module(body=[wanted[n] for n in _HELPERS], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {'time': time, 'json': json}
    exec(compile(module, str(_NODE), 'exec'), ns)  # noqa: S102 — our own source
    return ns


_FN = _load_helpers()


class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        pass


class _StubNode:
    """Just enough `self` for the three helpers: the sim handles they read, and
    recording stand-ins for the two publishers they call."""

    def __init__(self, profile=None, sim_arm=None, sim_world=None, objects=None):
        self._arm_profile = profile
        self._sim_arm = sim_arm
        self._sim_world = sim_world
        self._sim_objects = list(objects or [])
        self._last_sim_joints = None
        self.published_poses = []
        self.forced_object_publishes = 0
        self._logger = _Logger()

    def get_logger(self):
        return self._logger

    def _publish_sim_joint_state(self, q, late_s=0.0, publish_scene=True):
        self.published_poses.append(list(q))
        self._last_sim_joints = [float(v) for v in q]

    def _publish_sim_objects(self, force=False):
        if force:
            self.forced_object_publishes += 1

    # The real methods, bound.
    _SIM_RESET_HOLD_S = _node_class_constant('_SIM_RESET_HOLD_S')
    _sim_home_full_joints = _FN['_sim_home_full_joints']
    _seed_sim_rest_pose = _FN['_seed_sim_rest_pose']
    _reset_sim_scene = _FN['_reset_sim_scene']
    _publish_sim_teleport = _FN['_publish_sim_teleport']


class _PublishStubNode:
    """A stub that binds the REAL ``_publish_sim_joint_state`` (and the real
    ``_sim_joint_names`` it calls), so the wire-shape guard is exercised rather
    than assumed. ``sensor_msgs`` is stubbed into ``sys.modules`` because the
    function imports it lazily inside its own body; ``get_clock`` is absent on
    purpose, since the real method wraps the stamp in its own try/except."""

    _SIM_JOINT_NAMES = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5',
                        'gripper_joint_1']

    def __init__(self, profile=None):
        self._arm_profile = profile
        self._sim_joint_state_publisher = _RecordingPublisher()
        self._sim_world = None
        self._sim_objects_publisher = None
        self._last_sim_objects_json = None
        self._last_sim_joints = None
        self._sim_pub_lock = threading.RLock()
        self._last_sim_publish_mono = 0.0
        self._last_sim_stamp_ns = 0
        self._logger = _Logger()

    def get_logger(self):
        return self._logger

    _SIM_STAMP_REORDER_S = _node_class_constant('_SIM_STAMP_REORDER_S')
    _sim_joint_names = _FN['_sim_joint_names']
    _publish_sim_joint_state = _FN['_publish_sim_joint_state']
    _stamp_sim_joint_state = _FN['_stamp_sim_joint_state']
    _publish_sim_objects = lambda self, force=False, snapshot=None, resend=False: None  # noqa: E731


class _RecordingPublisher:
    def __init__(self):
        self.messages = []

    def publish(self, msg):
        self.messages.append(msg)


@pytest.fixture(autouse=True)
def _stub_sensor_msgs(monkeypatch):
    """`_publish_sim_joint_state` does `from sensor_msgs.msg import JointState`
    inside the function body, which is unavailable off a ROS install."""
    import sys
    import types

    class _JointState:
        def __init__(self):
            self.header = types.SimpleNamespace(
                stamp=types.SimpleNamespace(sec=0, nanosec=0))
            self.name = []
            self.position = []

    pkg = types.ModuleType('sensor_msgs')
    msgmod = types.ModuleType('sensor_msgs.msg')
    msgmod.JointState = _JointState
    pkg.msg = msgmod
    monkeypatch.setitem(sys.modules, 'sensor_msgs', pkg)
    monkeypatch.setitem(sys.modules, 'sensor_msgs.msg', msgmod)
    yield


def _profile(profile_id='omx_full'):
    return robot_profiles.resolve(profile_id)


def _home_full(profile):
    return list(profile.home_joints_rad) + [profile.gripper_open_rad]


def _sim_stack(profile, objects):
    """A world + arm wired the way _get_or_create_sim_workflow_manager wires them."""
    world = SimWorld(objects)
    arm = SimArm(
        objects=objects,
        num_arm_joints=len(profile.home_joints_rad),
        home_full_joints=_home_full(profile),
        close_threshold_rad=profile.sim_close_threshold_rad,
        world=world,
    )
    return world, arm


# ── the rest pose that gives the twin something to draw before run #1 ────────

# From the REGISTRY, never a restated list: a hardcoded triple stopped
# covering `edu1_studio` the day it was added, silently.
@pytest.mark.parametrize('profile_id',
                         sorted(robot_profiles.ROBOT_PROFILES))
def test_rest_pose_vector_matches_the_published_joint_names(profile_id):
    """`_publish_sim_joint_state` sets msg.name from the profile's joint_names and
    msg.position from this vector — a length mismatch is a malformed JointState."""
    profile = _profile(profile_id)
    node = _StubNode(profile=profile)
    q = node._sim_home_full_joints()
    assert q is not None
    assert len(q) == len(profile.joint_names)
    assert q == pytest.approx(_home_full(profile))


def test_seed_publishes_the_rest_pose_once():
    node = _StubNode(profile=_profile())
    node._seed_sim_rest_pose()
    assert node.published_poses == [pytest.approx(_home_full(_profile()))]


def test_seed_never_overwrites_a_live_pose():
    """The seed exists for the pre-first-run window only. Once anything has been
    published — a run, or an earlier seed — it must not fire again, or a reset
    could be undone by a stray boot path."""
    node = _StubNode(profile=_profile())
    node._last_sim_joints = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    node._seed_sim_rest_pose()
    assert node.published_poses == []


def test_seed_is_silent_without_a_profile_home():
    node = _StubNode(profile=None)
    node._seed_sim_rest_pose()
    assert node.published_poses == []


# ── the reset ────────────────────────────────────────────────────────────────

def test_reset_is_a_no_op_on_a_rig_that_never_opened_the_simulator():
    """`/workflow/stop` reaches this on EVERY stop, including a real-arm one, and
    the „Simulation zurücksetzen" button reaches it with nothing running at all.
    With no sim runtime it must publish nothing."""
    node = _StubNode(profile=_profile())
    node._reset_sim_scene()
    assert node.published_poses == []
    assert node.forced_object_publishes == 0
    assert node._logger.warnings == []


def test_reset_restores_placement_clears_the_grasp_and_parks_the_arm():
    profile = _profile()
    objects = [{'type': 'wuerfel', 'tag_id': 7, 'x': 0.15, 'y': 0.0, 'yaw': 0.0}]
    world, arm = _sim_stack(profile, objects)

    # A run happened: the cube was picked up and carried somewhere else.
    world.bind_tag(0, 20)
    assert world.capture_nearest(0.15, 0.0, 0.06) == 0
    world.carry_to(0.24, -0.08)
    assert world.is_held()
    assert world.objects()[0]['x'] == pytest.approx(0.24)

    node = _StubNode(profile=profile, sim_arm=arm, sim_world=world, objects=objects)
    node._reset_sim_scene()

    live = world.objects()[0]
    assert (live['x'], live['y']) == pytest.approx((0.15, 0.0))
    assert world.held_key() is None
    assert not world.is_held()
    # The twin is told, once, about both halves.
    assert node.published_poses == [pytest.approx(_home_full(profile))]
    assert node.forced_object_publishes == 1


def test_reset_publishes_the_pose_the_ARM_will_start_from_not_a_re_derived_one():
    """Read the pose back off SimArm rather than re-deriving it from the profile.

    The two agree for every shipped profile, so this is written around the case
    where they CANNOT: a profile that declares no HOME. The node then passes
    `home_full_joints=None` and SimArm falls back to its own module constant —
    `_sim_home_full_joints()` returns None there, so a re-derived reset would park
    the twin nowhere at all while the arm sat at its fallback HOME. That
    disagreement is the whole class of bug this change exists to end.
    """
    class _HomelessProfile:
        gripper_open_rad = 0.8
        sim_close_threshold_rad = None

    profile = _HomelessProfile()
    objects = [{'type': 'wuerfel', 'tag_id': 7, 'x': 0.15, 'y': 0.0, 'yaw': 0.0}]
    world = SimWorld(objects)
    arm = SimArm(objects=objects, home_full_joints=None, world=world)
    arm.publish([([0.3] * 6, 0.1)])
    assert arm.get_joints() == pytest.approx([0.3] * 6)

    node = _StubNode(profile=profile, sim_arm=arm, sim_world=world, objects=objects)
    assert node._sim_home_full_joints() is None, 'the re-derived pose is unavailable'
    node._reset_sim_scene()

    assert node.published_poses == [pytest.approx(arm.get_joints())]
    assert node.published_poses[-1] != pytest.approx([0.3] * 6), 'arm was re-seeded'


def test_reset_falls_back_to_the_world_alone_when_no_arm_was_built():
    profile = _profile()
    objects = [{'type': 'wuerfel', 'tag_id': 7, 'x': 0.15, 'y': 0.0, 'yaw': 0.0}]
    world = SimWorld(objects)
    world.bind_tag(0, 20)
    world.capture_nearest(0.15, 0.0, 0.06)

    node = _StubNode(profile=profile, sim_world=world, objects=objects)
    node._reset_sim_scene()

    assert world.held_key() is None
    assert node.published_poses == [pytest.approx(_home_full(profile))]
    assert node.forced_object_publishes == 1


def test_reset_never_raises_out_of_a_stop():
    """A reset is a convenience; a Stop must not fail because of it."""
    class _Exploding:
        def set_objects(self, _objects):
            raise RuntimeError('boom')

    node = _StubNode(profile=_profile(), sim_arm=_Exploding())
    node._reset_sim_scene()  # must not raise
    assert node._logger.warnings
    assert 'boom' in node._logger.warnings[-1]


# ── the wiring that has no runtime surface here ──────────────────────────────

def _method(name):
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found in physical_ai_server.py')


def _calls_in(fn):
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
    return out


def test_the_sim_manager_gets_its_own_finish_hook():
    """Both managers are constructed with an on_finished kwarg. Only the SIM one
    may reset the simulator — _on_workflow_finished is handed a phase and cannot
    tell which runtime exited, so a shared branch would reset off a real run."""
    hooks = [
        node.value.attr
        for node in ast.walk(_TREE)
        if isinstance(node, ast.keyword) and node.arg == 'on_finished'
        and isinstance(node.value, ast.Attribute)
    ]
    assert sorted(hooks) == ['_on_sim_workflow_finished', '_on_workflow_finished']


def test_the_sim_finish_hook_still_releases_the_mutex_before_resetting():
    calls = _calls_in(_method('_on_sim_workflow_finished'))
    assert '_on_workflow_finished' in calls, 'on_workflow would never be released'
    assert '_reset_sim_scene' in calls


def test_stop_resets_only_on_the_idle_branch_and_nowhere_else():
    """PLACEMENT, not a count — a count passes with both resets in one branch.

    The idle branch IS the „Simulator zurücksetzen" button (the React side reuses
    /workflow/stop so the reset needs no new .srv). The RUNNING branch must NOT
    reset: `stop()`'s joins are timeout-bounded and `is_running` goes False the
    moment the stop event is set, so any guard there is a tautology that can let
    a reset race a still-live daemon. `_on_sim_workflow_finished` owns that path.
    """
    fn = _method('workflow_stop_callback')

    idle_branch = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.ops[0], ast.Is)
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value is None):
            idle_branch = node
            break
    assert idle_branch is not None, 'the `if manager is None:` branch is gone'

    def resets_in(tree):
        return [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == '_reset_sim_scene']

    assert len(resets_in(fn)) == 1, 'exactly one reset call in the stop callback'
    assert len(resets_in(idle_branch)) == 1, 'and it must be the idle branch'


def test_the_stop_callback_never_reads_is_running_as_a_daemon_liveness_test():
    """`is_running` is False as soon as `_stop_event` is set, so `not
    manager.is_running` after `stop()` proves nothing about the daemon. Guarding
    anything on it reads as a liveness check and is not one."""
    fn = _method('workflow_stop_callback')
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        guard = ast.unparse(node.test)
        assert 'is_running' not in guard, (
            f'`{guard}` guards on is_running — it is a tautology after stop()')


def test_boot_brings_the_sim_publisher_up_and_seeds_it():
    """Without this the topic does not exist until the first sim run, so the twin
    has no pose AND — since it paints on demand — no render pump at all."""
    init = _method('__init__')
    calls = _calls_in(init)
    assert '_ensure_sim_publisher' in calls
    assert '_seed_sim_rest_pose' in calls


def test_the_boot_seed_runs_after_everything_it_reads():
    """ORDER, not just presence — and this one fails SILENTLY if it regresses.

    The seed is wrapped in a best-effort try/except (a simulator convenience must
    never stop the node booting), so hoisting it above `_init_core_components`
    (which creates `_sim_joint_state_publisher` / `_last_sim_joints` / the sim
    handles) or above the `_arm_profile` hoist (which `_sim_home_full_joints` and
    `_sim_joint_names` read) turns it into an AttributeError swallowed as one log
    line — and the twin goes dark again with every test still green.
    """
    init = _method('__init__')

    def line_of_call(attr):
        for node in ast.walk(init):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == attr):
                return node.lineno
        raise AssertionError(f'{attr}() is no longer called from __init__')

    def line_of_assign(attr):
        for node in ast.walk(init):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == attr:
                    return node.lineno
        raise AssertionError(f'self.{attr} is no longer assigned in __init__')

    seed = line_of_call('_seed_sim_rest_pose')
    assert line_of_call('_ensure_sim_publisher') < seed
    assert line_of_call('_init_core_components') < seed, 'sim handles not created yet'
    assert line_of_assign('_arm_profile') < seed, 'no profile → nothing to seed'
    # And bounded from BELOW: CLAUDE.md's Rule D1 requires _init_robot_profile to
    # be the LAST statement of __init__, so the seed may not slip past it.
    assert seed < line_of_call('_init_robot_profile'), (
        '_init_robot_profile must stay the last statement of __init__')


# ── the wire shape of what the seed and the idle republish emit ──────────────

def test_a_name_position_mismatch_is_refused_not_published():
    """The boot seed runs BEFORE `_init_robot_profile`, whose exception path
    downgrades `_arm_profile` to the omx_full default. An edu6 rig booting
    degraded therefore seeds a 7-vector and then resolves 6 OMX names — and
    `_sim_idle_republish` would re-emit that malformed JointState at 2 Hz for the
    life of the container. Refuse it instead, and say why once."""
    node = _PublishStubNode(profile=_profile('omx_full'))  # 6 names
    node._publish_sim_joint_state([0.0] * 7)              # 7 positions

    assert node._sim_joint_state_publisher.messages == []
    assert node._last_sim_joints is None, 'a refused pose must not be cached'
    assert node._logger.warnings and 'joint names' in node._logger.warnings[-1]


# From the REGISTRY, never a restated list: a hardcoded triple stopped
# covering `edu1_studio` the day it was added, silently.
@pytest.mark.parametrize('profile_id',
                         sorted(robot_profiles.ROBOT_PROFILES))
def test_the_matching_case_still_publishes_every_profile(profile_id):
    profile = _profile(profile_id)
    node = _PublishStubNode(profile=profile)
    q = _home_full(profile)
    node._publish_sim_joint_state(q)

    sent = node._sim_joint_state_publisher.messages
    assert len(sent) == 1
    assert list(sent[0].name) == list(profile.joint_names)
    assert list(sent[0].position) == pytest.approx(q)
    assert node._last_sim_joints == pytest.approx(q)


# ── the /sim/* stream while SimArm plays in real time (2026-09-11) ───────────
# SimArm's player thread emits every waypoint at its own time_from_start through
# `_publish_sim_frame`; the heartbeat fills the gaps when the arm rests. These
# bind the REAL publish helpers to a stub self with recording publishers.

class _FakeTime:
    """rclpy Time stand-in: seconds as a float, `- Duration`, `.nanoseconds`."""

    def __init__(self, s):
        self.s = s

    def __sub__(self, dur):
        return _FakeTime(self.s - dur.nanoseconds / 1e9)

    @property
    def nanoseconds(self):
        return int(round(self.s * 1e9))


class _FakeDuration:
    def __init__(self, nanoseconds=0):
        self.nanoseconds = nanoseconds


class _FakeClock:
    def __init__(self, s=100.0):
        self.s = s

    def now(self):
        return _FakeTime(self.s)


@pytest.fixture
def _stub_rclpy_duration(monkeypatch):
    import sys
    import types
    rclpy_mod = types.ModuleType('rclpy')
    dur_mod = types.ModuleType('rclpy.duration')
    dur_mod.Duration = _FakeDuration
    rclpy_mod.duration = dur_mod
    monkeypatch.setitem(sys.modules, 'rclpy', rclpy_mod)
    monkeypatch.setitem(sys.modules, 'rclpy.duration', dur_mod)
    yield


@pytest.fixture(autouse=True)
def _stub_std_msgs(monkeypatch):
    import sys
    import types

    class _String:
        def __init__(self):
            self.data = ''

    pkg = types.ModuleType('std_msgs')
    msgmod = types.ModuleType('std_msgs.msg')
    msgmod.String = _String
    pkg.msg = msgmod
    monkeypatch.setitem(sys.modules, 'std_msgs', pkg)
    monkeypatch.setitem(sys.modules, 'std_msgs.msg', msgmod)
    yield


class _StreamNode:
    """The real stream helpers on a stub self: recording publishers for both
    topics, a live SimWorld, and the lock + clock the real __init__ creates."""

    _SIM_JOINT_NAMES = _PublishStubNode._SIM_JOINT_NAMES
    _SIM_HEARTBEAT_QUIET_S = _node_class_constant('_SIM_HEARTBEAT_QUIET_S')
    _SIM_STAMP_REORDER_S = _node_class_constant('_SIM_STAMP_REORDER_S')

    def __init__(self, profile, world=None):
        self._arm_profile = profile
        self._sim_joint_state_publisher = _RecordingPublisher()
        self._sim_objects_publisher = _RecordingPublisher()
        self._sim_world = world
        self._sim_arm = None
        self._sim_objects = []
        self._last_sim_objects_json = None
        self._last_sim_joints = None
        self._sim_pub_lock = threading.RLock()
        self._last_sim_publish_mono = 0.0
        self._last_sim_stamp_ns = 0
        self.on_workflow = True        # mid-run: the heartbeat must NOT care
        self._clock = _FakeClock()
        self._logger = _Logger()

    def get_logger(self):
        return self._logger

    def get_clock(self):
        return self._clock

    def poses(self):
        return [list(m.position) for m in self._sim_joint_state_publisher.messages]

    def scenes(self):
        return [json.loads(m.data) for m in self._sim_objects_publisher.messages]

    _sim_joint_names = _FN['_sim_joint_names']
    _sim_home_full_joints = _FN['_sim_home_full_joints']
    _publish_sim_joint_state = _FN['_publish_sim_joint_state']
    _publish_sim_frame = _FN['_publish_sim_frame']
    _publish_sim_objects = _FN['_publish_sim_objects']
    _sim_idle_republish = _FN['_sim_idle_republish']
    _reset_sim_scene = _FN['_reset_sim_scene']
    _stamp_sim_joint_state = _FN['_stamp_sim_joint_state']
    _publish_sim_teleport = _FN['_publish_sim_teleport']
    _SIM_RESET_HOLD_S = _node_class_constant('_SIM_RESET_HOLD_S')

    def stamps(self):
        return [m.header.stamp.sec + m.header.stamp.nanosec / 1e9
                for m in self._sim_joint_state_publisher.messages]


_CUBE = [{'type': 'wuerfel', 'tag_id': 7, 'x': 0.15, 'y': 0.0, 'yaw': 0.0}]


def test_the_heartbeat_keeps_a_PAUSED_run_alive():
    """*Kills:* restoring the `on_workflow` early return.

    A run only streams while the arm moves. A „warte"-Block or a breakpoint left
    /sim/joint_states silent and after 3 s the twin's staleness watchdog put
    „Wartet auf Gelenkdaten …" over a working simulator. Mid-run, quiet stream →
    the heartbeat re-sends the last pose."""
    profile = _profile()
    node = _StreamNode(profile)
    node._publish_sim_joint_state(_home_full(profile))
    node._last_sim_publish_mono = time.monotonic() - 1.0     # quiet for 1 s
    assert node.on_workflow is True
    node._sim_idle_republish()
    assert len(node.poses()) == 2
    assert node.poses()[-1] == pytest.approx(_home_full(profile))


def test_the_heartbeat_stays_silent_while_frames_are_flowing():
    """While the player emits a frame every ~33 ms a heartbeat repeat of an
    older pose would make the twin stutter backwards."""
    profile = _profile()
    node = _StreamNode(profile)
    node._publish_sim_frame(_home_full(profile))        # just now
    node._sim_idle_republish()
    assert len(node.poses()) == 1


def test_the_heartbeat_resends_the_last_PUBLISHED_scene_never_the_live_world():
    """During a run the world is mutated a whole chunk before that chunk is
    played. A heartbeat that snapshotted the LIVE world at the start of a chunk
    announced a grasp before the twin's jaws reached the cube."""
    profile = _profile()
    world = SimWorld(_CUBE)
    world.bind_tag(0, 20)
    node = _StreamNode(profile, world=world)
    node._publish_sim_objects(force=True)                 # scene as played: free
    world.capture_nearest(0.15, 0.0, 0.06)                # the NEXT chunk's future
    node._last_sim_joints = _home_full(profile)
    node._last_sim_publish_mono = time.monotonic() - 1.0
    node._sim_idle_republish()
    scenes = node.scenes()
    assert len(scenes) == 2
    assert scenes[-1]['held'] is None, 'the heartbeat leaked the live world'


def test_a_frame_publishes_ITS_OWN_scene_and_no_scene_when_unchanged():
    profile = _profile()
    world = SimWorld(_CUBE)
    world.bind_tag(0, 20)
    node = _StreamNode(profile, world=world)
    frame_scene = world.snapshot()
    world.capture_nearest(0.15, 0.0, 0.06)                # live world moves on
    node._publish_sim_frame(_home_full(profile), frame_scene, 0.0)
    assert node.scenes() == [frame_scene], 'a frame must carry its own snapshot'
    node._publish_sim_frame(_home_full(profile), None, 0.0)
    assert len(node.scenes()) == 1, 'an unchanged frame publishes no scene'
    assert len(node.poses()) == 2


def test_a_late_frame_is_stamped_with_the_time_it_was_VALID(_stub_rclpy_duration):
    """The player wakes a little behind schedule; the stamp is back-dated by
    exactly that lateness so the twin interpolates on the trajectory's own
    timeline, not on thread-scheduling noise."""
    profile = _profile()
    node = _StreamNode(profile)
    node._clock.s = 100.0
    node._publish_sim_frame(_home_full(profile), None, 0.004)
    node._clock.s = 100.010
    node._publish_sim_frame(_home_full(profile), None, 0.0)
    assert node.stamps() == [pytest.approx(99.996, abs=1e-9),
                             pytest.approx(100.010, abs=1e-9)]


def test_the_heartbeat_cannot_pin_a_stale_pose_over_a_reset():
    """The race the `on_workflow` gate never closed (`_on_sim_workflow_finished`
    clears the flag BEFORE it resets): heartbeat reads the old pose, the reset
    publishes HOME, the heartbeat then publishes the old pose — and every later
    tick re-sends it. Under `_sim_pub_lock` the heartbeat either finishes before
    the reset or sees the reset's publish as "not quiet"."""
    profile = _profile()
    objects = list(_CUBE)
    world, arm = _sim_stack(profile, objects)
    node = _StreamNode(profile, world=world)
    node._sim_arm = arm
    node._sim_objects = objects
    node.on_workflow = False               # cleared just before the reset runs
    old = [0.3, -1.0, 0.8, 0.2, 0.1, -0.4]
    node._publish_sim_joint_state(old)
    node._last_sim_publish_mono = time.monotonic() - 1.0

    # Force the exact interleaving: the heartbeat has READ the old pose and is
    # building its message (get_clock) when the reset runs on another thread.
    # Without the lock the reset completes inside this window and the heartbeat
    # then publishes the old pose on top of HOME; with it, the reset waits.
    real_clock = node._clock
    reset_done = threading.Event()
    fired = []
    heartbeat_thread = []

    class _HookClock:
        def now(self):
            if (heartbeat_thread and threading.current_thread() is heartbeat_thread[0]
                    and not fired):
                fired.append(True)
                threading.Thread(
                    target=lambda: (node._reset_sim_scene(), reset_done.set())
                ).start()
                reset_done.wait(0.3)
            return real_clock.now()

    node._clock = _HookClock()
    hb = threading.Thread(target=node._sim_idle_republish)
    heartbeat_thread.append(hb)
    hb.start()
    hb.join(2.0)
    assert reset_done.wait(2.0), 'the reset never finished'
    assert fired, 'the heartbeat never reached its publish'
    assert node.poses()[-1] == pytest.approx(_home_full(profile)), (
        'a stale pose was published over the reset')


def test_the_node_builds_the_sim_arm_with_the_REAL_TIME_sink():
    """*Kills:* reverting the node to `joint_state_sink=` — the burst path, one
    second of motion every second."""
    fn = _method('_get_or_create_sim_workflow_manager')
    ctor = [n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == 'SimArm']
    assert len(ctor) == 1
    kw = {k.arg: ast.unparse(k.value) for k in ctor[0].keywords}
    assert kw.get('frame_sink') == 'self._publish_sim_frame'
    assert 'joint_state_sink' not in kw, 'the burst sink must not be wired too'


def test_the_heartbeat_holds_the_lock_across_its_check_AND_its_publishes():
    """Structure, because the race above is a matter of where the lock sits: a
    quiet check outside the hold would reopen it."""
    fn = _method('_sim_idle_republish')
    withs = [n for n in fn.body if isinstance(n, ast.With)]
    assert len(withs) == 1, 'the whole heartbeat body must be one `with` block'
    assert ast.unparse(withs[0].items[0].context_expr) == 'self._sim_pub_lock'
    calls = {n.func.attr for n in ast.walk(withs[0])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert {'_publish_sim_objects', '_publish_sim_joint_state'} <= calls
    reads = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert 'on_workflow' not in reads, (
        'the heartbeat must not be gated on on_workflow — a paused run goes stale')


def test_the_heartbeat_and_stamp_constants_are_the_designed_ones():
    """The heartbeat must stay silent while frames flow (~33 ms apart) yet still
    give an idle twin 2 Hz from its 0.5 s timer; the reorder window must cover a
    lock wait (ms) and stay far below a real clock step."""
    quiet = _node_class_constant('_SIM_HEARTBEAT_QUIET_S')
    assert quiet == 0.45
    assert 0.033 * 10 < quiet < 0.5
    assert _node_class_constant('_SIM_STAMP_REORDER_S') == 0.05


def test_stamps_leave_in_publish_order_even_for_a_late_frame(_stub_rclpy_duration):
    """A player frame that waited for the lock while the heartbeat held it is
    back-dated BELOW the heartbeat's stamp; the twin drops a sample older than
    its newest, so it would vanish. Within the reorder window it is nudged 1 µs
    past the previous stamp instead."""
    profile = _profile()
    node = _StreamNode(profile)
    node._clock.s = 100.000
    node._publish_sim_joint_state(_home_full(profile))        # the heartbeat
    node._clock.s = 100.002
    node._publish_sim_frame(_home_full(profile), None, 0.004)  # due at 99.998
    s = node.stamps()
    assert s[1] > s[0], 'a stamp went backwards — the twin would drop it'
    assert s[1] == pytest.approx(100.000001, abs=1e-9)


def test_a_real_clock_step_back_is_passed_on_not_squeezed(_stub_rclpy_duration):
    """Seconds of samples nudged onto one instant would merge in the twin; a
    genuine step back must reach it so it can start a new timeline."""
    profile = _profile()
    node = _StreamNode(profile)
    node._clock.s = 100.0
    node._publish_sim_joint_state(_home_full(profile))
    node._clock.s = 97.5                                       # WSL2 resync
    node._publish_sim_joint_state(_home_full(profile))
    assert node.stamps() == [pytest.approx(100.0), pytest.approx(97.5)]


def test_the_stamp_is_taken_INSIDE_the_publish_hold():
    """Taken before the hold, a reset's HOME could be stamped earlier than a
    heartbeat that won the lock, and the twin dropped HOME as out of order."""
    fn = _method('_publish_sim_joint_state')
    holds = [n for n in ast.walk(fn) if isinstance(n, ast.With)
             and ast.unparse(n.items[0].context_expr) == 'self._sim_pub_lock']
    assert len(holds) == 1
    inside = {n.func.attr for n in ast.walk(holds[0])
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert '_stamp_sim_joint_state' in inside
    outside_calls = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr in ('get_clock', '_stamp_sim_joint_state')
                     and not any(n is m for m in ast.walk(holds[0]))]
    assert outside_calls == [], 'a stamp is taken outside the hold'


def test_the_reset_is_published_as_a_TELEPORT_not_a_move(_stub_rclpy_duration):
    """*Kills:* publishing the Grundstellung as one lone pose.

    The twin blends between samples unless the gap or the implied speed says the
    data jumped. After a motionless tail the reset lands ~90-130 ms behind the
    last pose, and a jump of a radian or two over that gap is under the twin's
    speed limit — measured, it drew a 5-6 frame sweep through poses the virtual
    arm never took. The reset therefore re-sends the pose being shown, back-dated
    `_SIM_RESET_HOLD_S`, so the jump itself sits behind a 3 ms gap."""
    profile = _profile()
    objects = list(_CUBE)
    world, arm = _sim_stack(profile, objects)
    node = _StreamNode(profile, world=world)
    node._sim_arm = arm
    node._sim_objects = objects
    resting = [0.3, -1.0, 0.8, 0.2, 0.1, -0.4]
    node._clock.s = 100.0
    node._publish_sim_frame(resting)              # where the run ended
    node._clock.s = 100.120                       # …a motionless tail later
    node._reset_sim_scene()

    poses, stamps = node.poses(), node.stamps()
    assert poses[0] == pytest.approx(resting)
    assert poses[1] == pytest.approx(resting), 'the shown pose must be re-sent'
    assert poses[2] == pytest.approx(_home_full(profile))
    hold = _node_class_constant('_SIM_RESET_HOLD_S')
    assert stamps[1] == pytest.approx(100.120 - hold, abs=1e-9)
    assert stamps[2] == pytest.approx(100.120, abs=1e-9)
    # The jump sits behind a gap no joint speed can fill (the twin's rule is
    # 20 rad/s; the widest joint step here is ~1.3 rad).
    gap = stamps[2] - stamps[1]
    assert gap == pytest.approx(hold, abs=1e-9)
    widest = max(abs(a - b) for a, b in zip(poses[1], poses[2]))
    assert widest > 20.0 * gap


def test_a_reset_that_changes_nothing_publishes_one_pose(_stub_rclpy_duration):
    """No hold when the twin is already showing the Grundstellung — there is no
    jump to fence, and a duplicate would be noise."""
    profile = _profile()
    objects = list(_CUBE)
    world, arm = _sim_stack(profile, objects)
    node = _StreamNode(profile, world=world)
    node._sim_arm = arm
    node._sim_objects = objects
    node._publish_sim_frame(_home_full(profile))
    node._reset_sim_scene()
    assert len(node.poses()) == 2
    assert node.poses()[1] == pytest.approx(_home_full(profile))
