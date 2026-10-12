"""services/inference_sweep.py + inference_sessions.log_ended_sessions.

The session sweep (every 15 s): the `sweep_inference_sessions` RPC, then the
teacher run rows. It never waits for Modal: a terminate that hangs for a minute
must not move its period by a tick (F-2). Run rows are written only for sessions
the robot connected to (R-19), mark-then-insert, backend 'cloud', retention 50.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.services import inference_edge as edge  # noqa: E402
from app.services import inference_sessions as svc  # noqa: E402
from app.services import inference_sweep as sweep  # noqa: E402


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.ops = db, table, []

    def __getattr__(self, name):
        if name == "not_":
            self.ops.append(("not",))
            return self

        def op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self

        return op

    def execute(self):
        self.db.calls.append((self.table, self.ops))
        return self.db.answer(self.table, self.ops)


class _DB:
    def __init__(self, sessions, runs_before=0, insert_error=None):
        self.sessions = sessions
        self.calls = []
        self.inserted = []
        self.deleted = []
        self.runs_before = runs_before
        self.insert_error = insert_error
        self.rpc_calls = []

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, args):
        self.rpc_calls.append((name, args, time.monotonic()))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=3))

    def answer(self, table, ops):
        names = [o[0] for o in ops]
        if table == "inference_sessions" and names[0] == "select":
            return SimpleNamespace(data=[{"id": s["id"]} for s in self.sessions
                                         if s["status"] == "ended" and s.get("run_logged_at") is None])
        if table == "inference_sessions" and names[0] == "update":
            ids = next(o[1][1] for o in ops if o[0] == "in_")
            out = []
            for s in self.sessions:
                if s["id"] in ids and s.get("run_logged_at") is None:
                    s["run_logged_at"] = ops[0][1][0]["run_logged_at"]
                    out.append(dict(s))
            return SimpleNamespace(data=out)
        if table == "inference_runs" and names[0] == "insert":
            if self.insert_error:
                raise self.insert_error
            self.inserted.extend(ops[0][1][0])
            return SimpleNamespace(data=ops[0][1][0])
        if table == "inference_runs" and names[0] == "select":
            n = self.runs_before + len(self.inserted)
            return SimpleNamespace(data=[{"id": i} for i in range(min(n, 50))])
        if table == "inference_runs" and names[0] == "delete":
            self.deleted.append(ops)
            return SimpleNamespace(data=[])
        raise AssertionError((table, ops))


def _session(n, *, connected=True, reason="robot_stop", logged=None, status="ended"):
    return {
        "id": f"s{n}", "user_id": f"u{n % 2}", "classroom_id": "c-1", "status": status,
        "model_repo": "EduBotics-Solutions/m", "connected_at": "2026-10-11T10:00:00+00:00" if connected else None,
        "active_seconds": 42 if connected else 0, "end_reason": reason, "run_logged_at": logged,
    }


class TestLogEndedSessions(unittest.TestCase):
    def test_sweep_logs_only_connected_sessions(self) -> None:
        sessions = [
            _session(1, reason="user_stop"),
            _session(2, connected=False, reason="never_connected"),
            _session(3, reason="stall"),
            _session(4, connected=False, reason="edge_failed"),
            _session(5, reason="expired", logged="2026-10-11T09:00:00+00:00"),  # already logged
            _session(6, status="active", reason=None),  # not ended
        ]
        db = _DB(sessions)
        self.assertEqual(svc.log_ended_sessions(db), 2)
        # mark first, then insert: the update ran before the insert
        order = [(t, ops[0][0]) for t, ops in db.calls]
        self.assertLess(order.index(("inference_sessions", "update")), order.index(("inference_runs", "insert")))
        # the update is guarded by run_logged_at IS NULL (never twice)
        update_ops = next(ops for t, ops in db.calls if t == "inference_sessions" and ops[0][0] == "update")
        self.assertIn(("is_", ("run_logged_at", "null"), {}), update_ops)
        self.assertEqual(db.inserted, [
            {"student_user_id": "u1", "classroom_id": "c-1", "jetson_id": None,
             "policy_repo": "EduBotics-Solutions/m", "started_at": "2026-10-11T10:00:00+00:00", "duration_s": 42,
             "exit_reason": "stopped", "error_message_de": "", "backend": "cloud"},
            {"student_user_id": "u1", "classroom_id": "c-1", "jetson_id": None,
             "policy_repo": "EduBotics-Solutions/m", "started_at": "2026-10-11T10:00:00+00:00", "duration_s": 42,
             "exit_reason": "error", "error_message_de": "Die Verbindung zur Cloud war zu lange unterbrochen.",
             "backend": "cloud"},
        ])
        # never-connected rows are marked too (so they are never looked at again) but write nothing
        self.assertIsNotNone(sessions[1]["run_logged_at"])
        self.assertIsNotNone(sessions[3]["run_logged_at"])
        # a second run writes nothing
        self.assertEqual(svc.log_ended_sessions(db), 0)
        self.assertEqual(len(db.inserted), 2)

    def test_retention_keeps_the_newest_50(self) -> None:
        db = _DB([_session(1)], runs_before=60)
        svc.log_ended_sessions(db)
        self.assertEqual(len(db.deleted), 1)
        ops = db.deleted[0]
        self.assertIn(("eq", ("student_user_id", "u1"), {}), ops)
        self.assertIn(("not",), ops)

    def test_a_lost_insert_is_logged_and_never_duplicated(self) -> None:
        sessions = [_session(1)]
        db = _DB(sessions, insert_error=RuntimeError("db down"))
        with self.assertLogs(svc.logger, level="WARNING"):
            self.assertEqual(svc.log_ended_sessions(db), 0)
        db.insert_error = None
        self.assertEqual(svc.log_ended_sessions(db), 0)  # marked already: the row is dropped, not duplicated

    def test_nothing_to_do_is_one_query(self) -> None:
        db = _DB([])
        self.assertEqual(svc.log_ended_sessions(db), 0)
        self.assertEqual(len(db.calls), 1)


class TestSweepLoop(unittest.TestCase):
    def test_the_period_and_the_two_steps(self) -> None:
        self.assertEqual(sweep.SWEEP_PERIOD_S, 15.0)
        db = _DB([_session(1)])

        async def scenario():
            task = asyncio.ensure_future(sweep.sweep_loop(period_s=0.05, sb=db, initial_delay_s=0))
            await asyncio.sleep(0.18)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        self.assertGreaterEqual(len(db.rpc_calls), 3)
        self.assertTrue(all(name == "sweep_inference_sessions" and args == {} for name, args, _ in db.rpc_calls))
        self.assertEqual(len(db.inserted), 1)

    def test_a_failing_step_never_ends_the_loop(self) -> None:
        class _Broken:
            calls = 0

            def rpc(self, *a):
                _Broken.calls += 1
                raise RuntimeError("supabase down")

            def table(self, *a):
                raise RuntimeError("supabase down")

        async def scenario():
            task = asyncio.ensure_future(sweep.sweep_loop(period_s=0.03, sb=_Broken(), initial_delay_s=0))
            await asyncio.sleep(0.2)
            self.assertFalse(task.done())
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with self.assertLogs(sweep.logger, level="WARNING"):
            asyncio.run(scenario())
        self.assertGreaterEqual(_Broken.calls, 3)

    def test_blocked_terminate_never_delays_the_session_sweep(self) -> None:
        # F-2: a terminate that blocks (a minute in production) runs in the
        # terminate loop's own task; the sweep's two RPCs keep their period.
        gate = threading.Event()

        class _Server:
            class sessions:  # noqa: N801 - mimics the SDK attribute
                @staticmethod
                def terminate(token):
                    gate.wait(60)

        class _EdgeDB:
            def rpc(self, name, args):
                if name == "inference_edges_due":
                    rid = "00000000-0000-4000-8000-000000000001"
                    return SimpleNamespace(execute=lambda: SimpleNamespace(
                        data=[{"id": rid, "edge_token_enc": svc.edge_seal(rid, "edge-" + "e" * 30),
                               "ended_age_s": 20}]))
                return SimpleNamespace(execute=lambda: SimpleNamespace(data=True))

        db = _DB([])
        period = 0.1

        async def scenario():
            term = asyncio.ensure_future(edge.terminate_loop(_EdgeDB(), period_s=0.01))
            loop_task = asyncio.ensure_future(sweep.sweep_loop(period_s=period, sb=db, initial_delay_s=0))
            await asyncio.sleep(0.75)
            for t in (term, loop_task):
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        edge._reset_for_tests()
        try:
            with patch.object(edge, "_server", return_value=_Server()):
                asyncio.run(scenario())
        finally:
            gate.set()
            edge._reset_for_tests()
        stamps = [t for _, _, t in db.rpc_calls]
        self.assertGreaterEqual(len(stamps), 6)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertLess(max(gaps), period + 0.08, gaps)


if __name__ == "__main__":
    unittest.main()
