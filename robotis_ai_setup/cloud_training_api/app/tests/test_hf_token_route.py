"""routes/hf_token.py and its wiring (migration 042, a student's own HF token).

House pattern: stub fastapi + pydantic + the heavy leaf modules in sys.modules
(``test_dataset_identity_routes._ensure_stubs``), call the plain-`def` handlers
directly and hand them a fake supabase that emulates the one table and the one
RPC. The crypto, the shape rule and the identity helpers run for real; only the
Hub is faked.

German lint note: this file never binds an English string to ``detail=`` or a
``{"detail": ...}`` dict (german_detail_lint scans app/tests too).
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs
from app.tests.test_hf_credentials import OTHER_UID, SEQ_A, SEQ_B, TOK, UID, _Env

_ensure_stubs()

from fastapi import HTTPException  # noqa: E402
from app.routes import hf_token as route  # noqa: E402
from app.routes import me as meroute  # noqa: E402
from app.services import hf_credentials as hc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
MAIN_PY = os.path.join(APP, "main.py")
AUTH_PY = os.path.join(APP, "auth.py")
ROUTE_PY = os.path.join(APP, "routes", "hf_token.py")

STUDENT = {"id": UID, "role": "student", "hf_username": None}


def _profile(**over):
    return {**STUDENT, **over}


def _content(resp):
    """The JSON body of a success response, with the stub or with real Starlette."""
    if hasattr(resp, "body"):
        return json.loads(resp.body)
    return resp.content


def _headers(resp):
    return {k.lower(): v for k, v in dict(resp.headers).items()}


class FakeSupabase:
    """Emulates `user_hf_credentials` plus the `store_user_hf_credential` RPC and
    the few `users` / `trainings` calls the covered routes make."""

    def __init__(self):
        self.credentials: dict[str, dict] = {}
        self.users_hf: dict[str, str] = {}
        self.rpc_calls: list = []
        self.updated: list = []
        self.fail_rpc: Exception | None = None
        self.fail_read = False
        self.fail_delete = False

    def table(self, name):
        return _Query(self, name)

    def rpc(self, fn, params):
        self.rpc_calls.append((fn, dict(params)))
        return _Rpc(self, fn, params)


class _Rpc:
    def __init__(self, sb, fn, params):
        self.sb, self.fn, self.params = sb, fn, params

    def execute(self):
        if self.sb.fail_rpc is not None:
            raise self.sb.fail_rpc
        p = self.params
        # Migration 045: a non-NULL p_expected_fp makes the write conditional,
        # atomically, on the row still holding exactly that fingerprint.
        expected = p.get("p_expected_fp")
        if expected is not None:
            current = self.sb.credentials.get(p["p_user_id"])
            if current is None or current["token_fp"] != expected:
                raise RuntimeError(
                    "{'code': 'P0045', 'message': 'Das Token wurde inzwischen geändert oder entfernt.'}"
                )
        row = {
            "user_id": p["p_user_id"],
            "token_ciphertext": p["p_ciphertext"],
            "token_fp": p["p_fp"],
            "token_hint": p["p_hint"],
            "hf_username": p["p_hf_username"],
            "token_role": p["p_role"],
            "validated_at": "2026-10-03T12:00:00+00:00",
        }
        self.sb.credentials[p["p_user_id"]] = row
        self.sb.users_hf[p["p_user_id"]] = p["p_hf_username"]
        return SimpleNamespace(
            data={
                "stored": True,
                "hf_username": row["hf_username"],
                "hint": row["token_hint"],
                "fp": row["token_fp"],
                "role": row["token_role"],
                "validated_at": row["validated_at"],
            }
        )


class _Query:
    def __init__(self, sb, name):
        self.sb, self.name = sb, name
        self.op = None
        self.payload = None
        self.filters: dict = {}

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def in_(self, *_a, **_k):
        return self

    def is_(self, *_a, **_k):
        return self

    def execute(self):
        sb = self.sb
        if self.name == "user_hf_credentials":
            uid = self.filters.get("user_id")
            if self.op == "select":
                if sb.fail_read:
                    raise RuntimeError("read exploded")
                row = sb.credentials.get(uid)
                return SimpleNamespace(data=[dict(row)] if row else [])
            if self.op == "delete":
                if sb.fail_delete:
                    raise RuntimeError("delete exploded with detail-that-must-not-be-logged")
                row = sb.credentials.pop(uid, None)
                return SimpleNamespace(data=[row] if row else [])
        if self.name == "users" and self.op == "update":
            sb.updated.append(self.payload)
            return SimpleNamespace(data=[{"id": self.filters.get("id")}])
        return SimpleNamespace(data=[])


class _RouteCase(unittest.TestCase):
    """Real crypto under key A, a fake supabase and a fake Hub."""

    def setUp(self):
        self.env = _Env(SEQ_A)
        self.env.__enter__()
        self.sb = FakeSupabase()
        self._sb_patch = patch.object(route, "get_supabase", return_value=self.sb)
        self._hub_patch = patch.object(
            hc, "validate_with_hub", return_value={"name": "alice", "role": "write"}
        )
        self._sb_patch.start()
        self.hub = self._hub_patch.start()  # a MagicMock the tests steer

    def tearDown(self):
        self._hub_patch.stop()
        self._sb_patch.stop()
        self.env.__exit__(None, None, None)

    def put(self, token=TOK, profile=None):
        return route.put_hf_token(body={"token": token}, profile=profile or _profile())

    def assertHttp(self, status, call, *args, **kwargs):
        with self.assertRaises(HTTPException) as cm:
            call(*args, **kwargs)
        self.assertEqual(cm.exception.status_code, status, cm.exception.detail)
        return cm.exception


class TestRoundTrip(_RouteCase):
    def test_put_then_get_then_reveal(self) -> None:
        saved = self.put()
        body = _content(saved)
        self.assertEqual(
            {k: body[k] for k in ("stored", "usable", "hf_username", "hint", "fp", "role", "account_changed")},
            {
                "stored": True,
                "usable": True,
                "hf_username": "alice",
                "hint": "hf_…aaaa",
                "fp": "c1770a7966b0771e",
                "role": "write",
                "account_changed": False,
            },
        )
        self.assertTrue(body["validated_at"])

        status = _content(route.get_hf_token_status(profile=_profile()))
        self.assertEqual(
            status,
            {
                "stored": True,
                "usable": True,
                "hf_username": "alice",
                "hint": "hf_…aaaa",
                "fp": "c1770a7966b0771e",
                "role": "write",
                "validated_at": body["validated_at"],
            },
        )
        revealed = _content(route.reveal_hf_token(profile=_profile()))
        self.assertEqual(revealed, {"token": TOK, "fp": "c1770a7966b0771e"})

    def test_the_stored_row_is_an_envelope_and_never_the_plaintext(self) -> None:
        self.put()
        row = self.sb.credentials[UID]
        self.assertRegex(row["token_ciphertext"], r"^v1\.[0-9a-f]{8}\.[A-Za-z0-9_-]{32,}$")
        self.assertNotIn(TOK, json.dumps(row))
        fn, params = self.sb.rpc_calls[0]
        self.assertEqual(fn, "store_user_hf_credential")
        self.assertEqual(params["p_user_id"], UID)  # keyed to the JWT-verified profile
        self.assertEqual(
            sorted(params),
            ["p_ciphertext", "p_expected_fp", "p_fp", "p_hf_username", "p_hint", "p_role", "p_user_id"],
        )
        self.assertNotIn(TOK, json.dumps(params))

    def test_status_when_nothing_is_stored(self) -> None:
        self.assertEqual(
            _content(route.get_hf_token_status(profile=_profile())),
            {
                "stored": False,
                "usable": False,
                "hf_username": None,
                "hint": None,
                "fp": None,
                "role": None,
                "validated_at": None,
            },
        )

    def test_reveal_without_a_token_is_404_and_german(self) -> None:
        exc = self.assertHttp(404, route.reveal_hf_token, profile=_profile())
        self.assertIn("kein Hugging-Face-Token hinterlegt", exc.detail)

    def test_every_success_is_no_store(self) -> None:
        responses = [
            self.put(),
            route.get_hf_token_status(profile=_profile()),
            route.reveal_hf_token(profile=_profile()),
            route.verify_hf_token(profile=_profile()),
            route.delete_hf_token(profile=_profile()),
            route.get_hf_token_status(profile=_profile()),  # not stored
        ]
        for resp in responses:
            self.assertEqual(_headers(resp).get("cache-control"), "no-store")

    def test_the_fingerprint_belongs_to_the_account_that_reveals(self) -> None:
        self.put()
        # A row copied under another user id cannot be decrypted by that user (AAD).
        self.sb.credentials[OTHER_UID] = dict(self.sb.credentials[UID], user_id=OTHER_UID)
        exc = self.assertHttp(422, route.reveal_hf_token, profile=_profile(id=OTHER_UID))
        self.assertIn("nicht mehr entschlüsselt", exc.detail)

    def test_a_fingerprint_that_does_not_match_the_plaintext_is_refused(self) -> None:
        self.put()
        self.sb.credentials[UID]["token_fp"] = "0" * 16
        exc = self.assertHttp(422, route.reveal_hf_token, profile=_profile())
        self.assertIn("nicht mehr entschlüsselt", exc.detail)
        self.assertNotIn(TOK, repr(exc.detail))


class TestPutRejections(_RouteCase):
    def _no_hub_call(self):
        self.assertEqual(self.sb.rpc_calls, [])
        self.assertEqual(self.sb.credentials, {})

    def test_a_body_that_is_not_an_object_is_the_german_422_and_never_reaches_the_hub(self) -> None:
        # A JSON string/array body used to reach FastAPI's own validation, whose 422
        # carries the offending `input` - i.e. the token. Now it is read by hand.
        for body in (TOK, [TOK], None, 5, {"other": TOK}, {"token": None}):
            with patch.object(hc, "validate_with_hub") as hub:
                exc = self.assertHttp(422, route.put_hf_token, body=body, profile=_profile())
                hub.assert_not_called()
            self.assertIn("Das sieht nicht nach einem Hugging-Face-Token aus", exc.detail)
            self.assertNotIn(TOK, repr(exc.detail))
        self.assertEqual(self.sb.credentials, {})

    def test_shape_errors_are_422_and_never_echo_the_token(self) -> None:
        for bad in (TOK + "\n", " " + TOK, "hf_short", "hf_" + "a" * 257, "", None, 5, ["x"]):
            with patch.object(hc, "validate_with_hub") as hub:
                exc = self.assertHttp(422, self.put, token=bad)
                hub.assert_not_called()
            self.assertIn("Das sieht nicht nach einem Hugging-Face-Token aus", exc.detail)
            self.assertNotIn("a" * 16, repr(exc.detail))
            self.assertNotIn(TOK, repr(exc.detail) + str(exc))
        self._no_hub_call()

    def test_read_only_tokens_get_the_how_to(self) -> None:
        self.hub.return_value = {"name": "alice", "role": "read"}
        exc = self.assertHttp(422, self.put)
        self.assertIn("nur Leserechte", exc.detail)
        self.assertIn("„Settings“", exc.detail)
        self.assertIn("„Write“", exc.detail)
        self._no_hub_call()

    def test_the_deny_only_role_rule_lets_every_other_role_through(self) -> None:
        for role in ("write", "fineGrained", "god", "unknown", "admin"):
            self.hub.return_value = {"name": "alice", "role": role}
            self.assertEqual(_content(self.put())["role"], role)

    def test_a_fine_grained_token_that_cannot_write_to_the_own_account_gets_the_how_to(self) -> None:
        self.hub.return_value = {"name": "alice", "role": "fineGrained", "repo_write": False}
        exc = self.assertHttp(422, self.put)
        self.assertIn("nicht in deine eigenen Repositories schreiben", exc.detail)
        self.assertIn("„Write“", exc.detail)
        self.assertIn("„Repositories“", exc.detail)
        self.assertIn("„Settings“", exc.detail)
        self._no_hub_call()

    def test_a_fine_grained_token_that_can_write_or_cannot_be_judged_is_stored(self) -> None:
        # Fail OPEN: an unrecognised scope block (None) must never lock a student out.
        for verdict in (True, None):
            self.hub.return_value = {"name": "alice", "role": "fineGrained", "repo_write": verdict}
            self.assertEqual(_content(self.put())["role"], "fineGrained", verdict)

    def test_only_a_fine_grained_token_is_judged_by_its_scopes(self) -> None:
        # A write token never carries a verdict; even a stray False does not refuse it.
        self.hub.return_value = {"name": "alice", "role": "write", "repo_write": False}
        self.assertEqual(_content(self.put())["role"], "write")

    def test_a_denied_author_is_refused(self) -> None:
        for name in ("RobotisSW", "lerobot", "HuggingFace"):
            self.hub.return_value = {"name": name, "role": "write"}
            exc = self.assertHttp(422, self.put)
            self.assertIn("nicht zulässig", exc.detail)
        self._no_hub_call()

    def test_a_name_that_is_not_a_valid_account_name_is_refused(self) -> None:
        for name in ("-bad", "bad_name", "with space", "a" * 97):
            self.hub.return_value = {"name": name, "role": "write"}
            exc = self.assertHttp(422, self.put)
            self.assertIn("Kontoname dieses Tokens ist ungültig", exc.detail)
        self._no_hub_call()

    def test_the_hub_refusing_is_422_and_the_hub_down_is_502(self) -> None:
        self.hub.side_effect = hc.HfCredentialError("rejected")
        exc = self.assertHttp(422, self.put)
        self.assertIn("Hugging Face hat dieses Token abgelehnt", exc.detail)
        for code in ("unreachable", "bad_reply"):
            self.hub.side_effect = hc.HfCredentialError(code)
            exc = self.assertHttp(502, self.put)
            self.assertIn("gerade nicht erreichbar", exc.detail)
        self._no_hub_call()

    def test_no_token_semantics_error_is_ever_401_or_403(self) -> None:
        # The SPA signs the student out on 401/403 from /me.
        scenarios = (
            {"token": TOK + "\n"},
            {"token": None},
            {"ret": {"name": "alice", "role": "read"}},
            {"ret": {"name": "RobotisSW", "role": "write"}},
            {"ret": {"name": "-bad", "role": "write"}},
            {"effect": "rejected"},
            {"effect": "unreachable"},
            {"effect": "bad_reply"},
        )
        for sc in scenarios:
            self.hub.side_effect = hc.HfCredentialError(sc["effect"]) if "effect" in sc else None
            self.hub.return_value = sc.get("ret", {"name": "alice", "role": "write"})
            with self.assertRaises(HTTPException) as cm:
                self.put(token=sc.get("token", TOK))
            self.assertIn(cm.exception.status_code, (422, 502), sc)

    def test_a_database_failure_is_500_and_the_log_names_only_the_class(self) -> None:
        self.sb.fail_rpc = RuntimeError("db exploded with " + TOK)
        with self.assertLogs(level="DEBUG") as logs:
            exc = self.assertHttp(500, self.put)
        self.assertIn("Das Token konnte nicht gespeichert werden", exc.detail)
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(TOK, text)
        self.assertNotIn("db exploded", text)

    def test_an_unknown_user_from_the_rpc_is_404(self) -> None:
        for text in ("P0002 Benutzer nicht gefunden", "something P0002 else"):
            self.sb.fail_rpc = RuntimeError(text)
            exc = self.assertHttp(404, self.put)
            self.assertIn("Benutzerkonto nicht gefunden", exc.detail)

    def test_account_changed(self) -> None:
        self.assertFalse(_content(self.put(profile=_profile(hf_username=None)))["account_changed"])
        self.assertFalse(_content(self.put(profile=_profile(hf_username="Alice")))["account_changed"])
        self.assertTrue(_content(self.put(profile=_profile(hf_username="oldname")))["account_changed"])


class TestAccessControl(_RouteCase):
    def _all_routes(self, profile):
        return (
            lambda: route.get_hf_token_status(profile=profile),
            lambda: self.put(profile=profile),
            lambda: route.delete_hf_token(profile=profile),
            lambda: route.reveal_hf_token(profile=profile),
            lambda: route.verify_hf_token(profile=profile),
        )

    def test_students_only_on_all_five_routes(self) -> None:
        for role in ("teacher", "admin"):
            for call in self._all_routes(_profile(role=role)):
                exc = self.assertHttp(403, call)
                self.assertEqual(exc.detail, "Nur Schüler können ein Hugging-Face-Token hinterlegen.")
        self.assertEqual(self.sb.rpc_calls, [])

    def test_a_forged_body_cannot_name_another_user(self) -> None:
        # No route takes a user id; the RPC is always keyed to the verified profile.
        self.put()
        self.assertEqual({c[1]["p_user_id"] for c in self.sb.rpc_calls}, {UID})
        import inspect

        for fn in (route.get_hf_token_status, route.put_hf_token, route.delete_hf_token,
                   route.reveal_hf_token, route.verify_hf_token):
            params = [p for p in inspect.signature(fn).parameters if p not in ("profile", "body")]
            self.assertEqual(params, [], fn.__name__)

    def test_without_a_key_everything_but_delete_is_503(self) -> None:
        self.sb.credentials[UID] = {"token_ciphertext": "x", "token_fp": "y"}
        with _Env():
            for call in (
                lambda: route.get_hf_token_status(profile=_profile()),
                lambda: self.put(),
                lambda: route.reveal_hf_token(profile=_profile()),
                lambda: route.verify_hf_token(profile=_profile()),
            ):
                exc = self.assertHttp(503, call)
                self.assertIn("noch nicht eingerichtet", exc.detail)
            # A student can always take the token away, key or no key.
            self.assertEqual(_content(route.delete_hf_token(profile=_profile())), {"stored": False})
        self.assertNotIn(UID, self.sb.credentials)


class TestDeleteAndVerify(_RouteCase):
    def test_delete_is_idempotent_and_keeps_the_proven_username(self) -> None:
        self.put()
        self.assertEqual(self.sb.users_hf[UID], "alice")
        for _ in range(2):
            self.assertEqual(_content(route.delete_hf_token(profile=_profile())), {"stored": False})
        self.assertEqual(self.sb.credentials, {})
        self.assertEqual(self.sb.users_hf[UID], "alice")  # DELETE never touches users
        self.assertEqual(self.sb.updated, [])

    def test_delete_failure_is_german_500_without_the_cause(self) -> None:
        self.sb.fail_delete = True
        with self.assertLogs(level="DEBUG") as logs:
            exc = self.assertHttp(500, route.delete_hf_token, profile=_profile())
        self.assertIn("nicht entfernt werden", exc.detail)
        self.assertNotIn("detail-that-must-not-be-logged", "\n".join(logs.output))

    def test_verify_refreshes_and_reencrypts_under_the_current_key(self) -> None:
        with _Env(SEQ_B):  # the token was saved under key B ...
            self.put()
        first = self.sb.credentials[UID]["token_ciphertext"]
        self.assertTrue(first.startswith("v1." + hc.key_id(SEQ_B) + "."))
        with _Env(SEQ_A, SEQ_B):  # ... then the key rotated to A, B moved to PREVIOUS
            self.assertTrue(_content(route.get_hf_token_status(profile=_profile()))["usable"])
            body = _content(route.verify_hf_token(profile=_profile()))
            self.assertTrue(body["stored"] and body["usable"])
            second = self.sb.credentials[UID]["token_ciphertext"]
            self.assertTrue(second.startswith("v1." + hc.key_id(SEQ_A) + "."))
        with _Env(SEQ_A):  # PREVIOUS dropped: the re-encrypted token still works
            self.assertEqual(_content(route.reveal_hf_token(profile=_profile()))["token"], TOK)

    def test_a_token_whose_key_is_gone_reads_as_not_usable_and_cannot_be_revealed(self) -> None:
        with _Env(SEQ_B):
            self.put()
        status = _content(route.get_hf_token_status(profile=_profile()))
        self.assertTrue(status["stored"])
        self.assertFalse(status["usable"])
        for call in (route.reveal_hf_token, route.verify_hf_token):
            exc = self.assertHttp(422, call, profile=_profile())
            self.assertIn("nicht mehr entschlüsselt", exc.detail)

    def test_verify_does_not_resurrect_a_token_removed_while_the_hub_was_asked(self) -> None:
        self.put()

        def hub_then_delete(_token):
            route.delete_hf_token(profile=_profile())  # the student presses „Entfernen“ meanwhile
            return {"name": "alice", "role": "write"}

        self.hub.side_effect = hub_then_delete
        exc = self.assertHttp(409, route.verify_hf_token, profile=_profile())
        self.assertIn("geändert oder entfernt", exc.detail)
        self.assertEqual(self.sb.credentials, {})

    def test_verify_does_not_overwrite_a_token_saved_while_the_hub_was_asked(self) -> None:
        self.put()
        other = "hf_" + "b" * 34

        def hub_then_replace(_token):
            self.hub.side_effect = None
            self.put(token=other)  # a newer PUT lands while verify waits for the Hub
            return {"name": "alice", "role": "write"}

        self.hub.side_effect = hub_then_replace
        self.assertHttp(409, route.verify_hf_token, profile=_profile())
        self.assertEqual(_content(route.reveal_hf_token(profile=_profile()))["token"], other)

    def test_put_writes_unconditionally_and_verify_names_the_fingerprint_it_decrypted(self) -> None:
        self.put()
        fn, params = self.sb.rpc_calls[-1]
        self.assertEqual(fn, "store_user_hf_credential")
        self.assertIn("p_expected_fp", params)          # always sent: the probe names it
        self.assertIsNone(params["p_expected_fp"])
        route.verify_hf_token(profile=_profile())
        self.assertEqual(self.sb.rpc_calls[-1][1]["p_expected_fp"], "c1770a7966b0771e")

    def test_the_conditional_write_lives_in_the_rpc_not_in_a_python_re_read(self) -> None:
        # The old re-read left the gap between the read and the write open: a
        # DELETE landing in it was resurrected by the blind upsert (owner item d).
        self.put()
        reads = []
        real = route._read_rows

        def counting(uid, columns):
            reads.append(columns)
            return real(uid, columns)

        with patch.object(route, "_read_rows", side_effect=counting):
            route.verify_hf_token(profile=_profile())
        self.assertEqual(reads, [route._COLUMNS_SECRET])

    def test_the_rpcs_p0045_is_the_german_409(self) -> None:
        self.put()
        self.sb.fail_rpc = RuntimeError("{'code': 'P0045', 'message': 'x'}")
        exc = self.assertHttp(409, route.verify_hf_token, profile=_profile())
        self.assertIn("geändert oder entfernt", exc.detail)

    def test_verify_when_the_hub_has_since_revoked_the_token(self) -> None:
        self.put()
        self.hub.side_effect = hc.HfCredentialError("rejected")
        exc = self.assertHttp(422, route.verify_hf_token, profile=_profile())
        self.assertIn("dein gespeichertes Token abgelehnt", exc.detail)
        self.hub.side_effect = hc.HfCredentialError("unreachable")
        self.assertHttp(502, route.verify_hf_token, profile=_profile())
        self.hub.side_effect = None
        self.hub.return_value = {"name": "alice", "role": "read"}
        exc = self.assertHttp(422, route.verify_hf_token, profile=_profile())
        self.assertIn("nur Leserechte", exc.detail)

    def test_a_database_read_failure_is_a_german_500_everywhere(self) -> None:
        self.sb.fail_read = True
        for call in (route.get_hf_token_status, route.reveal_hf_token, route.verify_hf_token):
            exc = self.assertHttp(500, call, profile=_profile())
            self.assertIn("Token-Status konnte nicht geladen werden", exc.detail)


class TestNothingSecretIsLogged(_RouteCase):
    def test_a_full_session_logs_neither_the_token_nor_its_fingerprint_nor_the_ciphertext(self) -> None:
        with self.assertLogs(level="DEBUG") as logs:
            self.put()
            route.get_hf_token_status(profile=_profile())
            route.reveal_hf_token(profile=_profile())
            route.verify_hf_token(profile=_profile())
            ciphertext = self.sb.credentials[UID]["token_ciphertext"]
            route.delete_hf_token(profile=_profile())
        text = "\n".join(logs.output)
        self.assertIn(UID, text)  # the user id and the event are logged
        self.assertNotIn(TOK, text)
        self.assertNotIn(hc.fingerprint(TOK), text)
        self.assertNotIn(ciphertext, text)
        self.assertIsNone(re.search(r"hf_[A-Za-z0-9_-]{16,}", text))

    def test_no_logger_call_in_the_route_module_formats_a_secret(self) -> None:
        with open(ROUTE_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        banned = {"token", "ciphertext", "fp", "hint", "body", "row", "exc", "data", "res"}
        checked = 0
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                continue
            checked += 1
            for arg in node.args[1:]:
                # `type(exc).__name__` is the one sanctioned use of `exc`.
                allowed = ast.unparse(arg) == "type(exc).__name__"
                names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
                self.assertTrue(allowed or not (names & banned), ast.unparse(node))
        self.assertGreaterEqual(checked, 7)


class TestPatchMe(unittest.TestCase):
    def setUp(self):
        self.sb = FakeSupabase()
        self._patch = patch.object(meroute, "get_supabase", return_value=self.sb)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _call(self, raw="alice", profile=None):
        body = meroute.HfUsernameUpdate(hf_username=raw)
        fresh = {**_profile(), "username": "s", "full_name": "S", "classroom_id": None,
                 "workgroup_id": None, "training_credits": 0, "hf_username": raw}
        with patch.object(meroute, "get_user_profile", return_value=fresh):
            return asyncio.run(meroute.update_me(body=body, profile=profile or _profile()))

    def test_409_when_a_credential_exists_and_nothing_is_written(self) -> None:
        self.sb.credentials[UID] = {"user_id": UID}
        with self.assertRaises(HTTPException) as cm:
            self._call("someone-else")
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(
            cm.exception.detail,
            "Dein Hugging-Face-Konto ist über dein gespeichertes Token festgelegt. "
            "Entferne zuerst das Token auf der Startseite, wenn du ein anderes Konto "
            "verwenden möchtest.",
        )
        self.assertEqual(self.sb.updated, [])

    def test_a_token_stored_between_the_check_and_the_update_is_still_the_409(self) -> None:
        # Migration 044's users trigger raises P0044 when hf_username would leave
        # the stored credential's name; the check-then-write is atomic in SQL.
        real_table = self.sb.table

        def table(name):
            query = real_table(name)
            if name == "users":
                def execute():
                    raise RuntimeError("{'code': 'P0044', 'message': 'Dein Hugging-Face-Konto ...'}")
                query.execute = execute
            return query

        with patch.object(self.sb, "table", side_effect=table):
            with self.assertRaises(HTTPException) as cm:
                self._call("someone-else")
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("über dein gespeichertes Token festgelegt", cm.exception.detail)

    def test_still_200_without_a_credential(self) -> None:
        result = self._call("alice")
        self.assertEqual(self.sb.updated, [{"hf_username": "alice"}])
        self.assertEqual(result.hf_username, "alice")

    def test_another_students_credential_does_not_block_me(self) -> None:
        self.sb.credentials[OTHER_UID] = {"user_id": OTHER_UID}
        self._call("alice")
        self.assertEqual(self.sb.updated, [{"hf_username": "alice"}])

    def test_a_failing_lookup_is_a_german_500_not_a_silent_write(self) -> None:
        self.sb.fail_read = True
        with self.assertLogs(level="DEBUG") as logs:
            with self.assertRaises(HTTPException) as cm:
                self._call("alice")
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIn("Benutzer-ID konnte nicht gespeichert werden", cm.exception.detail)
        self.assertEqual(self.sb.updated, [])
        self.assertNotIn("read exploded", "\n".join(logs.output))


class TestDeleteMyAccountRemovesTheToken(unittest.TestCase):
    """Owner decision (M9): POST /me/delete also deletes the stored token, best
    effort, and never blocks the request."""

    def setUp(self):
        self.sb = FakeSupabase()
        self._patch = patch.object(meroute, "get_supabase", return_value=self.sb)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _call(self):
        profile = {"id": UID, "role": "student", "workgroup_id": None}
        return asyncio.run(meroute.delete_my_account(profile=profile))

    def test_the_credential_row_is_deleted_and_the_message_says_so(self) -> None:
        self.sb.credentials[UID] = {"user_id": UID}
        out = self._call()
        self.assertEqual(self.sb.credentials, {})
        self.assertTrue(out["hf_token_removed"])
        self.assertIs(out["deletion_performed"], False)
        self.assertIn("Hugging-Face-Token vom Server entfernt", out["message"])
        self.assertIn("KEINE Daten gelöscht", out["message"])

    def test_with_no_credential_the_message_claims_nothing(self) -> None:
        out = self._call()
        self.assertFalse(out["hf_token_removed"])
        self.assertNotIn("Hugging-Face-Token", out["message"])

    def test_a_failing_delete_never_blocks_the_request_and_logs_only_the_class(self) -> None:
        self.sb.credentials[UID] = {"user_id": UID}
        self.sb.fail_delete = True
        with self.assertLogs(level="DEBUG") as logs:
            out = self._call()
        self.assertEqual(out["status"], "requested")
        self.assertFalse(out["hf_token_removed"])
        self.assertNotIn("Hugging-Face-Token", out["message"])
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("detail-that-must-not-be-logged", text)

    def test_other_users_credentials_are_untouched(self) -> None:
        self.sb.credentials[OTHER_UID] = {"user_id": OTHER_UID}
        self._call()
        self.assertIn(OTHER_UID, self.sb.credentials)


class TestExportCannotCarryTheCiphertext(unittest.TestCase):
    def test_the_profile_select_is_explicit_and_names_no_credential_column(self) -> None:
        with open(AUTH_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "get_user_profile")
        strings = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        selects = [s for s in strings if "workgroup_id" in s]
        self.assertTrue(selects)
        for s in strings:
            self.assertNotEqual(s.strip(), "*")
            self.assertNotIn("token_ciphertext", s)
        self.assertNotIn("user_hf_credentials", " ".join(strings))

    def test_the_export_handler_never_reads_the_credentials_table(self) -> None:
        import inspect

        src = inspect.getsource(meroute.export_my_data)
        self.assertNotIn("user_hf_credentials", src)
        self.assertNotIn("token_ciphertext", src)


def _main_tree():
    with open(MAIN_PY, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=MAIN_PY)


def _module_assign(tree, name):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in main.py")


def _match(rules, method, path):
    """The middleware's matcher: longest prefix first, method-pinned, anchored."""
    for rule_method, prefix, limit, window in sorted(rules, key=lambda r: len(r[1]), reverse=True):
        if rule_method != "*" and rule_method != method:
            continue
        matched = path.startswith(prefix) if prefix.endswith("/") else (
            path == prefix or path.startswith(prefix + "/"))
        if matched:
            return (rule_method, prefix, limit, window)
    return None


class TestMainWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = _main_tree()
        cls.rules = _module_assign(cls.tree, "_RATE_LIMIT_RULES")

    def test_the_rate_rules(self) -> None:
        self.assertEqual(_match(self.rules, "PUT", "/me/hf-token"), ("PUT", "/me/hf-token", 6, 60.0))
        self.assertEqual(_match(self.rules, "GET", "/me/hf-token"), ("GET", "/me/hf-token", 60, 60.0))
        self.assertEqual(_match(self.rules, "DELETE", "/me/hf-token"), ("DELETE", "/me/hf-token", 10, 60.0))
        self.assertEqual(
            _match(self.rules, "POST", "/me/hf-token/reveal"),
            ("POST", "/me/hf-token/reveal", 20, 60.0),
        )
        self.assertEqual(
            _match(self.rules, "POST", "/me/hf-token/verify"),
            ("POST", "/me/hf-token/verify", 6, 60.0),
        )

    def test_neighbouring_routes_are_unaffected(self) -> None:
        self.assertEqual(_match(self.rules, "GET", "/me/export"), ("GET", "/me/export", 3, 3600.0))
        self.assertIsNone(_match(self.rules, "GET", "/me"))
        self.assertIsNone(_match(self.rules, "PATCH", "/me"))
        self.assertIsNone(_match(self.rules, "POST", "/me/hf-token"))  # no such route
        self.assertIsNone(_match(self.rules, "POST", "/me/hf-tokens"))
        self.assertEqual(
            _match(self.rules, "PATCH", "/me/tutorial-progress/x"),
            ("PATCH", "/me/tutorial-progress", 30, 60.0),
        )

    def test_the_prefix_is_rate_limited_per_user_not_per_ip(self) -> None:
        self.assertIn("/me/hf-token", _module_assign(self.tree, "_PER_USER_RATE_LIMIT_PREFIXES"))

    def test_the_put_body_is_size_limited(self) -> None:
        self.assertIn(("PUT", "/me/hf-token"), _module_assign(self.tree, "_BODY_SIZE_LIMITED_PREFIXES"))

    def test_the_router_is_included_after_me(self) -> None:
        includes = [
            n.value.args[0].id
            for n in self.tree.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "include_router"
        ]
        self.assertIn("hf_token_router", includes)
        self.assertEqual(includes.index("hf_token_router"), includes.index("me_router") + 1)

    def test_the_key_validation_runs_right_after_the_required_secrets(self) -> None:
        calls = [
            n.value.func.id
            for n in self.tree.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
        ]
        self.assertEqual(
            calls.index("_validate_hf_token_key"), calls.index("_validate_required_secrets") + 1
        )

    def _validate_fn(self):
        fn = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == "_validate_hf_token_key")
        ns: dict = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), MAIN_PY, "exec"), ns)  # noqa: S102
        return ns["_validate_hf_token_key"]

    def _required_fn(self):
        fn = next(n for n in self.tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_validate_required_secrets")
        ns: dict = {"os": os}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), MAIN_PY, "exec"), ns)  # noqa: S102
        return ns["_validate_required_secrets"]

    def test_an_absent_or_empty_key_stops_the_boot_like_every_required_secret(self) -> None:
        # Owner decision S2 (2026-10-04): without the key no student can store a
        # token, so the deploy must fail instead of booting into a 503 for all.
        required = self._required_fn()
        base = {"SUPABASE_URL": "http://ci.test", "SUPABASE_SERVICE_ROLE_KEY": "x",
                "MODAL_TOKEN_ID": "x", "MODAL_TOKEN_SECRET": "x"}
        for key_value in (None, ""):
            env = dict(base)
            if key_value is not None:
                env[hc.KEY_ENV] = key_value
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(RuntimeError) as cm:
                    required()
            self.assertIn(hc.KEY_ENV, str(cm.exception))
        with patch.dict(os.environ, {**base, hc.KEY_ENV: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
                        clear=True):
            required()

    def test_the_key_check_on_its_own_tolerates_absence_and_refuses_a_malformed_key(self) -> None:
        # _validate_hf_token_key is the malformed-key half; absence is the
        # required-secrets check's job (above), so on its own it stays quiet.
        validate = self._validate_fn()
        for env in ({}, {hc.KEY_ENV: ""}):
            with _Env():
                with patch.dict(os.environ, env):
                    validate()
        with _Env():
            with patch.dict(os.environ, {hc.KEY_ENV: "definitely not base64!"}):
                with self.assertRaises(RuntimeError) as cm:
                    validate()
        self.assertIn(hc.KEY_ENV, str(cm.exception))
        self.assertNotIn("definitely not base64", str(cm.exception))
        with _Env(SEQ_A):
            validate()

    def test_the_key_is_a_required_secret_and_no_longer_an_optional_warning(self) -> None:
        def strings_of(name):
            fn = next(n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            return [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]

        self.assertIn("EDUBOTICS_HF_TOKEN_KEY", strings_of("_validate_required_secrets"))
        self.assertNotIn("EDUBOTICS_HF_TOKEN_KEY", strings_of("_warn_optional_secrets"))


if __name__ == "__main__":
    unittest.main()
