"""Tests for wsl_bridge.list_video_devices() duplicate-symlink handling.

Audit Gap-D 2026-05-23: two UVC cameras that report the IDENTICAL USB
serial (e.g. the Innomaker U20CAM-720P pair both report SN0001) get the
same /dev/v4l/by-id/usb-..._SN0001-... udev symlink, but distinct
Bus info strings. The current implementation must NOT hand the same
by-id path to both camera slots — it should downgrade colliding rows to
their by-path equivalent so the entrypoint resolves them to distinct
/dev/videoN nodes.

Platform-agnostic: every WSL call is mocked, so the test runs on
Linux CI and on the maintainer's Windows workstation.
"""

import unittest
from unittest.mock import patch

from gui.app import wsl_bridge


def _fake_completed(stdout: str = "", returncode: int = 0):
    """Minimal subprocess.CompletedProcess shape used by the mocked run()."""
    class _Result:
        pass
    r = _Result()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


class TestVideoDeviceDedup(unittest.TestCase):

    def test_distinct_cameras_keep_distinct_byid_paths(self):
        """Two cameras with distinct serials → distinct by-id paths preserved."""
        # Format mirrors the shell pipeline's output:
        # `path|name|real_path|bus`
        fake_stdout = (
            "/dev/v4l/by-id/usb-Logitech_C920_AAAA-video-index0|Logitech C920|/dev/video0|usb-vhci_hcd.0-1\n"
            "/dev/v4l/by-id/usb-Logitech_C920_BBBB-video-index0|Logitech C920|/dev/video2|usb-vhci_hcd.0-2\n"
        )
        with patch.object(wsl_bridge, "run", return_value=_fake_completed(fake_stdout)):
            devices = wsl_bridge.list_video_devices()
        self.assertEqual(len(devices), 2)
        # Both keep their distinct by-id paths.
        self.assertIn("AAAA", devices[0]["path"])
        self.assertIn("BBBB", devices[1]["path"])

    def test_colliding_byid_paths_get_downgraded_to_bypath(self):
        """Two cameras sharing the same by-id symlink → both fall back to by-path."""
        # Both Innomaker cameras report SN0001 → shared by-id symlink.
        # The shell pipeline picked the same by-id `path` for both rows
        # but the `real_path` and `bus` columns differ.
        fake_stdout = (
            "/dev/v4l/by-id/usb-Innomaker_U20CAM_SN0001-video-index0|Innomaker U20CAM-720P|/dev/video0|usb-vhci_hcd.0-1\n"
            "/dev/v4l/by-id/usb-Innomaker_U20CAM_SN0001-video-index0|Innomaker U20CAM-720P|/dev/video2|usb-vhci_hcd.0-2\n"
        )

        # The fallback re-queries udev for a by-path symlink. Track which
        # real_path each call asked about and return a distinct symlink
        # for each.
        call_count = {"n": 0}

        def fake_run(cmd, timeout=5, check=False):
            # First call: the main `for d in /dev/video*` pipeline.
            if call_count["n"] == 0:
                call_count["n"] += 1
                return _fake_completed(fake_stdout)
            # Subsequent calls: by-path lookups. Discriminate on the
            # real_path embedded in the bash snippet.
            call_count["n"] += 1
            if "/dev/video0" in cmd:
                return _fake_completed("/dev/v4l/by-path/pci-vhci-usb-0:1:1.0-video-index0\n")
            if "/dev/video2" in cmd:
                return _fake_completed("/dev/v4l/by-path/pci-vhci-usb-0:2:1.0-video-index0\n")
            return _fake_completed("")

        with patch.object(wsl_bridge, "run", side_effect=fake_run):
            devices = wsl_bridge.list_video_devices()

        self.assertEqual(len(devices), 2, f"expected 2 cameras, got {devices}")
        paths = sorted(d["path"] for d in devices)
        # Neither device should still carry the colliding by-id path.
        for d in devices:
            self.assertNotIn(
                "by-id/usb-Innomaker_U20CAM_SN0001",
                d["path"],
                f"camera still has colliding by-id path: {d}",
            )
        # Both should now be by-path symlinks.
        self.assertTrue(all("by-path" in p for p in paths), paths)
        # And distinct from each other.
        self.assertEqual(len(set(paths)), 2)

    def test_collision_without_bypath_falls_back_to_byid(self):
        """If by-path is unavailable, keep the original (best-effort)."""
        fake_stdout = (
            "/dev/v4l/by-id/usb-Innomaker_U20CAM_SN0001-video-index0|Innomaker|/dev/video0|usb-1-1\n"
            "/dev/v4l/by-id/usb-Innomaker_U20CAM_SN0001-video-index0|Innomaker|/dev/video2|usb-1-2\n"
        )
        call_count = {"n": 0}

        def fake_run(cmd, timeout=5, check=False):
            if call_count["n"] == 0:
                call_count["n"] += 1
                return _fake_completed(fake_stdout)
            call_count["n"] += 1
            # No by-path available for either device.
            return _fake_completed("")

        with patch.object(wsl_bridge, "run", side_effect=fake_run):
            devices = wsl_bridge.list_video_devices()

        # Still returns two rows (de-dup didn't collapse them — bus
        # info kept them distinct), even though both fall back to the
        # shared by-id path. The entrypoint's [STOPP] guard will catch
        # this case at container start.
        self.assertEqual(len(devices), 2)


if __name__ == "__main__":
    unittest.main()
