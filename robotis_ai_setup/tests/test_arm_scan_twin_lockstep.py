"""Cross-boundary lockstep for the arm-scan twins (deps-free, AST-only).

``fast_rehydrate_arms`` exists twice — once for Windows
(``gui/app/device_manager.py``) and once natively for the Orange Pi
(``pi_agent/identify_arm.py``) — because neither platform can import the other
(``device_manager`` is Windows/usbipd-bound, ``pi_agent`` ships as a standalone
tarball). Same deliberate decision as the three ``ROBOT_PROFILES`` copies: a
lockstep test is the correct coupling, not a shared module.

What is fenced here is the SIGNATURE, and the reason is specific. The Pi twin
grew ``arm_family`` first and ``require_leader`` second, while Windows has them
the other way round — so a THIRD POSITIONAL argument meant opposite things on
the two platforms. Nothing catches that: each side's own tests pass, both
functions type-check, and every caller today happens to use keywords. The next
one would silently ask a Pi to scan the ``True`` arm family.

Deliberately NOT asserted: the bodies. They are legitimately different — the
Windows path attaches USB devices through usbipd and retries a presence loop,
the Pi reads ``/dev/serial/by-id`` that native udev has already linked. Only the
call CONTRACT has to agree.
"""

import ast
import unittest
from pathlib import Path

_SETUP_DIR = Path(__file__).resolve().parent.parent  # robotis_ai_setup/
_WINDOWS = _SETUP_DIR / "gui" / "app" / "device_manager.py"
_PI = _SETUP_DIR / "pi_agent" / "identify_arm.py"

# The parameters both twins must expose, in this order. Written out rather than
# derived from one side so that a change has to be made deliberately on BOTH —
# deriving it would let the two agree on whatever one of them happened to say.
_EXPECTED_ARGS = ["saved_leader_path", "saved_follower_path",
                  "require_leader", "arm_family"]
_EXPECTED_DEFAULTS = {"require_leader": True, "arm_family": "omx"}


def _signature(path: Path, func: str):
    """(arg names in order, {name: default literal}) for a top-level def."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func:
            args = [a.arg for a in node.args.args]
            defaults = {}
            # Defaults bind to the TAIL of args.
            for name, default in zip(args[len(args) - len(node.args.defaults):],
                                     node.args.defaults):
                defaults[name] = ast.literal_eval(default)
            return args, defaults
    raise AssertionError(f"{func} not found in {path}")


class TestFastRehydrateArmsTwinSignature(unittest.TestCase):
    def test_both_twins_expose_the_same_parameters_in_the_same_order(self):
        for label, path in (("windows", _WINDOWS), ("pi", _PI)):
            with self.subTest(label):
                args, _ = _signature(path, "fast_rehydrate_arms")
                self.assertEqual(args, _EXPECTED_ARGS)

    def test_both_twins_carry_the_same_defaults(self):
        for label, path in (("windows", _WINDOWS), ("pi", _PI)):
            with self.subTest(label):
                _, defaults = _signature(path, "fast_rehydrate_arms")
                self.assertEqual(defaults, _EXPECTED_DEFAULTS)

    def test_the_two_signatures_are_equal_to_each_other(self):
        # Belt: the two assertions above could both be updated to a new-but-
        # matching shape; this one fails the moment they diverge from EACH
        # OTHER, which is the property that actually bites.
        self.assertEqual(_signature(_WINDOWS, "fast_rehydrate_arms"),
                         _signature(_PI, "fast_rehydrate_arms"))

    def test_no_caller_passes_a_third_argument_positionally(self):
        """The trap this file exists for only fires through a positional call.

        Both defaults are keyword-friendly and every call site uses keywords
        today; this keeps it that way, so a future signature drift stays a
        review question rather than a silent behaviour swap.
        """
        for root in ("gui", "pi_agent"):
            for path in sorted((_SETUP_DIR / root).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    name = (fn.attr if isinstance(fn, ast.Attribute)
                            else getattr(fn, "id", None))
                    if name != "fast_rehydrate_arms":
                        continue
                    with self.subTest(f"{path.name}:{node.lineno}"):
                        self.assertLessEqual(
                            len(node.args), 2,
                            "pass require_leader / arm_family by KEYWORD — the "
                            "two platform twins must agree on their order, and "
                            "a positional third arg is how they stopped doing "
                            "so once already")


if __name__ == "__main__":
    unittest.main()
