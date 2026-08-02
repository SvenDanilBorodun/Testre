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
  * _try_rehydrate_arms — first-ever direct coverage: the omx_follower
    leader-less path succeeds without a LEADER_PORT; a mismatch only PROMPTS
    (never auto-scans); a follower-only rehydrate must not clobber a
    previously-scanned leader with None.
"""

import os
import textwrap
import types
import unittest

from gui.app.constants import DEFAULT_ROBOT_PROFILE, ROBOT_PROFILES

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

        def _diagnose(image=None, arm_family="omx"):
            diag_calls.append(image)
            return types.SimpleNamespace(message_de="Fehler\nDetails", details="x")

        fake_dm = types.SimpleNamespace(
            ensure_environment_stopped=lambda log=None: False,
        )
        fake_dev = types.SimpleNamespace(
            scan_and_identify_arms=lambda image, arm_family='omx': scan_result,
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


class TryRehydrateArmsTest(unittest.TestCase):
    """_try_rehydrate_arms — the launch-time fast rehydrate (first direct
    coverage). The persisted EDUBOTICS_ROBOT_TYPE drives whether a LEADER_PORT
    is required; on ANY mismatch the method only PROMPTS the student to rescan
    (it never triggers the full scan itself); and a follower-only rehydrate
    result (leader=None) must never overwrite an already-scanned leader."""

    _FOLLOWER_PATH = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_BBB2-if00"
    _LEADER_PATH = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_AAA1-if00"

    def _run(self, env, rehydrate_result, hardware=None):
        """Extract + run _try_rehydrate_arms against an owner of test doubles.

        ``env`` backs the fake config_generator.read_env_var (the persisted
        .env); ``rehydrate_result`` is what the fake fast_rehydrate_arms
        returns. Returns (owner, logs, rehydrate_calls, update_calls,
        follower_status, leader_status)."""
        rehydrate_calls = []

        def _fake_rehydrate(leader_path, follower_path, require_leader=True,
                            arm_family="omx"):
            rehydrate_calls.append({
                "leader": leader_path,
                "follower": follower_path,
                "require_leader": require_leader,
                "arm_family": arm_family,
            })
            return rehydrate_result

        ns = {
            "config_generator": types.SimpleNamespace(
                read_env_var=lambda key, path: env.get(key)),
            "device_manager": types.SimpleNamespace(
                fast_rehydrate_arms=_fake_rehydrate),
            "threading": types.SimpleNamespace(Thread=_SyncThread),
            "ROBOT_PROFILES": ROBOT_PROFILES,
            "DEFAULT_ROBOT_PROFILE": DEFAULT_ROBOT_PROFILE,
            "ENV_FILE": "/tmp/edubotics-test.env",
        }
        method = _load_method("_try_rehydrate_arms", ns)

        if hardware is None:
            hardware = types.SimpleNamespace(leader=None, follower=None)

        def _hardware_ready(profile):
            # Mirrors EduBoticsApp._hardware_ready: a both-arms type needs
            # leader+follower; a follower-only type needs only the follower.
            if ROBOT_PROFILES.get(profile, {}).get("scan_requires_leader", True):
                return (hardware.leader is not None
                        and hardware.follower is not None)
            return hardware.follower is not None

        logs, update_calls = [], []
        leader_status, follower_status = [], []
        owner = types.SimpleNamespace(
            cloud_only=types.SimpleNamespace(get=lambda: False),
            _scanning=False,
            _hardware_ready=_hardware_ready,
            hardware=hardware,
            # Execute after-callbacks synchronously so the status-var sets run.
            root=types.SimpleNamespace(
                after=lambda _ms, fn=None: fn() if fn is not None else None),
            leader_status_var=types.SimpleNamespace(set=leader_status.append),
            follower_status_var=types.SimpleNamespace(set=follower_status.append),
            _log=logs.append,
            _update_start_button=lambda: update_calls.append(True),
        )
        method(owner)
        return (owner, logs, rehydrate_calls, update_calls,
                follower_status, leader_status)

    def test_omx_follower_leaderless_env_rehydrates_follower_only(self):
        # (a) An omx_follower .env has NO LEADER_PORT — that is legal: the
        # rehydrate must run with require_leader=False and adopt the follower.
        follower = types.SimpleNamespace(
            description="OpenRB-150 BBB2", serial_path=self._FOLLOWER_PATH)
        env = {
            "EDUBOTICS_ROBOT_TYPE": "omx_follower",
            "FOLLOWER_PORT": self._FOLLOWER_PATH,
            "LEADER_PORT": None,
        }
        owner, logs, calls, update_calls, follower_status, leader_status = \
            self._run(env, (None, follower))
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["require_leader"])
        self.assertEqual(calls[0]["arm_family"], "omx")  # omx_follower family
        self.assertEqual(calls[0]["leader"], "")  # leader_port or ""
        self.assertEqual(calls[0]["follower"], self._FOLLOWER_PATH)
        self.assertIs(owner.hardware.follower, follower)
        self.assertIsNone(owner.hardware.leader)
        self.assertTrue(any("Wiederhergestellt" in s for s in follower_status))
        self.assertEqual(leader_status, [])  # no leader line for this type
        self.assertTrue(any("wiederhergestellt" in ln for ln in logs))
        self.assertEqual(update_calls, [True])

    def test_edu6_env_threads_edu6_arm_family(self):
        # (a2) An edu6_studio .env fast-rehydrates like a follower-only OMX kit,
        # but the family must be threaded as "edu6" so the CH343 adapter is
        # attached + matched (OMX defaults would silently no-op an edu6 rig).
        follower = types.SimpleNamespace(
            description="1a86 USB Single Serial", serial_path=self._FOLLOWER_PATH)
        env = {
            "EDUBOTICS_ROBOT_TYPE": "edu6_studio",
            "FOLLOWER_PORT": self._FOLLOWER_PATH,
            "LEADER_PORT": None,
        }
        _owner, _logs, calls, _update, _fs, _ls = self._run(env, (None, follower))
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["require_leader"])
        self.assertEqual(calls[0]["arm_family"], "edu6")

    def test_mismatch_prompts_rescan_and_never_auto_scans(self):
        # (b) fast_rehydrate_arms falls back with (None, None) — the method
        # must only PROMPT ("Arme scannen") and leave the hardware untouched;
        # it must NOT adopt anything or claim success.
        env = {
            "EDUBOTICS_ROBOT_TYPE": "omx_full",
            "FOLLOWER_PORT": self._FOLLOWER_PATH,
            "LEADER_PORT": self._LEADER_PATH,
        }
        owner, logs, calls, update_calls, follower_status, leader_status = \
            self._run(env, (None, None))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["require_leader"])
        self.assertIsNone(owner.hardware.leader)
        self.assertIsNone(owner.hardware.follower)
        self.assertTrue(
            any("nicht bestätigt" in ln and "Arme scannen" in ln
                for ln in logs),
            logs,
        )
        # No status lines set, no start-button refresh, no success message.
        self.assertEqual(follower_status, [])
        self.assertEqual(leader_status, [])
        self.assertEqual(update_calls, [])
        self.assertFalse(any("wiederhergestellt" in ln for ln in logs))

    def test_follower_only_rehydrate_keeps_previously_scanned_leader(self):
        # (c) THE `if leader is not None:` guard: a previously-scanned leader
        # already sits on self.hardware; an omx_follower rehydrate returns
        # leader=None — the guard must skip the assignment, never clobber the
        # scanned leader with None.
        prev_leader = types.SimpleNamespace(
            description="OpenRB-150 AAA1", serial_path=self._LEADER_PATH)
        follower = types.SimpleNamespace(
            description="OpenRB-150 BBB2", serial_path=self._FOLLOWER_PATH)
        hardware = types.SimpleNamespace(leader=prev_leader, follower=None)
        env = {
            "EDUBOTICS_ROBOT_TYPE": "omx_follower",
            "FOLLOWER_PORT": self._FOLLOWER_PATH,
            "LEADER_PORT": None,
        }
        owner, _logs, calls, _update_calls, follower_status, leader_status = \
            self._run(env, (None, follower), hardware=hardware)
        self.assertEqual(len(calls), 1)
        self.assertIs(owner.hardware.leader, prev_leader)  # NOT overwritten
        self.assertIs(owner.hardware.follower, follower)
        # Only the follower status line updates; the leader line stays as the
        # scan left it.
        self.assertTrue(any("Wiederhergestellt" in s for s in follower_status))
        self.assertEqual(leader_status, [])


class HardwareReadyArmFamilyTest(unittest.TestCase):
    """_hardware_ready is ARM-FAMILY-aware: crossing families invalidates a scan.

    MEASURED headless before this landed, with the real device_manager
    dataclasses: an OMX pair scanned gave ``_hardware_ready('edu6_studio')``
    True, and a single edu6 arm scanned gave ``_hardware_ready('omx_follower')``
    True. Either one arms „Umgebung starten" for a bringup against the wrong
    servo bus — the Pi twin of this hole was measured all the way through to a
    ~300 s ``service_healthy`` block and a generic German failure.

    The .env path was already safe here (``_try_rehydrate_arms`` passes
    ``arm_family=`` into ``fast_rehydrate_arms``, which filters), so the hole
    was purely in-session: scan, then move the Modus dropdown.
    """

    _OMX_L = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_LEADER-if00"
    _OMX_F = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_FOLLOWER-if00"
    _EDU6 = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A68010132-if00"

    def _arm(self, path):
        return types.SimpleNamespace(serial_path=path)

    def _ready(self, leader_path, follower_path, profile):
        """Drive the REAL _hardware_ready + _arms_conflict_with_family over the
        REAL device_manager predicate — a stubbed conflict check would prove
        only that the call happens, not that the verdict is right."""
        from gui.app import device_manager

        ns = {"ROBOT_PROFILES": ROBOT_PROFILES, "device_manager": device_manager}
        hardware_ready = _load_method("_hardware_ready", ns)
        conflict = _load_method("_arms_conflict_with_family", ns)
        leader = self._arm(leader_path) if leader_path else None
        follower = self._arm(follower_path) if follower_path else None
        owner = types.SimpleNamespace(
            hardware=types.SimpleNamespace(
                leader=leader, follower=follower,
                is_complete=leader is not None and follower is not None,
                follower_present=follower is not None),
            _selected_robot_profile=lambda: profile,
        )
        owner._arms_conflict_with_family = lambda p: conflict(owner, p)
        return hardware_ready(owner, profile)

    # ── the defect, both directions ─────────────────────────────────────────

    def test_an_omx_pair_is_not_ready_for_edu6(self):
        self.assertFalse(self._ready(self._OMX_L, self._OMX_F, "edu6_studio"))

    def test_an_edu6_arm_is_not_ready_for_omx_follower(self):
        self.assertFalse(self._ready(None, self._EDU6, "omx_follower"))

    def test_an_edu6_arm_is_not_ready_for_omx_full_either(self):
        # Already False before the fix, but for the WRONG reason (no leader) —
        # pin it against a future change that relaxes the leader rule.
        self.assertFalse(self._ready(self._EDU6, self._EDU6, "omx_full"))

    # ── what must NOT change ────────────────────────────────────────────────

    def test_an_ordinary_omx_rig_is_unaffected(self):
        self.assertTrue(self._ready(self._OMX_L, self._OMX_F, "omx_full"))
        self.assertTrue(self._ready(self._OMX_L, self._OMX_F, "omx_follower"))

    def test_an_ordinary_edu6_rig_is_unaffected(self):
        self.assertTrue(self._ready(None, self._EDU6, "edu6_studio"))

    def test_a_missing_scan_is_still_just_not_ready(self):
        self.assertFalse(self._ready(None, None, "omx_full"))
        self.assertFalse(self._ready(self._OMX_L, None, "omx_full"))
        self.assertFalse(self._ready(None, None, "edu6_studio"))

    def test_an_unattributable_port_is_not_refused(self):
        # "cannot prove" must never become "wrong": _EDU6_BYID_MARKERS is a
        # guess pending rig gate R1, so an unmatched board revision must still
        # start rather than be told to rescan forever.
        mystery = "/dev/serial/by-id/usb-Mystery_Board_0001-if00"
        self.assertTrue(self._ready(None, mystery, "edu6_studio"))
        self.assertTrue(self._ready(mystery, mystery, "omx_full"))

    def test_a_leftover_wrong_family_leader_does_not_refuse_a_follower_only_rig(self):
        # omx_follower never launches a leader, so judging it would refuse a
        # rig over a record nothing reads.
        self.assertTrue(self._ready(self._EDU6, self._OMX_F, "omx_follower"))

    def test_an_unknown_profile_falls_back_to_omx_and_still_judges(self):
        # ROBOT_PROFILES.get(...) → {} → arm_family "omx", the same default
        # _hardware_ready's leader rule uses. An edu6 arm under that fallback
        # is a genuine conflict.
        self.assertFalse(self._ready(self._OMX_L, self._EDU6, "kein_profil"))
        self.assertTrue(self._ready(self._OMX_L, self._OMX_F, "kein_profil"))


class ArmFamilyMismatchWordingTest(unittest.TestCase):
    """What the student is actually TOLD when the dropdown invalidates a scan.

    „Bitte … scannen" is wrong here and „Fehlende Hardware" is wrong as a
    title: the arms ARE scanned and the hardware is NOT missing. A student
    re-reading a green „Arme erkannt" from a minute ago needs to be told which
    of the two facts changed, or they go looking at cables.
    """

    @staticmethod
    def _module_str_consts(path, names):
        """Module-level string constants, read by AST.

        Not imported: ``gui_app`` pulls in tkinter/webview, and ``pi_agent``
        is not importable from every runner. Not regexed either — the literals
        are implicitly-concatenated across lines inside parens, which is
        exactly where a regex+eval gets the indentation wrong."""
        import ast
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    found[target.id] = ast.literal_eval(node.value)
        return found

    def _consts(self):
        names = ("ARM_FAMILY_MISMATCH_DE", "ARM_FAMILY_MISMATCH_STATUS_DE")
        out = self._module_str_consts(_GUI_SRC, names)
        self.assertEqual(set(out), set(names))
        return out

    def test_the_status_line_fires_only_on_a_family_conflict(self):
        seen = []
        ns = {"ROBOT_PROFILES": ROBOT_PROFILES,
              "DEFAULT_ROBOT_PROFILE": DEFAULT_ROBOT_PROFILE,
              "ARM_FAMILY_MISMATCH_STATUS_DE": self._consts()["ARM_FAMILY_MISMATCH_STATUS_DE"]}
        method = _load_method("_on_robot_type_changed", ns)

        def run(ready, conflict):
            seen.clear()
            owner = types.SimpleNamespace(
                _robot_label_to_id={"EduBotics 6-Achs – Roboter Studio": "edu6_studio"},
                robot_type_var=types.SimpleNamespace(
                    get=lambda: "EduBotics 6-Achs – Roboter Studio"),
                _apply_robot_type_labels=lambda: None,
                cloud_only=types.SimpleNamespace(get=lambda: False),
                _hardware_ready=lambda *a: ready,
                _arms_conflict_with_family=lambda p: conflict,
                _set_status=seen.append,
                _update_start_button=lambda: None,
            )
            method(owner)
            return list(seen)

        self.assertEqual(run(ready=True, conflict=False), ["Bereit — Start klicken."])
        self.assertEqual(run(ready=False, conflict=False),
                         ["Bereit — Hardware scannen, um zu beginnen"])
        self.assertEqual(run(ready=False, conflict=True),
                         [self._consts()["ARM_FAMILY_MISMATCH_STATUS_DE"]])

    def test_the_start_warning_names_the_robot_type_not_missing_hardware(self):
        shown = []
        consts = self._consts()
        ns = {"ROBOT_PROFILES": ROBOT_PROFILES,
              "ARM_FAMILY_MISMATCH_DE": consts["ARM_FAMILY_MISMATCH_DE"],
              "messagebox": types.SimpleNamespace(
                  showwarning=lambda t, m: shown.append((t, m)))}
        method = _load_method("_start_environment", ns)

        def run(profile, conflict):
            shown.clear()
            owner = types.SimpleNamespace(
                _stop_camera_previews=lambda: None,
                cloud_only=types.SimpleNamespace(get=lambda: False),
                hf_token_var=types.SimpleNamespace(get=lambda: ""),
                phone_camera_enabled=types.SimpleNamespace(get=lambda: False),
                _selected_robot_profile=lambda: profile,
                _hardware_ready=lambda *a: False,   # every case here is a refusal
                _arms_conflict_with_family=lambda p: conflict,
            )
            method(owner)
            return list(shown)

        self.assertEqual(run("edu6_studio", conflict=True),
                         [("Falscher Robotertyp", consts["ARM_FAMILY_MISMATCH_DE"])])
        # The two pre-existing wordings are untouched when there is no conflict.
        self.assertEqual(
            run("omx_full", conflict=False),
            [("Fehlende Hardware",
              "Bitte beide Arme scannen und identifizieren, bevor du startest.")])
        self.assertEqual(
            run("edu6_studio", conflict=False),
            [("Fehlende Hardware",
              "Bitte den Follower-Arm scannen und identifizieren, bevor du startest.")])

    def test_the_two_platforms_say_the_same_thing(self):
        """The wizards are twins and a student may meet either."""
        pi_agent_py = os.path.join(os.path.dirname(__file__), "..", "pi_agent",
                                   "agent.py")
        pi = self._module_str_consts(pi_agent_py, ("_ARM_FAMILY_MISMATCH_DE",))
        self.assertIn("_ARM_FAMILY_MISMATCH_DE", pi)
        self.assertEqual(pi["_ARM_FAMILY_MISMATCH_DE"],
                         self._consts()["ARM_FAMILY_MISMATCH_DE"])

    def test_the_wording_is_literal_german_not_transliterated(self):
        # Rule §1: ä/ö/ü/ß, never ae/oe/ue/ss. `german-strings-lint` only scans
        # lines carrying a [FEHLER]/[WARNUNG]/[STOPP] marker, and two of these
        # three surfaces (the status bar, the messagebox) carry none — so
        # nothing in CI would catch a transliteration here.
        texts = dict(self._consts())
        pi_agent_py = os.path.join(os.path.dirname(__file__), "..", "pi_agent",
                                   "agent.py")
        texts.update(self._module_str_consts(pi_agent_py,
                                             ("_ARM_FAMILY_MISMATCH_DE",)))
        self.assertEqual(len(texts), 3)
        for name, text in texts.items():
            with self.subTest(name):
                for bad in ("gehoeren", "gewaehlt", "waehlen", "muessen",
                            "zurueck", "pruefen"):
                    self.assertNotIn(bad, text)
        # The two long sentences are the ones that carry an umlaut word at all
        # (the status line legitimately has none), so pin THOSE literally.
        for name in ("ARM_FAMILY_MISMATCH_DE", "_ARM_FAMILY_MISMATCH_DE"):
            with self.subTest(name):
                self.assertIn("gehören", texts[name])
                self.assertIn("gewählten", texts[name])


if __name__ == "__main__":
    unittest.main()
