"""data_processing/hf_token_store.py: the robot's one-slot, memory-only Hugging
Face token (042).

Plain ``unittest.TestCase`` so it runs under pytest (CI) and under
``python -m unittest`` alike. The module is stdlib-only, so nothing here needs
ROS. Fixtures are low-entropy and the log-scrubber tests prove the token never
reaches a handler stream.
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from physical_ai_server.data_processing import hf_token_store as store

TOK = 'hf_' + 'a' * 34
OTHER = 'hf_' + 'abcdefghijklmnopqrstuvwxyzABCDEFGH'


class _Slot(unittest.TestCase):
    """A tmpdir with a slot path inside a directory that does not exist yet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = os.path.join(self._tmp.name, 'edubotics-hf')
        self.path = os.path.join(self.dir, 'token')
        self.env = {store.PATH_ENV: self.path}


class TestAcceptsAndPath(unittest.TestCase):
    def test_matrix(self):
        self.assertFalse(store.accepts({}))
        self.assertFalse(store.accepts({store.PATH_ENV: ''}))
        self.assertFalse(store.accepts({store.PATH_ENV: '   '}))
        self.assertFalse(store.accepts({store.PATH_ENV: 'relative/token'}))
        self.assertFalse(store.accepts({store.PATH_ENV: 'token'}))
        self.assertTrue(store.accepts({store.PATH_ENV: '/run/edubotics-hf/token'}))
        self.assertEqual(store.token_path({store.PATH_ENV: ' /run/edubotics-hf/token '}),
                         '/run/edubotics-hf/token')
        self.assertIsNone(store.token_path({}))

    def test_default_reads_the_process_environment(self):
        with patch.dict(os.environ, {store.PATH_ENV: '/run/edubotics-hf/token'}):
            self.assertTrue(store.accepts())
            self.assertEqual(store.token_path(), '/run/edubotics-hf/token')
        with patch.dict(os.environ):
            os.environ.pop(store.PATH_ENV, None)
            self.assertFalse(store.accepts())


class TestShapeAndFingerprint(unittest.TestCase):
    def test_fingerprint_vectors(self):
        for token, fp in (
            ('hf_' + 'a' * 34, 'c1770a7966b0771e'),
            ('hf_' + 'abcdefghijklmnopqrstuvwxyzABCDEFGH', '006da5aabd66bbc9'),
            ('hf_0123456789_-0123456789', 'ce20c2c58e1061d5'),
            ('hf_' + 'Z' * 16, 'c287328277ed770f'),
            ('hf_' + 'x' * 256, '40d885025c3959d6'),
        ):
            self.assertEqual(store.fingerprint(token), fp)

    def test_shape_matrix(self):
        self.assertFalse(store.valid_shape('hf_' + 'a' * 15))
        self.assertTrue(store.valid_shape('hf_' + 'a' * 16))
        self.assertTrue(store.valid_shape('hf_' + 'a' * 256))
        self.assertFalse(store.valid_shape('hf_' + 'a' * 257))
        self.assertFalse(store.valid_shape(TOK + '\n'))
        self.assertFalse(store.valid_shape(' ' + TOK))
        self.assertFalse(store.valid_shape(TOK + ' '))
        for bad in (5, None, b'hf_' + b'a' * 34, [TOK], ''):
            self.assertFalse(store.valid_shape(bad))


class TestWriteReadClear(_Slot):
    def test_write_then_read_with_the_documented_modes(self):
        with patch.dict(os.environ, self.env):
            store.write(TOK)
            self.assertEqual(store.read(), TOK)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        with open(self.path, 'rb') as fh:
            self.assertEqual(fh.read(), TOK.encode())  # bare token, no newline
        self.assertEqual(os.listdir(self.dir), ['token'])  # no staging file left

    def test_a_second_write_replaces_atomically(self):
        store.write(TOK, self.path)
        store.write(OTHER, self.path)
        self.assertEqual(store.read(self.path), OTHER)
        self.assertEqual(os.listdir(self.dir), ['token'])

    def test_bad_shapes_raise_and_write_nothing(self):
        for bad in ('', 'hf_short', TOK + '\n', ' ' + TOK, 5, None):
            with self.assertRaises(ValueError):
                store.write(bad, self.path)
        self.assertFalse(os.path.exists(self.dir))

    def test_no_slot_means_no_write(self):
        with patch.dict(os.environ):
            os.environ.pop(store.PATH_ENV, None)
            with self.assertRaises(ValueError):
                store.write(TOK)
            self.assertIsNone(store.read())
            self.assertFalse(store.clear())

    def test_read_is_total(self):
        os.makedirs(self.dir)
        cases = {
            'trailing newline': (TOK + '\n').encode(),
            'crlf': (TOK + '\r\n').encode(),
            'surrounding spaces': ('  ' + TOK + '  ').encode(),
        }
        for label, raw in cases.items():
            with open(self.path, 'wb') as fh:
                fh.write(raw)
            self.assertEqual(store.read(self.path), TOK, label)
        for label, raw in {
            'empty': b'',
            'garbage': b'not a token',
            'short': b'hf_short',
            'non utf-8': b'\xff\xfe\xfd',
            'too long': b'hf_' + b'a' * 5000,
            'inner space': b'hf_aaaaaaaaaaaaaaaa bbbb',
        }.items():
            with open(self.path, 'wb') as fh:
                fh.write(raw)
            self.assertIsNone(store.read(self.path), label)
        # Line breaks are dropped exactly like huggingface_hub's own token
        # cleaning, so the fingerprint describes the token it would really send.
        with open(self.path, 'wb') as fh:
            fh.write((TOK + '\n' + OTHER).encode())
        self.assertEqual(store.read(self.path), TOK + OTHER)
        os.unlink(self.path)
        self.assertIsNone(store.read(self.path))  # missing
        os.mkdir(self.path)
        self.assertIsNone(store.read(self.path))  # a directory

    def test_clear(self):
        self.assertFalse(store.clear(self.path))  # nothing there is no error
        store.write(TOK, self.path)
        self.assertTrue(store.clear(self.path))
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(store.clear(self.path))


class TestStagingFile(_Slot):
    """Audit M2: a write killed between the fsync and the replace must not
    leave a token that a later clear forgets."""

    def _leave_stale_staging(self, content=OTHER):
        os.makedirs(self.dir, mode=0o700)
        stale = os.path.join(self.dir, store.TMP_NAME)
        with open(stale, 'w') as fh:
            fh.write(content)
        os.chmod(stale, 0o644)
        return stale

    def test_clear_removes_a_leftover_staging_file_and_still_answers_false(self):
        stale = self._leave_stale_staging()
        self.assertFalse(store.clear(self.path))  # no ACTIVE token existed
        self.assertFalse(os.path.exists(stale))

    def test_clear_removes_both_and_answers_true_when_the_slot_existed(self):
        stale = self._leave_stale_staging()
        with open(self.path, 'w') as fh:
            fh.write(TOK)
        self.assertTrue(store.clear(self.path))
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(self.path))

    def test_a_write_never_reuses_a_stale_staging_file(self):
        stale = self._leave_stale_staging()
        store.write(TOK, self.path)
        self.assertEqual(os.listdir(self.dir), ['token'])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertFalse(os.path.exists(stale))

    def test_a_failed_replace_leaves_no_staging_file_behind(self):
        with patch('os.replace', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                store.write(TOK, self.path)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertIsNone(store.read(self.path))


class TestStatePayload(_Slot):
    def test_an_image_without_a_slot_still_publishes(self):
        payload = store.state_payload(3, True, environ={})
        self.assertEqual(payload, {'v': 1, 'seq': 3, 'accepts': False, 'present': False,
                                   'fp': None, 'busy': True})
        self.assertEqual(list(payload), ['v', 'seq', 'accepts', 'present', 'fp', 'busy'])

    def test_an_empty_slot(self):
        payload = store.state_payload(1, False, environ=self.env)
        self.assertEqual(payload, {'v': 1, 'seq': 1, 'accepts': True, 'present': False,
                                   'fp': None, 'busy': False})

    def test_a_present_slot_and_the_exact_encoded_string(self):
        store.write(TOK, self.path)
        payload = store.state_payload(2, False, environ=self.env)
        self.assertEqual(payload['fp'], 'c1770a7966b0771e')
        self.assertEqual(
            store.encode_payload(payload),
            '{"v":1,"seq":2,"accepts":true,"present":true,"fp":"c1770a7966b0771e","busy":false}')

    def test_the_state_is_derived_from_the_file_so_a_respawn_sees_the_same(self):
        store.write(TOK, self.path)
        first = store.state_payload(1, False, environ=self.env)
        second = store.state_payload(1, False, environ=self.env)  # "a fresh node"
        self.assertEqual(first, second)
        store.clear(self.path)
        self.assertFalse(store.state_payload(2, False, environ=self.env)['present'])

    def test_the_token_itself_is_never_in_the_payload(self):
        store.write(TOK, self.path)
        text = store.encode_payload(store.state_payload(1, False, environ=self.env))
        self.assertNotIn(TOK, text)
        self.assertNotIn('hf_', text)

    def test_a_garbage_file_is_not_present(self):
        os.makedirs(self.dir)
        with open(self.path, 'w') as fh:
            fh.write('garbage')
        self.assertFalse(store.state_payload(1, False, environ=self.env)['present'])

    def test_compact_json(self):
        text = store.encode_payload(store.state_payload(1, False, environ={}))
        self.assertNotIn(', ', text)
        self.assertNotIn(': ', text)
        json.loads(text)


class TestPurgeLegacy(_Slot):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self._tmp.name, 'hfhome')
        os.makedirs(os.path.join(self.home, 'lerobot'))
        os.makedirs(os.path.join(self.home, 'stored_tokens_keep'))
        for name in ('token', 'stored_tokens', 'keepme.txt'):
            with open(os.path.join(self.home, name), 'w') as fh:
                fh.write('x')

    def test_no_op_on_an_image_without_a_slot(self):
        self.assertEqual(store.purge_legacy(environ={'HF_HOME': self.home}), [])
        self.assertEqual(sorted(os.listdir(self.home)),
                         ['keepme.txt', 'lerobot', 'stored_tokens', 'stored_tokens_keep', 'token'])

    def test_removes_exactly_the_two_legacy_files_and_is_idempotent(self):
        env = {**self.env, 'HF_HOME': self.home}
        removed = store.purge_legacy(environ=env)
        self.assertEqual(sorted(os.path.basename(p) for p in removed), ['stored_tokens', 'token'])
        self.assertEqual(sorted(os.listdir(self.home)),
                         ['keepme.txt', 'lerobot', 'stored_tokens_keep'])
        self.assertEqual(store.purge_legacy(environ=env), [])

    def test_the_default_base_is_the_home_cache(self):
        base = os.path.join(self._tmp.name, 'fakehome', '.cache', 'huggingface')
        os.makedirs(base)
        with open(os.path.join(base, 'token'), 'w') as fh:
            fh.write('x')
        removed = store.purge_legacy(environ=self.env, home=os.path.join(self._tmp.name, 'fakehome'))
        self.assertEqual(removed, [os.path.join(base, 'token')])

    def test_it_never_deletes_the_live_slot_even_when_hf_home_points_at_it(self):
        slot_dir = os.path.join(self._tmp.name, 'slot')
        slot = os.path.join(slot_dir, 'token')
        store.write(TOK, slot)
        removed = store.purge_legacy(environ={store.PATH_ENV: slot, 'HF_HOME': slot_dir})
        self.assertNotIn(slot, removed)
        self.assertEqual(store.read(slot), TOK)

    def test_it_never_reads_or_returns_content(self):
        removed = store.purge_legacy(environ={**self.env, 'HF_HOME': self.home})
        self.assertTrue(all(isinstance(p, str) for p in removed))


def _scrubbers(target):
    return [f for f in target.filters if isinstance(f, store.TokenScrubber)]


class TestLogScrubber(unittest.TestCase):
    """The token must not reach any handler stream, however it is logged."""

    def setUp(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter('%(name)s %(message)s'))
        self.root = logging.getLogger()
        self._root_level = self.root.level
        self.root.setLevel(logging.DEBUG)
        self.root.addHandler(self.handler)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self._root_level)
        for holder in [self.root] + [logging.getLogger(n) for n in store.SCRUBBED_LOGGERS]:
            for f in _scrubbers(holder):
                holder.removeFilter(f)
            for h in holder.handlers:
                for f in _scrubbers(h):
                    h.removeFilter(f)

    def out(self):
        return self.stream.getvalue()

    def test_install_is_idempotent_with_one_filter_per_named_logger(self):
        store.install_log_scrubber()
        store.install_log_scrubber()
        for name in store.SCRUBBED_LOGGERS:
            self.assertEqual(len(_scrubbers(logging.getLogger(name))), 1, name)
        self.assertEqual(len(_scrubbers(self.handler)), 1)

    def test_the_named_loggers_are_the_hub_and_http_stack(self):
        for name in ('huggingface_hub', 'huggingface_hub.utils._http', 'httpx', 'httpcore',
                     'urllib3', 'requests', 'huggingface_hub._upload_large_folder'):
            self.assertIn(name, store.SCRUBBED_LOGGERS)

    def test_message_and_tuple_args(self):
        store.install_log_scrubber()
        log = logging.getLogger('huggingface_hub.utils._http')
        log.error('Authorization failed for %s', TOK)
        log.error('request with ' + TOK + ' failed')
        self.assertNotIn(TOK, self.out())
        self.assertEqual(self.out().count('hf_***'), 2)

    def test_dict_args(self):
        store.install_log_scrubber()
        logging.getLogger('httpx').error('%(who)s sent %(secret)s', {'who': 'robot', 'secret': TOK})
        self.assertNotIn(TOK, self.out())
        self.assertIn('robot sent hf_***', self.out())

    def test_a_child_logger_outside_the_named_list_is_covered_by_the_handler_filter(self):
        store.install_log_scrubber()
        logging.getLogger('huggingface_hub.lfs').error('lfs upload with %s', TOK)
        logging.getLogger('some.other.module').error('oops ' + TOK)
        self.assertNotIn(TOK, self.out())

    def test_an_exception_traceback_is_scrubbed(self):
        store.install_log_scrubber()
        log = logging.getLogger('huggingface_hub.hf_api')
        try:
            raise ValueError('bad ' + TOK)
        except ValueError:
            log.exception('upload failed')
        self.assertNotIn(TOK, self.out())
        self.assertIn('ValueError', self.out())
        self.assertIn('hf_***', self.out())

    def test_an_exception_object_as_a_format_argument_is_scrubbed(self):
        store.install_log_scrubber()
        logging.getLogger('requests').error('failed: %s', RuntimeError('token ' + TOK))
        self.assertNotIn(TOK, self.out())
        self.assertIn('failed: token hf_***', self.out())

    def test_an_exception_object_as_the_message_is_scrubbed(self):
        store.install_log_scrubber()
        logging.getLogger('urllib3').error(RuntimeError('token ' + TOK))
        self.assertNotIn(TOK, self.out())

    def test_stack_info_is_scrubbed(self):
        store.install_log_scrubber()
        record = logging.LogRecord('huggingface_hub', logging.ERROR, __file__, 1,
                                   'msg', None, None)
        record.stack_info = 'Stack (most recent call last):\n  x = "' + TOK + '"'
        store.TokenScrubber().filter(record)
        self.assertNotIn(TOK, record.stack_info)

    def test_a_handler_created_after_the_install_needs_a_second_call(self):
        store.install_log_scrubber()
        late_stream = io.StringIO()
        late = logging.StreamHandler(late_stream)
        # First in line, so no earlier (filtered) handler has scrubbed the shared
        # record before this one sees it.
        self.root.handlers.insert(0, late)
        self.addCleanup(self.root.removeHandler, late)
        # A logger outside the huggingface_hub tree: that library installs its own
        # handler on its root logger, which would scrub the shared record first.
        probe = logging.getLogger('late.handler.probe')
        probe.error('first ' + TOK)
        self.assertIn(TOK, late_stream.getvalue())  # the documented gap ...
        store.install_log_scrubber()  # ... which the node closes with a second call
        probe.error('second ' + TOK)
        self.assertNotIn('second ' + TOK, late_stream.getvalue())
        self.assertIn('second hf_***', late_stream.getvalue())

    def test_innocent_records_keep_their_own_formatting(self):
        store.install_log_scrubber()

        class Thing:
            def __str__(self):
                return 'a thing'

        thing = Thing()
        record = logging.LogRecord('httpx', logging.INFO, __file__, 1, '%d items, %s, %s',
                                   (3, 'ok', thing), None)
        self.assertTrue(store.TokenScrubber().filter(record))
        self.assertEqual(record.args[0], 3)
        self.assertIs(record.args[2], thing)  # untouched: no token in its text
        self.assertEqual(record.getMessage(), '3 items, ok, a thing')

    def test_the_filter_never_raises_and_never_drops_a_record(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError('no')

        record = logging.LogRecord('httpx', logging.INFO, __file__, 1, '%s', (Hostile(),), None)
        self.assertTrue(store.TokenScrubber().filter(record))
        record = logging.LogRecord('httpx', logging.INFO, __file__, 1, 'x', None, None)
        record.args = 5  # not a tuple or dict
        self.assertTrue(store.TokenScrubber().filter(record))

    def test_the_pattern_needs_at_least_sixteen_characters(self):
        self.assertEqual(store.TOKEN_IN_TEXT.sub('X', 'a hf_' + 'b' * 16 + ' z'), 'a X z')
        self.assertEqual(store.TOKEN_IN_TEXT.sub('X', 'hf_' + 'b' * 15), 'hf_' + 'b' * 15)


class TestModuleHygiene(unittest.TestCase):
    def test_stdlib_only_and_no_environment_variable_names_of_ours(self):
        import ast
        import pathlib

        path = pathlib.Path(store.__file__)
        tree = ast.parse(path.read_text(encoding='utf-8'))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
        self.assertLessEqual(imported, {'__future__', 'json', 'logging', 'hashlib', 'os', 're',
                                        'typing'})
        # env-forwarding-guard greps every EDUBOTICS_* name in this tree and demands a
        # compose forward; the token slot must not introduce one.
        import re
        self.assertEqual(re.findall(r'EDUBOTICS_[A-Z0-9_]+', path.read_text(encoding='utf-8')), [])


if __name__ == '__main__':
    unittest.main()
