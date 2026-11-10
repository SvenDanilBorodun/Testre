# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""The robot's public token key behind the robot login (VB-03, 2026-11-15).

``pi_agent/robot_key.py`` reads the file the ROS node writes through the opi
compose's bind mount; ``GET /robot-key`` serves it to a robot session only;
``docker_manager._compose_up`` creates the host directory before the robot
tier comes up. Stdlib only (no `cryptography`: the test points are made with a
tiny pure-Python P-256 multiplication, only to get real points).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

from pi_agent import agent, docker_manager, pi_auth, robot_key  # noqa: E402

# ── a real P-256 point (stdlib affine arithmetic; test-only) ────────────────
_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_A = _P - 3
_G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
      0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and (p1[1] + p2[1]) % _P == 0:
        return None
    if p1 == p2:
        lam = (3 * p1[0] * p1[0] + _A) * pow(2 * p1[1], -1, _P) % _P
    else:
        lam = (p2[1] - p1[1]) * pow(p2[0] - p1[0], -1, _P) % _P
    x = (lam * lam - p1[0] - p2[0]) % _P
    return x, (lam * (p1[0] - x) - p1[1]) % _P


def _point(d: int) -> str:
    acc, add = None, _G
    while d:
        if d & 1:
            acc = _add(acc, add)
        add = _add(add, add)
        d >>= 1
    raw = b"\x04" + acc[0].to_bytes(32, "big") + acc[1].to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _kid(pub: str) -> str:
    return hashlib.sha256(base64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))).hexdigest()[:16]


PUB = _point(0x1234567)
KID = _kid(PUB)
OTHER_PUB = _point(0x7654321)


def _payload(**override):
    data = {"v": 1, "enc": "enc1", "kid": KID, "pub": PUB}
    data.update(override)
    return data


class _DirBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.dir = os.path.join(self.root, "edubotics-robot-key")
        p = patch.dict(os.environ, {"EDUBOTICS_ROBOT_KEY_DIR": self.dir})
        p.start()
        self.addCleanup(p.stop)

    def write(self, data, raw=None):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "hf_token_key.json"), "wb") as f:
            f.write(raw if raw is not None else json.dumps(data).encode("ascii"))


class TestReadKey(_DirBase):
    def test_the_node_file_is_served_as_its_three_keys(self):
        self.write(_payload())
        self.assertEqual(robot_key.read_key(), {"enc": "enc1", "kid": KID, "pub": PUB})

    def test_no_directory_or_no_file_is_no_key(self):
        self.assertIsNone(robot_key.read_key())
        os.makedirs(self.dir)
        self.assertIsNone(robot_key.read_key())

    def test_every_deviation_from_the_schema_is_no_key(self):
        bad = [
            _payload(v=2),
            _payload(enc="enc2"),
            _payload(kid=KID.upper()),
            _payload(kid=KID[:15]),
            _payload(pub=PUB[:86]),
            _payload(pub=OTHER_PUB),  # a kid that is not the point's hash
            _payload(kid=_kid(OTHER_PUB)[:15] + "x"),
            {"v": 1, "enc": "enc1", "kid": KID},  # a key missing
            dict(_payload(), extra=1),  # a key too many
            _payload(kid=123),
            [1, 2],
        ]
        for data in bad:
            with self.subTest(data=data):
                self.write(data)
                self.assertIsNone(robot_key.read_key())

    def test_garbage_oversized_and_a_compressed_point_are_no_key(self):
        for raw in (b"not json", b"\xff\xfe", b"{" + b" " * 5000 + b"}"):
            with self.subTest(raw=raw[:10]):
                self.write(None, raw=raw)
                self.assertIsNone(robot_key.read_key())
        compressed = base64.urlsafe_b64encode(
            b"\x02" + base64.urlsafe_b64decode(PUB + "=")[1:33] + b"\x00" * 32).decode().rstrip("=")
        self.write(_payload(pub=compressed, kid=_kid(compressed)))
        self.assertIsNone(robot_key.read_key())

    def test_a_symlink_is_never_followed(self):
        os.makedirs(self.dir)
        target = os.path.join(self.root, "elsewhere.json")
        with open(target, "w", encoding="ascii") as f:
            json.dump(_payload(), f)
        os.symlink(target, os.path.join(self.dir, "hf_token_key.json"))
        self.assertIsNone(robot_key.read_key())


class TestReadKeyNeverBlocks(_DirBase):
    """VC-04 (verification round C): ``read_key`` runs on the agent's request
    thread, so nothing at the key path may block it. A FIFO hung it forever
    (``open`` before ``fstat``, no ``O_NONBLOCK``), one leaked thread per
    ``GET /robot-key``."""

    def path(self):
        os.makedirs(self.dir, exist_ok=True)
        return os.path.join(self.dir, "hf_token_key.json")

    def read_bounded(self, seconds=3.0):
        """``read_key`` on a thread; (finished, result)."""
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("r", robot_key.read_key()), daemon=True)
        t.start()
        t.join(seconds)
        return (not t.is_alive()), box.get("r", "unset")

    def test_a_fifo_at_the_key_path_answers_none_at_once(self):
        fifo = self.path()
        os.mkfifo(fifo)

        def release():
            # A read_key that hangs in open() waits for a writer: give it one,
            # so a failing run does not leave the thread blocked behind.
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass
        self.addCleanup(release)
        finished, result = self.read_bounded()
        self.assertTrue(finished, "read_key blocked on a FIFO")
        self.assertIsNone(result)

    def test_a_fifo_with_a_writer_holding_it_open_answers_none(self):
        fifo = self.path()
        os.mkfifo(fifo)
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(fifo, os.O_WRONLY)  # a writer that never writes
        self.addCleanup(os.close, reader)
        self.addCleanup(os.close, writer)
        finished, result = self.read_bounded()
        self.assertTrue(finished, "read_key blocked reading a FIFO")
        self.assertIsNone(result)

    def test_a_directory_at_the_key_path_answers_none(self):
        os.makedirs(self.path())
        finished, result = self.read_bounded()
        self.assertTrue(finished)
        self.assertIsNone(result)

    def test_a_symlink_to_a_fifo_answers_none_without_blocking(self):
        os.makedirs(self.dir, exist_ok=True)
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo)
        os.symlink(fifo, self.path())
        self.addCleanup(lambda: os.path.exists(fifo) and os.unlink(fifo))
        finished, result = self.read_bounded()
        self.assertTrue(finished)
        self.assertIsNone(result)

    def test_an_oversized_file_is_refused_before_reading_it(self):
        # A sparse file of 1 GB: a reader that did not check the size first
        # would allocate for it.
        with open(self.path(), "wb") as f:
            f.truncate(1 << 30)
        finished, result = self.read_bounded()
        self.assertTrue(finished)
        self.assertIsNone(result)

    def test_valid_json_padded_past_the_bound_is_refused(self):
        raw = json.dumps(_payload()).encode("ascii") + b" " * (robot_key.MAX_FILE_BYTES + 1)
        self.write(None, raw=raw)
        self.assertIsNone(robot_key.read_key())

    def test_the_key_is_opened_nonblocking_without_following_links(self):
        self.write(_payload())
        seen = []
        real_open = os.open

        def spy(path, flags, *args):
            if str(path).endswith("hf_token_key.json"):
                seen.append(flags)
            return real_open(path, flags, *args)
        with patch.object(robot_key.os, "open", spy):
            self.assertEqual(robot_key.read_key()["kid"], KID)
        self.assertEqual(len(seen), 1)
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
            with self.subTest(flag=name):
                self.assertTrue(seen[0] & getattr(os, name), name)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "mknod needs root")
    def test_a_character_device_at_the_key_path_answers_none(self):
        import stat as _stat
        os.mknod(self.path(), 0o600 | _stat.S_IFCHR, os.makedev(1, 3))  # /dev/null's numbers
        finished, result = self.read_bounded()
        self.assertTrue(finished)
        self.assertIsNone(result)


class TestEnsureDir(_DirBase):
    def test_creates_the_missing_directory_0755(self):
        self.assertTrue(robot_key.ensure_dir())
        self.assertTrue(os.path.isdir(self.dir))
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o755 & ~_umask())
        self.assertTrue(robot_key.ensure_dir())  # idempotent

    def test_a_file_in_its_place_is_reported_not_replaced(self):
        with open(self.dir, "w", encoding="ascii") as f:
            f.write("x")
        lines = []
        self.assertFalse(robot_key.ensure_dir(log=lines.append))
        self.assertTrue(os.path.isfile(self.dir))
        self.assertEqual(len(lines), 1)

    def test_the_german_lines_carry_no_english_exception_name(self):
        """The start job's progress log is German (Rule §1): a failing mkdir or
        lstat is said in German only, never with Python's class name
        (verification round C, VC-11)."""
        for target, error in (("makedirs", PermissionError("/run is read-only")),
                              ("lstat", OSError("I/O error"))):
            lines = []
            with self.subTest(target=target):
                if target == "lstat":
                    p = patch.object(robot_key.os, "lstat", side_effect=error)
                else:
                    p = patch.object(robot_key.os, "makedirs", side_effect=error)
                with p:
                    self.assertFalse(robot_key.ensure_dir(log=lines.append))
                self.assertEqual(len(lines), 1)
                self.assertIn("Schlüsselordner", lines[0])
                for english in ("PermissionError", "OSError", "Error", "read-only", "I/O"):
                    self.assertNotIn(english, lines[0])

    def test_compose_up_of_the_server_creates_it_first(self):
        calls = []

        def fake_run(cmd, **_kw):
            calls.append(("run", os.path.isdir(self.dir)))
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        with patch.object(docker_manager.subprocess, "run", side_effect=fake_run):
            self.assertTrue(docker_manager._compose_up("open_manipulator", "physical_ai_server",
                                                       "code_runner"))
        self.assertEqual(calls, [("run", True)])  # the directory existed when compose ran

    def test_compose_up_of_the_manager_alone_does_not(self):
        with patch.object(docker_manager.subprocess, "run",
                          return_value=type("R", (), {"returncode": 0, "stderr": ""})()):
            docker_manager._compose_up("physical_ai_manager")
        self.assertFalse(os.path.exists(self.dir))

    def test_the_default_path_is_the_compose_mount(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EDUBOTICS_ROBOT_KEY_DIR", None)
            self.assertEqual(robot_key.key_dir(), "/run/edubotics-robot-key")
            self.assertEqual(robot_key.key_file(), "/run/edubotics-robot-key/hf_token_key.json")


def _umask():
    old = os.umask(0)
    os.umask(old)
    return old


class TestTheRoute(_DirBase):
    """GET /robot-key on a real agent server, peer treated as the gateway."""

    def setUp(self):
        super().setUp()
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state, True)
        self.app = agent.AgentApp(env_file=os.path.join(self.state, ".env"),
                                  auth_file=os.path.join(self.state, "pi_auth.json"))
        self.app._auth = pi_auth.AuthStore(os.path.join(self.state, "pi_auth.json"), iterations=1000)
        p = patch.object(agent.AgentApp, "peer_is_trusted", staticmethod(lambda _ip: False))
        p.start()
        self.addCleanup(p.stop)
        self.httpd = agent.ThreadingHTTPServer(("127.0.0.1", 0), self.app._make_handler())
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def get(self, cookie=None):
        r = urllib.request.Request(self.base + "/robot-key")
        if cookie:
            r.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read()), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def cookie(self, role="student"):
        # redeem_with_cloud answers (status, role, user id, session_max_s).
        if not self.app._auth.claimed:
            code = self.app._auth.mint_code()
            with patch.object(agent.pi_auth, "redeem_with_cloud",
                              return_value=("ok", "teacher", "u-teacher", 900)):
                self.post("/auth/claim", {"code": pi_auth.format_code(code), "ticket": "t" * 43})
        with patch.object(agent.pi_auth, "redeem_with_cloud", return_value=("ok", role, f"u-{role}", 900)):
            headers = self.post("/auth/session", {"ticket": "t" * 43})
        return headers.get("Set-Cookie", "").split(";")[0]

    def post(self, path, body):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("Origin", "http://127.0.0.1")
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.headers

    def test_without_a_session_it_is_refused_like_every_lesson_route(self):
        self.write(_payload())
        status, payload, _ = self.get()
        self.assertEqual(status, 401)
        self.assertNotIn("key", payload)

    def test_a_session_of_any_role_reads_the_node_key(self):
        self.write(_payload())
        for role in ("student", "teacher"):
            with self.subTest(role=role):
                status, payload, headers = self.get(self.cookie(role))
                self.assertEqual(status, 200)
                self.assertEqual(payload, {"ok": True, "key": {"enc": "enc1", "kid": KID, "pub": PUB}})
                self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_no_file_is_an_explicit_null(self):
        status, payload, _ = self.get(self.cookie())
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "key": None})


if __name__ == "__main__":
    unittest.main()
