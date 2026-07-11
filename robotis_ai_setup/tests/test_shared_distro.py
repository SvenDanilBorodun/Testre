"""Cross-platform unit tests for the multi-account shared-VHDX support.

Pins the contract of docker_manager's shared-distro helpers:
  * shared_vhdx_path resolution (env override > ProgramData default)
  * classify_wsl_error — incl. the UTF-16-NUL-mangled wsl.exe output shape
  * register_shared_distro — exact `wsl --import-in-place` argv, and that
    success requires BOTH exit 0 AND a verified registration
  * probe_distro_start / terminate_distro argv + failure classification
  * WSL_START_ERROR_DE covers every classifier kind (a kind without a
    German message would surface as a KeyError-avoiding fallback, hiding
    the precise lock/ACL remediation from the student)

Like test_docker_auto_pull.py these run on the Linux CI host — all wsl.exe
interaction is mocked at the subprocess boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Make the `gui` package importable from the repo-root layout used by CI.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gui.app import docker_manager  # noqa: E402


def _completed(rc: int, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = rc
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _nul_mangled(text: str) -> str:
    """wsl.exe writes UTF-16LE; text=True decodes it with a NUL between every
    character. Reproduce that shape so the classifier's NUL-strip is pinned."""
    return "\x00".join(text) + "\x00"


class TestSharedVhdxPath(unittest.TestCase):
    def test_env_override_wins(self):
        with patch.dict(os.environ, {"EDUBOTICS_SHARED_VHDX": r"D:\edu\ext4.vhdx"}):
            self.assertEqual(docker_manager.shared_vhdx_path(), r"D:\edu\ext4.vhdx")

    def test_default_under_programdata(self):
        env = {"ProgramData": r"C:\ProgramData"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("EDUBOTICS_SHARED_VHDX", None)
            path = docker_manager.shared_vhdx_path()
        self.assertTrue(path.startswith(r"C:\ProgramData"))
        self.assertIn("EduBotics", path)
        self.assertTrue(path.endswith("ext4.vhdx"))

    def test_exists_follows_path(self):
        with tempfile.TemporaryDirectory() as d:
            vhdx = os.path.join(d, "ext4.vhdx")
            with patch.dict(os.environ, {"EDUBOTICS_SHARED_VHDX": vhdx}):
                self.assertFalse(docker_manager.shared_vhdx_exists())
                with open(vhdx, "wb") as f:
                    f.write(b"x")
                self.assertTrue(docker_manager.shared_vhdx_exists())


class TestClassifyWslError(unittest.TestCase):
    def test_sharing_violation_is_locked(self):
        out = "Wsl/Service/CreateInstance/MountVhd/HCS/ERROR_SHARING_VIOLATION"
        self.assertEqual(docker_manager.classify_wsl_error(out), "locked")

    def test_locked_hresult_code(self):
        self.assertEqual(
            docker_manager.classify_wsl_error("Error code: 0x80070020"),
            "locked")

    def test_nul_mangled_utf16_output_still_classifies(self):
        out = _nul_mangled(
            "The process cannot access the file because it is "
            "being used by another process.")
        self.assertEqual(docker_manager.classify_wsl_error(out), "locked")

    def test_access_denied(self):
        out = "Wsl/Service/CreateInstance/MountVhd/HCS/E_ACCESSDENIED"
        self.assertEqual(docker_manager.classify_wsl_error(out), "access_denied")

    def test_german_localized_not_found(self):
        out = ("Das System kann die angegebene Datei nicht finden.\n"
               "Error code: Wsl/Service/CreateInstance/MountVhd/0x80070002")
        self.assertEqual(docker_manager.classify_wsl_error(out), "not_found")

    def test_unknown_and_empty_are_other(self):
        self.assertEqual(docker_manager.classify_wsl_error("boom"), "other")
        self.assertEqual(docker_manager.classify_wsl_error(""), "other")
        self.assertEqual(docker_manager.classify_wsl_error(None), "other")

    def test_every_kind_has_a_german_message(self):
        kinds = set(docker_manager._WSL_ERROR_PATTERNS) | {"other"}
        for kind in kinds:
            msg = docker_manager.WSL_START_ERROR_DE.get(kind)
            self.assertTrue(msg, f"missing German message for kind {kind!r}")

    def test_locked_message_names_the_remedy(self):
        # The whole point of the classifier: the student must be told to LOG
        # THE OTHER ACCOUNT OFF, not to reinstall.
        self.assertIn("abmelden", docker_manager.WSL_START_ERROR_DE["locked"])


class TestRegisterSharedDistro(unittest.TestCase):
    def _with_vhdx(self):
        d = tempfile.TemporaryDirectory()
        vhdx = os.path.join(d.name, "ext4.vhdx")
        with open(vhdx, "wb") as f:
            f.write(b"x")
        return d, vhdx

    def test_missing_vhdx_short_circuits(self):
        with patch.dict(os.environ,
                        {"EDUBOTICS_SHARED_VHDX": "/nonexistent/ext4.vhdx"}):
            with patch.object(docker_manager.subprocess, "run") as run:
                ok, kind = docker_manager.register_shared_distro()
        self.assertFalse(ok)
        self.assertEqual(kind, "not_found")
        run.assert_not_called()

    def test_import_in_place_argv_and_success(self):
        d, vhdx = self._with_vhdx()
        try:
            with patch.dict(os.environ, {"EDUBOTICS_SHARED_VHDX": vhdx}):
                with patch.object(docker_manager.subprocess, "run",
                                  return_value=_completed(0)) as run, \
                     patch.object(docker_manager, "is_distro_registered",
                                  return_value=True):
                    ok, kind = docker_manager.register_shared_distro()
            self.assertTrue(ok)
            self.assertEqual(kind, "")
            argv = run.call_args[0][0]
            self.assertEqual(
                argv,
                ["wsl", "--import-in-place",
                 docker_manager.WSL_DISTRO_NAME, vhdx])
        finally:
            d.cleanup()

    def test_exit_zero_but_unregistered_is_failure(self):
        # Belt-and-suspenders: a lying/localized wsl.exe exit code must not
        # count as success unless the registration is actually visible.
        d, vhdx = self._with_vhdx()
        try:
            with patch.dict(os.environ, {"EDUBOTICS_SHARED_VHDX": vhdx}):
                with patch.object(docker_manager.subprocess, "run",
                                  return_value=_completed(0)), \
                     patch.object(docker_manager, "is_distro_registered",
                                  return_value=False):
                    ok, _kind = docker_manager.register_shared_distro()
            self.assertFalse(ok)
        finally:
            d.cleanup()

    def test_failure_output_is_classified(self):
        d, vhdx = self._with_vhdx()
        try:
            with patch.dict(os.environ, {"EDUBOTICS_SHARED_VHDX": vhdx}):
                with patch.object(
                        docker_manager.subprocess, "run",
                        return_value=_completed(
                            1, stderr="MountVhd/HCS/E_ACCESSDENIED")):
                    ok, kind = docker_manager.register_shared_distro()
            self.assertFalse(ok)
            self.assertEqual(kind, "access_denied")
        finally:
            d.cleanup()

    def test_timeout_is_other(self):
        d, vhdx = self._with_vhdx()
        try:
            with patch.dict(os.environ, {"EDUBOTICS_SHARED_VHDX": vhdx}):
                with patch.object(
                        docker_manager.subprocess, "run",
                        side_effect=subprocess.TimeoutExpired("wsl", 60)):
                    ok, kind = docker_manager.register_shared_distro()
            self.assertFalse(ok)
            self.assertEqual(kind, "other")
        finally:
            d.cleanup()


class TestProbeDistroStart(unittest.TestCase):
    def test_unregistered_is_not_found_without_subprocess(self):
        with patch.object(docker_manager, "is_distro_registered",
                          return_value=False):
            with patch.object(docker_manager.subprocess, "run") as run:
                ok, kind = docker_manager.probe_distro_start()
        self.assertFalse(ok)
        self.assertEqual(kind, "not_found")
        run.assert_not_called()

    def test_success(self):
        with patch.object(docker_manager, "is_distro_registered",
                          return_value=True):
            with patch.object(docker_manager.subprocess, "run",
                              return_value=_completed(0, stdout="ready")) as run:
                ok, kind = docker_manager.probe_distro_start()
        self.assertTrue(ok)
        self.assertEqual(kind, "")
        argv = run.call_args[0][0]
        self.assertEqual(
            argv,
            ["wsl", "-d", docker_manager.WSL_DISTRO_NAME, "--", "echo", "ready"])

    def test_locked_start_is_classified(self):
        with patch.object(docker_manager, "is_distro_registered",
                          return_value=True):
            with patch.object(
                    docker_manager.subprocess, "run",
                    return_value=_completed(
                        1, stderr=_nul_mangled("ERROR_SHARING_VIOLATION"))):
                ok, kind = docker_manager.probe_distro_start()
        self.assertFalse(ok)
        self.assertEqual(kind, "locked")


class TestTerminateDistro(unittest.TestCase):
    def test_argv_and_success(self):
        with patch.object(docker_manager.subprocess, "run",
                          return_value=_completed(0)) as run:
            self.assertTrue(docker_manager.terminate_distro())
        argv = run.call_args[0][0]
        self.assertEqual(
            argv, ["wsl", "--terminate", docker_manager.WSL_DISTRO_NAME])

    def test_never_raises(self):
        with patch.object(docker_manager.subprocess, "run",
                          side_effect=FileNotFoundError()):
            self.assertFalse(docker_manager.terminate_distro())
        with patch.object(docker_manager.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("wsl", 15)):
            self.assertFalse(docker_manager.terminate_distro())


if __name__ == "__main__":
    unittest.main()
