"""Unit tests for app.services.training_sweep (migration 023 cancel-retry).

Audit fix M1: a cancel_requested row that reaches MAX_CANCEL_ATTEMPTS
without being flipped terminal — because the route (no cap) drove
cancel_attempts past it on repeat clicks, or the sweep crashed after its
optimistic pre-write — used to be excluded by the sweep's
`cancel_attempts < MAX` query filter and sat in cancel_requested forever,
never freeing its credit. The fix drops that filter; _retry_one's cap
check flips any at/over-cap row to 'failed'.

training_sweep only imports stdlib at module level (the modal +
supabase clients are local imports inside the functions), so we stub
those two before exercising the functions.
"""

from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


# Stub the two clients training_sweep imports lazily so we never pull the
# real `modal` / `supabase` packages.
if "app.services.modal_client" not in sys.modules:
    _mc = types.ModuleType("app.services.modal_client")
    _mc.cancel_training_job = AsyncMock()
    sys.modules["app.services.modal_client"] = _mc
if "app.services.supabase_client" not in sys.modules:
    _sc = types.ModuleType("app.services.supabase_client")
    _sc.get_supabase = lambda: None
    sys.modules["app.services.supabase_client"] = _sc

from app.services import training_sweep  # noqa: E402


class _RecordingQuery:
    """Records the PostgREST-style builder chain + any update payload."""

    def __init__(self, recorder, updates):
        self._rec = recorder
        self._updates = updates
        self._payload = None

    def select(self, *a, **k):
        self._rec.append(("select", a))
        return self

    def eq(self, *a, **k):
        self._rec.append(("eq", a))
        return self

    def lt(self, *a, **k):
        self._rec.append(("lt", a))
        return self

    def lte(self, *a, **k):
        self._rec.append(("lte", a))
        return self

    def order(self, *a, **k):
        self._rec.append(("order", a))
        return self

    def limit(self, *a, **k):
        self._rec.append(("limit", a))
        return self

    def update(self, payload):
        self._payload = payload
        return self

    def execute(self):
        if self._payload is not None:
            self._updates.append(self._payload)
        return SimpleNamespace(data=[])


class _FakeSupabase:
    def __init__(self):
        self.calls = []
        self.updates = []

    def table(self, _name):
        return _RecordingQuery(self.calls, self.updates)


class TestRetryOneCap(unittest.IsolatedAsyncioTestCase):
    async def _run(self, row, modal_raises):
        sb = _FakeSupabase()
        cancel = AsyncMock(
            side_effect=RuntimeError("modal down") if modal_raises else None
        )
        with patch.object(sys.modules["app.services.modal_client"],
                           "cancel_training_job", cancel):
            status = await training_sweep._retry_one(sb, row)
        return status, sb

    async def test_at_cap_modal_failure_flips_to_failed(self):
        # attempts=MAX-1 → this attempt is the MAX'th → must terminate.
        row = {"id": 7, "cloud_job_id": "fc-1",
               "cancel_attempts": training_sweep.MAX_CANCEL_ATTEMPTS - 1}
        status, sb = await self._run(row, modal_raises=True)
        self.assertEqual(status, "failed")
        self.assertEqual(sb.updates[-1]["status"], "failed")
        self.assertIsNone(sb.updates[-1]["worker_token"])

    async def test_over_cap_modal_failure_flips_to_failed(self):
        # The exact stranding case: a row driven past the cap (route spam
        # clicks) must still resolve to terminal, not loop forever.
        row = {"id": 8, "cloud_job_id": "fc-2",
               "cancel_attempts": training_sweep.MAX_CANCEL_ATTEMPTS + 3}
        status, sb = await self._run(row, modal_raises=True)
        self.assertEqual(status, "failed")
        self.assertEqual(sb.updates[-1]["status"], "failed")

    async def test_below_cap_modal_failure_stays_cancel_requested(self):
        row = {"id": 9, "cloud_job_id": "fc-3", "cancel_attempts": 0}
        status, sb = await self._run(row, modal_raises=True)
        self.assertEqual(status, "cancel_requested")
        # Only the optimistic pre-write happened; no terminal flip.
        self.assertNotIn("status", sb.updates[-1])

    async def test_modal_success_flips_to_canceled(self):
        row = {"id": 10, "cloud_job_id": "fc-4", "cancel_attempts": 1}
        status, sb = await self._run(row, modal_raises=False)
        self.assertEqual(status, "canceled")
        self.assertEqual(sb.updates[-1]["status"], "canceled")

    async def test_missing_cloud_job_id_short_circuits_to_canceled(self):
        row = {"id": 11, "cloud_job_id": None, "cancel_attempts": 2}
        status, sb = await self._run(row, modal_raises=True)
        self.assertEqual(status, "canceled")
        self.assertEqual(sb.updates[-1]["status"], "canceled")


class TestTickQuery(unittest.IsolatedAsyncioTestCase):
    async def test_tick_query_has_no_cancel_attempts_bound(self):
        sb = _FakeSupabase()
        with patch.object(sys.modules["app.services.supabase_client"],
                          "get_supabase", lambda: sb):
            processed = await training_sweep._tick()
        self.assertEqual(processed, 0)
        methods = [c[0] for c in sb.calls]
        # The fix: the sweep selects ALL cancel_requested rows. A
        # cancel_attempts upper bound (.lt / .lte) would re-strand over-cap
        # rows, so neither must appear.
        self.assertNotIn("lt", methods)
        self.assertNotIn("lte", methods)
        self.assertIn("eq", methods)  # still filters status=cancel_requested


if __name__ == "__main__":
    unittest.main()
