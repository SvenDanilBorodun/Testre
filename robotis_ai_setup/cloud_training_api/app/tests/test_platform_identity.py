"""SA-08 (2026-11-13): the platform token's own account is a reserved namespace.

``services/platform_identity.py`` learns it from the Hub's whoami of HF_TOKEN
(never from a client) and from migration 059's table, and records each new name
there. Everything is best effort: a missing table or a Hub that does not answer
leaves the in-process deny-list in force.
"""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import hf_credentials, hf_identity, platform_identity, training_dispatch
from app.tests.test_dataset_identity_routes import _ensure_stubs

APP = Path(__file__).resolve().parents[1]


class _Sb:
    def __init__(self, rows=(), broken=False):
        self.rows = list(rows)
        self.broken = broken
        self.upserts = []

    def table(self, name):
        assert name == "hf_reserved_namespaces", name
        outer = self
        q = SimpleNamespace()

        def _execute():
            if outer.broken:
                raise RuntimeError("relation does not exist")
            return SimpleNamespace(data=list(outer.rows))

        def _upsert(payload, on_conflict=None):
            def _ex():
                if outer.broken:
                    raise RuntimeError("relation does not exist")
                outer.upserts.append((payload, on_conflict))
                return SimpleNamespace(data=[payload])
            return SimpleNamespace(execute=_ex)

        q.select = lambda *a, **k: SimpleNamespace(execute=_execute)
        q.upsert = _upsert
        return q


class PlatformIdentity(unittest.TestCase):
    def setUp(self):
        _ensure_stubs()
        import app.services.supabase_client  # noqa: F401  (patched below)
        self._saved = set(hf_identity._platform_accounts)
        platform_identity._pending.clear()
        platform_identity._resolved = False
        self.addCleanup(self._restore)

    def _restore(self):
        with hf_identity._platform_lock:
            hf_identity._platform_accounts.clear()
            hf_identity._platform_accounts.update(self._saved)
        platform_identity._pending.clear()
        platform_identity._resolved = False

    def _sync(self, sb, whoami=None, token="hf_platform"):
        with patch("app.services.supabase_client.get_supabase", return_value=sb), \
                patch.object(training_dispatch, "_hf_whoami_body",
                             whoami or (lambda t: {"name": "Plattform-Bot"})), \
                patch.dict(os.environ, {"HF_TOKEN": token}):
            return platform_identity.sync_once()

    def test_whoami_reserves_and_records_the_account(self):
        sb = _Sb()
        self.assertTrue(self._sync(sb))
        self.assertTrue(hf_identity.is_denied_author("plattform-bot"))
        self.assertEqual(sb.upserts, [({"name_lower": "plattform-bot", "kind": "platform_account"},
                                       "name_lower")])
        # Learned once: a second round writes nothing new.
        self._sync(sb)
        self.assertEqual(len(sb.upserts), 1)

    def test_the_table_reserves_a_previous_boots_account_without_the_hub(self):
        sb = _Sb(rows=[{"name_lower": "alter-bot", "kind": "platform_account"},
                       {"name_lower": "edubotics-solutions", "kind": "organisation"}])

        def _down(_t):
            raise hf_credentials.HfCredentialError("unreachable")

        self.assertFalse(self._sync(sb, whoami=_down))
        self.assertTrue(hf_identity.is_denied_author("Alter-Bot"))
        self.assertEqual(sb.upserts, [])

    def test_a_database_without_059_keeps_the_in_process_list(self):
        sb = _Sb(broken=True)
        self.assertTrue(self._sync(sb))
        self.assertTrue(hf_identity.is_denied_author("plattform-bot"))
        # Not written yet: the next round tries again.
        self.assertEqual(platform_identity._pending, {"plattform-bot"})

    def test_no_platform_token_asks_nothing(self):
        calls = []
        self.assertFalse(self._sync(_Sb(), whoami=lambda t: calls.append(t) or {}, token=""))
        self.assertEqual(calls, [])

    def test_a_malformed_whoami_name_reserves_nothing(self):
        self._sync(_Sb(), whoami=lambda t: {"name": "a/b"})
        self.assertEqual(platform_identity._pending, set())

    def test_the_hf_write_check_reports_the_account_too(self):
        src = (APP / "services" / "training_dispatch.py").read_text(encoding="utf-8")
        self.assertIn('platform_identity.note(info.get("name"))', src)

    def test_the_loop_runs_off_the_event_loop_and_is_a_startup_task(self):
        tree = ast.parse((APP / "services" / "platform_identity.py").read_text(encoding="utf-8"))
        loop = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "platform_identity_loop")
        self.assertIn("asyncio.to_thread(sync_once)", ast.unparse(loop))
        main = (APP / "main.py").read_text(encoding="utf-8")
        self.assertIn("platform_identity.platform_identity_loop()", main)
        self.assertIn('PLATFORM_IDENTITY_DISABLED', main)

    def test_the_organisation_needs_no_whoami(self):
        self.assertTrue(hf_identity.is_reserved_namespace(hf_identity.HF_ORG))


class TheSeedEqualsTheConstant(unittest.TestCase):
    """Migration 059 seeds the organisation; it must be HF_ORG, lowercased."""

    def test_059_seeds_hf_org(self):
        mig = next((APP.parent.parent / "supabase" / "migrations").glob("*_059_hf_reserved_namespaces.sql"))
        text = mig.read_text(encoding="utf-8")
        self.assertIn(f"('{hf_identity.HF_ORG.lower()}', 'organisation')", text)


if __name__ == "__main__":
    unittest.main()
