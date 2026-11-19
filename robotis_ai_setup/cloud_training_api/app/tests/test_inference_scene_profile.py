# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The reserve answers the training dataset's scene-camera profile (decision D58, spec S9).

`inference_sessions.dataset_scene_profile(row)` reads the stamp file
`meta/edubotics_scene_camera.json` of the training's dataset at its pinned
`training_params.dataset_revision` (40 hex; else the `v3.0` tag), ANONYMOUSLY:
200 + a valid stamp -> its profile, 404 + `X-Error-Code: EntryNotFound` ->
'legacy' (no stamp), anything else -> None. Only definite answers are cached.

The HTTP half runs the REAL httpx with a fake transport (`httpx.MockTransport`),
so the request a test inspects is the one httpx would put on the wire, the
headers the environment might add included. `test_platform_token_scope.py`
cannot see a raw httpx call; the no-`Authorization` test here is the fence.

The route half (`POST /inference/sessions`, the `_Case` fixture of
test_inference_routes.py): the lookup starts before the reservation and the GPU
start, the reserve waits for it at most SCENE_LOOKUP_WAIT_S after the GPU start
returned, and no exit of the route leaves it unretrieved.
"""

from __future__ import annotations

import ast
import asyncio
import gc
import inspect
import os
import tempfile
import textwrap
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

# First: importing it installs the house stubs (fastapi, pydantic) the route module needs.
from app.tests.test_inference_routes import (
    EDGE_TOK, MSID, STUDENT, TRAINING, _Case, _PgError, _Server, _body_request, _content,
)
from app.routes import inference as route
from app.services import dataset_preflight
from app.services import inference_edge as edge
from app.services import inference_sessions as svc

DATASET = "lena-schmidt/omx_f_wuerfel"
REV = "0123456789abcdef0123456789abcdef01234567"
URL = f"https://huggingface.co/datasets/{DATASET}/resolve/{REV}/meta/edubotics_scene_camera.json"
TOWER = b'{"v":1,"scene_camera_profile":"tower_v1","profile_id":"3efca10f3a05","platform":"windows"}'
TOWER_PI_NULLS = b'{"v":1,"scene_camera_profile":"tower_v1","profile_id":null,"platform":null}'
LEGACY_STAMP = b'{"v":1,"scene_camera_profile":"legacy","profile_id":null,"platform":null}'
# Synthetic, low-entropy stand-ins for secrets the environment might hold (gitleaks).
PLATFORM_TOKEN = "hf_platformtokenplatformtokenplatformtk"
NETRC_SECRET = "netrcsecretnetrcsecretnetrcsecret"


def _row(revision=REV, dataset=DATASET, **params):
    training_params = dict(params)
    if revision is not None:
        training_params["dataset_revision"] = revision
    return dict(TRAINING, dataset_name=dataset, training_params=training_params)


class _Hub:
    """The REAL httpx.Client with a fake transport: `answer(request)` -> httpx.Response
    (or raises). Every request is recorded as httpx built it."""

    def __init__(self, answer):
        self.answer = answer
        self.requests: list[httpx.Request] = []
        self._lock = threading.Lock()
        real_client = httpx.Client

        def handler(request):
            with self._lock:
                self.requests.append(request)
            return self.answer(request)

        transport = httpx.MockTransport(handler)
        self._patch = patch.object(httpx, "Client", lambda *a, **kw: real_client(*a, transport=transport, **kw))

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def _answer(status, body=b"", headers=None):
    return lambda request: httpx.Response(status, headers=headers or {}, content=body)


def _code(status, code):
    return _answer(status, b'{"error":"x"}', {"X-Error-Code": code})


class _Clean(unittest.TestCase):
    def setUp(self) -> None:
        svc._reset_scene_cache_for_tests()
        self.addCleanup(svc._reset_scene_cache_for_tests)


class TestTheLookup(_Clean):
    def ask(self, answer, row=None):
        with _Hub(answer) as hub:
            result = svc.dataset_scene_profile(row if row is not None else _row())
        return result, hub.requests

    def test_a_tower_stamp_at_the_pinned_revision(self) -> None:
        for body in (TOWER, TOWER_PI_NULLS):
            svc._reset_scene_cache_for_tests()
            result, requests = self.ask(_answer(200, body))
            self.assertEqual(result, "tower_v1")
            self.assertEqual([(r.method, str(r.url)) for r in requests], [("GET", URL)])
        # a stamp naming legacy is a valid stamp too (the robot's reader accepts it)
        svc._reset_scene_cache_for_tests()
        self.assertEqual(self.ask(_answer(200, LEGACY_STAMP))[0], "legacy")

    def test_no_stamp_file_is_legacy(self) -> None:
        result, requests = self.ask(_code(404, "EntryNotFound"))
        self.assertEqual(result, "legacy")
        self.assertEqual(len(requests), 1)

    def test_everything_else_is_unknown(self) -> None:
        def timeout(request):
            raise httpx.ReadTimeout("timed out", request=request)

        def refused(request):
            raise httpx.ConnectError("refused", request=request)

        cases = {
            "RepoNotFound 401": _code(401, "RepoNotFound"),
            "RepoNotFound 404": _code(404, "RepoNotFound"),
            "RevisionNotFound": _code(404, "RevisionNotFound"),
            "GatedRepo 401": _code(401, "GatedRepo"),
            "GatedRepo 403": _code(403, "GatedRepo"),
            "401": _answer(401),
            "403": _answer(403),
            "404 without a code": _answer(404),
            "EntryNotFound on another status": _code(403, "EntryNotFound"),
            "429": _answer(429),
            "500": _answer(500),
            "502": _answer(502),
            "503": _answer(503),
            "timeout": timeout,
            "connect error": refused,
            "malformed json": _answer(200, b'{"v":1,'),
            "unknown v": _answer(200, TOWER.replace(b'"v":1', b'"v":2')),
            "v true": _answer(200, TOWER.replace(b'"v":1', b'"v":true')),
            "v 1.0": _answer(200, TOWER.replace(b'"v":1', b'"v":1.0')),
            "unknown profile": _answer(200, TOWER.replace(b"tower_v1", b"tower_v2")),
            "extra key": _answer(200, TOWER[:-1] + b',"lens":"wide"}'),
            "missing key": _answer(200, b'{"v":1,"scene_camera_profile":"tower_v1","profile_id":null}'),
            "bad profile id": _answer(200, TOWER.replace(b"3efca10f3a05", b"3EFCA10F3A05")),
            "profile id with a newline": _answer(200, TOWER.replace(b"3efca10f3a05", b"3efca10f3a05\\n")),
            "bad platform": _answer(200, TOWER.replace(b"windows", b"linux")),
            "a list": _answer(200, b"[1]"),
            "a BOM": _answer(200, b"\xef\xbb\xbf" + TOWER),
            "not utf-8": _answer(200, b"\xff\xfe\x00"),
            "nested too deep": _answer(200, b"[" * 2000 + b"]" * 2000),
            "too large (declared)": _answer(200, TOWER + b" " * svc.SCENE_STAMP_MAX_BYTES),
            "an HTML page": _answer(200, b"<html>captive portal</html>"),
        }
        for name, answer in cases.items():
            with self.subTest(case=name):
                svc._reset_scene_cache_for_tests()
                result, requests = self.ask(answer)
                self.assertIsNone(result)
                self.assertEqual(len(requests), 1)

    def test_a_body_streamed_past_the_cap_is_unknown(self) -> None:
        def chunks():
            yield TOWER
            for _ in range(10):
                yield b" " * 1024

        result, _ = self.ask(lambda request: httpx.Response(200, content=chunks()))
        self.assertIsNone(result)
        # the cap is the robot reader's own (scene_provenance.STAMP_MAX_BYTES)
        self.assertEqual(svc.SCENE_STAMP_MAX_BYTES, 4096)
        svc._reset_scene_cache_for_tests()
        self.assertEqual(self.ask(_answer(200, TOWER + b" " * (4096 - len(TOWER))))[0], "tower_v1")

    def test_the_request_carries_no_authorization_and_no_token_reaches_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            netrc = os.path.join(tmp, "netrc")
            with open(netrc, "w", encoding="utf-8") as fh:
                fh.write(f"machine huggingface.co login platform password {NETRC_SECRET}\n")
            os.chmod(netrc, 0o600)
            env = {"HF_TOKEN": PLATFORM_TOKEN, "HUGGING_FACE_HUB_TOKEN": PLATFORM_TOKEN, "NETRC": netrc}
            with patch.dict(os.environ, env):
                for answer in (_answer(200, TOWER), _code(404, "EntryNotFound"), _answer(503)):
                    svc._reset_scene_cache_for_tests()
                    _, requests = self.ask(answer)
                    (request,) = requests
                    headers = {k.lower(): v for k, v in request.headers.items()}
                    self.assertNotIn("authorization", headers)
                    self.assertNotIn("proxy-authorization", headers)
                    self.assertNotIn("cookie", headers)
                    flat = str(request.url) + repr(sorted(headers.items()))
                    for secret in (PLATFORM_TOKEN, NETRC_SECRET):
                        self.assertNotIn(secret, flat)

    def test_no_credential_is_named_on_the_lookup_path(self) -> None:
        """Structural half of the fence: the lookup functions read no environment and
        no stored credential, and pass no header, auth or cookie to httpx."""
        for fn in (svc.dataset_scene_profile, svc._fetch_scene_profile, svc._scene_profile_of_stamp):
            src = textwrap.dedent(inspect.getsource(fn))
            tree = ast.parse(src)
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            keywords = {k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords}
            self.assertEqual({"environ", "getenv", "hf_credentials", "HF_TOKEN"} & (names | attrs), set(),
                             fn.__name__)
            self.assertEqual({"headers", "auth", "cookies"} & keywords, set(), fn.__name__)
            for word in ("HF_TOKEN", "Authorization", "Bearer"):
                self.assertNotIn(word, src, fn.__name__)

    def test_without_a_pinned_revision_the_v3_tag_is_read(self) -> None:
        tag_url = URL.replace(REV, "v3.0")
        rows = [_row(revision=None), _row(revision=REV[:12]), _row(revision=REV.upper()), _row(revision=7),
                dict(TRAINING, dataset_name=DATASET, training_params=None),
                dict(TRAINING, dataset_name=DATASET)]
        for row in rows:
            with self.subTest(params=row.get("training_params")):
                svc._reset_scene_cache_for_tests()
                result, requests = self.ask(_answer(200, TOWER), row)
                self.assertEqual(result, "tower_v1")
                self.assertEqual([str(r.url) for r in requests], [tag_url])
        self.assertEqual(svc.SCENE_STAMP_TAG, dataset_preflight.EXPECTED_CODEBASE_VERSION)

    def test_a_row_without_a_usable_dataset_name_asks_nothing(self) -> None:
        for name in (None, "", "noslash", "a/b/c", "../x", "a/..", "a/b?x=1", "a/b#c", "/a/b", "a /b", 7,
                     "a/" + "b" * 97):
            with self.subTest(name=name):
                result, requests = self.ask(_answer(200, TOWER), _row(dataset=name))
                self.assertIsNone(result)
                self.assertEqual(requests, [])
        self.assertIsNone(svc.dataset_scene_profile(None))

    def test_the_lookup_never_raises(self) -> None:
        def broken(request):
            raise RuntimeError("anything at all")

        self.assertIsNone(self.ask(broken)[0])
        with patch.object(svc, "_fetch_scene_profile", side_effect=MemoryError()):
            self.assertIsNone(svc.dataset_scene_profile(_row()))


class TestTheCache(_Clean):
    def test_a_definite_answer_at_a_pinned_revision_is_asked_once(self) -> None:
        for answer, expected in ((_answer(200, TOWER), "tower_v1"), (_code(404, "EntryNotFound"), "legacy")):
            svc._reset_scene_cache_for_tests()
            with _Hub(answer) as hub:
                first = svc.dataset_scene_profile(_row())
                with patch.object(svc.time, "monotonic", return_value=time.monotonic() + 10 * 365 * 86400):
                    second = svc.dataset_scene_profile(_row())  # no expiry for a pinned revision
            self.assertEqual((first, second), (expected, expected))
            self.assertEqual(len(hub.requests), 1)

    def test_an_unknown_answer_is_never_cached(self) -> None:
        answers = [_answer(503), _code(401, "RepoNotFound"), _answer(200, TOWER)]
        with _Hub(lambda request: answers.pop(0)(request)) as hub:
            results = [svc.dataset_scene_profile(_row()) for _ in range(4)]
        self.assertEqual(results, [None, None, "tower_v1", "tower_v1"])
        self.assertEqual(len(hub.requests), 3)

    def test_a_tag_answer_is_cached_for_the_model_ttl(self) -> None:
        row = _row(revision=None)
        now = [1000.0]
        with _Hub(_answer(200, TOWER)) as hub, patch.object(svc.time, "monotonic", side_effect=lambda: now[0]):
            svc.dataset_scene_profile(row)
            now[0] += svc.MODEL_CACHE_TTL_S - 1
            svc.dataset_scene_profile(row)
            self.assertEqual(len(hub.requests), 1)
            now[0] += 2
            svc.dataset_scene_profile(row)
            self.assertEqual(len(hub.requests), 2)
        self.assertEqual(svc.MODEL_CACHE_TTL_S, 60.0)

    def test_the_cache_is_a_bounded_lru(self) -> None:
        self.assertEqual(svc.SCENE_CACHE_MAX, 512)
        rows = [_row(revision=f"{i:040x}") for i in range(svc.SCENE_CACHE_MAX + 1)]
        with _Hub(_answer(200, TOWER)) as hub:
            for row in rows[:-1]:
                svc.dataset_scene_profile(row)
            svc.dataset_scene_profile(rows[0])          # used again: now the newest
            self.assertEqual(len(hub.requests), svc.SCENE_CACHE_MAX)
            svc.dataset_scene_profile(rows[-1])         # one more: the least recently used goes
            svc.dataset_scene_profile(rows[0])
            self.assertEqual(len(hub.requests), svc.SCENE_CACHE_MAX + 1)
            svc.dataset_scene_profile(rows[1])
            self.assertEqual(len(hub.requests), svc.SCENE_CACHE_MAX + 2)


# ------------------------------------------------------------------ the route


class _Lookup:
    """A stand-in for svc.dataset_scene_profile: answers `value` (or raises `error`)
    after `delay_s`, unless released earlier. Records the rows it was asked about."""

    def __init__(self, value="tower_v1", delay_s=0.0, error=None):
        self.value, self.delay_s, self.error = value, delay_s, error
        self.release = threading.Event()
        self.finished = threading.Event()
        self.rows = []

    def __call__(self, row):
        self.rows.append(row)
        try:
            if self.delay_s:
                self.release.wait(self.delay_s)
            if self.error is not None:
                raise type(self.error)(*self.error.args)  # a fresh instance on every call
            return self.value
        finally:
            self.finished.set()


class _RouteCase(_Case):
    def setUp(self) -> None:
        super().setUp()
        svc._reset_scene_cache_for_tests()
        self.addCleanup(svc._reset_scene_cache_for_tests)
        self.visible = _row()

    async def settle(self, lookup=None):
        for _ in range(1500):
            if not route._SCENE_LOOKUPS and not edge._TASKS and (lookup is None or lookup.finished.is_set()):
                break
            await asyncio.sleep(0.01)
        for _ in range(3):
            await asyncio.sleep(0)


class TestTheReserveAnswer(_RouteCase):
    def test_the_answer_carries_the_profile(self) -> None:
        for answer, expected in ((_answer(200, TOWER), "tower_v1"), (_code(404, "EntryNotFound"), "legacy"),
                                 (_answer(503), None), (_code(401, "RepoNotFound"), None)):
            svc._reset_scene_cache_for_tests()
            with _Hub(answer) as hub:
                body = self.assertAnswer(self.reserve(), 200)
            self.assertEqual(body["scene_camera_profile"], expected)
            self.assertEqual(set(body), {"session_id", "summary", "session_s", "remaining_today_s", "reserve_s",
                                         "scene_camera_profile"})
            (request,) = hub.requests
            self.assertEqual(str(request.url), URL)
            self.assertNotIn("authorization", {k.lower() for k in request.headers})

    def test_a_training_without_a_dataset_name_answers_null(self) -> None:
        self.visible = TRAINING
        with _Hub(_answer(200, TOWER)) as hub:
            body = self.assertAnswer(self.reserve(), 200)
        self.assertIsNone(body["scene_camera_profile"])
        self.assertEqual(hub.requests, [])

    def test_the_reserve_never_fails_because_of_the_lookup(self) -> None:
        for lookup in (_Lookup(error=RuntimeError("boom")), _Lookup(error=MemoryError())):
            with patch.object(svc, "dataset_scene_profile", lookup):
                body = self.assertAnswer(self.reserve(), 200)
            self.assertIsNone(body["scene_camera_profile"])

    def test_the_lookup_gets_the_visible_training_row(self) -> None:
        lookup = _Lookup()
        with patch.object(svc, "dataset_scene_profile", lookup):
            self.assertAnswer(self.reserve(), 200)
        self.assertEqual(lookup.rows, [self.visible])


class TestTheLookupRunsBesideTheStart(_RouteCase):
    """55 m-1: the lookup starts before the reservation and the GPU start, and the
    reserve waits for it at most SCENE_LOOKUP_WAIT_S after the start returned."""

    GPU_S = 2.0

    def setUp(self) -> None:
        super().setUp()
        self.lookup_running_at_gpu_start = []

        def start():
            self.lookup_running_at_gpu_start.append(bool(route._SCENE_LOOKUPS))
            self.events.append("gpu")
            time.sleep(self.GPU_S)
            return SimpleNamespace(session_id=MSID, token=EDGE_TOK)

        self.server = _Server(start=start)

    def timed(self, lookup):
        async def scenario():
            t0 = time.monotonic()
            resp = await route.reserve_session(_body_request(7), profile=STUDENT)
            elapsed = time.monotonic() - t0
            done_at_answer = lookup.finished.is_set()
            lookup.release.set()
            await self.settle(lookup)
            return resp, elapsed, done_at_answer

        with patch.object(svc, "dataset_scene_profile", lookup):
            return asyncio.run(scenario())

    def test_the_constant(self) -> None:
        self.assertEqual(route.SCENE_LOOKUP_WAIT_S, 2.0)

    def test_a_slow_lookup_answers_null_after_the_start_plus_the_wait(self) -> None:
        lookup = _Lookup(delay_s=10.0)
        resp, elapsed, done_at_answer = self.timed(lookup)
        body = self.assertAnswer(resp, 200)
        self.assertIsNone(body["scene_camera_profile"])
        self.assertFalse(done_at_answer)
        expected = self.GPU_S + route.SCENE_LOOKUP_WAIT_S
        self.assertGreaterEqual(elapsed, expected - 0.1)
        self.assertLess(elapsed, expected + 1.0)
        # it was running when the GPU start began, and it finished in the background
        self.assertEqual(self.lookup_running_at_gpu_start, [True])
        self.assertTrue(lookup.finished.is_set())
        self.assertEqual(route._SCENE_LOOKUPS, set())

    def test_a_lookup_faster_than_the_start_answers_its_value_without_waiting(self) -> None:
        lookup = _Lookup(value="legacy", delay_s=1.0)
        resp, elapsed, done_at_answer = self.timed(lookup)
        body = self.assertAnswer(resp, 200)
        self.assertEqual(body["scene_camera_profile"], "legacy")
        self.assertTrue(done_at_answer)
        self.assertGreaterEqual(elapsed, self.GPU_S - 0.05)
        self.assertLess(elapsed, self.GPU_S + 0.75)

    def test_the_lookup_starts_before_the_reservation(self) -> None:
        lookup = _Lookup()
        real_create = svc.create_reservation

        started_at_create = []

        def create(*a):
            started_at_create.append(bool(route._SCENE_LOOKUPS))
            return real_create(*a)

        self.server = _Server()
        with patch.object(svc, "create_reservation", create), patch.object(svc, "dataset_scene_profile", lookup):
            self.assertAnswer(self.reserve(), 200)
        self.assertEqual(started_at_create, [True])


class TestTheTimedOutAnswerIsKept(_RouteCase):
    """The first reserve answers null while the lookup still runs; the definite answer
    it brings back later is cached, so the next reserve has it without asking. A null
    (an answer that is not definite) is never cached: the next reserve asks again."""

    def test_a_late_definite_answer_serves_the_next_reserve(self) -> None:
        gate = threading.Event()

        def answer(request):
            gate.wait(10)
            return httpx.Response(200, content=TOWER)

        async def scenario():
            first = await route.reserve_session(_body_request(7), profile=STUDENT)
            gate.set()
            await self.settle()
            second = await route.reserve_session(_body_request(7), profile=STUDENT)
            await self.settle()
            return first, second

        with patch.object(route, "SCENE_LOOKUP_WAIT_S", 0.2), _Hub(answer) as hub:
            first, second = asyncio.run(scenario())
        self.assertIsNone(_content(first)["scene_camera_profile"])
        self.assertEqual(_content(second)["scene_camera_profile"], "tower_v1")
        self.assertEqual(len(hub.requests), 1)

    def test_a_first_null_is_asked_again(self) -> None:
        answers = [_answer(503), _answer(200, TOWER)]
        with _Hub(lambda request: answers.pop(0)(request)) as hub:
            first = self.assertAnswer(self.reserve(), 200)
            second = self.assertAnswer(self.reserve(), 200)
            third = self.assertAnswer(self.reserve(), 200)
        self.assertEqual([b["scene_camera_profile"] for b in (first, second, third)], [None, "tower_v1", "tower_v1"])
        self.assertEqual(len(hub.requests), 2)


class TestEveryExitRetrievesTheLookup(_RouteCase):
    """No exit of the route after the lookup started leaves a raising lookup
    unretrieved („Task exception was never retrieved"). Every exception the fakes
    raise is a fresh instance that nothing in the test keeps: an instance the test
    held would keep the route's frame, and with it the lookup task, alive past the
    check, and a missing retrieval would go unseen."""

    def paths(self):
        def resolve(code):
            def setup():
                def fake_resolve(repo):
                    raise svc.ModelResolveError(code)
                self._extra.append(patch.object(svc, "resolve_model", fake_resolve))
            return setup

        def create(code):
            def setup():
                self.store.raise_on["create_inference_session_safe"] = _PgError(code)
            return setup

        def start(make_exc):
            def setup():
                async def fake_start(user_id, on_late):
                    raise make_exc()
                self._extra.append(patch.object(edge, "start_session", fake_start))
            return setup

        def server(start_fn):
            def setup():
                self.server = _Server(start=start_fn)
            return setup

        def attach(override=None, error=None):
            def setup():
                if error is not None:
                    self.store.raise_on["attach_inference_edge"] = error
                self.store.attach_override = override
            return setup

        return {
            "resolve unreachable": (resolve("unreachable"), 502),
            "resolve not found": (resolve("not_found"), 409),
            "resolve invalid": (resolve("invalid"), 409),
            "resolve not act": (resolve("not_act"), 409),
            "daily cap": (create("P0048"), 429),
            "cloud busy": (create("P0049"), 503),
            "reservation not found": (create("P0002"), 404),
            "reservation not stored": (create("XX000"), 500),
            "start timed out": (start(lambda: edge.EdgeTimeout("running")), 503),
            "start superseded": (start(edge.StartSuperseded), 503),
            "start failed": (start(lambda: RuntimeError("modal down")), 503),
            "credentials of the wrong shape": (server(lambda: SimpleNamespace(session_id="x", token="y")), 503),
            "attach failed": (attach(error=RuntimeError("db down")), 500),
            "ended while starting": (attach(override={"status": "ended", "end_reason": "superseded"}), 503),
            "attach conflict": (attach(override={"status": "conflict"}), 500),
            "the answer, a slow lookup": (lambda: setattr(self, "_slow", True), 200),
            "the answer, a fast lookup": (lambda: None, 200),
        }

    def run_path(self, setup, status):
        self._extra, self._slow = [], False
        self.store.raise_on.clear()
        self.store.attach_override = None
        self.server = _Server()
        setup()
        lookup = _Lookup(error=RuntimeError("the lookup failed"), delay_s=0.3 if self._slow else 0.0)
        handled = []

        async def scenario():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _loop, context: handled.append(context.get("message")))
            with patch.object(route, "SCENE_LOOKUP_WAIT_S", 0.05):
                resp = await route.reserve_session(_body_request(7), profile=STUDENT)
            await self.settle(lookup)
            gc.collect()
            await asyncio.sleep(0)
            return resp

        for p in self._extra:
            p.start()
        try:
            with patch.object(svc, "dataset_scene_profile", lookup):
                resp = asyncio.run(scenario())
        finally:
            for p in reversed(self._extra):
                p.stop()
        self.assertAnswer(resp, status)
        self.assertTrue(lookup.finished.is_set())
        self.assertEqual(route._SCENE_LOOKUPS, set())
        return handled

    def test_no_exit_leaves_a_raising_lookup_unretrieved(self) -> None:
        for name, (setup, status) in self.paths().items():
            with self.subTest(path=name):
                handled = self.run_path(setup, status)
                self.assertEqual([m for m in handled if m and "never retrieved" in m], [], handled)


if __name__ == "__main__":
    unittest.main()
