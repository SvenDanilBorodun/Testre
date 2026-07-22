"""Cross-boundary lockstep for the two ROBOT_PROFILES registries (audit fix).

The GUI thin descriptor (gui/app/constants.py::ROBOT_PROFILES) and the server
ArmProfile registry (physical_ai_tools/physical_ai_server/physical_ai_server/
robot_profiles.py::ROBOT_PROFILES) encode ONE cross-agent contract: the profile
ids must be identical, and per id the GUI ``follower_only`` flag must agree with
the server's ``follower_only`` / ``capabilities.has_leader`` semantics (a
follower-only profile has NO leader). Both files carry "keep in lockstep"
comments; until now nothing enforced them together.

Both registries are parsed from SOURCE via ``ast`` — the server package pulls
in rclpy/NumPy at runtime and must NEVER be imported here (the suite stays
deps-free stdlib; the same technique as the server-side
test/test_robot_profiles.py::_extract_literal). The GUI side is additionally
cross-checked against the imported ``gui.app.constants`` so the AST extraction
itself can't silently drift from what the GUI actually runs.

Also locks the GUI-internal invariant that leader-need is encoded ONCE:
``scan_requires_leader == not follower_only`` for every profile (the two keys
have opposite defaults — a new profile setting only one of them would silently
disagree with itself).
"""

import ast
import unittest
from pathlib import Path

from gui.app.constants import DEFAULT_ROBOT_PROFILE, ROBOT_PROFILES

_TESTS_DIR = Path(__file__).resolve().parent
_GUI_CONSTANTS = _TESTS_DIR.parent / "gui" / "app" / "constants.py"
_SERVER_PROFILES = (
    _TESTS_DIR.parents[1] / "physical_ai_tools" / "physical_ai_server"
    / "physical_ai_server" / "robot_profiles.py"
)


def _parse_gui_registry():
    """(profiles_dict, default_id) from the GUI constants SOURCE.

    ROBOT_PROFILES is a plain dict literal there, so ast.literal_eval works
    directly on the assignment RHS."""
    tree = ast.parse(_GUI_CONSTANTS.read_text(encoding="utf-8"))
    profiles = None
    default_id = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "ROBOT_PROFILES":
            profiles = ast.literal_eval(node.value)
        elif target.id == "DEFAULT_ROBOT_PROFILE":
            default_id = ast.literal_eval(node.value)
    if profiles is None:
        raise AssertionError(f"ROBOT_PROFILES not found in {_GUI_CONSTANTS}")
    return profiles, default_id


def _armprofile_field_default(tree, field_name):
    """The literal default of an ``ArmProfile`` dataclass field, parsed from
    source. Used so a profile that OMITS an optional kwarg (e.g. omx_full's
    camera_roles) is cross-checked against the value it actually inherits."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ArmProfile":
            for stmt in node.body:
                if (isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id == field_name
                        and stmt.value is not None):
                    return ast.literal_eval(stmt.value)
    raise AssertionError(
        f"ArmProfile has no literal default for {field_name!r} in "
        f"{_SERVER_PROFILES} — update this parser"
    )


def _parse_server_registry():
    """(registry, default_id) from the server robot_profiles.py SOURCE.

    The server registry is built from ``ArmProfile(...)`` dataclass calls, not
    a dict literal, so this walks module-level assignments in two passes:

    1. every ``_NAME = ArmProfile(profile_id=..., follower_only=...,
       capabilities=Capabilities(..., has_leader=...), ...)`` is collected by
       variable name with its literal keyword values;
    2. the ``ROBOT_PROFILES: dict = {_NAME.profile_id: _NAME, ...}`` dict
       (an AnnAssign) is resolved through those variable names, so only
       profiles actually REGISTERED count — a defined-but-unregistered
       ArmProfile would surface as a missing id.

    Returns ``registry`` keyed by profile id with per-id dicts carrying
    ``follower_only`` and ``has_leader``.
    """
    tree = ast.parse(_SERVER_PROFILES.read_text(encoding="utf-8"))

    # The ArmProfile dataclass default for camera_roles — profiles that OMIT the
    # kwarg (omx_full) inherit it, so the lockstep check must resolve to the same
    # value the GUI mirror would have to match (not "absent").
    camera_roles_default = _armprofile_field_default(tree, "camera_roles")

    # Pass 1: ArmProfile(...) constructor calls by variable name.
    by_var = {}
    for node in tree.body:
        if (not isinstance(node, ast.Assign) or len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)):
            continue
        call = node.value
        if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
                or call.func.id != "ArmProfile"):
            continue
        info = {}
        for kw in call.keywords:
            if kw.arg in ("profile_id", "follower_only", "camera_roles"):
                info[kw.arg] = ast.literal_eval(kw.value)
            elif kw.arg == "capabilities" and isinstance(kw.value, ast.Call):
                for cap_kw in kw.value.keywords:
                    if cap_kw.arg == "has_leader":
                        info["has_leader"] = ast.literal_eval(cap_kw.value)
        for key in ("profile_id", "follower_only", "has_leader"):
            if key not in info:
                raise AssertionError(
                    f"ArmProfile {node.targets[0].id} in {_SERVER_PROFILES} "
                    f"has no literal {key}= keyword — update this parser"
                )
        # camera_roles is optional at the call site (dataclass default) — resolve
        # the omitted case to the class default so every profile carries one.
        info.setdefault("camera_roles", camera_roles_default)
        by_var[node.targets[0].id] = info

    # Pass 2: the registry dict + the default id.
    registry = None
    default_id = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_id, value = node.target.id, node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            target_id, value = node.targets[0].id, node.value
        else:
            continue
        if target_id == "ROBOT_PROFILES" and isinstance(value, ast.Dict):
            registry = {}
            for val in value.values:
                if not isinstance(val, ast.Name) or val.id not in by_var:
                    raise AssertionError(
                        f"Unexpected ROBOT_PROFILES value form in "
                        f"{_SERVER_PROFILES} — update this parser"
                    )
                info = by_var[val.id]
                registry[info["profile_id"]] = info
        elif target_id == "DEFAULT_PROFILE_ID":
            default_id = ast.literal_eval(value)
    if registry is None:
        raise AssertionError(f"ROBOT_PROFILES not found in {_SERVER_PROFILES}")
    return registry, default_id


class RegistryLockstepTest(unittest.TestCase):
    """GUI ↔ server ROBOT_PROFILES cross-boundary contract."""

    @classmethod
    def setUpClass(cls):
        cls.gui, cls.gui_default = _parse_gui_registry()
        cls.server, cls.server_default = _parse_server_registry()

    def test_gui_source_parse_matches_imported_constants(self):
        # Guards the AST technique itself: what we parsed from source must be
        # exactly what the GUI imports at runtime.
        self.assertEqual(self.gui, ROBOT_PROFILES)
        self.assertEqual(self.gui_default, DEFAULT_ROBOT_PROFILE)

    def test_profile_id_sets_identical(self):
        self.assertEqual(set(self.gui), set(self.server))
        # Not vacuous: the two shipped profiles must actually be present.
        self.assertLessEqual({"omx_full", "omx_follower"}, set(self.gui))

    def test_follower_only_agrees_per_id(self):
        for pid, gui_prof in self.gui.items():
            self.assertEqual(
                gui_prof["follower_only"], self.server[pid]["follower_only"],
                f"follower_only for {pid!r} disagrees between GUI and server",
            )

    def test_follower_only_matches_server_has_leader(self):
        # Server semantics: a follower-only profile has NO leader (and vice
        # versa) — capabilities.has_leader is the React-facing mirror of the
        # same fact, so all three encodings must agree per id.
        for pid, gui_prof in self.gui.items():
            self.assertEqual(
                gui_prof["follower_only"],
                not self.server[pid]["has_leader"],
                f"GUI follower_only vs server capabilities.has_leader "
                f"disagree for {pid!r}",
            )

    def test_camera_roles_agree_per_id(self):
        # The GUI auto-assigns a single camera the PROFILE's first role and the
        # server config topics hang off the same role list — the two registries
        # must encode the SAME tuple per id. Until now nothing checked it, so the
        # agreement (incl. omx_follower's scene-first fix) was by-value luck.
        for pid, gui_prof in self.gui.items():
            self.assertIn("camera_roles", gui_prof, pid)
            self.assertEqual(
                tuple(gui_prof["camera_roles"]),
                tuple(self.server[pid]["camera_roles"]),
                f"camera_roles for {pid!r} disagree between GUI and server",
            )
        # 'scene' is mandatory fleet-wide (Roboter Studio perception) — assert it
        # on the shared contract so a future profile can't drop it on one side.
        for pid, gui_prof in self.gui.items():
            self.assertIn("scene", gui_prof["camera_roles"], pid)

    def test_arm_family_note(self):
        # arm_family is a GUI-ONLY field (it scopes the usbipd VID/PID attach);
        # the server ArmProfile has no equivalent, so there is nothing to
        # cross-check for it here. The GUI-internal arm_family↔ARM_USB_IDS
        # contract is covered by test_feetech_bus.TestGuiDetectionSeam.
        for pid, gui_prof in self.gui.items():
            self.assertIn("arm_family", gui_prof, pid)

    def test_default_profile_id_lockstep(self):
        # Both sides fall back to the same id on unknown/absent values —
        # a diverged default would boot the server with a different profile
        # than the GUI advertises.
        self.assertEqual(self.gui_default, self.server_default)
        self.assertIn(self.gui_default, self.gui)


class ScanRequiresLeaderConsistencyTest(unittest.TestCase):
    """GUI-internal: leader-need is encoded twice (follower_only vs
    scan_requires_leader, opposite defaults) — they must never disagree."""

    def test_scan_requires_leader_is_not_follower_only(self):
        self.assertTrue(ROBOT_PROFILES)
        for pid, prof in ROBOT_PROFILES.items():
            self.assertIn("follower_only", prof, pid)
            self.assertIn("scan_requires_leader", prof, pid)
            self.assertEqual(
                prof["scan_requires_leader"], not prof["follower_only"],
                f"Profile {pid!r}: scan_requires_leader must equal "
                f"NOT follower_only",
            )


if __name__ == "__main__":
    unittest.main()
