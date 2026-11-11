"""Unit tests for app.services.hf_identity (migration 030 anchor helpers).

Pure functions — no Supabase / HF / fastapi needed, so these run anywhere.
"""

from __future__ import annotations

import unittest

from app.services import hf_identity
from app.services.hf_identity import (
    HF_ORG,
    is_denied_author,
    is_org_repo,
    is_reserved_namespace,
    namespace_of,
    normalize_hf_username,
    note_platform_account,
    reserved_namespaces,
)


class TestNormalizeHfUsername(unittest.TestCase):
    def test_trims_and_accepts_valid(self):
        self.assertEqual(normalize_hf_username("  Alice  "), "Alice")
        self.assertEqual(normalize_hf_username("a"), "a")
        self.assertEqual(
            normalize_hf_username("EduBotics-Solutions"), "EduBotics-Solutions"
        )
        self.assertEqual(normalize_hf_username("user-123"), "user-123")
        self.assertEqual(normalize_hf_username("x" * 96), "x" * 96)  # max length

    def test_rejects_empty_none_and_nonstr(self):
        for v in (None, "", "   ", 123, [], {}):
            self.assertIsNone(normalize_hf_username(v))

    def test_rejects_malformed(self):
        for v in (
            "-leading",
            "trailing-",
            "has space",
            "has/slash",
            "x" * 97,  # too long
            "bad*char",
            "a..b",  # dots not allowed
            "ümlaut",
        ):
            self.assertIsNone(normalize_hf_username(v), v)

    def test_does_not_apply_denylist(self):
        # normalize is format-only; the deny-list is a separate check so callers
        # can give a distinct error message.
        self.assertEqual(normalize_hf_username("RobotisSW"), "RobotisSW")


class TestIsDeniedAuthor(unittest.TestCase):
    def test_known_upstream_denied_case_insensitive(self):
        for name in (
            "RobotisSW",
            "robotissw",
            "  RobotisSW  ",
            "Hotteok",
            "lerobot",
            "HuggingFace",
            "hf-internal-testing",
        ):
            self.assertTrue(is_denied_author(name), name)

    def test_student_names_allowed(self):
        for name in ("alice", "SvenBorodun", "klasse-7b", "EduBotics-Solutions-x"):
            self.assertFalse(is_denied_author(name), name)

    def test_the_organisation_is_denied_case_insensitive(self):
        # SA-08 (2026-11-13): the platform's organisation is never a student's
        # namespace, in any spelling.
        for name in ("EduBotics-Solutions", "edubotics-solutions",
                     "  EDUBOTICS-SOLUTIONS  "):
            self.assertTrue(is_denied_author(name), name)
            self.assertTrue(is_reserved_namespace(name), name)

    def test_the_platform_account_is_denied_once_noted(self):
        name = "plattform-konto-test"
        self.assertFalse(is_denied_author(name))
        try:
            self.assertTrue(note_platform_account("Plattform-Konto-Test"))
            self.assertFalse(note_platform_account("plattform-konto-test"))
            self.assertTrue(is_denied_author(name))
            self.assertTrue(is_denied_author("PLATTFORM-KONTO-TEST"))
            self.assertIn(name, reserved_namespaces())
        finally:
            with hf_identity._platform_lock:
                hf_identity._platform_accounts.discard(name)

    def test_a_malformed_platform_account_is_not_noted(self):
        for bad in (None, "", "a/b", "has space", 42):
            self.assertFalse(note_platform_account(bad))


class TestRepoHelpers(unittest.TestCase):
    def test_namespace_of(self):
        self.assertEqual(namespace_of("anna/wuerfel"), "anna")
        self.assertIsNone(namespace_of("ohne-schraegstrich"))
        self.assertIsNone(namespace_of(None))
        self.assertIsNone(namespace_of("/x"))

    def test_is_org_repo_case_insensitive(self):
        self.assertTrue(is_org_repo("EduBotics-Solutions/act-1"))
        self.assertTrue(is_org_repo("edubotics-solutions/act-1"))
        self.assertFalse(is_org_repo("anna/act-1"))
        self.assertFalse(is_org_repo("EduBotics-Solutions-x/act-1"))
        self.assertFalse(is_org_repo(None))

    def test_the_org_constant_is_the_dispatchers(self):
        # One constant: the managed training publishes into exactly this org.
        import ast
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "services", "training_dispatch.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        assigned = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "HF_ORG" for t in n.targets)]
        self.assertEqual(assigned, [], "training_dispatch must import HF_ORG, not define it")
        self.assertEqual(HF_ORG, "EduBotics-Solutions")

    def test_none_and_empty(self):
        self.assertFalse(is_denied_author(None))
        self.assertFalse(is_denied_author(""))


if __name__ == "__main__":
    unittest.main()
