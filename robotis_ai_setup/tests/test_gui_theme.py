"""One visual system, and the two traps that come with it.

`grep -rn "ttk.Style\\|Style()" gui/` used to find NOTHING. Fonts were inline
literals in three shapes — `("Segoe UI", 18, "bold")`, `("Consolas", 9)` and
`("", 9, "bold")` with an EMPTY family — every helper paragraph repeated
`foreground="gray"`, and „Daten zurücksetzen", which irreversibly deletes the
student's datasets, models and the Roboter-Studio calibration, was a plain
`ttk.Button` indistinguishable from „Umgebung starten" except by
`pack(side=tk.RIGHT)`. `installer/robotis_ai_setup.iss` meanwhile sets
`WizardStyle=modern`: the installer was styled and the app it installs was not.

TRAP 1 — THE WINDOW TITLE IS A LOAD-BEARING IDENTIFIER (OD-4). `run()` focuses
an already-running instance with `user32.FindWindowW(None, "EduBotics")`, an
EXACT match, and `webview_window.py::run_in_process` gives the WebView2 CHILD
the same title (which is why `destroy_all` matches by PID, never by title). So
`root.title("EduBotics 2.15.0")` would silently break single-instance focus —
the second launch exits WITHOUT raising the first window — and a naive prefix
match could focus the child instead. The version therefore goes in a small grey
label under the heading, and `test_the_title_and_the_focus_lookup_agree` is the
lockstep that would have caught the change.

TRAP 2 — DPI (OD-5). This code sets `tk scaling` and NEVER makes the process
DPI-aware. `gui/build.spec`'s `EXE(...)` declares no `manifest=` and CI installs
`pyinstaller` unpinned, so the shipped .exe's awareness is whatever the latest
bootloader emits on release day — which is exactly why it is PROBED. On a
NON-aware process Windows already bitmap-stretches the window, so scaling Tk on
top of that enlarges the text a second time inside an already-stretched frame,
i.e. strictly worse than doing nothing.
"""

import ast
import pathlib
import unittest

_APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "gui" / "app"
_GUI_APP = _APP_DIR / "gui_app.py"
_THEME = _APP_DIR / "theme.py"


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — this test is stale")


class TheThemeExistsAndIsUsed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = _GUI_APP.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def test_the_theme_module_exports_what_the_gui_names(self):
        from gui.app import theme
        for name in ("apply_theme", "apply_scaling", "process_is_dpi_aware",
                     "step_style_for", "FONT_FAMILY", "LOG_FONT", "BADGE_FONT"):
            self.assertTrue(hasattr(theme, name), name)

    def test_the_theme_is_installed_before_the_ui_that_names_its_styles(self):
        init = _func(self.tree, "__init__")
        stmts = [ast.unparse(s) for s in init.body]
        self.assertIn("theme_mod.apply_theme(self.root)", stmts)
        self.assertLess(stmts.index("theme_mod.apply_theme(self.root)"),
                        stmts.index("self._build_ui()"))

    def test_the_main_window_names_styles_instead_of_font_literals(self):
        build = ast.unparse(_func(self.tree, "_build_ui"))
        self.assertNotIn("'Segoe UI'", build)
        self.assertNotIn("('', 9, 'bold')", build)
        for style in ("Title.TLabel", "Status.TLabel", "Primary.TButton",
                      "Danger.TButton"):
            self.assertIn(style, build)

    def test_the_font_FAMILY_has_exactly_one_source(self):
        """The modal dialogs keep their own SIZES (a URL is deliberately large)
        but must not each spell the family."""
        self.assertNotIn('font=("Segoe UI"', self.src)
        self.assertNotIn('font=("Consolas"', self.src)
        self.assertIn("theme_mod.FONT_FAMILY", self.src)

    def test_the_destructive_button_is_visually_distinct(self):
        """„Daten zurücksetzen" removes the datasets, the models and the
        Roboter-Studio calibration. Colour is not the only affordance — the
        right-hand placement and the confirmation dialog stay — but it must not
        look like the primary action either."""
        build = ast.unparse(_func(self.tree, "_build_ui"))
        reset = build.index("Daten zurücksetzen")
        window = build[reset - 400:reset + 400]
        self.assertIn("Danger.TButton", window)
        self.assertIn("side=tk.RIGHT", window)


class StepStateIsVisible(unittest.TestCase):
    """Schritt A-D said nothing about being done, pending or failed."""

    def test_the_style_is_derived_from_the_status_TEXT(self):
        from gui.app import theme
        self.assertEqual(theme.step_style_for("Gefunden: OpenRB-150 (/dev/x)"),
                         theme.STEP_OK)
        self.assertEqual(theme.step_style_for("Wiederhergestellt: OpenRB-150"),
                         theme.STEP_OK)
        self.assertEqual(theme.step_style_for("✓ Token gespeichert"),
                         theme.STEP_OK)
        self.assertEqual(theme.step_style_for("Nicht gefunden"),
                         theme.STEP_ERROR)
        self.assertEqual(theme.step_style_for("Nicht gescannt"),
                         theme.STEP_PENDING)
        self.assertEqual(theme.step_style_for("Kein Token gespeichert"),
                         theme.STEP_PENDING)

    def test_a_leaderless_profile_reads_as_PENDING_not_as_an_ERROR(self):
        """„Für diesen Robotertyp nicht nötig" is the correct outcome of a
        successful scan — painting it red would undo WP-1."""
        from gui.app import theme
        self.assertEqual(
            theme.step_style_for("Für diesen Robotertyp nicht nötig"),
            theme.STEP_PENDING)

    def test_an_unrecognised_line_is_never_guessed_green_or_red(self):
        from gui.app import theme
        for text in ("", None, "irgendwas", "Prüfe letzte Arm-Konfiguration"):
            with self.subTest(text):
                self.assertEqual(theme.step_style_for(text),
                                 theme.STEP_PENDING)

    def test_it_is_wired_as_a_TRACE_and_not_at_the_write_sites(self):
        """`_scan_arms._do_scan` and `_try_rehydrate_arms` are source-extracted
        against hand-built owners, so a new `self.<attr>` in either would be an
        AttributeError with nothing about the scan changed."""
        tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        binder = ast.unparse(_func(tree, "_bind_step_state"))
        self.assertIn("trace_add('write'", binder.replace('"', "'"))
        for method in ("_scan_arms", "_try_rehydrate_arms"):
            with self.subTest(method):
                self.assertNotIn("_bind_step_state",
                                 ast.unparse(_func(tree, method)))

    def test_all_three_step_vars_are_bound(self):
        build = ast.unparse(_func(ast.parse(
            _GUI_APP.read_text(encoding="utf-8")), "_build_ui"))
        for var in ("leader_status_var", "follower_status_var",
                    "hf_token_status_var"):
            with self.subTest(var):
                self.assertIn(f"self._bind_step_state(self.{var}", build)


class TheWindowTitleIsAnIdentifier(unittest.TestCase):
    """OD-4's lockstep — the one that would have caught the trap."""

    @classmethod
    def setUpClass(cls):
        cls.src = _GUI_APP.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def _title_argument(self):
        for node in ast.walk(_func(self.tree, "__init__")):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "title"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                return node.args[0].value
        self.fail("__init__ no longer sets a literal window title — if the "
                  "title became dynamic, `_focus_existing_window`'s exact "
                  "FindWindowW match must change in the SAME commit")

    def _findwindow_argument(self):
        for node in ast.walk(_func(self.tree, "_focus_existing_window")):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "FindWindowW"
                    and len(node.args) == 2
                    and isinstance(node.args[1], ast.Constant)):
                return node.args[1].value
        self.fail("_focus_existing_window no longer looks the window up by a "
                  "literal title — this test is stale")

    def test_the_title_and_the_focus_lookup_agree(self):
        self.assertEqual(
            self._title_argument(), self._findwindow_argument(),
            "the window title and the single-instance FindWindowW lookup have "
            "drifted apart — a second launch would exit WITHOUT raising the "
            "first window, silently")

    def test_the_version_is_on_screen_without_touching_the_title(self):
        self.assertEqual(self._title_argument(), "EduBotics")
        build = ast.unparse(_func(self.tree, "_build_ui"))
        self.assertIn("Version {APP_VERSION}", build)

    def test_the_webview_child_shares_the_title_so_a_prefix_match_is_unsafe(self):
        """Recorded because it is the reason option 2 was refused: the child
        window is titled „EduBotics" too, so an EnumWindows prefix match could
        focus the student's browser window instead of the wizard."""
        child = (_APP_DIR / "webview_window.py").read_text(encoding="utf-8")
        self.assertIn('title="EduBotics"', child)


class DpiIsProbedAndNeverForced(unittest.TestCase):

    def test_scaling_is_a_no_op_on_a_non_aware_process(self):
        from gui.app import theme
        self.assertFalse(theme.process_is_dpi_aware(),
                         "this host is not Windows, so awareness must be False")
        self.assertIsNone(theme.apply_scaling(object()),
                          "apply_scaling touched a non-aware process — on one "
                          "Windows already bitmap-stretches, scaling Tk on top "
                          "enlarges the text a second time")

    def test_the_process_is_never_MADE_aware(self):
        """OD-5: tk scaling only. Making the process aware changes every pixel
        for every student and needs a measurement on real Windows at
        100/125/150 % first."""
        src = _THEME.read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        # Strip docstrings, which name the APIs they explain.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.Module, ast.ClassDef)):
                doc = ast.get_docstring(node)
                if doc:
                    code = code.replace(doc, "")
        self.assertNotIn("SetProcessDpiAwareness", code)
        self.assertNotIn("SetProcessDPIAware", code)
        self.assertNotIn("SetProcessDpiAwarenessContext", code)

    def test_no_manifest_was_added_to_the_pyinstaller_spec(self):
        """OD-5 explicitly keeps this out of scope; the probe exists precisely
        because the spec declares nothing."""
        spec = (_APP_DIR.parent / "build.spec").read_text(encoding="utf-8")
        self.assertNotIn("manifest=", spec)

    def test_run_applies_scaling_after_the_root_exists(self):
        tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        run = ast.unparse(_func(tree, "run"))
        self.assertIn("theme_mod.apply_scaling(root)", run)
        self.assertLess(run.index("root = tk.Tk()"),
                        run.index("theme_mod.apply_scaling(root)"))
        self.assertLess(run.index("theme_mod.apply_scaling(root)"),
                        run.index("EduBoticsApp(root)"))


if __name__ == "__main__":
    unittest.main()
