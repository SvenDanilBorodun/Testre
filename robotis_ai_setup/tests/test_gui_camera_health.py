"""A camera that dies mid-lesson must not be silent in the GUI.

`CameraBridge.status()`'s own docstring says „Per-camera health for the GUI
status line" — and it had ZERO production callers. `gui_app.py` touched
`self.camera_bridge` at exactly five sites (construct,
`register_external_source`, `start`, the preview-contention check, `stop`) and
never asked it anything. So these five authored German diagnostics were
computed on every capture tick and displayed NOWHERE:

    Kamera '<role>' liefert keine Bilder — wird neu geöffnet (Index N).
    Warte auf Kamera-Verbindung zum Container (127.0.0.1:5557)…
    Kamera-Verbindung verloren: <exc>
    Handy-Kamera-Verbindung verloren: <exc>
    Kamera '<role>': Treiber nutzt <fourcc> statt MJPG — niedrige Bildrate …

The student met a dead camera as a missing image in the browser, minutes later.

This is NEW RUNTIME BEHAVIOUR — a periodic loop in a GUI whose container
lifecycle ownership is a documented invariant — so it is deliberately the
smallest thing that closes the gap: read `status()`, log on a CHANGE, cancel
with the bridge. It opens no device, touches no container and restarts nothing
(the bridge already re-opens a dead camera itself).
"""

import ast
import pathlib
import textwrap
import types
import unittest

_GUI_APP = (pathlib.Path(__file__).resolve().parent.parent
            / "gui" / "app" / "gui_app.py")


def _load_method(name, ns):
    src = _GUI_APP.read_text(encoding="utf-8")
    marker = f"    def {name}(self"
    start = src.index(marker)
    rest = src[start:]
    end = rest.find("\n    def ", len(marker))
    snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
    exec(compile(snippet, str(_GUI_APP), "exec"), ns)  # noqa: S102
    return ns[name]


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — this test is stale")


class _Rig:
    """An owner whose bridge answers a scripted sequence of `status()` dicts."""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.logs = []
        self.scheduled = []
        self.status_calls = 0
        owner = types.SimpleNamespace(
            camera_bridge=self,
            _log=self.logs.append,
            _camera_health_after_id=None,
            _camera_health_last={},
            # Passed to `root.after` as the re-arm callback; never invoked here
            # (the rig drives the ticks itself, one call at a time).
            _poll_camera_health=None,
            root=types.SimpleNamespace(
                after=lambda _ms, fn=None: self.scheduled.append(fn) or "after#1",
                after_cancel=lambda _id: None),
        )
        self.owner = owner

    # the CameraBridge surface this feature is allowed to touch
    def status(self):
        self.status_calls += 1
        if not self.sequence:
            return {"cameras": {}}
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _cams(**errors):
    return {"cameras": {role: {"error": msg} for role, msg in errors.items()}}


class ItLogsOncePerTransition(unittest.TestCase):

    def _tick(self, rig, times):
        poll = _load_method("_poll_camera_health", {
            "CAMERA_HEALTH_POLL_MS": 3000,
        })
        for _ in range(times):
            poll(rig.owner)
        return rig.logs

    def test_a_healthy_bridge_says_nothing_at_all(self):
        rig = _Rig([_cams(gripper="", scene="")] * 5)
        self.assertEqual(self._tick(rig, 5), [])

    def test_a_failure_is_reported_exactly_ONCE_while_it_persists(self):
        broken = "Kamera 'scene' liefert keine Bilder — wird neu geöffnet (Index 1)."
        rig = _Rig([_cams(scene=broken)] * 4)
        logs = self._tick(rig, 4)
        self.assertEqual(len(logs), 1,
                         f"the broken camera was logged {len(logs)} times — "
                         f"a 3 s loop would flood the Protokoll")
        self.assertIn(broken, logs[0])
        self.assertTrue(logs[0].startswith("[WARNUNG] "))

    def test_recovery_is_reported_too(self):
        broken = "Kamera 'scene' liefert keine Bilder — wird neu geöffnet (Index 1)."
        rig = _Rig([_cams(scene=broken), _cams(scene=broken), _cams(scene="")])
        logs = self._tick(rig, 3)
        self.assertEqual(len(logs), 2, logs)
        self.assertTrue(logs[1].startswith("[OK] "))
        self.assertIn("scene", logs[1])

    def test_a_DIFFERENT_error_on_the_same_camera_is_a_new_transition(self):
        rig = _Rig([_cams(scene="A liefert keine Bilder."),
                    _cams(scene="B Treiber nutzt YUY2 statt MJPG.")])
        logs = self._tick(rig, 2)
        self.assertEqual(len(logs), 2, logs)

    def test_each_camera_is_tracked_SEPARATELY(self):
        rig = _Rig([_cams(gripper="G kaputt", scene=""),
                    _cams(gripper="G kaputt", scene="S kaputt")])
        logs = self._tick(rig, 2)
        self.assertEqual(len(logs), 2, logs)
        self.assertIn("G kaputt", logs[0])
        self.assertIn("S kaputt", logs[1])


class ItIsReadOnlyAndCannotOutliveTheBridge(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))

    def test_a_missing_bridge_is_a_silent_no_op(self):
        rig = _Rig([])
        rig.owner.camera_bridge = None
        poll = _load_method("_poll_camera_health", {"CAMERA_HEALTH_POLL_MS": 3000})
        poll(rig.owner)
        self.assertEqual(rig.logs, [])
        self.assertEqual(rig.scheduled, [],
                         "the loop re-armed itself with no bridge to watch")

    def test_a_raising_status_ENDS_the_loop_instead_of_repeating(self):
        rig = _Rig([RuntimeError("bridge weg")])
        poll = _load_method("_poll_camera_health", {"CAMERA_HEALTH_POLL_MS": 3000})
        poll(rig.owner)
        self.assertEqual(len(rig.logs), 1)
        self.assertTrue(rig.logs[0].startswith("[WARNUNG] "))
        self.assertEqual(rig.scheduled, [],
                         "a broken bridge would log the same line every 3 s")

    def test_a_healthy_tick_re_arms_the_loop(self):
        rig = _Rig([_cams(scene="")])
        poll = _load_method("_poll_camera_health", {"CAMERA_HEALTH_POLL_MS": 3000})
        poll(rig.owner)
        self.assertEqual(len(rig.scheduled), 1)

    def test_it_touches_NOTHING_but_status(self):
        """The whole safety argument: no device, no container, no restart."""
        node = _func(self.tree, "_poll_camera_health")
        # Drop the docstring NODE, not its text: it explains the rule by naming
        # exactly the things the code must not do, so scanning it would fail on
        # the explanation instead of on the code. (`ast.get_docstring`
        # cleandoc()s, so a string replace on the unparsed source misses.)
        body = list(node.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        fn = "\n".join(ast.unparse(stmt) for stmt in body)
        for forbidden in ("docker_manager", "win_camera", "cv2", "subprocess",
                          "_start_camera_bridge", "_stop_camera_bridge",
                          "restart", "open_capture", ".start()", ".stop()"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, fn)
        self.assertIn("bridge.status()", fn)

    def test_the_bridge_teardown_cancels_the_loop(self):
        stop = ast.unparse(_func(self.tree, "_stop_camera_bridge"))
        self.assertIn("_stop_camera_health_poll()", stop)
        cancel = ast.unparse(_func(self.tree, "_stop_camera_health_poll"))
        self.assertIn("after_cancel", cancel)

    def test_the_teardown_is_safe_before_the_loop_ever_started(self):
        cancel = _load_method("_stop_camera_health_poll", {})
        owner = types.SimpleNamespace(
            root=types.SimpleNamespace(after_cancel=lambda _i: None))
        cancel(owner)  # no attributes set at all
        self.assertIsNone(owner._camera_health_after_id)
        self.assertEqual(owner._camera_health_last, {})

    def test_the_loop_is_armed_where_the_bridge_is_started(self):
        start = ast.unparse(_func(self.tree, "_start_camera_bridge"))
        self.assertIn("_poll_camera_health", start)
        self.assertIn("CAMERA_HEALTH_POLL_MS", start)

    def test_the_interval_is_a_named_constant_and_not_hot(self):
        src = _GUI_APP.read_text(encoding="utf-8")
        self.assertIn("CAMERA_HEALTH_POLL_MS = ", src)
        value = int(src.split("CAMERA_HEALTH_POLL_MS = ")[1].split("\n")[0])
        self.assertGreaterEqual(value, 1000,
                                "a sub-second poll on the Tk main thread")


if __name__ == "__main__":
    unittest.main()
