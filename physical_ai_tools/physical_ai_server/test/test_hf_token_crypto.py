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
