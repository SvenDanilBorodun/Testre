"""Fixture tests for `.github/scripts/compose_env_parity.py`.

The guard exists because `ci.yml::env-forwarding-guard` unions all four compose
files, so a key present on ONE target satisfies it for EVERY target and a key on
the WRONG SERVICE of the right file satisfies it too. Both shipped at once:
`EDUBOTICS_ROBOT_TYPE` sat on the opi compose under `physical_ai_server` only,
and an Orange Pi's arm container ran the OMX Dynamixel bringup against a Feetech
bus. Two verifiers measured that the union guard stayed green with all six keys
deleted from the opi compose.

So the first test below reproduces that exact shape. The repo has already paid
for the alternative once — `ps_cleanup_exit_guard` shipped unable to fail on the
bug it was written for — and these guards emit annotations rather than raising,
which is precisely the shape that rots silently.

Deps-free and cross-platform: the guard is plain Python, run via sys.executable
against throwaway fixture files.
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GUARD = _REPO_ROOT / ".github" / "scripts" / "compose_env_parity.py"


# A minimal but STRUCTURALLY REAL pair: `services:` at column 0, service names
# at indent 2, `environment:` at indent 4, entries at indent 6. That is the shape
# both shipped composes use, and the shape the parser refuses to deviate from.
def _compose(open_manip_keys, server_keys=("HF_TOKEN=${HF_TOKEN:-}",),
             manager_keys=(), extra=""):
    def block(keys):
        if not keys:
            return ""
        return "    environment:\n" + "".join(f"      - {k}\n" for k in keys)

    return (
        "services:\n"
        "  open_manipulator:\n"
        "    image: example\n"
        + block(open_manip_keys)
        + "  physical_ai_server:\n"
        "    image: example\n"
        + block(server_keys)
        + "  physical_ai_manager:\n"
        "    image: example\n"
        + block(manager_keys)
        + extra
    )


_STUDENT_ARM_KEYS = (
    "FOLLOWER_PORT=${FOLLOWER_PORT}",
    "EDUBOTICS_ROBOT_TYPE=${EDUBOTICS_ROBOT_TYPE:-omx_full}",
    "EDUBOTICS_EDU6_JOINT_SIGNS=${EDUBOTICS_EDU6_JOINT_SIGNS:-}",
    "EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS=${EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS:-}",
    "EDUBOTICS_EDU6_EDGE_MARGIN_TICKS=${EDUBOTICS_EDU6_EDGE_MARGIN_TICKS:-}",
    "EDUBOTICS_EDU6_TORQUE_ABORT_TICKS=${EDUBOTICS_EDU6_TORQUE_ABORT_TICKS:-}",
    "EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS=${EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS:-}",
)

# The six EDU6/robot-type names, as the failure messages must name them.
_THE_SIX = (
    "EDUBOTICS_ROBOT_TYPE",
    "EDUBOTICS_EDU6_JOINT_SIGNS",
    "EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS",
    "EDUBOTICS_EDU6_EDGE_MARGIN_TICKS",
    "EDUBOTICS_EDU6_TORQUE_ABORT_TICKS",
    "EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS",
)


def _run(student_text, opi_text, guard=None):
    """Run the guard over two fixture composes. Returns (rc, stdout+stderr)."""
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        s, o = root / "student.yml", root / "opi.yml"
        s.write_text(student_text, encoding="utf-8")
        o.write_text(opi_text, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(guard or _GUARD), str(s), str(o)],
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode, proc.stdout + proc.stderr


class TestTheOriginalBug(unittest.TestCase):
    def test_flags_the_six_keys_missing_from_the_arm_service(self):
        """The shipped bug: the student compose forwards all six to
        `open_manipulator`, the opi compose forwards none of them there."""
        student = _compose(_STUDENT_ARM_KEYS)
        opi = _compose(("FOLLOWER_PORT=${FOLLOWER_PORT}",))
        rc, out = _run(student, opi)
        self.assertEqual(rc, 1, out)
        for key in _THE_SIX:
            self.assertIn(key, out)
        # Naming the SERVICE is half the value — "somewhere in the file" is
        # exactly the diagnosis env-forwarding-guard already gives.
        self.assertIn("'open_manipulator'", out)
        self.assertEqual(out.count("::error"), len(_THE_SIX), out)

    def test_a_key_on_the_wrong_service_is_still_a_failure(self):
        """`EDUBOTICS_ROBOT_TYPE` present on the opi compose — but on
        `physical_ai_server`, which is what actually shipped. The arm container
        branches on it, so this must fail."""
        student = _compose(_STUDENT_ARM_KEYS)
        opi = _compose(
            ("FOLLOWER_PORT=${FOLLOWER_PORT}",) + _STUDENT_ARM_KEYS[2:],
            server_keys=("HF_TOKEN=${HF_TOKEN:-}",
                         "EDUBOTICS_ROBOT_TYPE=${EDUBOTICS_ROBOT_TYPE:-omx_full}"),
        )
        rc, out = _run(student, opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("EDUBOTICS_ROBOT_TYPE", out)
        self.assertIn("'open_manipulator'", out)
        # And the server-side surplus is reported too, in the other direction.
        self.assertIn("'physical_ai_server'", out)

    def test_matching_composes_pass(self):
        text = _compose(_STUDENT_ARM_KEYS)
        rc, out = _run(text, text)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("::error", out)


class TestParserRefusesRatherThanSkips(unittest.TestCase):
    """Every one of these would otherwise vanish from the comparison while the
    job stayed green — the "guard read nothing and passed" class."""

    def test_dict_form_entry_is_refused(self):
        opi = _compose(_STUDENT_ARM_KEYS).replace(
            "      - FOLLOWER_PORT=${FOLLOWER_PORT}\n",
            "      FOLLOWER_PORT: ${FOLLOWER_PORT}\n", 1)
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("not the `- KEY=value` form", out)

    def test_entry_without_a_value_is_refused(self):
        opi = _compose(_STUDENT_ARM_KEYS).replace(
            "      - FOLLOWER_PORT=${FOLLOWER_PORT}\n", "      - FOLLOWER_PORT\n", 1)
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("not the `- KEY=value` form", out)

    def test_duplicate_key_in_one_service_is_refused(self):
        dup = _compose(_STUDENT_ARM_KEYS + ("EDUBOTICS_ROBOT_TYPE=other",))
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), dup)
        self.assertEqual(rc, 1, out)
        self.assertIn("listed twice", out)

    def test_a_service_losing_its_whole_env_block_is_refused(self):
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), _compose(()))
        self.assertEqual(rc, 1, out)
        self.assertIn("no parsed environment entries", out)

    def test_a_file_with_no_services_is_refused(self):
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), "volumes:\n  ai_workspace:\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("no services parsed at all", out)

    def test_a_renamed_required_service_is_loud(self):
        opi = _compose(_STUDENT_ARM_KEYS).replace(
            "  physical_ai_server:\n", "  physical_ai_server_v2:\n", 1)
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("required service 'physical_ai_server' not found", out)

    def test_a_renamed_NON_required_service_is_loud(self):
        """`REQUIRED_ENV_SERVICES` cannot see this one — only service-set
        equality can. Asserted on the equality message specifically, because
        an earlier version of this test passed on a substring the required-set
        fence emits anyway, and so never exercised the loop it was named for."""
        opi = _compose(_STUDENT_ARM_KEYS).replace(
            "  physical_ai_manager:\n", "  physical_ai_manager_v2:\n", 1)
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("exists in", out)
        self.assertIn("physical_ai_manager_v2", out)

    def test_a_flush_indent_sequence_is_READ_not_skipped(self):
        """`environment:` followed by `- KEY=v` at the SAME indent is valid YAML
        and compose injects it. The first version of this parser closed the
        block on `indent <= env_indent` and dropped every such entry silently —
        a key genuinely missing on the Pi came back GREEN (adversarial review,
        2026-07-28). The miss landed on `physical_ai_manager`, i.e. outside the
        REQUIRED_ENV_SERVICES fence, which is why that fence is not enough."""
        student = _compose(_STUDENT_ARM_KEYS).replace(
            "  physical_ai_manager:\n    image: example\n",
            "  physical_ai_manager:\n    image: example\n"
            "    environment:\n    - EDUBOTICS_MANAGER_KNOB=1\n", 1)
        opi = _compose(_STUDENT_ARM_KEYS)
        rc, out = _run(student, opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("EDUBOTICS_MANAGER_KNOB", out)
        self.assertIn("'physical_ai_manager'", out)

    def test_a_flush_indent_sequence_at_parity_still_passes(self):
        """The other direction: reading the shape must not invent a diff."""
        text = _compose(_STUDENT_ARM_KEYS).replace(
            "  physical_ai_manager:\n    image: example\n",
            "  physical_ai_manager:\n    image: example\n"
            "    environment:\n    - EDUBOTICS_MANAGER_KNOB=1\n", 1)
        rc, out = _run(text, text)
        self.assertEqual(rc, 0, out)

    def test_a_second_environment_block_in_one_service_is_refused(self):
        """The realistic shape: two `environment:` keys separated by another
        property. Tracking "am I currently inside one" missed it and merged
        both blocks; tracking "has this service had one" catches it."""
        opi = _compose(_STUDENT_ARM_KEYS).replace(
            "  physical_ai_server:\n    image: example\n",
            "  physical_ai_server:\n    image: example\n"
            "    environment:\n      - EDUBOTICS_FIRST=1\n"
            "    volumes:\n      - /dev:/dev\n", 1)
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("a second `environment:`", out)

    def test_an_inline_or_flow_style_environment_is_refused(self):
        opi = _compose(_STUDENT_ARM_KEYS).replace(
            "    environment:\n", "    environment: [EDUBOTICS_A=1]\n", 1)
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("inline", out)

    def test_env_file_extends_and_merge_keys_are_refused(self):
        """Three compose mechanisms that carry environment past this parser.
        Refusing beats comparing an incomplete picture and calling it parity."""
        for injected in ("env_file: .env.extra", "extends: other", "<<: *common"):
            with self.subTest(injected=injected):
                opi = _compose(_STUDENT_ARM_KEYS).replace(
                    "  open_manipulator:\n    image: example\n",
                    f"  open_manipulator:\n    image: example\n    {injected}\n", 1)
                rc, out = _run(_compose(_STUDENT_ARM_KEYS), opi)
                self.assertEqual(rc, 1, out)
                self.assertIn("cannot see", out)

    def test_an_alias_that_cannot_carry_env_is_left_alone(self):
        """`logging: *opi_logging` is real, in the shipped opi compose. A
        blanket alias refusal would have failed the very file this gate exists
        to check."""
        text = _compose(_STUDENT_ARM_KEYS).replace(
            "  open_manipulator:\n    image: example\n",
            "  open_manipulator:\n    image: example\n"
            "    logging: &L\n      driver: json-file\n", 1).replace(
            "  physical_ai_server:\n    image: example\n",
            "  physical_ai_server:\n    image: example\n    logging: *L\n", 1)
        rc, out = _run(text, text)
        self.assertEqual(rc, 0, out)

    def test_an_opi_only_service_is_loud(self):
        opi = _compose(_STUDENT_ARM_KEYS) + (
            "  sidecar:\n    image: busybox\n"
            "    environment:\n      - EDUBOTICS_SNEAKY=1\n")
        rc, out = _run(_compose(_STUDENT_ARM_KEYS), opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("sidecar", out)

    def test_a_service_MISSING_from_the_opi_compose_is_loud(self):
        """The other and more serious direction: a whole service the Pi does not
        have. It needs its own test — a rename satisfies the opi-only branch
        too, so mutation testing showed this branch surviving until now."""
        student = _compose(_STUDENT_ARM_KEYS, manager_keys=("EDUBOTICS_X=1",))
        opi = _compose(_STUDENT_ARM_KEYS).replace(
            "  physical_ai_manager:\n    image: example\n", "", 1)
        rc, out = _run(student, opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("exists in", out)
        self.assertIn("its 1 env keys are unchecked on the Pi", out)


class TestValueDiffsAreInformationalOnly(unittest.TestCase):
    def test_a_differing_value_does_not_fail_the_gate(self):
        """The opi compose legitimately pins EDUBOTICS_CAMERA_SOURCE=usb_cam
        where the student compose interpolates. Gating value equality would
        break that on day one."""
        student = _compose(_STUDENT_ARM_KEYS + ("EDUBOTICS_CAMERA_SOURCE=${EDUBOTICS_CAMERA_SOURCE:-}",))
        opi = _compose(_STUDENT_ARM_KEYS + ("EDUBOTICS_CAMERA_SOURCE=usb_cam",))
        rc, out = _run(student, opi)
        self.assertEqual(rc, 0, out)
        self.assertIn("::notice", out)

    def test_an_interpolated_key_pinned_to_a_literal_is_classified(self):
        """Not gated, but it must be VISIBLE: a knob the student `.env` can
        override and the Pi `.env` cannot is the one value-diff sub-class that
        can silently disable a documented rollback."""
        student = _compose(_STUDENT_ARM_KEYS + ("EDUBOTICS_EDU6_EDGE_MARGIN_TICKS_X=${A:-}",))
        opi = _compose(_STUDENT_ARM_KEYS + ("EDUBOTICS_EDU6_EDGE_MARGIN_TICKS_X=40",))
        rc, out = _run(student, opi)
        self.assertEqual(rc, 0, out)
        self.assertIn("PINNED-LITERAL", out)

    def test_two_interpolated_defaults_differing_is_a_plain_value_notice(self):
        student = _compose(_STUDENT_ARM_KEYS + ("EDUBOTICS_X=${EDUBOTICS_X:-a}",))
        opi = _compose(_STUDENT_ARM_KEYS + ("EDUBOTICS_X=${EDUBOTICS_X:-b}",))
        rc, out = _run(student, opi)
        self.assertEqual(rc, 0, out)
        self.assertIn("::notice", out)
        self.assertNotIn("PINNED-LITERAL", out)


class TestAllowlist(unittest.TestCase):
    """These run the guard with NO arguments inside a synthetic repo root, so
    they exercise the same code path CI does — including the default path
    resolution off `__file__`, and the stale-entry check, which deliberately
    applies only to the shipped pair the allowlist describes."""

    def _run_as_shipped(self, allowlist_src, student, opi):
        src = _GUARD.read_text(encoding="utf-8")
        start = src.index('INTENTIONAL: "dict" = {')
        end = src.index("\n}\n", start) + len("\n}\n")
        mutated = src[:start] + allowlist_src + src[end:]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / ".github" / "scripts").mkdir(parents=True)
            (root / "robotis_ai_setup" / "docker").mkdir(parents=True)
            guard = root / ".github" / "scripts" / "compose_env_parity.py"
            guard.write_text(mutated, encoding="utf-8")
            (root / "robotis_ai_setup/docker/docker-compose.yml").write_text(
                student, encoding="utf-8")
            (root / "robotis_ai_setup/docker/docker-compose.opi.yml").write_text(
                opi, encoding="utf-8")
            proc = subprocess.run([sys.executable, str(guard)],
                                  capture_output=True, text=True, timeout=60)
            return proc.returncode, proc.stdout + proc.stderr

    def test_an_emptied_allowlist_turns_the_divergence_into_a_failure(self):
        student = _compose(_STUDENT_ARM_KEYS)
        opi = _compose(_STUDENT_ARM_KEYS, manager_keys=("EDUBOTICS_PI_ONLY=1",))
        allowed = (
            'INTENTIONAL: "dict" = {\n'
            '    ("opi", "physical_ai_manager", "EDUBOTICS_PI_ONLY"): "reason",\n'
            "}\n"
        )
        rc, out = self._run_as_shipped(allowed, student, opi)
        self.assertEqual(rc, 0, out)
        rc, out = self._run_as_shipped('INTENTIONAL: "dict" = {\n}\n', student, opi)
        self.assertEqual(rc, 1, out)
        self.assertIn("EDUBOTICS_PI_ONLY", out)

    def test_a_stale_allowlist_entry_fails(self):
        """A divergence that was fixed must not leave a live exemption behind —
        that is how an allowlist rots into permanent permission."""
        text = _compose(_STUDENT_ARM_KEYS)
        allowed = (
            'INTENTIONAL: "dict" = {\n'
            '    ("opi", "open_manipulator", "EDUBOTICS_LONG_GONE"): "was fixed",\n'
            "}\n"
        )
        rc, out = self._run_as_shipped(allowed, text, text)
        self.assertEqual(rc, 1, out)
        self.assertIn("EDUBOTICS_LONG_GONE", out)
        self.assertIn("no longer exists", out)

    def test_the_stale_check_does_not_fire_on_an_explicit_pair(self):
        """The allowlist describes the two SHIPPED composes. Judging a fixture
        pair against it would fail every comparison for an unrelated reason —
        found by these tests before it could confuse anyone."""
        text = _compose(_STUDENT_ARM_KEYS)
        rc, out = _run(text, text)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("no longer exists", out)

    def test_every_shipped_allowlist_entry_carries_a_reason(self):
        src = _GUARD.read_text(encoding="utf-8")
        namespace = {}
        start = src.index('INTENTIONAL: "dict" = {')
        end = src.index("\n}\n", start) + len("\n}\n")
        exec(src[start:end].replace('INTENTIONAL: "dict" =', "INTENTIONAL ="),
             namespace)
        for entry, reason in namespace["INTENTIONAL"].items():
            side, service, key = entry
            self.assertIn(side, ("student", "opi"), entry)
            self.assertTrue(service and key, entry)
            self.assertIsInstance(reason, str, entry)
            self.assertGreater(len(reason.strip()), 30, entry)


class TestAgainstTheRealComposes(unittest.TestCase):
    def test_the_shipped_composes_are_at_parity(self):
        """No args: the guard resolves both composes from its own location,
        exactly as CI invokes it. This is the live invariant, not a fixture."""
        proc = subprocess.run([sys.executable, str(_GUARD)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_shipped_pi_twin_is_covered_by_byte_identity(self):
        """This guard deliberately does not re-parse the pi_agent twin; it
        relies on the twin being byte-identical (pi-compose-twin-guard +
        release.yml::pi-agent-tarball). If that ever stops being true, the
        guard's scope claim is false — so assert the precondition here rather
        than discover it in the field."""
        src = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.opi.yml"
        twin = _REPO_ROOT / "robotis_ai_setup/pi_agent/docker/docker-compose.opi.yml"
        self.assertEqual(src.read_bytes(), twin.read_bytes())


if __name__ == "__main__":
    unittest.main()
