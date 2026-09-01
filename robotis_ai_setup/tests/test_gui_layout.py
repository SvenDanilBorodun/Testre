"""The helper paragraphs must stay READABLE when the window is small.

`wraplength` is a fixed PIXEL value, and all six grey helper paragraphs asked
for 620 px. Measured at the old `minsize(560, 480)`: the scroll container's
inner width was 501 px while every one of those labels had a `reqwidth` of
594-621 — so 93-120 px of each paragraph was cut off, and the container's ONLY
scrollbar is VERTICAL, so the clipped text could not be reached at all.

The fix is not a bigger number: `_build_scrollable_container`'s canvas
<Configure> handler re-wraps every registered label to the width the container
actually has. `_hint_label` is the registration point and also collapses the
three literals those six labels repeated.

Separately measured and NOT fully fixed: the form is taller than its own default
window — 1148 px of content on `omx_full` and 1030 px on the two leader-less
profiles, in a 700x830 window on this host — so the vertical scroll is
load-bearing ALWAYS, not only on small screens. Reducing the Protokoll height
and the button-row padding reclaimed some of it; the rest is recorded in
docs/KNOWN-ISSUES.md.

The Tk-backed class at the bottom is the only one that proves the re-wrap end
to end. It is OPT-IN (EDUBOTICS_GUI_RENDER_TESTS=1) — see the comment above it
for the measured reason. The classes above it run everywhere and are what
actually fences the mechanism.
"""

import ast
import os
import pathlib
import tempfile
import types
import unittest

_GUI_APP = (pathlib.Path(__file__).resolve().parent.parent
            / "gui" / "app" / "gui_app.py")


def _tree():
    return ast.parse(_GUI_APP.read_text(encoding="utf-8"))


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — this test is stale")


class TheHelperParagraphsReWrap(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = _tree()
        cls.src = _GUI_APP.read_text(encoding="utf-8")

    def test_build_ui_hardcodes_no_wrap_width(self):
        """Every helper paragraph goes through `_hint_label`, so every one of
        them is registered for the re-wrap. A stray inline `wraplength=` is a
        paragraph that silently keeps clipping."""
        build = ast.unparse(_func(self.tree, "_build_ui"))
        self.assertNotIn("wraplength=", build)
        self.assertIn("self._hint_label(", build)

    def test_hint_label_registers_the_label(self):
        fn = ast.unparse(_func(self.tree, "_hint_label"))
        self.assertIn("HINT_WRAP_DEFAULT_PX", fn)
        self.assertIn("self._wrapping_labels.append(", fn)

    def test_the_registry_exists_before_build_ui_runs(self):
        """`_build_scrollable_container` runs INSIDE `_build_ui` and binds the
        handler that reads it."""
        init = _func(self.tree, "__init__")
        stmts = [ast.unparse(s) for s in init.body]
        self.assertIn("self._wrapping_labels = []", stmts)
        self.assertLess(stmts.index("self._wrapping_labels = []"),
                        stmts.index("self._build_ui()"))

    def test_the_canvas_handler_rewraps_via_the_per_label_width(self):
        """The handler re-wraps every registered label, but it no longer decides
        the WIDTH itself: a flat margin is right at one nesting depth and wrong
        at another (the Schritt-D paragraph clipped by 8 px inside its padded
        LabelFrame). `_hint_wrap_width` measures the label's own inset."""
        fn = ast.unparse(_func(self.tree, "_build_scrollable_container"))
        self.assertIn("_wrapping_labels", fn)
        self.assertIn("wraplength=self._hint_wrap_width(", fn)

    def test_the_clamp_lives_in_the_per_label_width(self):
        """Wherever the width is decided, the floor must travel with it — an
        unclamped value is what would reintroduce the clipping at `minsize`."""
        fn = ast.unparse(_func(self.tree, "_hint_wrap_width"))
        self.assertIn("max(HINT_WRAP_MIN_PX", fn)
        self.assertIn("HINT_WRAP_FALLBACK_INSET_PX", fn)

    def test_the_handler_survives_a_destroyed_label(self):
        """The role frame is rebuilt on every camera change; a stale reference
        must be pruned, not raised on."""
        fn = ast.unparse(_func(self.tree, "_build_scrollable_container"))
        self.assertIn("winfo_exists()", fn)
        self.assertIn("except tk.TclError", fn)

    def test_the_wrap_floor_fits_inside_the_minimum_window(self):
        """`HINT_WRAP_MIN_PX` + both insets must be reachable at `minsize`, or
        the clamp reintroduces the clipping it prevents.

        The inset is DERIVED from `HINT_WRAP_FALLBACK_INSET_PX` rather than
        spelled 40, so raising the fallback cannot silently push the floor past
        the minimum window. It is the fallback that is checked because it is the
        WIDEST the floor ever competes with: a measured inset only makes the
        available width smaller, and the `max()` is what protects that case."""
        ns = {}
        for node in self.tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                try:
                    ns[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
        self.assertIn("HINT_WRAP_MIN_PX", ns)
        self.assertIn("HINT_WRAP_FALLBACK_INSET_PX", ns)
        init = ast.unparse(_func(self.tree, "__init__"))
        self.assertIn("self.root.minsize(", init)
        min_w = int(init.split("self.root.minsize(")[1].split(",")[0])
        self.assertLessEqual(
            ns["HINT_WRAP_MIN_PX"] + 2 * ns["HINT_WRAP_FALLBACK_INSET_PX"],
            min_w)


class _FakeLabel:
    def __init__(self, **kw):
        self.kw = kw

    def pack(self, **_kw):
        return self


class _FakeWinfo:
    """A widget that answers `winfo_rootx()`, or raises like a dead one."""

    def __init__(self, rootx, raises=False):
        self._rootx, self._raises = rootx, raises

    def winfo_rootx(self):
        if self._raises:
            raise _TclError("bad window path name")
        return self._rootx


class _TclError(Exception):
    pass


class HintWrapWidthDrivenHeadless(unittest.TestCase):
    """`_hint_wrap_width` — the per-label measurement, run rather than grepped.

    It is a staticmethod over three arguments with no tk calls beyond
    `winfo_rootx`, so it can be exercised directly with fakes."""

    def _fn(self):
        import textwrap
        src = _GUI_APP.read_text(encoding="utf-8")
        marker = "    def _hint_wrap_width("
        start = src.index(marker)
        # Include the decorator line that precedes it.
        head = src.rindex("\n", 0, start) + 1
        start = src.rindex("    @staticmethod", head - 40, start + 1)
        rest = src[start:]
        end = rest.find("\n    def ", rest.index(marker) - start + len(marker))
        snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
        ns = {
            "tk": types.SimpleNamespace(TclError=_TclError),
            "HINT_WRAP_MIN_PX": 220,
            "HINT_WRAP_FALLBACK_INSET_PX": 20,
        }
        exec(compile(snippet, str(_GUI_APP), "exec"), ns)  # noqa: S102
        return ns["_hint_wrap_width"]

    def test_a_measured_inset_is_doubled_for_the_symmetric_right_side(self):
        """The Schritt-D case: a paragraph inside a padded LabelFrame sits 30 px
        in, so it loses 30 px on the right too — 8 px more than the flat margin
        allowed, which is exactly the text that used to be unreachable."""
        fn = self._fn()
        canvas = _FakeWinfo(rootx=100)
        label = _FakeWinfo(rootx=130)
        self.assertEqual(fn(label, canvas, 640), 640 - 60)

    def test_a_shallow_label_keeps_the_wider_wrap(self):
        """Measuring must not punish the paragraphs that were already correct."""
        fn = self._fn()
        self.assertEqual(
            fn(_FakeWinfo(rootx=120), _FakeWinfo(rootx=100), 640), 640 - 40)

    def test_an_unrealized_widget_falls_back_instead_of_collapsing(self):
        """Before the first layout every winfo_* answer is meaningless — a 0 or
        negative inset must not be read as `the label spans the whole canvas`."""
        fn = self._fn()
        canvas = _FakeWinfo(rootx=100)
        for rootx in (100, 0):  # inset 0, inset -100
            self.assertEqual(fn(_FakeWinfo(rootx), canvas, 640), 640 - 40)

    def test_an_implausible_inset_falls_back(self):
        """A third of the window or more means the tree is mid-layout, not that
        the paragraph is genuinely nested that deeply."""
        fn = self._fn()
        self.assertEqual(
            fn(_FakeWinfo(rootx=400), _FakeWinfo(rootx=100), 640), 640 - 40)

    def test_a_dead_widget_falls_back_rather_than_raising(self):
        """The role frame is rebuilt on every camera change; the handler prunes
        stale labels, but this must not be the thing that raises first."""
        fn = self._fn()
        self.assertEqual(
            fn(_FakeWinfo(0, raises=True), _FakeWinfo(rootx=100), 640), 640 - 40)

    def test_the_floor_wins_in_a_very_narrow_window(self):
        fn = self._fn()
        self.assertEqual(
            fn(_FakeWinfo(rootx=130), _FakeWinfo(rootx=100), 200), 220)


class HintLabelDrivenHeadless(unittest.TestCase):
    """The registration, exercised rather than grepped."""

    def _hint_label(self):
        import textwrap
        src = _GUI_APP.read_text(encoding="utf-8")
        marker = "    def _hint_label(self"
        start = src.index(marker)
        rest = src[start:]
        end = rest.find("\n    def ", len(marker))
        snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
        ns = {
            "tk": types.SimpleNamespace(LEFT="left", W="w"),
            "ttk": types.SimpleNamespace(Label=lambda _p, **kw: _FakeLabel(**kw)),
            "HINT_WRAP_DEFAULT_PX": 620,
        }
        exec(compile(snippet, str(_GUI_APP), "exec"), ns)  # noqa: S102
        return ns["_hint_label"]

    def test_every_created_label_lands_in_the_registry(self):
        fn = self._hint_label()
        owner = types.SimpleNamespace(_wrapping_labels=[])
        a = fn(owner, None, "erster Absatz")
        b = fn(owner, None, textvariable="PY_VAR1")
        self.assertEqual(owner._wrapping_labels, [a, b])

    def test_it_carries_the_shared_look_and_the_wrap_default(self):
        """Look comes from the named style now (WP-8), width from the constant."""
        fn = self._hint_label()
        owner = types.SimpleNamespace(_wrapping_labels=[])
        label = fn(owner, None, "Text")
        self.assertEqual(label.kw["style"], "Hint.TLabel")
        self.assertNotIn("foreground", label.kw)
        self.assertNotIn("font", label.kw)
        self.assertEqual(label.kw["wraplength"], 620)
        self.assertEqual(label.kw["text"], "Text")

    def test_a_textvariable_label_carries_no_static_text(self):
        """The Modus paragraph is driven by `robot_help_var`; passing both
        would make tkinter ignore the variable."""
        fn = self._hint_label()
        owner = types.SimpleNamespace(_wrapping_labels=[])
        label = fn(owner, None, textvariable="PY_VAR1")
        self.assertNotIn("text", label.kw)
        self.assertEqual(label.kw["textvariable"], "PY_VAR1")


# OPT-IN, and the reason is measured, not squeamishness. Creating a Tk root,
# destroying it, then creating a SECOND one and calling `update()` on it
# ABORTS the interpreter on this host (Tk 9.0 / macOS, SIGTRAP) — so a probe
# that creates a throwaway root at import time poisons the real one, and a
# create/destroy per test is the same pattern. In CI there is no display at
# all, so the class would only ever skip there. The classes ABOVE are the
# portable fence and run everywhere; this one is the end-to-end proof a
# maintainer can run on a desktop:
#
#     EDUBOTICS_GUI_RENDER_TESTS=1 python3 -m unittest tests.test_gui_layout
#
# It is what produced the two numbers this file documents (the 594-621 vs 501
# clipping, and the Protokoll buttons collapsing to 1x1 px).
_RENDER_TESTS = os.environ.get("EDUBOTICS_GUI_RENDER_TESTS") == "1"


@unittest.skipUnless(_RENDER_TESTS,
                     "set EDUBOTICS_GUI_RENDER_TESTS=1 (needs a display)")
class TheRenderedWindowClipsNothing(unittest.TestCase):
    """End to end, against the REAL app. Fails before the fix, passes after."""

    def _build(self, profile, width, height):
        import tkinter as tk

        tmp = tempfile.mkdtemp(prefix="edubotics-layout-")
        saved = {k: os.environ.get(k)
                 for k in ("EDUBOTICS_ENV_FILE", "LOCALAPPDATA", "PROGRAMDATA")}
        os.environ["EDUBOTICS_ENV_FILE"] = os.path.join(tmp, ".env")
        os.environ["LOCALAPPDATA"] = tmp
        os.environ["PROGRAMDATA"] = tmp
        with open(os.environ["EDUBOTICS_ENV_FILE"], "w", encoding="utf-8") as fh:
            fh.write(f"EDUBOTICS_ROBOT_TYPE={profile}\n")
        try:
            from gui.app import gui_app
            original = gui_app.EduBoticsApp._check_prerequisites
            gui_app.EduBoticsApp._check_prerequisites = lambda self: None
            root = tk.Tk()
            root.geometry(f"{width}x{height}")
            root.update()
            app = gui_app.EduBoticsApp(root)
            root.geometry(f"{width}x{height}")
            root.update()
            root.update_idletasks()
            root.update()
            return root, app, original, saved
        except Exception:  # pragma: no cover — restore then let it surface
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            raise

    def _teardown(self, root, original, saved):
        from gui.app import gui_app
        gui_app.EduBoticsApp._check_prerequisites = original
        root.destroy()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _clipped(self, widget, out):
        try:
            wrap = int(widget.cget("wraplength"))
        except Exception:  # noqa: BLE001 — most widgets have no such option
            wrap = 0
        if wrap and widget.winfo_reqwidth() > widget.winfo_width():
            out.append((widget.winfo_reqwidth(), widget.winfo_width(),
                        (widget.cget("text") or "")[:40]))
        for child in widget.winfo_children():
            self._clipped(child, out)
        return out

    def test_no_paragraph_is_clipped_at_the_minimum_window_size(self):
        for profile in ("omx_full", "edu6_studio"):
            with self.subTest(profile):
                root, _app, original, saved = self._build(profile, 640, 520)
                try:
                    clipped = self._clipped(root, [])
                finally:
                    self._teardown(root, original, saved)
                self.assertEqual(
                    clipped, [],
                    "a helper paragraph is wider than its container and the "
                    "only scrollbar is vertical — that text is unreachable")

    def test_the_wrap_width_tracks_the_window(self):
        widths = {}
        for w in (640, 900):
            root, _app, original, saved = self._build("omx_full", w, 700)
            try:
                found = []

                def walk(widget):
                    try:
                        wrap = int(widget.cget("wraplength"))
                    except Exception:  # noqa: BLE001
                        wrap = 0
                    if wrap:
                        found.append(wrap)
                    for child in widget.winfo_children():
                        walk(child)

                walk(root)
                widths[w] = max(found) if found else 0
            finally:
                self._teardown(root, original, saved)
        self.assertLess(
            widths[640], widths[900],
            "wraplength is still a fixed number — it must follow the "
            "container, which is what makes the text reachable")

    def test_the_protokoll_buttons_are_actually_on_screen(self):
        """Measured regression: packed after the text widget they collapsed to
        1x1 px, because the form overflows and pack squeezes the last child."""
        root, _app, original, saved = self._build("omx_full", 700, 830)
        try:
            found = []

            def walk(widget):
                try:
                    text = widget.cget("text")
                except Exception:  # noqa: BLE001
                    text = ""
                if isinstance(text, str) and text.startswith("Protokoll ") \
                        and widget.winfo_class() == "TButton":
                    found.append((text, widget.winfo_width(),
                                  widget.winfo_height()))
                for child in widget.winfo_children():
                    walk(child)

            walk(root)
        finally:
            self._teardown(root, original, saved)
        self.assertEqual(len(found), 2, found)
        for text, w, h in found:
            self.assertGreater(w, 20, f"{text}: squeezed to {w}x{h} px")
            self.assertGreater(h, 10, f"{text}: squeezed to {w}x{h} px")


if __name__ == "__main__":
    unittest.main()
