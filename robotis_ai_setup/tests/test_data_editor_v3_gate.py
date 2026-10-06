"""data_editor_v3 on the Daten 2.0 engine + dataset_edit_callback routing (deps-free).

The engine (``v3_surgery``: pyarrow/PyAV, LeRobot inside its functions) is
STUBBED here by a fake with the same surface (``Source``, ``assemble``,
``verify_or_raise``, ``episode_identity``, ``check_compatible``,
``SurgeryError``) that writes plain directory trees and remembers the parts it
was asked for — so these tests judge data_editor_v3's own contract: German
pre-validation BEFORE any engine call; the swap (tmp → verify → bak → promote →
re-check → drop bak) leaving the source byte-untouched on every failure; the
split's two promotions as ONE transaction, also across a crash between the
renames (the journal recovery); the three-way union's parts are exactly
``dataset_sync.plan_keep_both``'s; every promoted output's record carries
``files``. The real engine is tested in ``physical_ai_server/test/test_v3_*.py``
and, with LeRobot, in the image (``.github/scripts/daten_smoke.py``).

The callback routing: ``dataset_edit_callback`` hands the edit to the node's
Daten service when there is one (``run_edit_blocking``), else routes v3.0 to
data_editor_v3 and v2.1 to the legacy DataEditor, refuses mixed versions in
German, and maps DataEditError to a German response.message.
"""

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
V3_PATH = PKG_ROOT / 'data_processing' / 'data_editor_v3.py'
WORKER_PATH = PKG_ROOT / 'data_processing' / 'edit_worker.py'
DATASET_PATHS_PATH = PKG_ROOT / 'data_processing' / 'dataset_paths.py'
COMMUNICATOR_PATH = PKG_ROOT / 'communication' / 'communicator.py'
SYNC_PATH = PKG_ROOT / 'data_processing' / 'dataset_sync.py'
TEXTS_PATH = PKG_ROOT / 'daten' / 'texts_de.py'

ENGINE_NAME = 'physical_ai_server.data_processing.v3_surgery'


# ---- shared helpers ------------------------------------------------------------

def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _load_by_path(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = _load_by_path('_gate_dataset_sync', SYNC_PATH)
T = _load_by_path('_gate_texts_de', TEXTS_PATH)


def _exec_or_unregister(spec, module, canonical):
    """exec_module, never leaving a half-built husk in sys.modules (a module
    whose exec raised would be handed to every LATER test module)."""
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(canonical, None)
        raise
    return module


# ---- the fake engine -------------------------------------------------------------

class _SurgeryError(RuntimeError):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code
        self.detail = detail


class _Engine:
    """Mutable behaviour of the fake v3_surgery."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.assemble_calls = []
        self.on_assemble = None          # hook(out, parts) -> may raise
        self.verify_fail = set()         # output names whose verify fails
        self.incompatible = []           # failing MERGE_CHECKS ids


ENGINE = _Engine()
MERGE_CHECKS = ('version', 'robot', 'fps', 'cameras', 'joints', 'video', 'stats')


class _FakeSource:
    def __init__(self, root):
        self.root = Path(root)
        try:
            info = json.loads((self.root / 'meta' / 'info.json').read_text())
        except Exception as e:  # noqa: BLE001
            raise _SurgeryError('layout', type(e).__name__) from e
        self.info = info
        self.ids = json.loads((self.root / 'ids.json').read_text())
        self.episodes = [{'episode_index': i} for i in range(info['total_episodes'])]
        self.video_keys = ['observation.images.gripper']


def _fake_check_compatible(sources):
    return [(c not in ENGINE.incompatible, c) for c in MERGE_CHECKS]


def _fake_episode_identity(src, ep):
    return src.ids[ep]


def _fake_assemble(out, parts, repo_id, *, progress=None):
    out = Path(out)
    ENGINE.assemble_calls.append({'out': out, 'parts': [(str(s.root), e) for s, e in parts],
                                  'repo_id': repo_id})
    if out.exists():
        raise _SurgeryError('exists')
    bad = [c for ok, c in _fake_check_compatible(list({id(s): s for s, _ in parts}.values())) if not ok]
    if bad:
        raise _SurgeryError('incompatible', ','.join(bad))
    if ENGINE.on_assemble is not None:
        ENGINE.on_assemble(out, parts)
    _write_tree(out, [s.ids[e] for s, e in parts])
    (out / 'BUILT').write_text(repo_id)
    for i in range(len(parts)):
        if progress:
            progress('copy', i + 1, len(parts))


def _fake_verify_or_raise(out, parts):
    if Path(out).name in ENGINE.verify_fail:
        raise _SurgeryError('verify_failed', 'fake')


def _install_engine_stub():
    _stub('physical_ai_server')
    _stub('physical_ai_server.data_processing')
    eng = types.ModuleType(ENGINE_NAME)
    eng.SurgeryError = _SurgeryError
    eng.Source = _FakeSource
    eng.check_compatible = _fake_check_compatible
    eng.episode_identity = _fake_episode_identity
    eng.assemble = _fake_assemble
    eng.verify_or_raise = _fake_verify_or_raise
    eng.MERGE_CHECKS = MERGE_CHECKS
    sys.modules[ENGINE_NAME] = eng
    return eng


def _write_tree(root, ids, version='v3.0', marker=''):
    """A dataset-shaped tree: meta/info.json, meta/episodes, data, videos, and
    ids.json (the fake engine's episode identities)."""
    root = Path(root)
    (root / 'meta' / 'episodes' / 'chunk-000').mkdir(parents=True, exist_ok=True)
    (root / 'data' / 'chunk-000').mkdir(parents=True, exist_ok=True)
    cam = root / 'videos' / 'observation.images.gripper' / 'chunk-000'
    cam.mkdir(parents=True, exist_ok=True)
    (root / 'meta' / 'info.json').write_text(json.dumps({
        'codebase_version': version, 'total_episodes': len(ids)}), encoding='utf-8')
    (root / 'meta' / 'episodes' / 'chunk-000' / 'file-000.parquet').write_text('meta ' + ','.join(ids))
    (root / 'data' / 'chunk-000' / 'file-000.parquet').write_text('data ' + ','.join(ids))
    (cam / 'file-000.mp4').write_text('mp4 ' + ','.join(ids))
    (root / 'ids.json').write_text(json.dumps(list(ids)))
    if marker:
        (root / marker).write_text(marker)
    return root


def _make_v3_tree(root, total_episodes, version='v3.0', marker=''):
    return _write_tree(root, [f'{Path(root).name}:{i}' for i in range(total_episodes)], version, marker)


def _make_v21_tree(root, total_episodes):
    (root / 'meta').mkdir(parents=True, exist_ok=True)
    (root / 'meta' / 'info.json').write_text(json.dumps({
        'codebase_version': 'v2.1', 'total_episodes': total_episodes}), encoding='utf-8')


def _make_corrupt_info_tree(root, total_episodes=3):
    _make_v3_tree(root, total_episodes)
    (root / 'meta' / 'info.json').write_text('{"codebase_version": "v3.0", "total_episo', encoding='utf-8')


def _make_versionless_info_tree(root, total_episodes=3):
    _make_v3_tree(root, total_episodes)
    (root / 'meta' / 'info.json').write_text(json.dumps({'total_episodes': total_episodes}), encoding='utf-8')


def _ids(root):
    return json.loads((Path(root) / 'ids.json').read_text())


def _files(root):
    out = {}
    for p in sorted(Path(root).rglob('*')):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and rel.startswith(('data/', 'meta/episodes/', 'videos/')):
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _load_v3_module():
    """Load data_editor_v3 ONCE under its canonical dotted name (communicator.py
    catches DataEditError by class identity: the same module object)."""
    canonical = 'physical_ai_server.data_processing.data_editor_v3'
    if canonical in sys.modules:
        return sys.modules[canonical]
    _stub('physical_ai_server')
    _stub('physical_ai_server.data_processing')
    spec = importlib.util.spec_from_file_location(canonical, str(V3_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    _exec_or_unregister(spec, module, canonical)
    sys.modules['physical_ai_server.data_processing'].data_editor_v3 = module
    return module


def _load_dataset_paths_module():
    canonical = 'physical_ai_server.data_processing.dataset_paths'
    _stub('physical_ai_server')
    _stub('physical_ai_server.data_processing')
    if canonical not in sys.modules:
        spec = importlib.util.spec_from_file_location(canonical, str(DATASET_PATHS_PATH))
        module = importlib.util.module_from_spec(spec)
        sys.modules[canonical] = module
        _exec_or_unregister(spec, module, canonical)
    module = sys.modules[canonical]
    sys.modules['physical_ai_server.data_processing'].dataset_paths = module
    return module


def _load_worker_module():
    canonical = 'physical_ai_server.data_processing.edit_worker'
    _load_dataset_paths_module()
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, str(WORKER_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = module
    _exec_or_unregister(spec, module, canonical)
    sys.modules['physical_ai_server.data_processing'].edit_worker = module
    return module


class _Base(BoundedTestCase):

    @classmethod
    def setUpClass(cls):
        cls._saved_engine = sys.modules.get(ENGINE_NAME)
        _install_engine_stub()
        cls.v3 = _load_v3_module()

    @classmethod
    def tearDownClass(cls):
        if cls._saved_engine is None:
            sys.modules.pop(ENGINE_NAME, None)
        else:
            sys.modules[ENGINE_NAME] = cls._saved_engine

    def setUp(self):
        _install_engine_stub()
        ENGINE.reset()
        self.tmpdir = Path(tempfile.mkdtemp(prefix='v3gate_')).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)


class DataEditorV3ModuleTest(_Base):

    # -- version gate ------------------------------------------------------------

    def test_is_v3_dataset(self):
        v3 = self.tmpdir / 'student' / 'ds_v3'
        _make_v3_tree(v3, 3)
        v21 = self.tmpdir / 'student' / 'ds_v21'
        _make_v21_tree(v21, 3)
        self.assertTrue(self.v3.is_v3_dataset(v3))
        self.assertFalse(self.v3.is_v3_dataset(v21))
        self.assertFalse(self.v3.is_v3_dataset(self.tmpdir / 'missing'))

    def test_is_v21_dataset_positive_only(self):
        v21 = self.tmpdir / 's' / 'ds_v21'
        _make_v21_tree(v21, 3)
        v3 = self.tmpdir / 's' / 'ds_v3'
        _make_v3_tree(v3, 3)
        corrupt = self.tmpdir / 's' / 'ds_corrupt'
        _make_corrupt_info_tree(corrupt, 3)
        versionless = self.tmpdir / 's' / 'ds_nover'
        _make_versionless_info_tree(versionless, 3)
        self.assertTrue(self.v3.is_v21_dataset(v21))
        self.assertFalse(self.v3.is_v21_dataset(v3))
        self.assertFalse(self.v3.is_v21_dataset(corrupt))
        self.assertFalse(self.v3.is_v21_dataset(versionless))
        self.assertFalse(self.v3.is_v21_dataset(self.tmpdir / 's' / 'missing'))

    # -- pre-validation fires BEFORE the engine --------------------------------------

    def test_delete_corrupt_info_is_german_beschaedigt_no_engine(self):
        src = self.tmpdir / 's' / 'ds'
        _make_corrupt_info_tree(src, 3)
        before = (src / 'meta' / 'info.json').read_text()
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [1])
        self.assertIn('beschädigt', str(ctx.exception))
        self.assertEqual(ENGINE.assemble_calls, [])
        self.assertEqual((src / 'meta' / 'info.json').read_text(), before)

    def test_delete_prevalidation_german_and_no_engine_call(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3)
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [])
        self.assertIn('Keine Episoden', str(ctx.exception))
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [7])
        self.assertIn('gibt es nicht', str(ctx.exception))
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [0, 1, 2])
        self.assertIn('Alle Episoden', str(ctx.exception))
        self.assertEqual(ENGINE.assemble_calls, [])

    def test_delete_missing_dataset_german(self):
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(self.tmpdir / 'nope'), [0])
        self.assertIn('nicht gefunden', str(ctx.exception))

    # -- delete: the swap -------------------------------------------------------------

    def test_delete_happy_path_builds_the_complement_and_swaps(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 4, marker='ORIGINAL')
        S.write_record(src, {'v': 1, 'repo_id': 'student/ds', 'hub_sha': 'a' * 40,
                             'local_digest': 'old', 'display_name': 'Würfel'})
        progress = []
        remaining = self.v3.delete_episodes_v3(str(src), [1, 3], progress=lambda *a: progress.append(a))
        self.assertEqual(remaining, 2)
        call = ENGINE.assemble_calls[0]
        self.assertEqual(call['out'], src.parent / 'ds.tmp_edit')
        self.assertEqual(call['repo_id'], 'student/ds')
        self.assertEqual([e for _, e in call['parts']], [0, 2])
        self.assertEqual(_ids(src), ['ds:0', 'ds:2'])
        self.assertFalse((src / 'ORIGINAL').exists())
        self.assertFalse((src.parent / 'ds.tmp_edit').exists())
        self.assertFalse((src.parent / 'ds.bak_edit').exists())
        rec = S.read_record(src)
        self.assertEqual(rec['files'], _files(src), 'the promoted output carries files')
        self.assertEqual((rec['hub_sha'], rec['display_name'], rec['local_digest']),
                         ('a' * 40, 'Würfel', 'old'), 'a delete keeps the rest of the record')
        self.assertEqual({p[0] for p in progress}, {'prepare', 'copy', 'verify', 'swap'})

    def test_a_record_less_delete_gets_a_record_with_files(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3)
        self.v3.delete_episodes_v3(str(src), [0])
        self.assertEqual(S.read_record(src), {'v': 1, 'repo_id': 'student/ds', 'files': _files(src)})

    def test_a_failed_verify_keeps_the_source_untouched(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')
        ENGINE.verify_fail.add('ds.tmp_edit')
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [1])
        self.assertEqual(str(ctx.exception), T.VERIFY_FAILED_DE)
        self.assertEqual(ctx.exception.code, 'verify_failed')
        self.assertTrue((src / 'ORIGINAL').exists())
        self.assertEqual(_ids(src), ['ds:0', 'ds:1', 'ds:2'])
        self.assertFalse((src.parent / 'ds.tmp_edit').exists())
        self.assertFalse((src.parent / 'ds.bak_edit').exists())

    def test_an_engine_refusal_is_its_german_sentence(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')

        def unaligned(out, parts):
            Path(out).mkdir(parents=True)
            raise _SurgeryError('unaligned', 'x')
        ENGINE.on_assemble = unaligned
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.delete_episodes_v3(str(src), [1])
        self.assertEqual(str(ctx.exception), T.UNALIGNED_DE)
        self.assertEqual(ctx.exception.code, 'unaligned')
        self.assertTrue((src / 'ORIGINAL').exists())
        self.assertFalse((src.parent / 'ds.tmp_edit').exists())

    def test_delete_promote_failure_restores_original(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')
        real_rename = Path.rename

        def failing_second_rename(self_path, target):
            if str(self_path).endswith('.tmp_edit'):
                raise OSError('simulated promote failure')
            return real_rename(self_path, target)
        with mock.patch.object(Path, 'rename', failing_second_rename):
            with self.assertRaises(self.v3.DataEditError) as ctx:
                self.v3.delete_episodes_v3(str(src), [1])
        self.assertIn('wiederhergestellt', str(ctx.exception))
        self.assertTrue((src / 'ORIGINAL').exists())
        self.assertFalse((src.parent / 'ds.bak_edit').exists())
        self.assertFalse((src.parent / 'ds.tmp_edit').exists())
        self.assertFalse(S.record_next_path(src).exists())

    def test_delete_clears_stale_artifacts(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_v3_tree(src, 3, marker='ORIGINAL')
        (src.parent / 'ds.tmp_edit').mkdir()
        (src.parent / 'ds.tmp_edit' / 'junk').write_text('x')
        (src.parent / 'ds.bak_edit').mkdir()
        self.assertEqual(self.v3.delete_episodes_v3(str(src), [0]), 2)
        self.assertEqual(_ids(src), ['ds:1', 'ds:2'])

    # -- merge ----------------------------------------------------------------------

    def test_merge_happy_path_all_episodes_in_order(self):
        a = _make_v3_tree(self.tmpdir / 'student' / 'a', 2)
        b = _make_v3_tree(self.tmpdir / 'student' / 'b', 3)
        S.write_record(a, {'v': 1, 'repo_id': 'student/a', 'private': True})
        out = self.tmpdir / 'student' / 'merged'
        n = self.v3.merge_datasets_v3([str(a), str(b)], str(out), display_name='Alles')
        self.assertEqual(n, 5)
        self.assertEqual(_ids(out), ['a:0', 'a:1', 'b:0', 'b:1', 'b:2'])
        rec = S.read_record(out)
        self.assertEqual((rec['repo_id'], rec['display_name'], rec['private']), ('student/merged', 'Alles', True))
        self.assertEqual(rec['files'], _files(out))
        self.assertFalse((out.parent / 'merged.tmp_edit').exists())

    def test_merge_rejects_single_source_and_an_existing_output(self):
        a = _make_v3_tree(self.tmpdir / 'student' / 'a', 2)
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.merge_datasets_v3([str(a)], str(self.tmpdir / 'out'))
        self.assertIn('mindestens zwei', str(ctx.exception))
        b = _make_v3_tree(self.tmpdir / 'student' / 'b', 2)
        out = self.tmpdir / 'occupied'
        out.mkdir()
        (out / 'something').write_text('x')
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.merge_datasets_v3([str(a), str(b)], str(out))
        self.assertEqual(str(ctx.exception), T.EXISTS_DE)
        self.assertEqual(ENGINE.assemble_calls, [])

    def test_merge_failure_cleans_output(self):
        a = _make_v3_tree(self.tmpdir / 'student' / 'a', 2)
        b = _make_v3_tree(self.tmpdir / 'student' / 'b', 3)
        out = self.tmpdir / 'student' / 'merged'

        def explode(o, parts):
            Path(o).mkdir(parents=True, exist_ok=True)
            (Path(o) / 'partial').write_text('x')
            raise RuntimeError('boom')
        ENGINE.on_assemble = explode
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.merge_datasets_v3([str(a), str(b)], str(out))
        self.assertEqual(str(ctx.exception), T.RUN_EDIT_FAILED_DE)
        self.assertFalse(out.exists())
        self.assertFalse((out.parent / 'merged.tmp_edit').exists())

    def test_an_incompatible_merge_names_the_checks_in_german(self):
        a = _make_v3_tree(self.tmpdir / 'student' / 'a', 2)
        b = _make_v3_tree(self.tmpdir / 'student' / 'b', 3)
        ENGINE.incompatible = ['stats']
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.merge_datasets_v3([str(a), str(b)], str(self.tmpdir / 'student' / 'm'))
        self.assertEqual(str(ctx.exception), 'Diese Datensätze lassen sich nicht zusammenführen: Statistiken.')
        self.assertEqual(ctx.exception.code, 'incompatible')

    # -- split: two outputs, one transaction ----------------------------------------

    def test_split_moves_the_chosen_episodes_into_a_new_dataset(self):
        src = _make_v3_tree(self.tmpdir / 'student' / 'ds', 5)
        S.write_record(src, {'v': 1, 'repo_id': 'student/ds', 'hub_sha': 'b' * 40, 'private': False})
        new = self.tmpdir / 'student' / 'ds_neu'
        kept, moved = self.v3.split_episodes_v3(str(src), [1, 4], str(new), display_name='Neu')
        self.assertEqual((kept, moved), (3, 2))
        self.assertEqual(_ids(new), ['ds:1', 'ds:4'])
        self.assertEqual(_ids(src), ['ds:0', 'ds:2', 'ds:3'])
        rec_new, rec_src = S.read_record(new), S.read_record(src)
        self.assertEqual((rec_new['display_name'], rec_new['private'], rec_new['files']), ('Neu', False, _files(new)))
        self.assertEqual((rec_src['hub_sha'], rec_src['files']), ('b' * 40, _files(src)))
        for leftover in ('ds.tmp_edit', 'ds_neu.tmp_edit', 'ds.bak_edit', '.ds.journal.json'):
            self.assertFalse((src.parent / leftover).exists(), leftover)

    def test_split_refusals(self):
        src = _make_v3_tree(self.tmpdir / 'student' / 'ds', 3)
        taken = _make_v3_tree(self.tmpdir / 'student' / 'taken', 1)
        with self.assertRaises(self.v3.DataEditError) as ctx:
            self.v3.split_episodes_v3(str(src), [1], str(taken))
        self.assertEqual(str(ctx.exception), T.EXISTS_DE)
        for bad in ([], [0, 1, 2], [5]):
            with self.assertRaises(self.v3.DataEditError):
                self.v3.split_episodes_v3(str(src), bad, str(self.tmpdir / 'student' / 'neu'))
        self.assertEqual(ENGINE.assemble_calls, [])

    def _split_until_rename(self, n_ok):
        """Run a split whose (n_ok+1)-th promotion rename fails hard (a crash
        between the renames); the journal is left for the recovery."""
        src = _make_v3_tree(self.tmpdir / 'student' / 'ds', 4, marker='ORIGINAL')
        new = self.tmpdir / 'student' / 'ds_neu'
        real_rename = Path.rename
        count = {'n': 0}

        class Crash(BaseException):
            pass

        def renames(self_path, target):
            if str(target).endswith(('/ds', '/ds.bak_edit', '/ds_neu')):
                count['n'] += 1
                if count['n'] > n_ok:
                    raise Crash()
            return real_rename(self_path, target)
        with mock.patch.object(Path, 'rename', renames), \
                mock.patch.object(self.v3, 'recover_split', lambda p: None):
            if n_ok < 3:
                with self.assertRaises(Crash):
                    self.v3.split_episodes_v3(str(src), [0, 2], str(new))
            else:                   # all three renames done, the process dies before the roll forward
                self.v3.split_episodes_v3(str(src), [0, 2], str(new))
        self.assertTrue(S.journal_path(src).exists(), 'the crash leaves the journal')
        return src, new

    def test_a_crash_between_the_renames_rolls_back_to_the_original(self):
        for n_ok in (1, 2):
            with self.subTest(renames_done=n_ok):
                src, new = self._split_until_rename(n_ok)
                self.assertEqual(self.v3.recover_split(src), 'back')
                self.assertTrue((src / 'ORIGINAL').exists())
                self.assertEqual(_ids(src), ['ds:0', 'ds:1', 'ds:2', 'ds:3'])
                self.assertFalse(new.exists())
                for leftover in ('ds.tmp_edit', 'ds_neu.tmp_edit', 'ds.bak_edit', '.ds.journal.json'):
                    self.assertFalse((src.parent / leftover).exists(), leftover)
                shutil.rmtree(src.parent)

    def test_a_crash_after_the_last_rename_rolls_forward(self):
        src, new = self._split_until_rename(3)     # never raises: all three renames done
        self.assertEqual(self.v3.recover_split(src), 'forward')
        self.assertEqual(_ids(new), ['ds:0', 'ds:2'])
        self.assertEqual(_ids(src), ['ds:1', 'ds:3'])
        self.assertEqual(S.read_record(new)['files'], _files(new))
        self.assertEqual(S.read_record(src)['files'], _files(src))
        self.assertFalse((src.parent / 'ds.bak_edit').exists())
        self.assertFalse(S.journal_path(src).exists())

    # -- union: the three-way plan ----------------------------------------------------

    def _union_fixture(self, with_base=True):
        loc = _write_tree(self.tmpdir / 'student' / 'ds', ['a', 'c', 'l1'])
        hub = _write_tree(self.tmpdir / 'student' / 'ds.tmp_keep', ['a', 'b', 'c', 'd', 'h1'])
        base = _write_tree(self.tmpdir / 'student' / 'ds.tmp_base', ['a', 'b', 'c', 'd']) if with_base else None
        S.write_record(loc, {'v': 1, 'repo_id': 'student/ds', 'hub_sha': 'c' * 40, 'display_name': 'Würfel',
                             'private': True})
        return loc, hub, base

    def test_union_parts_are_exactly_plan_keep_both(self):
        for with_base in (True, False):
            with self.subTest(with_base=with_base):
                ENGINE.reset()
                loc, hub, base = self._union_fixture(with_base)
                lid, hid = _ids(loc), _ids(hub)
                bid = _ids(base) if base else None
                hub_digest = S.meta_digest(hub)
                plan = S.plan_keep_both(lid, hid, bid)
                res = self.v3.union_episodes_v3(str(loc), str(hub), str(base) if base else None,
                                                hub_sha='d' * 40, hub_trees={'data': 't1'})
                call = ENGINE.assemble_calls[0]
                want = [(str(loc if side == 'L' else hub), i) for side, i in plan]
                self.assertEqual(call['parts'], want)
                self.assertEqual(_ids(loc), [(lid if s == 'L' else hid)[i] for s, i in plan])
                self.assertEqual(res['three_way'], with_base)
                rec = S.read_record(loc)
                self.assertEqual((rec['hub_sha'], rec['hub_trees'], rec['local_digest']),
                                 ('d' * 40, {'data': 't1'}, hub_digest), 'the HUB copy\'s record')
                self.assertEqual(rec['files'], _files(loc))
                self.assertEqual((rec['display_name'], rec['private']), ('Würfel', True))
                self.assertFalse(hub.exists())
                self.assertFalse((self.tmpdir / 'student' / 'ds.tmp_base').exists())
                shutil.rmtree(loc.parent)
        # with the base: deleted here stays deleted (b, d), added on either side kept
        self.assertEqual(S.plan_keep_both(['a', 'c', 'l1'], ['a', 'b', 'c', 'd', 'h1'], ['a', 'b', 'c', 'd']),
                         [('L', 0), ('L', 1), ('L', 2), ('H', 4)])

    # -- fences ----------------------------------------------------------------------

    def test_no_dataset_tools_and_no_module_level_engine_import(self):
        src = V3_PATH.read_text(encoding='utf-8')
        tree = ast.parse(src)
        banned = {'dataset_tools', '_copy_and_reindex_videos', '_force_recorder_vcodec'}
        for node in ast.walk(tree):
            names = set()
            if isinstance(node, ast.Import):
                names = {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module or ''} | {a.name for a in node.names}
            elif isinstance(node, ast.Name):
                names = {node.id}
            elif isinstance(node, ast.Attribute):
                names = {node.attr}
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names = {node.name}
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in [
                    n.body[0].value for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                    and isinstance(n.body[0], ast.Expr)]:
                names = {node.value}
            self.assertFalse({n.split('.')[-1] for n in names} & banned, ast.dump(node)[:200])
        body = tree.body[1:] if (tree.body and isinstance(tree.body[0], ast.Expr)
                                 and isinstance(tree.body[0].value, ast.Constant)) else tree.body
        for node in body:
            text = ast.unparse(node)
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.Expr)):
                self.assertNotIn('v3_surgery', text, 'v3_surgery is imported inside functions only (A18)')
                self.assertNotIn('lerobot', text)


# ---- callback routing ---------------------------------------------------------

class _FakeLogger:
    def error(self, *_a, **_k):
        pass

    def info(self, *_a, **_k):
        pass

    def warning(self, *_a, **_k):
        pass


class _FakeNode:
    def get_logger(self):
        return _FakeLogger()


class _FakeLegacyEditor:
    calls = []

    def merge_datasets(self, paths, output):
        _FakeLegacyEditor.calls.append(('merge', list(paths), output))

    def delete_episode(self, path, idx):
        _FakeLegacyEditor.calls.append(('delete_one', path, idx))

    def delete_episodes_batch(self, path, indices):
        _FakeLegacyEditor.calls.append(('delete_batch', path, list(indices)))


class _EditRequest:
    def __init__(self, mode, merge_list=(), delete_path='', episodes=(), output_path=''):
        self.mode = mode
        self.merge_dataset_list = list(merge_list)
        self.delete_dataset_path = delete_path
        self.delete_episode_num = list(episodes)
        self.output_path = output_path
        self.upload_huggingface = False


class _EditResponse:
    def __init__(self):
        self.success = False
        self.message = ''


def _install_ros_stubs():
    placeholder = type('_Placeholder', (), {})

    class _EditDatasetRequest:
        MERGE = 0
        DELETE = 1

    class _EditDataset:
        Request = _EditDatasetRequest

    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=placeholder)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=placeholder)
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', BrowserItem=placeholder, DatasetInfo=placeholder,
          TaskStatus=placeholder)
    _stub('physical_ai_interfaces.srv', BrowseFile=placeholder, EditDataset=_EditDataset,
          GetDatasetInfo=placeholder, GetImageTopicList=placeholder)
    _stub('rclpy')
    _stub('rclpy.node', Node=placeholder)
    _stub('rclpy.qos',
          DurabilityPolicy=types.SimpleNamespace(TRANSIENT_LOCAL=1, VOLATILE=2),
          HistoryPolicy=types.SimpleNamespace(KEEP_LAST=1),
          QoSProfile=lambda **kw: kw,
          ReliabilityPolicy=types.SimpleNamespace(RELIABLE=1, BEST_EFFORT=2))
    _stub('rosbag_recorder')
    _stub('rosbag_recorder.srv', SendCommand=placeholder)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', CompressedImage=placeholder, JointState=placeholder)
    _stub('std_msgs')
    _stub('std_msgs.msg', Empty=placeholder, String=placeholder)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=placeholder)
    _stub('physical_ai_server.communication')
    _stub('physical_ai_server.communication.multi_subscriber', MultiSubscriber=placeholder)
    _stub('physical_ai_server.data_processing.data_editor', DataEditor=_FakeLegacyEditor)
    _stub('physical_ai_server.utils')
    _stub('physical_ai_server.utils.file_browse_utils', FileBrowseUtils=placeholder)
    _stub('physical_ai_server.utils.parameter_utils',
          parse_topic_list=lambda *a, **k: [],
          parse_topic_list_with_names=lambda *a, **k: {})
    return _EditDataset


class DatasetEditCallbackRoutingTest(_Base):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.EditDataset = _install_ros_stubs()
        cls.worker = _load_worker_module()
        canonical = 'physical_ai_server.communication.communicator'
        spec = importlib.util.spec_from_file_location(canonical, str(COMMUNICATOR_PATH))
        module = importlib.util.module_from_spec(spec)
        sys.modules[canonical] = module
        spec.loader.exec_module(module)
        cls.communicator_mod = module

    def setUp(self):
        super().setUp()
        dp = _load_dataset_paths_module()
        original_root = dp.dataset_root
        dp.dataset_root = lambda: self.tmpdir
        self.addCleanup(setattr, dp, 'dataset_root', original_root)
        comm_cls = self.communicator_mod.Communicator
        self.comm = comm_cls.__new__(comm_cls)
        self.comm.node = _FakeNode()
        self.comm.data_editor = _FakeLegacyEditor()
        self.comm._edit_lock = threading.Lock()
        self.comm._edit_use_subprocess = False
        self.comm._edit_timeout_s = 3600
        _FakeLegacyEditor.calls = []

    def _call(self, request):
        return self.communicator_mod.Communicator.dataset_edit_callback(self.comm, request, _EditResponse())

    def test_with_a_daten_service_the_edit_is_its_job(self):
        calls = []

        class _Daten:
            def run_edit_blocking(self, payload):
                calls.append(payload)
                return {'success': False, 'message': 'Deutsch', 'code': 'busy_record'}
        self.comm.node.daten = _Daten()
        try:
            resp = self._call(_EditRequest(self.EditDataset.Request.DELETE, delete_path='/x/y', episodes=[2]))
        finally:
            del self.comm.node.daten
        self.assertEqual((resp.success, resp.message), (False, 'Deutsch'))
        self.assertEqual(calls, [{'mode': 'delete', 'merge_dataset_list': [], 'delete_dataset_path': '/x/y',
                                  'delete_episode_num': [2], 'output_path': ''}])
        self.assertEqual(ENGINE.assemble_calls, [])

    def test_v3_delete_routes_to_the_engine(self):
        src = _make_v3_tree(self.tmpdir / 'student' / 'ds', 3)
        resp = self._call(_EditRequest(self.EditDataset.Request.DELETE, delete_path=str(src), episodes=[1]))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(len(ENGINE.assemble_calls), 1, 'the v3 path is used')
        self.assertEqual(_FakeLegacyEditor.calls, [])

    def test_v21_delete_routes_to_legacy_path(self):
        src = self.tmpdir / 'student' / 'ds21'
        _make_v21_tree(src, 3)
        resp = self._call(_EditRequest(self.EditDataset.Request.DELETE, delete_path=str(src), episodes=[1]))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(ENGINE.assemble_calls, [])
        self.assertEqual(_FakeLegacyEditor.calls, [('delete_one', str(src), 1)])

    def test_empty_delete_selection_is_german(self):
        src = _make_v3_tree(self.tmpdir / 'student' / 'ds', 3)
        resp = self._call(_EditRequest(self.EditDataset.Request.DELETE, delete_path=str(src), episodes=[]))
        self.assertFalse(resp.success)
        self.assertIn('Keine Episoden', resp.message)

    def test_mixed_version_merge_rejected_german(self):
        a = _make_v3_tree(self.tmpdir / 'student' / 'a3', 2)
        b = self.tmpdir / 'student' / 'b21'
        _make_v21_tree(b, 2)
        resp = self._call(_EditRequest(self.EditDataset.Request.MERGE, merge_list=[str(a), str(b)],
                                       output_path=str(self.tmpdir / 'out')))
        self.assertFalse(resp.success)
        self.assertIn('unterschiedliche', resp.message)
        self.assertEqual(ENGINE.assemble_calls, [])
        self.assertEqual(_FakeLegacyEditor.calls, [])

    def test_v3_merge_routes_to_the_engine_and_v21_merge_to_legacy(self):
        a = _make_v3_tree(self.tmpdir / 'student' / 'a', 2)
        b = _make_v3_tree(self.tmpdir / 'student' / 'b', 1)
        resp = self._call(_EditRequest(self.EditDataset.Request.MERGE, merge_list=[str(a), str(b)],
                                       output_path=str(self.tmpdir / 'student' / 'merged')))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(len(ENGINE.assemble_calls), 1)
        self.assertEqual(_FakeLegacyEditor.calls, [])
        c, d = self.tmpdir / 'student' / 'c21', self.tmpdir / 'student' / 'd21'
        _make_v21_tree(c, 1)
        _make_v21_tree(d, 1)
        resp = self._call(_EditRequest(self.EditDataset.Request.MERGE, merge_list=[str(c), str(d)],
                                       output_path=str(self.tmpdir / 'out21')))
        self.assertTrue(resp.success, resp.message)
        self.assertEqual(len(ENGINE.assemble_calls), 1, 'no new v3 merge call')
        self.assertEqual(_FakeLegacyEditor.calls, [('merge', [str(c), str(d)], str(self.tmpdir / 'out21'))])

    def test_dataediterror_maps_to_german_response(self):
        src = _make_v3_tree(self.tmpdir / 'student' / 'ds', 3)
        resp = self._call(_EditRequest(self.EditDataset.Request.DELETE, delete_path=str(src), episodes=[0, 1, 2]))
        self.assertFalse(resp.success)
        self.assertIn('Alle Episoden', resp.message)
        self.assertNotIn('Error:', resp.message)

    def test_v3_corrupt_info_delete_routes_to_v3_not_legacy(self):
        src = self.tmpdir / 'student' / 'ds'
        _make_corrupt_info_tree(src, 3)
        before = (src / 'meta' / 'info.json').read_text()
        for episodes in ([1], [1, 2]):
            _FakeLegacyEditor.calls = []
            resp = self._call(_EditRequest(self.EditDataset.Request.DELETE, delete_path=str(src),
                                           episodes=episodes))
            self.assertFalse(resp.success)
            self.assertIn('beschädigt', resp.message)
            self.assertNotIn('Error:', resp.message)
            self.assertEqual(_FakeLegacyEditor.calls, [])
            self.assertEqual(ENGINE.assemble_calls, [])
        self.assertEqual((src / 'meta' / 'info.json').read_text(), before)

    def test_an_unexpected_error_answers_german_never_the_exception(self):
        src = _make_v3_tree(self.tmpdir / 'student' / 'ds', 3)
        with mock.patch.object(self.worker, 'run_edit', side_effect=ValueError('secret detail')):
            resp = self._call(_EditRequest(self.EditDataset.Request.DELETE, delete_path=str(src), episodes=[1]))
        self.assertFalse(resp.success)
        self.assertEqual(resp.message, T.RUN_EDIT_FAILED_DE)


if __name__ == '__main__':
    unittest.main()
