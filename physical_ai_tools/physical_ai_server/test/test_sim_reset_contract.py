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
import pathlib

import pytest

from physical_ai_server import robot_profiles
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_world import SimWorld


_NODE = (pathlib.Path(__file__).resolve().parents[1]
         / 'physical_ai_server' / 'physical_ai_server.py')
_SRC = _NODE.read_text(encoding='utf-8')
_TREE = ast.parse(_SRC)

_HELPERS = ('_sim_home_full_joints', '_seed_sim_rest_pose', '_reset_sim_scene',
            '_sim_joint_names', '_publish_sim_joint_state')


def _load_helpers():
    """Compile the three node helpers out of the source and return them as plain
    functions, so they can be bound to a stub self and actually EXERCISED."""
    wanted = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name in _HELPERS:
            wanted[node.name] = node
    missing = set(_HELPERS) - set(wanted)
    assert not missing, f'node helper(s) renamed or removed: {sorted(missing)}'
    module = ast.Module(body=[wanted[n] for n in _HELPERS], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {}
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

    def _publish_sim_joint_state(self, q):
        self.published_poses.append(list(q))
        self._last_sim_joints = [float(v) for v in q]

    def _publish_sim_objects(self, force=False):
        if force:
            self.forced_object_publishes += 1

    # The real methods, bound.
    _sim_home_full_joints = _FN['_sim_home_full_joints']
    _seed_sim_rest_pose = _FN['_seed_sim_rest_pose']
    _reset_sim_scene = _FN['_reset_sim_scene']


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
        self._logger = _Logger()

    def get_logger(self):
        return self._logger

    _sim_joint_names = _FN['_sim_joint_names']
    _publish_sim_joint_state = _FN['_publish_sim_joint_state']
    _publish_sim_objects = lambda self, force=False: None  # noqa: E731


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
            self.header = types.SimpleNamespace(stamp=None)
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

@pytest.mark.parametrize('profile_id', ['omx_full', 'omx_follower', 'edu6_studio'])
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


@pytest.mark.parametrize('profile_id', ['omx_full', 'omx_follower', 'edu6_studio'])
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
