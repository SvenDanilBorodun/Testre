"""Cross-platform unit tests for the 2.2.4 auto-pull-on-GUI-start hardening.

Covers the three layers added on 2026-05-15 in `docker_manager.check_for_updates`:
  1. Offline short-circuit via `is_dockerhub_reachable`.
  2. Manifest-digest pre-check (skip pull when local == remote).
  3. Last-pull persistence + freshness banner.

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


class TestIsDockerhubReachable(unittest.TestCase):
    """Layer 1: offline short-circuit so the GUI doesn't burn ~12 min of
    retry storm on a disconnected classroom network."""

    @patch("gui.app.docker_manager.socket.create_connection")
    def test_returns_true_when_socket_opens(self, mock_conn):
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        self.assertTrue(docker_manager.is_dockerhub_reachable())
        mock_conn.assert_called_once_with(
            ("registry-1.docker.io", 443),
            timeout=docker_manager.NETWORK_PROBE_TIMEOUT,
        )

    @patch("gui.app.docker_manager.socket.create_connection")
    def test_returns_false_on_connection_refused(self, mock_conn):
        mock_conn.side_effect = OSError("connection refused")
        self.assertFalse(docker_manager.is_dockerhub_reachable())

    @patch("gui.app.docker_manager.socket.create_connection")
    def test_returns_false_on_dns_failure(self, mock_conn):
        mock_conn.side_effect = socket.gaierror("dns failed")
        self.assertFalse(docker_manager.is_dockerhub_reachable())

    @patch("gui.app.docker_manager.socket.create_connection")
    def test_returns_false_on_timeout(self, mock_conn):
        mock_conn.side_effect = socket.timeout()
        self.assertFalse(docker_manager.is_dockerhub_reachable())


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

    @patch("gui.app.docker_manager.is_dockerhub_reachable")
    @patch.object(docker_manager, "SKIP_AUTO_PULL", True)
    def test_skip_env_var_short_circuits(self, mock_reach):
        logs: list[str] = []
        result = docker_manager.check_for_updates(log=logs.append)
        self.assertFalse(result)
        mock_reach.assert_not_called()
        self.assertTrue(any("Auto-Pull deaktiviert" in line for line in logs))

    @patch("gui.app.docker_manager._save_last_pull_info")
    @patch("gui.app.docker_manager._pull_one_image")
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=False)
    def test_offline_skips_all_pulls(self, _reach, mock_pull, mock_save):
        logs: list[str] = []
        result = docker_manager.check_for_updates(log=logs.append)
        self.assertFalse(result)
        mock_pull.assert_not_called()
        mock_save.assert_not_called()
        self.assertTrue(
            any("Docker Hub nicht erreichbar" in line for line in logs)
        )

    @patch("gui.app.docker_manager._save_last_pull_info")
    @patch("gui.app.docker_manager._pull_one_image")
    @patch(
        "gui.app.docker_manager._get_remote_manifest_digest",
        return_value="sha256:samedigestforall",
    )
    @patch(
        "gui.app.docker_manager._get_local_repo_digest",
        return_value="sha256:samedigestforall",
    )
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=True)
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
    @patch("gui.app.docker_manager._pull_one_image", return_value=True)
    @patch(
        "gui.app.docker_manager._get_remote_manifest_digest",
        return_value="sha256:newremoteversion",
    )
    @patch(
        "gui.app.docker_manager._get_local_repo_digest",
        return_value="sha256:oldlocalversion",
    )
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=True)
    def test_digest_mismatch_triggers_pull(
        self, _reach, _local, _remote, mock_pull, _save
    ):
        # subprocess.run for the `docker images -q` ID-before/after probes
        with patch("gui.app.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="oldid\n")
            docker_manager.check_for_updates(log=lambda *_: None)
        # _pull_one_image called once per image
        self.assertEqual(mock_pull.call_count, len(ALL_IMAGES))


if __name__ == "__main__":
    unittest.main()
