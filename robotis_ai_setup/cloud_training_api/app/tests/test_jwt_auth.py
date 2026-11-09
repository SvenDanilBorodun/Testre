"""The local access-token check (services/jwt_auth.py) and the auth dependencies
(app/auth.py), owner decision 2026-11-10.

Every token here is signed with a key generated for the test run and checked
against a fake JWKS; no real project secret is ever used. The JWKS fetch is the
module's one network seam (``jwt_auth._http_get_json``), replaced by a counter.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
import types
import unittest
import uuid
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.dirname(APP)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if "app.services.supabase_client" not in sys.modules:  # CI's test venv has no supabase-py
    _sc = types.ModuleType("app.services.supabase_client")
    _sc.get_supabase = lambda: (_ for _ in ()).throw(RuntimeError("no supabase in unit tests"))
    sys.modules["app.services.supabase_client"] = _sc

from app.services import jwt_auth as J  # noqa: E402

BASE = "https://unit.supabase.test"
ISS = BASE + "/auth/v1"
SUB = str(uuid.uuid4())


def _ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def _jwk(private, kid, alg="ES256"):
    pub = private.public_key()
    if alg == "ES256":
        doc = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(pub))
    else:
        doc = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub))
    doc.update({"kid": kid, "alg": alg, "use": "sig", "key_ops": ["verify"], "ext": True})
    return doc


KEY1, KEY2, OTHER = _ec_key(), _ec_key(), _ec_key()
RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key=KEY1, kid="kid-1", alg="ES256", **over):
    now = int(time.time())
    claims = {
        "iss": ISS, "aud": "authenticated", "sub": SUB, "role": "authenticated", "aal": "aal1",
        "iat": now, "exp": now + 3600, "session_id": "sess-1", "is_anonymous": False,
        "email": "max@edubotics.local",
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


class _Jwks:
    """The fake JWKS endpoint: a document (or an exception) per call, counted."""

    def __init__(self, *docs):
        self.docs = list(docs)
        self.calls = 0
        self.headers = []

    def __call__(self, url, headers):
        self.calls += 1
        self.headers.append(dict(headers))
        assert url == BASE + "/auth/v1/.well-known/jwks.json", url
        doc = self.docs[0] if len(self.docs) == 1 else self.docs.pop(0)
        if isinstance(doc, Exception):
            raise doc
        return doc


JWKS1 = {"keys": [_jwk(KEY1, "kid-1")]}
JWKS12 = {"keys": [_jwk(KEY1, "kid-1"), _jwk(KEY2, "kid-2")]}


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        J.KEYS.clear()
        J._inflight = None
        self.env = patch.dict(os.environ, {"SUPABASE_URL": BASE + "/", "AUTH_VERIFY_MODE": "local"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(J.KEYS.clear)

    def serve(self, *docs):
        fake = _Jwks(*docs)
        p = patch.object(J, "_http_get_json", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    async def invalid(self, tok):
        with self.assertRaises(J.TokenInvalid):
            await J.verify(tok)


class VerifyLocally(Base):
    async def test_a_valid_token_verifies_and_fetches_the_keys_once(self):
        fake = self.serve(JWKS1)
        claims = await J.verify(token())
        self.assertEqual((claims["sub"], claims["aal"], claims["session_id"]), (SUB, "aal1", "sess-1"))
        await J.verify(token(aal="aal2"))
        self.assertEqual(fake.calls, 1, "the keys are cached by kid")

    async def test_expired_beyond_the_leeway_is_refused_within_it_accepted(self):
        self.serve(JWKS1)
        now = int(time.time())
        await self.invalid(token(exp=now - J.LEEWAY_S - 5, iat=now - 3700))
        claims = await J.verify(token(exp=now - 5, iat=now - 3600))
        self.assertEqual(claims["sub"], SUB)

    async def test_wrong_audience_or_issuer_is_refused(self):
        self.serve(JWKS1)
        await self.invalid(token(aud="anon"))
        await self.invalid(token(iss="https://evil.supabase.test/auth/v1"))
        await self.invalid(token(iss=None))
        await self.invalid(token(exp=None))

    async def test_a_wrong_algorithm_is_refused(self):
        self.serve({"keys": [_jwk(KEY1, "kid-1"), _jwk(RSA, "kid-rsa", "RS256")]})
        # HS256 (the shared secret Supabase never publishes), even with a known kid
        hs = jwt.encode({"sub": SUB, "aud": "authenticated", "iss": ISS, "role": "authenticated",
                         "exp": int(time.time()) + 60, "iat": int(time.time())},
                        "a-shared-secret-of-at-least-32-bytes!!", algorithm="HS256", headers={"kid": "kid-1"})
        await self.invalid(hs)
        # alg none
        none = jwt.encode({"sub": SUB}, None, algorithm="none", headers={"kid": "kid-1"})
        await self.invalid(none)
        # RS256 in the header against an ES256 key: never the header's choice
        await self.invalid(token(key=RSA, kid="kid-1", alg="RS256"))
        # ...while the RS256 key verifies its own tokens
        self.assertEqual((await J.verify(token(key=RSA, kid="kid-rsa", alg="RS256")))["sub"], SUB)

    async def test_a_forged_signature_is_refused(self):
        self.serve(JWKS1)
        await self.invalid(token(key=OTHER, kid="kid-1"))
        head, body, sig = token().split(".")
        tampered = jwt.utils.base64url_encode(
            json.dumps({**json.loads(jwt.utils.base64url_decode(body)), "sub": str(uuid.uuid4())}).encode()
        ).decode()
        await self.invalid(f"{head}.{tampered}.{sig}")

    async def test_an_unknown_kid_refetches_once_and_then_backs_off(self):
        fake = self.serve(JWKS1, JWKS12, JWKS12)
        await J.verify(token())
        self.assertEqual(fake.calls, 1)
        # within JWKS_REFETCH_MIN_S of the last fetch even a real new key waits
        await self.invalid(token(key=KEY2, kid="kid-2"))
        self.assertEqual(fake.calls, 1)
        # a rotated key, once the back-off passed: the first token signed by it
        # triggers ONE refetch
        J.KEYS._last_attempt -= J.JWKS_REFETCH_MIN_S + 1
        await J.verify(token(key=KEY2, kid="kid-2"))
        self.assertEqual(fake.calls, 2)
        # a random kid inside the back-off window: refused, no further fetch
        for i in range(25):
            await self.invalid(token(kid=f"rand-{i}"))
        self.assertEqual(fake.calls, 2)

    async def test_concurrent_unknown_kids_share_one_fetch(self):
        fake = self.serve(JWKS12)
        results = await asyncio.gather(*[J.verify(token(key=KEY2, kid="kid-2")) for _ in range(20)])
        self.assertEqual({r["sub"] for r in results}, {SUB})
        self.assertEqual(fake.calls, 1)

    async def test_garbage_never_reaches_the_network(self):
        fake = self.serve(JWKS1)
        for bad in ("x.y.z", "a.b", "", "Bearer x", "a" * (J.MAX_TOKEN_CHARS + 1), "...", "eyJ.eyJ.sig"):
            with self.subTest(bad=bad[:20]):
                await self.invalid(bad)
        self.assertEqual(fake.calls, 0)

    async def test_the_claims_of_a_signed_in_user(self):
        self.serve(JWKS1)
        await self.invalid(token(role="service_role"))
        await self.invalid(token(is_anonymous=True))
        await self.invalid(token(sub="not-a-uuid"))
        await self.invalid(token(sub=None))

    async def test_a_jwks_outage_with_no_cached_key_is_unavailable(self):
        fake = self.serve(RuntimeError("connect timeout"))
        with self.assertRaises(J.KeysUnavailable):
            await J.verify(token())
        # inside the back-off: still unavailable, no second fetch
        with self.assertRaises(J.KeysUnavailable):
            await J.verify(token())
        self.assertEqual(fake.calls, 1)

    async def test_a_jwks_outage_keeps_the_cached_keys(self):
        self.serve(JWKS1, RuntimeError("503"))
        self.assertTrue(await J.refresh(force=True))
        self.assertFalse(await J.refresh(force=True))
        self.assertEqual((await J.verify(token()))["sub"], SUB)

    async def test_an_empty_or_symmetric_jwks_never_replaces_good_keys(self):
        self.serve(JWKS1, {"keys": [{"kty": "oct", "kid": "legacy", "alg": "HS256", "k": "c2VjcmV0"}]})
        await J.refresh(force=True)
        await J.refresh(force=True)
        self.assertEqual(J.KEYS.size(), 1)
        self.assertEqual((await J.verify(token()))["sub"], SUB)

    async def test_the_anon_key_rides_as_apikey_never_the_service_role_key(self):
        fake = self.serve(JWKS1)
        with patch.dict(os.environ, {"SUPABASE_ANON_KEY": "anon-public", "SUPABASE_SERVICE_ROLE_KEY": "srv"}):
            await J.refresh(force=True)
        self.assertEqual(fake.headers[-1].get("apikey"), "anon-public")
        self.assertNotIn("srv", json.dumps(fake.headers))

    def test_the_cached_check_never_fetches(self):
        J.KEYS.replace(J.parse_jwks(JWKS1))
        self.assertEqual(J.verified_subject(token()), SUB)
        self.assertIsNone(J.verified_subject(token(key=KEY2, kid="kid-2")))
        self.assertIsNone(J.verified_subject("x.y.z"))


class ParseJwks(unittest.TestCase):
    def test_only_asymmetric_signature_keys_of_an_allowed_algorithm(self):
        doc = {"keys": [
            _jwk(KEY1, "good"),
            {"kty": "oct", "kid": "sym", "alg": "HS256", "k": "c2VjcmV0"},
            {**_jwk(KEY2, "enc"), "use": "enc"},
            {**_jwk(KEY2, "noverify"), "key_ops": ["sign"]},
            {**_jwk(KEY2, "es384"), "alg": "ES384"},
            {**_jwk(KEY2, ""), "kid": ""},
            {**_jwk(KEY2, "x" * 200)},
            {**_jwk(RSA, "rsa-as-ec", "RS256"), "alg": "ES256"},
            "not a dict",
        ]}
        noalg = _jwk(KEY2, "noalg")
        del noalg["alg"]
        doc["keys"].append(noalg)
        self.assertEqual(sorted(J.parse_jwks(doc)), ["good", "noalg"])
        self.assertEqual(J.parse_jwks({"keys": "nope"}), {})
        self.assertEqual(J.parse_jwks(None), {})

    def test_the_mode_is_validated_at_boot(self):
        for good in ("", "local", "remote", " Remote "):
            with patch.dict(os.environ, {"AUTH_VERIFY_MODE": good}):
                J.validate_boot()
        with patch.dict(os.environ, {"AUTH_VERIFY_MODE": "off"}):
            with self.assertRaises(RuntimeError) as ctx:
                J.validate_boot()
            self.assertNotIn("off", str(ctx.exception))


class RemoteRollback(unittest.TestCase):
    def test_remote_mode_reads_the_claims_supabase_accepted(self):
        user = types.SimpleNamespace(id=SUB)
        sb = types.SimpleNamespace(auth=types.SimpleNamespace(get_user=lambda t: types.SimpleNamespace(user=user)))
        with patch.object(sys.modules["app.services.supabase_client"], "get_supabase", lambda: sb):
            claims = J.verify_remote_blocking(token(key=OTHER, kid="whatever", aal="aal2"))
        self.assertEqual((claims["sub"], claims["aal"]), (SUB, "aal2"))

    def test_remote_mode_refuses_what_supabase_refuses(self):
        def refuse(_t):
            raise RuntimeError("invalid JWT")

        sb = types.SimpleNamespace(auth=types.SimpleNamespace(get_user=refuse))
        with patch.object(sys.modules["app.services.supabase_client"], "get_supabase", lambda: sb):
            with self.assertRaises(J.TokenInvalid):
                J.verify_remote_blocking(token())
        other = types.SimpleNamespace(id=str(uuid.uuid4()))
        sb = types.SimpleNamespace(auth=types.SimpleNamespace(get_user=lambda t: types.SimpleNamespace(user=other)))
        with patch.object(sys.modules["app.services.supabase_client"], "get_supabase", lambda: sb):
            with self.assertRaises(J.TokenInvalid):
                J.verify_remote_blocking(token())


# ------------------------------------------------------------------ app/auth.py


def _load_auth():
    spec = importlib.util.spec_from_file_location("_auth_jwt_under_test", os.path.join(APP, "auth.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AUTH = _load_auth()
PROFILE = {"id": SUB, "role": "student", "username": "max", "full_name": "Max", "classroom_id": None,
           "workgroup_id": None, "training_credits": 0, "created_by": None, "hf_username": None,
           "email": "max@edubotics.local"}


class Dependencies(Base):
    def setUp(self):
        super().setUp()
        self.serve(JWKS1)
        self.reads = []

    def profiles(self, row):
        def read(user_id):
            self.reads.append(user_id)
            if isinstance(row, Exception):
                raise row
            return row

        p = patch.object(AUTH, "_read_profile", read)
        p.start()
        self.addCleanup(p.stop)

    async def status(self, header):
        with self.assertRaises(HTTPException) as ctx:
            await AUTH.get_current_user(authorization=header)
        return ctx.exception.status_code, ctx.exception.detail, getattr(ctx.exception, "code", None)

    async def test_a_valid_token_and_an_existing_account(self):
        self.profiles(dict(PROFILE))
        user = await AUTH.get_current_user(authorization="Bearer " + token())
        self.assertEqual((user.id, user.aal, user.profile["role"]), (SUB, "aal1", "student"))
        self.assertEqual(self.reads, [SUB])
        # the profile rides on the user: get_current_profile reads nothing again
        self.assertEqual((await AUTH.get_current_profile(user=user))["id"], SUB)
        self.assertEqual(self.reads, [SUB])

    async def test_the_old_framing_answers_are_unchanged(self):
        self.profiles(dict(PROFILE))
        self.assertEqual(await self.status(None), (401, "Anmeldung erforderlich.", None))
        self.assertEqual(await self.status("Basic abc"), (401, "Ungültige Anmeldedaten.", None))
        self.assertEqual(await self.status("Bearer x.y"), (401, AUTH.SESSION_INVALID_DE, None))
        self.assertEqual(await self.status("Bearer x.y.z"), (401, AUTH.SESSION_INVALID_DE, None))
        self.assertEqual((await AUTH.get_current_user(authorization="bearer " + token())).id, SUB)

    async def test_an_invalid_token_is_401_and_never_reads_the_database(self):
        self.profiles(dict(PROFILE))
        for bad in (token(key=OTHER), token(aud="anon"), token(exp=int(time.time()) - 600)):
            self.assertEqual(await self.status("Bearer " + bad), (401, AUTH.SESSION_INVALID_DE, None))
        self.assertEqual(self.reads, [])

    async def test_a_deleted_account_is_refused_at_once(self):
        self.profiles(None)
        self.assertEqual(await self.status("Bearer " + token()), (401, AUTH.ACCOUNT_GONE_DE, None))

    async def test_a_database_fault_is_503_never_a_sign_out(self):
        self.profiles(RuntimeError("connection reset"))
        self.assertEqual(await self.status("Bearer " + token()),
                         (503, AUTH.AUTH_UNAVAILABLE_DE, "auth_unavailable"))

    async def test_a_jwks_outage_is_503(self):
        J.KEYS.clear()
        self.serve(RuntimeError("down"))
        self.profiles(dict(PROFILE))
        self.assertEqual(await self.status("Bearer " + token()),
                         (503, AUTH.AUTH_UNAVAILABLE_DE, "auth_unavailable"))

    async def test_remote_mode_asks_supabase_in_a_thread(self):
        self.profiles(dict(PROFILE))
        calls = []

        def remote(tok):
            calls.append(tok)
            return J.check_claims(jwt.decode(tok, options={"verify_signature": False}))

        with patch.dict(os.environ, {"AUTH_VERIFY_MODE": "remote"}), patch.object(J, "verify_remote_blocking", remote):
            user = await AUTH.get_current_user(authorization="Bearer " + token(key=OTHER, kid="x"))
        self.assertEqual((user.id, len(calls)), (SUB, 1))


class AdminTwoFactor(Base):
    def setUp(self):
        super().setUp()
        self.serve(JWKS1)
        p = patch.object(AUTH, "_read_profile", lambda uid: {**PROFILE, "role": "admin"})
        p.start()
        self.addCleanup(p.stop)

    async def admin(self, **claims):
        user = await AUTH.get_current_user(authorization="Bearer " + token(**claims))
        return await AUTH.get_current_admin(user=user)

    async def test_an_aal1_admin_session_is_refused_with_the_code(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.admin(aal="aal1")
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail, ctx.exception.code),
                         (403, AUTH.MFA_REQUIRED_DE, "mfa_required"))
        with self.assertRaises(HTTPException):
            await self.admin(aal=None)

    async def test_an_aal2_admin_passes_and_is_logged_once_per_session(self):
        with self.assertLogs("_auth_jwt_under_test", level=logging.WARNING) as logs:
            self.assertEqual((await self.admin(aal="aal2", session_id="s-A"))["role"], "admin")
            await self.admin(aal="aal2", session_id="s-A")
            await self.admin(aal="aal2", session_id="s-B")
        marks = [r.getMessage() for r in logs.records if AUTH.ADMIN_SESSION_LOG_MARKER in r.getMessage()]
        self.assertEqual(marks, [f"{AUTH.ADMIN_SESSION_LOG_MARKER} user={SUB}"] * 2)
        self.assertNotIn("max@edubotics.local", " ".join(marks))

    async def test_teachers_and_students_are_untouched_by_the_second_factor(self):
        with patch.object(AUTH, "_read_profile", lambda uid: {**PROFILE, "role": "teacher"}):
            user = await AUTH.get_current_user(authorization="Bearer " + token(aal="aal1"))
            self.assertEqual((await AUTH.get_current_teacher(user=user))["role"], "teacher")


class CodedBody(unittest.TestCase):
    def test_the_handler_renders_detail_and_code(self):
        exc = AUTH.CodedHTTPException(403, AUTH.MFA_REQUIRED_DE, "mfa_required")
        resp = asyncio.run(AUTH.coded_http_exception_handler(None, exc))
        # the real JSONResponse renders `body`; a sibling suite's fastapi stub keeps `content`
        body = json.loads(resp.body) if hasattr(resp, "body") else resp.content
        self.assertEqual((resp.status_code, body),
                         (403, {"detail": AUTH.MFA_REQUIRED_DE, "code": "mfa_required"}))


if __name__ == "__main__":
    unittest.main()
