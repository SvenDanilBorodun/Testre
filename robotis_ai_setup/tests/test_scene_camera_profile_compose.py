# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-05: ``EDUBOTICS_SCENE_CAMERA_PROFILE`` reaches the arm container, and
only the arm container (spec 40 S1, decision D32).

The key is forwarded to ``open_manipulator`` in the student compose, the
Orange Pi compose and the Pi agent's shipped twin of it, with the default
``legacy`` (today's camera path), and on no other service: the server learns
the profile from the arm container's statement, never from its own
environment (G9). ``ci.yml::env-forwarding-guard`` unions every compose file,
so it cannot see a key that reaches the wrong service or one target only;
``compose-env-parity`` compares two files and not the twin. This test reads the
three files per service with that guard's own parser.
"""

import importlib.util
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_KEY = 'EDUBOTICS_SCENE_CAMERA_PROFILE'
_COMPOSES = {
    'student': _REPO / 'robotis_ai_setup' / 'docker' / 'docker-compose.yml',
    'opi': _REPO / 'robotis_ai_setup' / 'docker' / 'docker-compose.opi.yml',
    'pi agent twin': _REPO / 'robotis_ai_setup' / 'pi_agent' / 'docker' / 'docker-compose.opi.yml',
}
_GPU = _REPO / 'robotis_ai_setup' / 'docker' / 'docker-compose.gpu.yml'
_ARM_DIR = _REPO / 'robotis_ai_setup' / 'docker' / 'open_manipulator'
_INGEST = _ARM_DIR / 'camera_ingest_node.py'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PARITY = _load('compose_env_parity_for_scene_profile',
                _REPO / '.github' / 'scripts' / 'compose_env_parity.py')
_SPLIT = _load('scene_split_for_compose_tests', _ARM_DIR / 'scene_split.py')


def _services(path):
    return _PARITY.parse_compose(path, str(path.relative_to(_REPO)))


class TheProfileKeyReachesTheArmContainerOnly(unittest.TestCase):

    def test_it_sits_on_open_manipulator_with_the_default_legacy(self):
        for name, path in _COMPOSES.items():
            with self.subTest(compose=name):
                env = _services(path)['open_manipulator']
                self.assertIn(_KEY, env, f'{name}: open_manipulator does not forward {_KEY}')
                value, _line = env[_KEY]
                self.assertEqual(value, '${EDUBOTICS_SCENE_CAMERA_PROFILE:-legacy}')
                default = re.fullmatch(r'\$\{EDUBOTICS_SCENE_CAMERA_PROFILE:-(.*)\}', value).group(1)
                self.assertEqual(_SPLIT.normalise_profile(default), _SPLIT.PROFILE_LEGACY)

    def test_no_other_service_carries_it(self):
        for name, path in _COMPOSES.items():
            services = _services(path)
            self.assertIn('physical_ai_server', services)
            for service, env in services.items():
                if service == 'open_manipulator':
                    continue
                with self.subTest(compose=name, service=service):
                    self.assertNotIn(_KEY, env)
        self.assertNotIn(_KEY, _GPU.read_text(encoding='utf-8'))

    def test_the_twin_is_byte_identical_to_the_opi_compose(self):
        self.assertEqual(_COMPOSES['opi'].read_bytes(), _COMPOSES['pi agent twin'].read_bytes())


class EveryKeyTheIngestNodeReadsIsOnItsService(unittest.TestCase):
    """Per service, what the union guard cannot see: every ``EDUBOTICS_*``
    name the ingest node's source names reaches ``open_manipulator`` on both
    platforms."""

    def test_the_ingest_nodes_keys_are_forwarded_to_open_manipulator(self):
        names = set(re.findall(r'EDUBOTICS_[A-Z0-9_]+', _INGEST.read_text(encoding='utf-8')))
        self.assertIn(_KEY, names)
        self.assertIn('EDUBOTICS_CAMERA_NAMES', names)
        for name, path in _COMPOSES.items():
            env = _services(path)['open_manipulator']
            with self.subTest(compose=name):
                self.assertEqual(sorted(names - set(env)), [])


if __name__ == '__main__':
    unittest.main()
