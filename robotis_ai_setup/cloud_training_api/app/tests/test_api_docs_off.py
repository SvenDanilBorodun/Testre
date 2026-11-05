"""The interactive API docs and the OpenAPI schema are off in production.

FastAPI's defaults served /docs, /redoc and /openapi.json anonymously (89 paths,
the admin and worker routes included; measured live 2026-11-05). ``main.py``
now builds the app with ``**_api_docs_kwargs()``, which switches all three off
unless ``EDUBOTICS_API_DOCS=1``.

Read from the source, like the schema-probe tests: importing ``app.main`` starts
the whole service (validators, routers, background loops).
"""

from __future__ import annotations

import ast
import os
import unittest
from unittest.mock import patch

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _tree() -> ast.Module:
    with open(MAIN, encoding="utf-8") as fh:
        return ast.parse(fh.read(), MAIN)


def _docs_kwargs_fn():
    """``_api_docs_kwargs`` compiled on its own, with only ``os`` in scope."""
    fn = next(
        n for n in _tree().body
        if isinstance(n, ast.FunctionDef) and n.name == "_api_docs_kwargs"
    )
    namespace: dict = {"os": os}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), MAIN, "exec"), namespace)
    return namespace["_api_docs_kwargs"]


class ApiDocsOffTest(unittest.TestCase):
    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EDUBOTICS_API_DOCS", None)
            self.assertEqual(
                _docs_kwargs_fn()(),
                {"docs_url": None, "redoc_url": None, "openapi_url": None},
            )

    def test_anything_but_1_keeps_them_off(self):
        for value in ("", "0", "true", "yes", "2", " 0 "):
            with self.subTest(value=value), patch.dict(os.environ, {"EDUBOTICS_API_DOCS": value}):
                self.assertIsNone(_docs_kwargs_fn()()["openapi_url"])

    def test_1_turns_them_on_for_a_developer(self):
        for value in ("1", " 1 "):
            with self.subTest(value=value), patch.dict(os.environ, {"EDUBOTICS_API_DOCS": value}):
                self.assertEqual(_docs_kwargs_fn()(), {})

    def test_the_app_is_built_with_them(self):
        calls = [
            n.value for n in _tree().body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "app" for t in n.targets)
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "FastAPI"
        ]
        self.assertEqual(len(calls), 1, "exactly one `app = FastAPI(...)` expected")
        call = calls[0]
        starred = [
            k.value for k in call.keywords
            if k.arg is None and isinstance(k.value, ast.Call)
            and getattr(k.value.func, "id", None) == "_api_docs_kwargs"
        ]
        self.assertEqual(len(starred), 1, "FastAPI(...) must receive **_api_docs_kwargs()")
        explicit = {k.arg for k in call.keywords if k.arg}
        self.assertFalse(
            explicit & {"docs_url", "redoc_url", "openapi_url"},
            "an explicit docs_url/redoc_url/openapi_url would override the switch",
        )


if __name__ == "__main__":
    unittest.main()
