"""Daten 2.0 — the guarded single-commit upload and the hub-side transactions
(spec §E2, §E3, §C2; P16, P17, P28, P32).

Against the Appendix K fake hub IN-PROCESS: huggingface_hub 1.23's own
``create_commit`` — its no-op filter and its ``parent_commit`` plumbing — runs
unchanged on a patched transport whose server rules are the real hub's (a stale
parent is refused 412, LFS additions must be pre-uploaded, folders carry
content-addressed tree ids and their last commit). The fake hub's source is the
two constants at the bottom of this file (spec Appendix K, verbatim).

What is proven: ONE ``create_commit`` per upload with ``parent_commit`` = the
head read before, the orphan deletes in that commit, the marker FIRST in its
title; a stale parent → ``hub_changed`` and nothing else written; an UNCHANGED
re-upload overlapping another PC's upload (both placements) is refused — the
no-op return is never trusted (G-1) — the record keeps its head and the other
upload's episodes survive; a README-only commit is no change (S-a); a lost
response is recognised by the marker; an error with an unmoved head is the
library's own; an unconfirmed commit records only ``tag_ok: false``; the
permission matrix (expected sha / None / UNSET); the exact check; the tag rule
(H-6); the local gate on every path (P28); records only for the folder's own
repo (H-7); ``swap_in``/``recover`` in all four break states (H-8) and the
crash marker removed by a swap (H-1); ``keep`` of a partner's dataset refused
before anything is fetched (H-3); a corrupted transfer refused ``broken``.

The engine (``v3_surgery``) and LeRobot are stubbed (integrity and the
download's load check have their own tests); huggingface_hub is the real
1.23.0 (CI's python-tests installs it, spec §H8) — imported fresh inside an
isolated ``sys.modules`` for each class, because this directory's other loaders
stub it. Skipped where huggingface_hub is not installed.
"""

import ast
import contextlib
import importlib
import importlib.util
import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DP = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'data_processing'
HUB_SYNC_PATH = DP / 'hub_sync.py'
SYNC_PATH = DP / 'dataset_sync.py'
LENA_TOKEN = 'hf_harnesslenaAAAAAAAAAAAAAAAAAA'
TOKENS = {LENA_TOKEN: {'name': 'lena-schmidt', 'orgs': []},
          'hf_harnessmaxBBBBBBBBBBBBBBBBBBBB': {'name': 'max-weber', 'orgs': []}}
REPO = 'lena-schmidt/omx_f_wuerfel'


def _hub_available():
    """Installed on sys.path — asked of the path finder, never of sys.modules,
    which this directory's other loaders fill with stubs."""
    from importlib.machinery import PathFinder
    return all(PathFinder.find_spec(name) is not None for name in ('huggingface_hub', 'httpx'))


def write_fakehub(directory):
    """Write the fake hub's two modules into ``directory`` (a PYTHONPATH entry:
    ``sitecustomize`` patches every Python process started with FAKEHUB_ROOT)."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'fakehub_store.py').write_text(FAKEHUB_STORE_SRC)
    (directory / 'sitecustomize.py').write_text(FAKEHUB_SITE_SRC)
    return directory


def install_fakehub(directory, hub_root):
    """Import the fake hub into THIS process (the caller isolates sys.modules)."""
    os.environ['FAKEHUB_ROOT'] = str(hub_root)
    for name in [n for n in sys.modules if n == 'huggingface_hub' or n.startswith('huggingface_hub.')]:
        del sys.modules[name]
    sys.modules.pop('fakehub_store', None)
    sys.path.insert(0, str(directory))
    try:
        store = importlib.import_module('fakehub_store')
        spec = importlib.util.spec_from_file_location('_fakehub_site', str(pathlib.Path(directory) / 'sitecustomize.py'))
        site = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(site)
    finally:
        sys.path.remove(str(directory))
    return store


# ── fixtures ─────────────────────────────────────────────────────────────────

INFO = json.dumps({'codebase_version': 'v3.0', 'robot_type': 'omx_f', 'fps': 30, 'total_episodes': 4})
BASE = {
    'data/chunk-000/file-000.parquet': b'data-session-1' * 40,
    'meta/episodes/chunk-000/file-000.parquet': b'episodes-session-1' * 20,
    'videos/observation.images.scene/chunk-000/file-000.mp4': b'\x00video-1' * 200,
    'meta/info.json': INFO.encode(),
    'meta/stats.json': b'{"s": 1}',
    'meta/tasks.parquet': b'tasks-1',
}
SESSION2 = {
    'data/chunk-000/file-001.parquet': b'data-session-2' * 40,
    'meta/episodes/chunk-000/file-001.parquet': b'episodes-session-2' * 20,
    'videos/observation.images.scene/chunk-000/file-001.mp4': b'\x00video-2' * 200,
    'meta/info.json': INFO.replace('4', '7').encode(),
    'meta/stats.json': b'{"s": 2}',
}
OTHER2 = {
    'data/chunk-000/file-001.parquet': b'data-other-2' * 40,
    'meta/episodes/chunk-000/file-001.parquet': b'episodes-other-2' * 20,
    'videos/observation.images.scene/chunk-000/file-001.mp4': b'\x00video-X' * 200,
    'meta/info.json': INFO.replace('4', '6').encode(),
    'meta/stats.json': b'{"s": 3}',
}


def write_tree(root, *layers):
    root = pathlib.Path(root)
    for layer in layers:
        for p, data in layer.items():
            f = root / p
            f.parent.mkdir(parents=True, exist_ok=True)
            if data is None:
                f.unlink(missing_ok=True)
            else:
                f.write_bytes(data)
    return root


class _SurgeryError(RuntimeError):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code
        self.detail = detail


def _stub_engine():
    """v3_surgery: integrity fails on a BROKEN file; Source on meta/UNSUPPORTED."""
    eng = types.ModuleType('_stub_v3_surgery')
    eng.SurgeryError = _SurgeryError

    def integrity(root, known_good=frozenset()):
        if (pathlib.Path(root) / 'BROKEN').exists():
            raise _SurgeryError('broken', 'stub')

    def source(root):
        if (pathlib.Path(root) / 'meta' / 'UNSUPPORTED').exists():
            raise _SurgeryError('unsupported', 'stub')
        return types.SimpleNamespace(root=root)
    eng.integrity = integrity
    eng.Source = source
    return eng


def _stub_lerobot():
    mods = {n: types.ModuleType(n) for n in ('lerobot', 'lerobot.datasets', 'lerobot.datasets.lerobot_dataset')}

    def loader(repo_id, root=None, **kw):
        if (pathlib.Path(root) / 'meta' / 'NOLOAD').exists():
            raise RuntimeError('does not load')
    mods['lerobot.datasets.lerobot_dataset'].LeRobotDataset = loader
    return mods


class HubCase(unittest.TestCase):
    """A fresh fake hub per test; huggingface_hub, the fake and hub_sync live in
    an isolated sys.modules for the class (restored afterwards)."""

    @classmethod
    def setUpClass(cls):
        if not _hub_available():
            raise unittest.SkipTest('huggingface_hub is not installed (CI installs it, spec §H8)')
        cls._modules = mock.patch.dict(sys.modules)
        cls._modules.start()
        cls._env = mock.patch.dict(os.environ)
        cls._env.start()
        cls.base = pathlib.Path(tempfile.mkdtemp(prefix='d2_hubsync_'))
        cls.hub_root = cls.base / 'hub'
        cls.hub_root.mkdir()
        os.environ['HF_TOKEN_PATH'] = str(cls.base / 'token')
        os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
        os.environ.pop('HF_TOKEN', None)
        (cls.base / 'token').write_text(LENA_TOKEN)
        cls.FS = install_fakehub(write_fakehub(cls.base / 'fakehub'), cls.hub_root)
        sys.modules['_edubotics_dp_v3_surgery'] = _stub_engine()
        sys.modules['physical_ai_server.data_processing.v3_surgery'] = sys.modules['_edubotics_dp_v3_surgery']
        sys.modules.update(_stub_lerobot())
        spec = importlib.util.spec_from_file_location('_hub_sync_under_test', str(HUB_SYNC_PATH))
        cls.H = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.H)
        cls.S = cls.H.S
        import huggingface_hub
        cls.hf = huggingface_hub
        # huggingface_hub logs a warning for every no-op commit (an unchanged
        # re-upload is one): keep the test output readable.
        cls._hf_logger = logging.getLogger('huggingface_hub')
        cls._hf_level = cls._hf_logger.level
        cls._hf_logger.setLevel(logging.ERROR)

    @classmethod
    def tearDownClass(cls):
        cls._hf_logger.setLevel(cls._hf_level)
        cls._env.stop()
        cls._modules.stop()
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        for d in ('repos', 'blobs', 'cache'):
            shutil.rmtree(self.hub_root / d, ignore_errors=True)
        for f in ('audit.log', 'faults.json'):
            (self.hub_root / f).unlink(missing_ok=True)
        (self.hub_root / 'tokens.json').write_text(json.dumps(TOKENS))
        (self.base / 'token').write_text(LENA_TOKEN)
        self.ds_root = self.base / 'ds'
        shutil.rmtree(self.ds_root, ignore_errors=True)
        self.root = self.ds_root / 'lena-schmidt' / 'omx_f_wuerfel'
        self.other = self.base / 'other'
        shutil.rmtree(self.other, ignore_errors=True)
        self.api = self.hf.HfApi()

    # -- helpers ------------------------------------------------------------------

    def audit(self, op=None):
        p = self.hub_root / 'audit.log'
        lines = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        return [x for x in lines if op is None or x['op'] == op]

    def main(self, repo=REPO):
        return self.FS.load_refs(repo)['main']

    def tag(self, repo=REPO):
        return self.FS.load_refs(repo).get('tags', {}).get('v3.0')

    def hub_tree(self, repo=REPO, sha=None):
        return self.FS.commit(repo, sha or self.main(repo))['tree']

    def other_pc(self, *layers, title='another PC', tag='v3.0', repo=REPO):
        """Another PC's upload: the hub's current data plus its own session."""
        src = self.other / repo
        shutil.rmtree(src, ignore_errors=True)
        write_tree(src, *layers)
        return self.FS.put_tree(repo, src, private=False, title=title, tag=tag)

    def first_upload(self):
        write_tree(self.root, BASE)
        return self.H.upload(self.root, REPO, expected=None, private=False, api=self.api)

    def faults(self, *faults):
        (self.hub_root / 'faults.json').write_text(json.dumps(list(faults)))


class OneGuardedCommit(HubCase):

    def test_one_commit_with_the_parent_the_marker_first_and_the_orphans_inside(self):
        r = self.first_upload()
        self.assertEqual((r['tag'], r['tag_ok'], r['unconfirmed'], r['private']), ('ok', True, False, False))
        commits = self.audit('create_commit')
        self.assertEqual(len(commits), 1)
        created = self.FS.commit(REPO, commits[0]['sha'])['parent']
        self.assertEqual(commits[0]['parent'], created, 'the parent is the head read before')
        digest = self.S.meta_digest(self.root)
        self.assertTrue(commits[0]['title'].startswith(f'[edubotics:{digest[:16]}] '))
        self.assertEqual(self.tag(), commits[0]['sha'])
        rec = self.S.read_record(self.root)
        self.assertEqual(rec['hub_sha'], commits[0]['sha'])
        self.assertEqual(rec['local_digest'], digest)
        self.assertEqual(set(rec['files']), {p for p in BASE if p.startswith(('data/', 'meta/episodes/', 'videos/'))})
        # a second upload after deleting a session file: the orphan delete rides the SAME commit
        write_tree(self.root, SESSION2)
        self.H.upload(self.root, REPO, api=self.api)
        (self.root / 'data/chunk-000/file-001.parquet').unlink()
        (self.root / 'meta/episodes/chunk-000/file-001.parquet').unlink()
        (self.root / 'videos/observation.images.scene/chunk-000/file-001.mp4').unlink()
        (self.root / 'meta/stats.json').write_bytes(b'{"s": 9}')
        # (the engine's delete rewrites the record's files with its output's hashes)
        self.S.update_record(self.root, REPO, files=self.S.files_manifest(self.root))
        n = len(self.audit('create_commit'))
        self.H.upload(self.root, REPO, api=self.api)
        commits = self.audit('create_commit')
        self.assertEqual(len(commits), n + 1)
        tree = self.hub_tree()
        self.assertNotIn('data/chunk-000/file-001.parquet', tree)
        self.assertNotIn('videos/observation.images.scene/chunk-000/file-001.mp4', tree)
        self.assertIn('.gitattributes', tree, 'hub-managed files are never deleted')

    def test_the_progress_is_per_file(self):
        write_tree(self.root, BASE)
        seen = []
        self.H.upload(self.root, REPO, expected=None, api=self.api, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual([d for d, _ in seen], list(range(1, len(seen) + 1)))

    def test_the_card_is_written_with_the_real_visibility(self):
        write_tree(self.root, BASE)
        cards = []

        def card(root, repo, private):
            cards.append(private)
            (pathlib.Path(root) / 'README.md').write_text(f'card private={private}')
        self.H.upload(self.root, REPO, expected=None, private=True, api=self.api, write_card=card)
        self.assertEqual(cards, [True])
        self.assertIn('README.md', self.hub_tree())
        # a later upload asking for PUBLIC does not change an existing private repo
        write_tree(self.root, SESSION2)
        r = self.H.upload(self.root, REPO, private=False, api=self.api, write_card=card)
        self.assertEqual((cards[-1], r['private']), (True, True))

    def test_a_stale_parent_is_refused_and_nothing_else_is_written(self):
        self.first_upload()
        write_tree(self.root, SESSION2)
        head = self.main()

        class Racing(type(self.api)):
            def preupload_lfs_files(api_self, *a, **k):
                super().preupload_lfs_files(*a, **k)
                if self.main() == head:
                    self.other_pc(BASE, OTHER2)      # another PC commits right before ours
        with self.assertRaises(self.H.Refused) as e:
            self.H.upload(self.root, REPO, api=Racing())
        self.assertEqual(e.exception.code, 'hub_changed')
        self.assertTrue(self.audit('commit_refused_stale_parent') or self.main() != head)
        self.assertEqual(self.S.read_record(self.root)['hub_sha'], head)


class NeverTrustTheNoOpReturn(HubCase):
    """G-1 / P16: create_commit's no-op path returns main's CURRENT head without
    any parent check when every addition equals main at preupload time."""

    def _overlap(self, during_video):
        self.first_upload()
        head = self.main()
        rec_before = self.S.read_record(self.root)
        done = {'n': 0}

        class Racing(type(self.api)):
            def preupload_lfs_files(api_self, repo_id, additions, **k):
                additions = list(additions)
                is_video = any(a.path_in_repo.endswith('.mp4') for a in additions)
                if during_video and is_video and not done['n']:
                    done['n'] = 1
                    self.other_pc(BASE, SESSION2)       # during OUR video preupload
                super().preupload_lfs_files(repo_id, additions, **k)

            def create_commit(api_self, *a, **k):
                if not during_video and not done['n']:
                    done['n'] = 1
                    self.other_pc(BASE, SESSION2)       # between our preupload and our commit
                return super().create_commit(*a, **k)
        with self.assertRaises(self.H.Refused) as e:
            self.H.upload(self.root, REPO, api=Racing())       # UNCHANGED re-upload
        self.assertEqual(e.exception.code, 'hub_changed')
        self.assertEqual(self.S.read_record(self.root), rec_before, 'the record keeps its head')
        self.assertNotEqual(self.main(), head)
        ours = [c for c in self.audit('create_commit') if c['title'].startswith('[edubotics:')]
        self.assertEqual(len(ours), 1, 'no commit of ours beyond the first upload')
        # the next decision is newer, and the next upload cannot drop the other PC's session
        self.assertEqual(self.S.decide(self.root, self.S.read_record(self.root),
                                       self.H.hub_view(self.api, REPO))[0], 'newer')
        with self.assertRaises(self.H.Refused) as e2:
            self.H.upload(self.root, REPO, api=self.api)
        self.assertEqual(e2.exception.code, 'hub_differs')
        self.assertIn('data/chunk-000/file-001.parquet', self.hub_tree())

    def test_overlap_between_preupload_and_commit(self):
        self._overlap(during_video=False)

    def test_overlap_during_the_video_preupload(self):
        self._overlap(during_video=True)

    def test_a_readme_only_commit_is_accepted(self):
        self.first_upload()
        self.FS.edit_readme(REPO, '# edited on the website')
        r = self.H.upload(self.root, REPO, api=self.api)      # unchanged re-upload after a card edit
        self.assertTrue(r['tag_ok'])
        self.assertEqual(self.tag(), self.main())

    def test_a_legacy_dataset_with_no_marker_anywhere(self):
        write_tree(self.root, BASE)
        self.FS.put_tree(REPO, self.root, private=False, title='Add files using upload-large-folder tool')
        self.FS.edit_readme(REPO, '# card')
        r = self.H.upload(self.root, REPO, api=self.api)      # record-less, equal content
        self.assertTrue(r['tag_ok'])
        self.assertEqual(self.S.read_record(self.root)['hub_sha'], self.main())


class FailuresAreClassifiedByTheHub(HubCase):

    def test_a_lost_response_after_the_commit_landed_is_success(self):
        write_tree(self.root, BASE)
        self.faults({'op': 'create_commit', 'kind': 'lost_response', 'times': 1})
        r = self.H.upload(self.root, REPO, expected=None, api=self.api)
        self.assertTrue(r['tag_ok'])
        self.assertEqual(self.S.read_record(self.root)['hub_sha'], self.main())
        self.assertEqual(self.tag(), self.main())

    def test_an_error_with_the_head_unmoved_is_the_librarys_and_records_nothing(self):
        self.first_upload()
        rec = self.S.read_record(self.root)
        write_tree(self.root, SESSION2)
        self.faults({'op': 'create_commit', 'kind': '500', 'times': 1})
        with self.assertRaises(self.hf.errors.HfHubHTTPError):
            self.H.upload(self.root, REPO, api=self.api)
        self.assertEqual(self.S.read_record(self.root), rec)

    def test_a_failed_read_back_after_a_returned_commit_is_unconfirmed(self):
        write_tree(self.root, BASE)
        state = {'committed': False, 'tags': 0}

        class Blind(type(self.api)):
            def create_commit(api_self, *a, **k):
                out = super().create_commit(*a, **k)
                state['committed'] = True
                return out

            def list_repo_tree(api_self, *a, **k):
                if state['committed']:
                    raise RuntimeError('read-back fails')
                return super().list_repo_tree(*a, **k)

            def create_tag(api_self, *a, **k):
                state['tags'] += 1
                return super().create_tag(*a, **k)
        r = self.H.upload(self.root, REPO, expected=None, api=Blind())
        self.assertEqual((r['unconfirmed'], r['tag_ok'], r['tag']), (True, False, 'unconfirmed'))
        self.assertEqual(state['tags'], 0, 'no tag call')
        self.assertEqual(self.S.read_record(self.root), {'v': 1, 'repo_id': REPO, 'tag_ok': False})
        # the next upload (a no-op) settles the record and moves the tag
        r2 = self.H.upload(self.root, REPO, api=self.api)
        self.assertTrue(r2['tag_ok'])
        self.assertEqual(self.tag(), self.main())
        self.assertNotIn('tag_ok', self.S.read_record(self.root))


class ThePermission(HubCase):

    def test_expected_sha(self):
        self.first_upload()
        head = self.main()
        write_tree(self.root, SESSION2)
        self.H.upload(self.root, REPO, expected=head, api=self.api)            # head == expected
        self.FS.edit_readme(REPO, 'card')                                       # only a card commit since
        rec = self.S.read_record(self.root)
        write_tree(self.root, {'meta/stats.json': b'{"s": 8}'})
        self.H.upload(self.root, REPO, expected=rec['hub_sha'], api=self.api)
        self.other_pc(BASE, OTHER2)
        write_tree(self.root, {'meta/stats.json': b'{"s": 7}'})
        with self.assertRaises(self.H.Refused) as e:
            self.H.upload(self.root, REPO, expected=self.S.read_record(self.root)['hub_sha'], api=self.api)
        self.assertEqual(e.exception.code, 'hub_changed')

    def test_expected_none_means_no_dataset_online(self):
        self.first_upload()
        with self.assertRaises(self.H.Refused) as e:
            self.H.upload(self.root, REPO, expected=None, api=self.api)
        self.assertEqual(e.exception.code, 'hub_changed')

    def test_unset_decides_now(self):
        self.first_upload()
        write_tree(self.root, SESSION2)
        self.H.upload(self.root, REPO, api=self.api)                            # changed -> allowed
        self.other_pc(BASE, SESSION2, OTHER2)                                   # the hub moved on
        with self.assertRaises(self.H.Refused) as e:
            self.H.upload(self.root, REPO, api=self.api)                        # newer -> refused
        self.assertEqual(e.exception.code, 'hub_differs')
        write_tree(self.root, {'meta/stats.json': b'{"s": 99}'})
        with self.assertRaises(self.H.Refused) as e:
            self.H.upload(self.root, REPO, api=self.api)                        # conflict -> refused
        self.assertEqual(e.exception.code, 'hub_differs')

    def test_the_exact_check_refuses_a_same_size_different_video(self):
        write_tree(self.root, BASE)
        self.FS.put_tree(REPO, self.root, private=False, title='legacy')
        vid = 'videos/observation.images.scene/chunk-000/file-000.mp4'
        b = bytearray(BASE[vid])
        b[100] ^= 0xFF
        write_tree(self.root, {vid: bytes(b), 'meta/stats.json': b'{"s": 5}'})
        head = self.main()
        with self.assertRaises(self.H.Refused) as e:
            self.H.upload(self.root, REPO, api=self.api)
        self.assertEqual(e.exception.code, 'hub_differs')
        self.assertEqual(self.main(), head)
        self.assertEqual(self.audit('create_commit'), [])

    def test_the_namespace_is_the_tokens_account(self):
        write_tree(self.root, BASE)
        with self.assertRaises(self.H.Refused) as e:
            self.H.upload(self.root, 'schule-org/omx_f_wuerfel', expected=None, api=self.api)
        self.assertEqual(e.exception.code, 'namespace')
        self.assertEqual(self.audit(), [])


class TheTrainingPointer(HubCase):

    def test_left_alone_when_main_moved_to_other_data_before_the_tag(self):
        write_tree(self.root, BASE)

        class Racing(type(self.api)):
            def create_commit(api_self, *a, **k):
                out = super().create_commit(*a, **k)
                self.other_pc(BASE, OTHER2)                  # a newer upload right after ours
                return out
        r = self.H.upload(self.root, REPO, expected=None, api=Racing())
        self.assertEqual(r['tag'], 'newer')
        self.assertEqual(self.tag(), self.main(), 'the newer upload\'s own step owns the tag')
        rec = self.S.read_record(self.root)
        self.assertNotIn('tag_ok', rec)
        self.assertEqual(self.S.decide(self.root, rec, self.H.hub_view(self.api, REPO))[0], 'newer')

    def _race_in(self, method, fail_reread=False):
        self.first_upload()
        write_tree(self.root, SESSION2)
        state = {'raced': False, 'failed': False}

        class Racing(type(self.api)):
            def __getattribute__(api_self, name):
                return object.__getattribute__(api_self, name)

        def racer(original):
            def run(*a, **k):
                if not state['raced']:
                    state['raced'] = True
                    self.other_pc(BASE, SESSION2, OTHER2, title='[edubotics:0000000000000000] other PC')
                return original(*a, **k)
            return run
        api = Racing()
        setattr(api, method, racer(getattr(api, method)))
        if fail_reread:
            original_refs = api.list_repo_refs
            calls = {'n': 0}

            def refs(*a, **k):
                calls['n'] += 1
                if state['raced'] and not state['failed']:
                    state['failed'] = True
                    raise RuntimeError('re-read fails once')
                return original_refs(*a, **k)
            api.list_repo_refs = refs
        self.H.upload(self.root, REPO, api=api)
        self.assertTrue(state['raced'])
        self.assertEqual(self.tag(), self.main(), f'v3.0 ends at main\'s head ({method})')

    def test_another_upload_tagging_inside_our_delete_tag(self):
        self._race_in('delete_tag')

    def test_another_upload_tagging_inside_our_create_tag(self):
        self._race_in('create_tag')

    def test_a_race_inside_delete_tag_and_one_failed_reread_still_end_at_main(self):
        self._race_in('delete_tag', fail_reread=True)

    def test_three_failures_are_tag_ok_false_and_the_next_upload_moves_it(self):
        write_tree(self.root, BASE)
        self.faults({'op': 'create_tag', 'kind': '500', 'times': 3})
        r = self.H.upload(self.root, REPO, expected=None, api=self.api)
        self.assertEqual((r['tag'], r['tag_ok']), ('failed', False))
        self.assertIsNone(self.tag())
        rec = self.S.read_record(self.root)
        self.assertIs(rec['tag_ok'], False)
        self.assertEqual(self.S.decide(self.root, rec, self.H.hub_view(self.api, REPO))[0], 'changed')
        n = len(self.audit('create_commit'))
        r2 = self.H.upload(self.root, REPO, api=self.api)                     # a no-op commit, tag moved
        self.assertEqual(len(self.audit('create_commit')), n)
        self.assertTrue(r2['tag_ok'])
        self.assertEqual(self.tag(), self.main())


class TheLocalGate(HubCase):
    """P28: on the expected-sha, None and UNSET paths alike."""

    def _paths(self):
        head = self.main() if (self.hub_root / 'repos' / REPO).exists() else None
        return (('expected', head), ('none', None), ('unset', self.H.UNSET))

    def test_a_crash_marker_refuses_before_any_network_call(self):
        write_tree(self.root, BASE)
        self.S.session_marker_path(self.root).write_text('{}')
        for name, expected in self._paths():
            with self.subTest(path=name):
                with self.assertRaises(self.H.Refused) as e:
                    self.H.upload(self.root, REPO, expected=expected, api=self.api)
                self.assertEqual(e.exception.code, 'in_session')
        self.assertEqual(self.audit(), [], 'zero writes')

    def test_a_record_vouched_file_changed_is_local_broken(self):
        self.first_upload()
        vid = self.root / 'videos/observation.images.scene/chunk-000/file-000.mp4'
        b = bytearray(vid.read_bytes())
        b[10] ^= 1
        vid.write_bytes(bytes(b))
        write_tree(self.root, {'meta/stats.json': b'{"s": 4}'})
        head = self.main()
        n = len(self.audit())
        for name, expected in self._paths():
            with self.subTest(path=name):
                with self.assertRaises(self.H.Refused) as e:
                    self.H.upload(self.root, REPO, expected=expected, api=self.api)
                self.assertIn(e.exception.code, ('local_broken', 'hub_changed'))
                if name != 'none':
                    self.assertEqual(e.exception.code, 'local_broken')
        self.assertEqual(self.main(), head)
        self.assertEqual([x for x in self.audit()[n:] if x['op'] in ('create_commit', 'preupload_lfs_files')], [])

    def test_a_dataset_that_does_not_load_is_local_broken(self):
        self.first_upload()
        write_tree(self.root, {'BROKEN': b'1', 'meta/stats.json': b'{"s": 6}'})
        for name, expected in self._paths():
            with self.subTest(path=name):
                with self.assertRaises(self.H.Refused) as e:
                    self.H.upload(self.root, REPO, expected=expected, api=self.api)
                if name != 'none':
                    self.assertEqual(e.exception.code, 'local_broken')
        self.assertEqual(len(self.audit('create_commit')), 1)


class RecordsBelongToTheirOwnDataset(HubCase):

    def test_folder_x_uploaded_as_repo_y_writes_no_record_for_x(self):
        write_tree(self.root, BASE)
        self.H.upload(self.root, 'lena-schmidt/omx_f_anders', expected=None, api=self.api)
        self.assertFalse(self.S.record_path(self.root).exists())
        r = self.H.upload(self.root, REPO, expected=None, api=self.api)
        self.assertEqual(self.S.read_record(self.root)['hub_sha'], r['commit'])

    def test_a_record_of_another_repo_is_ignored_and_replaced(self):
        write_tree(self.root, BASE)
        self.S.write_record(self.root, {'v': 1, 'repo_id': 'lena-schmidt/omx_f_anders', 'hub_sha': 'f' * 40})
        r = self.H.upload(self.root, REPO, expected=None, api=self.api)
        rec = self.S.read_record(self.root)
        self.assertEqual((rec['repo_id'], rec['hub_sha']), (REPO, r['commit']))


class SwapAndRecover(HubCase):
    """H-8 / P32: recover() decides from the rename state alone."""

    def _swap_setup(self):
        target = write_tree(self.root, BASE, {'OLD': b'old'})
        self.S.write_record(target, {'v': 1, 'repo_id': REPO, 'hub_sha': 'a' * 40})
        self.S.session_marker_path(target).write_text('{}')
        tmp = write_tree(pathlib.Path(f'{target}.tmp_sync'), BASE, {'NEW': b'new'})
        new_rec = {'v': 1, 'repo_id': REPO, 'hub_sha': 'b' * 40}
        nxt = self.S.record_next_path(target)
        self.S.write_json_atomic(nxt, {'record': new_rec, 'tmp': '.tmp_sync', 'bak': '.bak_sync'})
        return target, tmp, nxt

    def test_a_complete_swap_removes_the_crash_marker(self):
        target = write_tree(self.root, BASE, {'OLD': b'old'})
        self.S.session_marker_path(target).write_text('{}')
        tmp = write_tree(pathlib.Path(f'{target}.tmp_sync'), BASE, {'NEW': b'new'})
        self.H.swap_in(tmp, target, {'v': 1, 'repo_id': REPO, 'hub_sha': 'b' * 40}, '.tmp_sync', '.bak_sync')
        self.assertTrue((target / 'NEW').exists())
        self.assertFalse(self.S.session_marker_path(target).exists())
        self.assertEqual(self.S.read_record(target)['hub_sha'], 'b' * 40)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()),
                         ['omx_f_wuerfel', 'omx_f_wuerfel.sync.json'])

    def test_before_the_first_rename_nothing_changed(self):
        target, tmp, nxt = self._swap_setup()
        self.H.recover(target)
        self.assertTrue((target / 'OLD').exists())
        self.assertEqual(self.S.read_record(target)['hub_sha'], 'a' * 40)
        self.assertTrue(self.S.session_marker_path(target).exists())
        self.assertFalse(nxt.exists())
        self.assertFalse(tmp.exists())

    def test_between_the_renames_the_old_copy_comes_back(self):
        target, tmp, nxt = self._swap_setup()
        target.rename(pathlib.Path(f'{target}.bak_sync'))
        self.H.recover(target)
        self.assertTrue((target / 'OLD').exists())
        self.assertEqual(self.S.read_record(target)['hub_sha'], 'a' * 40)
        self.assertTrue(self.S.session_marker_path(target).exists())
        self.assertFalse(nxt.exists())
        self.assertFalse(pathlib.Path(f'{target}.bak_sync').exists())
        self.assertFalse(tmp.exists())

    def test_after_the_second_rename_the_new_record_is_promoted(self):
        target, tmp, nxt = self._swap_setup()
        bak = pathlib.Path(f'{target}.bak_sync')
        target.rename(bak)
        tmp.rename(target)
        self.H.recover(target)
        self.assertTrue((target / 'NEW').exists())
        self.assertEqual(self.S.read_record(target)['hub_sha'], 'b' * 40)
        self.assertFalse(self.S.session_marker_path(target).exists())
        self.assertFalse(nxt.exists())
        self.assertFalse(bak.exists())

    def test_a_new_target_is_promoted(self):
        tmp = write_tree(pathlib.Path(f'{self.root}.tmp_sync'), BASE, {'NEW': b'new'})
        self.S.write_json_atomic(self.S.record_next_path(self.root),
                                 {'record': {'v': 1, 'repo_id': REPO, 'hub_sha': 'c' * 40},
                                  'tmp': '.tmp_sync', 'bak': '.bak_sync'})
        tmp.rename(self.root)
        self.H.recover(self.root)
        self.assertEqual(self.S.read_record(self.root)['hub_sha'], 'c' * 40)
        self.assertFalse(self.S.record_next_path(self.root).exists())


class TheOneDownload(HubCase):

    def _hub_dataset(self, repo=REPO, private=False):
        src = write_tree(self.other / 'seed' / repo, BASE)
        return self.FS.put_tree(repo, src, private=private, title='seed')

    def test_a_replace_swaps_in_with_the_record_and_files(self):
        head = self._hub_dataset()
        write_tree(self.root, {'meta/info.json': INFO.encode(), 'OLD': b'1'})
        self.S.session_marker_path(self.root).write_text('{}')
        digest = self.S.meta_digest(self.root)
        r = self.H.download(self.api, REPO, head, self.root, mode='replace', meta_digest=digest,
                            token=LENA_TOKEN, robot_type='omx_f', disk_floor=0)
        self.assertEqual(r['revision'], head)
        self.assertFalse((self.root / 'OLD').exists())
        rec = self.S.read_record(self.root)
        self.assertEqual(rec['hub_sha'], head)
        self.assertEqual(rec['files'], self.S.files_manifest(self.root))
        self.assertFalse(self.S.session_marker_path(self.root).exists(), 'H-1: the swap removes the marker')
        self.assertFalse(pathlib.Path(f'{self.root}.tmp_sync').exists())

    def test_a_stale_replace_and_a_replace_without_digest_fetch_nothing(self):
        head = self._hub_dataset()
        write_tree(self.root, BASE)
        with self.assertRaises(self.H.Refused) as e:
            self.H.download(self.api, REPO, head, self.root, mode='replace', token=LENA_TOKEN, disk_floor=0)
        self.assertEqual(e.exception.code, 'invalid')
        with self.assertRaises(self.H.Refused) as e:
            self.H.download(self.api, REPO, head, self.root, mode='replace', meta_digest='0' * 64,
                            token=LENA_TOKEN, disk_floor=0)
        self.assertEqual(e.exception.code, 'stale')
        self.assertFalse(pathlib.Path(f'{self.root}.tmp_sync').exists())

    def test_one_wrong_byte_is_broken_and_nothing_changes(self):
        head = self._hub_dataset()
        vid = self.hub_tree(sha=head)['videos/observation.images.scene/chunk-000/file-000.mp4']
        blob = self.hub_root / 'blobs' / vid['sha256']
        b = bytearray(blob.read_bytes())
        b[5] ^= 1
        blob.write_bytes(bytes(b))
        for mode in ('new', 'replace'):
            with self.subTest(mode=mode):
                if mode == 'replace':
                    write_tree(self.root, {'meta/info.json': INFO.encode(), 'OLD': b'1'})
                kw = {'meta_digest': self.S.meta_digest(self.root)} if mode == 'replace' else {}
                with self.assertRaises(self.H.Refused) as e:
                    self.H.download(self.api, REPO, head, self.root, mode=mode, token=LENA_TOKEN, disk_floor=0, **kw)
                self.assertEqual(e.exception.code, 'broken')
                self.assertFalse(pathlib.Path(f'{self.root}.tmp_sync').exists())
                self.assertEqual(self.root.exists(), mode == 'replace')
                if mode == 'replace':
                    self.assertTrue((self.root / 'OLD').exists())

    def test_a_copy_remembers_its_source_and_its_visibility(self):
        head = self._hub_dataset(repo='lehrer-mueller/omx_f_demo', private=False)
        target = self.ds_root / 'lena-schmidt' / 'omx_f_kopie'
        self.H.download(self.api, 'lehrer-mueller/omx_f_demo', head, target, mode='copy',
                        display_name='Kopie', token=LENA_TOKEN, disk_floor=0)
        rec = self.S.read_record(target)
        self.assertEqual(rec['source'], {'repo_id': 'lehrer-mueller/omx_f_demo', 'sha': head})
        self.assertEqual((rec['private'], rec['display_name']), (False, 'Kopie'))
        self.assertNotIn('hub_sha', rec)

    def test_keep_of_a_partners_dataset_is_refused_before_anything_is_fetched(self):
        repo = 'max-weber/omx_f_wuerfel'
        head = self._hub_dataset(repo=repo)
        target = write_tree(self.ds_root / 'max-weber' / 'omx_f_wuerfel', BASE)
        with mock.patch.object(self.hf, 'snapshot_download', side_effect=AssertionError('fetched')):
            with self.assertRaises(self.H.Refused) as e:
                self.H.download(self.api, repo, head, target, mode='keep', token=LENA_TOKEN, disk_floor=0)
        self.assertEqual(e.exception.code, 'namespace')
        self.assertFalse(pathlib.Path(f'{target}.tmp_keep').exists())

    def test_keep_runs_the_local_gate_first(self):
        head = self._hub_dataset()
        write_tree(self.root, BASE)
        self.S.session_marker_path(self.root).write_text('{}')
        with mock.patch.object(self.hf, 'snapshot_download', side_effect=AssertionError('fetched')):
            with self.assertRaises(self.H.Refused) as e:
                self.H.download(self.api, REPO, head, self.root, mode='keep', token=LENA_TOKEN, disk_floor=0)
        self.assertEqual(e.exception.code, 'in_session')

    def test_the_disk_rule_names_both_numbers(self):
        head = self._hub_dataset()
        with self.assertRaises(self.H.Refused) as e:
            self.H.download(self.api, REPO, head, self.root, mode='new', token=LENA_TOKEN, disk_floor=10 ** 18)
        self.assertEqual(e.exception.code, 'disk')
        self.assertGreater(e.exception.extra['free'], 0)
        self.assertEqual(e.exception.extra['need'], sum(e['size'] for e in self.hub_tree(sha=head).values()))

    def test_a_missing_repo_is_not_found(self):
        with self.assertRaises(self.H.Refused) as e:
            self.H.download(self.api, 'lena-schmidt/omx_f_gibtsnicht', None, self.root, mode='new',
                            token=LENA_TOKEN, disk_floor=0)
        self.assertEqual(e.exception.code, 'not_found')


class Fences(unittest.TestCase):

    def test_no_large_folder_or_folder_upload_and_no_module_level_hub_import(self):
        tree = ast.parse(HUB_SYNC_PATH.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ('upload_large_folder', 'upload_folder'))
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, ('upload_large_folder', 'upload_folder'))
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn('upload_large_folder', [a.name for a in node.names])
                self.assertNotIn('upload_folder', [a.name for a in node.names])
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or '']
                self.assertFalse(any(n.split('.')[0] == 'huggingface_hub' for n in names),
                                 'huggingface_hub is imported inside functions only (A18)')

    def test_the_constants_are_the_contracts(self):
        spec = importlib.util.spec_from_file_location(
            '_contract_for_hub_sync', str(DP.parent / 'daten' / 'contract.py'))
        c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(c)
        src = HUB_SYNC_PATH.read_text(encoding='utf-8')
        tree = ast.parse(src)
        values = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
        self.assertEqual((values['TAG'], values['TAG_RETRIES'], values['READBACK_TRIES']),
                         (c.TAG, c.TAG_RETRIES, c.READBACK_TRIES))
        sync_src = SYNC_PATH.read_text(encoding='utf-8')
        self.assertIn(f"MARKER_PREFIX = '{c.MARKER_PREFIX}'", sync_src)

FAKEHUB_STORE_SRC = r'''"""HARNESS-ONLY fake Hugging Face Hub store (never in the product image).

A filesystem hub with the REAL hub's rules and shapes (spec §0 P11/P22, §K.3):
  $FAKEHUB_ROOT/blobs/<sha256>                       every file's bytes, content-addressed
  $FAKEHUB_ROOT/repos/<ns>/<name>/refs.json          {"main": sha, "tags": {...}, "private": bool, "last_modified": iso}
  $FAKEHUB_ROOT/repos/<ns>/<name>/commits/<sha>.json {"parent": sha|null, "title": str, "date": iso,
                                                      "tree": {path: {"size", "sha256", "git", "lfs"}}}
  $FAKEHUB_ROOT/tokens.json                          {"<token>": {"name": "...", "orgs": [...]}}
  $FAKEHUB_ROOT/audit.log                            one JSON line per WRITE
  $FAKEHUB_ROOT/faults.json                          optional, re-read on EVERY call (see _fault)
Server rules: a commit whose parent_commit is not the branch head is refused (HTTP 412, the Hub's
documented optimistic-concurrency rule); an LFS addition must reference an uploaded blob; an LFS
file's blob_id is the git sha1 of its POINTER and lfs.sha256 the sha256 of its content; a regular
file's blob_id is the git sha1 of its content; .parquet/.mp4 are LFS; a folder's tree_id changes iff
anything below it changes; a missing/unreadable repo is RepositoryNotFoundError 401 anonymous and
404 with a token; a missing revision is RevisionNotFoundError 404.
"""
import datetime, fnmatch, hashlib, json, os, shutil, threading, time, uuid
from pathlib import Path

import httpx
from huggingface_hub.errors import (HfHubHTTPError, LocalTokenNotFoundError, RepositoryNotFoundError,
                                    RevisionNotFoundError)

ROOT = Path(os.environ.get('FAKEHUB_ROOT') or '/harness/fakehub_root')
LFS_EXT = ('.parquet', '.mp4')
_LOCK = threading.RLock()


def _resp(code, method='GET'):
    return httpx.Response(code, request=httpx.Request(method, 'https://fakehub/'))


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def audit(op, **kw):
    with open(ROOT / 'audit.log', 'a') as f:
        f.write(json.dumps({'t': time.time(), 'op': op, **kw}) + '\n')


def fault(op, repo_id=None, size=0):
    """faults.json: [{"op": fnmatch, "repo": fnmatch|null, "kind": "500|503|429|timeout|offline|delay|
    bandwidth|lost_response", "seconds": n, "bytes_per_s": n, "times": n|null}]. Returns the kinds
    that apply AFTER the operation (lost_response)."""
    p = ROOT / 'faults.json'
    if not p.exists():
        return []
    with _LOCK:
        faults = json.loads(p.read_text() or '[]')
        after = []
        for f in faults:
            if not fnmatch.fnmatch(op, f.get('op', '*')):
                continue
            if f.get('repo') is not None and not fnmatch.fnmatch(repo_id or '', f['repo']):
                continue
            if f.get('times') is not None:
                if f['times'] <= 0:
                    continue
                f['times'] -= 1
                p.write_text(json.dumps(faults))          # persisted BEFORE a fault raises
            kind = f['kind']
            if kind == 'delay':
                time.sleep(float(f.get('seconds', 5)))
            elif kind == 'bandwidth':
                time.sleep(size / float(f['bytes_per_s']))
            elif kind == 'lost_response':
                after.append(kind)
            elif kind == 'timeout':
                time.sleep(float(os.environ.get('FAKEHUB_TIMEOUT_S', '12')))
                raise httpx.ReadTimeout('fakehub timeout', request=httpx.Request('GET', 'https://fakehub/'))
            elif kind == 'offline':
                raise httpx.ConnectError('fakehub offline', request=httpx.Request('GET', 'https://fakehub/'))
            else:
                raise HfHubHTTPError(f'{kind} fakehub', response=_resp(int(kind)))
    return after


def git_sha1(b):
    return hashlib.sha1(b'blob %d\x00' % len(b) + b).hexdigest()


def pointer(sha256_hex, size):
    return f'version https://git-lfs.github.com/spec/v1\noid sha256:{sha256_hex}\nsize {size}\n'.encode()


def put_blob(b):
    sha = hashlib.sha256(b).hexdigest()
    p = ROOT / 'blobs' / sha
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f'.{os.getpid()}.{threading.get_ident()}')
        tmp.write_bytes(b)
        os.replace(tmp, p)
    return sha


def blob_path(sha):
    return ROOT / 'blobs' / sha


def entry_for(path, b):
    sha = put_blob(b)
    return {'size': len(b), 'sha256': sha, 'git': git_sha1(b), 'lfs': path.endswith(LFS_EXT)}


def blob_id(e):
    return git_sha1(pointer(e['sha256'], e['size'])) if e['lfs'] else e['git']


# ------------------------------------------------------------------ users and repos
def user(tok):
    if not tok:
        raise LocalTokenNotFoundError('no token')
    users = json.loads((ROOT / 'tokens.json').read_text())
    if tok not in users:
        raise HfHubHTTPError('401 Unauthorized', response=_resp(401))
    return users[tok]


def repo_dir(repo_id):
    return ROOT / 'repos' / repo_id


def load_refs(repo_id):
    return json.loads((repo_dir(repo_id) / 'refs.json').read_text())


def save_refs(repo_id, refs):
    p = repo_dir(repo_id) / 'refs.json'
    tmp = p.with_name(f'refs.{os.getpid()}.{threading.get_ident()}.tmp')
    tmp.write_text(json.dumps(refs))
    os.replace(tmp, p)


def check_access(repo_id, tok, write=False, op='?'):
    fault(op, repo_id)
    code = 404 if tok else 401                     # the real hub: anonymous -> 401, with a token -> 404
    if not (repo_dir(repo_id) / 'refs.json').exists():
        raise RepositoryNotFoundError(f'{code} {repo_id}', response=_resp(code))
    refs = load_refs(repo_id)
    ns = repo_id.split('/')[0]
    owner = False
    if tok:
        u = user(tok)
        owner = ns == u['name'] or ns in u.get('orgs', [])
    if (refs.get('private') and not owner) or (write and not owner):
        raise RepositoryNotFoundError(f'{code} {repo_id}', response=_resp(code))
    return refs


def commit(repo_id, sha):
    return json.loads((repo_dir(repo_id) / 'commits' / f'{sha}.json').read_text())


def resolve(repo_id, refs, revision):
    rev = revision or 'main'
    if rev == 'main':
        return refs['main']
    if rev in refs.get('tags', {}):
        return refs['tags'][rev]
    if (repo_dir(repo_id) / 'commits' / f'{rev}.json').exists():
        return rev
    raise RevisionNotFoundError(f'404 revision {rev}', response=_resp(404))


def new_commit(repo_id, refs, tree, title, parent):
    sha = (uuid.uuid4().hex + uuid.uuid4().hex)[:40]
    d = repo_dir(repo_id) / 'commits'
    d.mkdir(parents=True, exist_ok=True)
    (d / f'{sha}.json').write_text(json.dumps({'parent': parent, 'title': title, 'date': now_iso(), 'tree': tree}))
    refs['main'] = sha
    refs['last_modified'] = now_iso()
    save_refs(repo_id, refs)
    return sha


def create(repo_id, private):
    d = repo_dir(repo_id)
    d.mkdir(parents=True, exist_ok=True)
    refs = {'main': None, 'tags': {}, 'private': bool(private)}
    ga = b'*.parquet filter=lfs diff=lfs merge=lfs -text\n*.mp4 filter=lfs diff=lfs merge=lfs -text\n'
    new_commit(repo_id, refs, {'.gitattributes': entry_for('.gitattributes', ga)}, 'initial commit', None)


def tree_id(tree, folder):
    items = sorted((p, blob_id(e)) for p, e in tree.items() if p.startswith(folder + '/'))
    return hashlib.sha1(json.dumps(items).encode()).hexdigest() if items else None


def last_commit_of(repo_id, head, path, is_folder):
    """The newest commit in head's history that changed `path` (a folder: its tree id)."""
    def key(tree):
        return tree_id(tree, path) if is_folder else (blob_id(tree[path]) if path in tree else None)
    sha = head
    c = commit(repo_id, sha)
    k = key(c['tree'])
    while c['parent']:
        p = commit(repo_id, c['parent'])
        if key(p['tree']) != k:
            break
        sha, c = c['parent'], p
    return sha, c


# ------------------------------------------------------------------ helpers for the harness seed
def put_tree(repo_id, src, *, private=True, title='seed', tag='v3.0', robot_type=None, parent=None, repo_type='dataset'):
    """Commit the folder `src` (data/, meta/, videos/, README.md) as the new main of repo_id,
    replacing its sync dirs; moves `tag` to it. Harness seeding only. `repo_type` 'model'/'space'
    makes a repo the listings show as such (the inventory's models and Spaces, T-4)."""
    with _LOCK:
        if not (repo_dir(repo_id) / 'refs.json').exists():
            create(repo_id, private)
        refs = load_refs(repo_id)
        if repo_type != 'dataset':
            refs['type'] = repo_type
        refs['private'] = private if private is not None else refs.get('private', True)
        tree = {p: e for p, e in commit(repo_id, refs['main'])['tree'].items()
                if not p.startswith(('data/', 'meta/', 'videos/'))}
        for p in sorted(Path(src).rglob('*')):
            rel = p.relative_to(src).as_posix()
            if p.is_file() and '.cache' not in rel.split('/') and not rel.endswith('.sync.json'):
                b = p.read_bytes()
                if robot_type and rel == 'meta/info.json':
                    info = json.loads(b); info['robot_type'] = robot_type; b = json.dumps(info, indent=4).encode()
                tree[rel] = entry_for(rel, b)
        sha = new_commit(repo_id, refs, tree, title, refs['main'])
        if tag:
            refs['tags'][tag] = sha
            save_refs(repo_id, refs)
        return sha


def edit_readme(repo_id, text):
    """A human edits the dataset card on huggingface.co (a commit outside data/meta/videos)."""
    with _LOCK:
        refs = load_refs(repo_id)
        tree = dict(commit(repo_id, refs['main'])['tree'])
        tree['README.md'] = entry_for('README.md', text.encode())
        return new_commit(repo_id, refs, tree, 'Update README.md', refs['main'])
'''

FAKEHUB_SITE_SRC = r'''"""HARNESS-ONLY fake Hugging Face Hub (never in the product image).

Loaded by every Python process of the server container when the harness sets
PYTHONPATH=/harness/fakehub and FAKEHUB_ROOT (node, Daten sidecar, HF worker child, edit and
download workers). It replaces ONLY the transport: huggingface_hub's own HfApi.create_commit
(its no-op filter, its parent_commit plumbing, its preupload call) runs unchanged on top of a
patched `preupload_lfs_files` and `_send_commit`. The store and its server rules are in
fakehub_store.py. The token is the real one: huggingface_hub.get_token() (reads HF_TOKEN_PATH);
token=False (argument or HfApi(token=False)) is anonymous.
"""
import os
if os.environ.get('FAKEHUB_ROOT'):
    import fnmatch, json, shutil, sys, time
    from pathlib import Path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import httpx
    import huggingface_hub
    from huggingface_hub import hf_api
    from huggingface_hub.errors import EntryNotFoundError, HfHubHTTPError, RepositoryNotFoundError
    from huggingface_hub.hf_api import (CommitInfo, DatasetInfo, GitCommitInfo, GitRefInfo, GitRefs, RepoFile,
                                        RepoFolder, RepoUrl)
    import fakehub_store as FS

    DELAY = float(os.environ.get('FAKEHUB_DELAY_S', '0'))

    def _tok(self, token):
        if token is None:
            token = getattr(self, 'token', None)
        if token is False:
            return None
        return token if isinstance(token, str) else huggingface_hub.get_token()

    def whoami(self, token=None, **kw):
        FS.fault('whoami')
        u = FS.user(_tok(self, token))
        return {'type': 'user', 'name': u['name'], 'orgs': [{'name': o} for o in u.get('orgs', [])],
                'auth': {'accessToken': {'role': 'write'}}}

    def repo_exists(self, repo_id, *, repo_type=None, token=None, **kw):
        try:
            FS.check_access(repo_id, _tok(self, token), op='repo_exists')
            return True
        except RepositoryNotFoundError:
            return False

    def repo_info(self, repo_id, *, revision=None, repo_type=None, timeout=None, files_metadata=False, expand=None,
                  token=None, **kw):
        time.sleep(DELAY)
        refs = FS.check_access(repo_id, _tok(self, token), op='dataset_info')
        sha = FS.resolve(repo_id, refs, revision)
        tree = FS.commit(repo_id, sha)['tree']
        sib = [{'rfilename': p, **({'size': e['size']} if files_metadata else {})} for p, e in sorted(tree.items())]
        return DatasetInfo(id=repo_id, author=repo_id.split('/')[0], sha=sha, private=bool(refs.get('private')),
                           lastModified=refs.get('last_modified'), siblings=sib, tags=[], downloads=0, likes=0)

    def list_datasets(self, *, author=None, search=None, limit=None, token=None, **kw):
        time.sleep(DELAY)
        FS.fault('list_datasets', author)
        tok = _tok(self, token)
        base = FS.ROOT / 'repos'
        out = []
        for refs_p in sorted((base / author).glob('*/refs.json') if author else base.glob('*/*/refs.json')):
            rid = refs_p.parent.relative_to(base).as_posix()
            try:
                refs = FS.check_access(rid, tok, op='list_datasets.item')
            except RepositoryNotFoundError:
                continue
            if refs.get('type', 'dataset') != 'dataset':
                continue
            out.append(DatasetInfo(id=rid, author=rid.split('/')[0], sha=refs['main'], private=bool(refs.get('private')),
                                   lastModified=refs.get('last_modified'), tags=[], downloads=0, likes=0))
        return iter(out[:limit] if limit else out)

    def _list_other(kind, cls, author, expand):
        """models / Spaces: like the real hub, `sha` and `lastModified` only when `expand` asks (T-4)"""
        base = FS.ROOT / 'repos'
        want = set(expand or ())
        out = []
        for refs_p in sorted((base / author).glob('*/refs.json') if author else base.glob('*/*/refs.json')):
            refs = json.loads(refs_p.read_text())
            if refs.get('type') != kind:
                continue
            rid = refs_p.parent.relative_to(base).as_posix()
            out.append(cls(id=rid, author=rid.split('/')[0], private=bool(refs.get('private')),
                           sha=refs['main'] if 'sha' in want else None,
                           lastModified=refs.get('last_modified') if 'lastModified' in want else None, tags=[]))
        return iter(out)

    def list_models(self, *, author=None, expand=None, **kw):
        from huggingface_hub.hf_api import ModelInfo
        return _list_other('model', ModelInfo, author, expand)

    def list_spaces(self, *, author=None, expand=None, **kw):
        from huggingface_hub.hf_api import SpaceInfo
        return _list_other('space', SpaceInfo, author, expand)

    def list_repo_refs(self, repo_id, *, repo_type=None, token=None, **kw):
        refs = FS.check_access(repo_id, _tok(self, token), op='list_repo_refs')
        return GitRefs(branches=[GitRefInfo(name='main', ref='refs/heads/main', target_commit=refs['main'])], converts=[],
                       tags=[GitRefInfo(name=k, ref=f'refs/tags/{k}', target_commit=v) for k, v in refs.get('tags', {}).items()])

    def list_repo_commits(self, repo_id, *, repo_type=None, token=None, revision=None, formatted=False, **kw):
        refs = FS.check_access(repo_id, _tok(self, token), op='list_repo_commits')
        sha, out = FS.resolve(repo_id, refs, revision), []
        while sha:
            c = FS.commit(repo_id, sha)
            out.append(GitCommitInfo(commit_id=sha, authors=['fakehub'], created_at=hf_api.parse_datetime(c['date']),
                                     title=c['title'], message='', formatted_title=None, formatted_message=None))
            sha = c['parent']
        return out

    def list_repo_tree(self, repo_id, path_in_repo=None, *, recursive=False, expand=False, revision=None,
                       repo_type=None, token=None):
        refs = FS.check_access(repo_id, _tok(self, token), op='list_repo_tree')
        head = FS.resolve(repo_id, refs, revision)
        tree = FS.commit(repo_id, head)['tree']
        base = (path_in_repo.rstrip('/') + '/') if path_in_repo else ''
        files, folders = [], set()
        for p in sorted(tree):
            if not p.startswith(base):
                continue
            rest = p[len(base):]
            parts = rest.split('/')
            for i in range(1, len(parts)):
                if recursive or i == 1:
                    folders.add(base + '/'.join(parts[:i]))
            if recursive or len(parts) == 1:
                files.append(p)

        def lc(path, is_folder):
            sha, c = FS.last_commit_of(repo_id, head, path, is_folder)
            return {'id': sha, 'title': c['title'], 'date': c['date']}
        out = []
        for p in files:
            e = tree[p]
            kw = {'path': p, 'size': e['size'], 'oid': FS.blob_id(e), 'type': 'file'}
            if e['lfs']:
                kw['lfs'] = {'oid': e['sha256'], 'size': e['size'], 'pointerSize': len(FS.pointer(e['sha256'], e['size']))}
            if expand:
                kw['lastCommit'] = lc(p, False)
            out.append(RepoFile(**kw))
        for f in sorted(folders):
            kw = {'path': f, 'oid': FS.tree_id(tree, f)}
            if expand:
                kw['lastCommit'] = lc(f, True)
            out.append(RepoFolder(**kw))
        return iter(out)

    def list_repo_files(self, repo_id, *, revision=None, repo_type=None, token=None, **kw):
        refs = FS.check_access(repo_id, _tok(self, token), op='list_repo_files')
        return sorted(FS.commit(repo_id, FS.resolve(repo_id, refs, revision))['tree'])

    def create_repo(self, repo_id, *, token=None, private=None, repo_type=None, exist_ok=False, **kw):
        FS.fault('create_repo', repo_id)
        u = FS.user(_tok(self, token))
        ns = repo_id.split('/')[0]
        if ns != u['name'] and ns not in u.get('orgs', []):
            raise HfHubHTTPError('403 Forbidden', response=FS._resp(403, 'POST'))
        with FS._LOCK:
            if (FS.repo_dir(repo_id) / 'refs.json').exists():
                if not exist_ok:
                    raise HfHubHTTPError('409 Conflict', response=FS._resp(409, 'POST'))
            else:
                FS.create(repo_id, True if private is None else bool(private))
                FS.audit('create_repo', repo=repo_id, private=bool(private))
        return RepoUrl(f'https://huggingface.co/datasets/{repo_id}')

    def _set_modes(additions, repo_id):
        refs = FS.load_refs(repo_id)
        head_tree = FS.commit(repo_id, refs['main'])['tree']
        for op in additions:
            if getattr(op, '_upload_mode', None) is not None:
                continue
            lfs = op.path_in_repo.endswith(FS.LFS_EXT)
            op._upload_mode = 'lfs' if lfs else 'regular'
            op._should_ignore = False
            e = head_tree.get(op.path_in_repo)
            op._remote_oid = (e['sha256'] if lfs else e['git']) if (e and e['lfs'] == lfs) else None

    def preupload_lfs_files(self, repo_id, additions, *, token=None, repo_type=None, revision=None, create_pr=None,
                            num_threads=5, free_memory=True, gitignore_content=None):
        FS.check_access(repo_id, _tok(self, token), write=True, op='preupload_lfs_files')
        additions = list(additions)
        _set_modes(additions, repo_id)
        for op in additions:
            if op._upload_mode != 'lfs' or getattr(op, '_is_uploaded', False):
                continue
            FS.fault('preupload', repo_id, size=op.upload_info.size)
            with op.as_file() as f:
                b = f.read()
            if FS.put_blob(b) != op.upload_info.sha256.hex():
                raise HfHubHTTPError('400 sha256 mismatch (file changed during upload)', response=FS._resp(400, 'POST'))
            op._is_uploaded = True
        FS.audit('preupload_lfs_files', repo=repo_id, files=len(additions))

    def send_commit(*, operations, files_to_copy, commit_message, commit_description, repo_type, repo_id, headers,
                    revision, endpoint=None, parent_commit=None, create_pr=False, hot_reload=False, retry_on_error=False):
        from huggingface_hub import CommitOperationAdd, CommitOperationDelete
        after = FS.fault('create_commit', repo_id)
        with FS._LOCK:
            refs = FS.load_refs(repo_id)
            if parent_commit is not None and parent_commit != refs['main']:
                FS.audit('commit_refused_stale_parent', repo=repo_id, parent=parent_commit, head=refs['main'])
                raise HfHubHTTPError('412 Precondition Failed: A commit has happened since. Please refresh and try again.',
                                     response=FS._resp(412, 'POST'))
            tree = dict(FS.commit(repo_id, refs['main'])['tree'])
            for op in operations:
                if isinstance(op, CommitOperationDelete):
                    if op.is_folder:
                        for p in [p for p in tree if p.startswith(op.path_in_repo.rstrip('/') + '/')]:
                            del tree[p]
                    else:
                        tree.pop(op.path_in_repo, None)
                elif isinstance(op, CommitOperationAdd):
                    if op._upload_mode == 'lfs':
                        sha = op.upload_info.sha256.hex()
                        if not FS.blob_path(sha).exists():
                            raise HfHubHTTPError('400 LFS object not uploaded', response=FS._resp(400, 'POST'))
                        tree[op.path_in_repo] = {'size': op.upload_info.size, 'sha256': sha,
                                                 'git': FS.git_sha1(FS.blob_path(sha).read_bytes()), 'lfs': True}
                    else:
                        with op.as_file() as f:
                            tree[op.path_in_repo] = FS.entry_for(op.path_in_repo, f.read())
            sha = FS.new_commit(repo_id, refs, tree, commit_message, refs['main'])
            FS.audit('create_commit', repo=repo_id, sha=sha, parent=parent_commit, ops=len(list(operations)),
                     title=commit_message)
        if 'lost_response' in after:
            raise httpx.ReadTimeout('response lost after the server committed',
                                    request=httpx.Request('POST', 'https://fakehub/'))
        return CommitInfo(commit_url=f'https://huggingface.co/datasets/{repo_id}/commit/{sha}', commit_message=commit_message,
                          commit_description=commit_description, oid=sha, _endpoint='https://huggingface.co')

    def delete_tag(self, repo_id, *, tag, repo_type=None, token=None, **kw):
        FS.check_access(repo_id, _tok(self, token), write=True, op='delete_tag')
        with FS._LOCK:
            refs = FS.load_refs(repo_id)
            if tag not in refs.get('tags', {}):
                from huggingface_hub.errors import RevisionNotFoundError
                raise RevisionNotFoundError(f'404 tag {tag}', response=FS._resp(404))
            del refs['tags'][tag]
            FS.save_refs(repo_id, refs)
        FS.audit('delete_tag', repo=repo_id, tag=tag)

    def create_tag(self, repo_id, *, tag, tag_message=None, revision=None, repo_type=None, token=None, exist_ok=False, **kw):
        FS.check_access(repo_id, _tok(self, token), write=True, op='create_tag')
        with FS._LOCK:
            refs = FS.load_refs(repo_id)
            if tag in refs.get('tags', {}) and not exist_ok:
                raise HfHubHTTPError('409 tag exists', response=FS._resp(409, 'POST'))
            refs.setdefault('tags', {})[tag] = FS.resolve(repo_id, refs, revision)
            FS.save_refs(repo_id, refs)
        FS.audit('create_tag', repo=repo_id, tag=tag, sha=refs['tags'][tag])

    def delete_repo(self, repo_id, *, repo_type=None, token=None, missing_ok=False, **kw):
        try:
            FS.check_access(repo_id, _tok(self, token), write=True, op='delete_repo')
        except RepositoryNotFoundError:
            if missing_ok:
                return
            raise
        shutil.rmtree(FS.repo_dir(repo_id))
        FS.audit('delete_repo', repo=repo_id)

    def upload_large_folder(repo_id, folder_path, *, repo_type, revision=None, private=None, print_report=True, **kw):
        """Kept for callers outside the dataset sync (models): one commit, no parent check."""
        refs = FS.check_access(repo_id, _tok(None, kw.get('token')), write=True, op='upload_large_folder')
        with FS._LOCK:
            refs = FS.load_refs(repo_id)
            tree = dict(FS.commit(repo_id, refs['main'])['tree'])
            for p in sorted(Path(folder_path).rglob('*')):
                rel = p.relative_to(folder_path).as_posix()
                if p.is_file() and '.cache' not in rel.split('/'):
                    tree[rel] = FS.entry_for(rel, p.read_bytes())
            sha = FS.new_commit(repo_id, refs, tree, 'Add files using upload-large-folder tool', refs['main'])
        FS.audit('upload_large_folder', repo=repo_id, sha=sha)

    def snapshot_download(repo_id, *, repo_type=None, revision=None, local_dir=None, allow_patterns=None,
                          ignore_patterns=None, tqdm_class=None, token=None, **kw):
        refs = FS.check_access(repo_id, _tok(None, token), op='snapshot_download')
        sha = FS.resolve(repo_id, refs, revision)
        tree = FS.commit(repo_id, sha)['tree']
        dst = Path(local_dir) if local_dir else FS.ROOT / 'cache' / repo_id / sha
        pats = [allow_patterns] if isinstance(allow_patterns, str) else allow_patterns
        for rel, e in sorted(tree.items()):
            if pats and not any(fnmatch.fnmatch(rel, pat) for pat in pats):
                continue
            FS.fault('snapshot_download.file', repo_id, size=e['size'])
            time.sleep(DELAY / 10)
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FS.blob_path(e['sha256']), dst / rel)
        return str(dst)

    def hf_hub_download(repo_id, filename, *, repo_type=None, revision=None, local_dir=None, token=None, **kw):
        refs = FS.check_access(repo_id, _tok(None, token), op='hf_hub_download')
        sha = FS.resolve(repo_id, refs, revision)
        e = FS.commit(repo_id, sha)['tree'].get(filename)
        if e is None:
            raise EntryNotFoundError(f'404 {filename}', response=FS._resp(404))
        dst = (Path(local_dir) if local_dir else FS.ROOT / 'cache' / repo_id / sha) / filename
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FS.blob_path(e['sha256']), dst)
        return str(dst)

    for _name, _fn in [('whoami', whoami), ('repo_exists', repo_exists), ('repo_info', repo_info),
                       ('dataset_info', repo_info), ('list_datasets', list_datasets), ('list_models', list_models),
                       ('list_spaces', list_spaces), ('list_repo_refs', list_repo_refs),
                       ('list_repo_commits', list_repo_commits), ('list_repo_tree', list_repo_tree),
                       ('list_repo_files', list_repo_files), ('create_repo', create_repo),
                       ('preupload_lfs_files', preupload_lfs_files), ('delete_tag', delete_tag),
                       ('create_tag', create_tag), ('delete_repo', delete_repo)]:
        setattr(hf_api.HfApi, _name, _fn)
    hf_api.HfApi._validate_yaml = lambda self, content, repo_type=None, token=None: None
    hf_api._send_commit = send_commit
    for _mod in (huggingface_hub, hf_api):
        setattr(_mod, 'upload_large_folder', upload_large_folder)
    huggingface_hub.snapshot_download = snapshot_download
    huggingface_hub.hf_hub_download = hf_hub_download
    import huggingface_hub._snapshot_download as _sd
    _sd.snapshot_download = snapshot_download
    import huggingface_hub.file_download as _fd
    _fd.hf_hub_download = hf_hub_download
    # bound module-level aliases made from a default HfApi at import time
    _default = hf_api.HfApi()
    for _name in ('create_commit', 'preupload_lfs_files', 'create_repo', 'delete_repo', 'create_tag', 'delete_tag',
                  'list_repo_tree', 'list_repo_refs', 'list_repo_commits', 'dataset_info', 'repo_info', 'whoami'):
        for _mod in (huggingface_hub, hf_api):
            if _name in getattr(_mod, '__dict__', {}) or (_mod is huggingface_hub and hasattr(_mod, _name)):
                setattr(_mod, _name, getattr(_default, _name))
'''


if __name__ == '__main__':
    unittest.main()
