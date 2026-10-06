"""Daten 2.0 sync model (spec §C2/§C3/§E10): every row of the decision table,
``ours()`` both ways, the record I/O, ``meta_digest``/``manifest``, the
three-way „Beide behalten" plan — against hub views built by the REAL hub
rules (P11): an LFS entry's ``blob_id`` is the git sha1 of its POINTER and
``lfs.sha256`` the sha256 of the content, a regular file's ``blob_id`` the git
sha1 of the content, folders carry content-addressed tree ids and their last
commit. Pinned with real Hugging Face values (``IMsubin/omx_f_put_apple_into_
the_basket_v3`` at ``234f9ddd…``) and with git itself (``hello\\n``).

``dataset_sync`` is stdlib-only and loaded by path (this directory's other
loaders stub the ``physical_ai_server`` package).
"""

import hashlib
import importlib.util
import json
import pathlib
import shutil
import tempfile
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYNC_PATH = (REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
             / 'data_processing' / 'dataset_sync.py')


def _load():
    spec = importlib.util.spec_from_file_location('_dataset_sync_under_test', str(SYNC_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = _load()


# ── a fake hub with the real hub's rules ─────────────────────────────────────

def _entry(path, data):
    sha = hashlib.sha256(data).hexdigest()
    lfs = path.endswith(('.parquet', '.mp4'))
    blob = S.git_sha1_bytes(S.lfs_pointer(sha, len(data))) if lfs else S.git_sha1_bytes(data)
    return {'size': len(data), 'sha256': sha, 'blob_id': blob, 'lfs': lfs}


class FakeRepo:
    """Commits of whole trees; a hub view exactly like hub_sync.hub_view."""

    def __init__(self):
        self.commits = {}
        self.main = None
        self._n = 0

    def commit(self, files, title='upload', base=None, keep_from=None):
        """``files``: {path: bytes}; ``keep_from``: a commit whose tree is the start."""
        tree = dict(self.commits[keep_from]['tree']) if keep_from else {}
        for p, data in files.items():
            if data is None:
                tree.pop(p, None)
            else:
                tree[p] = _entry(p, data)
        self._n += 1
        sha = hashlib.sha1(f'c{self._n}'.encode()).hexdigest()
        self.commits[sha] = {'parent': self.main, 'title': title, 'date': f'2026-10-05T12:{self._n:02d}:00+00:00',
                             'tree': tree}
        self.main = sha
        return sha

    def tree_id(self, tree, folder):
        items = sorted((p, e['blob_id']) for p, e in tree.items() if p.startswith(folder + '/'))
        return hashlib.sha1(json.dumps(items).encode()).hexdigest() if items else None

    def last_commit(self, head, folder):
        sha = head
        c = self.commits[sha]
        k = self.tree_id(c['tree'], folder)
        while c['parent']:
            p = self.commits[c['parent']]
            if self.tree_id(p['tree'], folder) != k:
                break
            sha, c = c['parent'], p
        return {'oid': sha, 'title': c['title'], 'date': c['date']}

    def view(self, head=None):
        head = head or self.main
        tree = self.commits[head]['tree']
        trees = {d: self.tree_id(tree, d) for d in S.SYNC_DIRS}
        last = {d: self.last_commit(head, d) for d in S.SYNC_DIRS if trees[d]}

        def files():
            items = [types.SimpleNamespace(path=p, size=e['size'], blob_id=e['blob_id'],
                                           lfs=types.SimpleNamespace(sha256=e['sha256']) if e['lfs'] else None)
                     for p, e in sorted(tree.items())]
            items += [types.SimpleNamespace(path=d, tree_id=trees[d]) for d in S.SYNC_DIRS if trees[d]]
            return S.hub_entries(items)
        return {'state': 'present', 'head': head, 'trees': trees, 'last': last, 'files': files}


SESSION1 = {
    'data/chunk-000/file-000.parquet': b'data-session-1' * 20,
    'meta/episodes/chunk-000/file-000.parquet': b'episodes-session-1' * 10,
    'videos/observation.images.scene/chunk-000/file-000.mp4': b'\x00video-1' * 100,
    'meta/info.json': b'{"total_episodes": 4}',
    'meta/stats.json': b'{"s": 1}',
    'meta/tasks.parquet': b'tasks-1',
}
SESSION2 = {
    'data/chunk-000/file-001.parquet': b'data-session-2' * 20,
    'meta/episodes/chunk-000/file-001.parquet': b'episodes-session-2' * 10,
    'videos/observation.images.scene/chunk-000/file-001.mp4': b'\x00video-2' * 100,
    'meta/info.json': b'{"total_episodes": 7}',
    'meta/stats.json': b'{"s": 2}',
}
OTHER2 = {
    'data/chunk-000/file-001.parquet': b'data-other-2' * 20,
    'meta/episodes/chunk-000/file-001.parquet': b'episodes-other-2' * 10,
    'videos/observation.images.scene/chunk-000/file-001.mp4': b'\x00video-X' * 100,
    'meta/info.json': b'{"total_episodes": 6}',
    'meta/stats.json': b'{"s": 3}',
}


class _Tmp(unittest.TestCase):

    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix='d2_sync_'))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = self.base / 'lena-schmidt' / 'omx_f_wuerfel'

    def write(self, *layers, root=None):
        root = root or self.root
        for layer in layers:
            for p, data in layer.items():
                f = root / p
                f.parent.mkdir(parents=True, exist_ok=True)
                if data is None:
                    f.unlink(missing_ok=True)
                else:
                    f.write_bytes(data)
        return root

    def synced_record(self, repo, head):
        view = repo.view(head)
        return {'v': 1, 'repo_id': 'lena-schmidt/omx_f_wuerfel', 'hub_sha': head,
                'hub_trees': view['trees'], 'local_digest': S.meta_digest(self.root)}


# ── §C3, one row at a time ─────────────────────────────────────────────────

class TheDecisionTable(_Tmp):

    def test_not_asked_unreachable_not_visible(self):
        self.write(SESSION1)
        self.assertEqual(S.decide(self.root, None, None), ('unknown', 'not_asked', None))
        self.assertEqual(S.decide(self.root, None, {'state': 'unreachable'}), ('unknown', 'unreachable', None))
        self.assertEqual(S.decide(self.root, None, {'state': 'absent', 'complete': False}),
                         ('unknown', 'not_visible', None))

    def test_a_local_change_is_true_whatever_the_hub_did(self):
        repo = FakeRepo()
        head = repo.commit(SESSION1)
        self.write(SESSION1)
        rec = self.synced_record(repo, head)
        self.write({'meta/stats.json': b'{"s": 9}'})
        for hub in (None, {'state': 'unreachable'}, {'state': 'absent', 'complete': False}):
            self.assertEqual(S.decide(self.root, rec, hub), ('changed', None, None))

    def test_absent_complete_and_an_empty_repo_are_local(self):
        self.write(SESSION1)
        self.assertEqual(S.decide(self.root, None, {'state': 'absent', 'complete': True}), ('local', None, None))
        repo = FakeRepo()
        repo.commit({'.gitattributes': b'*.mp4 filter=lfs'})
        self.assertEqual(S.decide(self.root, None, repo.view()), ('local', None, None))

    def test_synced_the_four_cells(self):
        repo = FakeRepo()
        base = repo.commit(SESSION1)
        self.write(SESSION1)
        rec = self.synced_record(repo, base)
        self.assertEqual(S.decide(self.root, rec, repo.view()), ('current', None, None))            # (F,F)
        repo.commit(SESSION2, keep_from=base)
        self.assertEqual(S.decide(self.root, rec, repo.view()), ('newer', None, None))              # (F,T)
        self.assertEqual(S.decide(self.root, rec, repo.view(base)), ('current', None, None))
        self.write({'meta/stats.json': b'{"s": 9}'})
        self.assertEqual(S.decide(self.root, rec, repo.view(base)), ('changed', None, None))        # (T,F)
        self.assertEqual(S.decide(self.root, rec, repo.view()), ('conflict', None, None))           # (T,T)

    def test_tag_ok_false_reads_changed(self):
        repo = FakeRepo()
        head = repo.commit(SESSION1)
        self.write(SESSION1)
        rec = dict(self.synced_record(repo, head), tag_ok=False)
        self.assertEqual(S.decide(self.root, rec, repo.view()), ('changed', None, None))

    def test_a_readme_only_commit_is_no_change(self):
        repo = FakeRepo()
        base = repo.commit(SESSION1)
        self.write(SESSION1)
        rec = self.synced_record(repo, base)
        repo.commit({'README.md': b'# a card edited on the website'}, keep_from=base, title='Update README.md')
        self.assertNotEqual(repo.main, base)
        self.assertEqual(S.decide(self.root, rec, repo.view()), ('current', None, None))


class OursRecognisesALandedCommit(_Tmp):

    def test_our_marker_with_data_and_videos_not_newer_is_ours(self):
        repo = FakeRepo()
        base = repo.commit(SESSION1)
        self.write(SESSION1)
        rec = self.synced_record(repo, base)
        self.write(SESSION2)                     # a session, then an upload whose record write was lost
        digest = S.meta_digest(self.root)
        new = repo.commit(SESSION2, keep_from=base, title=f'{S.marker(digest)} EduBotics: omx_f_wuerfel')
        view = repo.view()
        self.assertTrue(S.ours(view, digest))
        state, reason, repair = S.decide(self.root, rec, view)
        self.assertEqual((state, reason), ('current', None))
        self.assertEqual(repair, {'hub_sha': new, 'hub_trees': view['trees']})
        self.assertEqual(S.decide(self.root, dict(rec, tag_ok=False), view)[0], 'changed')

    def test_another_digest_or_a_later_data_commit_is_not_ours(self):
        repo = FakeRepo()
        base = repo.commit(SESSION1)
        self.write(SESSION1)
        digest = S.meta_digest(self.root)
        repo.commit(SESSION2, keep_from=base, title=f'{S.marker("f" * 64)} EduBotics: x')
        self.assertFalse(S.ours(repo.view(), digest))
        repo2 = FakeRepo()
        b2 = repo2.commit(SESSION1, title=f'{S.marker(digest)} EduBotics: x')
        self.assertTrue(S.ours(repo2.view(), digest))
        repo2.commit({'data/chunk-000/file-009.parquet': b'later'}, keep_from=b2, title='another PC')
        self.assertFalse(S.ours(repo2.view(), digest))
        self.assertFalse(S.ours({'last': {}}, digest))
        self.assertFalse(S.ours(None, digest))


class TheContentDecision(_Tmp):
    """Record-less datasets: decided by content every time (R-5, S-1)."""

    def test_equal_descendant_ancestor_diverged(self):
        repo = FakeRepo()
        h1 = repo.commit(SESSION1)
        self.write(SESSION1)
        self.assertEqual(S.decide(self.root, None, repo.view()), ('current', None, None))
        self.write(SESSION2)                                                    # local = hub + a session
        self.assertEqual(S.decide(self.root, None, repo.view(h1)), ('changed', None, None))
        repo.commit(SESSION2, keep_from=h1)
        shutil.rmtree(self.root)
        self.write(SESSION1)                                                    # hub = local + a session
        self.assertEqual(S.decide(self.root, None, repo.view()), ('newer', None, None))
        shutil.rmtree(self.root)
        self.write(SESSION1, OTHER2)                                            # one base, two sessions
        self.assertEqual(S.decide(self.root, None, repo.view()), ('conflict', None, None))

    def test_a_record_without_hub_sha_and_tag_ok_false_reads_changed(self):
        repo = FakeRepo()
        repo.commit(SESSION1)
        self.write(SESSION1)
        rec = {'v': 1, 'repo_id': 'lena-schmidt/omx_f_wuerfel', 'tag_ok': False}
        self.assertEqual(S.decide(self.root, rec, repo.view()), ('changed', None, None))

    def test_videos_by_size_in_the_decision_listed_for_the_exact_check(self):
        repo = FakeRepo()
        repo.commit(SESSION1)
        self.write(SESSION1)
        vid = 'videos/observation.images.scene/chunk-000/file-000.mp4'
        flipped = bytearray(SESSION1[vid])
        flipped[50] ^= 0xFF
        self.write({vid: bytes(flipped)})
        view = repo.view()
        self.assertEqual(S.decide(self.root, None, view)[0], 'current')       # the accepted residual
        self.assertEqual(S.assumed_equal(self.root, view['files']()), [vid])
        self.assertFalse(S.file_equal_exact(self.root, vid, view['files']()[vid]))

    def test_data_and_meta_are_compared_exactly(self):
        repo = FakeRepo()
        repo.commit(SESSION1)
        self.write(SESSION1)
        p = 'data/chunk-000/file-000.parquet'
        changed = bytearray(SESSION1[p])
        changed[3] ^= 1
        self.write({p: bytes(changed)})
        self.assertEqual(S.decide(self.root, None, repo.view())[0], 'conflict')

    def test_a_local_file_equal_to_the_lfs_pointer_is_not_equal(self):
        """An LFS entry's blob_id is the POINTER's git sha1: a local file holding the
        pointer bytes has a git sha1 equal to it, and is still NOT the content."""
        repo = FakeRepo()
        repo.commit(SESSION1)
        p = 'data/chunk-000/file-000.parquet'
        entry = repo.view()['files']()[p]
        pointer = S.lfs_pointer(entry['lfs_sha256'], entry['size'])
        self.write({p: pointer})
        self.assertEqual(S.git_sha1_file(self.root / p), entry['blob_id'])
        self.assertFalse(S.file_equal_exact(self.root, p, entry))


class OfficialFieldsWithRealHubValues(unittest.TestCase):

    def test_the_lfs_pointer_rule_against_real_hub_values(self):
        real = [  # IMsubin/omx_f_put_apple_into_the_basket_v3 @ 234f9ddd8b7265c230a00f65e25d1287152d8c59
            ('meta/episodes/chunk-000/file-000.parquet', 143151,
             '142c650d25811a9fdc31fedf638e839b16efb754561173ab9aa70e84ccb3066a',
             '7e76f6eb740bc999b1a2626db7f0768d2e09b3c4', 131),
            ('data/chunk-000/file-000.parquet', 2607621,
             '2c7499ac2089863ddff6c2be7d72cf57992b79baed8fb310795ec78135d681bd',
             '0a4ef29048de14fd3a44b9da49f83bbad54bcdf1', 132),
            ('meta/tasks.parquet', 2226,
             '83952f3cd9bf6057b8e779cea417f88819fdadcd49f177b6c568fa96b580b340',
             '4560a37cedfb8fa3194aa27ffc1a01ccc3b87559', 129),
        ]
        for path, size, sha256, blob_id, pointer_size in real:
            pointer = S.lfs_pointer(sha256, size)
            self.assertEqual(len(pointer), pointer_size, path)
            self.assertEqual(S.git_sha1_bytes(pointer), blob_id, path)

    def test_the_content_rule_against_git(self):
        self.assertEqual(S.git_sha1_bytes(b'hello\n'), 'ce013625030ba8dba906f756967f9e9ca394464a')
        d = pathlib.Path(tempfile.mkdtemp(prefix='d2_git_'))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / 'f').write_bytes(b'hello\n')
        self.assertEqual(S.git_sha1_file(d / 'f'), 'ce013625030ba8dba906f756967f9e9ca394464a')


class TheRecord(_Tmp):

    def test_written_atomically_and_read_tolerantly(self):
        self.write(SESSION1)
        S.update_record(self.root, 'lena-schmidt/omx_f_wuerfel', hub_sha='a' * 40, display_name='Würfel')
        p = S.record_path(self.root)
        self.assertEqual(p.name, 'omx_f_wuerfel.sync.json')
        self.assertEqual(json.loads(p.read_text(encoding='utf-8'))['display_name'], 'Würfel')
        self.assertEqual([x.name for x in p.parent.iterdir() if x.name.endswith('.tmp')], [])
        p.write_text('{not json')
        self.assertIsNone(S.read_record(self.root))
        p.write_text(json.dumps({'v': 2, 'repo_id': 'lena-schmidt/omx_f_wuerfel'}))
        self.assertIsNone(S.read_record(self.root))
        p.write_text('[1, 2]')
        self.assertIsNone(S.read_record(self.root))

    def test_a_record_belongs_to_its_own_dataset_only(self):
        self.write(SESSION1)
        own = 'lena-schmidt/omx_f_wuerfel'
        self.assertIsNone(S.update_record(self.root, 'lena-schmidt/other', hub_sha='b' * 40))
        self.assertFalse(S.record_path(self.root).exists(), 'folder X uploaded as Y writes no record')
        S.write_record(self.root, {'v': 1, 'repo_id': 'lena-schmidt/other', 'hub_sha': 'c' * 40})
        self.assertEqual(S.own_record(self.root, own), {}, 'a record naming another repo is ignored')
        self.assertEqual(S.own_record(self.root, 'lena-schmidt/other'), {}, 'and Y is not this folder')
        rec = S.update_record(self.root, own, hub_sha='d' * 40)
        self.assertEqual(rec, {'v': 1, 'repo_id': own, 'hub_sha': 'd' * 40}, 'replaced, nothing of Y kept')
        self.assertEqual(S.update_record(self.root, own, hub_sha=None), {'v': 1, 'repo_id': own})

    def test_meta_digest_manifest_and_the_sibling_paths(self):
        self.write(SESSION1)
        d1 = S.meta_digest(self.root)
        self.assertEqual(S.meta_digest(self.root), d1)
        self.write({'data/chunk-000/file-000.parquet': b'other data'})
        self.assertEqual(S.meta_digest(self.root), d1, 'data/ is not part of the version id')
        self.write({'meta/stats.json': b'{"s": 5}'})
        self.assertNotEqual(S.meta_digest(self.root), d1)
        self.assertEqual(S.marker(d1), f'[edubotics:{d1[:16]}]')
        files = S.files_manifest(self.root)
        self.assertEqual(sorted(files), ['data/chunk-000/file-000.parquet',
                                         'meta/episodes/chunk-000/file-000.parquet',
                                         'videos/observation.images.scene/chunk-000/file-000.mp4'])
        self.assertEqual(files['data/chunk-000/file-000.parquet'],
                         hashlib.sha256(b'other data').hexdigest())
        self.assertEqual(S.manifest({'meta/info.json': 'x', 'videos/a.mp4': 'y'}), {'videos/a.mp4': 'y'})
        ns = self.root.parent
        self.assertEqual(S.session_marker_path(self.root), ns / 'omx_f_wuerfel.session.json')
        self.assertEqual(S.record_next_path(self.root), ns / '.omx_f_wuerfel.sync.next.json')
        self.assertEqual(S.lock_path(self.root), ns / '.omx_f_wuerfel.lock')
        self.assertEqual(S.journal_path(self.root), ns / '.omx_f_wuerfel.journal.json')
        self.assertEqual(S.folder_id(self.root), 'lena-schmidt/omx_f_wuerfel')


class KeepBothPlan(unittest.TestCase):
    """P18's cases on plain id lists (the ids are episode identities)."""

    def _ids(self, plan, local, hub):
        return [(local if side == 'L' else hub)[i] for side, i in plan]

    def test_deleted_here_stays_deleted_a_session_there_is_kept(self):
        base = ['a', 'b', 'c', 'd']
        local = ['a', 'c']                       # b and d deleted here since the sync
        hub = ['a', 'b', 'c', 'd', 'x', 'y', 'z']  # a session added there
        plan = S.plan_keep_both(local, hub, base)
        self.assertEqual(self._ids(plan, local, hub), ['a', 'c', 'x', 'y', 'z'])

    def test_the_reverse(self):
        base = ['a', 'b', 'c', 'd']
        local = ['a', 'b', 'c', 'd', 'x', 'y', 'z']
        hub = ['a', 'c']
        self.assertEqual(self._ids(S.plan_keep_both(local, hub, base), local, hub), ['a', 'c', 'x', 'y', 'z'])

    def test_both_recorded_the_union_once(self):
        base = ['a', 'b']
        local = ['a', 'b', 'l1', 'l2']
        hub = ['a', 'b', 'h1', 'h2', 'h3']
        self.assertEqual(self._ids(S.plan_keep_both(local, hub, base), local, hub),
                         ['a', 'b', 'l1', 'l2', 'h1', 'h2', 'h3'])

    def test_a_take_twice_in_one_copy_stays_twice(self):
        base = ['a']
        local = ['a', 'k', 'k']
        hub = ['a', 'k']
        self.assertEqual(self._ids(S.plan_keep_both(local, hub, base), local, hub), ['a', 'k', 'k'])

    def test_no_base_is_the_union_by_multiplicity(self):
        local = ['a', 'b', 'c']
        hub = ['a', 'd']
        self.assertEqual(self._ids(S.plan_keep_both(local, hub, None), local, hub), ['a', 'b', 'c', 'd'])
        # a deletion on one side then comes back (documented, §L)
        self.assertEqual(self._ids(S.plan_keep_both(['a'], ['a', 'b'], None), ['a'], ['a', 'b']), ['a', 'b'])

    def test_both_sides_deleted_a_different_episode(self):
        base = ['a', 'b', 'c']
        local = ['a', 'c']        # b deleted here
        hub = ['a', 'b']          # c deleted there
        self.assertEqual(self._ids(S.plan_keep_both(local, hub, base), local, hub), ['a'])


if __name__ == '__main__':
    unittest.main()
