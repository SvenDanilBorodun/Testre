"""Type-aware GUI behaviour for the robot-type (ArmProfile) selection (T3).

These exercise two EduBoticsApp methods WITHOUT constructing the full tkinter
app: the method source is extracted from gui_app.py and exec'd into an injected
namespace of test doubles (mirrors test_camera_preview_render's headless
snippet-extraction so a runner without tkinter/webview still runs them). The
methods reach only module globals we inject (config_generator, docker_manager,
device_manager, ROBOT_PROFILES, tk, IMAGE_OPEN_MANIPULATOR, threading), so the
extracted copy behaves identically to the real one.

Covered:
  * _rs_set_leader_mode — first-ever coverage: the forward regen AND the
    failed-restart rollback must BOTH carry robot_type= (EDUBOTICS_ROBOT_TYPE is
    MANAGED — dropping it on the rollback silently rewrites the type to
    omx_full); a follower-only type refuses the switch (belt-and-suspenders).
  * _scan_arms — a follower-only robot type treats a leader-less scan as SUCCESS
    and must NOT drop into the diagnose/repair flow; a both-arms type with only
    the follower found still routes into diagnose.
"""

import os
import textwrap
import types
import unittest

from gui.app.constants import ROBOT_PROFILES

_GUI_SRC = os.path.join(os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")


def _load_method(method_name, ns):
    """Extract `method_name` from gui_app.py and exec it into ``ns``.

    ``ns`` becomes the function's globals, so every module-level name the method
    references must be present there. Returns the callable."""
    with open(_GUI_SRC, "r", encoding="utf-8") as fh:
        source = fh.read()
    marker = f"    def {method_name}(self"
    start = source.index(marker)
    rest = source[start:]
    end = rest.find("\n    def ", len(marker))
    snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
    exec(compile(snippet, _GUI_SRC, "exec"), ns)  # noqa: S102 — in-repo source
    return ns[method_name]


class _SyncThread:
    """threading.Thread stand-in that runs target() synchronously on start()."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class RsSetLeaderModeTest(unittest.TestCase):
    """_rs_set_leader_mode robot_type carry (forward + rollback) + follower-only
    refusal. read_env_var returns the PREVIOUS EDUBOTICS_FOLLOWER_ONLY."""

    def _make(self, restart_ok, prev_follower_only="0"):
        calls = []
        fake_cg = types.SimpleNamespace(
            read_env_var=lambda k, p: prev_follower_only,
            generate_env_file=lambda *a, **kw: calls.append(kw) or "",
        )
        fake_dm = types.SimpleNamespace(
            restart_open_manipulator=lambda gpu=False, log=None: restart_ok,
        )
        ns = {
            "config_generator": fake_cg,
            "docker_manager": fake_dm,
            "ROBOT_PROFILES": ROBOT_PROFILES,
            "ENV_FILE": "/tmp/edubotics-test.env",
        }
        method = _load_method("_rs_set_leader_mode", ns)
        owner = types.SimpleNamespace(
            _rs_robot_type="omx_full",
            hardware=types.SimpleNamespace(leader=object(), follower=object()),
            _rs_phone_enabled=False,
            _rs_switch_in_flight=False,
            _rs_use_gpu=False,
        )
        return method, owner, calls

    def test_forward_regen_carries_robot_type(self):
        method, owner, calls = self._make(restart_ok=True)
        ok, _msg = method(owner, True, lambda *a: None)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["robot_type"], "omx_full")
        self.assertTrue(calls[0]["follower_only"])

    def test_failed_restart_rollback_preserves_robot_type(self):
        method, owner, calls = self._make(restart_ok=False, prev_follower_only="0")
        ok, _msg = method(owner, True, lambda *a: None)
        self.assertFalse(ok)
        # Two regens: forward (follower_only=True) + rollback (follower_only=prev).
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["robot_type"], "omx_full")
        self.assertTrue(calls[0]["follower_only"])
        # The rollback MUST still carry the type (else the type flips to omx_full)
        # and restore the previous follower_only (0 → False).
        self.assertEqual(calls[1]["robot_type"], "omx_full")
        self.assertFalse(calls[1]["follower_only"])

    def test_follower_only_type_refuses_switch(self):
        method, owner, calls = self._make(restart_ok=True)
        owner._rs_robot_type = "omx_follower"
        ok, msg = method(owner, True, lambda *a: None)
        self.assertFalse(ok)
        self.assertIn("nicht verfügbar", msg)
        # No .env was rewritten.
        self.assertEqual(calls, [])


class ScanArmsTypeAwareTest(unittest.TestCase):
    """_scan_arms success branch is type-aware: a follower-only type succeeds on
    a leader-less scan (no diagnose); a both-arms type does not."""

    def _run_scan(self, profile_id, scan_result):
        diag_calls = []

        def _diagnose(image=None):
            diag_calls.append(image)
            return types.SimpleNamespace(message_de="Fehler\nDetails", details="x")

        fake_dm = types.SimpleNamespace(
            ensure_environment_stopped=lambda log=None: False,
        )
        fake_dev = types.SimpleNamespace(
            scan_and_identify_arms=lambda image: scan_result,
            diagnose_usb_environment=_diagnose,
            get_diagnostics_log_path=lambda: "/tmp/diag.log",
        )
        ns = {
            "threading": types.SimpleNamespace(Thread=_SyncThread),
            "docker_manager": fake_dm,
            "device_manager": fake_dev,
            "IMAGE_OPEN_MANIPULATOR": "img",
            "ROBOT_PROFILES": ROBOT_PROFILES,
            "tk": types.SimpleNamespace(DISABLED="disabled", NORMAL="normal"),
        }
        method = _load_method("_scan_arms", ns)
        statuses = []
        owner = types.SimpleNamespace(
            _scanning=False,
            btn_scan_leader=types.SimpleNamespace(config=lambda **kw: None),
            _selected_robot_profile=lambda: profile_id,
            root=types.SimpleNamespace(after=lambda *a, **k: None),
            progress=types.SimpleNamespace(start=lambda *a: None, stop=lambda *a: None),
            hardware=types.SimpleNamespace(leader=None, follower=None),
            leader_status_var=types.SimpleNamespace(set=lambda *a: None),
            follower_status_var=types.SimpleNamespace(set=lambda *a: None),
            _set_status=statuses.append,
            _log=lambda *a: None,
            _clear_arm_repair=lambda: None,
            _stop_camera_bridge=lambda: None,
            _stop_rs_control_server=lambda: None,
            _update_start_button=lambda: None,
            _show_arm_repair=lambda d: None,
            running=False,
        )
        method(owner)
        return owner, statuses, diag_calls

    def test_follower_only_leaderless_scan_is_success_no_diagnose(self):
        follower = types.SimpleNamespace(
            description="OpenRB-150", serial_path="/dev/serial/by-id/follower")
        owner, statuses, diag_calls = self._run_scan(
            "omx_follower", (None, follower))
        # SUCCESS: follower adopted, diagnose NEVER run.
        self.assertIs(owner.hardware.follower, follower)
        self.assertEqual(diag_calls, [])
        self.assertTrue(any("Follower-Arm gefunden" in s for s in statuses))

    def test_both_arms_type_with_only_follower_routes_to_diagnose(self):
        follower = types.SimpleNamespace(
            description="OpenRB-150", serial_path="/dev/serial/by-id/follower")
        _owner, _statuses, diag_calls = self._run_scan(
            "omx_full", (None, follower))
        # A both-arms type is NOT satisfied by the follower alone → diagnose runs.
        self.assertEqual(diag_calls, ["img"])


if __name__ == "__main__":
    unittest.main()
