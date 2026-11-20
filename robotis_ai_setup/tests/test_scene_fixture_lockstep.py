#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The fixture contract in lockstep (spec 40 §3, WP-02).

``physical_ai_server/workflow/scene_fixture.py`` is the one source. Held equal
to it here:

* the React copies ``sceneFixture.json`` and ``sceneFixture.cases.json`` — a
  fresh render by ``tools/export_scene_fixture.py --check`` in a child
  interpreter (the contract module is standard library only);
* the reserved tag ids (the mats, the fixture tags, the clip-on targets) and
  every place that names the ids an object may use instead: ``code_rpc``'s
  ``_TAG_ID_MAX`` and its German sentence, ``robot_api``'s kind comment, and
  ``object_catalog``'s refusal (read from the contract, never restated);
* the V1 driver's twin of the tower literals (``docker/open_manipulator/
  feetech_geometry.py``, WP-T1): the arm container cannot import the server
  package, so its tower check carries the contract's values as literals.

The server's own suite pins the block's sha256 and the rules
(``test/test_scene_fixture.py``); this file needs neither ROS nor numpy.
"""

import ast
import json
import pathlib
import re
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'workflow'
FIXTURE_DIR = REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'components' / 'Workshop' / 'fixture'
EXPORT = REPO / 'tools' / 'export_scene_fixture.py'
DRIVER_GEOMETRY = REPO / 'robotis_ai_setup' / 'docker' / 'open_manipulator' / 'feetech_geometry.py'
TAG36H11_IDS = 587          # tag36h11 has ids 0..586

# The V1 driver's tower literal -> where the contract holds it (a name, then keys into its value).
DRIVER_TWINS = {
    'LIFT_M': ('LIFT_M',),
    'LENS_F': ('LENS_F',),
    'TOWER_OBB_F': ('TOWER_OBB_F',),
    'TOWER_LEAN_AT_LENS_M': ('TOWER_LEAN_AT_LENS_M',),
    'TOWER_AXIS_M': ('TOWER_AXIS_M',),
    'TOWER_BUILD_M': ('TOWER_BUILD_M',),
    'TOWER_TOP_ROTATION_M': ('TOWER_TOP_ROTATION_M',),
    'TOWER_SAMPLE_M': ('TOWER_SAMPLE_M',),
    'TOWER_RESIDUAL_DRIVER_M': ('TOWER_RESIDUAL_DRIVER_M',),
    'TOWER_TOP_PARTS': ('TOWER_TOP_PARTS',),
    'TOWER_T1_PARTS': ('TOWER_T1_PARTS',),
    'TOWER_DROOP_DESIGN_ENV_V1_RAD': ('TOWER_DROOP_DESIGN_ENV_RAD', 'v1'),
    'TOWER_DROOP_KAPPA_V1': ('TOWER_DROOP_KAPPA', 'v1'),
    'TOWER_DROOP_TAU_AC_V1_NM': ('TOWER_DROOP_TAU_AC_NM', 'v1'),
    'TOWER_DROOP_LINK_MASSES_V1': ('ARM_V1', 'link_masses'),
}


def _contract():
    """the contract block's assignments, by ``ast.literal_eval`` (no import, no package)."""
    text = (WORKFLOW / 'scene_fixture.py').read_text(encoding='utf-8')
    block = text.split('# ---- BEGIN CONTRACT BLOCK', 1)[1].split('# ---- END CONTRACT BLOCK ----', 1)[0]
    block = block.split('\n', 1)[1]
    out = {}
    for node in ast.parse(block).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                out[target.id] = ast.literal_eval(node.value)
    return out


def _module_constant(path, name):
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in {path.name}')


class SceneFixtureLockstep(unittest.TestCase):
    def test_the_react_copies_are_a_fresh_render(self):
        run = subprocess.run([sys.executable, '-B', str(EXPORT), '--check'], capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        data = json.loads((FIXTURE_DIR / 'sceneFixture.json').read_text(encoding='utf-8'))
        contract = _contract()
        self.assertEqual(set(data['constants']), set(contract))
        self.assertEqual(data['constants']['RESERVED_TAG_IDS'], list(contract['RESERVED_TAG_IDS']))
        self.assertEqual(data['constants']['FIXTURE_ID'], 'tower_v1')

    def test_the_reserved_range_is_the_calibrations_and_inside_tag36h11(self):
        c = _contract()
        lo, hi = c['RESERVED_TAG_IDS']
        self.assertEqual(c['TAG_FAMILY'], 'tag36h11')
        self.assertEqual(hi, TAG36H11_IDS - 1)
        ids = set(c['FIXTURE_TAGS_F'])
        for arm in ('ARM_V1', 'ARM_OMX'):
            ids |= set(c[arm]['mat_tags_f']) | set(c[arm]['target_tag_ids'])
        self.assertTrue(ids and all(lo <= t <= hi for t in ids), sorted(ids))

    def test_every_site_that_names_the_object_ids_says_0_to_399(self):
        lo = _contract()['RESERVED_TAG_IDS'][0]
        code_rpc = WORKFLOW / 'code_rpc.py'
        self.assertEqual(_module_constant(code_rpc, '_TAG_ID_MAX'), lo - 1)
        self.assertIn(f'(0 bis {lo - 1})', _module_constant(code_rpc, '_KIND_TAGIDS_DE'))
        api = (WORKFLOW / 'robot_api.py').read_text(encoding='utf-8')
        self.assertIn(f'#   tagids  1..16 ints each in 0..{lo - 1} ', api)
        self.assertIsNone(re.search(r'\b0\.\.586\b', api))          # the whole tag36h11 range
        catalog = (WORKFLOW / 'object_catalog.py').read_text(encoding='utf-8')
        self.assertIn('from physical_ai_server.workflow.scene_fixture import RESERVED_TAG_IDS', catalog)
        self.assertIn('ist für die Kalibrierung reserviert', catalog)


class TheV1DriversTwin(unittest.TestCase):
    """WP-T1: the driver's tower check judges the contract's tower with the contract's numbers."""

    def test_every_tower_literal_of_the_driver_is_the_contracts(self):
        contract = _contract()
        for name, path in DRIVER_TWINS.items():
            with self.subTest(name=name):
                expected = contract[path[0]]
                for key in path[1:]:
                    expected = expected[key]
                self.assertEqual(_module_constant(DRIVER_GEOMETRY, name), expected)

    def test_the_driver_residual_is_the_uncalibrated_v1s(self):
        contract = _contract()
        self.assertEqual(contract['TOWER_RESIDUAL_DRIVER_M'], contract['TOWER_RESIDUAL_UNCALIBRATED_M']['v1'])
        self.assertEqual(contract['TOWER_RESIDUAL_DRIVER_M'], 0.0260)

    def test_the_judged_parts_are_tower_parts(self):
        names = [part[0] for part in _contract()['TOWER_OBB_F']]
        self.assertTrue(set(_module_constant(DRIVER_GEOMETRY, 'TOWER_T1_PARTS')) <= set(names))


if __name__ == '__main__':
    unittest.main()
