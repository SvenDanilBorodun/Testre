# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-06: the scene-camera profile in the Windows GUI (spec 40 S1, decision
D32).

``EDUBOTICS_SCENE_CAMERA_PROFILE`` is a managed ``.env`` key on Windows
(``MANAGED_KEYS`` 9 -> 10). The setup's „Turm-Kamera (Turm-Kit)" box decides
it: ticking it probes the scene-role camera once for 1920x1080 MJPEG and clears
the box again with the reason when the camera cannot; „Umgebung starten" fixes
the profile for the session like the robot type, writes it through
``generate_env_file`` and hands it to the camera bridge; the Roboter-Studio
leader toggle regenerates the ``.env`` with it; the next launch reads it back.
A robot type without a scene camera hides the box.

``gui_app`` imports tkinter, which the CI interpreter does not have, so its
methods run source-extracted against doubles (the idiom of
test_gui_robot_type.py), and the statements of ``__init__`` that seed the box
run against a real ``.env`` written by the real generator.
"""

import ast
import os
import pathlib
import tempfile
import textwrap
import types
import unittest

from gui.app import config_generator, scene_split
from gui.app.config_generator import (generate_cloud_only_env, generate_env_file,
                                      read_env_var)
from gui.app.constants import ROBOT_PROFILES
from gui.app.device_manager import ArmDevice, HardwareConfig

_GUI_APP = pathlib.Path(__file__).resolve().parent.parent / 'gui' / 'app' / 'gui_app.py'
_KEY = 'EDUBOTICS_SCENE_CAMERA_PROFILE'


def _hardware():
    return HardwareConfig(
        leader=ArmDevice(busid='1-3', serial_path='/dev/serial/by-id/leader', role='leader',
                         description='OpenRB-150'),
        follower=ArmDevice(busid='1-4', serial_path='/dev/serial/by-id/follower',
                           role='follower', description='OpenRB-150'))


def _method(name, ns):
    src = _GUI_APP.read_text(encoding='utf-8')
    marker = f'    def {name}(self'
    start = src.index(marker)
    rest = src[start:]
    end = rest.find('\n    def ', len(marker))
    snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
    exec(compile(snippet, str(_GUI_APP), 'exec'), ns)  # noqa: S102
    return ns[name]


def _func(name):
    tree = ast.parse(_GUI_APP.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found')


def _module_constant(name):
    tree = ast.parse(_GUI_APP.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found')


class _Var:

    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _EnvFile(unittest.TestCase):

    def setUp(self):
        self._prev_src = os.environ.get('EDUBOTICS_CAMERA_SOURCE')
        os.environ['EDUBOTICS_CAMERA_SOURCE'] = 'native_bridge'
        fd, self.path = tempfile.mkstemp(suffix='.env')
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        if self._prev_src is None:
            os.environ.pop('EDUBOTICS_CAMERA_SOURCE', None)
        else:
            os.environ['EDUBOTICS_CAMERA_SOURCE'] = self._prev_src
        for path in (self.path, self.path + '.tmp'):
            if os.path.exists(path):
                os.unlink(path)


class TheKeyIsManaged(_EnvFile):

    def test_managed_keys_hold_the_key(self):
        self.assertIn(_KEY, config_generator.MANAGED_KEYS)
        self.assertEqual(len(config_generator.MANAGED_KEYS), 10)

    def test_generate_env_file_emits_it(self):
        for given, written in (('tower_v1', 'tower_v1'), ('legacy', 'legacy'),
                               (' "TOWER_V1" ', 'tower_v1'), ('tower', 'legacy'),
                               (None, 'legacy')):
            with self.subTest(given=given):
                content = generate_env_file(_hardware(), output_path=self.path,
                                            scene_camera_profile=given)
                self.assertEqual(content.count(f'{_KEY}='), 1, content)
                self.assertIn(f'{_KEY}={written}\n', content)
                self.assertEqual(read_env_var(_KEY, self.path), written)

    def test_the_default_is_legacy(self):
        content = generate_env_file(_hardware(), output_path=self.path)
        self.assertIn(f'{_KEY}=legacy\n', content)

    def test_cloud_only_emits_it_too(self):
        """So a cloud-only start does not drop the box's value: the next launch
        reads the key back."""
        content = generate_cloud_only_env(output_path=self.path, scene_camera_profile='tower_v1')
        self.assertEqual(content.count(f'{_KEY}='), 1, content)
        self.assertEqual(read_env_var(_KEY, self.path), 'tower_v1')
        content = generate_cloud_only_env(output_path=self.path)
        self.assertEqual(read_env_var(_KEY, self.path), 'legacy')

    def test_a_stale_value_is_superseded_not_preserved(self):
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write(f'{_KEY}=tower_v1\nMY_OVERRIDE=keep\n')
        content = generate_env_file(_hardware(), output_path=self.path)
        self.assertEqual(content.count(f'{_KEY}='), 1, content)
        self.assertEqual(read_env_var(_KEY, self.path), 'legacy')
        self.assertEqual(read_env_var('MY_OVERRIDE', self.path), 'keep')

    def test_the_value_is_what_the_arm_container_reads(self):
        """Every value the generator writes selects itself in the one value
        table (scene_split.normalise_profile, which the ingest node runs)."""
        for profile in scene_split.PROFILE_VALUES:
            generate_env_file(_hardware(), output_path=self.path, scene_camera_profile=profile)
            self.assertEqual(scene_split.normalise_profile(read_env_var(_KEY, self.path)), profile)


def _seed_box(env_path):
    """Run the statements of ``EduBoticsApp.__init__`` that seed the
    „Turm-Kamera" box (from ``_saved_scene_profile`` to ``_rs_scene_profile``)
    against the .env at ``env_path``, with the real config_generator."""
    tree = ast.parse(_GUI_APP.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'EduBoticsApp')
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    body = init.body

    def _index(prefix):
        return next(i for i, stmt in enumerate(body) if ast.unparse(stmt).startswith(prefix))

    start = _index('_saved_scene_profile =')
    end = _index('self._rs_scene_profile =')
    owner = types.SimpleNamespace()
    ns = {'self': owner, 'ENV_FILE': env_path, 'config_generator': config_generator,
          'tk': types.SimpleNamespace(BooleanVar=_Var),
          'normalise_scene_profile': scene_split.normalise_profile,
          'PROFILE_TOWER': scene_split.PROFILE_TOWER}
    exec(compile(ast.Module(body=body[start:end + 1], type_ignores=[]),  # noqa: S102
                 str(_GUI_APP), 'exec'), ns)
    return owner


class TheBoxSurvivesARestart(_EnvFile):

    def test_a_tower_session_comes_back_ticked(self):
        generate_env_file(_hardware(), output_path=self.path, scene_camera_profile='tower_v1')
        owner = _seed_box(self.path)
        self.assertIs(owner.tower_camera_enabled.get(), True)
        self.assertEqual(owner._rs_scene_profile, 'tower_v1')

    def test_a_legacy_session_and_a_first_launch_come_back_clear(self):
        generate_env_file(_hardware(), output_path=self.path)
        self.assertIs(_seed_box(self.path).tower_camera_enabled.get(), False)
        os.unlink(self.path)
        owner = _seed_box(self.path)
        self.assertIs(owner.tower_camera_enabled.get(), False)
        self.assertEqual(owner._rs_scene_profile, 'legacy')

    def test_a_hand_edited_value_goes_through_the_one_value_table(self):
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write(f'{_KEY}=" Tower_V1 "\n')
        self.assertIs(_seed_box(self.path).tower_camera_enabled.get(), True)
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write(f'{_KEY}=tower_v2\n')
        self.assertIs(_seed_box(self.path).tower_camera_enabled.get(), False)


class _SyncThread:

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class StartFixesTheProfileForTheSession(unittest.TestCase):

    def _start(self, *, ticked, profile='omx_full', cloud_only=False):
        calls = {}

        def generator(name):
            def generate(*args, **kwargs):
                calls[name] = kwargs
                return ''
            return generate

        bridges = []
        ns = {
            'ROBOT_PROFILES': ROBOT_PROFILES,
            'PROFILE_TOWER': scene_split.PROFILE_TOWER,
            'PROFILE_LEGACY': scene_split.PROFILE_LEGACY,
            'ENV_FILE': '/fake/.env',
            'threading': types.SimpleNamespace(Thread=_SyncThread),
            'config_generator': types.SimpleNamespace(
                generate_env_file=generator('env'),
                generate_cloud_only_env=generator('cloud')),
            'wsl_bridge': types.SimpleNamespace(
                list_serial_devices=lambda: ['/dev/l', '/dev/f']),
            'docker_manager': types.SimpleNamespace(
                start_containers=lambda **kw: True, start_cloud_only=lambda **kw: True),
            'health_checker': types.SimpleNamespace(
                wait_for_web_ui=lambda callback=None: True, full_health_check=lambda: {}),
            'tk': types.SimpleNamespace(NORMAL='normal', DISABLED='disabled'),
            '_redact_secret_env_line': lambda line: line,
        }
        method = _method('_start_environment', ns)
        owner = types.SimpleNamespace(
            _stop_camera_previews=lambda: None,
            cloud_only=_Var(cloud_only),
            phone_camera_enabled=_Var(False),
            tower_camera_enabled=_Var(ticked),
            _selected_robot_profile=lambda: profile,
            _hardware_ready=lambda *a: True,
            btn_start=types.SimpleNamespace(config=lambda **kw: None),
            btn_stop=types.SimpleNamespace(config=lambda **kw: None),
            btn_open_browser=types.SimpleNamespace(config=lambda **kw: None),
            _update_robot_type_combo_state=lambda: None,
            root=types.SimpleNamespace(after=lambda _d, fn=None, *a: None),
            progress=types.SimpleNamespace(start=lambda *a: None, stop=lambda *a: None),
            _set_status=lambda *a: None, _log=lambda *a: None,
            hardware=types.SimpleNamespace(
                leader=types.SimpleNamespace(serial_path='/dev/l'),
                follower=types.SimpleNamespace(serial_path='/dev/f')),
            gpu_available=False,
            _start_camera_bridge=lambda phone_enabled=False: bridges.append(owner._rs_scene_profile),
            _start_rs_control_server=lambda *a: None,
            _open_webview=lambda: None,
            _stop_rs_control_server=lambda: None,
            _update_start_button=lambda: None,
        )
        method(owner)
        return owner, calls, bridges

    def test_a_ticked_box_starts_the_tower_profile(self):
        owner, calls, bridges = self._start(ticked=True)
        self.assertEqual(owner._rs_scene_profile, 'tower_v1')
        self.assertEqual(calls['env']['scene_camera_profile'], 'tower_v1')
        self.assertEqual(bridges, ['tower_v1'])

    def test_a_clear_box_starts_legacy(self):
        owner, calls, bridges = self._start(ticked=False)
        self.assertEqual(owner._rs_scene_profile, 'legacy')
        self.assertEqual(calls['env']['scene_camera_profile'], 'legacy')
        self.assertEqual(bridges, ['legacy'])

    def test_cloud_only_writes_the_box_too(self):
        owner, calls, bridges = self._start(ticked=True, cloud_only=True)
        self.assertEqual(calls['cloud']['scene_camera_profile'], 'tower_v1')
        self.assertEqual(bridges, [])

    def test_a_robot_type_without_a_scene_camera_starts_legacy(self):
        ROBOT_PROFILES['_no_scene_type'] = dict(ROBOT_PROFILES['omx_follower'],
                                                camera_roles=('gripper',))
        self.addCleanup(ROBOT_PROFILES.pop, '_no_scene_type')
        owner, calls, _bridges = self._start(ticked=True, profile='_no_scene_type')
        self.assertEqual(calls['env']['scene_camera_profile'], 'legacy')


class TheSessionProfileReachesTheBridgeAndTheLeaderToggle(unittest.TestCase):

    def test_the_bridge_gets_the_profile_and_the_protokoll(self):
        made = []

        class _Bridge:
            def __init__(self, role_to_index, **kwargs):
                made.append((role_to_index, kwargs))

            def register_external_source(self, *a):
                pass

            def start(self):
                pass

        logs = []
        ns = {'cameras_use_native_bridge': lambda: True,
              'camera_bridge_mod': types.SimpleNamespace(
                  CameraBridge=_Bridge,
                  constants=types.SimpleNamespace(CAMERA_FRAMERATE=30.0)),
              'CAMERA_HEALTH_POLL_MS': 3000, 'PHONE_CAM_ID': 2}
        method = _method('_start_camera_bridge', ns)
        owner = types.SimpleNamespace(
            _stop_camera_bridge=lambda: None, _stop_phone_server=lambda: None,
            _stop_camera_health_poll=lambda: None, _poll_camera_health=None,
            hardware=types.SimpleNamespace(cameras=[
                types.SimpleNamespace(role='scene', win_index=4)]),
            _rs_scene_profile='tower_v1', _log=logs.append,
            root=types.SimpleNamespace(after=lambda *a: 'after#1'))
        method(owner)
        self.assertEqual(made, [({'scene': 4},
                                 {'scene_profile': 'tower_v1', 'log': owner._log})])

    def test_the_leader_toggle_keeps_the_profile_both_ways(self):
        calls = []
        for restart_ok, expected in ((True, 1), (False, 2)):
            calls.clear()
            ns = {'config_generator': types.SimpleNamespace(
                      read_env_var=lambda k, p: '0',
                      generate_env_file=lambda *a, **kw: calls.append(kw) or ''),
                  'docker_manager': types.SimpleNamespace(
                      restart_open_manipulator=lambda gpu=False, log=None, ok=restart_ok: ok),
                  'ROBOT_PROFILES': ROBOT_PROFILES, 'ENV_FILE': '/fake/.env'}
            method = _method('_rs_set_leader_mode', ns)
            owner = types.SimpleNamespace(
                _rs_robot_type='omx_full', _rs_scene_profile='tower_v1',
                hardware=types.SimpleNamespace(leader=object(), follower=object()),
                _rs_phone_enabled=False, _rs_switch_in_flight=False, _rs_use_gpu=False)
            method(owner, True, lambda *a: None)
            with self.subTest(restart_ok=restart_ok):
                self.assertEqual(len(calls), expected)
                for kw in calls:
                    self.assertEqual(kw['scene_camera_profile'], 'tower_v1')


class TheBoxProbesTheSceneCamera(unittest.TestCase):

    def _owner(self, cameras, probe_reason='', cloud_only=False):
        rec = types.SimpleNamespace(warnings=[], logs=[], events=[], probed=[])

        def probe(index):
            rec.probed.append(index)
            rec.events.append('probe')
            return probe_reason

        ns = {
            'threading': types.SimpleNamespace(Thread=_SyncThread),
            'tk': types.SimpleNamespace(NORMAL='normal', DISABLED='disabled'),
            'messagebox': types.SimpleNamespace(
                showwarning=lambda title, msg: rec.warnings.append((title, msg))),
            'camera_bridge_mod': types.SimpleNamespace(
                probe_scene_camera=probe,
                SCENE_PROBE_REFUSED_DE=_scene_probe_refused()),
            'TOWER_NEEDS_SCENE_CAMERA_DE': _module_constant('TOWER_NEEDS_SCENE_CAMERA_DE'),
        }
        toggled = _method('_on_tower_camera_toggled', ns)
        finish = _method('_finish_tower_probe', dict(ns))
        owner = types.SimpleNamespace(
            tower_camera_enabled=_Var(True),
            _tower_probe_running=False,
            hardware=types.SimpleNamespace(cameras=cameras),
            cloud_only=_Var(cloud_only),
            camera_frame='camera_frame',
            _stop_camera_previews=lambda: rec.events.append('previews stopped'),
            _restart_camera_previews=lambda: rec.events.append('previews restarted'),
            _set_frame_state=lambda frame, state: rec.events.append((frame, state)),
            _update_start_button=lambda: rec.events.append('start button'),
            _log=rec.logs.append,
            root=types.SimpleNamespace(after=lambda _d, fn=None, *a: fn(*a)),
        )
        owner._finish_tower_probe = lambda reason: finish(owner, reason)
        return owner, rec, toggled

    def test_no_scene_camera_clears_the_box_and_probes_nothing(self):
        owner, rec, toggled = self._owner(
            [types.SimpleNamespace(role='gripper', win_index=0, name='Cam')])
        toggled(owner)
        self.assertIs(owner.tower_camera_enabled.get(), False)
        self.assertEqual(rec.probed, [])
        self.assertEqual(rec.warnings, [('Turm-Kamera', _module_constant('TOWER_NEEDS_SCENE_CAMERA_DE'))])

    def test_a_camera_without_1080p_is_refused_with_the_sentence(self):
        reason = _scene_probe_refused()
        owner, rec, toggled = self._owner(
            [types.SimpleNamespace(role='scene', win_index=3, name='Cam')], probe_reason=reason)
        toggled(owner)
        self.assertEqual(rec.probed, [3])
        self.assertIs(owner.tower_camera_enabled.get(), False)
        self.assertEqual(rec.warnings, [('Turm-Kamera', reason)])
        self.assertIn('[WARNUNG] ' + reason, rec.logs)
        self.assertIs(owner._tower_probe_running, False)

    def test_a_1080p_camera_keeps_the_box(self):
        owner, rec, toggled = self._owner(
            [types.SimpleNamespace(role='scene', win_index=3, name='Cam')])
        toggled(owner)
        self.assertIs(owner.tower_camera_enabled.get(), True)
        self.assertEqual(rec.warnings, [])
        self.assertIs(owner._tower_probe_running, False)
        # The previews hold the cameras: released before the probe, back after
        # it; the camera section is frozen meanwhile.
        events = rec.events
        self.assertLess(events.index('previews stopped'), events.index('probe'))
        self.assertLess(events.index(('camera_frame', 'disabled')), events.index('probe'))
        self.assertLess(events.index('probe'), events.index('previews restarted'))
        self.assertLess(events.index('probe'), events.index(('camera_frame', 'normal')))

    def test_clearing_the_box_probes_nothing(self):
        owner, rec, toggled = self._owner(
            [types.SimpleNamespace(role='scene', win_index=3, name='Cam')])
        owner.tower_camera_enabled.set(False)
        toggled(owner)
        self.assertEqual((rec.probed, rec.warnings), ([], []))

    def test_the_usb_cam_rollback_has_no_camera_to_probe(self):
        owner, rec, toggled = self._owner(
            [types.SimpleNamespace(role='scene', win_index=-1, name='/dev/video0')])
        toggled(owner)
        self.assertIs(owner.tower_camera_enabled.get(), False)
        self.assertEqual(rec.probed, [])


def _scene_probe_refused():
    from gui.app import camera_bridge
    return camera_bridge.SCENE_PROBE_REFUSED_DE


class _Check:

    def __init__(self):
        self.manager = 'pack'
        self.states = []

    def pack(self, **kwargs):
        self.manager = 'pack'

    def pack_forget(self):
        self.manager = ''

    def winfo_manager(self):
        return self.manager

    def state(self, flags):
        self.states.append(flags)


class TheBoxBelongsToARobotTypeWithASceneCamera(unittest.TestCase):

    def _update(self, profile, *, running=False, cloud_only=False, probing=False):
        ns = {'ROBOT_PROFILES': ROBOT_PROFILES,
              'tk': types.SimpleNamespace(W='w', TclError=RuntimeError)}
        method = _method('_update_tower_camera_check', ns)
        owner = types.SimpleNamespace(
            tower_camera_check=_Check(), phone_cam_check=object(),
            _selected_robot_profile=lambda: profile, running=running,
            cloud_only=_Var(cloud_only), _tower_probe_running=probing)
        method(owner)
        return owner.tower_camera_check

    def test_every_shipped_type_shows_it(self):
        for profile in ROBOT_PROFILES:
            with self.subTest(profile=profile):
                check = self._update(profile)
                self.assertEqual(check.manager, 'pack')
                self.assertEqual(check.states[-1], ['!disabled'])

    def test_a_type_without_a_scene_role_hides_it(self):
        ROBOT_PROFILES['_no_scene_type'] = dict(ROBOT_PROFILES['omx_follower'],
                                                camera_roles=('gripper',))
        self.addCleanup(ROBOT_PROFILES.pop, '_no_scene_type')
        self.assertEqual(self._update('_no_scene_type').manager, '')

    def test_it_is_frozen_while_its_value_is_in_use(self):
        for kwargs in ({'running': True}, {'cloud_only': True}, {'probing': True}):
            with self.subTest(**kwargs):
                self.assertEqual(self._update('omx_full', **kwargs).states[-1], ['disabled'])

    def test_it_follows_the_robot_type_selector(self):
        """Wired where the selector's own state is kept, so every transition
        that reaches the selector (start, stop, type change, cloud mode)
        reaches the box."""
        self.assertIn('self._update_tower_camera_check()',
                      ast.unparse(_func('_update_robot_type_combo_state')))

    def test_the_box_is_built_in_the_camera_step(self):
        build = ast.unparse(_func('_build_ui'))
        self.assertIn("text='Turm-Kamera (Turm-Kit)'", build)
        self.assertIn('variable=self.tower_camera_enabled', build)
        self.assertIn('command=self._on_tower_camera_toggled', build)


if __name__ == '__main__':
    unittest.main()
