"""Tests for the Windows-to-WSL path conversion helper."""

import unittest

from gui.app.constants import _to_wsl_path


class TestToWslPath(unittest.TestCase):

    def test_simple_drive_path(self):
        self.assertEqual(
            _to_wsl_path(r"C:\Program Files\EduBotics\docker"),
            "/mnt/c/Program Files/EduBotics/docker",
        )

    def test_lowercase_drive(self):
        self.assertEqual(
            _to_wsl_path(r"d:\data\stuff"),
            "/mnt/d/data/stuff",
        )

    def test_already_forward_slashes(self):
        self.assertEqual(
            _to_wsl_path("C:/Users/sven/.env"),
            "/mnt/c/Users/sven/.env",
        )

    def test_trailing_backslash(self):
        # Trailing separators pass through. Double slashes are harmless in POSIX
        # — we do not bother collapsing them.
        self.assertEqual(
            _to_wsl_path(r"C:\foo\bar\\"),
            "/mnt/c/foo/bar//",
        )

    def test_empty_path(self):
        self.assertEqual(_to_wsl_path(""), "")

    def test_non_drive_path_unchanged(self):
        """Paths without a drive letter pass through as-is (minus backslashes)."""
        self.assertEqual(_to_wsl_path("/etc/hosts"), "/etc/hosts")
        self.assertEqual(_to_wsl_path(r"relative\path"), "relative/path")


if __name__ == "__main__":
    unittest.main()
