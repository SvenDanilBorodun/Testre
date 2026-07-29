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
    """(arg names in order, {name: default literal}) for a top-level def.

    Covers positional-only, positional-or-keyword AND keyword-only parameters:
    a kwonly argument added to one twin alone would otherwise be invisible here,
    which is the same "the guard cannot see the drift" hole this file exists to
    close one level up.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func:
            positional = [a.arg for a in node.args.posonlyargs + node.args.args]
            kwonly = [a.arg for a in node.args.kwonlyargs]
            defaults = {}
            # Positional defaults bind to the TAIL of the positional list.
            for name, default in zip(
                    positional[len(positional) - len(node.args.defaults):],
                    node.args.defaults):
                defaults[name] = ast.literal_eval(default)
            # kwonly defaults are positionally aligned, and may be None.
            for name, default in zip(kwonly, node.args.kw_defaults):
                if default is not None:
                    defaults[name] = ast.literal_eval(default)
            return positional + (["*"] + kwonly if kwonly else []), defaults
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

    def _call_sites(self):
        """Every ``fast_rehydrate_arms(...)`` call in the whole setup tree.

        Scans ALL of ``robotis_ai_setup/`` rather than a list of roots. Two
        reasons, both measured: a two-root ``("gui", "pi_agent")`` loop missed
        the 8 call sites in ``tests/test_device_manager_rehydrate.py``, and
        renaming either root made the guard pass having read ZERO files — the
        exact class CLAUDE.md records ("a directory rename used to turn every
        guard green having read nothing"), which the four `ps_*` guards all
        refuse. Hence the file-count and call-count floors below.
        """
        sites = []
        scanned = 0
        for path in sorted(_SETUP_DIR.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else getattr(fn, "id", None))
                if name == "fast_rehydrate_arms":
                    sites.append((path, node))
        return sites, scanned

    def test_the_caller_scan_is_not_reading_zero_files(self):
        """A guard that cannot fail is worse than no guard."""
        sites, scanned = self._call_sites()
        self.assertGreater(scanned, 100,
                           "the .py scan found almost nothing — did a directory "
                           "move? A vacuous pass is the failure mode here")
        self.assertGreaterEqual(
            len(sites), 2,
            "no fast_rehydrate_arms call sites found — the caller guard below "
            "would be vacuously green")

    def test_no_caller_passes_a_third_argument_positionally(self):
        """The trap this file exists for only fires through a positional call.

        Both defaults are keyword-friendly and every call site uses keywords
        today; this keeps it that way, so a future signature drift stays a
        review question rather than a silent behaviour swap.
        """
        sites, _ = self._call_sites()
        for path, node in sites:
            with self.subTest(f"{path.name}:{node.lineno}"):
                # `f(*args)` is ONE ast.Starred in node.args but an unknown
                # number at runtime, so a length test cannot judge it — the
                # unpacked tuple could carry a third element. Refuse it
                # outright unless it is the ONLY argument form used, which no
                # production caller does.
                starred = [a for a in node.args if isinstance(a, ast.Starred)]
                if starred:
                    self.assertEqual(
                        len(node.args), 1,
                        "a *args unpack mixed with positionals hides how many "
                        "arguments actually arrive — spell them out")
                    continue
                self.assertLessEqual(
                    len(node.args), 2,
                    "pass require_leader / arm_family by KEYWORD — the "
                    "two platform twins must agree on their order, and "
                    "a positional third arg is how they stopped doing "
                    "so once already")


if __name__ == "__main__":
    unittest.main()
