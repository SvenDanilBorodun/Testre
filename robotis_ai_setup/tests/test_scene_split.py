# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""``scene_split`` (WP-04): the tower camera's one derivation, its goldens,
its start-up self-test and its capture verifier.

The tower camera captures 1920x1080; the recorded scene picture is the
1280x960 window at (320, 60) reduced exactly 2:1 to 640x480. Two derivations
exist and both are fenced here: ``outside_from_jpeg`` (the camera's own JPEG
decoded at half size, then cropped; no full decode) and ``outside_from_bgr``
(a decoded frame's window, ``INTER_AREA`` 2:1, which is the rounded 2x2 block
mean). Their outputs are pinned bit for bit on CI's OpenCV (the goldens; a
golden that differs is reported, never edited), and the module's own
``selftest()`` re-proves the derivation with tolerances on whatever OpenCV it
runs on (the GUI ships another one).

``CaptureVerifier`` is the pure state machine both capture owners run
(closed -> opening -> verifying -> streaming -> fault, time injected): only in
``streaming`` may a frame leave, every frame is checked (decision D59: a
passthrough frame must be a COMPLETE JPEG, ``jpeg_complete``'s SOS-anchored
end-of-image rule), a refused frame is dropped and counted, and a decoded
stream must run at ``MIN_MODE_FPS`` (10).

The pixel tests need OpenCV and numpy and skip without them, like
test_camera_preview_render.py; everything else (the JPEG header rules, the
verifier, the profile table, the state message) runs without either.
"""

import hashlib
import json
import os
import time
import unittest
from unittest import mock

from gui.app import scene_split as ss

try:  # numpy may be absent on a bare runner.
    import numpy as np
    _HAVE_NUMPY = True
except ImportError:  # pragma: no cover - environment dependent
    _HAVE_NUMPY = False

try:  # numpy/cv2 may be absent on a bare runner.
    import cv2
    _HAVE_CV2 = _HAVE_NUMPY
except ImportError:  # pragma: no cover - environment dependent
    _HAVE_CV2 = False

_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'fixtures', 'scene_split_1080p.jpg')
_FIXTURE_SHA256 = '8fe7cf36784e7dd092ade1e66bd7f156907fa532ddc973fb9c57454862cb97df'
# The goldens (spec 40 S1 / A2, G5): sha256 of the 480x640x3 uint8 output's bytes.
_GOLDEN_FROM_JPEG = '41e96521634bbf3f82760c7f63ae06e37669f2051709efbf663dad40ffd1bd62'
_GOLDEN_FROM_BGR = '3ffa7b87e1751649e2d3efc781a538b5410db735d720cad6d8dcc648df6b275a'

with open(_FIXTURE, 'rb') as _fh:
    GOOD = _fh.read()

EOI = b'\xff\xd9'
SHIFTS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _sha256(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def _segment(marker, payload):
    return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, 'big') + payload


def _jpeg_header(width, height, sof=0xC0, tail=EOI):
    """A JPEG with every header segment the walk reads (APP0, DQT, SOF, DHT,
    SOS), a few entropy bytes and ``tail``. No decoder needed: the verifier
    and the two header rules read headers only."""
    sof_payload = (bytes((8,)) + height.to_bytes(2, 'big') + width.to_bytes(2, 'big')
                   + bytes((3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1)))
    return (b'\xff\xd8'
            + _segment(0xE0, b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00')
            + _segment(0xDB, bytes(65))
            + _segment(sof, sof_payload)
            + _segment(0xC4, bytes(17) + b'\x00')
            + _segment(0xDA, bytes((3, 1, 0, 2, 0x11, 3, 0x11, 0, 0x3F, 0)))
            + b'\x12\x34\xff\x00\x56'
            + tail)


def _sos_end(data):
    i = data.find(b'\xff\xda')
    return i + 2 + ((data[i + 2] << 8) | data[i + 3])


def _truncation_points(n):
    """Every 1 per mille from 1 % to 99.9 %, and each of the last 4 096 byte
    positions (spec 40 WP-04)."""
    return sorted(set(int(n * k / 1000) for k in range(10, 1000))
                  | set(range(max(4, n - 4096), n)))


# --------------------------------------------------------------------------
# The profile key, the constants and the profile ids
# --------------------------------------------------------------------------

class ProfileValueTable(unittest.TestCase):
    """``normalise_profile`` over the one value table (spec 40 S1): blanks and
    quotes stripped, lower-cased, ``tower_v1`` is the tower and anything
    else, absence included, is ``legacy``."""

    TABLE = (
        ('tower_v1', 'tower_v1'),
        ('"tower_v1"', 'tower_v1'),
        ("'tower_v1'", 'tower_v1'),
        (' tower_v1 ', 'tower_v1'),
        ('TOWER_V1', 'tower_v1'),
        (' "Tower_V1" ', 'tower_v1'),
        ('\ttower_v1\r\n', 'tower_v1'),
        ('legacy', 'legacy'),
        ('LEGACY', 'legacy'),
        ('', 'legacy'),
        ('  ', 'legacy'),
        ('""', 'legacy'),
        (None, 'legacy'),
        ('auto', 'legacy'),
        ('tower', 'legacy'),
        ('TOWER_V2', 'legacy'),
        ('tower_v1x', 'legacy'),
        ('tower v1', 'legacy'),
        ('tower_v1;legacy', 'legacy'),
        (b'tower_v1', 'legacy'),
        (1, 'legacy'),
    )

    def test_the_value_table(self):
        for raw, expected in self.TABLE:
            with self.subTest(raw=raw):
                self.assertEqual(ss.normalise_profile(raw), expected)

    def test_profile_values_name_exactly_the_two_profiles(self):
        self.assertEqual(ss.PROFILE_VALUES, {'tower_v1': 'tower_v1', 'legacy': 'legacy'})
        self.assertEqual((ss.PROFILE_LEGACY, ss.PROFILE_TOWER), ('legacy', 'tower_v1'))
        outputs = {ss.normalise_profile(raw) for raw, _ in self.TABLE}
        self.assertEqual(outputs, set(ss.PROFILE_VALUES.values()))


class Constants(unittest.TestCase):

    def test_the_geometry(self):
        self.assertEqual((ss.CAPTURE_W, ss.CAPTURE_H, ss.CAPTURE_FPS), (1920, 1080, 30))
        self.assertEqual((ss.WINDOW_X0, ss.WINDOW_Y0, ss.WINDOW_W, ss.WINDOW_H), (320, 60, 1280, 960))
        self.assertEqual((ss.OUT_W, ss.OUT_H), (640, 480))
        # The window lies inside the capture, reduces exactly 2:1, and starts on
        # an even pixel, so the half-size decode's crop is the same window.
        self.assertLessEqual(ss.WINDOW_X0 + ss.WINDOW_W, ss.CAPTURE_W)
        self.assertLessEqual(ss.WINDOW_Y0 + ss.WINDOW_H, ss.CAPTURE_H)
        self.assertEqual((ss.WINDOW_W, ss.WINDOW_H), (2 * ss.OUT_W, 2 * ss.OUT_H))
        self.assertEqual((ss.WINDOW_X0 % 2, ss.WINDOW_Y0 % 2), (0, 0))

    def test_the_wire(self):
        self.assertEqual((ss.SCENE_CAM_ID, ss.STUDIO_CAM_ID, ss.STATE_CAM_ID, ss.HELLO_CAM_ID),
                         (1, 16, 17, 255))
        # Camera ids 0 (gripper) and 2 (phone) stay what they are.
        self.assertTrue({0, 2}.isdisjoint({ss.STUDIO_CAM_ID, ss.STATE_CAM_ID, ss.HELLO_CAM_ID}))
        self.assertEqual(ss.HELLO, b'\x01')
        self.assertEqual(ss.STUDIO_MAX_HZ, 15.0)
        self.assertEqual(ss.STUDIO_TOPIC, '/studio/tower/compressed')
        self.assertEqual(ss.STATE_TOPIC, '/edubotics/scene_camera_state')
        self.assertEqual(ss.STATE_SCHEMA, 1)
        # The composes' healthchecks grep for these; the studio topic must match neither (G8).
        for pattern in ('/scene/image_raw', '/gripper/image_raw'):
            self.assertNotIn(pattern, ss.STUDIO_TOPIC)

    def test_the_rate_limits(self):
        """D59: a decoded stream needs 10 fps (revision 2's 20.0 switched a dim
        room's camera off); below 27 the state says fps_low (C9)."""
        self.assertEqual(ss.MIN_MODE_FPS, 10.0)
        self.assertEqual(ss.LOW_FPS, 27.0)


class ProfileIds(unittest.TestCase):

    def test_the_two_profile_ids(self):
        """G6: the id hashes the window and the sizes; changing one changes it."""
        self.assertEqual(ss.profile_id(80), '3efca10f3a05')
        self.assertEqual(ss.profile_id(95), '8b7f72cb250c')

    def test_the_profile_string(self):
        self.assertEqual(
            ss.PROFILE_STRING,
            'tower_v1|1920x1080@30|MJPG|window=320,60,1280,960|out=640x480|reduce=half_decode_crop|q={q}')
        self.assertEqual(ss.profile_id(80),
                         hashlib.sha256(ss.PROFILE_STRING.format(q=80).encode()).hexdigest()[:12])

    def test_a_changed_window_changes_the_id(self):
        with mock.patch.object(ss, 'PROFILE_STRING', ss.PROFILE_STRING.replace('320,60', '321,60')):
            self.assertNotEqual(ss.profile_id(80), '3efca10f3a05')

    def test_only_a_jpeg_quality_is_accepted(self):
        for bad in (0, 101, -5, 80.0, '80', True, None):
            with self.subTest(quality=bad):
                with self.assertRaises(ValueError):
                    ss.profile_id(bad)


# --------------------------------------------------------------------------
# The JPEG header rules: jpeg_size and jpeg_complete (no decode)
# --------------------------------------------------------------------------

class JpegSize(unittest.TestCase):

    def test_the_fixture_is_1920x1080(self):
        self.assertEqual(hashlib.sha256(GOOD).hexdigest(), _FIXTURE_SHA256)
        self.assertEqual(ss.jpeg_size(GOOD), (1920, 1080))

    def test_sof0_sof1_sof2_are_read(self):
        for sof in (0xC0, 0xC1, 0xC2):
            with self.subTest(sof=hex(sof)):
                self.assertEqual(ss.jpeg_size(_jpeg_header(1280, 720, sof=sof)), (1280, 720))

    def test_other_frame_kinds_and_broken_headers_have_no_size(self):
        for name, data in (
                ('lossless SOF3', _jpeg_header(640, 480, sof=0xC3)),
                ('arithmetic SOF9', _jpeg_header(640, 480, sof=0xC9)),
                ('no SOI', GOOD[2:]),
                ('cut inside the SOF', GOOD[:GOOD.find(b'\xff\xc0') + 6]),
                ('empty', b''),
                ('not bytes', 'not a jpeg'),
                ('None', None)):
            with self.subTest(name):
                self.assertIsNone(ss.jpeg_size(data))

    def test_bytes_like_inputs(self):
        self.assertEqual(ss.jpeg_size(bytearray(GOOD)), (1920, 1080))
        self.assertEqual(ss.jpeg_size(memoryview(GOOD)), (1920, 1080))

    @unittest.skipUnless(_HAVE_NUMPY, 'needs numpy')
    def test_a_one_row_uint8_sample_is_bytes(self):
        """MSMF hands a passthrough sample over as a 1xN uint8 array."""
        sample = np.frombuffer(GOOD, np.uint8).reshape(1, -1)
        self.assertEqual(ss.jpeg_size(sample), (1920, 1080))
        self.assertEqual(ss.jpeg_size(sample.reshape(-1)), (1920, 1080))


class JpegComplete(unittest.TestCase):
    """D59, spec 40 S1 revision 5: SOI, the marker walk to the FIRST SOS, and
    an EOI strictly after that scan header."""

    def test_whole_frames_are_complete(self):
        self.assertTrue(ss.jpeg_complete(GOOD))
        self.assertTrue(ss.jpeg_complete(_jpeg_header(1920, 1080)))

    def test_bytes_after_the_eoi_never_drop_a_frame(self):
        """Zero padding, FF fill and anything else some UVC cameras append after
        the EOI; and a second JPEG. Revision 3's "ends in FF D9" rule refused
        the non-fill garbage, so such a camera streamed nothing."""
        garbage = bytes(range(0x10, 0x21))  # 17 bytes, none of them 00 or FF
        self.assertEqual(len(garbage), 17)
        for name, tail in (('517 zero bytes', bytes(517)), ('FF FF', b'\xff\xff'),
                           ('17 bytes of non-fill garbage', garbage),
                           ('a second JPEG', GOOD)):
            with self.subTest(name):
                self.assertTrue(ss.jpeg_complete(GOOD + tail))

    @unittest.skipUnless(_HAVE_CV2, 'needs OpenCV and numpy')
    def test_the_garbage_tail_frame_decodes(self):
        frame = cv2.imdecode(np.frombuffer(GOOD + bytes(range(0x10, 0x21)), np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.shape, (1080, 1920, 3))

    def test_every_truncation_of_the_fixture_is_incomplete(self):
        points = _truncation_points(len(GOOD))
        self.assertEqual(len(points), 5073)
        passing = [n for n in points if ss.jpeg_complete(GOOD[:n])]
        self.assertEqual(passing, [])

    @unittest.skipUnless(_HAVE_CV2, 'needs OpenCV and numpy')
    def test_every_truncation_of_a_progressive_encode_is_incomplete(self):
        """Ten scans, each a SOS: the rule anchors on the FIRST and still finds
        only the final EOI."""
        img = cv2.imdecode(np.frombuffer(GOOD, np.uint8), cv2.IMREAD_COLOR)
        ok, enc = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 90,
                                             cv2.IMWRITE_JPEG_PROGRESSIVE, 1])
        self.assertTrue(ok)
        prog = enc.tobytes()
        self.assertGreater(prog.count(b'\xff\xda'), 1)
        self.assertTrue(ss.jpeg_complete(prog))
        self.assertEqual(ss.jpeg_size(prog), (1920, 1080))
        points = _truncation_points(len(prog))
        self.assertGreater(len(points), 5000)
        self.assertEqual([n for n in points if ss.jpeg_complete(prog[:n])], [])

    @unittest.skipUnless(_HAVE_CV2, 'needs OpenCV and numpy')
    def test_an_embedded_thumbnail_eoi_is_skipped(self):
        """An EXIF thumbnail's own FF D9 lies inside its APP1 segment, before
        the scan; the walk skips it by length."""
        img = cv2.imdecode(np.frombuffer(GOOD, np.uint8), cv2.IMREAD_COLOR)
        ok, thumb = cv2.imencode('.jpg', cv2.resize(img, (160, 90)), [cv2.IMWRITE_JPEG_QUALITY, 80])
        app1 = _segment(0xE1, b'Exif\x00\x00' + thumb.tobytes())
        framed = GOOD[:2] + app1 + GOOD[2:]
        self.assertTrue(ss.jpeg_complete(framed))
        self.assertEqual(ss.jpeg_size(framed), (1920, 1080))
        self.assertEqual([n for n in _truncation_points(len(framed))
                          if ss.jpeg_complete(framed[:n])], [])

    def test_an_empty_scan_is_incomplete(self):
        """Revision 5: the EOI must lie strictly after the scan header
        (``rfind(EOI, sos_end) > sos_end``); revision 4's ``>= 0`` accepted a
        frame whose EOI follows its SOS header at once."""
        end = _sos_end(GOOD)
        self.assertFalse(ss.jpeg_complete(GOOD[:end] + EOI))
        self.assertFalse(ss.jpeg_complete(GOOD[:end] + EOI + bytes(64)))
        self.assertFalse(ss.jpeg_complete(GOOD[:end]))
        self.assertTrue(ss.jpeg_complete(GOOD[:end] + b'\x00' + EOI))

    def test_broken_headers_are_incomplete(self):
        sos = GOOD.find(b'\xff\xda')
        for name, data in (
                ('no SOI', GOOD[2:]),
                ('EOI before the scan', b'\xff\xd8\xff\xd9' + GOOD[2:]),
                ('a second SOI before the scan', b'\xff\xd8' + GOOD),
                # Followed by 00 02, the two markers would read as a valid
                # length and the walk go on to the real header: only the
                # SOI/EOI rule itself refuses these.
                ('EOI before the scan, a length after it',
                 b'\xff\xd8\xff\xd9\x00\x02' + GOOD[2:]),
                ('a second SOI before the scan, a length after it',
                 b'\xff\xd8\xff\xd8\x00\x02' + GOOD[2:]),
                ('a byte where a marker must start', GOOD[:2] + b'\x00' + GOOD[2:]),
                ('a length below 2', GOOD[:2] + b'\xff\xe0\x00\x01' + GOOD[2:]),
                ('a length past the data', GOOD[:2] + b'\xff\xe0\xff\xff' + GOOD[2:200]),
                ('no SOS at all', GOOD[:sos] + EOI),
                ('fill bytes to the end', b'\xff\xd8\xff\xff\xff'),
                ('empty', b''),
                ('SOI only', b'\xff\xd8'),
                ('not bytes', 'text'),
                ('None', None)):
            with self.subTest(name):
                self.assertFalse(ss.jpeg_complete(data))

    def test_fill_bytes_and_standalone_markers_in_the_header(self):
        """A run of FF fill before a marker is skipped; RST0-7 and TEM carry no
        length."""
        self.assertTrue(ss.jpeg_complete(GOOD[:2] + b'\xff\xff\xff' + GOOD[2:]))
        self.assertTrue(ss.jpeg_complete(GOOD[:2] + b'\xff\x01\xff\xd0' + GOOD[2:]))

    def test_it_costs_at_most_5_us_per_frame(self):
        """Revision 4: at most 5 us (1.3 us measured on a development Mac with
        CI's pins); the best of five runs keeps a busy runner from failing it."""
        runs = []
        for _ in range(5):
            t0 = time.perf_counter()
            for _ in range(2000):
                ss.jpeg_complete(GOOD)
            runs.append((time.perf_counter() - t0) / 2000)
        self.assertLessEqual(min(runs), 5e-6, f'{min(runs) * 1e6:.2f} us per frame')


# --------------------------------------------------------------------------
# The two derivations, their goldens, and the exact 2:1 geometry (G3, G5)
# --------------------------------------------------------------------------

@unittest.skipUnless(_HAVE_CV2, 'needs OpenCV and numpy')
class Derivations(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.full = cv2.imdecode(np.frombuffer(GOOD, np.uint8), cv2.IMREAD_COLOR)

    def test_the_goldens(self):
        """Bit for bit on CI's OpenCV. A golden that differs is REPORTED (this
        failure), never edited: the probes proved both on OpenCV 4.13 and 4.6."""
        from_jpeg = ss.outside_from_jpeg(GOOD)
        from_bgr = ss.outside_from_bgr(self.full)
        for out in (from_jpeg, from_bgr):
            self.assertEqual(out.shape, (480, 640, 3))
            self.assertEqual(out.dtype, np.uint8)
            self.assertTrue(out.flags['C_CONTIGUOUS'])
        self.assertEqual(_sha256(from_jpeg), _GOLDEN_FROM_JPEG,
                         f'outside_from_jpeg golden differs on OpenCV {cv2.__version__}')
        self.assertEqual(_sha256(from_bgr), _GOLDEN_FROM_BGR,
                         f'outside_from_bgr golden differs on OpenCV {cv2.__version__}')

    def test_the_two_derivations_agree_and_a_moved_window_does_not(self):
        from_jpeg = ss.outside_from_jpeg(GOOD).astype(np.float64)
        same = np.abs(ss.outside_from_bgr(self.full) - from_jpeg).mean()
        self.assertLess(same, 1.0)
        for dx, dy in SHIFTS:
            with self.subTest(shift=(dx, dy)):
                moved = cv2.resize(self.full[60 + dy:1020 + dy, 320 + dx:1600 + dx], (640, 480),
                                   interpolation=cv2.INTER_AREA)
                self.assertGreater(np.abs(moved - from_jpeg).mean(), same)

    def test_from_bgr_is_the_rounded_2x2_block_mean(self):
        """G5: the exact 2:1 reduction, (a + b + c + d + 2) // 4 of the window."""
        rng = np.random.default_rng(20261120)
        noise = rng.integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
        for name, frame in (('fixture', self.full), ('noise', noise)):
            with self.subTest(name):
                w = frame[60:1020, 320:1600].astype(np.uint16)
                s = w[0::2, 0::2] + w[0::2, 1::2] + w[1::2, 0::2] + w[1::2, 1::2]
                np.testing.assert_array_equal(ss.outside_from_bgr(frame), ((s + 2) // 4).astype(np.uint8))

    def test_from_bgr_refuses_every_other_frame(self):
        for shape in ((1080, 1920), (1080, 1920, 4), (1080, 1920, 1), (720, 1280, 3),
                      (1081, 1920, 3), (1080, 1921, 3), (1079, 1920, 3), (960, 1280, 3),
                      (2160, 3840, 3), (540, 960, 3)):
            with self.subTest(shape=shape):
                with self.assertRaises(ss.SplitError):
                    ss.outside_from_bgr(np.zeros(shape, np.uint8))
        for name, frame in (('float frame', np.zeros((1080, 1920, 3), np.float32)),
                            ('bytes', GOOD), ('None', None)):
            with self.subTest(name):
                with self.assertRaises(ss.SplitError):
                    ss.outside_from_bgr(frame)

    def test_from_jpeg_refuses_every_other_size(self):
        """G3. 1919x1080 and 1920x1079 half-decode to 960x540 too, so the header
        size is checked as well."""
        for width, height in ((1280, 720), (1919, 1080), (1920, 1079), (1921, 1080),
                              (1920, 1081), (1920, 1088), (3840, 2160), (960, 540), (640, 480)):
            with self.subTest(size=(width, height)):
                ok, enc = cv2.imencode('.jpg', np.zeros((height, width, 3), np.uint8))
                self.assertTrue(ok)
                with self.assertRaises(ss.SplitError):
                    ss.outside_from_jpeg(enc.tobytes())
        for name, data in (('empty', b''), ('garbage', b'\xff\xd8 not a jpeg'),
                           ('header only', GOOD[:_sos_end(GOOD)]), ('None', None)):
            with self.subTest(name):
                with self.assertRaises(ss.SplitError):
                    ss.outside_from_jpeg(data)

    def test_from_jpeg_takes_a_one_row_uint8_sample(self):
        sample = np.frombuffer(GOOD, np.uint8).reshape(1, -1)
        self.assertEqual(_sha256(ss.outside_from_jpeg(sample)), _GOLDEN_FROM_JPEG)
        self.assertEqual(_sha256(ss.outside_from_jpeg(bytearray(GOOD))), _GOLDEN_FROM_JPEG)

    def test_a_chessboard_maps_by_two_u_plus_the_half_pixel(self):
        """G5 (P-T7): corners found in the outside frame map to the corners
        found in the full frame by u_full = 2u + 320.5, v_full = 2v + 60.5
        (median < 0.3 capture pixels; the probe 0.113); without the half-pixel
        terms the median is > 0.5 (the probe 0.710)."""
        mapped, unmapped = [], []
        for origin, square, rot in (((520.3, 260.7), 62.0, 3.0), ((700.9, 330.2), 54.5, -7.0),
                                    ((460.6, 180.4), 70.3, 11.0)):
            gray = _chessboard(origin, square, rot)
            out = cv2.cvtColor(ss.outside_from_bgr(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)),
                               cv2.COLOR_BGR2GRAY)
            full_corners, out_corners = _corners(gray), _corners(out)
            for err, (ox, oy) in ((mapped, (320.5, 60.5)), (unmapped, (320.0, 60.0))):
                m = np.stack([2 * out_corners[:, 0] + ox, 2 * out_corners[:, 1] + oy], axis=1)
                err.append(np.linalg.norm(m[:, None, :] - full_corners[None, :, :], axis=2).min(axis=1))
        self.assertLess(float(np.median(np.concatenate(mapped))), 0.3)
        self.assertGreater(float(np.median(np.concatenate(unmapped))), 0.5)


def _chessboard(origin, square, rot_deg, cols=10, rows=7, ss_factor=4):
    """A 10 x 7 chessboard (9 x 6 inner corners) in a 1080x1920 grey frame,
    slightly rotated so corners are sub-pixel, rendered with 4 x 4
    supersampling over the board's own bounding box only."""
    a = np.radians(rot_deg)
    c, s = np.cos(a), np.sin(a)
    pts = np.array([[origin[0] + (i * c - j * s) * square, origin[1] + (i * s + j * c) * square]
                    for i in (0, cols) for j in (0, rows)])
    x0, x1 = int(np.floor(pts[:, 0].min())) - 4, int(np.ceil(pts[:, 0].max())) + 4
    y0, y1 = int(np.floor(pts[:, 1].min())) - 4, int(np.ceil(pts[:, 1].max())) + 4
    xx = x0 + (np.arange((x1 - x0) * ss_factor, dtype=np.float32)[None, :] + 0.5) / ss_factor - 0.5
    yy = y0 + (np.arange((y1 - y0) * ss_factor, dtype=np.float32)[:, None] + 0.5) / ss_factor - 0.5
    u = ((xx - origin[0]) * c + (yy - origin[1]) * s) / square
    v = (-(xx - origin[0]) * s + (yy - origin[1]) * c) / square
    inside = (u >= 0) & (u < cols) & (v >= 0) & (v < rows)
    sub = np.full(u.shape, 215.0)
    sub[inside & (((np.floor(u) + np.floor(v)) % 2) == 0)] = 35.0
    img = np.full((1080, 1920), 215.0)
    img[y0:y1, x0:x1] = sub.reshape(y1 - y0, ss_factor, x1 - x0, ss_factor).mean(axis=(1, 3))
    return np.clip(np.rint(img), 0, 255).astype(np.uint8)


def _corners(gray):
    ok, cs = cv2.findChessboardCorners(gray, (9, 6))
    assert ok, 'chessboard not found'
    cs = cv2.cornerSubPix(gray, cs, (5, 5), (-1, -1),
                          (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 60, 1e-4))
    return cs.reshape(-1, 2)


# --------------------------------------------------------------------------
# selftest(): tolerance-based, on whatever OpenCV it runs
# --------------------------------------------------------------------------

def _moved_bgr(dx, dy):
    def outside_from_bgr(frame):
        return cv2.resize(frame[60 + dy:1020 + dy, 320 + dx:1600 + dx], (640, 480),
                          interpolation=cv2.INTER_AREA)
    return outside_from_bgr


def _moved_jpeg(dx, dy):
    def outside_from_jpeg(data):
        half = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_REDUCED_COLOR_2)
        return np.ascontiguousarray(half[30 + dy:510 + dy, 160 + dx:800 + dx])
    return outside_from_jpeg


@unittest.skipUnless(_HAVE_CV2, 'needs OpenCV and numpy')
class SelfTest(unittest.TestCase):

    def test_it_passes_on_this_build(self):
        self.assertEqual(ss.selftest(), '')

    def test_a_window_moved_by_one_pixel_is_caught(self):
        for dx, dy in SHIFTS:
            with self.subTest(shift=(dx, dy)):
                with mock.patch.object(ss, 'outside_from_bgr', _moved_bgr(dx, dy)):
                    self.assertNotEqual(ss.selftest(), '')

    def test_a_jpeg_crop_moved_by_one_pixel_is_caught(self):
        for dx, dy in SHIFTS:
            with self.subTest(shift=(dx, dy)):
                with mock.patch.object(ss, 'outside_from_jpeg', _moved_jpeg(dx, dy)):
                    self.assertNotEqual(ss.selftest(), '')

    def test_reversed_channels_are_caught(self):
        real_bgr, real_jpeg = ss.outside_from_bgr, ss.outside_from_jpeg
        with mock.patch.object(ss, 'outside_from_bgr', lambda f: real_bgr(f)[:, :, ::-1]):
            self.assertNotEqual(ss.selftest(), '')
        with mock.patch.object(ss, 'outside_from_jpeg', lambda d: real_jpeg(d)[:, :, ::-1]):
            self.assertNotEqual(ss.selftest(), '')

    def test_it_reports_instead_of_raising(self):
        def broken(_):
            raise RuntimeError('this OpenCV cannot')
        for name in ('outside_from_bgr', 'outside_from_jpeg'):
            with self.subTest(name):
                with mock.patch.object(ss, name, broken):
                    reason = ss.selftest()
                self.assertIsInstance(reason, str)
                self.assertNotEqual(reason, '')
        with mock.patch.object(ss, 'outside_from_bgr', lambda f: np.zeros((480, 640), np.uint8)):
            self.assertNotEqual(ss.selftest(), '')


# --------------------------------------------------------------------------
# CaptureVerifier: the pure state machine (spec 40 S1, the WP-04 table)
# --------------------------------------------------------------------------

WRONG_SIZE = _jpeg_header(1280, 720)


def _decoded(shape=(1080, 1920, 3)):
    return np.zeros(shape, np.uint8)


def _truncated():
    return GOOD[:len(GOOD) // 2]


class _Verifier:
    """Helpers to drive a verifier through time."""

    def make(self, platform='windows', quality=80):
        v = ss.CaptureVerifier(platform, quality)
        v.opening(0.0)
        v.opened(0.0)
        return v

    def feed(self, v, frames, t0, fps):
        """Feed ``frames`` at ``fps`` from ``t0``; returns (released, last t)."""
        released, t = [], t0
        for k, frame in enumerate(frames):
            t = t0 + k / fps
            if v.frame(t, frame):
                released.append(k)
        return released, t

    def streaming(self, frame=GOOD, fps=30.0):
        v = self.make()
        released, t = self.feed(v, [frame] * ss.VERIFY_FRAMES, 0.1, fps)
        self.assertEqual(v.state, 'streaming')
        return v, t


class VerifierTable(_Verifier, unittest.TestCase):

    def test_twenty_good_frames_reach_streaming_and_nothing_leaves_before(self):
        v = self.make()
        self.assertEqual(v.state, 'verifying')
        released, _ = self.feed(v, [GOOD] * 19, 0.1, 30.0)
        self.assertEqual(released, [])
        self.assertEqual(v.state, 'verifying')
        self.assertTrue(v.frame(0.1 + 19 / 30, GOOD))
        self.assertEqual((v.state, v.fault, v.passthrough), ('streaming', '', True))
        self.assertEqual((v.width, v.height), (1920, 1080))
        self.assertTrue(v.frame(0.1 + 20 / 30, GOOD))

    def test_a_wrong_size_while_verifying_is_wrong_mode_after_3_s(self):
        v = self.make()
        released, _ = self.feed(v, [WRONG_SIZE] * 89, 0.0, 30.0)  # up to t = 2.93 s
        self.assertEqual((released, v.state), ([], 'verifying'))
        self.assertFalse(v.frame(3.0, WRONG_SIZE))
        self.assertEqual((v.state, v.fault), ('fault', 'wrong_mode'))
        self.assertEqual((v.width, v.height), (1280, 720))
        self.assertEqual(v.retry_at, 5.0)

    def test_fifteen_bad_frames_in_streaming_are_bad_frames(self):
        v, t = self.streaming()
        released, t = self.feed(v, [WRONG_SIZE] * 14, t + 1 / 30, 30.0)
        self.assertEqual((released, v.state, v.dropped), ([], 'streaming', 14))
        self.assertFalse(v.frame(t + 1 / 30, WRONG_SIZE))
        self.assertEqual((v.state, v.fault, v.dropped), ('fault', 'bad_frames', 15))

    def test_a_good_frame_ends_a_bad_run(self):
        v, t = self.streaming()
        _, t = self.feed(v, [WRONG_SIZE] * 14 + [GOOD] + [WRONG_SIZE] * 14, t + 1 / 30, 30.0)
        self.assertEqual((v.state, v.dropped), ('streaming', 28))

    def test_two_seconds_of_silence_is_no_frames(self):
        v, t = self.streaming()
        v.tick(t + 1.99)
        self.assertEqual(v.state, 'streaming')
        v.tick(t + 2.0)
        self.assertEqual((v.state, v.fault), ('fault', 'no_frames'))

    def test_no_first_frame_within_the_start_up_allowance_is_no_frames(self):
        v = self.make()
        v.tick(ss.FIRST_FRAME_WAIT_S - 0.01)
        self.assertEqual(v.state, 'verifying')
        self.assertFalse(v.frame(ss.FIRST_FRAME_WAIT_S - 0.005, None))   # a failed read is no frame
        self.assertEqual(v.state, 'verifying')
        v.tick(ss.FIRST_FRAME_WAIT_S)
        self.assertEqual((v.state, v.fault, v.first_frame_at), ('fault', 'no_frames', None))
        self.assertEqual(ss.FIRST_FRAME_WAIT_S, 10.0)

    def test_silence_after_the_first_frame_while_verifying_is_no_frames(self):
        v = self.make()
        self.assertFalse(v.frame(4.0, GOOD))
        v.tick(5.99)
        self.assertEqual(v.state, 'verifying')
        v.tick(6.0)
        self.assertEqual((v.state, v.fault), ('fault', 'no_frames'))

    def test_the_start_up_counts_against_neither_the_window_nor_the_silence(self):
        """2.41.2: the first frame 2.5 s after the open (a UVC camera powering
        up) at 30 fps, and 9.5 s at 15 fps, both reach streaming; before, the
        2 s silence and the 3 s window from the open refused a camera whose
        every frame was right."""
        for first, fps in ((2.5, 30.0), (9.5, 15.0)):
            with self.subTest(first=first, fps=fps):
                v = self.make()
                released, _t = self.feed(v, [GOOD] * ss.VERIFY_FRAMES, first, fps)
                self.assertEqual((released, v.state), ([ss.VERIFY_FRAMES - 1], 'streaming'))
                self.assertEqual((v.opened_at, v.first_frame_at, v.passed),
                                 (0.0, first, ss.VERIFY_FRAMES))

    def test_the_window_counts_from_the_first_frame(self):
        v = self.make()
        released, _ = self.feed(v, [WRONG_SIZE] * 89, 5.0, 30.0)   # up to t = 7.93 s
        self.assertEqual((released, v.state), ([], 'verifying'))
        self.assertFalse(v.frame(5.0 + ss.VERIFY_WINDOW_S, WRONG_SIZE))
        self.assertEqual((v.state, v.fault, v.passed, v.dropped), ('fault', 'wrong_mode', 0, 89))

    def test_a_reopen_starts_the_allowance_and_the_counts_over(self):
        v, t = self.streaming()
        v.tick(t + ss.SILENCE_S)
        self.assertEqual(v.fault, 'no_frames')
        t0 = v.retry_at
        v.opening(t0)
        v.opened(t0)
        self.assertEqual((v.opened_at, v.first_frame_at, v.passed, v.dropped), (t0, None, 0, 0))
        v.tick(t0 + ss.SILENCE_S + 1.0)                  # no first frame yet: still starting
        self.assertEqual(v.state, 'verifying')

    def test_a_failed_read_is_no_frame(self):
        v, t = self.streaming()
        self.assertFalse(v.frame(t + 1.0, None))
        self.assertEqual((v.state, v.dropped), ('streaming', 0))
        self.assertFalse(v.frame(t + 2.0, None))
        self.assertEqual((v.state, v.fault), ('fault', 'no_frames'))

    def test_the_back_off(self):
        """2, 4, 8, 10, 10 s; the device stays closed until then."""
        v = ss.CaptureVerifier('windows', 80)
        t, delays = 0.0, []
        for _ in range(5):
            v.opening(t)
            v.open_failed(t)
            self.assertEqual((v.state, v.fault), ('fault', 'open_failed'))
            delays.append(v.retry_at - t)
            self.assertFalse(v.retry_due(v.retry_at - 0.01))
            self.assertTrue(v.retry_due(v.retry_at))
            t = v.retry_at
        self.assertEqual(delays, [2.0, 4.0, 8.0, 10.0, 10.0])

    def test_streaming_resets_the_back_off(self):
        v = ss.CaptureVerifier('windows', 80)
        for t in (0.0, 2.0):
            v.opening(t)
            v.open_failed(t)
        self.assertEqual(v.retry_at, 6.0)
        v.opening(6.0)
        v.opened(6.0)
        _, t = self.feed(v, [GOOD] * 20, 6.1, 30.0)
        self.assertEqual(v.state, 'streaming')
        v.tick(t + 2.0)
        self.assertEqual(v.retry_at, t + 2.0 + 2.0)

    def test_every_fault_but_selftest_closes_the_device_and_retries(self):
        v, t = self.streaming()
        v.tick(t + 2.0)
        self.assertEqual(v.state, 'fault')
        self.assertFalse(v.frame(t + 2.1, GOOD))  # a frame read after the fault is not counted
        self.assertEqual(v.dropped, 0)
        self.assertTrue(v.retry_due(v.retry_at))
        v.opening(v.retry_at)
        self.assertEqual(v.state, 'opening')

    def test_a_selftest_fault_is_final(self):
        v = ss.CaptureVerifier('pi', 95)
        v.selftest_failed(0.0)
        self.assertEqual((v.state, v.fault), ('fault', 'selftest'))
        self.assertIsNone(v.retry_at)
        self.assertFalse(v.retry_due(1e9))
        with self.assertRaises(RuntimeError):
            v.opening(1e9)
        v.close(1e9)
        self.assertEqual((v.state, v.fault), ('fault', 'selftest'))

    def test_illegal_transitions_raise(self):
        v = ss.CaptureVerifier('windows', 80)
        with self.assertRaises(RuntimeError):
            v.opened(0.0)
        with self.assertRaises(RuntimeError):
            v.open_failed(0.0)
        v.opening(0.0)
        with self.assertRaises(RuntimeError):
            v.opening(0.0)
        v.opened(0.0)
        with self.assertRaises(RuntimeError):
            v.opening(0.0)
        v.close(1.0)
        self.assertEqual((v.state, v.fault), ('closed', ''))
        v.opening(1.0)  # from closed again

    def test_frames_outside_verifying_and_streaming_are_ignored(self):
        v = ss.CaptureVerifier('windows', 80)
        self.assertFalse(v.frame(0.0, GOOD))
        v.opening(0.0)
        self.assertFalse(v.frame(0.0, GOOD))
        self.assertEqual((v.state, v.dropped), ('opening', 0))

    def test_only_two_platforms_and_a_jpeg_quality(self):
        for platform, quality in (('linux', 80), ('', 80), ('windows', 0), ('pi', '95')):
            with self.subTest(platform=platform, quality=quality):
                with self.assertRaises(ValueError):
                    ss.CaptureVerifier(platform, quality)


@unittest.skipUnless(_HAVE_NUMPY, 'needs numpy')
class VerifierDecodedRate(_Verifier, unittest.TestCase):
    """A decoded stream must ALSO run at MIN_MODE_FPS (D59: 10, revision 2's
    20.0 switched a dim room's camera off); a passthrough stream is proven by
    its JPEGs."""

    def test_decoded_5_fps_is_wrong_mode(self):
        v = self.make()
        released, _ = self.feed(v, [_decoded()] * 20, 0.1, 5.0)
        self.assertEqual((released, v.state, v.fault), ([], 'fault', 'wrong_mode'))

    def test_decoded_9_fps_is_wrong_mode_by_its_rate(self):
        """Twenty frames at 9 fps arrive within 3 s; the rate refuses them."""
        v = self.make()
        released, t = self.feed(v, [_decoded()] * 20, 0.1, 9.0)
        self.assertLess(t, 3.0)
        self.assertEqual((released, v.state, v.fault), ([], 'fault', 'wrong_mode'))

    def test_decoded_12_fps_streams_with_fps_low(self):
        v = self.make()
        released, _ = self.feed(v, [_decoded()] * 20, 0.1, 12.0)
        self.assertEqual(released, [19])
        self.assertEqual((v.state, v.fault, v.passthrough, v.fps_low), ('streaming', '', False, True))
        self.assertAlmostEqual(v.fps, 12.0, places=6)

    def test_decoded_24_fps_streams_with_fps_low(self):
        v = self.make()
        self.feed(v, [_decoded()] * 20, 0.1, 24.0)
        self.assertEqual((v.state, v.fps_low), ('streaming', True))
        self.assertAlmostEqual(v.fps, 24.0, places=6)

    def test_decoded_30_fps_streams(self):
        v = self.make()
        self.feed(v, [_decoded()] * 20, 0.1, 30.0)
        self.assertEqual((v.state, v.fps_low, v.passthrough), ('streaming', False, False))
        self.assertAlmostEqual(v.fps, 30.0, places=6)

    def test_passthrough_needs_no_rate(self):
        v = self.make()
        self.feed(v, [GOOD] * 20, 0.1, 12.0)
        self.assertEqual((v.state, v.fps_low, v.passthrough), ('streaming', True, True))

    def test_a_wrong_decoded_size_is_refused(self):
        v = self.make()
        self.feed(v, [_decoded((720, 1280, 3))] * 89, 0.0, 30.0)
        self.assertEqual(v.state, 'verifying')
        v.tick(3.0)
        self.assertEqual((v.state, v.fault, v.width, v.height), ('fault', 'wrong_mode', 1280, 720))

    def test_the_frame_kind_is_frozen(self):
        """A stream that started as passthrough and then hands decoded frames
        (or the reverse) is not the stream that was verified."""
        v, t = self.streaming()
        self.assertFalse(v.frame(t + 0.03, _decoded()))
        self.assertEqual(v.dropped, 1)
        v, t = self.streaming(frame=_decoded())
        self.assertFalse(v.frame(t + 0.03, GOOD))
        self.assertEqual(v.dropped, 1)

    def test_a_one_row_uint8_sample_is_passthrough(self):
        sample = np.frombuffer(GOOD, np.uint8).reshape(1, -1)
        v = self.make()
        self.feed(v, [sample] * 20, 0.1, 30.0)
        self.assertEqual((v.state, v.passthrough), ('streaming', True))


class VerifierEndOfImage(_Verifier, unittest.TestCase):
    """D59: in streaming every passthrough frame is checked again; a torn frame
    is dropped (neither split nor sent as the studio frame) and counted."""

    @staticmethod
    def capture_site(v, t, data, outside, studio):
        """What both capture owners do with each frame: only a released frame
        is split and forwarded."""
        if v.frame(t, data):
            outside.append(data)
            studio.append(data)

    def test_a_truncated_frame_is_dropped_and_counted(self):
        v, t = self.streaming()
        outside, studio = [], []
        self.capture_site(v, t + 1 / 30, _truncated(), outside, studio)
        self.assertEqual((outside, studio), ([], []))
        self.assertEqual((v.state, v.dropped), ('streaming', 1))
        self.capture_site(v, t + 2 / 30, GOOD, outside, studio)
        self.assertEqual((len(outside), len(studio), v.dropped), (1, 1, 1))
        self.assertEqual(json.loads(ss.state_message(**v.state_fields()))['dropped'], 1)

    def test_fifteen_truncated_frames_are_bad_frames(self):
        v, t = self.streaming()
        _, t = self.feed(v, [_truncated()] * 14, t + 1 / 30, 30.0)
        self.assertEqual((v.state, v.dropped), ('streaming', 14))
        self.assertFalse(v.frame(t + 1 / 30, _truncated()))
        self.assertEqual((v.state, v.fault, v.dropped), ('fault', 'bad_frames', 15))

    def test_an_empty_scan_frame_is_dropped(self):
        v, t = self.streaming()
        self.assertFalse(v.frame(t + 1 / 30, GOOD[:_sos_end(GOOD)] + EOI))
        self.assertEqual(v.dropped, 1)

    def test_a_padded_frame_streams(self):
        v, t = self.streaming(frame=GOOD + bytes(range(0x10, 0x21)))
        self.assertTrue(v.frame(t + 1 / 30, GOOD + bytes(517)))
        self.assertEqual(v.dropped, 0)

    def test_torn_frames_never_verify_a_stream(self):
        v = self.make()
        released, _ = self.feed(v, [_truncated()] * 89, 0.0, 30.0)
        v.tick(3.0)
        self.assertEqual((released, v.state, v.fault, v.dropped), ([], 'fault', 'wrong_mode', 89))

    def test_dropped_counts_since_the_device_was_last_opened(self):
        v, t = self.streaming()
        self.feed(v, [_truncated()] * 3, t + 1 / 30, 30.0)
        self.assertEqual(v.dropped, 3)
        v.close(t + 1.0)
        v.opening(t + 1.0)
        self.assertEqual(v.dropped, 3)
        v.opened(t + 1.0)
        self.assertEqual(v.dropped, 0)

    def test_a_frame_the_split_refuses_counts_like_a_bad_frame(self):
        v, t = self.streaming()
        for k in range(14):
            v.split_failed(t + k / 30)
        self.assertEqual((v.state, v.dropped), ('streaming', 14))
        v.split_failed(t + 0.5)
        self.assertEqual((v.state, v.fault, v.dropped), ('fault', 'bad_frames', 15))

    def test_a_released_frame_the_split_refuses_continues_the_run(self):
        """What both owners do: ``frame()`` releases the frame, the split
        refuses it, ``split_failed()``. The release must not end the run of
        refused frames, or a camera whose frames all fail the split would read
        ``streaming`` for ever while nothing leaves."""
        v, t = self.streaming()
        for _ in range(ss.BAD_FRAMES_LIMIT - 1):
            t += 1 / 30
            self.assertTrue(v.frame(t, GOOD))
            v.split_failed(t)
        self.assertEqual((v.state, v.dropped), ('streaming', ss.BAD_FRAMES_LIMIT - 1))
        t += 1 / 30
        self.assertTrue(v.frame(t, GOOD))
        v.split_failed(t)
        self.assertEqual((v.state, v.fault, v.dropped),
                         ('fault', 'bad_frames', ss.BAD_FRAMES_LIMIT))

    def test_frames_the_check_and_the_split_refuse_make_one_run(self):
        v, t = self.streaming()
        _, t = self.feed(v, [_truncated()] * 7, t + 1 / 30, 30.0)
        for _ in range(ss.BAD_FRAMES_LIMIT - 7):
            t += 1 / 30
            self.assertTrue(v.frame(t, GOOD))
            v.split_failed(t)
        self.assertEqual((v.state, v.fault, v.dropped),
                         ('fault', 'bad_frames', ss.BAD_FRAMES_LIMIT))

    def test_a_frame_that_leaves_ends_the_run(self):
        v, t = self.streaming()
        _, t = self.feed(v, [_truncated()] * (ss.BAD_FRAMES_LIMIT - 1), t + 1 / 30, 30.0)
        t += 1 / 30
        self.assertTrue(v.frame(t, GOOD))          # split and sent: the run is over
        t += 1 / 30
        self.assertTrue(v.frame(t, GOOD))
        v.split_failed(t)
        self.assertEqual((v.state, v.dropped), ('streaming', ss.BAD_FRAMES_LIMIT))


# --------------------------------------------------------------------------
# The state message (camera id 17)
# --------------------------------------------------------------------------

class StateMessage(_Verifier, unittest.TestCase):

    KEYS = ['v', 'profile', 'state', 'width', 'height', 'fps', 'fps_low', 'passthrough',
            'outside', 'profile_id', 'platform', 'fault', 'dropped']

    def test_the_keys_in_their_order(self):
        """``dropped`` is the LAST key, after ``fault`` (D59, additive), so
        ``v`` stays 1."""
        v, _ = self.streaming()
        msg = ss.state_message(**v.state_fields())
        parsed = json.loads(msg.decode('utf-8'))
        self.assertEqual(list(parsed), self.KEYS)
        self.assertEqual(list(parsed)[-2:], ['fault', 'dropped'])
        self.assertEqual(parsed['v'], 1)
        self.assertEqual(parsed, {
            'v': 1, 'profile': 'tower_v1', 'state': 'streaming', 'width': 1920, 'height': 1080,
            'fps': 30.0, 'fps_low': False, 'passthrough': True, 'outside': '640x480',
            'profile_id': '3efca10f3a05', 'platform': 'windows', 'fault': '', 'dropped': 0})
        self.assertNotIn(b' ', msg)
        self.assertLessEqual(len(msg), ss.STATE_MAX_BYTES)
        self.assertEqual(ss.STATE_MAX_BYTES, 4096)

    def test_the_pi_states_its_own_id(self):
        v = ss.CaptureVerifier('pi', 95)
        parsed = json.loads(ss.state_message(**v.state_fields()))
        self.assertEqual((parsed['state'], parsed['profile_id'], parsed['platform']),
                         ('closed', '8b7f72cb250c', 'pi'))
        self.assertEqual((parsed['width'], parsed['fps'], parsed['passthrough']), (None, None, None))

    def test_a_fault_keeps_what_was_seen(self):
        v = self.make()
        self.feed(v, [WRONG_SIZE] * 89, 0.0, 30.0)
        v.tick(3.0)
        parsed = json.loads(ss.state_message(**v.state_fields()))
        self.assertEqual((parsed['state'], parsed['fault'], parsed['width'], parsed['height'],
                          parsed['fps'], parsed['dropped']),
                         ('fault', 'wrong_mode', 1280, 720, None, 89))

    def test_it_refuses_a_wrong_field(self):
        v, _ = self.streaming()
        good = v.state_fields()
        with self.assertRaises(TypeError):
            ss.state_message(**{k: val for k, val in good.items() if k != 'dropped'})
        with self.assertRaises(TypeError):
            ss.state_message(**good, extra=1)
        with self.assertRaises(TypeError):
            ss.state_message(**good, v=2)
        for key, bad in (('state', 'running'), ('fault', 'oops'), ('profile', 'legacy2'),
                         ('platform', 'mac'), ('profile_id', 'xyz'), ('dropped', -1),
                         ('dropped', 1.5), ('width', '1920'), ('fps', float('nan')),
                         ('fps', float('inf')), ('fps_low', 1), ('passthrough', 'yes'),
                         ('outside', '1280x960')):
            with self.subTest(key=key, value=bad):
                with self.assertRaises(ValueError):
                    ss.state_message(**dict(good, **{key: bad}))

    def test_the_generation_moves_on_every_state_change(self):
        v = ss.CaptureVerifier('windows', 80)
        seen = [v.generation]
        v.opening(0.0)
        seen.append(v.generation)
        v.opened(0.0)
        seen.append(v.generation)
        self.feed(v, [GOOD] * 19, 0.1, 30.0)
        self.assertEqual(v.generation, seen[-1])
        self.feed(v, [GOOD], 1.0, 30.0)
        seen.append(v.generation)
        self.assertEqual(len(set(seen)), 4)


# --------------------------------------------------------------------------
# RateLimiter: the studio stream at most STUDIO_MAX_HZ
# --------------------------------------------------------------------------

class RateLimiterTest(unittest.TestCase):

    def test_steady_30_fps_becomes_15_hz(self):
        r = ss.RateLimiter(15.0)
        allowed = [k for k in range(300) if r.allow(k / 30)]
        self.assertEqual(len(allowed), 150)
        self.assertEqual(allowed[:4], [0, 2, 4, 6])

    def test_jitter_neither_halves_nor_exceeds_the_rate(self):
        """A frame that arrives a little early for its slot is not lost to the
        next-but-one slot: 30 fps with +/- 3 ms of jitter still gives 15 Hz,
        not 10."""
        r = ss.RateLimiter(15.0)
        pattern = (-0.003, 0.002, -0.001, 0.003, -0.002, 0.0)
        times = [k / 30 + pattern[k % len(pattern)] for k in range(1, 301)]
        allowed = [t for t in times if r.allow(t)]
        self.assertTrue(149 <= len(allowed) <= 151, len(allowed))
        for start in (1.0, 3.5, 7.2):
            in_one_second = [t for t in allowed if start <= t < start + 1.0]
            self.assertLessEqual(len(in_one_second), 16)

    def test_a_slower_source_passes_whole(self):
        r = ss.RateLimiter(15.0)
        self.assertTrue(all(r.allow(k / 10) for k in range(50)))

    def test_no_burst_after_a_gap(self):
        r = ss.RateLimiter(15.0)
        self.assertTrue(r.allow(0.0))
        self.assertTrue(r.allow(5.0))
        self.assertFalse(r.allow(5.0 + 1 / 30))
        self.assertTrue(r.allow(5.0 + 2 / 30))

    def test_it_reads_the_monotonic_clock_by_default(self):
        r = ss.RateLimiter(15.0)
        with mock.patch.object(ss.time, 'monotonic', return_value=100.0):
            self.assertTrue(r.allow())
            self.assertFalse(r.allow())

    def test_only_a_positive_finite_rate(self):
        for bad in (0, -1.0, float('nan'), float('inf'), True, '15'):
            with self.subTest(rate=bad):
                with self.assertRaises(ValueError):
                    ss.RateLimiter(bad)


if __name__ == '__main__':
    unittest.main()
