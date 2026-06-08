"""Unit tests for app.services.hf_identity (migration 030 anchor helpers).

Pure functions — no Supabase / HF / fastapi needed, so these run anywhere.
"""

from __future__ import annotations

import unittest

from app.services.hf_identity import is_denied_author, normalize_hf_username


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

    def test_student_and_shared_names_allowed(self):
        # EduBotics-Solutions is the legitimate shared class account — NOT denied.
        for name in ("alice", "SvenBorodun", "EduBotics-Solutions", "klasse-7b"):
            self.assertFalse(is_denied_author(name), name)

    def test_none_and_empty(self):
        self.assertFalse(is_denied_author(None))
        self.assertFalse(is_denied_author(""))


if __name__ == "__main__":
    unittest.main()
