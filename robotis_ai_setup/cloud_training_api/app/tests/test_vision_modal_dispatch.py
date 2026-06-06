"""Regression tests for the /vision Modal dispatch (app.routes.vision).

Mirrors the test_jetson.py shape: lightweight stubs for fastapi / pydantic /
supabase / modal via sys.modules so the file runs with or without real
installs.

Background (2026-06-06 production regression): vision.py resolved the
deployed detector with ``modal.Function.from_name(app, "OWLv2Detector.detect")``.
Modal >=1.x raises ``InvalidError`` for dotted class-method names ("use
`modal.Cls.from_name` instead"), and the Railway image resolved modal from
the then-unbounded ``modal>=0.64`` pin — so every /vision/detect call 503'd
once the pod picked up modal 1.4.3. The stub modal module below reproduces
the 1.4.x semantics faithfully, so a revert to the dotted Function.from_name
path fails these tests loudly.

What we cover:
  - dotted default name resolves via Cls.from_name and returns the payload
  - plain function names still go through Function.from_name
  - a dotted name whose method doesn't exist surfaces the German 503
  - requirements.txt keeps an upper-bounded modal pin (the root cause)
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


def _ensure_stubs() -> None:
    """Fill (or AUGMENT) the sys.modules stubs vision.py needs.

    Other test modules in this suite (test_jetson.py et al., loaded first
    by alphabetical discovery) register their own minimal fastapi/pydantic/
    app.auth stubs sized to THEIR routes. vision.py needs a couple of extra
    names (pydantic.field_validator, app.auth.get_current_user), so a
    presence-check alone isn't enough — add missing attributes onto an
    existing stub, but never touch attributes a real install provides.
    """
    import app  # noqa: F401
    import app.services  # noqa: F401

    auth = sys.modules.get("app.auth")
    if auth is None:
        auth = types.ModuleType("app.auth")
        sys.modules["app.auth"] = auth
    for name in ("get_current_user", "get_current_teacher"):
        if not hasattr(auth, name):
            setattr(auth, name, lambda *a, **kw: None)
    if not hasattr(auth, "get_user_profile"):
        auth.get_user_profile = lambda _uid: {}

    if "app.services.supabase_client" not in sys.modules:
        m = types.ModuleType("app.services.supabase_client")
        m.get_supabase = lambda: None
        sys.modules["app.services.supabase_client"] = m

    fa = sys.modules.get("fastapi")
    if fa is None:
        fa = types.ModuleType("fastapi")
        sys.modules["fastapi"] = fa
    if not hasattr(fa, "APIRouter"):
        class _APIRouter:
            def __init__(self, *_, **__):
                pass

            def get(self, *_, **__):
                def deco(fn):
                    return fn
                return deco

            def post(self, *_, **__):
                def deco(fn):
                    return fn
                return deco

        fa.APIRouter = _APIRouter
    if not hasattr(fa, "HTTPException"):
        class _HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail or "")
                self.status_code = status_code
                self.detail = detail

        fa.HTTPException = _HTTPException
    if not hasattr(fa, "Depends"):
        fa.Depends = lambda *_, **__: None

    pyd = sys.modules.get("pydantic")
    if pyd is None:
        pyd = types.ModuleType("pydantic")
        sys.modules["pydantic"] = pyd
    if not hasattr(pyd, "BaseModel"):
        class _BaseModel:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

        pyd.BaseModel = _BaseModel
    if not hasattr(pyd, "Field"):
        pyd.Field = lambda *_, **__: None
    if not hasattr(pyd, "field_validator"):
        def _field_validator(*_, **__):
            def deco(fn):
                return fn
            return deco

        pyd.field_validator = _field_validator


class _ModalInvalidError(Exception):
    pass


def _make_modal_stub(payload):
    """Build a stub `modal` module with faithful 1.4.x lookup semantics.

    - Function.from_name(app, name): raises InvalidError when `name`
      contains a dot (exactly what modal 1.4.3 does), otherwise returns a
      callable handle.
    - Cls.from_name(app, cls_name): returns a class factory whose
      instances expose bound method handles with `.remote.aio`.
    Records every lookup so tests can assert which path was taken.
    """
    m = types.ModuleType("modal")
    calls = {"function_from_name": [], "cls_from_name": []}

    class _Handle:
        def __init__(self, result):
            async def _aio(*a, **kw):
                return result

            self.remote = types.SimpleNamespace(aio=_aio)

    class _Function:
        @staticmethod
        def from_name(app_name, name):
            calls["function_from_name"].append((app_name, name))
            if "." in name:
                raise _ModalInvalidError(
                    f"Invalid Function name: '{name}'. If you are trying to "
                    "look up a class method, use `modal.Cls.from_name` instead."
                )
            return _Handle(payload)

    class _Instance:
        def __init__(self):
            self.detect = _Handle(payload)

    class _ClsRef:
        def __call__(self):
            return _Instance()

    class _Cls:
        @staticmethod
        def from_name(app_name, cls_name):
            calls["cls_from_name"].append((app_name, cls_name))
            return _ClsRef()

    m.Function = _Function
    m.Cls = _Cls
    m.exception = types.SimpleNamespace(NotFoundError=None)
    m._calls = calls
    return m


_ensure_stubs()

from app.routes import vision  # noqa: E402


class TestVisionModalDispatch(unittest.TestCase):
    PAYLOAD = {"detections": [{"label": "Würfel", "score": 0.9}], "cold_start": False}

    def _invoke(self):
        return asyncio.run(vision._invoke_modal(b"img", ["Würfel"], 0.25))

    def test_dotted_default_resolves_via_cls_from_name(self):
        stub = _make_modal_stub(self.PAYLOAD)
        with mock.patch.dict(sys.modules, {"modal": stub}), \
                mock.patch.object(vision, "VISION_FUNCTION_NAME", "OWLv2Detector.detect"):
            out = self._invoke()
        self.assertEqual(out["result"], self.PAYLOAD)
        self.assertEqual(stub._calls["cls_from_name"], [(vision.VISION_APP_NAME, "OWLv2Detector")])
        # The dotted name must never reach Function.from_name — on modal
        # >=1.x that raises InvalidError (the 2026-06-06 outage).
        self.assertEqual(stub._calls["function_from_name"], [])

    def test_plain_name_still_uses_function_from_name(self):
        stub = _make_modal_stub(self.PAYLOAD)
        with mock.patch.dict(sys.modules, {"modal": stub}), \
                mock.patch.object(vision, "VISION_FUNCTION_NAME", "detect_fn"):
            out = self._invoke()
        self.assertEqual(out["result"], self.PAYLOAD)
        self.assertEqual(stub._calls["function_from_name"], [(vision.VISION_APP_NAME, "detect_fn")])
        self.assertEqual(stub._calls["cls_from_name"], [])

    def test_dotted_name_with_unknown_method_surfaces_german_503(self):
        stub = _make_modal_stub(self.PAYLOAD)
        fastapi = sys.modules["fastapi"]
        with mock.patch.dict(sys.modules, {"modal": stub}), \
                mock.patch.object(vision, "VISION_FUNCTION_NAME", "OWLv2Detector.nope"):
            with self.assertRaises(fastapi.HTTPException) as ctx:
                self._invoke()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("Cloud-Erkennung", ctx.exception.detail)

    def test_requirements_pin_is_upper_bounded(self):
        # The root cause of the outage was the unbounded `modal>=0.64`
        # floor: every Railway build silently resolved the newest major.
        req = Path(__file__).resolve().parents[2] / "requirements.txt"
        lines = [
            ln.strip() for ln in req.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("modal")
        ]
        self.assertEqual(len(lines), 1, f"expected exactly one modal pin, got {lines}")
        self.assertIn("<2", lines[0].replace(" ", ""), f"modal pin lost its upper bound: {lines[0]}")
        self.assertIn(">=1.4", lines[0].replace(" ", ""), f"modal pin floor regressed: {lines[0]}")



if __name__ == "__main__":
    unittest.main()
