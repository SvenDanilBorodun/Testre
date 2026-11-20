# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-07: ``entrypoint_omx.sh`` hands the scene slot to the tower node in the
tower profile (spec 40 S1).

On the in-container camera path (``usb_cam``: the Orange Pi) the entrypoint
starts one camera per configured slot. With
``EDUBOTICS_SCENE_CAMERA_PROFILE=tower_v1`` the slot named ``scene`` starts
``scene_camera_node.py`` instead of the usb_cam launch; every other slot, and
every slot in ``legacy``, starts usb_cam exactly as before. The decision is
the shell function ``scene_slot_uses_tower_node NAME PROFILE``, which reads the
profile by the ONE value table (``scene_split.normalise_profile``): blanks and
quotes stripped at both ends, lower-cased, ``tower_v1`` or nothing.

The function and the loop are run from the file, under real bash, never
re-implemented here: editing them changes what this test measures. The
function must work under the bash 3.2 a Mac ships as well as the container's
bash 5; the loop itself needs bash 4 (``declare -A``, which it already used).
"""

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ARM_DIR = os.path.abspath(os.path.join(_HERE, '..', 'docker', 'open_manipulator'))
_ENTRYPOINT = os.path.join(_ARM_DIR, 'entrypoint_omx.sh')
KEY = 'EDUBOTICS_SCENE_CAMERA_PROFILE'

# The spec's table (40 WP-07): every one is the scene slot's profile value.
SPEC_VALUES = ('tower_v1', '"tower_v1"', ' tower_v1 ', 'TOWER_V1', 'legacy', '', 'auto')
# Values the table leaves out, where a shell strip set can drift from Python's
# (the same list test_scene_profile_value_table.py runs against the agent).
BEYOND_VALUES = ("'tower_v1'", '\ttower_v1\r\n', '""', "' \"tower_v1\" '",
                 '\xa0tower_v1', 'tower_v1\u2003', 'tower_v1x', 'tower v1',
                 'tower_v1;legacy', 'LEGACY', 'Tower_V1', '\vtower_v1\f')


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _load_scene_split():
    spec = importlib.util.spec_from_file_location(
        'scene_split_for_entrypoint_slot', os.path.join(_ARM_DIR, 'scene_split.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SPLIT = _load_scene_split()


def _function_source():
    """The function's definition, from its first line to its closing brace."""
    match = re.search(r'^scene_slot_uses_tower_node\(\) \{\n.*?^\}\n',
                      _read(_ENTRYPOINT), re.M | re.S)
    if match is None:
        raise AssertionError('entrypoint_omx.sh defines no scene_slot_uses_tower_node')
    return match.group(0)


def _profile_argument():
    """The entrypoint's own spelling of the profile argument in the loop."""
    match = re.search(r'scene_slot_uses_tower_node "\$name" ("\$\{' + KEY + r':-\}")',
                      _read(_ENTRYPOINT))
    if match is None:
        raise AssertionError('the usb_cam loop does not call scene_slot_uses_tower_node '
                             '"$name" with the profile key')
    return match.group(1)


def slot_verdict(name, profile):
    """The function's exit status for ``NAME PROFILE``, under bash."""
    script = _function_source() + 'scene_slot_uses_tower_node "$1" "$2"\n'
    return subprocess.run(['bash', '-c', script, 'entrypoint', name, profile],
                          capture_output=True, text=True, check=False).returncode


def entrypoint_profile(env):
    """The profile the entrypoint selects for the scene slot from the
    container environment ``env`` (a dict; a missing key is the unset
    variable): the loop's own call, under bash."""
    script = (_function_source()
              + f'scene_slot_uses_tower_node scene {_profile_argument()}\n')
    run_env = {k: v for k, v in os.environ.items() if k != KEY}
    if KEY in env:
        run_env[KEY] = env[KEY]
    result = subprocess.run(['bash', '-c', script], env=run_env, capture_output=True,
                            text=True, check=False)
    return 'tower_v1' if result.returncode == 0 else 'legacy'


class TheSlotFunction(unittest.TestCase):

    def test_the_specs_values_agree_with_normalise_profile(self):
        for value in SPEC_VALUES:
            with self.subTest(value=value):
                expected = 0 if _SPLIT.normalise_profile(value) == 'tower_v1' else 1
                self.assertEqual(slot_verdict('scene', value), expected)

    def test_the_tower_values_and_nothing_else_return_0(self):
        tower = [v for v in SPEC_VALUES if slot_verdict('scene', v) == 0]
        self.assertEqual(tower, ['tower_v1', '"tower_v1"', ' tower_v1 ', 'TOWER_V1'])

    def test_values_beyond_the_table_agree_with_normalise_profile(self):
        for value in BEYOND_VALUES:
            with self.subTest(value=value):
                expected = 0 if _SPLIT.normalise_profile(value) == 'tower_v1' else 1
                self.assertEqual(slot_verdict('scene', value), expected)

    def test_only_the_scene_slot_uses_the_tower_node(self):
        for name in ('gripper', '', 'Scene', 'scene ', 'phone', 'scene_studio'):
            with self.subTest(name=name):
                self.assertEqual(slot_verdict(name, 'tower_v1'), 1)
        self.assertEqual(slot_verdict('scene', 'tower_v1'), 0)

    def test_the_unset_key_is_legacy(self):
        self.assertEqual(entrypoint_profile({}), 'legacy')
        self.assertEqual(entrypoint_profile({KEY: ''}), 'legacy')
        self.assertEqual(entrypoint_profile({KEY: '"tower_v1"'}), 'tower_v1')


def _bash4():
    """A bash of version 4 or newer (the usb_cam loop's ``declare -A``)."""
    for candidate in ('bash', '/opt/homebrew/bin/bash', '/usr/local/bin/bash'):
        path = shutil.which(candidate)
        if path is None:
            continue
        out = subprocess.run([path, '-c', 'echo "${BASH_VERSINFO[0]}"'],
                             capture_output=True, text=True, check=False).stdout.strip()
        if out.isdigit() and int(out) >= 4:
            return path
    return None


def _usb_cam_loop():
    """The usb_cam camera loop, from ``declare -A SEEN_REAL_DEV`` to its
    closing ``done``."""
    match = re.search(r'^declare -A SEEN_REAL_DEV\b.*?^done\n', _read(_ENTRYPOINT), re.M | re.S)
    if match is None:
        raise AssertionError('the usb_cam loop moved — update this test')
    return match.group(0)


class TheUsbCamLoopStartsTheTowerNode(unittest.TestCase):
    """The loop (and the function it calls) under bash with ros2, python3 and
    the device wait stubbed: what each slot starts is printed instead of run."""

    STUBS = ('wait_for_camera() { return 0; }\n'
             'ros2() { echo "USB_CAM $*"; }\n'
             'python3() { echo "TOWER $*"; }\n'
             'PIDS=""\n')

    def setUp(self):
        self.bash = _bash4()
        if self.bash is None:
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                self.fail('no bash 4 on the CI runner: the usb_cam loop cannot be run')
            self.skipTest('needs bash >= 4 (the loop declares an associative array)')
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.devices = []
        for i in (1, 2):
            path = os.path.join(self.dir, f'video{i}')
            with open(path, 'w', encoding='utf-8'):
                pass
            self.devices.append(path)

    def run_loop(self, profile, names=('gripper', 'scene')):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('EDUBOTICS_', 'CAMERA_'))}
        for i, (device, name) in enumerate(zip(self.devices, names), 1):
            env[f'CAMERA_DEVICE_{i}'] = device
            env[f'CAMERA_NAME_{i}'] = name
        if profile is not None:
            env[KEY] = profile
        script = (self.STUBS + _function_source() + _usb_cam_loop()
                  + 'wait\necho "PIDS:$PIDS"\n')
        result = subprocess.run([self.bash, '-c', script], env=env, capture_output=True,
                                text=True, check=True)
        lines = result.stdout.splitlines()
        started = sorted(line for line in lines if line.startswith(('USB_CAM ', 'TOWER ')))
        pids = next(line for line in lines if line.startswith('PIDS:'))[len('PIDS:'):].split()
        return started, pids

    def test_tower_v1_starts_the_tower_node_for_the_scene_slot_only(self):
        started, pids = self.run_loop('tower_v1')
        self.assertEqual(len(started), 2, started)
        self.assertEqual(started[1], f'USB_CAM launch open_manipulator_bringup '
                                     f'camera_usb_cam.launch.py name:=gripper '
                                     f'video_device:={self.devices[0]} image_width:=640 '
                                     f'image_height:=480 framerate:=30.0 pixel_format:=yuyv')
        self.assertEqual(started[0], f'TOWER /usr/local/bin/scene_camera_node.py '
                                     f'--device {self.devices[1]} --name scene')
        self.assertEqual(len(pids), 2, 'both slots are recorded for the cleanup trap')

    def test_legacy_and_unset_start_usb_cam_for_both_slots(self):
        for profile in ('legacy', None, '', 'tower'):
            with self.subTest(profile=profile):
                started, pids = self.run_loop(profile)
                self.assertEqual([s.split(' name:=')[1].split()[0] for s in started],
                                 ['gripper', 'scene'])
                self.assertTrue(all(s.startswith('USB_CAM ') for s in started), started)
                self.assertEqual(len(pids), 2)

    def test_a_gripper_only_rig_never_starts_the_tower_node(self):
        started, _pids = self.run_loop('tower_v1', names=('gripper', 'gripper2'))
        self.assertTrue(all(s.startswith('USB_CAM ') for s in started), started)


if __name__ == '__main__':
    unittest.main()
