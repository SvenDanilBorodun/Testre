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

usb_cam is still used on the native-Linux / Jetson path (real USB host, no
bridge); the entrypoint selects between the two via EDUBOTICS_CAMERA_SOURCE.

Wire protocol (GUI = client, this node = server)
------------------------------------------------
The GUI connects to this node and writes a stream of length-prefixed frames.
A single multiplexed connection carries all cameras, disambiguated by a
1-byte camera id:

    [ uint8  cam_id ][ uint32 BE jpeg_len ][ uint64 BE capture_unix_nanos ][ jpeg_len bytes JPEG ]

cam_id indexes EDUBOTICS_CAMERA_NAMES (default "gripper,scene") so id 0 ->
/gripper/image_raw/compressed and id 1 -> /scene/image_raw/compressed.
capture_unix_nanos is carried for diagnostics only; the published header.stamp
uses the node clock at receipt (avoids host/container clock skew, and recorded
LeRobot timestamps are regularized to frame_index/fps at save anyway).

The published CompressedImage uses BEST_EFFORT / KEEP_LAST QoS to match the
recorder's subscriber (multi_subscriber.py: depth=1 BEST_EFFORT KEEP_LAST) and
web_video_server. format="jpeg"; the bytes are standard JPEG so cv_bridge's
`compressed_imgmsg_to_cv2(passthrough)` decodes them to BGR exactly as it did
for usb_cam's compressed topic.
"""

import os
import socket
import struct
import threading

import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage

# Header layout: cam_id (B), jpeg_len (I, big-endian), capture_ns (Q, big-endian)
_HEADER = struct.Struct('>BIQ')
_HEADER_LEN = _HEADER.size
# Reject absurd frame sizes early — a 640x480 JPEG is ~30-60 KB; cap at 8 MB
# so a desynced/garbage stream can't make us allocate gigabytes.
_MAX_JPEG_BYTES = 8 * 1024 * 1024


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
        qos = QoSProfile(
            depth=5,
            reliability=ReliabilityPolicy.BEST_EFFORT,
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

        self._stop = threading.Event()
        self._frame_counts = {cam_id: 0 for cam_id in self.publishers_by_id}
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
        """Accept loop. One client at a time (the GUI bridge); on disconnect
        we go back to accepting so a GUI restart reconnects cleanly."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('0.0.0.0', self.port))
        srv.listen(1)
        srv.settimeout(1.0)
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.get_logger().info(f'camera bridge connected from {addr}')
            try:
                conn.settimeout(5.0)
                # Disable Nagle — we want each JPEG frame out immediately.
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._handle_client(conn)
            except Exception as exc:  # noqa: BLE001 — log and keep serving
                self.get_logger().warn(f'camera bridge connection ended: {exc}')
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            self.get_logger().info('camera bridge disconnected — awaiting reconnect')
        try:
            srv.close()
        except OSError:
            pass

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
            self._publish(cam_id, payload)

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

    def _publish(self, cam_id, jpeg_bytes):
        entry = self.publishers_by_id.get(cam_id)
        if entry is None:
            # Unknown camera id — log once-ish and drop.
            self.get_logger().warn(f'dropping frame for unknown cam_id={cam_id}')
            return
        name, pub = entry
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = name
        msg.format = 'jpeg'
        msg.data = jpeg_bytes
        pub.publish(msg)
        self._frame_counts[cam_id] += 1

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
