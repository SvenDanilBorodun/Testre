"""Tests for docker_manager module (unit tests — mocked subprocess).

Windows-only: the module wraps every docker call with `wsl -d EduBotics`.
Asserting on those wrappers requires the Windows-assuming code path
(constants.WSL_DISTRO_NAME etc.). Skip on non-Windows CI.
"""

import sys
import unittest

if sys.platform != "win32":
    raise unittest.SkipTest("Windows-only tests; skipped on non-Windows CI.")

from unittest.mock import patch, MagicMock
import subprocess

from gui.app.docker_manager import is_docker_running, has_gpu, images_exist


class TestIsDockerRunning(unittest.TestCase):

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_true_when_docker_ok(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(is_docker_running())

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_false_when_docker_fails(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(is_docker_running())

    @patch("gui.app.docker_manager.subprocess.run", side_effect=FileNotFoundError)
    def test_returns_false_when_docker_not_found(self, mock_run):
        self.assertFalse(is_docker_running())


class TestHasGpu(unittest.TestCase):

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_true_with_nvidia(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(has_gpu())

    @patch("gui.app.docker_manager.subprocess.run", side_effect=FileNotFoundError)
    def test_returns_false_without_nvidia(self, mock_run):
        self.assertFalse(has_gpu())


class TestImagesExist(unittest.TestCase):

    @patch("gui.app.docker_manager.subprocess.run")
    def test_all_images_present(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = images_exist()
        self.assertTrue(all(result.values()))

    @patch("gui.app.docker_manager.subprocess.run")
    def test_missing_image(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        result = images_exist()
        self.assertTrue(all(not v for v in result.values()))


if __name__ == "__main__":
    unittest.main()
