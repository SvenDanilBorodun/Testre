"""Cross-platform unit tests for the 2.2.4 auto-pull-on-GUI-start hardening.

Covers the three layers in `docker_manager.check_for_updates`:
  1. Offline short-circuit via `is_registry_reachable` (GHCR then Docker Hub).
  2. Manifest-digest pre-check (skip pull when local == remote).
  3. Last-pull persistence + freshness banner.

Plus the GHCR→Docker Hub registry fallback (`_pull_image_with_fallback`,
`_fallback_ref`, `_registry_host`) added in the GHCR migration.

These tests intentionally don't depend on Windows-specific argv shapes (the
existing `test_docker_manager_wsl.py` covers that for the Windows runner) so
they run on the same Linux/macOS CI host as the rest of the host-side suite.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch


# Make the `gui` package importable from the repo-root layout used by CI.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gui.app import docker_manager  # noqa: E402
from gui.app.constants import ALL_IMAGES, IMAGE_FRESHNESS_WARN_DAYS  # noqa: E402


class TestRegistryHelpers(unittest.TestCase):
    """Registry host extraction + fallback-ref derivation."""

    def test_registry_host_for_ghcr(self):
        self.assertEqual(
            docker_manager._registry_host("ghcr.io/svendanilborodun"), "ghcr.io"
        )

    def test_registry_host_for_bare_owner_is_docker_hub(self):
        self.assertEqual(
            docker_manager._registry_host("nettername"), "registry-1.docker.io"
        )

    def test_registry_host_strips_port(self):
        self.assertEqual(docker_manager._registry_host("localhost:5000"), "localhost")

    def test_fallback_ref_swaps_primary_for_fallback(self):
        with patch.object(docker_manager, "REGISTRY", "ghcr.io/svendanilborodun"), \
             patch.object(docker_manager, "REGISTRY_FALLBACK", "nettername"):
            self.assertEqual(
                docker_manager._fallback_ref(
                    "ghcr.io/svendanilborodun/physical-ai-server:2.8.1"
                ),
                "nettername/physical-ai-server:2.8.1",
            )

    def test_fallback_ref_none_for_non_primary_ref(self):
        with patch.object(docker_manager, "REGISTRY", "ghcr.io/svendanilborodun"), \
             patch.object(docker_manager, "REGISTRY_FALLBACK", "nettername"):
            self.assertIsNone(docker_manager._fallback_ref("nettername/foo:1"))

    def test_fallback_ref_none_when_fallback_equals_primary(self):
        with patch.object(docker_manager, "REGISTRY", "nettername"), \
             patch.object(docker_manager, "REGISTRY_FALLBACK", "nettername"):
            self.assertIsNone(docker_manager._fallback_ref("nettername/foo:1"))


class TestIsRegistryReachable(unittest.TestCase):
    """Layer 1: offline short-circuit. Probes the primary (GHCR) host, then the
    fallback (Docker Hub) host; offline only when NEITHER answers."""

    def setUp(self):
        self._p1 = patch.object(docker_manager, "REGISTRY", "ghcr.io/svendanilborodun")
        self._p2 = patch.object(docker_manager, "REGISTRY_FALLBACK", "nettername")
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    @patch("gui.app.docker_manager.socket.create_connection")
    def test_true_when_primary_opens(self, mock_conn):
        # MagicMock supports the context-manager protocol used by _host_reachable.
        self.assertTrue(docker_manager.is_registry_reachable())
        # First probe targets the primary (GHCR) host.
        self.assertEqual(mock_conn.call_args_list[0][0][0], ("ghcr.io", 443))

    @patch("gui.app.docker_manager.socket.create_connection")
    def test_falls_back_to_hub_when_primary_down(self, mock_conn):
        def _side(addr, timeout):
            if addr == ("ghcr.io", 443):
                raise OSError("ghcr down")
            return MagicMock()
        mock_conn.side_effect = _side
        self.assertTrue(docker_manager.is_registry_reachable())
        # Probed both the primary and the fallback host.
        probed = [c[0][0] for c in mock_conn.call_args_list]
        self.assertIn(("ghcr.io", 443), probed)
        self.assertIn(("registry-1.docker.io", 443), probed)

    @patch("gui.app.docker_manager.socket.create_connection")
    def test_false_when_both_down(self, mock_conn):
        mock_conn.side_effect = OSError("all down")
        self.assertFalse(docker_manager.is_registry_reachable())

    @patch("gui.app.docker_manager.socket.create_connection")
    def test_false_on_timeout_both(self, mock_conn):
        mock_conn.side_effect = socket.timeout()
        self.assertFalse(docker_manager.is_registry_reachable())


class TestLocalRepoDigest(unittest.TestCase):
    """Helper: parse `docker image inspect` output for the RepoDigest sha."""

    @patch("gui.app.docker_manager.subprocess.run")
    def test_extracts_first_digest(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="nettername/physical-ai-server@sha256:abc123def456|\n",
        )
        digest = docker_manager._get_local_repo_digest("nettername/physical-ai-server:latest")
        self.assertEqual(digest, "sha256:abc123def456")

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_none_for_local_only_image(self, mock_run):
        # An image built locally (never pulled) has no RepoDigest.
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        self.assertIsNone(
            docker_manager._get_local_repo_digest("nettername/x:latest")
        )

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_none_when_image_absent(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="No such image")
        self.assertIsNone(
            docker_manager._get_local_repo_digest("nettername/x:latest")
        )


class TestRemoteManifestDigest(unittest.TestCase):
    """Helper: pick the linux/amd64 entry from a multi-platform manifest list."""

    @patch("gui.app.docker_manager.subprocess.run")
    def test_picks_linux_amd64_from_manifest_list(self, mock_run):
        manifest_list = {
            "manifests": [
                {
                    "digest": "sha256:amd64aaaaaaaaa",
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
                {
                    "digest": "sha256:unknown",
                    "platform": {"architecture": "unknown", "os": "unknown"},
                },
            ],
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(manifest_list)
        )
        digest = docker_manager._get_remote_manifest_digest("nettername/x:latest")
        self.assertEqual(digest, "sha256:amd64aaaaaaaaa")

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_none_when_no_amd64_variant(self, mock_run):
        manifest_list = {
            "manifests": [
                {
                    "digest": "sha256:arm64x",
                    "platform": {"architecture": "arm64", "os": "linux"},
                },
            ],
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(manifest_list)
        )
        self.assertIsNone(docker_manager._get_remote_manifest_digest("any:latest"))

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_none_on_manifest_inspect_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="not found"
        )
        self.assertIsNone(docker_manager._get_remote_manifest_digest("any:latest"))

    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_none_on_invalid_json(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json")
        self.assertIsNone(docker_manager._get_remote_manifest_digest("any:latest"))


class TestDigestCandidates(unittest.TestCase):
    """The 2026-06-05 fix: layer 2 compares the local RepoDigest against a
    SET of registry digests (manifest-list digest + per-platform children),
    because the list-vs-child mismatch made the old single-digest equality
    check never fire (every GUI launch: false "Update verfügbar" + no-op
    pull)."""

    _IMAGETOOLS_SAMPLE = (
        "Name:      docker.io/nettername/physical-ai-server:2.6.0\n"
        "MediaType: application/vnd.oci.image.index.v1+json\n"
        "Digest:    sha256:"
        + "a" * 64
        + "\n\n"
        "Manifests:\n"
        "  Name:        docker.io/nettername/physical-ai-server:2.6.0@sha256:"
        + "b" * 64
        + "\n"
        "  MediaType:   application/vnd.oci.image.manifest.v1+json\n"
        "  Platform:    linux/amd64\n"
    )

    def test_parse_extracts_list_and_child_digests(self):
        from gui.app.docker_manager import _parse_digest_candidates
        candidates = _parse_digest_candidates(self._IMAGETOOLS_SAMPLE)
        self.assertIn("sha256:" + "a" * 64, candidates)  # list digest
        self.assertIn("sha256:" + "b" * 64, candidates)  # amd64 child
        self.assertEqual(len(candidates), 2)

    def test_parse_empty_and_garbage(self):
        from gui.app.docker_manager import _parse_digest_candidates
        self.assertEqual(_parse_digest_candidates(""), set())
        self.assertEqual(_parse_digest_candidates(None), set())
        self.assertEqual(_parse_digest_candidates("sha256:tooshort"), set())

    @patch("gui.app.docker_manager.subprocess.run")
    def test_local_list_digest_matches_candidates(self, mock_run):
        # THE regression: RepoDigest == manifest-list digest must count as
        # current even though the legacy probe would have returned the
        # (different) amd64 child digest.
        mock_run.return_value = MagicMock(
            returncode=0, stdout=self._IMAGETOOLS_SAMPLE
        )
        candidates = docker_manager._get_remote_digest_candidates("any:tag")
        self.assertIn("sha256:" + "a" * 64, candidates)

    @patch(
        "gui.app.docker_manager._get_remote_manifest_digest",
        return_value="sha256:" + "c" * 64,
    )
    @patch("gui.app.docker_manager.subprocess.run")
    def test_falls_back_to_legacy_probe_when_buildx_fails(
        self, mock_run, _legacy
    ):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no buildx")
        candidates = docker_manager._get_remote_digest_candidates("any:tag")
        self.assertEqual(candidates, {"sha256:" + "c" * 64})

    @patch(
        "gui.app.docker_manager._get_remote_manifest_digest",
        return_value=None,
    )
    @patch("gui.app.docker_manager.subprocess.run")
    def test_returns_empty_set_when_everything_fails(self, mock_run, _legacy):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        self.assertEqual(
            docker_manager._get_remote_digest_candidates("any:tag"), set()
        )


class TestLastPullPersistence(unittest.TestCase):
    """Layer 3: persist + load + expose freshness for the GUI banner."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        self._tmp.close()
        os.unlink(self._tmp.name)  # delete so save creates it fresh
        self._patcher = patch.object(docker_manager, "LAST_PULL_FILE", self._tmp.name)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_save_then_load_roundtrip(self):
        digests = {
            ALL_IMAGES[0]: "sha256:aaaaaaaaaaaaaaaa",
            ALL_IMAGES[1]: "sha256:bbbbbbbbbbbbbbbb",
            ALL_IMAGES[2]: None,  # one image without a digest is OK
        }
        docker_manager._save_last_pull_info(digests)
        info = docker_manager._load_last_pull_info()
        self.assertIsNotNone(info)
        self.assertIn("timestamp", info)
        self.assertIsInstance(info["timestamp"], int)
        # None values are filtered out on save
        self.assertEqual(
            set(info["digests"].keys()), set(ALL_IMAGES[:2])
        )

    def test_load_returns_none_when_file_missing(self):
        # No file at the patched path → None
        self.assertIsNone(docker_manager._load_last_pull_info())

    def test_status_when_never_pulled(self):
        status = docker_manager.get_last_pull_status()
        self.assertIsNone(status["age_days"])
        self.assertTrue(status["is_stale"])
        self.assertEqual(status["digests"], {})

    def test_status_fresh_after_save(self):
        docker_manager._save_last_pull_info(
            {ALL_IMAGES[0]: "sha256:freshdigest12345"}
        )
        status = docker_manager.get_last_pull_status()
        self.assertIsNotNone(status["age_days"])
        self.assertLess(status["age_days"], 1.0)
        self.assertFalse(status["is_stale"])

    def test_status_stale_when_older_than_threshold(self):
        # Write a timestamp from > IMAGE_FRESHNESS_WARN_DAYS days ago
        with open(self._tmp.name, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "timestamp": int(time.time())
                    - (IMAGE_FRESHNESS_WARN_DAYS + 1) * 86400,
                    "digests": {},
                },
                f,
            )
        status = docker_manager.get_last_pull_status()
        self.assertGreater(status["age_days"], IMAGE_FRESHNESS_WARN_DAYS)
        self.assertTrue(status["is_stale"])


class TestCheckForUpdatesOrchestration(unittest.TestCase):
    """Top-level: the hardened `check_for_updates` short-circuits the right
    paths in the right order."""

    @patch("gui.app.docker_manager.is_registry_reachable")
    @patch.object(docker_manager, "SKIP_AUTO_PULL", True)
    def test_skip_env_var_short_circuits(self, mock_reach):
        logs: list[str] = []
        result = docker_manager.check_for_updates(log=logs.append)
        self.assertFalse(result)
        mock_reach.assert_not_called()
        self.assertTrue(any("Auto-Pull deaktiviert" in line for line in logs))

    @patch("gui.app.docker_manager._save_last_pull_info")
    @patch("gui.app.docker_manager._pull_image_with_fallback")
    @patch("gui.app.docker_manager.is_registry_reachable", return_value=False)
    def test_offline_skips_all_pulls(self, _reach, mock_pull, mock_save):
        logs: list[str] = []
        result = docker_manager.check_for_updates(log=logs.append)
        self.assertFalse(result)
        mock_pull.assert_not_called()
        mock_save.assert_not_called()
        self.assertTrue(
            any("Registry (GHCR/Docker Hub) nicht erreichbar" in line for line in logs)
        )

    @patch("gui.app.docker_manager._save_last_pull_info")
    @patch("gui.app.docker_manager._pull_image_with_fallback")
    @patch(
        "gui.app.docker_manager._get_remote_digest_candidates",
        return_value={"sha256:samedigestforall"},
    )
    @patch(
        "gui.app.docker_manager._get_local_repo_digest",
        return_value="sha256:samedigestforall",
    )
    @patch("gui.app.docker_manager.is_registry_reachable", return_value=True)
    def test_digest_match_skips_pull(
        self, _reach, _local, _remote, mock_pull, mock_save
    ):
        logs: list[str] = []
        result = docker_manager.check_for_updates(log=logs.append)
        self.assertFalse(result)
        mock_pull.assert_not_called()
        # Save must happen even on a no-op so the freshness timestamp resets
        mock_save.assert_called_once()
        # Every image should log "bereits aktuell"
        bereits_count = sum(1 for l in logs if "bereits aktuell" in l)
        self.assertEqual(bereits_count, len(ALL_IMAGES))

    @patch("gui.app.docker_manager._save_last_pull_info")
    @patch("gui.app.docker_manager._pull_image_with_fallback", return_value=True)
    @patch(
        "gui.app.docker_manager._get_remote_digest_candidates",
        return_value={"sha256:newremoteversion"},
    )
    @patch(
        "gui.app.docker_manager._get_local_repo_digest",
        return_value="sha256:oldlocalversion",
    )
    @patch("gui.app.docker_manager.is_registry_reachable", return_value=True)
    def test_digest_mismatch_triggers_pull(
        self, _reach, _local, _remote, mock_pull, _save
    ):
        # subprocess.run for the `docker images -q` ID-before/after probes
        with patch("gui.app.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="oldid\n")
            docker_manager.check_for_updates(log=lambda *_: None)
        # the fallback-aware pull is called once per image
        self.assertEqual(mock_pull.call_count, len(ALL_IMAGES))


class TestPullWithFallback(unittest.TestCase):
    """Primary→fallback pull: GHCR first, Docker Hub twin + retag on failure."""

    def setUp(self):
        for attr, val in (("REGISTRY", "ghcr.io/svendanilborodun"),
                          ("REGISTRY_FALLBACK", "nettername")):
            p = patch.object(docker_manager, attr, val)
            p.start()
            self.addCleanup(p.stop)

    @patch("gui.app.docker_manager._pull_one_image", return_value=True)
    @patch("gui.app.docker_manager._host_reachable", return_value=True)
    def test_primary_success_no_fallback(self, _reach, mock_pull):
        ok = docker_manager._pull_image_with_fallback(
            "ghcr.io/svendanilborodun/physical-ai-server:latest", 0, 1
        )
        self.assertTrue(ok)
        mock_pull.assert_called_once()
        self.assertEqual(
            mock_pull.call_args[0][0],
            "ghcr.io/svendanilborodun/physical-ai-server:latest",
        )

    @patch("gui.app.docker_manager.subprocess.run")
    @patch("gui.app.docker_manager._pull_one_image")
    @patch("gui.app.docker_manager._host_reachable", return_value=True)
    def test_fallback_pull_and_retag_on_primary_failure(self, _reach, mock_pull, mock_run):
        # GHCR pull fails, the Docker Hub twin succeeds; `docker tag` returns 0.
        mock_pull.side_effect = lambda image, *a, **k: image.startswith("nettername/")
        mock_run.return_value = MagicMock(returncode=0)
        ok = docker_manager._pull_image_with_fallback(
            "ghcr.io/svendanilborodun/physical-ai-server:latest", 0, 1
        )
        self.assertTrue(ok)
        pulled = [c[0][0] for c in mock_pull.call_args_list]
        self.assertIn("ghcr.io/svendanilborodun/physical-ai-server:latest", pulled)
        self.assertIn("nettername/physical-ai-server:latest", pulled)
        # A `docker tag nettername/... ghcr.io/...` was issued, then the
        # redundant fallback tag was removed (de-clutters `docker image ls`).
        run_cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertTrue(any("tag" in cmd for cmd in run_cmds),
                        f"expected a `docker tag` call, got {run_cmds}")
        self.assertTrue(any("rm" in cmd for cmd in run_cmds),
                        f"expected the fallback tag to be removed, got {run_cmds}")

    @patch("gui.app.docker_manager.subprocess.run")
    @patch("gui.app.docker_manager._pull_one_image", return_value=False)
    @patch("gui.app.docker_manager._host_reachable", return_value=True)
    def test_both_fail_returns_false(self, _reach, _pull, _run):
        ok = docker_manager._pull_image_with_fallback(
            "ghcr.io/svendanilborodun/physical-ai-server:latest", 0, 1
        )
        self.assertFalse(ok)


class TestPruneSupersededTags(unittest.TestCase):
    """prune_superseded_tags removes local tags != IMAGE_TAG across both
    registries (the GUI-path equivalent of pull_images.ps1's tag sweep — the
    disk leak that bloated the v2.13.0 incident VHDX)."""

    def setUp(self):
        for attr, val in (("REGISTRY", "ghcr.io/svendanilborodun"),
                          ("REGISTRY_FALLBACK", "nettername"),
                          ("IMAGE_TAG", "2.13.0"),
                          ("IMAGE_NAMES", ["open-manipulator"])):
            p = patch.object(docker_manager, attr, val)
            p.start()
            self.addCleanup(p.stop)

    @patch("gui.app.docker_manager.subprocess.run")
    def test_removes_superseded_keeps_current(self, mock_run):
        def _run(cmd, *a, **k):
            if "images" in cmd:  # the `docker images <repo> --format {{.Tag}}` probe
                return MagicMock(returncode=0, stdout="2.13.0\n2.11.0\n<none>\n")
            return MagicMock(returncode=0, stdout="")  # the rm calls
        mock_run.side_effect = _run
        removed = docker_manager.prune_superseded_tags()
        rm_targets = [c[0][0][-1] for c in mock_run.call_args_list if "rm" in c[0][0]]
        # Superseded :2.11.0 removed on BOTH registries; :2.13.0 and <none> kept.
        self.assertIn("ghcr.io/svendanilborodun/open-manipulator:2.11.0", rm_targets)
        self.assertIn("nettername/open-manipulator:2.11.0", rm_targets)
        self.assertNotIn("ghcr.io/svendanilborodun/open-manipulator:2.13.0", rm_targets)
        self.assertNotIn("ghcr.io/svendanilborodun/open-manipulator:<none>", rm_targets)
        self.assertEqual(removed, 2)


class TestRegistryResolution(unittest.TestCase):
    """constants.py resolves REGISTRY / REGISTRY_FALLBACK / IMAGE_TAG via
    env → docker/versions.env → hardcoded default (GHCR primary)."""

    def test_resolve_setting_env_override(self):
        from gui.app import constants
        with patch.dict(os.environ, {"EDUBOTICS_TEST_REG": "myreg"}):
            self.assertEqual(
                constants._resolve_setting("EDUBOTICS_TEST_REG", "TEST_REG", "default"),
                "myreg",
            )

    def test_resolve_setting_default_when_absent(self):
        from gui.app import constants
        self.assertEqual(
            constants._resolve_setting(
                "EDUBOTICS_DEFINITELY_UNSET_XYZ", "UNSET_KEY_XYZ", "thedefault"
            ),
            "thedefault",
        )

    @unittest.skipIf(os.environ.get("EDUBOTICS_REGISTRY"), "registry overridden in env")
    def test_default_registry_is_ghcr_with_hub_fallback(self):
        from gui.app import constants
        self.assertTrue(constants.REGISTRY.startswith("ghcr.io/"))
        self.assertEqual(constants.REGISTRY_FALLBACK, "nettername")
        self.assertTrue(all(img.startswith("ghcr.io/") for img in constants.ALL_IMAGES))


if __name__ == "__main__":
    unittest.main()
