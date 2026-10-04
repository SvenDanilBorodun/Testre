"""The per-student token slot in the node (042, spec B4).

``physical_ai_server.py`` imports rclpy and cannot be imported here, so the
methods are extracted by ``ast`` and exec'd onto stub nodes, exactly like
``test_signal_status_node_wiring.py`` does; the real ``hf_token_store`` and
``record_texts_de`` run underneath. Plain ``unittest.TestCase``.

Covers: /register_hf_user semantics and order (unsupported, no-op while busy,
busy refusal, clear, shape, write, failure), the state publisher / timer wiring,
the AST fences that keep the token away from loggers, subprocesses and the old
``huggingface-cli`` path, and the ``__init__`` ordering the other node tests pin.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import textwrap
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from physical_ai_server.data_processing import hf_token_store as store
from physical_ai_server.data_processing import record_texts_de as texts

_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_SERVER_PY = _PKG / 'physical_ai_server.py'
_WORKER_PY = _PKG / 'data_processing' / 'hf_api_worker.py'
_SRC = _SERVER_PY.read_text(encoding='utf-8')
_TREE = ast.parse(_SRC)

TOK = 'hf_' + 'a' * 34
OTHER = 'hf_' + 'abcdefghijklmnopqrstuvwxyzABCDEFGH'


# ── stand-ins ────────────────────────────────────────────────────────────────

class _Clock:
    t = 100.0

    @classmethod
    def monotonic(cls):
        return cls.t


class _String:
    def __init__(self):
        self.data = ''


class _Group:
    instances = 0

    def __init__(self):
        type(self).instances += 1


def _qos(**kw):
    return dict(kw)


class _DataManager:
    invalidations = 0

    @classmethod
    def invalidate_hf_namespace_cache(cls):
        cls.invalidations += 1


_ENUMS = dict(
    ReliabilityPolicy=types.SimpleNamespace(RELIABLE='RELIABLE', BEST_EFFORT='BEST_EFFORT'),
    DurabilityPolicy=types.SimpleNamespace(TRANSIENT_LOCAL='TRANSIENT_LOCAL', VOLATILE='VOLATILE'),
)


def _function_node(name):
    cls = next(n for n in _TREE.body
               if isinstance(n, ast.ClassDef) and n.name == 'PhysicalAIServer')
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _load(name, store_module=store):
    node = _function_node(name)
    src = textwrap.dedent(ast.get_source_segment(_SRC, node))
    ns = {
        'os': os, 'time': _Clock, 'threading': threading,
        'String': _String, 'MutuallyExclusiveCallbackGroup': _Group, 'QoSProfile': _qos,
        'hf_token_store': store_module, 'record_texts_de': texts, 'DataManager': _DataManager,
        **_ENUMS,
    }
    exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    return ns[name]


_METHODS = {n: _load(n) for n in (
    '_init_hf_token_state', '_hf_token_busy', '_publish_hf_token_state',
    '_hf_token_state_tick', '_apply_pending_hf_token_clear', '_apply_hf_token_request',
    'set_hf_user_callback')}


class _Worker:
    def __init__(self, *, alive=True, busy=False, raises=False):
        self.alive, self.busy, self.raises = alive, busy, raises

    def is_alive(self):
        if self.raises:
            raise RuntimeError('worker exploded')
        return self.alive

    def is_busy(self):
        if self.raises:
            raise RuntimeError('worker exploded')
        return self.busy


class _Logger:
    def __init__(self):
        self.lines = []

    def _log(self, text):
        self.lines.append(str(text))

    info = warning = error = debug = _log


class _Node:
    """A stub carrying the attributes `__init__` would set, with the six real
    methods bound onto it."""

    def __init__(self, *, worker=None, recording=False):
        self.on_recording = recording
        self.hf_api_worker = worker
        self._hf_token_lock = threading.Lock()
        self._hf_token_seq = 0
        self._hf_token_state_pub = None
        self._hf_token_last_error_log = None
        self._hf_token_clear_pending = False
        self.log = _Logger()
        self.publishers = []
        self.timers = []
        for name, fn in _METHODS.items():
            setattr(self, name, types.MethodType(fn, self))

    def get_logger(self):
        return self.log

    def create_publisher(self, msg_type, topic, qos):
        pub = types.SimpleNamespace(msg_type=msg_type, topic=topic, qos=qos, sent=[])
        pub.publish = pub.sent.append
        self.publishers.append(pub)
        return pub

    def create_timer(self, period, callback, callback_group=None):
        timer = types.SimpleNamespace(period=period, callback=callback, group=callback_group)
        self.timers.append(timer)
        return timer

    def request(self, token):
        response = types.SimpleNamespace(user_id_list=['stale'], success=None, message=None)
        return self.set_hf_user_callback(types.SimpleNamespace(token=token), response)

    def states(self):
        return [json.loads(m.data) for m in self._hf_token_state_pub.sent]


class _Base(unittest.TestCase):
    def setUp(self):
        _Clock.t = 100.0
        _DataManager.invalidations = 0
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.slot = os.path.join(self._tmp.name, 'edubotics-hf', 'token')
        patcher = patch.dict(os.environ, {store.PATH_ENV: self.slot})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'):
            os.environ.pop(name, None)
        # `_init_hf_token_state` re-installs the log scrubber; the real one would
        # attach a filter to the process-wide root and huggingface_hub handlers
        # and leak it into every later test in the same pytest process.
        scrubber = patch.object(store, 'install_log_scrubber')
        self.install_scrubber = scrubber.start()
        self.addCleanup(scrubber.stop)

    def node(self, **kw):
        node = _Node(**kw)
        node._hf_token_state_pub = types.SimpleNamespace(sent=[])
        node._hf_token_state_pub.publish = node._hf_token_state_pub.sent.append
        return node

    def stored(self):
        return store.read(self.slot)


# ── /register_hf_user ────────────────────────────────────────────────────────

class TestRegisterHfUser(_Base):
    def test_an_image_without_a_slot_refuses_and_touches_no_file(self):
        os.environ.pop(store.PATH_ENV)
        node = self.node()
        response = node.request(TOK)
        self.assertFalse(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_UNSUPPORTED_DE)
        self.assertEqual(response.user_id_list, [])
        self.assertFalse(os.path.exists(os.path.dirname(self.slot)))
        self.assertEqual(_DataManager.invalidations, 0)

    def test_a_valid_token_is_written_published_and_the_cache_invalidated_once(self):
        node = self.node()
        response = node.request(TOK)
        self.assertTrue(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_SET_OK_DE)
        self.assertEqual(response.user_id_list, [])
        self.assertEqual(self.stored(), TOK)
        self.assertEqual(_DataManager.invalidations, 1)
        (state,) = node.states()  # an immediate republish, not the 1 Hz timer
        self.assertEqual((state['accepts'], state['present'], state['fp'], state['busy']),
                         (True, True, 'c1770a7966b0771e', False))

    def test_the_same_token_while_busy_is_a_no_op_success(self):
        node = self.node()
        node.request(TOK)
        _DataManager.invalidations = 0
        node.on_recording = True
        with patch.object(store, 'write') as write:
            response = node.request(TOK)
        write.assert_not_called()
        self.assertTrue(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_SET_OK_DE)
        self.assertEqual(_DataManager.invalidations, 0)
        self.assertEqual(self.stored(), TOK)

    def test_another_token_while_recording_is_refused_and_the_file_is_unchanged(self):
        node = self.node()
        node.request(TOK)
        node.on_recording = True
        response = node.request(OTHER)
        self.assertFalse(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_BUSY_DE)
        self.assertEqual(self.stored(), TOK)

    def test_another_token_while_the_hf_worker_is_busy_is_refused(self):
        node = self.node(worker=_Worker(busy=True))
        store.write(TOK, self.slot)
        response = node.request(OTHER)
        self.assertFalse(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_BUSY_DE)
        self.assertEqual(self.stored(), TOK)

    def test_a_missing_dead_or_unaskable_worker_is_not_busy(self):
        for worker in (None, _Worker(alive=False, busy=True), _Worker(raises=True)):
            node = self.node(worker=worker)
            self.assertFalse(node._hf_token_busy(), repr(worker))
            response = node.request(OTHER)
            self.assertTrue(response.success, repr(worker))
            self.assertEqual(self.stored(), OTHER)
            store.clear(self.slot)

    def test_an_idle_live_worker_is_not_busy_and_a_recording_is(self):
        self.assertFalse(self.node(worker=_Worker(alive=True, busy=False))._hf_token_busy())
        self.assertTrue(self.node(recording=True)._hf_token_busy())
        self.assertTrue(self.node(recording=True, worker=_Worker(raises=True))._hf_token_busy())

    def test_an_empty_token_clears_and_republishes(self):
        node = self.node()
        node.request(TOK)
        _DataManager.invalidations = 0
        response = node.request('')
        self.assertTrue(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_CLEARED_DE)
        self.assertFalse(os.path.exists(self.slot))
        self.assertEqual(_DataManager.invalidations, 1)
        self.assertFalse(node.states()[-1]['present'])
        self.assertIsNone(node.states()[-1]['fp'])

    def test_clearing_while_busy_is_refused_and_remembered(self):
        node = self.node()
        node.request(TOK)
        node.on_recording = True
        response = node.request('')
        self.assertFalse(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_CLEAR_QUEUED_DE)
        self.assertEqual(self.stored(), TOK)
        self.assertTrue(node._hf_token_clear_pending)

    def test_empty_with_nothing_stored_is_success_even_when_busy(self):
        for busy in (False, True):
            node = self.node(recording=busy)
            response = node.request('')
            self.assertTrue(response.success)
            self.assertEqual(response.message, texts.HF_TOKEN_NONE_DE)
        self.assertEqual(_DataManager.invalidations, 0)

    def test_an_empty_request_sweeps_a_garbage_file_but_still_says_none(self):
        os.makedirs(os.path.dirname(self.slot))
        with open(self.slot, 'w') as fh:
            fh.write('garbage')
        node = self.node(recording=True)
        response = node.request('')
        self.assertTrue(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_NONE_DE)
        self.assertFalse(os.path.exists(self.slot))

    def test_a_bad_shape_is_refused_and_writes_nothing(self):
        node = self.node()
        for bad in ('hf_short', TOK + '\n', ' ' + TOK, 'not a token', 'hf_' + 'a' * 257):
            response = node.request(bad)
            self.assertFalse(response.success, bad)
            self.assertEqual(response.message, texts.HF_TOKEN_SHAPE_DE)
        self.assertFalse(os.path.exists(os.path.dirname(self.slot)))
        self.assertEqual(_DataManager.invalidations, 0)

    def test_busy_is_judged_before_the_shape(self):
        node = self.node(recording=True)
        self.assertEqual(node.request('hf_short').message, texts.HF_TOKEN_BUSY_DE)

    def test_a_token_that_is_not_a_string_counts_as_empty(self):
        for odd in (None, 5, b'hf_' + b'a' * 34, ['x']):
            node = self.node()
            response = node.request(odd)
            self.assertTrue(response.success, repr(odd))
            self.assertEqual(response.message, texts.HF_TOKEN_NONE_DE)
        node = self.node()
        node.request(TOK)
        self.assertEqual(node.request(None).message, texts.HF_TOKEN_CLEARED_DE)
        self.assertIsNone(self.stored())

    def test_a_write_failure_is_german_and_the_log_names_only_the_class(self):
        node = self.node()
        with patch.object(store, 'write', side_effect=OSError('disk exploded for ' + TOK)):
            response = node.request(TOK)
        self.assertFalse(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_WRITE_FAILED_DE)
        self.assertEqual(response.user_id_list, [])
        text = '\n'.join(node.log.lines)
        self.assertIn('OSError', text)
        self.assertNotIn(TOK, text)
        self.assertNotIn('disk exploded', text)
        self.assertNotIn(store.fingerprint(TOK), text)
        self.assertEqual(_DataManager.invalidations, 0)

    def test_an_unexpected_exception_never_escapes_the_service_callback(self):
        node = self.node()
        with patch.object(store, 'accepts', side_effect=RuntimeError('x ' + TOK)):
            response = node.request(TOK)
        self.assertFalse(response.success)
        self.assertEqual(response.message, texts.HF_TOKEN_WRITE_FAILED_DE)
        self.assertNotIn(TOK, '\n'.join(node.log.lines))

    def test_user_id_list_is_always_empty(self):
        node = self.node()
        for token in (TOK, '', 'hf_short', OTHER):
            self.assertEqual(node.request(token).user_id_list, [])

    def test_nothing_the_node_logs_contains_a_token_or_fingerprint(self):
        node = self.node()
        for token in (TOK, OTHER, '', 'hf_short'):
            node.request(token)
        text = '\n'.join(node.log.lines)
        for secret in (TOK, OTHER, store.fingerprint(TOK), store.fingerprint(OTHER)):
            self.assertNotIn(secret, text)

    def test_concurrent_requests_leave_one_consistent_slot(self):
        node = self.node()
        tokens = [f'hf_{i:016d}' for i in range(8)]
        threads = [threading.Thread(target=node.request, args=(t,)) for t in tokens]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn(self.stored(), tokens)
        self.assertEqual(os.listdir(os.path.dirname(self.slot)), ['token'])


# ── a clear refused while busy (review a, 2026-10-04) ────────────────────────

class TestPendingClear(_Base):
    def busy_node_with_a_refused_clear(self, **kw):
        node = self.node(**kw)
        node.request(TOK)
        node.on_recording = True
        self.assertFalse(node.request('').success)
        return node

    def test_the_tick_applies_it_on_the_busy_to_idle_edge(self):
        node = self.busy_node_with_a_refused_clear()
        node._hf_token_state_tick()                  # still recording: nothing happens
        self.assertEqual(self.stored(), TOK)
        self.assertTrue(node._hf_token_clear_pending)
        self.assertTrue(node.states()[-1]['present'])
        node.on_recording = False
        _DataManager.invalidations = 0
        node._hf_token_state_tick()                  # idle: the clear lands
        self.assertIsNone(self.stored())
        self.assertFalse(os.path.exists(self.slot))
        self.assertFalse(node._hf_token_clear_pending)
        self.assertEqual(_DataManager.invalidations, 1)
        self.assertFalse(node.states()[-1]['present'])
        self.assertIsNone(node.states()[-1]['fp'])
        self.assertIn('waited for the robot to be idle', '\n'.join(node.log.lines))

    def test_a_busy_hf_worker_holds_it_too(self):
        worker = _Worker(busy=True)
        node = self.node(worker=worker)
        store.write(TOK, self.slot)
        self.assertEqual(node.request('').message, texts.HF_TOKEN_CLEAR_QUEUED_DE)
        node._hf_token_state_tick()
        self.assertEqual(self.stored(), TOK)
        worker.busy = False
        node._hf_token_state_tick()
        self.assertIsNone(self.stored())

    def test_a_newer_set_cancels_the_pending_clear(self):
        node = self.busy_node_with_a_refused_clear()
        node.on_recording = False
        self.assertTrue(node.request(OTHER).success)  # the next student's push
        self.assertFalse(node._hf_token_clear_pending)
        node._hf_token_state_tick()
        self.assertEqual(self.stored(), OTHER)

    def test_a_set_refused_while_busy_keeps_it(self):
        # A refused set wrote nothing, so the previous student's token is
        # still in the slot: the clear must still land once the robot is idle
        # (the SPA pushes the newer token again afterwards).
        node = self.busy_node_with_a_refused_clear()
        self.assertEqual(node.request(OTHER).message, texts.HF_TOKEN_BUSY_DE)
        self.assertTrue(node._hf_token_clear_pending)
        node.on_recording = False
        node._hf_token_state_tick()
        self.assertIsNone(self.stored())
        self.assertFalse(node._hf_token_clear_pending)

    def test_a_set_refused_for_its_shape_keeps_it(self):
        node = self.busy_node_with_a_refused_clear()
        node.on_recording = False                    # idle, the tick has not run yet
        self.assertEqual(node.request('hf_short').message, texts.HF_TOKEN_SHAPE_DE)
        self.assertTrue(node._hf_token_clear_pending)
        self.assertEqual(self.stored(), TOK)
        node._hf_token_state_tick()
        self.assertIsNone(self.stored())

    def test_a_set_whose_write_fails_keeps_it(self):
        node = self.busy_node_with_a_refused_clear()
        node.on_recording = False
        with patch.object(store, 'write', side_effect=OSError('tmpfs full')):
            response = node.request(OTHER)
        self.assertEqual(response.message, texts.HF_TOKEN_WRITE_FAILED_DE)
        self.assertTrue(node._hf_token_clear_pending)
        node._hf_token_state_tick()
        self.assertIsNone(self.stored())
        self.assertFalse(node._hf_token_clear_pending)

    def test_a_robot_that_turns_busy_again_inside_the_tick_keeps_it(self):
        # The tick's own busy check passes, then a recording starts before the
        # request helper judges busy again: the helper re-queues the clear, and
        # the tick must not forget it.
        node = self.busy_node_with_a_refused_clear()
        node.on_recording = False
        answers = iter([False, False, True])
        node._hf_token_busy = lambda: next(answers, True)
        node._hf_token_state_tick()
        self.assertEqual(self.stored(), TOK)
        self.assertTrue(node._hf_token_clear_pending)

    def test_the_same_token_again_cancels_it(self):
        node = self.busy_node_with_a_refused_clear()
        self.assertTrue(node.request(TOK).success)    # no-op success, even while busy
        self.assertFalse(node._hf_token_clear_pending)
        node.on_recording = False
        node._hf_token_state_tick()
        self.assertEqual(self.stored(), TOK)

    def test_an_explicit_clear_after_idle_settles_it(self):
        node = self.busy_node_with_a_refused_clear()
        node.on_recording = False
        self.assertTrue(node.request('').success)
        self.assertFalse(node._hf_token_clear_pending)

    def test_a_failing_clear_is_dropped_with_one_class_only_log_line(self):
        node = self.busy_node_with_a_refused_clear()
        node.on_recording = False
        with patch.object(store, 'clear', side_effect=OSError('tmpfs gone ' + TOK)):
            node._hf_token_state_tick()
            node._hf_token_state_tick()
        self.assertFalse(node._hf_token_clear_pending)
        text = '\n'.join(node.log.lines)
        self.assertEqual(text.count('OSError'), 1)
        self.assertNotIn(TOK, text)

    def test_a_held_lock_defers_it_to_the_next_tick(self):
        node = self.busy_node_with_a_refused_clear()
        node.on_recording = False
        real = node._hf_token_lock
        waits = []

        class _Held:
            """The service callback holds the lock: acquire times out."""

            def acquire(self, timeout=None):
                waits.append(timeout)
                return False

            def release(self):
                raise AssertionError('released a lock it never got')

        node._hf_token_lock = _Held()
        node._hf_token_state_tick()
        self.assertEqual(waits, [0.5])                 # bounded, never a blocking wait
        self.assertEqual(self.stored(), TOK)
        self.assertTrue(node._hf_token_clear_pending)
        node._hf_token_lock = real
        node._hf_token_state_tick()
        self.assertIsNone(self.stored())

    def test_no_pending_clear_means_the_tick_never_touches_the_slot(self):
        node = self.node()
        node.request(TOK)
        with patch.object(store, 'clear') as clear:
            node._hf_token_state_tick()
        clear.assert_not_called()
        self.assertEqual(self.stored(), TOK)


# ── the state topic ──────────────────────────────────────────────────────────

class TestStateTopic(_Base):
    def test_the_tick_publishes_schema_v1(self):
        node = self.node()
        store.write(TOK, self.slot)
        node._hf_token_state_tick()
        node._hf_token_state_tick()
        first, second = node.states()
        self.assertEqual(second['seq'], first['seq'] + 1)
        self.assertEqual(list(first), ['v', 'seq', 'accepts', 'present', 'fp', 'busy'])
        self.assertEqual((first['v'], first['accepts'], first['present'], first['fp'],
                          first['busy']), (1, True, True, 'c1770a7966b0771e', False))
        self.assertNotIn(TOK, node._hf_token_state_pub.sent[0].data)

    def test_the_busy_flag_follows_the_recorder(self):
        node = self.node()
        node._hf_token_state_tick()
        node.on_recording = True
        node._hf_token_state_tick()
        self.assertEqual([s['busy'] for s in node.states()], [False, True])

    def test_an_image_without_a_slot_still_publishes_accepts_false(self):
        os.environ.pop(store.PATH_ENV)
        node = self.node()
        node._hf_token_state_tick()
        (state,) = node.states()
        self.assertEqual((state['accepts'], state['present'], state['fp']), (False, False, None))

    def test_no_publisher_yet_means_no_publish(self):
        node = self.node()
        node._hf_token_state_pub = None
        node._hf_token_state_tick()  # must not raise
        self.assertEqual(node.log.lines, [])

    def test_the_tick_never_raises_and_logs_at_most_every_30_s_by_class(self):
        node = self.node()

        def boom(_msg):
            raise RuntimeError('publish exploded ' + TOK)

        node._hf_token_state_pub.publish = boom
        node._hf_token_state_tick()
        _Clock.t += 10.0
        node._hf_token_state_tick()
        self.assertEqual(len(node.log.lines), 1)
        _Clock.t += 25.0
        node._hf_token_state_tick()
        self.assertEqual(len(node.log.lines), 2)
        text = '\n'.join(node.log.lines)
        self.assertIn('RuntimeError', text)
        self.assertNotIn(TOK, text)
        self.assertNotIn('publish exploded', text)

    def test_init_creates_the_latched_publisher_and_a_one_hertz_timer_on_its_own_group(self):
        node = _Node()
        _Group.instances = 0
        with patch.object(store, 'purge_legacy', return_value=[]) as purge:
            node._init_hf_token_state()
        install = self.install_scrubber
        (pub,) = node.publishers
        self.assertIs(pub.msg_type, _String)
        self.assertEqual(pub.topic, '/edubotics/hf_token_state')
        self.assertEqual(pub.topic, store.STATE_TOPIC)
        self.assertEqual(pub.qos, {'depth': 1, 'reliability': 'RELIABLE',
                                   'durability': 'TRANSIENT_LOCAL'})
        self.assertIs(node._hf_token_state_pub, pub)
        (timer,) = node.timers
        self.assertEqual(timer.period, 1.0)
        self.assertEqual(timer.period, store.PUBLISH_PERIOD_S)
        self.assertEqual(timer.callback, node._hf_token_state_tick)
        self.assertIsInstance(timer.group, _Group)
        self.assertIs(node._hf_state_group, timer.group)
        self.assertEqual(_Group.instances, 1)
        purge.assert_called_once_with()
        install.assert_called_once_with()  # again: HfApiWorker.__init__ ran basicConfig

    def test_init_purges_the_legacy_token_files_and_logs_only_a_count(self):
        node = _Node()
        with patch.object(store, 'purge_legacy', return_value=['/root/.cache/huggingface/token']):
            node._init_hf_token_state()
        text = '\n'.join(node.log.lines)
        self.assertIn('Removed 1 legacy', text)
        self.assertNotIn('/root/', text)

    def test_init_warns_when_an_environment_token_would_outrank_the_slot(self):
        for var in ('HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN'):
            node = _Node()
            with patch.dict(os.environ, {var: 'whatever-' + 'z' * 5}):
                node._init_hf_token_state()
            text = '\n'.join(node.log.lines)
            self.assertIn('outranks the personal token file', text)
            self.assertNotIn('whatever', text)
        node = _Node()
        node._init_hf_token_state()
        self.assertNotIn('outranks', '\n'.join(node.log.lines))

    def test_init_does_not_warn_on_an_image_without_a_slot(self):
        os.environ.pop(store.PATH_ENV)
        node = _Node()
        with patch.dict(os.environ, {'HF_TOKEN': 'whatever-' + 'z' * 5}):
            node._init_hf_token_state()
        self.assertNotIn('outranks', '\n'.join(node.log.lines))


# ── AST fences ───────────────────────────────────────────────────────────────

def _parents(tree):
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _logger_calls(fn):
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {'info', 'warning', 'warn', 'error', 'debug'}):
            yield node


class TestAstFences(unittest.TestCase):
    def test_the_old_login_path_is_gone_from_the_callback_and_its_helper(self):
        for name in ('set_hf_user_callback', '_apply_hf_token_request'):
            fn = _function_node(name)
            doc = fn.body[0].value if (
                isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)) else None
            # The docstring may say "no subprocess"; only CODE and string
            # literals that run count.
            strings = [n.value for n in ast.walk(fn)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)
                       and n is not doc]
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            blob = ' '.join(strings) + ' ' + ' '.join(names | attrs)
            self.assertNotIn('subprocess', blob, name)
            self.assertNotIn('register_huggingface_token', blob, name)
            self.assertNotIn('huggingface-cli', blob, name)
            self.assertNotIn('whoami', blob, name)
            self.assertNotIn('get_huggingface_user_id', blob, name)

    def test_the_old_function_is_deleted_everywhere(self):
        self.assertNotIn('register_huggingface_token', _SRC)
        dm = (_PKG / 'data_processing' / 'data_manager.py').read_text(encoding='utf-8')
        self.assertNotIn('register_huggingface_token', dm)
        self.assertNotIn('huggingface-cli', dm)
        self.assertNotIn('import subprocess', dm)

    def test_request_token_is_only_assigned_never_handed_to_a_logger(self):
        fn = _function_node('set_hf_user_callback')
        parents = _parents(fn)
        uses = [n for n in ast.walk(fn)
                if isinstance(n, ast.Attribute) and n.attr == 'token'
                and isinstance(n.value, ast.Name) and n.value.id == 'request']
        self.assertEqual(len(uses), 1)
        self.assertIsInstance(parents[uses[0]], ast.Assign)

    def test_no_logger_call_formats_a_secret(self):
        banned = {'token', 'request', 'stored', 'message', 'e'}
        checked = 0
        for name in ('set_hf_user_callback', '_apply_hf_token_request',
                     '_publish_hf_token_state', '_init_hf_token_state',
                     '_apply_pending_hf_token_clear'):
            for call in _logger_calls(_function_node(name)):
                checked += 1
                for arg in call.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name) and sub.id in banned:
                            # `type(e).__name__` is the one sanctioned use of `e`
                            inner = ast.unparse(arg)
                            self.assertIn('type(e).__name__', inner, ast.unparse(call))
                            self.assertEqual(sub.id, 'e', ast.unparse(call))
        self.assertGreaterEqual(checked, 5)

    def test_init_publisher_does_not_carry_the_token_state(self):
        # test_signal_status_node_wiring execs _init_ros_publisher with a fixed
        # namespace: the token topic must live in its own method.
        src = ast.get_source_segment(_SRC, _function_node('_init_ros_publisher'))
        self.assertNotIn('hf_token', src)

    def test_init_wires_the_state_between_publisher_and_service_init(self):
        init = _function_node('__init__')

        def is_self_call(stmt, method):
            return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Attribute)
                    and stmt.value.func.attr == method
                    and isinstance(stmt.value.func.value, ast.Name)
                    and stmt.value.func.value.id == 'self')

        body = init.body
        pub = next(i for i, s in enumerate(body) if is_self_call(s, '_init_ros_publisher'))
        guarded, service = body[pub + 1], body[pub + 2]
        self.assertIsInstance(guarded, ast.Try)
        self.assertTrue(is_self_call(guarded.body[0], '_init_hf_token_state'))
        self.assertTrue(guarded.handlers and guarded.handlers[0].type.id == 'Exception')
        self.assertTrue(is_self_call(service, '_init_ros_service'))
        # the degraded-boot contract is untouched: _init_robot_profile stays LAST
        self.assertTrue(is_self_call(body[-1], '_init_robot_profile'))
        # the attributes exist before any core component is built
        core = next(i for i, s in enumerate(body) if is_self_call(s, '_init_core_components'))
        assigned = {}
        for i, s in enumerate(body):
            if isinstance(s, ast.Assign):
                for t in s.targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == 'self'):
                        assigned.setdefault(t.attr, i)
        for attr in ('_hf_token_lock', '_hf_token_seq', '_hf_token_state_pub',
                     '_hf_token_last_error_log', '_hf_token_clear_pending'):
            self.assertLess(assigned[attr], core, attr)

    def test_the_module_installs_the_scrubber_right_after_the_bearer_loop(self):
        body = _TREE.body
        loop = next(i for i, s in enumerate(body)
                    if isinstance(s, ast.For) and ast.unparse(s.target) == '_log_name')
        names = [ast.unparse(s) for s in body[loop + 1: loop + 3]]
        self.assertIn('hf_token_store', names[0])
        self.assertEqual(names[1], 'hf_token_store.install_log_scrubber()')

    def test_the_hf_worker_child_installs_the_scrubber_before_the_error_forwarder(self):
        tree = ast.parse(_WORKER_PY.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HfApiWorker')
        fn = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == '_worker_process_loop')
        src = ast.unparse(fn)
        self.assertIn('install_log_scrubber', src)
        self.assertLess(src.index('install_log_scrubber'), src.index('install_upload_error_forwarder'))

    def test_the_service_is_registered_on_the_reentrant_hf_group(self):
        src = ast.get_source_segment(_SRC, _function_node('_init_ros_service'))
        self.assertIn("('/register_hf_user', SetHFUser, self.set_hf_user_callback, "
                      "self._hf_cb_group)", src)

    def test_no_environment_variable_of_ours_leaks_into_the_server_tree(self):
        # ci.yml::env-forwarding-guard greps every EDUBOTICS_* name under this tree
        # and demands a compose forward for it.
        import re
        offenders = []
        for path in _PKG.rglob('*.py'):
            if '__pycache__' in path.parts:
                continue
            for hit in re.findall(r'EDUBOTICS_[A-Z0-9_]*HF_TOKEN[A-Z0-9_]*',
                                  path.read_text(encoding='utf-8')):
                offenders.append((path.name, hit))
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
