# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""D82 X-2: the setup window's camera preview shows exactly the field the
native camera bridge records.

``camera_bridge._CaptureWorker.run`` centre-crops a frame of another aspect
ratio to the target's (640x480 = 4:3 by default) before its resize, so a
16:9 camera records its centre 4:3. The preview (``gui_app.py``'s
``_frame_to_photo_data``) only downscaled the frame for display, so with such
a camera the student aimed it looking at a wider picture than the dataset
gets. Both now call ONE helper, ``camera_bridge.crop_to_target_aspect``.

gui_app imports tkinter at module level and the CI venv has none, so the
preview function is taken out of gui_app.py with ``ast`` and run on its own,
with ``camera_bridge`` (tkinter-free) as the ``camera_bridge_mod`` it names.
The pixel tests need OpenCV and numpy and skip without them, like
test_camera_preview_render.py; the one-implementation fence needs neither.
"""

import ast
import base64
import os
import unittest
from unittest import mock

from gui.app import camera_bridge, constants

try:  # numpy/cv2 may be absent on a bare runner.
    import cv2
    import numpy as np
    _HAVE_CV2 = True
except ImportError:  # pragma: no cover - environment dependent
    _HAVE_CV2 = False

_APP_DIR = os.path.join(os.path.dirname(__file__), '..', 'gui', 'app')
_GUI_SRC = os.path.join(_APP_DIR, 'gui_app.py')
_BRIDGE_SRC = os.path.join(_APP_DIR, 'camera_bridge.py')


def _tree(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return ast.parse(fh.read(), filename=path)


def _function(tree, name, owner=None):
    """The top-level function ``name``, or the method ``name`` of ``owner``."""
    body = tree.body
    if owner is not None:
        body = next(node.body for node in body
                    if isinstance(node, ast.ClassDef) and node.name == owner)
    return next(node for node in body
                if isinstance(node, ast.FunctionDef) and node.name == name)


def _calls(func):
    """Every call in ``func`` as its dotted name (``mod.fn`` or ``fn``)."""
    names = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            names.add(f'{target.value.id}.{target.attr}')
    return names


def _load_frame_to_photo_data():
    """``_frame_to_photo_data`` exactly as gui_app.py defines it."""
    func = _function(_tree(_GUI_SRC), '_frame_to_photo_data')
    ns = {'base64': base64, 'camera_bridge_mod': camera_bridge}
    exec(compile(ast.Module(body=[func], type_ignores=[]), _GUI_SRC, 'exec'), ns)  # noqa: S102
    return ns['_frame_to_photo_data']


def _decode(data):
    return cv2.imdecode(np.frombuffer(base64.b64decode(data), np.uint8),
                        cv2.IMREAD_COLOR)


def _old_preview(frame):
    """The preview before the crop: a 320-px-wide area downscale only."""
    img = frame
    if img.shape[1] > 320:
        img = cv2.resize(img, (320, max(1, round(img.shape[0] * 320 / img.shape[1]))),
                         interpolation=cv2.INTER_AREA)
    return base64.b64encode(cv2.imencode('.png', img)[1].tobytes())


class _TargetIs640x480(unittest.TestCase):

    def setUp(self):
        for name, value in (('CAMERA_WIDTH', 640), ('CAMERA_HEIGHT', 480)):
            patcher = mock.patch.object(constants, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class ThePreviewShowsTheRecordedField(_TargetIs640x480):

    def setUp(self):
        super().setUp()
        self.preview = _load_frame_to_photo_data()

    def test_a_16_9_frame_loses_its_sides_in_the_preview(self):
        # The outer 160 px on each side of a 1280x720 frame are what the
        # bridge cuts away; paint them a colour the centre does not have.
        frame = np.full((720, 1280, 3), 100, np.uint8)
        frame[:, :160] = (0, 255, 0)
        frame[:, 1120:] = (0, 255, 0)
        shown = _decode(self.preview(frame))
        green = (shown[:, :, 1] > 150) & (shown[:, :, 0] < 50)
        self.assertFalse(green.any(), f'{int(green.sum())} side-column pixels shown')
        self.assertEqual(shown.shape, (240, 320, 3))
        expected = cv2.resize(frame[:, 160:1120], (320, 240), interpolation=cv2.INTER_AREA)
        self.assertTrue(np.array_equal(shown, expected))

    def test_a_taller_frame_loses_its_top_and_bottom_in_the_preview(self):
        # CIF: 352x288 keeps 264 rows, 12 cut at the top and 12 at the bottom.
        frame = np.random.default_rng(352).integers(0, 256, (288, 352, 3), dtype=np.uint8)
        shown = _decode(self.preview(frame))
        expected = cv2.resize(frame[12:276], (320, 240), interpolation=cv2.INTER_AREA)
        self.assertEqual(shown.shape, (240, 320, 3))
        self.assertTrue(np.array_equal(shown, expected))

    def test_a_4_3_frame_gives_the_same_preview_bytes_as_before(self):
        for width, height in ((640, 480), (1280, 960), (320, 240)):
            with self.subTest(size=f'{width}x{height}'):
                frame = np.random.default_rng(width).integers(
                    0, 256, (height, width, 3), dtype=np.uint8)
                self.assertEqual(self.preview(frame), _old_preview(frame))


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class TheCropHelper(_TargetIs640x480):

    def test_the_centre_field_of_each_shape(self):
        frame = np.arange(720 * 1280 * 3, dtype=np.uint32).reshape(720, 1280, 3)
        self.assertTrue(np.array_equal(camera_bridge.crop_to_target_aspect(frame),
                                       frame[:, 160:1120]))
        frame = np.arange(288 * 352 * 3, dtype=np.uint32).reshape(288, 352, 3)
        self.assertTrue(np.array_equal(camera_bridge.crop_to_target_aspect(frame),
                                       frame[12:276]))
        frame = np.arange(960 * 960 * 3, dtype=np.uint32).reshape(960, 960, 3)
        self.assertTrue(np.array_equal(camera_bridge.crop_to_target_aspect(frame),
                                       frame[120:840]))

    def test_a_frame_of_the_target_aspect_is_returned_as_it_is(self):
        for width, height in ((640, 480), (1280, 960), (320, 240)):
            with self.subTest(size=f'{width}x{height}'):
                frame = np.zeros((height, width, 3), np.uint8)
                self.assertIs(camera_bridge.crop_to_target_aspect(frame), frame)

    def test_a_one_pixel_wide_frame_keeps_one_row(self):
        frame = np.zeros((480, 1, 3), np.uint8)
        self.assertEqual(camera_bridge.crop_to_target_aspect(frame).shape, (1, 1, 3))


class OneImplementation(unittest.TestCase):
    """The preview and the capture worker call the same helper; neither keeps
    a copy of the crop arithmetic that could drift from the other."""

    def test_the_preview_calls_the_bridge_helper(self):
        func = _function(_tree(_GUI_SRC), '_frame_to_photo_data')
        self.assertIn('camera_bridge_mod.crop_to_target_aspect', _calls(func))

    def test_gui_app_names_the_bridge_module_camera_bridge_mod(self):
        imports = [node for node in _tree(_GUI_SRC).body
                   if isinstance(node, ast.ImportFrom) and node.level == 1 and not node.module]
        aliases = {(alias.name, alias.asname) for node in imports for alias in node.names}
        self.assertIn(('camera_bridge', 'camera_bridge_mod'), aliases)

    def test_the_capture_worker_calls_the_helper(self):
        run = _function(_tree(_BRIDGE_SRC), 'run', owner='_CaptureWorker')
        self.assertIn('crop_to_target_aspect', _calls(run))
        names = {node.id for node in ast.walk(run) if isinstance(node, ast.Name)}
        self.assertFalse(names & {'crop_w', 'crop_h', 'x0', 'y0'})


if __name__ == '__main__':
    unittest.main()
