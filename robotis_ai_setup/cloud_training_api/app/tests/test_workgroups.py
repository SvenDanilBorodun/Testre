"""Unit tests for the workgroup helpers + dataset sweep service.

These tests deliberately avoid spinning up Supabase or the full FastAPI
app — they run with the lightweight deps already installed by CI's
api-tests job (fastapi, pydantic, huggingface_hub stub). Integration
tests that need real Postgres semantics (RLS, FOR UPDATE concurrency)
require a Supabase branch and are documented in the plan as a follow-up
when test infrastructure is in place.

What we cover:
  - resolve_visible_workgroup_ids: audit-table primary path + fallback
  - dataset_sweep._parse_author: defensive parsing
  - dataset_sweep._gather_author_candidates: trainings + datasets scan,
    dedup by user_id
  - dataset_sweep._extract_meta: tolerant of missing HF SDK fields
  - dataset_sweep._run_sweep_once: skips when HF_TOKEN missing,
    duplicates handled gracefully
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


# ------------------------------------------------------------------
# Stub the heavy app.auth + app.services.supabase_client modules so
# this test file can run with or without fastapi / supabase installed.
# CI installs the real deps; local dev runs may not. The module under
# test (app.services.workgroups) imports get_user_profile and
# get_supabase at module load — without stubs Python would raise
# ModuleNotFoundError on `from supabase import ...`. We *only* override
# the two leaf modules, leaving the real `app` and `app.services`
# packages intact so their other submodules import normally.
#
# Per-test patches replace these stubs' attributes via patch.object
# on the workgroups module itself — that's the standard pattern.
# ------------------------------------------------------------------
def _ensure_test_stubs() -> None:
    # Import the real parent packages first so Python's resolver knows
    # about them; then overlay the leaf stubs only if the real modules
    # haven't already been imported.
    import app  # noqa: F401
    import app.services  # noqa: F401

    if "app.auth" not in sys.modules:
        m = types.ModuleType("app.auth")
        m.get_user_profile = lambda _uid: {}
        sys.modules["app.auth"] = m
    if "app.services.supabase_client" not in sys.modules:
        m = types.ModuleType("app.services.supabase_client")
        m.get_supabase = lambda: None
        sys.modules["app.services.supabase_client"] = m
    # huggingface_hub is heavy. The sweep imports HfApi lazily; if the
    # real module isn't installed we install a stub that tests can
    # introspect via patch.object.
    if "huggingface_hub" not in sys.modules:
        m = types.ModuleType("huggingface_hub")
        class _HfApiStub:  # noqa: D401
            def __init__(self, *a, **kw): pass
            def list_datasets(self, *a, **kw): return []
        m.HfApi = _HfApiStub
        sys.modules["huggingface_hub"] = m


_ensure_test_stubs()


# ------------------------------------------------------------------
# Test doubles for the supabase chain. The Supabase Python client is a
# fluent builder: client.table(name).select(...).eq(...).execute().
# We mimic it just enough to drive the helpers under test.
# ------------------------------------------------------------------
class FakeQuery:
    def __init__(self, payload):
        self._payload = payload

    def select(self, *_a, **_kw):
        return self

    def eq(self, *_a, **_kw):
        return self

    def in_(self, *_a, **_kw):
        return self

    def is_(self, *_a, **_kw):
        return self

    def order(self, *_a, **_kw):
        return self

    def limit(self, *_a, **_kw):
        return self

    def range(self, *_a, **_kw):
        return self

    def update(self, *_a, **_kw):
        return self

    def insert(self, _payload):
        return self

    def delete(self):
        return self

    def execute(self):
        return SimpleNamespace(data=self._payload)


class FakeSupabase:
    """Maps table name -> list of FakeQuery payloads, popped FIFO."""

    def __init__(self, by_table):
        # by_table: {"trainings": [list_payload_1, list_payload_2, ...]}
        self._by_table = {k: list(v) for k, v in by_table.items()}

    def table(self, name):
        if name not in self._by_table:
            return FakeQuery([])
        if not self._by_table[name]:
            return FakeQuery([])
        return FakeQuery(self._by_table[name].pop(0))


# ------------------------------------------------------------------
# resolve_visible_workgroup_ids
# ------------------------------------------------------------------
class TestResolveVisibleWorkgroupIds(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("SUPABASE_URL", "http://test")
        os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test")

    def test_uses_audit_table_when_present(self):
        from app.services import workgroups as wg

        fake = FakeSupabase(
            {
                "workgroup_memberships": [
                    [{"workgroup_id": "g1"}, {"workgroup_id": "g2"}]
                ],
            }
        )
        with patch.object(wg, "get_supabase", return_value=fake), patch.object(
            wg, "get_user_profile", return_value={"workgroup_id": "g3"}
        ):
            result = wg.resolve_visible_workgroup_ids("uid")
        self.assertEqual(result, ["g1", "g2"])

    def test_falls_back_to_profile_when_audit_empty(self):
        from app.services import workgroups as wg

        fake = FakeSupabase({"workgroup_memberships": [[]]})
        with patch.object(wg, "get_supabase", return_value=fake), patch.object(
            wg, "get_user_profile", return_value={"workgroup_id": "g3"}
        ):
            result = wg.resolve_visible_workgroup_ids("uid")
        self.assertEqual(result, ["g3"])

    def test_returns_empty_when_no_membership_and_no_profile_group(self):
        from app.services import workgroups as wg

        fake = FakeSupabase({"workgroup_memberships": [[]]})
        with patch.object(wg, "get_supabase", return_value=fake), patch.object(
            wg, "get_user_profile", return_value={"workgroup_id": None}
        ):
            result = wg.resolve_visible_workgroup_ids("uid")
        self.assertEqual(result, [])

    def test_skips_audit_rows_with_null_workgroup_id(self):
        from app.services import workgroups as wg

        fake = FakeSupabase(
            {
                "workgroup_memberships": [
                    [{"workgroup_id": "g1"}, {"workgroup_id": None}]
                ],
            }
        )
        with patch.object(wg, "get_supabase", return_value=fake), patch.object(
            wg, "get_user_profile", return_value={}
        ):
            result = wg.resolve_visible_workgroup_ids("uid")
        self.assertEqual(result, ["g1"])


# ------------------------------------------------------------------
# dataset_sweep._parse_author
# ------------------------------------------------------------------
class TestParseAuthor(unittest.TestCase):
    def test_valid_repo(self):
        from app.services.dataset_sweep import _parse_author

        self.assertEqual(_parse_author("alice/dataset-a"), "alice")
        self.assertEqual(_parse_author("EduBotics-Solutions/foo"), "EduBotics-Solutions")

    def test_invalid_inputs(self):
        from app.services.dataset_sweep import _parse_author

        self.assertIsNone(_parse_author(""))
        self.assertIsNone(_parse_author(None))
        self.assertIsNone(_parse_author("no_slash_here"))


# ------------------------------------------------------------------
# dataset_sweep._gather_author_candidates
# ------------------------------------------------------------------
class TestGatherAuthorCandidates(unittest.TestCase):
    def test_dedup_by_user_id(self):
        from app.services import dataset_sweep as ds

        fake = FakeSupabase(
            {
                "trainings": [
                    [
                        {"user_id": "u1", "dataset_name": "alice/d1", "model_name": "alice/m1"},
                        {"user_id": "u1", "dataset_name": "alice/d2", "model_name": None},
                        {"user_id": "u2", "dataset_name": "bob/d3", "model_name": "alice/m2"},
                    ],
                ],
                "datasets": [
                    [
                        {"owner_user_id": "u1", "hf_repo_id": "alice/d1"},
                        {"owner_user_id": "u3", "hf_repo_id": "alice/d4"},
                    ],
                ],
            }
        )
        result = ds._gather_author_candidates(fake)
        # Both u1 and u2 wrote to "alice", but only one entry per user.
        alice = sorted([c["user_id"] for c in result["alice"]])
        self.assertEqual(alice, ["u1", "u2", "u3"])
        bob = [c["user_id"] for c in result["bob"]]
        self.assertEqual(bob, ["u2"])

    def test_skips_invalid_repo_ids(self):
        from app.services import dataset_sweep as ds

        fake = FakeSupabase(
            {
                "trainings": [
                    [
                        {"user_id": "u1", "dataset_name": "no_slash", "model_name": ""},
                    ],
                ],
            }
        )
        result = ds._gather_author_candidates(fake)
        self.assertEqual(result, {})


# ------------------------------------------------------------------
# dataset_sweep._extract_meta
# ------------------------------------------------------------------
class TestExtractMeta(unittest.TestCase):
    def test_id_preferred(self):
        from app.services.dataset_sweep import _extract_meta

        obj = SimpleNamespace(id="alice/data-x", repo_id="ignored/ignored")
        meta = _extract_meta(obj)
        self.assertEqual(meta["hf_repo_id"], "alice/data-x")
        self.assertEqual(meta["name"], "data-x")

    def test_repo_id_fallback(self):
        from app.services.dataset_sweep import _extract_meta

        obj = SimpleNamespace(repo_id="alice/data-y")
        meta = _extract_meta(obj)
        self.assertEqual(meta["hf_repo_id"], "alice/data-y")

    def test_no_id_returns_empty(self):
        from app.services.dataset_sweep import _extract_meta

        obj = SimpleNamespace()
        self.assertEqual(_extract_meta(obj), {})


# ------------------------------------------------------------------
# dataset_sweep._run_sweep_once
# ------------------------------------------------------------------
class TestRunSweepOnce(unittest.TestCase):
    def test_no_token_short_circuits(self):
        from app.services import dataset_sweep as ds

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HF_TOKEN", None)
            self.assertEqual(ds._run_sweep_once(), 0)

    def test_no_candidates_no_calls(self):
        from app.services import dataset_sweep as ds
        # Import the module so the patch target resolves; it's only
        # imported lazily inside _run_sweep_once otherwise.
        import app.services.supabase_client  # noqa: F401

        fake = FakeSupabase({"trainings": [[]], "datasets": [[]]})
        with patch.dict(os.environ, {"HF_TOKEN": "t"}, clear=False), patch.object(
            ds, "_gather_author_candidates", return_value={}
        ), patch.object(
            ds, "_enrich_with_workgroup", return_value={}
        ), patch("app.services.supabase_client.get_supabase", return_value=fake):
            self.assertEqual(ds._run_sweep_once(), 0)


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main()
