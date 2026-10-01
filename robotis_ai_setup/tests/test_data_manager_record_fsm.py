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


class _FsmTestCase(unittest.TestCase):
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

    def test_enqueue_failure_sets_the_not_started_sentence(self):
        dm, up = make(n=1, episode=1, warmup=0, upload_raises=True)
        results = ticks(dm, 80)
        self.assertIn(True, results)
        self.assertEqual(dm._upload_blocked_reason_de, MOD.UPLOAD_NOT_STARTED_DE)
        self.assertIn('später im Tab Daten', MOD.UPLOAD_NOT_STARTED_DE)

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
        self.assertFalse(dm.end_after_error())
        self.assertEqual(up, [])

    def test_a_failing_finalize_still_clears_the_marker_and_reports(self):
        dm, up = make(n=3)
        dm._session_marker_enabled = True
        run_until(dm, 'run')
        ticks(dm, 10)
        dm._lerobot_dataset.finalize_raises = True
        self.assertFalse(dm.end_after_error())
        self.assertFalse(dm._session_marker_path().exists())
        self.assertTrue(dm._upload_blocked_reason_de)
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

    def test_data_manager_names_no_private_encoder_attribute(self):
        import ast
        tree = ast.parse(DATA_MANAGER_PATH.read_text(encoding='utf-8'))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for private in self._PRIVATE:
            self.assertNotIn(private, names)

    def test_no_gc_collect_on_the_recording_path(self):
        source = DATA_MANAGER_PATH.read_text(encoding='utf-8')
        self.assertNotIn('gc.collect', source)
        self.assertNotIn('_validate_episode_buffer', source)


if __name__ == '__main__':
    unittest.main()
