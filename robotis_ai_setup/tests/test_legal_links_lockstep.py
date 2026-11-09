# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The legal links across the three places that name them (launch plan step 4,
2026-11-11), and the Windows GUI's half.

The cloud API serves three /version keys from three Railway variables
(``cloud_training_api/app/services/legal_links.py::LEGAL_LINKS``); the React
app (``features/legal/legalLinks.js::LEGAL_LINK_FIELDS``) and the GUI
(``gui/app/update_checker.py::LEGAL_LINKS``) each read them with their own
German link text. A key renamed in one place would silently drop that link
everywhere else, so the three tables are held equal here, in order, with the
same three German words.

The GUI half: the checker never raises and only ever hands out an https URL;
``gui_app`` builds an (empty) row under the version line, fills it from a
daemon thread and opens nothing but a re-validated https address.
Deps-free (stdlib + ast)."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.dirname(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gui.app import update_checker as U  # noqa: E402

API_LEGAL = os.path.join(ROOT, "cloud_training_api", "app", "services", "legal_links.py")
REACT_LEGAL = os.path.join(REPO, "physical_ai_tools", "physical_ai_manager", "src", "features", "legal", "legalLinks.js")
GUI_APP = os.path.join(ROOT, "gui", "app", "gui_app.py")

KEYS = ("legal_impressum_url", "legal_datenschutz_url", "legal_nutzungsbedingungen_url")
LABELS = ("Impressum", "Datenschutz", "Nutzungsbedingungen")
VARS = ("LEGAL_IMPRESSUM_URL", "LEGAL_DATENSCHUTZ_URL", "LEGAL_NUTZUNGSBEDINGUNGEN_URL")


def _api_table():
    with open(API_LEGAL, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "LEGAL_LINKS":
            return ast.literal_eval(node.value)
    raise AssertionError("LEGAL_LINKS not found in legal_links.py — this test is stale")


def _react_table():
    with open(REACT_LEGAL, encoding="utf-8") as fh:
        src = fh.read()
    block = re.search(r"export const LEGAL_LINK_FIELDS = Object\.freeze\(\[(.*?)\]\);", src, re.S)
    assert block, "LEGAL_LINK_FIELDS not found in legalLinks.js — this test is stale"
    return tuple(re.findall(r"\{ key: '([a-z_]+)', label: '([^']+)' \}", block.group(1)))


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — this test is stale")


class ThreeTablesOneContract(unittest.TestCase):
    def test_the_api_serves_the_three_keys_from_the_three_variables(self):
        self.assertEqual(_api_table(), tuple(zip(VARS, KEYS)))

    def test_react_reads_the_same_keys_with_the_same_german_words(self):
        self.assertEqual(_react_table(), tuple(zip(KEYS, LABELS)))

    def test_the_gui_reads_the_same_keys_with_the_same_german_words(self):
        self.assertEqual(U.LEGAL_LINKS, tuple(zip(KEYS, LABELS)))

    def test_all_three_cap_an_address_at_the_same_length(self):
        with open(API_LEGAL, encoding="utf-8") as fh:
            self.assertIn("MAX_URL_CHARS = 2048", fh.read())
        with open(REACT_LEGAL, encoding="utf-8") as fh:
            self.assertIn("export const LEGAL_URL_MAX_CHARS = 2048;", fh.read())
        self.assertEqual(U.LEGAL_URL_MAX_CHARS, 2048)


def _cm(body: bytes):
    resp = MagicMock()
    resp.read.return_value = body
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class GuiChecker(unittest.TestCase):
    ALL = {
        "version": "2.40.0",
        "legal_impressum_url": "https://edubotics.example/impressum",
        "legal_datenschutz_url": "https://edubotics.example/datenschutz",
        "legal_nutzungsbedingungen_url": "https://edubotics.example/nutzungsbedingungen",
    }

    def test_parse_names_each_set_link_in_order(self):
        self.assertEqual(
            U.parse_legal_links(self.ALL),
            [
                ("Impressum", "https://edubotics.example/impressum"),
                ("Datenschutz", "https://edubotics.example/datenschutz"),
                ("Nutzungsbedingungen", "https://edubotics.example/nutzungsbedingungen"),
            ],
        )
        self.assertEqual(
            U.parse_legal_links({"legal_datenschutz_url": " https://edubotics.example/d ", "legal_impressum_url": None}),
            [("Datenschutz", "https://edubotics.example/d")],
        )
        for body in (None, [], "x", 3, {"version": "2.37.0"}):
            self.assertEqual(U.parse_legal_links(body), [])

    def test_only_a_clean_https_address_is_ever_handed_out(self):
        for bad in (
            "http://edubotics.example/x", "javascript:alert(1)", "file:///C:/Windows/system32/calc.exe",
            "ms-settings:privacy", "https://", "https://user:pw@edubotics.example/", "https://edubotics.example/a b",
            'https://edubotics.example/"x', "https://übung.example/", "https://edubotics.example:0/",
            "https://edubotics.example:99999/", "https://edubotics.example/" + "a" * 2100, 7, None,
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(U.safe_legal_url(bad))
        self.assertEqual(U.safe_legal_url("https://xn--bung-zra.example/d?x=1#y"), "https://xn--bung-zra.example/d?x=1#y")

    def test_fetch_reads_version_and_never_raises(self):
        with patch.object(U.urllib.request, "urlopen", return_value=_cm(json.dumps(self.ALL).encode())) as op:
            self.assertEqual(len(U.fetch_legal_links("https://api.test/")), 3)
        req = op.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.test/version")
        for effect in (OSError("offline"), TimeoutError(), ValueError("x")):
            with self.subTest(effect=effect), patch.object(U.urllib.request, "urlopen", side_effect=effect):
                self.assertEqual(U.fetch_legal_links("https://api.test"), [])
        with patch.object(U.urllib.request, "urlopen", return_value=_cm(b"<html>captive portal</html>")):
            self.assertEqual(U.fetch_legal_links("https://api.test"), [])
        self.assertEqual(U.fetch_legal_links(""), [])


class GuiWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(GUI_APP, encoding="utf-8") as fh:
            cls.src = fh.read()
        cls.tree = ast.parse(cls.src)

    def test_an_empty_row_sits_right_under_the_version_line(self):
        build = ast.unparse(_func(self.tree, "_build_ui"))
        version = build.index("text=f'Version {APP_VERSION}'")
        row = build.index("self.legal_frame = ttk.Frame(main)")
        self.assertLess(version, row)
        self.assertLess(row, build.index("self.status_var = tk.StringVar"))
        self.assertIn("self.legal_frame.pack()", build)

    def test_the_links_load_on_a_daemon_thread_at_start(self):
        check = ast.unparse(_func(self.tree, "_check_prerequisites"))
        self.assertIn("threading.Thread(target=self._load_legal_links, daemon=True).start()", check)
        load = ast.unparse(_func(self.tree, "_load_legal_links"))
        self.assertIn("update_checker.fetch_legal_links(UPDATE_API_URL)", load)
        self.assertIn("self.root.after(0", load)

    def test_only_a_revalidated_https_address_is_opened(self):
        show = ast.unparse(_func(self.tree, "_show_legal_links"))
        self.assertIn("update_checker.safe_legal_url(url) != url", show)
        self.assertIn("webbrowser.open(target)", show)
        self.assertIn("theme_mod.LINK_LABEL", show)
        # Keyboard-reachable, not only clickable.
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            self.assertIn(sequence, show)
        self.assertIn("takefocus=True", show)

    def test_the_theme_defines_the_link_style(self):
        # Read, not imported: theme.py imports tkinter, which a CI venv may lack.
        with open(os.path.join(ROOT, "gui", "app", "theme.py"), encoding="utf-8") as fh:
            theme = fh.read()
        self.assertIn('LINK_LABEL = "Link.TLabel"', theme)
        self.assertIn('LINK_FONT = (FONT_FAMILY, 8, "underline")', theme)
        self.assertIn("style.configure(LINK_LABEL, font=LINK_FONT, foreground=COLOR_LINK)", theme)


if __name__ == "__main__":
    unittest.main()
