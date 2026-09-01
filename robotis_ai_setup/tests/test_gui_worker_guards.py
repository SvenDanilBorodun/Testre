"""Every background worker must return the UI to a usable state, always.

Four `threading.Thread` targets in `gui_app.py` reset their own owning flag,
their spinner and their button as PLAIN STATEMENTS mid-body:

    _scan_arms._do_scan          `_scanning`, progress, „Arme scannen"/„Arm scannen"
    _scan_cameras._do_scan       `_scanning`, progress, „Kameras scannen"
    _stop_environment._do_stop   `self.running`, progress, Start/Stopp
    _factory_reset._do_reset     progress, „Daten zurücksetzen" + Start

Every one of them calls into `device_manager` / `docker_manager` / the camera
bridge, and every one has early `return`s ABOVE its reset. A raise anywhere
above the reset left the flag latched and the button disabled for the rest of
the session, with the progress bar spinning forever — the student's only fix
was restarting the GUI. `_start_environment._do_start` already had exactly this
guard, with the rationale in a comment; the asymmetry was the defect.

THE SHAPE IS LOAD-BEARING, not stylistic. `test_shutdown_teardown` requires the
webview close to be a TOP-LEVEL `ast.Try` of `_do_scan` WITH handlers, and pins
the arm-scan button to exactly ['disabled', 'normal']. So the guard wraps the
thread TARGET and the in-body reset is DELETED rather than duplicated — four
other shapes were measured and all failed one of those two assertions.
"""

import ast
import pathlib
import types
import unittest

from gui.app import constants as _constants  # noqa: F401 — deps-free import probe
from gui.app.constants import ROBOT_PROFILES

_GUI_APP_SRC = (pathlib.Path(__file__).resolve().parent.parent
                / "gui" / "app" / "gui_app.py")

# (outer method, guarded wrapper, raw worker, the flag its `finally` must clear)
_WORKERS = (
    ("_scan_arms", "_do_scan_guarded", "_do_scan", "self._scanning"),
    ("_scan_cameras", "_do_scan_guarded", "_do_scan", "self._scanning"),
    ("_stop_environment", "_do_stop_guarded", "_do_stop", "self.running"),
    ("_factory_reset", "_do_reset_guarded", "_do_reset", None),
)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — this test is stale")


def _nested(fn, name):
    for node in ast.walk(fn):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{fn.name}.{name} not found — this test is stale")


class EveryWorkerThreadIsGuarded(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP_SRC.read_text(encoding="utf-8"))

    def _thread_targets(self, fn):
        """Every `threading.Thread(target=X)` argument name in `fn`."""
        out = []
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "Thread"):
                for kw in node.keywords:
                    if kw.arg == "target" and isinstance(kw.value, ast.Name):
                        out.append(kw.value.id)
        return out

    def test_the_thread_target_is_the_guarded_wrapper(self):
        for outer, wrapper, _raw, _flag in _WORKERS:
            with self.subTest(outer):
                fn = _func(self.tree, outer)
                self.assertEqual(
                    self._thread_targets(fn), [wrapper],
                    f"{outer} spawns its raw worker directly — a raise in it "
                    f"strands the UI for the rest of the session")

    def test_the_wrapper_has_BOTH_a_handler_and_a_finally(self):
        """A handler alone leaves the flag latched; a `finally` alone swallows
        the reason into a silent, already-reset UI."""
        for outer, wrapper, _raw, _flag in _WORKERS:
            with self.subTest(outer):
                fn = _nested(_func(self.tree, outer), wrapper)
                tries = [n for n in fn.body if isinstance(n, ast.Try)]
                self.assertEqual(
                    len(tries), 1,
                    f"{outer}.{wrapper} is not a single top-level try")
                self.assertTrue(tries[0].handlers,
                                f"{outer}.{wrapper} has no except handler")
                self.assertTrue(tries[0].finalbody,
                                f"{outer}.{wrapper} has no finally")

    def test_the_finally_clears_the_owning_flag(self):
        for outer, wrapper, _raw, flag in _WORKERS:
            if flag is None:
                continue
            with self.subTest(outer):
                fn = _nested(_func(self.tree, outer), wrapper)
                final = "\n".join(
                    ast.unparse(s)
                    for t in fn.body if isinstance(t, ast.Try)
                    for s in t.finalbody)
                self.assertIn(
                    f"{flag} = False", final,
                    f"{outer}.{wrapper}'s finally does not clear {flag}")

    def test_the_finally_always_stops_the_spinner(self):
        """One `ttk.Progressbar`, no refcount — a worker that dies mid-flight
        leaves an indeterminate trough animating with nothing running."""
        for outer, wrapper, _raw, _flag in _WORKERS:
            with self.subTest(outer):
                fn = _nested(_func(self.tree, outer), wrapper)
                final = "\n".join(
                    ast.unparse(s)
                    for t in fn.body if isinstance(t, ast.Try)
                    for s in t.finalbody)
                self.assertIn("progress.stop()", final)

    def test_the_reset_is_MOVED_not_DUPLICATED(self):
        """The arm scan's button must transition exactly once each way.

        `test_shutdown_teardown.test_confirming_scans_byte_for_byte_as_it_did_
        before` pins ['disabled', 'normal']; a `finally` that re-enables the
        button IN ADDITION to an in-body call produces a third transition and
        was one of the four shapes that failed.
        """
        raw = _nested(_func(self.tree, "_scan_arms"), "_do_scan")
        code = ast.unparse(raw)
        self.assertNotIn("self._scanning = False", code)
        self.assertNotIn("progress.stop()", code)
        self.assertNotIn("btn_scan_leader.config(state=tk.NORMAL)", code)

    def test_the_camera_button_reset_left_the_ui_closure(self):
        """It lived in `_update_checkbuttons`, which runs only if `_do_scan`
        got far enough to schedule it — i.e. never on the failure path."""
        raw = _nested(_func(self.tree, "_scan_cameras"), "_do_scan")
        self.assertNotIn("btn_scan_camera.config(state=tk.NORMAL)",
                         ast.unparse(raw))

    def test_the_webview_close_is_STILL_a_top_level_try_of_do_scan(self):
        """The reason four other shapes failed, asserted here too so this file
        fails FIRST and says why, instead of leaving it to a teardown test."""
        raw = _nested(_func(self.tree, "_scan_arms"), "_do_scan")
        self.assertIn(
            "try:\n    webview_window.destroy_all()\nexcept Exception:\n    pass",
            {ast.unparse(stmt) for stmt in raw.body},
            "the webview close is no longer at `_do_scan`'s own top level — "
            "wrapping the BODY in a try is the shape that breaks it")


class ARaisingScanDoesNotStrandTheButton(unittest.TestCase):
    """Behavioural, against the shipped `_scan_arms` and a raising scanner."""

    def _drive(self, scan_raises):
        import textwrap

        src = _GUI_APP_SRC.read_text(encoding="utf-8")
        marker = "    def _scan_arms(self"
        start = src.index(marker)
        rest = src[start:]
        end = rest.find("\n    def ", len(marker))
        snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])

        rec = types.SimpleNamespace(button=[], button_arm=[], logs=[],
                                    statuses=[], stopped=0)

        class _SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                if self._target is not None:
                    self._target()

        def _boom(image, arm_family="omx"):
            if scan_raises:
                raise RuntimeError("usbipd ist abgestürzt")
            return None, types.SimpleNamespace(
                description="OpenRB-150", serial_path="/dev/serial/by-id/f")

        ns = {
            "threading": types.SimpleNamespace(Thread=_SyncThread),
            "docker_manager": types.SimpleNamespace(
                ensure_environment_stopped=lambda log=None: False),
            "device_manager": types.SimpleNamespace(
                scan_and_identify_arms=_boom,
                diagnose_usb_environment=lambda **kw: types.SimpleNamespace(
                    message_de="x", details=""),
                get_diagnostics_log_path=lambda: "/tmp/d.log"),
            "IMAGE_OPEN_MANIPULATOR": "img",
            "ROBOT_PROFILES": ROBOT_PROFILES,
            "tk": types.SimpleNamespace(DISABLED="disabled", NORMAL="normal"),
        }
        exec(compile(snippet, str(_GUI_APP_SRC), "exec"), ns)  # noqa: S102
        method = ns["_scan_arms"]

        owner = types.SimpleNamespace(
            _scanning=False,
            _scan_confirm_open=False,
            _confirm_arm_scan_closes_window=lambda: True,
            btn_scan_leader=types.SimpleNamespace(
                config=lambda **kw: rec.button.append(kw.get("state"))),
            btn_scan_arm=types.SimpleNamespace(
                config=lambda **kw: rec.button_arm.append(kw.get("state"))),
            btn_stop=types.SimpleNamespace(config=lambda **kw: None),
            btn_open_browser=types.SimpleNamespace(config=lambda **kw: None),
            _selected_robot_profile=lambda: "edu6_studio",
            root=types.SimpleNamespace(
                after=lambda _d, fn=None, *a: fn(*a) if fn is not None else None),
            progress=types.SimpleNamespace(
                start=lambda *a: None,
                stop=lambda *a: setattr(rec, "stopped", rec.stopped + 1)),
            hardware=types.SimpleNamespace(leader=None, follower=None),
            leader_status_var=types.SimpleNamespace(set=lambda *a: None),
            follower_status_var=types.SimpleNamespace(set=lambda *a: None),
            _set_status=rec.statuses.append,
            _log=rec.logs.append,
            _clear_arm_repair=lambda: None,
            _stop_camera_bridge=lambda: None,
            _stop_rs_control_server=lambda: None,
            _update_start_button=lambda: None,
            _show_arm_repair=lambda *a, **k: None,
            running=False,
        )
        method(owner)
        return owner, rec

    def test_a_raising_scanner_leaves_the_button_usable(self):
        owner, rec = self._drive(scan_raises=True)
        self.assertFalse(
            owner._scanning,
            "`_scanning` stayed latched — every later scan (arms AND cameras) "
            "returns immediately for the rest of the session")
        self.assertEqual(rec.button, ["disabled", "normal"])
        self.assertEqual(rec.button_arm, ["disabled", "normal"])
        self.assertEqual(rec.stopped, 1, "the progress bar spins forever")

    def test_the_failure_is_reported_in_german_and_names_the_protokoll(self):
        _owner, rec = self._drive(scan_raises=True)
        self.assertTrue(any(ln.startswith("[FEHLER] ") for ln in rec.logs),
                        rec.logs)
        self.assertTrue(any("abgebrochen" in ln for ln in rec.logs), rec.logs)
        self.assertTrue(any("Protokoll" in s for s in rec.statuses),
                        rec.statuses)
        # Rule §1 — these [FEHLER] lines ARE in `german-strings-lint`'s grep
        # scope, so a transliteration here would fail CI. Assert it locally too:
        # the status bar carries no marker and is invisible to that grep.
        for text in rec.logs + rec.statuses:
            for bad in ("Protokol ", "fehlgeschlagem", "abgebrochem",
                        "Pruefung", "pruefen", "uebersprungen"):
                self.assertNotIn(bad, text)

    def test_a_healthy_scan_is_completely_unchanged(self):
        owner, rec = self._drive(scan_raises=False)
        self.assertFalse(owner._scanning)
        self.assertEqual(rec.button, ["disabled", "normal"])
        self.assertEqual(rec.stopped, 1)
        self.assertFalse([ln for ln in rec.logs if ln.startswith("[FEHLER]")])
        self.assertTrue(any("Roboterarm gefunden" in s for s in rec.statuses))


class TheIgnoredCameraScanClickSaysSomething(unittest.TestCase):
    """`_scanning` is shared, but only the ARM button is disabled by an arm
    scan. Clicking „Kameras scannen" during one returned in total silence."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP_SRC.read_text(encoding="utf-8"))

    def test_the_busy_return_writes_a_status(self):
        fn = _func(self.tree, "_scan_cameras")
        guard = None
        for stmt in fn.body:
            if isinstance(stmt, ast.If) and "_scanning" in ast.unparse(stmt.test):
                guard = stmt
                break
        self.assertIsNotNone(guard, "_scan_cameras has no `_scanning` guard")
        body = ast.unparse(guard)
        self.assertIn("_set_status", body,
                      "the busy click is still silent — the button looks alive "
                      "and does nothing")
        self.assertIn("läuft bereits", body)

    def test_the_ARM_scan_guard_stays_silent(self):
        """`test_declining_does_ABSOLUTELY_NOTHING` requires it: the decline
        path shares this clause with `_scan_confirm_open`, so a status here
        would be written by a click the student took back."""
        fn = _func(self.tree, "_scan_arms")
        for stmt in fn.body:
            if isinstance(stmt, ast.If) and "_scan_confirm_open" in ast.unparse(stmt.test):
                self.assertNotIn("_set_status", ast.unparse(stmt))
                self.assertNotIn("_log", ast.unparse(stmt))
                return
        self.fail("_scan_arms' re-entrancy guard is gone — this test is stale")

    def test_the_camera_scan_shows_the_progress_bar_like_the_arm_scan(self):
        fn = _nested(_func(self.tree, "_scan_cameras"), "_do_scan")
        self.assertIn("progress.start", ast.unparse(fn),
                      "a camera scan still gives no motion cue")


if __name__ == "__main__":
    unittest.main()
