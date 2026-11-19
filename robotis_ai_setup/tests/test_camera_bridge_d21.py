# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""D21/D24: the native camera bridge's three of the four fixes, each fenced
here (the fourth, b, is the ingest node's: test_camera_ingest_d21.py).

a. A sender whose camera has not produced a frame yet WAITS for it. It used to
   start at sequence -1 against the capture worker's 0, so ``wait_new`` never
   blocked: millions of calls a second and a whole core until the first frame.
c. A camera whose frame is not 4:3 is centre-cropped to 4:3 before the resize
   to 640x480, so a circle stays a circle (a 16:9 frame used to be squeezed by
   a third). A 4:3 frame takes the unchanged path, byte for byte.
d. No worker thread keeps its stop event in ``self._stop``: that name shadows
   ``threading.Thread._stop``, which ``join()`` and ``is_alive()`` call on
   CPython 3.11 and 3.12, so ``CameraBridge.stop()`` raised
   ``TypeError: 'Event' object is not callable`` at its first join, after
   which no other thread was joined and ``timeEndPeriod(1)`` never ran
   (``gui_app._stop_camera_bridge`` swallowed the error).

OpenCV is replaced by a stand-in where a test looks at no pixels, so (a) and
(d) run without it; the pixel tests need OpenCV and numpy and skip without
them, like test_camera_preview_render.py. The loops of (a) and (c) run as the
target of a plain helper thread (or in the test's own thread), never through
the worker's own ``join``, so on the code before the fixes each test fails for
its own fix only.
"""

import ctypes
import socket
import struct
import sys
import threading
import time
import types
import unittest
from unittest import mock

from gui.app import camera_bridge, constants
from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

try:  # numpy/cv2 may be absent on a bare runner.
    import cv2
    import numpy as np
    _HAVE_CV2 = True
except ImportError:  # pragma: no cover - environment dependent
    _HAVE_CV2 = False

_JPEG = b'\xff\xd8 a stand-in jpeg \xff\xd9'
_CAP_INFO = {'fps': 30.0, 'fourcc': 'MJPG', 'msmf': True}


def _stand_in_cv2():
    """What the capture and sender loops call on cv2, without OpenCV."""
    return types.SimpleNamespace(
        IMWRITE_JPEG_QUALITY=1,
        imencode=lambda ext, frame, params: (True, memoryview(_JPEG)),
        resize=lambda frame, size: frame,
    )


class _TargetSizeFrame:
    """A frame of exactly the bridge's target size (no resize, no crop)."""

    shape = (constants.CAMERA_HEIGHT, constants.CAMERA_WIDTH, 3)


class _StreamingCap:
    """A camera that yields ``frame`` every few milliseconds."""

    def __init__(self, frame):
        self.frame = frame

    def read(self):
        time.sleep(0.005)
        return True, self.frame

    def release(self):
        pass


class _OneFrameCap:
    """Yields ``frame`` once; the next read stops ``worker`` and fails."""

    def __init__(self, frame, worker):
        self.frame = frame
        self.worker = worker
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.reads == 1:
            return True, self.frame
        self.worker.stop()
        return False, None

    def release(self):
        pass


class _RecordingSock:

    def __init__(self, on_send=None):
        self.sent = bytearray()
        self.on_send = on_send

    def sendall(self, data):
        self.sent += data
        if self.on_send is not None:
            self.on_send()

    def close(self):
        pass


class _PhoneSlot:
    """The phone receiver's latest-slot surface the external sender polls."""

    def __init__(self):
        self.seq = 0

    def get(self):
        self.seq += 1
        return _JPEG, time.time_ns(), self.seq

    def fresh_within(self, age_s):
        return True


class _LoopbackSink:
    """A TCP server on 127.0.0.1 that accepts every connection and drains it,
    standing in for the container's camera ingest node."""

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(('127.0.0.1', 0))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self.bytes_by_conn = []
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
            with self._lock:
                self.bytes_by_conn.append(0)
                slot = len(self.bytes_by_conn) - 1
            threading.Thread(target=self._drain, args=(conn, slot), daemon=True).start()

    def _drain(self, conn, slot):
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                with self._lock:
                    self.bytes_by_conn[slot] += len(chunk)

    def received(self):
        with self._lock:
            return list(self.bytes_by_conn)

    def close(self):
        self._closed.set()
        self._srv.close()


def _free_loopback_port():
    """A port nobody listens on (bound once, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _wait_until(predicate, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TheSenderWaitsForTheFirstFrame(BoundedTestCase):
    """(a)"""

    def test_a_camera_without_a_frame_costs_at_most_ten_wait_new_calls_in_0_3_s(self):
        worker = camera_bridge._CaptureWorker(1, 'scene', 0)  # never started: never a frame
        calls = [0]
        real_wait_new = worker.wait_new

        def counted_wait_new(last_seq, timeout):
            calls[0] += 1
            return real_wait_new(last_seq, timeout)

        worker.wait_new = counted_wait_new
        halt = threading.Event()
        sender = camera_bridge._SenderWorker(worker, '127.0.0.1', 1, halt)
        sock = _RecordingSock()
        sender._connect = lambda: sock
        with mock.patch.dict(sys.modules, {'cv2': _stand_in_cv2()}):
            loop = threading.Thread(target=sender.run, daemon=True)
            loop.start()
            time.sleep(0.3)
            halt.set()
            worker.stop()
            loop.join(5.0)
        self.assertFalse(loop.is_alive())
        self.assertLessEqual(calls[0], 10)
        self.assertEqual(bytes(sock.sent), b'')


@unittest.skipUnless(_HAVE_CV2, 'cv2/numpy not available')
class ANon43FrameIsCroppedNotSqueezed(BoundedTestCase):
    """(c)"""

    def setUp(self):
        for name, value in (('CAMERA_WIDTH', 640), ('CAMERA_HEIGHT', 480)):
            patcher = mock.patch.object(constants, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _captured(self, frame):
        """What the capture worker stores for one camera frame."""
        worker = camera_bridge._CaptureWorker(1, 'scene', 0)
        cap = _OneFrameCap(frame, worker)
        with mock.patch.object(camera_bridge.win_camera, 'open_capture',
                               return_value=(cap, _CAP_INFO)):
            worker.run()
        (stored, _ns), seq = worker.latest()
        self.assertEqual(seq, 1)
        return stored

    def test_a_circle_stays_a_circle(self):
        for width, height in ((1280, 720), (960, 960)):
            with self.subTest(size=f'{width}x{height}'):
                frame = np.zeros((height, width, 3), np.uint8)
                cv2.circle(frame, (width // 2, height // 2), 300, (255, 255, 255), -1)
                stored = self._captured(frame)
                self.assertEqual(stored.shape, (480, 640, 3))
                ys, xs = np.nonzero(stored[:, :, 0] > 127)
                circle_w = xs.max() - xs.min() + 1
                circle_h = ys.max() - ys.min() + 1
                self.assertLessEqual(abs(circle_w / circle_h - 1.0), 0.02,
                                     f'{circle_w}x{circle_h} px')
                # Centre-cropped: the circle drawn at the centre stays there.
                self.assertLessEqual(abs((xs.max() + xs.min()) / 2 - 319.5), 2.0)
                self.assertLessEqual(abs((ys.max() + ys.min()) / 2 - 239.5), 2.0)

    def test_a_one_pixel_wide_frame_is_still_stored(self):
        # The crop height of a 1-pixel-wide frame rounds down to 0 rows; an
        # empty slice made cv2.resize raise inside run(), which ended the
        # capture thread with no German error (the squeeze before the crop
        # stored such a frame).
        for width, height in ((1, 1), (1, 2), (1, 480)):
            with self.subTest(size=f'{width}x{height}'):
                frame = np.full((height, width, 3), 200, np.uint8)
                stored = self._captured(frame)
                self.assertEqual(stored.shape, (480, 640, 3))

    def test_4_3_frames_send_the_same_bytes_as_before(self):
        """The sender's bytes for a 640x480 and a 1280x960 camera equal the
        bytes of the path before the crop existed: no resize for 640x480,
        ``cv2.resize(frame, (640, 480))`` for 1280x960, then the same JPEG."""
        params = [cv2.IMWRITE_JPEG_QUALITY, constants.CAMERA_JPEG_QUALITY]
        for width, height in ((640, 480), (1280, 960)):
            with self.subTest(size=f'{width}x{height}'):
                frame = np.random.default_rng(width).integers(
                    0, 256, (height, width, 3), dtype=np.uint8)
                before = frame if width == 640 else cv2.resize(frame, (640, 480))
                expected = cv2.imencode('.jpg', before, params)[1].tobytes()

                worker = camera_bridge._CaptureWorker(1, 'scene', 0)
                halt = threading.Event()
                sender = camera_bridge._SenderWorker(worker, '127.0.0.1', 1, halt)

                def stop_both():
                    halt.set()
                    worker.stop()

                sock = _RecordingSock(on_send=stop_both)
                sender._connect = lambda sock=sock: sock
                with mock.patch.object(camera_bridge.win_camera, 'open_capture',
                                       return_value=(_StreamingCap(frame), _CAP_INFO)):
                    loops = [threading.Thread(target=sender.run, daemon=True),
                             threading.Thread(target=worker.run, daemon=True)]
                    for loop in loops:
                        loop.start()
                    for loop in loops:
                        loop.join(10.0)
                self.assertFalse(any(loop.is_alive() for loop in loops))
                cam_id, length, _ns = struct.unpack('>BIQ', bytes(sock.sent[:13]))
                self.assertEqual((cam_id, length), (1, len(expected)))
                self.assertEqual(bytes(sock.sent[13:13 + length]), expected)


class StopJoinsEveryWorker(BoundedTestCase):
    """(d)"""

    TEST_TIMEOUT_S = 60.0

    def test_no_worker_thread_shadows_thread_stop(self):
        # The invariant behind (d), judged on every CPython: 3.13 has no
        # Thread._stop any more, so there the two stop tests below pass even
        # with the shadowing name.
        bridge = camera_bridge.CameraBridge({'gripper': 0, 'scene': 1})
        bridge.register_external_source(constants.PHONE_CAM_ID, _PhoneSlot())
        threads = bridge.workers + bridge._senders + bridge._external_senders
        self.assertEqual(len(threads), 5)
        for thread in threads:
            with self.subTest(thread=thread.name):
                self.assertNotIn('_stop', vars(thread))

    def _start_bridge(self, port):
        """Start a two-camera bridge with the phone feed against ``port`` and
        wait until both cameras deliver frames."""
        bridge = camera_bridge.CameraBridge({'gripper': 0, 'scene': 1},
                                            host='127.0.0.1', port=port)
        bridge.register_external_source(constants.PHONE_CAM_ID, _PhoneSlot())
        threads = bridge.workers + bridge._senders + bridge._external_senders
        joined = []
        for thread in threads:
            real_join = thread.join

            def join(timeout=None, _thread=thread, _real_join=real_join):
                _real_join(timeout)
                joined.append(_thread.name)

            thread.join = join
        thread_errors = []
        windll = types.SimpleNamespace(winmm=mock.Mock())
        windll.winmm.timeEndPeriod.return_value = 0
        for patcher in (
                mock.patch.dict(sys.modules, {'cv2': _stand_in_cv2()}),
                mock.patch.object(camera_bridge.win_camera, 'open_capture',
                                  side_effect=lambda *a: (_StreamingCap(_TargetSizeFrame()),
                                                          _CAP_INFO)),
                mock.patch.object(threading, 'excepthook',
                                  lambda args: thread_errors.append(args.exc_value)),
                mock.patch.object(ctypes, 'windll', windll, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge.start()

        def halt_all():  # a failed test leaves no loop running (cleanups run last-in first-out)
            bridge._stop.set()
            for worker in bridge.workers:
                worker.stop()

        self.addCleanup(halt_all)
        bridge._timer_raised = True  # start() raises the timer only on Windows
        self.assertTrue(_wait_until(
            lambda: all(w.latest()[1] > 0 for w in bridge.workers), 5.0))
        return types.SimpleNamespace(bridge=bridge, threads=threads, joined=joined,
                                     thread_errors=thread_errors, windll=windll)

    def _assert_stop_joins_every_worker(self, run):
        try:
            run.bridge.stop(join_timeout=5.0)
        except Exception as exc:  # the defect: TypeError at the first join
            self.fail(f'CameraBridge.stop() raised {exc!r}')
        self.assertEqual(sorted(run.joined), sorted(t.name for t in run.threads))
        for thread in run.threads:
            self.assertFalse(thread.is_alive(), thread.name)
        run.windll.winmm.timeEndPeriod.assert_called_once_with(1)
        self.assertFalse(run.bridge._timer_raised)
        self.assertEqual(run.thread_errors, [])

    def test_stop_joins_every_worker_while_streaming(self):
        sink = _LoopbackSink()
        self.addCleanup(sink.close)
        run = self._start_bridge(sink.port)
        # All three connections stream before the stop.
        self.assertTrue(_wait_until(
            lambda: len(sink.received()) == 3 and all(n > 0 for n in sink.received()), 5.0),
            sink.received())
        self.assertTrue(run.bridge.connected)
        self._assert_stop_joins_every_worker(run)

    def test_stop_joins_every_worker_while_the_container_is_down(self):
        run = self._start_bridge(_free_loopback_port())
        self.assertTrue(_wait_until(
            lambda: all(s.last_error_de for s in run.bridge._senders), 5.0))
        self.assertFalse(run.bridge.connected)
        self._assert_stop_joins_every_worker(run)

if __name__ == '__main__':
    unittest.main()
