#!/usr/bin/env python3
#
# Aufnahme 2.0 — the recorder state machine (DataManager.record()) driven end to
# end with a fake LeRobot writer and a controllable clock.
#
# The fake reproduces the LeRobot 0.5.1 DatasetWriter semantics that matter here
# (verified against the real 0.5.1 package, and exercised for real by
# .github/scripts/record_fsm_smoke.py inside the built image):
#   * the writer starts with an EMPTY buffer (size 0), not None;
#   * add_frame() lazily recreates a None buffer;
#   * save_episode() RAISES on a size-0 buffer and leaves a fresh EMPTY buffer;
#   * DataManager._episode_reset() sets the buffer to None.
#
# The cases are the round-2 probe table (spec §9 V22) plus Q7 (a collision during
# a latched save counts the committed episode) and the record-path German.
# data_manager.py imports a large ROS/cv2/HF/lerobot tree at module level; it is
# stubbed in sys.modules before loading (same approach as the other
# test_data_manager_* files).

import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_MANAGER_PATH = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
    / 'data_processing' / 'data_manager.py'
)


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _stub_unless_importable(name):
    try:
        __import__(name)
    except Exception:  # noqa: BLE001 — deps-free suite: stub what is missing
        _stub(name)


class _TaskStatus:
    READY, WARMING_UP, RESETTING, RECORDING, SAVING, STOPPED = 0, 1, 2, 3, 4, 5

    def __init__(self):
        self.phase = 0
        self.total_time = 0
        self.proceed_time = 0
        self.current_episode_number = 0
        self.encoding_progress = -1.0
        self.error = ''
        self.robot_type = ''
        self.task_info = None
        self.current_task_instruction = ''


class _DataConverter:
    def set_action_duration_from_fps(self, fps):
        pass


class _Cpu:
    def get_cpu_usage(self):
        return 0.0


class _Ram:
    @staticmethod
    def get_ram_gb():
        return (8.0, 2.0)


class _Storage:
    @staticmethod
    def get_storage_gb(path='/'):
        return (100.0, 10.0)


def _install_stubs():
    _placeholder = type('_Placeholder', (), {})
    for name in ('cv2', 'numpy', 'requests'):
        _stub_unless_importable(name)
    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=_placeholder)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=_placeholder)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', JointState=_placeholder)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=_placeholder)
    _stub('huggingface_hub',
          CommitOperationDelete=_placeholder,
          DatasetCard=_placeholder, DatasetCardData=_placeholder,
          HfApi=_placeholder, ModelCard=_placeholder, ModelCardData=_placeholder,
          snapshot_download=lambda *a, **k: None,
          upload_large_folder=lambda *a, **k: None)
    _stub('huggingface_hub.errors',
          LocalTokenNotFoundError=type('LocalTokenNotFoundError', (Exception,), {}),
          RevisionNotFoundError=type('RevisionNotFoundError', (Exception,), {}))
    _stub('lerobot')
    _stub('lerobot.datasets')
    _stub('lerobot.datasets.utils', DEFAULT_FEATURES={})
    _stub('lerobot.datasets.dataset_metadata', CODEBASE_VERSION='v3.0')
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', TaskStatus=_TaskStatus)
    _stub('physical_ai_server')
    _stub('physical_ai_server.data_processing')
    dsp_name = 'physical_ai_server.data_processing.dataset_paths'
    if dsp_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            dsp_name, str(DATA_MANAGER_PATH.parent / 'dataset_paths.py'))
        dsp = importlib.util.module_from_spec(spec)
        sys.modules[dsp_name] = dsp
        try:
            spec.loader.exec_module(dsp)
        except BaseException:
            sys.modules.pop(dsp_name, None)
            raise
    sys.modules['physical_ai_server.data_processing'].dataset_paths = sys.modules[dsp_name]
    _stub('physical_ai_server.data_processing.data_converter', DataConverter=_DataConverter)
    _stub('physical_ai_server.data_processing.lerobot_dataset_wrapper',
          LeRobotDatasetWrapper=_placeholder)
    _stub('physical_ai_server.data_processing.progress_tracker',
          HuggingFaceProgressTqdm=_placeholder, HuggingFaceLogCapture=_placeholder)
    _stub('physical_ai_server.device_manager')
    _stub('physical_ai_server.device_manager.cpu_checker', CPUChecker=_Cpu)
    _stub('physical_ai_server.device_manager.ram_checker', RAMChecker=_Ram)
    _stub('physical_ai_server.device_manager.storage_checker', StorageChecker=_Storage)


def _load_data_manager_module():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        '_edubotics_dm_record_fsm_test', str(DATA_MANAGER_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Loaded lazily from setUpClass, never at import: `discover` imports every test
# module before running any, and stubbing huggingface_hub & co. at import time
# would shadow the real packages for the modules imported after this one.
MOD = None
DataManager = None


def _ensure_loaded():
    global MOD, DataManager
    if MOD is None:
        MOD = _load_data_manager_module()
        DataManager = MOD.DataManager
        # Never ask HuggingFace whoami from a unit test ("cannot judge" = allow).
        DataManager._rig_hf_namespaces = classmethod(lambda cls: None)
        MOD.time = _Clock
    return MOD


class _FsmTestCase(BoundedTestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_loaded()


class _Clock:
    """Replaces the module's `time`: perf_counter/monotonic/time share one t."""
    t = 1000.0

    @classmethod
    def perf_counter(cls):
        return cls.t

    monotonic = perf_counter

    @classmethod
    def time(cls):
        return cls.t


class _FakeDataset:
    def __init__(self, drop_on_save=False):
        self._buf = {'size': 0, 'task': [], 'timestamp': []}   # writer starts EMPTY
        self.committed = 0
        self.committed_sizes = []
        self.cancelled = 0
        self.discarded = 0
        self.discard_cost_s = 0.0  # the real cancel waits ~0.9 s
        self.events = []          # (event, tick number) in call order
        self.finalized = False
        self.finalize_raises = False
        self.drop_on_save = drop_on_save

    @property
    def episode_buffer(self):
        return self._buf

    @episode_buffer.setter
    def episode_buffer(self, value):
        self._buf = value

    def add_frame_without_write_image(self, frame, task):
        self.events.append(('add', _TICK_NO[0]))
        if self._buf is None:
            self._buf = {'size': 0, 'task': [], 'timestamp': []}
        self._buf['size'] += 1
        self._buf['timestamp'].append(self._buf['size'] / 30.0)

    def save_episode_without_write_image(self):
        if self._buf['size'] == 0:
            raise ValueError(
                'You must add one or several frames with `add_frame` before '
                'calling `add_episode`.')
        self.events.append(('save', _TICK_NO[0]))
        self.committed += 1
        self.committed_sizes.append(self._buf['size'])
        self._buf = {'size': 0, 'task': [], 'timestamp': []}

    def __len__(self):
        # LeRobotDataset.__len__ is the number of SAVED frames, so a fresh
        # dataset is FALSY until its first episode is saved — exactly like
        # the real 0.5.1 class. `if dataset:` is therefore never an existence
        # test.
        return sum(self.committed_sizes)

    def streaming_dropped_frame_count(self):
        return 3 if self.drop_on_save else 0

    def cancel_streaming_episode(self):
        self.events.append(('cancel', _TICK_NO[0]))
        self.cancelled += 1

    def discard_episode(self):
        # The wrapper's discard: LeRobot's clear_episode_buffer() — cancels the
        # streaming encoder at once (the slow part, ~0.9 s on the real writer)
        # and leaves a fresh, empty buffer.
        self.events.append(('discard', _TICK_NO[0]))
        self.discarded += 1
        _Clock.t += self.discard_cost_s
        self._buf = {'size': 0, 'task': [], 'timestamp': []}

    def check_video_encoding_completed(self):
        return True

    def finalize(self):
        if self.finalize_raises:
            raise RuntimeError('disk full')
        self.finalized = True


class _TaskInfo:
    def __init__(self, *, warmup=2, episode=3, reset=2, n=3, push=True):
        self.task_name = 'Würfel in die Schale'
        self.task_type = 'record'
        self.user_id = 'maxmuster'
        self.task_instruction = ['Greife den Würfel.']
        self.fps = 30
        self.tags = []
        self.warmup_time_s = warmup
        self.episode_time_s = episode
        self.reset_time_s = reset
        self.num_episodes = n
        self.push_to_hub = push
        self.private_mode = True
        self.use_optimized_save_mode = True
        self.record_rosbag2 = False


_TEMP_ROOTS = []


def tearDownModule():
    for root in _TEMP_ROOTS:
        shutil.rmtree(root, ignore_errors=True)


def make(drop=False, upload_raises=False, **kw):
    uploads = []

    def _upload(repo, path, private):
        if upload_raises:
            raise RuntimeError('queue closed')
        uploads.append(repo)

    root = Path(tempfile.mkdtemp(prefix='dm_fsm_'))
    _TEMP_ROOTS.append(root)
    dm = DataManager(root, 'omx_f', _TaskInfo(**kw), upload_callback=_upload)
    dm._lerobot_dataset = _FakeDataset(drop_on_save=drop)
    dm.create_frame = lambda images, state, action: {'x': 1}
    return dm, uploads


_TICK_NO = [0]


def tick(dm, dt=1 / 30):
    _Clock.t += dt
    _TICK_NO[0] += 1
    return dm.record(images={}, state=[], action=[])


def ticks(dm, n):
    return [tick(dm) for _ in range(n)]


def run_until(dm, status, limit=2000):
    for _ in range(limit):
        if dm.get_status() == status:
            return True
        tick(dm)
    return False


def status_seq(dm, n):
    out = []
    for _ in range(n):
        done = tick(dm)
        st = dm.get_current_record_status()
        out.append((st.phase, st.current_episode_number, st.error))
        if done:
            out.append('DONE')
            break
    return out


class StopTest(_FsmTestCase):
    # STOP is dead from the page but must stay honest (reviewer probe A).

    def test_stop_in_first_warmup_counts_and_uploads_nothing(self):
        dm, up = make()
        tick(dm)
        dm.record_stop()
        ticks(dm, 3)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(up, [])

    def test_stop_mid_run_keeps_and_counts_the_partial_episode(self):
        dm, up = make()
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.record_stop()
        ticks(dm, 3)
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(up, ['maxmuster/omx_f_Wuerfel-in-die-Schale'])

    def test_stop_during_a_latched_save_counts_the_committed_episode(self):
        # Verifier V1-1 (owner: count it). save() already committed the episode;
        # HEAD counted and uploaded it, the first cut of Aufnahme 2.0 did not.
        for n in (3, 1):
            with self.subTest(num_episodes=n):
                dm, up = make(n=n)
                run_until(dm, 'save')
                tick(dm)
                self.assertTrue(dm._on_saving)
                dm.record_stop()
                results = ticks(dm, 5)
                self.assertIn(True, results)
                self.assertEqual(dm._lerobot_dataset.committed, 1)
                self.assertEqual(dm._record_episode_count, 1)
                self.assertEqual(len(up), 1)

    def test_stop_after_a_committed_finish_counts_it(self):
        for n in (3, 1):
            with self.subTest(num_episodes=n):
                dm, up = make(n=n)
                run_until(dm, 'run')
                ticks(dm, 45)
                dm.record_finish()
                tick(dm)                        # FINISH committed the episode
                self.assertTrue(dm._on_saving)
                self.assertEqual(dm.get_status(), 'finish')
                dm.record_stop()
                results = ticks(dm, 5)
                self.assertIn(True, results)
                self.assertEqual(dm._lerobot_dataset.committed, 1)
                self.assertEqual(dm._record_episode_count, 1)
                self.assertEqual(len(up), 1)

    def test_stop_after_a_finish_that_committed_nothing_counts_nothing(self):
        dm, up = make()
        tick(dm)
        dm.record_finish()
        tick(dm)                                # finish latched, nothing committed
        self.assertTrue(dm._on_saving)
        dm.record_stop()
        results = ticks(dm, 5)
        self.assertIn(True, results)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(up, [])

    def test_stop_before_the_first_tick_without_a_dataset(self):
        dm, up = make()
        dm._lerobot_dataset = None
        dm.record_stop()
        results = ticks(dm, 4)
        self.assertIn(True, results)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(up, [])


class FinishTest(_FsmTestCase):

    def test_finish_in_the_first_warmup_completes_without_raising(self):
        # H1: HEAD set 'save' on an EMPTY buffer and the next tick raised.
        dm, up = make()
        tick(dm)
        dm.record_finish()
        results = ticks(dm, 3)
        self.assertIn(True, results)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertTrue(dm._lerobot_dataset.finalized)
        self.assertEqual(up, [])

    def test_finish_under_one_second_drops_the_run(self):
        # Q3: a run shorter than 1 s is not a real attempt.
        dm, up = make(warmup=0)
        run_until(dm, 'run')
        ticks(dm, 15)
        dm.record_finish()
        results = ticks(dm, 4)
        self.assertIn(True, results)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(dm._record_episode_count, 0)
        # Official discard only (round 5, O6): the dropped run is cancelled by
        # the wrapper's discard_episode(), never by a private encoder cancel.
        self.assertGreaterEqual(dm._lerobot_dataset.discarded, 1)
        self.assertEqual(dm._lerobot_dataset.cancelled, 0)
        self.assertEqual(up, [])

    def test_finish_mid_run_saves_counts_and_uploads(self):
        # H3: HEAD saved the partial episode but never counted it (no upload).
        dm, up = make()
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.record_finish()
        results = ticks(dm, 4)
        self.assertIn(True, results)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(up, ['maxmuster/omx_f_Wuerfel-in-die-Schale'])

    def test_finish_in_reset_finalizes_what_was_saved(self):
        dm, up = make()
        run_until(dm, 'reset')
        dm.record_finish()
        results = ticks(dm, 4)
        self.assertIn(True, results)
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(len(up), 1)

    def test_finish_during_latched_save_then_collision_completes(self):
        # Reviewer probe B + F7b: a collision re_record no longer reopens a
        # finishing session; the committed episode is counted once.
        dm, up = make()
        run_until(dm, 'save')
        tick(dm)                    # save() committed, _on_saving latched
        dm.record_finish()
        dm.re_record()              # collision trip
        self.assertEqual(dm.get_status(), 'finish')
        results = ticks(dm, 5)
        self.assertIn(True, results)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(len(up), 1)

    def test_finish_in_run_then_collision_before_the_next_tick(self):
        dm, up = make()
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.record_finish()
        dm.re_record()
        results = ticks(dm, 4)
        self.assertEqual(dm.get_status(), 'finish')
        self.assertIn(True, results)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)

    def test_finish_twice_is_a_noop(self):
        dm, _ = make()
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.record_finish()
        dm.record_finish()
        results = ticks(dm, 4)
        self.assertIn(True, results)
        self.assertEqual(dm._record_episode_count, 1)

    def test_finish_with_frame_drop_ends_with_the_saved_episodes(self):
        # H4: HEAD re-routed a FINISH that met a frame drop to 'reset' and the
        # session went on. Now it ends, and says why.
        dm, up = make(drop=True)
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.record_finish()
        tick(dm)
        self.assertEqual(dm.get_status(), 'finish')
        st = dm.get_current_record_status()
        self.assertTrue(st.error.startswith('[WARNUNG] Episode 1: Kamera-Bilder gingen'))
        self.assertIn('Die Aufnahme endet mit den schon gespeicherten Episoden.', st.error)
        results = ticks(dm, 3)
        self.assertIn(True, results)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(up, [])

    def test_finish_before_the_first_tick_without_a_dataset(self):
        dm, up = make()
        dm._lerobot_dataset = None
        dm.record_finish()
        results = ticks(dm, 3)
        self.assertIn(True, results)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(up, [])

    def test_normal_two_episode_session_is_unchanged(self):
        dm, up = make(n=2)
        seen = []
        done = False
        for _ in range(2000):
            done = tick(dm)
            if not seen or seen[-1] != dm.get_status():
                seen.append(dm.get_status())
            if done:
                break
        self.assertTrue(done)
        self.assertEqual(dm._record_episode_count, 2)
        self.assertEqual(dm._lerobot_dataset.committed, 2)
        self.assertTrue(dm._lerobot_dataset.finalized)
        self.assertEqual(up, ['maxmuster/omx_f_Wuerfel-in-die-Schale'])
        self.assertEqual(seen[:5], ['warmup', 'run', 'save', 'reset', 'run'])


class RerecordFinishWindowTest(_FsmTestCase):
    # Q4: a FINISH drops a run that STARTED after a wire RERECORD <= 5 s ago.

    def test_rerecord_then_finish_2_2_s_later_with_reset_zero_keeps_nothing(self):
        # Reviewer probe E.
        dm, _ = make(reset=0, episode=10)
        run_until(dm, 'reset')
        run_until(dm, 'run')
        ticks(dm, 45)
        c0, k0 = dm._lerobot_dataset.committed, dm._record_episode_count
        self.assertTrue(dm.rerecord_from_command())
        ticks(dm, 66)
        self.assertEqual(dm.get_status(), 'run')
        dm.record_finish()
        results = ticks(dm, 4)
        self.assertIn(True, results)
        self.assertEqual(dm._lerobot_dataset.committed - c0, 0)
        self.assertEqual(dm._record_episode_count - k0, 0)

    def test_a_run_already_committed_when_finish_arrives_stays(self):
        # E2 (accepted edge): episode 3 s, reset 0, FINISH 4.2 s after RERECORD.
        dm, _ = make(reset=0, episode=3)
        run_until(dm, 'run')
        ticks(dm, 45)
        dm.rerecord_from_command()
        ticks(dm, 126)
        dm.record_finish()
        ticks(dm, 4)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)

    def test_finish_outside_the_window_keeps_the_new_run(self):
        # E3: „Behalten und beenden“ 6 s after „Wiederholen“.
        dm, _ = make(reset=0, episode=10)
        run_until(dm, 'run')
        ticks(dm, 45)
        dm.rerecord_from_command()
        ticks(dm, 180)
        dm.record_finish()
        ticks(dm, 4)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)

    def test_discard_and_finish_with_a_reset_keeps_nothing(self):
        # E4: „Verwerfen und beenden“ (RERECORD then FINISH) with Zurücksetzen 2 s.
        dm, up = make(reset=2, episode=10)
        run_until(dm, 'run')
        ticks(dm, 45)
        dm.rerecord_from_command()
        ticks(dm, 3)
        dm.record_finish()
        ticks(dm, 4)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(up, [])

    def test_rerecord_with_reset_zero_publishes_no_resetting_tick(self):
        # G / H15: with Zurücksetzen = 0 the reset->run hop happens inside the next
        # tick, so the client sees RECORDING -> RECORDING.
        dm, _ = make(reset=0)
        run_until(dm, 'run')
        ticks(dm, 10)
        dm.rerecord_from_command()
        seq = status_seq(dm, 3)
        self.assertEqual([s[0] for s in seq], [_TaskStatus.RECORDING] * 3)

    def test_rerecord_refused_while_finishing_or_after_commit(self):
        dm, _ = make()
        run_until(dm, 'save')
        tick(dm)                     # committed, latched
        self.assertFalse(dm.rerecord_from_command())
        self.assertEqual(dm.get_status(), 'save')
        dm, _ = make()
        run_until(dm, 'run')
        dm.record_finish()
        self.assertFalse(dm.rerecord_from_command())
        self.assertEqual(dm.get_status(), 'finish')
        dm, _ = make()
        dm.record_stop()
        self.assertFalse(dm.rerecord_from_command())
        self.assertEqual(dm.get_status(), 'stop')

    def test_rerecord_in_warmup_goes_to_reset(self):
        dm, _ = make()
        tick(dm)
        self.assertTrue(dm.rerecord_from_command())
        self.assertEqual(dm.get_status(), 'reset')


class SkipTest(_FsmTestCase):
    # MOVE_TO_NEXT (single task): skip a warm-up/reset, or save an episode.

    def test_skip_outcomes(self):
        dm, _ = make()
        tick(dm)
        self.assertEqual(dm.record_early_save(), 'run')          # warm-up skipped
        self.assertEqual(dm.get_status(), 'run')
        tick(dm)
        self.assertEqual(dm.record_early_save(), 'too_early')    # < 1 s
        self.assertEqual(dm.get_status(), 'run')
        ticks(dm, 40)
        self.assertEqual(dm.record_early_save(), 'save')         # >= 1 s
        self.assertEqual(dm.get_status(), 'save')

    def test_skip_while_saving_is_refused(self):
        dm, _ = make()
        run_until(dm, 'save')
        self.assertEqual(dm.record_early_save(), '')
        self.assertEqual(dm.get_status(), 'save')

    def test_skip_in_reset_starts_the_next_run(self):
        dm, _ = make()
        run_until(dm, 'reset')
        self.assertEqual(dm.record_early_save(), 'run')
        self.assertEqual(dm.get_status(), 'run')
        # The skipped-to run starts a fresh episode: the first tick records.
        tick(dm)
        self.assertEqual(dm._lerobot_dataset.episode_buffer['size'], 1)

    def test_skip_while_finishing_is_refused(self):
        dm, _ = make()
        run_until(dm, 'run')
        dm.record_finish()
        self.assertEqual(dm.record_early_save(), '')
        self.assertEqual(dm.get_status(), 'finish')


class LowDiskTest(_FsmTestCase):

    def test_low_disk_in_reset_finishes_once_with_the_sentence(self):
        dm, up = make()
        run_until(dm, 'reset')
        sentence = ('Der Speicher ist fast voll (nur noch 0,9 GB frei), deshalb '
                    'wurde die Aufnahme beendet.')
        self.assertTrue(dm.finish_for_low_disk(sentence))
        self.assertFalse(dm.finish_for_low_disk(sentence))       # once
        seq = status_seq(dm, 4)
        self.assertEqual(seq[0][0], _TaskStatus.SAVING)
        self.assertEqual(seq[0][2], f'[WARNUNG] {sentence}')
        self.assertIn('DONE', seq)
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(len(up), 1)

    def test_low_disk_is_ignored_while_saving(self):
        dm, _ = make()
        run_until(dm, 'save')
        self.assertFalse(dm.finish_for_low_disk('x'))
        self.assertEqual(dm.get_status(), 'save')


class CollisionPathTest(_FsmTestCase):

    def test_collision_during_latched_save_counts_the_committed_episode(self):
        # Q7 (owner-approved): the episode is already on disk; count it, then
        # rewind for the resume. HEAD left committed_on_disk=1, count=0.
        dm, _ = make(n=3)
        run_until(dm, 'save')
        tick(dm)
        self.assertTrue(dm._on_saving)
        dm.re_record()
        self.assertEqual(dm.get_status(), 'reset')
        self.assertFalse(dm._on_saving)
        ticks(dm, 3)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)
        # The resumed session records the NEXT episode index.
        run_until(dm, 'run')
        ticks(dm, 40)
        self.assertEqual(dm.get_current_record_status().current_episode_number, 1)

    def test_q7_count_writes_the_session_marker(self):
        dm, _ = make(n=3)
        dm._session_marker_enabled = True
        run_until(dm, 'save')
        tick(dm)
        dm.re_record()
        marker = dm._session_marker_path()
        self.assertTrue(marker.exists())
        self.assertIn('"episodes_saved": 1', marker.read_text(encoding='utf-8'))

    def test_q7_on_the_last_episode_completes_after_the_resume(self):
        # The count reaches the target: finish like the normal save branch
        # instead of rewinding into a run the session has no room for.
        dm, up = make(n=1)
        run_until(dm, 'save')
        tick(dm)
        dm.re_record()
        self.assertEqual(dm.get_status(), 'finish')
        buffer_sizes = []
        results = []
        for _ in range(3):
            results.append(tick(dm))
            buf = dm._lerobot_dataset.episode_buffer
            buffer_sizes.append(0 if buf is None else buf['size'])
        self.assertEqual(buffer_sizes, [0, 0, 0])   # no frame recorded after it
        self.assertIn(True, results)
        # A literal rewind to 'reset' would publish a phantom RESETTING
        # („Episode 2 von 1“) and a RECORDING tick before READY (verifier V1-7).
        dm, _ = make(n=1, reset=2)
        run_until(dm, 'save')
        tick(dm)
        dm.re_record()
        phases = set()
        for _ in range(200):
            done = tick(dm)
            phases.add(dm.get_current_record_status().phase)
            if done:
                break
        self.assertTrue(done)
        self.assertEqual(phases, {_TaskStatus.SAVING})
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(len(up), 1)

    def test_collision_during_saving_before_commit_discards(self):
        dm, _ = make(n=3)
        run_until(dm, 'save')       # 'save' published, save() not yet run
        dm.re_record()
        ticks(dm, 3)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(dm.get_status(), 'reset')

    def test_collision_in_a_finishing_session_is_a_noop(self):
        # F7b.
        dm, _ = make()
        run_until(dm, 'run')
        dm.record_finish()
        dm.re_record()
        self.assertEqual(dm.get_status(), 'finish')
        dm, _ = make()
        dm.record_stop()
        dm.re_record()
        self.assertEqual(dm.get_status(), 'stop')

    def test_forced_recovery_ends_the_session(self):
        # F1: collision re_record mid-run, then end_session_now.
        dm, up = make()
        run_until(dm, 'reset')
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.re_record()
        self.assertTrue(dm.end_session_now())
        self.assertEqual(dm.get_status(), 'finish')
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertTrue(dm._lerobot_dataset.finalized)
        self.assertEqual(up, ['maxmuster/omx_f_Wuerfel-in-die-Schale'])

    def test_forced_recovery_after_finish_and_collision_carries_the_count(self):
        # F1b.
        dm, up = make()
        run_until(dm, 'save')
        tick(dm)
        dm.record_finish()
        dm.re_record()
        self.assertTrue(dm.end_session_now())
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(len(up), 1)

    def test_forced_recovery_keeps_the_frame_loss_warning_for_the_status(self):
        # Verifier V1-3: end_session_now leaves the warning for the ONE record
        # status the collision monitor publishes before its READY.
        dm, up = make()
        run_until(dm, 'run')
        ticks(dm, 45)
        dm._lerobot_dataset.drop_on_save = True
        self.assertTrue(dm.end_session_now())
        st = dm.get_current_record_status()
        self.assertEqual(st.phase, _TaskStatus.SAVING)
        self.assertTrue(st.error.startswith(
            '[WARNUNG] Episode 1: Kamera-Bilder gingen beim Speichern verloren'))
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(up, [])

    def test_a_discard_in_the_first_episode_really_empties_the_buffer(self):
        # A fresh LeRobotDataset is falsy (len == saved frames == 0), and
        # _episode_reset used `if self._lerobot_dataset and …`, so in the FIRST
        # episode of every new dataset „Wiederholen“, a collision discard and
        # the frame-drop discard left the discarded frames in the buffer: the
        # next saved episode carried them (measured with the real writer: 30
        # discarded + 40 new = 70 frames saved).
        for discard in ('rerecord', 'collision'):
            with self.subTest(discard=discard):
                dm, _ = make(reset=0, episode=10)
                run_until(dm, 'run')
                ticks(dm, 30)
                self.assertEqual(len(dm._lerobot_dataset), 0)      # falsy
                if discard == 'rerecord':
                    self.assertTrue(dm.rerecord_from_command())
                else:
                    dm.re_record()
                buf = dm._lerobot_dataset.episode_buffer
                self.assertTrue(buf is None or buf['size'] == 0)
                run_until(dm, 'run')
                ticks(dm, 40)
                self.assertEqual(dm.record_early_save(), 'save')
                ticks(dm, 3)
                self.assertEqual(dm._lerobot_dataset.committed_sizes, [40])

    def test_a_frame_drop_discard_in_the_first_episode_empties_the_buffer(self):
        dm, _ = make(drop=True, reset=0, episode=10)
        run_until(dm, 'run')
        ticks(dm, 40)
        self.assertEqual(dm.record_early_save(), 'save')
        tick(dm)                                  # save() sees the drop, discards
        self.assertEqual(dm.get_status(), 'reset')
        buf = dm._lerobot_dataset.episode_buffer
        self.assertTrue(buf is None or buf['size'] == 0)

    def test_end_session_now_without_a_dataset(self):
        dm, up = make()
        dm._lerobot_dataset = None
        self.assertTrue(dm.end_session_now())
        self.assertEqual(up, [])


class UploadBlockedReasonTest(_FsmTestCase):

    def test_namespace_refusal_sets_the_blocked_reason(self):
        dm, up = make(n=1, episode=1, warmup=0)
        original = DataManager._rig_hf_namespaces
        DataManager._rig_hf_namespaces = classmethod(lambda cls: {'someone-else'})
        try:
            results = ticks(dm, 80)
        finally:
            DataManager._rig_hf_namespaces = original
        self.assertIn(True, results)
        self.assertEqual(up, [])
        self.assertTrue(dm._upload_blocked_reason_de.startswith(
            'Upload abgelehnt: Der Roboter darf nicht'))
        # Round 7: the sentence is record_texts_de's, not an inline copy.
        self.assertEqual(dm._upload_blocked_reason_de,
                         _texts().NAMESPACE_REFUSED_DE)

    def test_enqueue_failure_sets_the_not_started_sentence(self):
        dm, up = make(n=1, episode=1, warmup=0, upload_raises=True)
        results = ticks(dm, 80)
        self.assertIn(True, results)
        self.assertEqual(dm._upload_blocked_reason_de, MOD.UPLOAD_NOT_STARTED_DE)
        self.assertIn('später im Tab Daten', MOD.UPLOAD_NOT_STARTED_DE)
        # Round 7: the module name stays (callers read it) and IS the text's.
        self.assertIs(MOD.UPLOAD_NOT_STARTED_DE, _texts().UPLOAD_NOT_STARTED_DE)

    def test_finalize_failure_sets_the_blocked_reason_and_skips_upload(self):
        dm, up = make(n=1, episode=1, warmup=0)
        dm._lerobot_dataset.finalize_raises = True
        results = ticks(dm, 80)
        self.assertIn(True, results)
        self.assertEqual(up, [])
        self.assertIn('Datensatz konnte nicht abgeschlossen werden',
                      dm._upload_blocked_reason_de)

    def test_a_clean_session_has_no_blocked_reason(self):
        dm, up = make(n=1, episode=1, warmup=0)
        ticks(dm, 80)
        self.assertEqual(dm._upload_blocked_reason_de, '')
        self.assertEqual(len(up), 1)


def _reach(state, n):
    """A DataManager in `state` (the verifier's matrix states)."""
    dm, up = make(n=n)
    if state == 'warmup':
        tick(dm)
    elif state == 'run_short':
        run_until(dm, 'run')
        ticks(dm, 10)
    elif state == 'run_long':
        run_until(dm, 'run')
        ticks(dm, 45)
    elif state == 'save_unlatched':
        run_until(dm, 'save')
    elif state == 'save_latched':
        run_until(dm, 'save')
        tick(dm)
    elif state == 'reset':
        run_until(dm, 'reset')
    elif state == 'finish_unlatched':
        run_until(dm, 'run')
        ticks(dm, 45)
        dm.record_finish()
    elif state == 'finish_latched':
        run_until(dm, 'run')
        ticks(dm, 45)
        dm.record_finish()
        tick(dm)
    elif state == 'stop':
        run_until(dm, 'run')
        ticks(dm, 45)
        dm.record_stop()
    return dm, up


_MATRIX_STATES = ('warmup', 'run_short', 'run_long', 'save_unlatched', 'save_latched',
                  'reset', 'finish_unlatched', 'finish_latched', 'stop')
_MATRIX_COMMANDS = {
    'MOVE_TO_NEXT': lambda dm: dm.record_early_save(),
    'RERECORD': lambda dm: dm.rerecord_from_command(),
    'FINISH': lambda dm: dm.record_finish(),
    'STOP': lambda dm: dm.record_stop(),
    'COLLISION': lambda dm: dm.re_record(),
    'F1_END': lambda dm: dm.end_session_now(),
    'LOW_DISK': lambda dm: dm.finish_for_low_disk('Speicher fast voll.'),
}


class DiscardCancelsTheEncoderTest(_FsmTestCase):
    """Round 3 (owner: do it the way LeRobot does). Discarding an unsaved take
    must cancel LeRobot's streaming encoder through its public
    clear_episode_buffer() (the wrapper's discard_episode) BEFORE the next
    take's first frame — never lazily inside that frame, where the ~0.9 s
    cancel left a hole the dataset's timestamps do not know about.

    The cancel runs in the record tick right after the discard (the reset or
    discard tick, where no frame is recorded), never synchronously in the
    command or collision callback: those share the node's default callback
    group with the collision detector and its relax-in-place timer.
    """

    def _events(self, dm, kind):
        return [t for e, t in dm._lerobot_dataset.events if e == kind]

    def _assert_cancel_before_the_new_take(self, dm, discard_tick):
        discards = self._events(dm, 'discard')
        self.assertEqual(len(discards), 1)
        adds_after = [t for t in self._events(dm, 'add') if t > discard_tick]
        self.assertTrue(adds_after, 'the new take recorded no frame')
        # Strictly earlier tick: the slow cancel shares no tick with a frame.
        self.assertLess(discards[0], adds_after[0])
        return discards[0]

    def _mid_run(self, **kw):
        dm, up = make(**kw)
        run_until(dm, 'run')
        ticks(dm, 40)
        return dm, up

    def test_wire_rerecord_cancels_in_the_next_tick_not_in_the_command(self):
        for reset in (0, 2):
            with self.subTest(reset=reset):
                dm, _ = self._mid_run(reset=reset)
                self.assertTrue(dm.rerecord_from_command())
                self.assertEqual(dm._lerobot_dataset.discarded, 0)   # not in the callback
                at = _TICK_NO[0]
                run_until(dm, 'run')
                ticks(dm, 3)
                cancel_tick = self._assert_cancel_before_the_new_take(dm, at)
                self.assertEqual(cancel_tick, at + 1)                # the very next tick

    def test_collision_discard_never_blocks_the_collision_callback(self):
        dm, _ = self._mid_run(reset=0)
        dm.re_record()                                    # the collision trip
        self.assertEqual(dm._lerobot_dataset.discarded, 0)
        at = _TICK_NO[0]
        run_until(dm, 'run')                              # the resumed timer
        ticks(dm, 3)
        self.assertEqual(self._assert_cancel_before_the_new_take(dm, at), at + 1)

    def test_frame_drop_rerecord_discards_in_the_next_record_step(self):
        # Round 5 (O6, official cancel only): the redo leaves _discard_pending and
        # the next record step (the reset tick, no frame) runs the wrapper's
        # discard_episode(); no private encoder cancel anywhere.
        dm, _ = self._mid_run(drop=True, reset=0)
        self.assertEqual(dm.record_early_save(), 'save')
        tick(dm)                                          # save() sees the drop
        drop_tick = _TICK_NO[0]
        self.assertEqual(self._events(dm, 'cancel'), [])
        self.assertEqual(self._events(dm, 'discard'), [])
        dm._lerobot_dataset.drop_on_save = False
        run_until(dm, 'run')
        ticks(dm, 3)
        self.assertEqual(self._assert_cancel_before_the_new_take(dm, drop_tick),
                         drop_tick + 1)

    def test_a_finish_that_drops_its_run_discards_before_the_finalize(self):
        dm, up = make(warmup=0)
        run_until(dm, 'run')
        ticks(dm, 10)                                     # < EARLY_SAVE_MIN_S
        dm.record_finish()
        tick(dm)                                          # the run is dropped
        drop_tick = _TICK_NO[0]
        self.assertEqual(self._events(dm, 'cancel'), [])
        self.assertIn(True, ticks(dm, 3))
        self.assertEqual(self._events(dm, 'discard'), [drop_tick + 1])
        self.assertTrue(dm._lerobot_dataset.finalized)
        self.assertEqual(up, [])

    def test_move_to_next_right_after_a_discard_still_cancels_before_the_frame(self):
        # Within the same tick window: RERECORD, then „Jetzt starten“ skips the
        # reset. The cancel runs first in that tick, the frame after it.
        dm, _ = self._mid_run(reset=5)
        dm.rerecord_from_command()
        self.assertEqual(dm.record_early_save(), 'run')
        tick(dm)
        kinds = [e for e, t in dm._lerobot_dataset.events if t == _TICK_NO[0]]
        self.assertEqual(kinds, ['discard', 'add'])

    def test_discard_then_finish_cancels_and_keeps_nothing(self):
        dm, up = self._mid_run(reset=0)
        dm.rerecord_from_command()
        dm.record_finish()
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._lerobot_dataset.discarded, 1)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(up, [])

    def test_the_save_path_never_discards(self):
        dm, _ = make(n=2, reset=0)
        for _ in range(2000):
            if tick(dm):
                break
        self.assertEqual(dm._lerobot_dataset.committed, 2)
        self.assertEqual(dm._lerobot_dataset.discarded, 0)
        self.assertEqual(dm._lerobot_dataset.cancelled, 0)

    def test_nothing_to_cancel_after_a_save_or_in_the_warmup(self):
        dm, _ = make(n=3)
        run_until(dm, 'save')
        tick(dm)                                          # committed + latched
        dm.re_record()                                    # Q7: count, rewind
        ticks(dm, 3)
        self.assertEqual(dm._lerobot_dataset.discarded, 0)
        dm, _ = make()
        tick(dm)
        dm.rerecord_from_command()                        # in the warm-up
        ticks(dm, 3)
        self.assertEqual(dm._lerobot_dataset.discarded, 0)

    def test_the_run_clock_starts_after_the_cancel(self):
        # Round 4: „Jetzt starten“ between a RERECORD and the next tick sets the
        # run clock BEFORE the ~0.9 s cancel; the take really starts after it.
        # Else Q3 (< 1 s is dropped) and „too_early“ saw a run ~1 s too old.
        dm, _ = self._mid_run(reset=5)
        dm._lerobot_dataset.discard_cost_s = 0.9
        self.assertTrue(dm.rerecord_from_command())
        self.assertEqual(dm.record_early_save(), 'run')
        tick(dm)                                          # cancel, then frame 0
        self.assertEqual([e for e, t in dm._lerobot_dataset.events
                          if t == _TICK_NO[0]], ['discard', 'add'])
        self.assertLess(dm._run_age_s(), 0.1)
        ticks(dm, 14)                                     # ~0.5 s of the new take
        self.assertEqual(dm.record_early_save(), 'too_early')
        dm.record_finish()                                # Q3: dropped
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._lerobot_dataset.committed_sizes, [])

    def test_the_resync_cancel_is_not_repeated_by_the_next_tick(self):
        # Round 4: the collision monitor cancels the discarded take while the
        # arm is still frozen (cancel_pending_discard); the next record tick
        # must not cancel again.
        dm, _ = self._mid_run(reset=0)
        dm.re_record()
        self.assertTrue(dm.cancel_pending_discard())
        self.assertEqual(dm._lerobot_dataset.discarded, 1)
        self.assertFalse(dm.cancel_pending_discard())     # idempotent
        run_until(dm, 'run')
        ticks(dm, 3)
        self.assertEqual(dm._lerobot_dataset.discarded, 1)
        self.assertEqual(self._events(dm, 'add')[-1], _TICK_NO[0])   # recording again

    def test_cancel_pending_discard_without_a_pending_discard(self):
        dm, _ = self._mid_run()
        self.assertFalse(dm.cancel_pending_discard())
        self.assertEqual(dm._lerobot_dataset.discarded, 0)

    def test_a_failing_discard_never_breaks_the_tick(self):
        dm, _ = self._mid_run(reset=0)

        def _boom():
            raise RuntimeError('encoder cancel failed')

        dm._lerobot_dataset.discard_episode = _boom
        dm.rerecord_from_command()
        run_until(dm, 'run')
        ticks(dm, 3)                                      # must not raise
        self.assertEqual(dm.get_status(), 'run')


class CommandMatrixTest(_FsmTestCase):
    """Every command in every recorder state (verifier V1 matrix): the session
    completes without raising, what is on disk is exactly what is counted, and
    the upload happens iff something was counted."""

    def test_every_command_in_every_state_keeps_disk_and_count_equal(self):
        for n in (3, 1):
            for state in _MATRIX_STATES:
                if n == 1 and state == 'reset':
                    continue                      # unreachable: n=1 goes to finish
                for name, command in _MATRIX_COMMANDS.items():
                    with self.subTest(num_episodes=n, state=state, command=name):
                        dm, up = _reach(state, n)
                        out = command(dm)
                        done = name == 'F1_END' and out is True
                        for _ in range(3000):
                            if done:
                                break
                            done = tick(dm)
                        self.assertTrue(done)
                        committed = dm._lerobot_dataset.committed
                        self.assertEqual(committed, dm._record_episode_count)
                        self.assertEqual(bool(up), dm._record_episode_count > 0)


class SessionMarkerTest(_FsmTestCase):
    """Verifier V1-4: a session that ends before any dataset exists (FINISH or a
    forced recovery during the <= 5 s wait for sensor data) still removes the
    crash marker its first tick wrote — else the next boot reports a crashed
    session that never recorded anything."""

    def _dm_without_dataset(self):
        dm, up = make()
        dm._lerobot_dataset = None
        dm._session_marker_enabled = True
        return dm, up

    def _finishing_ticks(self, dm):
        for _ in range(4):
            _Clock.t += 1 / 30
            if dm.record(None, None, None):
                return True
        return False

    def test_finish_without_a_dataset_clears_the_marker(self):
        dm, up = self._dm_without_dataset()
        dm.record_finish()
        _Clock.t += 1 / 30
        self.assertFalse(dm.record(None, None, None))   # first tick writes it
        self.assertTrue(dm._session_marker_path().exists())
        self.assertTrue(self._finishing_ticks(dm))
        self.assertFalse(dm._session_marker_path().exists())
        self.assertEqual(up, [])

    def test_forced_recovery_without_a_dataset_clears_the_marker(self):
        dm, up = self._dm_without_dataset()
        self.assertTrue(dm.end_session_now())
        self.assertFalse(dm._session_marker_path().exists())
        self.assertEqual(up, [])

    def test_stop_without_a_dataset_clears_the_marker(self):
        dm, _ = self._dm_without_dataset()
        dm.record_stop()
        self.assertTrue(self._finishing_ticks(dm))
        self.assertFalse(dm._session_marker_path().exists())

    def test_a_normal_finish_still_clears_the_marker(self):
        dm, _ = make()
        dm._session_marker_enabled = True
        run_until(dm, 'run')
        ticks(dm, 45)
        self.assertTrue(dm._session_marker_path().exists())
        dm.record_finish()
        self.assertIn(True, ticks(dm, 3))
        self.assertFalse(dm._session_marker_path().exists())


class _SameFrameConverter:
    def set_action_duration_from_fps(self, fps):
        pass

    def compressed_image2cvmat(self, value, desired_encoding='rgb8'):
        return b'frozen-frame-bytes'


class StaleCameraWarningTest(_FsmTestCase):

    def _dm(self, recording):
        dm, _ = make()
        dm._session_marker_enabled = recording
        dm.data_converter = _SameFrameConverter()
        return dm

    def _feed(self, dm, seconds, step=1 / 30):
        warnings = []
        end = _Clock.t + seconds
        while _Clock.t < end:
            _Clock.t += step
            dm.convert_msgs_to_raw_datas({'scene': object()}, None, [])
            st = dm.get_current_record_status()
            if st.error:
                warnings.append((round(_Clock.t, 3), st.error))
        return warnings

    def test_recording_warning_is_german_and_throttled(self):
        dm = self._dm(recording=True)
        warnings = self._feed(dm, 12.0)
        self.assertTrue(warnings)
        self.assertEqual(
            warnings[0][1],
            '[WARNUNG] Die Szenen-Kamera zeigt seit über 5 s dasselbe Bild. '
            'Die Aufnahme läuft weiter – prüfe, ob die Kamera hängt.')
        # Round 7: built by record_texts_de, not an inline copy.
        self.assertEqual(warnings[0][1],
                         '[WARNUNG] ' + _texts().stale_camera_recording_de('scene', 5.0))
        # At most once per 5 s.
        times = [t for t, _ in warnings]
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, 5.0 - 1e-6)
        self.assertLessEqual(len(warnings), 2)

    def test_non_recording_data_manager_keeps_the_head_text(self):
        # Inference (F3): HEAD's sentence, re-set on every tick once frozen.
        dm = self._dm(recording=False)
        warnings = self._feed(dm, 7.0)
        self.assertGreater(len(warnings), 10)
        self.assertEqual(
            warnings[0][1],
            '[WARNUNG] Kamera "scene" liefert seit über 5s dasselbe Bild. '
            'Aufnahme läuft weiter — bitte prüfen, ob die Szene wirklich '
            'statisch ist oder die Kamera hängt.')

    def test_camera_name_de(self):
        self.assertEqual(MOD.camera_name_de('gripper'), 'Greifer-Kamera')
        self.assertEqual(MOD.camera_name_de('scene'), 'Szenen-Kamera')
        self.assertEqual(MOD.camera_name_de('wrist'), 'Kamera „wrist“')


class InlineSentencesMovedTest(BoundedTestCase):
    """Round 7: the last student-facing sentences of the recording path live
    in record_texts_de.py; the data manager keeps no inline copy (the parser
    folds implicit concatenation and f-string parts into Constant nodes). The
    inference stale-camera sentence stays inline on purpose (F3)."""

    MOVED = (
        'Das Hochladen konnte nicht gestartet werden',
        'Upload abgelehnt',
        'Alte Dateien auf Hugging Face',
        'Der Versions-Tag des Datensatzes',
        'zeigt seit über',
        'keine Video-Datei gespeichert',
    )

    def test_no_inline_copy_is_left(self):
        import ast
        src = Path(DATA_MANAGER_PATH).read_text(encoding="utf-8")
        constants = [node.value for node in ast.walk(ast.parse(src))
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        for fragment in self.MOVED:
            hits = [c for c in constants if fragment in c]
            self.assertEqual(hits, [], fragment)

    def test_the_names_are_referenced(self):
        src = Path(DATA_MANAGER_PATH).read_text(encoding="utf-8")
        # Daten 2.0: HUB_SYNC_FAILED_DE is removed (the orphan deletes are in
        # the upload's ONE commit); the guarded upload's mapping still names
        # NAMESPACE_REFUSED_DE and HUB_TAG_FAILED_DE.
        for name in ('UPLOAD_NOT_STARTED_DE', 'NAMESPACE_REFUSED_DE',
                     'HUB_TAG_FAILED_DE',
                     'stale_camera_recording_de', 'missing_video_de'):
            self.assertIn(f'record_texts_de.{name}', src, name)


class RecorderLockTest(_FsmTestCase):
    """Round 5 (O1, spec §4): one recorder lock. The record tick holds it for a
    whole record step, commands for one transition; a collision's discard never
    waits for it (applied at once when the lock is free, else by the holder at
    release — with a re-check so a request landing between the holder's last
    drain and its release is never lost)."""

    _MUTATORS = ('record', 're_record', 'rerecord_from_command', 'record_finish',
                 'record_stop', 'record_early_save', 'record_skip_task',
                 'record_next_episode', 'finish_for_low_disk', 'end_session_now',
                 'cancel_pending_discard', 'get_current_record_status', 'end_after_error')

    def test_every_public_mutator_holds_the_lock(self):
        for name in self._MUTATORS:
            with self.subTest(name=name):
                self.assertTrue(getattr(getattr(DataManager, name), 'edubotics_recorder_locked',
                                        False))

    def _hold_in_thread(self, dm, body):
        """Run body() in another thread while it holds dm.lock."""
        import threading
        entered, release = threading.Event(), threading.Event()

        def _holder():
            with dm.locked():
                entered.set()
                release.wait(5)
                body()

        t = threading.Thread(target=_holder)
        t.start()
        self.assertTrue(entered.wait(5))
        return t, release

    def test_a_collision_discard_with_a_free_lock_applies_now(self):
        dm, _ = make()
        run_until(dm, 'run')
        ticks(dm, 10)
        self.assertTrue(dm.request_collision_discard())
        self.assertEqual(dm.get_status(), 'reset')
        self.assertFalse(dm._collision_discard_requested)

    def test_a_collision_discard_never_waits_and_the_holder_applies_it(self):
        import time as real_time
        dm, _ = make()
        run_until(dm, 'run')
        ticks(dm, 10)
        t, release = self._hold_in_thread(dm, lambda: None)
        started = real_time.monotonic()
        self.assertFalse(dm.request_collision_discard())
        self.assertLess(real_time.monotonic() - started, 0.1)
        self.assertEqual(dm.get_status(), 'run')          # not applied yet
        release.set()
        t.join(5)
        self.assertEqual(dm.get_status(), 'reset')        # applied at the release

    def test_a_request_between_the_last_drain_and_the_release_is_applied(self):
        dm, _ = make()
        run_until(dm, 'run')
        ticks(dm, 10)
        original = dm._drain_collision_request_locked
        state = {'first': True}

        def _drain_then_request():
            original()
            if state['first']:                # the holder's own (empty) drain ...
                state['first'] = False
                dm._collision_discard_requested = True   # ... then the trip lands
        dm._drain_collision_request_locked = _drain_then_request
        with dm.locked():
            pass
        self.assertEqual(dm.get_status(), 'reset')
        self.assertFalse(dm._collision_discard_requested)

    def test_a_drain_never_runs_inside_a_record_step(self):
        dm, _ = make()
        run_until(dm, 'run')
        ticks(dm, 10)
        fake = dm._lerobot_dataset
        add = fake.add_frame_without_write_image
        seen = []

        def _add_while_the_trip_lands(frame, task):
            add(frame, task)
            dm._collision_discard_requested = True       # the trip, mid-step
            seen.append(dm.get_status())
        fake.add_frame_without_write_image = _add_while_the_trip_lands
        tick(dm)
        self.assertEqual(seen, ['run'])                  # the frame step completed
        self.assertEqual(dm.get_status(), 'reset')       # then the discard applied

    def test_try_locked_answers_busy_within_the_timeout(self):
        import time as real_time
        dm, _ = make()
        t, release = self._hold_in_thread(dm, lambda: None)
        started = real_time.monotonic()
        with dm.try_locked(0.25) as got:
            self.assertFalse(got)
        elapsed = real_time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 0.3 + 0.2)
        release.set()
        t.join(5)
        with dm.try_locked(0.25) as got:
            self.assertTrue(got)
            self.assertTrue(dm.lock._is_owned())
        self.assertFalse(dm.lock._is_owned())

    def test_try_locked_drains_at_its_release(self):
        dm, _ = make()
        run_until(dm, 'run')
        ticks(dm, 10)
        with dm.try_locked(0.25) as got:
            self.assertTrue(got)
            dm._collision_discard_requested = True
        self.assertEqual(dm.get_status(), 'reset')

    def test_a_data_manager_built_without_init_still_locks(self):
        # The deps-free contract tests build DataManager via __new__.
        bare = DataManager.__new__(DataManager)
        with bare.locked():
            pass
        with bare.try_locked(0.1) as got:
            self.assertTrue(got)


class FrameCountTakeTest(_FsmTestCase):
    """Round 5 (spec §3.3): a take ends by FRAME COUNT, never by wall clock:
    n_target = max(1, round(episode_time_s * fps)) frames, then 'save'. A tick
    without images adds nothing. run_entered_mono is stamped at every run
    entry, like _run_entered_at."""

    def test_a_take_has_exactly_episode_time_times_fps_frames(self):
        for dt in (0.0, 1 / 30, 0.5):
            with self.subTest(dt=dt):
                dm, _ = make(warmup=0, episode=2, n=1)
                run_until(dm, 'run')
                while dm.get_status() == 'run':
                    _Clock.t += dt
                    dm.record(images={}, state=[], action=[])
                self.assertEqual(dm._lerobot_dataset.episode_buffer['size'], 60)

    def test_images_none_adds_nothing(self):
        dm, _ = make(warmup=0, episode=1, n=1)
        run_until(dm, 'run')
        before = dm._lerobot_dataset.episode_buffer['size']
        for _ in range(100):
            _Clock.t += 1.0
            dm.record(images=None, state=None, action=None)
        self.assertEqual(dm.get_status(), 'run')
        self.assertEqual(dm._lerobot_dataset.episode_buffer['size'], before)

    def test_proceed_time_is_frames_over_fps(self):
        dm, _ = make(warmup=0, episode=3, n=1)
        run_until(dm, 'run')
        ticks(dm, 45)
        self.assertAlmostEqual(dm._proceed_time,
                               dm._lerobot_dataset.episode_buffer['size'] / 30.0)

    def test_run_entered_mono_at_every_run_entry(self):
        dm, _ = make(warmup=1, reset=1)
        self.assertIsNone(dm.run_entered_mono)
        run_until(dm, 'run')                               # warm-up -> run
        self.assertEqual(dm.run_entered_mono, _Clock.t)
        run_until(dm, 'reset')
        _Clock.t += 0.2
        self.assertEqual(dm.record_early_save(), 'run')    # „Jetzt starten“
        self.assertEqual(dm.run_entered_mono, _Clock.t)
        ticks(dm, 30)
        dm.rerecord_from_command()
        self.assertEqual(dm.record_early_save(), 'run')
        _Clock.t += 0.4
        self.assertTrue(dm.cancel_pending_discard())       # the re-stamp
        self.assertEqual(dm.run_entered_mono, _Clock.t)


class SaveCountPendingTest(_FsmTestCase):
    """R5-3 / I2j: a latched save that committed NOTHING is not an episode — the
    old-client multi-task MOVE_TO_NEXT in the first warm-up used to count one
    (robot 2 / disk 1). _save_count_pending is read at all four sites."""

    def _empty_latched_save(self, n=3):
        dm, up = make(n=n)
        tick(dm)                                          # warm-up
        dm.record_next_episode()                          # old multi-task NEXT
        tick(dm)                                          # 'save' latches, nothing saved
        return dm, up

    def test_the_save_completion_branch_counts_nothing(self):
        dm, up = self._empty_latched_save()
        ticks(dm, 3)
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(dm.get_current_record_status().current_episode_number, 0)

    def test_stop_after_an_empty_latched_save_counts_nothing(self):
        dm, up = make(n=3)
        tick(dm)
        dm.record_next_episode()
        tick(dm)                                          # save latched, empty
        self.assertTrue(dm._on_saving)
        dm.record_stop()
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(up, [])

    def test_finish_after_an_empty_latched_save_counts_nothing(self):
        dm, up = make(n=3)
        tick(dm)
        dm.record_next_episode()
        tick(dm)
        dm.record_finish()
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._record_episode_count, 0)
        self.assertEqual(up, [])

    def test_a_collision_after_an_empty_latched_save_counts_nothing(self):
        dm, _ = make(n=3)
        tick(dm)
        dm.record_next_episode()
        tick(dm)
        dm.re_record()                                    # Q7 branch
        self.assertEqual(dm._record_episode_count, 0)

    def test_a_real_latched_save_still_counts_everywhere(self):
        dm, _ = make(n=3)
        run_until(dm, 'save')
        tick(dm)
        dm.re_record()
        self.assertEqual(dm._record_episode_count, 1)


class ErrorStopFinalizesTest(_FsmTestCase):
    """D5 (owner): every error stop with a dataset keeps the saved episodes,
    drops the running take through the official discard BEFORE finalize,
    finalizes, clears the crash marker, and uploads NOTHING."""

    def test_an_error_stop_mid_take_finalizes_what_was_saved(self):
        dm, up = make(n=3, reset=0)
        dm._session_marker_enabled = True
        run_until(dm, 'reset')
        run_until(dm, 'run')
        ticks(dm, 20)
        self.assertTrue(dm.end_after_error())
        fake = dm._lerobot_dataset
        self.assertEqual(fake.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)
        discard = [t for e, t in fake.events if e == 'discard']
        self.assertEqual(len(discard), 1)
        self.assertTrue(fake.finalized)
        self.assertFalse(dm._session_marker_path().exists())
        self.assertEqual(up, [])
        # inert afterwards: a stray tick changes nothing
        self.assertFalse(tick(dm))
        self.assertEqual(fake.committed, 1)

    def test_a_latched_uncounted_save_is_counted_by_the_error_stop(self):
        dm, up = make(n=3)
        run_until(dm, 'save')
        tick(dm)                                          # committed, latched
        self.assertTrue(dm.end_after_error())
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(up, [])

    def test_without_a_dataset_nothing_happens(self):
        dm, up = make()
        dm._lerobot_dataset = None
        self.assertIsNone(dm.end_after_error())
        self.assertEqual(up, [])

    def test_a_failing_finalize_keeps_the_marker_and_reports(self):
        # F3 (round 6): the crash marker is cleared ONLY when finalize
        # succeeded; an incomplete dataset keeps it (the next boot says so).
        dm, up = make(n=3)
        dm._session_marker_enabled = True
        run_until(dm, 'run')
        ticks(dm, 10)
        dm._lerobot_dataset.finalize_raises = True
        self.assertIs(dm.end_after_error(), False)
        self.assertTrue(dm._session_marker_path().exists())
        self.assertEqual(dm._upload_blocked_reason_de, _texts().FINALIZE_FAILED_DE)
        self.assertEqual(up, [])

    def test_saved_episode_count_survives_for_the_sentence(self):
        dm, _ = make(n=3, reset=0)
        run_until(dm, 'reset')
        self.assertTrue(dm.end_after_error())
        self.assertEqual(dm.saved_episode_count(), 1)


class OfficialDiscardOnlyTest(_FsmTestCase):
    """O6: the recording path cancels a take only through LeRobot's public
    clear_episode_buffer() (the wrapper's discard_episode); cancel_streaming_episode
    is an alias of it. data_manager.py names no private encoder attribute."""

    _PRIVATE = ('_streaming_encoder', '_dropped_frames', '_frame_queues', '_stop_events')

    def test_the_recording_path_names_no_private_encoder_attribute(self):
        import ast
        wrapper = DATA_MANAGER_PATH.with_name('lerobot_dataset_wrapper.py')
        for path in (DATA_MANAGER_PATH, wrapper):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
                names |= {n.value for n in ast.walk(tree)
                          if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                for private in self._PRIVATE:
                    self.assertNotIn(private, names)

    def test_the_forked_card_template_is_gone(self):
        self.assertFalse(DATA_MANAGER_PATH.with_name('dataset_card_template.md').exists())

    def test_no_gc_collect_on_the_recording_path(self):
        source = DATA_MANAGER_PATH.read_text(encoding='utf-8')
        self.assertNotIn('gc.collect', source)
        self.assertNotIn('_validate_episode_buffer', source)


def _texts():
    return MOD.record_texts_de


class GapRedoTest(_FsmTestCase):
    """O2 + C6: a take whose source was silent >= SOURCE_GAP_S on its own
    timeline (the node's capture integrity notes it) is not committed but
    re-recorded — at most twice per episode, the third gapped take of the same
    episode is SAVED with GAP_KEPT_DE. Under FINISH it ends with the saved."""

    def _gapped_take(self, dm, gap=('leader', None)):
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.note_take_gap(gap)
        dm.record_early_save()
        tick(dm)                                   # save() judges

    def test_two_redos_then_the_third_is_kept_with_the_warning(self):
        dm, up = make(n=2, reset=0)
        self._gapped_take(dm)
        self.assertEqual(dm.get_status(), 'reset')
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(dm.get_current_record_status().error,
                         '[WARNUNG] ' + _texts().source_gap_de('leader', None, 1))
        self._gapped_take(dm)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self._gapped_take(dm, gap=('camera', 'scene'))
        self.assertEqual(dm._lerobot_dataset.committed, 1)   # the third is kept
        self.assertEqual(dm.get_current_record_status().error,
                         '[WARNUNG] ' + _texts().source_gap_kept_de('camera', 'scene', 1))

    def test_the_cap_restarts_for_the_next_episode(self):
        dm, _ = make(n=3, reset=0)
        for _ in range(3):
            self._gapped_take(dm)
        ticks(dm, 2)                                # episode 1 counted
        self.assertEqual(dm._record_episode_count, 1)
        self._gapped_take(dm)                       # episode 2's first gap: a redo
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm.get_status(), 'reset')

    def test_a_take_without_a_gap_commits(self):
        dm, _ = make(n=2, reset=0)
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.note_take_gap(None)
        dm.record_early_save()
        tick(dm)
        self.assertEqual(dm._lerobot_dataset.committed, 1)

    def test_the_gap_belongs_to_one_take(self):
        dm, _ = make(n=2, reset=0)
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.note_take_gap(('follower', None))
        dm.rerecord_from_command()                  # the take is discarded anyway
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.record_early_save()
        tick(dm)
        self.assertEqual(dm._lerobot_dataset.committed, 1)

    def test_a_note_after_the_commit_is_ignored(self):
        dm, _ = make(n=2)
        run_until(dm, 'save')
        tick(dm)                                    # committed, latched
        dm.note_take_gap(('leader', None))
        self.assertIsNone(dm._take_gap_source)

    def test_a_gap_under_finish_ends_with_the_saved_episodes(self):
        dm, up = make(n=3, reset=0)
        run_until(dm, 'reset')                      # episode 1 saved
        run_until(dm, 'run')
        ticks(dm, 45)
        dm.note_take_gap(('camera', 'gripper'))
        dm.record_finish()
        statuses = status_seq(dm, 6)
        self.assertIn('DONE', statuses)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)
        warnings = [e for st in statuses if st != 'DONE' for e in [st[2]] if e]
        self.assertTrue(any(w.startswith('[WARNUNG] Signalaussetzer: Die Greifer-Kamera hat '
                                         'in Episode 2 kurz keine Daten geliefert. Die Episode '
                                         'wurde verworfen') for w in warnings), warnings)
        self.assertEqual(len(up), 1)


class FrameLossCapTest(_FsmTestCase):
    """C7: a frame loss re-records the take (FRAME_LOSS_REDO_DE); the third of
    the same episode ends the session like „Beenden“ (FRAME_LOSS_END_DE), saved
    episodes kept, finalized and uploaded."""

    def _lossy_take(self, dm):
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.record_early_save()
        tick(dm)

    def test_two_redos_then_the_session_ends(self):
        dm, up = make(n=3, reset=0)
        run_until(dm, 'reset')                       # episode 1 saved cleanly
        dm._lerobot_dataset.drop_on_save = True
        self._lossy_take(dm)
        self.assertEqual(dm.get_status(), 'reset')
        self.assertEqual(dm.get_current_record_status().error,
                         '[WARNUNG] ' + _texts().frame_loss_redo_de(2))
        self._lossy_take(dm)
        self.assertEqual(dm.get_status(), 'reset')
        self._lossy_take(dm)                         # the third: end
        self.assertEqual(dm.get_status(), 'finish')
        self.assertEqual(dm.get_current_record_status().error,
                         '[WARNUNG] ' + _texts().frame_loss_end_de(2))
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)
        self.assertTrue(dm._lerobot_dataset.finalized)
        self.assertEqual(len(up), 1)

    def test_the_frame_loss_check_comes_before_the_gap_cap(self):
        # A gapped take that may be kept is still never kept with lost frames:
        # with the cap reached, the loss ends the session instead.
        dm, _ = make(n=3, reset=0)
        dm._redos[1] = 2                             # the shared cap is reached
        dm._lerobot_dataset.drop_on_save = True
        run_until(dm, 'run')
        ticks(dm, 40)
        dm.note_take_gap(('leader', None))
        dm.record_early_save()
        tick(dm)
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(dm.get_status(), 'finish')

    def test_a_loss_under_stop_completes_the_stop(self):
        dm, up = make(n=3, reset=0)
        run_until(dm, 'reset')
        run_until(dm, 'run')
        ticks(dm, 45)
        dm._lerobot_dataset.drop_on_save = True
        dm.record_stop()
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(dm._lerobot_dataset.committed, 1)


class SharedRedoCapTest(_FsmTestCase):
    """Round 6: ONE re-record cap of MAX_REDOS_PER_EPISODE (2) per episode,
    shared by source-gap and frame-loss discards. A third gapped take is kept
    with GAP_KEPT_DE; a third lossy take ends the session (C7) — reachable
    whatever mix led there."""

    def _take(self, dm, *, gap=False, drop=False):
        run_until(dm, 'run')
        ticks(dm, 40)
        dm._lerobot_dataset.drop_on_save = drop
        if gap:
            dm.note_take_gap(('leader', None))
        dm.record_early_save()
        tick(dm)
        dm._lerobot_dataset.drop_on_save = False

    def test_the_cap_is_two(self):
        MAX_REDOS_PER_EPISODE = MOD.MAX_REDOS_PER_EPISODE
        self.assertEqual(MAX_REDOS_PER_EPISODE, 2)

    def test_gap_drop_gap_keeps_the_third(self):
        dm, _ = make(n=2, reset=0)
        self._take(dm, gap=True)
        self.assertEqual(dm.get_status(), 'reset')
        self._take(dm, drop=True)
        self.assertEqual(dm.get_status(), 'reset')
        self._take(dm, gap=True)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm.get_current_record_status().error,
                         '[WARNUNG] ' + _texts().source_gap_kept_de('leader', None, 1))

    def test_gap_gap_drop_ends_the_session(self):
        dm, up = make(n=2, reset=0)
        self._take(dm, gap=True)
        self._take(dm, gap=True)
        self._take(dm, drop=True)
        self.assertEqual(dm.get_status(), 'finish')
        self.assertEqual(dm.get_current_record_status().error,
                         '[WARNUNG] ' + _texts().frame_loss_end_de(1))
        # Round 7: one frame loss after two gaps — the end sentence counts
        # nothing it cannot know.
        self.assertNotIn('dreimal', dm.get_current_record_status().error)
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(up, [])

    def test_drop_drop_gap_keeps_the_third_without_a_wrong_wieder(self):
        # Round 7: the third take is the FIRST gap; the kept sentence must not
        # say „wieder“ nor count one kind.
        dm, _ = make(n=2, reset=0)
        self._take(dm, drop=True)
        self.assertEqual(dm.get_status(), 'reset')
        self._take(dm, drop=True)
        self.assertEqual(dm.get_status(), 'reset')
        self._take(dm, gap=True)
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        error = dm.get_current_record_status().error
        self.assertEqual(error,
                         '[WARNUNG] ' + _texts().source_gap_kept_de('leader', None, 1))
        self.assertNotIn('wieder ', error)
        self.assertNotIn('dreimal', error)

    def test_drop_gap_drop_ends_the_session(self):
        dm, _ = make(n=2, reset=0)
        self._take(dm, drop=True)
        self._take(dm, gap=True)
        self._take(dm, drop=True)
        self.assertEqual(dm.get_status(), 'finish')

    def test_a_saved_episode_restarts_the_count(self):
        dm, _ = make(n=3, reset=0)
        self._take(dm, gap=True)
        self._take(dm, drop=True)
        self._take(dm)                                # saved: episode 1 done
        ticks(dm, 2)
        self.assertEqual(dm._record_episode_count, 1)
        self._take(dm, drop=True)                     # episode 2's first: a redo
        self.assertEqual(dm.get_status(), 'reset')


class _BlockingDiscardDataset(_FakeDataset):
    """The official discard waits (like LeRobot's ~1 s cancel) until released."""

    def __init__(self):
        super().__init__()
        import threading
        self.in_discard = threading.Event()
        self.release = threading.Event()

    def discard_episode(self):
        self.in_discard.set()
        self.release.wait(5)
        super().discard_episode()


class QueuedEndTest(_FsmTestCase):
    """F1 (round 6, owner: the server queues the end): FINISH/STOP never wait
    for the recorder lock and are never refused as busy. Applied at once when
    the lock is free, else the moment the holder releases it (like the
    collision discard) — so „Verwerfen und beenden“ (RERECORD, then FINISH while
    the official discard runs) always ends the session with the discarded take
    dropped; a run that started after the RERECORD is still dropped (Q4)."""

    def _discarding(self, reset):
        import threading
        dm, up = make(n=3, reset=reset)
        dm._lerobot_dataset = _BlockingDiscardDataset()
        run_until(dm, 'reset')                        # episode 1 saved
        run_until(dm, 'run')
        ticks(dm, 45)                                 # 1.5 s into episode 2
        self.assertTrue(dm.rerecord_from_command())
        tick_thread = threading.Thread(target=tick, args=(dm,))
        tick_thread.start()                           # the tick runs the discard
        self.assertTrue(dm._lerobot_dataset.in_discard.wait(5))
        return dm, up, tick_thread

    def test_finish_during_the_discard_is_accepted_and_ends_the_session(self):
        import time as real_time
        for reset in (0, 2):
            with self.subTest(reset=reset):
                dm, up, tick_thread = self._discarding(reset)
                started = real_time.monotonic()
                self.assertFalse(dm.request_end('finish'))     # queued, not refused
                self.assertLess(real_time.monotonic() - started, 0.05)
                self.assertNotEqual(dm.get_status(), 'finish')
                dm._lerobot_dataset.release.set()
                tick_thread.join(5)
                self.assertEqual(dm.get_status(), 'finish')
                self.assertIn(True, ticks(dm, 6))
                self.assertEqual(dm._lerobot_dataset.committed, 1)   # only episode 1
                self.assertEqual(dm._record_episode_count, 1)
                self.assertEqual(len(up), 1)

    def test_stop_is_queued_the_same_way(self):
        dm, up, tick_thread = self._discarding(0)
        self.assertFalse(dm.request_end('stop'))
        dm._lerobot_dataset.release.set()
        tick_thread.join(5)
        self.assertEqual(dm.get_status(), 'stop')
        self.assertIn(True, ticks(dm, 6))
        self.assertEqual(dm._record_episode_count, 1)

    def test_discard_in_flight_only_while_a_discard_is_pending_or_running(self):
        # Round 7: what tells the command path to answer FINISH_QUEUED_DE.
        dm, up, tick_thread = self._discarding(2)
        self.assertTrue(dm.discard_in_flight())          # inside the discard
        dm._lerobot_dataset.release.set()
        tick_thread.join(5)
        self.assertFalse(dm.discard_in_flight())
        dm2, _ = make(n=3)
        run_until(dm2, 'run')
        ticks(dm2, 45)
        self.assertFalse(dm2.discard_in_flight())
        dm2.rerecord_from_command()
        self.assertTrue(dm2.discard_in_flight())         # pending, next tick runs it
        tick(dm2)
        self.assertFalse(dm2.discard_in_flight())

    def test_a_free_lock_applies_the_end_now(self):
        dm, _ = make()
        run_until(dm, 'run')
        ticks(dm, 45)
        self.assertTrue(dm.request_end('finish'))
        self.assertEqual(dm.get_status(), 'finish')

    def test_q4_still_drops_a_run_that_started_after_the_rerecord(self):
        import threading
        dm, up = make(n=3, reset=0)
        run_until(dm, 'reset')
        run_until(dm, 'run')
        ticks(dm, 45)
        dm.rerecord_from_command()
        run_until(dm, 'run')
        ticks(dm, 40)                                 # > 1 s: Q3 alone would keep it
        held, release = threading.Event(), threading.Event()

        def _holder():
            with dm.locked():
                held.set()
                release.wait(5)
        t = threading.Thread(target=_holder)
        t.start()
        self.assertTrue(held.wait(5))
        self.assertFalse(dm.request_end('finish'))
        release.set()
        t.join(5)
        self.assertEqual(dm.get_status(), 'finish')
        self.assertIn(True, ticks(dm, 6))
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(dm._lerobot_dataset.committed, 1)

    def test_a_collision_queued_with_the_end_discards_first(self):
        # Rule §2: the take a collision interrupted is discarded, never saved
        # by a FINISH that happened to be queued beside it.
        import threading
        dm, up = make(n=3, reset=0)
        run_until(dm, 'run')
        ticks(dm, 45)
        held, release = threading.Event(), threading.Event()

        def _holder():
            with dm.locked():
                held.set()
                release.wait(5)
        t = threading.Thread(target=_holder)
        t.start()
        self.assertTrue(held.wait(5))
        self.assertFalse(dm.request_end('finish'))
        self.assertFalse(dm.request_collision_discard())
        release.set()
        t.join(5)
        self.assertIn(True, ticks(dm, 6))
        self.assertEqual(dm._lerobot_dataset.committed, 0)
        self.assertEqual(up, [])


class SourceStopTest(_FsmTestCase):
    """R5-2: a required source that stops ends the session like „Verwerfen und
    beenden“ — the running take dropped whatever its length, saved kept,
    finalize + upload by the usual guards, once per session."""

    def test_mid_run_the_take_is_dropped_and_the_saved_kept(self):
        dm, up = make(n=3, reset=0)
        run_until(dm, 'reset')
        run_until(dm, 'run')
        ticks(dm, 60)                                # 2 s: FINISH alone would keep it
        sentence = _texts().source_stop_de('camera', 'scene', take_dropped=True)
        self.assertTrue(dm.end_for_source_stop(sentence))
        self.assertEqual(dm.get_current_record_status().error, '[WARNUNG] ' + sentence)
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._lerobot_dataset.committed, 1)
        self.assertEqual(dm._record_episode_count, 1)
        self.assertEqual(len(up), 1)

    def test_once_per_session_and_only_in_warmup_run_reset(self):
        dm, _ = make()
        tick(dm)
        self.assertTrue(dm.end_for_source_stop('Aufnahme beendet: x'))
        self.assertFalse(dm.end_for_source_stop('Aufnahme beendet: y'))
        for state in ('save_unlatched', 'save_latched', 'finish_unlatched', 'stop'):
            with self.subTest(state=state):
                dm, _ = _reach(state, 3)
                self.assertFalse(dm.end_for_source_stop('Aufnahme beendet: z'))

    def test_in_reset_it_finishes_without_a_take(self):
        dm, up = make(n=3, reset=2)
        run_until(dm, 'reset')
        self.assertTrue(dm.end_for_source_stop('Aufnahme beendet: x'))
        self.assertIn(True, ticks(dm, 4))
        self.assertEqual(dm._record_episode_count, 1)


class _OpenedDataset:
    def __init__(self, repo_id=None, root=None, fps=30):
        self.fps = fps
        self.robot_type = None

    def set_robot_type(self, robot_type):
        self.robot_type = robot_type

    def start_image_writer(self, **kw):
        pass


class _Img:
    shape = (480, 640, 3)


class ResumeCompatibilityTest(_FsmTestCase):
    """D4: resuming an existing dataset goes through LeRobot's OWN
    compatibility check; a mismatch is refused in German by its first field,
    the dataset is not installed and nothing is written."""

    def setUp(self):
        self._saved = (MOD._resume_compatibility_check, MOD.LeRobotDatasetWrapper)
        MOD.LeRobotDatasetWrapper = lambda repo, root: _OpenedDataset(fps=25)
        self.checks = []

    def tearDown(self):
        MOD._resume_compatibility_check, MOD.LeRobotDatasetWrapper = self._saved

    def _dm(self, error=None):
        dm, _ = make()
        dm._lerobot_dataset = None
        dm._check_dataset_exists = lambda repo, root: True

        def _check(dataset, robot_type, fps, features):
            self.checks.append((robot_type, fps, sorted(features)))
            if error:
                raise ValueError(error)
        MOD._resume_compatibility_check = _check
        return dm

    def test_same_rig_resumes(self):
        dm = self._dm()
        self.assertTrue(dm.check_lerobot_dataset({'scene': _Img()}, ['j1', 'j2']))
        self.assertIsNotNone(dm._lerobot_dataset)
        self.assertEqual(dm._lerobot_dataset.robot_type, 'omx_f')
        robot_type, fps, features = self.checks[0]
        self.assertEqual((robot_type, fps), ('omx_f', 30.0))
        self.assertIn('observation.images.scene', features)
        self.assertIn('observation.state', features)

    def test_another_fps_is_refused_naming_the_datasets_fps(self):
        dm = self._dm('Dataset metadata compatibility check failed with mismatches:\n'
                      'fps: expected 30, got 25')
        self.assertFalse(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        self.assertIsNone(dm._lerobot_dataset)
        self.assertEqual(dm._last_warning_message,
                         _texts().resume_fps_de('Würfel in die Schale', 25))

    def test_other_features_or_robot_are_refused(self):
        for line, expected in (
                ("features: expected {...}, got {...}",
                 _texts().resume_features_de('Würfel in die Schale')),
                ('robot_type: expected omx_f, got edu6_studio',
                 _texts().resume_robot_de('Würfel in die Schale'))):
            with self.subTest(line=line):
                dm = self._dm('Dataset metadata compatibility check failed with '
                              'mismatches:\n' + line)
                self.assertFalse(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
                self.assertEqual(dm._last_warning_message, expected)

    def test_the_first_mismatching_field_decides(self):
        dm = self._dm('Dataset metadata compatibility check failed with mismatches:\n'
                      'robot_type: expected omx_f, got x\nfps: expected 30, got 25')
        dm.check_lerobot_dataset({'scene': _Img()}, ['j1'])
        self.assertEqual(dm._last_warning_message,
                         _texts().resume_robot_de('Würfel in die Schale'))


class _FakeHub:
    def __init__(self, *, whoami_error=None, exists=False, exists_error=None, hang=None):
        self.whoami_error = whoami_error
        self.exists = exists
        self.exists_error = exists_error
        self.hang = hang                     # an Event the whoami waits on (a black hole)
        self.calls = []

    def __call__(self, *a, **k):
        return self

    def whoami(self):
        self.calls.append('whoami')
        if self.hang is not None:
            self.hang.wait(10)
        if self.whoami_error:
            raise self.whoami_error
        return {'name': 'maxmuster', 'orgs': []}

    def repo_exists(self, repo_id, repo_type=None):
        self.calls.append(('repo_exists', repo_id, repo_type))
        if self.exists_error:
            raise self.exists_error
        return self.exists


def _http_error(code):
    error = RuntimeError(f'HTTP {code}')
    error.response = types.SimpleNamespace(status_code=code)
    return error


class HubExistenceCheckTest(_FsmTestCase):
    """D7: a LOGGED-IN existence check; a failure to ask is never read as
    „absent“. Daten 2.0 (D14, §C4): an UNREACHABLE hub no longer refuses — the
    session records a new local dataset with OFFLINE_START_DE, `_sync_base`
    stays SYNC_UNCHECKED, and the guarded end-of-session upload decides by
    itself (it never overwrites a differing hub dataset)."""

    def setUp(self):
        self._saved = (MOD.HfApi, MOD.LeRobotDatasetWrapper)
        MOD.LeRobotDatasetWrapper = lambda repo, root: _OpenedDataset()
        MOD._resume_compatibility_check, self._saved_check = (
            lambda *a, **k: None, MOD._resume_compatibility_check)

    def tearDown(self):
        MOD.HfApi, MOD.LeRobotDatasetWrapper = self._saved
        MOD._resume_compatibility_check = self._saved_check

    def _dm(self, hub, push=True):
        MOD.HfApi = hub
        dm, _ = make(push=push)
        dm._lerobot_dataset = None
        self.created = []
        self.downloaded = []
        dm._create_dataset = lambda repo, images, joints: self.created.append(repo) or \
            _OpenedDataset()
        dm._download_dataset = lambda repo: self.downloaded.append(repo)
        return dm

    def _assert_offline(self, dm):
        self.assertTrue(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        self.assertEqual(self.created, ['maxmuster/omx_f_Wuerfel-in-die-Schale'])
        self.assertTrue(dm._task_info.push_to_hub)               # the upload still runs, guarded
        self.assertEqual(dm._sync_base, MOD.SYNC_UNCHECKED)
        self.assertEqual(dm.get_current_record_status().error,
                         '[WARNUNG] ' + _texts().OFFLINE_START_DE)

    def test_an_unreachable_hub_records_offline(self):
        for error in (ConnectionError('refused'), TimeoutError('timed out'),
                      _http_error(503), _http_error(429)):
            with self.subTest(error=error):
                self._assert_offline(self._dm(_FakeHub(whoami_error=error)))

    def test_a_failing_existence_query_records_offline_too(self):
        self._assert_offline(self._dm(_FakeHub(exists_error=RuntimeError('503'))))

    def test_a_refused_existence_query_names_the_token(self):
        # The token passed whoami but the hub refuses the repo query (401/403).
        dm = self._dm(_FakeHub(exists_error=_http_error(403)))
        self.assertFalse(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        self.assertEqual(self.created, [])
        self.assertEqual(dm._last_warning_message, _texts().HUB_CHECK_AUTH_DE)

    def test_no_token_starts_without_upload_and_says_so(self):
        # F4 (round 6): no token stored -> the session records, upload OFF.
        error = type('LocalTokenNotFoundError', (Exception,), {})('Token is required')
        dm = self._dm(_FakeHub(whoami_error=error))
        self.assertTrue(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        self.assertEqual(self.created, ['maxmuster/omx_f_Wuerfel-in-die-Schale'])
        self.assertFalse(dm._task_info.push_to_hub)
        self.assertEqual(dm.get_current_record_status().error,
                         '[WARNUNG] ' + _texts().UPLOAD_OFF_NO_TOKEN_DE)

    def test_an_expired_token_starts_without_upload_and_says_so(self):
        for error in (_http_error(401), RuntimeError('401 Unauthorized')):
            with self.subTest(error=error):
                dm = self._dm(_FakeHub(whoami_error=error))
                self.assertTrue(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
                self.assertFalse(dm._task_info.push_to_hub)
                self.assertEqual(dm.get_current_record_status().error,
                                 '[WARNUNG] ' + _texts().UPLOAD_OFF_TOKEN_INVALID_DE)

    def test_a_session_without_upload_never_uploads(self):
        dm = self._dm(_FakeHub(whoami_error=_http_error(401)))
        uploads = []
        dm._upload_callback = lambda *a: uploads.append(a)
        self.assertTrue(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        dm._lerobot_dataset = _FakeDataset()
        dm.create_frame = lambda images, state, action: {'x': 1}
        for _ in range(3000):
            if tick(dm):
                break
        self.assertEqual(dm._record_episode_count, 3)
        self.assertEqual(uploads, [])

    def test_a_black_holed_hub_records_offline_within_the_bound(self):
        import threading
        import time as real_time
        self.assertEqual(MOD.HUB_CHECK_TIMEOUT_S, 15.0)
        saved = MOD.HUB_CHECK_TIMEOUT_S
        MOD.HUB_CHECK_TIMEOUT_S = 0.3
        hang = threading.Event()
        try:
            dm = self._dm(_FakeHub(hang=hang))
            started = real_time.monotonic()
            self._assert_offline(dm)
            elapsed = real_time.monotonic() - started
        finally:
            MOD.HUB_CHECK_TIMEOUT_S = saved
            hang.set()
        self.assertLess(elapsed, 1.5)

    def test_absent_creates_a_new_dataset(self):
        hub = _FakeHub(exists=False)
        dm = self._dm(hub)
        self.assertTrue(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        self.assertEqual(self.created, ['maxmuster/omx_f_Wuerfel-in-die-Schale'])
        self.assertEqual(hub.calls[0], 'whoami')
        self.assertEqual(hub.calls[1], ('repo_exists', 'maxmuster/omx_f_Wuerfel-in-die-Schale',
                                        'dataset'))

    def test_present_downloads_and_resumes(self):
        dm = self._dm(_FakeHub(exists=True))
        self.assertTrue(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        self.assertEqual(self.downloaded, ['maxmuster/omx_f_Wuerfel-in-die-Schale'])
        self.assertEqual(self.created, [])

    def test_a_failed_download_refuses_instead_of_creating(self):
        dm = self._dm(_FakeHub(exists=True))

        def _boom(repo):
            raise ConnectionError('reset by peer')
        dm._download_dataset = _boom
        self.assertFalse(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        self.assertEqual(self.created, [])
        self.assertTrue(dm._last_warning_message)

    def test_upload_off_never_asks_the_hub(self):
        hub = _FakeHub(whoami_error=ConnectionError('no network'))
        dm = self._dm(hub, push=False)
        self.assertTrue(dm.check_lerobot_dataset({'scene': _Img()}, ['j1']))
        self.assertEqual(hub.calls, [])


class SavedLengthCheckTest(_FsmTestCase):
    """The post-save length check: a camera whose saved video span differs from
    length / fps by more than half a frame is named in German (detect + inform,
    LeRobot's train-time FrameTimestampError condition)."""

    def _dm(self, spans, length=90):
        dm, _ = make()
        latest = {'length': [length]}
        for key, (a, b) in spans.items():
            latest[f'videos/{key}/from_timestamp'] = [a]
            latest[f'videos/{key}/to_timestamp'] = [b]
        meta = types.SimpleNamespace(latest_episode=latest, fps=30,
                                     video_keys=list(spans))
        dm._verify_saved_lengths(meta, list(spans))
        return dm

    def test_matching_spans_say_nothing(self):
        dm = self._dm({'observation.images.scene': (6.0, 9.0),
                       'observation.images.gripper': (6.0, 9.01)})
        self.assertEqual(dm._last_warning_message, '')

    def test_a_short_video_is_named(self):
        dm = self._dm({'observation.images.scene': (6.0, 8.9),
                       'observation.images.gripper': (6.0, 9.0)})
        self.assertEqual(
            dm._last_warning_message,
            'Episode 1: Video und Daten der Kamera(s) Szenen-Kamera sind nicht gleich '
            'lang. Diese Episode muss neu aufgenommen werden, sonst bricht das Training ab.')

    def test_no_latest_episode_is_no_check(self):
        dm, _ = make()
        dm._verify_saved_lengths(types.SimpleNamespace(latest_episode=None), ['x'])
        self.assertEqual(dm._last_warning_message, '')


class DatasetCardAndHfReasonTest(_FsmTestCase):
    """O5/D6: the dataset README is LeRobot's card, rebuilt on EVERY upload
    (dataset_card.build_dataset_card: repo_id, info.json, tags, public iff not
    private). R5-4b: an upload failure is reported by its cause."""

    def test_the_readme_is_rebuilt_every_upload(self):
        calls = []
        saved = MOD.dataset_card.build_dataset_card
        MOD.dataset_card.build_dataset_card = (
            lambda repo_id, info, tags, public: calls.append(
                (repo_id, info, tags, public)) or f'---\nrepo: {repo_id}\n---\n')
        try:
            root = Path(tempfile.mkdtemp(prefix='dm_card_'))
            _TEMP_ROOTS.append(root)
            (root / 'meta').mkdir()
            (root / 'meta' / 'info.json').write_text('{"robot_type": "omx_f", "fps": 30}')
            (root / 'README.md').write_text('old')
            DataManager._create_readme_if_not_exists(root, 'dataset', repo_id='u/r',
                                                     private=False)
            self.assertEqual((root / 'README.md').read_text(), '---\nrepo: u/r\n---\n')
            DataManager._create_readme_if_not_exists(root, 'dataset', repo_id='u/r')
        finally:
            MOD.dataset_card.build_dataset_card = saved
        self.assertEqual(calls[0], ('u/r', {'robot_type': 'omx_f', 'fps': 30},
                                    ['robotis', 'omx_f'], True))
        self.assertFalse(calls[1][3])                 # private (the default): no licence

    def test_hf_failures_are_classified_by_cause(self):
        self.assertEqual(DataManager._classify_hf_failure_de(ConnectionError('refused')),
                         _texts().HF_NETWORK_ERROR_DE)
        self.assertEqual(DataManager._classify_hf_failure_de(RuntimeError('401 Unauthorized')),
                         _texts().HF_AUTH_ERROR_DE)
        self.assertIsNone(DataManager._classify_hf_failure_de(RuntimeError('odd')))
        self.assertEqual(DataManager.HF_AUTH_ERROR_DE, _texts().HF_AUTH_ERROR_DE)


if __name__ == '__main__':
    unittest.main()
