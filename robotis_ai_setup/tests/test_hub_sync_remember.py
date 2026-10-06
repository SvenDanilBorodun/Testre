"""Daten 2.0 fix round 2, T2-1 (owner: „remember the state first") — the sync
record of a dataset WITHOUT one that is provably identical to its hub copy
(``hub_sync.remember_identical``, spec §C2/§C3).

Every dataset uploaded before Daten 2.0 has no record. Decided by content it
reads „Aktuell"; once the student deleted an episode it read „Hier und online
verschieden", and „Beide behalten" (no base) brought the deleted episode back.
Remembered when it is identical, a later local edit reads „Hier geändert – nicht
hochgeladen" and one upload makes the hub equal to it.

What is proven, against the Appendix K fake hub in-process (``HubCase``):

* identical → the record a download or an upload writes (``hub_sha`` = the head,
  ``hub_trees``, ``local_digest``, ``files``, ``synced_at``; ``display_name`` and
  ``private`` kept); the next decision reads it; delete an episode → ``changed``
  (without the record: ``conflict``); the upload then leaves the hub holding
  exactly the remaining files;
* never on a guess: a video of the same size with one other byte, a descendant,
  an ancestor, a different file set → nothing written;
* never for another repo (G-11/H-7), never over a record that names another
  repo, never with a crash marker, never over a synced record;
* the final check and the write under the dataset's stage lock: a held lock, a
  lock file that was replaced, a dataset that changed while it was hashed →
  nothing written; the lock is never waited for;
* a stage (edit, download, delete) waits briefly for the stage lock instead of
  failing at once, so the remember step's short hold never refuses one.
"""

import fcntl
import os
import pathlib
import tempfile
import threading
import time

from test_hub_sync_upload import BASE, HubCase, INFO, REPO, SESSION2, write_tree

SYNC_TOP = ('data/', 'meta/', 'videos/')
EPISODE_TOP = ('data/', 'meta/episodes/', 'videos/')


class RememberTheStateFirst(HubCase):

    # -- helpers ------------------------------------------------------------------

    def legacy(self, *layers, display_name=None):
        """A dataset uploaded before Daten 2.0: on the hub exactly as here, no record."""
        write_tree(self.root, *(layers or (BASE,)))
        self.FS.put_tree(REPO, self.root, private=False, title='an upload from before Daten 2.0')
        if display_name is not None:
            self.S.write_record(self.root, {'v': 1, 'repo_id': REPO, 'display_name': display_name,
                                            'private': True})
        return self.view()

    def view(self):
        v = self.H.hub_view(self.api, REPO)
        return v, v['files']()

    def remember(self, view_files, repo=REPO, root=None):
        view, files = view_files
        return self.H.remember_identical(root or self.root, repo, view, files)

    def local_sync_files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*')
                if p.is_file() and p.relative_to(self.root).as_posix().startswith(SYNC_TOP)}

    def hub_sync_files(self):
        tree = self.hub_tree()
        return {p: (self.FS.blob_path(e['sha256'])).read_bytes() for p, e in tree.items() if p.startswith(SYNC_TOP)}

    def delete_an_episode(self):
        """What the engine's delete of ONE episode leaves (v3.0 keeps many
        episodes per file): that file's rows and packets rewritten without it,
        meta rewritten, the record's ``files`` = the output's hashes, the rest
        of the record kept."""
        write_tree(self.root, {
            'data/chunk-000/file-000.parquet': b'data-session-1-without-episode-2' * 30,
            'meta/episodes/chunk-000/file-000.parquet': b'episodes-session-1-without-2' * 15,
            'videos/observation.images.scene/chunk-000/file-000.mp4': b'\x00video-1-without-2' * 150,
            'meta/info.json': INFO.replace('4', '6').encode(),
            'meta/stats.json': b'{"s": 6}'})
        if self.S.read_record(self.root):
            self.S.update_record(self.root, REPO, files=self.S.files_manifest(self.root))

    def no_record(self):
        self.assertFalse(self.S.record_path(self.root).exists(), 'no record may be written')

    # -- identical: remembered ------------------------------------------------------

    def test_an_identical_copy_gets_the_record_a_download_would_write(self):
        vf = self.legacy(BASE, SESSION2, display_name='Würfel legen')
        view = vf[0]
        self.assertEqual(self.S.decide(self.root, self.S.own_record(self.root, REPO), view)[0], 'current')
        self.assertEqual(self.remember(vf), 'written')
        rec = self.S.read_record(self.root)
        self.assertEqual(rec['repo_id'], REPO)
        self.assertEqual(rec['hub_sha'], view['head'])
        self.assertEqual(rec['hub_trees'], view['trees'])
        self.assertEqual(rec['local_digest'], self.S.meta_digest(self.root))
        self.assertEqual(rec['files'], self.S.files_manifest(self.root))
        self.assertEqual(set(rec['files']), {p for p in self.local_sync_files() if p.startswith(EPISODE_TOP)})
        self.assertTrue(rec['synced_at'])
        self.assertEqual((rec['display_name'], rec['private']), ('Würfel legen', True), 'the rest is kept')
        self.assertNotIn('tag_ok', rec)
        state, _, _ = self.S.decide(self.root, rec, self.view()[0])
        self.assertEqual(state, 'current')
        self.assertEqual(self.remember(self.view()), 'synced', 'a second time changes nothing')

    def test_then_a_deleted_episode_is_changed_and_one_upload_makes_the_hub_equal(self):
        vf = self.legacy(BASE, SESSION2)
        head = vf[0]['head']
        self.assertEqual(self.remember(vf), 'written')
        self.delete_an_episode()
        rec = self.S.read_record(self.root)
        self.assertEqual(self.S.decide(self.root, rec, self.view()[0])[0], 'changed')
        # the contrast: the same copy WITHOUT the record is a conflict (the old fallback)
        saved = self.S.record_path(self.root).read_bytes()
        self.S.record_path(self.root).unlink()
        self.assertEqual(self.S.decide(self.root, None, self.view()[0])[0], 'conflict')
        self.S.record_path(self.root).write_bytes(saved)
        n = len(self.audit('create_commit'))
        r = self.H.upload(self.root, REPO, expected=head, api=self.api)
        self.assertEqual((r['tag'], r['tag_ok']), ('ok', True))
        self.assertEqual(len(self.audit('create_commit')), n + 1, 'ONE commit')
        self.assertEqual(self.hub_sync_files(), self.local_sync_files(), 'the hub holds exactly what is left')
        self.assertEqual(self.hub_sync_files()['data/chunk-000/file-000.parquet'],
                         b'data-session-1-without-episode-2' * 30, 'the deleted episode is gone online too')
        self.assertEqual(self.S.decide(self.root, self.S.read_record(self.root), self.view()[0])[0], 'current')

    # -- never on a guess ---------------------------------------------------------

    def test_a_same_size_video_with_one_other_byte_is_never_remembered(self):
        vf = self.legacy()
        video = self.root / 'videos/observation.images.scene/chunk-000/file-000.mp4'
        data = bytearray(video.read_bytes())
        data[len(data) // 2] ^= 0xFF
        video.write_bytes(bytes(data))
        self.assertEqual(self.S.decide(self.root, None, vf[0])[0], 'current', 'the decision compares videos by size')
        self.assertEqual(self.remember(vf), 'differs')
        self.no_record()

    def test_a_same_size_meta_file_with_one_other_byte_is_never_remembered(self):
        vf = self.legacy()
        (self.root / 'meta/tasks.parquet').write_bytes(b'tasks-X')
        self.assertEqual(self.remember(vf), 'differs')
        self.no_record()

    def test_a_descendant_an_ancestor_or_another_file_set_is_never_remembered(self):
        vf = self.legacy(BASE)
        write_tree(self.root, SESSION2)                      # here = hub + a session
        self.assertEqual(self.remember(vf), 'differs')
        self.no_record()
        write_tree(self.root, {p: None for p in SESSION2 if p.startswith(EPISODE_TOP)},
                   {'meta/info.json': INFO.encode(), 'meta/stats.json': b'{"s": 1}'})
        self.other_pc(BASE, SESSION2)                        # hub = here + a session
        self.assertEqual(self.remember(self.view()), 'differs')
        self.no_record()
        (self.root / 'meta' / 'extra.json').write_text('{}')  # same hub, one more local file
        self.assertEqual(self.remember(self.view()), 'differs')
        self.no_record()

    # -- never for another repo, a crashed or a synced dataset ---------------------

    def test_only_the_folders_own_repo(self):
        vf = self.legacy()
        self.assertEqual(self.remember(vf, repo='lena-schmidt/omx_f_other'), 'foreign')
        self.no_record()

    def test_never_over_a_record_that_names_another_repo(self):
        vf = self.legacy()
        foreign = {'v': 1, 'repo_id': 'lena-schmidt/omx_f_other', 'hub_sha': '9' * 40}
        self.S.write_record(self.root, foreign)
        self.assertEqual(self.remember(vf), 'foreign')
        self.assertEqual(self.S.read_record(self.root), foreign, 'untouched')

    def test_never_with_a_crash_marker_and_not_one_file_read(self):
        vf = self.legacy()
        self.S.session_marker_path(self.root).write_text('{}')
        real, read = self.H._hash_file, []
        self.H._hash_file = lambda path, lfs: read.append(path) or real(path, lfs)
        try:
            self.assertEqual(self.remember(vf), 'in_session')
        finally:
            self.H._hash_file = real
        self.assertEqual(read, [], 'a session\'s dataset is never even read')
        self.no_record()

    def test_a_session_that_began_while_it_was_hashed_gets_no_record(self):
        vf = self.legacy()
        real = self.H.stage_lock

        def lock_after_a_start(root, wait_s=None):
            self.S.session_marker_path(root).write_text('{}')   # the recorder's first tick
            return real(root, wait_s=wait_s)
        self.H.stage_lock = lock_after_a_start
        try:
            self.assertEqual(self.remember(vf), 'in_session')
        finally:
            self.H.stage_lock = real
        self.no_record()

    def test_never_over_a_synced_record_or_an_unconfirmed_first_upload(self):
        vf = self.legacy()
        synced = {'v': 1, 'repo_id': REPO, 'hub_sha': '8' * 40, 'local_digest': 'x'}
        self.S.write_record(self.root, synced)
        self.assertEqual(self.remember(vf), 'synced')
        self.assertEqual(self.S.read_record(self.root), synced)
        unconfirmed = {'v': 1, 'repo_id': REPO, 'tag_ok': False}
        self.S.write_record(self.root, unconfirmed)
        self.assertEqual(self.remember(vf), 'synced')
        self.assertEqual(self.S.read_record(self.root), unconfirmed)

    def test_a_record_written_while_it_was_hashed_is_never_overwritten(self):
        """An upload (it takes no stage lock) that finished meanwhile wrote the
        record of its own commit: that record stands."""
        real = self.H.stage_lock
        for written in ({'v': 1, 'repo_id': REPO, 'hub_sha': '7' * 40, 'local_digest': 'u'},
                        {'v': 1, 'repo_id': 'lena-schmidt/omx_f_other'}):
            with self.subTest(record=written):
                self.tearDown_dataset_reset()
                vf = self.legacy()

                def lock_after_an_upload(root, wait_s=None, written=written):
                    self.S.write_record(root, written)
                    return real(root, wait_s=wait_s)
                self.H.stage_lock = lock_after_an_upload
                try:
                    self.assertIn(self.remember(vf), ('synced', 'foreign'))
                finally:
                    self.H.stage_lock = real
                self.assertEqual(self.S.read_record(self.root), written)

    def test_never_without_a_present_hub_view(self):
        self.legacy()
        for view in ({'state': 'absent', 'complete': True}, {'state': 'unreachable'}, None):
            self.assertEqual(self.H.remember_identical(self.root, REPO, view, {}), 'no_hub')
        self.no_record()

    # -- the lock, and a change while hashing --------------------------------------

    def test_a_held_stage_lock_is_never_waited_for(self):
        vf = self.legacy()
        fd = os.open(self.S.lock_path(self.root), os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        t0 = time.monotonic()
        self.assertEqual(self.remember(vf), 'busy')
        self.assertLess(time.monotonic() - t0, self.H.STAGE_LOCK_WAIT_S / 2, 'never waits for the lock')
        self.no_record()

    def test_a_lock_on_a_replaced_lock_file_is_never_taken_for_the_datasets(self):
        """A whole-dataset delete unlinks the lock file. A lock that lands on the
        old file while another stage holds the new one excludes nobody: the
        step must see the file at the path is not the one it locked, and
        refuse instead of writing."""
        vf = self.legacy()
        lock = pathlib.Path(self.S.lock_path(self.root))
        lock.write_text('')
        real_fcntl = self.H.fcntl
        held = []

        class Swapping:
            LOCK_EX, LOCK_NB = real_fcntl.LOCK_EX, real_fcntl.LOCK_NB

            @staticmethod
            def flock(fd, op):
                if not held:                                  # between our open and our flock:
                    lock.unlink()                             # the delete removed the file and
                    other = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
                    real_fcntl.flock(other, real_fcntl.LOCK_EX | real_fcntl.LOCK_NB)
                    held.append(other)                        # the next stage holds the new one
                return real_fcntl.flock(fd, op)
        self.H.fcntl = Swapping
        try:
            self.assertEqual(self.remember(vf), 'busy')
        finally:
            self.H.fcntl = real_fcntl
            for fd in held:
                os.close(fd)
        self.no_record()

    def test_a_stage_locks_the_file_at_the_path_not_an_unlinked_one(self):
        """Each attempt re-opens the lock file: a stage waiting while the file is
        replaced gets the lock of the file that is there now."""
        self.legacy()
        lock = pathlib.Path(self.S.lock_path(self.root))
        old = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, old)
        fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)        # held for good on the OLD file

        def replace():
            lock.unlink()
            lock.write_text('')
        threading.Timer(0.2, replace).start()
        fd = self.H.stage_lock(self.root)
        self.addCleanup(os.close, fd)
        self.assertEqual(os.fstat(fd).st_ino, os.stat(lock).st_ino)

    def test_a_dataset_that_changed_while_it_was_hashed_gets_no_record(self):
        vf = self.legacy()
        real = self.H.stage_lock
        for change in (lambda: (self.root / 'meta/info.json').write_bytes(INFO.replace('4', '3').encode()),
                       lambda: write_tree(self.root, {'data/chunk-000/file-009.parquet': b'new session'}),
                       lambda: os.utime(self.root / 'videos/observation.images.scene/chunk-000/file-000.mp4',
                                        ns=(1, 1))):
            with self.subTest(change=change):
                self.tearDown_dataset_reset()
                vf = self.legacy()

                def lock_after_a_change(root, wait_s=None, change=change):
                    change()                                    # an edit finished between hashing and the lock
                    return real(root, wait_s=wait_s)
                self.H.stage_lock = lock_after_a_change
                try:
                    self.assertEqual(self.remember(vf), 'changed')
                finally:
                    self.H.stage_lock = real
                self.no_record()

    def tearDown_dataset_reset(self):
        import shutil
        shutil.rmtree(self.ds_root, ignore_errors=True)

    # -- stages tolerate the step's short hold -------------------------------------

    def test_a_stage_waits_for_a_short_hold_and_still_refuses_a_long_one(self):
        self.legacy()
        fd = os.open(self.S.lock_path(self.root), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        threading.Timer(0.2, os.close, (fd,)).start()        # the remember step's write: a short hold
        t0 = time.monotonic()
        got = self.H.stage_lock(self.root)
        self.assertLess(time.monotonic() - t0, self.H.STAGE_LOCK_WAIT_S)
        self.H.release_lock(got)
        held = os.open(self.S.lock_path(self.root), os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)      # another stage: held for its whole run
        t0 = time.monotonic()
        with self.assertRaises(BlockingIOError):
            self.H.stage_lock(self.root)
        self.assertGreaterEqual(time.monotonic() - t0, self.H.STAGE_LOCK_WAIT_S * 0.9)
        with self.assertRaises(BlockingIOError):
            self.H.stage_lock(self.root, wait_s=0)


if __name__ == '__main__':
    import unittest
    unittest.main()
