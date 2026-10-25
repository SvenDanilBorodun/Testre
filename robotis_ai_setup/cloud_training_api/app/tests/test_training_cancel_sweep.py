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


class _PgError(Exception):
    def __init__(self, code):
        super().__init__({"code": code, "message": "x"})
        self.code = code


class _FakeSupabase:
    """Table writes (the legacy optimistic pre-write) + the 049 RPCs, recorded."""

    def __init__(self, rpc_data=None, rpc_raises=None):
        self.calls = []
        self.updates = []
        self.rpcs = []
        self._rpc_data = rpc_data or {}
        self._rpc_raises = rpc_raises or {}

    def table(self, _name):
        return _RecordingQuery(self.calls, self.updates)

    def rpc(self, name, args):
        self.rpcs.append((name, dict(args)))
        outer = self

        class _R:
            def execute(self):
                if name in outer._rpc_raises:
                    raise outer._rpc_raises[name]
                return SimpleNamespace(data=outer._rpc_data.get(name))

        return _R()

    def completes(self):
        return [a for n, a in self.rpcs if n == "complete_cancel_training"]


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

    # The terminal flip is complete_cancel_training (049, §4.4): the one write
    # that sets status, terminated_at, worker_token NULL and the message, and
    # only while the row is still cancel_requested (its SQL is asserted in
    # supabase/tests/049_managed_training_assertions.sql).

    async def test_at_cap_modal_failure_flips_to_failed(self):
        # attempts=MAX-1 → this attempt is the MAX'th → must terminate.
        row = {"id": 7, "cloud_job_id": "fc-1",
               "cancel_attempts": training_sweep.MAX_CANCEL_ATTEMPTS - 1}
        status, sb = await self._run(row, modal_raises=True)
        self.assertEqual(status, "failed")
        self.assertEqual(sb.completes(), [{"p_training_id": 7, "p_final": "failed",
                                           "p_message": training_sweep.LEGACY_CANCEL_GIVEUP_DE}])

    async def test_over_cap_modal_failure_flips_to_failed(self):
        # The exact stranding case: a row driven past the cap (route spam
        # clicks) must still resolve to terminal, not loop forever.
        row = {"id": 8, "cloud_job_id": "fc-2",
               "cancel_attempts": training_sweep.MAX_CANCEL_ATTEMPTS + 3}
        status, sb = await self._run(row, modal_raises=True)
        self.assertEqual(status, "failed")
        self.assertEqual([c["p_final"] for c in sb.completes()], ["failed"])

    async def test_below_cap_modal_failure_stays_cancel_requested(self):
        row = {"id": 9, "cloud_job_id": "fc-3", "cancel_attempts": 0}
        status, sb = await self._run(row, modal_raises=True)
        self.assertEqual(status, "cancel_requested")
        # Only the optimistic pre-write happened; no terminal flip.
        self.assertNotIn("status", sb.updates[-1])
        self.assertEqual(sb.updates[-1]["cancel_attempts"], 1)
        self.assertEqual(sb.completes(), [])

    async def test_modal_success_flips_to_canceled(self):
        row = {"id": 10, "cloud_job_id": "fc-4", "cancel_attempts": 1}
        status, sb = await self._run(row, modal_raises=False)
        self.assertEqual(status, "canceled")
        self.assertEqual(sb.completes(), [{"p_training_id": 10, "p_final": "canceled", "p_message": None}])

    async def test_missing_cloud_job_id_short_circuits_to_canceled(self):
        row = {"id": 11, "cloud_job_id": None, "cancel_attempts": 2}
        status, sb = await self._run(row, modal_raises=True)
        self.assertEqual(status, "canceled")
        self.assertEqual([c["p_final"] for c in sb.completes()], ["canceled"])

    async def test_a_row_resolved_meanwhile_is_not_an_error(self):
        row = {"id": 12, "cloud_job_id": "fc-5", "cancel_attempts": 0}
        sb = _FakeSupabase(rpc_raises={"complete_cancel_training": _PgError("P0049")})
        with patch.object(sys.modules["app.services.modal_client"], "cancel_training_job", AsyncMock()):
            status = await training_sweep._retry_one(sb, row)
        self.assertEqual(status, "resolved")

    def test_the_legacy_giveup_text_is_unchanged(self):
        self.assertEqual(
            training_sweep.LEGACY_CANCEL_GIVEUP_DE,
            "Abbruch konnte nicht an Cloud-Worker zugestellt werden (Modal-Cancel nach 5 Versuchen "
            "weiterhin fehlgeschlagen). Credit wurde freigegeben — bitte Training neu starten.",
        )


class TestRetryOneManaged(unittest.IsolatedAsyncioTestCase):
    """049 (§4.4): a managed row's retry goes through cancel_request_training
    (it counts the attempt and names the CURRENT attempt), stops that attempt,
    then complete_cancel_training; S21 when the cap is reached (FR-22)."""

    async def _run(self, info, stopped=True, raises=None):
        from app.services import training_dispatch

        sb = _FakeSupabase(rpc_data={"cancel_request_training": [info]}, rpc_raises=raises or {})
        stop = AsyncMock(return_value=stopped)
        with patch.object(training_dispatch, "stop_for_cancel", stop):
            status = await training_sweep._retry_one(
                sb, {"id": 21, "user_id": "u-1", "managed": True, "cloud_job_id": None, "cancel_attempts": 0})
        return status, sb, stop

    async def test_a_stopped_attempt_completes_as_canceled(self):
        info = {"o_managed": True, "o_attempt_state": "running", "o_backend": "runpod_community",
                "o_cloud_ref": "pod-1", "o_cancel_attempts": 2}
        status, sb, stop = await self._run(info)
        self.assertEqual(status, "canceled")
        self.assertEqual(sb.rpcs[0], ("cancel_request_training", {"p_training_id": 21, "p_user_id": "u-1"}))
        stop.assert_awaited_once_with("runpod_community", "running", "pod-1")
        self.assertEqual(sb.completes(), [{"p_training_id": 21, "p_final": "canceled", "p_message": None}])
        self.assertEqual(sb.updates, [], "a managed row is never written directly")

    async def test_a_failed_stop_below_the_cap_waits(self):
        info = {"o_managed": True, "o_attempt_state": "running", "o_backend": "modal", "o_cloud_ref": "fc-1",
                "o_cancel_attempts": 2}
        status, sb, _ = await self._run(info, stopped=False)
        self.assertEqual(status, "cancel_requested")
        self.assertEqual(sb.completes(), [])

    async def test_the_cap_fails_the_row_with_s21(self):
        from app.services.training_texts_de import S21

        info = {"o_managed": True, "o_attempt_state": "starting", "o_backend": "runpod_secure",
                "o_cloud_ref": "pod-2", "o_cancel_attempts": training_sweep.MAX_CANCEL_ATTEMPTS}
        status, sb, _ = await self._run(info, stopped=False)
        self.assertEqual(status, "failed")
        self.assertEqual(sb.completes(), [{"p_training_id": 21, "p_final": "failed", "p_message": S21}])
        self.assertNotIn("Modal", S21)  # names no provider

    async def test_a_row_no_longer_active_is_resolved(self):
        status, sb, stop = await self._run({}, raises={"cancel_request_training": _PgError("P0050")})
        self.assertEqual(status, "resolved")
        stop.assert_not_awaited()
        self.assertEqual(sb.completes(), [])


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
        selected = [c[1][0] for c in sb.calls if c[0] == "select"][0]
        for col in ("managed", "user_id", "cloud_job_id", "cancel_attempts"):
            self.assertIn(col, selected)


if __name__ == "__main__":
    unittest.main()
