"""Platform-agnostic argv-shape tests for the usbipd 5.x attach/detach
commands.

The only other device_manager test (test_device_manager.py) is fully
Windows-gated (skips on non-win32) AND never exercises
attach_usb_to_wsl, so a regression to the rejected usbipd 4.x
`--wsl --distribution <name>` form would pass every CI run on the Linux
box and only break at runtime on a student's Windows machine. These
assert the EXACT attach/detach argv WITHOUT running usbipd, on any OS.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Make the gui package importable (mirrors test_usbipd_resolver.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gui"))

from app import device_manager  # noqa: E402
from app.constants import WSL_DISTRO_NAME  # noqa: E402


class TestUsbipdAttachArgv(unittest.TestCase):
    def _patches(self):
        # usbipd_cmd just prepends the resolved exe; stub it so the test
        # doesn't depend on usbipd being installed on the CI host.
        return (
            patch.object(device_manager, "usbipd_cmd",
                         side_effect=lambda *a: ["usbipd.exe", *a]),
            patch.object(device_manager.subprocess, "run",
                         return_value=MagicMock(returncode=0)),
        )

    def test_attach_uses_usbipd_5x_positional_wsl_form(self):
        cmd_patch, run_patch = self._patches()
        with cmd_patch, run_patch as run:
            ok = device_manager.attach_usb_to_wsl("1-6")

        self.assertTrue(ok)
        run.assert_called_once()
        argv = run.call_args[0][0]
        self.assertEqual(
            argv,
            ["usbipd.exe", "attach", "--wsl", WSL_DISTRO_NAME, "--busid", "1-6"],
        )
        # The rejected usbipd 4.x form must never reappear, and --wsl must
        # be immediately followed by the distro name (the 5.x positional
        # contract documented in attach_usb_to_wsl's docstring).
        self.assertNotIn("--distribution", argv)
        self.assertEqual(argv[argv.index("--wsl") + 1], WSL_DISTRO_NAME)

    def test_detach_uses_busid_only(self):
        cmd_patch, run_patch = self._patches()
        with cmd_patch, run_patch as run:
            ok = device_manager.detach_usb_from_wsl("1-6")

        self.assertTrue(ok)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["usbipd.exe", "detach", "--busid", "1-6"])
        self.assertNotIn("--distribution", argv)


if __name__ == "__main__":
    unittest.main()
