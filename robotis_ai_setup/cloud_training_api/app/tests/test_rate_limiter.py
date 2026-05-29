"""Unit tests for app.services.rate_limiter.RateLimiter.

The limiter is pure stdlib (no fastapi / supabase), so these run without
the heavy app-import stubs the other cloud-API tests need.

Covers:
  - sliding-window limiting still works for a hot key
  - the window decays so an idle key recovers its budget
  - the LRU key-cap bounds memory under a forged-token / spoofed-IP flood
    (the audit fix) while keeping the most-recently-used keys
"""

from __future__ import annotations

import unittest

from app.services.rate_limiter import RateLimiter


class TestRateLimiterLimiting(unittest.TestCase):
    def test_hot_key_is_limited(self):
        rl = RateLimiter()
        self.assertTrue(rl.check("b", "k", limit=3, window_s=60.0))
        self.assertTrue(rl.check("b", "k", limit=3, window_s=60.0))
        self.assertTrue(rl.check("b", "k", limit=3, window_s=60.0))
        # 4th within the window is rejected.
        self.assertFalse(rl.check("b", "k", limit=3, window_s=60.0))

    def test_distinct_keys_have_independent_budgets(self):
        rl = RateLimiter()
        self.assertTrue(rl.check("b", "alice", limit=1, window_s=60.0))
        self.assertFalse(rl.check("b", "alice", limit=1, window_s=60.0))
        # bob is a different key → unaffected by alice exhausting hers.
        self.assertTrue(rl.check("b", "bob", limit=1, window_s=60.0))

    def test_window_decays(self):
        rl = RateLimiter()
        # window_s=0 means every prior timestamp is immediately stale, so
        # the budget never actually fills.
        self.assertTrue(rl.check("b", "k", limit=1, window_s=0.0))
        self.assertTrue(rl.check("b", "k", limit=1, window_s=0.0))


class TestRateLimiterMemoryCap(unittest.TestCase):
    def test_lru_caps_distinct_keys_per_bucket(self):
        rl = RateLimiter()
        rl._MAX_KEYS_PER_BUCKET = 5  # shrink for the test
        for i in range(100):
            # Each forged token / spoofed IP is a distinct key.
            rl.check("POST:/jetson/", f"jwt:{i}", limit=10, window_s=60.0)
        bucket = rl._buckets["POST:/jetson/"]
        self.assertLessEqual(len(bucket), 5)
        # The most-recently-seen keys are retained; the oldest evicted.
        self.assertIn("jwt:99", bucket)
        self.assertNotIn("jwt:0", bucket)

    def test_cap_does_not_break_active_key_limiting(self):
        # Even while a flood of one-shot keys churns through the LRU, a
        # repeatedly-hit key keeps being limited correctly because each
        # access moves it to the most-recently-used end (never evicted).
        rl = RateLimiter()
        rl._MAX_KEYS_PER_BUCKET = 3
        # Hot key consumes its budget of 2.
        self.assertTrue(rl.check("b", "hot", limit=2, window_s=60.0))
        self.assertTrue(rl.check("b", "hot", limit=2, window_s=60.0))
        # Flood with throwaway keys (would evict 'hot' if it weren't moved
        # to the MRU end on each access).
        for i in range(20):
            rl.check("b", f"junk{i}", limit=2, window_s=60.0)
            # Touch 'hot' again so it stays MRU; it should now be rejected.
            self.assertFalse(rl.check("b", "hot", limit=2, window_s=60.0))


if __name__ == "__main__":
    unittest.main()
