"""Unit tests for app.routes.jetson + app.services.jetson_sweep.

Mirrors the test_workgroups.py shape: lightweight stubs for fastapi /
supabase / heavy deps via sys.modules so the file runs with or without
real installs. Integration tests that need real Postgres semantics
(P0030 race, FOR UPDATE) require a Supabase branch and are documented
as a follow-up.

What we cover:
  - _generate_pairing_code: 6 digits, zero-padded
  - _generate_mdns_name: deterministic from jetson_id
  - _is_online: NULL / old / fresh timestamps map to False/False/True
  - _map_pg_error: P0030 → 409, P0031 → 410, P0011 → 403, P0002 → 404,
    P0001 → 401, unknown → 500
  - _serialise: builds JetsonInfo from a row + owner_info dict
  - jetson_sweep._run_sweep_once: returns RPC's affected count, swallows
    exceptions
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch


# Stub fastapi + supabase before importing app.routes.jetson.
def _ensure_stubs() -> None:
    import app  # noqa: F401
    import app.services  # noqa: F401

    if "app.auth" not in sys.modules:
        m = types.ModuleType("app.auth")
        m.get_current_profile = lambda *a, **kw: {}
        m.get_current_teacher = lambda *a, **kw: {}
        m.get_user_profile = lambda _uid: {}
        sys.modules["app.auth"] = m
    if "app.services.supabase_client" not in sys.modules:
        m = types.ModuleType("app.services.supabase_client")
        m.get_supabase = lambda: None
        sys.modules["app.services.supabase_client"] = m
    # fastapi shim — only the bits the routes module imports at top level.
    if "fastapi" not in sys.modules:
        m = types.ModuleType("fastapi")

        class _APIRouter:
            def __init__(self, *_, **__): pass

            def get(self, *_, **__):
                def deco(fn): return fn
                return deco

            def post(self, *_, **__):
                def deco(fn): return fn
                return deco

        class _HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail or "")
                self.status_code = status_code
                self.detail = detail

        def _depends(*_, **__): return None
        def _path(*_, **__): return None
        def _body(*_, **__): return None

        m.APIRouter = _APIRouter
        m.HTTPException = _HTTPException
        m.Depends = _depends
        m.Path = _path
        m.Body = _body
        sys.modules["fastapi"] = m

    if "pydantic" not in sys.modules:
        m = types.ModuleType("pydantic")

        class _BaseModel:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

            def model_dump(self):
                return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

        def _field(*_a, **_kw): return None
        m.BaseModel = _BaseModel
        m.Field = _field
        sys.modules["pydantic"] = m


_ensure_stubs()

# Now safe to import — module top-level uses the stubs above.
from app.routes import jetson as jetson_routes  # noqa: E402
from app.services import jetson_sweep  # noqa: E402


class TestPairingCode(unittest.TestCase):
    def test_pairing_code_is_six_digits(self):
        for _ in range(20):
            code = jetson_routes._generate_pairing_code()
            self.assertEqual(len(code), 6)
            self.assertTrue(code.isdigit(), msg=f"non-digit code: {code!r}")

    def test_pairing_code_zero_padded(self):
        # secrets.randbelow can return values < 100000; the f"{n:06d}"
        # format must pad them to 6 digits.
        with patch("app.routes.jetson.secrets.randbelow", return_value=42):
            self.assertEqual(jetson_routes._generate_pairing_code(), "000042")


class TestMdnsName(unittest.TestCase):
    def test_deterministic_from_uuid(self):
        name = jetson_routes._generate_mdns_name(
            "abc12345-6789-0123-4567-890123456789"
        )
        self.assertEqual(name, "edubotics-jetson-abc12345.local")

    def test_no_hyphen_in_id(self):
        name = jetson_routes._generate_mdns_name("singletokenid")
        self.assertEqual(name, "edubotics-jetson-singletokenid.local")


class TestIsOnline(unittest.TestCase):
    def test_none_is_offline(self):
        self.assertFalse(jetson_routes._is_online(None))

    def test_old_timestamp_is_offline(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.assertFalse(jetson_routes._is_online(old))

    def test_fresh_timestamp_is_online(self):
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.assertTrue(jetson_routes._is_online(fresh))

    def test_z_suffix_handled(self):
        # Postgres returns ISO timestamps with +00:00; the helper also
        # accepts the Z suffix as a defense.
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.assertTrue(jetson_routes._is_online(fresh))

    def test_invalid_timestamp_is_offline(self):
        self.assertFalse(jetson_routes._is_online("not-a-date"))


class TestPgErrorMap(unittest.TestCase):
    def test_p0030_to_409(self):
        exc = jetson_routes._map_pg_error(Exception("P0030 Jetson belegt"))
        self.assertEqual(exc.status_code, 409)
        self.assertIn("belegt", exc.detail.lower())

    def test_p0031_to_410(self):
        exc = jetson_routes._map_pg_error(Exception("P0031 lock"))
        self.assertEqual(exc.status_code, 410)
        self.assertIn("verloren", exc.detail.lower())

    def test_p0011_to_403(self):
        exc = jetson_routes._map_pg_error(Exception("P0011 ownership"))
        self.assertEqual(exc.status_code, 403)
        self.assertIn("lehrer", exc.detail.lower())

    def test_p0002_to_404(self):
        exc = jetson_routes._map_pg_error(Exception("P0002 not found"))
        self.assertEqual(exc.status_code, 404)
        self.assertIn("nicht gefunden", exc.detail.lower())

    def test_p0001_to_401(self):
        exc = jetson_routes._map_pg_error(Exception("P0001 token"))
        self.assertEqual(exc.status_code, 401)
        self.assertIn("token", exc.detail.lower())

    def test_unknown_to_500(self):
        exc = jetson_routes._map_pg_error(Exception("some other error"))
        self.assertEqual(exc.status_code, 500)


class TestSerialise(unittest.TestCase):
    def test_serialise_unowned(self):
        row = {
            "id": "j1",
            "classroom_id": "c1",
            "mdns_name": "edubotics-jetson-j1.local",
            "lan_ip": "192.168.1.42",
            "agent_version": "v1.0.0",
            "last_seen_at": (
                datetime.now(timezone.utc) - timedelta(seconds=5)
            ).isoformat(),
            "current_owner_user_id": None,
            "claimed_at": None,
        }
        info = jetson_routes._serialise(row)
        self.assertEqual(info.jetson_id, "j1")
        self.assertEqual(info.classroom_id, "c1")
        self.assertTrue(info.online)
        self.assertIsNone(info.current_owner_user_id)
        self.assertIsNone(info.current_owner_username)

    def test_serialise_owned_with_username(self):
        row = {
            "id": "j1",
            "classroom_id": "c1",
            "mdns_name": "m",
            "lan_ip": "1.2.3.4",
            "agent_version": "v1",
            "last_seen_at": (datetime.now(timezone.utc)).isoformat(),
            "current_owner_user_id": "u9",
            "claimed_at": "2026-05-16T10:00:00+00:00",
        }
        owner = {"username": "anna", "full_name": "Anna Beispiel"}
        info = jetson_routes._serialise(row, owner_info=owner)
        self.assertEqual(info.current_owner_user_id, "u9")
        self.assertEqual(info.current_owner_username, "anna")
        self.assertEqual(info.current_owner_full_name, "Anna Beispiel")


class TestSweep(unittest.TestCase):
    # _run_sweep_once does a local `from app.services.supabase_client
    # import get_supabase` so we patch the module attribute that lookup
    # resolves to. The _ensure_stubs() block above installed a stub
    # module at that path with a get_supabase = lambda: None default;
    # tests override it via attribute set + restore.
    def _with_fake_supabase(self, fake_sb):
        import app.services.supabase_client as sc

        class _Ctx:
            def __enter__(_):
                _._orig = sc.get_supabase
                sc.get_supabase = lambda: fake_sb
                return _

            def __exit__(_, *exc):
                sc.get_supabase = _._orig
        return _Ctx()

    def test_run_sweep_once_returns_affected_count(self):
        fake_sb = SimpleNamespace()
        fake_result = SimpleNamespace(data=7)
        rpc_call = SimpleNamespace(execute=lambda: fake_result)
        fake_sb.rpc = lambda *_a, **_kw: rpc_call
        with self._with_fake_supabase(fake_sb):
            n = jetson_sweep._run_sweep_once()
        self.assertEqual(n, 7)

    def test_run_sweep_once_swallows_exceptions(self):
        fake_sb = SimpleNamespace()

        def _raise(*_a, **_kw):
            class _Q:
                def execute(self): raise RuntimeError("boom")
            return _Q()
        fake_sb.rpc = _raise
        with self._with_fake_supabase(fake_sb):
            n = jetson_sweep._run_sweep_once()
        # On error the loop must NOT raise — it logs and returns 0 so the
        # outer asyncio loop keeps ticking.
        self.assertEqual(n, 0)

    def test_run_sweep_once_zero_when_no_data(self):
        fake_sb = SimpleNamespace()
        fake_result = SimpleNamespace(data=None)
        rpc_call = SimpleNamespace(execute=lambda: fake_result)
        fake_sb.rpc = lambda *_a, **_kw: rpc_call
        with self._with_fake_supabase(fake_sb):
            n = jetson_sweep._run_sweep_once()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
