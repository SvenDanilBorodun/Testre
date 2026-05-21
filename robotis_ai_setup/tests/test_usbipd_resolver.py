"""Tests for the usbipd_resolver module.

Cross-platform: the module's resolution logic is mockable on every OS.
The Windows-only branches (winreg) are exercised via stubbed modules so
the suite runs identically on the Linux CI box and the maintainer's
Windows dev box.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the gui package importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gui"))

from app import usbipd_resolver
from app.usbipd_resolver import (
    UsbipdNotFoundError,
    find_usbipd,
    reset_cache_for_tests,
    usbipd_cmd,
)


class ResolverBase(unittest.TestCase):
    def setUp(self):
        reset_cache_for_tests()
        # Snapshot PATH so tests don't bleed into each other.
        self._orig_path = os.environ.get("PATH", "")
        self._orig_platform = sys.platform

    def tearDown(self):
        os.environ["PATH"] = self._orig_path
        reset_cache_for_tests()


class TestFindUsbipdPathLookup(ResolverBase):

    def test_returns_path_when_shutil_which_resolves(self):
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", return_value=r"C:\Program Files\usbipd-win\usbipd.exe"):
            result = find_usbipd()
        self.assertEqual(result, r"C:\Program Files\usbipd-win\usbipd.exe")

    def test_caches_resolution(self):
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", side_effect=[r"C:\x\usbipd.exe", None]) as mock_which:
            first = find_usbipd()
            second = find_usbipd()
        self.assertEqual(first, second)
        # second call should not invoke shutil.which again
        self.assertEqual(mock_which.call_count, 1)

    def test_force_refresh_invalidates_cache(self):
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", side_effect=[r"C:\old\usbipd.exe", r"C:\new\usbipd.exe"]):
            first = find_usbipd()
            second = find_usbipd(force_refresh=True)
        self.assertEqual(first, r"C:\old\usbipd.exe")
        self.assertEqual(second, r"C:\new\usbipd.exe")


class TestFindUsbipdProgramFiles(ResolverBase):

    def test_falls_back_to_program_files(self):
        # _candidate_install_paths() builds paths with Path() which treats
        # the Windows backslash separator differently on POSIX vs Windows.
        # The resolver is Windows-only anyway — skip on non-win32 hosts.
        if sys.platform != "win32":
            self.skipTest("Windows-only path semantics")
        fake_path = Path(r"C:\Program Files\usbipd-win\usbipd.exe")

        def fake_is_file(self):
            return str(self).lower() == str(fake_path).lower()

        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {"ProgramFiles": r"C:\Program Files"}, clear=False), \
             patch.object(Path, "is_file", fake_is_file):
            result = find_usbipd()
        self.assertIsNotNone(result)
        self.assertEqual(result.lower(), str(fake_path).lower())

    def test_returns_none_when_nowhere(self):
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", return_value=None), \
             patch.object(Path, "is_file", lambda self: False), \
             patch.object(usbipd_resolver, "_from_registry_uninstall", return_value=None), \
             patch.object(usbipd_resolver, "_refreshed_path_from_registry", return_value=None):
            result = find_usbipd()
        self.assertIsNone(result)


class TestFindUsbipdRegistry(ResolverBase):

    def test_uses_registry_when_path_and_program_files_miss(self):
        fake_exe = r"C:\custom\usbipd.exe"
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", return_value=None), \
             patch.object(Path, "is_file", lambda self: str(self) == fake_exe), \
             patch.object(usbipd_resolver, "_from_registry_uninstall", return_value=fake_exe):
            result = find_usbipd()
        self.assertEqual(result, fake_exe)


class TestFindUsbipdRegistryPathRefresh(ResolverBase):

    def test_refreshes_path_and_retries_which(self):
        fake_dir = r"C:\refreshed\usbipd-win"
        fake_exe = fake_dir + r"\usbipd.exe"

        which_calls = {"count": 0}

        def fake_which(name):
            which_calls["count"] += 1
            # First call (initial scan) → not found.
            # Second call (after PATH refresh) → found.
            if which_calls["count"] == 1:
                return None
            return fake_exe

        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", side_effect=fake_which), \
             patch.object(Path, "is_file", lambda self: False), \
             patch.object(usbipd_resolver, "_from_registry_uninstall", return_value=None), \
             patch.object(
                 usbipd_resolver, "_refreshed_path_from_registry",
                 return_value=fake_dir,
             ):
            result = find_usbipd()
        self.assertEqual(result, fake_exe)
        self.assertEqual(which_calls["count"], 2)


class TestUsbipdCmd(ResolverBase):

    def test_returns_argv_with_resolved_path(self):
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", return_value=r"C:\x\usbipd.exe"):
            argv = usbipd_cmd("list")
        self.assertEqual(argv, [r"C:\x\usbipd.exe", "list"])

    def test_raises_when_not_found(self):
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", return_value=None), \
             patch.object(Path, "is_file", lambda self: False), \
             patch.object(usbipd_resolver, "_from_registry_uninstall", return_value=None), \
             patch.object(usbipd_resolver, "_refreshed_path_from_registry", return_value=None):
            with self.assertRaises(UsbipdNotFoundError):
                usbipd_cmd("list")

    def test_filenotfounderror_subclass_for_backcompat(self):
        # The resolver raises UsbipdNotFoundError, which must still be
        # catchable as FileNotFoundError so existing call sites stay valid.
        self.assertTrue(issubclass(UsbipdNotFoundError, FileNotFoundError))


class TestPathInjection(ResolverBase):

    def test_injects_install_dir_into_path(self):
        # _inject_into_path() uses Path().parent which behaves differently
        # on POSIX (treats '\' as a regular character). Windows-only.
        if sys.platform != "win32":
            self.skipTest("Windows-only path semantics")
        fake_dir = r"C:\Program Files\usbipd-win"
        fake_exe = fake_dir + r"\usbipd.exe"
        with patch("shutil.which", return_value=fake_exe):
            os.environ["PATH"] = r"C:\Windows\System32"
            find_usbipd()
        self.assertIn(fake_dir.lower(), os.environ["PATH"].lower())


class TestHardwareIdsForDevices(unittest.TestCase):
    """Belongs in test_device_manager.py logically, but device_manager is
    Windows-only-import-gated in the existing test file. Keep here so the
    helper has at least one cross-platform unit test."""

    def test_dedupes_and_lowercases(self):
        # Lazy import — device_manager imports usbipd_resolver which we've
        # already loaded, so this is cheap.
        from app.device_manager import USBDevice, hardware_ids_for_devices
        devs = [
            USBDevice("1-1", "046D:0825", "Logitech Webcam", "Not shared"),
            USBDevice("1-2", "046d:0825", "Logitech Webcam (dupe)", "Not shared"),
            USBDevice("1-3", "0C45:6713", "Sunplus", "Not shared"),
        ]
        ids = hardware_ids_for_devices(devs)
        self.assertEqual(ids, ["046d:0825", "0c45:6713"])


if __name__ == "__main__":
    unittest.main()
