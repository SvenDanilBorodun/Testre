"""Retired robot types and retired prober tokens on a fielded install.

`edu6_studio` and `edu1_studio` no longer exist in any registry. A classroom
PC or Pi can still carry one in its managed ``.env`` (``EDUBOTICS_ROBOT_TYPE``),
and an image pinned to an older tag can still answer the scan with an old
prober token. This is the ONE test file that feeds those real retired ids and
tokens in; production code names none of them:

  * the Windows selector preselects ``omx_full`` for a stored retired id,
  * the Pi's ``resolve_robot_type`` and the server's ``robot_profiles.resolve``
    both fall back to ``omx_full``,
  * both host scan twins read every retired prober token as an in-container
    image OLDER than the host (``_STALE_IMAGE_NOTICE_DE``) — through the generic
    unknown-family rule, never a literal — and never slot it as the follower.
"""

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_SETUP_DIR = Path(__file__).resolve().parent.parent      # robotis_ai_setup/
_REPO = _SETUP_DIR.parent
if str(_SETUP_DIR) not in sys.path:
    sys.path.insert(0, str(_SETUP_DIR))

from gui.app import device_manager as win  # noqa: E402
from gui.app.constants import DEFAULT_ROBOT_PROFILE, ROBOT_PROFILES  # noqa: E402
from pi_agent import constants as pi_constants  # noqa: E402
from pi_agent import identify_arm as pi  # noqa: E402

_RETIRED_IDS = ('edu6_studio', 'edu1_studio')
_RETIRED_TOKENS = ('edu1', 'edu6', 'edu1_arm_found', 'edu6_arm_found')
_GUI_APP = _SETUP_DIR / 'gui' / 'app' / 'gui_app.py'
_SERVER_PROFILES = (_REPO / 'physical_ai_tools' / 'physical_ai_server'
                    / 'physical_ai_server' / 'robot_profiles.py')


def _selector_seed(saved):
    """Run the shipped selector-seeding statements of ``EduBoticsApp.__init__``
    (from the ``_robot_id_to_label`` map to ``_rs_robot_type``) against a fake
    ``.env`` holding ``saved``. The statements are extracted from the source, so
    this is the code a student's GUI runs, not a restatement of it."""
    source = _GUI_APP.read_text(encoding='utf-8')
    tree = ast.parse(source)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == 'EduBoticsApp')
    init = next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    body = init.body

    def _index(predicate):
        return next(i for i, stmt in enumerate(body) if predicate(ast.unparse(stmt)))

    start = _index(lambda s: s.startswith('self._robot_id_to_label ='))
    end = _index(lambda s: s.startswith('self._rs_robot_type ='))
    module = ast.Module(body=body[start:end + 1], type_ignores=[])

    class _StringVar:
        def __init__(self, value=None):
            self.value = value

        def get(self):
            return self.value

    owner = types.SimpleNamespace()
    ns = {
        'self': owner,
        'ROBOT_PROFILES': ROBOT_PROFILES,
        'DEFAULT_ROBOT_PROFILE': DEFAULT_ROBOT_PROFILE,
        'ENV_FILE': '/fake/.env',
        'tk': types.SimpleNamespace(StringVar=_StringVar),
        'config_generator': types.SimpleNamespace(
            read_env_var=lambda key, _path: saved
            if key == 'EDUBOTICS_ROBOT_TYPE' else None),
    }
    exec(compile(module, str(_GUI_APP), 'exec'), ns)  # noqa: S102
    return owner


def _load_server_profiles():
    spec = importlib.util.spec_from_file_location('_retired_robot_profiles',
                                                  _SERVER_PROFILES)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses look the module up
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class TestARetiredStoredIdFallsBackToOmxFull(unittest.TestCase):

    def test_no_registry_carries_a_retired_id(self):
        server = _load_server_profiles()
        for pid in _RETIRED_IDS:
            self.assertNotIn(pid, ROBOT_PROFILES)
            self.assertNotIn(pid, pi_constants.ROBOT_PROFILES)
            self.assertNotIn(pid, server.ROBOT_PROFILES)

    def test_the_windows_selector_preselects_omx_full(self):
        for pid in _RETIRED_IDS:
            with self.subTest(pid):
                owner = _selector_seed(pid)
                self.assertEqual(owner._robot_profile_selected, 'omx_full')
                self.assertEqual(owner._rs_robot_type, 'omx_full')
                self.assertEqual(owner.robot_type_var.get(),
                                 ROBOT_PROFILES['omx_full']['display_de'])
        # …and a stored V1 id is kept, so the fallback is not unconditional.
        self.assertEqual(_selector_seed('v1_studio')._robot_profile_selected,
                         'v1_studio')

    def test_the_pi_resolves_to_omx_full(self):
        for pid in _RETIRED_IDS:
            with self.subTest(pid):
                self.assertEqual(pi_constants.resolve_robot_type(pid), 'omx_full')
                self.assertEqual(pi_constants.arm_family_for_robot_type(pid), 'omx')
        self.assertEqual(pi_constants.resolve_robot_type('v1_studio'), 'v1_studio')

    def test_the_server_resolves_to_omx_full(self):
        server = _load_server_profiles()
        for pid in _RETIRED_IDS:
            with self.subTest(pid):
                with self.assertLogs(server._logger, level='WARNING'):
                    self.assertEqual(server.resolve(pid).profile_id, 'omx_full')


class TestARetiredProberTokenReadsAsAStaleImage(unittest.TestCase):

    def test_both_twins_classify_every_retired_token_as_stale(self):
        for token in _RETIRED_TOKENS:
            with self.subTest(token):
                self.assertTrue(win.is_stale_image_token(token))
                self.assertTrue(pi.is_stale_image_token(token))

    def test_the_windows_scan_refuses_them_with_the_stale_image_sentence(self):
        dev = types.SimpleNamespace(busid='1-1', description='CH343',
                                    serial_path='/dev/serial/by-id/x')
        for selected in ('v1', 'omx'):
            for token in _RETIRED_TOKENS:
                with self.subTest(selected=selected, token=token):
                    win.LAST_SCAN_NOTICE = ''
                    with patch.object(win, 'self_heal_wsl_serial'), \
                            patch.object(win, 'attach_all_robotis_devices',
                                         return_value=[dev]), \
                            patch.object(win, 'find_serial_paths_for_arms',
                                         return_value=['/dev/serial/by-id/x']), \
                            patch.object(win, 'start_scanner_container',
                                         return_value=True), \
                            patch.object(win, 'stop_scanner_container'), \
                            patch.object(win, 'identify_arm_via_docker',
                                         return_value=token), \
                            patch('time.sleep'):
                        leader, follower = win.scan_and_identify_arms(
                            'img', arm_family=selected)
                    self.assertIsNone(leader)
                    self.assertIsNone(follower)
                    self.assertEqual(win.LAST_SCAN_NOTICE,
                                     win._STALE_IMAGE_NOTICE_DE)

    def test_the_pi_scan_refuses_them_with_the_stale_image_sentence(self):
        path = '/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A68010132-if00'
        for selected in ('v1', 'omx'):
            for token in _RETIRED_TOKENS:
                with self.subTest(selected=selected, token=token):
                    pi.LAST_SCAN_NOTICE = ''
                    with patch.object(pi, '_poll_serial_paths',
                                      return_value=[path]), \
                            patch.object(pi, 'start_scanner_container',
                                         return_value=True), \
                            patch.object(pi, 'stop_scanner_container'), \
                            patch.object(pi, 'identify_arm_via_docker',
                                         return_value=token), \
                            patch.object(pi.time, 'sleep'):
                        leader, follower = pi.scan_and_identify_arms(
                            'img', arm_family=selected)
                    self.assertIsNone(leader)
                    self.assertIsNone(follower)
                    self.assertEqual(pi.LAST_SCAN_NOTICE,
                                     pi._STALE_IMAGE_NOTICE_DE)

    def test_the_sentence_is_byte_equal_on_both_twins(self):
        self.assertEqual(win._STALE_IMAGE_NOTICE_DE, pi._STALE_IMAGE_NOTICE_DE)
        self.assertEqual(
            win._STALE_IMAGE_NOTICE_DE,
            'Das Roboter-Abbild ist älter als die Software und erkennt diesen '
            'Arm nicht. Bitte die Umgebung aktualisieren und erneut scannen.')

    def test_production_scan_code_spells_no_retired_family(self):
        """M1: the rule is generic. No retired family name appears in either
        twin's source (the tokens arrive only from an old image)."""
        for path in (_SETUP_DIR / 'gui' / 'app' / 'device_manager.py',
                     _SETUP_DIR / 'pi_agent' / 'identify_arm.py',
                     _SETUP_DIR / 'docker' / 'open_manipulator' / 'identify_arm.py'):
            text = path.read_text(encoding='utf-8').lower()
            for name in ('edu1', 'edu6', 'edu:1'):
                self.assertNotIn(name, text, f'{path.name}: {name}')


if __name__ == '__main__':
    unittest.main()
