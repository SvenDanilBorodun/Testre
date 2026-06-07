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


class _CaptureWorker(threading.Thread):
    """Free-running capture loop for one camera. Keeps only the newest frame."""

    def __init__(self, cam_id: int, role: str, index: int):
        super().__init__(name=f"cam-{role}", daemon=True)
        self.cam_id = cam_id
        self.role = role
        self.index = index
        self._stop = threading.Event()
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
        self._stop.set()
        # Wake any sender blocked in wait_new so stop() is prompt.
        with self._cond:
            self._cond.notify_all()

    def run(self):
        cv2 = None
        cap = None
        failures = 0
        while not self._stop.is_set():
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


class _ConnectMixin:
    """Shared connect-with-backoff for the camera senders.

    Both the per-camera sender and the phone external sender are CLIENTS to the
    container's TCP server and reconnect identically; only the frame source
    differs. Subclasses set ``self.host`` / ``self.port`` / ``self._stop`` /
    ``self.connected`` / ``self.last_error_de`` and call ``self._connect()``.
    """

    def _connect(self) -> "socket.socket | None":
        attempt = 0
        while not self._stop.is_set():
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
                if self._stop.wait(backoff):
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
        self._stop = stop_event
        self.connected = False
        self.last_error_de = ""

    def run(self):
        import cv2
        jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, constants.CAMERA_JPEG_QUALITY]
        last_seq = -1
        sock = None
        while not self._stop.is_set():
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
        self._stop = stop_event
        self.stale_max_age_s = stale_max_age_s
        self.poll_interval_s = poll_interval_s
        self.connected = False
        self.last_error_de = ""
        # role label used by status() — a synthetic name for the external feed.
        self.role = constants.PHONE_CAMERA_NAME

    def run(self):
        last_seq = -1
        sock = None
        while not self._stop.is_set():
            if sock is None:
                sock = self._connect()
                if sock is None:
                    break  # stopped while connecting
            # Poll the slot. Send only a NEW, FRESH frame; otherwise idle.
            got = self.slot.get() if self.slot is not None else None
            if (got is None
                    or got[2] == last_seq
                    or not self.slot.fresh_within(self.stale_max_age_s)):
                if self._stop.wait(self.poll_interval_s):
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


class CameraBridge:
    """Owns one capture worker + one sender (own TCP connection) per camera.

    May ALSO carry one external sender (the phone receiver's latest-slot →
    cam_id 2) registered via register_external_source() before start()."""

    def __init__(self, role_to_index: dict[str, int],
                 host: str | None = None, port: int | None = None):
        self.host = host or constants.CAMERA_INGEST_HOST
        self.port = port or constants.PORT_CAMERA_INGEST
        self.workers: list[_CaptureWorker] = []
        self._stop = threading.Event()
        self._timer_raised = False
        self._senders: list[_SenderWorker] = []
        self._external_senders: list[_ExternalSenderWorker] = []
        for role, index in role_to_index.items():
            if index is None:
                continue
            if role not in constants.CAMERA_BRIDGE_ROLES:
                raise ValueError(f"Unbekannte Kamerarolle: {role!r}")
            cam_id = constants.CAMERA_BRIDGE_ROLES.index(role)
            worker = _CaptureWorker(cam_id, role, index)
            self.workers.append(worker)
            self._senders.append(
                _SenderWorker(worker, self.host, self.port, self._stop))
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

    def start(self):
        self._raise_timer_resolution()
        for w in self.workers:
            w.start()
        for s in self._senders:
            s.start()
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
