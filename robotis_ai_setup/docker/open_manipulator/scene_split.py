# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower scene camera's one derivation, shared by both capture owners.

TWIN MODULE. This file exists twice, byte for byte: ``gui/app/scene_split.py``
(the Windows GUI's copy) and ``docker/open_manipulator/scene_split.py`` (the
arm container's copy). A recording must not depend on which platform cut its
picture, so ``tests/test_scene_split_twin_lockstep.py`` fails when the two
differ. Edit one, copy it over the other. Imports: the standard library,
numpy and cv2 only, and numpy/cv2 only inside the functions that touch
pixels, so the profile reader, the JPEG header rules and the verifier also
work where OpenCV cannot load.

THE PROFILE. ``EDUBOTICS_SCENE_CAMERA_PROFILE`` is ``tower_v1`` or, for any
other value and for its absence, ``legacy`` (today's camera path, untouched).
With ``tower_v1`` one camera on the tower captures MJPEG 1920x1080 at 30 fps.
The recorded scene picture (the "outside" frame, camera id 1, wire unchanged)
is the 1280x960 window at (320, 60) reduced exactly 2:1 to 640x480;
Roboter Studio gets the camera's own frame on camera id 16 at most 15 Hz; a
JSON state message rides camera id 17. ``profile_id`` names this derivation
(window, sizes, JPEG quality of the outside frame) for the dataset stamp.

TWO DERIVATIONS, ONE PICTURE. ``outside_from_jpeg`` decodes the camera's own
JPEG at half size and crops (no full decode); ``outside_from_bgr`` takes a
decoded frame's window and reduces it with ``INTER_AREA``, which at exactly
2:1 is the rounded 2x2 block mean. They differ by less than one level. A
pixel (u, v) of the outside frame is the capture pixel (2u + 320.5,
2v + 60.5). Their output is pinned bit for bit on CI's OpenCV (the goldens
in tests/test_scene_split.py); ``selftest()`` re-proves the derivation with
tolerances on whatever OpenCV build runs it, so a rounding difference of one
level never switches the camera off while a wrong window does.

THE CAPTURE VERIFIER. ``CaptureVerifier`` is a pure state machine with the
time injected: closed -> opening -> verifying -> streaming -> fault. Only a
frame it releases (``frame()`` returns True, i.e. only in ``streaming``) may be
split into the outside frame or sent as the studio frame. Every frame is
checked; a passthrough frame must be a COMPLETE JPEG (decision D59: a lost
USB packet cuts a JPEG short, and OpenCV 4.6/4.10 still decode such a frame
to full size with black, garbage or the previous frame's rows). A refused
frame is dropped and counted (``dropped``, the state message's last key).
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import re
import time

# ---------------------------------------------------------------------------
# The profile key
# ---------------------------------------------------------------------------

PROFILE_LEGACY, PROFILE_TOWER = 'legacy', 'tower_v1'
# The one value table every reader of EDUBOTICS_SCENE_CAMERA_PROFILE uses.
PROFILE_VALUES = {'tower_v1': 'tower_v1', 'legacy': 'legacy'}
# What a raw value loses at both ends before the lookup: blanks and the quotes
# config_generator.upsert_env_var writes around every value.
_PROFILE_STRIP = ' \t\r\n\v\f"\''

# ---------------------------------------------------------------------------
# The geometry and the wire
# ---------------------------------------------------------------------------

CAPTURE_W, CAPTURE_H, CAPTURE_FPS = 1920, 1080, 30
WINDOW_X0, WINDOW_Y0, WINDOW_W, WINDOW_H = 320, 60, 1280, 960
OUT_W, OUT_H = 640, 480
OUTSIDE = f'{OUT_W}x{OUT_H}'
SCENE_CAM_ID, STUDIO_CAM_ID, STATE_CAM_ID, HELLO_CAM_ID = 1, 16, 17, 255
STUDIO_MAX_HZ = 15.0
HELLO = b'\x01'                  # payload of the hello frame AND the ingest's one-byte answer to it
# Must contain neither '/scene/image_raw' nor '/gripper/image_raw': the composes'
# healthchecks grep for those, and a studio stream must never satisfy them.
STUDIO_TOPIC = '/studio/tower/compressed'
STATE_TOPIC = '/edubotics/scene_camera_state'
STATE_SCHEMA = 1
STATE_MAX_BYTES = 4096           # the ingest node drops a larger state message
PLATFORMS = ('windows', 'pi')

# D59: below this a DECODED stream is refused as wrong_mode. YUY2 1080p, what the
# rate test exists to refuse, runs at 5 fps; a camera with automatic exposure
# halves its MJPEG rate in a dim room (30 -> 15), which revision 2's 20.0 refused.
MIN_MODE_FPS = 10.0
LOW_FPS = 27.0                   # below this the state says fps_low; the stream keeps running (C9)

PROFILE_STRING = (f'{PROFILE_TOWER}|{CAPTURE_W}x{CAPTURE_H}@{CAPTURE_FPS}|MJPG'
                  f'|window={WINDOW_X0},{WINDOW_Y0},{WINDOW_W},{WINDOW_H}'
                  f'|out={OUT_W}x{OUT_H}|reduce=half_decode_crop|q={{q}}')

# ---------------------------------------------------------------------------
# The verifier's states, faults and timings (spec 40 S1)
# ---------------------------------------------------------------------------

STATE_CLOSED, STATE_OPENING, STATE_VERIFYING, STATE_STREAMING, STATE_FAULT = (
    'closed', 'opening', 'verifying', 'streaming', 'fault')
STATES = (STATE_CLOSED, STATE_OPENING, STATE_VERIFYING, STATE_STREAMING, STATE_FAULT)
FAULT_NONE, FAULT_SELFTEST, FAULT_OPEN_FAILED, FAULT_WRONG_MODE, FAULT_NO_FRAMES, FAULT_BAD_FRAMES = (
    '', 'selftest', 'open_failed', 'wrong_mode', 'no_frames', 'bad_frames')
FAULTS = (FAULT_NONE, FAULT_SELFTEST, FAULT_OPEN_FAILED, FAULT_WRONG_MODE, FAULT_NO_FRAMES,
          FAULT_BAD_FRAMES)
VERIFY_FRAMES = 20               # passing frames that prove the mode ...
VERIFY_WINDOW_S = 3.0            # ... within this long after the open, else wrong_mode
BAD_FRAMES_LIMIT = 15            # consecutive refused frames while streaming -> bad_frames
SILENCE_S = 2.0                  # this long without any frame -> no_frames
RETRY_FIRST_S, RETRY_MAX_S = 2.0, 10.0   # after a fault: 2, 4, 8, 10, 10 ... s
FPS_WINDOW_FRAMES = 31           # the measured rate spans the last 30 frame intervals

STATE_KEYS = ('v', 'profile', 'state', 'width', 'height', 'fps', 'fps_low', 'passthrough',
              'outside', 'profile_id', 'platform', 'fault', 'dropped')

# selftest() limits (P-T6; the probe's values on OpenCV 4.13 / 4.10 in brackets).
SELFTEST_JPEG_QUALITY = 90
SELFTEST_BGR_MAX_DIFF = 1.0      # outside_from_bgr vs the 2x2 block mean, every pixel [0.50]
SELFTEST_BGR_MOVED_MIN = 2.0     # ... vs the window moved by one capture pixel, mean [3.98]
SELFTEST_JPEG_MEAN_DIFF = 2.5    # outside_from_jpeg vs the block mean, mean [1.18]
SELFTEST_JPEG_MOVED_MIN = 5.0    # ... vs the crop moved by one half-size pixel, mean [8.00]

_SHIFTS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_PROFILE_ID_RE = re.compile(r'[0-9a-f]{12}')

# JPEG markers: RST0-7 and TEM stand alone, every other marker carries a length.
_STANDALONE = frozenset(range(0xD0, 0xD8)) | {0x01}
_SOF_WITH_SIZE = frozenset((0xC0, 0xC1, 0xC2))   # baseline, extended, progressive
_SOS = 0xDA
_NONE = frozenset()
_EOI = b'\xff\xd9'


class SplitError(ValueError):
    """A frame that is not the tower camera's 1920x1080 picture."""


def normalise_profile(value) -> str:
    """The profile a raw ``EDUBOTICS_SCENE_CAMERA_PROFILE`` value selects:
    blanks and quotes stripped at both ends, lower-cased, looked up in
    ``PROFILE_VALUES``; anything else, ``None`` and a non-string included, is
    ``legacy``."""
    if not isinstance(value, str):
        return PROFILE_LEGACY
    return PROFILE_VALUES.get(value.strip(_PROFILE_STRIP).lower(), PROFILE_LEGACY)


def profile_id(jpeg_quality: int) -> str:
    """The first 12 hex digits of the sha256 of ``PROFILE_STRING`` with the
    outside frame's JPEG quality: '3efca10f3a05' at 80 (Windows),
    '8b7f72cb250c' at 95 (Orange Pi)."""
    if type(jpeg_quality) is not int or not 1 <= jpeg_quality <= 100:
        raise ValueError(f'a JPEG quality is an int in 1..100, not {jpeg_quality!r}')
    return hashlib.sha256(PROFILE_STRING.format(q=jpeg_quality).encode('utf-8')).hexdigest()[:12]


# ---------------------------------------------------------------------------
# The JPEG header rules (no decode)
# ---------------------------------------------------------------------------

def _frame_kind(frame):
    """('jpeg', bytes) for a passthrough sample, ('bgr', array) for a decoded
    frame, (None, None) for anything else. A passthrough sample is bytes,
    bytearray, memoryview or a 1-D / 1xN uint8 array (MSMF's raw sample); a
    decoded frame is an array with three dimensions."""
    if isinstance(frame, (bytes, bytearray)):
        return 'jpeg', frame
    if isinstance(frame, memoryview):
        return 'jpeg', frame.tobytes()
    try:
        import numpy
    except ImportError:
        return None, None
    if isinstance(frame, numpy.ndarray):
        if frame.ndim == 3:
            return 'bgr', frame
        if frame.dtype == numpy.uint8 and (frame.ndim == 1 or (frame.ndim == 2 and frame.shape[0] == 1)):
            return 'jpeg', frame.tobytes()
    return None, None


def _header_segment(data, wanted):
    """Walk a JPEG's marker segments from its SOI to the first marker in
    ``wanted`` or the first SOS, whichever comes first, and return
    ``(marker, i, length)`` with ``i`` the index of that segment's length
    field; ``None`` for a broken header. A run of FF fill bytes before a marker
    is skipped; RST0-7 and TEM have no length; any other marker is followed by
    a big-endian length L >= 2 that lies inside the data. An SOI or EOI before
    the first scan, a byte other than FF where a marker must start, a walk that
    leaves the data or a bad length make the header broken."""
    n = len(data)
    if n < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    i = 2
    while i < n:
        if data[i] != 0xFF:
            return None
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker in _STANDALONE:
            continue
        if marker == 0xD8 or marker == 0xD9:
            return None
        if i + 2 > n:
            return None
        length = (data[i] << 8) | data[i + 1]
        if length < 2 or i + length > n:
            return None
        if marker == _SOS or marker in wanted:
            return marker, i, length
        i += length
    return None


def _size(raw):
    found = _header_segment(raw, _SOF_WITH_SIZE)
    if found is None:
        return None
    marker, i, length = found
    if marker == _SOS or length < 8:
        return None
    return (raw[i + 5] << 8) | raw[i + 6], (raw[i + 3] << 8) | raw[i + 4]


def _complete(raw):
    found = _header_segment(raw, _NONE)
    if found is None:
        return False
    _, i, length = found
    sos_end = i + length
    return raw.rfind(_EOI, sos_end) > sos_end


def jpeg_size(data) -> tuple[int, int] | None:
    """``(width, height)`` from the first SOF0/SOF1/SOF2 segment, read without a
    decode; ``None`` when the header is broken or names another frame kind."""
    kind, raw = _frame_kind(data)
    if kind != 'jpeg':
        return None
    return _size(raw)


def jpeg_complete(data) -> bool:
    """True when the JPEG is whole (decision D59; spec 40 S1, revision 5): it
    starts with SOI, its header walks cleanly to the FIRST SOS, and an EOI
    (FF D9) lies strictly after that scan header. Entropy-coded data never holds FF D9 and an
    embedded thumbnail's own EOI sits inside an APP segment the walk skips, so
    a frame cut anywhere after its header has no EOI past the scan header;
    bytes after the EOI (zero padding, FF fill, anything a camera appends) keep
    a frame that decodes. An EOI right after the scan header is an empty scan,
    which is incomplete."""
    kind, raw = _frame_kind(data)
    if kind != 'jpeg':
        return False
    return _complete(raw)


# ---------------------------------------------------------------------------
# The two derivations
# ---------------------------------------------------------------------------

def _numpy_cv2():
    import numpy
    import cv2
    return numpy, cv2


def outside_from_jpeg(data):
    """The outside frame (480x640x3 uint8 BGR) from the camera's own JPEG: a
    half-size decode, then the window's crop. Raises ``SplitError`` unless the
    data is a complete 1920x1080 JPEG whose half-size decode is 960x540x3 (the
    header size is read too: 1919x1080 and 1920x1079 also half-decode to
    960x540)."""
    kind, raw = _frame_kind(data)
    if kind != 'jpeg':
        raise SplitError('not a JPEG sample')
    size = _size(raw)
    if size != (CAPTURE_W, CAPTURE_H):
        raise SplitError(f'the JPEG is {size}, not {CAPTURE_W}x{CAPTURE_H}')
    if not _complete(raw):
        raise SplitError('the JPEG is incomplete')
    np, cv2 = _numpy_cv2()
    try:
        half = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_REDUCED_COLOR_2)
    except cv2.error as exc:
        raise SplitError(f'the half-size decode failed: {exc}') from None
    if half is None or half.shape != (CAPTURE_H // 2, CAPTURE_W // 2, 3):
        raise SplitError(f'the half-size decode is {None if half is None else half.shape}')
    y0, x0 = WINDOW_Y0 // 2, WINDOW_X0 // 2
    return np.ascontiguousarray(half[y0:y0 + OUT_H, x0:x0 + OUT_W])


def outside_from_bgr(frame):
    """The outside frame (480x640x3 uint8 BGR) from a decoded 1080x1920x3 uint8
    frame: the window reduced 2:1 with INTER_AREA (the rounded 2x2 block mean).
    Raises ``SplitError`` for any other frame."""
    np, cv2 = _numpy_cv2()
    if (not isinstance(frame, np.ndarray) or frame.shape != (CAPTURE_H, CAPTURE_W, 3)
            or frame.dtype != np.uint8):
        shape = getattr(frame, 'shape', type(frame).__name__)
        raise SplitError(f'the frame is {shape}, not a {CAPTURE_H}x{CAPTURE_W}x3 uint8 image')
    window = frame[WINDOW_Y0:WINDOW_Y0 + WINDOW_H, WINDOW_X0:WINDOW_X0 + WINDOW_W]
    return cv2.resize(window, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)


# ---------------------------------------------------------------------------
# The self-test: the derivation on THIS OpenCV build, with tolerances
# ---------------------------------------------------------------------------

def selftest() -> str:
    """'' when both derivations behave on the running OpenCV build, else the
    reason (English, for the log). Never raises. Both capture owners run it
    once before they open the camera; a reason is the fault ``selftest``."""
    try:
        return _selftest()
    except Exception as exc:  # a broken build is a reason, never a crash of the capture owner
        return f'the self-test raised {type(exc).__name__}: {exc}'


def _selftest_frame(np):
    """A fixed 1080x1920x3 frame from a closed formula: channel c at pixel
    (x, y) is round(128 + 52 sin(2 pi x / 61 + 0.7 c) + 44 sin(2 pi y / 47 + 1.3 c)
    + 20 sin(2 pi (x + y) / 29)), every value inside 12..244."""
    x = np.arange(CAPTURE_W, dtype=np.float64)[None, :]
    y = np.arange(CAPTURE_H, dtype=np.float64)[:, None]
    diagonal = 20.0 * np.sin(2.0 * np.pi * (x + y) / 29.0)
    frame = np.empty((CAPTURE_H, CAPTURE_W, 3), np.uint8)
    for c in range(3):
        value = (128.0 + 52.0 * np.sin(2.0 * np.pi * x / 61.0 + 0.7 * c)
                 + 44.0 * np.sin(2.0 * np.pi * y / 47.0 + 1.3 * c) + diagonal)
        frame[:, :, c] = np.clip(np.rint(value), 0, 255)
    return frame


def _block_mean(np, frame, dx, dy):
    """numpy alone: the 2x2 block mean of the window moved by (dx, dy)."""
    w = frame[WINDOW_Y0 + dy:WINDOW_Y0 + dy + WINDOW_H,
              WINDOW_X0 + dx:WINDOW_X0 + dx + WINDOW_W].astype(np.float64)
    return (w[0::2, 0::2] + w[0::2, 1::2] + w[1::2, 0::2] + w[1::2, 1::2]) / 4.0


def _not_outside(np, out, name):
    if (not isinstance(out, np.ndarray) or out.shape != (OUT_H, OUT_W, 3)
            or out.dtype != np.uint8):
        return f'{name} returned {getattr(out, "shape", type(out).__name__)}, not {OUT_H}x{OUT_W}x3 uint8'
    return ''


def _selftest():
    np, cv2 = _numpy_cv2()
    frame = _selftest_frame(np)
    reference = _block_mean(np, frame, 0, 0)

    out = outside_from_bgr(frame)
    reason = _not_outside(np, out, 'outside_from_bgr')
    if reason:
        return reason
    out = out.astype(np.float64)
    worst = float(np.abs(out - reference).max())
    if worst > SELFTEST_BGR_MAX_DIFF:
        return (f'outside_from_bgr differs from the 2x2 block mean by up to {worst:.2f} levels '
                f'(limit {SELFTEST_BGR_MAX_DIFF})')
    for dx, dy in _SHIFTS:
        moved = float(np.abs(out - _block_mean(np, frame, dx, dy)).mean())
        if moved < SELFTEST_BGR_MOVED_MIN:
            return (f'outside_from_bgr is within {moved:.2f} levels of the window moved by '
                    f'({dx}, {dy}) (at least {SELFTEST_BGR_MOVED_MIN} expected)')

    ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, SELFTEST_JPEG_QUALITY])
    if not ok:
        return 'cv2.imencode refused the self-test frame'
    out = outside_from_jpeg(encoded.tobytes())
    reason = _not_outside(np, out, 'outside_from_jpeg')
    if reason:
        return reason
    out = out.astype(np.float64)
    mean = float(np.abs(out - reference).mean())
    if mean > SELFTEST_JPEG_MEAN_DIFF:
        return (f'outside_from_jpeg differs from the 2x2 block mean by {mean:.2f} levels on '
                f'average (limit {SELFTEST_JPEG_MEAN_DIFF})')
    for dx, dy in _SHIFTS:
        moved = float(np.abs(out - _block_mean(np, frame, 2 * dx, 2 * dy)).mean())
        if moved < SELFTEST_JPEG_MOVED_MIN:
            return (f'outside_from_jpeg is within {moved:.2f} levels of the crop moved by '
                    f'({dx}, {dy}) (at least {SELFTEST_JPEG_MOVED_MIN} expected)')
    return ''


# ---------------------------------------------------------------------------
# The capture verifier
# ---------------------------------------------------------------------------

def _inspect(frame):
    """(kind, (width, height) or None, passes the frame check)."""
    kind, value = _frame_kind(frame)
    if kind == 'jpeg':
        size = _size(value)
        return kind, size, size == (CAPTURE_W, CAPTURE_H) and _complete(value)
    if kind == 'bgr':
        shape = value.shape
        size = (int(shape[1]), int(shape[0]))
        return kind, size, shape == (CAPTURE_H, CAPTURE_W, 3) and value.dtype.name == 'uint8'
    return None, None, False


class CaptureVerifier:
    """The capture owner's state machine (spec 40 S1); every method takes ``now``,
    a monotonic time in seconds.

    The owner calls ``opening(now)`` before it opens the device, then
    ``opened(now)`` or ``open_failed(now)``; ``frame(now, frame)`` for each
    frame read (``None`` for a read that returned nothing) and ``tick(now)``
    whenever it wakes without a frame. ``frame`` returns True only for a frame
    that may leave the capture site: one that passed the frame check while the
    verifier is ``streaming``. Only such a frame is split into the outside
    frame and sent as the studio frame. A frame the split still refuses is
    reported with ``split_failed(now)``. In ``fault`` the owner closes the
    device and opens it again once ``retry_due(now)``; ``selftest_failed`` is
    final. ``close(now)`` is the owner stopping.

    verifying: within ``VERIFY_WINDOW_S`` of the open, ``VERIFY_FRAMES`` frames
    must pass the frame check (passthrough: a complete 1920x1080 JPEG; decoded:
    a 1080x1920x3 uint8 frame) and, for a decoded stream, arrive at
    ``MIN_MODE_FPS`` or faster; else ``wrong_mode``. The first passing frame
    freezes the kind (passthrough or decoded); a frame of the other kind is
    refused. streaming: every frame is checked again; a refused frame is
    dropped and counted; ``BAD_FRAMES_LIMIT`` refused frames in a row are
    ``bad_frames``. ``SILENCE_S`` without any frame is ``no_frames`` in both.
    After a fault the device is retried after 2, 4, 8, then every 10 s;
    reaching ``streaming`` starts that sequence over.
    """

    def __init__(self, platform: str, jpeg_quality: int):
        if platform not in PLATFORMS:
            raise ValueError(f'the platform is one of {PLATFORMS}, not {platform!r}')
        self.platform = platform
        self.profile_id = profile_id(jpeg_quality)
        self.state = STATE_CLOSED
        self.fault = FAULT_NONE
        self.generation = 0          # moves on every change of state or fault
        self.retry_at = None
        self.dropped = 0             # frames the check refused since the device was last opened
        self.width = None            # of the last frame seen, None while unknown
        self.height = None
        self.passthrough = None      # True: the camera's own JPEG; False: a decoded frame
        self._faults_in_a_row = 0
        self._start_session(None)

    # -- the owner's calls ------------------------------------------------

    def opening(self, now):
        if not (self.state == STATE_CLOSED
                or (self.state == STATE_FAULT and self.fault != FAULT_SELFTEST)):
            raise RuntimeError(f'cannot open from {self.state}/{self.fault or "-"}')
        self.retry_at = None
        self._set(STATE_OPENING, FAULT_NONE)

    def opened(self, now):
        self._require(STATE_OPENING)
        self.dropped = 0
        self.width = self.height = self.passthrough = None
        self._start_session(now)
        self._set(STATE_VERIFYING, FAULT_NONE)

    def open_failed(self, now):
        self._require(STATE_OPENING)
        self._fail(now, FAULT_OPEN_FAILED)

    def selftest_failed(self, now):
        self.retry_at = None
        self._set(STATE_FAULT, FAULT_SELFTEST)

    def close(self, now):
        if self.fault == FAULT_SELFTEST:
            return
        self.retry_at = None
        self._faults_in_a_row = 0
        self._set(STATE_CLOSED, FAULT_NONE)

    def retry_due(self, now) -> bool:
        return (self.state == STATE_FAULT and self.fault != FAULT_SELFTEST
                and self.retry_at is not None and now >= self.retry_at)

    def tick(self, now):
        if self.state not in (STATE_VERIFYING, STATE_STREAMING):
            return
        if now - self._last_frame_at >= SILENCE_S:
            self._fail(now, FAULT_NO_FRAMES)
        elif self.state == STATE_VERIFYING and now >= self._deadline:
            self._fail(now, FAULT_WRONG_MODE)

    def frame(self, now, frame) -> bool:
        self.tick(now)
        if frame is None or self.state not in (STATE_VERIFYING, STATE_STREAMING):
            return False
        self._last_frame_at = now
        kind, size, ok = _inspect(frame)
        self.width, self.height = size if size is not None else (None, None)
        if ok and self._kind is not None and kind != self._kind:
            ok = False
        if not ok:
            self._refused(now)
            return False
        self._bad_in_a_row = 0
        if self._kind is None:
            self._kind = kind
            self.passthrough = kind == 'jpeg'
        self._times.append(now)
        if self.state == STATE_VERIFYING:
            self._good += 1
            if self._good < VERIFY_FRAMES:
                return False
            if not self.passthrough:
                rate = self._rate()
                if rate is None or rate < MIN_MODE_FPS:
                    self._fail(now, FAULT_WRONG_MODE)
                    return False
            self._faults_in_a_row = 0
            self._set(STATE_STREAMING, FAULT_NONE)
        return True

    def split_failed(self, now):
        """A released frame the split still refused: dropped and counted like a
        frame the check refused."""
        if self.state == STATE_STREAMING:
            self._refused(now)

    # -- what the state message reports -----------------------------------

    @property
    def fps(self):
        """The measured rate of passing frames over the last 30 intervals while
        verifying or streaming, else None."""
        if self.state not in (STATE_VERIFYING, STATE_STREAMING):
            return None
        return self._rate()

    @property
    def fps_low(self) -> bool:
        fps = self.fps
        return fps is not None and fps < LOW_FPS

    def state_fields(self) -> dict:
        """The keyword arguments of ``state_message`` for this verifier."""
        fps = self.fps
        return {'profile': PROFILE_TOWER, 'state': self.state, 'width': self.width,
                'height': self.height, 'fps': None if fps is None else round(fps, 1),
                'fps_low': self.fps_low, 'passthrough': self.passthrough, 'outside': OUTSIDE,
                'profile_id': self.profile_id, 'platform': self.platform, 'fault': self.fault,
                'dropped': self.dropped}

    # -- inside -------------------------------------------------------------

    def _start_session(self, now):
        self._last_frame_at = now
        self._deadline = None if now is None else now + VERIFY_WINDOW_S
        self._good = 0
        self._bad_in_a_row = 0
        self._kind = None
        self._times = collections.deque(maxlen=FPS_WINDOW_FRAMES)

    def _rate(self):
        if len(self._times) < 2:
            return None
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else None

    def _refused(self, now):
        self.dropped += 1
        if self.state == STATE_STREAMING:
            self._bad_in_a_row += 1
            if self._bad_in_a_row >= BAD_FRAMES_LIMIT:
                self._fail(now, FAULT_BAD_FRAMES)

    def _fail(self, now, fault):
        self._faults_in_a_row += 1
        self.retry_at = now + min(RETRY_FIRST_S * 2 ** min(self._faults_in_a_row - 1, 8), RETRY_MAX_S)
        self._set(STATE_FAULT, fault)

    def _require(self, state):
        if self.state != state:
            raise RuntimeError(f'expected {state}, the verifier is {self.state}')

    def _set(self, state, fault):
        if (state, fault) != (self.state, self.fault):
            self.state, self.fault = state, fault
            self.generation += 1


# ---------------------------------------------------------------------------
# The studio stream's rate limit and the state message
# ---------------------------------------------------------------------------

class RateLimiter:
    """At most ``max_hz`` frames per second on average, one per slot of
    1 / max_hz, never a burst. The slots stay anchored to the first allowed
    frame, so a frame that arrives a little early for its slot is taken late
    in the slot rather than skipping to the next one: 30 fps with jitter
    becomes 15 Hz, not 10. After a gap of a whole slot the schedule restarts
    at the next frame. ``now`` defaults to ``time.monotonic()``."""

    def __init__(self, max_hz: float):
        if type(max_hz) not in (int, float) or not math.isfinite(max_hz) or max_hz <= 0:
            raise ValueError(f'max_hz is a positive finite number, not {max_hz!r}')
        self.max_hz = float(max_hz)
        self._period = 1.0 / self.max_hz
        self._next = None

    def allow(self, now=None) -> bool:
        if now is None:
            now = time.monotonic()
        if self._next is not None and now < self._next:
            return False
        if self._next is None or now - self._next >= self._period:
            self._next = now + self._period
        else:
            self._next += self._period
        return True


def _bad_state_field(key, value):
    if key == 'profile':
        return value not in PROFILE_VALUES.values()
    if key == 'state':
        return value not in STATES
    if key == 'fault':
        return value not in FAULTS
    if key == 'platform':
        return value not in PLATFORMS
    if key == 'profile_id':
        return type(value) is not str or not _PROFILE_ID_RE.fullmatch(value)
    if key == 'outside':
        return value != OUTSIDE
    if key in ('width', 'height'):
        return value is not None and (type(value) is not int or value < 0)
    if key == 'fps':
        return value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                      or value < 0)
    if key == 'fps_low':
        return type(value) is not bool
    if key == 'passthrough':
        return value is not None and type(value) is not bool
    return type(value) is not int or value < 0          # dropped


def state_message(**fields) -> bytes:
    """The camera state (camera id 17) as compact UTF-8 JSON, keys in the
    fixed order of ``STATE_KEYS`` with ``v`` = ``STATE_SCHEMA`` first and
    ``dropped`` last. Every other key must be given, and nothing else;
    a value of the wrong kind is refused."""
    expected = STATE_KEYS[1:]
    if set(fields) != set(expected):
        raise TypeError(f'state_message takes exactly {expected}; missing '
                        f'{sorted(set(expected) - set(fields))}, unknown {sorted(set(fields) - set(expected))}')
    for key in expected:
        if _bad_state_field(key, fields[key]):
            raise ValueError(f'state_message: {key}={fields[key]!r} is not allowed')
    message = {'v': STATE_SCHEMA}
    message.update((key, fields[key]) for key in expected)
    data = json.dumps(message, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')
    if len(data) > STATE_MAX_BYTES:
        raise ValueError(f'state_message is {len(data)} bytes, more than {STATE_MAX_BYTES}')
    return data
