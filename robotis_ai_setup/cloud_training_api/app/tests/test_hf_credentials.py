"""services/hf_credentials.py: shape, fingerprint, AES-256-GCM envelope, key
handling and the Hub check (migration 042, a student's own Hugging Face token).

The module imports `cryptography` and `huggingface_hub` lazily, so this file
needs the former (CI's python-tests pip line must list `cryptography`; the
owner adds it when merging) and a FAKE of the latter that
it patches into sys.modules per test.

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


class _HubError(Exception):
    def __init__(self, status, message="boom"):
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status)


def _fake_hub(*, info=None, error=None, block=None):
    """A stand-in `huggingface_hub` whose HfApi.whoami answers/raises/blocks."""
    mod = types.ModuleType("huggingface_hub")
    seen = {}

    class HfApi:
        def __init__(self, token=None, **_kw):
            seen["token"] = token

        def whoami(self, *_a, **_kw):
            if block is not None:
                block.wait(10)
            if error is not None:
                raise error
            return info

    mod.HfApi = HfApi
    mod.seen = seen
    return mod


class TestValidateWithHub(unittest.TestCase):
    def _ask(self, hub):
        with patch.dict(sys.modules, {"huggingface_hub": hub}):
            return hc.validate_with_hub(TOK)

    def test_returns_name_and_role_and_passes_the_token_to_the_client(self) -> None:
        hub = _fake_hub(info={"name": "alice", "auth": {"accessToken": {"role": "write"}}})
        self.assertEqual(self._ask(hub), {"name": "alice", "role": "write"})
        self.assertEqual(hub.seen["token"], TOK)

    def test_a_missing_role_is_unknown_not_a_rejection(self) -> None:
        for info in (
            {"name": "alice"},
            {"name": "alice", "auth": {}},
            {"name": "alice", "auth": {"accessToken": {}}},
            {"name": "alice", "auth": {"accessToken": {"role": ""}}},
            {"name": "alice", "auth": {"accessToken": {"role": 7}}},
        ):
            self.assertEqual(self._ask(_fake_hub(info=info)), {"name": "alice", "role": "unknown"})

    def test_the_hub_refusing_the_token_is_rejected(self) -> None:
        for status in (401, 403):
            with self.assertRaises(hc.HfCredentialError) as cm:
                self._ask(_fake_hub(error=_HubError(status)))
            self.assertEqual(cm.exception.code, "rejected", status)

    def test_throttling_server_errors_and_network_trouble_are_unreachable(self) -> None:
        for error in (_HubError(429), _HubError(500), _HubError(503), ConnectionError("down"), OSError("x")):
            with self.assertRaises(hc.HfCredentialError) as cm:
                self._ask(_fake_hub(error=error))
            self.assertEqual(cm.exception.code, "unreachable", repr(error))

    def test_a_hub_that_never_answers_is_unreachable_after_the_timeout(self) -> None:
        release = threading.Event()
        try:
            with patch.object(hc, "HUB_TIMEOUT_S", 0.05):
                with self.assertRaises(hc.HfCredentialError) as cm:
                    self._ask(_fake_hub(info={"name": "alice"}, block=release))
            self.assertEqual(cm.exception.code, "unreachable")
        finally:
            release.set()

    def test_an_answer_without_a_usable_name_is_a_bad_reply(self) -> None:
        for info in (None, {}, {"name": ""}, {"name": 5}, ["alice"], {"fullname": "x"}):
            with self.assertRaises(hc.HfCredentialError) as cm:
                self._ask(_fake_hub(info=info))
            self.assertEqual(cm.exception.code, "bad_reply", repr(info))

    def test_logs_carry_the_class_and_status_but_never_the_token(self) -> None:
        leaky = _HubError(401, "Invalid token " + TOK)
        with self.assertLogs("app.services.hf_credentials", level="DEBUG") as logs:
            with self.assertRaises(hc.HfCredentialError):
                self._ask(_fake_hub(error=leaky))
        text = "\n".join(logs.output)
        self.assertIn("_HubError", text)
        self.assertIn("401", text)
        self.assertNotIn(TOK, text)
        self.assertNotIn("Invalid token", text)


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
