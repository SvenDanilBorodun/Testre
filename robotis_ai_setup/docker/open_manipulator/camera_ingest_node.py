#!/usr/bin/env python3
"""EduBotics native-camera ingest node (WSL2/Windows student path).

Why this exists
---------------
On the Windows student PC the two USB cameras are NOT attached to the WSL2
distro via usbipd. The WSL2 `vhci_hcd` USB/IP bridge caps each UVC camera at
~6-10 Hz (per-device isochronous round-trip latency — benchmarked 2026-05-23,
not contention/bandwidth/CPU) and the camera traffic jitters the Dynamixel
100 Hz serial reads on the shared bridge. Instead, the GUI process captures
both cameras NATIVELY on Windows (OpenCV/MSMF, full 30 fps) and streams JPEG
frames over a localhost TCP socket into this node, which republishes them as
`sensor_msgs/CompressedImage` on the exact same topics usb_cam used. Every
downstream consumer (LeRobot recorder, inference, web_video_server browser
preview, Roboter Studio CV) is unchanged — only the publisher changed.

usb_cam is still used on the native-Linux path (real USB host, no
bridge); the entrypoint selects between the two via EDUBOTICS_CAMERA_SOURCE.

Wire protocol (GUI = client, this node = server)
------------------------------------------------
The GUI connects to this node and writes a stream of length-prefixed frames.
The GUI opens ONE connection per camera (decoupling them so one camera's stall
can't delay the other); each frame is still self-describing via a 1-byte camera
id, so a single multiplexed connection (older GUI) is also accepted and demuxed:

    [ uint8  cam_id ][ uint32 BE jpeg_len ][ uint64 BE capture_unix_nanos ][ jpeg_len bytes JPEG ]

cam_id indexes EDUBOTICS_CAMERA_NAMES (default "gripper,scene") so id 0 ->
/gripper/image_raw/compressed and id 1 -> /scene/image_raw/compressed.
capture_unix_nanos becomes the published header.stamp (see _publish): the two
cameras stream on independent connections, so the GUI's true capture time —
both from the same host clock — lets the recorder's camera pairer align the
pair instead of falling back to latest-per-camera. It's only ever compared
camera-to-camera and is regularized to frame_index/fps at dataset save.

Three more ids belong to the tower scene camera (scene_split.py, COPY'd next
to this file; spec 40 S1). They exist in every profile, so the two publishers
below are silent wherever no GUI sends them:
  * 255, the hello: the GUI's senders for ids 16 and 17 open with
    [255][len 1][capture_ns 0][0x01] and wait for the one byte HELLO, which
    this node writes back on the same connection; the frame is published
    nowhere. An older image answers nothing, which is how the GUI tells it is
    older. No other frame is ever answered, so a connection that sends no
    hello (ids 0, 1, 2) is never written to.
  * 16 -> STUDIO_TOPIC: the tower camera's own picture for Roboter Studio,
    frame_id "scene_studio", stamped like the cameras.
  * 17 -> STATE_TOPIC: the camera's state, UTF-8 JSON (one object, at most
    STATE_MAX_BYTES), latched; anything else is dropped.
With EDUBOTICS_SCENE_CAMERA_PROFILE=tower_v1 the recorded scene picture on
id 1 must be exactly 640x480 (read from its JPEG header) or it is dropped
(guard G7b); with legacy, the default and every other value, id 1 is
published as before. A dropped frame and an unknown id are logged on the
first occurrence and then at most once per 10 s per id, with the count.

The published CompressedImage uses RELIABLE / KEEP_LAST QoS. A RELIABLE
publisher is the only one compatible with BOTH consumers: web_video_server
(the Aufnahme-tab preview on :8080) subscribes RELIABLE-only with no QoS
override in this build, and the recorder/inference subscribers
(multi_subscriber.py: depth=1 BEST_EFFORT KEEP_LAST) accept a RELIABLE
publisher fine (a BEST_EFFORT subscriber matches a RELIABLE publisher; the
reverse — our previous BEST_EFFORT publisher — silently dropped every frame to
web_video_server with "requesting incompatible QoS"). usb_cam's compressed
topic was RELIABLE-compatible, which is why the preview worked before the
native bridge. format="jpeg"; the bytes are standard JPEG so cv_bridge's
`compressed_imgmsg_to_cv2(passthrough)` decodes them to BGR exactly as it did
for usb_cam's compressed topic.
"""

import json
import os
import socket
import struct
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

# Header layout: cam_id (B), jpeg_len (I, big-endian), capture_ns (Q, big-endian)
_HEADER = struct.Struct('>BIQ')
_HEADER_LEN = _HEADER.size
# Reject absurd frame sizes early — a 640x480 JPEG is ~30-60 KB; cap at 8 MB
# so a desynced/garbage stream can't make us allocate gigabytes.
_MAX_JPEG_BYTES = 8 * 1024 * 1024
# A dropped frame (an id with no publisher, a refused size or state) is warned
# about on its first occurrence, then at most once per id in this interval,
# with the count.
_DROP_WARN_INTERVAL_S = 10.0
# The studio picture's header.frame_id (the server's StudioCamera camera name).
_STUDIO_FRAME_ID = 'scene_studio'


def _refuse_json_constant(name):
    raise ValueError(f'{name} is not JSON')


class CameraIngestNode(Node):

    def __init__(self):
        super().__init__('camera_ingest')

        self.port = int(os.environ.get('EDUBOTICS_CAMERA_INGEST_PORT', '5557'))
        names_raw = os.environ.get('EDUBOTICS_CAMERA_NAMES', 'gripper,scene')
        self.camera_names = [n.strip() for n in names_raw.split(',') if n.strip()]
        if not self.camera_names:
            self.camera_names = ['gripper', 'scene']

        # One publisher per camera. Topic mirrors usb_cam's remapped
        # /<name>/image_raw/compressed exactly (see omx_f_config.yaml
        # camera_topic_list and physical_ai_server communicator subscribers).
        # RELIABLE so web_video_server (RELIABLE-only) receives frames; the
        # BEST_EFFORT recorder/inference subscribers match a RELIABLE publisher
        # too. KEEP_LAST depth bounds history so a slow consumer can't make the
        # publisher block. See the module docstring for the full rationale.
        qos = QoSProfile(
            depth=5,
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
        )
        self.publishers_by_id = {}
        for cam_id, name in enumerate(self.camera_names):
            topic = f'/{name}/image_raw/compressed'
            self.publishers_by_id[cam_id] = (
                name,
                self.create_publisher(CompressedImage, topic, qos),
            )
            self.get_logger().info(f'camera_ingest publisher {cam_id}: {topic}')

        # The tower camera's two topics (module docstring), created in every
        # profile so they never appear or vanish with the camera.
        self._studio_pub = self.create_publisher(
            CompressedImage, scene_split.STUDIO_TOPIC,
            QoSProfile(depth=2, reliability=ReliabilityPolicy.RELIABLE,
                       history=HistoryPolicy.KEEP_LAST))
        self._state_pub = self.create_publisher(
            String, scene_split.STATE_TOPIC,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                       history=HistoryPolicy.KEEP_LAST,
                       durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.scene_profile = scene_split.normalise_profile(
            os.environ.get('EDUBOTICS_SCENE_CAMERA_PROFILE'))
        self.get_logger().info(f'camera_ingest scene camera profile: {self.scene_profile}')

        self._stop = threading.Event()
        self._frame_counts = {cam_id: 0 for cam_id in self.publishers_by_id}
        # cam_id -> [monotonic time of its last warning, frames dropped since].
        # The client handlers are threads of their own, hence the lock.
        self._drops = {}
        self._drops_lock = threading.Lock()
        self._server_thread = threading.Thread(
            target=self._serve_forever, name='ingest-tcp', daemon=True)
        self._server_thread.start()
        # Periodic Hz log so `docker logs open_manipulator` shows the feed is
        # alive (and at what rate) without needing a ROS shell.
        self.create_timer(10.0, self._log_rates)
        self._last_counts = dict(self._frame_counts)
        self.get_logger().info(
            f'camera_ingest listening on 0.0.0.0:{self.port} '
            f'for {len(self.camera_names)} camera(s): {self.camera_names}')

    # ----- TCP server -----
    def _serve_forever(self):
        """Accept loop. The GUI bridge opens ONE connection per camera (so a
        stall on one camera can't delay the other), so we accept multiple
        concurrent clients and serve each on its own handler thread. A single
        multiplexed client (older GUI) still works — its one handler demuxes
        both cam_ids by header. Each frame is self-describing (cam_id in the
        header), so demuxing is per-frame regardless of connection count; the
        per-cam_id publishers are distinct, so concurrent handlers publish to
        different publishers without contention. On disconnect we keep
        accepting so a GUI restart reconnects cleanly."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('0.0.0.0', self.port))
        # Backlog ≥ camera count (+1 slack) so simultaneous per-camera connects
        # on (re)connect are never refused.
        srv.listen(max(2, len(self.camera_names) + 1))
        srv.settimeout(1.0)
        handlers = []
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                handlers = [t for t in handlers if t.is_alive()]
                continue
            except OSError:
                break
            self.get_logger().info(f'camera bridge connected from {addr}')
            t = threading.Thread(
                target=self._client_handler, args=(conn, addr),
                name=f'ingest-client-{addr[1]}', daemon=True)
            t.start()
            handlers.append(t)
        try:
            srv.close()
        except OSError:
            pass

    def _client_handler(self, conn, addr):
        """Serve one client connection until it closes or stops."""
        try:
            conn.settimeout(5.0)
            # Disable Nagle — we want each JPEG frame out immediately.
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._handle_client(conn)
        except Exception as exc:  # noqa: BLE001 — log and keep serving
            self.get_logger().warn(f'camera bridge connection {addr} ended: {exc}')
        finally:
            try:
                conn.close()
            except OSError:
                pass
        self.get_logger().info(
            f'camera bridge {addr} disconnected — awaiting reconnect')

    def _handle_client(self, conn):
        while not self._stop.is_set():
            header = self._recv_exact(conn, _HEADER_LEN)
            if header is None:
                return  # peer closed cleanly
            cam_id, jpeg_len, capture_ns = _HEADER.unpack(header)
            if jpeg_len == 0 or jpeg_len > _MAX_JPEG_BYTES:
                raise ValueError(f'implausible jpeg_len={jpeg_len} (stream desync?)')
            payload = self._recv_exact(conn, jpeg_len)
            if payload is None:
                return
            if cam_id == scene_split.HELLO_CAM_ID:
                # The only frame ever answered: this image speaks ids 16/17.
                conn.sendall(scene_split.HELLO)
                continue
            self._publish(cam_id, payload, capture_ns)

    @staticmethod
    def _recv_exact(conn, n):
        """Read exactly n bytes; return None on clean EOF."""
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = conn.recv(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    def _publish(self, cam_id, jpeg_bytes, capture_ns=0):
        if cam_id == scene_split.STUDIO_CAM_ID:
            self._studio_pub.publish(self._image_msg(_STUDIO_FRAME_ID, jpeg_bytes, capture_ns))
            return
        if cam_id == scene_split.STATE_CAM_ID:
            self._publish_state(jpeg_bytes)
            return
        entry = self.publishers_by_id.get(cam_id)
        if entry is None:
            # Unknown camera id: drop the frame. Warning on every frame flooded
            # the log at the camera's rate (D21).
            self._drop_frame(cam_id, f'unknown cam_id={cam_id}')
            return
        name, pub = entry
        if cam_id == scene_split.SCENE_CAM_ID and self.scene_profile == scene_split.PROFILE_TOWER:
            # G7b: the tower profile records only the GUI's 640x480 cut of the
            # tower camera; any other size is not that picture.
            size = scene_split.jpeg_size(jpeg_bytes)
            if size != (scene_split.OUT_W, scene_split.OUT_H):
                shown = 'size unreadable' if size is None else f'{size[0]}x{size[1]}'
                self._drop_frame(cam_id, f'cam_id={cam_id} ({shown}; the tower profile '
                                         f'records {scene_split.OUTSIDE} only)')
                return
        pub.publish(self._image_msg(name, jpeg_bytes, capture_ns))
        self._frame_counts[cam_id] += 1

    def _image_msg(self, frame_id, jpeg_bytes, capture_ns):
        msg = CompressedImage()
        # Stamp with the GUI's true CAPTURE time (capture_ns from the wire), not
        # the receipt time. The two cameras are captured + streamed on
        # INDEPENDENT threads/connections, so receipt times carry independent
        # network/thread jitter; the recorder's multi-camera pairer
        # (communicator.py::_pick_synced_camera_msgs, 15 ms slop) compares the
        # two cameras' header.stamps to each other, so capture time — both from
        # the same Windows host clock — lets it actually align the pair instead
        # of falling back to latest-per-camera. capture_ns is unix-epoch ns
        # (time.time_ns() on the host; WSL keeps the container clock in step),
        # only ever compared camera-to-camera here, and is discarded at dataset
        # save (LeRobot regularizes to frame_index/fps), so the host/container
        # clock distinction is immaterial downstream. Fall back to the node
        # clock if a client sends 0 (older GUI / desync guard).
        if capture_ns:
            msg.header.stamp.sec = int(capture_ns // 1_000_000_000)
            msg.header.stamp.nanosec = int(capture_ns % 1_000_000_000)
        else:
            msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = frame_id
        msg.format = 'jpeg'
        msg.data = jpeg_bytes
        return msg

    def _publish_state(self, payload):
        """Camera id 17: the tower camera's state, republished verbatim (its
        key order intact) when it is ONE JSON object in UTF-8 of at most
        STATE_MAX_BYTES. Strict JSON: NaN and Infinity, which Python's parser
        takes and a browser's JSON.parse refuses, are dropped too."""
        cam_id = scene_split.STATE_CAM_ID
        if len(payload) > scene_split.STATE_MAX_BYTES:
            self._drop_frame(cam_id, f'cam_id={cam_id} (state message of {len(payload)} bytes, '
                                     f'more than {scene_split.STATE_MAX_BYTES})')
            return
        try:
            text = payload.decode('utf-8')
            state = json.loads(text, parse_constant=_refuse_json_constant)
        except (ValueError, RecursionError):
            state = None
        if not isinstance(state, dict):
            self._drop_frame(cam_id, f'cam_id={cam_id} (the state message is not one JSON object)')
            return
        self._state_pub.publish(String(data=text))

    def _drop_frame(self, cam_id, what):
        """Log a dropped frame: the first one of an id, then at most one line
        per id and _DROP_WARN_INTERVAL_S with the count since."""
        now = time.monotonic()
        warning = None
        with self._drops_lock:
            seen = self._drops.get(cam_id)
            if seen is None:
                self._drops[cam_id] = [now, 0]
                warning = f'dropping frame for {what}'
            else:
                seen[1] += 1
                if now - seen[0] >= _DROP_WARN_INTERVAL_S:
                    warning = f'dropped {seen[1]} frames for {what} in the last {now - seen[0]:.0f} s'
                    seen[0], seen[1] = now, 0
        if warning is not None:
            self.get_logger().warn(warning)

    def _log_rates(self):
        parts = []
        for cam_id, (name, _pub) in self.publishers_by_id.items():
            delta = self._frame_counts[cam_id] - self._last_counts.get(cam_id, 0)
            parts.append(f'{name}={delta / 10.0:.1f}Hz')
            self._last_counts[cam_id] = self._frame_counts[cam_id]
        self.get_logger().info('camera_ingest rates: ' + ', '.join(parts))

    def destroy_node(self):
        self._stop.set()
        super().destroy_node()


def main():
    rclpy.init()
    node = CameraIngestNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
