"""Deps-free unit tests for pi_agent.constants — the version + image-pin
resolution the whole Pi fleet's update safety rests on.

`constants` computes APP_VERSION / IMAGE_TAG at IMPORT time from files on disk,
so these tests exercise the pure helpers directly (and re-import the module in a
sandbox where an end-to-end resolve is what's under test) rather than trying to
mutate already-frozen module globals.

Mirrors the sibling pi_agent tests' import convention (no tests/__init__.py;
robotis_ai_setup on sys.path, `from pi_agent import ...`).
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

from pi_agent import constants  # noqa: E402


class TestDefaultImageTag(unittest.TestCase):
    """The Pi must never silently resolve `latest`.

    `docker-publish.yml` pushes `:latest` on EVERY push to main, so a Pi that
    resolves it tracks main HEAD instead of its release — and no version
    handshake anywhere would catch it. release.yml bakes
    pi_agent/docker/versions.env to prevent that; this is the defense behind it
    for the day that file goes missing.
    """

    def test_an_authoritative_version_becomes_the_pin(self):
        with patch.object(constants, "_VERSION_IS_AUTHORITATIVE", True), \
             patch.object(constants, "APP_VERSION", "2.14.0"):
            self.assertEqual(constants._default_image_tag(), "2.14.0")

    def test_no_version_source_at_all_keeps_latest(self):
        # The baked-in literal is only "whatever this source tree was cut from".
        # Pinning to it would be confidently WRONG — worse than `latest`'s
        # honest "I don't know" — so with no readable version source, keep the
        # documented old behaviour.
        with patch.object(constants, "_VERSION_IS_AUTHORITATIVE", False), \
             patch.object(constants, "APP_VERSION", "2.13.0"):
            self.assertEqual(constants._default_image_tag(), "latest")

    def test_an_empty_app_version_keeps_latest(self):
        # An empty tag would resolve `…-opi:` and fail every pull.
        with patch.object(constants, "_VERSION_IS_AUTHORITATIVE", True), \
             patch.object(constants, "APP_VERSION", ""):
            self.assertEqual(constants._default_image_tag(), "latest")


class TestVersionFileResolution(unittest.TestCase):
    """`_read_version_file` also decides whether the version is AUTHORITATIVE —
    the flag `_default_image_tag` keys on — so its fall-through matters."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _read_in(self, module_dir, env=None):
        """Run _read_version_file as if constants.py lived in `module_dir`."""
        fake = os.path.join(module_dir, "constants.py")
        with patch.object(constants, "__file__", fake), \
             patch.dict(os.environ, env or {}, clear=False):
            if env is None and "EDUBOTICS_VERSION" in os.environ:
                del os.environ["EDUBOTICS_VERSION"]
            with patch.object(constants, "_VERSION_IS_AUTHORITATIVE", False):
                value = constants._read_version_file()
                return value, constants._VERSION_IS_AUTHORITATIVE

    def test_a_packaged_version_file_is_authoritative(self):
        pkg = os.path.join(self.dir, "pi_agent")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "VERSION"), "w", encoding="utf-8") as f:
            f.write("2.14.0\n")
        value, authoritative = self._read_in(pkg)
        self.assertEqual(value, "2.14.0")
        self.assertTrue(authoritative)

    def test_a_blank_version_file_is_not_authoritative(self):
        """A truncated write must not make APP_VERSION empty.

        Harmless while the version was only REPORTED; now `_default_image_tag`
        derives an image pin from it, and an empty pin fails every pull. The
        blank file is skipped, so resolution falls through to the literal — and
        stays non-authoritative, so IMAGE_TAG stays `latest`.
        """
        pkg = os.path.join(self.dir, "pi_agent")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "VERSION"), "w", encoding="utf-8") as f:
            f.write("   \n")
        value, authoritative = self._read_in(pkg)
        self.assertFalse(authoritative)
        self.assertTrue(value)  # the literal, never ""

    def test_the_env_override_is_authoritative(self):
        pkg = os.path.join(self.dir, "pi_agent")
        os.makedirs(pkg)
        value, authoritative = self._read_in(pkg, env={"EDUBOTICS_VERSION": "9.9.9"})
        self.assertEqual(value, "9.9.9")
        self.assertTrue(authoritative)

    def test_the_fallback_literal_matches_the_repo_version_file(self):
        """release.yml::version-preflight greps this literal and hard-fails a tag
        whose value drifted. A literal that reads LOW re-offers the same forced
        update forever (it is what APP_VERSION degrades to if the packaged
        VERSION ever goes missing), so pin it here too — the grep only runs on a
        tag push, while this runs on every PR."""
        repo_version = (SETUP_DIR.parent / "VERSION").read_text(encoding="utf-8").strip()
        source = Path(constants.__file__).read_text(encoding="utf-8")
        self.assertIn(f'return "{repo_version}"', source)


class TestImageTagEndToEnd(unittest.TestCase):
    """The resolution ORDER (env → versions.env → default) is what actually
    ships; re-import constants in a sandbox to exercise it whole."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_versions_env_still_wins_over_the_version_file(self):
        # The CI-baked pin is the primary mechanism; the APP_VERSION fallback
        # must never override it.
        with patch.object(constants, "_read_versions_env", return_value="2.99.0"), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EDUBOTICS_IMAGE_TAG", None)
            self.assertEqual(
                constants._resolve_setting("EDUBOTICS_IMAGE_TAG", "IMAGE_TAG",
                                           constants._default_image_tag()),
                "2.99.0",
            )

    def test_the_env_override_still_wins_over_everything(self):
        # One-variable rollback must keep working.
        with patch.dict(os.environ, {"EDUBOTICS_IMAGE_TAG": "latest"}), \
             patch.object(constants, "_read_versions_env", return_value="2.99.0"):
            self.assertEqual(
                constants._resolve_setting("EDUBOTICS_IMAGE_TAG", "IMAGE_TAG",
                                           constants._default_image_tag()),
                "latest",
            )

    def test_without_a_versions_env_the_pin_is_the_product_version(self):
        """The whole point: a Pi whose baked versions.env went missing degrades
        to its OWN release, not main HEAD."""
        with patch.object(constants, "_read_versions_env", return_value=""), \
             patch.object(constants, "_VERSION_IS_AUTHORITATIVE", True), \
             patch.object(constants, "APP_VERSION", "2.14.0"), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EDUBOTICS_IMAGE_TAG", None)
            self.assertEqual(
                constants._resolve_setting("EDUBOTICS_IMAGE_TAG", "IMAGE_TAG",
                                           constants._default_image_tag()),
                "2.14.0",
            )

    def test_image_refs_carry_the_resolved_tag_across_both_registries(self):
        # image_ref derives BOTH registries' refs from one short name (a
        # split('/') would munge the two-segment ghcr.io/<owner> prefix).
        ref = constants.image_ref("physical-ai-server-opi", tag="2.14.0")
        self.assertEqual(ref, f"{constants.REGISTRY}/physical-ai-server-opi:2.14.0")
        fb = constants.image_ref("physical-ai-server-opi",
                                 registry=constants.REGISTRY_FALLBACK, tag="2.14.0")
        self.assertEqual(fb, "nettername/physical-ai-server-opi:2.14.0")


class TestSystemFilesVersionFile(unittest.TestCase):
    def test_default_lives_outside_pi_agent(self):
        """Load-bearing: the stamp records the version the COMPOSE + SYSTEMD
        files came from, and self-update rsyncs pi_agent/ only. A stamp inside
        pi_agent/ would be advanced by every self-update and could never report
        drift — the one thing it exists to do."""
        self.assertEqual(constants.SYSTEM_FILES_VERSION_FILE,
                         "/opt/edubotics/.system-files-version")
        self.assertNotIn("pi_agent", constants.SYSTEM_FILES_VERSION_FILE)

    def test_is_env_overridable_for_tests(self):
        importlib.reload(constants)  # pick up a clean baseline
        with patch.dict(os.environ,
                        {"EDUBOTICS_SYSTEM_FILES_VERSION_FILE": "/tmp/stamp"}):
            reloaded = importlib.reload(constants)
            self.assertEqual(reloaded.SYSTEM_FILES_VERSION_FILE, "/tmp/stamp")
        importlib.reload(constants)  # restore for the rest of the suite

    def test_setup_sh_writes_the_path_the_agent_reads(self):
        """The writer (setup.sh) and the reader (constants) are in different
        languages with no shared symbol — a rename on either side is a silent
        no-op that disables the drift warning forever. Pin the seam."""
        setup_sh = (Path(constants.__file__).parent / "setup.sh").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn(".system-files-version", setup_sh)


if __name__ == "__main__":
    unittest.main()
