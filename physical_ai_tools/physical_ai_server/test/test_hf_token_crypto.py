"""The student's Hugging Face token, encrypted on its way to the robot (2026-11-13).

``data_processing/hf_token_crypto.py`` is the robot's half; the SPA's
``features/hfToken/hfTokenCrypto.js`` + ``hfTokenSeal.js`` (the audited @noble
libraries since 2026-11-14) are the other. They meet in the fixture the JS test
writes (``hf-token-crypto-cases.json``): every case there was encrypted by the
SPA's product path and is decrypted here by the robot's module, and every known
answer (a fixed ephemeral secret and iv) is recomputed here byte for byte with
``cryptography``, so the two cannot drift apart unseen. The fixture's robot key
is a TEST key.
"""

from __future__ import annotations

import base64
import json
import logging
import unittest
from pathlib import Path

import pytest

pytest.importorskip('cryptography')

from physical_ai_server.data_processing import hf_token_crypto as crypto  # noqa: E402
from physical_ai_server.data_processing import hf_token_store as store  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE = (_REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'features' / 'hfToken'
            / '__tests__' / 'fixtures' / 'hf-token-crypto-cases.json')

TOK = 'hf_' + 'b' * 20 + '0123'


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


class TestInteropWithTheSpa(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_FIXTURE.is_file(), f'{_FIXTURE} is missing: run the JS test with '
                                            'EDUBOTICS_REGEN_HF_CRYPTO_CASES=1')
        self.fixture = json.loads(_FIXTURE.read_text(encoding='utf-8'))
        robot = self.fixture['robot']
        self.key = crypto.RobotKey.from_private_value(int.from_bytes(_b64url(robot['d']), 'big'))

    def test_the_fixture_key_is_the_robot_key_the_spa_encrypted_for(self):
        robot = self.fixture['robot']
        self.assertEqual(self.key.public_b64, robot['pub'])
        self.assertEqual(self.key.kid, robot['kid'])
        self.assertEqual(self.key.kid, crypto.kid_of(_b64url(robot['pub'])))

    def test_every_case_the_spa_encrypted_decrypts_on_the_robot(self):
        # ONE SPA path on every page (2026-11-14): @noble/curves, @noble/hashes
        # and @noble/ciphers, the same code on a Windows page and on a Pi's
        # plain-HTTP page (which has no crypto.subtle).
        self.assertEqual({case['impl'] for case in self.fixture['cases']}, {'noble'})
        self.assertGreaterEqual(len(self.fixture['cases']), 3)
        for case in self.fixture['cases']:
            with self.subTest(impl=case['impl'], plain=case['plain'][:12]):
                self.assertEqual(self.key.decrypt(case['wire']), case['plain'])
                self.assertTrue(store.valid_shape(case['plain']))

    def test_every_known_answer_is_what_cryptography_computes_byte_for_byte(self):
        # The SPA's noble code, given a fixed ephemeral secret and iv, wrote
        # these wire values; `cryptography` (OpenSSL) recomputes each one from
        # the same inputs, so every step (ECDH x coordinate, HKDF salt and info,
        # GCM iv, AAD, tag layout, encoding) is pinned on both sides.
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        robot = self.fixture['robot']
        robot_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _b64url(robot['pub']))
        kid = robot['kid'].encode('ascii')
        kats = self.fixture['kats']
        self.assertGreaterEqual(len(kats), 3)
        for kat in kats:
            with self.subTest(plain=kat['plain'][:12]):
                mine = ec.derive_private_key(int(kat['ephemeral_d'], 16), ec.SECP256R1())
                shared = mine.exchange(ec.ECDH(), robot_public)
                key = HKDF(algorithm=hashes.SHA256(), length=32, salt=crypto.CONTEXT,
                           info=b'aes-256-gcm|' + kid).derive(shared)
                iv = bytes.fromhex(kat['iv'])
                ciphertext = AESGCM(key).encrypt(iv, kat['plain'].encode('utf-8'), crypto.CONTEXT + b'|' + kid)
                point = mine.public_key().public_bytes(serialization.Encoding.X962,
                                                       serialization.PublicFormat.UncompressedPoint)
                expected = (f"{crypto.WIRE_PREFIX}{robot['kid']}.{crypto.b64url(point)}."
                            f"{crypto.b64url(iv)}.{crypto.b64url(ciphertext)}")
                self.assertEqual(kat['wire'], expected)
                self.assertEqual(self.key.decrypt(kat['wire']), kat['plain'])

    def test_a_case_for_another_robot_is_stale(self):
        other = crypto.RobotKey.generate()
        with self.assertRaises(crypto.WireError) as caught:
            other.decrypt(self.fixture['cases'][0]['wire'])
        self.assertEqual(caught.exception.code, 'stale')


class TestTheWire(unittest.TestCase):
    def setUp(self):
        self.key = crypto.RobotKey.generate()

    def wire(self, token=TOK):
        return crypto.encrypt_for_tests(self.key.public_b64, self.key.kid, token)

    def test_round_trip_and_a_fresh_ephemeral_key_every_time(self):
        a, b = self.wire(), self.wire()
        self.assertNotEqual(a, b)
        self.assertEqual(self.key.decrypt(a), TOK)
        self.assertNotIn('hf_', a)
        self.assertTrue(crypto.is_wire(a))

    def test_the_advertisement(self):
        ad = self.key.advertisement()
        self.assertEqual(ad, {'enc': 'enc1', 'kid': self.key.kid, 'pub': self.key.public_b64})
        self.assertEqual(len(_b64url(ad['pub'])), 65)
        self.assertRegex(ad['kid'], r'^[0-9a-f]{16}$')
        self.assertNotEqual(ad['kid'], crypto.RobotKey.generate().kid)

    def test_malformed_values_are_refused_before_any_crypto(self):
        good = self.wire()
        kid = self.key.kid
        for bad in ('', 'enc1:', 'hf_x', 'enc1:' + 'x' * 2000, f'enc1:{kid}.a.b',
                    f'enc1:{kid}.!!.a.b', f'enc1:{kid.upper()}' + good[len('enc1:') + 16:],
                    good.replace('.', '.A', 1)):
            with self.subTest(bad=bad[:24]):
                with self.assertRaises(crypto.WireError) as caught:
                    self.key.decrypt(bad)
                self.assertIn(caught.exception.code, ('malformed', 'bad'))

    def test_a_tampered_value_fails_the_aead_check(self):
        good = self.wire()
        head, tail = good.rsplit('.', 1)
        raw = bytearray(_b64url(tail))
        raw[0] ^= 1
        tampered = head + '.' + crypto.b64url(bytes(raw))
        with self.assertRaises(crypto.WireError) as caught:
            self.key.decrypt(tampered)
        self.assertEqual(caught.exception.code, 'bad')

    def test_the_aad_binds_the_key_id(self):
        # The same ciphertext presented under another robot's id is stale, and
        # one whose id was rewritten to the robot's own fails the AEAD check.
        other = crypto.RobotKey.generate()
        foreign = crypto.encrypt_for_tests(other.public_b64, other.kid, TOK)
        relabelled = 'enc1:' + self.key.kid + foreign[len('enc1:') + 16:]
        with self.assertRaises(crypto.WireError) as caught:
            self.key.decrypt(relabelled)
        self.assertEqual(caught.exception.code, 'bad')

    def test_a_point_off_the_curve_is_refused(self):
        good = self.wire()
        parts = good[len('enc1:'):].split('.')
        point = bytearray(_b64url(parts[1]))
        point[-1] ^= 1  # no longer on P-256
        parts[1] = crypto.b64url(bytes(point))
        with self.assertRaises(crypto.WireError):
            self.key.decrypt('enc1:' + '.'.join(parts))

    def test_the_error_never_carries_the_value(self):
        good = self.wire()
        try:
            crypto.RobotKey.generate().decrypt(good)
        except crypto.WireError as e:
            self.assertNotIn(good, str(e))
            self.assertNotIn(good[:30], repr(e))


class TestScrubber(unittest.TestCase):
    def test_an_encrypted_token_never_reaches_a_log_line(self):
        key = crypto.RobotKey.generate()
        wire = crypto.encrypt_for_tests(key.public_b64, key.kid, TOK)
        record = logging.LogRecord('huggingface_hub', logging.ERROR, __file__, 1,
                                   'refused %s and %s', (wire, TOK), None)
        store.TokenScrubber().filter(record)
        text = record.getMessage()
        self.assertNotIn(wire, text)
        self.assertNotIn(wire.split('.', 1)[1], text)
        self.assertNotIn(TOK, text)
        self.assertIn(store.REDACTED_WIRE, text)
        self.assertIn(store.REDACTED, text)

    def test_the_state_payload_advertises_only_with_a_slot(self):
        key = crypto.RobotKey.generate()
        with_slot = store.state_payload(1, False, path='/nonexistent/token',
                                        environ={store.PATH_ENV: '/nonexistent/token'}, key=key)
        self.assertEqual(list(with_slot), ['v', 'seq', 'accepts', 'present', 'fp', 'busy',
                                           'enc', 'kid', 'pub'])
        without = store.state_payload(1, False, environ={}, key=key)
        self.assertNotIn('pub', without)
        plain = store.state_payload(1, False, path='/nonexistent/token',
                                    environ={store.PATH_ENV: '/nonexistent/token'})
        self.assertEqual(list(plain), ['v', 'seq', 'accepts', 'present', 'fp', 'busy'])


class TestKeyFile(unittest.TestCase):
    """VB-03: the public half for the pi-agent (``write_key_file``)."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.path = Path(self.dir) / crypto.KEY_FILE_NAME

    def test_it_holds_exactly_the_advertisement_and_its_schema(self):
        # The schema the pi-agent accepts (pi_agent/robot_key.py::KEY_SCHEMA).
        assert crypto.KEY_FILE_SCHEMA == 1
        key = crypto.RobotKey.generate()
        self.assertTrue(crypto.write_key_file(key, self.dir))
        self.assertEqual(json.loads(self.path.read_text(encoding='ascii')),
                         {'v': crypto.KEY_FILE_SCHEMA, **key.advertisement()})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        # The kid is the public point's hash, as the agent and the SPA check.
        self.assertEqual(key.kid, crypto.kid_of(_b64url(key.public_b64)))

    def test_a_missing_directory_writes_nothing(self):
        missing = str(Path(self.dir) / 'absent')
        self.assertFalse(crypto.write_key_file(crypto.RobotKey.generate(), missing))
        self.assertFalse(Path(missing).exists())

    def test_no_key_removes_a_file_a_previous_node_left(self):
        self.assertTrue(crypto.write_key_file(crypto.RobotKey.generate(), self.dir))
        self.assertFalse(crypto.write_key_file(None, self.dir))
        self.assertFalse(self.path.exists())
        self.assertFalse(crypto.write_key_file(None, self.dir))  # and again: no error

    def test_the_default_directory_is_read_at_call_time(self):
        from unittest.mock import patch
        key = crypto.RobotKey.generate()
        with patch.object(crypto, 'KEY_FILE_DIR', self.dir):
            self.assertTrue(crypto.write_key_file(key))
        self.assertTrue(self.path.is_file())

    def test_no_private_material_in_the_file(self):
        key = crypto.RobotKey.generate()
        crypto.write_key_file(key, self.dir)
        text = self.path.read_text(encoding='ascii')
        private = key._private.private_numbers().private_value.to_bytes(32, 'big')
        self.assertNotIn(crypto.b64url(private), text)
        self.assertNotIn(private.hex(), text)
        self.assertEqual(set(json.loads(text)), {'v', 'enc', 'kid', 'pub'})


class TestReplay(unittest.TestCase):
    """VC-05 (verification round C, 2026-11-15): a captured ``enc1:`` value
    could be replayed to the node for the rest of its run, putting a
    classmate's token back into the slot after they signed out. The key now
    refuses a value it already decrypted, judged on the DECODED parts (a
    re-encoded value is the same value), and an exhausted record refuses
    everything until the node rotates the key."""

    def setUp(self):
        self.key = crypto.RobotKey.generate()

    def wire(self, token=TOK, context=crypto.CONTEXT):
        return crypto.encrypt_for_tests(self.key.public_b64, self.key.kid, token, context=context)

    def test_the_same_value_is_refused_the_second_time(self):
        wire = self.wire()
        self.assertEqual(self.key.decrypt(wire), TOK)
        with self.assertRaises(crypto.WireError) as caught:
            self.key.decrypt(wire)
        self.assertEqual(caught.exception.code, 'replayed')
        self.assertNotIn(wire, str(caught.exception))

    def test_a_negated_client_point_is_the_same_value(self):
        # Verification round D, VD-01: ECDH uses only the x-coordinate of the
        # shared point, so the client point (x, y) and its negation (x, p - y)
        # give the same shared secret, the same key and the same plaintext.
        # A digest over the point AS SENT called the negated twin new, and a
        # captured value opened once more. Every context, both orders.
        p = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff

        def negate(wire):
            kid, point, iv, ciphertext = crypto.parse_wire(wire)
            y = int.from_bytes(point[33:], 'big')
            neg = b'\x04' + point[1:33] + (p - y).to_bytes(32, 'big')
            return f'{crypto.WIRE_PREFIX}{kid}.{crypto.b64url(neg)}.{crypto.b64url(iv)}.{crypto.b64url(ciphertext)}'

        for context in (crypto.CONTEXT, crypto.CLOUD_SESSION_CONTEXT, crypto.CLOUD_EDGE_CONTEXT):
            for first_negated in (False, True):
                with self.subTest(context=context, first_negated=first_negated):
                    wire = self.wire(context=context)
                    twin = negate(wire)
                    self.assertNotEqual(twin, wire)
                    first, second = (twin, wire) if first_negated else (wire, twin)
                    self.assertEqual(self.key.decrypt(first, context=context), TOK)
                    with self.assertRaises(crypto.WireError) as caught:
                        self.key.decrypt(second, context=context)
                    self.assertEqual(caught.exception.code, 'replayed')

    def test_a_genuine_re_push_of_the_same_token_is_never_a_duplicate(self):
        for _ in range(5):
            self.assertEqual(self.key.decrypt(self.wire()), TOK)

    def test_a_value_that_failed_is_not_recorded(self):
        wire = self.wire()
        head, tail = wire.rsplit('.', 1)
        raw = bytearray(_b64url(tail))
        raw[0] ^= 1
        with self.assertRaises(crypto.WireError):
            self.key.decrypt(head + '.' + crypto.b64url(bytes(raw)))
        self.assertEqual(self.key.decrypt(wire), TOK)  # the genuine value still works once

    def test_a_non_canonical_re_encoding_is_refused_not_a_new_value(self):
        # Python's base64 decoder ignores the unused low bits of the last
        # character: a value whose last character differs only there decodes
        # to the same bytes. A digest of the STRING would call it new.
        wire = self.wire()
        self.assertEqual(self.key.decrypt(wire), TOK)
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
        head, tail = wire.rsplit('.', 1)
        unused = {2: 4, 3: 2}.get(len(tail) % 4, 0)  # low bits the decoder drops
        self.assertGreater(unused, 0, 'the fixture tail must leave unused bits')
        index = alphabet.index(tail[-1])
        twin = head + '.' + tail[:-1] + alphabet[index ^ 1]
        self.assertNotEqual(twin, wire)
        self.assertEqual(_b64url(twin.rsplit('.', 1)[1]), _b64url(tail))  # the same bytes
        with self.assertRaises(crypto.WireError) as caught:
            self.key.decrypt(twin)
        self.assertIn(caught.exception.code, ('malformed', 'replayed'))

    def test_two_threads_racing_the_same_value_accept_it_once(self):
        import threading
        wire = self.wire()
        results = []

        def go():
            try:
                results.append(self.key.decrypt(wire))
            except crypto.WireError as e:
                results.append(e.code)
        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), sorted([TOK] + ['replayed'] * 7))

    def test_a_full_record_refuses_everything_until_a_new_key(self):
        from unittest.mock import patch
        with patch.object(crypto, 'MAX_SEEN_WIRES', 3):
            key = crypto.RobotKey.generate()
            for _ in range(3):
                self.assertFalse(key.exhausted)
                key.decrypt(crypto.encrypt_for_tests(key.public_b64, key.kid, TOK))
            self.assertTrue(key.exhausted)
            with self.assertRaises(crypto.WireError) as caught:
                key.decrypt(crypto.encrypt_for_tests(key.public_b64, key.kid, TOK))
            self.assertEqual(caught.exception.code, 'stale')  # the SPA re-reads the key
            fresh = crypto.RobotKey.generate()  # the node's rotation: an empty record
            self.assertFalse(fresh.exhausted)
            self.assertEqual(fresh.decrypt(crypto.encrypt_for_tests(fresh.public_b64, fresh.kid, TOK)), TOK)

    def test_the_record_is_bounded_and_holds_no_value(self):
        self.key.decrypt(self.wire())
        (digest,) = self.key._seen
        self.assertIsInstance(digest, bytes)
        self.assertEqual(len(digest), 32)
        assert crypto.MAX_SEEN_WIRES == 1024  # 32 KB of digests per key at most

    def test_the_wire_bounds_are_pinned(self):
        # A token is at most 3 + 256 characters, a connect token 43: 1024 is
        # far above both wire values. The edge token is up to 4096 characters
        # (cloud_protocol.EDGE_TOKEN_RE): its wire value is up to 5 610.
        assert crypto.MAX_WIRE_LEN == 1024
        assert crypto.CLOUD_EDGE_MAX_WIRE_LEN == 6144


class TestContexts(unittest.TestCase):
    """VC-06 (verification round C): Cloud-Inferenz's two session tokens ride
    the same envelope, each with its OWN context (HKDF salt and AAD), so no
    envelope can be replayed as another kind."""

    def setUp(self):
        self.key = crypto.RobotKey.generate()
        self.contexts = (crypto.CONTEXT, crypto.CLOUD_SESSION_CONTEXT, crypto.CLOUD_EDGE_CONTEXT)

    def test_three_distinct_contexts(self):
        self.assertEqual(len(set(self.contexts)), 3)
        self.assertEqual(crypto.CONTEXT, b'edubotics-hf-token:v1')
        self.assertEqual(crypto.CLOUD_SESSION_CONTEXT, b'edubotics-cloud-session-token:v1')
        self.assertEqual(crypto.CLOUD_EDGE_CONTEXT, b'edubotics-cloud-edge-token:v1')

    def test_a_value_opens_only_under_its_own_context(self):
        plain = 'C' * 43
        for made in self.contexts:
            for read in self.contexts:
                with self.subTest(made=made, read=read):
                    wire = crypto.encrypt_for_tests(self.key.public_b64, self.key.kid, plain, context=made)
                    if made == read:
                        self.assertEqual(self.key.decrypt(wire, context=read), plain)
                    else:
                        with self.assertRaises(crypto.WireError) as caught:
                            self.key.decrypt(wire, context=read)
                        self.assertEqual(caught.exception.code, 'bad')

    def test_the_default_context_is_the_hf_token(self):
        wire = crypto.encrypt_for_tests(self.key.public_b64, self.key.kid, TOK)
        self.assertEqual(self.key.decrypt(wire, context=crypto.CONTEXT), TOK)

    def test_an_unknown_context_is_a_programming_error(self):
        wire = crypto.encrypt_for_tests(self.key.public_b64, self.key.kid, TOK)
        with self.assertRaises(ValueError):
            self.key.decrypt(wire, context=b'edubotics-something-else:v1')

    def test_the_longest_edge_token_fits_its_bound_and_not_the_token_bound(self):
        edge = '!' + '~' * 4095  # cloud_protocol.EDGE_TOKEN_RE: printable ASCII, 16..4096
        wire = crypto.encrypt_for_tests(self.key.public_b64, self.key.kid, edge,
                                        context=crypto.CLOUD_EDGE_CONTEXT)
        self.assertGreater(len(wire), crypto.MAX_WIRE_LEN)
        self.assertLessEqual(len(wire), crypto.CLOUD_EDGE_MAX_WIRE_LEN)
        self.assertEqual(self.key.decrypt(wire, context=crypto.CLOUD_EDGE_CONTEXT), edge)
        token_sized = crypto.encrypt_for_tests(self.key.public_b64, self.key.kid, edge,
                                               context=crypto.CLOUD_SESSION_CONTEXT)
        with self.assertRaises(crypto.WireError) as caught:
            self.key.decrypt(token_sized, context=crypto.CLOUD_SESSION_CONTEXT)
        self.assertEqual(caught.exception.code, 'malformed')

    def test_one_record_across_contexts(self):
        wire = crypto.encrypt_for_tests(self.key.public_b64, self.key.kid, 'C' * 43,
                                        context=crypto.CLOUD_SESSION_CONTEXT)
        self.key.decrypt(wire, context=crypto.CLOUD_SESSION_CONTEXT)
        with self.assertRaises(crypto.WireError) as caught:
            self.key.decrypt(wire, context=crypto.CLOUD_SESSION_CONTEXT)
        self.assertEqual(caught.exception.code, 'replayed')
