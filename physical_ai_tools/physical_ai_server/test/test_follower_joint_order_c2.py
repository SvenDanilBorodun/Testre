"""C2 — Communicator.get_latest_follower_joints() must return the follower
joint vector in the CANONICAL order (joint1..joint5, gripper_joint_1)
regardless of the /joint_states publish order.

The broadcaster commonly publishes joints NAME-SORTED
([gripper_joint_1, joint1..joint5]); reading msg.position in MESSAGE order
scrambled the vector, which lurched the first workflow move and yielded
the wrong z_table at touch-off FK. The method now reorders by msg.name.

communicator.py imports ROS message modules at import time; we stub those
(same approach as the collision-monitor contract test) and load the module
under a throwaway name so the real physical_ai_server package is untouched.
We bypass the heavy __init__ via object.__new__ and drive the method
directly off a hand-built follower_topic_msgs map.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMM_PATH = (
    REPO_ROOT / 'physical_ai_server' / 'communication' / 'communicator.py'
)


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _install_stubs():
    _ph = type('_Placeholder', (), {})
    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=_ph)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=_ph)
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', BrowserItem=_ph, DatasetInfo=_ph, TaskStatus=_ph)
    _stub('physical_ai_interfaces.srv',
          BrowseFile=_ph, EditDataset=_ph, GetDatasetInfo=_ph, GetImageTopicList=_ph)
    _stub('rclpy')
    _stub('rclpy.node', Node=_ph)
    _stub('rclpy.qos',
          HistoryPolicy=types.SimpleNamespace(KEEP_LAST=1),
          QoSProfile=lambda **kw: kw,
          ReliabilityPolicy=types.SimpleNamespace(RELIABLE=1, BEST_EFFORT=2))
    _stub('rclpy.callback_groups', MutuallyExclusiveCallbackGroup=_ph)
    _stub('rosbag_recorder')
    _stub('rosbag_recorder.srv', SendCommand=_ph)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', CompressedImage=_ph, JointState=_ph)
    _stub('std_msgs')
    _stub('std_msgs.msg', String=_ph)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=_ph)

    # Internal physical_ai_server LEAF modules the communicator imports that
    # cannot import in CI (they need rclpy / pandas). Register ONLY these exact
    # dotted names — we deliberately do NOT stub the `physical_ai_server` package
    # or its real subpackages, so other tests in the same pytest session keep
    # importing physical_ai_server.workflow.* against the real package.
    _stub('physical_ai_server.communication.multi_subscriber', MultiSubscriber=_ph)
    _stub('physical_ai_server.data_processing.data_editor', DataEditor=_ph)
    _stub('physical_ai_server.utils.parameter_utils',
          parse_topic_list=lambda *_a, **_k: {},
          parse_topic_list_with_names=lambda *_a, **_k: {})


def _load_communicator():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        '_edubotics_communicator_test', COMM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Communicator


Communicator = _load_communicator()


class _JointStateMsg:
    def __init__(self, name, position):
        self.name = list(name)
        self.position = list(position)


def _bare_comm(follower_msgs):
    """Build a Communicator without running __init__, set just the field the
    method reads."""
    c = object.__new__(Communicator)
    c.follower_topic_msgs = follower_msgs
    return c


# The broadcaster's common NAME-SORTED publish order (the bug trigger).
_SORTED_NAMES = ['gripper_joint_1', 'joint1', 'joint2', 'joint3', 'joint4', 'joint5']
# Distinct sentinel values so a scramble is obvious.
_SORTED_POS = [0.6, 0.1, 0.2, 0.3, 0.4, 0.5]
# What canonical order must yield: joint1..joint5, gripper_joint_1.
_CANONICAL = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]


def test_name_sorted_message_is_reordered_to_canonical():
    msg = _JointStateMsg(_SORTED_NAMES, _SORTED_POS)
    c = _bare_comm({'follower': msg})
    out = c.get_latest_follower_joints()
    assert out == _CANONICAL


def test_already_canonical_message_is_passed_through_unchanged():
    msg = _JointStateMsg(
        ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1'],
        _CANONICAL,
    )
    c = _bare_comm({'follower': msg})
    assert c.get_latest_follower_joints() == _CANONICAL


def test_arbitrary_order_is_reordered_by_name():
    names = ['joint3', 'joint1', 'gripper_joint_1', 'joint5', 'joint2', 'joint4']
    pos = [0.3, 0.1, 0.6, 0.5, 0.2, 0.4]
    c = _bare_comm({'follower': _JointStateMsg(names, pos)})
    assert c.get_latest_follower_joints() == _CANONICAL


def test_partial_message_missing_joint_fails_loud_returns_none():
    # gripper_joint_1 missing → must NOT silently zero-fill; return None so
    # FK / the workflow seed never consume a bogus pose.
    names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5']
    pos = [0.1, 0.2, 0.3, 0.4, 0.5]
    c = _bare_comm({'follower': _JointStateMsg(names, pos)})
    assert c.get_latest_follower_joints() is None


def test_mismatched_name_position_lengths_returns_none():
    c = _bare_comm({'follower': _JointStateMsg(_SORTED_NAMES, _SORTED_POS[:4])})
    assert c.get_latest_follower_joints() is None


def test_no_message_returns_none():
    c = _bare_comm({'follower': None})
    assert c.get_latest_follower_joints() is None


def test_mobile_follower_is_skipped():
    # A mobile (Odometry) follower entry must be skipped; the arm follower
    # still resolves canonically.
    arm = _JointStateMsg(_SORTED_NAMES, _SORTED_POS)
    c = _bare_comm({'follower_mobile': object(), 'follower': arm})
    assert c.get_latest_follower_joints() == _CANONICAL
