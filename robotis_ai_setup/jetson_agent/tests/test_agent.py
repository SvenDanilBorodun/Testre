"""Cross-platform unit tests for the Jetson agent.

These run on Linux CI (and macOS dev hosts) without needing a real
Jetson, docker daemon, or Cloud API — every subprocess + network call
is mocked. Integration tests with real hardware are documented in the
plan as a follow-up smoke step.

What we cover:
  - _images_from_compose: parses image: lines from the compose file
  - _is_registry_reachable: True/False from GHCR-then-Hub socket probes
  - _pull_one_image_with_retries: GHCR→Docker Hub fallback + re-tag
  - _get_local_repo_digest: parses `docker image inspect` output
  - _get_remote_manifest_digest: picks linux/arm64 from the manifest list
  - _parse_digest_candidates / _get_remote_digest_candidates: the 970905d
    set-membership pre-check (imagetools primary, legacy probe fallback)
  - _auto_pull_images digest pre-check: membership match skips the pull,
    non-match pulls
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
        # The regex expands ${REGISTRY:-<anything>} to the resolved REGISTRY
        # constant, regardless of what default the compose file carries.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(
                "services:\n"
                "  s:\n"
                "    image: ${REGISTRY:-nettername}/foo:arm64-latest\n"
            )
            path = f.name
        try:
            with patch.object(agent, "COMPOSE_PATH", Path(path)), \
                    patch.object(agent, "REGISTRY", "ghcr.io/svendanilborodun"):
                images = agent._images_from_compose()
            self.assertEqual(images, ["ghcr.io/svendanilborodun/foo:arm64-latest"])
        finally:
            os.unlink(path)

    def test_returns_empty_when_compose_missing(self):
        with patch.object(agent, "COMPOSE_PATH", Path("/nonexistent/compose.yml")):
            self.assertEqual(agent._images_from_compose(), [])


class TestRegistryReachable(unittest.TestCase):
    def test_returns_true_when_connect_succeeds(self):
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=None)
        with patch("agent.socket.create_connection", return_value=mock_sock):
            self.assertTrue(agent._is_registry_reachable())

    def test_returns_false_on_oserror_for_both_hosts(self):
        # OSError on every probe (primary GHCR + fallback Hub) -> offline.
        with patch("agent.socket.create_connection", side_effect=OSError("ENETUNREACH")):
            self.assertFalse(agent._is_registry_reachable())

    def test_returns_false_on_timeout(self):
        with patch("agent.socket.create_connection", side_effect=socket.timeout):
            self.assertFalse(agent._is_registry_reachable())

    def test_falls_back_to_hub_when_primary_down(self):
        def _side(addr, timeout):
            if addr == ("ghcr.io", 443):
                raise OSError("ghcr down")
            return MagicMock()
        with patch.object(agent, "REGISTRY", "ghcr.io/svendanilborodun"), \
                patch.object(agent, "REGISTRY_FALLBACK", "nettername"), \
                patch("agent.socket.create_connection", side_effect=_side):
            self.assertTrue(agent._is_registry_reachable())


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


# Full-length digests: _DIGEST_RE only matches sha256: + 64 hex chars, so the
# short fakes used by the legacy tests above would silently not match here.
_LIST_DIGEST = "sha256:" + "a" * 64
_ARM64_DIGEST = "sha256:" + "b" * 64
_AMD64_DIGEST = "sha256:" + "c" * 64

_IMAGETOOLS_OUTPUT = f"""\
Name:      nettername/foo:arm64-latest
MediaType: application/vnd.oci.image.index.v1+json
Digest:    {_LIST_DIGEST}

Manifests:
  Name:        nettername/foo:arm64-latest@{_ARM64_DIGEST}
  MediaType:   application/vnd.oci.image.manifest.v1+json
  Platform:    linux/arm64

  Name:        nettername/foo:arm64-latest@{_AMD64_DIGEST}
  MediaType:   application/vnd.oci.image.manifest.v1+json
  Platform:    linux/amd64
"""


class TestDigestCandidates(unittest.TestCase):
    """The 970905d set-membership pre-check, ported from the GUI: the local
    RepoDigest of a buildx-pushed image is the manifest-LIST digest while the
    legacy probe returns the arm64 CHILD digest — plain equality never
    matched, so the pre-check was dead and every agent start did a no-op
    pull. Candidates must contain BOTH shapes."""

    def _fake_run(self, stdout, returncode=0):
        return MagicMock(stdout=stdout, returncode=returncode)

    def test_parse_digest_candidates_extracts_all(self):
        candidates = agent._parse_digest_candidates(_IMAGETOOLS_OUTPUT)
        self.assertEqual(candidates, {_LIST_DIGEST, _ARM64_DIGEST, _AMD64_DIGEST})

    def test_parse_digest_candidates_empty_on_none_or_garbage(self):
        self.assertEqual(agent._parse_digest_candidates(""), set())
        self.assertEqual(agent._parse_digest_candidates(None), set())
        self.assertEqual(agent._parse_digest_candidates("sha256:tooshort"), set())

    def test_remote_candidates_from_imagetools(self):
        with patch(
            "agent.subprocess.run",
            return_value=self._fake_run(_IMAGETOOLS_OUTPUT),
        ) as run_mock:
            candidates = agent._get_remote_digest_candidates("nettername/foo:arm64-latest")
        self.assertEqual(candidates, {_LIST_DIGEST, _ARM64_DIGEST, _AMD64_DIGEST})
        self.assertEqual(
            run_mock.call_args[0][0][:4],
            ["docker", "buildx", "imagetools", "inspect"],
        )

    def test_remote_candidates_fallback_to_legacy(self):
        # imagetools fails (rc=1) -> falls back to `docker manifest inspect`,
        # which still picks the arm64 CHILD digest (wrapped in a set).
        manifest = json.dumps({
            "manifests": [
                {"platform": {"architecture": "amd64", "os": "linux"},
                 "digest": _AMD64_DIGEST},
                {"platform": {"architecture": "arm64", "os": "linux"},
                 "digest": _ARM64_DIGEST},
            ]
        })
        with patch(
            "agent.subprocess.run",
            side_effect=[self._fake_run("", returncode=1), self._fake_run(manifest)],
        ):
            candidates = agent._get_remote_digest_candidates("nettername/foo:arm64-latest")
        self.assertEqual(candidates, {_ARM64_DIGEST})

    def test_membership_short_circuits_pull(self):
        # Local RepoDigest is the LIST digest; candidates contain it ->
        # no pull, digest carried into the last-pull payload.
        saved: dict = {}
        with patch.object(agent, "_images_from_compose", return_value=["nettername/foo:arm64-latest"]), \
                patch.object(agent, "_is_registry_reachable", return_value=True), \
                patch.object(agent, "_get_local_repo_digest", return_value=_LIST_DIGEST), \
                patch.object(agent, "_get_remote_digest_candidates",
                             return_value={_LIST_DIGEST, _ARM64_DIGEST}), \
                patch.object(agent, "_pull_one_image_with_retries") as pull_mock, \
                patch.object(agent, "_save_last_pull_info", side_effect=saved.update):
            agent._auto_pull_images()
        pull_mock.assert_not_called()
        self.assertEqual(saved, {"nettername/foo:arm64-latest": _LIST_DIGEST})

    def test_non_match_triggers_pull(self):
        with patch.object(agent, "_images_from_compose", return_value=["nettername/foo:arm64-latest"]), \
                patch.object(agent, "_is_registry_reachable", return_value=True), \
                patch.object(agent, "_get_local_repo_digest", return_value=_LIST_DIGEST), \
                patch.object(agent, "_get_remote_digest_candidates",
                             return_value={_ARM64_DIGEST, _AMD64_DIGEST}), \
                patch.object(agent, "_pull_one_image_with_retries",
                             return_value=_LIST_DIGEST) as pull_mock, \
                patch.object(agent, "_save_last_pull_info"):
            agent._auto_pull_images()
        pull_mock.assert_called_once_with("nettername/foo:arm64-latest")


class TestRegistryFallbackHelpers(unittest.TestCase):
    def test_registry_host(self):
        self.assertEqual(agent._registry_host("ghcr.io/svendanilborodun"), "ghcr.io")
        self.assertEqual(agent._registry_host("nettername"), "registry-1.docker.io")

    def test_fallback_ref(self):
        with patch.object(agent, "REGISTRY", "ghcr.io/svendanilborodun"), \
                patch.object(agent, "REGISTRY_FALLBACK", "nettername"):
            self.assertEqual(
                agent._fallback_ref("ghcr.io/svendanilborodun/foo-jetson:latest"),
                "nettername/foo-jetson:latest",
            )
            self.assertIsNone(agent._fallback_ref("nettername/foo:1"))


class TestPullFallback(unittest.TestCase):
    """_pull_one_image_with_retries falls back to the Docker Hub twin when every
    primary (GHCR) attempt fails, re-tagging it to the primary name."""

    def test_fallback_to_hub_on_primary_failure(self):
        calls = []
        digest = "sha256:" + "d" * 64

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            if cmd[:2] == ["docker", "pull"]:
                ref = cmd[2]
                # GHCR fails, the Docker Hub twin succeeds.
                return MagicMock(returncode=0 if ref.startswith("nettername/") else 1, stderr="x")
            if cmd[:2] == ["docker", "tag"]:
                return MagicMock(returncode=0, stderr="")
            # _get_local_repo_digest inspect
            return MagicMock(returncode=0, stdout=f"nettername/foo@{digest}|\n")

        with patch.object(agent, "REGISTRY", "ghcr.io/svendanilborodun"), \
                patch.object(agent, "REGISTRY_FALLBACK", "nettername"), \
                patch.object(agent._shutdown, "wait", return_value=False), \
                patch("agent.subprocess.run", side_effect=fake_run):
            result = agent._pull_one_image_with_retries(
                "ghcr.io/svendanilborodun/foo:latest"
            )

        self.assertEqual(result, digest)
        pulls = [c[2] for c in calls if c[:2] == ["docker", "pull"]]
        self.assertIn("nettername/foo:latest", pulls)
        tags = [c for c in calls if c[:2] == ["docker", "tag"]]
        self.assertTrue(
            any(t[2] == "nettername/foo:latest"
                and t[3] == "ghcr.io/svendanilborodun/foo:latest" for t in tags)
        )


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


class _StateMachineTestBase(unittest.TestCase):
    """Shared setUp/tearDown that snapshots + restores the agent's module
    globals so state-machine tests can't leak into one another (or into
    the pull/digest tests above)."""

    def setUp(self):
        self._owner_backup = agent._current_owner_user_id
        self._state_backup = agent._state
        self._attempts_backup = dict(agent._failed_claim_attempts)
        agent._current_owner_user_id = None
        agent._state = "paired_idle"
        agent._failed_claim_attempts.clear()
        agent._shutdown.clear()

    def tearDown(self):
        agent._shutdown.clear()
        agent._current_owner_user_id = self._owner_backup
        agent._state = self._state_backup
        agent._failed_claim_attempts.clear()
        agent._failed_claim_attempts.update(self._attempts_backup)


class TestClaimRetryRelease(_StateMachineTestBase):
    """H4 / M5: a persistent claim failure must not strand the server-side
    lock. After CLAIM_RETRY_LIMIT failures the gave-up tick re-frees the
    lock and resets the local view instead of silently returning."""

    def test_giveup_tick_releases_lock_and_resets_owner(self):
        owner = "student-uuid"
        with patch.object(agent, "_bring_up_stack", side_effect=RuntimeError("boom")) as bring_up, \
                patch.object(agent, "_move_to_safe_home"), \
                patch.object(agent, "_start_proxy"), \
                patch.object(agent, "_stop_proxy"), \
                patch.object(agent, "_bring_down_stack"), \
                patch.object(agent, "_release_lock_via_cloud_api") as release:
            # Exhaust the retry budget. The main loop assigns the owner to
            # the local view before each call (NULL -> UUID); the exception
            # path then frees the local view back to None.
            for _ in range(agent.CLAIM_RETRY_LIMIT):
                agent._current_owner_user_id = owner
                agent._transition_to_claimed(owner)
                self.assertIsNone(agent._current_owner_user_id)
            self.assertEqual(bring_up.call_count, agent.CLAIM_RETRY_LIMIT)
            self.assertEqual(
                agent._failed_claim_attempts.get(owner), agent.CLAIM_RETRY_LIMIT
            )

            # The next claim attempt for the SAME owner hits the early
            # return. It must release the server-side lock, reset the local
            # owner view, and NOT re-run the docker bring-up storm.
            release.reset_mock()
            agent._current_owner_user_id = owner
            agent._transition_to_claimed(owner)

            release.assert_called_once_with(owner)
            self.assertIsNone(agent._current_owner_user_id)
            self.assertEqual(agent._state, "paired_idle")
            self.assertEqual(bring_up.call_count, agent.CLAIM_RETRY_LIMIT)  # unchanged

    def test_successful_claim_clears_counter(self):
        owner = "student-uuid"
        agent._failed_claim_attempts[owner] = 1
        with patch.object(agent, "_bring_up_stack"), \
                patch.object(agent, "_move_to_safe_home"), \
                patch.object(agent, "_start_proxy"), \
                patch.object(agent, "_stop_proxy"), \
                patch.object(agent, "_bring_down_stack"), \
                patch.object(agent, "_release_lock_via_cloud_api"):
            agent._current_owner_user_id = owner
            agent._transition_to_claimed(owner)
        self.assertEqual(agent._state, "claimed")
        self.assertNotIn(owner, agent._failed_claim_attempts)


class TestMainLoopRetryReset(_StateMachineTestBase):
    """M5: the per-owner retry counter must be cleared whenever the server
    authoritatively reports no owner — even if the local view is already
    None (the gave-up path leaves it None). Otherwise a stale counter
    short-circuits the same owner's next claim into the H4 deadlock."""

    def _run_one_iteration(self, heartbeats):
        seq = iter(heartbeats)

        def fake_heartbeat(_env):
            return next(seq)

        def fake_wait(_timeout):
            # Break the loop after each scripted heartbeat is consumed.
            agent._shutdown.set()
            return True

        with patch.object(agent, "_agent_heartbeat", side_effect=fake_heartbeat), \
                patch.object(agent._shutdown, "wait", side_effect=fake_wait), \
                patch.object(agent, "_transition_to_released") as released, \
                patch.object(agent, "_transition_to_claimed") as claimed:
            agent._main_loop({"env": "x"})
        return released, claimed

    def test_server_free_clears_stale_counter_when_local_already_none(self):
        agent._failed_claim_attempts["ghost"] = agent.CLAIM_RETRY_LIMIT
        agent._current_owner_user_id = None  # already re-freed locally

        released, _ = self._run_one_iteration([(True, None)])

        self.assertEqual(agent._failed_claim_attempts, {})
        released.assert_not_called()  # nothing to wipe — local view was None

    def test_server_release_wipes_and_clears_counter(self):
        agent._failed_claim_attempts["old"] = 2
        agent._current_owner_user_id = "old"  # we still think we hold it

        released, _ = self._run_one_iteration([(True, None)])

        self.assertEqual(agent._failed_claim_attempts, {})
        self.assertIsNone(agent._current_owner_user_id)
        released.assert_called_once()

    def test_unreachable_api_does_not_clear_counter_or_wipe(self):
        # ok=False (network blip) must NOT be treated as "owner is None".
        agent._failed_claim_attempts["holder"] = 1
        agent._current_owner_user_id = "holder"

        released, claimed = self._run_one_iteration([(False, None)])

        self.assertEqual(agent._failed_claim_attempts, {"holder": 1})
        self.assertEqual(agent._current_owner_user_id, "holder")
        released.assert_not_called()
        claimed.assert_not_called()


class TestShutdownReleasesLock(_StateMachineTestBase):
    """H2: main()'s finally must release a held server-side lock, not just
    tear down the containers (the log line claimed it did, but the call was
    missing)."""

    def test_main_finally_releases_held_lock(self):
        agent._current_owner_user_id = "held-owner"
        env = {
            "EDUBOTICS_JETSON_ID": "jetson-1",
            "EDUBOTICS_AGENT_TOKEN": "tok",
            "EDUBOTICS_CLOUD_API_URL": "https://cloud.example.com",
        }
        with patch.object(agent, "_load_env", return_value=env), \
                patch.object(agent, "_start_loopback_server"), \
                patch.object(agent, "_auto_pull_images"), \
                patch.object(agent, "_main_loop"), \
                patch.object(agent, "_release_lock_via_cloud_api") as release, \
                patch.object(agent, "_stop_proxy") as stop_proxy, \
                patch.object(agent, "_bring_down_stack") as bring_down, \
                patch("agent.signal.signal"):
            agent.main()
        release.assert_called_once_with("held-owner")
        stop_proxy.assert_called_once()
        bring_down.assert_called_once()

    def test_main_finally_noop_when_no_lock_held(self):
        agent._current_owner_user_id = None
        env = {
            "EDUBOTICS_JETSON_ID": "jetson-1",
            "EDUBOTICS_AGENT_TOKEN": "tok",
            "EDUBOTICS_CLOUD_API_URL": "https://cloud.example.com",
        }
        with patch.object(agent, "_load_env", return_value=env), \
                patch.object(agent, "_start_loopback_server"), \
                patch.object(agent, "_auto_pull_images"), \
                patch.object(agent, "_main_loop"), \
                patch.object(agent, "_release_lock_via_cloud_api") as release, \
                patch.object(agent, "_stop_proxy"), \
                patch.object(agent, "_bring_down_stack"), \
                patch("agent.signal.signal"):
            agent.main()
        release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
