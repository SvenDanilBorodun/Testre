"""Daten 2.0 link tokens (spec §B5): mint/verify, expiry, scope, tampering, a
constant-time compare, and the atomically published shared secret (P22).

``daten/link_tokens.py`` is stdlib-only; it is loaded by path (this directory's
other loaders stub the ``physical_ai_server`` package). The secret race runs 8
real processes per round, released together, 50 rounds.
"""

import ast
import base64
import importlib.util
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
LINK_TOKENS_PATH = PKG / 'daten' / 'link_tokens.py'
CONTRACT_PATH = PKG / 'daten' / 'contract.py'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LT = _load(LINK_TOKENS_PATH, '_daten_link_tokens_under_test')
SECRET = b'k' * 32


class MintAndVerify(unittest.TestCase):

    def test_round_trip_for_both_scopes(self):
        for scope in ('lib', LT.dataset_scope('lena-schmidt/omx_f_wuerfel')):
            token = LT.mint(SECRET, scope, now=1000)
            self.assertTrue(token.startswith('v1.'))
            self.assertEqual(LT.verify(SECRET, token, scope, now=1001), (True, None))

    def test_the_format_is_v1_payload_mac32(self):
        token = LT.mint(SECRET, 'lib', ttl_s=1800, now=1000)
        version, payload, mac = token.split('.')
        self.assertEqual(version, 'v1')
        self.assertEqual(len(mac), 32)
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        self.assertEqual(claims, {'s': 'lib', 'e': 2800})

    def test_expiry(self):
        token = LT.mint(SECRET, 'lib', ttl_s=1800, now=1000)
        self.assertEqual(LT.verify(SECRET, token, 'lib', now=2799), (True, None))
        self.assertEqual(LT.verify(SECRET, token, 'lib', now=2800), (False, 'token_expired'))

    def test_a_dataset_token_is_refused_on_another_dataset_and_on_the_library(self):
        token = LT.mint(SECRET, LT.dataset_scope('lena-schmidt/a'), now=1000)
        self.assertEqual(LT.verify(SECRET, token, LT.dataset_scope('lena-schmidt/b'), now=1001),
                         (False, 'scope'))
        self.assertEqual(LT.verify(SECRET, token, 'lib', now=1001), (False, 'scope'))
        lib = LT.mint(SECRET, 'lib', now=1000)
        self.assertEqual(LT.verify(SECRET, lib, LT.dataset_scope('lena-schmidt/a'), now=1001),
                         (False, 'scope'))

    def test_a_tampered_payload_or_mac_is_invalid(self):
        token = LT.mint(SECRET, LT.dataset_scope('lena-schmidt/a'), now=1000)
        version, payload, mac = token.split('.')
        forged_claims = base64.urlsafe_b64encode(
            json.dumps({'s': 'lib', 'e': 10 ** 10}).encode()).rstrip(b'=').decode()
        flipped = mac[:-1] + ('A' if mac[-1] != 'A' else 'B')
        for bad in (f'{version}.{forged_claims}.{mac}', f'{version}.{payload}.{flipped}',
                    f'v2.{payload}.{mac}', f'{payload}.{mac}', '', 'v1..', 'v1.x.y',
                    'v1.' + 'a' * 2000 + '.' + mac, None, 123):
            self.assertEqual(LT.verify(SECRET, bad, 'lib', now=1001), (False, 'token_invalid'),
                             repr(bad)[:60])

    def test_another_secret_is_invalid(self):
        token = LT.mint(SECRET, 'lib', now=1000)
        self.assertEqual(LT.verify(b'x' * 32, token, 'lib', now=1001), (False, 'token_invalid'))

    def test_the_mac_is_compared_in_constant_time(self):
        tree = ast.parse(LINK_TOKENS_PATH.read_text(encoding='utf-8'))
        verify = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == 'verify')
        calls = [n for n in ast.walk(verify) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'compare_digest']
        self.assertGreaterEqual(len(calls), 1)
        # and no plain == / != on the MAC
        for node in ast.walk(verify):
            if isinstance(node, ast.Compare):
                names = {getattr(x, 'id', None) for x in [node.left, *node.comparators]}
                self.assertFalse({'mac', 'expected'} & names, ast.dump(node))

    def test_the_constants_are_the_contract_s(self):
        contract = _load(CONTRACT_PATH, '_daten_contract_for_tokens')
        self.assertEqual(LT.TOKEN_TTL_S, contract.TOKEN_TTL_S)
        self.assertEqual(LT.SECRET_DIR, contract.SECRET_DIR)


_RACER = textwrap.dedent('''
    import importlib.util, os, sys, time
    spec = importlib.util.spec_from_file_location('lt', sys.argv[1])
    lt = importlib.util.module_from_spec(spec); spec.loader.exec_module(lt)
    go = sys.argv[3]
    open(sys.argv[4] + '/' + str(os.getpid()), 'w').close()      # ready
    while not os.path.exists(go):
        time.sleep(0.0002)
    sys.stdout.write(lt.load_or_create_secret(sys.argv[2]).hex())
''')


class TheSecretIsPublishedAtomically(unittest.TestCase):
    ROUNDS = 50
    PROCS = 8

    def test_eight_racing_processes_always_read_the_same_32_bytes(self):
        base = pathlib.Path(tempfile.mkdtemp(prefix='d2_secret_'))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        racer = base / 'racer.py'
        racer.write_text(_RACER)
        disagreements = short = 0
        for r in range(self.ROUNDS):
            d = base / f'round{r}' / 'edubotics-daten'
            go = base / f'go{r}'
            ready = base / f'ready{r}'
            ready.mkdir()
            procs = [subprocess.Popen([sys.executable, str(racer), str(LINK_TOKENS_PATH), str(d), str(go),
                                       str(ready)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(self.PROCS)]
            deadline = time.monotonic() + 30
            while len(os.listdir(ready)) < self.PROCS and time.monotonic() < deadline:
                time.sleep(0.002)   # every racer imported and spinning on the go file
            go.write_text('go')
            outs = []
            for p in procs:
                out, err = p.communicate(timeout=30)
                self.assertEqual(p.returncode, 0, err)
                outs.append(out)
            short += sum(1 for o in outs if len(o) != 64)
            disagreements += len(set(outs)) - 1
            self.assertEqual(sorted(os.listdir(d)), ['secret'], f'round {r}: one entry, no tmp left')
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(d / 'secret').st_mode), 0o600)
            self.assertEqual((d / 'secret').read_bytes().hex(), outs[0])
        self.assertEqual((disagreements, short), (0, 0))

    def test_a_short_secret_on_disk_is_replaced_not_used(self):
        d = pathlib.Path(tempfile.mkdtemp(prefix='d2_secret_short_')) / 'x'
        self.addCleanup(shutil.rmtree, d.parent, ignore_errors=True)
        d.mkdir(mode=0o700)
        (d / 'secret').write_bytes(b'abc')
        secret = LT.load_or_create_secret(str(d))
        self.assertEqual(len(secret), 32)
        self.assertEqual((d / 'secret').read_bytes(), secret)


if __name__ == '__main__':
    unittest.main()
