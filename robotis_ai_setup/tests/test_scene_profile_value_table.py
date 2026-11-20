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
Today it holds the two readers that exist: ``scene_split.normalise_profile``
(the derivation's own, as the ingest node calls it on
``os.environ.get(...)``) and ``activation_agent.scene_profile_from_env`` (the
statement on ``/edubotics/scene_camera_profile``, WP-PROF). The entrypoint's
shell function (WP-07) and the V1 driver's reading (WP-T1) each join as one
more entry.
"""

import importlib.util
import os
import unittest

from test_activation_agent import _load_agent_namespace

_ARM_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'docker', 'open_manipulator'))
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


def _normalise_profile(env):
    return _SPLIT.normalise_profile(env.get(KEY))


def _agent(env):
    reader = _AGENT.get('scene_profile_from_env')
    if reader is None:
        raise AssertionError('activation_agent.py defines no scene_profile_from_env')
    return reader(dict(env))


READERS = {
    'scene_split.normalise_profile': _normalise_profile,
    'activation_agent.scene_profile_from_env': _agent,
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


if __name__ == '__main__':
    unittest.main()
