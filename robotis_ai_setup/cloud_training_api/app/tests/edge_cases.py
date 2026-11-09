# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The API's edge (app/edge.py, 2026-11-10): the client address, the body
limits, the per-user rate-limit key and the failed-authentication cap, mounted
on a small FastAPI app exactly the way main.py mounts them.

Run by test_edge.py in a CHILD interpreter: several suites of this package put
stub ``fastapi``/``pydantic`` modules into ``sys.modules`` during discovery, and
these cases need the real FastAPI, Starlette and TestClient (the pattern of
test_inference_routes.py's real-FastAPI script). Not named ``test_*`` so
discovery never imports it in the stubbed process."""

from __future__ import annotations

import ast
import json
import os
import sys
import time
import types
import unittest
import uuid
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.dirname(APP)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if "app.services.supabase_client" not in sys.modules:  # CI's test venv has no supabase-py
    _sc = types.ModuleType("app.services.supabase_client")
    _sc.get_supabase = lambda: (_ for _ in ()).throw(RuntimeError("no supabase in unit tests"))
    sys.modules["app.services.supabase_client"] = _sc

from app import edge  # noqa: E402
from app.services import jwt_auth as J  # noqa: E402
from app.services.rate_limiter import FailureCounter, RateLimiter  # noqa: E402

BASE = "https://unit.supabase.test"
KEY = ec.generate_private_key(ec.SECP256R1())
JWK = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "k1", "alg": "ES256", "use": "sig"})


def user_token(sub, **over):
    now = int(time.time())
    claims = {"iss": BASE + "/auth/v1", "aud": "authenticated", "sub": sub, "role": "authenticated",
              "iat": now, "exp": now + 3600, "aal": "aal1"}
    claims.update(over)
    return jwt.encode(claims, KEY, algorithm="ES256", headers={"kid": "k1"})


def _main_literal(name):
    with open(os.path.join(APP, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(getattr(t, "id", None) == name for t in targets):
            if isinstance(node.value, ast.BinOp):  # `384 * 1024`: constants and operators only
                assert all(isinstance(n, (ast.BinOp, ast.Constant, ast.operator)) for n in ast.walk(node.value))
                return eval(compile(ast.Expression(node.value), "main.py", "eval"))  # noqa: S307
            return ast.literal_eval(node.value)
    raise AssertionError(name)


def build_app(rules=None, *, limit=60):
    app = FastAPI()
    seen = {"keys": []}
    limiter = RateLimiter()

    class Spy(RateLimiter):
        def check(self, bucket, key, lim, window):
            seen["keys"].append(key)
            return limiter.check(bucket, key, lim, window)

    app.add_middleware(edge.RateLimitMiddleware, rules=rules or [("*", "/trainings/start", 3, 60.0)],
                       per_user_prefixes=_main_literal("_PER_USER_RATE_LIMIT_PREFIXES"), limiter=Spy(), hops=1)
    app.add_middleware(edge.BodySizeLimitMiddleware, limited_prefixes=_main_literal("_BODY_SIZE_LIMITED_PREFIXES"),
                       limited_max_bytes=_main_literal("_MAX_WRITE_BODY_BYTES"))
    app.add_middleware(edge.AuthFailureLimitMiddleware, counter=FailureCounter(), hops=1, limit=limit)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def anything(path: str, request: Request):
        body = await request.body()
        auth = request.headers.get("authorization", "")
        if auth and not auth.endswith("good"):
            return JSONResponse(status_code=401, content={"detail": "Ungültige oder abgelaufene Sitzung."})
        return {"path": path, "bytes": len(body)}

    return app, seen


class ClientAddress(unittest.TestCase):
    def test_the_right_most_entry_never_the_left_most(self):
        f = edge.client_ip_from
        self.assertEqual(f("10.0.0.1, 203.0.113.7", "100.64.0.1", 1), "203.0.113.7")
        self.assertEqual(f("203.0.113.7", "100.64.0.1", 1), "203.0.113.7")
        self.assertEqual(f("1.1.1.1, 203.0.113.7, 100.64.0.9", "100.64.0.1", 2), "203.0.113.7")
        self.assertEqual(f(" 2001:db8::1 ", None, 1), "2001:db8::1")
        self.assertEqual(f("[2001:db8::1]:443", None, 1), "2001:db8::1")
        self.assertEqual(f("203.0.113.7:51234", None, 1), "203.0.113.7")

    def test_fewer_entries_than_hops_or_garbage_is_the_socket_peer(self):
        f = edge.client_ip_from
        self.assertEqual(f("203.0.113.7", "100.64.0.1", 2), "100.64.0.1")
        self.assertEqual(f("", "100.64.0.1", 1), "100.64.0.1")
        self.assertEqual(f(None, "100.64.0.1", 1), "100.64.0.1")
        self.assertEqual(f("not-an-ip", "100.64.0.1", 1), "100.64.0.1")
        self.assertEqual(f(None, None, 1), "unknown")

    def test_the_hop_count_is_validated_at_boot(self):
        for raw, want in (("", 1), ("1", 1), ("3", 3), ("5", 5)):
            with patch.dict(os.environ, {"TRUSTED_PROXY_HOPS": raw}):
                self.assertEqual(edge.trusted_proxy_hops(), want)
        for raw in ("0", "6", "-1", "two", "1.5"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"TRUSTED_PROXY_HOPS": raw}):
                with self.assertRaises(RuntimeError) as ctx:
                    edge.trusted_proxy_hops()
                self.assertNotIn(raw, str(ctx.exception).replace("1 to 5", ""))

    def test_main_validates_both_settings_at_import(self):
        with open(os.path.join(APP, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("_TRUSTED_PROXY_HOPS = _validate_edge_settings()", src)
        self.assertIn("_jwt_auth.validate_boot()", src)


class BodyLimits(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(build_app()[0])

    def test_one_megabyte_on_every_route(self):
        ok = self.client.post("/datasets", content=b"x" * (edge.MAX_BODY_BYTES - 10))
        self.assertEqual(ok.status_code, 200)
        big = self.client.post("/datasets", content=b"x" * (edge.MAX_BODY_BYTES + 1))
        self.assertEqual((big.status_code, big.json()["detail"]), (413, "Anfrage zu groß (>1024 KB)."))
        for method in ("put", "patch", "delete"):
            with self.subTest(method=method):
                resp = self.client.request(method.upper(), "/anything", content=b"x" * (edge.MAX_BODY_BYTES + 1))
                self.assertEqual(resp.status_code, 413)

    def test_the_tighter_caps_stay(self):
        resp = self.client.post("/workflows", content=b"x" * (384 * 1024 + 1))
        self.assertEqual((resp.status_code, resp.json()["detail"]), (413, "Anfrage zu groß (>384 KB)."))
        self.assertEqual(self.client.post("/workflows", content=b"x" * (384 * 1024)).status_code, 200)
        self.assertEqual(self.client.put("/me/hf-token", content=b"x" * (384 * 1024 + 1)).status_code, 413)

    def test_a_chunked_body_is_refused_on_every_body_method(self):
        def chunks():
            yield b"{}"

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                resp = self.client.request(method, "/me", content=chunks())
                self.assertEqual(resp.status_code, 411)
                self.assertEqual(resp.json()["detail"], edge.CHUNKED_REFUSED_DE)
        # a GET is never judged
        self.assertEqual(self.client.get("/me", headers={"transfer-encoding": "chunked"}).status_code, 200)

    def test_a_malformed_length_is_400(self):
        resp = self.client.post("/me", content=b"{}", headers={"content-length": "12abc"})
        self.assertEqual((resp.status_code, resp.json()["detail"]), (400, edge.BAD_LENGTH_DE))

    def test_a_bodyless_post_passes(self):
        self.assertEqual(self.client.post("/inference/sessions/x/end").status_code, 200)


class RateLimitKey(unittest.TestCase):
    def setUp(self):
        J.KEYS.clear()
        J.KEYS.replace(J.parse_jwks({"keys": [JWK]}))
        self.addCleanup(J.KEYS.clear)
        env = patch.dict(os.environ, {"SUPABASE_URL": BASE, "AUTH_VERIFY_MODE": "local"})
        env.start()
        self.addCleanup(env.stop)
        self.app, self.seen = build_app(limit=10_000)
        self.client = TestClient(self.app)

    def post(self, token, ip="203.0.113.7"):
        return self.client.post("/trainings/start", headers={"Authorization": f"Bearer {token}",
                                                             "X-Forwarded-For": ip})

    def test_two_tokens_of_one_user_share_one_bucket(self):
        sub = str(uuid.uuid4())
        self.post(user_token(sub, iat=int(time.time()) - 5))
        self.post(user_token(sub, session_id="other"))
        self.assertEqual(self.seen["keys"], [f"user:{sub}"] * 2)

    def test_a_rotating_fake_token_falls_into_its_address_bucket(self):
        statuses = [self.post(f"{uuid.uuid4().hex}.{uuid.uuid4().hex}.{uuid.uuid4().hex}").status_code
                    for _ in range(6)]
        self.assertEqual(set(self.seen["keys"]), {"203.0.113.7"})
        self.assertEqual(statuses[-1], 429, "3 a minute from one address, however the token rotates")

    def test_a_forged_token_cannot_claim_a_victims_bucket(self):
        victim = str(uuid.uuid4())
        forger = ec.generate_private_key(ec.SECP256R1())
        forged = jwt.encode({"sub": victim, "aud": "authenticated", "iss": BASE + "/auth/v1",
                             "role": "authenticated", "exp": int(time.time()) + 60, "iat": int(time.time())},
                            forger, algorithm="ES256", headers={"kid": "k1"})
        self.post(forged)
        self.assertNotIn(f"user:{victim}", self.seen["keys"])

    def test_a_worker_token_keeps_its_hashed_key(self):
        tok = str(uuid.uuid4())
        app, seen = build_app(rules=[("POST", "/trainings/worker", 120, 60.0)], limit=10_000)
        TestClient(app).post("/trainings/worker/1/1/start", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(len(seen["keys"]), 1)
        self.assertTrue(seen["keys"][0].startswith("wtok:"))
        self.assertNotIn(tok, seen["keys"][0])

    def test_the_remote_rollback_keeps_the_old_hashed_key(self):
        with patch.dict(os.environ, {"AUTH_VERIFY_MODE": "remote"}):
            self.post(user_token(str(uuid.uuid4())))
        self.assertTrue(self.seen["keys"][0].startswith("jwt:"))

    def test_a_spoofed_left_most_address_does_not_rotate_the_bucket(self):
        """The owner's curl check, in-process: a rotating X-Forwarded-For value
        written by the client sits LEFT of the address the edge added."""
        statuses = [self.client.post("/trainings/start",
                                     headers={"X-Forwarded-For": f"10.0.0.{i}, 198.51.100.4"}).status_code
                    for i in range(6)]
        self.assertEqual(set(self.seen["keys"]), {"198.51.100.4"})
        self.assertIn(429, statuses)


class FailedAuthenticationCap(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(build_app(limit=5)[0])

    def get(self, auth=None, ip="203.0.113.7", path="/me"):
        headers = {"X-Forwarded-For": ip}
        if auth:
            headers["Authorization"] = auth
        return self.client.get(path, headers=headers)

    def test_failures_turn_into_a_german_429(self):
        self.assertEqual([self.get("Bearer bad").status_code for _ in range(5)], [401] * 5)
        resp = self.get("Bearer bad")
        self.assertEqual((resp.status_code, resp.json()["detail"]), (429, edge.AUTH_FAILURES_DE))
        # the address is cut off for every request that carries a token ...
        self.assertEqual(self.get("Bearer good").status_code, 429)
        # ... but /health, /version and the preflights carry none and pass
        self.assertEqual(self.get(None, path="/version").status_code, 200)
        # another address is untouched
        self.assertEqual(self.get("Bearer good", ip="198.51.100.4").status_code, 200)

    def test_successes_and_bare_requests_never_count(self):
        for _ in range(20):
            self.assertEqual(self.get("Bearer good").status_code, 200)
            self.assertEqual(self.get(None).status_code, 200)
        self.assertEqual(self.get("Bearer bad").status_code, 401)

    def test_main_mounts_the_cap_outside_the_body_and_rate_limits(self):
        with open(os.path.join(APP, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        order = [src.index(f"_edge.{name}") for name in
                 ("RateLimitMiddleware", "BodySizeLimitMiddleware", "AuthFailureLimitMiddleware")]
        self.assertEqual(order, sorted(order), "added in this order = outermost last (Starlette)")
        self.assertLess(order[-1], src.index("    CORSMiddleware,"))


class FailureCounterBounds(unittest.TestCase):
    def test_counts_slide_and_keys_are_capped(self):
        c = FailureCounter()
        self.assertEqual([c.record("a", 60.0) for _ in range(3)], [1, 2, 3])
        self.assertEqual(c.count("a", 60.0), 3)
        self.assertEqual(c.count("a", 0.0), 0)
        self.assertEqual(c.count("b", 60.0), 0)
        with patch.object(FailureCounter, "_MAX_KEYS", 4):
            c = FailureCounter()
            for i in range(10):
                c.record(f"ip{i}", 60.0)
            self.assertEqual(len(c._keys), 4)


if __name__ == "__main__":
    unittest.main()
