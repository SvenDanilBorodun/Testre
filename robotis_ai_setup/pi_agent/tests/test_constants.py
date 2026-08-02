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

import os
import shutil
import subprocess
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


class TestVersionsEnvWalk(unittest.TestCase):
    """`_read_versions_env` is the seam the whole fleet's image pin rides on, and
    NOTHING exercised it: every image-tag test above patches it wholesale.

    Both writers — `setup.sh` (provision time) and `release.yml::pi-agent-tarball`
    (self-update tarball) — target exactly `pi_agent/docker/versions.env`, which
    is the FIRST candidate the walk probes. CI gates the tarball's side. A
    reorder of the walk, or a path typo in setup.sh, would silently drop the pin
    back to `latest`/`APP_VERSION` — the failure mode that previously killed the
    robot tier fleet-wide (a Pi quietly tracking main HEAD).
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pkg = os.path.join(self.dir, "pi_agent")
        os.makedirs(self.pkg)

    def _write(self, directory, body):
        d = os.path.join(directory, "docker")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "versions.env"), "w", encoding="utf-8") as f:
            f.write(body)

    def _read(self, key):
        """Run _read_versions_env as if constants.py lived in the fake package."""
        with patch.object(constants, "__file__", os.path.join(self.pkg, "constants.py")):
            return constants._read_versions_env(key)

    def test_the_first_candidate_is_pi_agent_docker_versions_env(self):
        self._write(self.pkg, "IMAGE_TAG=2.14.0\n")
        self.assertEqual(self._read("IMAGE_TAG"), "2.14.0")

    def test_the_nearest_file_wins_for_a_key_it_carries(self):
        # A pin one level up must never override the packaged one.
        self._write(self.dir, "IMAGE_TAG=0.0.0-outer\n")
        self._write(self.pkg, "IMAGE_TAG=2.14.0\n")
        self.assertEqual(self._read("IMAGE_TAG"), "2.14.0")

    def test_a_key_missing_from_the_nearest_file_keeps_walking(self):
        """IMPROVEMENT 9. The walk used to stop at the first versions.env FOUND,
        even when it lacked the requested key. setup.sh writes a
        pi_agent/docker/versions.env carrying ONLY IMAGE_TAG, so a future
        REGISTRY / REGISTRY_FALLBACK pin shipped one level up would have been
        permanently invisible on every provisioned Pi — falsifying
        `_resolve_setting`'s "a future registry change ships in the CI-baked
        versions.env with no agent rebuild"."""
        self._write(self.dir, "REGISTRY=ghcr.io/someone-else\n")
        self._write(self.pkg, "IMAGE_TAG=2.14.0\n")
        self.assertEqual(self._read("IMAGE_TAG"), "2.14.0")
        self.assertEqual(self._read("REGISTRY"), "ghcr.io/someone-else")

    def test_a_blank_value_is_treated_as_unset(self):
        # `IMAGE_TAG=` would resolve the unpullable ref `…-opi:`.
        self._write(self.pkg, "IMAGE_TAG=\n")
        self.assertEqual(self._read("IMAGE_TAG"), "")

    def test_absent_everywhere_returns_empty(self):
        self.assertEqual(self._read("IMAGE_TAG"), "")

    def test_setup_sh_writes_the_exact_path_the_walk_probes(self):
        """The writer is shell and the reader is Python with no shared symbol.
        `.system-files-version` has its own seam test; this pins the pin file."""
        setup_sh = (Path(constants.__file__).parent / "setup.sh").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("pi_agent/docker", setup_sh)
        self.assertIn("versions.env", setup_sh)
        self.assertIn("IMAGE_TAG=", setup_sh)


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
        """Resolved in a SUBPROCESS, not via importlib.reload.

        `agent`, `docker_manager` and `config_generator` all do
        `from .constants import IMAGE_TAG, REGISTRY, ENV_FILE, …`, so reloading
        `constants` in-process rebinds the module's globals while every importer
        keeps its ORIGINAL objects — a split-brain that only stayed invisible
        because the env was restored and the values recomputed identically. One
        future import-time value that does not round-trip (a timestamp, an
        os.getpid()) and this test starts corrupting whichever module happens to
        run after it, in a way that reads as a bug somewhere else entirely. A
        subprocess exercises the same import-time resolution with no shared
        state to poison.
        """
        env = dict(os.environ, EDUBOTICS_SYSTEM_FILES_VERSION_FILE="/tmp/stamp")
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]);"
             " from pi_agent import constants;"
             " print(constants.SYSTEM_FILES_VERSION_FILE)",
             str(SETUP_DIR)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "/tmp/stamp")

    def test_setup_sh_writes_the_path_the_agent_reads(self):
        """The writer (setup.sh) and the reader (constants) are in different
        languages with no shared symbol — a rename on either side is a silent
        no-op that disables the drift warning forever. Pin the seam."""
        setup_sh = (Path(constants.__file__).parent / "setup.sh").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn(".system-files-version", setup_sh)


class TestArmUsbIdsLockstep(unittest.TestCase):
    """`ARM_USB_IDS` is duplicated on Windows and the Pi because pi_agent ships
    as a standalone tarball with no import path to gui/app. A lockstep test is
    the coupling (the deliberate WP-7 decision); without it the two tables drift
    and an arm scannable on one platform is invisible on the other."""

    def test_table_equals_the_windows_one(self):
        from gui.app import constants as win

        self.assertEqual(constants.ARM_USB_IDS, win.ARM_USB_IDS)

    def test_the_edu6_row_is_the_ch343p(self):
        self.assertEqual(constants.ARM_USB_IDS["edu6"], (("1A86", "55D3"),))

    def test_the_omx_row_is_any_pid_under_the_robotis_vid(self):
        self.assertEqual(constants.ARM_USB_IDS["omx"],
                         ((constants.ROBOTIS_VID, None),))

    def test_the_udev_rule_grants_every_table_entry_a_permission_floor(self):
        """The rule file and the table are two independent transcriptions of the
        same hardware fact. A VID/PID in one and not the other is exactly the
        gap that made the CH343P unopenable outside a privileged container."""
        rules = (Path(constants.__file__).parent / "udev"
                 / constants.UDEV_RULE_NAMES[0]).read_text(encoding="utf-8")
        lines = [ln for ln in rules.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        for family, ids in constants.ARM_USB_IDS.items():
            for vid, pid in ids:
                if pid is None:
                    # Any-PID families are covered per known board instead, by
                    # test_the_robotis_boards_keep_their_udev_lines. KNOWN GAP:
                    # that pins the two literal OpenRB PIDs, so adding a third
                    # OpenRB PID to ARM_USB_IDS["omx"] would NOT be caught here
                    # — the VID reaching the rules file is only ever asserted
                    # through a literal-PID row.
                    continue
                self.assertTrue(
                    any(f'idVendor}}=="{vid.lower()}"' in ln
                        and f'idProduct}}=="{pid.lower()}"' in ln
                        for ln in lines),
                    f"{family}: no udev line for {vid}:{pid}")

    def test_the_robotis_boards_keep_their_udev_lines(self):
        rules = (Path(constants.__file__).parent / "udev"
                 / constants.UDEV_RULE_NAMES[0]).read_text(encoding="utf-8")
        for pid in ("0103", "2202"):
            self.assertIn(f'idProduct}}=="{pid}"', rules)


class TestArmFamilyForRobotType(unittest.TestCase):
    """How a managed EDUBOTICS_ROBOT_TYPE picks the arm family the scan looks
    for. This used to be a standalone `_ROBOT_TYPE_ARM_FAMILY` mapping (the WP-2
    bridge); it now reads straight off `ROBOT_PROFILES`, so there is exactly ONE
    id→family statement on the Pi. The behaviour asserted here is unchanged —
    that is the point of keeping these cases after the absorb."""

    def test_each_profile_maps_to_its_family(self):
        self.assertEqual(constants.arm_family_for_robot_type("omx_full"), "omx")
        self.assertEqual(constants.arm_family_for_robot_type("omx_follower"), "omx")
        self.assertEqual(constants.arm_family_for_robot_type("edu6_studio"), "edu6")

    def test_unknown_absent_and_blank_fall_back_to_omx(self):
        """Matches robot_profiles.resolve: an unrecognised .env value keeps
        today's OMX behaviour instead of making every arm unscannable."""
        for value in (None, "", "   ", "nonsense", "EDU6_STUDIO"):
            self.assertEqual(constants.arm_family_for_robot_type(value),
                             constants.DEFAULT_ARM_FAMILY, repr(value))
        self.assertEqual(constants.DEFAULT_ARM_FAMILY, "omx")

    def test_surrounding_whitespace_is_tolerated(self):
        """`.env` values reach us via read_env_var; a stray space must not
        silently demote an edu6 rig to an OMX scan."""
        self.assertEqual(constants.arm_family_for_robot_type(" edu6_studio "), "edu6")

    def test_ids_and_families_match_the_windows_registry(self):
        """The absorb must not have changed a single verdict: the family this
        accessor returns still equals the Windows descriptor's, id for id.
        (The full three-way registry contract — Windows, Pi, server — lives in
        tests/test_robot_profile_lockstep.py.)"""
        from gui.app import constants as win

        self.assertEqual(set(constants.ROBOT_PROFILES), set(win.ROBOT_PROFILES))
        for rid, row in win.ROBOT_PROFILES.items():
            self.assertEqual(constants.arm_family_for_robot_type(rid),
                             row["arm_family"], rid)
        self.assertEqual(constants.DEFAULT_ARM_FAMILY,
                         win.ROBOT_PROFILES[win.DEFAULT_ROBOT_PROFILE]["arm_family"])

    def test_there_is_only_one_id_to_family_statement_left(self):
        """The WP-2 bridge mapping is GONE, not shadowed. Two sources of truth
        for the same fact is how the platforms drift apart — the whole reason
        the bridge carried a "WP-3 replaces this" note."""
        self.assertFalse(hasattr(constants, "_ROBOT_TYPE_ARM_FAMILY"))
        # DEFAULT_ARM_FAMILY is DERIVED from the registry, not restated.
        self.assertEqual(
            constants.DEFAULT_ARM_FAMILY,
            constants.ROBOT_PROFILES[constants.DEFAULT_ROBOT_PROFILE]["arm_family"])


if __name__ == "__main__":
    unittest.main()
