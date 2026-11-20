# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""``EDUBOTICS_SCENE_CAMERA_PROFILE`` has ONE value table, and every reader of
the key runs it (spec 40 S1, decision D63).

The key selects the scene camera's profile: ``tower_v1`` after stripping
blanks and quotes and lower-casing, ``legacy`` for anything else and for its
absence. It is read in several places of the arm container, in Python and in
shell, and a reader that disagreed would put two halves of one robot on two
profiles: the camera on the tower while the arm container states ``legacy``
(the tower guard's scope reads that statement, D63), or the reverse.

``READERS`` maps each reader to a function of the container environment (a
dict; a missing key is the unset variable) answering the profile it selects.
Today it holds the readers that exist: ``scene_split.normalise_profile``
(the derivation's own, as the ingest node calls it on
``os.environ.get(...)``), ``activation_agent.scene_profile_from_env`` (the
statement on ``/edubotics/scene_camera_profile``, WP-PROF), the entrypoint's
shell function ``scene_slot_uses_tower_node`` (which hands the scene slot to
the tower node, WP-07; run under bash with the loop's own argument) and the
Orange Pi agent's ``config_generator.scene_camera_profile_of`` (what the
System tab shows and the start pre-flight judges, WP-07) and the V1 driver's
``feetech_arm_node.scene_profile_from_env`` (the scope of its tower check and
of ``/edubotics/tower_state``, WP-T1; compiled out of the driver, which
imports rclpy, with the real ``scene_split``).
"""

import ast
import importlib.util
import os
import sys
import unittest

from test_activation_agent import _load_agent_namespace
from test_entrypoint_scene_slot import entrypoint_profile

_SETUP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _SETUP_DIR not in sys.path:
    sys.path.insert(0, _SETUP_DIR)
from pi_agent import config_generator as pi_config_generator  # noqa: E402

_ARM_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'docker', 'open_manipulator'))
_DRIVER = os.path.join(_ARM_DIR, 'feetech_arm_node.py')
KEY = 'EDUBOTICS_SCENE_CAMERA_PROFILE'

# (label, the container environment, the profile every reader must select)
TABLE = (
    ('tower_v1', {KEY: 'tower_v1'}, 'tower_v1'),
    ('quoted, blanks, mixed case', {KEY: ' "Tower_V1" '}, 'tower_v1'),
    ('legacy', {KEY: 'legacy'}, 'legacy'),
    ('empty', {KEY: ''}, 'legacy'),
    ('unset', {}, 'legacy'),
    ('tower', {KEY: 'tower'}, 'legacy'),
    ('TOWER_V2', {KEY: 'TOWER_V2'}, 'legacy'),
)


def _load_scene_split():
    spec = importlib.util.spec_from_file_location(
        'scene_split_for_value_table', os.path.join(_ARM_DIR, 'scene_split.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SPLIT = _load_scene_split()
_AGENT = _load_agent_namespace()


def _load_driver_namespace():
    """``feetech_arm_node``'s reader and the key's name, executed out of the
    driver's AST with the real ``scene_split`` (the module imports rclpy)."""
    with open(_DRIVER, encoding='utf-8') as handle:
        tree = ast.parse(handle.read())
    ns = {'scene_split': _SPLIT}
    for node in tree.body:
        wanted = (isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'SCENE_PROFILE_ENV' for t in node.targets)) or (
            isinstance(node, ast.FunctionDef) and node.name == 'scene_profile_from_env')
        if wanted:
            exec(compile(ast.Module([node], []), _DRIVER, 'exec'), ns)  # noqa: S102
    return ns


_DRIVER_NS = _load_driver_namespace()


def _normalise_profile(env):
    return _SPLIT.normalise_profile(env.get(KEY))


def _agent(env):
    reader = _AGENT.get('scene_profile_from_env')
    if reader is None:
        raise AssertionError('activation_agent.py defines no scene_profile_from_env')
    return reader(dict(env))


def _pi_agent(env):
    reader = getattr(pi_config_generator, 'scene_camera_profile_of', None)
    if reader is None:
        raise AssertionError('pi_agent/config_generator.py defines no scene_camera_profile_of')
    return reader(env.get(KEY))


def _driver(env):
    reader = _DRIVER_NS.get('scene_profile_from_env')
    if reader is None:
        raise AssertionError('feetech_arm_node.py defines no scene_profile_from_env')
    return reader(dict(env))


READERS = {
    'scene_split.normalise_profile': _normalise_profile,
    'activation_agent.scene_profile_from_env': _agent,
    'entrypoint_omx.sh scene_slot_uses_tower_node': entrypoint_profile,
    'pi_agent config_generator.scene_camera_profile_of': _pi_agent,
    'feetech_arm_node.scene_profile_from_env': _driver,
}


class OneValueTable(unittest.TestCase):

    def test_every_reader_selects_the_tables_profile(self):
        for name, reader in READERS.items():
            for label, env, expected in TABLE:
                with self.subTest(reader=name, value=label):
                    self.assertEqual(reader(env), expected)

    def test_the_table_reaches_both_profiles(self):
        # A table whose every row answered `legacy` would let a reader that
        # never selects the tower pass.
        self.assertEqual({expected for _label, _env, expected in TABLE},
                         set(_SPLIT.PROFILE_VALUES.values()))


class ThePythonReadersAgreeBeyondTheTable(unittest.TestCase):
    """The agent keeps its own stdlib copy of the rule (it imports neither
    numpy nor cv2, and scene_split's pixel functions import both), so its
    strip set is compared on values the shared table leaves out: a bare
    ``str.strip()`` would also strip a no-break space, where
    ``normalise_profile`` keeps it and answers ``legacy``."""

    VALUES = ("'tower_v1'", '\ttower_v1\r\n', 'TOWER_V1', '""', "' \"tower_v1\" '",
              '\xa0tower_v1', 'tower_v1 ', 'tower_v1x', 'tower v1',
              'tower_v1;legacy', 'LEGACY', 'auto')

    def test_the_agent_answers_as_normalise_profile(self):
        for raw in self.VALUES:
            env = {KEY: raw}
            with self.subTest(value=raw):
                self.assertEqual(_agent(env), _normalise_profile(env))

    def test_the_pi_agent_answers_as_normalise_profile(self):
        # The Pi agent keeps a stdlib copy too (it ships without scene_split).
        for raw in self.VALUES:
            env = {KEY: raw}
            with self.subTest(value=raw):
                self.assertEqual(_pi_agent(env), _normalise_profile(env))

    def test_the_v1_driver_answers_as_normalise_profile(self):
        for raw in self.VALUES:
            env = {KEY: raw}
            with self.subTest(value=raw):
                self.assertEqual(_driver(env), _normalise_profile(env))


if __name__ == '__main__':
    unittest.main()
