# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The legal links' configuration (launch plan step 4, 2026-11-11):
LEGAL_IMPRESSUM_URL, LEGAL_DATENSCHUTZ_URL and LEGAL_NUTZUNGSBEDINGUNGEN_URL are
each empty (no link) or an absolute https URL; anything else stops the boot
with a message that names the variable and never the value. Stdlib only."""

from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PARENT = os.path.dirname(os.path.dirname(HERE))
if APP_PARENT not in sys.path:
    sys.path.insert(0, APP_PARENT)

from app.services import legal_links as L  # noqa: E402

VARS = ("LEGAL_IMPRESSUM_URL", "LEGAL_DATENSCHUTZ_URL", "LEGAL_NUTZUNGSBEDINGUNGEN_URL")


def _env(**values):
    env = {k: v for k, v in os.environ.items() if k not in VARS}
    env.update(values)
    return patch.dict(os.environ, env, clear=True)


class CheckUrl(unittest.TestCase):
    def test_unset_and_blank_mean_no_link(self):
        for raw in (None, "", "   ", "\t"):
            self.assertIsNone(L.check_url(raw))

    def test_clean_https_addresses_pass_stripped(self):
        for raw, want in (
            ("https://edubotics.example/impressum", "https://edubotics.example/impressum"),
            ("  https://edubotics.example/  ", "https://edubotics.example/"),
            ("https://edubotics.example", "https://edubotics.example"),
            ("https://xn--bung-zra.example/datenschutz?lang=de#schule", "https://xn--bung-zra.example/datenschutz?lang=de#schule"),
            ("https://edubotics.example:8443/a%20b", "https://edubotics.example:8443/a%20b"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(L.check_url(raw), want)

    def test_everything_else_is_refused(self):
        for raw in (
            "http://edubotics.example/impressum",       # not https
            "HTTPS//edubotics.example",                  # no scheme
            "edubotics.example/impressum",               # relative
            "javascript:alert(1)",
            "data:text/html,<b>x</b>",
            "https://",                                  # no host
            "https:///impressum",
            "https://user:pw@edubotics.example/",        # credentials
            "https://user@edubotics.example/",
            "https://edubotics.example/a b",             # whitespace
            'https://edubotics.example/"onmouseover=x',  # a quote
            "https://edubotics.example/'x",
            "https://edubotics.example/<x>",
            "https://edubotics.example/\\x",
            "https://edubotics.example/`x`",
            "https://übung.example/",                    # not punycode
            "https://edubotics.example:99999/",          # bad port
            "https://edubotics.example:x/",
            "https://edubotics.example/" + "a" * L.MAX_URL_CHARS,
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    L.check_url(raw)
                self.assertNotIn(raw.strip(), str(ctx.exception))


class Boot(unittest.TestCase):
    def test_unset_or_valid_boots(self):
        with _env():
            L.validate_boot()
        with _env(**{v: f"https://edubotics.example/{v.lower()}" for v in VARS}):
            L.validate_boot()
        with _env(LEGAL_IMPRESSUM_URL=""):
            L.validate_boot()

    def test_an_invalid_address_stops_the_boot_naming_the_variable_never_the_value(self):
        for var in VARS:
            secretish = "http://typo.example/geheim-pfad"
            with self.subTest(var=var), _env(**{var: secretish}):
                with self.assertRaises(RuntimeError) as ctx:
                    L.validate_boot()
                msg = str(ctx.exception)
                self.assertIn(var, msg)
                self.assertNotIn("geheim", msg)
                self.assertIn("https://", msg)

    def test_links_serves_set_addresses_and_none_otherwise(self):
        with _env(LEGAL_IMPRESSUM_URL="https://edubotics.example/i", LEGAL_DATENSCHUTZ_URL="ftp://x"):
            self.assertEqual(
                L.links(),
                {
                    "legal_impressum_url": "https://edubotics.example/i",
                    "legal_datenschutz_url": None,
                    "legal_nutzungsbedingungen_url": None,
                },
            )

    def test_the_table_is_the_three_variables_in_display_order(self):
        self.assertEqual(
            L.LEGAL_LINKS,
            (
                ("LEGAL_IMPRESSUM_URL", "legal_impressum_url"),
                ("LEGAL_DATENSCHUTZ_URL", "legal_datenschutz_url"),
                ("LEGAL_NUTZUNGSBEDINGUNGEN_URL", "legal_nutzungsbedingungen_url"),
            ),
        )

    def test_main_validates_them_at_import(self):
        with open(os.path.join(APP_PARENT, "app", "main.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        calls = [
            n.value.func.id for n in tree.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
        ]
        self.assertIn("_validate_legal_links", calls)
        # Before the routers are mounted: a bad address never serves a request.
        first_router = next(
            i for i, n in enumerate(tree.body)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "attr", None) == "include_router"
        )
        validate = next(
            i for i, n in enumerate(tree.body)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "_validate_legal_links"
        )
        self.assertLess(validate, first_router)


if __name__ == "__main__":
    unittest.main()
