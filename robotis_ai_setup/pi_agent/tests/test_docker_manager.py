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

    def test_ghcr_pull_is_attempted_even_when_the_host_probe_says_unreachable(self):
        """M3 / GUI parity: NO host-reachability pre-gate on the primary pull.

        The old `if primary_up:` gate probed ghcr.io:443 from this host and
        skipped the GHCR pull on any blip. Behind one classroom NAT that diverts
        the WHOLE fleet onto Docker Hub's anonymous rate wall the instant the
        probe flaps — which is why the GUI deliberately removed the same gate.
        The probe survives ONLY to classify a FAILED pull (below), so a
        succeeding pull must never even consult it.
        """
        with patch.object(dm, "_host_reachable", return_value=False) as probe, \
             patch.object(dm, "_pull_one_image", return_value=True) as pull, \
             patch.object(dm, "_pull_fallback_and_retag") as fb:
            self.assertEqual(
                dm._pull_image_with_fallback("ghcr.io/o/x:latest", 0, 1), dm.PULL_OK)
        pull.assert_called_once()   # attempted despite the "unreachable" probe
        fb.assert_not_called()      # and never needlessly diverted to Hub
        probe.assert_not_called()   # happy path pays for no probe at all

    def test_an_unreachable_ghcr_does_not_burn_the_remaining_attempts(self):
        """MINOR 7. De-gating the FIRST attempt was right; burning attempts 2-3
        against a host that provably does not answer is not.

        On a school that silently DROPs ghcr.io, docker can hang each attempt to
        the 600 s cap — up to ~30 min per image before the Hub fallback is even
        tried, on every pull path including „Umgebung starten". One attempt, then
        the reachability verdict routes straight to Hub.
        """
        attempts = {"n": 0}

        def failing_pull(argv, *a, **kw):
            if "pull" in argv:
                attempts["n"] += 1
                return _Proc(1, stderr="i/o timeout")
            return _Proc(0)

        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=False), \
             patch.object(dm.subprocess, "run", side_effect=failing_pull), \
             patch.object(dm.time, "sleep"):
            self.assertEqual(
                dm._pull_image_with_fallback("ghcr.io/o/x:2.13.0", 0, 1), dm.PULL_TRANSIENT)
        self.assertEqual(attempts["n"], 1, "retries against a dead host are pure wall-clock")

    def test_a_reachable_ghcr_still_gets_every_retry(self):
        # The case the de-gating exists to protect: a transient 5xx on a host
        # that IS answering must keep its full budget rather than diverting the
        # whole classroom onto Docker Hub's anonymous rate wall.
        attempts = {"n": 0}

        def failing_pull(argv, *a, **kw):
            if "pull" in argv:
                attempts["n"] += 1
                return _Proc(1, stderr="503 Service Unavailable")
            return _Proc(0)

        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable", return_value=True), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=False), \
             patch.object(dm.subprocess, "run", side_effect=failing_pull), \
             patch.object(dm.time, "sleep"):
            dm._pull_image_with_fallback("ghcr.io/o/x:2.13.0", 0, 1)
        self.assertEqual(attempts["n"], 3)

    def test_the_reachability_probe_runs_at_most_once_per_image(self):
        # It serves BOTH the retry gate and the TRANSIENT/MISSING classifier;
        # each probe costs up to NETWORK_PROBE_TIMEOUT, so memoise it.
        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable", return_value=False) as probe, \
             patch.object(dm, "_pull_one_image", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=False):
            dm._pull_image_with_fallback("ghcr.io/o/x:2.13.0", 0, 1)
        primary_probes = [c for c in probe.call_args_list if c.args and c.args[0] == "ghcr.io"]
        self.assertEqual(len(primary_probes), 1, probe.call_args_list)

    def test_an_unreachable_primary_still_classifies_a_failed_pull_as_transient(self):
        """The probe's remaining job. MISSING is a claim about GHCR's CONTENTS
        and only a reachable GHCR can settle it — the Hub retag is explicitly
        best-effort and may legitimately 404 a tag GHCR serves. So an
        unreachable primary must yield TRANSIENT whatever Hub said, or a school
        that blocks ghcr.io would be told its release was never published."""
        with patch.object(dm, "REGISTRY", "ghcr.io/o"), \
             patch.object(dm, "REGISTRY_FALLBACK", "nettername"), \
             patch.object(dm, "_host_reachable", return_value=False), \
             patch.object(dm, "_pull_one_image", return_value=False), \
             patch.object(dm, "_pull_fallback_and_retag", return_value=False):
            self.assertEqual(
                dm._pull_image_with_fallback("ghcr.io/o/x:2.13.0", 0, 1), dm.PULL_TRANSIENT)

    def test_pull_images_prunes_superseded_tags_after_a_successful_pull(self):
        with patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/x:2.13.0"]), \
             patch.object(dm, "_image_present_locally", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_OK), \
             patch.object(dm, "prune_superseded_tags") as prune:
            self.assertTrue(dm.pull_images())
        prune.assert_called_once()

    def test_pull_images_does_not_prune_after_a_failed_pull(self):
        # Never untag the version on disk when the new one didn't arrive — that
        # is the only thing standing between the student and a dead Pi.
        with patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/x:2.13.0"]), \
             patch.object(dm, "_image_present_locally", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_MISSING), \
             patch.object(dm, "prune_superseded_tags") as prune:
            self.assertFalse(dm.pull_images())
        prune.assert_not_called()

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
        setup.sh's pull_images step reports on this value, so the bench operator would be
        told „Images pulled." and would image a golden SD card with no images.
        """
        for outcome in (dm.PULL_MISSING, dm.PULL_TRANSIENT):
            with self.subTest(outcome=outcome):
                with patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/x:2.13.0"]), \
                     patch.object(dm, "_image_present_locally", return_value=False), \
                     patch.object(dm, "_pull_image_with_fallback", return_value=outcome):
                    self.assertFalse(dm.pull_images())

    def test_pull_images_succeeds_when_every_image_pulls(self):
        # prune_superseded_tags MUST stay mocked. It reads the module-level
        # IMAGE_NAMES / REGISTRY / REGISTRY_FALLBACK — which patching ALL_IMAGES
        # does NOT cover — so the real one shells out to `docker images` +
        # `docker image rm` against the PRODUCTION repos on any host with a live
        # daemon (a maintainer's Linux box, or the Pi itself), untagging a ~5-6 GB
        # image set this suite never pulled. CI never catches it: a GHA runner has
        # docker but no -opi images, so `docker images <repo>` is empty and no rm
        # is issued — the leak is green everywhere it is harmless and destructive
        # exactly where it is not. This module promises "every docker invocation
        # is mocked … runs on any host with no docker daemon"; keep that true.
        with patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/x:2.13.0"]), \
             patch.object(dm, "_image_present_locally", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_OK), \
             patch.object(dm, "prune_superseded_tags"):
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
        # subprocess.run stays mocked: check_for_updates brackets each pull with
        # a real `docker images -q <image>` id probe, which otherwise reaches the
        # host's live daemon (see the note on
        # test_pull_images_succeeds_when_every_image_pulls).
        with patch.object(dm, "SKIP_AUTO_PULL", False), \
             patch.object(dm, "ALL_IMAGES", ["ghcr.io/o/physical-ai-server-opi:2.13.0"]), \
             patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", return_value=None), \
             patch.object(dm, "_get_remote_digest_candidates", return_value=set()), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_MISSING), \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))), \
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
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))), \
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


# ── superseded-tag prune (H1: the eMMC filler) ───────────────────────────────


def _run_check_for_updates(first_time: bool):
    """Drive check_for_updates with the pull mocked OK and no network.

    `check_for_updates` brackets each pull with `docker images -q <image>` to
    see whether the local bytes changed. ``first_time=True`` models an image
    ABSENT before the pull ("" → an id) — which is what every image looks like
    on the post-self-update boot pull, since the new :X.Y.Z tag is absent by
    definition. ``False`` models an unchanged id.
    """
    if first_time:
        ids = iter(["", "sha256:new"] * len(dm.ALL_IMAGES))
    else:
        ids = iter(["sha256:same", "sha256:same"] * len(dm.ALL_IMAGES))

    def fake_run(argv, *a, **kw):
        if "images" in argv and "-q" in argv:
            return _Proc(0, stdout=next(ids, ""))
        return _Proc(0)

    with patch.object(dm, "is_registry_reachable", return_value=True), \
         patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_OK), \
         patch.object(dm, "_get_local_repo_digest", return_value=None), \
         patch.object(dm, "_get_remote_digest_candidates", return_value=set()), \
         patch.object(dm.subprocess, "run", side_effect=fake_run), \
         patch.object(dm, "_save_last_pull_info"):
        return dm.check_for_updates()


class TestPruneSupersededTags(unittest.TestCase):
    """`docker image prune -f` is dangling-ONLY, so after an update pulls
    :X.Y.Z the previously-installed :X.Y-1 TAGGED images survive. On the Pi's
    soldered eMMC — which also holds the OS and the student's datasets — that is
    a full ~5-6 GB image set per release until dockerd and the always-on manager
    die together."""

    def _run_prune(self, listed_tags, rm_proc=None):
        """Drive prune_superseded_tags with `docker images` returning
        ``listed_tags`` for every repo. Returns (removed_count, recorder)."""
        rec = _Recorder(_Proc(0))
        rec.when(lambda a: "images" in a,
                 _Proc(0, stdout="\n".join(listed_tags) + "\n"))
        if rm_proc is not None:
            rec.when(lambda a: "rm" in a, rm_proc)
        with patch.object(dm.subprocess, "run", rec), \
             patch.object(dm, "IMAGE_TAG", "2.14.0"):
            removed = dm.prune_superseded_tags()
        return removed, rec

    def test_keeps_the_current_tag_and_removes_superseded_ones(self):
        removed, rec = self._run_prune(["2.14.0", "2.13.0", "2.12.1"])
        rm_args = [a for a in rec.argvs() if "rm" in a]
        rmd = {a[-1] for a in rm_args}
        # The running release must survive — removing it would take the
        # always-on manager (and the wizard, the only repair trigger) with it.
        self.assertFalse(any(r.endswith(":2.14.0") for r in rmd), rmd)
        self.assertTrue(any(r.endswith(":2.13.0") for r in rmd), rmd)
        self.assertTrue(any(r.endswith(":2.12.1") for r in rmd), rmd)
        self.assertEqual(removed, len(rm_args))

    def test_covers_both_registries_times_every_opi_image(self):
        # A Pi that fell back to the Hub twin holds `nettername/...` tags; only
        # untagging GHCR names would leave those on the disk forever.
        _, rec = self._run_prune(["2.13.0"])
        rmd = {a[-1] for a in rec.argvs() if "rm" in a}
        for name in dm.IMAGE_NAMES:
            self.assertTrue(any(r == f"{dm.REGISTRY}/{name}:2.13.0" for r in rmd), name)
            self.assertTrue(
                any(r == f"{dm.REGISTRY_FALLBACK}/{name}:2.13.0" for r in rmd), name)
        # The -opi flavour is the only one the Pi runs.
        self.assertTrue(all("opi" in n for n in dm.IMAGE_NAMES))

    def test_dangling_none_tag_is_skipped(self):
        # `<none>` is not a tag you can `image rm repo:<none>` — that is what
        # `image prune -f` is for, and check_for_updates still runs it after.
        _, rec = self._run_prune(["<none>", "2.14.0"])
        self.assertFalse([a for a in rec.argvs() if "rm" in a])

    def test_an_in_use_image_is_left_alone_and_not_counted(self):
        # `image rm` (no -f) refuses an image a running container uses, so a
        # live manager / robot tier is never yanked out from under itself. The
        # old tag simply lingers one more cycle.
        removed, _ = self._run_prune(
            ["2.13.0"],
            rm_proc=_Proc(1, stderr="conflict: unable to remove, container is using it"))
        self.assertEqual(removed, 0)

    def test_rm_is_never_forced(self):
        """The `-f` that would make the prune "actually work" is the one that
        breaks it. `image rm` (no -f) is what makes the in-use refusal above a
        real property rather than a hopeful comment: the sibling test fakes the
        refusal with a canned returncode REGARDLESS of argv, so it passes just
        as happily against `image rm -f` — which does NOT refuse. Forcing would
        untag an image the always-on manager or a live robot tier is running:
        the container survives, but the tag is gone and the next `compose up`
        must re-pull ~5-6 GB over a school link. Assert on the argv itself so
        the safety property is pinned by the code, not by the comment.
        """
        _, rec = self._run_prune(["2.13.0", "2.12.0"])
        rm_calls = [a for a in rec.argvs() if "rm" in a]
        self.assertTrue(rm_calls, "no rm was issued — the assertion below would be vacuous")
        for argv in rm_calls:
            self.assertNotIn("-f", argv, f"prune must never force-remove: {argv}")
            self.assertNotIn("--force", argv, f"prune must never force-remove: {argv}")

    def test_never_raises_when_docker_is_absent(self):
        with patch.object(dm.subprocess, "run", side_effect=FileNotFoundError("no docker")):
            self.assertEqual(dm.prune_superseded_tags(), 0)  # best-effort

    def test_a_failed_images_listing_is_skipped(self):
        rec = _Recorder(_Proc(0))
        rec.when(lambda a: "images" in a, _Proc(1, stderr="daemon down"))
        with patch.object(dm.subprocess, "run", rec):
            self.assertEqual(dm.prune_superseded_tags(), 0)
        self.assertFalse([a for a in rec.argvs() if "rm" in a])

    def test_check_for_updates_prunes_after_a_real_pull(self):
        # The integration that matters: without this the pinned per-release
        # tags accumulate on every update.
        with patch.object(dm, "prune_superseded_tags") as prune:
            self.assertTrue(_run_check_for_updates(first_time=True))
        prune.assert_called_once()

    def test_check_for_updates_does_not_prune_after_a_PARTIAL_upgrade(self):
        """C1. THE one that turns a slow link into a dead Pi.

        Scenario: a Pi on :2.13.0 runs the wizard's „Jetzt aktualisieren".
        _run_update_job's FIRST step is stop_robot_tier (`stop` + `rm -f`), so
        nothing references physical-ai-server-opi:2.13.0 any more. Two small
        images pull fine at :2.14.0; the ~5-6 GB server times out three times on
        the school link → PULL_TRANSIENT (non-fatal by design, so `missing_out`
        stays empty and the job still reports „Aktualisierung abgeschlossen.").

        With `if any_updated:` alone, that partial success pruned every non-
        current tag across IMAGE_NAMES × both registries — including the still-
        working :2.13.0 server image, which `image rm` no longer refuses because
        its container was just removed. The Pi ends with NO physical-ai-server
        image at ANY tag: „Umgebung starten" is broken until a full 5-6 GB pull
        succeeds, and the EDUBOTICS_IMAGE_TAG rollback is gone too. Mirrors the
        GUI's `any_updated and not any_failed` guard.
        """
        first = {"done": False}

        def one_ok_then_transient(image, i, total, log=None, attempts=3):
            if not first["done"]:
                first["done"] = True
                return dm.PULL_OK
            return dm.PULL_TRANSIENT

        # The successful image is a first-time pull ("" → an id), so any_updated
        # is genuinely True — this test would be vacuous otherwise.
        ids = iter(["", "sha256:new"] + ["sha256:same", "sha256:same"] * 8)

        def fake_run(argv, *a, **kw):
            if "images" in argv and "-q" in argv:
                return _Proc(0, stdout=next(ids, ""))
            return _Proc(0)

        with patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", return_value=None), \
             patch.object(dm, "_get_remote_digest_candidates", return_value=set()), \
             patch.object(dm, "_pull_image_with_fallback", side_effect=one_ok_then_transient), \
             patch.object(dm, "prune_superseded_tags") as prune, \
             patch.object(dm, "_save_last_pull_info"), \
             patch.object(dm.subprocess, "run", side_effect=fake_run):
            changed = dm.check_for_updates(log=lambda _m: None)
        self.assertTrue(changed, "bytes really did change — the guard, not the trigger, is under test")
        prune.assert_not_called()

    def test_a_missing_release_also_blocks_the_prune(self):
        # PULL_MISSING and PULL_TRANSIENT are different diagnoses but identical
        # here: the new bytes did not arrive, so the old ones must stay.
        def one_ok_then_missing(image, i, total, log=None, attempts=3, _s=[False]):
            if not _s[0]:
                _s[0] = True
                return dm.PULL_OK
            return dm.PULL_MISSING

        ids = iter(["", "sha256:new"] + ["sha256:same", "sha256:same"] * 8)

        def fake_run(argv, *a, **kw):
            if "images" in argv and "-q" in argv:
                return _Proc(0, stdout=next(ids, ""))
            return _Proc(0)

        with patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", return_value=None), \
             patch.object(dm, "_get_remote_digest_candidates", return_value=set()), \
             patch.object(dm, "_pull_image_with_fallback", side_effect=one_ok_then_missing), \
             patch.object(dm, "prune_superseded_tags") as prune, \
             patch.object(dm, "_save_last_pull_info"), \
             patch.object(dm.subprocess, "run", side_effect=fake_run):
            dm.check_for_updates(log=lambda _m: None, missing_out=[])
        prune.assert_not_called()

    def test_an_explicit_image_tag_pin_disables_the_prune_entirely(self):
        """EDUBOTICS_IMAGE_TAG is the operator's one-variable rollback (set in
        /etc/edubotics/.env, which is also the unit's EnvironmentFile). Under a
        pin, the tags the prune would remove are exactly the ones the operator
        needs to roll FORWARD to — untagging them turns a reversible pin into a
        one-way trip over a 5-6 GB re-pull. Whoever manages tags by hand owns the
        disk too."""
        rec = _Recorder(_Proc(0))
        rec.when(lambda a: "images" in a, _Proc(0, stdout="2.13.0\n2.12.0\n"))
        with patch.dict(os.environ, {"EDUBOTICS_IMAGE_TAG": "2.13.0"}), \
             patch.object(dm.subprocess, "run", rec), \
             patch.object(dm, "IMAGE_TAG", "2.13.0"):
            self.assertEqual(dm.prune_superseded_tags(), 0)
        self.assertFalse([a for a in rec.argvs() if "rm" in a])

    def test_check_for_updates_does_not_prune_when_nothing_changed(self):
        # Every image already current → no pull, no new tags, nothing
        # superseded. Pruning anyway would be pointless eMMC churn on every
        # no-op update check.
        d = "sha256:" + "a" * 64
        with patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", return_value=d), \
             patch.object(dm, "_get_remote_digest_candidates", return_value={d}), \
             patch.object(dm, "prune_superseded_tags") as prune, \
             patch.object(dm, "_save_last_pull_info"):
            self.assertFalse(dm.check_for_updates())
        prune.assert_not_called()


# ── M1/F7: the first-time-pull miscount ──────────────────────────────────────


class TestFirstTimePullCounts(unittest.TestCase):
    def test_absent_image_pulled_for_the_first_time_counts_as_an_update(self):
        """`old_id == ""` means the image wasn't present before this run.

        That IS an update, but the old `if old_id and new_id and old_id != new_id`
        guard dropped it: any_updated stayed False, the prune never ran and the
        log claimed nothing changed after a real pull. It bites hardest on the
        post-self-update boot pull, where EVERY image is a first-time pull (the
        new :X.Y.Z tag is absent by definition) — so the prune the C1 path needs
        would never have fired at all.
        """
        with patch.object(dm, "prune_superseded_tags"):
            self.assertTrue(_run_check_for_updates(first_time=True))

    def test_an_unchanged_image_is_still_not_counted(self):
        # The guard was loosened from `old_id and new_id and old_id != new_id`
        # to `new_id and old_id != new_id` — only the empty-OLD case moved. An
        # image whose id is unchanged must still not count (that is the digest
        # pre-check's whole economy).
        with patch.object(dm, "prune_superseded_tags"):
            self.assertFalse(_run_check_for_updates(first_time=False))


# ── lifecycle command construction (native, --no-deps, never `down`) ─────────


class _LifecycleBase(unittest.TestCase):
    def setUp(self):
        # Give _compose a real ENV_FILE so --env-file is included in the argv.
        fd, self.env_path = tempfile.mkstemp(suffix=".env")
        os.write(fd, b"REGISTRY=ghcr.io/x\n")
        os.close(fd)
        self._patch_env = patch.object(dm, "ENV_FILE", self.env_path)
        self._patch_env.start()
        # start_robot_tier now pre-pulls (H3), and _compose_pull's first gate is
        # a REAL TCP probe of ghcr.io:443. This module promises every docker /
        # network call is mocked, so pin it: reachable = the interesting path
        # (an unreachable registry short-circuits the pull away entirely).
        self._patch_reach = patch.object(dm, "is_registry_reachable", return_value=True)
        self._patch_reach.start()

    def tearDown(self):
        self._patch_reach.stop()
        self._patch_env.stop()
        try:
            os.unlink(self.env_path)
        except OSError:
            pass


# ── H3: resilient pre-pull before compose up ─────────────────────────────────


class TestPrePull(_LifecycleBase):
    """`compose up --force-recreate`'s default pull_policy fetches an ABSENT
    image from ${REGISTRY} (GHCR) ONLY — no Hub twin, no retry, bounded by the
    `up` wall-clock. On the ~5-6 GB opi server image that is a GHCR-degraded
    school's hard stop."""

    def test_robot_tier_pulls_resiliently_before_up(self):
        seen = []
        with patch.object(dm, "_image_is_current", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback",
                          side_effect=lambda img, i, t, log=None, attempts=3:
                          seen.append(img) or dm.PULL_OK), \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            self.assertTrue(dm.start_robot_tier())
        # Both robot-tier images, through the fallback-capable path...
        self.assertIn(dm.IMAGE_OPEN_MANIPULATOR, seen)
        self.assertIn(dm.IMAGE_PHYSICAL_AI_SERVER, seen)
        # ...and never the manager (--no-deps leaves it running).
        self.assertNotIn(dm.IMAGE_PHYSICAL_AI_MANAGER, seen)

    def test_the_interactive_start_path_pulls_with_a_one_shot_budget(self):
        """M6. „Umgebung starten" runs on the HTTP request thread that holds the
        agent's _lifecycle_lock, so its pull budget is what bounds how long
        „Stoppen" (and every other wizard control) can only answer 503. At the
        patient default a GHCR-degraded school burns 3×600 s on GHCR plus 2×600 s
        on Hub PER image before `up` even starts. One shot per registry keeps the
        worst case survivable; `_compose_up` still self-heals an absent image.
        """
        budgets = []
        with patch.object(dm, "_image_is_current", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback",
                          side_effect=lambda img, i, t, log=None, attempts=3:
                          budgets.append(attempts) or dm.PULL_OK), \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            dm.start_robot_tier()
        self.assertTrue(budgets)
        self.assertEqual(set(budgets), {1}, budgets)

    def test_the_update_path_keeps_the_patient_budget(self):
        """The other half of the trade: on /update nobody is watching a lock, so
        retrying is the right call — do not let the start-path cap leak here."""
        budgets = []
        with patch.object(dm, "is_registry_reachable", return_value=True), \
             patch.object(dm, "_get_local_repo_digest", return_value=None), \
             patch.object(dm, "_get_remote_digest_candidates", return_value=set()), \
             patch.object(dm, "_pull_image_with_fallback",
                          side_effect=lambda img, i, t, log=None, attempts=3:
                          budgets.append(attempts) or dm.PULL_OK), \
             patch.object(dm, "prune_superseded_tags"), \
             patch.object(dm, "_save_last_pull_info"), \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            dm.check_for_updates()
        self.assertTrue(budgets)
        self.assertEqual(set(budgets), {3}, budgets)

    def test_robot_tier_skips_the_pull_when_images_are_current(self):
        with patch.object(dm, "_image_is_current", return_value=True), \
             patch.object(dm, "_pull_image_with_fallback") as pull, \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            self.assertTrue(dm.start_robot_tier())
        pull.assert_not_called()

    def test_a_transient_pull_failure_still_starts_the_tier(self):
        # Best-effort by design: an offline classroom must still be able to
        # recreate on the images already on disk.
        with patch.object(dm, "_image_is_current", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_TRANSIENT), \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            self.assertTrue(dm.start_robot_tier())

    def test_an_unreachable_registry_short_circuits_the_pull(self):
        with patch.object(dm, "is_registry_reachable", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback") as pull, \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            self.assertTrue(dm.start_robot_tier())
        pull.assert_not_called()

    def test_skip_auto_pull_short_circuits_the_pull(self):
        with patch.object(dm, "SKIP_AUTO_PULL", True), \
             patch.object(dm, "_pull_image_with_fallback") as pull, \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            self.assertTrue(dm.start_robot_tier())
        pull.assert_not_called()

    # ── the manager half: ACQUISITION only, never a freshness refresh ────────

    def test_manager_start_does_not_pull_when_the_image_is_present(self):
        """The fast-boot contract. start_cloud_only runs on EVERY boot before
        the wizard serves; a freshness check would put a registry probe + a
        manifest inspect on the path a teacher is watching and make the UI's
        arrival hostage to the school network."""
        with patch.object(dm, "_image_present_locally", return_value=True), \
             patch.object(dm, "is_registry_reachable") as reach, \
             patch.object(dm, "_image_is_current") as current, \
             patch.object(dm, "_pull_image_with_fallback") as pull, \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            self.assertTrue(dm.start_manager())
        pull.assert_not_called()
        # Not one network call: the local-presence test is the whole gate.
        reach.assert_not_called()
        current.assert_not_called()

    def test_manager_start_acquires_an_absent_image_resiliently(self):
        """The chicken-and-egg break. Bench provisioning warns but SHIPS a Pi
        whose pull was incomplete; the only in-field repair trigger (/update)
        lives in the wizard that the missing manager image is supposed to serve.
        Pulling the absent image through the fallback path lets that Pi self-heal
        on the next boot — over Hub if GHCR is blocked."""
        seen = []
        with patch.object(dm, "_image_present_locally", return_value=False), \
             patch.object(dm, "_image_is_current", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback",
                          side_effect=lambda img, i, t, log=None, attempts=3:
                          seen.append(img) or dm.PULL_OK), \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))):
            self.assertTrue(dm.start_manager())
        self.assertEqual(seen, [dm.IMAGE_PHYSICAL_AI_MANAGER])

    def test_manager_start_still_ups_when_the_acquisition_fails(self):
        with patch.object(dm, "_image_present_locally", return_value=False), \
             patch.object(dm, "_image_is_current", return_value=False), \
             patch.object(dm, "_pull_image_with_fallback", return_value=dm.PULL_MISSING), \
             patch.object(dm.subprocess, "run", _Recorder(_Proc(0))) as rec:
            self.assertTrue(dm.start_manager())
        self.assertTrue(rec.any_call(lambda a: _contains(a, "up", "physical_ai_manager")))


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
