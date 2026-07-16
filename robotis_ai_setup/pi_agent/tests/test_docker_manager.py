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

    def test_ref_tag(self):
        # The two-segment ghcr.io/<owner> prefix and a registry PORT are the
        # traps here — a naive split(':') returns "5000/name" for the latter.
        self.assertEqual(
            dm._ref_tag("ghcr.io/svendanilborodun/physical-ai-server-opi:2.12.2"),
            "2.12.2",
        )
        self.assertEqual(dm._ref_tag("nettername/physical-ai-server-opi:latest"), "latest")
        self.assertEqual(dm._ref_tag("localhost:5000/physical-ai-server-opi:1.2.3"), "1.2.3")
        # No tag at all → docker's own default.
        self.assertEqual(dm._ref_tag("nettername/physical-ai-server-opi"), "latest")

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
        # assertEqual, not assertTrue: every outcome is a non-empty string now,
        # so assertTrue passes for PULL_MISSING too and pins nothing.
        with patch.object(dm, "_host_reachable", return_value=True), \
             patch.object(dm, "_pull_one_image", return_value=True) as pull:
            self.assertEqual(
                dm._pull_image_with_fallback("ghcr.io/o/x:latest", 0, 1), dm.PULL_OK)
        pull.assert_called_once()

    def test_pull_with_fallback_hub_retag(self):
        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable", return_value=True), \
             patch.object(dm, "_pull_one_image", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=True) as fb:
            self.assertEqual(
                dm._pull_image_with_fallback("ghcr.io/o/x:latest", 0, 1), dm.PULL_OK)
        fb.assert_called_once()

    def test_pull_images_reports_failure_when_an_image_cannot_be_pulled(self):
        """pull_images must compare the outcome against PULL_OK, not truthiness.

        _pull_image_with_fallback used to return a bool; it now returns one of
        three non-empty strings, and every one of them is truthy. The `not ...`
        form therefore reported success after a pull that failed outright.
        setup.sh's step 9 exits on this value, so the bench operator would be
        told „Images pulled." and would image a golden SD card with no images.
        """
        for outcome in (dm.PULL_MISSING, dm.PULL_TRANSIENT):
            with self.subTest(outcome=outcome):
                with patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/x:2.13.0"]), \
                     patch.object(dm, "_image_present_locally", return_value=False), \
                     patch.object(dm, "_pull_image_with_fallback", return_value=outcome):
                    self.assertFalse(dm.pull_images())

    def test_pull_images_succeeds_when_every_image_pulls(self):
        with patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/x:2.13.0"]), \
             patch.object(dm, "_image_present_locally", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_OK):
            self.assertTrue(dm.pull_images())

    def test_pull_both_registries_fail_names_the_version_in_german(self):
        """A release whose -opi images never published must say so, in German.

        This path became REACHABLE when release.yml started baking
        pi_agent/docker/versions.env: the agent now asks for `:X.Y.Z` instead of
        `:latest`, so a skipped opi build (its leg is continue-on-error) means
        both registries 404. That is the intended trade — before the pin the Pi
        silently ran main HEAD — but only if the message identifies the VERSION.
        A bare docker "manifest unknown" reads like a network fault and sends
        the teacher to the wrong problem.
        """
        logs = []
        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable", return_value=True), \
             patch.object(dm, "_pull_one_image", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=False):
            outcome = dm._pull_image_with_fallback(
                "ghcr.io/o/physical-ai-server-opi:2.13.0", 0, 1, log=logs.append
            )
        # Both registries REACHABLE and both refused it → the tag does not
        # exist. Classified MISSING (not merely "falsy"), because the update job
        # fails the whole job on this and must never do so for a transient.
        self.assertEqual(outcome, dm.PULL_MISSING)
        blob = "\n".join(logs)
        self.assertIn("[FEHLER]", blob)
        # The version must be named — that is the whole point of the message.
        self.assertIn("2.13.0", blob)
        # German, with literal umlauts (Rule §1 / ci.yml::german-strings-lint).
        self.assertIn("für dieses Release nicht veröffentlicht", blob)
        # And it must NOT be mistaken for a connectivity problem.
        self.assertIn("kein Netzwerkfehler", blob)
        # The old text blamed an unreachable GHCR on EVERY fallback, including
        # this one where GHCR is up and merely lacks the tag. Blaming the
        # network here contradicts the [FEHLER] verdict two lines later.
        self.assertNotIn("nicht erreichbar", blob)
        self.assertIn("bei GHCR nicht gefunden", blob)

    def test_pull_unreachable_registries_classify_transient_not_missing(self):
        """A Wi-Fi blip must NOT be reported as an unpublished release.

        This is the other half of the classification the update job depends on:
        TRANSIENT keeps today's graceful degrade onto the images already on
        disk, MISSING fails the job. Confusing the two either turns a blip into
        a red error or lets a never-published release report success.
        """
        logs = []
        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable", return_value=False), \
             patch.object(dm, "_pull_one_image", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=False):
            outcome = dm._pull_image_with_fallback(
                "ghcr.io/o/physical-ai-server-opi:2.13.0", 0, 1, log=logs.append
            )
        self.assertEqual(outcome, dm.PULL_TRANSIENT)
        blob = "\n".join(logs)
        # Must NOT accuse the release of being unpublished.
        self.assertNotIn("[FEHLER]", blob)
        self.assertNotIn("nicht veröffentlicht", blob)
        self.assertIn("keine Registry erreichbar", blob)

    def test_unreachable_ghcr_plus_hub_404_is_transient_not_missing(self):
        """GHCR unreachable + Hub lacking the tag must NOT accuse the release.

        MISSING is a claim about GHCR's contents, and GHCR is the only registry
        that can settle it: docker-publish.yml makes the Hub retag best-effort
        („the Docker Hub fallback for this tag may be stale until the next
        release"), so Hub 404s a tag GHCR serves all by itself. With ghcr.io
        unreachable we never got the authoritative answer — and this is exactly
        a school that firewalls ghcr.io, the network this product exists for.
        Blaming the release there is both wrong and self-contradictory: the same
        log already says „Primär-Registry (GHCR) nicht erreichbar".
        """
        logs = []
        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable",
                          side_effect=lambda h, *a, **k: h != "ghcr.io"), \
             patch.object(dm, "_pull_one_image", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=False):
            outcome = dm._pull_image_with_fallback(
                "ghcr.io/o/physical-ai-server-opi:2.13.0", 0, 1, log=logs.append
            )
        self.assertEqual(outcome, dm.PULL_TRANSIENT)
        blob = "\n".join(logs)
        self.assertNotIn("[FEHLER]", blob)
        self.assertNotIn("nicht veröffentlicht", blob)
        # GHCR was never asked, so the log must not claim what GHCR holds.
        self.assertNotIn("bei GHCR nicht vorhanden", blob)
        self.assertIn("Primär-Registry (GHCR) nicht erreichbar", blob)

    def test_missing_message_claims_only_what_was_established(self):
        """With Hub unreachable the message must not assert Hub's contents.

        GHCR answered and lacks the tag → MISSING is right. But the earlier
        wording („weder bei GHCR noch bei Docker Hub vorhanden") asserted Hub's
        contents too, which we never learned when Hub was unreachable.
        """
        logs = []
        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable",
                          side_effect=lambda h, *a, **k: h == "ghcr.io"), \
             patch.object(dm, "_pull_one_image", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=False):
            outcome = dm._pull_image_with_fallback(
                "ghcr.io/o/physical-ai-server-opi:2.13.0", 0, 1, log=logs.append
            )
        self.assertEqual(outcome, dm.PULL_MISSING)
        blob = "\n".join(logs)
        self.assertIn("2.13.0", blob)
        self.assertIn("bei GHCR nicht vorhanden", blob)
        self.assertNotIn("weder bei GHCR noch bei Docker Hub vorhanden", blob)

    def test_check_for_updates_reports_missing_images_to_caller(self):
        """check_for_updates must hand PULL_MISSING up via missing_out.

        Its bool return means "did bytes change", which is why the update job
        could not tell a never-published release from a no-op. missing_out is
        the seam that lets the caller fail loudly without changing the
        established non-fatal per-image contract.
        """
        missing = []
        with patch.object(dm, "SKIP_AUTO_PULL", False), \
             patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/physical-ai-server-opi:2.13.0"]), \
             patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", return_value=None), \
             patch.object(dm, "_get_remote_digest_candidates", return_value=set()), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_MISSING), \
             patch.object(dm, "_save_last_pull_info"):
            changed = dm.check_for_updates(missing_out=missing)
        self.assertEqual(missing, ["physical-ai-server-opi:2.13.0"])
        # The bool contract is unchanged: nothing was pulled, so nothing changed.
        self.assertFalse(changed)

    def test_check_for_updates_omits_missing_from_the_kept_version_claim(self):
        """A MISSING image must not claim the previous version is still in use.

        On a freshly flashed Pi there are no local images, so
        „aktuelle Version wird weiter verwendet" is false — and it directly
        contradicts the [FEHLER] logged moments earlier.
        """
        logs = []
        with patch.object(dm, "SKIP_AUTO_PULL", False), \
             patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/physical-ai-server-opi:2.13.0"]), \
             patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", return_value=None), \
             patch.object(dm, "_get_remote_digest_candidates", return_value=set()), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_MISSING), \
             patch.object(dm, "_save_last_pull_info"):
            dm.check_for_updates(log=logs.append, missing_out=[])
        self.assertNotIn("aktuelle Version wird weiter verwendet", "\n".join(logs))


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
        # `docker images -q` is probed once BEFORE and once AFTER each pull, and
        # check_for_updates reports "bytes changed" only when the two ids
        # DIFFER. The previous recorder answered "oldid" to both, so the
        # comment below described a comparison the mock made impossible and the
        # test asserted only that a pull was attempted.
        ids = iter(["oldid", "newid"] * len(dm.ALL_IMAGES))

        def fake_run(argv, *a, **kw):
            if "images" in argv:
                return _Proc(0, stdout=next(ids, "newid"))
            return _Proc(0)

        # PULL_OK, not True: check_for_updates compares `outcome != PULL_OK`, so
        # a bool mock silently routed this "stale triggers pull" test down the
        # „Übersprungen" SKIP branch — the opposite of what it documents.
        with patch.object(dm, "SKIP_AUTO_PULL", False), \
             patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", side_effect=[local] * 10), \
             patch.object(dm, "_get_remote_digest_candidates", return_value=remote), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_OK) as pull, \
             patch.object(dm, "_save_last_pull_info"), \
             patch.object(dm.subprocess, "run", fake_run):
            changed = dm.check_for_updates()
        # One pull per image in ALL_IMAGES.
        self.assertEqual(pull.call_count, len(dm.ALL_IMAGES))
        # And the pull was really taken as an update: old id → new id.
        self.assertTrue(changed)


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

    def test_ensure_environment_stopped_error_fails_safe(self):
        # A docker-inspect failure maps a container to "error". Treating that
        # as ABSENT would fail-OPEN the mandatory pre-scan stop (Dynamixel bus
        # exclusivity) — a persistent "error" must retry once, then stop anyway.
        err = {"open_manipulator": "error",
               "physical_ai_server": "error",
               "physical_ai_manager": "running"}
        with patch.object(dm, "get_container_status", return_value=err) as st, \
             patch.object(dm, "stop_robot_tier") as stop:
            self.assertTrue(dm.ensure_environment_stopped())
        self.assertEqual(st.call_count, 2)  # probed, then retried once
        stop.assert_called_once()           # fail-SAFE: idempotent stop ran

    def test_ensure_environment_stopped_error_then_recovered_absent(self):
        # The retry is real: a transient error that resolves to "not found"
        # on the second probe must NOT trigger the stop.
        err = {"open_manipulator": "error", "physical_ai_server": "not found",
               "physical_ai_manager": "running"}
        gone = {"open_manipulator": "not found", "physical_ai_server": "not found",
                "physical_ai_manager": "running"}
        with patch.object(dm, "get_container_status", side_effect=[err, gone]), \
             patch.object(dm, "stop_robot_tier") as stop:
            self.assertFalse(dm.ensure_environment_stopped())
        stop.assert_not_called()

    def test_ensure_environment_stopped_error_then_running_stops(self):
        # Transient error resolving to a RUNNING container → normal stop path.
        err = {"open_manipulator": "error", "physical_ai_server": "not found",
               "physical_ai_manager": "running"}
        run = {"open_manipulator": "running", "physical_ai_server": "not found",
               "physical_ai_manager": "running"}
        with patch.object(dm, "get_container_status", side_effect=[err, run]), \
             patch.object(dm, "stop_robot_tier") as stop:
            self.assertTrue(dm.ensure_environment_stopped())
        stop.assert_called_once()


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

    def test_switch_preserves_robot_type(self):
        # The forward write must carry the managed EDUBOTICS_ROBOT_TYPE through
        # (the GUI's _rs_set_leader_mode scar: omitting it silently rewrites
        # the type to the default on every toggle).
        from pi_agent import config_generator as cg
        cg.generate_env_file(self.cfg, self.env_path, follower_only=False,
                             robot_type="omx_follower")
        with patch.object(dm, "restart_open_manipulator", return_value=True):
            ok, _ = dm.set_leader_mode(self.cfg, follower_only=True)
        self.assertTrue(ok)
        self.assertEqual(cg.read_env_var("EDUBOTICS_ROBOT_TYPE", self.env_path),
                         "omx_follower")

    def test_rollback_preserves_robot_type(self):
        # And the rollback write must carry it too.
        from pi_agent import config_generator as cg
        cg.generate_env_file(self.cfg, self.env_path, follower_only=False,
                             robot_type="omx_follower")
        with patch.object(dm, "restart_open_manipulator", return_value=False):
            ok, _ = dm.set_leader_mode(self.cfg, follower_only=True)
        self.assertFalse(ok)
        self.assertEqual(cg.read_env_var("EDUBOTICS_ROBOT_TYPE", self.env_path),
                         "omx_follower")


if __name__ == "__main__":
    unittest.main()
