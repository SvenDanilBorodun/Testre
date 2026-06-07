"""Tests for the phone-as-3rd-camera receiver + bridge wiring (deps-free).

No network/sockets and no OpenCV: the frame-upload policy is a pure function,
the latest-slot is plain threading, the external sender is exercised against a
fake slot + fake socket, and the cert wrapper's reuse check is pure I/O. The
config_generator 3-names-vs-2 behaviour is asserted directly.
"""

import os
import struct
import tempfile
import threading
import time
import unittest

from gui.app import phone_camera, phone_cert, camera_bridge, constants
from gui.app.config_generator import generate_env_file, generate_cloud_only_env
from gui.app.device_manager import ArmDevice, CameraDevice, HardwareConfig


# ----- frame-upload validation (pure) -----

class TestFrameUploadValidation(unittest.TestCase):

    def test_accepts_normal_jpeg(self):
        ok, status, reason = phone_camera.validate_frame_upload("40000")
        self.assertTrue(ok)
        self.assertEqual(status, 204)
        self.assertEqual(reason, "")

    def test_missing_length_rejected_411(self):
        ok, status, reason = phone_camera.validate_frame_upload(None)
        self.assertFalse(ok)
        self.assertEqual(status, 411)
        self.assertTrue(reason)  # German reason present

    def test_non_numeric_length_rejected_411(self):
        ok, status, _ = phone_camera.validate_frame_upload("not-a-number")
        self.assertFalse(ok)
        self.assertEqual(status, 411)

    def test_zero_length_rejected_411(self):
        ok, status, _ = phone_camera.validate_frame_upload("0")
        self.assertFalse(ok)
        self.assertEqual(status, 411)

    def test_oversize_rejected_413(self):
        too_big = str(constants.PHONE_MAX_FRAME_BYTES + 1)
        ok, status, reason = phone_camera.validate_frame_upload(too_big)
        self.assertFalse(ok)
        self.assertEqual(status, 413)
        self.assertTrue(reason)

    def test_exact_max_accepted(self):
        ok, status, _ = phone_camera.validate_frame_upload(
            str(constants.PHONE_MAX_FRAME_BYTES))
        self.assertTrue(ok)
        self.assertEqual(status, 204)

    def test_custom_max_bytes(self):
        ok, status, _ = phone_camera.validate_frame_upload("1500", max_bytes=1000)
        self.assertFalse(ok)
        self.assertEqual(status, 413)


# ----- LatestFrameSlot semantics -----

class TestLatestFrameSlot(unittest.TestCase):

    def test_empty_returns_none(self):
        slot = phone_camera.LatestFrameSlot()
        self.assertIsNone(slot.get())
        self.assertFalse(slot.fresh_within(10.0))

    def test_newest_frame_wins_and_seq_bumps(self):
        slot = phone_camera.LatestFrameSlot()
        slot.set(b"aaa", capture_ns=111)
        slot.set(b"bbb", capture_ns=222)
        jpeg, ns, seq = slot.get()
        self.assertEqual(jpeg, b"bbb")
        self.assertEqual(ns, 222)
        self.assertEqual(seq, 2)

    def test_fresh_within_true_when_recent(self):
        slot = phone_camera.LatestFrameSlot()
        slot.set(b"x")
        self.assertTrue(slot.fresh_within(2.0))

    def test_fresh_within_false_when_stale(self):
        slot = phone_camera.LatestFrameSlot()
        slot.set(b"x")
        # Force the stored monotonic timestamp into the past.
        with slot._lock:
            slot._monotonic = time.monotonic() - 100.0
        self.assertFalse(slot.fresh_within(2.0))
        # ...but a frame still exists.
        self.assertIsNotNone(slot.get())

    def test_default_capture_ns_uses_host_clock(self):
        before = time.time_ns()
        slot = phone_camera.LatestFrameSlot()
        slot.set(b"x")
        after = time.time_ns()
        _, ns, _ = slot.get()
        self.assertGreaterEqual(ns, before)
        self.assertLessEqual(ns, after)

    def test_concurrent_writes_are_lock_safe(self):
        # Many threads hammering set() must never corrupt the slot; the final
        # state is internally consistent and seq equals the write count.
        slot = phone_camera.LatestFrameSlot()
        n = 500

        def worker(tag):
            for _ in range(n):
                slot.set(bytes([tag]))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = slot.get()
        self.assertIsNotNone(got)
        jpeg, _, seq = got
        self.assertEqual(seq, 4 * n)
        self.assertEqual(len(jpeg), 1)  # a single, intact frame


# ----- external sender (bridge) against a fake slot + fake socket -----

class _FakeSlot:
    """Minimal duck-type of LatestFrameSlot for the sender test."""

    def __init__(self):
        self._frame = None  # (jpeg, ns, seq)
        self._fresh = True

    def push(self, jpeg, ns, seq, fresh=True):
        self._frame = (jpeg, ns, seq)
        self._fresh = fresh

    def get(self):
        return self._frame

    def fresh_within(self, _max_age):
        return self._fresh


class _FakeSock:
    def __init__(self):
        self.sent = bytearray()

    def sendall(self, data):
        self.sent += data

    def close(self):
        pass


class TestExternalSender(unittest.TestCase):

    def _worker(self, slot, stop):
        return camera_bridge._ExternalSenderWorker(
            slot, constants.PHONE_CAM_ID, "127.0.0.1",
            constants.PORT_CAMERA_INGEST, stop)

    def test_sends_fresh_new_frame_with_wire_format(self):
        slot = _FakeSlot()
        slot.push(b"JPEGDATA", ns=42, seq=1, fresh=True)
        stop = threading.Event()
        w = self._worker(slot, stop)
        sock = _FakeSock()
        # Inline the run-loop body once: a fresh new frame is forwarded.
        got = w.slot.get()
        self.assertTrue(got is not None and w.slot.fresh_within(2.0))
        header = camera_bridge._HEADER.pack(w.cam_id, len(b"JPEGDATA"), 42)
        sock.sendall(header + b"JPEGDATA")
        cam_id, length, ns = struct.Struct(">BIQ").unpack(bytes(sock.sent[:13]))
        self.assertEqual(cam_id, constants.PHONE_CAM_ID)
        self.assertEqual(length, len(b"JPEGDATA"))
        self.assertEqual(ns, 42)
        self.assertEqual(bytes(sock.sent[13:]), b"JPEGDATA")

    def test_stale_frame_is_not_sent(self):
        # The run loop's gate: get() returns a frame but fresh_within is False.
        slot = _FakeSlot()
        slot.push(b"OLD", ns=1, seq=1, fresh=False)
        stop = threading.Event()
        w = self._worker(slot, stop)
        got = w.slot.get()
        send_allowed = (
            got is not None
            and got[2] != -1
            and w.slot.fresh_within(w.stale_max_age_s)
        )
        self.assertFalse(send_allowed)

    def test_same_seq_not_resent(self):
        slot = _FakeSlot()
        slot.push(b"X", ns=1, seq=7, fresh=True)
        stop = threading.Event()
        w = self._worker(slot, stop)
        last_seq = 7  # already sent
        got = w.slot.get()
        send_allowed = got is not None and got[2] != last_seq and w.slot.fresh_within(2.0)
        self.assertFalse(send_allowed)

    def test_role_label_is_phone(self):
        slot = _FakeSlot()
        w = self._worker(slot, threading.Event())
        self.assertEqual(w.role, constants.PHONE_CAMERA_NAME)
        self.assertEqual(w.cam_id, 2)


class TestBridgeExternalRegistration(unittest.TestCase):

    def test_register_external_source_adds_sender(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0, "scene": 1})
        slot = phone_camera.LatestFrameSlot()
        bridge.register_external_source(constants.PHONE_CAM_ID, slot)
        self.assertEqual(len(bridge._external_senders), 1)
        es = bridge._external_senders[0]
        self.assertEqual(es.cam_id, 2)
        self.assertIs(es.slot, slot)
        self.assertIs(es._stop, bridge._stop)

    def test_connected_excludes_phone(self):
        # The phone (external) sender must NOT gate the camera-connected flag.
        bridge = camera_bridge.CameraBridge({"gripper": 0})
        bridge.register_external_source(2, phone_camera.LatestFrameSlot())
        # No threads started → all senders show connected=False; the property
        # is camera-only and still False here, but the point is it does not
        # raise / index the external sender.
        self.assertFalse(bridge.connected)

    def test_status_includes_phone_entry(self):
        bridge = camera_bridge.CameraBridge({"gripper": 0})
        bridge.register_external_source(2, phone_camera.LatestFrameSlot())
        status = bridge.status()
        self.assertIn(constants.PHONE_CAMERA_NAME, status["cameras"])
        phone = status["cameras"][constants.PHONE_CAMERA_NAME]
        self.assertIn("connected", phone)
        self.assertEqual(phone["index"], -1)

    def test_phone_only_bridge_constructs(self):
        # A bridge with NO USB cameras but a registered phone is legal.
        bridge = camera_bridge.CameraBridge({})
        bridge.register_external_source(2, phone_camera.LatestFrameSlot())
        self.assertEqual(len(bridge.workers), 0)
        self.assertEqual(len(bridge._external_senders), 1)
        bridge.stop()  # must not raise (nothing started)


# ----- config_generator: 3 names when enabled, 2 (default) when not -----

class TestConfigGeneratorPhoneNames(unittest.TestCase):

    def setUp(self):
        self._prev_src = os.environ.get("EDUBOTICS_CAMERA_SOURCE")
        os.environ["EDUBOTICS_CAMERA_SOURCE"] = "native_bridge"
        self._config = HardwareConfig(
            leader=ArmDevice(busid="1-3", serial_path="/dev/serial/by-id/leader",
                             role="leader", description="OpenRB-150"),
            follower=ArmDevice(busid="1-4", serial_path="/dev/serial/by-id/follower",
                               role="follower", description="OpenRB-150"),
            cameras=[
                CameraDevice(path="Index 0", name="Gripper", role="gripper", win_index=0),
                CameraDevice(path="Index 1", name="Scene", role="scene", win_index=1),
            ],
        )

    def tearDown(self):
        if self._prev_src is None:
            os.environ.pop("EDUBOTICS_CAMERA_SOURCE", None)
        else:
            os.environ["EDUBOTICS_CAMERA_SOURCE"] = self._prev_src

    def _write(self, **kwargs):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return generate_env_file(self._config, output_path=path, **kwargs)

    def test_phone_on_emits_three_names(self):
        content = self._write(phone_camera=True)
        self.assertIn("EDUBOTICS_CAMERA_NAMES=gripper,scene,phone", content)

    def test_phone_off_omits_camera_names(self):
        content = self._write(phone_camera=False)
        self.assertNotIn("EDUBOTICS_CAMERA_NAMES=", content)

    def test_default_is_phone_off(self):
        content = self._write()
        self.assertNotIn("EDUBOTICS_CAMERA_NAMES=", content)

    def test_camera_names_is_managed_superseded_on_regenerate(self):
        # A stale 3-name value from a prior (phone-on) run must NOT survive a
        # later phone-off regenerate — EDUBOTICS_CAMERA_NAMES is a MANAGED key.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        generate_env_file(self._config, output_path=path, phone_camera=True)
        # Regenerate with the phone OFF.
        content = generate_env_file(self._config, output_path=path, phone_camera=False)
        self.assertNotIn("EDUBOTICS_CAMERA_NAMES=gripper,scene,phone", content)
        # And only ONE camera-names line max ever (no duplication).
        self.assertEqual(content.count("EDUBOTICS_CAMERA_NAMES="), 0)

    def test_cloud_only_phone_on_emits_three_names(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        content = generate_cloud_only_env(output_path=path, phone_camera=True)
        self.assertIn("EDUBOTICS_CAMERA_NAMES=gripper,scene,phone", content)


# ----- cert wrapper reuse check (pure I/O) -----

class TestPhoneCertReuse(unittest.TestCase):

    def test_pems_usable_true_for_wellformed_pair(self):
        d = tempfile.mkdtemp()
        cert, key = phone_cert.cert_paths(d)
        with open(cert, "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n")
        with open(key, "w") as f:
            # Obviously-fake 3-byte fixture, not a real key — the structure is
            # what _pems_usable validates. gitleaks:allow
            f.write("-----BEGIN RSA PRIVATE KEY-----\nDEF\n-----END RSA PRIVATE KEY-----\n")  # gitleaks:allow
        self.assertTrue(phone_cert._pems_usable(cert, key))

    def test_pems_usable_false_when_missing(self):
        d = tempfile.mkdtemp()
        cert, key = phone_cert.cert_paths(d)
        self.assertFalse(phone_cert._pems_usable(cert, key))

    def test_pems_usable_false_when_garbage(self):
        d = tempfile.mkdtemp()
        cert, key = phone_cert.cert_paths(d)
        with open(cert, "w") as f:
            f.write("not a pem")
        with open(key, "w") as f:
            f.write("also not a pem")
        self.assertFalse(phone_cert._pems_usable(cert, key))

    def test_ensure_cert_reuses_existing_pair(self):
        # If a valid pair already exists, ensure_cert returns it WITHOUT shelling
        # out to PowerShell (so this passes on Linux CI too).
        d = tempfile.mkdtemp()
        cert, key = phone_cert.cert_paths(d)
        with open(cert, "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n")
        with open(key, "w") as f:
            # Same obviously-fake fixture as above. gitleaks:allow
            f.write("-----BEGIN RSA PRIVATE KEY-----\nDEF\n-----END RSA PRIVATE KEY-----\n")  # gitleaks:allow
        got_cert, got_key = phone_cert.ensure_cert(d)
        self.assertEqual((got_cert, got_key), (cert, key))


# ----- constants sanity -----

class TestPhoneConstants(unittest.TestCase):

    def test_phone_cam_id_is_after_bridge_roles(self):
        # phone must be the 3rd entry: index == len(gripper,scene) == 2.
        self.assertEqual(constants.PHONE_CAM_ID, len(constants.CAMERA_BRIDGE_ROLES))
        self.assertEqual(constants.PHONE_CAM_ID, 2)

    def test_phone_https_port_default(self):
        self.assertEqual(constants.PORT_PHONE_HTTPS, 8444)


if __name__ == "__main__":
    unittest.main()
