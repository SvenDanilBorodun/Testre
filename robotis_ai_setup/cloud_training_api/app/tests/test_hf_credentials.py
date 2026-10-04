"""services/hf_credentials.py: shape, fingerprint, AES-256-GCM envelope, key
handling and the Hub check (migration 042, a student's own Hugging Face token).

The module imports `cryptography` and `httpx` lazily, so this file needs the
former (CI's python-tests pip line lists `cryptography`) and a FAKE of the
latter that it patches into sys.modules per test.

Fixtures are deliberately low-entropy and are never assigned to a name that
says token/key/secret next to a long literal (gitleaks scans the history).
"""

from __future__ import annotations

import base64
import os
import sys
import threading
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services import hf_credentials as hc

UID = "11111111-2222-3333-4444-555555555555"
OTHER_UID = "99999999-8888-7777-6666-555555555555"
TOK = "hf_" + "a" * 34


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


SEQ_A = bytes(range(32))
SEQ_B = bytes(range(0x20, 0x40))
KID_A = "3b28fa93"
KID_B = "cfc9913b"
# Envelope of TOK under SEQ_A, nonce bytes(range(0xa0, 0xac)), AAD = UID.
VECTOR_ENVELOPE = (
    "v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY"
)


class _Env:
    """Patch the key variables and drop the module's key cache around a test."""

    def __init__(self, current=None, previous=None):
        self.values = {}
        if current is not None:
            self.values[hc.KEY_ENV] = _b64(current)
        if previous is not None:
            self.values[hc.PREVIOUS_KEY_ENV] = _b64(previous)

    def __enter__(self):
        self._patch = patch.dict(os.environ, self.values)
        self._patch.start()
        if hc.KEY_ENV not in self.values:
            os.environ.pop(hc.KEY_ENV, None)
        if hc.PREVIOUS_KEY_ENV not in self.values:
            os.environ.pop(hc.PREVIOUS_KEY_ENV, None)
        hc._reset_for_tests()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        hc._reset_for_tests()


class TestShapeAndFingerprint(unittest.TestCase):
    def test_fingerprint_vectors(self) -> None:
        for token, fp in (
            ("hf_" + "a" * 34, "c1770a7966b0771e"),
            ("hf_" + "abcdefghijklmnopqrstuvwxyzABCDEFGH", "006da5aabd66bbc9"),
            ("hf_0123456789_-0123456789", "ce20c2c58e1061d5"),
            ("hf_" + "Z" * 16, "c287328277ed770f"),
            ("hf_" + "x" * 256, "40d885025c3959d6"),
        ):
            self.assertEqual(hc.fingerprint(token), fp, token[:12])
            self.assertEqual(len(hc.fingerprint(token)), 16)

    def test_shape_matrix(self) -> None:
        self.assertFalse(hc.valid_shape("hf_" + "a" * 15))
        self.assertTrue(hc.valid_shape("hf_" + "a" * 16))
        self.assertTrue(hc.valid_shape("hf_" + "a" * 256))
        self.assertFalse(hc.valid_shape("hf_" + "a" * 257))
        self.assertTrue(hc.valid_shape("hf_0123456789_-0123456789"))

    def test_shape_rejects_whitespace_and_non_strings(self) -> None:
        # `$` would let a trailing newline through; fullmatch must not.
        self.assertFalse(hc.valid_shape(TOK + "\n"))
        self.assertFalse(hc.valid_shape(" " + TOK))
        self.assertFalse(hc.valid_shape(TOK + " "))
        self.assertFalse(hc.valid_shape("hf_" + "a" * 20 + "\n"))
        self.assertFalse(hc.valid_shape("HF_" + "a" * 34))
        self.assertFalse(hc.valid_shape("hf_" + "é" * 20))
        for bad in (5, None, b"hf_" + b"a" * 34, ["hf_" + "a" * 34], {"t": TOK}, ""):
            self.assertFalse(hc.valid_shape(bad), repr(bad)[:30])

    def test_hint_shows_only_the_last_four_characters(self) -> None:
        self.assertEqual(hc.hint(TOK), "hf_…aaaa")
        self.assertEqual(hc.hint("hf_" + "abcdefghijklmnopqrstuvwxyzABCDEFGH"), "hf_…EFGH")


class TestKeyHandling(unittest.TestCase):
    def test_absent_key_is_none_and_not_configured(self) -> None:
        self.assertEqual(hc.load_keys({}), {"current": None, "previous": None})
        self.assertEqual(hc.load_keys({hc.KEY_ENV: "   "}), {"current": None, "previous": None})
        with _Env():
            self.assertFalse(hc.configured())

    def test_a_valid_key_has_the_documented_key_id(self) -> None:
        keys = hc.load_keys({hc.KEY_ENV: _b64(SEQ_A), hc.PREVIOUS_KEY_ENV: _b64(SEQ_B)})
        self.assertEqual(keys["current"], (KID_A, SEQ_A))
        self.assertEqual(keys["previous"], (KID_B, SEQ_B))
        self.assertEqual(hc.key_id(SEQ_A), KID_A)
        self.assertEqual(hc.key_id(SEQ_B), KID_B)

    def test_surrounding_whitespace_in_the_variable_is_tolerated(self) -> None:
        keys = hc.load_keys({hc.KEY_ENV: "  " + _b64(SEQ_A) + "\n"})
        self.assertEqual(keys["current"][0], KID_A)

    def test_present_but_malformed_keys_refuse_and_name_only_the_variable(self) -> None:
        secretish = "!!!not base64 at all!!!"
        cases = (
            ({hc.KEY_ENV: secretish}, hc.KEY_ENV),
            ({hc.KEY_ENV: _b64(b"abc")}, hc.KEY_ENV),  # 3 bytes
            ({hc.KEY_ENV: _b64(bytes(33))}, hc.KEY_ENV),  # 33 bytes
            ({hc.KEY_ENV: _b64(SEQ_A), hc.PREVIOUS_KEY_ENV: secretish}, hc.PREVIOUS_KEY_ENV),
            ({hc.PREVIOUS_KEY_ENV: _b64(SEQ_B)}, hc.PREVIOUS_KEY_ENV),  # previous without current
            ({hc.KEY_ENV: _b64(SEQ_A), hc.PREVIOUS_KEY_ENV: _b64(SEQ_A)}, hc.PREVIOUS_KEY_ENV),
        )
        for env, name in cases:
            with self.assertRaises(RuntimeError) as cm:
                hc.load_keys(env)
            message = str(cm.exception)
            self.assertIn(name, message)
            for value in env.values():
                self.assertNotIn(value.strip(), message)

    def test_a_stray_character_inside_a_key_is_refused_not_dropped(self) -> None:
        # b64decode WITHOUT validate=True silently discards characters outside the
        # alphabet, so a key with a typo in it would still decode to 32 bytes and
        # quietly become a DIFFERENT key (every stored token then reads as unusable).
        good = _b64(SEQ_A)
        for bad in (good[:10] + "!" + good[10:], good[:10] + " " + good[10:], good[:10] + "\n" + good[10:]):
            with self.assertRaises(RuntimeError):
                hc.load_keys({hc.KEY_ENV: bad})


class TestEnvelope(unittest.TestCase):
    def test_the_published_vector_is_reproduced_bit_for_bit(self) -> None:
        envelope = hc._seal(SEQ_A, KID_A, bytes(range(0xA0, 0xAC)), UID, TOK)
        self.assertEqual(envelope, VECTOR_ENVELOPE)
        self.assertEqual(len(envelope), 99)

    def test_the_published_vector_decrypts(self) -> None:
        with _Env(SEQ_A):
            self.assertEqual(hc.decrypt(UID, VECTOR_ENVELOPE), TOK)

    def test_round_trip_and_envelope_format(self) -> None:
        with _Env(SEQ_A):
            envelope = hc.encrypt(UID, TOK)
            self.assertRegex(envelope, r"^v1\.[0-9a-f]{8}\.[A-Za-z0-9_-]{32,}$")
            self.assertTrue(envelope.startswith("v1." + KID_A + "."))
            self.assertNotIn(TOK, envelope)
            self.assertEqual(hc.decrypt(UID, envelope), TOK)
            self.assertTrue(hc.usable(envelope))

    def test_a_fresh_nonce_per_encryption(self) -> None:
        with _Env(SEQ_A):
            seen = {hc.encrypt(UID, TOK) for _ in range(200)}
        self.assertEqual(len(seen), 200)

    def test_another_users_id_cannot_decrypt(self) -> None:
        with _Env(SEQ_A):
            with self.assertRaises(hc.HfCredentialError) as cm:
                hc.decrypt(OTHER_UID, VECTOR_ENVELOPE)
            self.assertEqual(cm.exception.code, "undecryptable")

    def test_the_user_id_is_case_insensitive_in_the_aad(self) -> None:
        with _Env(SEQ_A):
            self.assertEqual(hc.decrypt(UID.upper(), VECTOR_ENVELOPE), TOK)

    def test_a_different_key_cannot_decrypt(self) -> None:
        with _Env(SEQ_A):
            envelope = hc.encrypt(UID, TOK)
        # Same kid claimed by an envelope sealed under another key: forge it.
        forged = hc._seal(SEQ_B, KID_A, bytes(12), UID, TOK)
        with _Env(SEQ_A):
            with self.assertRaises(hc.HfCredentialError) as cm:
                hc.decrypt(UID, forged)
            self.assertEqual(cm.exception.code, "undecryptable")
            self.assertEqual(hc.decrypt(UID, envelope), TOK)

    def test_tampering_and_garbage_are_undecryptable_never_an_InvalidTag(self) -> None:
        head, kid, blob = VECTOR_ENVELOPE.split(".")
        flipped = blob[:10] + ("B" if blob[10] != "B" else "C") + blob[11:]
        cases = (
            f"{head}.{kid}.{flipped}",
            f"{head}.{kid}.{blob[:-4]}",
            f"{head}.{kid}.{blob[:20]}",  # short blob
            f"{head}.{kid}.!!!!",
            f"v2.{kid}.{blob}",
            f"{head}.ZZZZZZZZ.{blob}",
            f"{head}.{kid}",
            "",
            "not an envelope",
            None,
            5,
        )
        with _Env(SEQ_A):
            for envelope in cases:
                with self.assertRaises(hc.HfCredentialError) as cm:
                    hc.decrypt(UID, envelope)
                self.assertEqual(cm.exception.code, "undecryptable", repr(envelope)[:40])

    def test_no_key_means_key_missing(self) -> None:
        with _Env():
            with self.assertRaises(hc.HfCredentialError) as cm:
                hc.encrypt(UID, TOK)
            self.assertEqual(cm.exception.code, "key_missing")
            with self.assertRaises(hc.HfCredentialError) as cm:
                hc.decrypt(UID, VECTOR_ENVELOPE)
            self.assertEqual(cm.exception.code, "key_missing")

    def test_the_error_never_carries_the_plaintext(self) -> None:
        with _Env(SEQ_A):
            try:
                hc.decrypt(OTHER_UID, VECTOR_ENVELOPE)
            except hc.HfCredentialError as exc:
                self.assertNotIn(TOK, str(exc))
                self.assertNotIn(TOK, repr(exc))
                self.assertIsNone(exc.__cause__)

    def test_rotation_current_becomes_previous(self) -> None:
        with _Env(SEQ_A):
            old = hc.encrypt(UID, TOK)
        # The key was rotated: B is current, A moved to PREVIOUS.
        with _Env(SEQ_B, SEQ_A):
            self.assertTrue(hc.usable(old))
            self.assertEqual(hc.decrypt(UID, old), TOK)
            new = hc.encrypt(UID, TOK)
            self.assertTrue(new.startswith("v1." + KID_B + "."))
            self.assertTrue(hc.usable(new))
            self.assertEqual(hc.decrypt(UID, new), TOK)
        # PREVIOUS dropped: the old envelope is no longer usable, the new one is.
        with _Env(SEQ_B):
            self.assertFalse(hc.usable(old))
            with self.assertRaises(hc.HfCredentialError) as cm:
                hc.decrypt(UID, old)
            self.assertEqual(cm.exception.code, "undecryptable")
            self.assertTrue(hc.usable(new))
            self.assertEqual(hc.decrypt(UID, new), TOK)

    def test_usable_is_false_for_garbage_and_without_a_key(self) -> None:
        with _Env(SEQ_A):
            for bad in ("", "x", "v1.zz.zz", None, 5):
                self.assertFalse(hc.usable(bad), repr(bad))
        with _Env():
            self.assertFalse(hc.usable(VECTOR_ENVELOPE))


def _fake_httpx(*, status=200, body=None, error=None, block=None, json_error=False):
    """A stand-in `httpx` whose Client.get answers/raises/blocks like the Hub."""
    mod = types.ModuleType("httpx")
    seen: dict = {}

    class Timeout:
        def __init__(self, value, **_kw):
            self.value = value

    class _Response:
        status_code = status

        def json(self):
            if json_error:
                raise ValueError("Expecting value: line 1 column 1")
            return body

    class Client:
        def __init__(self, timeout=None, follow_redirects=True, **_kw):
            seen["timeout"] = timeout
            seen["follow_redirects"] = follow_redirects

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            seen["closed"] = True
            return False

        def get(self, url, headers=None, **_kw):
            seen["url"] = url
            seen["headers"] = dict(headers or {})
            if block is not None:
                block.wait(10)
            if error is not None:
                raise error
            return _Response()

    mod.Timeout = Timeout
    mod.Client = Client
    mod.seen = seen
    return mod


def _whoami(name="alice", role="write", **extra):
    access = {"role": role, "displayName": "edubotics"}
    access.update(extra)
    return {"id": "6512a0b1c2d3e4f5a6b7c8d9", "type": "user", "name": name,
            "auth": {"type": "access_token", "accessToken": access}}


def _scoped(*entries, **block):
    return {"scoped": list(entries), **block}


def _user_entry(perms, name="alice", _id="6512a0b1c2d3e4f5a6b7c8d9"):
    entity = {"type": "user"}
    if name is not None:
        entity["name"] = name
    if _id is not None:
        entity["_id"] = _id
    return {"entity": entity, "permissions": list(perms)}


class TestValidateWithHub(unittest.TestCase):
    def _ask(self, fake, environ=None):
        with patch.dict(sys.modules, {"httpx": fake}), patch.dict(os.environ, environ or {}):
            if environ is None:
                os.environ.pop("HF_ENDPOINT", None)
            return hc.validate_with_hub(TOK)

    def test_asks_whoami_v2_with_the_token_as_a_bearer_and_a_bounded_client(self) -> None:
        fake = _fake_httpx(body=_whoami())
        self.assertEqual(self._ask(fake), {"name": "alice", "role": "write", "repo_write": None})
        self.assertEqual(fake.seen["url"], "https://huggingface.co/api/whoami-v2")
        self.assertEqual(fake.seen["headers"], {"Authorization": "Bearer " + TOK})
        # The REQUEST is bounded (audit f): huggingface_hub's whoami set no timeout.
        self.assertEqual(fake.seen["timeout"].value, hc.HUB_TIMEOUT_S)
        self.assertIs(fake.seen["follow_redirects"], False)
        self.assertTrue(fake.seen["closed"])

    def test_hf_endpoint_is_honoured_like_huggingface_hub_does(self) -> None:
        fake = _fake_httpx(body=_whoami())
        self._ask(fake, environ={"HF_ENDPOINT": "https://hub.example.test/"})
        self.assertEqual(fake.seen["url"], "https://hub.example.test/api/whoami-v2")

    def test_a_missing_role_is_unknown_not_a_rejection(self) -> None:
        for info in (
            {"name": "alice"},
            {"name": "alice", "auth": {}},
            {"name": "alice", "auth": {"accessToken": {}}},
            {"name": "alice", "auth": {"accessToken": {"role": ""}}},
            {"name": "alice", "auth": {"accessToken": {"role": 7}}},
            {"name": "alice", "auth": "x"},
            {"name": "alice", "auth": {"accessToken": "x"}},
        ):
            self.assertEqual(self._ask(_fake_httpx(body=info)),
                             {"name": "alice", "role": "unknown", "repo_write": None}, repr(info))

    def test_the_hub_refusing_the_token_is_rejected(self) -> None:
        for status in (401, 403):
            with self.assertRaises(hc.HfCredentialError) as cm:
                self._ask(_fake_httpx(status=status, body={"error": "Invalid credentials"}))
            self.assertEqual(cm.exception.code, "rejected", status)

    def test_throttling_server_errors_odd_statuses_and_network_trouble_are_unreachable(self) -> None:
        for fake in (
            _fake_httpx(status=429), _fake_httpx(status=500), _fake_httpx(status=503),
            _fake_httpx(status=404), _fake_httpx(status=302),
            _fake_httpx(error=ConnectionError("down")), _fake_httpx(error=OSError("x")),
            _fake_httpx(error=TimeoutError("read timed out")),
        ):
            with self.assertRaises(hc.HfCredentialError) as cm:
                self._ask(fake)
            self.assertEqual(cm.exception.code, "unreachable")

    def test_a_hub_that_never_answers_is_unreachable_after_the_timeout(self) -> None:
        release = threading.Event()
        try:
            with patch.object(hc, "HUB_TIMEOUT_S", 0.05):
                with self.assertRaises(hc.HfCredentialError) as cm:
                    self._ask(_fake_httpx(body=_whoami(), block=release))
            self.assertEqual(cm.exception.code, "unreachable")
        finally:
            release.set()

    def test_an_answer_without_a_usable_name_is_a_bad_reply(self) -> None:
        for body in (None, {}, {"name": ""}, {"name": 5}, ["alice"], {"fullname": "x"}):
            with self.assertRaises(hc.HfCredentialError) as cm:
                self._ask(_fake_httpx(body=body))
            self.assertEqual(cm.exception.code, "bad_reply", repr(body))
        with self.assertRaises(hc.HfCredentialError) as cm:
            self._ask(_fake_httpx(json_error=True))
        self.assertEqual(cm.exception.code, "bad_reply")

    def test_logs_carry_the_class_and_status_but_never_the_token(self) -> None:
        for fake, marker in (
            (_fake_httpx(error=ConnectionError("Invalid token " + TOK)), "ConnectionError"),
            (_fake_httpx(status=401, body={"error": "Invalid token " + TOK}), "401"),
        ):
            with self.assertLogs("app.services.hf_credentials", level="DEBUG") as logs:
                with self.assertRaises(hc.HfCredentialError):
                    self._ask(fake)
            text = "\n".join(logs.output)
            self.assertIn(marker, text)
            self.assertNotIn(TOK, text)
            self.assertNotIn("Invalid token", text)

    def test_a_fine_grained_token_carries_its_repo_write_verdict(self) -> None:
        cases = (
            (_scoped(_user_entry(["repo.content.read", "repo.write"])), True),
            (_scoped(_user_entry(["repo.content.read"])), False),
            ({"note": "an unrecognised shape"}, None),
        )
        for block, verdict in cases:
            info = _whoami(role="fineGrained", fineGrained=block)
            self.assertEqual(self._ask(_fake_httpx(body=info))["repo_write"], verdict, repr(block))
        # Only a fine-grained token is judged: a write token never carries a verdict.
        info = _whoami(role="write", fineGrained=_scoped(_user_entry([])))
        self.assertIsNone(self._ask(_fake_httpx(body=info))["repo_write"])


class TestFineGrainedRepoWrite(unittest.TestCase):
    """The Hub's OpenAPI schema for GET /api/whoami-v2 auth.accessToken.fineGrained."""

    def verdict(self, block, **who):
        return hc.fine_grained_repo_write(_whoami(role="fineGrained", fineGrained=block, **who)
                                          if block is not ... else _whoami(role="fineGrained"))

    def test_write_to_the_own_account_is_true(self) -> None:
        for perms in (["repo.write"], ["repo.content.read", "repo.content.write"],
                      ["repo.content.read", "repo.write", "discussion.write"]):
            self.assertIs(self.verdict(_scoped(_user_entry(perms))), True, perms)

    def test_any_repository_write_permission_counts_not_only_todays_two_names(self) -> None:
        # review d: a future Hugging Face rename of a repository write permission
        # must not refuse a token that can upload (a permission that starts
        # with "repo" and ends in ".write" is a repository write).
        for perms in (["repo.contents.write"], ["repos.write"], ["repo.settings.write"],
                      ["REPO.WRITE"], ["repo.content.read", "repo.new-name.write"]):
            self.assertIs(self.verdict(_scoped(_user_entry(perms))), True, perms)
        for name in hc.REPO_WRITE_PERMISSIONS:
            self.assertTrue(hc.is_repo_write_permission(name), name)

    def test_a_write_outside_the_repositories_never_counts(self) -> None:
        for perms in (["discussion.write"], ["post.write"], ["inference.write"], ["collection.write"],
                      ["discussion.write", "post.write", "repo.content.read"], ["write"], ["repo"],
                      ["repo.read"], ["repo.write.read"], [".write"], [None, 7, {"repo": "write"}]):
            self.assertIs(self.verdict(_scoped(_user_entry(perms))), False, perms)
        for odd in (None, 7, "", "repowrite", "discussion.write"):
            self.assertFalse(hc.is_repo_write_permission(odd), repr(odd))

    def test_the_owner_is_matched_by_name_case_insensitively_or_by_id(self) -> None:
        self.assertIs(self.verdict(_scoped(_user_entry(["repo.write"], name="ALICE"))), True)
        self.assertIs(self.verdict(_scoped(_user_entry(["repo.write"], name=None))), True)

    def test_parseable_and_no_own_write_is_false(self) -> None:
        org = {"entity": {"type": "org", "name": "school", "_id": "a" * 24}, "permissions": ["repo.write"]}
        repo = {"entity": {"type": "dataset", "name": "alice/wuerfel", "_id": "b" * 24},
                "permissions": ["repo.write"]}
        other_user = _user_entry(["repo.write"], name="mallory", _id="c" * 24)
        for block in (
            _scoped(),                                             # nothing scoped at all
            _scoped(_user_entry(["repo.content.read"])),           # read only
            _scoped(_user_entry([])),                              # an empty permission list
            _scoped(org),                                          # writes to an organisation only
            _scoped(repo),                                         # one repository, not new datasets
            _scoped(other_user),
            {"scoped": [], "global": ["discussion.write", "post.write"]},  # global never writes a repo
            {"scoped": [_user_entry(["repo.content.read"])], "canReadGatedRepos": True},
        ):
            self.assertIs(self.verdict(block), False, repr(block))

    def test_anything_unrecognised_fails_open(self) -> None:
        for block in (
            "fine",                                               # not a dict
            {},                                                   # no scoped
            {"scoped": "all"},                                    # scoped not a list
            {"scoped": ["repo.write"]},                           # an entry that is not a dict
            {"scoped": [{"entity": "user", "permissions": []}]},  # entity not a dict
            {"scoped": [{"entity": {"type": "user"}, "permissions": "repo.write"}]},
            {"scoped": [{"entity": {"type": "user"}, "permissions": []}]},  # cannot attribute
        ):
            self.assertIsNone(self.verdict(block), repr(block))
        self.assertIsNone(self.verdict(...))                     # the block is missing
        self.assertIsNone(hc.fine_grained_repo_write(None))
        self.assertIsNone(hc.fine_grained_repo_write({"name": "alice"}))
        no_name = _whoami(role="fineGrained", fineGrained=_scoped(_user_entry(["repo.write"])))
        no_name["name"] = ""
        self.assertIsNone(hc.fine_grained_repo_write(no_name))

    def test_an_unattributable_user_entity_without_an_owner_id_fails_open(self) -> None:
        info = _whoami(role="fineGrained", fineGrained=_scoped(_user_entry(["repo.content.read"], name=None)))
        del info["id"]
        self.assertIsNone(hc.fine_grained_repo_write(info))


class TestHasCredential(unittest.TestCase):
    def _supabase(self, data):
        calls = []

        class _Q:
            def select(self, cols):
                calls.append(("select", cols))
                return self

            def eq(self, col, val):
                calls.append(("eq", col, val))
                return self

            def execute(self):
                calls.append(("execute",))
                return SimpleNamespace(data=data)

        class _Sb:
            def table(self, name):
                calls.append(("table", name))
                return _Q()

        return _Sb(), calls

    def test_true_when_a_row_exists_and_only_select_eq_execute_are_used(self) -> None:
        sb, calls = self._supabase([{"user_id": UID}])
        self.assertTrue(hc.has_credential(sb, UID))
        self.assertEqual(
            calls,
            [("table", "user_hf_credentials"), ("select", "user_id"), ("eq", "user_id", UID), ("execute",)],
        )

    def test_false_for_no_rows_none_or_a_missing_data_attribute(self) -> None:
        for data in ([], None):
            sb, _ = self._supabase(data)
            self.assertFalse(hc.has_credential(sb, UID))


if __name__ == "__main__":
    unittest.main()
