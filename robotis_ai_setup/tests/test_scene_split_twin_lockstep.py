# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""``scene_split.py`` exists twice and must be ONE file (WP-04).

The Windows GUI (``gui/app/``) and the arm container
(``docker/open_manipulator/``, plain COPY) each run the tower camera's
derivation, its self-test and its capture verifier, and a recording must not
depend on which platform cut the picture. So the two copies are byte for
byte the same file, and the module may import nothing but the standard
library, numpy and cv2: both places have those and nothing else in common
(no ROS, no package of either side).

Deps-free: ``filecmp`` and ``ast`` only.
"""

import ast
import filecmp
import os
import sys
import unittest

_SETUP = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
GUI_COPY = os.path.join(_SETUP, 'gui', 'app', 'scene_split.py')
ARM_COPY = os.path.join(_SETUP, 'docker', 'open_manipulator', 'scene_split.py')
ALLOWED_THIRD_PARTY = {'numpy', 'cv2'}


def _imported_roots(source):
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add('.' * node.level + (node.module or ''))
            elif node.module:
                roots.add(node.module.split('.')[0])
    return roots


class SceneSplitTwin(unittest.TestCase):

    def test_both_copies_exist(self):
        for path in (GUI_COPY, ARM_COPY):
            with self.subTest(path=path):
                self.assertTrue(os.path.isfile(path), path)

    def test_the_two_copies_are_byte_identical(self):
        self.assertTrue(filecmp.cmp(GUI_COPY, ARM_COPY, shallow=False),
                        'gui/app/scene_split.py and docker/open_manipulator/scene_split.py differ')

    def test_it_imports_only_the_standard_library_numpy_and_cv2(self):
        with open(GUI_COPY, encoding='utf-8') as fh:
            roots = _imported_roots(fh.read())
        self.assertTrue(roots, 'no import found: the scan read nothing')
        allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | {'__future__'}
        self.assertEqual(sorted(roots - allowed), [])
        self.assertTrue(ALLOWED_THIRD_PARTY <= roots, 'numpy and cv2 are what the derivation runs on')

    def test_the_import_scan_has_teeth(self):
        roots = _imported_roots('import json\nfrom rclpy.node import Node\n'
                                'def f():\n    from . import constants\n')
        allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY
        self.assertEqual(sorted(roots - allowed), ['.', 'rclpy'])


if __name__ == '__main__':
    unittest.main()
