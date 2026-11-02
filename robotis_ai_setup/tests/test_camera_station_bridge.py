"""The camera station's two streams in the native bridge (plan §3.2, D9).

One native 1920x1080 camera feeds:
  * the RECORDING stream: centre 1440x1080 crop -> INTER_AREA -> exactly
    640x480, under the scene cam_id, identical whether Studio is open or not;
  * the STUDIO stream: the full native frame under cam_id 200, <= 15 fps,
    only while switched on.

A fake VideoCapture stands in for MSMF and a fake ingest server reads the real
`>BIQ` wire over real TCP on 127.0.0.1, so everything between the camera
handle and the socket is the shipped code. Needs OpenCV + numpy (CI's
python-tests installs both); skipped without them.

What only the bench can prove (plan §12.2) is NOT here: MSMF's real native
mode and rate, the GUI CPU with two cameras, the gripper camera's rate beside
the station (B1).
"""

import ast
import hashlib
import os
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

try:
    import cv2
    import numpy as np
except Exception:  # noqa: BLE001 — the suite must import without them
    cv2 = None
    np = None

from gui.app import camera_bridge, constants, win_camera

_HEADER = struct.Struct(">BIQ")
_SCENE_ID = constants.CAMERA_BRIDGE_ROLES.index("scene")
_GUI_APP = os.path.join(os.path.dirname(__file__), "..", "gui", "app")


def _native_frame(index):
    """A 1920x1080 BGR frame that encodes ``index`` and its crop edges.

    The 240-px side bands (outside the 4:3 crop) are pure red; the centre is a
    horizontal gradient whose level depends on ``index``. A stretch of the
    full frame would carry red into the recording frame; the crop never
    does."""
    f = np.zeros((1080, 1920, 3), np.uint8)
    f[:, :240] = (0, 0, 255)
    f[:, 1680:] = (0, 0, 255)
    level = (index * 7) % 200 + 20
    ramp = np.linspace(level, level + 30, 1440).astype(np.uint8)
    f[:, 240:1680, 0] = ramp
    f[:, 240:1680, 1] = level
    f[:, 240:1680, 2] = 0
    return f


class _FakeCap:
    """Stands in for an MSMF VideoCapture: delivers frames at ``fps``."""

    def __init__(self, size=(1920, 1080), fps=60.0, limit=None):
        self.size = size
        self.period = 1.0 / fps
        self.limit = limit
        self.reads = 0
        self.released = 0
        self._next = time.monotonic()

    def read(self):
        if self.limit is not None and self.reads >= self.limit:
            time.sleep(0.01)
            return False, None
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next = max(self._next + self.period, time.monotonic())
        idx = self.reads
        self.reads += 1
        if self.size == (1920, 1080):
            return True, _native_frame(idx)
        w, h = self.size
        return True, np.full((h, w, 3), (idx * 5) % 256, np.uint8)

    def release(self):
        self.released += 1


class _FakeIngest:
    """A local stand-in for camera_ingest_node.py's TCP server."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.port = self.srv.getsockname()[1]
        self.frames = []          # dicts
        self.opened = 0
        self.closed = 0
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            with self.lock:
                self.opened += 1
                conn_id = self.opened
            threading.Thread(target=self._serve, args=(conn, conn_id),
                             daemon=True).start()

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _serve(self, conn, conn_id):
        conn.settimeout(5.0)
        try:
            while not self._stop.is_set():
                head = self._recv_exact(conn, _HEADER.size)
                if head is None:
                    break
                cam_id, n, ns = _HEADER.unpack(head)
                payload = self._recv_exact(conn, n)
                if payload is None:
                    break
                img = cv2.imdecode(np.frombuffer(payload, np.uint8),
                                   cv2.IMREAD_COLOR)
                with self.lock:
                    self.frames.append({
                        "conn": conn_id, "cam": cam_id, "ns": ns,
                        "shape": img.shape, "img": img, "t": time.monotonic()})
        except OSError:
            pass
        finally:
            conn.close()
            with self.lock:
                self.closed += 1

    def by_cam(self, cam):
        with self.lock:
            return [f for f in self.frames if f["cam"] == cam]

    def close(self):
        self._stop.set()
        self.srv.close()


def _wait_until(pred, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _fn_hash(path, qual):
    """sha256 of the function body's SOURCE LINES (comments included), so the
    pin is literally byte-for-byte and independent of the Python version
    (ast.dump differs between versions). For _CaptureWorker.run the station
    dispatch, the first statement, is excluded."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    tree = ast.parse(text)
    node = tree
    for name in qual.split("."):
        node = next(n for n in node.body
                    if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                    and n.name == name)
    body = node.body
    if (qual.endswith("_CaptureWorker.run") and isinstance(body[0], ast.If)
            and ast.unparse(body[0].test) == "self.station"):
        body = body[1:]
    lines = text.splitlines()
    seg = "\n".join(lines[body[0].lineno - 1: node.end_lineno])
    return hashlib.sha256(seg.encode("utf-8")).hexdigest()


class TodaysPathIsPinnedByteForByte(unittest.TestCase):
    """Non-station rigs keep today's capture path exactly (plan §3.2).

    The hashes are of the source lines of today's bodies (camera_bridge's
    _CaptureWorker.run after the `if self.station` dispatch, _SenderWorker.run,
    win_camera.open_capture), taken from main with only the `_stop` ->
    `_stop_event` rename applied. Regenerate them ONLY with a reviewed change
    to today's non-station path."""

    PINS = {
        ("camera_bridge.py", "_CaptureWorker.run"):
            "fbf46e9814c07bd1a7e4d149a58a83d0011930225d2e66322d202dea6724ca69",
        ("camera_bridge.py", "_SenderWorker.run"):
            "119d38922496e109b74b7deeb57540b7c7e2e3fa0baa9a31bf23ead880385e24",
        ("win_camera.py", "open_capture"):
            "e1c6989324b9d2f03315a760009a061648dd0006cd2444b6ee8940f62fcdcabb",
    }

    def test_todays_bodies_are_unchanged(self):
        for (fname, qual), want in self.PINS.items():
            with self.subTest(qual):
                self.assertEqual(_fn_hash(os.path.join(_GUI_APP, fname), qual),
                                 want)

    def test_the_station_dispatch_is_the_first_statement_and_returns(self):
        with open(camera_bridge.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "_CaptureWorker")
        run = next(n for n in cls.body
                   if isinstance(n, ast.FunctionDef) and n.name == "run")
        first = run.body[0]
        self.assertIsInstance(first, ast.If)
        self.assertEqual(ast.unparse(first.test), "self.station")
        self.assertEqual([ast.unparse(s) for s in first.body],
                         ["self._run_station()", "return"])
        self.assertEqual(first.orelse, [])

    def test_a_non_station_bridge_has_no_station_sender(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0, "scene": 1})
        self.assertFalse(bridge.has_station)
        self.assertIsNone(bridge._station_sender)
        self.assertFalse(any(w.station for w in bridge.workers))
        self.assertFalse(bridge.set_station_stream(True))
        self.assertEqual(len(bridge._threads_to_join()), 4)

    def test_a_station_bridge_without_a_scene_camera_has_none_either(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0}, station=True)
        self.assertFalse(bridge.has_station)
        self.assertFalse(bridge.workers[0].station)

    def test_only_the_scene_worker_is_the_station_worker(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0, "scene": 1},
                                            station=True)
        by_role = {w.role: w for w in bridge.workers}
        self.assertTrue(by_role["scene"].station)
        self.assertFalse(by_role["gripper"].station)
        self.assertIs(bridge._station_sender.worker, by_role["scene"])
        self.assertIs(bridge._station_sender._stop_event, bridge._stop)
        self.assertEqual(by_role["scene"].cam_id, _SCENE_ID)

    def test_the_wire_constants(self):
        self.assertEqual(camera_bridge.STATION_SCENE_CAM_ID, 200)
        self.assertEqual(camera_bridge.STATION_SEND_FPS, 15)
        self.assertEqual(camera_bridge.STATION_JPEG_QUALITY, 90)
        self.assertEqual((camera_bridge.STATION_CROP_X0,
                          camera_bridge.STATION_CROP_X1), (240, 1680))
        self.assertEqual((camera_bridge.STATION_RECORD_WIDTH,
                          camera_bridge.STATION_RECORD_HEIGHT), (640, 480))
        # 200 fits the u8 wire field and is no enumerated camera index.
        self.assertLess(camera_bridge.STATION_SCENE_CAM_ID, 256)
        self.assertNotIn(camera_bridge.STATION_SCENE_CAM_ID,
                         range(len(constants.CAMERA_BRIDGE_ROLES) + 1))


@unittest.skipIf(cv2 is None, "OpenCV + numpy required")
class TheRecordingFrame(unittest.TestCase):

    def test_centre_crop_then_inter_area_to_exactly_640x480(self):
        src = _native_frame(3)
        before = src.copy()
        out = camera_bridge.station_record_frame(cv2, src)
        self.assertEqual(out.shape, (480, 640, 3))
        want = cv2.resize(src[:, 240:1680], (640, 480),
                          interpolation=cv2.INTER_AREA)
        self.assertTrue(np.array_equal(out, want))
        self.assertTrue(np.array_equal(src, before), "the source was modified")
        # No red band: a stretch of the full frame would carry it in.
        self.assertEqual(int(out[:, :, 2].max()), 0)

    def test_the_overrides_of_the_manual_size_are_ignored(self):
        src = _native_frame(1)
        with patch.object(constants, "CAMERA_WIDTH", 1280), \
                patch.object(constants, "CAMERA_HEIGHT", 720):
            out = camera_bridge.station_record_frame(cv2, src)
        self.assertEqual(out.shape, (480, 640, 3))


@unittest.skipIf(cv2 is None, "OpenCV + numpy required")
class TheStationWorkerInMemory(unittest.TestCase):
    """The worker's two frame stores, before any socket."""

    def _run_worker(self, cap, seconds, native_on=False, opens=None):
        def _open(index):
            if opens is not None:
                opens.append(index)
            return cap, {"fps": 30.0, "fourcc": "", "msmf": True}
        worker = camera_bridge._CaptureWorker(_SCENE_ID, "scene", 4,
                                              station=True)
        worker.set_native(native_on)
        records, natives = [], []
        with patch.object(win_camera, "open_station_capture", side_effect=_open):
            worker.start()
            end = time.monotonic() + seconds
            last = -1
            while time.monotonic() < end:
                latest, seq = worker.wait_new(last, 0.2)
                if latest is not None and seq != last:
                    records.append((seq, latest))
                    natives.append(worker.latest_native())
                    last = seq
            worker.stop()
            worker.join(2.0)
        return worker, records, natives

    def test_recording_frames_are_the_crop_of_their_source(self):
        cap = _FakeCap(fps=60.0)
        worker, records, natives = self._run_worker(cap, 0.6, native_on=True)
        self.assertGreater(len(records), 10)
        for (_seq, (frame, ns)), (nat, _nseq) in zip(records, natives):
            self.assertEqual(frame.shape, (480, 640, 3))
            if nat is not None and nat[1] == ns:
                want = cv2.resize(nat[0][:, 240:1680], (640, 480),
                                  interpolation=cv2.INTER_AREA)
                self.assertTrue(np.array_equal(frame, want))
                self.assertEqual(nat[0].shape, (1080, 1920, 3))
        paired = [1 for (_s, (_f, ns)), (nat, _n) in zip(records, natives)
                  if nat is not None and nat[1] == ns]
        self.assertGreater(len(paired), 5, "the same capture_ns on both")
        self.assertEqual(cap.released, 1)

    def test_no_native_frame_is_kept_while_the_studio_stream_is_off(self):
        cap = _FakeCap(fps=60.0)
        worker, records, natives = self._run_worker(cap, 0.4, native_on=False)
        self.assertGreater(len(records), 5)
        self.assertTrue(all(n == (None, 0) for n in natives))

    def test_a_wrong_size_reopens_once_then_delivers_nothing(self):
        cap = _FakeCap(size=(1280, 720), fps=60.0)
        opens = []
        worker, records, natives = self._run_worker(cap, 0.5, native_on=True,
                                                    opens=opens)
        self.assertEqual(len(opens), 2, "reopened exactly once")
        self.assertEqual(records, [])
        self.assertIsNone(worker.latest()[0])
        self.assertIsNone(worker.latest_native()[0])
        self.assertIn("1280×720", worker.last_error_de)
        self.assertIn("1920×1080", worker.last_error_de)
        self.assertFalse(worker.is_alive())

    def test_a_640x480_camera_is_not_stretched_either(self):
        cap = _FakeCap(size=(640, 480), fps=60.0)
        opens = []
        worker, records, _n = self._run_worker(cap, 0.4, opens=opens)
        self.assertEqual(len(opens), 2)
        self.assertEqual(records, [])
        self.assertIn("640×480", worker.last_error_de)


@unittest.skipIf(cv2 is None, "OpenCV + numpy required")
class TodaysWorkerInMemory(unittest.TestCase):
    """A non-station worker keeps today's behaviour on real frames."""

    def _frames(self, size, seconds=0.4):
        w, h = size
        cap = _FakeCap(size=size, fps=60.0)
        opened = []

        def _open(index, width, height, fps):
            opened.append((index, width, height, fps))
            return cap, {"fps": 30.0, "fourcc": "", "msmf": True}
        worker = camera_bridge._CaptureWorker(_SCENE_ID, "scene", 2)
        got = []
        with patch.object(win_camera, "open_capture", side_effect=_open), \
                patch.object(win_camera, "open_station_capture",
                             side_effect=AssertionError("station open")):
            worker.start()
            end = time.monotonic() + seconds
            last = -1
            while time.monotonic() < end:
                latest, seq = worker.wait_new(last, 0.2)
                if latest is not None and seq != last:
                    got.append(latest[0])
                    last = seq
            worker.stop()
            worker.join(2.0)
        self.assertEqual(opened[0], (2, constants.CAMERA_WIDTH,
                                     constants.CAMERA_HEIGHT,
                                     constants.CAMERA_FRAMERATE))
        return got

    def test_a_640x480_frame_passes_untouched(self):
        got = self._frames((640, 480))
        self.assertGreater(len(got), 5)
        for f in got:
            self.assertEqual(f.shape, (480, 640, 3))

    def test_another_size_gets_todays_default_resize(self):
        got = self._frames((1280, 720))
        self.assertGreater(len(got), 5)
        for f in got:
            self.assertEqual(f.shape, (480, 640, 3))
        level = int(got[0][0, 0, 0])
        src = np.full((720, 1280, 3), level, np.uint8)
        self.assertTrue(np.array_equal(
            got[0], cv2.resize(src, (640, 480))))


@unittest.skipIf(cv2 is None, "OpenCV + numpy required")
class TheTwoStreamsOverTheWire(unittest.TestCase):
    """The shipped bridge, threads and sockets, against a fake ingest server."""

    def setUp(self):
        self.ingest = _FakeIngest()
        self.cap = _FakeCap(fps=60.0)
        self.opens = []

        def _open(index):
            self.opens.append(index)
            return self.cap, {"fps": 30.0, "fourcc": "", "msmf": True}
        self._patch = patch.object(win_camera, "open_station_capture",
                                   side_effect=_open)
        self._patch.start()
        self.bridge = camera_bridge.CameraBridge(
            {"scene": 4}, host="127.0.0.1", port=self.ingest.port,
            station=True)
        self.bridge.start()

    def tearDown(self):
        self.bridge.stop()
        self._patch.stop()
        self.ingest.close()

    def test_recording_only_640x480_under_the_scene_id_station_only_native(self):
        self.assertTrue(_wait_until(lambda: len(self.ingest.by_cam(_SCENE_ID)) > 5))
        self.assertEqual(self.ingest.by_cam(200), [], "studio stream before on")
        self.assertTrue(self.bridge.set_station_stream(True))
        self.assertTrue(_wait_until(lambda: len(self.ingest.by_cam(200)) >= 8))
        self.bridge.set_station_stream(False)
        time.sleep(0.3)
        rec = self.ingest.by_cam(_SCENE_ID)
        stu = self.ingest.by_cam(200)
        self.assertTrue(all(f["shape"] == (480, 640, 3) for f in rec))
        self.assertTrue(all(f["shape"] == (1080, 1920, 3) for f in stu))
        self.assertEqual({f["cam"] for f in self.ingest.frames},
                         {_SCENE_ID, 200})
        # Every studio frame carries the capture_ns of a recording frame.
        rec_ns = {f["ns"] for f in rec}
        self.assertTrue(all(f["ns"] in rec_ns for f in stu))
        # The recording frame equals crop + INTER_AREA of its source (JPEG
        # tolerance): pair one studio frame with its recording twin.
        twin = next(f for f in rec if f["ns"] == stu[-1]["ns"])
        want = cv2.resize(stu[-1]["img"][:, 240:1680], (640, 480),
                          interpolation=cv2.INTER_AREA)
        diff = np.abs(twin["img"].astype(int) - want.astype(int)).mean()
        self.assertLess(diff, 4.0)
        self.assertLess(int(twin["img"][:, :, 2].max()), 60, "red band leaked")

    def test_the_studio_stream_is_capped_at_15_fps(self):
        self.bridge.set_station_stream(True)
        self.assertTrue(_wait_until(lambda: len(self.ingest.by_cam(200)) >= 2))
        t0 = time.monotonic()
        n0 = len(self.ingest.by_cam(200))
        time.sleep(1.5)
        n1 = len(self.ingest.by_cam(200))
        rate = (n1 - n0) / (time.monotonic() - t0)
        self.assertLessEqual(rate, 15.0 + 1.0)
        self.assertGreater(rate, 8.0, "the studio stream barely runs")
        times = [f["t"] for f in self.ingest.by_cam(200)[-10:]]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreater(min(gaps), (1 / 15.0) * 0.6)

    def test_the_station_connection_exists_only_while_the_stream_is_on(self):
        self.assertTrue(_wait_until(lambda: self.ingest.opened == 1))
        time.sleep(0.3)
        self.assertEqual(self.ingest.opened, 1, "an idle station connection")
        self.bridge.set_station_stream(True)
        self.assertTrue(_wait_until(lambda: self.ingest.opened == 2))
        self.assertTrue(_wait_until(lambda: len(self.ingest.by_cam(200)) >= 2))
        self.bridge.set_station_stream(False)
        self.assertTrue(_wait_until(lambda: self.ingest.closed == 1, 2.0),
                        "the station connection stayed open after off")
        n = len(self.ingest.by_cam(200))
        time.sleep(0.4)
        self.assertEqual(len(self.ingest.by_cam(200)), n, "sent after off")
        st = self.bridge.station_status()
        self.assertEqual(st["stream"], "off")
        self.assertEqual(st["station_fps"], 0.0)

    def test_200_switches_never_touch_the_camera_or_the_recording_stream(self):
        self.assertTrue(_wait_until(lambda: len(self.ingest.by_cam(_SCENE_ID)) > 3))
        start_reads = self.cap.reads
        t0 = time.monotonic()
        for i in range(200):
            self.bridge.set_station_stream(i % 2 == 0)
            time.sleep(0.004)
        self.bridge.set_station_stream(False)
        time.sleep(0.3)
        elapsed = time.monotonic() - t0
        self.assertEqual(self.opens, [4], "the camera was reopened")
        self.assertEqual(self.cap.released, 0, "the camera was released")
        reads = self.cap.reads - start_reads
        rec = [f for f in self.ingest.by_cam(_SCENE_ID) if f["t"] >= t0]
        # The recording stream forwarded (essentially) every captured frame
        # during the toggling, at the camera's own pace.
        self.assertGreaterEqual(len(rec), reads - 3)
        gaps = [b["t"] - a["t"] for a, b in zip(rec, rec[1:])]
        self.assertLess(max(gaps), 0.25,
                        "a recording frame was held back by the switching")
        self.assertGreater(len(rec), 0.8 * elapsed * 60 * 0.5)



@unittest.skipIf(cv2 is None, "needs OpenCV + numpy")
class TheStudioStreamKeepsItsRate(unittest.TestCase):
    """Fix round 1, F4: the Studio stream is paced on a deadline fixed at PICK
    time, so the encode and the send never add to its period. With a 30 fps
    camera and up to +20 ms of extra encode time it stays at >= 14 fps (the
    server's health line is 10 fps) and never above 15."""

    def _rate(self, extra_s):
        ingest = _FakeIngest()
        cap = _FakeCap(fps=30.0)
        real = cv2.imencode

        def slow_imencode(ext, img, *a, **k):
            if extra_s and getattr(img, "shape", (0, 0))[:2] == (1080, 1920):
                time.sleep(extra_s)
            return real(ext, img, *a, **k)

        with patch.object(win_camera, "open_station_capture",
                          side_effect=lambda index: (cap, {"fps": 30.0})), \
                patch.object(cv2, "imencode", side_effect=slow_imencode):
            bridge = camera_bridge.CameraBridge(
                {"scene": 4}, host="127.0.0.1", port=ingest.port, station=True)
            bridge.start()
            try:
                self.assertTrue(bridge.set_station_stream(True))
                self.assertTrue(_wait_until(lambda: len(ingest.by_cam(200)) >= 3))
                time.sleep(0.5)
                t0, n0 = time.monotonic(), len(ingest.by_cam(200))
                time.sleep(3.0)
                t1, n1 = time.monotonic(), len(ingest.by_cam(200))
            finally:
                bridge.stop()
                ingest.close()
        return (n1 - n0) / (t1 - t0), n1 - n0, t1 - t0

    def _check(self, extra_s):
        rate, n, dur = self._rate(extra_s)
        self.assertGreaterEqual(rate, 14.0, (extra_s, rate))
        # Never above 15 fps: at most one frame per 1/15 s slot of the window.
        self.assertLessEqual(n, 15.0 * dur + 1, (extra_s, rate))

    def test_no_extra_encode_cost(self):
        self._check(0.0)

    def test_10_ms_extra_encode_cost(self):
        self._check(0.010)

    def test_20_ms_extra_encode_cost(self):
        self._check(0.020)


@unittest.skipIf(cv2 is None, "needs OpenCV + numpy")
class TheStudioSenderNeverSpins(unittest.TestCase):
    """Fix round 1, F9: with the stream on and no native frame at all (here the
    camera stopped delivering before the stream was switched on, so its reads
    fail), the station sender blocks on the worker's condition instead of
    looping: a handful of waits per second and almost no CPU."""

    def test_no_native_frame_means_no_busy_loop(self):
        ingest = _FakeIngest()
        cap = _FakeCap(fps=30.0, limit=10)
        with patch.object(win_camera, "open_station_capture",
                          side_effect=lambda index: (cap, {"fps": 30.0})):
            bridge = camera_bridge.CameraBridge(
                {"scene": 4}, host="127.0.0.1", port=ingest.port, station=True)
            worker = bridge.workers[0]
            calls = [0]
            real_wait = worker.wait_new_native

            def counted(last_seq, timeout):
                calls[0] += 1
                return real_wait(last_seq, timeout)
            worker.wait_new_native = counted
            bridge.start()
            try:
                # The recording stream got its frames, then the camera stopped.
                self.assertTrue(_wait_until(
                    lambda: len(ingest.by_cam(_SCENE_ID)) >= 10, 5.0))
                self.assertTrue(bridge.set_station_stream(True))
                self.assertTrue(_wait_until(lambda: bridge._station_sender.connected))
                time.sleep(0.2)
                c0, cpu0, t0 = calls[0], time.process_time(), time.monotonic()
                time.sleep(1.5)
                c1, cpu1, t1 = calls[0], time.process_time(), time.monotonic()
            finally:
                bridge.stop()
                ingest.close()
        self.assertLessEqual(c1 - c0, 10, "the station sender loops without blocking")
        self.assertLess((cpu1 - cpu0) / (t1 - t0), 0.25, "the process burns CPU")
        self.assertEqual(ingest.by_cam(200), [])


if __name__ == "__main__":
    unittest.main()
