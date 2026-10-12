"""services/inference_edge.py — the ONLY Modal caller of the Cloud-Inferenz path.

Proves the pool and terminate rules of spec §4.3 (D33, D35, F-2, G-7) with a
fake Modal server (no `modal` import anywhere): a start still QUEUED at the
bound is cancelled and never runs; one RUNNING is never cancelled and its late
success is attached; the start pool can never starve a termination; the
terminate pass gives up on old rows, marks undecryptable ones, retries a failed
or queued call, never resubmits one still running, runs at most eight at once
and waits for each without cancelling it; the prefetch carries both tokens in
their headers and follows no redirect; nothing logs a token or an envelope.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import logging
import os
import sys
import threading
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.services import hf_credentials as hc  # noqa: E402
from app.services import inference_edge as edge  # noqa: E402
from app.services import inference_sessions as svc  # noqa: E402

KEY = base64.b64encode(bytes(range(32))).decode()
EDGE_TOK = "edge-" + "t" * 40
CONNECT_TOK = "c" * 43


def _id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


class _Keyed(unittest.TestCase):
    def setUp(self) -> None:
        self._p = patch.dict(os.environ, {hc.KEY_ENV: KEY})
        self._p.start()
        os.environ.pop(hc.PREVIOUS_KEY_ENV, None)
        hc._reset_for_tests()
        edge._reset_for_tests()

    def tearDown(self) -> None:
        self._p.stop()
        hc._reset_for_tests()
        edge._reset_for_tests()


class _FakeServer:
    def __init__(self, *, start=None, terminate=None, url="https://w--edubotics-inference-cloudinference.eu-west.modal.direct"):
        self.started = []
        self.terminated = []
        self._start = start
        self._terminate = terminate
        self._url = url
        self.url_calls = 0
        outer = self

        class _Sessions:
            def start(self, idle_timeout=600):
                outer.started.append(idle_timeout)
                if outer._start is not None:
                    return outer._start()
                return SimpleNamespace(session_id="sd-abc", token=EDGE_TOK)

            def terminate(self, token):
                outer.terminated.append(token)
                if outer._terminate is not None:
                    return outer._terminate(token)
                return None

        self.sessions = _Sessions()

    def get_url(self):
        self.url_calls += 1
        return self._url


class _DB:
    """Answers inference_edges_due with `rows` (once each pass) and records marks."""

    def __init__(self, rows=None, mark_error=None):
        self.rows = list(rows or [])
        self.marked = []
        self.due_args = []
        self.mark_error = mark_error

    def rpc(self, name, args):
        outer = self

        class _R:
            def execute(self):
                if name == "inference_edges_due":
                    outer.due_args.append(dict(args))
                    return SimpleNamespace(data=[r for r in outer.rows if r["id"] not in outer.marked])
                if name == "mark_inference_edge_terminated":
                    if outer.mark_error:
                        raise outer.mark_error
                    outer.marked.append(args["p_session_id"])
                    return SimpleNamespace(data=True)
                raise AssertionError(name)

        return _R()


def _row(n: int, *, age: int = 20, token: str = EDGE_TOK, enc: str | None = None):
    rid = _id(n)
    return {"id": rid, "edge_token_enc": enc if enc is not None else svc.edge_seal(rid, token), "ended_age_s": age}


# ------------------------------------------------------------- constants


class TestConstants(unittest.TestCase):
    def test_the_spec_values(self) -> None:
        self.assertEqual(edge.APP, "edubotics-inference")
        self.assertEqual(edge.CLS, "CloudInference")
        self.assertEqual((edge.START_TIMEOUT_S, edge.PREFETCH_TIMEOUT_S, edge.URL_TTL_S), (120.0, 120.0, 600.0))
        self.assertEqual(edge.TERMINATE_AFTER_S, 15)
        self.assertEqual((edge.TERMINATE_PERIOD_S, edge.TERMINATE_BATCH, edge.TERMINATE_CONCURRENCY,
                          edge.TERMINATE_CALL_WAIT_S), (15.0, 32, 8, 20.0))
        self.assertEqual((edge.EDGE_AUTH_HEADER, edge.CONNECT_AUTH_HEADER), ("Modal-Authorization", "Authorization"))
        self.assertEqual(edge.PREFETCH_PATH, "/v1/prefetch")

    def test_start_and_control_pools_are_separate_and_sized(self) -> None:
        self.assertIsNot(edge.START_POOL, edge.CONTROL_POOL)
        self.assertEqual(edge.START_POOL._max_workers, 64)
        self.assertEqual(edge.CONTROL_POOL._max_workers, 8)
        # G-7: every waiting start holds one connection of Modal's shared connector (limit 100).
        self.assertLessEqual(edge.START_POOL._max_workers + edge.CONTROL_POOL._max_workers, 90)
        self.assertGreaterEqual(edge.START_POOL._max_workers, svc.GLOBAL_CAP)

    def test_modal_is_imported_inside_functions_only(self) -> None:
        import ast

        path = edge.__file__
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertNotIn("modal", [a.name for a in node.names])
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual((node.module or "").split(".")[0], "modal")


# ----------------------------------------------------------------- starts


class TestStartSession(_Keyed):
    def test_a_quick_start_returns_the_credentials(self) -> None:
        fake = _FakeServer()
        with patch.object(edge, "_server", return_value=fake):
            creds = asyncio.run(edge.start_session("u1", self._never))
        self.assertEqual(creds.session_id, "sd-abc")
        self.assertEqual(fake.started, [svc.EDGE_IDLE_S])

    async def _never(self, _creds):  # pragma: no cover - must not run
        raise AssertionError("on_late must not run for an in-time start")

    def test_queued_start_is_cancelled_on_timeout_and_never_runs(self) -> None:
        gate = threading.Event()
        calls = []

        def slow_start():
            calls.append(time.monotonic())
            gate.wait(5)
            return SimpleNamespace(session_id="sd-first", token=EDGE_TOK)

        fake = _FakeServer(start=slow_start)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        late = []

        async def on_late(creds):
            late.append(creds.session_id)

        async def scenario():
            first = asyncio.ensure_future(edge.start_session("u-first", on_late))
            await asyncio.sleep(0.05)  # the first occupies the only worker
            with self.assertRaises(edge.EdgeTimeout) as ctx:
                await edge.start_session("u-second", on_late)  # another student: a start of its own
            self.assertEqual(ctx.exception.args[0], "queued")
            self.assertNotIn("u-second", edge._STARTS)  # a cancelled start is never joined
            with self.assertRaises(edge.EdgeTimeout) as ctx:
                await first  # the first is still RUNNING at its own bound
            self.assertEqual(ctx.exception.args[0], "running")
            gate.set()
            for _ in range(100):
                if late:
                    break
                await asyncio.sleep(0.02)

        try:
            with patch.object(edge, "_server", return_value=fake), patch.object(edge, "START_POOL", pool), \
                    patch.object(edge, "START_TIMEOUT_S", 0.3):
                asyncio.run(scenario())
        finally:
            gate.set()
            pool.shutdown(wait=True)
        self.assertEqual(late, ["sd-first"])  # only the running one arrives late
        self.assertEqual(len(calls), 1)  # the queued one never ran

    async def _noop(self, _creds):
        return None

    def test_running_start_late_success_is_attached(self) -> None:
        late = []

        def slow_start():
            time.sleep(0.4)
            return SimpleNamespace(session_id="sd-late", token=EDGE_TOK)

        async def on_late(creds):
            late.append(creds.session_id)

        async def scenario():
            t0 = time.monotonic()
            with self.assertRaises(edge.EdgeTimeout) as ctx:
                await edge.start_session("u1", on_late)
            self.assertEqual(ctx.exception.args[0], "running")
            self.assertLess(time.monotonic() - t0, 0.35)
            for _ in range(100):
                if late:
                    break
                await asyncio.sleep(0.02)

        fake = _FakeServer(start=slow_start)
        with patch.object(edge, "_server", return_value=fake), patch.object(edge, "START_TIMEOUT_S", 0.15):
            asyncio.run(scenario())
        self.assertEqual(late, ["sd-late"])

    def test_late_attach_task_is_kept_in_the_module_task_set(self) -> None:
        # G-7: the loop holds tasks weakly; the late attach must be strongly held.
        release = threading.Event()
        seen_in_set = []

        def slow_start():
            time.sleep(0.25)
            return SimpleNamespace(session_id="sd-late", token=EDGE_TOK)

        async def on_late(_creds):
            seen_in_set.append(asyncio.current_task() in edge._TASKS)
            await asyncio.to_thread(release.wait, 2)

        async def scenario():
            with self.assertRaises(edge.EdgeTimeout):
                await edge.start_session("u1", on_late)
            for _ in range(100):
                if seen_in_set:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(edge._TASKS), 1)
            release.set()
            for _ in range(100):
                if not edge._TASKS:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(edge._TASKS), 0)

        fake = _FakeServer(start=slow_start)
        with patch.object(edge, "_server", return_value=fake), patch.object(edge, "START_TIMEOUT_S", 0.1):
            asyncio.run(scenario())
        self.assertEqual(seen_in_set, [True])

    def test_a_late_failure_attaches_nothing(self) -> None:
        late = []

        def failing():
            time.sleep(0.3)
            raise RuntimeError("no capacity")

        async def on_late(creds):
            late.append(creds)

        async def scenario():
            with self.assertRaises(edge.EdgeTimeout):
                await edge.start_session("u1", on_late)
            await asyncio.sleep(0.5)

        with patch.object(edge, "_server", return_value=_FakeServer(start=failing)), \
                patch.object(edge, "START_TIMEOUT_S", 0.1):
            asyncio.run(scenario())
        self.assertEqual(late, [])

    def test_an_sdk_error_propagates(self) -> None:
        def broken():
            raise RuntimeError("lookup failed")

        with patch.object(edge, "_server", return_value=_FakeServer(start=broken)):
            with self.assertRaises(RuntimeError):
                asyncio.run(edge.start_session("u1", self._noop))

    def test_start_and_control_pools_are_separate(self) -> None:
        # 64 starts blocked in the start pool never starve terminate or get_url.
        gate = threading.Event()

        def blocked():
            gate.wait(10)
            return SimpleNamespace(session_id="sd-x", token=EDGE_TOK)

        fake = _FakeServer(start=blocked)
        futures = []
        try:
            with patch.object(edge, "_server", return_value=fake):
                futures = [edge.START_POOL.submit(fake.sessions.start) for _ in range(64)]
                time.sleep(0.1)

                async def scenario():
                    t0 = time.monotonic()
                    ok = await edge.terminate_now(EDGE_TOK)
                    url = await edge.server_url()
                    return ok, url, time.monotonic() - t0

                ok, url, took = asyncio.run(scenario())
        finally:
            gate.set()
            for f in futures:
                f.result(timeout=5)
        self.assertTrue(ok)
        self.assertTrue(url.startswith("https://"))
        self.assertLess(took, 1.0)
        self.assertEqual(fake.terminated, [EDGE_TOK])


# ------------------------------------------------------------ joined starts


class _Gated:
    """A sessions.start that blocks until `gate` is set (a GPU shortage) and answers
    credentials of its own per call (sd-1, sd-2, ...), or raises `error`."""

    def __init__(self, error=None):
        self.gate = threading.Event()
        self.error = error
        self.n = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.n += 1
            n = self.n
        self.gate.wait(10)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(session_id=f"sd-{n}", token=EDGE_TOK)


class TestJoinedStarts(_Keyed):
    """Owner, round 3 (R1 MINOR): the page retries a `gpu_not_started` reserve up to
    twice, and a timed-out start keeps running (it cannot be cancelled), so a student
    once held up to three START_POOL threads and three GPU starts. A reservation that
    arrives while a start launched for one of the SAME student's earlier reservations
    still runs now waits on that start instead of submitting another; its success goes
    to the newest reservation still waiting, else (nobody waits) to the newest
    reservation's late path, exactly once."""

    def setUp(self) -> None:
        super().setUp()
        self.late: list[tuple[str, str]] = []
        self.start = _Gated()
        self._patches = [patch.object(edge, "_server", return_value=_FakeServer(start=self.start))]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        self.start.gate.set()
        for p in reversed(self._patches):
            p.stop()
        super().tearDown()

    def on_late(self, tag):
        async def record(creds):
            self.late.append((tag, creds.session_id))

        return record

    async def until(self, cond, tries=300):
        for _ in range(tries):
            if cond():
                return
            await asyncio.sleep(0.01)

    def run_with(self, scenario, timeout_s):
        with patch.object(edge, "START_TIMEOUT_S", timeout_s):
            return asyncio.run(scenario())

    def test_a_retry_joins_the_running_start_and_takes_its_success(self) -> None:
        async def scenario():
            with self.assertRaises(edge.EdgeTimeout) as ctx:
                await edge.start_session("u1", self.on_late("r1"))
            self.assertEqual(ctx.exception.args[0], "running")
            retry = asyncio.ensure_future(edge.start_session("u1", self.on_late("r2")))
            await asyncio.sleep(0.05)
            self.assertFalse(retry.done())
            self.start.gate.set()
            creds = await retry
            await asyncio.sleep(0.05)
            return creds

        creds = self.run_with(scenario, 0.25)
        self.assertEqual(creds.session_id, "sd-1")
        self.assertEqual(self.start.n, 1)   # the retry submitted no second sessions.start
        self.assertEqual(self.late, [])     # handed to the waiting retry, never ALSO to a late path
        self.assertEqual(edge._STARTS, {})  # the next reservation starts afresh

    def test_a_success_after_every_attempt_timed_out_goes_to_the_newest_reservation_once(self) -> None:
        async def scenario():
            for tag in ("r1", "r2", "r3"):
                with self.assertRaises(edge.EdgeTimeout) as ctx:
                    await edge.start_session("u1", self.on_late(tag))
                self.assertEqual(ctx.exception.args[0], "running")
            self.start.gate.set()
            await self.until(lambda: self.late)
            await asyncio.sleep(0.1)

        self.run_with(scenario, 0.1)
        self.assertEqual(self.start.n, 1)
        self.assertEqual(self.late, [("r3", "sd-1")])

    def test_a_newer_reservation_takes_over_and_the_older_request_is_released_at_once(self) -> None:
        # e.g. the student picked another model while the first one's GPU started:
        # a Modal session is not bound to a model, the newer reservation takes it.
        async def scenario():
            first = asyncio.ensure_future(edge.start_session("u1", self.on_late("r1")))
            await asyncio.sleep(0.05)
            second = asyncio.ensure_future(edge.start_session("u1", self.on_late("r2")))
            await self.until(first.done)
            with self.assertRaises(edge.StartSuperseded):
                first.result()
            self.assertFalse(second.done())
            self.start.gate.set()
            creds = await second
            await asyncio.sleep(0.05)
            return creds

        creds = self.run_with(scenario, 2.0)
        self.assertEqual(creds.session_id, "sd-1")
        self.assertEqual(self.start.n, 1)
        self.assertEqual(self.late, [])

    def test_two_students_never_share_a_start(self) -> None:
        async def scenario():
            a = asyncio.ensure_future(edge.start_session("ua", self.on_late("a")))
            b = asyncio.ensure_future(edge.start_session("ub", self.on_late("b")))
            await self.until(lambda: self.start.n == 2)
            self.assertFalse(a.done() or b.done())
            self.start.gate.set()
            return await a, await b

        ca, cb = self.run_with(scenario, 2.0)
        self.assertEqual(self.start.n, 2)
        self.assertEqual({ca.session_id, cb.session_id}, {"sd-1", "sd-2"})
        self.assertEqual(self.late, [])

    def test_a_failure_of_a_joined_start_reaches_the_waiting_retry_and_nothing_arrives_late(self) -> None:
        class AuthError(Exception):
            pass

        self.start.error = AuthError("token rejected")

        async def scenario():
            with self.assertRaises(edge.EdgeTimeout):
                await edge.start_session("u1", self.on_late("r1"))
            retry = asyncio.ensure_future(edge.start_session("u1", self.on_late("r2")))
            await asyncio.sleep(0.05)
            self.start.gate.set()
            with self.assertRaises(AuthError):  # the SDK's own exception: permanent, not a timeout
                await retry
            await asyncio.sleep(0.1)

        self.run_with(scenario, 0.25)
        self.assertEqual(self.start.n, 1)
        self.assertEqual(self.late, [])
        self.assertEqual(edge._STARTS, {})

    def test_a_request_that_goes_away_leaves_its_success_to_its_late_path(self) -> None:
        async def scenario():
            waiting = asyncio.ensure_future(edge.start_session("u1", self.on_late("r1")))
            await asyncio.sleep(0.05)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.start.gate.set()
            await self.until(lambda: self.late)
            await asyncio.sleep(0.05)

        self.run_with(scenario, 2.0)
        self.assertEqual(self.late, [("r1", "sd-1")])

    def test_a_success_already_handed_to_a_request_that_goes_away_is_not_lost(self) -> None:
        # The success is delivered, then the request is cancelled before it could read it.
        real_settle = edge._settle
        holder = {}

        def settle_then_cancel(entry):
            real_settle(entry)
            holder["task"].cancel()

        async def scenario():
            holder["task"] = asyncio.ensure_future(edge.start_session("u1", self.on_late("r1")))
            await asyncio.sleep(0.05)
            self.start.gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await holder["task"]
            await self.until(lambda: self.late)
            await asyncio.sleep(0.05)

        with patch.object(edge, "_settle", settle_then_cancel):
            self.run_with(scenario, 2.0)
        self.assertEqual(self.late, [("r1", "sd-1")])

    def test_a_queued_start_nobody_waits_for_is_cancelled_and_the_retry_starts_its_own(self) -> None:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        other = threading.Event()
        blocker = pool.submit(other.wait, 5)  # the only worker is busy with someone else's start
        self.start.gate.set()                 # no shortage once a worker is free

        async def scenario():
            with self.assertRaises(edge.EdgeTimeout) as ctx:
                await edge.start_session("u1", self.on_late("r1"))
            self.assertEqual(ctx.exception.args[0], "queued")
            self.assertNotIn("u1", edge._STARTS)
            other.set()
            return await edge.start_session("u1", self.on_late("r2"))

        try:
            with patch.object(edge, "START_POOL", pool):
                creds = self.run_with(scenario, 0.2)
        finally:
            other.set()
            blocker.result(timeout=5)
            pool.shutdown(wait=True)
        self.assertEqual(creds.session_id, "sd-1")  # the cancelled one never ran
        self.assertEqual(self.start.n, 1)
        self.assertEqual(self.late, [])

    def test_a_start_presumed_hung_is_not_joined(self) -> None:
        # Beyond the SDK's own ~25 min bound a start is presumed hung: a new reservation
        # starts its own, so one hung start cannot lock its student out for good.
        self.assertEqual(edge.START_JOIN_MAX_AGE_S, 1800.0)
        self.assertGreater(edge.START_JOIN_MAX_AGE_S, 25 * 60)

        async def scenario():
            with self.assertRaises(edge.EdgeTimeout):
                await edge.start_session("u1", self.on_late("r1"))
            with patch.object(edge, "START_JOIN_MAX_AGE_S", 0.0):
                fresh = asyncio.ensure_future(edge.start_session("u1", self.on_late("r2")))
                await self.until(lambda: self.start.n == 2)
            self.start.gate.set()
            creds = await fresh
            await self.until(lambda: self.late)
            await asyncio.sleep(0.05)
            return creds

        creds = self.run_with(scenario, 0.25)
        self.assertEqual(self.start.n, 2)
        self.assertEqual(creds.session_id, "sd-2")
        self.assertEqual(self.late, [("r1", "sd-1")])  # the hung one, if it ever ends, is terminated

    def test_a_class_retrying_in_a_gpu_shortage_never_fills_the_start_pool(self) -> None:
        """R1's simulation, scaled to the real pool: 30 students each make the page's
        3 attempts (RESERVE_ATTEMPTS) while every start hangs. Before, each attempt
        held a thread of its own (90 > 64) and a student of another class, whose start
        would have been instant, was queued and answered `gpu_not_started`. Now the
        class holds 30 threads and that student gets a GPU."""
        class_size, attempts = 30, 3
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=edge.START_POOL._max_workers)
        outsider_turn = threading.Event()
        calls = []
        lock = threading.Lock()

        def start():
            with lock:
                calls.append(outsider_turn.is_set())
                n = len(calls)
            if not outsider_turn.is_set():
                self.start.gate.wait(10)  # the shortage
            return SimpleNamespace(session_id=f"sd-{n}", token=EDGE_TOK)

        timeouts = []

        async def student(i):
            for attempt in range(1, attempts + 1):
                try:
                    await edge.start_session(f"student-{i}", self.on_late(f"student-{i}/{attempt}"))
                except edge.EdgeTimeout as exc:
                    timeouts.append(exc.args[0])

        async def scenario():
            await asyncio.gather(*(student(i) for i in range(class_size)))
            class_calls, joined = len(calls), len(edge._STARTS)
            outsider_turn.set()
            try:
                outsider = await edge.start_session("outsider", self.on_late("outsider"))
            except edge.EdgeTimeout as exc:  # before: 'queued', the pool was full
                outsider = exc
            self.start.gate.set()  # the shortage ends: every class start succeeds late
            await self.until(lambda: len(self.late) >= class_size)
            await asyncio.sleep(0.1)
            return outsider, class_calls, joined

        try:
            with patch.object(edge, "_server", return_value=_FakeServer(start=start)), \
                    patch.object(edge, "START_POOL", pool):
                outsider, class_calls, joined = self.run_with(scenario, 0.15)
        finally:
            self.start.gate.set()
            pool.shutdown(wait=True)
        self.assertNotIsInstance(outsider, edge.EdgeTimeout)  # the other class's student got a GPU
        self.assertEqual(outsider.token, EDGE_TOK)
        self.assertEqual(class_calls, class_size)  # one sessions.start per student, not one per attempt
        self.assertEqual(joined, class_size)
        self.assertEqual(timeouts, ["running"] * (class_size * attempts))
        self.assertEqual(calls, [False] * class_size + [True])
        # each student's success went once, to their newest reservation
        self.assertEqual(sorted(tag for tag, _ in self.late),
                         sorted(f"student-{i}/{attempts}" for i in range(class_size)))
        self.assertEqual(len({sid for _, sid in self.late}), class_size)


# -------------------------------------------------------------- terminate


class TestTerminate(_Keyed):
    def _pass(self, db, fake, **patches):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(edge, "_server", return_value=fake))
            for name, value in patches.items():
                stack.enter_context(patch.object(edge, name, value))
            return asyncio.run(edge.terminate_pass(db))

    def test_terminate_due_min_age_give_up_undecryptable_and_retry(self) -> None:
        good = _row(1, age=20)
        old = _row(2, age=svc.EDGE_IDLE_S + 61)
        garbage = _row(3, enc="e1.00000000." + "A" * 30)

        def flaky(token):
            if flaky.calls == 0:
                flaky.calls += 1
                raise RuntimeError("modal control plane down")
            flaky.calls += 1

        flaky.calls = 0
        fake = _FakeServer(terminate=flaky)
        db = _DB([good, old, garbage])
        with self.assertLogs(edge.logger, level="WARNING") as logs:
            stats = self._pass(db, fake)
        self.assertEqual(db.due_args, [{"p_min_age_s": edge.TERMINATE_AFTER_S, "p_limit": edge.TERMINATE_BATCH}])
        # the old row was given up on WITHOUT a call; the garbage one is marked and named
        self.assertEqual(sorted(db.marked), sorted([old["id"], garbage["id"]]))
        self.assertEqual(fake.terminated, [EDGE_TOK])  # one call, for the good row; it failed
        self.assertEqual(stats["failed"], 1)
        self.assertTrue(any("undecryptable" in line and garbage["id"] in line for line in logs.output))
        # the next pass retries the failed one and marks it
        stats = self._pass(db, fake)
        self.assertIn(good["id"], db.marked)
        self.assertEqual(stats["terminated"], 1)
        self.assertEqual(fake.terminated, [EDGE_TOK, EDGE_TOK])

    def test_terminate_pass_single_flight(self) -> None:
        # the same id twice in one answer is handled once
        row = _row(1)
        db = _DB([row, dict(row)])
        fake = _FakeServer()
        stats = self._pass(db, fake)
        self.assertEqual(fake.terminated, [EDGE_TOK])
        self.assertEqual(db.marked, [row["id"]])
        self.assertEqual(stats["skipped"], 1)

    def test_terminate_concurrency_8_and_per_call_wait_20_s(self) -> None:
        self.assertEqual((edge.TERMINATE_CONCURRENCY, edge.TERMINATE_CALL_WAIT_S), (8, 20.0))
        active = [0]
        peak = [0]
        lock = threading.Lock()

        def slow(token):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1

        rows = [_row(i) for i in range(1, 21)]
        db = _DB(rows)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=32)
        try:
            stats = self._pass(db, _FakeServer(terminate=slow), CONTROL_POOL=pool)
        finally:
            pool.shutdown(wait=True)
        self.assertEqual(peak[0], 8)
        self.assertEqual(stats["terminated"], 20)
        self.assertEqual(len(db.marked), 20)

    def test_the_per_call_wait_does_not_cancel_a_running_call(self) -> None:
        gate = threading.Event()
        finished = []

        def hangs(token):
            gate.wait(5)
            finished.append(token)

        row = _row(1)
        db = _DB([row])
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            t0 = time.monotonic()
            stats = self._pass(db, _FakeServer(terminate=hangs), CONTROL_POOL=pool, TERMINATE_CALL_WAIT_S=0.2)
            took = time.monotonic() - t0
            self.assertLess(took, 1.0)
            self.assertEqual(stats["running"], 1)
            self.assertEqual(db.marked, [])
            self.assertIn(row["id"], edge._in_flight_ids())
            gate.set()
            pool.shutdown(wait=True)
            self.assertEqual(finished, [EDGE_TOK])  # it ran to its end
            self.assertNotIn(row["id"], edge._in_flight_ids())
        finally:
            gate.set()

    def test_queued_terminate_is_cancelled_and_retried(self) -> None:
        gate = threading.Event()
        calls = []

        def hangs(token):
            calls.append(token)
            gate.wait(5)

        rows = [_row(1), _row(2)]
        db = _DB(rows)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            stats = self._pass(db, _FakeServer(terminate=hangs), CONTROL_POOL=pool, TERMINATE_CALL_WAIT_S=0.2)
            self.assertEqual(stats["running"], 1)
            self.assertEqual(stats["queued"], 1)
            self.assertEqual(len(calls), 1)  # the queued call was cancelled before it ran
            gate.set()
            pool.shutdown(wait=True)
        finally:
            gate.set()
        pool2 = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            stats = self._pass(db, _FakeServer(), CONTROL_POOL=pool2)
        finally:
            pool2.shutdown(wait=True)
        self.assertEqual(stats["terminated"], 2)
        self.assertEqual(sorted(db.marked), sorted(r["id"] for r in rows))

    def test_in_flight_termination_not_resubmitted(self) -> None:
        gate = threading.Event()
        calls = []

        def hangs(token):
            calls.append(token)
            gate.wait(5)

        row = _row(1)
        db = _DB([row])
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            self._pass(db, _FakeServer(terminate=hangs), CONTROL_POOL=pool, TERMINATE_CALL_WAIT_S=0.1)
            stats = self._pass(db, _FakeServer(terminate=hangs), CONTROL_POOL=pool, TERMINATE_CALL_WAIT_S=0.1)
            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(len(calls), 1)
        finally:
            gate.set()
            pool.shutdown(wait=True)

    def test_a_failed_mark_is_retried_next_pass(self) -> None:
        db = _DB([_row(1)], mark_error=RuntimeError("db down"))
        stats = self._pass(db, _FakeServer())
        self.assertEqual(stats["terminated"], 0)
        db.mark_error = None
        self._pass(db, _FakeServer())
        self.assertEqual(len(db.marked), 1)

    def test_terminate_now_only_for_a_string(self) -> None:
        self.assertFalse(asyncio.run(edge.terminate_now(None)))
        fake = _FakeServer(terminate=lambda t: (_ for _ in ()).throw(RuntimeError("x")))
        with patch.object(edge, "_server", return_value=fake):
            self.assertFalse(asyncio.run(edge.terminate_now(EDGE_TOK)))


class TestLoopAndKick(_Keyed):
    def test_kick_wakes_the_loop_before_its_period(self) -> None:
        passes = []

        async def fake_pass(sb):
            passes.append(time.monotonic())

        async def scenario():
            task = asyncio.ensure_future(edge.terminate_loop(object(), period_s=30.0))
            await asyncio.sleep(0.05)
            t0 = time.monotonic()
            edge.kick()
            for _ in range(50):
                if passes:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return t0

        with patch.object(edge, "terminate_pass", fake_pass):
            t0 = asyncio.run(scenario())
        self.assertEqual(len(passes), 1)
        self.assertLess(passes[0] - t0, 0.5)

    def test_kick_from_another_thread_and_without_a_loop_never_blocks(self) -> None:
        edge._reset_for_tests()
        edge.kick()  # no loop yet: a no-op
        passes = []

        async def fake_pass(sb):
            passes.append(1)

        async def scenario():
            task = asyncio.ensure_future(edge.terminate_loop(object(), period_s=30.0))
            await asyncio.sleep(0.05)
            await asyncio.to_thread(edge.kick)
            for _ in range(50):
                if passes:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with patch.object(edge, "terminate_pass", fake_pass):
            asyncio.run(scenario())
        self.assertEqual(passes, [1])

    def test_a_failing_pass_never_ends_the_loop(self) -> None:
        passes = []

        async def broken(sb):
            passes.append(1)
            raise RuntimeError("supabase down")

        async def scenario():
            task = asyncio.ensure_future(edge.terminate_loop(object(), period_s=0.05))
            await asyncio.sleep(0.3)
            self.assertFalse(task.done())
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with patch.object(edge, "terminate_pass", broken):
            asyncio.run(scenario())
        self.assertGreaterEqual(len(passes), 3)


# ------------------------------------------------------------ url + prefetch


def _fake_httpx(seen, *, status=204, error=None):
    mod = types.ModuleType("httpx")

    class AsyncClient:
        def __init__(self, timeout=None, **kw):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, follow_redirects=None, **kw):
            seen["url"] = url
            seen["headers"] = dict(headers or {})
            seen["follow_redirects"] = follow_redirects
            seen["content"] = kw.get("content")
            if error is not None:
                raise error
            return SimpleNamespace(status_code=status)

    mod.AsyncClient = AsyncClient
    return mod


class TestUrlAndPrefetch(_Keyed):
    def test_server_url_is_cached(self) -> None:
        fake = _FakeServer()
        clock = [100.0]
        with patch.object(edge, "_server", return_value=fake), patch.object(edge.time, "monotonic", lambda: clock[0]):
            self.assertEqual(asyncio.run(edge.server_url()), fake._url)
            asyncio.run(edge.server_url())
            self.assertEqual(fake.url_calls, 1)
            clock[0] += edge.URL_TTL_S + 1
            asyncio.run(edge.server_url())
            self.assertEqual(fake.url_calls, 2)

    def test_a_non_https_url_is_refused(self) -> None:
        with patch.object(edge, "_server", return_value=_FakeServer(url="http://plain.example")):
            self.assertIsNone(asyncio.run(edge.server_url()))

    def test_prefetch_sends_both_tokens_in_headers_and_follows_no_redirect(self) -> None:
        seen = {}
        fake = _FakeServer()
        with patch.object(edge, "_server", return_value=fake), \
                patch.dict(sys.modules, {"httpx": _fake_httpx(seen)}):
            with self.assertLogs(edge.logger, level="INFO") as logs:
                asyncio.run(edge.prefetch(EDGE_TOK, CONNECT_TOK))
        self.assertEqual(seen["url"], fake._url + "/v1/prefetch")
        self.assertEqual(seen["headers"], {"Modal-Authorization": f"Bearer {EDGE_TOK}",
                                           "Authorization": f"Bearer {CONNECT_TOK}"})
        self.assertIs(seen["follow_redirects"], False)
        self.assertEqual(seen["timeout"], edge.PREFETCH_TIMEOUT_S)
        self.assertIsNone(seen["content"])
        text = "\n".join(logs.output)
        self.assertIn("204", text)
        self.assertNotIn(EDGE_TOK, text)
        self.assertNotIn(CONNECT_TOK, text)

    def test_prefetch_never_raises_and_logs_the_type_only(self) -> None:
        seen = {}
        with patch.object(edge, "_server", return_value=_FakeServer()), \
                patch.dict(sys.modules, {"httpx": _fake_httpx(seen, error=OSError(f"Bearer {EDGE_TOK}"))}):
            with self.assertLogs(edge.logger, level="WARNING") as logs:
                asyncio.run(edge.prefetch(EDGE_TOK, CONNECT_TOK))
        text = "\n".join(logs.output)
        self.assertIn("OSError", text)
        self.assertNotIn(EDGE_TOK, text)

    def test_spawn_keeps_the_task_until_done(self) -> None:
        async def scenario():
            gate = asyncio.Event()

            async def work():
                await gate.wait()

            task = edge.spawn(work())
            self.assertIn(task, edge._TASKS)
            gate.set()
            await task
            await asyncio.sleep(0)
            self.assertNotIn(task, edge._TASKS)

        asyncio.run(scenario())


class TestUrlWarmUp(_Keyed):
    """V1-9: the server URL is resolved once at API startup (its own startup task),
    so the first prefetch of a process never pays `get_url()`; a Modal that does
    not answer leaves the lazy path exactly as before. No network anywhere."""

    def _prefetch(self, fake, seen):
        with patch.object(edge, "_server", return_value=fake), \
                patch.dict(sys.modules, {"httpx": _fake_httpx(seen)}):
            asyncio.run(edge.prefetch(EDGE_TOK, CONNECT_TOK))

    def test_the_warm_up_fills_the_cache_for_the_first_prefetch(self) -> None:
        fake = _FakeServer()
        clock = [100.0]
        seen = {}
        with patch.object(edge.time, "monotonic", lambda: clock[0]):
            with patch.object(edge, "_server", return_value=fake):
                self.assertIsNone(asyncio.run(edge.warm_url()))
            self.assertEqual(fake.url_calls, 1)
            clock[0] += 30.0  # the first reserve comes later; the prefetch finds the URL cached
            self._prefetch(fake, seen)
        self.assertEqual(fake.url_calls, 1)
        self.assertEqual(seen["url"], fake._url + "/v1/prefetch")

    def test_the_warm_up_respects_the_url_ttl(self) -> None:
        fake = _FakeServer()
        clock = [100.0]
        with patch.object(edge.time, "monotonic", lambda: clock[0]):
            with patch.object(edge, "_server", return_value=fake):
                asyncio.run(edge.warm_url())
            clock[0] += edge.URL_TTL_S + 1
            self._prefetch(fake, {})
        self.assertEqual(fake.url_calls, 2)

    def test_an_unreachable_modal_never_raises_and_the_prefetch_resolves_lazily(self) -> None:
        class _Down(_FakeServer):
            def get_url(self):
                self.url_calls += 1
                raise ConnectionError("modal unreachable")

        down = _Down()
        with patch.object(edge, "_server", return_value=down):
            with self.assertLogs(edge.logger, level="WARNING") as logs:
                self.assertIsNone(asyncio.run(edge.warm_url()))
        self.assertIn("ConnectionError", "\n".join(logs.output))
        self.assertNotIn("modal unreachable", "\n".join(logs.output))
        self.assertIsNone(edge._url_cache)
        # the very first `_server()` raising is just as harmless
        with patch.object(edge, "_server", side_effect=RuntimeError("no modal")):
            self.assertIsNone(asyncio.run(edge.warm_url()))
        fake = _FakeServer()
        seen = {}
        self._prefetch(fake, seen)
        self.assertEqual(fake.url_calls, 1)
        self.assertEqual(seen["url"], fake._url + "/v1/prefetch")

    def test_a_hanging_get_url_is_left_behind_and_the_next_caller_starts_fresh(self) -> None:
        gate = threading.Event()

        class _Hang(_FakeServer):
            def get_url(self):
                self.url_calls += 1
                gate.wait(10)
                return self._url

        hang = _Hang()
        try:
            with patch.object(edge, "_server", return_value=hang), patch.object(edge, "PREFETCH_TIMEOUT_S", 0.1):
                t0 = time.monotonic()
                self.assertIsNone(asyncio.run(edge.warm_url()))
                self.assertLess(time.monotonic() - t0, 2.0)
                self.assertIsNone(edge._url_cache)
                # a later caller does not join the call that is still hanging
                asyncio.run(edge.server_url())
                self.assertEqual(hang.url_calls, 2)
        finally:
            gate.set()

    def test_concurrent_callers_share_one_get_url(self) -> None:
        gate = threading.Event()

        class _Slow(_FakeServer):
            def get_url(self):
                self.url_calls += 1
                gate.wait(10)
                return self._url

        slow = _Slow()

        async def scenario():
            warm = asyncio.get_running_loop().create_task(edge.warm_url())
            await asyncio.sleep(0.05)
            first = asyncio.get_running_loop().create_task(edge.server_url())
            await asyncio.sleep(0.05)
            gate.set()
            await warm
            return await first

        try:
            with patch.object(edge, "_server", return_value=slow):
                self.assertEqual(asyncio.run(scenario()), slow._url)
        finally:
            gate.set()
        self.assertEqual(slow.url_calls, 1)

    def test_get_url_runs_in_the_control_pool(self) -> None:
        names = []

        class _Where(_FakeServer):
            def get_url(self):
                names.append(threading.current_thread().name)
                return self._url

        with patch.object(edge, "_server", return_value=_Where()):
            asyncio.run(edge.warm_url())
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("modal-control"), names)


class TestNoSecretInLogs(_Keyed):
    def test_no_token_edge_token_envelope_or_hash_in_any_log_record(self) -> None:
        records = []

        class _Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage() + " " + repr(record.args))

        handler = _Grab(level=logging.DEBUG)
        edge.logger.addHandler(handler)
        old_level = edge.logger.level
        edge.logger.setLevel(logging.DEBUG)
        try:
            rows = [_row(1), _row(2, enc="e1.00000000." + "A" * 30), _row(3, age=99999)]

            def boom(token):
                raise RuntimeError(f"terminate failed for {token}")

            with patch.object(edge, "_server", return_value=_FakeServer(terminate=boom)):
                asyncio.run(edge.terminate_pass(_DB(rows)))
            seen = {}
            with patch.object(edge, "_server", return_value=_FakeServer()), \
                    patch.dict(sys.modules, {"httpx": _fake_httpx(seen, error=RuntimeError(EDGE_TOK + CONNECT_TOK))}):
                asyncio.run(edge.prefetch(EDGE_TOK, CONNECT_TOK))
        finally:
            edge.logger.removeHandler(handler)
            edge.logger.setLevel(old_level)
        text = "\n".join(records)
        self.assertTrue(records)
        for secret in (EDGE_TOK, CONNECT_TOK, rows[0]["edge_token_enc"], "e1."):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
