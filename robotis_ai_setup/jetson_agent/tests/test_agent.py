"""Cross-platform unit tests for the Jetson agent.

These run on Linux CI (and macOS dev hosts) without needing a real
Jetson, docker daemon, or Cloud API — every subprocess + network call
is mocked. Integration tests with real hardware are documented in the
plan as a follow-up smoke step.

What we cover:
  - _images_from_compose: parses image: lines from the compose file
  - _is_dockerhub_reachable: returns True/False based on socket success
  - _get_local_repo_digest: parses `docker image inspect` output
  - _get_remote_manifest_digest: picks linux/arm64 from the manifest list
  - _save_last_pull_info: round-trips via the JSON file
  - _detect_lan_ip: returns a string (no actual network needed)
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Skip the websockets / python-jose import path by short-circuiting the
# proxy import. agent.py does not import the proxy at module load (it
# only execs it as a subprocess), so this isn't strictly needed — but
# the test file imports BOTH modules in some test cases.
if "websockets" not in sys.modules:
    sys.modules["websockets"] = MagicMock()
if "jose" not in sys.modules:
    jose_stub = MagicMock()
    jose_stub.jwt = MagicMock()
    jose_stub.jwk = MagicMock()
    jose_stub.exceptions = MagicMock()
    sys.modules["jose"] = jose_stub
    sys.modules["jose.exceptions"] = jose_stub.exceptions

# Add the agent directory to sys.path so we can import the module.
AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

# Stub out env loading so agent module-load doesn't crash without
# /etc/edubotics/jetson.env.
ENV_PATH_BACKUP = os.environ.get("EDUBOTICS_JETSON_ENV")
_tmp_env = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".env")
_tmp_env.write(
    "EDUBOTICS_JETSON_ID=test-jetson-uuid\n"
    "EDUBOTICS_AGENT_TOKEN=test-agent-token\n"
    "EDUBOTICS_CLOUD_API_URL=https://test.example.com\n"
)
_tmp_env.close()
os.environ["EDUBOTICS_JETSON_ENV"] = _tmp_env.name


import agent  # noqa: E402


def tearDownModule():  # noqa: N802 (unittest naming)
    try:
        os.unlink(_tmp_env.name)
    except OSError:
        pass
    if ENV_PATH_BACKUP is not None:
        os.environ["EDUBOTICS_JETSON_ENV"] = ENV_PATH_BACKUP


class TestImagesFromCompose(unittest.TestCase):
    def test_parses_image_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(
                "services:\n"
                "  open_manipulator:\n"
                "    image: nettername/open-manipulator:arm64-latest\n"
                "  physical_ai_server:\n"
                '    image: "nettername/physical-ai-server:arm64-latest"\n'
            )
            path = f.name
        try:
            with patch.object(agent, "COMPOSE_PATH", Path(path)):
                images = agent._images_from_compose()
            self.assertIn("nettername/open-manipulator:arm64-latest", images)
            self.assertIn("nettername/physical-ai-server:arm64-latest", images)
        finally:
            os.unlink(path)

    def test_expands_registry_default(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(
                "services:\n"
                "  s:\n"
                "    image: ${REGISTRY:-nettername}/foo:arm64-latest\n"
            )
            path = f.name
        try:
            with patch.object(agent, "COMPOSE_PATH", Path(path)):
                images = agent._images_from_compose()
            self.assertEqual(images, ["nettername/foo:arm64-latest"])
        finally:
            os.unlink(path)

    def test_returns_empty_when_compose_missing(self):
        with patch.object(agent, "COMPOSE_PATH", Path("/nonexistent/compose.yml")):
            self.assertEqual(agent._images_from_compose(), [])


class TestDockerhubReachable(unittest.TestCase):
    def test_returns_true_when_connect_succeeds(self):
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=None)
        with patch("agent.socket.create_connection", return_value=mock_sock):
            self.assertTrue(agent._is_dockerhub_reachable())

    def test_returns_false_on_oserror(self):
        with patch("agent.socket.create_connection", side_effect=OSError("ENETUNREACH")):
            self.assertFalse(agent._is_dockerhub_reachable())

    def test_returns_false_on_timeout(self):
        with patch("agent.socket.create_connection", side_effect=socket.timeout):
            self.assertFalse(agent._is_dockerhub_reachable())


class TestDigests(unittest.TestCase):
    def _fake_run(self, stdout, returncode=0):
        return MagicMock(stdout=stdout, returncode=returncode)

    def test_local_repo_digest_parses_repodigests_format(self):
        # `docker image inspect --format "{{range .RepoDigests}}{{.}}|{{end}}"`
        # produces "image@sha256:...|other@sha256:...|" — pick the FIRST
        # entry's digest.
        out = "nettername/foo@sha256:aaaa1111|other/bar@sha256:bbbb2222|\n"
        with patch("agent.subprocess.run", return_value=self._fake_run(out)):
            digest = agent._get_local_repo_digest("nettername/foo:arm64-latest")
        self.assertEqual(digest, "sha256:aaaa1111")

    def test_local_repo_digest_none_on_failure(self):
        with patch("agent.subprocess.run", return_value=self._fake_run("", returncode=1)):
            self.assertIsNone(agent._get_local_repo_digest("foo"))

    def test_remote_manifest_digest_picks_arm64(self):
        manifest = json.dumps({
            "manifests": [
                {"platform": {"architecture": "amd64", "os": "linux"},
                 "digest": "sha256:amd64111"},
                {"platform": {"architecture": "arm64", "os": "linux"},
                 "digest": "sha256:arm64222"},
            ]
        })
        with patch("agent.subprocess.run", return_value=self._fake_run(manifest)):
            digest = agent._get_remote_manifest_digest("nettername/foo:arm64-latest")
        # On the Jetson agent (arm64 host), the helper picks arm64 NOT
        # amd64 — this is the key difference from the GUI's same helper.
        self.assertEqual(digest, "sha256:arm64222")

    def test_remote_manifest_digest_handles_single_platform(self):
        manifest = json.dumps({"digest": "sha256:singletonabcd"})
        with patch("agent.subprocess.run", return_value=self._fake_run(manifest)):
            digest = agent._get_remote_manifest_digest("foo")
        self.assertEqual(digest, "sha256:singletonabcd")

    def test_remote_manifest_digest_none_on_invalid_json(self):
        with patch("agent.subprocess.run", return_value=self._fake_run("not json")):
            self.assertIsNone(agent._get_remote_manifest_digest("foo"))


class TestLastPullInfo(unittest.TestCase):
    def test_save_last_pull_info_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subdir" / ".last_pull.json"
            with patch.object(agent, "LAST_PULL_FILE", path):
                agent._save_last_pull_info({
                    "nettername/foo": "sha256:abcd",
                    "nettername/bar": None,  # skipped
                })
            self.assertTrue(path.is_file())
            data = json.loads(path.read_text())
            # None digests are skipped — last-pull is for tracking what
            # actually has a digest, not what was attempted.
            self.assertEqual(data["digests"], {"nettername/foo": "sha256:abcd"})
            self.assertIn("timestamp", data)


class TestDetectLanIp(unittest.TestCase):
    def test_returns_string(self):
        # Real socket call — should return SOMETHING (LAN IP or empty).
        # On a network-less CI host the UDP socket trick raises OSError
        # and we return "". On a normal host it returns the LAN IP.
        result = agent._detect_lan_ip()
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
