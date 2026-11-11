"""Cloud-Inferenz's session tokens in the robot envelope (verification round C, VC-06, 2026-11-15).

On a remote page the SPA encrypts the connect token and the Modal edge token of
``/edubotics/cloud_inference/start`` for the robot's key, in the HF token's
``enc1:`` envelope but each under its OWN context
(``hf_token_crypto.CLOUD_SESSION_CONTEXT`` / ``CLOUD_EDGE_CONTEXT``). The SPA's
``features/cloudInference/__tests__/cloudTokenCrypto.test.js`` writes
``cloud-token-crypto-cases.json``; here the robot's module opens every case
under its context and ``cryptography`` recomputes every known answer byte for
byte, so the two sides cannot drift apart unseen (the context strings
included). The robot key is the HF fixture's TEST key.
"""

from __future__ import annotations

import base64
import json
import re
import unittest
from pathlib import Path

import pytest

pytest.importorskip('cryptography')

from physical_ai_server.data_processing import hf_token_crypto as crypto  # noqa: E402
from physical_ai_server.inference import cloud_protocol  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'features'
_HF_FIXTURE = _SRC / 'hfToken' / '__tests__' / 'fixtures' / 'hf-token-crypto-cases.json'
_FIXTURE = _SRC / 'cloudInference' / '__tests__' / 'fixtures' / 'cloud-token-crypto-cases.json'
_CRYPTO_JS = _SRC / 'hfToken' / 'hfTokenCrypto.js'

_CONTEXTS = {'session': crypto.CLOUD_SESSION_CONTEXT, 'edge': crypto.CLOUD_EDGE_CONTEXT}
_SHAPES = {'session': cloud_protocol.TOKEN_RE, 'edge': cloud_protocol.EDGE_TOKEN_RE}


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


class TestInteropWithTheSpa(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_FIXTURE.is_file(), f'{_FIXTURE} is missing: run the JS test with '
                                            'EDUBOTICS_REGEN_CLOUD_CRYPTO_CASES=1')
        self.fixture = json.loads(_FIXTURE.read_text(encoding='utf-8'))
        robot = json.loads(_HF_FIXTURE.read_text(encoding='utf-8'))['robot']
        self.key = crypto.RobotKey.from_private_value(int.from_bytes(_b64url(robot['d']), 'big'))

    def test_the_spa_spells_the_three_contexts_the_robot_does(self):
        js = _CRYPTO_JS.read_text(encoding='utf-8')
        for name, value in (('CONTEXT', crypto.CONTEXT), ('CLOUD_SESSION_CONTEXT', crypto.CLOUD_SESSION_CONTEXT),
                            ('CLOUD_EDGE_CONTEXT', crypto.CLOUD_EDGE_CONTEXT)):
            m = re.search(rf"export const {name} = '([^']+)';", js)
            self.assertIsNotNone(m, name)
            self.assertEqual(m.group(1).encode('ascii'), value, name)
        self.assertEqual(self.fixture['contexts'], {
            'session': crypto.CLOUD_SESSION_CONTEXT.decode(), 'edge': crypto.CLOUD_EDGE_CONTEXT.decode(),
            'hf_token': crypto.CONTEXT.decode()})

    def test_the_fixture_key_is_the_robot_key_the_spa_encrypted_for(self):
        self.assertEqual(self.fixture['robot'], {'kid': self.key.kid, 'pub': self.key.public_b64})

    def test_every_case_opens_under_its_own_context_and_has_its_token_shape(self):
        self.assertEqual({c['context'] for c in self.fixture['cases']}, {'session', 'edge'})
        for case in self.fixture['cases']:
            with self.subTest(context=case['context'], length=len(case['plain'])):
                key = crypto.RobotKey.from_private_value(self.key._private.private_numbers().private_value)
                self.assertEqual(key.decrypt(case['wire'], context=_CONTEXTS[case['context']]), case['plain'])
                self.assertIsNotNone(_SHAPES[case['context']].fullmatch(case['plain']))

    def test_no_case_opens_under_another_context(self):
        for case in self.fixture['cases']:
            for other in (crypto.CONTEXT, crypto.CLOUD_SESSION_CONTEXT, crypto.CLOUD_EDGE_CONTEXT):
                if other == _CONTEXTS[case['context']]:
                    continue
                with self.subTest(context=case['context'], other=other):
                    key = crypto.RobotKey.from_private_value(self.key._private.private_numbers().private_value)
                    with self.assertRaises(crypto.WireError) as caught:
                        key.decrypt(case['wire'], context=other)
                    self.assertIn(caught.exception.code, ('bad', 'malformed'))

    def test_every_known_answer_is_what_cryptography_computes_byte_for_byte(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        robot = self.fixture['robot']
        robot_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _b64url(robot['pub']))
        kid = robot['kid'].encode('ascii')
        kats = self.fixture['kats']
        self.assertEqual({k['context'] for k in kats}, {'session', 'edge'})
        for kat in kats:
            context = _CONTEXTS[kat['context']]
            with self.subTest(context=kat['context']):
                mine = ec.derive_private_key(int(kat['ephemeral_d'], 16), ec.SECP256R1())
                shared = mine.exchange(ec.ECDH(), robot_public)
                key = HKDF(algorithm=hashes.SHA256(), length=32, salt=context,
                           info=b'aes-256-gcm|' + kid).derive(shared)
                iv = bytes.fromhex(kat['iv'])
                ciphertext = AESGCM(key).encrypt(iv, kat['plain'].encode('utf-8'), context + b'|' + kid)
                point = mine.public_key().public_bytes(serialization.Encoding.X962,
                                                       serialization.PublicFormat.UncompressedPoint)
                expected = (f"{crypto.WIRE_PREFIX}{robot['kid']}.{crypto.b64url(point)}."
                            f"{crypto.b64url(iv)}.{crypto.b64url(ciphertext)}")
                self.assertEqual(kat['wire'], expected)
                self.assertEqual(self.key.decrypt(kat['wire'], context=context), kat['plain'])

    def test_the_longest_edge_token_case_fits_the_robots_bound(self):
        longest = max((c for c in self.fixture['cases'] if c['context'] == 'edge'), key=lambda c: len(c['plain']))
        self.assertEqual(len(longest['plain']), 4096)
        self.assertLessEqual(len(longest['wire']), crypto.CLOUD_EDGE_MAX_WIRE_LEN)

    def test_an_old_robot_refuses_an_envelope_as_a_malformed_session(self):
        """A robot from before VC-06 validates the raw strings: an `enc1:` connect
        token is not 43 urlsafe characters, so the start is refused
        (cloud_texts_de.BAD_SESSION_DE, German) and nothing runs."""
        for case in self.fixture['cases']:
            if case['context'] == 'session':
                self.assertIsNone(cloud_protocol.TOKEN_RE.fullmatch(case['wire']))
