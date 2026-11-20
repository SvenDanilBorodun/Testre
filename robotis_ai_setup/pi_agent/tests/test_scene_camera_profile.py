# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-07: the scene camera's profile on the Orange Pi (spec 40 S1, decisions
C6, D32).

``EDUBOTICS_SCENE_CAMERA_PROFILE`` (``tower_v1`` | ``legacy``) is a MANAGED
``.env`` key on the Pi too (13 -> 14 keys). The System tab's „Turm-Kamera
(Turm-Kit)" box sets it through ``POST /scene-camera-profile``, a maintenance
route (a teacher's or an admin's session, or a loopback peer) that takes
``_lifecycle_lock`` with the bounded wait like every mutating route;
``GET /status`` reports it. Every regenerate of the ``.env`` carries the
stored value forward, so an arm scan, a camera-role change or the leader
toggle never switches the tower camera off behind the teacher's back.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

os.environ.setdefault("EDUBOTICS_PI_AUTH_FILE", os.path.join(
    tempfile.mkdtemp(prefix="pi-auth-test-"), "pi_auth.json"))
os.environ.setdefault("EDUBOTICS_PI_CONSOLE", "")

from pi_agent import agent  # noqa: E402
from pi_agent import config_generator as cg  # noqa: E402
from pi_agent.config_generator import ArmDevice, CameraDevice, HardwareConfig  # noqa: E402
from test_pi_auth import _GatewayBase  # noqa: E402

KEY = "EDUBOTICS_SCENE_CAMERA_PROFILE"
_SPLIT_FILE = SETUP_DIR / "docker" / "open_manipulator" / "scene_split.py"


def _load_scene_split():
    spec = importlib.util.spec_from_file_location("scene_split_for_pi_agent", _SPLIT_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SPLIT = _load_scene_split()


def _both_arms():
    return HardwareConfig(
        leader=ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_LEADER"),
        follower=ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_FOLLOWER"),
        cameras=[CameraDevice(path="/dev/video0", role="gripper"),
                 CameraDevice(path="/dev/video2", role="scene")])


class _TmpEnv(unittest.TestCase):
    def setUp(self):
        self._prev_domain = os.environ.get("EDUBOTICS_ROS_DOMAIN")
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "30"
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, ".env")

    def tearDown(self):
        if self._prev_domain is None:
            os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        else:
            os.environ["EDUBOTICS_ROS_DOMAIN"] = self._prev_domain


class TheManagedKey(_TmpEnv):

    def test_the_pi_manages_fourteen_keys_and_the_profile_is_one(self):
        self.assertIn(KEY, cg.MANAGED_KEYS)
        self.assertEqual(len(cg.MANAGED_KEYS), 14)

    def test_a_fresh_env_is_legacy_in_both_generators(self):
        both = cg.generate_env_file(_both_arms(), self.path)
        self.assertEqual(both.count(f"{KEY}="), 1)
        self.assertIn(f"{KEY}=legacy\n", both)
        os.unlink(self.path)
        cloud = cg.generate_cloud_only_env(self.path)
        self.assertEqual(cloud.count(f"{KEY}="), 1)
        self.assertIn(f"{KEY}=legacy\n", cloud)

    def test_a_regenerate_carries_the_stored_profile_forward(self):
        cg.generate_env_file(_both_arms(), self.path)
        cg.upsert_env_var(KEY, "tower_v1", self.path)          # what the route writes
        content = cg.generate_env_file(_both_arms(), self.path)
        self.assertIn(f"{KEY}=tower_v1\n", content)
        self.assertEqual(content.count(f"{KEY}="), 1)
        content = cg.generate_cloud_only_env(self.path)
        self.assertIn(f"{KEY}=tower_v1\n", content)
        self.assertEqual(cg.read_env_var(KEY, self.path), "tower_v1")

    def test_a_stored_value_is_written_as_it_reads(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(f"{KEY}=\"TOWER_V1\" \n")
        content = cg.generate_env_file(_both_arms(), self.path)
        self.assertIn(f"{KEY}=tower_v1\n", content)
        self.assertEqual(content.count(f"{KEY}="), 1)

    def test_a_hand_edited_value_is_superseded_by_its_reading(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(f"{KEY}=tower\n")
        content = cg.generate_env_file(_both_arms(), self.path)
        self.assertIn(f"{KEY}=legacy\n", content)
        self.assertNotIn("=tower\n", content)

    def test_the_reader_is_the_one_value_table(self):
        values = (None, "", "tower_v1", '"tower_v1"', " tower_v1 ", "TOWER_V1", "legacy",
                  "auto", "tower", "\xa0tower_v1", "'tower_v1'", "tower_v1x", 7)
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(cg.scene_camera_profile_of(value),
                                 _SPLIT.normalise_profile(value))


class TheTowerFormat(unittest.TestCase):

    def test_the_preflight_asks_for_the_derivations_capture(self):
        # The agent ships without scene_split; its pre-flight spells the tower
        # camera's mode itself.
        self.assertEqual(agent._TOWER_CAPTURE,
                         ("MJPG", _SPLIT.CAPTURE_W, _SPLIT.CAPTURE_H, float(_SPLIT.CAPTURE_FPS)))
        self.assertEqual(set(agent._SCENE_CAMERA_PROFILES), set(_SPLIT.PROFILE_VALUES.values()))


class TheStatus(_TmpEnv):

    def test_status_reports_the_stored_profile(self):
        app = agent.AgentApp(env_file=self.path, auth_file=os.path.join(self.dir, "a.json"))
        with patch.object(agent.docker_manager, "get_container_status", return_value={}):
            code, status = app.handle_status()
        self.assertEqual(code, 200)
        self.assertEqual(status["scene_camera_profile"], "legacy")
        cg.upsert_env_var(KEY, "tower_v1", self.path)
        with patch.object(agent.docker_manager, "get_container_status", return_value={}):
            self.assertEqual(app.handle_status()[1]["scene_camera_profile"], "tower_v1")


class TheRoute(_GatewayBase):
    """``POST /scene-camera-profile {"profile": …}`` through the real server."""

    def setUp(self):
        super().setUp()
        self.env = self.app.env_file

    def post(self, body, role="teacher"):
        cookie = self.session_cookie(role)
        return self.req("POST", "/scene-camera-profile", body, {"Cookie": cookie})

    def test_the_route_is_a_maintenance_route(self):
        self.assertIn("/scene-camera-profile", agent._MAINTENANCE_POST_PATHS)

    def test_a_teacher_switches_the_tower_camera_on_and_off(self):
        for role in ("teacher", "admin"):
            for profile in ("tower_v1", "legacy"):
                with self.subTest(role=role, profile=profile):
                    status, payload, _ = self.post({"profile": profile}, role)
                    self.assertEqual(status, 200, payload)
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["scene_camera_profile"], profile)
                    self.assertEqual(cg.read_env_var(KEY, self.env), profile)
                    status, st, _ = self.req("GET", "/status",
                                             headers={"Cookie": self.session_cookie(role)})
                    self.assertEqual(st["scene_camera_profile"], profile)

    def test_a_student_gets_the_german_403_and_nothing_is_written(self):
        status, payload, _ = self.post({"profile": "tower_v1"}, "student")
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "role_not_allowed")
        self.assertIn("Lehrkraft oder ein Admin", payload["message"])
        self.assertIsNone(cg.read_env_var(KEY, self.env))

    def test_no_session_is_a_401(self):
        self.claim()
        status, payload, _ = self.req("POST", "/scene-camera-profile", {"profile": "tower_v1"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "login_required")
        self.assertIsNone(cg.read_env_var(KEY, self.env))

    def test_another_value_is_a_german_400_and_nothing_is_written(self):
        for body in ({"profile": "TOWER_V1"}, {"profile": "tower"}, {"profile": ""},
                     {"profile": None}, {"profile": 1}, {"profile": ["tower_v1"]}, {}):
            with self.subTest(body=body):
                status, payload, _ = self.post(body)
                self.assertEqual(status, 400, payload)
                self.assertFalse(payload["ok"])
                self.assertIn("Kameraprofil", payload["message"])
                self.assertIsNone(cg.read_env_var(KEY, self.env))

    def test_a_held_lifecycle_lock_is_the_german_503_after_a_bounded_wait(self):
        cookie = self.session_cookie("teacher")
        p = patch.object(agent, "_LIFECYCLE_LOCK_WAIT_S", 0.05)
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(self.app._lifecycle_lock.acquire(timeout=1))
        try:
            started = time.monotonic()
            status, payload, _ = self.req("POST", "/scene-camera-profile",
                                          {"profile": "tower_v1"}, {"Cookie": cookie})
            self.assertLess(time.monotonic() - started, 4.0)
        finally:
            self.app._lifecycle_lock.release()
        self.assertEqual(status, 503)
        self.assertEqual(payload["message"], agent.AgentApp._busy_lifecycle()[1]["message"])
        self.assertIsNone(cg.read_env_var(KEY, self.env))
        self.assertTrue(self.app._lifecycle_lock.acquire(timeout=1))
        self.app._lifecycle_lock.release()

    def test_the_handler_takes_the_lock_with_the_bounded_wait(self):
        calls = []
        real = self.app._acquire_lifecycle

        def acquire():
            calls.append(True)
            return real()
        with patch.object(self.app, "_acquire_lifecycle", acquire):
            code, _ = self.app.handle_set_scene_camera_profile({"profile": "tower_v1"})
        self.assertEqual((code, calls), (200, [True]))
        self.assertTrue(self.app._lifecycle_lock.acquire(blocking=False))
        self.app._lifecycle_lock.release()


class TheRouteOnLoopback(_GatewayBase):
    """A root process on the Pi itself needs no session."""

    trusted = True

    def test_a_loopback_peer_may_set_it(self):
        status, payload, _ = self.req("POST", "/scene-camera-profile",
                                      {"profile": "tower_v1"}, origin=None)
        self.assertEqual(status, 200, payload)
        self.assertEqual(cg.read_env_var(KEY, self.app.env_file), "tower_v1")


if __name__ == "__main__":
    unittest.main()
