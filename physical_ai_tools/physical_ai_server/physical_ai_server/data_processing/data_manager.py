#!/usr/bin/env python3
#
# Copyright 2025 ROBOTIS CO., LTD.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Dongyun Kim, Seongwoo Kim

import gc
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

import cv2
from geometry_msgs.msg import Twist
from huggingface_hub import (
    CommitOperationDelete,
    DatasetCard,
    DatasetCardData,
    HfApi,
    ModelCard,
    ModelCardData,
    snapshot_download,
    upload_large_folder
)
from huggingface_hub.errors import LocalTokenNotFoundError, RevisionNotFoundError
from lerobot.datasets.dataset_metadata import CODEBASE_VERSION
from lerobot.datasets.utils import DEFAULT_FEATURES
from nav_msgs.msg import Odometry
import numpy as np
from physical_ai_interfaces.msg import TaskStatus
from physical_ai_server.data_processing import dataset_paths
from physical_ai_server.data_processing.data_converter import DataConverter
from physical_ai_server.data_processing.lerobot_dataset_wrapper import (
    LeRobotDatasetWrapper,
)
from physical_ai_server.data_processing.progress_tracker import (
    HuggingFaceProgressTqdm
)
from physical_ai_server.device_manager.cpu_checker import CPUChecker
from physical_ai_server.device_manager.ram_checker import RAMChecker
from physical_ai_server.device_manager.storage_checker import StorageChecker
import requests
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectory


class DataManager:
    RECORDING = False
    RECORD_COMPLETED = True
    SKIP_TIME = 0.1  # Seconds

    # Progress queue for multiprocessing communication
    _progress_queue = None

    def __init__(
            self,
            save_root_path,
            robot_type,
            task_info,
            upload_callback=None):
        self._robot_type = robot_type
        import re
        safe_task_name = re.sub(r'[^a-zA-Z0-9._-]', '-', task_info.task_name).strip('-')
        # `user_id` is CLIENT-SUPPLIED and was the ONE component here that was
        # never sanitised, while `task_name` beside it always was. That matters
        # because `_save_path` reaches a `shutil.rmtree` in
        # `_check_dataset_exists` (an incomplete dataset is deleted and
        # re-created), and `save_root_path / '<abs>/x'` DISCARDS the root —
        # pathlib's absolute-segment rule again. So an absolute or `..`-bearing
        # user_id turned a per-frame recording check into an arbitrary
        # directory delete. Same sanitiser as task_name, then `..`-collapse,
        # then a confine() that PROVES the result stayed under the root.
        safe_user_id = re.sub(r'[^a-zA-Z0-9._-]', '-', str(task_info.user_id or '')).strip('-')
        # `-` and `.` survive the sanitiser above, so `..` does too. HF user
        # ids cannot be dot-only; anything that is becomes a safe placeholder
        # rather than a traversal.
        if not safe_user_id or set(safe_user_id) <= {'.'}:
            safe_user_id = 'unknown-user'
        self._save_repo_name = f'{safe_user_id}/{robot_type}_{safe_task_name}'
        self._save_path = save_root_path / self._save_repo_name
        # Belt as well as braces: prove it, rather than trusting the sanitiser.
        # A raise here is correct — refusing to record beats deleting a tree.
        self._save_path = dataset_paths.confine(self._save_path, save_root_path)
        self._save_rosbag_path = '/workspace/rosbag2/' + self._save_repo_name
        self._on_saving = False
        self._single_task = len(task_info.task_instruction) == 1
        self._task_info = task_info
        # Wired by the node to HfApiWorker.send_request so the
        # end-of-recording auto-push runs out-of-process, surfaces errors
        # on /huggingface/status, and lets the React side fire the
        # /datasets/register Cloud-API call on success. None when the
        # DataManager is constructed standalone (tests, fallback path).
        self._upload_callback = upload_callback
        # One-shot idempotency guard. The state machine has two call
        # sites that can both reach _upload_dataset on the same tick
        # (the 'finish' branch and the post-loop cap-reached check); the
        # timer also normally stops on RECORD_COMPLETED, but we belt-
        # and-suspenders against a future refactor that loses that stop,
        # plus the joystick re-entry path. Reset to False is intentional
        # only at construction — a new DataManager is built for every
        # recording (see init_robot_control_parameters_from_user_task).
        self._upload_enqueued = False

        self._lerobot_dataset = None
        self._record_episode_count = 0
        # Session-marker state (leLab-comparison PR-3). DISARMED by
        # default; the node arms it for START_RECORD sessions only.
        # (record_inference_mode is refused at START_INFERENCE — the
        # inference timer has no record() path, so such a session would
        # arm the marker while writing nothing.)
        self._session_marker_enabled = False
        self._session_marker_written = False
        self._session_started_unix = 0
        self._start_time_s = 0
        self._proceed_time = 0
        self._status = 'warmup'
        self._cpu_checker = CPUChecker()
        self.data_converter = DataConverter()
        # Propagate the task fps into the action-duration setter so
        # published action messages use the right time_from_start at
        # non-30 Hz recordings. Safe no-op if fps is missing/zero.
        self.data_converter.set_action_duration_from_fps(
            getattr(task_info, 'fps', 0) or 0
        )
        self.force_save_for_safety = False
        self._stop_save_completed = False
        self.current_instruction = ''
        self._current_task = 0
        self._init_task_limits()
        self._current_scenario_number = 0
        # Surfaced to TaskStatus.error as a [WARNUNG] prefix so the React UI
        # renders a banner after the truncated episode saves. Cleared on the
        # next episode reset.
        self._last_warning_message: str = ''
        # Stale-camera detection at recording time (mirrors the inference
        # path's overlay/inference_manager.py:_check_stale_cameras). Without
        # this a frozen USB camera silently writes the same frame to every
        # tick of the dataset — the trained model then learns from a static
        # observation. Hashing 4 sparse 256-byte slices is cheap (~µs per
        # frame) and detects any decoded-pixel change.
        self._last_image_hashes: dict[str, int] = {}
        self._last_image_change_time: dict[str, float] = {}
        self._stale_threshold_s = 2.0
        self._stale_halt_threshold_s = 5.0
        # v2.5.0: streaming_encoding=True (LeRobotDatasetWrapper default) feeds
        # camera frames directly to ffmpeg as they arrive — no per-frame PNG
        # temp files, no in-RAM frame accumulation. The v2.4 JpegFrame buffer
        # + its env-var-toggled safety valve are gone (env var removed from
        # docker-compose.yml; ROS node bounds memory architecturally now).

    def get_status(self):
        return self._status

    def get_save_rosbag_path(self):
        episode_index = self._lerobot_dataset.get_episode_index()
        if episode_index is None:
            return None
        return self._save_rosbag_path + f'/{episode_index}'

    def should_record_rosbag2(self):
        return self._task_info.record_rosbag2

    # ── Crash-recoverable session marker (leLab-comparison PR-3) ─────────
    # A tiny SIBLING json next to the dataset dir (never inside it —
    # push_to_hub uploads the folder verbatim) that exists exactly while a
    # recording session is in flight. A crash/power-cut leaves it behind;
    # the node surfaces a one-shot German notice at the next start so the
    # student knows the dataset is incomplete and can delete it in the
    # Daten tab. DETECT + INFORM ONLY: the buffered (<10) episodes died
    # with the process — finalizing a crashed session is impossible, so
    # nothing here ever auto-finalizes or auto-deletes.

    SESSION_MARKER_SUFFIX = '.session.json'

    def _session_marker_path(self):
        # Telemetry only: a DataManager built without the full __init__
        # (tests construct via __new__; standalone helpers) has no
        # _save_path — markers silently disable rather than ever touching
        # the recording/finalize contract.
        save_path = getattr(self, '_save_path', None)
        if not isinstance(save_path, Path):
            return None
        return save_path.parent / (save_path.name + self.SESSION_MARKER_SUFFIX)

    def _write_session_marker(self):
        try:
            marker = self._session_marker_path()
            if marker is None:
                return
            marker.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({
                'repo_id': getattr(self, '_save_repo_name', ''),
                'episodes_saved': getattr(self, '_record_episode_count', 0),
                'status': getattr(self, '_status', ''),
                'started_unix': getattr(self, '_session_started_unix', 0),
            })
            tmp = marker.with_name(marker.name + '.tmp')
            tmp.write_text(payload, encoding='utf-8')
            os.replace(tmp, marker)
        except Exception as e:  # noqa: BLE001 — telemetry must never block
            print(f'[WARNUNG] Sitzungsmarker konnte nicht geschrieben '
                  f'werden: {e}', file=sys.stderr, flush=True)

    def _clear_session_marker(self):
        try:
            marker = self._session_marker_path()
            if marker is not None:
                marker.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — telemetry must never block
            pass

    def record(
            self,
            images,
            state,
            action):

        if self._start_time_s == 0:
            self._start_time_s = time.perf_counter()
            if (getattr(self, '_session_marker_enabled', False)
                    and not getattr(self, '_session_marker_written', True)):
                self._session_marker_written = True
                self._session_started_unix = time.time()
                self._write_session_marker()

        if self._status == 'warmup':
            self._current_task = 0
            self._current_scenario_number = 0
            if not self._check_time(self._task_info.warmup_time_s, 'run'):
                return self.RECORDING

        elif self._status == 'run':
            # v2.5.0: streaming_encoding=True bounds the in-RAM episode buffer
            # at upstream's frame_index/None placeholder level, so the v2.4
            # in-RAM safety valve is no longer needed — the container can
            # record arbitrarily long episodes.
            if not self._check_time(self._task_info.episode_time_s, 'save'):
                frame = self.create_frame(images, state, action)
                if self._task_info.use_optimized_save_mode:
                    self._lerobot_dataset.add_frame_without_write_image(
                        frame,
                        self.current_instruction)
                else:
                    self._lerobot_dataset.add_frame(
                        frame,
                        self.current_instruction)

        elif self._status == 'save':
            if self._on_saving:
                if (
                    self._lerobot_dataset.check_video_encoding_completed()
                    or (
                        not self._single_task
                        and self._lerobot_dataset.check_append_buffer_completed()
                    )
                ):
                    self._verify_saved_video_files()
                    self._episode_reset()
                    self._record_episode_count += 1
                    self._write_session_marker()
                    self._get_current_scenario_number()
                    self._current_task += 1
                    self._on_saving = False

                    # Check if we've reached the target episode count
                    if (self._record_episode_count <
                            self._task_info.num_episodes):
                        # Not finished yet, go to reset for next episode
                        self._status = 'reset'
                        self._start_time_s = 0
                    else:
                        # Finished! Set status to 'finish' to skip reset
                        self._status = 'finish'
            else:
                # save() returns False when it discarded the episode for
                # re-recording (streaming frame drop) — it has already set
                # _status='reset', so do NOT latch _on_saving.
                if self.save():
                    self._on_saving = True

        elif self._status == 'reset':
            if not self._single_task:
                if not self._check_time(self.SKIP_TIME, 'run'):
                    return self.RECORDING
            else:
                if not self._check_time(self._task_info.reset_time_s, 'run'):
                    return self.RECORDING

        elif self._status == 'skip_task':
            if not self._check_time(self.SKIP_TIME, 'run'):
                return self.RECORDING

        elif self._status == 'stop':
            if not self._stop_save_completed:
                if self._on_saving:
                    if self._lerobot_dataset.check_video_encoding_completed():
                        self._on_saving = False
                        self._episode_reset()
                        self._record_episode_count += 1
                        self._write_session_marker()
                        self._get_current_scenario_number()
                        self._current_task += 1
                        self._stop_save_completed = True
                        # v3.0: 'Stop' must finalize (and upload) exactly like
                        # 'finish' — otherwise a Stop-ended dataset ships a
                        # footer-less data parquet (and, for <10 episodes, no
                        # meta/episodes/*.parquet at all): silent corruption that
                        # only surfaces at train time / on a later manual push.
                        # Flush LeRobot's writers BEFORE upload (see
                        # _finalize_dataset); skip the upload if finalize failed.
                        finalized = self._finalize_dataset()
                        if (finalized and self._task_info.push_to_hub and
                                self._record_episode_count > 0):
                            self._upload_dataset(
                                self._task_info.tags,
                                self._task_info.private_mode)
                        return self.RECORD_COMPLETED
                else:
                    # save() returns False when it discarded the episode for
                    # re-recording (streaming frame drop); _status is now
                    # 'reset', so do NOT latch _on_saving.
                    if self.save():
                        self._proceed_time = 0
                        self._on_saving = True
            return self.RECORDING

        elif self._status == 'finish':
            if self._on_saving:
                if self._lerobot_dataset.check_video_encoding_completed():
                    self._on_saving = False
                    self._episode_reset()
                    # v0.5.1: close the data ParquetWriter + flush the episode
                    # metadata buffer to disk BEFORE upload — without this the
                    # dataset is incomplete on disk (see _finalize_dataset).
                    finalized = self._finalize_dataset()
                    if (finalized and self._task_info.push_to_hub and
                            self._record_episode_count > 0):
                        self._upload_dataset(
                            self._task_info.tags,
                            self._task_info.private_mode)
                    return self.RECORD_COMPLETED
            else:
                # save() returns False when it discarded the episode for
                # re-recording (streaming frame drop); _status is now 'reset',
                # so do NOT latch _on_saving or kick off encoding.
                if self.save():
                    if not self._single_task:
                        self._lerobot_dataset.video_encoding()
                    self._proceed_time = 0
                    self._on_saving = True

        if self._record_episode_count >= self._task_info.num_episodes:
            if self._lerobot_dataset.check_video_encoding_completed():
                # v0.5.1: flush writers to disk BEFORE upload (see
                # _finalize_dataset) — this is the auto-complete path that fires
                # once the target episode count is reached.
                finalized = self._finalize_dataset()
                if (finalized and self._task_info.push_to_hub and
                        self._record_episode_count > 0):
                    self._upload_dataset(
                        self._task_info.tags,
                        self._task_info.private_mode)
                return self.RECORD_COMPLETED

        return self.RECORDING

    def save(self) -> bool:
        """Commit the in-flight episode to disk.

        Returns True when the episode was committed (or there was nothing to
        commit), False when the episode was DISCARDED for automatic re-recording
        because the streaming encoder dropped frames (see
        _discard_episode_for_redo). Callers must only latch ``_on_saving = True``
        when this returns True — a False return has already routed the state
        machine back to 'reset'.
        """
        if self._lerobot_dataset.episode_buffer is None:
            return True
        # Validate the buffer BEFORE save() consumes it. Logs to stderr only —
        # validation never blocks the actual save.
        try:
            self._validate_episode_buffer()
        except Exception as e:
            print(
                f'[WARNUNG] Episode-Prüfung fehlgeschlagen (nicht kritisch): {e}',
                file=sys.stderr, flush=True,
            )
        # Streaming frame-drop guard: with streaming_encoding=True the encoder
        # silently drops camera frames under CPU overload while add_frame still
        # appended a parquet row each tick — so the encoded video would be
        # SHORTER than the data parquet and LeRobot would raise
        # FrameTimestampError at train time. The encoder's per-episode drop
        # counter is still valid here (start_episode cleared it on this episode's
        # first frame; finish_episode hasn't run yet), so detect it BEFORE
        # save_episode() commits and re-record the episode instead of shipping a
        # desynced one. (This is detect-before-commit — strictly safer than the
        # post-commit warning in _verify_saved_video_files.)
        try:
            dropped = self._lerobot_dataset.streaming_dropped_frame_count()
        except Exception:  # noqa: BLE001 — detection must never block recording
            dropped = 0
        if dropped > 0:
            self._discard_episode_for_redo(dropped)
            return False
        # v2.5.0: with streaming_encoding=True + parallel_encoding=False the
        # video files are fully written by the time save_episode() returns, so
        # there is no async encoder snapshot to take here. The mp4s are checked
        # for real, after the save, in _verify_saved_video_files() against the
        # v3.0 on-disk layout — see that method.
        if self._task_info.use_optimized_save_mode:
            if not self._single_task:
                self._lerobot_dataset.save_episode_without_video_encoding()
            else:
                self._lerobot_dataset.save_episode_without_write_image()
        else:
            if self._lerobot_dataset.episode_buffer['size'] > 0:
                self._lerobot_dataset.save_episode()
        return True

    def _discard_episode_for_redo(self, dropped_frames: int) -> None:
        """Discard the current in-flight episode and route to re-recording it.

        Called from save() when the streaming encoder dropped frames: the video
        for this episode would be shorter than its parquet rows. We cancel the
        streaming episode (drops the temp mp4), clear the in-RAM buffer, and set
        _status='reset' so the SAME episode index is re-recorded — the episode
        count is NOT incremented, so nothing desynced ever reaches disk. Mirrors
        re_record()'s transition.
        """
        episode_no = self._record_episode_count + 1
        warning = (
            f'Episode {episode_no}: {dropped_frames} Kamera-Bild(er) gingen '
            f'beim Speichern verloren (Video-Encoder überlastet). Video und '
            f'Daten wären nicht synchron und das Training würde abbrechen — '
            f'die Episode wird automatisch neu aufgenommen.'
        )
        self._last_warning_message = warning
        print(f'[WARNUNG] {warning}', file=sys.stderr, flush=True)
        try:
            self._lerobot_dataset.cancel_streaming_episode()
        except Exception:  # noqa: BLE001 — discard must never block recording
            pass
        self._stop_save_completed = False
        self._on_saving = False
        self._episode_reset()
        self._status = 'reset'

    def _finalize_dataset(self) -> bool:
        """Flush LeRobot's writers so the on-disk dataset is actually complete.

        LeRobot v0.5.1 keeps the data ParquetWriter open and buffers per-episode
        metadata (DatasetMetadata._metadata_buffer, default size 10) until
        finalize() runs. Without an explicit finalize() a recording of fewer
        than 10 episodes ships a data parquet with NO footer (unreadable by
        pyarrow/datasets) and NO meta/episodes/*.parquet at all — yet info.json
        and the mp4 files still look valid, so the corruption is silent and only
        surfaces when Modal training (or a local re-read) tries to load the
        dataset. Must run once after the last save_episode() and before upload.
        Idempotent: LeRobotDataset.finalize() guards on its _is_finalized flag.

        Returns True when the dataset is finalized (or was already); False when
        finalize() raised — in which case the caller MUST skip the upload, since
        the on-disk files would be incomplete.
        """
        ds = self._lerobot_dataset
        if ds is None:
            return False
        try:
            ds.finalize()
            # The session completed cleanly — drop the crash marker so the
            # next boot raises no stale-session notice.
            self._clear_session_marker()
            return True
        except Exception as e:
            warning = (
                'Datensatz konnte nicht abgeschlossen werden — die Aufnahme '
                'ist unvollständig und muss neu aufgenommen werden.'
            )
            self._last_warning_message = warning
            print(f'[FEHLER] {warning} ({e})', file=sys.stderr, flush=True)
            return False

    def _verify_saved_video_files(self):
        """After save_episode() returns, verify each camera produced a
        non-empty video file on disk.

        LeRobot v0.5.1 (dataset codebase v3.0) writes one *concatenated* mp4
        per video key at <root>/videos/<video_key>/chunk-NNN/file-NNN.mp4 —
        several episodes share a file, so the exact per-episode filename is not
        predictable from here. (The pre-v2.5.0 v2.1 layout
        videos/chunk-NNN/<key>/episode_NNNNNN.mp4 no longer exists; checking
        it produced false-positive "muss neu aufgenommen werden" errors on
        every single episode.) We therefore verify, per camera, that the key's
        video directory holds at least one non-empty .mp4 — which still catches
        the catastrophic "no video was written at all" case without
        false-positiving on the concatenated layout. The warning is surfaced in
        German on TaskStatus.error; it never blocks the save.
        """
        ds = self._lerobot_dataset
        if ds is None:
            return
        try:
            root = Path(str(getattr(ds, 'root', '') or ''))
            meta = getattr(ds, 'meta', None)
            video_keys = list(getattr(meta, 'video_keys', []) or []) if meta else []
        except Exception:
            return
        if not str(root) or not video_keys:
            return
        missing: list = []
        for key in video_keys:
            key_dir = root / 'videos' / key
            try:
                has_video = key_dir.is_dir() and any(
                    p.is_file() and p.stat().st_size > 0
                    for p in key_dir.rglob('*.mp4')
                )
            except OSError:
                has_video = False
            if not has_video:
                missing.append(key.replace('observation.images.', ''))
        if missing:
            warning = (
                f'Episode {self._record_episode_count + 1}: Für Kamera(s) '
                f'{missing} wurde keine Video-Datei gespeichert. Diese Episode '
                f'muss neu aufgenommen werden, sonst ist das Training '
                f'unbrauchbar.'
            )
            self._last_warning_message = warning
            print(f'[FEHLER] {warning}', file=sys.stderr, flush=True)

    def _validate_episode_buffer(self):
        """Inspect the in-memory episode buffer for silent data loss.

        Checks frame timestamp gaps larger than 2x the expected frame interval —
        usually a camera publisher hiccup or callback starvation.

        Findings are logged in German for the student-facing operator UI;
        they never block the save.
        """
        buf = self._lerobot_dataset.episode_buffer
        if buf is None:
            return

        episode_no = self._record_episode_count + 1
        fps = getattr(self._task_info, 'fps', None)
        expected_dt = (1.0 / fps) if fps and fps > 0 else None

        # Timestamp gaps inside the buffer.
        timestamps = buf.get('timestamp')
        if (
            expected_dt is not None
            and isinstance(timestamps, list)
            and len(timestamps) >= 2
        ):
            threshold = 2.0 * expected_dt
            gaps = []
            for i in range(1, len(timestamps)):
                try:
                    dt = float(timestamps[i]) - float(timestamps[i - 1])
                except (TypeError, ValueError):
                    continue
                if dt > threshold:
                    gaps.append((i, dt))
            if gaps:
                # Limit the report to the worst few so we don't spam stderr.
                gaps.sort(key=lambda g: g[1], reverse=True)
                worst = gaps[:3]
                summary = ', '.join(
                    f'Frame {idx}: {dt * 1000:.0f} ms' for idx, dt in worst
                )
                print(
                    f'[WARNUNG] Episode {episode_no}: {len(gaps)} '
                    f'Zeitlücken erkannt (erwartet ~{expected_dt * 1000:.0f} ms '
                    f'pro Frame). Größte Lücken: {summary}. '
                    f'Mögliche Ursache: Kamera oder Sensor hat Frames verloren.',
                    file=sys.stderr, flush=True,
                )

    def create_frame(
            self,
            images: dict,
            state: list,
            action: list) -> dict:

        frame = {}
        for camera_name, image in images.items():
            frame[f'observation.images.{camera_name}'] = image
        frame['observation.state'] = np.array(state, dtype=np.float32)
        frame['action'] = np.array(action, dtype=np.float32)
        self.current_instruction = self._task_info.task_instruction[
            self._current_task % len(self._task_info.task_instruction)
        ]
        return frame

    def record_early_save(self):
        if self._lerobot_dataset.episode_buffer is not None:
            self._status = 'save'

    def record_stop(self):
        self._status = 'stop'

    def record_finish(self):
        self._status = 'finish'

    def re_record(self):
        self._stop_save_completed = False
        # Abandon any in-flight save: re_record means "discard the current episode and
        # restart it". If a collision (or a manual Wiederholen) fires while _status=='save'
        # with _on_saving latched True, leaving it set would make the NEXT 'save' tick skip
        # save() and jump straight to the encoding-complete branch — counting an episode whose
        # frames were never written. Clearing it here keeps the re-recorded episode honest.
        self._on_saving = False
        self._episode_reset()
        self._status = 'reset'

    def record_skip_task(self):
        self._stop_save_completed = False
        self._episode_reset()
        self._status = 'skip_task'
        self._get_current_scenario_number()
        self._current_task += 1

    def record_next_episode(self):
        self._status = 'save'

    def get_current_record_status(self):
        current_status = TaskStatus()
        current_status.robot_type = self._robot_type
        current_status.task_info = self._task_info

        if self._status == 'warmup':
            current_status.phase = TaskStatus.WARMING_UP
            current_status.total_time = int(self._task_info.warmup_time_s)
        elif self._status == 'run':
            current_status.phase = TaskStatus.RECORDING
            current_status.total_time = int(self._task_info.episode_time_s)
        elif self._status == 'reset':
            current_status.phase = TaskStatus.RESETTING
            current_status.total_time = int(self._task_info.reset_time_s)
        elif self._status == 'save' or self._status == 'finish':
            is_saving, encoding_progress = self._get_encoding_progress()
            current_status.phase = TaskStatus.SAVING
            current_status.total_time = int(0)
            self._proceed_time = int(0)
            if is_saving:
                current_status.encoding_progress = encoding_progress
            else:
                current_status.encoding_progress = 0.0
        elif self._status == 'stop':
            is_saving, encoding_progress = self._get_encoding_progress()
            current_status.total_time = int(0)
            self._proceed_time = int(0)
            if is_saving:
                current_status.phase = TaskStatus.SAVING
                current_status.encoding_progress = encoding_progress
            else:
                current_status.phase = TaskStatus.STOPPED

        current_status.current_task_instruction = self.current_instruction
        current_status.proceed_time = int(getattr(self, '_proceed_time', 0))
        current_status.current_episode_number = int(self._record_episode_count)

        # Propagate the last non-fatal warning (e.g. RAM truncation) into
        # TaskStatus.error with a [WARNUNG] prefix so the React UI can
        # render it distinctly from hard errors. Without this, truncation
        # was invisible to the student. Clear-on-read so a persistent
        # warning surfaces exactly once per occurrence: if a new
        # truncation/mismatch happens, the warning is re-set by record()
        # and re-surfaced on the next status tick.
        if self._last_warning_message:
            current_status.error = f'[WARNUNG] {self._last_warning_message}'
            self._last_warning_message = ''

        total_storage, used_storage = StorageChecker.get_storage_gb('/')
        current_status.used_storage_size = float(used_storage)
        current_status.total_storage_size = float(total_storage)

        current_status.used_cpu = float(self._cpu_checker.get_cpu_usage())

        ram_total, ram_used = RAMChecker.get_ram_gb()
        current_status.used_ram_size = float(ram_used)
        current_status.total_ram_size = float(ram_total)
        if not self._single_task:
            current_status.current_scenario_number = self._current_scenario_number

        return current_status

    def _get_current_scenario_number(self):
        task_count = len(self._task_info.task_instruction)
        if task_count == 0:
            return
        next_task_index = (self._current_task + 1) % task_count
        if next_task_index == 0:
            self._current_scenario_number += 1

    def _get_encoding_progress(self):
        # v2.5.0: streaming_encoding=True + parallel_encoding=False means
        # save_episode() encodes synchronously and only returns once the
        # episode's video is fully written. There is no async per-camera
        # encoder to poll (the v2.4 self.encoders dict is gone in LeRobot
        # v0.5.1), so the SAVING phase is effectively instantaneous. Report
        # "not saving / 100%" so the React UI never renders a progress bar
        # that can't move.
        return False, 100.0

    def _check_stale_cameras(self, camera_data: dict) -> str | None:
        """Hash sparse byte slices of each decoded camera frame to detect
        a frozen feed. Mirrors overlays/inference_manager.py logic so
        recording and inference treat dead cameras the same way. Returns
        the camera name once it has been frozen >_stale_halt_threshold_s,
        or None when fresh.
        """
        now = time.monotonic()
        halt_on: str | None = None
        for name, img in camera_data.items():
            # v2.5.0: hash the decoded RGB ndarray's bytes. Streaming encoding
            # means the recording buffer never holds compressed JPEG, so the
            # JpegFrame branch is gone.
            buf = img.tobytes() if hasattr(img, 'tobytes') else bytes(img)
            n = len(buf)
            if n <= 1024:
                sample = buf
            else:
                slice_size = 256
                offsets = (0, n // 4, n // 2, (3 * n) // 4)
                sample = b''.join(buf[o:o + slice_size] for o in offsets)
            h = hash(sample)
            prev = self._last_image_hashes.get(name)
            if prev != h:
                self._last_image_hashes[name] = h
                self._last_image_change_time[name] = now
                continue
            last_change = self._last_image_change_time.get(name, now)
            if now - last_change > self._stale_halt_threshold_s and halt_on is None:
                halt_on = name
        return halt_on

    def convert_msgs_to_raw_datas(
            self,
            image_msgs,
            follower_msgs,
            total_joint_order,
            leader_msgs=None,
            leader_joint_order=None) -> tuple:

        camera_data = {}
        follower_data = []
        leader_data = []

        if image_msgs is not None:
            for key, value in image_msgs.items():
                # v2.5.0: always decode to RGB ndarray. streaming_encoding=True
                # bounds in-RAM growth at the encoder boundary, so the v2.4
                # JpegFrame compressed-bytes optimization is no longer needed.
                # cv_bridge handles the BGR→RGB swap in-decoder when we ask
                # for rgb8, saving one full-frame allocation + memcpy per
                # camera per tick.
                camera_data[key] = self.data_converter.compressed_image2cvmat(
                    value, desired_encoding='rgb8')
            stale = self._check_stale_cameras(camera_data)
            if stale is not None:
                # Warn (don't halt) — slow precision demos legitimately
                # produce static scenes for >5 s (insertion, alignment,
                # waiting for a human to place an object). Aborting the
                # episode here was the single most-frequent false
                # positive against real workflows; at recording time
                # the worst case is a degraded frame, not a hardware
                # event. (The inference path currently has NO stale-
                # camera halt of its own — this warning fires there too
                # via convert_msgs_to_raw_datas, but is warn-only.)
                warning = (
                    f'Kamera "{stale}" liefert seit über '
                    f'{self._stale_halt_threshold_s:.0f}s dasselbe Bild. '
                    f'Aufnahme läuft weiter — bitte prüfen, ob die '
                    f'Szene wirklich statisch ist oder die Kamera hängt.'
                )
                self._last_warning_message = warning
                print(f'[WARNUNG] {warning}', file=sys.stderr, flush=True)
        if follower_msgs is not None:
            for key, value in follower_msgs.items():
                if value is not None:
                    follower_data.extend(self.joint_msgs2tensor_array(
                        value, total_joint_order))
        if leader_msgs is not None:
            for key, value in leader_joint_order.items():
                # remove joint_order. from key
                prefix_key = key.replace('joint_order.', '')
                if prefix_key not in leader_msgs:
                    return camera_data, follower_data, None
                elif leader_msgs[prefix_key] is not None:
                    leader_data.extend(self.joint_msgs2tensor_array(
                        leader_msgs[prefix_key], value))
                else:
                    return camera_data, follower_data, None

        return camera_data, follower_data, leader_data

    def joint_msgs2tensor_array(self, msg_data, joint_order=None):
        if isinstance(msg_data, JointTrajectory):
            return self.data_converter.joint_trajectory2tensor_array(
                msg_data, joint_order)
        elif isinstance(msg_data, JointState):
            return self.data_converter.joint_state2tensor_array(
                msg_data, joint_order)
        elif isinstance(msg_data, Odometry):
            return self.data_converter.odometry2tensor_array(msg_data)
        elif isinstance(msg_data, Twist):
            return self.data_converter.twist2tensor_array(msg_data)
        else:
            raise ValueError(f'Unsupported message type: {type(msg_data)}')

    def _episode_reset(self):
        if (
            self._lerobot_dataset
            and (hasattr(self._lerobot_dataset, 'episode_buffer')
                 or self._current_task == 0)
        ):
            if self._lerobot_dataset.episode_buffer is not None:
                for key, value in self._lerobot_dataset.episode_buffer.items():
                    if isinstance(value, list):
                        value.clear()
                    del value
                self._lerobot_dataset.episode_buffer.clear()
            self._lerobot_dataset.episode_buffer = None
        self._start_time_s = 0
        # Drop the stale-camera hashes so a re-recorded episode starts
        # fresh — otherwise the very first frame of the new episode would
        # always be flagged "same as last frame of previous episode" and
        # immediately advance the stale clock.
        self._last_image_hashes.clear()
        self._last_image_change_time.clear()
        # NOTE: _last_warning_message is deliberately NOT cleared here.
        # _episode_reset() runs inside the same record() tick that set the
        # warning (RAM truncation -> record_early_save -> save -> encoding
        # complete -> _episode_reset), so clearing here would wipe the
        # warning before get_current_record_status() — called from the
        # outer ROS timer — ever surfaces it to the UI. Instead, the
        # warning is cleared in get_current_record_status() after it has
        # been copied onto TaskStatus.error, which guarantees the student
        # sees it at least once.
        gc.collect()

    def _check_time(self, limit_time, next_status):
        self._proceed_time = time.perf_counter() - self._start_time_s
        if self._proceed_time >= limit_time:
            self._status = next_status
            self._start_time_s = 0
            self._proceed_time = 0
            return True
        else:
            return False

    def _check_dataset_exists(self, repo_id, root):
        # Local dataset check
        if os.path.exists(root):
            dataset_necessary_folders = ['meta', 'videos', 'data']
            invalid_foler = False
            for folder in dataset_necessary_folders:
                if not os.path.exists(os.path.join(root, folder)):
                    print(f'Dataset {repo_id} is incomplete, missing {folder} folder.')
                    invalid_foler = True
            if not invalid_foler:
                return True
            else:
                print(f'Dataset {repo_id} is incomplete, re-creating dataset.')
                shutil.rmtree(root)

        if self._task_info.push_to_hub:
            # Huggingface dataset check
            url = f'https://huggingface.co/api/datasets/{repo_id}'
            response = requests.get(url)
            url_exist_code = 200

            if response.status_code == url_exist_code:
                print(f'Dataset {repo_id} exists on Huggingface, downloading...')
                self._download_dataset(repo_id)
                return True

        return False

    def check_lerobot_dataset(self, images, joint_list):
        try:
            if self._lerobot_dataset is None:
                if self._check_dataset_exists(
                        self._save_repo_name,
                        self._save_path):
                    self._lerobot_dataset = LeRobotDatasetWrapper(
                        self._save_repo_name,
                        self._save_path
                    )
                else:
                    self._lerobot_dataset = self._create_dataset(
                        self._save_repo_name,
                        images, joint_list)

                if not self._task_info.use_optimized_save_mode:
                    self._lerobot_dataset.start_image_writer(
                            num_processes=1,
                            num_threads=1
                        )
            self._lerobot_dataset.set_robot_type(self._robot_type)
            return True
        except Exception as e:
            print(f'Error checking lerobot dataset: {e}')
            return False

    def _create_dataset(
            self,
            repo_id,
            images,
            joint_list) -> LeRobotDatasetWrapper:

        features = DEFAULT_FEATURES.copy()
        for camera_name, image in images.items():
            features[f'observation.images.{camera_name}'] = {
                'dtype': 'video',
                'names': ['height', 'width', 'channels'],
                'shape': image.shape
            }

        features['observation.state'] = {
            'dtype': 'float32',
            'names': joint_list,
            'shape': (len(joint_list),)
        }

        features['action'] = {
            'dtype': 'float32',
            'names': joint_list,
            'shape': (len(joint_list),)
        }
        return LeRobotDatasetWrapper.create(
                repo_id=repo_id,
                fps=self._task_info.fps,
                features=features,
                use_videos=True
            )

    def _upload_dataset(self, tags, private=True):
        """Auto-push the recorded dataset to HuggingFace.

        Prefers the HfApiWorker callback (wired by the node) so the
        upload runs out-of-process: the ROS spin thread stays
        responsive, progress + Success/Failed events flow through
        /huggingface/status (German toasts in the React UI), and a
        successful upload triggers the React side to call
        /datasets/register on the Cloud API — without that registration,
        Modal training cannot discover the dataset.

        Falls back to a direct push_to_hub when no callback was wired
        (tests, standalone import). ``private`` is forwarded from the
        student's "Privater Modus" choice in the React UI (TaskInfo
        .private_mode). It defaults to True so a missing/garbled flag
        fails safe to private — classroom recordings can contain
        children's faces / audio. A student may opt a repo public at
        record time; teachers can also flip individual repos later from
        the HF dashboard.

        The signature said ``private=False`` until 2026-08-06, flatly
        contradicting the paragraph above. That default is only ONE of the two
        layers: the operative one is ``TaskInfo.msg``, where ``bool
        private_mode`` carried NO default and ROS 2 booleans default to FALSE —
        so any rosbridge client that simply OMITTED the field got a PUBLIC repo
        of children's faces. React always sends ``private_mode: true``, which is
        what kept this latent. Both layers now default to private; keep them
        in lockstep (fenced by test_upload_privacy_fails_safe.py).
        """
        private = bool(private)
        if self._upload_enqueued:
            # Already kicked off; subsequent state-machine ticks are no-ops.
            return
        self._upload_enqueued = True

        # Never upload into a namespace the rig's own HF token does not own.
        # _save_repo_name is built from the CLIENT-SUPPLIED task_info.user_id,
        # so without this an unauthenticated rosbridge client could name any
        # namespace at all — and after a student handover the previous
        # student's id was still in play. Marked enqueued ABOVE first, so a
        # refusal cannot spin the state machine re-attempting every tick.
        allowed = self._rig_hf_namespaces()
        namespace = (self._save_repo_name or '').split('/')[0]
        if allowed is not None and namespace not in allowed:
            self._last_warning_message = (
                f'Upload abgelehnt: Der Roboter darf nicht in das '
                f'HuggingFace-Konto „{namespace}" hochladen. Bitte die '
                f'Benutzer-ID prüfen und erneut anmelden.'
            )
            print(
                f'[FEHLER] Upload REFUSED: repo namespace {namespace!r} is not '
                f'owned by this rig\'s HuggingFace token (owns: {sorted(allowed)})',
                file=sys.stderr, flush=True,
            )
            return
        if self._upload_callback is not None:
            try:
                self._upload_callback(
                    self._save_repo_name,
                    str(self._save_path),
                    private,
                )
            except Exception as e:
                print(
                    f'[WARNUNG] Upload konnte nicht eingereiht werden: {e}',
                    file=sys.stderr, flush=True,
                )
            return

        # Standalone fallback — no progress events, no auto-register.
        try:
            self._lerobot_dataset.push_to_hub(
                tags=tags,
                private=private,
                upload_large_folder=True)
        except Exception as e:
            print(
                f'[WARNUNG] Direkter Hub-Upload fehlgeschlagen: {e}',
                file=sys.stderr, flush=True,
            )

    def _download_dataset(self, repo_id):
        snapshot_download(
            repo_id,
            repo_type='dataset',
            local_dir=self._save_path,
        )

    def convert_action_to_joint_trajectory_msg(self, action):
        joint_trajectory_msgs = self.data_converter.tensor_array2joint_trajectory(
            action,
            self.total_joint_order)
        return joint_trajectory_msgs

    def get_task_info(self):
        return self._task_info

    def _init_task_limits(self):
        if not self._single_task:
            self._task_info.num_episodes = 1_000_000
            self._task_info.episode_time_s = 1_000_000

    # Namespaces the rig's own HF token may write to (its account + orgs).
    # Cached at CLASS level because it is a property of the RIG's token, not of
    # a recording: whoami is an 8 s-bounded network call and _upload_dataset
    # runs on the end-of-recording save path, which is already busy.
    # Invalidated by register_huggingface_token.
    _hf_namespace_cache = None

    @classmethod
    def invalidate_hf_namespace_cache(cls):
        cls._hf_namespace_cache = None

    @classmethod
    def _rig_hf_namespaces(cls):
        """Namespaces this rig's token owns, or None when unknowable.

        None means "cannot judge" — no token registered, whoami timed out, the
        school network is down. The caller then ALLOWS the upload, deliberately:

          * recording with no cloud login is a fully supported path (only
            Training and Inferenz gate on a session), so a hard gate here would
            break it;
          * with no token the upload fails on its own anyway, so nothing is
            actually published;
          * and turning a transient network blip into a destroyed upload is a
            worse outcome than the case this guard exists for.

        This is a REFUSE-ON-PROOF gate, matching hf_token_is_foreign's
        treatment of an absent stamp.
        """
        if cls._hf_namespace_cache is not None:
            return cls._hf_namespace_cache
        try:
            ids = cls.get_huggingface_user_id()
        except Exception:  # noqa: BLE001 — no token / network / HF outage
            return None
        if not ids:
            return None
        cls._hf_namespace_cache = frozenset(ids)
        return cls._hf_namespace_cache

    @staticmethod
    def get_robot_type_from_info_json(info_json_path):
        with open(info_json_path, 'r', encoding='utf-8') as f:
            info = json.load(f)
        return info.get('robot_type', '')

    @staticmethod
    def get_huggingface_user_id():
        def api_call():
            api = HfApi()
            try:
                user_info = api.whoami()
                user_ids = [user_info['name']]
                for org_info in user_info['orgs']:
                    user_ids.append(org_info['name'])
                return user_ids
            except LocalTokenNotFoundError as e:
                print(f'No registered HuggingFace token found: {e}')
                raise Exception('No registered HuggingFace token found')
            except Exception as e:
                print(f'Token validation failed: {e}')
                raise

        # Use queue to get result from thread
        result_queue = queue.Queue()

        def worker():
            try:
                result = api_call()
                result_queue.put(('success', result))
            except Exception as e:
                result_queue.put(('error', e))

        # Start thread and wait with timeout
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        try:
            # Wait for the whoami result. 8 s (was 1.5 s) so the Benutzer-ID
            # list still loads on a slow/cold school network instead of
            # silently returning an empty list. Safe to block this long: the
            # HF services run in their own ReentrantCallbackGroup
            # (physical_ai_server._init_ros_service), so this wait no longer
            # stalls the heartbeat / task-status timers.
            status, data = result_queue.get(timeout=8.0)
            if status == 'success':
                if data:
                    print(data)
                return data
            else:
                raise data
        except queue.Empty:
            print('HuggingFace whoami timed out after 8 seconds')
            return None

    @staticmethod
    def register_huggingface_token(hf_token):
        def validate_token():
            api = HfApi(token=hf_token)
            try:
                user_info = api.whoami()
                user_name = user_info['name']
                print(f'Successfully validated HuggingFace token for user: {user_name}')
                return True
            except Exception as e:
                print(f'Token is invalid, please check hf token: {e}')
                return False

        # Use queue to get result from thread
        result_queue = queue.Queue()

        def worker():
            result = validate_token()
            result_queue.put(result)

        # Start thread and wait with timeout
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        try:
            # Wait for result with 1.5 second timeout
            is_valid = result_queue.get(timeout=1.5)
            if not is_valid:
                return False
        except queue.Empty:
            print('Token validation timed out after 1.5 seconds')
            return False

        try:
            result = subprocess.run([
                'huggingface-cli', 'login', '--token', hf_token
            ], capture_output=True, text=True, check=True)

            # The rig's identity just changed, so the cached namespace
            # allowlist in _rig_hf_namespaces is stale. Without this a token
            # swap would keep refusing (or keep permitting) against the old
            # account for the life of the node.
            DataManager.invalidate_hf_namespace_cache()
            print('Successfully logged in to HuggingFace Hub')
            return result

        except subprocess.CalledProcessError as e:
            print(f'Failed to login with huggingface-cli: {e}')
            print(f'Error output: {e.stderr}')
            return False
        except FileNotFoundError:
            print('huggingface-cli not found. Please install package.')
            return False

    @staticmethod
    def download_huggingface_repo(
        repo_id,
        repo_type='dataset'
    ):
        download_path = {
            'dataset': Path.home() / '.cache/huggingface/lerobot',
            # v2.5.0: the LeRobot install is pip-managed from PyPI and the
            # vendored ros2_ws/src/physical_ai_tools/lerobot tree is stripped
            # by the image build, so model downloads must NOT target a path
            # under it. Use a stable outputs dir outside the stripped tree.
            'model': Path.home() / 'ros2_ws/outputs/train/'
        }

        save_path = download_path.get(repo_type)

        if save_path is None:
            raise ValueError(f'Invalid repo type: {repo_type}')

        save_dir = save_path / repo_id

        DataManager._last_hf_failure_reason_de = None
        try:
            print(f'Starting download of {repo_id} ({repo_type})...')

            # Create a wrapper class that includes the progress_queue
            class ProgressTqdmWrapper(HuggingFaceProgressTqdm):

                def __init__(self, *args, **kwargs):
                    kwargs['progress_queue'] = DataManager._progress_queue
                    super().__init__(*args, **kwargs)

            result = snapshot_download(
                repo_id=repo_id,
                repo_type=repo_type,
                local_dir=save_dir,
                tqdm_class=ProgressTqdmWrapper
            )

            print(f'Download completed: {repo_id}')
            return result
        except Exception as e:
            print(f'Error downloading HuggingFace repo: {e}')
            # Print more detailed error information
            import traceback
            print(f'Detailed error traceback:\n{traceback.format_exc()}')
            DataManager._last_hf_failure_reason_de = (
                DataManager._classify_hf_failure_de(e)
            )
            return False

    @classmethod
    def set_progress_queue(cls, progress_queue):
        """Set progress queue for multiprocessing communication."""
        cls._progress_queue = progress_queue

    @staticmethod
    def _create_dataset_card(local_dir, readme_path):
        """
        Create DatasetCard README for dataset repository.

        Args:
        ----
        local_dir: Local directory path containing dataset
        readme_path: Path where README.md will be saved

        """
        # Load meta/info.json for dataset structure info
        info_path = Path(local_dir) / 'meta' / 'info.json'
        dataset_info = None
        if info_path.exists():
            with open(info_path, 'r', encoding='utf-8') as f:
                dataset_info = json.load(f)

        # Prepare tags
        tags = ['robotis', 'LeRobot']
        robot_type = DataManager.get_robot_type_from_info_json(info_path)
        if robot_type and robot_type != '':
            tags.append(robot_type)

        # Create DatasetCardData
        card_data = DatasetCardData(
            license='apache-2.0',
            tags=tags,
            task_categories=['robotics'],
            configs=[
                {
                    'config_name': 'default',
                    'data_files': 'data/*/*.parquet',
                }
            ],
        )

        # Prepare dataset structure section
        dataset_structure = ''
        if dataset_info:
            dataset_structure = '[meta/info.json](meta/info.json):\n'
            dataset_structure += '```json\n'
            info_json = json.dumps(dataset_info, indent=4)
            dataset_structure += f'{info_json}\n'
            dataset_structure += '```\n'

        # Get template path
        template_dir = Path(__file__).parent
        template_path = str(template_dir / 'dataset_card_template.md')

        # Create card from template
        card = DatasetCard.from_template(
            card_data,
            template_path=template_path,
            dataset_structure=dataset_structure,
            license='apache-2.0',
        )
        card.save(str(readme_path))
        print('✅ Dataset README.md created using HuggingFace Hub')

    @staticmethod
    def _create_model_card(local_dir, readme_path):
        """
        Create ModelCard README for model repository.

        Args:
        ----
        local_dir: Local directory path containing model
        readme_path: Path where README.md will be saved

        """
        # Find train_config.json (check common locations first)
        train_config = None
        common_paths = [
            Path(local_dir) / 'train_config.json',
            Path(local_dir) / 'config' / 'train_config.json',
            Path(local_dir) / 'pretrained_model' / 'train_config.json',
        ]

        # Check common paths first (fast)
        for config_path in common_paths:
            if config_path.exists():
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        train_config = json.load(f)
                    print(f'✓ Found train_config.json at {config_path}')
                    break
                except Exception as e:
                    print(f'⚠️ Error reading {config_path}: {e}')
                    continue

        # If not found, search recursively (slower fallback)
        if train_config is None:
            for config_path in Path(local_dir).rglob('train_config.json'):
                try:
                    with open(config_path, 'r', encoding='utf-8') as f:
                        train_config = json.load(f)
                    print(f'✓ Found train_config.json at {config_path}')
                    break
                except Exception as e:
                    print(f'⚠️ Error reading {config_path}: {e}')
                    continue

        if train_config is None:
            print(f'⚠️ train_config.json not found in {local_dir}')

        dataset_repo = ''
        if train_config:
            dataset_repo = train_config.get(
                'dataset', {}
            ).get('repo_id', '')

        # Prepare tags
        tags = ['robotis', 'robotics']

        # Create ModelCardData with conditional datasets
        card_data_kwargs = {
            'license': 'apache-2.0',
            'tags': tags,
            'pipeline_tag': 'robotics',
        }
        if dataset_repo:
            card_data_kwargs['datasets'] = [dataset_repo]

        card_data = ModelCardData(**card_data_kwargs)

        # Get template path
        template_dir = Path(__file__).parent
        template_path = str(template_dir / 'model_card_template.md')

        # Create card from template
        card = ModelCard.from_template(
            card_data,
            template_path=template_path,
        )
        card.save(str(readme_path))
        print('✅ Model README.md created using HuggingFace Hub')

    @staticmethod
    def _create_readme_if_not_exists(local_dir, repo_type):
        """
        Create README.md file if it doesn't exist in the folder.

        Uses HuggingFace Hub's DatasetCard or ModelCard.

        """
        readme_path = Path(local_dir) / 'README.md'

        if readme_path.exists():
            print(f'README.md already exists in {local_dir}')
            return

        print(f'Creating README.md in {local_dir}')

        try:
            if repo_type == 'dataset':
                DataManager._create_dataset_card(local_dir, readme_path)
        except Exception as e:
            print(f'⚠️ Warning: Failed to create README.md: {e}')
            import traceback
            print(f'Traceback: {traceback.format_exc()}')

    # Student-facing German explanation for an invalid/expired HF token —
    # MUST point at the GUI token field ("Schritt D"), never at `hf auth
    # login` (the token lives in the host .env, set once in the GUI; there
    # is deliberately NO in-app token UI). leLab-comparison PR-1.
    HF_AUTH_ERROR_DE = (
        'Hugging Face-Token ungültig oder abgelaufen. Bitte in der '
        'EduBotics-App unter „Schritt D: HuggingFace-Token" einen gültigen '
        'Token speichern und die Umgebung neu starten.'
    )

    # German failure reason of the most recent upload/download attempt.
    # The HfApiWorker calls upload/download IN ITS OWN PROCESS and reads
    # this immediately after a falsy return (single-threaded loop), so the
    # class attribute is a safe side-channel that keeps the long-standing
    # bool/path return contracts intact.
    _last_hf_failure_reason_de = None

    @staticmethod
    def _classify_hf_failure_de(error_text):
        """Map an HF exception text to a precise German reason (or None)."""
        lowered = str(error_text).lower()
        auth_markers = (
            '401',
            'unauthorized',
            'authentication',
            'authenticated',
            'invalid user token',
            'invalid token',
            'huggingfacehub_token',
            'token is required',
        )
        if any(marker in lowered for marker in auth_markers):
            return DataManager.HF_AUTH_ERROR_DE
        return None

    @staticmethod
    def upload_huggingface_repo(
        repo_id,
        repo_type,
        local_dir,
        private=True,
    ):
        DataManager._last_hf_failure_reason_de = None
        try:
            api = HfApi()

            # Verify authentication first
            try:
                user_info = api.whoami()
                print(f'Authenticated as: {user_info["name"]}')
            except Exception as auth_e:
                print(f'Authentication failed: {auth_e}')
                print('Please make sure you are authenticated with HuggingFace')
                # whoami failing IS the auth failure — no substring guessing.
                DataManager._last_hf_failure_reason_de = (
                    DataManager.HF_AUTH_ERROR_DE
                )
                return False

            # Repository visibility follows the student's "Privater Modus"
            # choice (forwarded from TaskInfo.private_mode). Defaults to
            # PRIVATE so a missing flag fails safe: student recordings may
            # contain faces, classroom audio, or other data that must not
            # leak to the public HF index (GDPR / school DPA). When the
            # student opts public, the repo is created public; teachers can
            # still flip any repo's visibility later from the HF dashboard.
            # NOTE: exist_ok=True only verifies an existing repo — it does
            # not retroactively change visibility, so the flag only takes
            # effect on the first (creating) upload of a given repo_id.
            private = bool(private)
            print(
                f'Creating HuggingFace repository: {repo_id} '
                f'(private={private})'
            )
            url = api.create_repo(
                repo_id,
                repo_type=repo_type,
                private=private,
                exist_ok=True,
            )
            print(f'Repository created/verified: {url}')

            # Delete .cache folder before upload
            DataManager._delete_dot_cache_folder_before_upload(local_dir)

            # Create README.md if it doesn't exist
            DataManager._create_readme_if_not_exists(
                local_dir, repo_type
            )

            print(f'Uploading folder {local_dir} to repository {repo_id}')

            # Capture stdout for logging
            from contextlib import redirect_stdout
            from .progress_tracker import HuggingFaceLogCapture

            # Use log capture with progress queue
            log_capture = HuggingFaceLogCapture(progress_queue=DataManager._progress_queue)

            with redirect_stdout(log_capture):
                # Upload folder contents
                upload_large_folder(
                    repo_id=repo_id,
                    folder_path=local_dir,
                    repo_type=repo_type,
                    print_report=True,
                    print_report_every=1,
                )

            # Post-upload maintenance for dataset repos: remote-orphan
            # sweep + version-tag re-point. Both are LOAD-BEARING for
            # training correctness, so a failure fails the whole upload
            # (the student retries; upload_large_folder resumes cheaply).
            if repo_type == 'dataset':
                if not DataManager._sync_dataset_repo_after_upload(
                        api, repo_id, local_dir):
                    return False

            return True
        except Exception as e:
            print(f'Error Uploading HuggingFace repo: {e}')
            # Print more detailed error information
            import traceback
            print(f'Detailed error traceback:\n{traceback.format_exc()}')
            DataManager._last_hf_failure_reason_de = (
                DataManager._classify_hf_failure_de(e)
            )
            return False

    # Remote paths the orphan sweep may delete under. Everything else
    # (README.md, .gitattributes, hub-managed files) is off-limits.
    _DATASET_CONTENT_PREFIXES = ('data/', 'meta/', 'videos/')

    @staticmethod
    def _sync_dataset_repo_after_upload(api, repo_id, local_dir):
        """Make the hub dataset trainable-correct after upload_large_folder.

        1. Delete remote files that no longer exist locally.
           upload_large_folder only adds/updates files; after an episode
           delete the removed data/video files survive on the hub, and
           LeRobot v3.0 loads data parquet by GLOB (io_utils.load_nested_
           dataset) — an orphaned file can occupy row positions of live
           episodes and silently corrupt training.
        2. Re-point the version tag. LeRobot 0.5.1 trains at revision
           CODEBASE_VERSION ('v3.0'); a bare create_tag 409s on the second
           upload of a repo, leaving the tag pinned to the FIRST upload's
           commit — every re-upload (edit, appended episodes) would be
           invisible to training. Mirrors upstream push_to_hub
           (delete_tag + create_tag, lerobot_dataset.py).

        Returns True on success. On failure sets the German reason
        side-channel and returns False — the upload must NOT be reported
        successful, or training would silently use a stale/mixed state.
        """
        try:
            local_files = {
                p.relative_to(local_dir).as_posix()
                for p in Path(local_dir).rglob('*')
                if p.is_file()
            }
            remote_files = api.list_repo_files(repo_id, repo_type='dataset')
            orphans = [
                f for f in remote_files
                if f.startswith(DataManager._DATASET_CONTENT_PREFIXES)
                and f not in local_files
            ]
            if orphans:
                print(
                    f'Deleting {len(orphans)} remote file(s) no longer '
                    f'present locally (first few: {orphans[:5]})'
                )
                api.create_commit(
                    repo_id=repo_id,
                    repo_type='dataset',
                    operations=[
                        CommitOperationDelete(path_in_repo=f) for f in orphans
                    ],
                    commit_message='Remove files deleted locally (EduBotics sync)',
                )
        except Exception as e:
            print(f'Error syncing remote dataset files for {repo_id}: {e}')
            DataManager._last_hf_failure_reason_de = (
                'Alte Dateien auf Hugging Face konnten nicht entfernt '
                'werden. Ohne Bereinigung würde das Training gelöschte '
                'Episoden weiterverwenden — bitte den Upload erneut '
                'versuchen.'
            )
            return False

        try:
            print(f'Re-pointing tag "{CODEBASE_VERSION}" for {repo_id}')
            try:
                api.delete_tag(
                    repo_id=repo_id, tag=CODEBASE_VERSION,
                    repo_type='dataset')
            except RevisionNotFoundError:
                pass  # first upload of this repo: no tag yet
            api.create_tag(
                repo_id=repo_id, tag=CODEBASE_VERSION, repo_type='dataset')
        except Exception as e:
            print(f'Error re-pointing version tag for {repo_id}: {e}')
            DataManager._last_hf_failure_reason_de = (
                'Der Versions-Tag des Datensatzes konnte nicht aktualisiert '
                'werden. Ohne aktuellen Tag trainiert die Cloud auf einem '
                'alten Stand — bitte den Upload erneut versuchen.'
            )
            return False
        return True

    @staticmethod
    def _delete_dot_cache_folder_before_upload(local_dir):
        dot_cache_path = Path(local_dir) / '.cache'
        if dot_cache_path.exists():
            shutil.rmtree(dot_cache_path)
            print(f'🗑️ Deleted {local_dir}/.cache folder before upload')

    @staticmethod
    def delete_huggingface_repo(
        repo_id,
        repo_type='dataset',
    ):
        try:
            result = HfApi().delete_repo(repo_id, repo_type=repo_type)
            return result
        except Exception as e:
            print(f'Error deleting HuggingFace repo: {e}')
            return False

    @staticmethod
    def get_huggingface_repo_list(
        author,
        data_type='dataset'
    ):
        repo_id_list = []
        if data_type == 'dataset':
            dataset_list = HfApi().list_datasets(author=author)
            for dataset in dataset_list:
                repo_id_list.append(dataset.id)

        elif data_type == 'model':
            model_list = HfApi().list_models(author=author)
            for model in model_list:
                repo_id_list.append(model.id)
        reverse = repo_id_list[::-1]
        return reverse

    @staticmethod
    def get_collections_repo_list(
        collection_id
    ):
        collection_list = HfApi().get_collection(collection_id)
        repo_list_in_collection = []
        for item in collection_list.items:
            repo_list_in_collection.append(item.item_id)
        return repo_list_in_collection
