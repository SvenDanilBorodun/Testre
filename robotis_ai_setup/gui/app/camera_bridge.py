"""Native camera capture + TCP streaming to the container ingest node.

Each assigned Windows camera gets TWO threads and its OWN TCP connection:

  * a free-running *capture* thread (latest-frame-wins, no buffer —
    phosphobot's stability trick), and
  * a *sender* thread that paces to the target fps on an absolute deadline,
    JPEG-encodes the newest frame, and writes it over that camera's own
    localhost TCP connection to camera_ingest_node.py.

One connection PER camera (was: a single multiplexed connection for both)
decouples the cameras — a stall or reconnect on one camera can no longer delay
the other, and removes a structural ≤1-frame-per-tick ceiling that capped the
shared sender at the tick rate. The wire frame still carries `cam_id`, so the
ingest node demuxes per-frame regardless of which connection it arrived on
(and a single multiplexed client still works for backward compatibility).

Wire protocol (must match camera_ingest_node.py):
    [ uint8 cam_id ][ uint32 BE jpeg_len ][ uint64 BE capture_unix_nanos ][ jpeg ]
cam_id is the index into constants.CAMERA_BRIDGE_ROLES, so gripper -> 0 ->
/gripper/image_raw/compressed and scene -> 1 -> /scene/image_raw/compressed
(the container's EDUBOTICS_CAMERA_NAMES must list the roles in the same order).

Each sender is a CLIENT: it connects to the container's TCP server (published
as 127.0.0.1:5557 via the WSL2 NAT localhost-forwarder) and reconnects with
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
# ONE deadline for joining every thread in CameraBridge.stop(). It runs on the
# Tk thread when the GUI closes, so closing blocks at most this long however
# many threads the bridge owns.
STOP_DEADLINE_S = 2.0

# ── OMX camera station (native 1920x1080, one open, two streams) ──────────
# The station camera's own id on the >BIQ wire, for the Studio stream only.
# 200 is outside every enumerated camera index (gripper 0, scene 1, phone 2),
# and camera_ingest_node.py spells the same literal. The RECORDING stream of
# the station camera keeps the scene cam_id.
STATION_SCENE_CAM_ID = 200
# The Studio stream sends at most this many native frames per second; the
# newest frame wins.
STATION_SEND_FPS = 15
STATION_JPEG_QUALITY = 90
# Recording frame on a station rig: the centre 4:3 crop of the native 16:9
# frame, area-downscaled to exactly 640x480 (the defaults of CAMERA_WIDTH /
# CAMERA_HEIGHT; the station path deliberately ignores their
# EDUBOTICS_CAMERA_WIDTH/_HEIGHT overrides). Never a stretch.
STATION_CROP_X0 = 240
STATION_CROP_X1 = 1680
STATION_RECORD_WIDTH = 640
STATION_RECORD_HEIGHT = 480


def station_record_frame(cv2, frame):
    """The recording frame of one native 1920x1080 station frame.

    ``frame[:, 240:1680]`` (the centre 1440x1080, 4:3) resized with
    INTER_AREA to exactly 640x480. The input is not modified."""
    crop = frame[:, STATION_CROP_X0:STATION_CROP_X1]
    return cv2.resize(crop, (STATION_RECORD_WIDTH, STATION_RECORD_HEIGHT),
                      interpolation=cv2.INTER_AREA)


# The Thread subclasses below keep their stop flag in `_stop_event`, never
# `_stop`: on CPython 3.11/3.12 (the installer ships 3.11) `Thread.join()`
# calls the private method `self._stop()`, so an instance attribute of that
# name shadows it and `join()` raises `TypeError: 'Event' object is not
# callable` — which aborted CameraBridge.stop() at the first finished thread
# and skipped the camera joins and timeEndPeriod.


class _CaptureWorker(threading.Thread):
    """Free-running capture loop for one camera. Keeps only the newest frame."""

    def __init__(self, cam_id: int, role: str, index: int,
                 station: bool = False):
        super().__init__(name=f"cam-{role}", daemon=True)
        self.cam_id = cam_id
        self.role = role
        self.index = index
        # True only for the scene camera of a camera-station rig: it is then
        # opened natively (win_camera.open_station_capture) and every frame
        # feeds TWO streams (see _run_station). False keeps today's path.
        self.station = station
        self._stop_event = threading.Event()
        self._cond = threading.Condition()
        self._latest = None          # (frame_bgr, capture_ns)
        self._latest_seq = 0         # monotonic; lets the sender detect new frames
        # Station only: the full native frame, kept ONLY while the Studio
        # stream is on (set_native), with its own sequence counter.
        self._native_on = False
        self._latest_native = None   # (frame_bgr 1920x1080, capture_ns)
        self._latest_native_seq = 0
        self.fps = 0.0
        self.last_error_de = ""
        # Negotiated (read-back) capture format — set on open. A camera that
        # silently runs at 15 fps when 30 was requested shows up here; the GUI
        # surfaces `format_warn_de` so a half-rate feed isn't invisible.
        self.negotiated_fps = 0.0
        self.negotiated_fourcc = ""
        self.format_warn_de = ""
        self._frame_count = 0
        self._rate_t0 = time.monotonic()

    def latest(self):
        with self._cond:
            return self._latest, self._latest_seq

    def wait_new(self, last_seq, timeout):
        """Block until a frame newer than last_seq is stored — woken
        immediately by the capture loop's notify (NOT subject to the Windows
        ~15.6ms timer granularity that a polled Event.wait suffers), or until
        timeout (for stop responsiveness). Returns (latest, seq)."""
        with self._cond:
            if self._latest_seq == last_seq:
                self._cond.wait(timeout)
            return self._latest, self._latest_seq

    def stop(self):
        self._stop_event.set()
        # Wake any sender blocked in wait_new so stop() is prompt.
        with self._cond:
            self._cond.notify_all()

    # ── station: the native frame for the Studio stream ──────────────
    def set_native(self, on: bool) -> None:
        """Keep (on) or stop keeping (off) the full native frame. Touches no
        camera handle and nothing the recording sender reads."""
        with self._cond:
            self._native_on = bool(on)
            if not on:
                self._latest_native = None
            self._cond.notify_all()

    def latest_native(self):
        with self._cond:
            return self._latest_native, self._latest_native_seq

    def wait_new_native(self, last_seq, timeout):
        """Like wait_new, for the native frame. Returns (latest_native, seq);
        latest_native is None while the Studio stream is off."""
        with self._cond:
            if self._latest_native_seq == last_seq:
                self._cond.wait(timeout)
            return self._latest_native, self._latest_native_seq

    def run(self):
        if self.station:
            self._run_station()
            return
        cv2 = None
        cap = None
        failures = 0
        while not self._stop_event.is_set():
            if cap is None:
                try:
                    import cv2 as _cv2
                    cv2 = _cv2
                    cap, info = win_camera.open_capture(
                        self.index, constants.CAMERA_WIDTH,
                        constants.CAMERA_HEIGHT, constants.CAMERA_FRAMERATE)
                    self.negotiated_fps = info.get("fps", 0.0)
                    self.negotiated_fourcc = info.get("fourcc", "")
                    # The camera always *claims* 30 fps via CAP_PROP_FPS even
                    # when it can't deliver it, so the negotiated fps is not a
                    # reliable signal — the real rate is `self.fps` (measured
                    # below). The one reliable warn is a reported non-MJPG
                    # fourcc (DSHOW's YUY2 fallback → ~14 fps). MSMF returns a
                    # garbage fourcc read-back, so skip the warn there entirely.
                    if (not info.get("msmf")
                            and self.negotiated_fourcc
                            and self.negotiated_fourcc != "MJPG"):
                        self.format_warn_de = (
                            f"Kamera '{self.role}': Treiber nutzt "
                            f"{self.negotiated_fourcc} statt MJPG — niedrige "
                            f"Bildrate möglich (EDUBOTICS_CAMERA_BACKEND prüfen)."
                        )
                    else:
                        self.format_warn_de = ""
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
            # Normalise resolution to the target. On MSMF we deliberately do NOT
            # request a resolution at open (it would force a ~12s re-negotiation
            # and a slower mode — see win_camera.open_capture), so the camera
            # runs its native size; resize only if it differs from the target.
            # The Innomaker's native MSMF size is already 640x480 (no-op here);
            # this keeps the bridge correct on cameras whose native size differs.
            if (frame.shape[1] != constants.CAMERA_WIDTH
                    or frame.shape[0] != constants.CAMERA_HEIGHT):
                frame = cv2.resize(frame, (constants.CAMERA_WIDTH, constants.CAMERA_HEIGHT))
            ns = time.time_ns()
            with self._cond:
                self._latest = (frame, ns)
                self._latest_seq += 1
                self._cond.notify_all()
            self._frame_count += 1
            now = time.monotonic()
            if now - self._rate_t0 >= 2.0:
                self.fps = self._frame_count / (now - self._rate_t0)
                self._frame_count = 0
                self._rate_t0 = now
        if cap is not None:
            cap.release()

    def _run_station(self):
        """Station capture: the camera is opened ONCE natively and never
        reopened for Roboter Studio (owner decision D9).

        Every frame must be exactly 1920x1080. A wrong size reopens the camera
        once; if it is still wrong, the worker reports a German error and
        delivers nothing at all (never a stretch, never today's resize).

        Each frame then feeds two streams, both stamped with the same
        capture_ns:
          * the recording frame (station_record_frame: centre crop, INTER_AREA,
            exactly 640x480) into ``_latest``, which _SenderWorker forwards
            under the scene cam_id exactly as today — identical whether the
            Studio stream is on or not;
          * while the Studio stream is on, the full frame into
            ``_latest_native`` for _StationSenderWorker (cam_id 200).
        """
        want_w = constants.STATION_NATIVE_WIDTH
        want_h = constants.STATION_NATIVE_HEIGHT
        cv2 = None
        cap = None
        failures = 0
        size_reopened = False
        while not self._stop_event.is_set():
            if cap is None:
                try:
                    import cv2 as _cv2
                    cv2 = _cv2
                    cap, info = win_camera.open_station_capture(self.index)
                    self.negotiated_fps = info.get("fps", 0.0)
                    self.negotiated_fourcc = info.get("fourcc", "")
                    self.format_warn_de = ""
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
                        f"Stationskamera liefert keine Bilder — wird neu "
                        f"geöffnet (Index {self.index})."
                    )
                    cap.release()
                    cap = None
                    time.sleep(0.5)
                continue
            failures = 0
            h, w = frame.shape[:2]
            if (w, h) != (want_w, want_h):
                cap.release()
                cap = None
                if not size_reopened:
                    size_reopened = True
                    continue
                self.last_error_de = (
                    f"Stationskamera liefert {w}×{h} statt {want_w}×{want_h} "
                    f"Bildpunkten — es werden keine Bilder gesendet (Index "
                    f"{self.index})."
                )
                return
            ns = time.time_ns()
            record = station_record_frame(cv2, frame)
            with self._cond:
                self._latest = (record, ns)
                self._latest_seq += 1
                if self._native_on:
                    self._latest_native = (frame, ns)
                    self._latest_native_seq += 1
                self._cond.notify_all()
            self._frame_count += 1
            now = time.monotonic()
            if now - self._rate_t0 >= 2.0:
                self.fps = self._frame_count / (now - self._rate_t0)
                self._frame_count = 0
                self._rate_t0 = now
        if cap is not None:
            cap.release()


class _ConnectMixin:
    """Shared connect-with-backoff for the camera senders.

    Both the per-camera sender and the phone external sender are CLIENTS to the
    container's TCP server and reconnect identically; only the frame source
    differs. Subclasses set ``self.host`` / ``self.port`` / ``self._stop_event`` /
    ``self.connected`` / ``self.last_error_de`` and call ``self._connect()``.
    """

    def _connect_cancelled(self) -> bool:
        """Whether to give up connecting. The station sender also gives up
        once the Studio stream is off."""
        return self._stop_event.is_set()

    def _connect(self) -> "socket.socket | None":
        attempt = 0
        while not self._connect_cancelled():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.connected = True
                self.last_error_de = ""
                return sock
            except OSError:
                self.connected = False
                self.last_error_de = (
                    f"Warte auf Kamera-Verbindung zum Container "
                    f"({self.host}:{self.port})…"
                )
                backoff = _RECONNECT_BACKOFF_S[min(attempt, len(_RECONNECT_BACKOFF_S) - 1)]
                attempt += 1
                # Wait responsively so stop() returns promptly.
                if self._stop_event.wait(backoff):
                    break
        return None


class _SenderWorker(_ConnectMixin, threading.Thread):
    """Owns ONE camera's TCP connection. Event-driven: blocks on the capture
    worker's Condition and forwards each newly-captured frame as it arrives (no
    rate cap — the real capture rate is the limiter). Reconnects with backoff."""

    def __init__(self, worker: _CaptureWorker, host: str, port: int,
                 stop_event: threading.Event):
        super().__init__(name=f"cam-send-{worker.role}", daemon=True)
        self.worker = worker
        self.host = host
        self.port = port
        self._stop_event = stop_event
        self.connected = False
        self.last_error_de = ""

    def run(self):
        import cv2
        jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, constants.CAMERA_JPEG_QUALITY]
        last_seq = -1
        sock = None
        while not self._stop_event.is_set():
            if sock is None:
                sock = self._connect()
                if sock is None:
                    break  # stopped while connecting
            # Event-driven: block until the capture worker stores a NEW frame
            # (woken by its notify — immediate, NOT the ~15.6ms Windows timer
            # tick that throttled the previous polled pacer to ~20fps). The
            # 0.5s timeout is only a stop-responsiveness fallback.
            #
            # We forward EVERY captured frame — NO rate cap. The capture worker
            # is latest-frame-wins at the camera's true ~30fps, so the sender is
            # a 1:1 forwarder bounded by the real capture rate. A per-frame rate
            # cap here previously clipped delivery to ~28fps (its post-send
            # timestamp accumulated the encode time into each period) which made
            # the recorder — a fixed-rate timer that grabs the latest cached
            # frame — occasionally re-grab the same frame (~6.7% duplicate frames
            # at 28-vs-30). Delivering at full capture rate keeps a fresh frame
            # always available; sub-rate recording is the recorder's job (it
            # subsamples to task_info.fps), not the bridge's.
            latest, seq = self.worker.wait_new(last_seq, 0.5)
            if latest is None or seq == last_seq:
                continue  # timeout with no new frame — re-check stop and wait
            frame, ns = latest
            ok, buf = cv2.imencode(".jpg", frame, jpeg_params)
            if not ok:
                continue
            jpeg = buf.tobytes()
            header = _HEADER.pack(self.worker.cam_id, len(jpeg),
                                  ns & 0xFFFFFFFFFFFFFFFF)
            try:
                sock.sendall(header + jpeg)
                last_seq = seq
            except OSError as exc:
                self.connected = False
                self.last_error_de = f"Kamera-Verbindung verloren: {exc}"
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                continue
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class _ExternalSenderWorker(_ConnectMixin, threading.Thread):
    """Forwards an EXTERNAL JPEG source (the phone receiver's latest-slot) over
    its OWN TCP connection to camera_ingest_node.py as a fixed cam_id.

    Unlike _SenderWorker there is no local OpenCV capture and the bytes are
    ALREADY JPEG (the phone encoded them) — we forward verbatim, NO re-encode.
    The slot is polled (the phone posts at ~10 fps, so a tight event loop buys
    nothing) and a STALENESS GATE drops the frame when the phone stopped posting:
    we only send a slot whose newest frame is < stale_max_age old AND whose
    sequence advanced since the last send. That way a closed tab / locked screen
    lets the /phone topic go quiet instead of pinning the last frame forever.

    The slot is a phone_camera.LatestFrameSlot, but this worker only relies on
    its duck-typed (get()->(jpeg,capture_ns,seq) | None, fresh_within(age)->bool)
    surface so the bridge stays import-light and the worker is unit-testable with
    a fake slot.
    """

    def __init__(self, slot, cam_id: int, host: str, port: int,
                 stop_event: threading.Event,
                 stale_max_age_s: float = constants.PHONE_FRAME_STALE_MAX_AGE_S,
                 poll_interval_s: float = 0.05):
        super().__init__(name=f"cam-send-ext-{cam_id}", daemon=True)
        self.slot = slot
        self.cam_id = cam_id
        self.host = host
        self.port = port
        self._stop_event = stop_event
        self.stale_max_age_s = stale_max_age_s
        self.poll_interval_s = poll_interval_s
        self.connected = False
        self.last_error_de = ""
        # role label used by status() — a synthetic name for the external feed.
        self.role = constants.PHONE_CAMERA_NAME

    def run(self):
        last_seq = -1
        sock = None
        while not self._stop_event.is_set():
            if sock is None:
                sock = self._connect()
                if sock is None:
                    break  # stopped while connecting
            # Poll the slot. Send only a NEW, FRESH frame; otherwise idle.
            got = self.slot.get() if self.slot is not None else None
            if (got is None
                    or got[2] == last_seq
                    or not self.slot.fresh_within(self.stale_max_age_s)):
                if self._stop_event.wait(self.poll_interval_s):
                    break
                continue
            jpeg, capture_ns, seq = got
            header = _HEADER.pack(self.cam_id, len(jpeg),
                                  int(capture_ns) & 0xFFFFFFFFFFFFFFFF)
            try:
                sock.sendall(header + jpeg)
                last_seq = seq
            except OSError as exc:
                self.connected = False
                self.last_error_de = f"Handy-Kamera-Verbindung verloren: {exc}"
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
                continue
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class _StationSenderWorker(_ConnectMixin, threading.Thread):
    """The Studio stream of a camera-station rig: the scene camera's NATIVE
    1920x1080 frames under STATION_SCENE_CAM_ID (200), over its own TCP
    connection, at most STATION_SEND_FPS per second (the newest frame wins),
    and only while the stream is on.

    A separate thread and connection from the recording sender, so a 1080p
    encode (cv2.imencode releases the GIL) never delays a recording frame. It
    reads only ``_latest_native``, so it never sends a 640x480 frame, and the
    recording sender never sends a native one. It connects when the stream
    turns on and closes its connection when the stream turns off, so no idle
    connection meets the ingest node's 5 s receive timeout. Turning the stream
    on or off never touches the camera handle, the crop or the recording
    sender."""

    def __init__(self, worker: _CaptureWorker, host: str, port: int,
                 stop_event: threading.Event,
                 send_fps: float = STATION_SEND_FPS):
        super().__init__(name="cam-send-station", daemon=True)
        self.worker = worker
        self.host = host
        self.port = port
        self._stop_event = stop_event
        self._active = threading.Event()
        self._wake = threading.Event()
        self._min_interval_s = 1.0 / float(send_fps)
        self.connected = False
        self.last_error_de = ""
        self.fps = 0.0
        self.sent = 0                # frames sent since construction
        self._rate_count = 0
        self._rate_t0 = time.monotonic()

    @property
    def active(self) -> bool:
        return self._active.is_set()

    def set_active(self, on: bool) -> None:
        """Turn the Studio stream on or off. Non-blocking: the worker keeps
        the native frame only while on, and this thread connects / closes."""
        if on:
            self.worker.set_native(True)
            self._active.set()
        else:
            self._active.clear()
            self.worker.set_native(False)
        self._wake.set()

    def _connect_cancelled(self) -> bool:
        return self._stop_event.is_set() or not self._active.is_set()

    def _close(self, sock):
        self.connected = False
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def run(self):
        cv2 = None
        jpeg_params = None
        sock = None
        last_seq = -1
        last_send = 0.0
        want = (constants.STATION_NATIVE_HEIGHT, constants.STATION_NATIVE_WIDTH)
        while not self._stop_event.is_set():
            if not self._active.is_set():
                if sock is not None:
                    self._close(sock)
                    sock = None
                self.fps = 0.0
                self._rate_count = 0
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if sock is None:
                sock = self._connect()
                if sock is None:
                    continue  # stopped, or the stream was switched off
                self._rate_count = 0
                self._rate_t0 = time.monotonic()
            latest, seq = self.worker.wait_new_native(last_seq, 0.5)
            if latest is None or seq == last_seq:
                continue
            # At most STATION_SEND_FPS: wait out the interval, then take the
            # NEWEST native frame (it may have moved on meanwhile).
            wait_s = last_send + self._min_interval_s - time.monotonic()
            if wait_s > 0:
                if self._stop_event.wait(wait_s):
                    break
                latest, seq = self.worker.latest_native()
                if latest is None:
                    continue  # switched off while waiting
            if not self._active.is_set():
                continue
            frame, ns = latest
            if tuple(frame.shape[:2]) != want:
                continue  # never send anything but a native frame
            if cv2 is None:
                import cv2 as _cv2
                cv2 = _cv2
                jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, STATION_JPEG_QUALITY]
            ok, buf = cv2.imencode(".jpg", frame, jpeg_params)
            if not ok:
                continue
            jpeg = buf.tobytes()
            header = _HEADER.pack(STATION_SCENE_CAM_ID, len(jpeg),
                                  ns & 0xFFFFFFFFFFFFFFFF)
            try:
                sock.sendall(header + jpeg)
            except OSError as exc:
                self.last_error_de = f"Stationskamera-Verbindung verloren: {exc}"
                self._close(sock)
                sock = None
                continue
            last_send = time.monotonic()
            last_seq = seq
            self.sent += 1
            self._rate_count += 1
            if last_send - self._rate_t0 >= 2.0:
                self.fps = self._rate_count / (last_send - self._rate_t0)
                self._rate_count = 0
                self._rate_t0 = last_send
        self._close(sock)


class CameraBridge:
    """Owns one capture worker + one sender (own TCP connection) per camera.

    May ALSO carry one external sender (the phone receiver's latest-slot →
    cam_id 2) registered via register_external_source() before start()."""

    def __init__(self, role_to_index: dict[str, int],
                 host: str | None = None, port: int | None = None,
                 station: bool = False):
        self.host = host or constants.CAMERA_INGEST_HOST
        self.port = port or constants.PORT_CAMERA_INGEST
        self.workers: list[_CaptureWorker] = []
        self._stop = threading.Event()
        self._timer_raised = False
        self._senders: list[_SenderWorker] = []
        self._external_senders: list[_ExternalSenderWorker] = []
        # Camera-station rig: the scene camera is opened natively once and
        # ALSO feeds the Studio stream (cam_id 200) while it is switched on.
        # None on every other rig — no station sender exists there at all.
        self._station_sender: _StationSenderWorker | None = None
        for role, index in role_to_index.items():
            if index is None:
                continue
            if role not in constants.CAMERA_BRIDGE_ROLES:
                raise ValueError(f"Unbekannte Kamerarolle: {role!r}")
            cam_id = constants.CAMERA_BRIDGE_ROLES.index(role)
            is_station = bool(station) and role == "scene"
            worker = _CaptureWorker(cam_id, role, index, station=is_station)
            self.workers.append(worker)
            self._senders.append(
                _SenderWorker(worker, self.host, self.port, self._stop))
            if is_station:
                self._station_sender = _StationSenderWorker(
                    worker, self.host, self.port, self._stop)
        self.last_error_de = ""

    def register_external_source(self, cam_id: int, slot) -> None:
        """Register an external JPEG source (the phone receiver's latest-slot)
        to be forwarded as ``cam_id`` over its own TCP connection.

        Call BEFORE start(). The phone is cam_id 2 (after gripper/scene); the
        container's EDUBOTICS_CAMERA_NAMES must list a 3rd name ("phone") so the
        ingest node has a publisher for /phone/image_raw/compressed. Frames flow
        only while the phone keeps posting (staleness gate in the worker)."""
        self._external_senders.append(
            _ExternalSenderWorker(slot, cam_id, self.host, self.port, self._stop))

    @property
    def connected(self) -> bool:
        """True once every CAMERA sender has an open connection.

        The phone (external sender) is intentionally excluded: it is optional and
        may legitimately be disconnected (no phone paired) while the local
        cameras are fine. Phone state is reported separately via status()."""
        return bool(self._senders) and all(s.connected for s in self._senders)

    def _raise_timer_resolution(self):
        """Drop the Windows system timer granularity from ~15.6ms to 1ms while
        the bridge runs. Defensive: keeps every timer-based wait fine-grained
        (Condition/Event timeouts, OpenCV internals, the connect backoff) so
        nothing rounds up to the ~15.6ms tick. This was essential when the sender
        was a polled pacer (it otherwise collapsed to ~17fps); the sender is now
        event-driven (woken immediately by the capture Condition notify), but the
        1ms timer is retained as a low-cost safety — the standard move for
        real-time capture apps (OBS, phosphobot). Paired with timeEndPeriod(1) in
        stop(). No-op off Windows / on failure."""
        import sys
        if sys.platform != "win32":
            return
        try:
            import ctypes
            if ctypes.windll.winmm.timeBeginPeriod(1) == 0:  # TIMERR_NOERROR
                self._timer_raised = True
        except Exception:  # noqa: BLE001
            pass

    def _threads_to_join(self) -> list:
        """Every thread stop() joins, senders before the capture workers (a
        sender blocked in wait_new is woken by its worker's stop())."""
        station = [self._station_sender] if self._station_sender else []
        return [*self._senders, *self._external_senders, *station,
                *self.workers]

    def start(self):
        self._raise_timer_resolution()
        for w in self.workers:
            w.start()
        for s in self._senders:
            s.start()
        for es in self._external_senders:
            es.start()
        if self._station_sender is not None:
            self._station_sender.start()

    def stop(self, join_timeout: float = STOP_DEADLINE_S):
        """Stop every thread, joining all of them against ONE shared deadline.

        ``join_timeout`` is the budget for the WHOLE stop, not per thread: this
        runs on the Tk thread (``gui_app._on_close``), and a per-thread timeout
        multiplied by the thread count would freeze the window for that sum.
        A thread still alive at the deadline is a daemon and dies with the
        process.
        """
        deadline = time.monotonic() + join_timeout
        self._stop.set()
        for w in self.workers:
            w.stop()
        # `ident is None` until a thread is started — guard so stop() is safe
        # even if start() was never called (e.g. construction then teardown).
        for t in self._threads_to_join():
            if t.ident is not None:
                t.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._timer_raised:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:  # noqa: BLE001
                pass
            self._timer_raised = False

    # ── the Studio stream (camera-station rigs only) ─────────────────
    @property
    def has_station(self) -> bool:
        """True when this bridge carries the station's Studio stream."""
        return self._station_sender is not None

    def set_station_stream(self, on: bool) -> bool:
        """Switch the Studio stream on or off. Non-blocking and idempotent.

        Never opens, releases or reopens the camera, and never touches the
        recording stream. Returns False when this bridge has no station
        stream (not a station rig, or no scene camera)."""
        if self._station_sender is None:
            return False
        self._station_sender.set_active(bool(on))
        return True

    def station_status(self) -> dict:
        """The station's two streams for GET /station/status: the recording
        crop's rate (``scene_fps``), the Studio stream's send rate
        (``station_fps``, 0 while off) and the first German error."""
        sender = self._station_sender
        if sender is None:
            return {"stream": "off", "scene_fps": 0.0, "station_fps": 0.0,
                    "error_de": ""}
        worker = sender.worker
        on = sender.active
        return {
            "stream": "on" if on else "off",
            "scene_fps": round(worker.fps, 1),
            "station_fps": round(sender.fps, 1) if on else 0.0,
            "error_de": (worker.last_error_de
                         or (sender.last_error_de if on else "")),
        }

    def status(self) -> dict:
        """Per-camera health for the GUI status line (German error strings)."""
        sender_by_role = {s.worker.role: s for s in self._senders}
        # Surface the first non-empty error/warn across all cameras.
        agg_error = self.last_error_de
        for w in self.workers:
            agg_error = agg_error or w.last_error_de or w.format_warn_de
        cameras = {
            w.role: {
                "fps": round(w.fps, 1),
                "negotiated_fps": round(w.negotiated_fps, 1),
                "index": w.index,
                "connected": (
                    sender_by_role[w.role].connected
                    if w.role in sender_by_role else False
                ),
                "error": w.last_error_de or w.format_warn_de,
            }
            for w in self.workers
        }
        # The phone (external) feed reports connection + its own error only — no
        # local capture metrics (it has no _CaptureWorker / OpenCV device).
        for es in self._external_senders:
            cameras[es.role] = {
                "fps": 0.0,
                "negotiated_fps": 0.0,
                "index": -1,
                "connected": es.connected,
                "error": es.last_error_de,
            }
        return {
            "connected": self.connected,
            "error": agg_error,
            "cameras": cameras,
        }
