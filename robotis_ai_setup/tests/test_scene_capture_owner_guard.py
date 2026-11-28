# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Guard G10 (spec 40 S1): under ``gui/app`` only ``win_camera.py`` sets a
capture property, and the server package sets none.

Decision D4 freezes the tower camera in one mode, opened by
``win_camera.open_scene_capture`` with the open-method constant
``SCENE_OPEN_PARAMS`` (1920x1080 at 30 fps) and ``CAP_PROP_CONVERT_RGB`` 0 so
the camera's own JPEG is passed through (back to 1, the backend decoding, when
Windows runs the camera in another format); Roboter Studio cannot change a camera
setting. A capture property set anywhere else (an exposure call planted in
gui_app.py, say) would change the recorded picture behind the verifier's back,
so the guard scans the sources by AST: a ``.set(...)`` whose first argument
names a ``CAP_PROP_`` constant. Its mutation plants exactly that call in a copy
of gui_app.py.
"""

import ast
import os
import pathlib
import re
import types
import unittest
from unittest import mock

from gui.app import scene_split, win_camera

_SETUP = pathlib.Path(__file__).resolve().parent.parent
_GUI_APP_DIR = _SETUP / 'gui' / 'app'
_SERVER_PKG = (_SETUP.parent / 'physical_ai_tools' / 'physical_ai_server'
               / 'physical_ai_server')


def capture_property_setters(source):
    """[(line, call)] of every ``<x>.set(<…CAP_PROP_…>, …)`` in ``source``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'set' and node.args
                and 'CAP_PROP_' in ast.unparse(node.args[0])):
            found.append((node.lineno, ast.unparse(node)))
    return found


def _setters_by_file(root):
    out = {}
    for path in sorted(root.rglob('*.py')):
        found = capture_property_setters(path.read_text(encoding='utf-8'))
        if found:
            out[str(path.relative_to(root))] = found
    return out


class OnlyWinCameraSetsACaptureProperty(unittest.TestCase):

    def test_gui_app_has_one_owner(self):
        self.assertEqual(set(_setters_by_file(_GUI_APP_DIR)), {'win_camera.py'})

    def test_the_server_package_sets_none(self):
        self.assertTrue(_SERVER_PKG.is_dir(), _SERVER_PKG)
        self.assertEqual(_setters_by_file(_SERVER_PKG), {})

    def test_the_mutation_is_caught(self):
        """``cap.set(cv2.CAP_PROP_EXPOSURE, 0)`` planted in a copy of gui_app.py."""
        source = (_GUI_APP_DIR / 'gui_app.py').read_text(encoding='utf-8')
        self.assertEqual(capture_property_setters(source), [])
        planted = source + '\n\ndef _planted(cap, cv2):\n    cap.set(cv2.CAP_PROP_EXPOSURE, 0)\n'
        found = capture_property_setters(planted)
        self.assertEqual([call for _line, call in found], ['cap.set(cv2.CAP_PROP_EXPOSURE, 0)'])

    def test_the_tower_mode_is_set_in_open_scene_capture(self):
        """The GUI's two CONVERT_RGB calls (the passthrough, and the decoded
        fallback for a camera Windows runs in another format) sit in
        open_scene_capture and nowhere else."""
        tree = ast.parse((_GUI_APP_DIR / 'win_camera.py').read_text(encoding='utf-8'))
        func = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == 'open_scene_capture')
        expected = ['cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)', 'cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)']
        self.assertEqual([call for _line, call in capture_property_setters(ast.unparse(func))
                          if 'CONVERT_RGB' in call], expected)
        everywhere = [call for calls in _setters_by_file(_GUI_APP_DIR).values()
                      for _line, call in calls if 'CONVERT_RGB' in call]
        self.assertEqual(everywhere, expected)


_MJPG, _NV12, _YUY2 = 0x47504A4D, 0x3231564E, 0x32595559   # FOURCCs as MSMF reads them back
_RGB24_GUID_DATA1 = 20                                       # MSMF's decoded output format


class _FakeCap:
    """Reads back ``native`` (the camera's FOURCC) while CONVERT_RGB is 0 and
    the decoded output's GUID while it is 1, as OpenCV's MSMF backend does."""

    def __init__(self, calls, opened=True, native=_MJPG):
        self.calls = calls
        self.opened = opened
        self.native = native
        self.convert = 1

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.calls.append(('set', prop, value))
        if prop == 16:
            self.convert = value
        return True

    def get(self, prop):
        return {3: 1920.0, 4: 1080.0, 5: 30.0,
                6: float(self.native if self.convert == 0 else _RGB24_GUID_DATA1)}.get(prop, 0.0)

    def read(self):
        self.calls.append(('read',))
        return True, 'frame'

    def release(self):
        self.calls.append(('release',))


def _fake_cv2(calls, opened=True, native=_MJPG):
    def video_capture(*args):
        calls.append(('VideoCapture',) + tuple(
            list(a) if isinstance(a, (list, tuple)) else a for a in args))
        return _FakeCap(calls, opened, native)

    return types.SimpleNamespace(
        CAP_MSMF=1400, CAP_DSHOW=700, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5, CAP_PROP_FOURCC=6, CAP_PROP_CONVERT_RGB=16,
        VideoCapture=video_capture,
        VideoWriter_fourcc=lambda *chars: ''.join(chars))


class OpenSceneCapture(unittest.TestCase):

    def _open(self, backend, opened=True, native=_MJPG, want_cap=False):
        calls = []
        with mock.patch.object(win_camera, '_import_cv2',
                               return_value=_fake_cv2(calls, opened, native)), \
                mock.patch.dict(os.environ, {'EDUBOTICS_CAMERA_BACKEND': backend}):
            if not opened:
                with self.assertRaises(win_camera.CameraUnavailableError):
                    win_camera.open_scene_capture(2)
                return calls
            cap = win_camera.open_scene_capture(2)
        self.assertIsInstance(cap, win_camera.SceneCapture)
        self.assertIsInstance(cap.cap, _FakeCap)
        return (calls, cap) if want_cap else calls

    def test_the_open_method_constant(self):
        self.assertEqual(list(win_camera.SCENE_OPEN_PARAMS),
                         [3, scene_split.CAPTURE_W, 4, scene_split.CAPTURE_H, 5,
                          scene_split.CAPTURE_FPS])
        self.assertEqual(list(win_camera.SCENE_OPEN_PARAMS), [3, 1920, 4, 1080, 5, 30])

    def test_msmf_opens_with_the_params_and_passes_the_jpeg_through(self):
        self.assertEqual(self._open('msmf'), [
            ('VideoCapture', 2, 1400, [3, 1920, 4, 1080, 5, 30]),
            ('set', 16, 0),
        ])

    def test_dshow_keeps_the_dshow_order_and_passes_the_jpeg_through(self):
        self.assertEqual(self._open('dshow'), [
            ('VideoCapture', 2, 700),
            ('set', 3, 1920), ('set', 4, 1080), ('set', 5, 30), ('set', 6, 'MJPG'),
            ('set', 16, 0),
        ])

    def test_an_mjpeg_camera_is_passed_through_and_its_mode_read_back(self):
        _calls, cap = self._open('msmf', want_cap=True)
        self.assertEqual(cap.mode, win_camera.SceneMode('MJPG', 1920, 1080, 30.0, passthrough=True))

    def test_another_format_lets_the_backend_decode(self):
        """OpenCV's MSMF picks the camera mode by size and rate only, so the
        camera may run 1920x1080 as NV12 or YUY2: its raw buffers are no JPEG,
        so CONVERT_RGB goes back to 1 and the verifier judges decoded frames."""
        for native, name in ((_NV12, 'NV12'), (_YUY2, 'YUY2')):
            with self.subTest(native=name):
                calls, cap = self._open('msmf', native=native, want_cap=True)
                self.assertEqual(calls, [
                    ('VideoCapture', 2, 1400, [3, 1920, 4, 1080, 5, 30]),
                    ('set', 16, 0), ('set', 16, 1),
                ])
                self.assertEqual(cap.mode, win_camera.SceneMode(name, 1920, 1080, 30.0,
                                                                passthrough=False))

    def test_an_unreadable_format_is_decoded_too(self):
        calls, cap = self._open('msmf', native=0, want_cap=True)
        self.assertEqual(calls[-1], ('set', 16, 1))
        self.assertEqual((cap.mode.fourcc, cap.mode.passthrough), ('', False))

    def test_dshow_reads_the_format_back_the_same_way(self):
        calls, cap = self._open('dshow', native=_YUY2, want_cap=True)
        self.assertEqual(calls[-2:], [('set', 16, 0), ('set', 16, 1)])
        self.assertIs(cap.mode.passthrough, False)

    def test_the_scene_capture_reads_and_releases_the_device(self):
        calls, cap = self._open('msmf', want_cap=True)
        self.assertEqual(cap.read(), (True, 'frame'))
        cap.release()
        self.assertEqual(calls[-2:], [('read',), ('release',)])

    def test_a_fourcc_is_four_printable_characters(self):
        self.assertEqual([win_camera._fourcc_text(v) for v in
                          (_MJPG, _NV12, 0x34363248, _RGB24_GUID_DATA1, 0, -1.0,
                           float('nan'), None)],
                         ['MJPG', 'NV12', 'H264', '', '', '', '', ''])

    def test_a_device_that_does_not_open_is_released_and_refused(self):
        calls = self._open('msmf', opened=False)
        self.assertEqual(calls[-1], ('release',))
        self.assertNotIn(('set', 16, 0), calls)

    def test_the_property_ids_are_opencvs(self):
        try:
            import cv2
        except ImportError:  # pragma: no cover - environment dependent
            self.skipTest('cv2 not available')
        self.assertEqual(list(win_camera.SCENE_OPEN_PARAMS),
                         [cv2.CAP_PROP_FRAME_WIDTH, 1920, cv2.CAP_PROP_FRAME_HEIGHT, 1080,
                          cv2.CAP_PROP_FPS, 30])


class TheBackendDocstringClaimsNoRate(unittest.TestCase):
    """Spec 40 §2 correction 31: the two notes of 2026-05-24 disagree on the
    legacy camera's rate; rig gate RG-4 measures it."""

    def test_no_rate_and_a_pointer_to_rg4(self):
        doc = win_camera._capture_backend.__doc__
        self.assertIn('RG-4', doc)
        self.assertIsNone(re.search(r'\d\s*(fps|Hz|Bilder)', doc), doc)
        self.assertNotIn('physically unreachable', doc)


if __name__ == '__main__':
    unittest.main()
