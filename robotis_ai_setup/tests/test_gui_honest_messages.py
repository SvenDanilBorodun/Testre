"""Four messages that told the student something that was not true.

  * `device_manager.diagnose_usb_environment`'s terminal message opened with
    „Alle Systeme einsatzbereit" ON A FAILURE PATH and named a Python file
    (`identify_arm.py`) to a 13-year-old. `_do_scan` puts
    `message_de.splitlines()[0]` into the status bar and that string had no
    newline, so the WHOLE sentence — filename included — became the status bar.
  * `_open_webview` logged „Web-Oberfläche wird im EduBotics-Fenster geöffnet."
    even when `open_student_window` had SHORT-CIRCUITED on a live child: nothing
    opened, nothing came to the foreground, and the freshly minted
    `?fresh=<nonce>` was discarded.
  * `_scan_cameras` tested `camera_privacy_blocked()` for truthiness, folding
    its third answer — None, „could not determine" — into the False branch, so
    a machine where the registry probe FAILED got the confident „Kamera per USB
    anschließen und prüfen …" tip.
  * `update_checker.download_installer` raised two authored German diagnoses
    and then swallowed them in its own `except Exception`, so a 404, a
    truncated transfer, a checksum mismatch, a full disk and a timeout were all
    `None` — on a modal the student cannot close.
"""

import ast
import os
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from gui.app import device_manager, update_checker

_APP = pathlib.Path(__file__).resolve().parent.parent / "gui" / "app"
_GUI_APP = _APP / "gui_app.py"


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — this test is stale")


class TheArmDiagnosisDoesNotContradictItself(unittest.TestCase):

    def _message(self):
        src = (_APP / "device_manager.py").read_text(encoding="utf-8")
        # The message is an implicitly-concatenated literal with one f-string
        # part, so read the SHAPE from source rather than executing the whole
        # probe (which shells out to usbipd/docker).
        marker = 'diag.message_de = ('
        idx = src.index("USB, Umgebung und Image", src.index("diag.ok = True"))
        start = src.rindex(marker, 0, idx)
        end = src.index("\n    )", start)
        return src[start:end]

    def test_it_no_longer_claims_everything_is_fine(self):
        self.assertNotIn("Alle Systeme einsatzbereit", self._message())

    def test_it_names_no_python_file(self):
        text = self._message()
        for leak in ("identify_arm.py", ".py"):
            self.assertNotIn(leak, text.replace("device_manager.py", ""))

    def test_the_first_line_is_short_enough_for_the_status_bar(self):
        """`_do_scan` puts `splitlines()[0]` there, so the newline is what keeps
        the servo-ID instruction out of a one-line widget."""
        text = self._message()
        self.assertIn("\\n", text,
                      "the message is one line again — the whole thing, "
                      "including the servo hint, becomes the status bar")
        first = text.split("\\n")[0]
        self.assertNotIn("Servo-IDs", first)

    def test_it_is_literal_german(self):
        text = self._message()
        for bad in ("pruefen", "Umgebeung", "koennen", "zugeordnet werden koennen"):
            self.assertNotIn(bad, text)


class TheWebviewReportsWhatActuallyHappened(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))

    def test_a_live_window_is_reported_as_already_open(self):
        fn = ast.unparse(_func(self.tree, "_open_webview"))
        self.assertIn("webview_window.has_live_window()", fn)
        self.assertIn("bereits geöffnet", fn)

    def test_the_check_happens_BEFORE_the_success_message(self):
        fn = ast.unparse(_func(self.tree, "_open_webview"))
        self.assertLess(
            fn.index("has_live_window()"),
            fn.index("wird im EduBotics-Fenster geöffnet"),
            "the false success line still runs on the short-circuit path")

    def test_the_short_circuit_in_webview_window_is_UNTOUCHED(self):
        """It exists because relaunching would destroy the student's unsaved
        Blockly work. This package changes only what the GUI SAYS."""
        src = (_APP / "webview_window.py").read_text(encoding="utf-8")
        fn = _func(ast.parse(src), "open_student_window")
        code = ast.unparse(fn)
        self.assertIn("_process", code)
        self.assertIn("return True", code)


class TheCameraPrivacyProbeKeepsItsThirdAnswer(unittest.TestCase):

    def test_the_tri_state_has_three_branches(self):
        tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        fn = ast.unparse(_func(tree, "_scan_cameras"))
        self.assertIn("if blocked is True:", fn)
        self.assertIn("elif blocked is False:", fn)
        self.assertNotIn("if blocked:", fn)

    def test_the_undeterminable_branch_does_not_sound_certain(self):
        tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        fn = ast.unparse(_func(tree, "_scan_cameras"))
        self.assertIn("nicht geprüft werden", fn)

    def test_the_probe_really_can_answer_None(self):
        """The premise, read from the source of truth rather than assumed."""
        from gui.app import win_camera
        doc = (win_camera.camera_privacy_blocked.__doc__ or "")
        self.assertIn("None", doc)


class TheUpdateDownloadSaysWhyItFailed(unittest.TestCase):

    BODY = b"pretend-installer" * 500

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    class _Resp:
        def __init__(self, body, content_length=None):
            self._body = body
            self._pos = 0
            cl = len(body) if content_length is None else content_length
            self.headers = {"Content-Length": str(cl)}

        def read(self, n):
            chunk = self._body[self._pos:self._pos + n]
            self._pos += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _dl(self, *, content_length=None, expected=None, raises=None):
        reasons = []
        target = "gui.app.update_checker.urllib.request.urlopen"
        kw = ({"side_effect": raises} if raises is not None
              else {"return_value": self._Resp(self.BODY, content_length)})
        with patch(target, **kw):
            path = update_checker.download_installer(
                "http://x/EduBotics_Setup.exe", dest_dir=self.tmp,
                expected_sha256=expected, reason_callback=reasons.append)
        return path, reasons

    def test_a_truncated_download_says_so(self):
        path, reasons = self._dl(content_length=len(self.BODY) + 5000)
        self.assertIsNone(path)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("Unvollständiger Download", reasons[0])

    def test_a_checksum_mismatch_says_so(self):
        path, reasons = self._dl(expected="00" * 32)
        self.assertIsNone(path)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("Prüfsumme", reasons[0])
        self.assertIn("beschädigt", reasons[0])

    def test_a_404_is_distinguishable_from_a_dead_link(self):
        _p, http = self._dl(
            raises=urllib.error.HTTPError("u", 404, "nf", None, None))
        _p2, offline = self._dl(raises=urllib.error.URLError("boom"))
        self.assertNotEqual(http, offline)
        self.assertIn("404", http[0])
        self.assertIn("Internetverbindung", offline[0])

    def test_a_disk_failure_never_leaks_an_english_errno_string(self):
        """`IOError` IS `OSError` in Python 3, so a naive „already German?"
        check on IOError would put `[Errno 28] No space left` on the modal."""
        _p, reasons = self._dl(raises=OSError(28, "No space left on device"))
        self.assertEqual(len(reasons), 1)
        self.assertNotIn("No space left", reasons[0])
        self.assertIn("Speicherplatz", reasons[0])

    def test_an_unclassified_failure_still_gets_a_german_sentence(self):
        _p, reasons = self._dl(raises=ValueError("kaputt"))
        self.assertEqual(len(reasons), 1)
        self.assertNotIn("kaputt", reasons[0])
        self.assertIn("fehlgeschlagen", reasons[0])

    def test_a_GOOD_download_reports_no_reason_at_all(self):
        path, reasons = self._dl()
        self.assertTrue(path and os.path.isfile(path))
        self.assertEqual(reasons, [])

    def test_the_return_contract_is_unchanged(self):
        """A tuple return would break every existing caller and test for no
        gain — the reason rides an optional callback instead."""
        import inspect
        sig = inspect.signature(update_checker.download_installer)
        self.assertIn("reason_callback", sig.parameters)
        self.assertIsNone(sig.parameters["reason_callback"].default)
        path, _ = self._dl()
        self.assertIsInstance(path, str)

    def test_the_dialog_shows_the_reason(self):
        tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        fn = ast.unparse(_func(tree, "_show_update_dialog"))
        self.assertIn("reason_callback=", fn)
        self.assertIn("status_var.set(r)", fn)
        self.assertNotIn(
            'status_var.set("Download fehlgeschlagen. Bitte '
            'Internetverbindung prüfen.")', fn,
            "every failure still claims the connection is the problem")

    def test_the_skip_button_still_appears_after_one_failure(self):
        """The modal is NON-CLOSABLE until then; nothing here may change that."""
        tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        fn = ast.unparse(_func(tree, "_show_update_dialog"))
        self.assertIn("self._update_fail_count += 1", fn)
        self.assertIn("if self._update_fail_count >= 1:", fn)
        self.assertIn("btn_skip.config(state=tk.NORMAL)", fn)


if __name__ == "__main__":
    unittest.main()
