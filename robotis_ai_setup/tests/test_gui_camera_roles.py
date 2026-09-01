"""Camera roles are PROFILE-SCOPED on Windows too, and the UI says so honestly.

Three separate defects, all measured against the shipped GUI:

  * `_start_camera_previews`'s single-camera branch hardcoded „Rolle: Greifer".
    `_on_cameras_changed` had ALREADY been made profile-aware, so on a
    follower-only kit the student saw a green „Szenen-Kamera: Cam0" and, three
    rows below it under the live thumbnail, a grey „Rolle: Greifer".
  * the role picker offered Greifer/Szene unconditionally and
    `config_generator.generate_env_file` accepted either on ANY robot type. On
    `edu6_studio` (camera_roles=('scene',)) that writes CAMERA_NAME_1="gripper",
    and the failure is SILENT AND GREEN: the compose healthcheck greps
    /${CAMERA_NAME_1}/image_raw/compressed — the very topic the student's own
    role name causes the bridge to publish — so „Umgebung starten" reports
    success and Roboter Studio is simply empty. CLAUDE.md names this twin
    divergence with the Pi as "known, accepted, unguarded"; both halves of the
    Pi's fence (filter the options AND refuse server-side) now exist here.
  * the role went STALE across a Robotertyp switch: pick a camera under
    omx_full (role „gripper"), switch the dropdown to edu6_studio, and the role
    and its green label both stayed „Greifer". `_on_robot_type_changed` called
    `_apply_robot_type_labels` and `_update_start_button` but never
    `_on_cameras_changed`. That is the likeliest real-world route into the
    misconfiguration above.

`_on_cameras_changed` is source-extracted against fake `ttk`/`tk` so this stays
deps-free (no display in CI); `generate_env_file` is exercised for real.
"""

import os
import tempfile
import textwrap
import types
import unittest

from gui.app.config_generator import generate_env_file
from gui.app.constants import ROBOT_PROFILES
from gui.app.device_manager import ArmDevice, CameraDevice, HardwareConfig

_GUI_SRC = os.path.join(os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")


def _method_source(name):
    with open(_GUI_SRC, "r", encoding="utf-8") as fh:
        source = fh.read()
    marker = f"    def {name}(self"
    start = source.index(marker)
    rest = source[start:]
    end = rest.find("\n    def ", len(marker))
    return textwrap.dedent(rest[: end if end != -1 else len(rest)])


class _FakeLabel:
    def __init__(self, sink, **kw):
        self.kw = kw
        sink.append(kw)

    def pack(self, **_kw):
        return self


class _FakeFrame:
    def __init__(self):
        self._children = []

    def winfo_children(self):
        return list(self._children)


class _FakeCheckVar:
    def __init__(self, value=True):
        self._v = value

    def get(self):
        return self._v


def _cam(name, path, index):
    return CameraDevice(path=path, name=name, role="", win_index=index)


class RolesComeFromTheProfile(unittest.TestCase):
    """Drive the shipped `_on_cameras_changed` per profile × camera count."""

    def _run(self, profile, n_cams):
        labels = []
        ns = {
            "ROBOT_PROFILES": ROBOT_PROFILES,
            "ttk": types.SimpleNamespace(
                Label=lambda _parent, **kw: _FakeLabel(labels, **kw)),
            "tk": types.SimpleNamespace(W="w", LEFT="left"),
        }
        exec(compile(_method_source("_on_cameras_changed"), _GUI_SRC, "exec"),  # noqa: S102
             ns)
        method = ns["_on_cameras_changed"]

        cams = [_cam(f"Cam{i}", f"Index {i}", i) for i in range(n_cams)]
        previews = []
        owner = types.SimpleNamespace(
            cameras=cams,
            camera_check_vars=[_FakeCheckVar() for _ in cams],
            camera_role_frame=_FakeFrame(),
            hardware=types.SimpleNamespace(cameras=[]),
            _selected_robot_profile=lambda: profile,
            _stop_camera_previews=lambda: None,
            _start_camera_previews=previews.append,
        )
        method(owner)
        return owner, labels, previews

    def test_every_assigned_role_is_one_the_profile_declares(self):
        for profile, row in ROBOT_PROFILES.items():
            allowed = set(row["camera_roles"])
            for n in (1, 2):
                with self.subTest(profile=profile, cameras=n):
                    owner, _labels, _p = self._run(profile, n)
                    assigned = {c.role for c in owner.hardware.cameras}
                    self.assertTrue(
                        assigned <= allowed,
                        f"{profile} was given {assigned - allowed}, which its "
                        f"server config has no topic for")

    def test_a_lone_camera_on_a_follower_only_kit_is_the_SCENE_camera(self):
        for profile in ("omx_follower", "edu6_studio"):
            with self.subTest(profile):
                owner, labels, _p = self._run(profile, 1)
                self.assertEqual(owner.hardware.cameras[0].role, "scene")
                self.assertTrue(
                    any("Szenen-Kamera" in (lb.get("text") or "")
                        for lb in labels), labels)

    def test_a_lone_camera_on_omx_full_is_still_the_GRIPPER_camera(self):
        owner, labels, _p = self._run("omx_full", 1)
        self.assertEqual(owner.hardware.cameras[0].role, "gripper")
        self.assertTrue(
            any("Greifer-Kamera" in (lb.get("text") or "") for lb in labels))

    def test_a_single_role_profile_uses_only_ONE_camera_and_says_so(self):
        """`edu6_studio_config.yaml` subscribes to exactly one topic, so a
        second camera would publish one nothing reads. Silently dropping a
        ticked box would be its own defect, so the cap is stated in German."""
        owner, labels, _p = self._run("edu6_studio", 2)
        self.assertEqual(len(owner.hardware.cameras), 1)
        self.assertTrue(
            any("nur eine Kamera" in (lb.get("text") or "") for lb in labels),
            labels)

    def test_a_two_role_profile_keeps_the_pair_GRIPPER_FIRST(self):
        """`omx_follower` lists ('scene', 'gripper') — that order picks a LONE
        camera's role and must NOT reorder a pair: `generate_env_file` writes
        CAMERA_NAME_1/_2 in list order and the ingest node maps gripper→cam_id
        0, scene→1."""
        for profile in ("omx_full", "omx_follower"):
            with self.subTest(profile):
                owner, _labels, _p = self._run(profile, 2)
                self.assertEqual([c.role for c in owner.hardware.cameras],
                                 ["gripper", "scene"])

    def test_an_unknown_profile_falls_back_to_both_roles(self):
        owner, _labels, _p = self._run("kein_profil", 2)
        self.assertEqual([c.role for c in owner.hardware.cameras],
                         ["gripper", "scene"])


class ThePreviewLabelAgreesWithTheAssignedRole(unittest.TestCase):
    """The two labels used to contradict each other three rows apart."""

    def test_the_hardcoded_greifer_label_is_gone(self):
        src = _method_source("_start_camera_previews")
        self.assertNotIn('text="Rolle: Greifer"', src)
        self.assertNotIn("role is fixed (Greifer)", src)

    def test_the_single_camera_branch_reads_the_assigned_role(self):
        src = _method_source("_start_camera_previews")
        self.assertIn('getattr(cam, "role", "")', src)

    def test_the_picker_values_come_from_the_profile(self):
        src = _method_source("_start_camera_previews")
        self.assertNotIn('values=["Greifer", "Szene"]', src)
        self.assertIn("role_values", src)
        self.assertIn("camera_roles", src)


class TheGeneratorRefusesARoleTheProfileDoesNotDeclare(unittest.TestCase):
    """The server-side half — a stale selection or a programmatic caller
    bypasses the picker entirely. Mirrors the Pi's German 400."""

    def _config(self, role):
        # A leader is present so the both-arms profiles get past the
        # leader-null guard; the follower-only ones derive follower_only=True
        # and simply omit LEADER_PORT. Neither affects the camera fence.
        return HardwareConfig(
            leader=ArmDevice("1-3", "/dev/serial/by-id/leader",
                             "leader", "OpenRB-150"),
            follower=ArmDevice("1-4", "/dev/serial/by-id/follower",
                               "follower", "OpenRB-150"),
            cameras=[CameraDevice(path="Index 0", name="Cam0", role=role,
                                  win_index=0)],
        )

    def _tmp(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env",
                                         delete=False) as f:
            return f.name

    def test_edu6_refuses_a_gripper_camera(self):
        p = self._tmp()
        try:
            with self.assertRaises(ValueError) as ctx:
                generate_env_file(self._config("gripper"), output_path=p,
                                  robot_type="edu6_studio")
            msg = str(ctx.exception)
            self.assertIn("Greifer", msg)
            self.assertIn(ROBOT_PROFILES["edu6_studio"]["display_de"], msg)
            # Rule §1 — literal umlauts, and this message reaches a student.
            self.assertIn("Für", msg)
            for bad in ("Fuer", "fuer"):
                self.assertNotIn(bad, msg)
        finally:
            os.unlink(p)

    def test_edu6_accepts_its_own_scene_camera(self):
        p = self._tmp()
        try:
            content = generate_env_file(self._config("scene"), output_path=p,
                                        robot_type="edu6_studio")
            self.assertIn('CAMERA_NAME_1="scene"', content)
        finally:
            os.unlink(p)

    def test_both_omx_profiles_still_accept_both_roles(self):
        for profile in ("omx_full", "omx_follower"):
            for role in ("gripper", "scene"):
                with self.subTest(profile=profile, role=role):
                    p = self._tmp()
                    try:
                        content = generate_env_file(
                            self._config(role), output_path=p,
                            robot_type=profile,
                            follower_only=None)
                        self.assertIn(f'CAMERA_NAME_1="{role}"', content)
                    finally:
                        os.unlink(p)

    def test_a_bogus_role_still_gets_the_message_that_names_what_a_role_IS(self):
        """The profile fence sits AFTER the generic one on purpose: „Kamera
        ohne gültige Rolle (gripper/scene)" is the programmatic-misuse guard
        and tells a maintainer what the valid values are."""
        p = self._tmp()
        try:
            with self.assertRaises(ValueError) as ctx:
                generate_env_file(self._config("BOGUS"), output_path=p,
                                  robot_type="edu6_studio")
            self.assertIn("ohne gültige Rolle", str(ctx.exception))
        finally:
            os.unlink(p)

    def test_an_unknown_robot_type_is_not_fenced_into_a_dead_end(self):
        """`ROBOT_PROFILES.get(...)` → {} yields both roles, the same fallback
        every other consumer takes. Refusing here would make a typo'd type
        unstartable rather than merely mis-profiled."""
        p = self._tmp()
        try:
            content = generate_env_file(self._config("gripper"), output_path=p,
                                        robot_type="kein_profil")
            self.assertIn('CAMERA_NAME_1="gripper"', content)
        finally:
            os.unlink(p)


class ARobotTypeSwitchReDerivesTheCameraRole(unittest.TestCase):
    """WP-5: one line in `_on_robot_type_changed`, and the defect it closes."""

    def test_the_type_change_re_derives_the_roles(self):
        import ast
        with open(_GUI_SRC, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_on_robot_type_changed")
        code = ast.unparse(fn)
        self.assertIn("self._on_cameras_changed()", code)
        self.assertLess(
            code.index("self._apply_robot_type_labels()"),
            code.index("self._on_cameras_changed()"),
            "the roles are re-derived before the labels that describe them")

    def test_switching_from_omx_full_to_edu6_moves_the_role_to_scene(self):
        """End to end through the two real methods: the stale role used to
        survive the switch and reach `generate_env_file` unchanged."""
        profile = {"id": "omx_full"}
        labels = []
        ns = {
            "ROBOT_PROFILES": ROBOT_PROFILES,
            "ttk": types.SimpleNamespace(
                Label=lambda _parent, **kw: _FakeLabel(labels, **kw)),
            "tk": types.SimpleNamespace(W="w", LEFT="left"),
        }
        exec(compile(_method_source("_on_cameras_changed"), _GUI_SRC, "exec"),  # noqa: S102
             ns)
        on_cameras_changed = ns["_on_cameras_changed"]

        cams = [_cam("Cam0", "Index 0", 0)]
        owner = types.SimpleNamespace(
            cameras=cams,
            camera_check_vars=[_FakeCheckVar()],
            camera_role_frame=_FakeFrame(),
            hardware=types.SimpleNamespace(cameras=[]),
            _selected_robot_profile=lambda: profile["id"],
            _stop_camera_previews=lambda: None,
            _start_camera_previews=lambda _c: None,
        )
        on_cameras_changed(owner)
        self.assertEqual(cams[0].role, "gripper")
        self.assertTrue(
            any("Greifer-Kamera" in (lb.get("text") or "") for lb in labels))

        # The student changes their mind about the robot type.
        labels.clear()
        profile["id"] = "edu6_studio"
        on_cameras_changed(owner)
        self.assertEqual(
            cams[0].role, "scene",
            "the camera kept role='gripper' after the switch — .env then ships "
            "a topic the edu6 server never subscribes to")
        self.assertTrue(
            any("Szenen-Kamera" in (lb.get("text") or "") for lb in labels),
            labels)


if __name__ == "__main__":
    unittest.main()
