"""Native camera capture + TCP streaming to the container ingest node.

Captures each assigned Windows camera in its own free-running thread (latest-
frame-wins, no buffer — phosphobot's stability trick), and a single sender
thread paces at the target fps, JPEG-encodes the newest frame from each camera,
and writes it over one multiplexed localhost TCP connection to
camera_ingest_node.py inside the open_manipulator container.

Wire protocol (must match camera_ingest_node.py):
    [ uint8 cam_id ][ uint32 BE jpeg_len ][ uint64 BE capture_unix_nanos ][ jpeg ]
cam_id is the index into constants.CAMERA_BRIDGE_ROLES, so gripper -> 0 ->
/gripper/image_raw/compressed and scene -> 1 -> /scene/image_raw/compressed
(the container's EDUBOTICS_CAMERA_NAMES must list the roles in the same order).

The bridge is a CLIENT: it connects to the container's TCP server (published as
127.0.0.1:5557 via the WSL2 NAT localhost-forwarder) and reconnects with
backoff, so a container restart or a late "Umgebung starten" both recover
without restarting the GUI.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

from . import constants, win_camera

# Header layout matches camera_ingest_node.py _HEADER exactly.
_HEADER = struct.Struct(">BIQ")
_RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 3.0, 5.0)  # capped, then repeats last
_MAX_READ_FAILURES = 10  # consecutive read() failures before reopening (phosphobot uses the same threshold)


class _CaptureWorker(threading.Thread):
    """Free-running capture loop for one camera. Keeps only the newest frame."""

    def __init__(self, cam_id: int, role: str, index: int):
        super().__init__(name=f"cam-{role}", daemon=True)
        self.cam_id = cam_id
        self.role = role
        self.index = index
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest = None          # (frame_bgr, capture_ns)
        self._latest_seq = 0         # monotonic; lets the sender detect new frames
        self.fps = 0.0
        self.last_error_de = ""
        self._frame_count = 0
        self._rate_t0 = time.monotonic()

    def latest(self):
        with self._lock:
            return self._latest, self._latest_seq

    def stop(self):
        self._stop.set()

    def run(self):
        cv2 = None
        cap = None
        failures = 0
        while not self._stop.is_set():
            if cap is None:
                try:
                    import cv2 as _cv2
                    cv2 = _cv2
                    cap = win_camera.open_capture(
                        self.index, constants.CAMERA_WIDTH,
                        constants.CAMERA_HEIGHT, constants.CAMERA_FRAMERATE)
                    self.last_error_de = ""
                    failures = 0
                except win_camera.CameraUnavailableError as exc:
                    self.last_error_de = str(exc)
                    time.sleep(2.0)
                    continue
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= _MAX_READ_FAILURES:
                    self.last_error_de = (
                        f"Kamera '{self.role}' liefert keine Bilder — wird neu "
                        f"geöffnet (Index {self.index})."
                    )
                    cap.release()
                    cap = None
                    time.sleep(0.5)
                continue
            failures = 0
            ns = time.time_ns()
            with self._lock:
                self._latest = (frame, ns)
                self._latest_seq += 1
            self._frame_count += 1
            now = time.monotonic()
            if now - self._rate_t0 >= 2.0:
                self.fps = self._frame_count / (now - self._rate_t0)
                self._frame_count = 0
                self._rate_t0 = now
        if cap is not None:
            cap.release()


class CameraBridge:
    """Owns the capture workers + the TCP sender thread."""

    def __init__(self, role_to_index: dict[str, int],
                 host: str | None = None, port: int | None = None):
        self.host = host or constants.CAMERA_INGEST_HOST
        self.port = port or constants.PORT_CAMERA_INGEST
        self.workers: list[_CaptureWorker] = []
        for role, index in role_to_index.items():
            if index is None:
                continue
            if role not in constants.CAMERA_BRIDGE_ROLES:
                raise ValueError(f"Unbekannte Kamerarolle: {role!r}")
            cam_id = constants.CAMERA_BRIDGE_ROLES.index(role)
            self.workers.append(_CaptureWorker(cam_id, role, index))
        self._stop = threading.Event()
        self._sender = threading.Thread(target=self._send_loop, name="cam-sender", daemon=True)
        self.connected = False
        self.last_error_de = ""

    def start(self):
        for w in self.workers:
            w.start()
        self._sender.start()

    def stop(self, join_timeout: float = 3.0):
        self._stop.set()
        for w in self.workers:
            w.stop()
        # `ident is None` until a thread is started — guard so stop() is safe
        # even if start() was never called (e.g. construction then teardown).
        if self._sender.ident is not None:
            self._sender.join(timeout=join_timeout)
        for w in self.workers:
            if w.ident is not None:
                w.join(timeout=join_timeout)

    def status(self) -> dict:
        """Per-camera health for the GUI status line (German error strings)."""
        return {
            "connected": self.connected,
            "error": self.last_error_de,
            "cameras": {
                w.role: {
                    "fps": round(w.fps, 1),
                    "index": w.index,
                    "error": w.last_error_de,
                }
                for w in self.workers
            },
        }

    # ----- sender -----
    def _connect(self) -> socket.socket | None:
        attempt = 0
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.connected = True
                self.last_error_de = ""
                return sock
            except OSError as exc:
                self.connected = False
                self.last_error_de = (
                    f"Warte auf Kamera-Verbindung zum Container "
                    f"({self.host}:{self.port})…"
                )
                backoff = _RECONNECT_BACKOFF_S[min(attempt, len(_RECONNECT_BACKOFF_S) - 1)]
                attempt += 1
                # Sleep in small slices so stop() is responsive.
                slept = 0.0
                while slept < backoff and not self._stop.is_set():
                    time.sleep(0.1)
                    slept += 0.1
                del exc
        return None

    def _send_loop(self):
        import cv2
        period = 1.0 / max(constants.CAMERA_FRAMERATE, 1.0)
        jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, constants.CAMERA_JPEG_QUALITY]
        last_seq = {w.cam_id: -1 for w in self.workers}
        sock = None
        while not self._stop.is_set():
            if sock is None:
                sock = self._connect()
                if sock is None:
                    break  # stopped while connecting
            tick = time.monotonic()
            try:
                for w in self.workers:
                    latest, seq = w.latest()
                    if latest is None or seq == last_seq[w.cam_id]:
                        continue  # no frame yet, or unchanged since last send
                    frame, ns = latest
                    ok, buf = cv2.imencode(".jpg", frame, jpeg_params)
                    if not ok:
                        continue
                    jpeg = buf.tobytes()
                    header = _HEADER.pack(w.cam_id, len(jpeg), ns & 0xFFFFFFFFFFFFFFFF)
                    sock.sendall(header + jpeg)
                    last_seq[w.cam_id] = seq
            except OSError as exc:
                self.connected = False
                self.last_error_de = f"Kamera-Verbindung verloren: {exc}"
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                continue
            # Pace to target fps.
            elapsed = time.monotonic() - tick
            if elapsed < period:
                time.sleep(period - elapsed)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
