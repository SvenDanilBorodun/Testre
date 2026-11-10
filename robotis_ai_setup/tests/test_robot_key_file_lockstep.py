# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""The robot's token key file, one path across four places (VB-03, 2026-11-15).

On an Orange Pi the page encrypts the student's Hugging Face token only for
the key the pi-agent serves (`GET /api/system/robot-key`), read from a file the
ROS node writes through a bind mount. Four places must agree, or the page on a
Pi can never send a token:

* the node: ``hf_token_crypto.KEY_FILE_DIR`` / ``KEY_FILE_NAME`` /
  ``KEY_FILE_SCHEMA`` / ``ENC_NAME``;
* the agent: ``pi_agent/constants.py::ROBOT_KEY_DIR`` / ``ROBOT_KEY_FILE_NAME``
  and ``pi_agent/robot_key.py::KEY_SCHEMA`` / ``ENC_NAME``, and its route;
* the opi compose AND its tarball twin: one bind mount of that directory onto
  physical_ai_server, at the same path inside the container; the Windows
  composes carry none (there the page keeps the topic's key on loopback);
* the SPA: ``robotChannel.js::AGENT_KEY_URL``.

Read as text and AST, stdlib only (none of the four is imported).
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1]
REPO = SETUP.parent
CRYPTO = (REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
          / 'data_processing' / 'hf_token_crypto.py')
AGENT_CONSTANTS = SETUP / 'pi_agent' / 'constants.py'
AGENT_ROBOT_KEY = SETUP / 'pi_agent' / 'robot_key.py'
AGENT = SETUP / 'pi_agent' / 'agent.py'
COMPOSES = {
    'opi': SETUP / 'docker' / 'docker-compose.opi.yml',
    'twin': SETUP / 'pi_agent' / 'docker' / 'docker-compose.opi.yml',
}
WINDOWS_COMPOSES = (SETUP / 'docker' / 'docker-compose.yml',
                    SETUP / 'docker' / 'docker-compose.gpu.yml')
CHANNEL = (REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'features' / 'hfToken'
           / 'robotChannel.js')


def _literals(path: Path) -> dict:
    out = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant):
            out[node.targets[0].id] = node.value.value
    return out


def _service_block(compose: str, name: str) -> str:
    start = compose.index(f'\n  {name}:\n')
    rest = compose[start + 1:]
    m = re.search(r'\n  [A-Za-z_][A-Za-z0-9_-]*:\n|\n[A-Za-z]', rest)
    return rest[:m.start()] if m else rest


class RobotKeyFileLockstep(unittest.TestCase):
    def setUp(self):
        self.node = _literals(CRYPTO)
        self.agent = _literals(AGENT_CONSTANTS)
        self.reader = _literals(AGENT_ROBOT_KEY)

    def test_the_directory_and_the_file_name(self):
        self.assertEqual(self.node['KEY_FILE_DIR'], '/run/edubotics-robot-key')
        self.assertEqual(self.agent['ROBOT_KEY_DIR'], self.node['KEY_FILE_DIR'])
        self.assertEqual(self.agent['ROBOT_KEY_FILE_NAME'], self.node['KEY_FILE_NAME'])

    def test_the_schema_the_agent_accepts_is_the_one_the_node_writes(self):
        self.assertEqual(self.reader['KEY_SCHEMA'], self.node['KEY_FILE_SCHEMA'])
        self.assertEqual(self.reader['ENC_NAME'], self.node['ENC_NAME'])

    def test_both_opi_composes_bind_mount_it_onto_the_server_only(self):
        mount = f"      - {self.node['KEY_FILE_DIR']}:{self.node['KEY_FILE_DIR']}\n"
        for label, path in COMPOSES.items():
            with self.subTest(compose=label):
                text = path.read_text(encoding='utf-8')
                self.assertEqual(text.count(mount), 1, label)
                self.assertIn(mount, _service_block(text, 'physical_ai_server'))
                for other in ('open_manipulator', 'physical_ai_manager', 'code_runner'):
                    self.assertNotIn(self.node['KEY_FILE_DIR'], _service_block(text, other))

    def test_no_windows_compose_carries_it(self):
        for path in WINDOWS_COMPOSES:
            with self.subTest(compose=path.name):
                self.assertNotIn(self.node['KEY_FILE_DIR'], path.read_text(encoding='utf-8'))

    def test_the_page_reads_the_route_the_agent_serves(self):
        js = CHANNEL.read_text(encoding='utf-8')
        m = re.search(r"^export const AGENT_KEY_URL = '([^']+)';$", js, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), '/api/system/robot-key')  # nginx strips /api/system
        self.assertIn('elif path == "/robot-key":', AGENT.read_text(encoding='utf-8'))

    def test_the_route_is_not_public(self):
        # Behind the robot login like every lesson route: it must not join the
        # agent's public set (pi_agent/tests/test_pi_auth.py proves the 401).
        tree = ast.parse(AGENT.read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == '_PUBLIC_GET_PATHS' for t in node.targets):
                self.assertNotIn('/robot-key', ast.unparse(node.value))
                break
        else:
            self.fail('_PUBLIC_GET_PATHS not found')


if __name__ == '__main__':
    unittest.main()
