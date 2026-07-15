"""Tests for the pi-agent phone-camera receiver (deps-free).

No network/sockets and no OpenCV: the frame-upload policy is a pure function,
the latest-slot is plain threading, and the openssl cert minting is mocked (so
the reuse-vs-mint branch is covered without a real openssl on CI). The ROS
republish is intentionally NOT exercised — it does not exist on the Pi
(preview-only backend; see the module's OPEN item).
"""

from __future__ import annotations

import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[2]  # robotis_ai_setup
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pi_agent import phone_camera  # noqa: E402


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
        too_big = str(phone_camera.PHONE_MAX_FRAME_BYTES + 1)
        ok, status, reason = phone_camera.validate_frame_upload(too_big)
        self.assertFalse(ok)
        self.assertEqual(status, 413)
        self.assertTrue(reason)

    def test_exact_max_accepted(self):
        ok, status, _ = phone_camera.validate_frame_upload(
            str(phone_camera.PHONE_MAX_FRAME_BYTES))
        self.assertTrue(ok)
        self.assertEqual(status, 204)

    def test_custom_max_bytes(self):
        ok, status, _ = phone_camera.validate_frame_upload("1500", max_bytes=1000)
        self.assertFalse(ok)
        self.assertEqual(status, 413)

    def test_german_reasons_have_umlauts(self):
        # Rule §1: the reject reasons are German with literal umlauts.
        _, _, r411 = phone_camera.validate_frame_upload(None)
        _, _, r413 = phone_camera.validate_frame_upload(
            str(phone_camera.PHONE_MAX_FRAME_BYTES + 1))
        self.assertIn("Länge", r411)
        self.assertIn("groß", r413)


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
        with slot._lock:
            slot._monotonic = time.monotonic() - 100.0
        self.assertFalse(slot.fresh_within(2.0))
        self.assertIsNotNone(slot.get())

    def test_concurrent_writes_are_lock_safe(self):
        slot = phone_camera.LatestFrameSlot()
        n = 300

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


# ----- cert minting (openssl mocked) -----

class TestEnsureCert(unittest.TestCase):

    def test_pems_usable_true_for_wellformed_pair(self):
        d = tempfile.mkdtemp()
        cert, key = phone_camera.cert_paths(d)
        with open(cert, "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n")
        with open(key, "w") as f:
            # Obviously-fake fixture — the structure is what _pems_usable checks.
            # openssl -nodes writes "PRIVATE KEY" (PKCS#8); accept it too. gitleaks:allow
            f.write("-----BEGIN PRIVATE KEY-----\nDEF\n-----END PRIVATE KEY-----\n")  # gitleaks:allow
        self.assertTrue(phone_camera._pems_usable(cert, key))

    def test_pems_usable_false_when_missing(self):
        d = tempfile.mkdtemp()
        cert, key = phone_camera.cert_paths(d)
        self.assertFalse(phone_camera._pems_usable(cert, key))

    def test_pems_usable_false_when_garbage(self):
        d = tempfile.mkdtemp()
        cert, key = phone_camera.cert_paths(d)
        with open(cert, "w") as f:
            f.write("not a pem")
        with open(key, "w") as f:
            f.write("also not a pem")
        self.assertFalse(phone_camera._pems_usable(cert, key))

    def test_ensure_cert_reuses_existing_pair_without_openssl(self):
        # A valid pair already exists → ensure_cert returns it WITHOUT running
        # openssl (so this passes on any CI host).
        d = tempfile.mkdtemp()
        cert, key = phone_camera.cert_paths(d)
        with open(cert, "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n")
        with open(key, "w") as f:
            f.write("-----BEGIN PRIVATE KEY-----\nDEF\n-----END PRIVATE KEY-----\n")  # gitleaks:allow
        with patch("pi_agent.phone_camera.subprocess.run") as run:
            got_cert, got_key = phone_camera.ensure_cert(d)
        run.assert_not_called()
        self.assertEqual((got_cert, got_key), (cert, key))

    def test_ensure_cert_mints_with_openssl_when_missing(self):
        d = tempfile.mkdtemp()
        cert, key = phone_camera.cert_paths(d)

        def fake_openssl(argv, **kwargs):
            # Simulate openssl writing the two PEMs.
            with open(cert, "w") as f:
                f.write("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n")
            with open(key, "w") as f:
                f.write("-----BEGIN PRIVATE KEY-----\nY\n-----END PRIVATE KEY-----\n")  # gitleaks:allow
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("pi_agent.phone_camera.subprocess.run", side_effect=fake_openssl) as run:
            got_cert, got_key = phone_camera.ensure_cert(d)
        self.assertEqual((got_cert, got_key), (cert, key))
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["openssl", "req", "-x509"])
        self.assertIn("rsa:2048", argv)
        self.assertIn(f"/CN={phone_camera._PHONE_CERT_CN}", argv)

    def test_ensure_cert_key_is_0600_regardless_of_umask(self):
        # The TLS private key must never rely on the systemd UMask alone: a
        # manual bench run mints with the ambient umask. ensure_cert must pin
        # 0600 itself after a successful mint — simulate the worst case by
        # having the fake openssl write a world-readable key.
        import os
        d = tempfile.mkdtemp()
        cert, key = phone_camera.cert_paths(d)

        def fake_openssl(argv, **kwargs):
            with open(cert, "w") as f:
                f.write("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n")
            with open(key, "w") as f:
                f.write("-----BEGIN PRIVATE KEY-----\nY\n-----END PRIVATE KEY-----\n")  # gitleaks:allow
            os.chmod(key, 0o644)  # worst-case ambient umask
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("pi_agent.phone_camera.subprocess.run", side_effect=fake_openssl):
            _, got_key = phone_camera.ensure_cert(d)
        self.assertEqual(os.stat(got_key).st_mode & 0o777, 0o600)

    def test_ensure_cert_raises_german_when_openssl_missing(self):
        d = tempfile.mkdtemp()
        with patch("pi_agent.phone_camera.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(phone_camera.PhoneCertError) as ctx:
                phone_camera.ensure_cert(d)
        self.assertIn("openssl", str(ctx.exception))

    def test_ensure_cert_raises_german_when_openssl_fails(self):
        d = tempfile.mkdtemp()
        with patch("pi_agent.phone_camera.subprocess.run",
                   return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
            with self.assertRaises(phone_camera.PhoneCertError) as ctx:
                phone_camera.ensure_cert(d)
        # German user-facing message (Rule §1).
        self.assertIn("Zertifikat", str(ctx.exception))


# ----- config defaults -----

class TestPhoneDefaults(unittest.TestCase):
    def test_https_port_default(self):
        self.assertEqual(phone_camera.PORT_PHONE_HTTPS, 8444)

    def test_max_frame_bytes_default(self):
        self.assertEqual(phone_camera.PHONE_MAX_FRAME_BYTES, 2 * 1024 * 1024)


# ----- PhoneCameraServer over a real loopback TLS socket (#24) -----

class TestPhoneCameraServerLoopback(unittest.TestCase):
    def test_default_bind_host_is_all_interfaces(self):
        # The phone must reach the receiver over the LAN — bound 0.0.0.0 by
        # design (the body-size gate is the only admission control).
        srv = phone_camera.PhoneCameraServer("cert", "key")
        self.assertEqual(srv.host, "0.0.0.0")

    def _raw_post_frame(self, port, ctx, content_length, body=b""):
        """Send a handcrafted POST /frame over TLS and return the HTTP status.

        Content-Length is set independently of ``body`` so an oversized frame is
        rejected at the header check without shipping megabytes (the server
        answers 413 before reading the body)."""
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock = ctx.wrap_socket(raw, server_hostname="edubotics-phone-cam")
        try:
            req = (
                "POST /frame HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Content-Type: image/jpeg\r\n"
                f"Content-Length: {content_length}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii") + body
            sock.sendall(req)
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
        finally:
            sock.close()
        # "HTTP/1.0 413 Payload Too Large" → 413
        return int(resp.split()[1])

    def test_oversized_rejected_and_valid_frame_stored(self):
        d = tempfile.mkdtemp()
        srv = None
        try:
            try:
                cert, key = phone_camera.ensure_cert(d)
            except phone_camera.PhoneCertError as e:
                self.skipTest(f"openssl unavailable: {e}")
            slot = phone_camera.LatestFrameSlot()
            srv = phone_camera.PhoneCameraServer(
                cert, key, slot, port=0, host="127.0.0.1")
            srv.start()
            port = srv._httpd.server_address[1]
            ctx = ssl._create_unverified_context()

            # Oversized: Content-Length past the cap → 413, nothing stored.
            status = self._raw_post_frame(
                port, ctx, phone_camera.PHONE_MAX_FRAME_BYTES + 1, body=b"")
            self.assertEqual(status, 413)
            self.assertIsNone(slot.get())

            # Valid frame: 204 + the exact bytes land in the slot.
            frame = b"\xff\xd8\xff\xe0jpeg-ish-bytes"
            status = self._raw_post_frame(port, ctx, len(frame), body=frame)
            self.assertEqual(status, 204)
            got = slot.get()
            self.assertIsNotNone(got)
            self.assertEqual(got[0], frame)
        finally:
            if srv is not None:
                srv.shutdown()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
