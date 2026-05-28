#!/usr/bin/env python3
#
# Copyright 2025 ROBOTIS CO., LTD.
# Modified 2026-05 for EduBotics v2.5.0 — streaming_encoding replaces the
# v2.4 JPEG-in-RAM hack. Most of the prior wrapper became upstream-native.
#
# Licensed under the Apache License, Version 2.0 (the "License").

"""Thin EduBotics shim over LeRobotDataset.

EduBotics v2.5.0 (LeRobot v0.5.1) defaults vcodec='h264' + streaming_encoding=True
so:
  - Student PCs always record with software H.264 (the upstream default is
    libsvtav1, which is slow to decode on Windows 10 CPUs in the React preview).
  - No PNG-per-frame temp files written to disk during recording. Frames go
    directly into ffmpeg via StreamingVideoEncoder. Episode-buffer image slots
    hold None placeholders; in-RAM footprint is bounded regardless of episode
    length, replacing the EDUBOTICS_MAX_BUFFER_GB safety valve that was
    fundamental to the v2.4 JpegFrame architecture.

Wrapper-only methods kept ONLY for backward compatibility with data_manager.py
call sites — every one is a thin alias that delegates to upstream
LeRobotDataset / DatasetWriter. The 460+ lines of forked save logic, JpegFrame
class, _LazyDecodeBuffer, per-camera FFmpegEncoder thread management, and
compute_episode_stats_buffer are GONE. Upstream LeRobot v0.5.1 owns all of it.
"""

import os

from lerobot.datasets.lerobot_dataset import LeRobotDataset


# v0.5.1 vcodec allowed set: {'auto','h264','hevc','libsvtav1', plus HW encoders}.
# 'h264' = software x264 (libx264 underneath) — fast CPU decode on student
# Windows 10 PCs in the React video preview.
EDUBOTICS_VCODEC = os.environ.get('EDUBOTICS_VCODEC', 'h264')


def _apply_edubotics_defaults(kwargs: dict) -> dict:
    """Mutate kwargs in place to set EduBotics' recording defaults if absent."""
    kwargs.setdefault('vcodec', EDUBOTICS_VCODEC)
    kwargs.setdefault('streaming_encoding', True)
    return kwargs


class LeRobotDatasetWrapper(LeRobotDataset):
    """LeRobotDataset with EduBotics recording defaults pre-applied.

    All wrapper-only methods on this class are thin aliases over upstream
    LeRobotDataset methods so existing data_manager.py call sites keep working
    without a full refactor of the recording state machine.
    """

    def __init__(self, *args, **kwargs):
        _apply_edubotics_defaults(kwargs)
        super().__init__(*args, **kwargs)
        self._robot_type = 'default'

    @classmethod
    def create(cls, *args, **kwargs):
        _apply_edubotics_defaults(kwargs)
        return super().create(*args, **kwargs)

    # ---------- EduBotics shim methods (data_manager.py call sites) ----------

    def set_robot_type(self, robot_type: str) -> None:
        """Set the robot_type field. With v0.5.1 we'd prefer to pass robot_type
        at .create() time, but the current data_manager flow sets it post-create
        after the joint list is resolved — keep this method as a back-compat
        alias that also patches meta.info."""
        self._robot_type = robot_type
        if hasattr(self, 'meta') and self.meta is not None and getattr(self.meta, 'info', None) is not None:
            self.meta.info['robot_type'] = robot_type

    def get_episode_index(self):
        """Return the current episode index from the writer's episode buffer."""
        buf = getattr(self.writer, 'episode_buffer', None)
        if buf is None or 'episode_index' not in buf:
            return None
        return buf['episode_index']

    def add_frame_without_write_image(self, frame: dict, task: str) -> None:
        """Alias for upstream add_frame — streaming_encoding=True means there
        are no PNG temp files to skip writing in the first place. We keep the
        name so data_manager.py's optimized-save branch keeps compiling."""
        frame = dict(frame)
        frame['task'] = task
        self.add_frame(frame)

    def save_episode_without_video_encoding(self):
        """Alias for upstream save_episode — streaming inlines video encoding
        during add_frame, so the "without video encoding" semantic is moot."""
        self.save_episode(parallel_encoding=False)

    def save_episode_without_write_image(self):
        """Alias for upstream save_episode — same reason as above."""
        self.save_episode(parallel_encoding=False)

    # ---------- Multi-task / batched-encode no-ops ----------
    # EduBotics v2.5.0 is single-task per recording session (Modal Cloud
    # handles multi-task model training). The wrapper's pre-v2.5.0 multi-task
    # batched-encode path is gone; these stubs satisfy the existing
    # data_manager.py polling logic without doing anything.

    def video_encoding(self) -> None:
        """No-op — streaming encoder finishes inline during save_episode."""
        return None

    def check_video_encoding_completed(self) -> bool:
        """Always True — save_episode blocks on streaming_encoder.finish_episode()
        before returning, so by the time data_manager polls this, encoding is done."""
        return True

    def check_append_buffer_completed(self) -> bool:
        """Always True — single-task flow has no async append buffer."""
        return True

    def append_episode_buffer(self, episode_buffer: dict, episode_length) -> None:
        """No-op — multi-task batched encode is not used in v2.5.0."""
        return None

    # ---------- Removed in v2.5.0 (kept ONLY as no-ops to avoid AttributeError) ----------

    def reset_buffer_accounting(self) -> None:
        """No-op — EDUBOTICS_MAX_BUFFER_GB valve replaced by upstream
        streaming_encoding. The data_manager.py reset call is now harmless."""
        return None
