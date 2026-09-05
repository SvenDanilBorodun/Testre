"""ONE severity vocabulary in the Protokoll, and a pane you can get text out of.

Counted over `gui/app/*.py` + `gui/main.py` before this landed: `[FEHLER]`×1,
`[WARNUNG]`×5, `FEHLER:`×7, `WARNUNG:`×9, `ACHTUNG:`×1, `Hinweis:`×5, `Tipp:`×4,
`⚠️`×1 — eight conventions in one pane. The canonical set already SHIPPED and
already landed in that same pane: `installer/scripts/preflight_system.ps1::Emit`
takes `[ValidateSet('OK','WARNUNG','FEHLER','INFO')]` and writes `"[$Level]
$Message"`, and `_run_preflight_diagnostics` echoes those lines straight
through. So the student was already reading `[OK]`/`[INFO]` interleaved with
`WARNUNG:`/`ACHTUNG:`/`Tipp:`/`⚠️`. Adopting the PowerShell set is convergence.

DELIBERATE DEVIATION from the plan, recorded here because it reverses a
recommendation: severity stays an INLINE MARKER and `_log` gained no `level=`
parameter. Five methods that call `self._log` are source-extracted by this
suite against a bare `list.append` double (`_scan_arms._do_scan`,
`_try_rehydrate_arms`, `_bind_hf_token`, `_run_prerequisite_checks_body`,
`_prompt_finalize_install`), so a keyword argument in any of them is a
TypeError waiting for the next person who adds a log line — and `_log` would
have had two call conventions, one per method, decided by whether a test
happens to extract it. `_tag_for_line` recovers the severity from the text
instead, which also colours the PowerShell-produced lines that no parameter
could reach.

Three assertions in `test_gui_install_lifecycle` constrain this and must keep
passing untouched: `_bind_hf_token`'s foreign-token line MUST contain the
literal „[WARNUNG]", its legacy-adoption line MUST NOT, and the ordinary path
must log NOTHING at all.
"""

import ast
import pathlib
import re
import unittest

_GUI_DIR = pathlib.Path(__file__).resolve().parent.parent / "gui"
_GUI_APP = _GUI_DIR / "app" / "gui_app.py"
_PREFLIGHT_PS1 = (pathlib.Path(__file__).resolve().parent.parent
                  / "installer" / "scripts" / "preflight_system.ps1")

# The bare forms this package replaced. A student-visible string starting with
# any of them is a ninth convention creeping back in.
_BARE_FORMS = ("FEHLER:", "WARNUNG:", "ACHTUNG:", "Hinweis:", "Tipp:", "⚠")

_MARKER_RE = re.compile(r"\[([A-ZÄÖÜ]+)\]")


def _python_sources():
    files = sorted((_GUI_DIR / "app").glob("*.py"))
    files.append(_GUI_DIR / "main.py")
    return [p for p in files if p.is_file()]


def _string_constants(path):
    """Every string literal in the module, with its line number."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
    return out


def _docstring_lines(path):
    """Line numbers occupied by docstrings — English prose, out of scope."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            doc = body[0].value
            spans.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return spans


class OneSeverityVocabulary(unittest.TestCase):

    def test_the_bare_forms_are_gone_from_every_log_line(self):
        """A `_log` argument may not open with a pre-bracket severity word."""
        offenders = []
        for path in _python_sources():
            skip = _docstring_lines(path)
            for lineno, text in _string_constants(path):
                if lineno in skip:
                    continue
                stripped = text.lstrip()
                for bare in _BARE_FORMS:
                    if stripped.startswith(bare):
                        offenders.append(f"{path.name}:{lineno} {text[:60]!r}")
        self.assertEqual(
            offenders, [],
            "a pre-bracket severity convention is back — the Protokoll had "
            "eight of them and preflight_system.ps1's four are the canonical "
            "set:\n" + "\n".join(offenders))

    def test_every_bracketed_marker_is_one_of_the_four(self):
        """The vocabulary is CLOSED — a `[HINWEIS]` would be a ninth form."""
        allowed = {"OK", "INFO", "WARNUNG", "FEHLER", "STOPP"}
        offenders = []
        for path in _python_sources():
            skip = _docstring_lines(path)
            for lineno, text in _string_constants(path):
                if lineno in skip:
                    continue
                m = _MARKER_RE.match(text.lstrip())
                if m and m.group(1) not in allowed:
                    offenders.append(f"{path.name}:{lineno} [{m.group(1)}]")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_four_levels_are_the_POWERSHELL_ones(self):
        """Read out of `preflight_system.ps1` rather than restated, because
        that script's lines land in this very pane."""
        if not _PREFLIGHT_PS1.is_file():  # pragma: no cover — dev tree only
            self.skipTest("preflight_system.ps1 not present")
        text = _PREFLIGHT_PS1.read_text(encoding="utf-8-sig")
        m = re.search(r"ValidateSet\(([^)]*)\)", text)
        self.assertIsNotNone(m, "preflight_system.ps1::Emit lost its ValidateSet")
        ps_levels = set(re.findall(r"'([A-Z]+)'", m.group(1)))
        gui = _GUI_APP.read_text(encoding="utf-8")
        for level in ps_levels:
            with self.subTest(level):
                self.assertIn(
                    f'"{level.lower()}"', gui,
                    f"the GUI has no colour tag for [{level}], which "
                    f"preflight_system.ps1 writes into the same pane")


class TheProtokollIsUsable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        cls.src = _GUI_APP.read_text(encoding="utf-8")

    def _func(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not found — this test is stale")

    def test_log_takes_exactly_the_message(self):
        """The deviation, pinned: adding a `level=` keyword would be a
        TypeError in five source-extracted methods whose `_log` double is a
        bare `list.append`."""
        fn = self._func("_log")
        args = [a.arg for a in fn.args.args]
        self.assertEqual(args, ["self", "msg"])
        self.assertEqual(fn.args.defaults, [])
        self.assertIsNone(fn.args.kwarg)

    def test_the_severity_tag_is_derived_from_the_line_itself(self):
        fn = self._func("_tag_for_line")
        code = ast.unparse(fn)
        for marker in ("[FEHLER]", "[WARNUNG]", "[OK]", "[INFO]"):
            self.assertIn(marker, code)

    def test_an_unmarked_line_is_never_guessed_into_a_colour(self):
        ns = {}
        exec(compile(ast.unparse(self._func("_tag_for_line")),  # noqa: S102
                     str(_GUI_APP), "exec"), ns)
        tag_for = ns["_tag_for_line"]
        self.assertEqual(tag_for("Leader gefunden: /dev/serial/by-id/x"), "")
        self.assertEqual(tag_for("[FEHLER] kaputt"), "fehler")
        self.assertEqual(tag_for("[WARNUNG] hm"), "warnung")
        self.assertEqual(tag_for("[OK] gut"), "ok")
        self.assertEqual(tag_for("[INFO] hallo"), "info")
        # A preflight_system.ps1 line arrives indented by `_run_preflight_
        # diagnostics`, and must still colour.
        self.assertEqual(tag_for("  [OK] WSL2 ist aktiv"), "ok")

    def test_the_buffer_is_capped(self):
        self.assertIn("LOG_MAX_LINES", ast.unparse(self._func("_log")))
        self.assertIn("LOG_MAX_LINES = ", self.src)

    def test_copy_and_save_exist_and_are_wired_to_buttons(self):
        for name in ("_copy_log", "_save_log"):
            self._func(name)  # raises if missing
        build = ast.unparse(self._func("_build_ui"))
        self.assertIn("Protokoll kopieren", build)
        self.assertIn("Protokoll speichern", build)
        self.assertIn("command=self._copy_log", build)
        self.assertIn("command=self._save_log", build)

    def test_the_button_row_is_reserved_BEFORE_the_text_widget(self):
        """Measured: packed AFTER `log_text`, both buttons collapsed to 1x1 px.

        The whole form is ~290 px taller than its own default window, so pack
        runs out of height inside the Protokoll frame and squeezes whatever it
        allocates last. `side=BOTTOM` packed first reserves the row and gives
        `log_text` the remainder.
        """
        build = ast.unparse(self._func("_build_ui"))
        row = build.index("log_btn_row.pack(")
        text = build.index("self.log_text.pack(")
        self.assertLess(
            row, text,
            "the Protokoll buttons are packed after the text widget again — "
            "they get squeezed to 1x1 px and become unreachable")
        self.assertIn("side=tk.BOTTOM", build[row:text])

    def test_a_multi_line_block_is_coloured_PER_LINE(self):
        """`_run_preflight_diagnostics` sends its whole block in one call, and
        those are exactly the lines that carry their own markers."""
        append = ast.unparse(self._func("_log"))
        self.assertIn("msg.split('\\n')", append.replace('"', "'"))

    def test_the_save_target_is_the_shared_diagnostics_leaf(self):
        """One folder holds every support artefact — the same leaf the six
        installer .ps1 write `install_diagnostics.log` into."""
        self.assertIn("_edubotics_diag_dir()",
                      ast.unparse(self._func("_save_log")))

    def test_neither_helper_can_raise_into_the_ui(self):
        for name in ("_copy_log", "_save_log"):
            with self.subTest(name):
                fn = self._func(name)
                self.assertTrue(
                    [n for n in ast.walk(fn) if isinstance(n, ast.Try)],
                    f"{name} is unguarded — a clipboard or disk failure would "
                    f"raise into the Tk callback handler")

    def test_the_preflight_block_is_emitted_ATOMICALLY(self):
        """`_check_prerequisites` runs this on one daemon thread and the
        prerequisite chain on another; `_log` serialises lines through
        `root.after` but not BLOCKS, so one `_log` per line let a multi-line
        diagnosis interleave with unrelated output."""
        fn = self._func("_run_preflight_diagnostics")
        code = ast.unparse(fn)
        self.assertIn("'\\n'.join(", code.replace('"', "'"))
        self.assertNotIn("for line in lines:\n    self._log(", code)


class TypographyIsConsistent(unittest.TestCase):
    """255 em-dashes vs 8 en-dashes, and mixed quote styles, in one window."""

    _ALLOWED_EN_DASH_CONTEXTS = ("OMX – ", "6-Achs – ", "Edu:1 – ")

    def test_student_visible_text_uses_em_dashes(self):
        offenders = []
        for path in _python_sources():
            skip = _docstring_lines(path)
            for lineno, text in _string_constants(path):
                if lineno in skip or "–" not in text:
                    continue
                if any(ctx in text for ctx in self._ALLOWED_EN_DASH_CONTEXTS):
                    continue  # profile display labels — a real product name
                offenders.append(f"{path.name}:{lineno} {text[:60]!r}")
        self.assertEqual(
            offenders, [],
            "en-dash in student-visible text; this file uses — everywhere "
            "else:\n" + "\n".join(offenders))

    def test_ui_element_names_are_quoted_the_german_way(self):
        """`_apply_robot_type_labels` writes „Arme scannen“ while other strings
        wrote 'Umgebung starten' — same window, same sentence shape."""
        offenders = []
        pattern = re.compile(r"'(Umgebung starten|Arme scannen|Arm scannen|"
                             r"Kameras scannen|Stoppen)'")
        for path in _python_sources():
            skip = _docstring_lines(path)
            for lineno, text in _string_constants(path):
                if lineno in skip:
                    continue
                if pattern.search(text):
                    offenders.append(f"{path.name}:{lineno} {text[:60]!r}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
