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
  - dataset_sweep._students_with_hf_username: migration-030 per-user anchor
  - dataset_sweep._extract_repo_id: tolerant of missing HF SDK fields
  - dataset_sweep._run_sweep_once: kill-switch + HF_TOKEN gating, per-user
    anchor (no fan-out across users), upstream deny-list
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

    @property
    def not_(self):
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
# dataset_sweep._students_with_hf_username (migration 030 anchor)
# ------------------------------------------------------------------
class TestStudentsWithHfUsername(unittest.TestCase):
    def test_keeps_only_rows_with_id_and_hf_username(self):
        from app.services import dataset_sweep as ds

        # The stub doesn't apply the `.not_.is_(hf_username, null)` filter, so
        # the code's own defensive filter (id AND hf_username truthy) is what
        # we exercise here.
        fake = FakeSupabase(
            {
                "users": [
                    [
                        {"id": "u1", "hf_username": "alice", "workgroup_id": "g1"},
                        {"id": "u2", "hf_username": None, "workgroup_id": None},
                        {"id": None, "hf_username": "bob", "workgroup_id": None},
                    ]
                ],
            }
        )
        rows = ds._students_with_hf_username(fake)
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, ["u1"])

    def test_scan_failure_returns_empty(self):
        from app.services import dataset_sweep as ds

        class _Boom:
            def table(self, _n):
                raise RuntimeError("column hf_username does not exist")

        self.assertEqual(ds._students_with_hf_username(_Boom()), [])


# ------------------------------------------------------------------
# dataset_sweep._extract_repo_id
# ------------------------------------------------------------------
class TestExtractRepoId(unittest.TestCase):
    def test_id_preferred(self):
        from app.services.dataset_sweep import _extract_repo_id

        obj = SimpleNamespace(id="alice/data-x", repo_id="ignored/ignored")
        self.assertEqual(_extract_repo_id(obj), "alice/data-x")

    def test_repo_id_fallback(self):
        from app.services.dataset_sweep import _extract_repo_id

        self.assertEqual(_extract_repo_id(SimpleNamespace(repo_id="alice/y")), "alice/y")

    def test_no_id_returns_none(self):
        from app.services.dataset_sweep import _extract_repo_id

        self.assertIsNone(_extract_repo_id(SimpleNamespace()))


# ------------------------------------------------------------------
# dataset_sweep._run_sweep_once (migration 030 per-user anchor)
# ------------------------------------------------------------------
class _SweepSupabase:
    """Supabase double for the sweep: serves a fixed users payload and a fixed
    'already registered' datasets payload, and records every datasets insert."""

    def __init__(self, users, registered=None):
        self._users = users
        self._registered = registered or []
        self.inserted = []

    def table(self, name):
        outer = self

        class _Q:
            def __init__(self):
                self._payload = None

            def select(self, *a, **k):
                return self

            def eq(self, *a, **k):
                return self

            @property
            def not_(self):
                return self

            def is_(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def insert(self, payload):
                self._payload = payload
                return self

            def execute(self):
                if self._payload is not None:
                    outer.inserted.append(self._payload)
                    return SimpleNamespace(data=[self._payload])
                if name == "users":
                    return SimpleNamespace(data=outer._users)
                if name == "datasets":
                    return SimpleNamespace(data=outer._registered)
                return SimpleNamespace(data=[])

        return _Q()


class TestRunSweepOnce(unittest.TestCase):
    def test_kill_switch_disables_tick(self):
        from app.services import dataset_sweep as ds

        with patch.dict(
            os.environ, {"DATASET_SWEEP_ENABLED": "0", "HF_TOKEN": "t"}, clear=False
        ):
            self.assertEqual(ds._run_sweep_once(), 0)

    def test_no_token_short_circuits(self):
        from app.services import dataset_sweep as ds

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HF_TOKEN", None)
            os.environ.pop("DATASET_SWEEP_ENABLED", None)
            self.assertEqual(ds._run_sweep_once(), 0)

    def test_no_students_no_calls(self):
        from app.services import dataset_sweep as ds
        import app.services.supabase_client  # noqa: F401

        with patch.dict(os.environ, {"HF_TOKEN": "t"}, clear=False), patch.object(
            ds, "_students_with_hf_username", return_value=[]
        ), patch(
            "app.services.supabase_client.get_supabase", return_value=_SweepSupabase([])
        ):
            self.assertEqual(ds._run_sweep_once(), 0)

    def test_per_user_anchor_registers_only_own_namespace(self):
        # The core migration-030 contract: a student anchored to "alice" gets
        # ONLY alice/* repos, attributed to THEM — no fan-out, no cross-author.
        from app.services import dataset_sweep as ds
        import app.services.supabase_client  # noqa: F401

        sb = _SweepSupabase(
            users=[{"id": "u1", "hf_username": "alice", "workgroup_id": "g1"}],
            registered=[],
        )

        class _HfApi:
            def __init__(self, *a, **k):
                pass

            def list_datasets(self, author=None, **k):
                return [
                    SimpleNamespace(id="alice/d1"),
                    SimpleNamespace(id="alice/d2"),
                    SimpleNamespace(id="bob/stolen"),  # cross-namespace → dropped
                ]

        with patch.dict(
            os.environ, {"HF_TOKEN": "t", "DATASET_SWEEP_ENABLED": "1"}, clear=False
        ), patch(
            "app.services.supabase_client.get_supabase", return_value=sb
        ), patch.object(sys.modules["huggingface_hub"], "HfApi", _HfApi):
            count = ds._run_sweep_once()

        self.assertEqual(count, 2)
        repos = sorted(p["hf_repo_id"] for p in sb.inserted)
        self.assertEqual(repos, ["alice/d1", "alice/d2"])
        self.assertTrue(all(p["owner_user_id"] == "u1" for p in sb.inserted))
        self.assertTrue(all(p["discovered_via_sweep"] for p in sb.inserted))

    def test_already_registered_repos_skipped(self):
        from app.services import dataset_sweep as ds
        import app.services.supabase_client  # noqa: F401

        sb = _SweepSupabase(
            users=[{"id": "u1", "hf_username": "alice", "workgroup_id": None}],
            registered=[{"hf_repo_id": "alice/d1"}],
        )

        class _HfApi:
            def __init__(self, *a, **k):
                pass

            def list_datasets(self, author=None, **k):
                return [SimpleNamespace(id="alice/d1"), SimpleNamespace(id="alice/d2")]

        with patch.dict(os.environ, {"HF_TOKEN": "t"}, clear=False), patch(
            "app.services.supabase_client.get_supabase", return_value=sb
        ), patch.object(sys.modules["huggingface_hub"], "HfApi", _HfApi):
            count = ds._run_sweep_once()

        self.assertEqual(count, 1)
        self.assertEqual([p["hf_repo_id"] for p in sb.inserted], ["alice/d2"])

    def test_deny_listed_author_never_enumerated(self):
        # A student whose hf_username is an upstream account (RobotisSW) must
        # NEVER be enumerated — the exact mass-import bug migration 030 fixes.
        from app.services import dataset_sweep as ds
        import app.services.supabase_client  # noqa: F401

        sb = _SweepSupabase(
            users=[{"id": "u9", "hf_username": "RobotisSW", "workgroup_id": None}]
        )
        list_calls = []

        class _HfApi:
            def __init__(self, *a, **k):
                pass

            def list_datasets(self, author=None, **k):
                list_calls.append(author)
                return [SimpleNamespace(id="RobotisSW/x")]

        with patch.dict(os.environ, {"HF_TOKEN": "t"}, clear=False), patch(
            "app.services.supabase_client.get_supabase", return_value=sb
        ), patch.object(sys.modules["huggingface_hub"], "HfApi", _HfApi):
            count = ds._run_sweep_once()

        self.assertEqual(count, 0)
        self.assertEqual(list_calls, [])  # list_datasets never called for denied author
        self.assertEqual(sb.inserted, [])


# ------------------------------------------------------------------
# Run
# ------------------------------------------------------------------
if __name__ == "__main__":
    unittest.main()
