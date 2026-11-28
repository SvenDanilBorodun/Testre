# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-06: the tower scene camera on Windows (spec 40 S1).

With ``EDUBOTICS_SCENE_CAMERA_PROFILE=tower_v1`` the camera bridge captures
the scene camera with ``camera_bridge._SceneCaptureWorker`` instead of today's
``_CaptureWorker``: one frozen mode (1920x1080 MJPEG, the camera's own JPEG
passed through), checked by ``scene_split.CaptureVerifier`` before any frame
leaves it. In ``streaming`` each frame is split into the recorded 640x480
outside frame (camera id 1, wire unchanged, sent by the unchanged
``_SenderWorker``), the camera's own JPEG for Roboter Studio (id 16, at most
15 Hz) and the camera state (id 17). The senders for ids 16 and 17 speak first:
a hello the ingest node answers, else they stay idle and the GUI says once that
the robot image is older than the software (G7). ``legacy`` builds exactly
today's bridge.

The camera is a stand-in yielding the real 1080p fixture of WP-04; the clock is
injected and moves 1/30 s per read, so the verifier's 3 s and 2 s windows pass
in milliseconds. The pixel tests need OpenCV and numpy and skip without them.
"""

import json
import pathlib
import socket
import struct
import sys
import textwrap
import threading
import time
import types
import unittest
from unittest import mock

from gui.app import camera_bridge, constants, scene_split, win_camera
from test_scene_split import _jpeg_header
from timeout_guard import BoundedTestCase

try:  # numpy/cv2 may be absent on a bare runner.
    import cv2
    import numpy as np
    _HAVE_CV2 = True
except ImportError:  # pragma: no cover - environment dependent
    _HAVE_CV2 = False

_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'scene_split_1080p.jpg'
_GUI_APP = pathlib.Path(__file__).resolve().parent.parent / 'gui' / 'app' / 'gui_app.py'
_HEADER = struct.Struct('>BIQ')
_HELLO_FRAME = _HEADER.pack(scene_split.HELLO_CAM_ID, 1, 0) + scene_split.HELLO
_FPS = 30.0
_STAND_IN_JPEG = b'\xff\xd8 a stand-in jpeg \xff\xd9'


class _Clock:
    """A monotonic clock the camera stand-in moves one frame per read."""

    def __init__(self, fps=_FPS):
        self.fps = fps
        self.frames = 0

    def __call__(self):
        return 1000.0 + self.frames / self.fps

    def tick(self):
        self.frames += 1


class _Cam:
    """A camera stand-in: ``frame_at(i)`` is the i-th read's frame (None = a
    failed read); ``on_read(i)`` runs before it is returned."""

    def __init__(self, clock, frame_at, on_read=None):
        self.clock = clock
        self.frame_at = frame_at
        self.on_read = on_read
        self.reads = 0
        self.released = False

    def read(self):
        if self.on_read is not None:
            self.on_read(self.reads)
        frame = self.frame_at(self.reads)
        self.reads += 1
        self.clock.tick()
        return frame is not None, frame

    def release(self):
        self.released = True


def _passthrough(jpeg):
    """What MSMF hands back with CONVERT_RGB 0: the JPEG as a 1xN uint8 array."""
    return np.frombuffer(jpeg, np.uint8).reshape(1, -1)


def _jpeg(frame, quality=90):
    ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()


def _frames(data):
    """[(cam_id, capture_ns, payload)] of a sender's byte stream."""
    out, i = [], 0
    while i + _HEADER.size <= len(data):
        cam_id, length, ns = _HEADER.unpack(data[i:i + _HEADER.size])
        i += _HEADER.size
        out.append((cam_id, ns, bytes(data[i:i + length])))
        i += length
    return out


def _state(worker):
    item, _seq = worker.state_slot.latest()
    return json.loads(item[0].decode('utf-8'))


def _wait_until(predicate, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Patched(BoundedTestCase):
    """Every test here: the self-test passes unless a test says otherwise, and
    the scene camera is whatever ``self.cam`` is."""

    def setUp(self):
        self.clock = _Clock()
        self.cam = None
        self.opened = mock.Mock(side_effect=lambda index: self.cam)
        for patcher in (mock.patch.object(camera_bridge.win_camera, 'open_scene_capture',
                                          self.opened),
                        mock.patch.object(camera_bridge.scene_split, 'selftest', return_value='')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _worker(self, log=None):
        return camera_bridge._SceneCaptureWorker(1, 'scene', 4, log=log, clock=self.clock)

    def _run_until(self, worker, predicate, timeout_s=20.0):
        """Run ``worker`` in its own thread until ``predicate()`` holds, then
        stop and join it."""
        loop = threading.Thread(target=worker.run, daemon=True)
        loop.start()
        try:
            self.assertTrue(_wait_until(predicate, timeout_s), 'the predicate never held')
        finally:
            worker.stop()
            loop.join(5.0)
        self.assertFalse(loop.is_alive())


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class ThePassthroughStream(_Patched):

    def setUp(self):
        super().setUp()
        self.jpeg = _FIXTURE.read_bytes()

    def test_after_20_frames_the_scene_sender_gets_the_outside_frame(self):
        """640x480 JPEGs on camera id 1 whose decode is outside_from_jpeg's
        frame (the existing quality 80); nothing is queued before streaming."""
        frame = _passthrough(self.jpeg)
        seqs, states = [], []
        worker = self._worker()

        def on_read(i):
            seqs.append(worker.latest()[1])
            states.append(worker.verifier.state)

        self.cam = _Cam(self.clock, lambda i: frame, on_read)
        halt = threading.Event()
        sender = camera_bridge._SenderWorker(worker, '127.0.0.1', 1, halt)
        sent = bytearray()

        class _Sock:
            def sendall(self, data):
                sent.extend(data)
                if len(_frames(sent)) >= 3:
                    halt.set()
                    worker.stop()

            def close(self):
                pass

        sender._connect = lambda: _Sock()
        loop = threading.Thread(target=worker.run, daemon=True)
        loop.start()
        sender.run()
        loop.join(5.0)
        self.assertFalse(loop.is_alive())

        # Nothing left the worker while it verified: reads 1 … 20 saw sequence 0.
        self.assertEqual(seqs[:20], [0] * 20)
        self.assertEqual(states[:20], ['verifying'] * 20)
        self.assertEqual((states[20], seqs[20]), ('streaming', 1))

        outside = scene_split.outside_from_jpeg(self.jpeg)
        expected = _jpeg(outside, constants.CAMERA_JPEG_QUALITY)
        frames = _frames(sent)
        self.assertGreaterEqual(len(frames), 3)
        for cam_id, ns, payload in frames:
            self.assertEqual(cam_id, scene_split.SCENE_CAM_ID)
            self.assertGreater(ns, 0)
            self.assertEqual(scene_split.jpeg_size(payload), (640, 480))
            self.assertEqual(payload, expected)
            decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(decoded.shape, (480, 640, 3))
            self.assertLess(float(np.abs(decoded.astype(float) - outside).mean()), 3.0)

    def test_the_studio_frames_are_the_camera_bytes_at_most_15_per_second(self):
        frame = _passthrough(self.jpeg)
        studio = []
        worker = self._worker()

        def on_read(i):
            studio.append(worker.studio.latest())
            if i == 120:
                worker.stop()

        self.cam = _Cam(self.clock, lambda i: frame, on_read)
        worker.run()
        studio.append(worker.studio.latest())
        self.assertEqual(studio[19][1], 0, 'a studio frame before streaming')
        first = next(i for i, (_item, seq) in enumerate(studio) if seq == 1)
        span_s = (len(studio) - 1 - first) / _FPS
        count = studio[-1][1]
        self.assertLessEqual(count, 15.0 * span_s + 1, f'{count} studio frames in {span_s:.2f} s')
        self.assertGreaterEqual(count, 15.0 * span_s - 1, f'{count} studio frames in {span_s:.2f} s')
        payloads = {item[0] for item, seq in studio if item is not None}
        self.assertEqual(payloads, {self.jpeg})

    def test_a_truncated_frame_reaches_no_sender_and_the_state_counts_it(self):
        """D59: a JPEG cut short decodes to a full-size frame on OpenCV 4.10,
        so the end-of-image check has to drop it before the split."""
        whole = _passthrough(self.jpeg)
        cut = _passthrough(self.jpeg[:len(self.jpeg) // 2])
        bad = range(40, 44)     # four in a row: fewer than BAD_FRAMES_LIMIT
        outside, studio = [], []
        worker = self._worker()

        def on_read(i):
            outside.append(worker.latest()[1])
            studio.append(worker.studio.latest())
            if i == 100:
                worker.stop()

        self.cam = _Cam(self.clock, lambda i: cut if i in bad else whole, on_read)
        worker.run()
        # outside[i] is the sequence before read i: each whole streaming frame
        # adds one, the four cut ones add none.
        self.assertEqual(outside[40] - outside[39], 1)
        self.assertEqual(outside[44], outside[40])
        self.assertEqual(outside[45] - outside[44], 1)
        self.assertEqual(studio[44][1] - studio[40][1], 0)
        cut_bytes = self.jpeg[:len(self.jpeg) // 2]
        for item, _seq in studio:
            if item is not None:
                self.assertNotEqual(item[0], cut_bytes)
        self.assertEqual(worker.verifier.dropped, 4)
        self.assertEqual(_state(worker)['dropped'], 4)

    def test_a_frame_only_the_split_refuses_is_dropped_and_counted(self):
        """A complete 1920x1080 header with no decodable picture: the verifier
        releases it, ``outside_from_jpeg`` refuses it. It reaches no sender and
        counts as dropped, and 15 in a row are ``bad_frames``, as on the Pi
        (test_scene_camera_node)."""
        whole = _passthrough(self.jpeg)
        undecodable = _passthrough(_jpeg_header(1920, 1080))
        self.assertTrue(scene_split.jpeg_complete(undecodable))
        self.assertEqual(scene_split.jpeg_size(undecodable), (1920, 1080))
        with self.assertRaises(scene_split.SplitError):
            scene_split.outside_from_jpeg(undecodable)
        first_bad = scene_split.VERIFY_FRAMES
        bad = range(first_bad, first_bad + scene_split.BAD_FRAMES_LIMIT)
        outside, studio, dropped = [], [], []
        worker = self._worker()

        def on_read(i):
            outside.append(worker.latest()[1])
            studio.append(worker.studio.latest()[1])
            dropped.append(worker.verifier.dropped)
            if i == bad[-1]:
                worker.stop()

        self.cam = _Cam(self.clock, lambda i: undecodable if i in bad else whole, on_read)
        worker.run()
        # The 20 whole frames streamed one; none of the 15 added another.
        self.assertEqual((outside[first_bad], studio[first_bad]), (1, 1))
        self.assertEqual((worker.latest()[1], worker.studio.latest()[1]), (1, 1))
        self.assertEqual(dropped[first_bad:], list(range(scene_split.BAD_FRAMES_LIMIT)))
        self.assertEqual(worker.verifier.dropped, scene_split.BAD_FRAMES_LIMIT)
        self.assertEqual((worker.verifier.state, worker.verifier.fault), ('fault', 'bad_frames'))
        self.assertTrue(self.cam.released)
        self.assertEqual(_state(worker)['fault'], 'bad_frames')

    def test_the_state_says_streaming_with_passthrough_and_the_windows_profile_id(self):
        frame = _passthrough(self.jpeg)
        worker = self._worker()
        self.cam = _Cam(self.clock, lambda i: frame,
                        lambda i: worker.stop() if i == 60 else None)
        worker.run()
        state = _state(worker)
        self.assertEqual(list(state), list(scene_split.STATE_KEYS))
        self.assertEqual((state['profile'], state['state'], state['passthrough']),
                         ('tower_v1', 'streaming', True))
        self.assertEqual((state['width'], state['height'], state['outside']),
                         (1920, 1080, '640x480'))
        self.assertEqual((state['platform'], state['fault']), ('windows', ''))
        self.assertEqual(state['profile_id'], scene_split.profile_id(constants.CAMERA_JPEG_QUALITY))

    def test_reaching_streaming_logs_the_protokoll_line_once(self):
        frame = _passthrough(self.jpeg)
        logs = []
        worker = self._worker(log=logs.append)
        self.cam = _Cam(self.clock, lambda i: frame,
                        lambda i: worker.stop() if i == 60 else None)
        worker.run()
        self.assertEqual(logs, [
            'Turm-Kamera: 1920×1080 MJPG, 30.0 Bilder/s, Direktdurchleitung aktiv'])
        self.assertEqual(worker.last_error_de, '')


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class TheDecodedFallback(_Patched):

    def test_decoded_frames_take_the_fallback(self):
        """No raw sample from the backend (Windows runs the camera as NV12, so
        win_camera lets the backend decode): outside_from_bgr, the studio frame
        is the decoded frame encoded at quality 90, and the Protokoll line
        names the format Windows negotiated."""
        frame = cv2.imdecode(np.frombuffer(_FIXTURE.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(frame.shape, (1080, 1920, 3))
        logs = []
        worker = self._worker(log=logs.append)
        self.cam = _Cam(self.clock, lambda i: frame,
                        lambda i: worker.stop() if i == 40 else None)
        self.cam.mode = win_camera.SceneMode('NV12', 1920, 1080, 30.0, passthrough=False)
        worker.run()
        self.assertIs(worker.verifier.passthrough, False)
        (outside, ns), seq = worker.latest()
        self.assertGreater(seq, 0)
        self.assertTrue(np.array_equal(outside, scene_split.outside_from_bgr(frame)))
        (studio, _ns), _seq = worker.studio.latest()
        self.assertEqual(studio, _jpeg(frame, camera_bridge.STUDIO_FALLBACK_JPEG_QUALITY))
        self.assertIs(_state(worker)['passthrough'], False)
        self.assertEqual(logs, [
            'Turm-Kamera: 1920×1080 NV12, 30.0 Bilder/s, Direktdurchleitung aus'])


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class TheWrongModeNeverLeaves(_Patched):
    """G3, the GUI half: a 1280x720 stream is refused while verifying and the
    worker queues no outside frame for it."""

    def _assert_wrong_mode(self, frame):
        worker = self._worker()
        self.cam = _Cam(self.clock, lambda i: frame)
        self._run_until(worker, lambda: worker.verifier.fault == 'wrong_mode')
        self.assertEqual(worker.latest(), (None, 0))
        self.assertEqual(worker.studio.latest(), (None, 0))
        self.assertTrue(self.cam.released, 'the device stays open after the fault')
        self.assertEqual(worker.last_error_de, camera_bridge.SCENE_WRONG_MODE_DE)
        self.assertEqual(_state(worker)['fault'], 'wrong_mode')

    def test_a_1280x720_jpeg_stream(self):
        small = cv2.resize(cv2.imdecode(np.frombuffer(_FIXTURE.read_bytes(), np.uint8),
                                        cv2.IMREAD_COLOR), (1280, 720))
        self._assert_wrong_mode(_passthrough(_jpeg(small)))

    def test_a_decoded_1280x720_stream(self):
        self._assert_wrong_mode(np.zeros((720, 1280, 3), np.uint8))


class TheSelfTestComesFirst(_Patched):

    def test_a_failing_selftest_never_opens_the_device(self):
        camera_bridge.scene_split.selftest.return_value = 'outside_from_bgr is off by 9 levels'
        worker = self._worker()
        worker.run()         # returns at once: nothing to stream
        self.opened.assert_not_called()
        self.assertEqual((worker.verifier.state, worker.verifier.fault), ('fault', 'selftest'))
        self.assertEqual(worker.last_error_de, camera_bridge.SCENE_SELFTEST_FAILED_DE)
        self.assertEqual(camera_bridge.SCENE_SELFTEST_FAILED_DE,
                         'Selbsttest der Bildaufbereitung fehlgeschlagen — die Turm-Kamera '
                         'bleibt aus. Bitte EduBotics aktualisieren.')
        self.assertEqual(_state(worker)['fault'], 'selftest')
        self.assertEqual(worker.latest(), (None, 0))

    def test_the_protokoll_says_it_once(self):
        """Through the GUI's own camera-health poll, which logs a camera's
        error once per change."""
        camera_bridge.scene_split.selftest.return_value = 'broken build'
        bridge = camera_bridge.CameraBridge({'scene': 1}, scene_profile='tower_v1')
        bridge.workers[0].run()
        logs = []
        owner = types.SimpleNamespace(
            camera_bridge=bridge, _log=logs.append, _camera_health_after_id=None,
            _camera_health_last={}, _poll_camera_health=None,
            root=types.SimpleNamespace(after=lambda _ms, fn=None: 'after#1'))
        poll = _load_gui_method('_poll_camera_health', {'CAMERA_HEALTH_POLL_MS': 3000})
        for _ in range(5):
            poll(owner)
        self.assertEqual(logs, ['[WARNUNG] ' + camera_bridge.SCENE_SELFTEST_FAILED_DE])
        self.opened.assert_not_called()


class AnOpenFailureIsNamed(_Patched):

    def test_open_failed_names_another_program_or_no_camera(self):
        self.opened.side_effect = win_camera.CameraUnavailableError('busy')
        worker = self._worker()
        self._run_until(worker, lambda: worker.verifier.fault == 'open_failed')
        self.assertEqual(worker.last_error_de,
                         'Die Turm-Kamera wird von einem anderen Programm verwendet oder ist '
                         'nicht angeschlossen.')
        self.assertEqual(worker.latest(), (None, 0))


def _load_gui_method(name, ns):
    src = _GUI_APP.read_text(encoding='utf-8')
    marker = f'    def {name}(self'
    start = src.index(marker)
    rest = src[start:]
    end = rest.find('\n    def ', len(marker))
    snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
    exec(compile(snippet, str(_GUI_APP), 'exec'), ns)  # noqa: S102
    return ns[name]


# ---------------------------------------------------------------------------
# The hello (G7)
# ---------------------------------------------------------------------------

class _FakeIngest:
    """The container's camera ingest node on 127.0.0.1: records every
    connection's bytes; ``answer`` decides whether a hello is answered (an
    image of this release) or not (an older one, which publishes it nowhere)."""

    def __init__(self, answer):
        self.answer = answer
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(('127.0.0.1', 0))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self.conns = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._closed.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            data = bytearray()
            with self._lock:
                self.conns.append(data)
            threading.Thread(target=self._serve, args=(conn, data), daemon=True).start()

    def _serve(self, conn, data):
        answered = False
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                with self._lock:
                    data.extend(chunk)
                    hello = bytes(data[:len(_HELLO_FRAME)]) == _HELLO_FRAME
                if self.answer and hello and not answered:
                    answered = True
                    conn.sendall(scene_split.HELLO)

    def streams(self):
        with self._lock:
            return [bytes(d) for d in self.conns]

    def close(self):
        self._closed.set()
        self._srv.close()


def _after_hello(stream):
    """The frames a sender wrote after its hello."""
    frames = _frames(stream)
    if frames and frames[0][0] == scene_split.HELLO_CAM_ID:
        frames = frames[1:]
    return frames


class _Source:
    """A slot that always holds an item (the worker's studio or state slot)."""

    def __init__(self):
        self.slot = camera_bridge._Slot()
        self.slot.put((_STAND_IN_JPEG, 123456789))


class TheHelloGatesIds16And17(BoundedTestCase):

    def _ingest(self, answer):
        ingest = _FakeIngest(answer)
        self.addCleanup(ingest.close)
        return ingest

    def _sender(self, ingest, source, refused, cam_id=scene_split.STUDIO_CAM_ID, resend_s=None):
        halt = threading.Event()
        sender = camera_bridge._HelloSenderWorker(
            source.slot, cam_id, '127.0.0.1', ingest.port, halt,
            lambda: refused.append(cam_id), resend_s=resend_s)
        sender.start()

        def stop():
            halt.set()
            source.slot.wake()
            sender.join(5.0)

        self.addCleanup(stop)
        return sender, stop

    def test_an_unanswered_hello_sends_no_frame_and_says_so_once(self):
        ingest = self._ingest(answer=False)
        refused = []
        source = _Source()
        sender, stop = self._sender(ingest, source, refused)
        self.assertTrue(_wait_until(lambda: refused, 5.0))
        source.slot.put((_STAND_IN_JPEG, 5))       # new frames keep arriving …
        time.sleep(0.3)
        stop()
        self.assertFalse(sender.is_alive())
        self.assertEqual(ingest.streams(), [_HELLO_FRAME])   # … and none is sent
        self.assertEqual(len(refused), 1)
        self.assertEqual(_after_hello(ingest.streams()[0]), [])

    def test_an_answered_hello_forwards_the_bytes_verbatim(self):
        ingest = self._ingest(answer=True)
        refused = []
        source = _Source()
        self._sender(ingest, source, refused)
        self.assertTrue(_wait_until(lambda: len(ingest.streams()) == 1
                                    and len(ingest.streams()[0]) >= len(_HELLO_FRAME), 5.0))
        source.slot.put((_STAND_IN_JPEG + b'2', 77))
        self.assertTrue(_wait_until(lambda: _after_hello(ingest.streams()[0]), 5.0))
        self.assertEqual(_after_hello(ingest.streams()[0])[0],
                         (scene_split.STUDIO_CAM_ID, 77, _STAND_IN_JPEG + b'2'))
        self.assertEqual(refused, [])

    def test_the_check_has_teeth(self):
        """The mutation of G7: with the answer check removed the same silent
        ingest receives frames, so the test above would fail."""
        ingest = self._ingest(answer=False)
        refused = []
        source = _Source()
        with mock.patch.object(camera_bridge._HelloSenderWorker, '_hello', return_value=True):
            self._sender(ingest, source, refused)
            source.slot.put((_STAND_IN_JPEG, 6))
            self.assertTrue(_wait_until(
                lambda: ingest.streams() and _after_hello(ingest.streams()[0]), 5.0))

    def test_the_state_sender_sends_at_once_and_again_when_nothing_changed(self):
        ingest = self._ingest(answer=True)
        source = _Source()
        self._sender(ingest, source, [], cam_id=scene_split.STATE_CAM_ID, resend_s=0.2)
        self.assertTrue(_wait_until(
            lambda: ingest.streams() and len(_after_hello(ingest.streams()[0])) >= 3, 5.0))
        for cam_id, _ns, payload in _after_hello(ingest.streams()[0]):
            self.assertEqual((cam_id, payload), (scene_split.STATE_CAM_ID, _STAND_IN_JPEG))

    def test_a_studio_sender_waits_for_a_new_frame(self):
        """D21 a for the new sender: it starts at the slot's sequence, so the
        frame already there before it connected is not sent, and nothing spins."""
        ingest = self._ingest(answer=True)
        source = _Source()
        self._sender(ingest, source, [])
        self.assertTrue(_wait_until(lambda: ingest.streams()
                                    and ingest.streams()[0] == _HELLO_FRAME, 5.0))
        time.sleep(0.3)
        self.assertEqual(ingest.streams(), [_HELLO_FRAME])


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

def _stand_in_cv2():
    return types.SimpleNamespace(
        IMWRITE_JPEG_QUALITY=1,
        imencode=lambda ext, frame, params: (True, memoryview(_STAND_IN_JPEG)),
        resize=lambda frame, size: frame)


class _TargetSizeFrame:
    shape = (constants.CAMERA_HEIGHT, constants.CAMERA_WIDTH, 3)


class _StreamingCap:

    def __init__(self, frame):
        self.frame = frame

    def read(self):
        time.sleep(0.005)
        return True, self.frame

    def release(self):
        pass


class TheLegacyProfileIsTodaysBridge(BoundedTestCase):

    def test_legacy_builds_todays_capture_worker_for_the_scene_camera(self):
        bridge = camera_bridge.CameraBridge({'gripper': 0, 'scene': 1}, scene_profile='legacy')
        self.assertEqual([type(w) for w in bridge.workers],
                         [camera_bridge._CaptureWorker, camera_bridge._CaptureWorker])
        self.assertEqual([type(s) for s in bridge._senders],
                         [camera_bridge._SenderWorker, camera_bridge._SenderWorker])
        self.assertEqual(bridge._tower_senders, [])
        # Without the argument too: legacy is the default.
        self.assertEqual(camera_bridge.CameraBridge({'scene': 1})._tower_senders, [])

    def test_legacy_opens_no_connection_for_ids_16_and_17(self):
        ingest = _FakeIngest(answer=True)
        self.addCleanup(ingest.close)
        bridge = camera_bridge.CameraBridge({'gripper': 0, 'scene': 1}, host='127.0.0.1',
                                            port=ingest.port, scene_profile='legacy')
        scene_open = mock.Mock()
        for patcher in (mock.patch.dict(sys.modules, {'cv2': _stand_in_cv2()}),
                        mock.patch.object(camera_bridge.win_camera, 'open_capture',
                                          side_effect=lambda *a: (_StreamingCap(_TargetSizeFrame()),
                                                                  {'fps': 30.0, 'fourcc': 'MJPG',
                                                                   'msmf': True})),
                        mock.patch.object(camera_bridge.win_camera, 'open_scene_capture',
                                          scene_open)):
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge.start()
        self.addCleanup(bridge.stop)
        self.assertTrue(_wait_until(lambda: len(ingest.streams()) == 2
                                    and all(len(_frames(s)) >= 2 for s in ingest.streams()), 5.0))
        time.sleep(0.3)
        bridge.stop()
        streams = ingest.streams()
        self.assertEqual(len(streams), 2)
        self.assertEqual({cam_id for s in streams for cam_id, _ns, _p in _frames(s)}, {0, 1})
        scene_open.assert_not_called()


class TheTowerProfileBuildsTheSceneWorker(BoundedTestCase):

    def test_the_scene_camera_gets_the_tower_worker_and_two_hello_senders(self):
        bridge = camera_bridge.CameraBridge({'gripper': 0, 'scene': 1}, scene_profile='tower_v1')
        bridge.register_external_source(constants.PHONE_CAM_ID, object())
        by_role = {w.role: w for w in bridge.workers}
        self.assertIs(type(by_role['gripper']), camera_bridge._CaptureWorker)
        self.assertIs(type(by_role['scene']), camera_bridge._SceneCaptureWorker)
        self.assertEqual(by_role['scene'].cam_id, scene_split.SCENE_CAM_ID)
        # The scene sender is today's sender, on today's wire (camera id 1).
        self.assertEqual([type(s) for s in bridge._senders],
                         [camera_bridge._SenderWorker, camera_bridge._SenderWorker])
        self.assertEqual({s.worker.role for s in bridge._senders}, {'gripper', 'scene'})
        tower = {s.cam_id: s for s in bridge._tower_senders}
        self.assertEqual(set(tower), {scene_split.STUDIO_CAM_ID, scene_split.STATE_CAM_ID})
        self.assertIs(tower[scene_split.STUDIO_CAM_ID].source, by_role['scene'].studio)
        self.assertIs(tower[scene_split.STATE_CAM_ID].source, by_role['scene'].state_slot)
        self.assertIsNone(tower[scene_split.STUDIO_CAM_ID].resend_s)
        self.assertEqual(tower[scene_split.STATE_CAM_ID].resend_s, camera_bridge.STATE_RESEND_S)
        self.assertEqual(camera_bridge.STATE_RESEND_S, 1.0)
        self.assertEqual(camera_bridge.HELLO_WAIT_S, 1.0)
        self.assertIs(type(bridge._external_senders[0]), camera_bridge._ExternalSenderWorker)

    def test_a_gripper_only_rig_builds_no_tower_sender(self):
        bridge = camera_bridge.CameraBridge({'gripper': 0}, scene_profile='tower_v1')
        self.assertEqual(bridge._tower_senders, [])

    def test_the_new_threads_use_halt(self):
        """D24: never ``self._stop`` on a thread (it shadows Thread._stop)."""
        bridge = camera_bridge.CameraBridge({'gripper': 0, 'scene': 1}, scene_profile='tower_v1')
        for thread in bridge.workers + bridge._senders + bridge._tower_senders:
            with self.subTest(thread=thread.name):
                self.assertNotIn('_stop', vars(thread))
                self.assertIsInstance(thread._halt, threading.Event)

    def test_stop_before_start_is_safe(self):
        camera_bridge.CameraBridge({'scene': 1}, scene_profile='tower_v1').stop()


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class AnOlderImage(BoundedTestCase):
    """The whole tower bridge against an ingest node that does not answer the
    hello (an image older than the GUI)."""

    TEST_TIMEOUT_S = 60.0

    def test_ids_16_and_17_send_nothing_and_the_gui_logs_once(self):
        jpeg = _FIXTURE.read_bytes()
        ingest = _FakeIngest(answer=False)
        self.addCleanup(ingest.close)
        logs = []
        frame = _passthrough(jpeg)

        class _Cap:
            def read(self):
                time.sleep(1.0 / _FPS)
                return True, frame

            def release(self):
                pass

        for patcher in (mock.patch.object(camera_bridge.win_camera, 'open_scene_capture',
                                          side_effect=lambda index: _Cap()),
                        mock.patch.object(camera_bridge.scene_split, 'selftest',
                                          return_value='')):
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge = camera_bridge.CameraBridge({'scene': 1}, host='127.0.0.1', port=ingest.port,
                                            scene_profile='tower_v1', log=logs.append)
        bridge.start()
        self.addCleanup(bridge.stop)
        self.assertTrue(_wait_until(
            lambda: logs.count(camera_bridge.OLDER_IMAGE_DE) == 1
            and all(s.idle for s in bridge._tower_senders), 10.0), logs)
        # The recorded scene picture still flows on id 1.
        self.assertTrue(_wait_until(
            lambda: any(_frames(s) and _frames(s)[0][0] == scene_split.SCENE_CAM_ID
                        for s in ingest.streams()), 10.0))
        time.sleep(0.5)
        bridge.stop()
        self.assertEqual(logs.count(camera_bridge.OLDER_IMAGE_DE), 1, logs)
        self.assertEqual(camera_bridge.OLDER_IMAGE_DE,
                         'Das Roboter-Abbild ist älter als die Software — Roboter Studio '
                         'bekommt kein Bild der Turm-Kamera.')
        streams = ingest.streams()
        self.assertEqual(sorted(streams).count(_HELLO_FRAME), 2, [s[:20] for s in streams])
        for stream in streams:
            ids = {cam_id for cam_id, _ns, _p in _frames(stream)}
            self.assertTrue(ids in ({scene_split.HELLO_CAM_ID}, {scene_split.SCENE_CAM_ID}), ids)


# ---------------------------------------------------------------------------
# The checkbox's capability probe
# ---------------------------------------------------------------------------

class TheProbe(_Patched):

    @unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
    def test_twenty_1080p_jpegs_pass(self):
        frame = _passthrough(_FIXTURE.read_bytes())
        self.cam = _Cam(self.clock, lambda i: frame)
        self.assertEqual(camera_bridge.probe_scene_camera(3, clock=self.clock), '')
        self.opened.assert_called_once_with(3)
        self.assertEqual(self.cam.reads, scene_split.VERIFY_FRAMES)
        self.assertTrue(self.cam.released)

    @unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
    def test_a_1280x720_camera_is_refused(self):
        small = cv2.resize(cv2.imdecode(np.frombuffer(_FIXTURE.read_bytes(), np.uint8),
                                        cv2.IMREAD_COLOR), (1280, 720))
        frame = _passthrough(_jpeg(small))
        self.cam = _Cam(self.clock, lambda i: frame)
        self.assertEqual(camera_bridge.probe_scene_camera(3, clock=self.clock),
                         'Diese Kamera liefert kein Bild mit 1920 × 1080 Bildpunkten. '
                         'Die Turm-Kamera wurde nicht aktiviert.')
        self.assertTrue(self.cam.released)

    def test_a_silent_camera_is_refused_as_silent(self):
        """It is given the whole start-up allowance, and the sentence says it
        sent nothing (not that it sent the wrong size)."""
        self.cam = _Cam(self.clock, lambda i: None)
        self.assertEqual(camera_bridge.probe_scene_camera(3, clock=self.clock, sleep=_no_sleep),
                         camera_bridge.SCENE_PROBE_NO_FRAMES_DE)
        self.assertGreaterEqual(self.cam.reads / _FPS, scene_split.FIRST_FRAME_WAIT_S)
        self.assertTrue(self.cam.released)
        self.assertEqual(camera_bridge.SCENE_PROBE_NO_FRAMES_DE,
                         'Die Kamera liefert keine Bilder. Bitte andere Programme schließen, '
                         'die die Kamera benutzen, und das USB-Kabel prüfen. '
                         'Die Turm-Kamera wurde nicht aktiviert.')

    def test_a_camera_that_does_not_open_is_named(self):
        self.opened.side_effect = win_camera.CameraUnavailableError('busy')
        slept = []
        reason = camera_bridge.probe_scene_camera(3, clock=self.clock, sleep=slept.append)
        self.assertTrue(reason.startswith(camera_bridge.SCENE_OPEN_FAILED_DE), reason)
        self.assertIn('Die Turm-Kamera wurde nicht aktiviert.', reason)
        self.assertEqual(self.opened.call_count, camera_bridge.PROBE_OPEN_ATTEMPTS)
        self.assertEqual(slept, [camera_bridge.PROBE_OPEN_RETRY_S] * (camera_bridge.PROBE_OPEN_ATTEMPTS - 1))

    @unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
    def test_a_device_still_closing_after_the_previews_opens_on_the_next_try(self):
        frame = _passthrough(_FIXTURE.read_bytes())
        cam = _Cam(self.clock, lambda i: frame)
        self.opened.side_effect = [win_camera.CameraUnavailableError('busy'), cam]
        self.assertEqual(camera_bridge.probe_scene_camera(3, clock=self.clock, sleep=_no_sleep), '')
        self.assertEqual(self.opened.call_count, 2)
        self.assertTrue(cam.released)

    def test_a_failing_selftest_refuses_without_opening(self):
        with mock.patch.object(camera_bridge.scene_split, 'selftest', return_value='bad build'):
            self.assertEqual(camera_bridge.probe_scene_camera(3, clock=self.clock),
                             camera_bridge.SCENE_PROBE_SELFTEST_DE)
        self.opened.assert_not_called()


def _no_sleep(_seconds):
    pass


class _SlowStartCam:
    """The first read blocks ``first_s`` (Media Foundation and V4L2 start the
    stream at the first read), every later one ``1 / fps``; ``frame_at(i)``
    as on ``_Cam``. The clock is a float the camera moves."""

    def __init__(self, clock, first_s, fps, frame_at, mode=None):
        self.clock, self.first_s, self.fps, self.frame_at = clock, first_s, fps, frame_at
        self.reads, self.released, self.mode = 0, False, mode

    def read(self):
        self.clock.t += self.first_s if self.reads == 0 else 1.0 / self.fps
        self.reads += 1
        frame = self.frame_at(self.reads - 1)
        return frame is not None, frame

    def release(self):
        self.released = True


class _SecondsClock:

    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class TheProbeAllowsForTheCameraStartUp(_Patched):
    """2.41.2: the verifier counted the camera's start-up against its 2 s
    silence and its 3 s window, so a camera whose every frame was a complete
    1920x1080 MJPEG was refused with „kein Bild mit 1920 × 1080" when its
    first frame took 2 s (or 1.8 s at 15 fps in a dim room). The start-up now
    has its own allowance and the window counts from the first frame."""

    def _probe(self, first_s, fps, frame_at, mode=None):
        clock = _SecondsClock()
        self.cam = _SlowStartCam(clock, first_s, fps, frame_at, mode)
        logs = []
        reason = camera_bridge.probe_scene_camera(3, clock=clock, log=logs.append, sleep=_no_sleep)
        self.assertTrue(self.cam.released)
        self.assertEqual(len(logs), 1, logs)
        return reason, logs[0]

    def setUp(self):
        super().setUp()
        self.good = _passthrough(_FIXTURE.read_bytes())

    def test_a_camera_that_starts_slowly_passes(self):
        for first_s, fps in ((2.0, 30.0), (2.5, 30.0), (6.0, 30.0), (1.8, 15.0), (9.5, 15.0)):
            with self.subTest(first_s=first_s, fps=fps):
                reason, _line = self._probe(first_s, fps, lambda i: self.good)
                self.assertEqual(reason, '')

    def test_the_report_names_the_format_the_first_frame_and_the_frames(self):
        mode = win_camera.SceneMode('MJPG', 1920, 1080, 30.0, passthrough=True)
        reason, line = self._probe(2.5, 30.0, lambda i: self.good, mode)
        self.assertEqual(reason, '')
        self.assertEqual(line, 'Turm-Kamera-Prüfung: Windows liefert MJPG 1920×1080 bei 30.0 '
                               'Bilder/s, Direktdurchleitung aktiv; erstes Bild nach 2.5 s; '
                               '20 von 20 Bildern in Ordnung (30.0 Bilder/s).')

    def test_the_wrong_size_is_still_refused_with_its_sentence(self):
        small = _passthrough(_jpeg(cv2.resize(cv2.imdecode(
            np.frombuffer(_FIXTURE.read_bytes(), np.uint8), cv2.IMREAD_COLOR), (1280, 720))))
        reason, line = self._probe(2.5, 30.0, lambda i: small)
        self.assertEqual(reason, camera_bridge.SCENE_PROBE_REFUSED_DE)
        self.assertIn('kein Bild mit 1920 × 1080', reason)
        self.assertIn('erstes Bild nach 2.5 s', line)
        self.assertIn('0 von ', line)
        self.assertIn('zuletzt 1280×720', line)

    def test_a_camera_that_sends_nothing_within_the_allowance_is_silent(self):
        reason, line = self._probe(scene_split.FIRST_FRAME_WAIT_S + 0.5, 30.0, lambda i: self.good)
        self.assertEqual(reason, camera_bridge.SCENE_PROBE_NO_FRAMES_DE)
        self.assertIn('kein Bild erhalten', line)

    def test_cut_frames_are_named_damaged(self):
        cut = _passthrough(_FIXTURE.read_bytes()[:150_000])
        reason, _line = self._probe(0.3, 30.0, lambda i: cut)
        self.assertEqual(reason, camera_bridge.SCENE_PROBE_BROKEN_DE)

    def test_mostly_cut_frames_are_named_damaged(self):
        cut = _passthrough(_FIXTURE.read_bytes()[:150_000])
        reason, _line = self._probe(0.3, 30.0, lambda i: self.good if i % 10 == 0 else cut)
        self.assertEqual(reason, camera_bridge.SCENE_PROBE_BROKEN_DE)

    def test_a_few_cut_frames_while_verifying_still_pass(self):
        cut = _passthrough(_FIXTURE.read_bytes()[:150_000])
        reason, _line = self._probe(0.3, 30.0, lambda i: self.good if i % 4 else cut)
        self.assertEqual(reason, '')

    def test_a_dark_room_too_slow_for_the_window_says_so(self):
        reason, line = self._probe(0.3, 5.0, lambda i: self.good)
        self.assertEqual(reason, camera_bridge.SCENE_PROBE_TOO_SLOW_DE.format(rate=' (nur 5)'))
        self.assertTrue(reason.startswith('Die Kamera liefert zu wenige Bilder pro Sekunde (nur 5). '
                                          'Meist ist es zu dunkel'), reason)
        self.assertIn('(5.0 Bilder/s)', line)

    def test_an_uncompressed_mode_too_slow_says_so(self):
        decoded = np.zeros((1080, 1920, 3), np.uint8)
        mode = win_camera.SceneMode('YUY2', 1920, 1080, 30.0, passthrough=False)
        reason, line = self._probe(0.3, 5.0, lambda i: decoded, mode)
        self.assertEqual(reason, camera_bridge.SCENE_PROBE_UNCOMPRESSED_DE.format(
            rate=' (5 Bilder pro Sekunde)'))
        self.assertIn('Windows liefert YUY2 1920×1080', line)
        self.assertIn('Direktdurchleitung aus', line)

    def test_a_slow_decoded_stream_of_another_format_is_named_dark(self):
        """NV12 may be MJPEG that Windows decoded, and an unreadable format is
        unknown: a slow stream of either is most likely a dark room."""
        decoded = np.zeros((1080, 1920, 3), np.uint8)
        for fourcc in ('NV12', ''):
            with self.subTest(fourcc=fourcc):
                mode = win_camera.SceneMode(fourcc, 1920, 1080, 30.0, passthrough=False)
                reason, _line = self._probe(0.3, 7.5, lambda i: decoded, mode)
                self.assertEqual(reason, camera_bridge.SCENE_PROBE_TOO_SLOW_DE.format(
                    rate=' (nur 8)'))

    def test_decoded_frames_at_the_full_rate_pass(self):
        decoded = np.zeros((1080, 1920, 3), np.uint8)
        mode = win_camera.SceneMode('NV12', 1920, 1080, 30.0, passthrough=False)
        reason, _line = self._probe(1.0, 30.0, lambda i: decoded, mode)
        self.assertEqual(reason, '')

    def test_a_read_that_raises_is_a_failed_read(self):
        def frame_at(i):
            if i < 3:
                raise OSError('MSMF hiccup')
            return self.good
        clock = _SecondsClock()
        self.cam = _SlowStartCam(clock, 0.5, 30.0, frame_at)
        self.assertEqual(camera_bridge.probe_scene_camera(3, clock=clock, sleep=_no_sleep), '')

    def test_every_sentence_is_german_and_ends_with_not_activated(self):
        for name in ('SCENE_PROBE_REFUSED_DE', 'SCENE_PROBE_NO_FRAMES_DE', 'SCENE_PROBE_BROKEN_DE',
                     'SCENE_PROBE_TOO_SLOW_DE', 'SCENE_PROBE_UNCOMPRESSED_DE',
                     'SCENE_PROBE_SELFTEST_DE', 'SCENE_PROBE_FAILED_DE'):
            text = getattr(camera_bridge, name).format(rate='')
            with self.subTest(name=name):
                self.assertTrue(text.endswith('Die Turm-Kamera wurde nicht aktiviert.'), text)
                for pair in ('ae', 'oe', 'ue'):
                    self.assertNotIn(f'schlie{pair}', text)
                self.assertNotIn('{', text)


if __name__ == '__main__':
    unittest.main()
