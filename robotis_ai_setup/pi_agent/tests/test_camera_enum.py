"""Tests for pi_agent.camera_enum native v4l2 enumeration + dedup.

Ported from the GUI's ``tests/test_wsl_bridge_camera_dedup.py``: two UVC
cameras that report the IDENTICAL USB serial (the Innomaker U20CAM-720P pair
both report SN0001) get the same ``/dev/v4l/by-id/usb-..._SN0001-...`` symlink
but distinct v4l2 ``Bus info``; the enumerator must NOT hand the same by-id path
to both slots — it downgrades colliding rows to their by-path equivalent so the
container resolves them to distinct ``/dev/videoN`` nodes.

Deps-free: the native ``bash`` runner is mocked, so no v4l2 hardware is needed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]  # robotis_ai_setup
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pi_agent import camera_enum  # noqa: E402


class TestListVideoDevices(unittest.TestCase):

    def test_empty_when_no_output(self):
        with patch.object(camera_enum, "_run_bash", return_value=""):
            self.assertEqual(camera_enum.list_video_devices(), [])

    def test_none_output_returns_empty(self):
        # bash missing / timeout / non-zero exit → _run_bash returns None.
        with patch.object(camera_enum, "_run_bash", return_value=None):
            self.assertEqual(camera_enum.list_video_devices(), [])

    def test_distinct_cameras_keep_distinct_byid_paths(self):
        """Two cameras with distinct serials → distinct by-id paths preserved."""
        fake_stdout = (
            "/dev/v4l/by-id/usb-Logitech_C920_AAAA-video-index0|Logitech C920|/dev/video0|usb-1-1\n"
            "/dev/v4l/by-id/usb-Logitech_C920_BBBB-video-index0|Logitech C920|/dev/video2|usb-1-2\n"
        )
        with patch.object(camera_enum, "_run_bash", return_value=fake_stdout):
            devices = camera_enum.list_video_devices()
        self.assertEqual(len(devices), 2)
        self.assertIn("AAAA", devices[0]["path"])
        self.assertIn("BBBB", devices[1]["path"])
        self.assertEqual(devices[0]["name"], "Logitech C920")

    def test_colliding_byid_paths_get_downgraded_to_bypath(self):
        """Two cameras sharing the same by-id symlink → both fall back to by-path."""
        enum_out = (
            "/dev/v4l/by-id/usb-Innomaker_U20CAM_SN0001-video-index0|Innomaker U20CAM-720P|/dev/video0|usb-1-1\n"
            "/dev/v4l/by-id/usb-Innomaker_U20CAM_SN0001-video-index0|Innomaker U20CAM-720P|/dev/video2|usb-1-2\n"
        )

        def fake_run(script, timeout=15):
            # The main enum pipeline scans /dev/video* .
            if "for d in /dev/video*" in script:
                return enum_out
            # by-path lookups: discriminate on the embedded real_path.
            if "/dev/video0" in script:
                return "/dev/v4l/by-path/platform-usb-0:1:1.0-video-index0\n"
            if "/dev/video2" in script:
                return "/dev/v4l/by-path/platform-usb-0:2:1.0-video-index0\n"
            return ""

        with patch.object(camera_enum, "_run_bash", side_effect=fake_run):
            devices = camera_enum.list_video_devices()

        self.assertEqual(len(devices), 2, f"expected 2 cameras, got {devices}")
        paths = sorted(d["path"] for d in devices)
        for d in devices:
            self.assertNotIn("by-id/usb-Innomaker_U20CAM_SN0001", d["path"],
                             f"camera still has colliding by-id path: {d}")
        self.assertTrue(all("by-path" in p for p in paths), paths)
        self.assertEqual(len(set(paths)), 2)

    def test_collision_without_bypath_falls_back_to_byid(self):
        """If by-path is unavailable, keep the original (best-effort)."""
        enum_out = (
            "/dev/v4l/by-id/usb-Innomaker_U20CAM_SN0001-video-index0|Innomaker|/dev/video0|usb-1-1\n"
            "/dev/v4l/by-id/usb-Innomaker_U20CAM_SN0001-video-index0|Innomaker|/dev/video2|usb-1-2\n"
        )

        def fake_run(script, timeout=15):
            if "for d in /dev/video*" in script:
                return enum_out
            return ""  # no by-path available for either device

        with patch.object(camera_enum, "_run_bash", side_effect=fake_run):
            devices = camera_enum.list_video_devices()
        # Still two rows (bus info kept them distinct); the container's [STOPP]
        # guard catches the shared-path case at start.
        self.assertEqual(len(devices), 2)

    def test_dedup_on_bus_info(self):
        """Two rows for the SAME camera (two capture nodes, same Bus info)
        collapse to one entry keyed on Bus info."""
        enum_out = (
            "/dev/video0|Some Cam|/dev/video0|usb-1-1\n"
            "/dev/video1|Some Cam|/dev/video1|usb-1-1\n"  # same bus → dropped
            "/dev/video2|Other Cam|/dev/video2|usb-1-2\n"
        )
        with patch.object(camera_enum, "_run_bash", return_value=enum_out):
            devices = camera_enum.list_video_devices()
        self.assertEqual(len(devices), 2)

    def test_malformed_rows_skipped(self):
        enum_out = (
            "garbage-without-pipes\n"
            "/dev/video0|Good Cam|/dev/video0|usb-1-1\n"
        )
        with patch.object(camera_enum, "_run_bash", return_value=enum_out):
            devices = camera_enum.list_video_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "Good Cam")


class TestScanCameras(unittest.TestCase):
    def test_wraps_as_role_less_camera_devices(self):
        with patch.object(camera_enum, "list_video_devices", return_value=[
            {"path": "/dev/video0", "name": "Gripper Cam"},
            {"path": "/dev/video2", "name": "Scene Cam"},
        ]):
            cams = camera_enum.scan_cameras()
        self.assertEqual(len(cams), 2)
        self.assertIsInstance(cams[0], camera_enum.CameraDevice)
        self.assertEqual(cams[0].path, "/dev/video0")
        self.assertEqual(cams[0].name, "Gripper Cam")
        self.assertEqual(cams[0].role, "")       # assigned downstream


class TestRunBash(unittest.TestCase):
    def test_missing_bash_returns_none(self):
        with patch("pi_agent.camera_enum.subprocess.run", side_effect=FileNotFoundError()):
            self.assertIsNone(camera_enum._run_bash("echo hi", timeout=1))

    def test_timeout_returns_none(self):
        import subprocess
        with patch("pi_agent.camera_enum.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("bash", 1)):
            self.assertIsNone(camera_enum._run_bash("echo hi", timeout=1))

    def test_nonzero_exit_still_returns_stdout(self):
        # Faithful to the GUI's run(check=False)+.stdout: stdout is parsed
        # regardless of exit code so one v4l2-ctl hiccup can't nuke the scan.
        from unittest.mock import MagicMock
        with patch("pi_agent.camera_enum.subprocess.run",
                   return_value=MagicMock(returncode=1, stdout=b"partial\n")):
            self.assertEqual(camera_enum._run_bash("false", timeout=1), "partial\n")

    def test_success_decodes_stdout(self):
        from unittest.mock import MagicMock
        with patch("pi_agent.camera_enum.subprocess.run",
                   return_value=MagicMock(returncode=0, stdout=b"hello\n")):
            self.assertEqual(camera_enum._run_bash("echo hello", timeout=1), "hello\n")


class TestShellPipelineSmoke(unittest.TestCase):
    """HARDWARE-SMOKE: exercises the REAL bash ``_ENUM_SCRIPT`` end-to-end with
    fake ``v4l2-ctl`` / ``udevadm`` on PATH (the shell half normally needs real
    v4l2 nodes). Everything ABOVE this class mocks ``_run_bash``; this class is
    the one that actually runs the format-filter grep + name/bus sed pipeline, so
    a shell regression (e.g. the sed pattern breaking) is caught deps-free."""

    def _write_exec(self, path, body):
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def test_fake_v4l2_tools_produce_a_camera_row(self):
        if shutil.which("bash") is None:
            self.skipTest("bash unavailable")
        bindir = tempfile.mkdtemp()
        old_path = os.environ.get("PATH", "")
        try:
            # Fake v4l2-ctl: report a real capture format ([0]: line) + a Card
            # type / Bus info the sed pipeline must extract.
            self._write_exec(os.path.join(bindir, "v4l2-ctl"), (
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *--list-formats*) printf '\\t[0]: '\\''MJPG'\\'' (Motion-JPEG)\\n' ;;\n"
                "  *--info*) printf '\\tCard type     : FakeCam Integration\\n"
                "\\tBus info      : usb-fake-1.1\\n' ;;\n"
                "esac\n"
            ))
            # Fake udevadm: no by-id/by-path symlinks (keeps the device path).
            self._write_exec(os.path.join(bindir, "udevadm"), "#!/bin/sh\nexit 0\n")
            os.environ["PATH"] = bindir + os.pathsep + old_path
            devices = camera_enum.list_video_devices()
        finally:
            os.environ["PATH"] = old_path
            shutil.rmtree(bindir, ignore_errors=True)
        # The fake Card type reached the parsed result → the shell pipeline's
        # grep/sed extraction works. (One row: the constant Bus info dedups any
        # real nodes a CI host might expose to a single entry.)
        names = [d["name"] for d in devices]
        self.assertIn("FakeCam Integration", names, devices)


if __name__ == "__main__":
    unittest.main()
