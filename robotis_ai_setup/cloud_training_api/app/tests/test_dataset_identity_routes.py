"""Route tests for the migration-030 HF-identity surface:
  * POST /datasets/sync  (routes/datasets.py::sync_datasets)
  * PATCH /me            (routes/me.py::update_me)

Follows the house pattern: stub fastapi + pydantic + the heavy leaf modules in
sys.modules so the routes import without real deps. The hf_identity helpers are
NOT stubbed — the deny-list / normalisation logic is exercised for real.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _ensure_stubs() -> None:
    import app  # noqa: F401
    import app.services  # noqa: F401

    if "huggingface_hub" not in sys.modules:
        m = types.ModuleType("huggingface_hub")

        class _HfApi:
            def __init__(self, *a, **k):
                pass

            def list_datasets(self, *a, **k):
                return []

            def dataset_info(self, *a, **k):
                return SimpleNamespace(author=None)

        m.HfApi = _HfApi
        sys.modules["huggingface_hub"] = m
        u = types.ModuleType("huggingface_hub.utils")

        class RepositoryNotFoundError(Exception):
            pass

        u.RepositoryNotFoundError = RepositoryNotFoundError
        sys.modules["huggingface_hub.utils"] = u

    if "app.auth" not in sys.modules:
        # Superset stub: this file is alphabetically first under `unittest
        # discover`, so its shims must satisfy every route any sibling test
        # later imports (teacher uses get_current_teacher, etc.).
        m = types.ModuleType("app.auth")
        m.get_current_profile = lambda *a, **kw: {}
        m.get_current_user = lambda *a, **kw: SimpleNamespace(id="u1")
        m.get_current_teacher = lambda *a, **kw: {}
        m.get_current_admin = lambda *a, **kw: {}
        m.get_user_profile = lambda _uid: {}
        sys.modules["app.auth"] = m
    if "app.services.supabase_client" not in sys.modules:
        m = types.ModuleType("app.services.supabase_client")
        m.get_supabase = lambda: None
        sys.modules["app.services.supabase_client"] = m
    if "app.services.modal_client" not in sys.modules:
        m = types.ModuleType("app.services.modal_client")

        async def _cancel(*a, **k):
            return None

        m.cancel_training_job = _cancel
        sys.modules["app.services.modal_client"] = m
    # NOTE: do NOT stub app.services.workgroups — the real module imports only
    # the already-stubbed app.auth + supabase_client, and test_workgroups.py
    # patches the real module's get_supabase. Shadowing it breaks those tests.

    if "fastapi" not in sys.modules:
        m = types.ModuleType("fastapi")

        class _Router:
            def __init__(self, *a, **k):
                pass

            def _deco(self, *a, **k):
                def d(fn):
                    return fn

                return d

            get = post = patch = delete = put = _deco

        class _HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail or "")
                self.status_code = status_code
                self.detail = detail

        m.APIRouter = _Router
        m.HTTPException = _HTTPException
        m.Depends = lambda *a, **k: None
        m.Query = lambda *a, **k: None
        m.Path = lambda *a, **k: None
        m.Body = lambda *a, **k: None
        m.Header = lambda *a, **k: None

        class _Request:  # routes/inference.py reads the reserve body by hand (V1-8)
            pass

        m.Request = _Request
        sys.modules["fastapi"] = m

        r = types.ModuleType("fastapi.responses")

        class _JSONResponse:
            def __init__(self, content=None, status_code=200, headers=None):
                self.content = content
                self.status_code = status_code
                self.headers = headers

        r.JSONResponse = _JSONResponse
        sys.modules["fastapi.responses"] = r

    if "pydantic" not in sys.modules:
        m = types.ModuleType("pydantic")

        class _BaseModel:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

            def model_dump(self):
                return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

        def _field(*_a, **_kw):
            return None

        def _field_validator(*_a, **_kw):
            def d(fn):
                return fn

            return d

        m.BaseModel = _BaseModel
        m.Field = _field
        m.field_validator = _field_validator
        sys.modules["pydantic"] = m


_ensure_stubs()

from fastapi import HTTPException  # noqa: E402
from app.routes import datasets as dsroute  # noqa: E402
from app.routes import me as meroute  # noqa: E402


# ------------------------------------------------------------------
# POST /datasets/sync
# ------------------------------------------------------------------
class _SyncSupabase:
    def __init__(self, registered, proven):
        self._registered = registered
        self._proven = proven  # user_hf_credentials.hf_username, or None
        self.rpc_calls = []
        self.tables = []

    def table(self, name):
        outer = self
        outer.tables.append(name)

        class _Q:
            def select(self, *a, **k):
                return self

            def eq(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def execute(self):
                if name == "user_hf_credentials":
                    rows = [] if outer._proven is None else [{"hf_username": outer._proven}]
                    return SimpleNamespace(data=rows)
                return SimpleNamespace(data=outer._registered)

        return _Q()

    def rpc(self, fn, params):
        self.rpc_calls.append((fn, params))

        class _R:
            def execute(self):
                return SimpleNamespace(
                    data={
                        "id": "d",
                        "owner_user_id": params["p_user_id"],
                        "hf_repo_id": params["p_hf_repo_id"],
                        "name": params["p_name"],
                        "description": "",
                        "workgroup_id": params["p_workgroup_id"],
                        "created_at": "t",
                        "updated_at": "t",
                    }
                )

        return _R()


class TestSyncDatasets(unittest.TestCase):
    """SA-08: discovery runs on the PROVEN name (the stored token's account),
    anonymously; users.hf_username alone drives nothing."""

    def _run(self, profile, hf_list, registered, proven="__from_profile__"):
        if proven == "__from_profile__":
            proven = profile.get("hf_username")
        sb = _SyncSupabase(registered, proven)
        self.api_tokens = []
        self.listed_authors = []
        outer = self

        class _HfApi:
            def __init__(self, *a, **k):
                outer.api_tokens.append(k.get("token", "<unset>"))

            def list_datasets(self, author=None, **k):
                outer.listed_authors.append(author)
                return hf_list

        with patch.object(dsroute, "get_user_profile", return_value=profile), patch.object(
            dsroute, "get_supabase", return_value=sb
        ), patch.object(dsroute, "HfApi", _HfApi):
            result = dsroute.sync_datasets(user=SimpleNamespace(id=profile["id"]))
        return result, sb

    def test_not_linked_returns_400(self):
        with self.assertRaises(HTTPException) as cm:
            self._run({"id": "u1", "hf_username": None, "workgroup_id": None}, [], [])
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("Token", cm.exception.detail)
        self.assertEqual(self.listed_authors, [])

    def test_a_self_asserted_name_without_a_token_drives_nothing(self):
        # A legacy PATCH /me value with no stored token: no listing at all.
        with self.assertRaises(HTTPException) as cm:
            self._run({"id": "u1", "hf_username": "alice", "workgroup_id": None},
                      [SimpleNamespace(id="alice/d1")], [], proven=None)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.listed_authors, [])

    def test_the_proven_name_wins_over_the_profile(self):
        result, sb = self._run(
            {"id": "u1", "hf_username": "EduBotics-Solutions", "workgroup_id": None},
            [SimpleNamespace(id="anna/d1")], [], proven="anna")
        self.assertEqual(self.listed_authors, ["anna"])
        self.assertEqual(result.registered, 1)

    def test_denied_username_returns_400(self):
        for denied in ("RobotisSW", "EduBotics-Solutions", "edubotics-solutions"):
            with self.subTest(denied=denied):
                with self.assertRaises(HTTPException) as cm:
                    self._run(
                        {"id": "u1", "hf_username": denied, "workgroup_id": None}, [], []
                    )
                self.assertEqual(cm.exception.status_code, 400)
                self.assertEqual(self.listed_authors, [])

    def test_an_unreadable_credential_table_is_503_not_a_listing(self):
        class _Broken(_SyncSupabase):
            def table(self, name):
                if name == "user_hf_credentials":
                    raise RuntimeError("down")
                return super().table(name)

        sb = _Broken([], "alice")
        with patch.object(dsroute, "get_user_profile", return_value={"id": "u1"}), patch.object(
            dsroute, "get_supabase", return_value=sb
        ), patch.object(dsroute, "HfApi", side_effect=AssertionError("no listing")):
            with self.assertRaises(HTTPException) as cm:
                dsroute.sync_datasets(user=SimpleNamespace(id="u1"))
        self.assertEqual(cm.exception.status_code, 503)

    def test_the_listing_is_anonymous(self):
        self._run({"id": "u1", "hf_username": "alice", "workgroup_id": None},
                  [SimpleNamespace(id="alice/d1")], [])
        self.assertEqual(self.api_tokens, [False])

    def test_registers_only_own_namespace_keyed_to_jwt(self):
        result, sb = self._run(
            {"id": "u1", "hf_username": "alice", "workgroup_id": "g1"},
            [
                SimpleNamespace(id="alice/d1"),
                SimpleNamespace(id="alice/d2"),
                SimpleNamespace(id="bob/stolen"),  # cross-namespace → ignored
            ],
            [],
        )
        self.assertEqual(result.registered, 2)
        self.assertEqual(result.total_found, 2)  # bob/stolen not counted
        self.assertEqual(len(sb.rpc_calls), 2)
        for fn, params in sb.rpc_calls:
            # IDOR contract: keyed to the JWT user.id + the caller's own author.
            self.assertEqual(fn, "register_dataset_safe")
            self.assertEqual(params["p_user_id"], "u1")
            self.assertEqual(params["p_hf_author"], "alice")
            self.assertTrue(params["p_hf_repo_id"].startswith("alice/"))

    def test_already_known_not_reregistered(self):
        result, sb = self._run(
            {"id": "u1", "hf_username": "alice", "workgroup_id": None},
            [SimpleNamespace(id="alice/d1"), SimpleNamespace(id="alice/d2")],
            [{"hf_repo_id": "alice/d1"}],
        )
        self.assertEqual(result.registered, 1)
        self.assertEqual(result.already_known, 1)
        self.assertEqual(len(sb.rpc_calls), 1)
        self.assertEqual(sb.rpc_calls[0][1]["p_hf_repo_id"], "alice/d2")


# ------------------------------------------------------------------
# PATCH /me
# ------------------------------------------------------------------
class _MeSupabase:
    def __init__(self):
        self.updated = []

    def rpc(self, name, args):
        # 052: PATCH /me returns _build_my_profile, which reads a student's credit_quota
        assert name == "credit_quota", name
        row = {"o_role": "student", "o_account_kind": "student", "o_account_id": args["p_user_id"], "o_free": 2,
               "o_reserved": 1, "o_spent": 4, "o_member_limit": None, "o_member_used": None, "o_next_hours": 2}
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[row]))

    def table(self, _name):
        outer = self

        class _Q:
            def __init__(self):
                self._payload = None

            def update(self, payload):
                self._payload = payload
                return self

            def select(self, *a, **k):
                return self

            def eq(self, *a, **k):
                return self

            def single(self):
                return self

            def execute(self):
                if self._payload is not None:
                    outer.updated.append(self._payload)
                    return SimpleNamespace(data=[{"id": "u1"}])
                return SimpleNamespace(data=None)

        return _Q()


_PROFILE = {
    "id": "u1",
    "role": "student",
    "username": "s",
    "full_name": "S",
    "hf_username": None,
    "classroom_id": None,
    "workgroup_id": None,
    "training_credits": 0,
}


class TestUpdateMe(unittest.TestCase):
    """SA-08: PATCH /me never writes hf_username (the token save is the only writer)."""

    def test_no_name_is_accepted_any_more(self):
        sb = _MeSupabase()
        for raw in ("alice", "-not-valid", "RobotisSW", "EduBotics-Solutions", "edubotics-solutions"):
            with self.subTest(raw=raw):
                with patch.object(meroute, "get_supabase", return_value=sb):
                    with self.assertRaises(HTTPException) as cm:
                        meroute.update_me(body={"hf_username": raw}, profile=dict(_PROFILE))
                self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(sb.updated, [])


if __name__ == "__main__":
    unittest.main()
