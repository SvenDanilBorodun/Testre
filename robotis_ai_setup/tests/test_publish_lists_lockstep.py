"""The delivery pipeline enumerates the fleet's images in FIVE more places
than the fleet itself does (deps-free text test).

`gui/app/constants.py::IMAGE_NAMES` (Windows) and
`pi_agent/constants.py::IMAGE_NAMES` (Orange Pi) are what a student's machine
pulls. Between the repo and that machine sit the lists that PUBLISH the images,
each hand-written and none derived from the other:

  - docker-publish.yml's retag lists (AMD64_REPOS / ARM64_REPOS / OPI_REPOS),
    its flat dual-push integrity list (REPOS=, whose own comment says "keep
    BOTH in sync"), its third OPI_REPOS restatement, and the smoke-test matrix's
    repo columns;
  - release.yml's anonymous-pullable probe loop, which gates the Pi
    self-update advertisement on every -opi image of the release;
  - build-images.sh's per-platform *_OUT_REPO variables, from which its two
    push loops derive the repo names;
  - image_source_parity.sh's `kind` case, which is the only thing that proves
    an image's shipped tree matches the repo.

A name missing from ONE of them is invisible there: the retag job never
creates the GHCR tag students pull; the release advertises a Pi self-update
whose compose references an image that never published; build-images.sh
builds the image and never pushes it; the parity guard never looks. The
`code-runner` / `code-runner-opi` pair (2026-09-21) is the first image added
since these lists existed, so this is the test that would have been missing.

Text regexes on purpose: the gui suite carries no pyyaml, and the shape being
asserted is the literal line a reviewer reads.
"""

import os
import re
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_ROOT = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_SETUP_ROOT)

_PUBLISH_YML = os.path.join(_REPO_ROOT, ".github", "workflows", "docker-publish.yml")
_RELEASE_YML = os.path.join(_REPO_ROOT, ".github", "workflows", "release.yml")
_BUILD_SH = os.path.join(_SETUP_ROOT, "docker", "build-images.sh")
_PARITY_SH = os.path.join(_REPO_ROOT, ".github", "scripts", "image_source_parity.sh")

_RUNNER = "code-runner"
_RUNNER_OPI = "code-runner-opi"


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _shell_list(text, var):
    """Every `VAR="a b c"` assignment in `text`, each as a list of names."""
    return [m.split() for m in re.findall(r'^\s*' + var + r'="([^"]*)"\s*$', text, re.M)]


def _smoke_matrix(text):
    """The smoke-test matrix's include entries as {platform: {key: value}}.
    Comment lines are skipped; a value of `""` reads as the empty string."""
    start = text.index("\n  smoke-test:\n")
    section = text[start:text.index("\n    steps:\n", start)]
    entries, current = {}, None
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^(-\s+)?(\w+):\s*(.*)$", stripped)
        if not m:
            continue
        key, value = m.group(2), m.group(3).strip()
        if value in ('""', "''"):
            value = ""
        if m.group(1) and key == "platform":
            current = value
            entries[current] = {}
        elif current is not None:
            entries[current][key] = value
    return entries


def _case_bodies(text):
    """build-images.sh's platform `case` arms as {platform: body}."""
    return dict(re.findall(r"\n    (amd64|arm64|opi)\)\n(.*?)\n        ;;", text, re.S))


class TestDockerPublishListsCarryTheRunner(unittest.TestCase):
    def setUp(self):
        self.text = _read(_PUBLISH_YML)

    def _gui_names(self):
        from gui.app import constants
        return list(constants.IMAGE_NAMES)

    def _pi_names(self):
        # pi_agent has its own package root; read the list off the source so
        # this suite does not import the agent's constants module (its
        # sys.path is the pi_agent test suite's, not ours).
        src = _read(os.path.join(_SETUP_ROOT, "pi_agent", "constants.py"))
        body = re.search(r"^IMAGE_NAMES = \[\n(.*?)\n\]", src, re.M | re.S).group(1)
        return re.findall(r'"([a-z0-9-]+)"', body)

    def test_the_fleet_lists_this_test_compares_against_carry_the_runner(self):
        # The premise, restated so a failure below reads correctly: the two
        # IMAGE_NAMES already say four names (WP6); this test holds the
        # delivery lists to them.
        self.assertEqual(self._gui_names()[-1], _RUNNER)
        self.assertEqual(self._pi_names()[-1], _RUNNER_OPI)

    def test_retag_lists_are_the_fleets_image_names(self):
        (amd64,) = _shell_list(self.text, "AMD64_REPOS")
        (arm64,) = _shell_list(self.text, "ARM64_REPOS")
        opi_lists = _shell_list(self.text, "OPI_REPOS")
        self.assertEqual(amd64, self._gui_names(),
                         "AMD64_REPOS must be gui IMAGE_NAMES, in order")
        self.assertEqual(len(opi_lists), 2, "OPI_REPOS is stated twice (retag + integrity)")
        for opi in opi_lists:
            self.assertEqual(opi, self._pi_names(),
                             "every OPI_REPOS restatement must be pi IMAGE_NAMES, in order")
        # A10: no Jetson runner. The Roboter-Studio tab is jetsonIncompatible.
        self.assertFalse(any(_RUNNER in name for name in arm64), arm64)

    def test_the_flat_integrity_list_is_the_three_lists_joined(self):
        (amd64,) = _shell_list(self.text, "AMD64_REPOS")
        (arm64,) = _shell_list(self.text, "ARM64_REPOS")
        opi = _shell_list(self.text, "OPI_REPOS")[0]
        (flat,) = _shell_list(self.text, "REPOS")
        self.assertEqual(flat, amd64 + arm64 + opi,
                         "REPOS= (the 'keep BOTH in sync' list) drifted from the retag lists")

    def test_the_smoke_matrix_carries_a_runner_repo_per_platform(self):
        matrix = _smoke_matrix(self.text)
        self.assertEqual(set(matrix), {"amd64", "arm64", "opi"})
        self.assertEqual(matrix["amd64"].get("runner_repo"), _RUNNER)
        self.assertEqual(matrix["arm64"].get("runner_repo"), "",
                         "the Jetson leg must name NO runner (A10), explicitly")
        self.assertEqual(matrix["opi"].get("runner_repo"), _RUNNER_OPI)

    def test_the_anonymous_pull_probe_and_the_parity_step_see_the_runner(self):
        # The probe loop runs BEFORE the GHCR login; a runner package left
        # Private would 401 every student and silently fall back to Hub.
        self.assertRegex(self.text, r'RUNNER_REPO: \$\{\{ matrix\.runner_repo \}\}')
        self.assertRegex(self.text,
                         r'for repo in "\$SERVER_REPO" "\$ARM_REPO" "\$MANAGER_REPO" "\$RUNNER_REPO"; do')
        self.assertIn("image_source_parity.sh code-runner", self.text)

    def test_the_runner_size_ceiling_reads_uncompressed_layer_bytes(self):
        # `.Size` is COMPRESSED under the containerd snapshotter (the amd64
        # server gate's UNITS block); a ceiling read from it would be inert.
        step = re.search(r"- name: [^\n]*code-runner[^\n]*size ceiling.*?(?=\n      - name: )",
                         self.text, re.S | re.I)
        self.assertIsNotNone(step, "no code-runner size-ceiling step in smoke-test")
        body = step.group(0)
        self.assertIn("docker image history --human=false", body)
        self.assertIn("CEIL=$((900 * 1000 * 1000))", body)
        self.assertNotRegex(body, r"docker image inspect[^\n]*\{\{\.Size\}\}")

    def test_the_workflow_names_the_runner_at_least_eight_times(self):
        # The acceptance grep, kept literal: header comment, three list sites,
        # the matrix column, the probe env, the size step, the parity step.
        self.assertGreaterEqual(self.text.count(_RUNNER), 8)


class TestReleaseProbeLoopNamesEveryOpiImage(unittest.TestCase):
    def test_the_pullable_probe_loop_is_the_pi_image_names(self):
        text = _read(_RELEASE_YML)
        src = _read(os.path.join(_SETUP_ROOT, "pi_agent", "constants.py"))
        body = re.search(r"^IMAGE_NAMES = \[\n(.*?)\n\]", src, re.M | re.S).group(1)
        pi_names = re.findall(r'"([a-z0-9-]+)"', body)
        loops = re.findall(r"^\s*for repo in ((?:[a-z0-9-]+-opi ?)+); do\s*$", text, re.M)
        self.assertEqual(len(loops), 1, "exactly one -opi probe loop expected in release.yml")
        self.assertEqual(set(loops[0].split()), set(pi_names),
                         "a Pi would advertise a self-update whose compose names an "
                         "image this loop never proved pullable")


class TestBuildImagesBuildsAndPushesTheRunner(unittest.TestCase):
    def setUp(self):
        self.text = _read(_BUILD_SH)
        self.cases = _case_bodies(self.text)

    def _runner_out_repo(self, platform):
        m = re.search(r'^\s*RUNNER_OUT_REPO=("[^"]*")\s*$', self.cases[platform], re.M)
        self.assertIsNotNone(m, f"{platform}: no RUNNER_OUT_REPO in its case arm")
        return m.group(1)

    def test_runner_out_repo_is_set_per_platform_and_empty_on_the_jetson(self):
        self.assertEqual(set(self.cases), {"amd64", "arm64", "opi"})
        self.assertEqual(self._runner_out_repo("amd64"), '"${REGISTRY}/code-runner"')
        self.assertEqual(self._runner_out_repo("arm64"), '""')
        self.assertEqual(self._runner_out_repo("opi"), '"${REGISTRY}/code-runner-opi"')

    def test_the_runner_is_built_only_when_its_out_repo_is_non_empty(self):
        self.assertRegex(self.text, r'if \[ -n "\$RUNNER_OUT_REPO" \]; then')
        build = re.search(r'if \[ -n "\$RUNNER_OUT_REPO" \]; then(.*?)\nfi', self.text, re.S)
        self.assertIsNotNone(build)
        body = build.group(1)
        self.assertIn('-f "${SCRIPT_DIR}/code_runner/Dockerfile"', body)
        self.assertIn('-t "${RUNNER_OUT_REPO}:${IMAGE_TAG_SUFFIX}"', body)
        self.assertIn("--no-cache --pull", body)
        self.assertIn("--load", body)
        self.assertNotIn("--push", body, "the runner rides the push loops, never buildx --push")

    def test_both_push_loops_derive_the_runner_name_from_its_out_repo(self):
        loops = re.findall(r'^\s*for img in (.*?); do\s*$', self.text, re.M)
        self.assertEqual(len(loops), 2, "the amd64 and the opi push loop")
        for loop in loops:
            self.assertIn('"${RUNNER_OUT_REPO##*/}"', loop)


class TestParityScriptHasTheThirdKind(unittest.TestCase):
    def test_the_code_runner_kind_diffs_the_shipped_tree_against_the_repo(self):
        text = _read(_PARITY_SH)
        case = re.search(r"\n  code-runner\)\n(.*?)\n    ;;", text, re.S)
        self.assertIsNotNone(case, "image_source_parity.sh has no code-runner kind")
        body = case.group(1)
        self.assertIn("/opt/edubotics/runner", body)
        self.assertIn("robotis_ai_setup/docker/code_runner/runner", body)
        self.assertIn("java-classes", body, "the build-time java-classes/ dir must be stripped")
        self.assertIn("diff -rq", body)
        self.assertIn("physical-ai-server | open-manipulator | code-runner", text)


if __name__ == "__main__":
    unittest.main()
