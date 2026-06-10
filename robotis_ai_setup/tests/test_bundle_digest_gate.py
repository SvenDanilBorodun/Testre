"""Unit tests for the offline-image-bundle digest gate (P1).

A `docker load`-ed bundle image has an EMPTY RepoDigests (moby#22011); a later
`docker pull`/`docker compose pull` of the same tag re-downloads every layer
(moby#46664). Without a fix, the first online launch AND the first
"Umgebung starten" would each re-download the full ~GB image set the installer
already loaded — defeating the offline bundle.

The fix: an install-seeded `bundled_digests.json` sidecar (per-image
manifest-LIST digest) + a sidecar-aware `_image_is_current()` helper applied to
BOTH `check_for_updates` (launch) and `_compose_pull` (env-start, service-
scoped). These tests pin that behaviour and guard the regressions the
adversarial review surfaced (the launch no-op alone was insufficient; cloud-only
must be single-service-scoped; the fallback must not suppress real updates).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gui.app import docker_manager  # noqa: E402
from gui.app.constants import (  # noqa: E402
    ALL_IMAGES,
    IMAGE_OPEN_MANIPULATOR,
    IMAGE_PHYSICAL_AI_MANAGER,
    IMAGE_PHYSICAL_AI_SERVER,
)

_LIST = "sha256:" + "a" * 64       # manifest-list digest (what the sidecar stores)
_CHILD = "sha256:" + "b" * 64      # amd64 child digest
_OTHER = "sha256:" + "c" * 64      # an unrelated/newer digest


class TestBundledDigestLookup(unittest.TestCase):
    """`_bundled_digest_for`: exact + registry-tolerant lookup, absent file."""

    def test_absent_sidecar_returns_none(self):
        with patch.object(docker_manager, "_load_bundled_digests", return_value={}):
            self.assertIsNone(docker_manager._bundled_digest_for(ALL_IMAGES[0]))

    def test_exact_ref_match(self):
        sidecar = {ALL_IMAGES[0]: _LIST}
        with patch.object(docker_manager, "_load_bundled_digests", return_value=sidecar):
            self.assertEqual(docker_manager._bundled_digest_for(ALL_IMAGES[0]), _LIST)

    def test_registry_override_tolerant_match(self):
        # Sidecar keyed under nettername/ (what CI pushed); the GUI resolves the
        # image under an EDUBOTICS_REGISTRY override → match on bare repo:tag.
        sidecar = {"nettername/physical-ai-server:2.8.0": _LIST}
        with patch.object(docker_manager, "_load_bundled_digests", return_value=sidecar):
            self.assertEqual(
                docker_manager._bundled_digest_for("myreg/physical-ai-server:2.8.0"),
                _LIST,
            )

    def test_ignores_non_sha256_values(self):
        sidecar = {ALL_IMAGES[0]: "not-a-digest"}
        with patch.object(docker_manager, "_load_bundled_digests", return_value=sidecar):
            self.assertIsNone(docker_manager._bundled_digest_for(ALL_IMAGES[0]))


class TestResolveLocalDigest(unittest.TestCase):
    """`_resolve_local_digest`: real RepoDigest wins; sidecar only fills the gap,
    and ONLY when the image is actually present (not merely in a stale sidecar)."""

    def test_real_repo_digest_takes_precedence(self):
        with patch.object(docker_manager, "_get_local_repo_digest", return_value=_CHILD), \
             patch.object(docker_manager, "_bundled_digest_for", return_value=_LIST):
            digest, is_bundled = docker_manager._resolve_local_digest(ALL_IMAGES[0])
            self.assertEqual(digest, _CHILD)
            self.assertFalse(is_bundled)

    def test_falls_back_to_sidecar_when_present_and_repodigest_empty(self):
        # The docker-load'd-bundle case: image IS present, RepoDigest empty.
        with patch.object(docker_manager, "_get_local_repo_digest", return_value=None), \
             patch.object(docker_manager, "_image_present_locally", return_value=True), \
             patch.object(docker_manager, "_bundled_digest_for", return_value=_LIST):
            digest, is_bundled = docker_manager._resolve_local_digest(ALL_IMAGES[0])
            self.assertEqual(digest, _LIST)
            self.assertTrue(is_bundled)

    def test_absent_image_with_stale_sidecar_returns_none(self):
        # The hardened edge: the image is GONE (distro re-created) but the
        # install-dir sidecar still lists its digest. Trusting it would skip a
        # required pull → `manifest unknown` at compose up. Must NOT resolve.
        with patch.object(docker_manager, "_get_local_repo_digest", return_value=None), \
             patch.object(docker_manager, "_image_present_locally", return_value=False), \
             patch.object(docker_manager, "_bundled_digest_for", return_value=_LIST):
            digest, is_bundled = docker_manager._resolve_local_digest(ALL_IMAGES[0])
            self.assertIsNone(digest)
            self.assertFalse(is_bundled)

    def test_present_but_no_sidecar_entry_returns_none(self):
        with patch.object(docker_manager, "_get_local_repo_digest", return_value=None), \
             patch.object(docker_manager, "_image_present_locally", return_value=True), \
             patch.object(docker_manager, "_bundled_digest_for", return_value=None):
            digest, is_bundled = docker_manager._resolve_local_digest(ALL_IMAGES[0])
            self.assertIsNone(digest)
            self.assertFalse(is_bundled)


class TestImagePresentLocally(unittest.TestCase):
    """`_image_present_locally`: rc==0 from `docker image inspect` ⇒ present;
    distinguishes a docker-load'd bundle image (empty RepoDigests) from absent."""

    def test_present_when_inspect_succeeds(self):
        with patch("gui.app.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="sha256:id\n")
            self.assertTrue(docker_manager._image_present_locally(ALL_IMAGES[0]))

    def test_absent_when_inspect_fails(self):
        with patch("gui.app.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="No such image")
            self.assertFalse(docker_manager._image_present_locally(ALL_IMAGES[0]))


class TestImageIsCurrent(unittest.TestCase):
    """`_image_is_current`: the shared gate used by both pull sites."""

    def test_real_digest_in_remote_is_current(self):
        with patch.object(docker_manager, "_resolve_local_digest", return_value=(_CHILD, False)):
            self.assertTrue(docker_manager._image_is_current(ALL_IMAGES[0], {_CHILD, _LIST}))

    def test_bundled_list_digest_in_remote_is_current(self):
        # The Walkthrough-C core: loaded image (sidecar list digest) ∈ remote.
        with patch.object(docker_manager, "_resolve_local_digest", return_value=(_LIST, True)):
            self.assertTrue(docker_manager._image_is_current(ALL_IMAGES[0], {_LIST, _CHILD}))

    def test_bundled_on_flaky_network_is_current(self):
        # Remote probe failed (empty) but the image is a known bundled one →
        # do NOT re-download the loaded bytes.
        with patch.object(docker_manager, "_resolve_local_digest", return_value=(_LIST, True)):
            self.assertTrue(docker_manager._image_is_current(ALL_IMAGES[0], set()))

    def test_real_digest_on_flaky_network_is_not_current(self):
        # Preserve today's behaviour: a genuinely-pulled image with a failed
        # probe is treated as unknown → pull.
        with patch.object(docker_manager, "_resolve_local_digest", return_value=(_CHILD, False)):
            self.assertFalse(docker_manager._image_is_current(ALL_IMAGES[0], set()))

    def test_stale_image_is_not_current(self):
        with patch.object(docker_manager, "_resolve_local_digest", return_value=(_CHILD, False)):
            self.assertFalse(docker_manager._image_is_current(ALL_IMAGES[0], {_OTHER}))

    def test_absent_image_is_not_current(self):
        with patch.object(docker_manager, "_resolve_local_digest", return_value=(None, False)):
            self.assertFalse(docker_manager._image_is_current(ALL_IMAGES[0], {_LIST}))


class TestCheckForUpdatesBundleNoOp(unittest.TestCase):
    """Walkthrough C (launch): a freshly-bundled box online for the first time
    must log 'bereits aktuell' for all 3 images and pull NOTHING."""

    @patch("gui.app.docker_manager._save_last_pull_info")
    @patch("gui.app.docker_manager._pull_one_image")
    @patch("gui.app.docker_manager._get_remote_digest_candidates", return_value={_LIST, _CHILD})
    @patch("gui.app.docker_manager._image_present_locally", return_value=True)  # loaded, present
    @patch("gui.app.docker_manager._get_local_repo_digest", return_value=None)  # loaded → empty
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=True)
    def test_loaded_images_are_no_op(self, _reach, _local, _present, _remote, mock_pull, _save):
        sidecar = {img: _LIST for img in ALL_IMAGES}
        logs: list[str] = []
        with patch.object(docker_manager, "_load_bundled_digests", return_value=sidecar):
            result = docker_manager.check_for_updates(log=logs.append)
        self.assertFalse(result)
        mock_pull.assert_not_called()
        self.assertEqual(sum(1 for l in logs if "bereits aktuell" in l), len(ALL_IMAGES))

    @patch("gui.app.docker_manager._save_last_pull_info")
    @patch("gui.app.docker_manager._pull_one_image", return_value=True)
    @patch("gui.app.docker_manager._get_remote_digest_candidates", return_value={_LIST, _CHILD})
    @patch("gui.app.docker_manager._image_present_locally", return_value=False)  # GONE
    @patch("gui.app.docker_manager._get_local_repo_digest", return_value=None)
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=True)
    def test_absent_images_with_stale_sidecar_are_pulled(self, _reach, _local, _present, _remote, mock_pull, _save):
        # Even though the sidecar still lists each image's digest AND that digest
        # is in the remote candidate set, the images are GONE locally → the gate
        # must PULL all three rather than falsely report 'bereits aktuell'.
        sidecar = {img: _LIST for img in ALL_IMAGES}
        with patch.object(docker_manager, "_load_bundled_digests", return_value=sidecar):
            docker_manager.check_for_updates(log=lambda *_: None)
        self.assertEqual(mock_pull.call_count, len(ALL_IMAGES))

    @patch("gui.app.docker_manager._save_last_pull_info")
    @patch("gui.app.docker_manager._pull_one_image", return_value=True)
    @patch("gui.app.docker_manager._get_remote_digest_candidates", return_value={_OTHER})
    @patch("gui.app.docker_manager._get_local_repo_digest", return_value=None)
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=True)
    def test_new_version_not_suppressed_by_stale_sidecar(self, _reach, _local, _remote, mock_pull, _save):
        # Sidecar still keyed to the OLD bundled list digest, but the remote now
        # advertises a different digest → the gate must NOT suppress the pull.
        sidecar = {img: _LIST for img in ALL_IMAGES}
        with patch("gui.app.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="oldid\n")
            with patch.object(docker_manager, "_load_bundled_digests", return_value=sidecar):
                docker_manager.check_for_updates(log=lambda *_: None)
        self.assertEqual(mock_pull.call_count, len(ALL_IMAGES))


class TestComposePullSkip(unittest.TestCase):
    """Walkthrough C (env-start): `docker compose pull` must be skipped when the
    relevant image(s) are current — else a loaded bundle re-downloads on first
    'Umgebung starten'."""

    @patch("gui.app.docker_manager.subprocess.run")
    @patch("gui.app.docker_manager._image_is_current", return_value=True)
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=True)
    def test_skips_compose_pull_when_all_current(self, _reach, _cur, mock_run):
        logs: list[str] = []
        self.assertTrue(docker_manager._compose_pull(log=logs.append))
        mock_run.assert_not_called()
        self.assertTrue(any("bereits aktuell" in l for l in logs))

    @patch("gui.app.docker_manager.subprocess.run")
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=True)
    def test_cloud_only_is_single_service_scoped(self, _reach, mock_run):
        # manager current, others stale: full-stack would pull, but cloud-only
        # (service=physical_ai_manager) must skip on the manager image alone.
        def current(image, remote_candidates=None):
            return image == IMAGE_PHYSICAL_AI_MANAGER
        with patch.object(docker_manager, "_image_is_current", side_effect=current):
            self.assertTrue(
                docker_manager._compose_pull(service="physical_ai_manager", log=lambda *_: None)
            )
        mock_run.assert_not_called()

    @patch("gui.app.docker_manager.subprocess.run")
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=True)
    def test_full_stack_pulls_when_one_stale(self, _reach, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        def current(image, remote_candidates=None):
            return image == IMAGE_PHYSICAL_AI_MANAGER  # others stale
        with patch.object(docker_manager, "_image_is_current", side_effect=current):
            docker_manager._compose_pull(log=lambda *_: None)
        mock_run.assert_called_once()  # the real `docker compose pull` ran

    @patch("gui.app.docker_manager._image_is_current")
    @patch("gui.app.docker_manager.subprocess.run")
    @patch("gui.app.docker_manager.is_dockerhub_reachable", return_value=False)
    def test_offline_skips_without_manifest_probes(self, _reach, mock_run, mock_cur):
        # Offline env-start: skip the pull immediately, WITHOUT probing each
        # image's remote digest (~10s/image of manifest timeouts on a bundle).
        logs: list[str] = []
        self.assertTrue(docker_manager._compose_pull(log=logs.append))
        mock_run.assert_not_called()
        mock_cur.assert_not_called()  # no per-image manifest probing offline
        self.assertTrue(any("nicht erreichbar" in l for l in logs))

    @patch("gui.app.docker_manager.subprocess.run")
    @patch.object(docker_manager, "SKIP_AUTO_PULL", True)
    def test_skip_auto_pull_env_short_circuits(self, mock_run):
        self.assertTrue(docker_manager._compose_pull(log=lambda *_: None))
        mock_run.assert_not_called()


class TestSidecarTagEqualityContract(unittest.TestCase):
    """The bundle's sidecar keys MUST equal the GUI's runtime-resolved
    ALL_IMAGES (same registry + IMAGE_TAG from the same release), or the
    fallback silently misses and the bundle re-pulls. This pins the invariant
    the P3 CI bundle job must satisfy."""

    def test_sidecar_keyed_by_all_images_resolves_each(self):
        sidecar = {img: _LIST for img in ALL_IMAGES}
        self.assertEqual(set(sidecar.keys()), set(ALL_IMAGES))
        with patch.object(docker_manager, "_load_bundled_digests", return_value=sidecar):
            for img in ALL_IMAGES:
                self.assertEqual(docker_manager._bundled_digest_for(img), _LIST)

    def test_service_image_map_covers_all_images(self):
        self.assertEqual(
            set(docker_manager._SERVICE_IMAGE.values()),
            {IMAGE_OPEN_MANIPULATOR, IMAGE_PHYSICAL_AI_SERVER, IMAGE_PHYSICAL_AI_MANAGER},
        )


if __name__ == "__main__":
    unittest.main()
