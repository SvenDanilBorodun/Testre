# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""WP-07: „Umgebung starten" asks the scene camera what it can deliver
(spec 40 S1, decisions C6 and D1).

Before the robot tier starts, the agent reads the scene-role camera's format
list with one bounded ``v4l2-ctl --device=<d> --list-formats-ext``
(``camera_enum.list_formats``):

* ``tower_v1`` needs MJPG 1920x1080 at 30 fps or more; a list that proves the
  camera cannot deliver it refuses the start with a German 409 and no container
  is started.
* ``legacy`` is NEVER refused (a refusal could stop a rig that records today,
  D1). When the list proves the camera does not offer the format the compose
  hands usb_cam (``EDUBOTICS_CAMERA_PIXEL_FORMAT`` yuyv -> YUYV, mjpeg2rgb ->
  MJPG, ``EDUBOTICS_CAMERA_WIDTH`` x ``_HEIGHT`` at ``EDUBOTICS_CAMERA_FRAMERATE``,
  from the Pi's ``.env``, defaults 640 x 480 at 30), the start goes ahead and
  ``/status`` carries ``scene_camera_hint``.
* A list that cannot be read allows the start, with no hint: refuse on proof
  only.

The camera here is the U20CAM-1080P-S1 of decision D2, whose manual lists
MJPEG 1920x1080 ... 640x320 at 30 fps and YUY2 1920x1080 at 5, 1280x720 at 10,
640x480 and 640x360 at 30 (spec 41 H1); the texts are v4l2-ctl's.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

os.environ.setdefault("EDUBOTICS_PI_AUTH_FILE", os.path.join(
    tempfile.mkdtemp(prefix="pi-auth-test-"), "pi_auth.json"))
os.environ.setdefault("EDUBOTICS_PI_CONSOLE", "")

from pi_agent import agent  # noqa: E402
from pi_agent import camera_enum  # noqa: E402
from pi_agent import config_generator as cg  # noqa: E402
from pi_agent.config_generator import ArmDevice, CameraDevice, HardwareConfig  # noqa: E402

KEY = "EDUBOTICS_SCENE_CAMERA_PROFILE"
GRIPPER = "/dev/v4l/by-id/usb-Innomaker_U20CAM-720P_SN0001-video-index0"
SCENE = "/dev/v4l/by-id/usb-Innomaker_U20CAM-1080P-S1_SN0002-video-index0"

REFUSED_DE = ("Die Szenen-Kamera liefert kein MJPEG-Bild mit 1920 × 1080 Bildpunkten. "
              "Bitte die Turm-Kamera anschließen oder in der Einrichtung „Turm-Kamera“ abwählen.")
HINT_DE = ("Die Szenen-Kamera meldet das Aufnahmeformat (640 × 480 mit 30 Bildern pro "
           "Sekunde) nicht. Ist es die Turm-Kamera, bitte in der Einrichtung „Turm-Kamera“ "
           "auswählen.")


def _listing(*formats):
    """v4l2-ctl --list-formats-ext output: ``formats`` are (fourcc, name,
    [(width, height, [fps, ...]), ...])."""
    lines = ["ioctl: VIDIOC_ENUM_FMT", "\tType: Video Capture", ""]
    for index, (fourcc, name, sizes) in enumerate(formats):
        lines.append(f"\t[{index}]: '{fourcc}' ({name})")
        for width, height, rates in sizes:
            lines.append(f"\t\tSize: Discrete {width}x{height}")
            for fps in rates:
                lines.append(f"\t\t\tInterval: Discrete {1 / fps:.3f}s ({fps:.3f} fps)")
    return "\n".join(lines) + "\n"


_MJPG_SIZES = [(1920, 1080, [30, 25, 20, 15, 10, 5]), (1280, 960, [30, 15]),
               (1280, 720, [30, 15]), (1024, 576, [30]), (800, 600, [30]),
               (640, 480, [30, 15]), (640, 320, [30])]
_YUYV_SIZES = [(1920, 1080, [5]), (1280, 720, [10, 5]), (640, 480, [30, 15]),
               (640, 360, [30])]
# The manual's list (spec 41 H1).
MANUAL = _listing(("MJPG", "Motion-JPEG, compressed", _MJPG_SIZES),
                  ("YUYV", "YUYV 4:2:2", _YUYV_SIZES))
# A webcam without 1080p MJPEG (an ordinary scene camera, RG-16 (c)).
NO_1080P_MJPG = _listing(("MJPG", "Motion-JPEG, compressed",
                          [(1280, 720, [30]), (640, 480, [30])]),
                         ("YUYV", "YUYV 4:2:2", [(640, 480, [30])]))
# 1080p MJPEG, but only at 15 fps.
SLOW_1080P_MJPG = _listing(("MJPG", "Motion-JPEG, compressed", [(1920, 1080, [15, 5])]),
                           ("YUYV", "YUYV 4:2:2", [(640, 480, [30])]))
# YUYV only, and only at 1080p 5 fps (what review M3 once feared).
YUYV_1080P_ONLY = _listing(("YUYV", "YUYV 4:2:2", [(1920, 1080, [5])]))


class TheFormatList(unittest.TestCase):

    def test_the_manuals_list(self):
        modes = camera_enum.parse_formats(MANUAL)
        self.assertTrue(camera_enum.offers(modes, "MJPG", 1920, 1080, 30))
        self.assertTrue(camera_enum.offers(modes, "YUYV", 640, 480, 30))
        self.assertTrue(camera_enum.offers(modes, "YUYV", 1920, 1080, 5))
        self.assertFalse(camera_enum.offers(modes, "YUYV", 1920, 1080, 30))
        self.assertFalse(camera_enum.offers(modes, "YUYV", 1280, 720, 30))
        self.assertFalse(camera_enum.offers(modes, "MJPG", 1920, 1200, 30))
        self.assertFalse(camera_enum.offers(modes, "H264", 1920, 1080, 30))

    def test_a_rate_a_hair_below_is_still_the_rate(self):
        modes = camera_enum.parse_formats(
            "\t[0]: 'MJPG' (Motion-JPEG, compressed)\n\t\tSize: Discrete 1920x1080\n"
            "\t\t\tInterval: Discrete 0.033s (29.970 fps)\n")
        self.assertTrue(camera_enum.offers(modes, "MJPG", 1920, 1080, 30))
        modes = camera_enum.parse_formats(SLOW_1080P_MJPG)
        self.assertFalse(camera_enum.offers(modes, "MJPG", 1920, 1080, 30))

    def test_the_older_v4l2_ctl_layout(self):
        text = ("ioctl: VIDIOC_ENUM_FMT\n\tIndex       : 0\n\tType        : Video Capture\n"
                "\tPixel Format: 'MJPG' (compressed)\n\tName        : Motion-JPEG\n"
                "\t\tSize: Discrete 1920x1080\n\t\t\tInterval: Discrete 0.033s (30.000 fps)\n")
        self.assertTrue(camera_enum.offers(camera_enum.parse_formats(text), "MJPG", 1920, 1080, 30))

    def test_a_stepwise_size_and_a_continuous_interval(self):
        text = ("\t[0]: 'MJPG' (Motion-JPEG, compressed)\n"
                "\t\tSize: Stepwise 16x16 - 1920x1080 with step 16/8\n"
                "\t\t\tInterval: Continuous 0.033s - 1.000s (1.000-30.000 fps)\n")
        modes = camera_enum.parse_formats(text)
        self.assertTrue(camera_enum.offers(modes, "MJPG", 1920, 1080, 30))
        self.assertTrue(camera_enum.offers(modes, "MJPG", 640, 480, 30))
        self.assertFalse(camera_enum.offers(modes, "MJPG", 1921, 1080, 30))
        self.assertFalse(camera_enum.offers(modes, "MJPG", 1920, 1080, 31))
        self.assertFalse(camera_enum.offers(modes, "MJPG", 650, 480, 30), "off the 16-pixel step")

    def test_a_size_without_intervals_is_not_proof_against_it(self):
        modes = camera_enum.parse_formats(
            "\t[0]: 'MJPG' (Motion-JPEG, compressed)\n\t\tSize: Discrete 1920x1080\n")
        self.assertTrue(camera_enum.offers(modes, "MJPG", 1920, 1080, 30))

    def test_text_without_formats_is_no_list(self):
        self.assertEqual(camera_enum.parse_formats(""), [])
        self.assertEqual(camera_enum.parse_formats("Cannot open device /dev/video9\n"), [])


class ListFormats(unittest.TestCase):

    def run_with(self, outcome):
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        with patch.object(camera_enum.subprocess, "run", run):
            result = camera_enum.list_formats("/dev/video2")
        return result, calls

    def test_one_bounded_v4l2_ctl_call(self):
        result, calls = self.run_with(subprocess.CompletedProcess([], 0, MANUAL.encode(), b""))
        self.assertEqual([argv for argv, _kw in calls],
                         [["v4l2-ctl", "--device=/dev/video2", "--list-formats-ext"]])
        timeout = calls[0][1].get("timeout")
        self.assertIsNotNone(timeout)
        self.assertLessEqual(timeout, 10)
        self.assertTrue(camera_enum.offers(result, "MJPG", 1920, 1080, 30))

    def test_every_failure_to_read_is_none(self):
        for outcome in (FileNotFoundError("v4l2-ctl"),
                        subprocess.TimeoutExpired(["v4l2-ctl"], 5),
                        PermissionError("denied"),
                        subprocess.CompletedProcess([], 1, MANUAL.encode(), b"busy"),
                        subprocess.CompletedProcess([], 0, b"", b""),
                        subprocess.CompletedProcess([], 0, b"\xff\xfe garbage\n", b"")):
            with self.subTest(outcome=repr(outcome)[:60]):
                result, _calls = self.run_with(outcome)
                self.assertIsNone(result)


class _StartBase(unittest.TestCase):
    """A real AgentApp with a temp .env, an omx_full rig with a gripper and a
    scene camera, and a manager that gates the robot tier."""

    def setUp(self):
        self._prev_domain = os.environ.get("EDUBOTICS_ROS_DOMAIN")
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "30"
        self.addCleanup(self._restore_domain)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = os.path.join(self.dir, ".env")
        self.app = agent.AgentApp(env_file=self.env, auth_file=os.path.join(self.dir, "a.json"))
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_LEADER"),
            follower=ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_FOLLOWER"),
            cameras=[CameraDevice(path=GRIPPER, role="gripper"),
                     CameraDevice(path=SCENE, role="scene")])
        for target, name, value in (
                (agent.AgentApp, "_manager_login_state", lambda _self: "enforced"),
                (agent.docker_manager, "get_container_status", lambda: {}),
                (agent.docker_manager, "start_robot_tier", self._start)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.started = 0
        self.v4l2_calls = []

    def _restore_domain(self):
        if self._prev_domain is None:
            os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        else:
            os.environ["EDUBOTICS_ROS_DOMAIN"] = self._prev_domain

    def _start(self, log=None):
        self.started += 1
        return True

    def start(self, listing, profile=None, **env):
        """„Umgebung starten" with ``listing`` as v4l2-ctl's answer (a text, or
        an exception the call raises)."""
        if profile is not None:
            cg.upsert_env_var(KEY, profile, self.env)
        for key, value in env.items():
            cg.upsert_env_var(key, value, self.env)

        def run(argv, **kwargs):
            if argv[0] != "v4l2-ctl":
                raise AssertionError(f"unexpected command {argv}")
            self.v4l2_calls.append(list(argv))
            if isinstance(listing, BaseException):
                raise listing
            return subprocess.CompletedProcess(argv, 0, listing.encode(), b"")
        with patch.object(camera_enum.subprocess, "run", run):
            return self.app.handle_environment_start({})

    def hint(self):
        return self.app.handle_status()[1].get("scene_camera_hint")


class TheTowerProfileIsRefusedOnProof(_StartBase):

    def test_the_manuals_list_starts(self):
        code, payload = self.start(MANUAL, "tower_v1")
        self.assertEqual(code, 200, payload)
        self.assertEqual(self.started, 1)
        self.assertIsNone(self.hint())
        self.assertEqual(self.v4l2_calls,
                         [["v4l2-ctl", f"--device={SCENE}", "--list-formats-ext"]])

    def test_no_1080p_mjpeg_is_the_german_409_and_no_container(self):
        for listing in (NO_1080P_MJPG, SLOW_1080P_MJPG, YUYV_1080P_ONLY):
            with self.subTest(listing=listing.splitlines()[3]):
                code, payload = self.start(listing, "tower_v1")
                self.assertEqual(code, 409, payload)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["message"], REFUSED_DE)
                self.assertEqual(self.started, 0)
                # Refused before the .env is regenerated: it holds only what
                # the box stored.
                with open(self.env, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), f'{KEY}="tower_v1"\n')

    def test_the_refusal_is_said_in_the_protokoll(self):
        lines = []
        self.app._log = lines.append
        self.start(NO_1080P_MJPG, "tower_v1")
        self.assertTrue(any(REFUSED_DE in line for line in lines), lines)

    def test_an_unreadable_list_starts_without_a_hint(self):
        for outcome in (FileNotFoundError("v4l2-ctl"), "", "Cannot open device\n"):
            with self.subTest(outcome=repr(outcome)):
                self.started = 0
                code, payload = self.start(outcome, "tower_v1")
                self.assertEqual(code, 200, payload)
                self.assertEqual(self.started, 1)
                self.assertIsNone(self.hint())

    def test_a_rig_without_a_scene_camera_is_not_asked(self):
        self.app._hardware.cameras = [CameraDevice(path=GRIPPER, role="gripper")]
        code, _ = self.start(NO_1080P_MJPG, "tower_v1")
        self.assertEqual(code, 200)
        self.assertEqual(self.v4l2_calls, [])

    def test_a_path_outside_the_capture_node_allowlist_is_never_opened(self):
        # The roles route takes any string; opening /dev/watchdog arms it.
        for path in ("/dev/watchdog", "/etc/shadow", "/dev/v4l/by-id/../../watchdog"):
            with self.subTest(path=path):
                self.app._hardware.cameras = [CameraDevice(path=path, role="scene")]
                code, _ = self.start(NO_1080P_MJPG, "tower_v1")
                self.assertEqual(code, 200)
                self.assertEqual(self.v4l2_calls, [])

    def test_a_cloud_only_start_asks_nothing(self):
        cg.upsert_env_var(KEY, "tower_v1", self.env)
        with patch.object(camera_enum.subprocess, "run",
                          side_effect=AssertionError("asked the camera")):
            code, payload = self.app.handle_environment_start({"cloud_only": True})
        self.assertEqual(code, 200, payload)


class TheLegacyProfileIsNeverRefused(_StartBase):

    def test_the_manuals_list_starts_without_a_hint(self):
        for profile in (None, "legacy"):
            with self.subTest(profile=profile):
                code, payload = self.start(MANUAL, profile)
                self.assertEqual(code, 200, payload)
                self.assertIsNone(self.hint())

    def test_yuyv_only_at_1080p_starts_and_hints(self):
        code, payload = self.start(YUYV_1080P_ONLY, "legacy")
        self.assertEqual(code, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.started, 1)
        self.assertEqual(self.hint(), HINT_DE)

    def test_no_list_is_no_hint(self):
        code, _ = self.start(FileNotFoundError("v4l2-ctl"), "legacy")
        self.assertEqual(code, 200)
        self.assertIsNone(self.hint())

    def test_the_hint_names_the_format_the_compose_hands_usb_cam(self):
        mjpg_720 = _listing(("MJPG", "Motion-JPEG, compressed", [(1280, 720, [30])]))
        yuyv_720 = _listing(("YUYV", "YUYV 4:2:2", [(1280, 720, [30])]))
        env = {"EDUBOTICS_CAMERA_PIXEL_FORMAT": "mjpeg2rgb", "EDUBOTICS_CAMERA_WIDTH": "1280",
               "EDUBOTICS_CAMERA_HEIGHT": "720", "EDUBOTICS_CAMERA_FRAMERATE": "15.0"}
        code, _ = self.start(mjpg_720, "legacy", **env)
        self.assertEqual(code, 200)
        self.assertIsNone(self.hint())
        code, _ = self.start(yuyv_720, "legacy", **env)
        self.assertEqual(code, 200)
        self.assertEqual(self.hint(), HINT_DE.replace("640 × 480 mit 30", "1280 × 720 mit 15"))

    def test_a_format_it_cannot_judge_is_no_hint(self):
        for env in ({"EDUBOTICS_CAMERA_PIXEL_FORMAT": "uyvy"},
                    {"EDUBOTICS_CAMERA_WIDTH": "breit"},
                    {"EDUBOTICS_CAMERA_FRAMERATE": "schnell"}):
            with self.subTest(env=env):
                code, _ = self.start(YUYV_1080P_ONLY, "legacy", **env)
                self.assertEqual(code, 200)
                self.assertIsNone(self.hint())
                for key in env:
                    cg.upsert_env_var(key, "", self.env)

    def test_the_hint_goes_with_the_next_start_and_with_a_profile_change(self):
        self.start(YUYV_1080P_ONLY, "legacy")
        self.assertEqual(self.hint(), HINT_DE)
        self.start(MANUAL, "legacy")
        self.assertIsNone(self.hint())
        self.start(YUYV_1080P_ONLY, "legacy")
        self.assertEqual(self.hint(), HINT_DE)
        code, _ = self.app.handle_set_scene_camera_profile({"profile": "tower_v1"})
        self.assertEqual(code, 200)
        self.assertIsNone(self.hint())

    def test_the_hint_is_said_in_the_protokoll(self):
        lines = []
        self.app._log = lines.append
        self.start(YUYV_1080P_ONLY, "legacy")
        self.assertTrue(any(HINT_DE in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
