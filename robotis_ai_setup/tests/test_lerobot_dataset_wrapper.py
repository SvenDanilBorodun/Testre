#!/usr/bin/env python3
#
# Regression tests for the EduBotics LeRobotDatasetWrapper v0.5.1 bridges.
#
# Context: the v2.5.0 LeRobot v0.2.0 -> v0.5.1 migration moved the in-flight
# episode buffer, the image writer, and the streaming encoder off the dataset
# object and onto the DatasetWriter (self.writer). data_manager.py still uses
# the pre-v0.5.1 call shapes (self._lerobot_dataset.episode_buffer reads/writes,
# a 2-arg add_frame, and a direct start_image_writer). The wrapper bridges those
# call sites onto the writer so the recording loop runs against real v0.5.1
# instead of raising AttributeError/TypeError on the first episode save.
#
# The only recording-path test (test_lerobot_stats_contract.py) was deleted in
# the migration commit (10243f7) with no replacement, which is why those crash
# bugs reached the shipped overlay unnoticed. This file restores coverage of the
# wrapper bridges WITHOUT requiring the real (heavy, GPU) lerobot install — it
# stubs lerobot.datasets.lerobot_dataset.LeRobotDataset with a minimal fake that
# mirrors the v0.5.1 API surface we depend on.

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# v2.5.3: physical-ai-server is now COPY-wholesale — the single source of truth
# is the package tree, not the (deleted) docker overlays/ dir.
WRAPPER_PATH = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
    / 'data_processing' / 'lerobot_dataset_wrapper.py'
)


class _FakeWriter:
    """Stand-in for lerobot v0.5.1 DatasetWriter (only what we bridge to)."""

    def __init__(self):
        self.episode_buffer = {'size': 0, 'task': [], 'episode_index': 0}
        self.image_writer_calls = []

    def start_image_writer(self, num_processes=0, num_threads=4):
        self.image_writer_calls.append((num_processes, num_threads))


class _FakeLeRobotDataset:
    """Minimal stand-in for lerobot v0.5.1 LeRobotDataset.

    Mirrors the contract the wrapper relies on: a one-arg add_frame, a create()
    classmethod, and NO episode_buffer / start_image_writer of its own (those
    moved to the writer in v0.5.1 — accessing them on the dataset is exactly the
    AttributeError the wrapper bridges away).
    """

    def __init__(self, *args, **kwargs):
        self.init_args = args
        self.init_kwargs = kwargs
        self.writer = None
        self.added_frames = []

    def add_frame(self, frame):  # upstream v0.5.1: single positional dict
        self.added_frames.append(frame)

    @classmethod
    def create(cls, *args, **kwargs):
        obj = cls.__new__(cls)
        obj.init_args = args
        obj.init_kwargs = kwargs
        obj.create_kwargs = kwargs
        obj.writer = None
        obj.added_frames = []
        return obj


def _load_wrapper_module():
    lerobot_pkg = types.ModuleType('lerobot')
    datasets_pkg = types.ModuleType('lerobot.datasets')
    ld_mod = types.ModuleType('lerobot.datasets.lerobot_dataset')
    ld_mod.LeRobotDataset = _FakeLeRobotDataset
    sys.modules['lerobot'] = lerobot_pkg
    sys.modules['lerobot.datasets'] = datasets_pkg
    sys.modules['lerobot.datasets.lerobot_dataset'] = ld_mod

    spec = importlib.util.spec_from_file_location(
        '_edubotics_wrapper_under_test', str(WRAPPER_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LeRobotDatasetWrapperBridgeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_wrapper_module()
        cls.Wrapper = cls.mod.LeRobotDatasetWrapper

    def _bare_wrapper(self, with_writer=True):
        """An instance without running __init__ (we don't need the real base
        constructor), with the attributes the bridges touch."""
        w = self.Wrapper.__new__(self.Wrapper)
        w.writer = _FakeWriter() if with_writer else None
        w.added_frames = []
        return w

    # ---- EduBotics recording defaults ----

    def test_init_applies_streaming_and_vcodec_defaults(self):
        ds = self.Wrapper(repo_id='u/x', fps=30, features={})
        self.assertTrue(ds.init_kwargs.get('streaming_encoding'))
        self.assertIn('vcodec', ds.init_kwargs)

    def test_create_applies_streaming_and_vcodec_defaults(self):
        ds = self.Wrapper.create(repo_id='u/x', fps=30, features={})
        self.assertTrue(ds.create_kwargs.get('streaming_encoding'))
        self.assertIn('vcodec', ds.create_kwargs)

    def test_create_respects_explicit_overrides(self):
        ds = self.Wrapper.create(
            repo_id='u/x', fps=30, features={},
            streaming_encoding=False, vcodec='hevc',
        )
        self.assertFalse(ds.create_kwargs.get('streaming_encoding'))
        self.assertEqual(ds.create_kwargs.get('vcodec'), 'hevc')

    # ---- B1: episode_buffer property bridges to the writer ----

    def test_episode_buffer_getter_delegates_to_writer(self):
        w = self._bare_wrapper()
        self.assertIs(w.episode_buffer, w.writer.episode_buffer)
        self.assertEqual(w.episode_buffer['size'], 0)

    def test_episode_buffer_setter_delegates_to_writer(self):
        w = self._bare_wrapper()
        w.episode_buffer = None
        self.assertIsNone(w.writer.episode_buffer)

    def test_episode_buffer_getter_none_when_no_writer(self):
        w = self._bare_wrapper(with_writer=False)
        self.assertIsNone(w.episode_buffer)

    def test_episode_buffer_setter_safe_without_writer(self):
        w = self._bare_wrapper(with_writer=False)
        w.episode_buffer = None  # must not raise

    # ---- B2: add_frame accepts the legacy 2-arg call and folds task in ----

    def test_add_frame_two_arg_injects_task(self):
        w = self._bare_wrapper()
        frame = {'observation.state': [1, 2, 3]}
        w.add_frame(frame, 'pick the cube')
        self.assertEqual(len(w.added_frames), 1)
        self.assertEqual(w.added_frames[0]['task'], 'pick the cube')
        # The caller's dict must not be mutated.
        self.assertNotIn('task', frame)

    def test_add_frame_one_arg_passthrough(self):
        w = self._bare_wrapper()
        frame = {'task': 'already', 'x': 1}
        w.add_frame(frame)
        self.assertEqual(w.added_frames[0], frame)

    def test_add_frame_without_write_image_injects_task(self):
        w = self._bare_wrapper()
        w.add_frame_without_write_image({'x': 1}, 'place')
        self.assertEqual(w.added_frames[0]['task'], 'place')

    # ---- start_image_writer forwards to the writer ----

    def test_start_image_writer_forwards_to_writer(self):
        w = self._bare_wrapper()
        w.start_image_writer(num_processes=1, num_threads=1)
        self.assertEqual(w.writer.image_writer_calls, [(1, 1)])

    def test_start_image_writer_noop_without_writer(self):
        w = self._bare_wrapper(with_writer=False)
        w.start_image_writer(num_processes=1, num_threads=1)  # must not raise

    # ---- no-op shims used by the data_manager state machine ----

    def test_video_encoding_shims(self):
        w = self._bare_wrapper()
        self.assertIsNone(w.video_encoding())
        self.assertTrue(w.check_video_encoding_completed())
        self.assertTrue(w.check_append_buffer_completed())
        self.assertIsNone(w.reset_buffer_accounting())


if __name__ == '__main__':
    unittest.main()
