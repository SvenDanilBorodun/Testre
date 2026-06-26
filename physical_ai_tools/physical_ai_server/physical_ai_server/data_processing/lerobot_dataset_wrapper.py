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
    length, replacing the v2.4 in-RAM safety valve (formerly toggled by
    an env var that no longer exists; see CLAUDE.md "Recent changes").

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

    # ---- v0.5.1 lifecycle bridges (DatasetWriter moved these off the dataset) ----
    # In LeRobot v0.5.1 the in-flight episode buffer, the image writer, and the
    # streaming encoder all live on self.writer (DatasetWriter), not on the
    # dataset object. data_manager.py still uses the pre-v0.5.1 call shapes
    # (self._lerobot_dataset.episode_buffer, a 2-arg add_frame, and a direct
    # start_image_writer). These three bridges keep those call sites working
    # without a full state-machine refactor, so the recording loop runs against
    # real v0.5.1 instead of raising AttributeError/TypeError on the first save.

    @property
    def episode_buffer(self):
        """Bridge data_manager.py's ``self._lerobot_dataset.episode_buffer``
        reads onto the v0.5.1 DatasetWriter. Returns None before the first
        frame or when the dataset is read-only (writer is None)."""
        writer = getattr(self, 'writer', None)
        if writer is None:
            return None
        return getattr(writer, 'episode_buffer', None)

    @episode_buffer.setter
    def episode_buffer(self, value):
        """Bridge the ``episode_buffer = None`` teardown in
        data_manager._episode_reset onto the writer. Setting None is safe:
        upstream add_frame lazily recreates the buffer via
        _create_episode_buffer(), and save_episode() already resets it to a
        fresh empty buffer on its own."""
        writer = getattr(self, 'writer', None)
        if writer is not None:
            writer.episode_buffer = value

    def add_frame(self, frame: dict, task: str = None, **kwargs) -> None:
        """v0.5.1 add_frame takes a single ``frame`` dict that must carry a
        ``'task'`` key. The non-optimized-save call site in data_manager.py
        passes the task as a second positional arg (the pre-v0.5.1 signature);
        accept it and fold it into the dict so both the one-arg (upstream) and
        two-arg (legacy) call styles work."""
        if task is not None:
            frame = dict(frame)
            frame['task'] = task
        return super().add_frame(frame, **kwargs)

    def start_image_writer(self, num_processes: int = 0, num_threads: int = 4) -> None:
        """v0.5.1 moved start_image_writer onto the DatasetWriter. Forward to
        it so data_manager.py's non-optimized-save branch keeps working. With
        streaming_encoding=True (our default) and all-video camera features
        there are no PNG image-features to write, so this is effectively a
        no-op — but we forward for correctness if image-dtype features are
        ever added."""
        writer = getattr(self, 'writer', None)
        if writer is not None and hasattr(writer, 'start_image_writer'):
            writer.start_image_writer(num_processes, num_threads)

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

    # ---------- Streaming frame-drop accounting ----------
    # With streaming_encoding=True, the per-camera StreamingVideoEncoder drops
    # frames when its bounded queue stays full (encoder can't keep up — e.g. a
    # CPU-starved student PC, video_utils.StreamingVideoEncoder.feed_frame). It
    # only logs a warning; meanwhile add_frame still appends a parquet row (with
    # a None video placeholder), so a dropped frame leaves the encoded video
    # SHORTER than the data parquet for that episode. At train time LeRobot then
    # raises FrameTimestampError. The encoder exposes a per-camera drop counter
    # (_dropped_frames), reset at each episode's first frame (start_episode) and
    # NOT cleared by finish_episode — so it must be read BEFORE save_episode()
    # commits, which data_manager.save() does to discard + re-record the episode.

    def streaming_dropped_frame_count(self) -> int:
        """Total frames the streaming encoder dropped for the CURRENT (in-flight)
        episode, summed across cameras. Returns 0 when streaming is disabled or
        the encoder internals are unavailable — i.e. "no detection" rather than a
        false positive."""
        writer = getattr(self, 'writer', None)
        enc = getattr(writer, '_streaming_encoder', None) if writer is not None else None
        dropped = getattr(enc, '_dropped_frames', None) if enc is not None else None
        if not dropped:
            return 0
        try:
            return int(sum(int(v) for v in dropped.values()))
        except (TypeError, ValueError, AttributeError):
            return 0

    def cancel_streaming_episode(self) -> None:
        """Discard the in-flight streaming episode (its temp mp4 + encoder
        threads) so a dropped-frame episode can be cleanly re-recorded. Safe
        no-op when streaming is disabled. The next episode's start_episode would
        also cancel a stale active episode, but doing it here frees the threads
        and temp file promptly."""
        writer = getattr(self, 'writer', None)
        enc = getattr(writer, '_streaming_encoder', None) if writer is not None else None
        if enc is not None and hasattr(enc, 'cancel_episode'):
            try:
                enc.cancel_episode()
            except Exception:  # noqa: BLE001 — discard must never raise
                pass

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
        """No-op — the v2.4 in-RAM safety valve is replaced by upstream
        streaming_encoding. The data_manager.py reset call is now harmless."""
        return None
