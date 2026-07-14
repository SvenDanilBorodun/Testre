"""Deps-free unit tests for pi_agent.docker_manager.

Every ``docker`` / ``docker compose`` invocation is mocked at the
``subprocess.run`` boundary (or the helper level), so these run on any host
with no docker daemon. Focus areas:

  - arm64 digest pre-check (legacy manifest probe picks linux/arm64; the
    set-membership machinery is arch-neutral)
  - GHCR → Docker Hub pull fallback + re-tag
  - the NEVER-`compose down` rule: every teardown uses stop + rm -f on named
    robot-tier services only; the always-on manager is never stopped by
    factory_reset
  - two-tier lifecycle command construction (--no-deps, service names)
  - set_leader_mode .env regenerate + rollback-on-failure

Mirrors the Jetson tests' import convention.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

from pi_agent import docker_manager as dm  # noqa: E402
from pi_agent.config_generator import ArmDevice, HardwareConfig  # noqa: E402


class _Proc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Recorder:
    """subprocess.run side_effect that records argv and returns a scripted /
    predicate-matched _Proc."""

    def __init__(self, default=None):
        self.calls = []  # list[list[str]]
        self._default = default or _Proc(0)
        self._rules = []  # list[(predicate, _Proc)]

    def when(self, predicate, proc):
        self._rules.append((predicate, proc))
        return self

    def __call__(self, argv, *a, **kw):
        self.calls.append(list(argv))
        for pred, proc in self._rules:
            if pred(argv):
                return proc
        return self._default

    def argvs(self):
        return self.calls

    def any_call(self, predicate):
        return any(predicate(c) for c in self.calls)


def _contains(argv, *needles):
    return all(n in argv for n in needles)


# ── registry host / fallback ref ─────────────────────────────────────────────


class TestRegistryHelpers(unittest.TestCase):
    def test_registry_host(self):
        self.assertEqual(dm._registry_host("ghcr.io/svendanilborodun"), "ghcr.io")
        self.assertEqual(dm._registry_host("nettername"), "registry-1.docker.io")
        self.assertEqual(dm._registry_host("localhost:5000/foo"), "localhost")

    def test_fallback_ref_prefix_swap(self):
        with patch.object(dm, "REGISTRY", "ghcr.io/svendanilborodun"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"):
            self.assertEqual(
                dm._fallback_ref("ghcr.io/svendanilborodun/physical-ai-server-opi:latest"),
                "nettername/physical-ai-server-opi:latest",
            )
            # A ref that isn't the primary registry → None.
            self.assertIsNone(dm._fallback_ref("nettername/physical-ai-server-opi:latest"))

    def test_fallback_ref_none_when_no_distinct_fallback(self):
        with patch.object(dm, "REGISTRY", "nettername"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"):
            self.assertIsNone(dm._fallback_ref("nettername/x:latest"))

    def test_is_registry_reachable(self):
        with patch.object(dm, "_host_reachable", side_effect=[False, True]):
            self.assertTrue(dm.is_registry_reachable())
        with patch.object(dm, "_host_reachable", return_value=False):
            self.assertFalse(dm.is_registry_reachable())


# ── digest helpers (arm64) ───────────────────────────────────────────────────


class TestDigestHelpers(unittest.TestCase):
    def test_get_local_repo_digest_parses(self):
        out = "nettername/foo@sha256:" + "a" * 64 + "|"
        rec = _Recorder(_Proc(0, stdout=out))
        with patch.object(dm.subprocess, "run", rec):
            self.assertEqual(dm._get_local_repo_digest("x"), "sha256:" + "a" * 64)

    def test_get_local_repo_digest_none_on_error(self):
        with patch.object(dm.subprocess, "run", _Recorder(_Proc(1))):
            self.assertIsNone(dm._get_local_repo_digest("x"))

    def test_remote_manifest_digest_picks_arm64(self):
        arm = "sha256:" + "b" * 64
        amd = "sha256:" + "c" * 64
        manifest = (
            '{"manifests":['
            f'{{"platform":{{"architecture":"amd64","os":"linux"}},"digest":"{amd}"}},'
            f'{{"platform":{{"architecture":"arm64","os":"linux"}},"digest":"{arm}"}}'
            ']}'
        )
        with patch.object(dm.subprocess, "run", _Recorder(_Proc(0, stdout=manifest))):
            self.assertEqual(dm._get_remote_manifest_digest("x"), arm)

    def test_parse_digest_candidates(self):
        d1 = "sha256:" + "d" * 64
        d2 = "sha256:" + "e" * 64
        text = f"Name: foo\nDigest: {d1}\n  Platform: linux/arm64\n  Digest: {d2}\n"
        self.assertEqual(dm._parse_digest_candidates(text), {d1, d2})

    def test_remote_digest_candidates_uses_buildx_then_legacy(self):
        d1 = "sha256:" + "f" * 64
        # buildx imagetools inspect succeeds → its digests are used.
        rec = _Recorder(_Proc(1)).when(
            lambda a: "buildx" in a, _Proc(0, stdout=f"Digest: {d1}\n"))
        with patch.object(dm.subprocess, "run", rec):
            self.assertEqual(dm._remote_digest_candidates_for_ref("x"), {d1})

    def test_remote_digest_candidates_legacy_fallback(self):
        arm = "sha256:" + "1" * 64
        manifest = (
            '{"manifests":['
            f'{{"platform":{{"architecture":"arm64","os":"linux"}},"digest":"{arm}"}}]}}'
        )
        # buildx fails (rc1, no digests) → falls back to the legacy manifest probe.
        rec = _Recorder(_Proc(1)).when(
            lambda a: "manifest" in a, _Proc(0, stdout=manifest))
        with patch.object(dm.subprocess, "run", rec):
            self.assertEqual(dm._remote_digest_candidates_for_ref("x"), {arm})

    def test_image_is_current_membership(self):
        d = "sha256:" + "2" * 64
        with patch.object(dm, "_get_local_repo_digest", return_value=d):
            self.assertTrue(dm._image_is_current("x", remote_candidates={d, "sha256:" + "3" * 64}))
            self.assertFalse(dm._image_is_current("x", remote_candidates={"sha256:" + "3" * 64}))
        with patch.object(dm, "_get_local_repo_digest", return_value=None):
            self.assertFalse(dm._image_is_current("x", remote_candidates={d}))


# ── pull with fallback ───────────────────────────────────────────────────────


class TestPull(unittest.TestCase):
    def test_pull_one_image_success(self):
        rec = _Recorder(_Proc(0))
        with patch.object(dm.subprocess, "run", rec), patch.object(dm.time, "sleep"):
            self.assertTrue(dm._pull_one_image("img", 0, 1))
        self.assertTrue(rec.any_call(lambda a: _contains(a, "docker", "pull", "img")))

    def test_pull_one_image_retries_then_succeeds(self):
        rec = _Recorder().when(lambda a: True, _Proc(0))
        # First attempt fails, second succeeds.
        seq = [_Proc(1, stderr="net blip"), _Proc(0)]
        with patch.object(dm.subprocess, "run", side_effect=seq) as run, \
             patch.object(dm.time, "sleep") as slp:
            self.assertTrue(dm._pull_one_image("img", 0, 1))
        self.assertEqual(run.call_count, 2)
        self.assertTrue(slp.called)  # backoff slept before retry

    def test_pull_one_image_all_fail(self):
        with patch.object(dm.subprocess, "run", return_value=_Proc(1, stderr="boom")), \
             patch.object(dm.time, "sleep"):
            self.assertFalse(dm._pull_one_image("img", 0, 1, max_attempts=2))

    def test_pull_with_fallback_ghcr_ok(self):
        with patch.object(dm, "_host_reachable", return_value=True), \
             patch.object(dm, "_pull_one_image", return_value=True) as pull:
            self.assertTrue(dm._pull_image_with_fallback("ghcr.io/o/x:latest", 0, 1))
        pull.assert_called_once()

    def test_pull_with_fallback_hub_retag(self):
        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable", return_value=True), \
             patch.object(dm, "_pull_one_image", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=True) as fb:
            self.assertTrue(dm._pull_image_with_fallback("ghcr.io/o/x:latest", 0, 1))
        fb.assert_called_once()


class TestCheckForUpdates(unittest.TestCase):
    def test_skip_auto_pull(self):
        with patch.object(dm, "SKIP_AUTO_PULL", True):
            self.assertFalse(dm.check_for_updates())

    def test_offline_short_circuit(self):
        with patch.object(dm, "SKIP_AUTO_PULL", False), \
             patch.object(dm, "is_registry_reachable", return_value=False):
            self.assertFalse(dm.check_for_updates())

    def test_all_current_skips_pull(self):
        d = "sha256:" + "a" * 64
        with patch.object(dm, "SKIP_AUTO_PULL", False), \
             patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", return_value=d), \
             patch.object(dm, "_get_remote_digest_candidates", return_value={d}), \
             patch.object(dm, "_pull_image_with_fallback") as pull, \
             patch.object(dm, "_save_last_pull_info"):
            self.assertFalse(dm.check_for_updates())
        pull.assert_not_called()

    def test_stale_triggers_pull(self):
        local = "sha256:" + "a" * 64
        remote = {"sha256:" + "b" * 64}
        # images -q returns different ids before/after → bytes changed.
        rec = _Recorder(_Proc(0)).when(
            lambda a: "images" in a, _Proc(0, stdout="oldid"))
        with patch.object(dm, "SKIP_AUTO_PULL", False), \
             patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", side_effect=[local] * 10), \
             patch.object(dm, "_get_remote_digest_candidates", return_value=remote), \
             patch.object(dm, "_pull_image_with_fallback", return_value=True) as pull, \
             patch.object(dm, "_save_last_pull_info"), \
             patch.object(dm.subprocess, "run", rec):
            dm.check_for_updates()
        # One pull per image in ALL_IMAGES.
        self.assertEqual(pull.call_count, len(dm.ALL_IMAGES))


# ── lifecycle command construction (native, --no-deps, never `down`) ─────────


class _LifecycleBase(unittest.TestCase):
    def setUp(self):
        # Give _compose a real ENV_FILE so --env-file is included in the argv.
        fd, self.env_path = tempfile.mkstemp(suffix=".env")
        os.write(fd, b"REGISTRY=ghcr.io/x\n")
        os.close(fd)
        self._patch_env = patch.object(dm, "ENV_FILE", self.env_path)
        self._patch_env.start()

    def tearDown(self):
        self._patch_env.stop()
        try:
            os.unlink(self.env_path)
        except OSError:
            pass


class TestLifecycleCommands(_LifecycleBase):
    def test_start_manager_no_deps(self):
        rec = _Recorder(_Proc(0))
        with patch.object(dm.subprocess, "run", rec):
            self.assertTrue(dm.start_manager())
        self.assertTrue(rec.any_call(lambda a: _contains(
            a, "docker", "compose", "up", "-d", "--force-recreate", "--no-deps",
            "physical_ai_manager")))
        # The manager start never names the robot tier.
        self.assertFalse(rec.any_call(lambda a: "open_manipulator" in a))

    def test_start_robot_tier_both_named(self):
        rec = _Recorder(_Proc(0))
        with patch.object(dm.subprocess, "run", rec):
            self.assertTrue(dm.start_robot_tier())
        self.assertTrue(rec.any_call(lambda a: _contains(
            a, "up", "-d", "--force-recreate", "--no-deps",
            "open_manipulator", "physical_ai_server")))

    def test_restart_open_manipulator_only(self):
        rec = _Recorder(_Proc(0))
        with patch.object(dm.subprocess, "run", rec):
            self.assertTrue(dm.restart_open_manipulator())
        self.assertTrue(rec.any_call(lambda a: _contains(
            a, "up", "-d", "--force-recreate", "--no-deps", "open_manipulator")))
        self.assertFalse(rec.any_call(lambda a: "physical_ai_server" in a))

    def test_compose_includes_env_file(self):
        rec = _Recorder(_Proc(0))
        with patch.object(dm.subprocess, "run", rec):
            dm.start_manager()
        self.assertTrue(rec.any_call(lambda a: "--env-file" in a and self.env_path in a))

    def test_stop_robot_tier_uses_stop_and_rm_never_down(self):
        rec = _Recorder(_Proc(0))
        with patch.object(dm.subprocess, "run", rec):
            dm.stop_robot_tier()
        self.assertTrue(rec.any_call(lambda a: _contains(
            a, "stop", "open_manipulator", "physical_ai_server")))
        self.assertTrue(rec.any_call(lambda a: _contains(
            a, "rm", "-f", "open_manipulator", "physical_ai_server")))
        # THE invariant: never `compose down`.
        self.assertFalse(rec.any_call(lambda a: "down" in a))
        # And never touch the always-on manager.
        self.assertFalse(rec.any_call(lambda a: "physical_ai_manager" in a))

    def test_ensure_environment_stopped_present(self):
        with patch.object(dm, "get_container_status",
                          return_value={"open_manipulator": "running",
                                        "physical_ai_server": "exited",
                                        "physical_ai_manager": "running"}), \
             patch.object(dm, "stop_robot_tier") as stop:
            self.assertTrue(dm.ensure_environment_stopped())
        stop.assert_called_once()

    def test_ensure_environment_stopped_absent_noop(self):
        with patch.object(dm, "get_container_status",
                          return_value={"open_manipulator": "not found",
                                        "physical_ai_server": "not found",
                                        "physical_ai_manager": "running"}), \
             patch.object(dm, "stop_robot_tier") as stop:
            self.assertFalse(dm.ensure_environment_stopped())
        stop.assert_not_called()


class TestFactoryReset(_LifecycleBase):
    def test_factory_reset_wipes_by_suffix_never_down_manager_survives(self):
        vols = (
            "edubotics_ai_workspace\n"
            "edubotics_huggingface_cache\n"
            "edubotics_edubotics_calib\n"
            "some_other_volume\n"
        )
        rec = _Recorder(_Proc(0)).when(
            lambda a: "ls" in a, _Proc(0, stdout=vols))
        with patch.object(dm.subprocess, "run", rec):
            ok, msg = dm.factory_reset()
        self.assertTrue(ok)
        # Robot tier stopped (stop + rm -f), never `down`.
        self.assertTrue(rec.any_call(lambda a: _contains(a, "stop", "open_manipulator")))
        self.assertFalse(rec.any_call(lambda a: "down" in a))
        # The always-on manager is NOT stopped/removed (documented deviation).
        self.assertFalse(rec.any_call(
            lambda a: "physical_ai_manager" in a and ("stop" in a or "rm" in a)))
        # Only the three data volumes are removed (matched by suffix).
        rm_calls = [a for a in rec.argvs() if _contains(a, "volume", "rm")]
        self.assertEqual(len(rm_calls), 1)
        removed = rm_calls[0]
        self.assertIn("edubotics_ai_workspace", removed)
        self.assertIn("edubotics_huggingface_cache", removed)
        self.assertIn("edubotics_edubotics_calib", removed)
        self.assertNotIn("some_other_volume", removed)

    def test_factory_reset_no_volumes(self):
        rec = _Recorder(_Proc(0)).when(
            lambda a: "ls" in a, _Proc(0, stdout="some_other_volume\n"))
        with patch.object(dm.subprocess, "run", rec):
            ok, msg = dm.factory_reset()
        self.assertTrue(ok)
        self.assertIn("nichts zu löschen", msg)
        self.assertFalse(rec.any_call(lambda a: _contains(a, "volume", "rm")))


class TestContainerStatus(_LifecycleBase):
    def test_get_container_status(self):
        def run(argv, *a, **kw):
            name = argv[-1]
            return _Proc(0, stdout="running") if name == "physical_ai_manager" else _Proc(1)
        with patch.object(dm.subprocess, "run", side_effect=run):
            st = dm.get_container_status()
        self.assertEqual(st["physical_ai_manager"], "running")
        self.assertEqual(st["open_manipulator"], "not found")

    def test_manager_and_robot_tier_running(self):
        with patch.object(dm, "get_container_status",
                          return_value={"open_manipulator": "running",
                                        "physical_ai_server": "running",
                                        "physical_ai_manager": "running"}):
            self.assertTrue(dm.manager_running())
            self.assertTrue(dm.robot_tier_running())
        with patch.object(dm, "get_container_status",
                          return_value={"open_manipulator": "exited",
                                        "physical_ai_server": "running",
                                        "physical_ai_manager": "running"}):
            self.assertTrue(dm.manager_running())
            self.assertFalse(dm.robot_tier_running())


# ── set_leader_mode (.env regenerate + rollback) ─────────────────────────────


class TestSetLeaderMode(unittest.TestCase):
    def setUp(self):
        self._prev_domain = os.environ.get("EDUBOTICS_ROS_DOMAIN")
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "30"
        fd, self.env_path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        os.unlink(self.env_path)
        self._patch_env = patch.object(dm, "ENV_FILE", self.env_path)
        self._patch_env.start()
        self.cfg = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/serial/by-id/LEADER"),
            follower=ArmDevice(serial_path="/dev/serial/by-id/FOLLOWER"),
            cameras=[],
        )
        # Seed a both-arms .env (FOLLOWER_ONLY=0).
        from pi_agent import config_generator as cg
        cg.generate_env_file(self.cfg, self.env_path, follower_only=False)

    def tearDown(self):
        self._patch_env.stop()
        if self._prev_domain is None:
            os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        else:
            os.environ["EDUBOTICS_ROS_DOMAIN"] = self._prev_domain
        for p in (self.env_path, self.env_path + ".tmp"):
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_no_follower_refused(self):
        cfg = HardwareConfig(follower=None)
        ok, msg = dm.set_leader_mode(cfg, follower_only=True)
        self.assertFalse(ok)
        self.assertIn("Follower", msg)

    def test_both_arms_requires_leader(self):
        cfg = HardwareConfig(follower=ArmDevice(serial_path="/dev/f"), leader=None)
        ok, msg = dm.set_leader_mode(cfg, follower_only=False)
        self.assertFalse(ok)
        self.assertIn("Leader", msg)

    def test_switch_to_follower_only_success(self):
        from pi_agent import config_generator as cg
        with patch.object(dm, "restart_open_manipulator", return_value=True):
            ok, msg = dm.set_leader_mode(self.cfg, follower_only=True)
        self.assertTrue(ok)
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "1")

    def test_failed_restart_rolls_back_env(self):
        from pi_agent import config_generator as cg
        # Was both-arms (0); a failed switch to follower-only must roll back to 0.
        with patch.object(dm, "restart_open_manipulator", return_value=False):
            ok, msg = dm.set_leader_mode(self.cfg, follower_only=True)
        self.assertFalse(ok)
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "0")
        self.assertIn("vorherige Modus", msg)


if __name__ == "__main__":
    unittest.main()
