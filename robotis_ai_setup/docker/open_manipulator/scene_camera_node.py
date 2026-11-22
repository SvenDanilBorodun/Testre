#!/usr/bin/env python3
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower scene camera's capture owner on the Orange Pi (spec 40 S1).

On the in-container camera path (usb_cam: the Orange Pi) the entrypoint starts
this node instead of usb_cam for the slot named ``scene`` when
``EDUBOTICS_SCENE_CAMERA_PROFILE`` reads ``tower_v1``
(``entrypoint_omx.sh::scene_slot_uses_tower_node``). It is the Pi twin of the
Windows GUI's ``camera_bridge._SceneCaptureWorker`` and shares its one
derivation, ``scene_split.py`` (COPY'd next to this file):

* ``scene_split.selftest()`` once, before the camera is opened; a failure is
  the final fault ``selftest`` and the camera is never opened.
* The camera through V4L2: MJPG 1920x1080 at 30 fps with ``CONVERT_RGB`` off,
  so a frame arrives as the camera's own JPEG. Before every open the node asks
  the driver not to lower the frame rate in a dim room
  (``exposure_dynamic_framerate=0``, the older kernels' name
  ``exposure_auto_priority=0``); a missing tool or control is one log line.
* Every frame goes through ``scene_split.CaptureVerifier`` (platform ``pi``);
  only a frame it releases (in ``streaming``) leaves: the 640x480 outside frame,
  encoded at ``--quality`` (95; the profile id names it), on
  ``/<name>/image_raw/compressed`` exactly as the ingest node publishes a
  camera (``format='jpeg'``, ``frame_id=<name>``, RELIABLE depth 5, stamped
  with the wall clock at the read), and the camera's own JPEG at most
  ``STUDIO_MAX_HZ`` times a second on ``STUDIO_TOPIC`` for Roboter Studio. A
  frame the check refuses — a truncated JPEG among them (decision D59) — is
  published on neither topic and counted in the state's ``dropped``.
* The state message on ``STATE_TOPIC`` (latched) on every change and once a
  second, kept alive by a timer while a read blocks.

It reads no environment variable: its device, its name and its quality are its
arguments; the profile is the entrypoint's decision.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scene_split  # noqa: E402  (COPY'd next to this file in the image)

import rclpy  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from sensor_msgs.msg import CompressedImage  # noqa: E402
from std_msgs.msg import String  # noqa: E402

DEFAULT_QUALITY = 95                 # the outside frame's JPEG quality on the Pi
STUDIO_FALLBACK_JPEG_QUALITY = 90    # the studio frame of a decoded (not passthrough) stream
STUDIO_FRAME_ID = 'scene_studio'     # what the ingest node stamps on camera id 16
STATE_RESEND_S = 1.0
STATE_TIMER_S = 0.5                  # keeps the state alive while a read blocks
FAILED_READ_WAIT_S = 0.05
IDLE_WAIT_S = 0.2
V4L2_CTL_TIMEOUT_S = 3.0
# Tried in this order before every open; the first the driver takes ends the
# attempt. Linux 5.x renamed the UVC control exposure_auto_priority.
V4L2_CONTROLS = ('exposure_dynamic_framerate=0', 'exposure_auto_priority=0')
SELFTEST_FAILED_DE = ('Selbsttest der Bildaufbereitung fehlgeschlagen – die Turm-Kamera '
                      'bleibt aus. Bitte EduBotics aktualisieren.')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='The tower scene camera (MJPG 1920x1080) for the Orange Pi.')
    parser.add_argument('--device', required=True, help='the V4L2 capture node')
    parser.add_argument('--name', required=True,
                        help='the camera name: publishes /<name>/image_raw/compressed')
    parser.add_argument('--quality', type=int, default=DEFAULT_QUALITY,
                        help='JPEG quality of the 640x480 outside frame')
    return parser.parse_args(argv)


def open_capture(device):
    """The tower camera through V4L2 in MJPG 1920x1080 at 30 fps with
    ``CONVERT_RGB`` off, or None when the device does not open."""
    import cv2
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, scene_split.CAPTURE_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, scene_split.CAPTURE_H)
    cap.set(cv2.CAP_PROP_FPS, scene_split.CAPTURE_FPS)
    cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
    return cap


def _encode(image, quality):
    """JPEG bytes of a BGR image, or None."""
    import cv2
    ok, buf = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def _jpeg_bytes(frame):
    """The bytes of a passthrough sample (a 1xN uint8 array or bytes)."""
    return frame.tobytes() if hasattr(frame, 'tobytes') else bytes(frame)


class SceneCameraNode(Node):
    """Owns the tower camera. ``start()`` runs the capture loop on a thread of
    its own (``step()`` once per turn); ``clock`` is the monotonic clock the
    verifier and the studio rate run on."""

    def __init__(self, device, name, quality=DEFAULT_QUALITY, clock=time.monotonic):
        super().__init__('scene_camera')
        self.device = device
        self.name = name
        self.quality = quality
        self._monotonic = clock   # never `_clock`: rclpy's Node keeps its ROS clock there
        self.verifier = scene_split.CaptureVerifier('pi', quality)
        self._studio_rate = scene_split.RateLimiter(scene_split.STUDIO_MAX_HZ)
        # The verifier is driven by the capture thread and ticked by the timer;
        # the state message is built and published under the same lock, so two
        # threads never publish an older state after a newer one.
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread = None
        self._cap = None
        self._selftested = False
        self._seen_generation = None
        self._state_at = None
        self._announced = False
        self._controls_warned = False

        self._scene_pub = self.create_publisher(
            CompressedImage, f'/{name}/image_raw/compressed',
            QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE,
                       history=HistoryPolicy.KEEP_LAST))
        self._studio_pub = self.create_publisher(
            CompressedImage, scene_split.STUDIO_TOPIC,
            QoSProfile(depth=2, reliability=ReliabilityPolicy.RELIABLE,
                       history=HistoryPolicy.KEEP_LAST))
        self._state_pub = self.create_publisher(
            String, scene_split.STATE_TOPIC,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                       history=HistoryPolicy.KEEP_LAST,
                       durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.create_timer(STATE_TIMER_S, self._tick)
        self.get_logger().info(
            f'scene_camera: {device} as {name}, outside quality {quality} '
            f'(profile id {self.verifier.profile_id})')

    # -- the capture loop ----------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, name='scene-capture', daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._halt.is_set() and self.step():
                pass
        except Exception as exc:
            self._end_by_error(exc)
            raise
        finally:
            self._release()

    def _end_by_error(self, exc):
        """The loop ended by an exception: the camera stays off until the node
        starts again, and the state message left behind says so (never a stale
        ``streaming``)."""
        now = self._monotonic()
        with self._lock:
            self.verifier.close(now)
        self.get_logger().warn(f'scene_camera: the capture loop ended ({type(exc).__name__}); '
                               'the camera stays off until the environment is restarted')
        self._note(now)

    def step(self):
        """One turn: the self-test, an open when due, or one frame read (a read
        that raises is a read that returned nothing: the verifier's silence
        rule closes the device and it is opened again after the back-off).
        False once the self-test failed (the loop ends; the timer keeps the
        state message alive)."""
        if not self._selftested:
            self._selftested = True
            reason = scene_split.selftest()
            if reason:
                with self._lock:
                    self.verifier.selftest_failed(self._monotonic())
                self.get_logger().warn(f'[FEHLER] {SELFTEST_FAILED_DE} ({reason})')
                self._note(self._monotonic())
                return False
            return True
        if self.verifier.fault == scene_split.FAULT_SELFTEST:
            return False
        if self._cap is None:
            self._cap = self._open_when_due()
            return True
        try:
            ok, frame = self._cap.read()
        except Exception:  # noqa: BLE001 — a read that raises returned nothing (X-K1PI)
            ok, frame = False, None
        now = self._monotonic()
        stamp_ns = time.time_ns()
        if not ok:
            frame = None
        with self._lock:
            released = self.verifier.frame(now, frame)
        if frame is None:
            self._halt.wait(FAILED_READ_WAIT_S)
        elif released:
            self._hand_over(now, frame, stamp_ns)
        self._note(now)
        if self.verifier.state == scene_split.STATE_FAULT:
            self._release()
        return True

    def _open_when_due(self):
        """The opened capture, or None while a fault's back-off runs or when
        the open failed (the fault ``open_failed``)."""
        now = self._monotonic()
        if self.verifier.state == scene_split.STATE_FAULT and not self.verifier.retry_due(now):
            self._note(now)
            self._halt.wait(IDLE_WAIT_S)
            return None
        self._try_v4l2_controls()
        with self._lock:
            self.verifier.opening(self._monotonic())
        try:
            cap = open_capture(self.device)
        except Exception:  # noqa: BLE001 — every open failure is the fault open_failed
            cap = None
        with self._lock:
            if cap is None:
                self.verifier.open_failed(self._monotonic())
            else:
                self.verifier.opened(self._monotonic())
        if cap is None:
            self.get_logger().warn(f'scene_camera: {self.device} did not open')
        self._note(self._monotonic())
        return cap

    def _try_v4l2_controls(self):
        """Ask the driver for a constant frame rate (``V4L2_CONTROLS``, the
        first one it takes wins). Never fails the camera; the first attempt
        nothing took is one log line."""
        failure = None
        for control in V4L2_CONTROLS:
            try:
                result = subprocess.run(['v4l2-ctl', '-d', self.device, '-c', control],
                                        capture_output=True, timeout=V4L2_CTL_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as exc:
                failure = f'the tool is not usable ({type(exc).__name__})'
                break
            if result.returncode == 0:
                return
            failure = 'the driver takes neither ' + ' nor '.join(V4L2_CONTROLS)
        if not self._controls_warned:
            self._controls_warned = True
            self.get_logger().warn(
                f'scene_camera: v4l2-ctl could not set a constant frame rate: {failure}')

    def _hand_over(self, now, frame, stamp_ns):
        """A frame the verifier released: the outside frame and, at most
        ``STUDIO_MAX_HZ`` times a second, the studio frame."""
        passthrough = self.verifier.passthrough
        try:
            if passthrough:
                outside = scene_split.outside_from_jpeg(frame)
            else:
                outside = scene_split.outside_from_bgr(frame)
        except scene_split.SplitError:
            outside = None
        data = None if outside is None else _encode(outside, self.quality)
        if data is None:
            with self._lock:
                self.verifier.split_failed(now)
            return
        self._scene_pub.publish(self._image_msg(self.name, data, stamp_ns))
        if self._studio_rate.allow(now):
            studio = (_jpeg_bytes(frame) if passthrough
                      else _encode(frame, STUDIO_FALLBACK_JPEG_QUALITY))
            if studio is not None:
                self._studio_pub.publish(self._image_msg(STUDIO_FRAME_ID, studio, stamp_ns))

    @staticmethod
    def _image_msg(frame_id, data, stamp_ns):
        msg = CompressedImage()
        msg.header.stamp.sec = int(stamp_ns // 1_000_000_000)
        msg.header.stamp.nanosec = int(stamp_ns % 1_000_000_000)
        msg.header.frame_id = frame_id
        msg.format = 'jpeg'
        msg.data = data
        return msg

    def _release(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001 — a failed release must not end the loop
                pass
            self._cap = None

    # -- the state message ---------------------------------------------------

    def _tick(self):
        now = self._monotonic()
        with self._lock:
            self.verifier.tick(now)
        self._note(now)

    def _note(self, now):
        """Publish the state message on a change of the verifier's state or
        fault and once a second, and log the changes."""
        with self._lock:
            verifier = self.verifier
            changed = verifier.generation != self._seen_generation
            if not changed and self._state_at is not None and now - self._state_at < STATE_RESEND_S:
                return
            self._seen_generation = verifier.generation
            self._state_at = now
            self._state_pub.publish(
                String(data=scene_split.state_message(**verifier.state_fields()).decode('utf-8')))
            state, fault, fps, passthrough = (verifier.state, verifier.fault, verifier.fps,
                                              verifier.passthrough)
        if not changed:
            return
        if state == scene_split.STATE_STREAMING and not self._announced:
            self._announced = True
            self.get_logger().info(
                f'Turm-Kamera: 1920×1080 MJPG, {fps or 0.0:.1f} Bilder/s, '
                f'Direktdurchleitung {"aktiv" if passthrough else "aus"}')
        elif state == scene_split.STATE_FAULT and fault != scene_split.FAULT_SELFTEST:
            self.get_logger().warn(f'scene_camera: fault {fault} '
                                   f'(dropped {verifier.dropped} frames since the open)')

    def destroy_node(self):
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        super().destroy_node()


def main(argv=None):
    args = parse_args(argv)
    rclpy.init()
    node = SceneCameraNode(args.device, args.name, args.quality)

    # docker stop / the entrypoint's cleanup send SIGTERM: leave spin() so the
    # capture thread releases the camera.
    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)
    node.start()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
