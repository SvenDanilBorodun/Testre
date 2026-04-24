"""Tests for the WSL-routing wrapper around docker commands.

Verifies that every docker invocation is prefixed with
`wsl -d EduBotics [--cd <cwd>] -- docker ...` and never calls plain `docker`.

Windows-only: asserts on the exact wsl-wrapped argv. Skip on non-Windows CI.
"""

import sys
import unittest

if sys.platform != "win32":
    raise unittest.SkipTest("Windows-only tests; skipped on non-Windows CI.")

from unittest.mock import patch, MagicMock

from gui.app import docker_manager
from gui.app.constants import WSL_DISTRO_NAME


def _first_positional_arg(call) -> list:
    """subprocess.run/Popen accept the command as first positional or kwarg."""
    if call.args:
        return list(call.args[0])
    return list(call.kwargs["args"])


class TestDockerCmdWrapping(unittest.TestCase):
    """Every user-facing docker op must be wrapped through wsl -d EduBotics."""

    def _assert_wsl_wrapped(self, cmd: list):
        self.assertEqual(cmd[0], "wsl", msg=f"Expected wsl prefix, got {cmd!r}")
        self.assertEqual(cmd[1], "-d")
        self.assertEqual(cmd[2], WSL_DISTRO_NAME)
        # -- separates wsl flags from the inner command; docker must come after
        sep_idx = cmd.index("--")
        self.assertEqual(cmd[sep_idx + 1], "docker",
                         msg=f"Expected docker after --, got {cmd!r}")

    @patch("gui.app.docker_manager.subprocess.run")
    def test_is_docker_running_wraps(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        docker_manager.is_docker_running()
        self._assert_wsl_wrapped(_first_positional_arg(mock_run.call_args))

    @patch("gui.app.docker_manager.subprocess.run")
    def test_images_exist_wraps(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        docker_manager.images_exist()
        for call in mock_run.call_args_list:
            self._assert_wsl_wrapped(_first_positional_arg(call))

    @patch("gui.app.docker_manager.subprocess.run")
    def test_start_containers_wraps_and_sets_cwd(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        docker_manager.start_containers(gpu=False)
        cmd = _first_positional_arg(mock_run.call_args)
        self._assert_wsl_wrapped(cmd)
        # --cd should be present with a /mnt/ path
        self.assertIn("--cd", cmd)
        cwd = cmd[cmd.index("--cd") + 1]
        self.assertTrue(cwd.startswith("/mnt/"),
                        msg=f"cwd should be /mnt/<drive>/... got {cwd!r}")

    @patch("gui.app.docker_manager.subprocess.run")
    def test_stop_containers_wraps(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        docker_manager.stop_containers(gpu=False)
        self._assert_wsl_wrapped(_first_positional_arg(mock_run.call_args))

    @patch("gui.app.docker_manager.subprocess.run")
    def test_get_container_logs_wraps(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        docker_manager.get_container_logs("physical_ai_server", lines=10)
        self._assert_wsl_wrapped(_first_positional_arg(mock_run.call_args))

    @patch("gui.app.docker_manager.subprocess.run")
    def test_manager_container_running_wraps(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="running")
        docker_manager.manager_container_running()
        self._assert_wsl_wrapped(_first_positional_arg(mock_run.call_args))


class TestIsDistroRegistered(unittest.TestCase):

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_true_when_distro_listed(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=f"Ubuntu\n{WSL_DISTRO_NAME}\n")
        self.assertTrue(docker_manager.is_distro_registered())

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_false_when_distro_missing(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Ubuntu\nDebian\n")
        self.assertFalse(docker_manager.is_distro_registered())

    @patch("gui.app.docker_manager.subprocess.run")
    def test_handles_utf16_nul_bytes(self, mock_run):
        # wsl --list --quiet sometimes sneaks through stray NULs even with text=True
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"Ubuntu\x00\n{WSL_DISTRO_NAME}\x00\n",
        )
        self.assertTrue(docker_manager.is_distro_registered())

    @patch("gui.app.docker_manager.subprocess.run", side_effect=FileNotFoundError)
    def test_returns_false_when_wsl_not_found(self, mock_run):
        self.assertFalse(docker_manager.is_distro_registered())


class TestHasGpu(unittest.TestCase):
    """nvidia-smi runs on the Windows host, NOT through wsl."""

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_true_with_nvidia(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(docker_manager.has_gpu())
        # Verify it's NOT wrapped through wsl
        cmd = _first_positional_arg(mock_run.call_args)
        self.assertEqual(cmd[0], "nvidia-smi")

    @patch("gui.app.docker_manager.subprocess.run", side_effect=FileNotFoundError)
    def test_returns_false_without_nvidia(self, mock_run):
        self.assertFalse(docker_manager.has_gpu())


if __name__ == "__main__":
    unittest.main()
