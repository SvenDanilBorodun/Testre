# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Scene-camera provenance in lockstep (decision D58): the robot, the cloud API, the page.

``physical_ai_server/scene_provenance.py`` is the one source of the stamp file's
path, its values and the profile topic. The cloud API cannot import it (another
image), so ``services/inference_sessions.py`` reads a training dataset's stamp
by the same rules itself: the same path, profiles, version, platforms, keys and
size cap, and over one table of stamp bodies the same verdict as the robot's own
reader. The page's hook ``hooks/useSceneCameraProfile.js`` names the topic and
the profile values, and the page calls the two cameras what the robot calls them.

R-B constants only; R-C's WP-PROF adds the arm container's topic.

The API module is imported in a CHILD interpreter: the GUI's tests own another
package named ``app``.
"""

import ast
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
PROVENANCE = PKG / 'scene_provenance.py'
RECORD_TEXTS = PKG / 'data_processing' / 'record_texts_de.py'
API_ROOT = REPO_ROOT / 'robotis_ai_setup' / 'cloud_training_api'
API_SESSIONS = API_ROOT / 'app' / 'services' / 'inference_sessions.py'
SRC = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_manager' / 'src'
HOOK = SRC / 'hooks' / 'useSceneCameraProfile.js'
CLOUD_COPY = SRC / 'components' / 'CloudInference' / 'cloudCopy.js'

TOWER = '{"v":1,"scene_camera_profile":"tower_v1","profile_id":"3efca10f3a05","platform":"windows"}'

# Stamp bodies (as text; each is also tried as raw bytes where it says so).
STAMP_CASES = {
    'tower, Windows': TOWER,
    'tower, Pi': '{"v":1,"scene_camera_profile":"tower_v1","profile_id":"8b7f72cb250c","platform":"pi"}',
    'tower, unknown id and platform': '{"v":1,"scene_camera_profile":"tower_v1","profile_id":null,"platform":null}',
    'a legacy stamp': '{"v":1,"scene_camera_profile":"legacy","profile_id":null,"platform":null}',
    'keys in another order, spaces': ' {"platform": "pi", "profile_id": null, "v": 1, "scene_camera_profile": "tower_v1"}\n',
    'v 2': TOWER.replace('"v":1', '"v":2'),
    'v true': TOWER.replace('"v":1', '"v":true'),
    'v 1.0': TOWER.replace('"v":1', '"v":1.0'),
    'v "1"': TOWER.replace('"v":1', '"v":"1"'),
    'a later profile': TOWER.replace('tower_v1', 'tower_v2'),
    'profile null': TOWER.replace('"tower_v1"', 'null'),
    'an extra key': TOWER[:-1] + ',"lens":"wide"}',
    'a key missing': '{"v":1,"scene_camera_profile":"tower_v1","profile_id":null}',
    'an upper-case profile id': TOWER.replace('3efca10f3a05', '3EFCA10F3A05'),
    'a short profile id': TOWER.replace('3efca10f3a05', '3efca10f3a0'),
    'a numeric profile id': TOWER.replace('"3efca10f3a05"', '123456789012'),
    'a profile id with a trailing newline': TOWER.replace('3efca10f3a05', '3efca10f3a05\\n'),
    'another platform': TOWER.replace('windows', 'linux'),
    'malformed json': '{"v":1,',
    'a list': '[1]',
    'empty': '',
    'nested too deep': '[' * 2000 + ']' * 2000,
}
BYTE_CASES = {
    'a BOM': b'\xef\xbb\xbf' + TOWER.encode(),
    'not utf-8': b'\xff\xfe\x00',
    'latin-1 text': '{"v":1,"scene_camera_profile":"tower_v1","profile_id":null,"platform":"pi","ä":1}'.encode('latin-1'),
}

_API_CHILD = r'''
import json, sys
payload = json.load(sys.stdin)
sys.path.insert(0, payload["api_root"])
from app.services import inference_sessions as svc
out = {}
for name, hexed in payload["bodies"].items():
    out[name] = svc._scene_profile_of_stamp(bytes.fromhex(hexed))
print(json.dumps(out))
'''


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assigned(path, name):
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not assigned in {path.name}')


def _js_export(path, name):
    m = re.search(rf'^export const {name} = (.+);$', path.read_text(encoding='utf-8'), re.M)
    if m is None:
        raise AssertionError(f'{name} not exported by {path.name}')
    return m.group(1)


def _js_strings(text):
    return re.findall(r"'([^'\\]*)'", text)


class SceneProvenanceLockstep(BoundedTestCase):

    @classmethod
    def setUpClass(cls):
        for path in (PROVENANCE, RECORD_TEXTS, API_SESSIONS, HOOK, CLOUD_COPY):
            if not path.is_file():
                raise AssertionError(f'missing: {path.relative_to(REPO_ROOT)}')
        cls.P = _load(PROVENANCE, '_edubotics_lockstep_scene_provenance')

    def test_the_api_reads_the_robots_stamp_file(self):
        pairs = {
            'SCENE_STAMP_RELPATH': self.P.STAMP_RELPATH,
            'SCENE_PROFILES': self.P.PROFILES,
            'SCENE_STAMP_VERSION': self.P.STAMP_VERSION,
            'SCENE_PLATFORMS': self.P.PLATFORMS,
            'SCENE_STAMP_MAX_BYTES': self.P.STAMP_MAX_BYTES,
            '_SCENE_STAMP_KEYS': self.P._STAMP_KEYS,
        }
        for api_name, robot_value in pairs.items():
            with self.subTest(constant=api_name):
                self.assertEqual(_assigned(API_SESSIONS, api_name), robot_value)

    def test_the_api_judges_a_stamp_as_the_robot_does(self):
        bodies = {name: text.encode('utf-8') for name, text in STAMP_CASES.items()}
        bodies.update(BYTE_CASES)
        for name, body in bodies.items():
            self.assertLessEqual(len(body), self.P.STAMP_MAX_BYTES, name)  # the size cap is compared above
        robot = {}
        with tempfile.TemporaryDirectory() as tmp:
            for i, (name, body) in enumerate(bodies.items()):
                root = pathlib.Path(tmp) / f'ds{i}'
                (root / 'meta').mkdir(parents=True)
                (root / self.P.STAMP_RELPATH).write_bytes(body)
                robot[name] = self.P.dataset_profile(root)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        proc = subprocess.run(
            [sys.executable, '-B', '-c', _API_CHILD],
            input=json.dumps({'api_root': str(API_ROOT), 'bodies': {n: b.hex() for n, b in bodies.items()}}),
            capture_output=True, text=True, encoding='utf-8', timeout=120, env=env, cwd=str(REPO_ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        api = json.loads(proc.stdout.strip().splitlines()[-1])
        diverging = {name: (robot[name], api[name]) for name in bodies if robot[name] != api[name]}
        self.assertEqual(diverging, {})
        # the table reaches every verdict
        self.assertEqual({robot[n] for n in bodies}, {'tower_v1', 'legacy', None})

    def test_the_hook_names_the_robots_topic_and_profiles(self):
        topic = _js_export(HOOK, 'SCENE_CAMERA_PROFILE_TOPIC')
        self.assertEqual(_js_strings(topic), [self.P.PROFILE_TOPIC], topic)
        profiles = _js_export(HOOK, 'SCENE_CAMERA_PROFILES')
        self.assertTrue(profiles.startswith('Object.freeze(['), profiles)
        self.assertEqual(tuple(_js_strings(profiles)), self.P.PROFILES)

    def test_the_page_names_the_cameras_as_the_robot_does(self):
        names = _assigned(RECORD_TEXTS, 'SCENE_CAMERA_NAMES_DE')
        self.assertEqual(tuple(names), self.P.PROFILES)
        m = re.search(r'^  sceneCameraNames: \{([^}]*)\},$', CLOUD_COPY.read_text(encoding='utf-8'), re.M)
        self.assertIsNotNone(m, 'cloudCopy.sceneCameraNames')
        page = dict(re.findall(r"(\w+): '([^']*)'", m.group(1)))
        self.assertEqual(page, names)


if __name__ == '__main__':
    unittest.main()
