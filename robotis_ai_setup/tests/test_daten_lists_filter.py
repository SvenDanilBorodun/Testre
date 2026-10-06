"""Daten 2.0, R-28: the old page's two lists (/get_user_list, /get_dataset_list —
the Training page's dataset picker) show datasets and namespaces only. A Daten
transaction's ``.tmp_sync``/``.bak_sync``/``.trash_edit``/… directory, a hidden
sibling (``.<name>.lock``, a journal, a next record), the ``.sync.json`` record
and the ``.session.json`` crash marker, and a symlink never appear.

The filter is ``dataset_sync.listed_names`` (stdlib, loaded by path); the node's
two callbacks are checked to call it (physical_ai_server.py needs rclpy, so the
wiring is read from its AST). The filter's name rule and suffix list equal the
wire contract's (``daten/contract.py``).
"""

import ast
import importlib.util
import os
import pathlib
import shutil
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = _load(PKG / 'data_processing' / 'dataset_sync.py', '_daten_lists_dataset_sync')
C = _load(PKG / 'daten' / 'contract.py', '_daten_lists_contract')


class ListedNames(unittest.TestCase):

    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix='d2_lists_'))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        ns = self.base / 'lena-schmidt'
        for name in ('omx_f_wuerfel', 'omx_f_becher', 'omx_f_v1.2'):
            (ns / name / 'meta').mkdir(parents=True)
        for suffix in C.RESERVED_SUFFIXES:
            (ns / f'omx_f_wuerfel{suffix}').mkdir()
        (ns / '.omx_f_wuerfel.lock').write_text('')
        (ns / '.omx_f_wuerfel.journal.json').write_text('{}')
        (ns / '.omx_f_wuerfel.sync.next.json').write_text('{}')
        (ns / 'omx_f_wuerfel.sync.json').write_text('{}')
        (ns / 'omx_f_wuerfel.session.json').write_text('{}')
        (ns / '.hidden_dir').mkdir()
        (ns / '_starts_with_underscore').mkdir()
        (ns / 'link').symlink_to(ns / 'omx_f_becher', target_is_directory=True)
        (self.base / '.cache').mkdir()
        (self.base / 'schule-org').mkdir()
        self.ns = ns

    def test_datasets_only(self):
        self.assertEqual(sorted(S.listed_names(self.ns)), ['omx_f_becher', 'omx_f_v1.2', 'omx_f_wuerfel'])

    def test_namespaces_only(self):
        self.assertEqual(sorted(S.listed_names(self.base)), ['lena-schmidt', 'schule-org'])

    def test_directory_order_is_kept(self):
        self.assertEqual(S.listed_names(self.ns),
                         [n for n in os.listdir(self.ns) if n in {'omx_f_becher', 'omx_f_v1.2', 'omx_f_wuerfel'}])

    def test_the_rule_is_the_wire_contracts(self):
        self.assertEqual(S.DATASET_PART_RE.pattern, C.DATASET_PART_RE)
        self.assertEqual(S.RESERVED_SUFFIXES, C.RESERVED_SUFFIXES)


class TheNodeUsesIt(unittest.TestCase):

    def test_both_list_callbacks_filter(self):
        tree = ast.parse((PKG / 'physical_ai_server.py').read_text(encoding='utf-8'))
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for name in ('get_user_list_callback', 'get_dataset_list_callback'):
            with self.subTest(name=name):
                calls = [n for n in ast.walk(funcs[name]) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == 'listed_names'
                         and isinstance(n.func.value, ast.Name) and n.func.value.id == 'dataset_sync']
                self.assertEqual(len(calls), 1)
                self.assertNotIn('listdir', {getattr(n, 'attr', None) for n in ast.walk(funcs[name])},
                                 'an unfiltered os.listdir is back')


if __name__ == '__main__':
    unittest.main()
