"""Route tests for ``/inference/*`` (Cloud-Inferenz control plane, spec §4.3).

House pattern (``test_dataset_identity_routes._ensure_stubs``): fastapi and
pydantic are stubbed, each async handler is driven with ``asyncio.run``, the
route module's ``get_supabase`` is patched with an in-memory model of the 048
service-role RPCs, and the Modal edge (``inference_edge``) is replaced by a fake
server. No ``modal`` import anywhere.

What is pinned: students only (A-Q4); the reserve ladder and its order (no
Modal call before the reservation row exists); only the token HASH reaches the
database; the reserve answer carries no secret; every Modal start outcome (a
timeout ends the row ``edge_failed`` and a late success is still attached, a
credential of the wrong shape is terminated, an attach-time end is 503 even when
superseded (G-4), missing/conflict is terminated + 500); the arm rotates the
connect token and is the ONLY answer carrying secrets; the caller scoping of
end/heartbeat; ``Cache-Control: no-store`` on every answer; no token, edge
token, envelope or hash in any log record; ``main.py``'s rate rules, prefix,
router and startup tasks; the teacher's run list carries ``backend``.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from fastapi import HTTPException  # noqa: E402,F401
from app.routes import inference as route  # noqa: E402
from app.services import hf_credentials as hc  # noqa: E402
from app.services import inference_edge as edge  # noqa: E402
from app.services import inference_sessions as svc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(os.path.dirname(HERE), "main.py")
KEY = base64.b64encode(bytes(range(32))).decode()
EDGE_TOK = "edge-" + "k" * 40
MSID = "sd-AbC123_x"
STUDENT = {"id": "11111111-1111-4111-8111-111111111111", "role": "student", "classroom_id": "c-1",
           "workgroup_id": None}
OTHER = {"id": "22222222-2222-4222-8222-222222222222", "role": "student", "classroom_id": "c-1",
         "workgroup_id": None}
TEACHER = {"id": "33333333-3333-4333-8333-333333333333", "role": "teacher"}
ADMIN = {"id": "44444444-4444-4444-8444-444444444444", "role": "admin"}
REPO = "EduBotics-Solutions/vfbdfveg-act-omx_f_kjkkj-bfs6egcrlv"
SHA = "6477f6ca9a53ea62317f95d65b05ab7fd32db59a"
TRAINING = {"id": 7, "user_id": STUDENT["id"], "workgroup_id": None, "status": "succeeded",
            "model_type": "act", "model_name": REPO}
SUMMARY = {"type": "act", "expected_cameras": ["gripper", "scene"],
           "image_shapes": {"gripper": [3, 480, 640], "scene": [3, 480, 640]}, "trained_fps": 30,
           "chunk_size": 100, "n_action_steps": 15, "state_dim": 6, "action_dim": 6, "requires_task": False,
           "model_repo": REPO, "model_revision": SHA}


def _content(resp):
    """The JSON body, with the stub or with real Starlette (whose JSONResponse has `.body`)."""
    if hasattr(resp, "body"):
        return json.loads(resp.body)
    return resp.content


def _headers(resp):
    return {k.lower(): v for k, v in dict(resp.headers or {}).items()}


class _FakeRequest:
    """Just enough of a Starlette Request for the reserve handler: `await body()`."""

    def __init__(self, raw: bytes):
        self._raw = raw

    async def body(self) -> bytes:
        return self._raw


def _body_request(training_id=7, raw=None) -> _FakeRequest:
    if raw is None:
        raw = json.dumps({"training_id": training_id}).encode("utf-8")
    return _FakeRequest(raw)


MALFORMED_DE = "Die Anfrage ist ungültig. Bitte lade die Seite neu und versuche es noch einmal."


class _PgError(Exception):
    def __init__(self, code, msg="x"):
        super().__init__({"code": code, "message": msg})
        self.code = code


class _Store:
    """An in-memory model of the 048 service-role RPCs, close enough to the SQL."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.raise_on: dict[str, Exception] = {}
        self.attach_override: dict | None = None
        self.used_today_s = 120

    def rpc(self, name, args):
        self.calls.append((name, dict(args)))
        outer = self

        class _R:
            def execute(self):
                if name in outer.raise_on:
                    raise outer.raise_on[name]
                return SimpleNamespace(data=getattr(outer, "_" + name)(dict(args)))

        return _R()

    def _own(self, args):
        row = self.rows.get(args["p_session_id"])
        if row is None or row["user_id"] != args["p_user_id"]:
            raise _PgError("P0002", "Sitzung nicht gefunden")
        return row

    def _create_inference_session_safe(self, a):
        for r in self.rows.values():
            if r["user_id"] == a["p_user_id"] and r["status"] in ("reserved", "active"):
                r.update(status="ended", end_reason="superseded")
        sid = str(uuid.uuid4())
        self.rows[sid] = {"id": sid, "user_id": a["p_user_id"], "status": "reserved", "token_hash": a["p_token_hash"],
                          "modal_session_id": None, "edge_token_enc": None, "end_reason": None,
                          "session_s": min(a["p_max_session_s"], a["p_daily_cap_s"]), "max_end_in_s": None}
        return {"session_id": sid, "session_s": self.rows[sid]["session_s"],
                "remaining_today_s": a["p_daily_cap_s"], "reserve_until": "2026-10-12T10:05:00+00:00"}

    def _attach_inference_edge(self, a):
        if self.attach_override is not None:
            return self.attach_override
        row = self.rows.get(a["p_session_id"])
        if row is None:
            return {"status": "missing"}
        if row["modal_session_id"] is not None:
            return {"status": "conflict"}
        row.update(modal_session_id=a["p_modal_session_id"], edge_token_enc=a["p_edge_token_enc"])
        if row["status"] == "ended":
            return {"status": "ended", "end_reason": row["end_reason"]}
        return {"status": row["status"]}

    def _arm_inference_session(self, a):
        row = self._own(a)
        if row["status"] == "ended":
            return {"status": "ended", "end_reason": row["end_reason"]}
        if row["status"] == "active":
            return {"status": "active"}
        if row["edge_token_enc"] is None:
            return {"status": "not_ready"}
        row["token_hash"] = a["p_token_hash"]
        return {"status": "armed", "edge_token_enc": row["edge_token_enc"], "session_s": row["session_s"]}

    def _end_inference_session(self, a):
        row = self._own(a)
        if row["status"] != "ended":
            row.update(status="ended", end_reason=a["p_reason"])
        return {"status": "ended", "end_reason": row["end_reason"], "active_seconds": 0}

    def _heartbeat_inference_session_client(self, a):
        row = self._own(a)
        if row["status"] == "ended":
            return {"status": "ended", "end_reason": row["end_reason"]}
        return {"status": row["status"], "end_reason": None, "max_end_in_s": row["max_end_in_s"]}

    def _inference_usage(self, a):
        live = next((r["id"] for r in self.rows.values()
                     if r["user_id"] == a["p_user_id"] and r["status"] in ("reserved", "active")), None)
        return {"used_today_s": self.used_today_s, "remaining_today_s": a["p_daily_cap_s"] - self.used_today_s,
                "live_session_id": live}


class _Server:
    def __init__(self, *, start=None, terminate=None):
        self.started, self.terminated = [], []
        outer = self

        class _S:
            def start(self, idle_timeout=600):
                outer.started.append(idle_timeout)
                if start is not None:
                    return start()
                return SimpleNamespace(session_id=MSID, token=EDGE_TOK)

            def terminate(self, token):
                outer.terminated.append(token)
                if terminate is not None:
                    terminate(token)

        self.sessions = _S()

    def get_url(self):
        return "https://w--edubotics-inference-cloudinference.eu-west.modal.direct"


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {hc.KEY_ENV: KEY})
        self._env.start()
        os.environ.pop(hc.PREVIOUS_KEY_ENV, None)
        hc._reset_for_tests()
        edge._reset_for_tests()
        self.store = _Store()
        self.server = _Server()
        self.prefetched: list[tuple[str, str]] = []
        self.kicks = [0]
        self.visible = TRAINING
        self.resolved = dict(SUMMARY)
        self.resolve_error = None
        self.events: list[str] = []

        async def fake_prefetch(edge_token, connect_token):
            self.prefetched.append((edge_token, connect_token))

        def fake_visible(profile, training_id):
            self.events.append("visible")
            return self.visible

        def fake_resolve(repo):
            self.events.append("resolve")
            if self.resolve_error is not None:
                raise self.resolve_error
            return dict(self.resolved)

        def fake_kick():
            self.kicks[0] += 1

        self._patches = [
            patch.object(route, "get_supabase", return_value=self.store),
            patch.object(edge, "_server", side_effect=lambda: self._server_now()),
            patch.object(edge, "prefetch", fake_prefetch),
            patch.object(edge, "kick", fake_kick),
            patch.object(svc, "visible_training", fake_visible),
            patch.object(svc, "resolve_model", fake_resolve),
        ]
        for p in self._patches:
            p.start()

    def _server_now(self):
        self.events.append("modal")
        return self.server

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._env.stop()
        hc._reset_for_tests()
        edge._reset_for_tests()

    # drivers -------------------------------------------------------------

    def run_async(self, coro):
        async def wrapper():
            result = await coro
            # let a spawned prefetch / late attach run to its end
            for _ in range(50):
                if not edge._TASKS:
                    break
                await asyncio.sleep(0.01)
            return result

        return asyncio.run(wrapper())

    def reserve(self, profile=STUDENT, training_id=7, raw=None):
        # The handler reads the body by hand (V1-8): FastAPI never validates it.
        return self.run_async(route.reserve_session(_body_request(training_id, raw), profile=profile))

    def arm(self, sid, profile=STUDENT):
        return self.run_async(route.arm_session(sid, profile=profile))

    def heartbeat(self, sid, profile=STUDENT):
        return self.run_async(route.heartbeat_session(sid, profile=profile))

    def end(self, sid, profile=STUDENT):
        return self.run_async(route.end_session(sid, profile=profile))

    def usage(self, profile=STUDENT):
        return self.run_async(route.read_usage(profile=profile))

    def assertAnswer(self, resp, status):
        body = _content(resp)
        self.assertEqual(resp.status_code, status, body)
        self.assertEqual(_headers(resp).get("cache-control"), "no-store")
        return body


class TestStudentsOnly(_Case):
    def test_every_route_refuses_non_students(self) -> None:
        sid = str(uuid.uuid4())
        for profile in (TEACHER, ADMIN):
            for call in (lambda p: self.reserve(p), lambda p: self.arm(sid, p), lambda p: self.heartbeat(sid, p),
                         lambda p: self.end(sid, p), lambda p: self.usage(p)):
                body = self.assertAnswer(call(profile), 403)
                self.assertEqual(body["detail"], "Die Cloud-Inferenz ist nur für Schülerinnen und Schüler verfügbar.")
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.events, [])


class TestReserve(_Case):
    def test_the_happy_path(self) -> None:
        body = self.assertAnswer(self.reserve(), 200)
        self.assertEqual(set(body), {"session_id", "summary", "session_s", "remaining_today_s", "reserve_s"})
        self.assertEqual(body["summary"]["expected_cameras"], ["gripper", "scene"])
        self.assertEqual(body["reserve_s"], svc.RESERVE_S)
        row = self.store.rows[body["session_id"]]
        self.assertEqual(row["modal_session_id"], MSID)
        self.assertTrue(row["edge_token_enc"].startswith("e1."))
        self.assertEqual(svc.edge_open(body["session_id"], row["edge_token_enc"]), EDGE_TOK)
        self.assertEqual(self.server.started, [svc.EDGE_IDLE_S])

    def test_reserve_ladder_own_group_404(self) -> None:
        self.visible = None
        body = self.assertAnswer(self.reserve(), 404)
        self.assertEqual(body["detail"], "Modell nicht gefunden.")
        self.assertEqual(self.store.calls, [])
        self.assertNotIn("modal", self.events)

    def test_reserve_refusals_in_order(self) -> None:
        cases = [
            (dict(TRAINING, status="running"), None, 409, "Dieses Training ist noch nicht fertig."),
            (dict(TRAINING, model_type="diffusion"), None, 409,
             "Dieses Modell kann noch nicht in der Cloud ausgeführt werden – in der Cloud laufen bisher nur "
             "ACT-Modelle."),
            (dict(TRAINING, model_name="SvenBorodun/act_omx"), None, 409,
             "Dieses Modell liegt nicht im EduBotics-Modellarchiv."),
            (dict(TRAINING, model_name=None), None, 409, "Dieses Modell liegt nicht im EduBotics-Modellarchiv."),
            (TRAINING, svc.ModelResolveError("not_found"), 409, "Das Modell wurde bei Hugging Face nicht gefunden."),
            (TRAINING, svc.ModelResolveError("invalid"), 409, "Die Modelldateien sind unvollständig oder beschädigt."),
            (TRAINING, svc.ModelResolveError("not_act"), 409,
             "Dieses Modell kann noch nicht in der Cloud ausgeführt werden – in der Cloud laufen bisher nur "
             "ACT-Modelle."),
            (TRAINING, svc.ModelResolveError("unreachable"), 502,
             "Hugging Face ist gerade nicht erreichbar. Bitte in einer Minute erneut versuchen."),
        ]
        for training, error, status, detail in cases:
            self.visible, self.resolve_error = training, error
            self.store.calls.clear()
            self.events.clear()
            body = self.assertAnswer(self.reserve(), status)
            self.assertEqual(body["detail"], detail)
            # nothing written and no Modal call before the reservation row exists
            self.assertEqual(self.store.calls, [])
            self.assertNotIn("modal", self.events)
        # the status check runs before the model is resolved
        self.visible, self.resolve_error = dict(TRAINING, status="failed"), None
        self.events.clear()
        self.reserve()
        self.assertEqual(self.events, ["visible"])

    def test_p0048_429_p0049_503_p0002_404(self) -> None:
        for code, status, detail in (
            ("P0048", 429, "Deine Cloud-Zeit für heute ist aufgebraucht. Morgen geht es weiter."),
            ("P0049", 503, "Die Cloud ist gerade voll ausgelastet. Bitte in ein paar Minuten erneut versuchen."),
            ("P0002", 404, "Modell nicht gefunden."),
            ("XX000", 500, "Die Cloud-Sitzung konnte nicht angelegt werden."),
        ):
            self.store.raise_on["create_inference_session_safe"] = _PgError(code)
            self.events.clear()
            body = self.assertAnswer(self.reserve(), status)
            self.assertEqual(body["detail"], detail)
            self.assertNotIn("modal", self.events)  # a refused caller never costs a GPU
            self.assertEqual(self.server.started, [])

    def test_the_retry_marker_is_on_the_start_failure_503_and_no_other_answer(self) -> None:
        """Round 2 (owner): the page retries a reserve only on this marker, never on
        German text. A failed start leaves no live row and costs no Cloud-Zeit."""
        self.assertEqual(route.GPU_NOT_STARTED_CODE, "gpu_not_started")
        coded, plain = [], []

        def record(resp, into):
            body = _content(resp)
            into.append((resp.status_code, body))

        # the start failure itself (an SDK error; the timeout is pinned above)
        self.server = _Server(start=lambda: (_ for _ in ()).throw(RuntimeError("down")))
        record(self.reserve(), coded)
        (row,) = [r for r in self.store.rows.values()]
        self.assertEqual((row["status"], row["end_reason"]), ("ended", "edge_failed"))
        self.assertIsNone(row.get("connected_at"))
        # every other answer of every route
        self.server = _Server()
        record(self.reserve(), plain)                                   # 200
        sid = next(r["id"] for r in self.store.rows.values() if r["status"] == "reserved")
        for code in ("P0048", "P0049", "P0002", "XX000"):
            self.store.raise_on["create_inference_session_safe"] = _PgError(code)
            record(self.reserve(), plain)                               # 429, 503 busy, 404, 500
        self.store.raise_on.clear()
        self.visible = None
        record(self.reserve(), plain)                                   # 404
        self.visible = TRAINING
        for err in ("not_found", "invalid", "not_act", "unreachable"):
            self.resolve_error = svc.ModelResolveError(err)
            record(self.reserve(), plain)                               # 409, 502
        self.resolve_error = None
        record(self.reserve(raw=b"{}"), plain)                          # 422
        record(self.reserve(TEACHER), plain)                            # 403
        self.store.attach_override = {"status": "ended", "end_reason": "superseded"}
        record(self.reserve(), plain)                                   # 503, attach-time end
        self.store.attach_override = {"status": "conflict"}
        record(self.reserve(), plain)                                   # 500
        self.store.attach_override = None
        record(self.arm(sid), plain)
        record(self.heartbeat(sid), plain)
        record(self.end(sid), plain)
        record(self.usage(), plain)
        self.assertEqual([(st, b.get("code")) for st, b in coded], [(503, "gpu_not_started")])
        statuses = {st for st, _ in plain}
        self.assertTrue({200, 403, 404, 409, 422, 429, 500, 502, 503} <= statuses, statuses)
        for status, body in plain:
            self.assertNotIn("code", body, (status, body))

    def test_reserve_sends_only_the_hash_to_the_rpc(self) -> None:
        tokens = []
        real_new = svc.new_token

        def spy():
            t, h = real_new()
            tokens.append((t, h))
            return t, h

        with patch.object(svc, "new_token", spy):
            self.reserve()
        (t0, h0), = tokens
        create = next(a for n, a in self.store.calls if n == "create_inference_session_safe")
        self.assertEqual(create["p_token_hash"], h0)
        self.assertEqual(h0, hashlib.sha256(t0.encode()).hexdigest())
        self.assertNotIn(t0, json.dumps(self.store.calls))
        # the reservation's connect token reaches only the prefetch, with the edge token
        self.assertEqual(self.prefetched, [(EDGE_TOK, t0)])

    def test_reserve_answer_carries_no_secret(self) -> None:
        tokens = []
        real_new = svc.new_token

        def spy():
            t, h = real_new()
            tokens.append((t, h))
            return t, h

        with patch.object(svc, "new_token", spy):
            body = self.assertAnswer(self.reserve(), 200)
        flat = json.dumps(body)
        (t0, h0), = tokens
        for secret in (t0, h0, EDGE_TOK, "e1.", MSID, "sd-", "token", "edge"):
            self.assertNotIn(secret, flat)

    def test_reserve_triggers_terminate_due_and_prefetch_with_both_tokens(self) -> None:
        body = self.assertAnswer(self.reserve(), 200)
        self.assertGreaterEqual(self.kicks[0], 1)  # a superseded reservation frees its slot now
        self.assertEqual(len(self.prefetched), 1)
        edge_token, connect_token = self.prefetched[0]
        self.assertEqual(edge_token, EDGE_TOK)
        self.assertRegex(connect_token, r"\A[A-Za-z0-9_-]{43}\Z")
        self.assertEqual(self.store.rows[body["session_id"]]["token_hash"],
                         hashlib.sha256(connect_token.encode()).hexdigest())

    def test_a_new_reservation_supersedes_and_kicks(self) -> None:
        first = self.assertAnswer(self.reserve(), 200)["session_id"]
        kicks = self.kicks[0]
        self.assertAnswer(self.reserve(), 200)
        self.assertEqual(self.store.rows[first]["end_reason"], "superseded")
        self.assertGreater(self.kicks[0], kicks)

    def test_reserve_start_timeout_ends_edge_failed_and_late_success_is_attached(self) -> None:
        def slow():
            time.sleep(0.35)
            return SimpleNamespace(session_id=MSID, token=EDGE_TOK)

        self.server = _Server(start=slow)

        async def scenario():
            resp = await route.reserve_session(_body_request(7), profile=STUDENT)
            # the late success arrives after the answer, on the same (long-lived) loop
            await asyncio.sleep(0.5)
            for _ in range(50):
                if not edge._TASKS:
                    break
                await asyncio.sleep(0.01)
            return resp

        with patch.object(edge, "START_TIMEOUT_S", 0.1):
            body = self.assertAnswer(asyncio.run(scenario()), 503)
        self.assertEqual(body, {"detail": "Die Cloud-GPU konnte gerade nicht gestartet werden. Bitte versuche es "
                                          "gleich noch einmal.", "code": "gpu_not_started"})
        (row,) = self.store.rows.values()
        self.assertEqual((row["status"], row["end_reason"]), ("ended", "edge_failed"))
        # attached to the ENDED row (so the terminate loop ends it), never handed out
        self.assertEqual(row["modal_session_id"], MSID)
        self.assertEqual(svc.edge_open(row["id"], row["edge_token_enc"]), EDGE_TOK)
        self.assertEqual(self.prefetched, [])
        self.assertGreaterEqual(self.kicks[0], 2)

    def test_reserve_sdk_error_ends_edge_failed_503(self) -> None:
        def broken():
            raise RuntimeError("app not deployed")

        self.server = _Server(start=broken)
        body = self.assertAnswer(self.reserve(), 503)
        self.assertIn("gestartet", body["detail"])
        self.assertEqual(body["code"], route.GPU_NOT_STARTED_CODE)
        (row,) = self.store.rows.values()
        self.assertEqual(row["end_reason"], "edge_failed")

    def test_reserve_invalid_credentials_shape_terminates_and_503(self) -> None:
        for creds in (SimpleNamespace(session_id="not-sd", token=EDGE_TOK),
                      SimpleNamespace(session_id=MSID, token="short"),
                      SimpleNamespace(session_id=MSID, token="has space " + "x" * 20),
                      SimpleNamespace(session_id=None, token=None)):
            self.store = _Store()
            self.server = _Server(start=lambda c=creds: c)
            with patch.object(route, "get_supabase", return_value=self.store):
                body = self.assertAnswer(self.reserve(), 503)
            self.assertIn("gestartet", body["detail"])
            self.assertEqual(body["code"], route.GPU_NOT_STARTED_CODE)
            (row,) = self.store.rows.values()
            self.assertEqual(row["end_reason"], "edge_failed")
            self.assertIsNone(row["modal_session_id"])
            self.assertEqual(self.server.terminated, [creds.token] if isinstance(creds.token, str) else [])
            self.assertEqual(self.prefetched, [])

    def test_reserve_attach_time_end_is_503(self) -> None:
        # F-1, G-4: every attach-time end answers 503, superseded included, so a 409
        # from reserve always means the model itself was refused.
        for reason in ("superseded", "client_gone", "never_connected", "user_stop"):
            self.store.attach_override = {"status": "ended", "end_reason": reason}
            kicks = self.kicks[0]
            body = self.assertAnswer(self.reserve(), 503)
            # the same sentence, but NO retry marker: this reservation ended for a reason of
            # its own (another tab superseded it, the page let it go) — the GPU did start
            self.assertEqual(body, {"detail": "Die Cloud-GPU konnte gerade nicht gestartet werden. Bitte versuche "
                                              "es gleich noch einmal."})
            self.assertGreater(self.kicks[0], kicks)
            self.assertEqual(self.server.terminated, [])  # the terminate LOOP ends it, from the stored edge
            self.assertEqual(self.prefetched, [])

    def test_reserve_attach_missing_or_conflict_terminates_and_500(self) -> None:
        for status in ("missing", "conflict"):
            self.server = _Server()
            self.store.attach_override = {"status": status}
            body = self.assertAnswer(self.reserve(), 500)
            self.assertEqual(body["detail"], "Die Cloud-Sitzung konnte nicht angelegt werden.")
            self.assertEqual(self.server.terminated, [EDGE_TOK])
            self.assertEqual(self.prefetched, [])

    def test_a_failing_attach_terminates_and_ends_the_row(self) -> None:
        self.store.raise_on["attach_inference_edge"] = RuntimeError("db down")
        body = self.assertAnswer(self.reserve(), 500)
        self.assertEqual(body["detail"], "Die Cloud-Sitzung konnte nicht angelegt werden.")
        self.assertEqual(self.server.terminated, [EDGE_TOK])
        (row,) = self.store.rows.values()
        self.assertEqual(row["end_reason"], "edge_failed")

    def test_a_malformed_body_is_a_german_422_that_echoes_nothing(self) -> None:
        """V1-8: never FastAPI's English 422 with the request's `input`."""
        bad_ids = (0, -3, "7", None, True, False, 7.0, 7.5, [7], {"x": 7}, 2 ** 31, "x" * 300)
        raws = [b"", b"{}", b"[]", b'"x"', b"null", b"7", b"{", b'{"training_id": 7', b"\xff\xfe\x00",
                b"[" * 2000 + b"]" * 2000, b"training_id=7",
                b'{"trainingId": 7}', b"[" * 5000 + b"]" * 5000, b'{"training_id": 1' + b"0" * 5000 + b"}",
                b'{"training_id": 7, "pad": "' + b"x" * 5000 + b'"}']
        cases = [dict(training_id=t) for t in bad_ids] + [dict(raw=r) for r in raws]
        for case in cases:
            with self.subTest(case=repr(case)[:80]):
                body = self.assertAnswer(self.reserve(**case), 422)
                self.assertEqual(body, {"detail": MALFORMED_DE})
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.events, [])

    def test_a_well_formed_body_with_extra_keys_is_accepted(self) -> None:
        raw = json.dumps({"training_id": 7, "extra": True}).encode()
        self.assertAnswer(self.reserve(raw=raw), 200)
        self.assertAnswer(self.reserve(training_id=2 ** 31 - 1), 200)

    def test_non_students_are_refused_before_the_body_is_read(self) -> None:
        body = self.assertAnswer(self.reserve(TEACHER, raw=b"{"), 403)
        self.assertEqual(body["detail"], "Die Cloud-Inferenz ist nur für Schülerinnen und Schüler verfügbar.")


_REAL_FASTAPI_SCRIPT = r"""
import json, sys, types

auth = types.ModuleType("app.auth")


def get_current_profile():
    return {}


auth.get_current_profile = get_current_profile
sys.modules["app.auth"] = auth


class _PgError(Exception):
    code = "P0002"


class _Store:
    def rpc(self, name, args):
        class _R:
            def execute(self):
                raise _PgError()
        return _R()


sc = types.ModuleType("app.services.supabase_client")
sc.get_supabase = lambda: _Store()
sys.modules["app.services.supabase_client"] = sc

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.routes import inference as route
from app.services import inference_sessions as svc

seen = []


def visible(profile, training_id):
    seen.append(training_id)
    if training_id != 8:
        return None
    return {"id": 8, "status": "succeeded", "model_type": "act", "model_name": "EduBotics-Solutions/m"}


async def start_fails(on_late):
    raise RuntimeError("modal down")


svc.visible_training = visible
svc.resolve_model = lambda repo: {"model_repo": repo, "model_revision": "a" * 40}
svc.create_reservation = lambda *a: {"session_id": "0f5c0a52-5a0c-4d4e-9d61-2b0d2c1f0a12", "session_s": 1800,
                                     "remaining_today_s": 7000}


async def ended_edge_failed(*a):
    return None


route._end_edge_failed = ended_edge_failed
route.inference_edge.start_session = start_fails
route.inference_edge.kick = lambda: None
app = FastAPI()
app.include_router(route.router)
app.dependency_overrides[route.get_current_profile] = lambda: {
    "id": "11111111-1111-4111-8111-111111111111", "role": "student"}
client = TestClient(app)
sid = "0f5c0a52-5a0c-4d4e-9d61-2b0d2c1f0a11"
json_ct = {"content-type": "application/json"}
cases = [
    ("reserve {}", "/inference/sessions", {"json": {}}),
    ("reserve 0", "/inference/sessions", {"json": {"training_id": 0}}),
    ("reserve str", "/inference/sessions", {"json": {"training_id": "7"}}),
    ("reserve list", "/inference/sessions", {"json": [7]}),
    ("reserve nobody", "/inference/sessions", {}),
    ("reserve badjson", "/inference/sessions", {"content": b"{", "headers": json_ct}),
    ("reserve form", "/inference/sessions",
     {"content": b"training_id=7", "headers": {"content-type": "application/x-www-form-urlencoded"}}),
    ("reserve valid", "/inference/sessions", {"json": {"training_id": 7}}),
    ("gpu not started", "/inference/sessions", {"json": {"training_id": 8}}),
]
for tail in ("arm", "heartbeat", "end"):
    for i, kw in enumerate(({"json": {}}, {"json": {"training_id": 0}}, {"content": b"{", "headers": json_ct})):
        cases.append((tail + " " + str(i), "/inference/sessions/" + sid + "/" + tail, kw))
out = {"seen": seen, "answers": []}
for name, path, kw in cases:
    r = client.post(path, **kw)
    out["answers"].append({"name": name, "status": r.status_code, "text": r.text,
                           "cc": r.headers.get("cache-control")})
print(json.dumps(out))
"""


class TestRealFastApiBodies(unittest.TestCase):
    """V1-8 through the REAL FastAPI stack (ci.yml::python-tests installs fastapi and
    httpx). This suite stubs `fastapi` process-wide (house pattern), so the real
    stack runs in a child process: the reserve body is judged by the handler, never
    by FastAPI's English 422 with the request's `input`, and the bodyless POST
    routes never answer 422 at all."""

    def test_real_fastapi_answers(self) -> None:
        import subprocess
        import sys

        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(HERE))
        env[hc.KEY_ENV] = KEY
        proc = subprocess.run([sys.executable, "-c", _REAL_FASTAPI_SCRIPT], capture_output=True, text=True,
                              env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        answers = {a["name"]: a for a in out["answers"]}
        for name, a in answers.items():
            with self.subTest(name=name):
                self.assertEqual(a["cc"], "no-store")
                self.assertNotIn('"input"', a["text"])
                if name.startswith("reserve") and name != "reserve valid":
                    self.assertEqual(a["status"], 422, a["text"])
                    self.assertEqual(json.loads(a["text"]), {"detail": MALFORMED_DE})
                elif name == "gpu not started":
                    # the retry marker survives the real JSONResponse (a body field, readable cross-origin)
                    self.assertEqual(a["status"], 503, a["text"])
                    self.assertEqual(json.loads(a["text"]), {
                        "detail": "Die Cloud-GPU konnte gerade nicht gestartet werden. Bitte versuche es gleich "
                                  "noch einmal.",
                        "code": "gpu_not_started"})
                elif name == "reserve valid":
                    # the handler parsed it and asked the visibility ladder (patched: not visible)
                    self.assertEqual(a["status"], 404, a["text"])
                    self.assertEqual(json.loads(a["text"]), {"detail": "Modell nicht gefunden."})
                else:
                    self.assertEqual(a["status"], 404, a["text"])
                    self.assertEqual(json.loads(a["text"]), {"detail": "Cloud-Sitzung nicht gefunden."})
        self.assertEqual(out["seen"], [7, 8])


class TestArm(_Case):
    def _reserved(self):
        return self.assertAnswer(self.reserve(), 200)["session_id"]

    def test_arm_rotates_the_token_and_returns_both_tokens_once(self) -> None:
        sid = self._reserved()
        old_hash = self.store.rows[sid]["token_hash"]
        body = self.assertAnswer(self.arm(sid), 200)
        self.assertEqual(set(body), {"token", "edge_token", "session_s"})
        self.assertEqual(body["edge_token"], EDGE_TOK)
        self.assertRegex(body["token"], r"\A[A-Za-z0-9_-]{43}\Z")
        new_hash = self.store.rows[sid]["token_hash"]
        self.assertNotEqual(new_hash, old_hash)  # the reservation's token died
        self.assertEqual(new_hash, hashlib.sha256(body["token"].encode()).hexdigest())
        self.assertNotIn(body["token"], json.dumps(self.store.calls))
        # a second arm mints yet another token
        again = self.assertAnswer(self.arm(sid), 200)
        self.assertNotEqual(again["token"], body["token"])

    def test_arm_410_409_404_503_paths(self) -> None:
        sid = self._reserved()
        # not ready (no edge yet)
        self.store.rows[sid].update(modal_session_id=None, edge_token_enc=None)
        self.assertEqual(self.assertAnswer(self.arm(sid), 409)["detail"],
                         "Die Cloud-GPU wird noch vorbereitet. Bitte einen Moment warten.")
        # already active
        self.store.rows[sid]["status"] = "active"
        self.assertEqual(self.assertAnswer(self.arm(sid), 409)["detail"], "Dieser Cloud-Lauf läuft bereits.")
        # ended: the sentence of its reason, or the generic one
        self.store.rows[sid].update(status="ended", end_reason="client_gone")
        self.assertEqual(self.assertAnswer(self.arm(sid), 410)["detail"], "Die Inferenz-Seite war nicht mehr geöffnet.")
        self.store.rows[sid].update(end_reason="user_stop")
        self.assertEqual(self.assertAnswer(self.arm(sid), 410)["detail"], "Die Reservierung ist abgelaufen.")
        # not own / unknown / malformed
        self.assertEqual(self.assertAnswer(self.arm(sid, profile=OTHER), 404)["detail"], "Cloud-Sitzung nicht gefunden.")
        self.assertEqual(self.assertAnswer(self.arm(str(uuid.uuid4())), 404)["detail"], "Cloud-Sitzung nicht gefunden.")
        # the envelope does not open (the key was rotated away): ended edge_failed, 503
        sid2 = self._reserved()
        with patch.dict(os.environ, {hc.KEY_ENV: base64.b64encode(bytes(32)).decode()}):
            hc._reset_for_tests()
            body = self.assertAnswer(self.arm(sid2), 503)
        hc._reset_for_tests()
        self.assertEqual(body["detail"], "Die Cloud-Sitzung ist nicht mehr nutzbar. Bitte erneut starten.")
        self.assertEqual(self.store.rows[sid2]["end_reason"], "edge_failed")

    def test_malformed_session_id_404(self) -> None:
        for bad in ("x", "", "../x", str(uuid.uuid4()).upper(), str(uuid.uuid4()) + "\n", "1" * 36, None):
            for call in (self.arm, self.heartbeat, self.end):
                body = self.assertAnswer(call(bad), 404)
                self.assertEqual(body["detail"], "Cloud-Sitzung nicht gefunden.")
        self.assertEqual([n for n, _ in self.store.calls], [])


class TestHeartbeatEndUsage(_Case):
    def test_end_and_heartbeat_scoped_to_the_caller(self) -> None:
        sid = self.assertAnswer(self.reserve(), 200)["session_id"]
        self.assertEqual(self.assertAnswer(self.heartbeat(sid, OTHER), 404)["detail"], "Cloud-Sitzung nicht gefunden.")
        self.assertEqual(self.assertAnswer(self.end(sid, OTHER), 404)["detail"], "Cloud-Sitzung nicht gefunden.")
        self.assertEqual(self.store.rows[sid]["status"], "reserved")
        for name, args in self.store.calls:
            if name in ("heartbeat_inference_session_client", "end_inference_session"):
                self.assertEqual(args["p_user_id"], OTHER["id"])  # the JWT's id, never the request's
        kicks = self.kicks[0]
        body = self.assertAnswer(self.end(sid), 200)
        self.assertEqual(body, {"status": "ended", "end_reason": "user_stop", "active_seconds": 0})
        self.assertGreater(self.kicks[0], kicks)  # a reservation never used is terminated at once
        self.assertEqual(self.assertAnswer(self.end(sid), 200)["end_reason"], "user_stop")  # idempotent

    def test_heartbeat_ended_is_200_with_german_reason(self) -> None:
        sid = self.assertAnswer(self.reserve(), 200)["session_id"]
        self.store.rows[sid].update(status="ended", end_reason="expired")
        body = self.assertAnswer(self.heartbeat(sid), 200)
        self.assertEqual(body, {"status": "ended", "end_reason": "expired",
                                "end_reason_de": "Das Zeitlimit für diesen Lauf war erreicht.", "max_end_in_s": None})

    def test_heartbeat_max_end_in_s_null_while_reserved(self) -> None:
        sid = self.assertAnswer(self.reserve(), 200)["session_id"]
        body = self.assertAnswer(self.heartbeat(sid), 200)
        self.assertEqual(body, {"status": "reserved", "end_reason": None, "end_reason_de": "", "max_end_in_s": None})
        self.store.rows[sid].update(status="active", max_end_in_s=1234.5)
        self.assertEqual(self.assertAnswer(self.heartbeat(sid), 200)["max_end_in_s"], 1234.5)

    def test_a_store_failure_is_a_german_500(self) -> None:
        sid = self.assertAnswer(self.reserve(), 200)["session_id"]
        self.store.raise_on["heartbeat_inference_session_client"] = RuntimeError("down")
        self.assertEqual(self.assertAnswer(self.heartbeat(sid), 500)["detail"], "Die Cloud-Sitzung konnte nicht geladen werden.")

    def test_usage_shape(self) -> None:
        body = self.assertAnswer(self.usage(), 200)
        self.assertEqual(body, {"daily_cap_s": svc.DAILY_CAP_S, "session_cap_s": svc.SESSION_MAX_S,
                                "used_today_s": 120, "remaining_today_s": svc.DAILY_CAP_S - 120,
                                "live_session_id": None})
        sid = self.assertAnswer(self.reserve(), 200)["session_id"]
        self.assertEqual(self.assertAnswer(self.usage(), 200)["live_session_id"], sid)
        self.store.raise_on["inference_usage"] = RuntimeError("down")
        self.assertEqual(self.assertAnswer(self.usage(), 500)["detail"], "Die Cloud-Zeit konnte nicht geladen werden.")


class TestNoSecretInLogs(_Case):
    def test_no_token_edge_token_envelope_or_hash_in_any_log_record(self) -> None:
        records = []

        class _Grab(logging.Handler):
            def emit(self, record):
                try:
                    records.append(record.getMessage())
                except Exception:  # noqa: BLE001
                    records.append(repr(record.msg) + repr(record.args))

        grab = _Grab(level=logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(grab)
        root.setLevel(logging.DEBUG)
        tokens = []
        real_new = svc.new_token

        def spy():
            t, h = real_new()
            tokens.append((t, h))
            return t, h

        try:
            with patch.object(svc, "new_token", spy):
                sid = self.assertAnswer(self.reserve(), 200)["session_id"]
                armed = self.assertAnswer(self.arm(sid), 200)
                self.heartbeat(sid)
                self.end(sid)
                self.store.attach_override = {"status": "conflict"}
                self.reserve()
                self.store.attach_override = None
                self.server = _Server(start=lambda: SimpleNamespace(session_id="bad", token=EDGE_TOK))
                self.reserve()
                self.store.raise_on["arm_inference_session"] = RuntimeError(f"boom {EDGE_TOK}")
                self.arm(sid)
        finally:
            root.removeHandler(grab)
            root.setLevel(old)
        text = "\n".join(records)
        secrets = [EDGE_TOK, armed["token"], "e1."] + [t for pair in tokens for t in pair]
        for row in self.store.rows.values():
            if row["edge_token_enc"]:
                secrets.append(row["edge_token_enc"])
        for secret in secrets:
            self.assertNotIn(secret, text)


# ------------------------------------------------------------- main.py wiring


def _main_tree():
    with open(MAIN_PY, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=MAIN_PY)


def _main_literal(name):
    for node in ast.walk(_main_tree()):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(name)


def _match(rules, method, path):
    for rule_method, prefix, limit, window in sorted(rules, key=lambda r: len(r[1]), reverse=True):
        if rule_method != "*" and rule_method != method:
            continue
        matched = path.startswith(prefix) if prefix.endswith("/") else (
            path == prefix or path.startswith(prefix + "/"))
        if matched:
            return (rule_method, prefix, limit, window)
    return None


class TestMainWiring(unittest.TestCase):
    def test_rate_rules_and_per_user_prefix(self) -> None:
        rules = _main_literal("_RATE_LIMIT_RULES")
        self.assertEqual(rules[-2:], [("POST", "/inference/sessions", 30, 60.0), ("GET", "/inference/usage", 30, 60.0)])
        sid = str(uuid.uuid4())
        for path in ("/inference/sessions", f"/inference/sessions/{sid}/arm", f"/inference/sessions/{sid}/heartbeat",
                     f"/inference/sessions/{sid}/end"):
            self.assertEqual(_match(rules, "POST", path), ("POST", "/inference/sessions", 30, 60.0), path)
        self.assertEqual(_match(rules, "GET", "/inference/usage"), ("GET", "/inference/usage", 30, 60.0))
        self.assertIsNone(_match(rules, "POST", "/inference/sessionsX"))
        prefixes = _main_literal("_PER_USER_RATE_LIMIT_PREFIXES")
        self.assertIn("/inference/", prefixes)
        self.assertTrue(any(f"/inference/sessions/{sid}/end".startswith(p) for p in prefixes))

    def test_the_router_and_the_two_startup_tasks(self) -> None:
        with open(MAIN_PY, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("from app.routes.inference import router as inference_router", src)
        self.assertIn("app.include_router(inference_router)", src)
        self.assertIn("from app.services.inference_sweep import sweep_loop as _inference_sweep_loop", src)
        fns = {n.name: ast.get_source_segment(src, n) for n in ast.walk(_main_tree())
               if isinstance(n, ast.AsyncFunctionDef)}
        self.assertIn("_start_inference_sweep", fns)
        self.assertIn("_start_inference_terminate_loop", fns)
        for name in ("_start_inference_sweep", "_start_inference_terminate_loop"):
            self.assertIn('os.environ.get("INFERENCE_SWEEPER_DISABLED") == "1"', fns[name])
        self.assertIn("_inference_sweep_loop()", fns["_start_inference_sweep"])
        self.assertIn("terminate_loop(", fns["_start_inference_terminate_loop"])

    def test_the_url_warm_up_is_a_startup_task_that_never_blocks_boot(self) -> None:
        """V1-9: scheduled with create_task (never awaited, so neither the lifespan
        startup nor /health waits for Modal), kept strongly, off in unit tests."""
        fns = {n.name: n for n in ast.walk(_main_tree()) if isinstance(n, ast.AsyncFunctionDef)}
        self.assertIn("_start_inference_url_warmup", fns)
        fn = fns["_start_inference_url_warmup"]
        self.assertFalse([n for n in ast.walk(fn) if isinstance(n, ast.Await)])
        with open(MAIN_PY, encoding="utf-8") as fh:
            src = ast.get_source_segment(fh.read(), fn)
        self.assertIn('os.environ.get("INFERENCE_SWEEPER_DISABLED") == "1"', src)
        self.assertIn("_keep_task(_asyncio.create_task(_inference_edge.warm_url()))", src)
        decorated = [d for d in fn.decorator_list if isinstance(d, ast.Call)
                     and getattr(d.func, "attr", "") == "on_event"]
        self.assertEqual([d.args[0].value for d in decorated], ["startup"])

    def test_the_retry_code_is_the_pages_code(self) -> None:
        """Lockstep: the page retries on exactly the code this API sends."""
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
        path = os.path.join(repo, "physical_ai_tools", "physical_ai_manager", "src", "features", "cloudInference",
                            "controller.js")
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        m = re.search(r"export const GPU_NOT_STARTED_CODE = '([a-z_]+)';", js)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), route.GPU_NOT_STARTED_CODE)

    def test_three_reserve_attempts_fit_the_per_user_budget(self) -> None:
        """Round 2: the page's 3 automatic reserve attempts (worst case an immediate SDK
        error, RESERVE_RETRY_DELAY_MS apart) fit the shared POST budget of ONE
        student, together with the ends of the failed attempts' sessions."""
        from app.services.rate_limiter import RateLimiter

        rule = _match(_main_literal("_RATE_LIMIT_RULES"), "POST", "/inference/sessions")
        self.assertEqual(rule, ("POST", "/inference/sessions", 30, 60.0))
        prefixes = _main_literal("_PER_USER_RATE_LIMIT_PREFIXES")
        self.assertIn("/inference/", prefixes)  # keyed per student, not per classroom NAT
        limiter = RateLimiter()
        method, prefix, limit, window = rule
        allowed = [limiter.check(f"{method}:{prefix}", "jwt:student", limit, window) for _ in range(3 + 3)]
        self.assertEqual(allowed, [True] * 6)
        # a classmate behind the same NAT has a budget of their own
        self.assertTrue(limiter.check(f"{method}:{prefix}", "jwt:classmate", limit, window))

    def test_a_failed_start_costs_no_cloud_time(self) -> None:
        """Round 2: an attempt that never connected adds 0 s to today's Cloud-Zeit (048)."""
        mig = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations")
        (name,) = [n for n in os.listdir(mig) if n.endswith("_048_inference_sessions.sql")]
        with open(os.path.join(mig, name), encoding="utf-8") as fh:
            sql = fh.read()
        used = sql[sql.index("FUNCTION public._inference_used_today_s"):]
        used = used[:used.index("$$;")]
        self.assertIn("CASE WHEN connected_at IS NULL THEN 0", used)

    def test_no_new_secret(self) -> None:
        with open(MAIN_PY, encoding="utf-8") as fh:
            src = fh.read()
        required = ast.literal_eval(re.search(r"required = (\([^)]*\))", src).group(1))
        self.assertEqual(required, ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "MODAL_TOKEN_ID",
                                    "MODAL_TOKEN_SECRET", "EDUBOTICS_HF_TOKEN_KEY"))
        self.assertNotIn("INFERENCE_", re.search(r"def _warn_optional_secrets.*?\n\n\n", src, re.S).group(0))


class TestRouteSource(unittest.TestCase):
    def test_every_handler_is_async_and_takes_the_profile(self) -> None:
        path = os.path.join(os.path.dirname(HERE), "routes", "inference.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        handlers = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and d.func.attr in ("get", "post") for d in n.decorator_list)]
        self.assertEqual(sorted(h.name for h in handlers),
                         ["arm_session", "end_session", "heartbeat_session", "read_usage", "reserve_session"])
        for h in handlers:
            self.assertIn("profile", [a.arg for a in h.args.args + h.args.kwonlyargs], h.name)
        plain = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)
                 and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post")
                         for d in n.decorator_list)]
        self.assertEqual(plain, [])


class TestTeacherRuns(unittest.TestCase):
    def test_teacher_runs_include_backend(self) -> None:
        from app.routes import teacher

        seen = {}

        class _Q:
            def select(self, cols):
                seen["cols"] = cols
                return self

            def eq(self, *a):
                return self

            def order(self, *a, **k):
                return self

            def limit(self, *a):
                return self

            def execute(self):
                return SimpleNamespace(data=[
                    {"id": 1, "policy_repo": "EduBotics-Solutions/m", "started_at": None, "duration_s": 3.0,
                     "exit_reason": "stopped", "error_message_de": "", "created_at": "t", "backend": "cloud"},
                ])

        class _DB:
            def table(self, name):
                seen["table"] = name
                return _Q()

        with patch.object(teacher, "_assert_student_owned", return_value={}), \
                patch.object(teacher, "get_supabase", return_value=_DB()):
            runs = asyncio.run(teacher.list_student_inference_runs("s1", teacher={"id": "t1"}))
        self.assertIn("backend", [c.strip() for c in seen["cols"].split(",")])
        self.assertEqual(runs[0].backend, "cloud")
        # an older row (or API) without the key reads as the Jetson
        path = os.path.join(os.path.dirname(HERE), "routes", "teacher.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "InferenceRunSummary")
        backend = next(n for n in cls.body if isinstance(n, ast.AnnAssign) and n.target.id == "backend")
        self.assertEqual(ast.literal_eval(backend.value), "jetson")


if __name__ == "__main__":
    unittest.main()
