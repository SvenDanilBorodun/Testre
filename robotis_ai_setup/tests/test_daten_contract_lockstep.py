"""Daten 2.0: ONE wire contract, two languages (spec §J.2, §B2).

``daten/contract.py`` above its marker line ≡ the page's ``datenContract.js``:
the same names, the same values (a Python tuple = the JS
``Object.freeze([...])``), the same order. ``HTTP_PORT`` equals the port of the
``/daten-api/`` block of both manager nginx files. And every Python module that
repeats a contract value instead of importing it (the deps-free loaders cannot
reach the package) says the same as the contract.

``contract.py`` is read with ``ast`` (assignments above the marker), the JS with
one regex per line (``^export const (\\w+) = (.+);$``). The page's files are
implementer B's; until both halves are on one branch they may be absent — the
cross-language half then SKIPS with that reason (``EDUBOTICS_DATEN_PAGE_ROOT``
points it at another checkout, e.g. the page worktree). The Python half always
runs.
"""

import ast
import os
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
CONTRACT = PKG / 'daten' / 'contract.py'
MARKER = '# ---- Python-only below this line (not in datenContract.js) ----'
PAGE_ROOT = pathlib.Path(os.environ.get('EDUBOTICS_DATEN_PAGE_ROOT') or REPO_ROOT)
MANAGER = PAGE_ROOT / 'physical_ai_tools' / 'physical_ai_manager'
JS = MANAGER / 'src' / 'features' / 'editDataset' / 'datenContract.js'
NGINX = (MANAGER / 'nginx.conf', MANAGER / 'nginx.opi.conf.template')
_JS_LINE = re.compile(r'^export const (\w+) = (.+);$')
_JS_ARRAY = re.compile(r"^Object\.freeze\(\[(.*)\]\)$")
_JS_STRING = re.compile(r"^'([^'\\]*)'$")


def contract_values(above_marker=True):
    """``[(name, value)]`` in file order; ``above_marker`` selects the shared part."""
    text = CONTRACT.read_text(encoding='utf-8')
    assert text.count(MARKER) == 1, 'the marker line must appear exactly once'
    head, tail = text.split(MARKER)
    out = []
    for node in ast.parse(head if above_marker else tail).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            out.append((node.targets[0].id, ast.literal_eval(node.value) if above_marker else
                        eval(compile(ast.Expression(node.value), str(CONTRACT), 'eval'), {})))  # noqa: S307 — our own constants
    return out


def js_values(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('export const'):
            continue
        m = _JS_LINE.match(line)
        assert m, f'not a one-line export const: {line!r}'
        name, raw = m.group(1), m.group(2).strip()
        if re.fullmatch(r'-?\d+', raw):
            value = int(raw)
        elif _JS_STRING.match(raw):
            value = _JS_STRING.match(raw).group(1)
        else:
            arr = _JS_ARRAY.match(raw)
            assert arr, f'{name}: neither an integer, a single-quoted string nor Object.freeze([...])'
            items = [x.strip() for x in arr.group(1).split(',') if x.strip()]
            assert all(_JS_STRING.match(x) for x in items), f'{name}: an array item is not a single-quoted string'
            value = tuple(_JS_STRING.match(x).group(1) for x in items)
        out.append((name, value))
    return out


def _assigned(path, name):
    """A module-level constant read by ``ast`` (for modules the deps-free run
    cannot import: data_manager, v3_surgery)."""
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = node.value
            if isinstance(value, ast.Call) and getattr(value.func, 'attr', '') == 'compile':
                value = value.args[0]
            return eval(compile(ast.Expression(value), str(path), 'eval'), {})  # noqa: S307 — literals
    raise AssertionError(f'{name} not assigned in {path.name}')


def _load(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TheContractFile(unittest.TestCase):

    def test_the_shared_part_has_only_plain_values(self):
        names = [n for n, _ in contract_values()]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn('HTTP_PORT', names)
        for name, value in contract_values():
            self.assertIsInstance(value, (int, str, tuple), name)
            if isinstance(value, str) and name.endswith('_RE'):
                self.assertNotIn('\\', value, f'{name}: a regex here must need no backslash in either language')
                re.compile(value)

    def test_the_module_imports_what_it_declares(self):
        C = _load(CONTRACT, '_daten_contract_lockstep')
        for name, value in contract_values() + contract_values(above_marker=False):
            self.assertEqual(getattr(C, name), value, name)


@unittest.skipUnless(JS.is_file(), f'the page half (spec §J.1, implementer B) is not on this branch: {JS}')
class PythonEqualsJavaScript(unittest.TestCase):

    def test_same_names_values_and_order(self):
        self.assertEqual(js_values(JS.read_text(encoding='utf-8')), contract_values())

    def test_http_port_is_the_nginx_daten_block_port(self):
        port = dict(contract_values())['HTTP_PORT']
        for path in NGINX:
            with self.subTest(path=path.name):
                text = path.read_text(encoding='utf-8')
                block = re.search(r'location /daten-api/ \{(.*?)\n    \}', text, re.S)
                self.assertIsNotNone(block, f'{path.name} has no /daten-api/ block')
                ports = re.findall(r'proxy_pass http://\$edubotics_daten:(\d+);', block.group(1))
                self.assertEqual(ports, [str(port)])
                self.assertIn('limit_except GET HEAD { deny all; }', block.group(1))

    def test_the_parser_has_teeth(self):
        text = JS.read_text(encoding='utf-8').replace("'merge']);", "'merge', 'rename']);", 1)
        self.assertNotEqual(js_values(text), contract_values())


class PythonCopiesEqualTheContract(unittest.TestCase):
    """The modules that repeat a contract value say the same."""

    @classmethod
    def setUpClass(cls):
        cls.C = dict(contract_values() + contract_values(above_marker=False))
        cls.LT = _load(PKG / 'daten' / 'link_tokens.py', '_lockstep_link_tokens')
        cls.S = _load(PKG / 'data_processing' / 'dataset_sync.py', '_lockstep_dataset_sync')
        cls.HS = _load(PKG / 'data_processing' / 'hub_sync.py', '_lockstep_hub_sync')
        cls.SIG = _load(PKG / 'signal_status.py', '_lockstep_signal_status')
        cls.DW = _load(PKG / 'daten' / 'download_worker.py', '_lockstep_download_worker')

    def test_link_tokens(self):
        self.assertEqual((self.LT.SECRET_DIR, self.LT.TOKEN_TTL_S), (self.C['SECRET_DIR'], self.C['TOKEN_TTL_S']))

    def test_dataset_sync(self):
        self.assertEqual(self.S.MARKER_PREFIX, self.C['MARKER_PREFIX'])
        self.assertEqual(self.S.DATASET_PART_RE.pattern, self.C['DATASET_PART_RE'])
        self.assertEqual(self.S.RESERVED_SUFFIXES, self.C['RESERVED_SUFFIXES'])
        self.assertEqual(self.S.SESSION_MARKER_SUFFIX,
                         _assigned(PKG / 'data_processing' / 'data_manager.py', 'SESSION_MARKER_SUFFIX'))

    def test_hub_sync(self):
        self.assertEqual((self.HS.TAG, self.HS.TAG_RETRIES, self.HS.READBACK_TRIES),
                         (self.C['TAG'], self.C['TAG_RETRIES'], self.C['READBACK_TRIES']))
        self.assertEqual(self.HS.DISK_START_FLOOR_BYTES, self.SIG.DISK_START_FLOOR_BYTES)
        self.assertEqual(set(self.HS.TMP_SUFFIXES + self.HS.BAK_SUFFIXES + (self.HS.TRASH_SUFFIX,)),
                         set(self.C['RESERVED_SUFFIXES']))

    def test_the_recorder_the_engine_and_the_download_worker(self):
        dm = PKG / 'data_processing' / 'data_manager.py'
        self.assertEqual(_assigned(dm, 'START_UPLOAD_POLL_S'), self.C['START_UPLOAD_POLL_S'])
        v3 = PKG / 'data_processing' / 'v3_surgery.py'
        self.assertEqual(_assigned(v3, 'FEATURE_KEY_RE'), self.C['FEATURE_KEY_RE'])
        self.assertEqual(_assigned(v3, 'MAX_INDEX'), self.C['MAX_INDEX'])
        self.assertEqual(self.DW.POLL_S, self.C['DOWNLOAD_POLL_S'])


if __name__ == '__main__':
    unittest.main()
