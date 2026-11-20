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

The tower scene camera (``EDUBOTICS_SCENE_CAMERA_PROFILE=tower_v1``, spec 40
S1): ``_SceneCaptureWorker`` replaces ``_CaptureWorker`` for the scene camera
only. It opens the camera in one frozen mode (``win_camera.open_scene_capture``)
and lets a frame out only once ``scene_split.CaptureVerifier`` streams. Each
frame then becomes the recorded 640x480 outside frame (cam_id 1, sent by the
unchanged ``_SenderWorker``), the camera's own JPEG for Roboter Studio (cam_id
16, at most 15 Hz) and the camera state (cam_id 17, JSON). The senders of ids
16 and 17 (``_HelloSenderWorker``) speak first: one hello frame
``[255][1][0][0x01]`` the ingest node answers with ``0x01``; an older image
answers nothing and they stay idle. Connections of ids 0, 1 and 2 never send or
receive anything new, and ``legacy`` (the default) builds today's bridge.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

from . import constants, scene_split, win_camera

# Header layout matches camera_ingest_node.py _HEADER exactly.
_HEADER = struct.Struct(">BIQ")
_RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 3.0, 5.0)  # capped, then repeats last
_MAX_READ_FAILURES = 10  # consecutive read() failures before reopening (phosphobot uses the same threshold)

# The tower scene camera (spec 40 S1).
HELLO_WAIT_S = 1.0                  # how long a sender of id 16/17 waits for the hello's answer
STATE_RESEND_S = 1.0                # the camera state goes out on every change and at least this often
STUDIO_FALLBACK_JPEG_QUALITY = 90   # the studio frame of a decoded (not passed-through) stream
_FAILED_READ_WAIT_S = 0.005         # a read that returned nothing, before the next one
_IDLE_WAIT_S = 0.1                  # the worker's pace while it waits to retry a fault

SCENE_SELFTEST_FAILED_DE = (
    "Selbsttest der Bildaufbereitung fehlgeschlagen — die Turm-Kamera bleibt "
    "aus. Bitte EduBotics aktualisieren.")
SCENE_OPEN_FAILED_DE = (
    "Die Turm-Kamera wird von einem anderen Programm verwendet oder ist nicht "
    "angeschlossen.")
SCENE_WRONG_MODE_DE = (
    "Die Turm-Kamera liefert kein MJPEG-Bild mit 1920 × 1080 Bildpunkten und "
    "mindestens 10 Bildern pro Sekunde — sie wird erneut geöffnet.")
SCENE_NO_FRAMES_DE = "Die Turm-Kamera liefert keine Bilder — sie wird erneut geöffnet."
SCENE_BAD_FRAMES_DE = "Die Turm-Kamera liefert beschädigte Bilder — sie wird erneut geöffnet."
SCENE_STREAMING_DE = "Turm-Kamera: 1920×1080 MJPG, {fps:.1f} Bilder/s, Direktdurchleitung {passthrough}"
_NOT_ACTIVATED_DE = "Die Turm-Kamera wurde nicht aktiviert."
SCENE_PROBE_REFUSED_DE = (
    f"Diese Kamera liefert kein Bild mit 1920 × 1080 Bildpunkten. {_NOT_ACTIVATED_DE}")
OLDER_IMAGE_DE = (
    "Das Roboter-Abbild ist älter als die Software — Roboter Studio bekommt kein "
    "Bild der Turm-Kamera.")
_SCENE_FAULT_DE = {
    scene_split.FAULT_SELFTEST: SCENE_SELFTEST_FAILED_DE,
    scene_split.FAULT_OPEN_FAILED: SCENE_OPEN_FAILED_DE,
    scene_split.FAULT_WRONG_MODE: SCENE_WRONG_MODE_DE,
    scene_split.FAULT_NO_FRAMES: SCENE_NO_FRAMES_DE,
    scene_split.FAULT_BAD_FRAMES: SCENE_BAD_FRAMES_DE,
}


def crop_to_target_aspect(frame):
    """The centre of ``frame`` in the target's aspect ratio
    (``constants.CAMERA_WIDTH`` x ``CAMERA_HEIGHT``, 4:3 by default).

    A wider frame loses its sides, a taller one its top and bottom; a frame
    already of that aspect is returned as it is. The one implementation of the
    field the bridge records: the capture worker resizes the result to the
    target size, and the setup window's preview (``gui_app.py``'s
    ``_frame_to_photo_data``) shows the same field (D21, D82). The crop keeps
    at least one row or column: against the 4:3 target a 1-pixel-wide frame's
    crop rounds down to 0 rows, and cv2.resize raises on an empty frame, which
    would end the capture thread.
    """
    height, width = frame.shape[:2]
    if width * constants.CAMERA_HEIGHT > height * constants.CAMERA_WIDTH:
        crop_w = max(1, height * constants.CAMERA_WIDTH // constants.CAMERA_HEIGHT)
        x0 = (width - crop_w) // 2
        return frame[:, x0:x0 + crop_w]
    if width * constants.CAMERA_HEIGHT < height * constants.CAMERA_WIDTH:
        crop_h = max(1, width * constants.CAMERA_HEIGHT // constants.CAMERA_WIDTH)
        y0 = (height - crop_h) // 2
        return frame[y0:y0 + crop_h]
    return frame


class _CaptureWorker(threading.Thread):
    """Free-running capture loop for one camera. Keeps only the newest frame."""

    def __init__(self, cam_id: int, role: str, index: int):
        super().__init__(name=f"cam-{role}", daemon=True)
        self.cam_id = cam_id
        self.role = role
        self.index = index
        # Never `self._stop`: threading.Thread has a method of that name, which
        # join() and is_alive() call on CPython 3.11/3.12 (D24).
        self._halt = threading.Event()
        self._cond = threading.Condition()
        self._latest = None          # (frame_bgr, capture_ns)
        self._latest_seq = 0         # monotonic; lets the sender detect new frames
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
        self._halt.set()
        # Wake any sender blocked in wait_new so stop() is prompt.
        with self._cond:
            self._cond.notify_all()

    def run(self):
        cv2 = None
        cap = None
        failures = 0
        while not self._halt.is_set():
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
            height, width = frame.shape[:2]
            if (width != constants.CAMERA_WIDTH
                    or height != constants.CAMERA_HEIGHT):
                # Centre-crop a frame of another aspect ratio to the target's
                # first, so a 16:9 camera is cut at the sides instead of
                # squeezed (D21); a frame already of that aspect is resized
                # exactly as before.
                frame = cv2.resize(crop_to_target_aspect(frame),
                                   (constants.CAMERA_WIDTH, constants.CAMERA_HEIGHT))
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


class _ConnectMixin:
    """Shared connect-with-backoff for the camera senders.

    Both the per-camera sender and the phone external sender are CLIENTS to the
    container's TCP server and reconnect identically; only the frame source
    differs. Subclasses set ``self.host`` / ``self.port`` / ``self._halt`` /
    ``self.connected`` / ``self.last_error_de`` and call ``self._connect()``.
    """

    def _connect(self) -> "socket.socket | None":
        attempt = 0
        while not self._halt.is_set():
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
                if self._halt.wait(backoff):
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
        self._halt = stop_event  # never `_stop`, see _CaptureWorker
        self.connected = False
        self.last_error_de = ""

    def run(self):
        import cv2
        jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, constants.CAMERA_JPEG_QUALITY]
        # Start at the worker's current sequence so wait_new blocks until a
        # frame exists. Starting below it (the old -1 against the worker's 0)
        # returned at once on every call until the first frame: a busy-spin of
        # millions of calls a second on a camera that never delivers (D21).
        _, last_seq = self.worker.latest()
        sock = None
        while not self._halt.is_set():
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
        self._halt = stop_event  # never `_stop`, see _CaptureWorker
        self.stale_max_age_s = stale_max_age_s
        self.poll_interval_s = poll_interval_s
        self.connected = False
        self.last_error_de = ""
        # role label used by status() — a synthetic name for the external feed.
        self.role = constants.PHONE_CAMERA_NAME

    def run(self):
        last_seq = -1
        sock = None
        while not self._halt.is_set():
            if sock is None:
                sock = self._connect()
                if sock is None:
                    break  # stopped while connecting
            # Poll the slot. Send only a NEW, FRESH frame; otherwise idle.
            got = self.slot.get() if self.slot is not None else None
            if (got is None
                    or got[2] == last_seq
                    or not self.slot.fresh_within(self.stale_max_age_s)):
                if self._halt.wait(self.poll_interval_s):
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


class _Slot:
    """A latest-wins hand-over from the tower worker to one sender: the newest
    item and a sequence that moves with every put. ``wait_new`` blocks while
    the sequence still equals the caller's, so a sender never spins (D21)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._item = None
        self._seq = 0

    def put(self, item):
        with self._cond:
            self._item = item
            self._seq += 1
            self._cond.notify_all()

    def latest(self):
        with self._cond:
            return self._item, self._seq

    def wait_new(self, last_seq, timeout):
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            return self._item, self._seq

    def wake(self):
        """Wake every waiter (the bridge is stopping)."""
        with self._cond:
            self._cond.notify_all()


class _SceneCaptureWorker(threading.Thread):
    """The tower scene camera's capture owner on Windows (spec 40 S1).

    Runs ``scene_split.selftest()`` once, then opens the camera with
    ``win_camera.open_scene_capture`` and feeds every frame to a
    ``scene_split.CaptureVerifier``. Only a frame the verifier releases (in
    ``streaming``) leaves: its outside frame into the slot today's
    ``_SenderWorker`` sends as camera id 1 (``latest``/``wait_new``, the
    ``_CaptureWorker`` surface), the camera's own JPEG into ``studio`` at most
    ``scene_split.STUDIO_MAX_HZ`` times a second (a decoded frame is encoded at
    ``STUDIO_FALLBACK_JPEG_QUALITY``). The state message goes into
    ``state_slot`` on every change of state or fault and once a second. A fault
    closes the device, which is opened again when the verifier's back-off is
    due; a failed self-test is final and the device is never opened. ``clock``
    is the monotonic clock the verifier runs on; ``log`` gets the one
    Protokoll line of the first ``streaming``.
    """

    def __init__(self, cam_id: int, role: str, index: int, log=None, clock=time.monotonic):
        super().__init__(name=f"cam-{role}-tower", daemon=True)
        self.cam_id = cam_id
        self.role = role
        self.index = index
        self._halt = threading.Event()  # never `_stop`, see _CaptureWorker
        self._log = log
        self._clock = clock
        self.verifier = scene_split.CaptureVerifier("windows", constants.CAMERA_JPEG_QUALITY)
        self._outside = _Slot()   # (outside frame BGR, capture_ns) for camera id 1
        self.studio = _Slot()     # (camera JPEG bytes, capture_ns) for camera id 16
        self.state_slot = _Slot()  # (state message bytes, send time ns) for camera id 17
        self._studio_rate = scene_split.RateLimiter(scene_split.STUDIO_MAX_HZ)
        self._seen_generation = None
        self._state_at = None
        self._announced = False
        # What CameraBridge.status() reads, as on _CaptureWorker.
        self.fps = 0.0
        self.negotiated_fps = 0.0
        self.last_error_de = ""
        self.format_warn_de = ""

    def latest(self):
        return self._outside.latest()

    def wait_new(self, last_seq, timeout):
        return self._outside.wait_new(last_seq, timeout)

    def stop(self):
        self._halt.set()
        for slot in (self._outside, self.studio, self.state_slot):
            slot.wake()

    def run(self):
        if scene_split.selftest():
            self.verifier.selftest_failed(self._clock())
            self._note(self._clock())
            return
        cap = None
        try:
            while not self._halt.is_set():
                if cap is None:
                    cap = self._open_when_due()
                    continue
                ok, frame = cap.read()
                now = self._clock()
                capture_ns = time.time_ns()
                if not ok or frame is None:
                    self.verifier.frame(now, None)
                    self._halt.wait(_FAILED_READ_WAIT_S)
                elif self.verifier.frame(now, frame):
                    self._hand_over(now, frame, capture_ns)
                self._note(now)
                if self.verifier.state == scene_split.STATE_FAULT:
                    cap.release()
                    cap = None
        finally:
            if cap is not None:
                cap.release()

    def _open_when_due(self):
        """The opened device, or None while a fault's back-off runs or when
        the open failed (the fault ``open_failed``)."""
        verifier = self.verifier
        now = self._clock()
        if verifier.state == scene_split.STATE_FAULT and not verifier.retry_due(now):
            self._note(now)
            self._halt.wait(_IDLE_WAIT_S)
            return None
        verifier.opening(now)
        try:
            cap = win_camera.open_scene_capture(self.index)
        except Exception:  # noqa: BLE001 — every open failure is the fault open_failed
            verifier.open_failed(self._clock())
            cap = None
        else:
            verifier.opened(self._clock())
        self._note(self._clock())
        return cap

    def _hand_over(self, now, frame, capture_ns):
        """A frame the verifier released: the outside frame and, rate-limited,
        the studio frame."""
        try:
            if self.verifier.passthrough:
                outside = scene_split.outside_from_jpeg(frame)
            else:
                outside = scene_split.outside_from_bgr(frame)
        except scene_split.SplitError:
            self.verifier.split_failed(now)
            return
        self._outside.put((outside, capture_ns))
        if self._studio_rate.allow(now):
            studio = self._studio_jpeg(frame)
            if studio is not None:
                self.studio.put((studio, capture_ns))

    def _studio_jpeg(self, frame):
        """The camera's own JPEG bytes, unchanged; a decoded frame encoded."""
        if self.verifier.passthrough:
            return frame.tobytes() if hasattr(frame, "tobytes") else bytes(frame)
        import cv2
        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, STUDIO_FALLBACK_JPEG_QUALITY])
        return buf.tobytes() if ok else None

    def _note(self, now):
        """React to a change of the verifier (the German error, the Protokoll
        line) and keep the state message fresh."""
        verifier = self.verifier
        fps = verifier.fps
        self.fps = 0.0 if fps is None else fps
        if verifier.generation != self._seen_generation:
            self._seen_generation = verifier.generation
            if verifier.state == scene_split.STATE_STREAMING:
                self.last_error_de = ""
                if not self._announced and self._log is not None:
                    self._log(SCENE_STREAMING_DE.format(
                        fps=self.fps, passthrough="aktiv" if verifier.passthrough else "aus"))
                self._announced = True
            elif verifier.state == scene_split.STATE_FAULT:
                self.last_error_de = _SCENE_FAULT_DE.get(verifier.fault, "")
        elif self._state_at is not None and now - self._state_at < STATE_RESEND_S:
            return
        self._state_at = now
        self.state_slot.put(
            (scene_split.state_message(**verifier.state_fields()), time.time_ns()))


class _HelloSenderWorker(_ConnectMixin, threading.Thread):
    """Sends one tower slot verbatim as ``cam_id`` (16: the studio frame, 17:
    the camera state) over its OWN TCP connection, and only to an ingest node
    that answered the hello (guard G7).

    After connecting it sends ``[255][len 1][capture_ns 0][0x01]`` and waits
    ``HELLO_WAIT_S`` for the one-byte answer ``scene_split.HELLO``. No answer
    is an image older than this GUI (it publishes the hello nowhere): the
    sender calls ``on_unanswered`` and stays idle until the bridge stops. A
    connection that ends during the hello (nothing behind the forwarder yet)
    is made again. Like ``_SenderWorker`` it starts at the slot's sequence and
    sends only what is newer; with ``resend_s`` it also sends the newest item
    at once after the hello and again when nothing newer came for that long
    (the camera state).
    """

    def __init__(self, source: _Slot, cam_id: int, host: str, port: int,
                 stop_event: threading.Event, on_unanswered, resend_s=None):
        super().__init__(name=f"cam-send-tower-{cam_id}", daemon=True)
        self.source = source
        self.cam_id = cam_id
        self.host = host
        self.port = port
        self._halt = stop_event  # never `_stop`, see _CaptureWorker
        self._on_unanswered = on_unanswered
        self.resend_s = resend_s
        self.connected = False
        self.idle = False
        self.last_error_de = ""

    def _hello(self, sock):
        """True: answered; False: no answer (an older image); None: the
        connection ended."""
        timeout = sock.gettimeout()
        try:
            sock.sendall(_HEADER.pack(scene_split.HELLO_CAM_ID, len(scene_split.HELLO), 0)
                         + scene_split.HELLO)
            sock.settimeout(HELLO_WAIT_S)
            answer = sock.recv(len(scene_split.HELLO))
        except socket.timeout:
            return False
        except OSError:
            return None
        finally:
            try:
                sock.settimeout(timeout)
            except OSError:
                pass
        if not answer:
            return None
        return answer == scene_split.HELLO

    def _resend_due(self, sent_at):
        return self.resend_s is not None and (
            sent_at is None or time.monotonic() - sent_at >= self.resend_s)

    def _wait_s(self, sent_at):
        if self.resend_s is None or sent_at is None:
            return 0.5
        return min(0.5, max(0.0, self.resend_s - (time.monotonic() - sent_at)))

    def run(self):
        _, last_seq = self.source.latest()
        sent_at = None
        sock = None
        while not self._halt.is_set():
            if sock is None:
                sock = self._connect()
                if sock is None:
                    break  # stopped while connecting
                answered = self._hello(sock)
                if not answered:
                    self.connected = False
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = None
                    if answered is None:
                        if self._halt.wait(_RECONNECT_BACKOFF_S[0]):
                            break
                        continue
                    self.idle = True
                    self._on_unanswered()
                    self._halt.wait()
                    break
                sent_at = None
            if self._resend_due(sent_at):
                item, seq = self.source.latest()
            else:
                item, seq = self.source.wait_new(last_seq, self._wait_s(sent_at))
                if seq == last_seq:
                    continue
            if item is None:
                self.source.wait_new(seq, self._wait_s(sent_at))
                continue
            payload, capture_ns = item
            header = _HEADER.pack(self.cam_id, len(payload), capture_ns & 0xFFFFFFFFFFFFFFFF)
            try:
                sock.sendall(header + payload)
                last_seq = seq
                sent_at = time.monotonic()
            except OSError as exc:
                self.connected = False
                self.last_error_de = f"Kamera-Verbindung verloren: {exc}"
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def probe_scene_camera(index: int, clock=time.monotonic) -> str:
    """The „Turm-Kamera" box's capability probe (spec 40 S1): open the camera
    as the tower worker does and read until ``scene_split.VERIFY_FRAMES``
    frames passed the frame check, within the verifier's bounds (3 s for the
    frames, 2 s of silence). ``''`` when the camera delivers them, else the
    German reason. The device is always released."""
    verifier = scene_split.CaptureVerifier("windows", constants.CAMERA_JPEG_QUALITY)
    verifier.opening(clock())
    try:
        cap = win_camera.open_scene_capture(index)
    except Exception:  # noqa: BLE001 — every open failure refuses the box
        return f"{SCENE_OPEN_FAILED_DE} {_NOT_ACTIVATED_DE}"
    try:
        verifier.opened(clock())
        while verifier.state == scene_split.STATE_VERIFYING:
            ok, frame = cap.read()
            verifier.frame(clock(), frame if ok else None)
            if not ok or frame is None:
                time.sleep(_FAILED_READ_WAIT_S)
    finally:
        cap.release()
    return "" if verifier.state == scene_split.STATE_STREAMING else SCENE_PROBE_REFUSED_DE


class CameraBridge:
    """Owns one capture worker + one sender (own TCP connection) per camera.

    May ALSO carry one external sender (the phone receiver's latest-slot →
    cam_id 2) registered via register_external_source() before start().

    ``scene_profile`` is the session's ``EDUBOTICS_SCENE_CAMERA_PROFILE``:
    with ``tower_v1`` the scene camera is captured by ``_SceneCaptureWorker``
    and gets the two senders of ids 16 and 17 besides its own; anything else
    is ``legacy``, today's bridge. ``log`` takes the tower camera's Protokoll
    lines."""

    def __init__(self, role_to_index: dict[str, int],
                 host: str | None = None, port: int | None = None,
                 scene_profile: str = scene_split.PROFILE_LEGACY, log=None):
        self.host = host or constants.CAMERA_INGEST_HOST
        self.port = port or constants.PORT_CAMERA_INGEST
        self.workers: list[_CaptureWorker] = []
        self._stop = threading.Event()
        self._timer_raised = False
        self._senders: list[_SenderWorker] = []
        self._external_senders: list[_ExternalSenderWorker] = []
        self._tower_senders: list[_HelloSenderWorker] = []
        self._log = log
        self._older_image_lock = threading.Lock()
        self._older_image_said = False
        tower = scene_split.normalise_profile(scene_profile) == scene_split.PROFILE_TOWER
        for role, index in role_to_index.items():
            if index is None:
                continue
            if role not in constants.CAMERA_BRIDGE_ROLES:
                raise ValueError(f"Unbekannte Kamerarolle: {role!r}")
            cam_id = constants.CAMERA_BRIDGE_ROLES.index(role)
            if tower and role == "scene":
                worker = _SceneCaptureWorker(cam_id, role, index, log=log)
                self._tower_senders.append(_HelloSenderWorker(
                    worker.studio, scene_split.STUDIO_CAM_ID, self.host, self.port,
                    self._stop, self._older_image))
                self._tower_senders.append(_HelloSenderWorker(
                    worker.state_slot, scene_split.STATE_CAM_ID, self.host, self.port,
                    self._stop, self._older_image, resend_s=STATE_RESEND_S))
            else:
                worker = _CaptureWorker(cam_id, role, index)
            self.workers.append(worker)
            self._senders.append(
                _SenderWorker(worker, self.host, self.port, self._stop))
        self.last_error_de = ""

    def _older_image(self):
        """A sender of id 16 or 17 got no answer to its hello: say so once."""
        with self._older_image_lock:
            if self._older_image_said:
                return
            self._older_image_said = True
        if self._log is not None:
            self._log(OLDER_IMAGE_DE)

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

    def start(self):
        self._raise_timer_resolution()
        for w in self.workers:
            w.start()
        for s in self._senders:
            s.start()
        for ts in self._tower_senders:
            ts.start()
        for es in self._external_senders:
            es.start()

    def stop(self, join_timeout: float = 3.0):
        self._stop.set()
        for w in self.workers:
            w.stop()
        # `ident is None` until a thread is started — guard so stop() is safe
        # even if start() was never called (e.g. construction then teardown).
        for s in self._senders:
            if s.ident is not None:
                s.join(timeout=join_timeout)
        for ts in self._tower_senders:
            if ts.ident is not None:
                ts.join(timeout=join_timeout)
        for es in self._external_senders:
            if es.ident is not None:
                es.join(timeout=join_timeout)
        for w in self.workers:
            if w.ident is not None:
                w.join(timeout=join_timeout)
        if self._timer_raised:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:  # noqa: BLE001
                pass
            self._timer_raised = False

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
