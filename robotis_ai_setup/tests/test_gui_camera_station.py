"""The camera-station option in the Windows GUI (plan §3.1, §3.3).

Deps-free: gui_app.py's methods are source-extracted and bound to a small
owner class, so no Tk display, OpenCV or camera is needed (the pattern of
test_gui_robot_type / test_shutdown_teardown). The probe itself, MSMF and the
real cameras are bench items (plan §12.2, B1/B5).
"""

import ast
import importlib.util
import os
import textwrap
import types
import unittest

from gui.app import config_generator, constants
from gui.app.constants import ROBOT_PROFILES
from gui.app.roboter_studio_control import RoboterStudioControlServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_GUI_SRC = os.path.join(_HERE, "..", "gui", "app", "gui_app.py")
_LAYOUT_DATA = os.path.join(
    _HERE, "..", "..", "physical_ai_tools", "physical_ai_server",
    "physical_ai_server", "station", "layout_data.py")


def _source():
    with open(_GUI_SRC, encoding="utf-8") as fh:
        return fh.read()


def _tree():
    return ast.parse(_source())


def _class_methods():
    tree = _tree()
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "EduBoticsApp")
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def _method(name):
    return _class_methods()[name]


def _toplevel(name):
    return next(n for n in _tree().body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _module_str_consts():
    out = {}
    for node in _tree().body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith("STATION_")):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def _owner_class(method_names, ns):
    """A class whose methods are gui_app's own, exec'd against ``ns``."""
    src = _source()
    methods = _class_methods()
    attrs = {}
    for name in method_names:
        node = methods[name]
        snippet = textwrap.dedent(ast.get_source_segment(src, node))
        if node.decorator_list:
            snippet = "\n".join(
                f"@{ast.unparse(d)}" for d in node.decorator_list) + "\n" + snippet
        exec(compile(snippet, _GUI_SRC, "exec"), ns)  # noqa: S102
        attrs[name] = ns[name]
    return type("Owner", (), attrs)


def _with_toplevel(ns, *names):
    src = _source()
    for name in names:
        snippet = ast.get_source_segment(src, _toplevel(name))
        exec(compile(snippet, _GUI_SRC, "exec"), ns)  # noqa: S102
    return ns


class _Var:
    def __init__(self, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Cam:
    def __init__(self, role, win_index, path=None):
        self.role = role
        self.win_index = win_index
        self.path = path or f"Kamera {win_index}"


def _station_ns(native=True, backend="msmf", probe=None, upserts=None,
                shown=None):
    consts = _module_str_consts()
    ns = dict(consts)
    ns.update({
        "ROBOT_PROFILES": ROBOT_PROFILES,
        "ENV_FILE": "/tmp/edubotics-station-test.env",
        "cameras_use_native_bridge": lambda: native,
        "win_camera": types.SimpleNamespace(
            capture_backend_name=lambda: backend,
            probe_station_camera=probe or (lambda index: None),
            StationProbeResult=_ProbeResult),
        "config_generator": types.SimpleNamespace(
            STATION_ENABLED_KEY=config_generator.STATION_ENABLED_KEY,
            upsert_env_var=lambda k, v, p: (upserts.append((k, v))
                                            if upserts is not None else None)),
        "messagebox": types.SimpleNamespace(
            showwarning=lambda t, m: (shown.append((t, m))
                                      if shown is not None else None)),
        "threading": types.SimpleNamespace(Thread=_SyncThread),
        "tk": types.SimpleNamespace(DISABLED="disabled", NORMAL="normal",
                                    X="x"),
        "STATION_OPTION_OFFERED": True,
    })
    return _with_toplevel(ns, "_station_profile_ok", "_station_native_blocker_de")


class _ProbeResult:
    def __init__(self, ok, frames=0, fps=0.0, width=0, height=0, error_de=""):
        self.ok, self.frames, self.fps = ok, frames, fps
        self.width, self.height, self.error_de = width, height, error_de


class _SyncThread:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


_STATION_METHODS = (
    "_station_scene_camera", "_station_scene_key_of",
    "_station_enable_blocker_de", "_set_station_flag", "_clear_station",
    "_station_check_scene_role", "_on_station_toggled", "_station_probe_done",
    "_station_effective_for_start", "_station_stream_off")


def _make_owner(profile="omx_full", cams=(), enabled=False, running=False,
                key=None, **ns_kw):
    logs, statuses, upserts, shown, restarts = [], [], [], [], []
    ns = _station_ns(upserts=upserts, shown=shown, **ns_kw)
    cls = _owner_class(_STATION_METHODS, ns)
    owner = cls()
    owner.hardware = types.SimpleNamespace(cameras=list(cams))
    owner._station_enabled = enabled
    owner._station_scene_key = key
    owner._station_probe_running = False
    owner._rs_robot_type = profile
    owner._rs_control = None
    owner.station_var = _Var(enabled)
    owner.running = running
    owner.camera_bridge = None
    owner._selected_robot_profile = lambda: profile
    owner._log = logs.append
    owner._set_status = statuses.append
    owner._stop_camera_previews = lambda: None
    owner._update_robot_type_combo_state = lambda: None
    owner._restart_camera_previews = lambda: restarts.append(1)
    owner.root = types.SimpleNamespace(after=lambda _d, fn=None, *a: fn(*a))
    rec = types.SimpleNamespace(logs=logs, statuses=statuses, upserts=upserts,
                                shown=shown, restarts=restarts, ns=ns)
    return owner, rec


class TheOptionIsOfferedOnlyWithAReleasedLayout(unittest.TestCase):

    def _released_in_layout_data(self):
        spec = importlib.util.spec_from_file_location("layout_data_t", _LAYOUT_DATA)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return any(lay.get("layout_state") == "released"
                   for lay in mod.LAYOUTS.values())

    def test_the_constant_equals_a_released_layout_shipping(self):
        self.assertEqual(constants.STATION_OPTION_OFFERED,
                         self._released_in_layout_data())

    def test_this_release_ships_it_hidden(self):
        self.assertIs(constants.STATION_OPTION_OFFERED, False)

    def _apply(self, profile, offered):
        rec = []
        packed = {"state": None}
        ns = {"ROBOT_PROFILES": ROBOT_PROFILES,
              "tk": types.SimpleNamespace(X="x", LEFT="left"),
              "STATION_OPTION_OFFERED": offered}
        _with_toplevel(ns, "_station_profile_ok")
        cls = _owner_class(("_apply_robot_type_labels",), ns)
        owner = cls()

        class _W:
            def __init__(self, name):
                self.name = name

            def configure(self, **kw):
                pass

            def pack(self, **kw):
                if self.name == "station_frame":
                    packed["state"] = True

            def pack_forget(self):
                if self.name == "station_frame":
                    packed["state"] = False
        for name in ("leader_frame", "follower_frame", "camera_frame",
                     "camera_hint_label", "btn_scan_camera",
                     "leader_hint_label", "follower_hint_label",
                     "btn_scan_leader", "btn_scan_arm",
                     "follower_status_label", "station_frame"):
            setattr(owner, name, _W(name))
        owner.robot_help_var = _Var("")
        owner._selected_robot_profile = lambda: profile
        owner._apply_robot_type_labels()
        return packed["state"]

    def test_hidden_on_every_profile_while_not_offered(self):
        for profile in ROBOT_PROFILES:
            with self.subTest(profile):
                self.assertIs(self._apply(profile, False), False)

    def test_offered_only_for_the_omx_profiles(self):
        self.assertIs(self._apply("omx_full", True), True)
        self.assertIs(self._apply("omx_follower", True), True)
        self.assertIs(self._apply("v1_studio", True), False)


class EnablingTheStation(unittest.TestCase):

    def test_refused_while_the_environment_runs(self):
        owner, rec = _make_owner(cams=[_Cam("scene", 1)], running=True)
        owner.station_var.set(True)
        owner._on_station_toggled()
        self.assertFalse(owner._station_enabled)
        self.assertFalse(owner.station_var.get())
        self.assertEqual(rec.upserts, [])
        self.assertTrue(any("gestoppter Umgebung" in m for m in rec.logs))

    def test_refused_under_the_usb_cam_rollback(self):
        owner, rec = _make_owner(cams=[_Cam("scene", 1)], native=False,
                                 probe=lambda i: self.fail("probed"))
        owner.station_var.set(True)
        owner._on_station_toggled()
        self.assertFalse(owner._station_enabled)
        self.assertFalse(owner.station_var.get())
        self.assertEqual(rec.upserts, [])
        self.assertIn("EDUBOTICS_CAMERA_SOURCE=usb_cam", rec.shown[0][1])

    def test_refused_under_the_dshow_rollback(self):
        owner, rec = _make_owner(cams=[_Cam("scene", 1)], backend="dshow",
                                 probe=lambda i: self.fail("probed"))
        owner.station_var.set(True)
        owner._on_station_toggled()
        self.assertFalse(owner._station_enabled)
        self.assertIn("EDUBOTICS_CAMERA_BACKEND=dshow", rec.shown[0][1])

    def test_refused_on_a_non_omx_profile(self):
        owner, rec = _make_owner(profile="v1_studio", cams=[_Cam("scene", 1)],
                                 probe=lambda i: self.fail("probed"))
        owner.station_var.set(True)
        owner._on_station_toggled()
        self.assertFalse(owner._station_enabled)
        self.assertIn("OMX", rec.shown[0][1])

    def test_refused_without_a_scene_camera(self):
        owner, rec = _make_owner(cams=[_Cam("gripper", 0)],
                                 probe=lambda i: self.fail("probed"))
        owner.station_var.set(True)
        owner._on_station_toggled()
        self.assertFalse(owner._station_enabled)
        self.assertIn("„Szene“", rec.shown[0][1])

    def test_a_passing_probe_of_the_scene_camera_enables_and_persists(self):
        probed = []
        owner, rec = _make_owner(
            cams=[_Cam("gripper", 0), _Cam("scene", 3)],
            probe=lambda i: (probed.append(i)
                             or _ProbeResult(True, frames=60, fps=30.0)))
        owner.station_var.set(True)
        owner._on_station_toggled()
        self.assertEqual(probed, [3], "the probe opened the scene camera")
        self.assertTrue(owner._station_enabled)
        self.assertTrue(owner.station_var.get())
        self.assertEqual(owner._station_scene_key, (3, "Kamera 3"))
        self.assertEqual(rec.upserts, [("EDUBOTICS_STATION_ENABLED", "1")])
        self.assertEqual(rec.restarts, [1], "previews re-opened after the probe")
        self.assertFalse(owner._station_probe_running)

    def test_a_failing_probe_enables_nothing_and_says_why_in_german(self):
        msg = "Die Kamera auf der Rolle „Szene“ liefert 1280×720 statt 1920×1080"
        owner, rec = _make_owner(
            cams=[_Cam("scene", 1)],
            probe=lambda i: _ProbeResult(False, error_de=msg))
        owner.station_var.set(True)
        owner._on_station_toggled()
        self.assertFalse(owner._station_enabled)
        self.assertFalse(owner.station_var.get())
        self.assertEqual(rec.upserts, [])
        self.assertEqual(rec.shown[0][1], msg)
        self.assertTrue(any(msg in m for m in rec.logs))

    def test_a_role_changed_during_the_probe_is_a_refusal(self):
        owner, rec = None, None

        def _probe(_i):
            owner.hardware.cameras[0].role = "gripper"
            owner.hardware.cameras[1].role = "scene"
            return _ProbeResult(True, frames=60, fps=30.0)
        owner, rec = _make_owner(cams=[_Cam("scene", 0), _Cam("gripper", 1)],
                                 probe=_probe)
        owner.station_var.set(True)
        owner._on_station_toggled()
        self.assertFalse(owner._station_enabled)
        self.assertEqual(rec.upserts, [])

    def test_turning_it_off_persists_zero(self):
        owner, rec = _make_owner(cams=[_Cam("scene", 1)], enabled=True,
                                 key=(1, "Kamera 1"))
        owner.station_var.set(False)
        owner._on_station_toggled()
        self.assertFalse(owner._station_enabled)
        self.assertIsNone(owner._station_scene_key)
        self.assertEqual(rec.upserts, [("EDUBOTICS_STATION_ENABLED", "0")])


class TheFlagIsClearedByARoleOrTypeChange(unittest.TestCase):

    def test_another_camera_on_the_scene_role_clears_it(self):
        cams = [_Cam("gripper", 0), _Cam("scene", 1)]
        owner, rec = _make_owner(cams=cams, enabled=True, key=(1, "Kamera 1"))
        cams[0].role, cams[1].role = "scene", "gripper"
        owner._station_check_scene_role()
        self.assertFalse(owner._station_enabled)
        self.assertEqual(rec.upserts, [("EDUBOTICS_STATION_ENABLED", "0")])
        self.assertTrue(any("andere" in m and "Kamera" in m for m in rec.logs))

    def test_removing_the_scene_camera_clears_it(self):
        cams = [_Cam("scene", 1)]
        owner, _rec = _make_owner(cams=cams, enabled=True, key=(1, "Kamera 1"))
        owner.hardware.cameras = []
        owner._station_check_scene_role()
        self.assertFalse(owner._station_enabled)

    def test_the_same_camera_keeps_it(self):
        cams = [_Cam("gripper", 0), _Cam("scene", 1)]
        owner, rec = _make_owner(cams=cams, enabled=True, key=(1, "Kamera 1"))
        owner._station_check_scene_role()
        self.assertTrue(owner._station_enabled)
        self.assertEqual(rec.upserts, [])

    def test_the_first_role_pick_after_a_gui_restart_is_not_a_change(self):
        cams = [_Cam("scene", 5)]
        owner, _rec = _make_owner(cams=cams, enabled=True, key=None)
        owner._station_check_scene_role()
        self.assertTrue(owner._station_enabled)

    def test_a_non_omx_robot_type_clears_it(self):
        owner, rec = _make_owner(profile="v1_studio", cams=[_Cam("scene", 1)],
                                 enabled=True, key=(1, "Kamera 1"))
        owner._station_check_scene_role()
        self.assertFalse(owner._station_enabled)
        self.assertTrue(any("OMX" in m for m in rec.logs))

    def test_omx_full_to_omx_follower_keeps_it(self):
        owner, _rec = _make_owner(profile="omx_follower",
                                  cams=[_Cam("scene", 1)], enabled=True,
                                  key=(1, "Kamera 1"))
        owner._station_check_scene_role()
        self.assertTrue(owner._station_enabled)

    def test_both_role_writers_ask_for_the_check(self):
        for name in ("_on_cameras_changed", "_on_preview_role_changed"):
            with self.subTest(name):
                body = _method(name).body
                self.assertEqual(ast.unparse(body[-1]),
                                 "self._station_check_scene_role()")
        # The robot-type switch reaches it through _on_cameras_changed.
        code = ast.unparse(_method("_on_robot_type_changed"))
        self.assertIn("self._on_cameras_changed()", code)


class TheFlagAtUmgebungStarten(unittest.TestCase):

    def _start(self, native=True, backend="msmf", cloud=False, cams=None,
               enabled=True, profile="omx_full"):
        cams = [_Cam("scene", 1)] if cams is None else cams
        calls, logs, threads = [], [], []
        ns = _station_ns(native=native, backend=backend)

        class _CaptureThread:
            def __init__(self, target=None, daemon=None):
                threads.append(target)

            def start(self):
                pass
        ns.update({
            "threading": types.SimpleNamespace(Thread=_CaptureThread),
            "config_generator": types.SimpleNamespace(
                generate_env_file=lambda *a, **kw: calls.append(("env", kw)) or "",
                generate_cloud_only_env=lambda *a, **kw: calls.append(("cloud", kw)) or "",
                STATION_ENABLED_KEY="EDUBOTICS_STATION_ENABLED"),
            "wsl_bridge": types.SimpleNamespace(list_serial_devices=lambda: []),
            "docker_manager": types.SimpleNamespace(
                start_containers=lambda gpu=False, log=None: False,
                start_cloud_only=lambda log=None: False),
            "_redact_secret_env_line": lambda line: line,
            "messagebox": types.SimpleNamespace(
                showwarning=lambda *a: self.fail(f"refused: {a}")),
        })
        cls = _owner_class(_STATION_METHODS + ("_start_environment",), ns)
        owner = cls()
        owner.hardware = types.SimpleNamespace(cameras=cams, leader=None,
                                               follower=None)
        owner._station_enabled = enabled
        owner._station_scene_key = None
        owner._station_probe_running = False
        owner._rs_control = None
        owner.station_var = _Var(enabled)
        owner.camera_bridge = None
        owner.gpu_available = False
        owner.running = False
        owner._selected_robot_profile = lambda: profile
        owner.cloud_only = _Var(cloud)
        owner.phone_camera_enabled = _Var(False)
        owner._hardware_ready = lambda *a: True
        owner._arms_conflict_with_family = lambda p: False
        owner._stop_camera_previews = lambda: None
        owner._update_robot_type_combo_state = lambda: None
        owner._update_start_button = lambda: None
        owner._stop_rs_control_server = lambda: None
        owner._set_status = lambda m: None
        owner._log = logs.append
        owner.btn_start = types.SimpleNamespace(config=lambda **kw: None)
        owner.progress = types.SimpleNamespace(start=lambda *a: None,
                                               stop=lambda *a: None)
        owner.root = types.SimpleNamespace(after=lambda _d, fn=None, *a: fn(*a))
        owner._start_environment()
        self.assertEqual(len(threads), 1)
        threads[0]()          # the worker: writes the .env, fails to start
        return owner, calls, logs

    def test_an_enabled_station_on_the_native_bridge_emits_one(self):
        owner, calls, logs = self._start()
        self.assertTrue(owner._rs_station_enabled)
        self.assertEqual(calls, [("env", calls[0][1])])
        self.assertIs(calls[0][1]["station_enabled"], True)
        self.assertFalse(any("Kamerastation ausgeschaltet" in m for m in logs))
        self.assertEqual(owner._station_scene_key, (1, "Kamera 1"),
                         "the start confirms the scene camera")

    def test_a_dshow_rollback_after_enabling_emits_zero_with_one_line(self):
        owner, calls, logs = self._start(backend="dshow")
        self.assertFalse(owner._rs_station_enabled)
        self.assertIs(calls[0][1]["station_enabled"], False)
        lines = [m for m in logs if "Kamerastation ausgeschaltet: " in m]
        self.assertEqual(len(lines), 1, logs)
        self.assertIn("EDUBOTICS_CAMERA_BACKEND=dshow", lines[0])
        self.assertTrue(owner._station_enabled, "the student's choice is kept")

    def test_a_usb_cam_rollback_after_enabling_emits_zero_with_one_line(self):
        owner, calls, logs = self._start(native=False)
        self.assertIs(calls[0][1]["station_enabled"], False)
        lines = [m for m in logs if "Kamerastation ausgeschaltet: " in m]
        self.assertEqual(len(lines), 1)
        self.assertIn("usb_cam", lines[0])

    def test_no_scene_camera_at_start_emits_zero(self):
        owner, calls, logs = self._start(cams=[_Cam("gripper", 0)])
        self.assertIs(calls[0][1]["station_enabled"], False)
        self.assertEqual(
            len([m for m in logs if "Kamerastation ausgeschaltet: " in m]), 1)

    def test_a_disabled_station_emits_zero_silently(self):
        owner, calls, logs = self._start(enabled=False)
        self.assertIs(calls[0][1]["station_enabled"], False)
        self.assertFalse(any("Kamerastation" in m for m in logs))

    def test_cloud_only_passes_the_flag_through(self):
        owner, calls, logs = self._start(cloud=True, cams=[])
        self.assertEqual(calls[0][0], "cloud")
        self.assertIs(calls[0][1]["station_enabled"], True)


class EveryEnvGenerationCarriesTheFlag(unittest.TestCase):
    """A managed key is re-emitted only from the generator's arguments: a call
    site without `station_enabled=` would write 0 (plan §3.1)."""

    def test_every_call_site_passes_station_enabled(self):
        sites = []
        for node in ast.walk(_tree()):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("generate_env_file",
                                           "generate_cloud_only_env")
                    and ast.unparse(node.func.value) == "config_generator"):
                kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
                sites.append((node.lineno, node.func.attr, kw))
        self.assertEqual(len(sites), 4, sites)
        for line, fn, kw in sites:
            with self.subTest(line=line, fn=fn):
                self.assertEqual(kw.get("station_enabled"),
                                 "self._rs_station_enabled")

    def test_the_flag_is_read_back_at_gui_start(self):
        init = ast.unparse(_method("__init__"))
        self.assertIn(
            "config_generator.station_flag_on(config_generator.read_env_var("
            "config_generator.STATION_ENABLED_KEY, ENV_FILE))", init)
        self.assertIn("STATION_OPTION_OFFERED and _saved_station and "
                      "_station_profile_ok(_saved_profile)", init)

    def test_the_bridge_is_built_with_the_run_s_flag(self):
        code = ast.unparse(_method("_start_camera_bridge"))
        self.assertIn("CameraBridge(role_to_index, station=self._rs_station_enabled)",
                      code)


class TheStudioStreamGoesOffOnEveryTeardown(unittest.TestCase):

    def _order(self, fn_node, first, then):
        code = ast.unparse(fn_node)
        self.assertIn(first, code)
        self.assertIn(then, code)
        self.assertLess(code.index(first), code.index(then))

    def test_stop_environment_before_the_bridge_stops(self):
        do_stop = next(n for n in ast.walk(_method("_stop_environment"))
                       if isinstance(n, ast.FunctionDef) and n.name == "_do_stop")
        self.assertEqual(ast.unparse(do_stop.body[3]), "self._station_stream_off()")
        self._order(do_stop, "self._station_stream_off()",
                    "self._stop_camera_bridge()")

    def test_scan_before_the_window_and_the_containers(self):
        do_scan = next(n for n in ast.walk(_method("_scan_arms"))
                       if isinstance(n, ast.FunctionDef) and n.name == "_do_scan")
        self._order(do_scan, "self._station_stream_off()",
                    "webview_window.destroy_all()")
        self._order(do_scan, "self._station_stream_off()",
                    "docker_manager.ensure_environment_stopped(")

    def test_close_after_the_prompt_before_the_teardown(self):
        fn = _method("_on_close")
        self._order(fn, "messagebox.askyesno(", "self._station_stream_off()")
        self._order(fn, "self._station_stream_off()",
                    "self._stop_camera_bridge()")

    def test_start_before_the_new_environment(self):
        self._order(_method("_start_environment"),
                    "self._station_stream_off()", "self.running = True")

    def test_the_window_exit_backstop(self):
        offs = []
        ns = {"docker_manager": types.SimpleNamespace(
                  stop_cloud_inference_after_window_close=lambda *a, **k: None,
                  clear_robot_hf_token_after_window_close=lambda *a, **k: None),
              "webview_window": types.SimpleNamespace(is_current_generation=None)}
        cls = _owner_class(("_on_student_window_closed", "_station_stream_off"), ns)
        owner = cls()
        owner._log = lambda m: None
        owner._rs_control = types.SimpleNamespace(
            station_stream_off=lambda: offs.append(1))
        owner._on_student_window_closed(7)
        self.assertEqual(offs, [1])

    def test_the_backstop_through_the_real_bridge_switches_the_stream_off(self):
        switched = []
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"), get_status=lambda: {},
            get_station_status=lambda: {"enabled": True},
            on_station_stream=lambda on: switched.append(on) or (True, ""))
        srv.handle_station_stream(True)
        cls = _owner_class(("_station_stream_off",), {})
        owner = cls()
        owner._rs_control = srv
        owner._station_stream_off()
        self.assertEqual(switched, [True, False])
        self.assertEqual(srv.handle_station_status()[1]["stream"], "off")

    def test_stream_off_without_a_control_server_is_a_no_op(self):
        cls = _owner_class(("_station_stream_off",), {})
        owner = cls()
        owner._rs_control = None
        owner._station_stream_off()


class TheControlServerOnAStationRig(unittest.TestCase):

    def _start(self, profile, station):
        built = []

        class _FakeServer:
            def __init__(self, **kw):
                built.append(kw)
                self.port = 8769

            def start(self):
                return True
        ns = {"ROBOT_PROFILES": ROBOT_PROFILES,
              "RoboterStudioControlServer": _FakeServer}
        cls = _owner_class(("_start_rs_control_server",), ns)
        owner = cls()
        owner._rs_robot_type = profile
        owner._rs_station_enabled = station
        owner._rs_control = None
        owner._log = lambda m: None
        owner._rs_set_leader_mode = owner._rs_get_status = None
        owner._rs_get_task_busy = None
        owner._rs_get_station_status = owner._rs_set_station_stream = None
        owner._start_rs_control_server(False, False)
        return built

    def test_constructed_on_omx_follower_when_the_station_is_on(self):
        built = self._start("omx_follower", True)
        self.assertEqual(len(built), 1)
        self.assertIn("get_station_status", built[0])
        self.assertIn("on_station_stream", built[0])

    def test_not_constructed_on_omx_follower_without_the_station(self):
        self.assertEqual(self._start("omx_follower", False), [])

    def test_constructed_on_omx_full_as_today(self):
        self.assertEqual(len(self._start("omx_full", False)), 1)

    def _status(self, profile):
        ns = {"ROBOT_PROFILES": ROBOT_PROFILES, "ENV_FILE": "/tmp/x.env",
              "config_generator": types.SimpleNamespace(
                  read_env_var=lambda k, p: "1"),
              "docker_manager": types.SimpleNamespace(
                  get_container_status=lambda: {"open_manipulator": "running"})}
        cls = _owner_class(("_rs_get_status",), ns)
        owner = cls()
        owner._rs_robot_type = profile
        owner._rs_switch_in_flight = False
        st = owner._rs_get_status()
        srv = RoboterStudioControlServer(
            on_set_mode=lambda fo, log: (True, "ok"), get_status=lambda: st)
        return srv.handle_status()[1]

    def test_roboter_studio_status_has_leader_false_on_omx_follower(self):
        self.assertIs(self._status("omx_follower")["has_leader"], False)

    def test_roboter_studio_status_has_leader_true_on_omx_full(self):
        self.assertIs(self._status("omx_full")["has_leader"], True)

    def test_the_stream_switch_without_a_station_bridge(self):
        consts = _module_str_consts()
        cls = _owner_class(("_rs_set_station_stream",), dict(consts))
        owner = cls()
        owner.camera_bridge = None
        self.assertEqual(owner._rs_set_station_stream(False), (True, ""))
        ok, msg = owner._rs_set_station_stream(True)
        self.assertFalse(ok)
        self.assertIn("Stationskamera", msg)
        switched = []
        owner.camera_bridge = types.SimpleNamespace(
            has_station=True, set_station_stream=switched.append)
        self.assertEqual(owner._rs_set_station_stream(True), (True, ""))
        self.assertEqual(switched, [True])

    def test_the_leader_endpoints_on_omx_follower_keep_today_s_refusal(self):
        """The bridge exists on omx_follower now; its leader endpoints answer
        500 there (not 409), as before."""
        ns = {"ROBOT_PROFILES": ROBOT_PROFILES, "ENV_FILE": "/tmp/x.env",
              "config_generator": types.SimpleNamespace(),
              "docker_manager": types.SimpleNamespace()}
        cls = _owner_class(("_rs_set_leader_mode",), ns)
        owner = cls()
        owner._rs_robot_type = "omx_follower"
        srv = RoboterStudioControlServer(on_set_mode=owner._rs_set_leader_mode,
                                         get_status=lambda: {})
        code, body = srv.handle_set_mode(False)
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])


class TheGermanSentences(unittest.TestCase):

    def test_literal_umlauts_and_no_english_marker_words(self):
        consts = _module_str_consts()
        self.assertGreaterEqual(len(consts), 10)
        for name, text in consts.items():
            if not isinstance(text, str):
                continue
            with self.subTest(name):
                for bad in ("oe", "ae", "ue"):
                    for word in ("fuer", "Pruef", "geaendert", "gepruef",
                                 "Groesse", "laesst", "Rueck"):
                        self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
