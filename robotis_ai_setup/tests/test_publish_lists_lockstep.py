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


def _gui_image_names():
    from gui.app import constants
    return list(constants.IMAGE_NAMES)


def _pi_image_names():
    # pi_agent has its own package root; read the list off the source so this
    # suite does not import the agent's constants module (its sys.path is the
    # pi_agent test suite's, not ours).
    src = _read(os.path.join(_SETUP_ROOT, "pi_agent", "constants.py"))
    body = re.search(r"^IMAGE_NAMES = \[\n(.*?)\n\]", src, re.M | re.S).group(1)
    return re.findall(r'"([a-z0-9-]+)"', body)


def _step_body(text, name_fragment):
    """The `run:` script of the first step whose `- name:` contains the fragment."""
    m = re.search(
        r"\n      - name: [^\n]*" + re.escape(name_fragment) + r"[^\n]*\n(.*?)(?=\n      - (?:name|uses): )",
        text,
        re.S,
    )
    return m.group(1) if m else None


class TestDockerPublishListsCarryTheRunner(unittest.TestCase):
    def setUp(self):
        self.text = _read(_PUBLISH_YML)

    _gui_names = staticmethod(_gui_image_names)
    _pi_names = staticmethod(_pi_image_names)

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


class TestTheFirstPublishCarveOutIsNarrow(unittest.TestCase):
    """A GHCR package that has NEVER been pushed is Private by default, and the
    anonymous-pull probe cannot tell that apart from a package that REGRESSED to
    Private — both answer DENIED. Measured 2026-09-21, anonymously:
    physical-ai-server / open-manipulator / physical-ai-manager /
    physical-ai-server-opi each answer token 200 + manifest 200, while
    code-runner and code-runner-opi answer DENIED at the token endpoint.

    So the FIRST docker-publish run that creates the runner package reaches this
    probe and gets DENIED — and the amd64 leg is NOT `continue-on-error`, so it
    fails W4. On a tag push that is after W1's migration and both Railway
    deploys have landed, and it skips W5 (installer) and W6.

    The carve-out is an explicit allowlist, deliberately shaped so it cannot
    grow into a hole: only the two never-published repos are on it, DENIED stays
    fatal for every repo that HAS published, and a missing TAG stays fatal for
    everyone (that is a publish failure, not a visibility one). The residual —
    static text cannot know whether the first publish has happened — is carried
    by the warning itself, which names the removal in every run it fires in.
    """

    # The ONLY repos a DENIED verdict may be non-fatal for. Widening this pair
    # is a deliberate act that has to edit this test too.
    FIRST_PUBLISH = [_RUNNER, _RUNNER_OPI]

    def setUp(self):
        self.text = _read(_PUBLISH_YML)
        self.body = _step_body(self.text, "Assert GHCR package is PUBLIC")
        self.assertIsNotNone(self.body, "no anonymous-pull probe step in smoke-test")

    @staticmethod
    def _fleet():
        """Every image name either platform pulls."""
        return set(_gui_image_names()) | set(_pi_image_names())

    def _allowlist(self):
        lists = _shell_list(self.body, "FIRST_PUBLISH_REPOS")
        self.assertEqual(len(lists), 1,
                         "the probe must declare FIRST_PUBLISH_REPOS exactly once")
        return lists[0]

    def _arm(self, pattern):
        """One arm of the probe's `case "$err"`, ending at ITS OWN `;;`.

        Anchored on the arm's indentation (18 spaces), not on the first `;;`:
        the DENIED arm nests a second `case`, whose arm ends in a `;;` of its
        own two levels deeper.
        """
        m = re.search(pattern + r"[^\n]*\n(.*?)\n {18};;\n", self.body, re.S)
        self.assertIsNotNone(m, f"no {pattern} arm in the probe's case")
        return m.group(1)

    def test_the_allowlist_is_exactly_the_two_never_published_repos(self):
        self.assertEqual(self._allowlist(), self.FIRST_PUBLISH)

    def test_no_repo_that_already_publishes_may_join_the_allowlist(self):
        # The whole point: DENIED stays fatal for every image students already
        # pull. A visibility REGRESSION on one of those is the B1 audit finding
        # this step exists for, and it must never be downgraded to a warning.
        established = self._fleet() - set(self.FIRST_PUBLISH)
        self.assertTrue(established, "sanity: the fleet is more than the runner")
        for repo in sorted(established):
            self.assertNotIn(repo, self._allowlist(),
                             f"{repo} already publishes — DENIED must stay fatal for it")

    def test_every_allowlisted_name_is_a_real_fleet_image(self):
        # A typo here is worse than no allowlist: the guard reads as present and
        # is inert, so the first publish still fails W4.
        for repo in self._allowlist():
            self.assertIn(repo, self._fleet())

    def test_a_denied_allowlisted_repo_warns_and_continues_instead_of_exiting(self):
        arm = self._arm(r"\*denied\*\|\*unauthorized\*")
        guard = re.search(
            r'case " \$\{FIRST_PUBLISH_REPOS\} " in\n(.*?)\n\s*esac', arm, re.S)
        self.assertIsNotNone(
            guard, "the DENIED arm must consult FIRST_PUBLISH_REPOS before it exits")
        guarded = guard.group(1)
        self.assertIn("::warning::", guarded)
        self.assertNotIn("exit 1", guarded,
                         "an allowlisted repo's first publish must not fail the release")
        self.assertRegex(guarded, r"\bcontinue 2\b",
                         "the carve-out must skip to the next REPO, not the next attempt")
        # …and the fatal branch still exists, for everyone else.
        self.assertIn("exit 1", arm.replace(guarded, ""))

    def test_the_warning_names_how_to_retire_the_carve_out(self):
        # Nothing static can know whether the first publish has happened, so the
        # only thing that keeps the allowlist from outliving it is that it SAYS
        # so, loudly, in every run it fires in.
        warning = re.search(r'echo "::warning::([^"]*)"',
                            self._arm(r"\*denied\*\|\*unauthorized\*"))
        self.assertIsNotNone(warning, "the carve-out must print a ::warning::")
        text = warning.group(1)
        self.assertIn("FIRST_PUBLISH_REPOS", text,
                      "the warning must name the list to remove the repo from")
        self.assertIn("public", text,
                      "the warning must name the visibility flip that retires it")

    def test_a_missing_tag_stays_fatal_for_every_repo(self):
        # „manifest unknown" means the package IS anonymously reachable and the
        # TAG is absent — a publish failure, not a first-publish visibility one.
        unknown = self._arm(r"\*manifest\\ unknown\*")
        self.assertNotIn("FIRST_PUBLISH_REPOS", unknown)
        self.assertIn("exit 1", unknown)


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
